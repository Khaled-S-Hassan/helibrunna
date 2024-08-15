"""
Helibrunna - A HuggingFace compatible xLSTM trainer.

Copyright (c) 2024 Dr. Tristan Behrens

All rights reserved. This software and associated documentation files (the "Software") may only be used, copied, modified, merged, published, distributed, sublicensed, and/or sold under the terms and conditions set forth by the author, Dr. Tristan Behrens.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT, OR OTHERWISE, ARISING FROM, OUT OF, OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

"""

import datetime
import os
import fire
import matplotlib.pyplot as plt
import torch
from accelerate import Accelerator
from dacite import from_dict
from datasets import load_dataset
import json
from omegaconf import OmegaConf
import shutil
from safetensors.torch import save_file
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import WhitespaceSplit
from tokenizers.trainers import WordLevelTrainer
from torch.utils.data import DataLoader
from transformers import DataCollatorForLanguageModeling
from transformers import PreTrainedTokenizerFast
from xlstm.xlstm_lm_model import xLSTMLMModel, xLSTMLMModelConfig

# Import the LinearWarmupCosineAnnealing scheduler from the experiments module.
# Source: https://github.com/NX-AI/xlstm/tree/main
if not os.path.exists("experiments/lr_scheduler.py"):
    import urllib.request
    url = "https://raw.githubusercontent.com/NX-AI/xlstm/main/experiments/lr_scheduler.py"
    os.makedirs("experiments", exist_ok=True)
    urllib.request.urlretrieve(url, "experiments/lr_scheduler.py")
from experiments.lr_scheduler import LinearWarmupCosineAnnealing


def run_training(config_path: str):
    """
    Run the training process based on the provided configuration file.
    Args:
        config_path (str): The path to the configuration file.
    Raises:
        FileNotFoundError: If the configuration file is not found.
    Returns:
        None
    """

    # Initialize the accelerator.
    print("Initializing accelerator...")
    accelerator = Accelerator()

    # Load the configuration.
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with open(config_path, "r") as f:
        config_yaml = f.read()
    config = OmegaConf.create(config_yaml)
    OmegaConf.resolve(config)

    # Set log every step to save every step.
    if "log_every_step" not in config.training:
        config.training.log_every_step = 1
    if "save_every_step" not in config.training:
        config.training.save_every_step = -1

    # Create the output directory.
    run_dir = "run_" + datetime.datetime.now().strftime("%Y%m%d-%H%M")
    output_dir = os.path.join(config.training.output_dir, run_dir)
    os.makedirs(output_dir, exist_ok=True)
    print(f"Output directory: {output_dir}")

    # Load the dataset.
    hugging_face_id = config.dataset.hugging_face_id
    print(f"Loading dataset: {hugging_face_id}")
    raw_datasets = load_dataset(hugging_face_id)
    print(raw_datasets)

    # Get the tokenizer.
    if config.tokenizer.type == "whitespace":
        print("Training whitespace tokenizer...")
        tokenizer = train_whitespace_tokenizer(raw_datasets)
    elif config.tokenizer.type == "pretrained":
        tokenizer_id = config.tokenizer.pretrained_id
        print(f"Loading pre-trained tokenizer: {tokenizer_id}...")
        tokenizer = PreTrainedTokenizerFast(tokenizer_id)
    else:
        raise ValueError(f"Unknown tokenizer type: {tokenizer.type}")
    print(f"Vocabulary size: {tokenizer.vocab_size}")

    # Assign the vocabulary size to the model configuration.
    config.model.vocab_size = tokenizer.vocab_size

    # Get the context length.
    context_length = config.model.context_length

    # Prepare the tokenized datasets.
    def tokenize_function(example):
        tokenized_example = tokenizer(
            example["text"],
            truncation=True,
            padding=False,
            max_length=context_length,
        ) 
        return {
            "input_ids": tokenized_example["input_ids"]
        } 

    # Check a sample.
    tokenized = tokenize_function(raw_datasets["train"][0])
    assert list(tokenized.keys()) == ["input_ids"], list(tokenized.keys())

    # Tokenize the datasets.
    print("Tokenizing datasets...")
    tokenized_datasets = raw_datasets.map(tokenize_function, batched=True, remove_columns=raw_datasets["train"].column_names)

    # Check a sample.
    sample = raw_datasets["train"][0]
    tokenized = tokenized_datasets["train"][0]
    assert list(tokenized.keys()) == ["input_ids"], list(tokenized.keys())
    print(f"Original text: {sample}")
    print(f"Tokenized text: {tokenized}")
        
    # Create the data collator.
    data_collator = DataCollatorForLanguageModeling(tokenizer, mlm=False)

    # Create the model.
    print("Creating model...")
    model = xLSTMLMModel(from_dict(xLSTMLMModelConfig, OmegaConf.to_container(config.model))).to(
        device=accelerator.device
    )
    model.reset_parameters()

    # Apply precision.
    training_dtype = get_torch_dtype(config.training.weight_precision)
    model = model.to(dtype=training_dtype)
    print(f"Training dtype: {training_dtype}")

    # Print the model.
    print(model)
    num_params = sum(p.numel() for p in model.parameters())
    print(f"Number of parameters: {num_params:_}")

    # Prepare the DataLoader from the tokenized dataset.
    print("Preparing DataLoader...")
    train_dataloader = DataLoader(
        tokenized_datasets["train"],
        batch_size=config.training.batch_size,
        shuffle=True,
        collate_fn=data_collator
    )

    # Prepare the optimizer and learning rate scheduler.
    optimizer_groups = model._create_weight_decay_optim_groups()
    optimizer = torch.optim.AdamW(
        (
            {"weight_decay": config.training.weight_decay, "params": optimizer_groups[0]},
            {"weight_decay": 0.0, "params": optimizer_groups[1]},
        ),
        lr=config.training.lr,
    )
    lr_scheduler = LinearWarmupCosineAnnealing(
        optimizer,
        config.training.lr_warmup_steps,
        config.training.lr_decay_until_steps,
        config.training.lr,
        config.training.lr_decay_factor * config.training.lr,
    )

    # Prepare model, optimizer, and dataloader for accelerator.
    model, optimizer, train_dataloader = accelerator.prepare(model, optimizer, train_dataloader)

    # Get some parameters.
    save_every_step = config.training.save_every_step
    log_every_step = config.training.log_every_step
    num_epochs = config.training.num_epochs
    enable_mixed_precision = config.training.enable_mixed_precision
    vocab_size = config.model.vocab_size
    wandb_project = config.training.get("wandb_project", None)
    tensorboard_dir = config.training.get("tensorboard_dir", None)

    # Get a subset of the config that includes only the model.
    model_config = OmegaConf.select(config, "model")

    # Create the readme.
    create_readme(output_dir, config)

    # Save the config as yaml and delete it.
    with open(os.path.join(output_dir, "config.yaml"), "w") as f:
        OmegaConf.save(config, f)
    del config

    # Save the tokenizer.
    tokenizer.save_pretrained(output_dir)

    # Enable wandb if requested.
    if wandb_project is not None:
        print(f"Enabling wandb logging for project: {wandb_project}")
        import wandb
        wandb.init(project=wandb_project, name=run_dir)

    # Enable tensorboard if requested.
    if tensorboard_dir is not None:
        tensorboard_dir = os.path.join(output_dir, tensorboard_dir)
        print(f"Enabling tensorboard logging in directory: {tensorboard_dir}")
        from torch.utils.tensorboard import SummaryWriter
        writer = SummaryWriter(tensorboard_dir)

    # Training loop.
    step = 0
    running_loss = []
    history = {
        "loss": [],
        "lr": [],
        "step": [],
    }
    # TODO: Use steps from config.
    for epoch in range(num_epochs):
        for batch in train_dataloader:

            # Assuming batch only contains 'input_ids'
            inputs = batch['input_ids'].to(accelerator.device)

            # Get the labels by shifting the inputs. Remove the first token. Fill the last token with 0.
            labels = torch.roll(inputs, -1, dims=1)
            labels[:, -1] = 0
            
            # Forward pass.
            model.train()
            optimizer.zero_grad()
            with torch.autocast(
                device_type=accelerator.device.type,
                dtype=training_dtype,
                enabled=enable_mixed_precision,
            ):

                outputs = model(inputs.to(device=accelerator.device))
                loss = torch.nn.functional.cross_entropy(
                    outputs.view(-1, vocab_size),
                    labels.view(-1),
                    ignore_index=-1,
                )
                loss.backward()
                optimizer.step()
                lr_scheduler.step()
                running_loss.append(loss.item())
            step += 1

            # Save every step.
            if step % save_every_step == 0 and step > 0 and save_every_step > 0:
                checkpoint_dir = os.path.join(output_dir, f"checkpoint-{step}")
                save_model(model, model_config, checkpoint_dir)

            # Log every step.
            if step % log_every_step == 0 and step > 0 and log_every_step > 0:
                average_loss = sum(running_loss) / len(running_loss)
                last_lr = lr_scheduler.get_last_lr()[0]
                history["loss"].append(average_loss)
                history["lr"].append(last_lr)
                history["step"].append(step)
                running_loss = []
                if wandb_project is not None:
                    wandb.log({"loss": average_loss, "lr": last_lr, "step": step})
                if tensorboard_dir is not None:
                    writer.add_scalar("loss", average_loss, step)
                    writer.add_scalar("lr", last_lr, step)
                    writer.add_scalar("step", step, step)
                    writer.flush()
                print(f"Step {step} completed. Loss: {average_loss}, LR: {last_lr}")

            #train_metrics.update(outputs, labels)
        print(f'Epoch {epoch + 1}/{num_epochs} completed. Loss: {loss.item()}')

    # Print some information.
    print(f"Training completed. Epochs: {epoch}, Steps: {step}")

    # Save the last model.
    checkpoint_dir = os.path.join(output_dir, f"checkpoint-{step}-last")
    save_model(model, model_config, checkpoint_dir)

    # Save the history as JSON.
    history_path = os.path.join(output_dir, "history.json")
    with open(history_path, "w") as f:
        json.dump(history, f)

    # Plot the loss.
    plt.plot(history["loss"])
    plt.xlabel("Epoch")
    plt.ylabel("Loss")
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "loss.png"))
    plt.close()

    # Plot the learning rate.
    plt.plot(history["lr"])
    plt.xlabel("Epoch")
    plt.ylabel("Learning Rate")
    plt.tight_layout()
    plt.savefig(os.path.join(output_dir, "lr.png"))
    plt.close()


def train_whitespace_tokenizer(raw_datasets):
    """
    Trains a whitespace tokenizer using the provided raw datasets.
    Args:
        raw_datasets (dict): A dictionary containing the raw datasets.
    Returns:
        PreTrainedTokenizerFast: The trained whitespace tokenizer.
    """
    
    # Initialize the tokenizer.
    tokenizer = Tokenizer(WordLevel(unk_token="[UNK]"))
    tokenizer.pre_tokenizer = WhitespaceSplit()
    trainer = WordLevelTrainer(
        special_tokens=["[UNK]", "[CLS]", "[SEP]", "[PAD]", "[MASK]"]
    )

    # Train the tokenizer.
    def get_training_corpus():
        dataset = raw_datasets["train"]
        for start_idx in range(0, len(dataset), 1000):
            samples = dataset[start_idx : start_idx + 1000]
            yield samples["text"]
    training_corpus = get_training_corpus()
    tokenizer.train_from_iterator(training_corpus, trainer=trainer)

    # Convert the tokenizer to a fast tokenizer.
    tokenizer = PreTrainedTokenizerFast(tokenizer_file="tokenizer.json")
    tokenizer.add_special_tokens({'pad_token': '[PAD]'})

    # Return the tokenizer.
    return tokenizer


def get_torch_dtype(dtype: str) -> torch.dtype:
    """
    Returns the corresponding torch.dtype for the given dtype string.

    Args:
        dtype (str): The dtype string.

    Returns:
        torch.dtype: The corresponding torch.dtype.

    Raises:
        ValueError: If the dtype is unknown.
    """

    if dtype == "float32":
        return torch.float32
    elif dtype == "bfloat16":
        return torch.bfloat16
    elif dtype == "float16":
        return torch.float16
    else:
        raise ValueError(f"Unknown dtype: {dtype}")
    

def save_model(model, model_config, output_dir):
    """
    Save the model and its configuration to the specified output directory.

    Args:
        model (torch.nn.Module): The model to be saved.
        model_config (OmegaConf.DictConfig): The configuration of the model.
        output_dir (str): The directory where the model and configuration will be saved.

    Returns:
        None
    """

    # Make sure the folder exists.
    os.makedirs(output_dir, exist_ok=True)

    # Save the model.
    torch.save(model.state_dict(), os.path.join(output_dir, "model.pth"))
    save_file(model.state_dict(), os.path.join(output_dir, "model.safetensors"))

    # Save the model configuration as JSON.
    model_config_path = os.path.join(output_dir, "config.yaml")
    OmegaConf.save(model_config, model_config_path)


def create_readme(output_dir, config):
    """
    Create a README file based on a template and provided configuration.
    Args:
        output_dir (str): The directory where the README file will be saved.
        config (dict): The configuration dictionary containing the necessary information.
    Raises:
        FileNotFoundError: If the template or banner file is not found.
    Returns:
        None
    """

    # Load the template.
    template_path = os.path.join("assets", "readmetemplate.md")
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"Template not found: {template_path}")
    
    # Load the template.
    with open(template_path, "r") as f:
        readme_text = f.read()

    # Project name.
    model_name = config.training.model_name

    # Configuration convert the configuration to a yaml string.
    configuration = OmegaConf.to_yaml(config)

    # Base model.
    base_model = "None"
    if "base_model" in config.model:
        base_model = config.model.base_model

    # Tags.
    tags = ["NLP"]
    if "tags" in config.model:
        tags = config.model.tags.split(",")
    tags = "\n".join([f"  - {tag}" for tag in tags])

    # Languages.
    languages = ["en"]
    if "languages" in config.model:
        languages = config.model.languages.split(",")
    languages = "\n".join([f"  - {language}" for language in languages])

    # Datasets.
    datasets = [config.dataset.hugging_face_id]
    datasets = "\n".join([f"  - {dataset}" for dataset in datasets])
    
    # License.
    license = "mit"

    # Format the template.
    readme_text = readme_text.format(
        model_name=model_name,
        configuration=configuration,
        base_model=base_model,
        tags=tags,
        languages=languages,
        datasets=datasets,
        license=license,
    )

    # Save the readme.
    readme_path = os.path.join(output_dir, "README.md")
    with open(readme_path, "w") as f:
        f.write(readme_text)

    # Copy the banner.
    banner_path = os.path.join("assets", "trainedwithhelibrunna.jpg")
    if not os.path.exists(banner_path):
        raise FileNotFoundError(f"Banner not found: {banner_path}")
    banner_target_path = os.path.join(output_dir, "banner.jpg")
    shutil.copy(banner_path, banner_target_path)


# Run the training.
if __name__ == "__main__":
    fire.Fire(run_training)