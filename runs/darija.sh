#!/bin/bash

# End-to-end pipeline for the Darija NanoChat model:
# - Prepare parquet shards from Lyte/darija-pretraining-corpus
# - Train a Darija-focused tokenizer
# - Pretrain a depth-18 base model
# - SFT on Darija instruction datasets

export OMP_NUM_THREADS=1
export NANOCHAT_BASE_DIR="${NANOCHAT_BASE_DIR:-$HOME/.cache/nanochat}"
export NANOCHAT_DATA_DIR="${NANOCHAT_DATA_DIR:-$NANOCHAT_BASE_DIR/darija_data}"
mkdir -p "$NANOCHAT_BASE_DIR"

# -----------------------------------------------------------------------------
# Python venv setup with uv

command -v uv &> /dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -d ".venv" ] || uv venv
uv sync --extra gpu
source .venv/bin/activate

# -----------------------------------------------------------------------------
# wandb setup
if [ -z "$WANDB_RUN" ]; then
    WANDB_RUN=darija
fi

# -----------------------------------------------------------------------------
# Kick off a fresh report
python -m nanochat.report reset

# -----------------------------------------------------------------------------
# Data + tokenizer

python -m scripts.darija_data_prep --data-dir "$NANOCHAT_DATA_DIR"
python -m scripts.tok_train --max-chars 2000000000 --doc-cap 12000 --vocab-size 32768
python -m scripts.tok_eval

# -----------------------------------------------------------------------------
# Base model (pretraining)
torchrun --standalone --nproc_per_node=8 -m scripts.base_train -- --depth=18 --target-param-data-ratio=10.5 --device-batch-size=16 --fp8 --run=$WANDB_RUN
torchrun --standalone --nproc_per_node=8 -m scripts.base_eval -- --device-batch-size=16

# -----------------------------------------------------------------------------
# SFT on Darija instruction datasets
torchrun --standalone --nproc_per_node=8 -m scripts.chat_sft -- --device-batch-size=16 --mixture=darija --run=$WANDB_RUN
torchrun --standalone --nproc_per_node=8 -m scripts.chat_eval -- -i sft

# -----------------------------------------------------------------------------
# Final report
python -m nanochat.report generate
