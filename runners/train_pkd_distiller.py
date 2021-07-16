# coding=utf-8
# Copyright 2019-present, the HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
Training the distilled model.
Supported architectures include: GPT2 -> DistilGPT2.
"""
#import sys
#sys.path.append('/home/yassir/gpt2-ks/transformers_local')
import argparse
import json
import os
import pickle
import shutil
import torch

from datasets import load_dataset, load_from_disk

#from transformers import GPT2Config, GPT2LMHeadModel, GPT2Tokenizer
from transformers_local import GPT2Config, GPT2LMHeadModel, GPT2Tokenizer

#from distillation.pkd_distiller import PKD_Distiller, require_teacher
from distillation.pkd_distiller_deepspeed import PKD_Distiller, require_teacher
from utilities.utils import init_gpu_params, logger, set_seed

#from hanging_threads import start_monitoring
#start_monitoring(seconds_frozen=360, test_interval=360)


MODEL_CLASSES = {
    "gpt2": (GPT2Config, GPT2LMHeadModel, GPT2Tokenizer),
    "distilgpt2": (GPT2Config, GPT2LMHeadModel, GPT2Tokenizer),
}


def sanity_checks(args):
    """
    A bunch of args sanity checks to perform even starting...
    """
    assert (args.student_type in ["gpt2", "distilgpt2"]) and (args.teacher_type in ["gpt2"])

    assert os.path.isfile(args.student_config)
    if args.student_pretrained_weights is not None:
        assert os.path.isfile(args.student_pretrained_weights)
    
    if not args.teacher_name_or_path in ["gpt2", "gpt2-medium", "gpt2-large", "gpt2-xl"]:
        assert os.path.isfile(args.teacher_name_or_path) or os.path.isdir(args.teacher_name_or_path)
    
    if args.deepspeed is not None:
        assert os.path.isfile(args.deepspeed)

    assert args.alpha_lm >= 0.0
    assert args.alpha_att >= 0.0
    assert args.alpha_val >= 0.0
    assert args.alpha_pkd >= 0.0
    assert args.beta_pkd >= 0.0

    assert args.beta_pkd + args.alpha_lm + args.alpha_att + args.alpha_val + args.beta_pkd > 0.0


def freeze_pos_embeddings(student, args):
    student.transformer.wpe.weight.requires_grad = False



def main():
    parser = argparse.ArgumentParser(description="Training")
    parser.add_argument("--force", action="store_true", help="Overwrite dump_path if it already exists.")

    parser.add_argument(
        "--dump_path", type=str, required=True, help="The output directory (log, checkpoints, parameters, etc.)"
    )
    parser.add_argument(
        "--data_dir",
        type=str,
        required=True,
        help="The directory containing grouped tokens.",
    )

    parser.add_argument("--student_type", type=str, choices=["distilgpt2", "gpt2"], required=True, help="The student type (DistilGPT2, GPT2).")
    parser.add_argument("--student_config", type=str, required=True, help="Path to the student configuration.")
    parser.add_argument("--student_pretrained_weights", default=None, type=str, help="Load student initialization checkpoint.")

    parser.add_argument("--teacher_type", choices=["gpt2"], required=True, help="Teacher type (GPT2).")
    parser.add_argument("--teacher_name_or_path", type=str, required=True, help="The teacher type or weights.")
    parser.add_argument("--temperature", default=2.0, type=float, help="Temperature for the softmax temperature.")

    parser.add_argument("--alpha_lm", default=1.0, type=float, help="Linear weight for the LM loss.")
    parser.add_argument("--minilm", action="store_true", help="Use of mini-lm loss")
    parser.add_argument("--alpha_att", default=0.5, type=float, help="Linear weight for the self attention loss (Mini-LM).")
    parser.add_argument("--alpha_val", default=0.5, type=float, help="Linear weight for the value-value loss (Mini-LM).")
    parser.add_argument("--pkd", action="store_true", help="Use of pkd loss")
    parser.add_argument("--alpha_pkd", default=0.33, type=float, help="Linear weight for the NLL loss (PKD).")
    parser.add_argument("--beta_pkd", default=0.33, type=float, help="Linear weight for the MSE loss (PKD).")
    parser.add_argument("--pkd_output", default=0, type=int, help="Output dimension for random projection.")
    parser.add_argument("--freeze_pos_embs", action="store_true", help="Freeze positional embeddings during distillation.")
    parser.add_argument("--n_epoch", type=int, default=3, help="Number of pass on the whole dataset.")
    parser.add_argument("--max_steps", type=int, default=-1, help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--batch_size", type=int, default=5, help="Batch size (for each process).")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=50, help="Gradient accumulation for larger training batches.")
    parser.add_argument("--warmup_prop", default=0., type=float, help="Linear warmup proportion.")
    parser.add_argument("--weight_decay", default=0.0, type=float, help="Weight deay if we apply some.")
    parser.add_argument("--learning_rate", default=5e-5, type=float, help="The initial learning rate for Adam.")
    parser.add_argument("--adam_epsilon", default=1e-6, type=float, help="Epsilon for Adam optimizer.")
    parser.add_argument("--adam_beta1", default=0.9, type=float, help="Beta1 for Adam optimizer.")
    parser.add_argument("--adam_beta2", default=0.999, type=float, help="Beta1 for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=5.0, type=float, help="Max gradient norm.")
    parser.add_argument("--std_range", default=0.02, type=float, help="Random initialization range.")
    ### YE Attempt
    parser.add_argument("--deepspeed", default=None, type=str, help="Path to deepspeed configuration.")

    parser.add_argument("--fp16", action="store_true", help="Whether to use 16-bit (mixed) precision (through NVIDIA apex) instead of 32-bit")
    parser.add_argument(
        "--fp16_opt_level",
        type=str,
        default="O1",
        help="For fp16: Apex AMP optimization level selected in ['O0', 'O1', 'O2', and 'O3']."
        "See details at https://nvidia.github.io/apex/amp.html",
    )
    parser.add_argument("--n_gpu", type=int, default=1, help="Number of GPUs in the node.")
    parser.add_argument("--local_rank", type=int, default=-1, help="Distributed training - Local rank")
    parser.add_argument("--seed", type=int, default=56, help="Random seed")

    parser.add_argument("--log_interval", type=int, default=500, help="Tensorboard logging interval.")
    parser.add_argument("--checkpoint_interval", type=int, default=4000, help="Checkpoint interval.")
    parser.add_argument('--teacher_layers','--list', nargs='+', type=int, default=[0, 2, 4, 7, 9, 11])
    args = parser.parse_args()
    sanity_checks(args)

    # ARGS #
    device = init_gpu_params(args)
    set_seed(args)

    if args.is_master:
        if os.path.exists(args.dump_path):
            if not args.force:
                raise ValueError(
                    f"Serialization dir {args.dump_path} already exists, but you have not precised wheter to overwrite it"
                    "Use `--force` if you want to overwrite it"
                )
            else:
                shutil.rmtree(args.dump_path)

        if not os.path.exists(args.dump_path):
            os.makedirs(args.dump_path)
        logger.info(f"Experiment will be dumped and logged in {args.dump_path}")

        # SAVE PARAMS #
        logger.info(f"Param: {args}")
        #with open(os.path.join(args.dump_path, "parameters.json"), "w") as f:
        #    json.dump(vars(args), f, indent=4)
    
    student_config_class, student_model_class, _ = MODEL_CLASSES[args.student_type]
    teacher_config_class, teacher_model_class, teacher_tokenizer_class = MODEL_CLASSES[args.teacher_type]

    # TOKENIZER #
    tokenizer = teacher_tokenizer_class.from_pretrained(args.teacher_type)
    special_tok_ids = {}
    for tok_name, tok_symbol in tokenizer.special_tokens_map.items():
        idx = tokenizer.all_special_tokens.index(tok_symbol)
        special_tok_ids[tok_name] = tokenizer.all_special_ids[idx]
    logger.info(f"Special tokens {special_tok_ids}")
    args.special_tok_ids = special_tok_ids
    args.max_model_input_size = tokenizer.max_model_input_sizes[args.teacher_type] 

    # DATA LOADER #
    logger.info(f"Loading data from {args.data_dir}")
    train_lm_seq_dataset = load_from_disk(args.data_dir)

    # STUDENT #
    logger.info(f"Loading student config from {args.student_config}")
    stu_architecture_config = student_config_class.from_pretrained(args.student_config)
    stu_architecture_config.output_hidden_states = True

    if args.student_pretrained_weights is not None:
        logger.info(f"Loading pretrained weights from {args.student_pretrained_weights}")
        student = student_model_class.from_pretrained(args.student_pretrained_weights, config=stu_architecture_config)
    else:
        student = student_model_class(stu_architecture_config)

    if args.n_gpu > 0:
        student.to(f"cuda:{args.local_rank}")
    logger.info("Student loaded.")

    # TEACHER #
    if require_teacher(args):
        teacher = teacher_model_class.from_pretrained(args.teacher_name_or_path, output_hidden_states=True)
        if args.n_gpu > 0:
            teacher.to(f"cuda:{args.local_rank}")
        logger.info(f"Teacher loaded from {args.teacher_name_or_path}.")
    else:
        teacher = None
        logger.info(f"Won't load Teacher.")
    # FREEZING #
    if args.freeze_pos_embs:
        freeze_pos_embeddings(student, args)

    # SANITY CHECKS #
    if require_teacher(args):
        assert student.config.vocab_size == teacher.config.vocab_size
        assert student.config.hidden_size == teacher.config.hidden_size
        assert student.config.max_position_embeddings == teacher.config.max_position_embeddings
 

    # DISTILLER #
    torch.cuda.empty_cache()
    distiller = PKD_Distiller(
        params=args, dataset=train_lm_seq_dataset["train"], student=student, teacher=teacher, tokenizer=tokenizer
    )
    logger.info("Let's go get some drinks.")
    distiller.train()
    


if __name__ == "__main__":
    main()
