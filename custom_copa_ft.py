import sys
import submitit
import os
from cluster_submission_utils import find_master, update_submission_log


def main(submit_arguments):
    from datasets import load_dataset
    from dataclasses import dataclass, field
    from transformers import (
        TrainingArguments,
        Trainer,
        AutoModelForMultipleChoice,
        AutoTokenizer,
        AutoConfig,
        EvalPrediction,
        HfArgumentParser,
        EarlyStoppingCallback,
    )
    import numpy as np
    from transformers.trainer_utils import get_last_checkpoint

    @dataclass
    class DataTrainingArguments:
        """
        Arguments pertaining to what data we are going to input our model for training and eval.
        """

        output_dir: str = field(
            default=None,
            metadata={"help": ("Directory for model checkpoints and saving")},
        )

    parser = HfArgumentParser(DataTrainingArguments)
    sys.argv = ""
    if len(submit_arguments) == 1 and submit_arguments[0].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        data_args = parser.parse_json_file(json_file=os.path.abspath(submit_arguments[0]))
    else:
        print("calling parser")
        # add a comma to refer to first part of tuple output
        (data_args,) = parser.parse_args_into_dataclasses(submit_arguments)

    print("passed args: ", data_args)

    tokenizer = AutoTokenizer.from_pretrained("xlm-roberta-base")
    raw_datasets = load_dataset("super_glue", "copa")

    def encode_batch(examples):
        """Encodes a batch of input data using the model tokenizer."""
        all_encoded = {"input_ids": [], "attention_mask": []}
        # Iterate through all examples in this batch
        for premise, question, choice1, choice2 in zip(
            examples["premise"], examples["question"], examples["choice1"], examples["choice2"]
        ):
            sentences_a = [premise + " " + question for _ in range(2)]
            # Both answer choices are passed in an array according to the format needed for the multiple-choice prediction head
            sentences_b = [choice1, choice2]
            encoded = tokenizer(
                sentences_a,
                sentences_b,
                max_length=60,
                truncation=True,
                padding="max_length",
            )
            all_encoded["input_ids"].append(encoded["input_ids"])
            all_encoded["attention_mask"].append(encoded["attention_mask"])
        return all_encoded

    def preprocess_dataset(dataset):
        # Encode the input data
        dataset = dataset.map(encode_batch, batched=True)
        # The transformers model expects the target class column to be named "labels"
        dataset = dataset.rename_column("label", "labels")
        # Transform to pytorch tensors and only output the required columns
        dataset.set_format(columns=["input_ids", "attention_mask", "labels"])
        return dataset

    def compute_metrics(p: EvalPrediction):
        preds = np.argmax(p.predictions, axis=1)
        return {"acc": (preds == p.label_ids).mean()}

    tokenized_datasets = preprocess_dataset(raw_datasets)
    # Load the model configuration and adapter model
    config = AutoConfig.from_pretrained(
        "xlm-roberta-base",
    )
    model = AutoModelForMultipleChoice.from_pretrained(
        "xlm-roberta-base",
        config=config,
    )

    training_args = TrainingArguments(
        output_dir=data_args.output_dir,
        eval_strategy="epoch",
        save_strategy="epoch",
        learning_rate=1e-4,
        load_best_model_at_end=True,
        metric_for_best_model="acc",
        greater_is_better=True,
        per_device_train_batch_size=16,
        per_device_eval_batch_size=16,
        num_train_epochs=100,
        weight_decay=0.01,
        save_steps=25000,
        overwrite_output_dir=False,
        # The next line is important to ensure the dataset labels are properly passed to the model
        remove_unused_columns=False,
    )
    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=tokenized_datasets["train"],
        eval_dataset=tokenized_datasets["validation"],
        processing_class=tokenizer,
        compute_metrics=compute_metrics,
        callbacks=[EarlyStoppingCallback(early_stopping_patience=10)],
    )
    last_checkpoint = get_last_checkpoint(data_args.output_dir)
    if last_checkpoint is not None:
        print(f"Resuming training from checkpoint: {last_checkpoint}")
        training_args.resume_from_checkpoint = last_checkpoint
    else:
        print("No checkpoint found, starting training from scratch.")
    trainer.train()


if __name__ == "__main__":
    debug = True
    job_name = debug * "debug_" + "convergence_finetune_copa"

    master_dir = find_master()

    # Set the experiment folder as a subdirectory of 'Master_thesis'
    experiments_dir = master_dir / "experiment_folder"

    run_count = update_submission_log(experiments_dir, job_name)
    experiments_dir = experiments_dir / job_name / f"{run_count:03d}"
    experiments_dir.mkdir(parents=True, exist_ok=True)  # Create if it doesn't exist
    partition = f"gpu_p100{debug * '_debug'}"
    parameters = {
        "slurm_partition": partition,
        # "slurm_time": "03:00:00",
        "slurm_time": f"{'01:00:00' if partition.endswith('debug') else '02:30:00'}",
        "slurm_job_name": job_name,
        "slurm_additional_parameters": {
            "clusters": f"{'genius' if partition.startswith(('gpu_p100', 'gpu_v100')) else 'wice'}",
            "account": os.environ["ACCOUNT_INFO"],  # replace with your account
            "nodes": 1,
            "cpus_per_gpu": 16,
            "gpus_per_node": 1,
            "mail_type": "BEGIN,END,FAIL",
            "mail_user": f"{'' if partition.endswith('debug') else 'stef.accou@student.kuleuven.be'}",
        },
    }

    # Initialize the Submitit executor with the new experiments_dir
    executor = submitit.AutoExecutor(folder=str(experiments_dir))
    executor.update_parameters(**parameters)

    job_input = sys.argv[1:] if len(sys.argv) > 1 else "default text"

    job = executor.submit(main, job_input)
    # job = executor.submit(main)
    print("job submitted")
