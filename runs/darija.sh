#!/bin/bash

# Train a Moroccan Darija LLM using nanochat on 8xH200 (or 8xH100).
# Assumes the Darija parquet shards are already prepared (scripts/darija_data_prep.py)
# and uploaded / available in $NANOCHAT_DATA_DIR.
#
# Usage:
#   # Minimal (no wandb):
#   bash runs/darija.sh
#   # With wandb + screen:
#   WANDB_RUN=darija screen -L -Logfile runs/darija.log -S darija bash runs/darija.sh

export OMP_NUM_THREADS=1
export NANOCHAT_BASE_DIR="${NANOCHAT_BASE_DIR:-$HOME/.cache/nanochat}"
mkdir -p "$NANOCHAT_BASE_DIR"

# Point nanochat at the Darija shards instead of ClimbMix
export NANOCHAT_DATA_DIR="${NANOCHAT_DATA_DIR:-$NANOCHAT_BASE_DIR/darija_data}"

if [ ! -d "$NANOCHAT_DATA_DIR" ]; then
    echo "ERROR: NANOCHAT_DATA_DIR=$NANOCHAT_DATA_DIR does not exist."
    echo "Run:  python -m scripts.darija_data_prep  first."
    exit 1
fi

# Python venv
command -v uv &> /dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -d ".venv" ] || uv venv
uv sync --extra gpu
source .venv/bin/activate

# wandb
WANDB_RUN="${WANDB_RUN:-dummy}"

# Report header
python -m nanochat.report reset

# -----------------------------------------------------------------------------
# Tokenizer (train on the Darija data)
python -m scripts.tok_train
python -m scripts.tok_eval

# -----------------------------------------------------------------------------
# Base model pretraining
# depth=18=~700M params, ratio=10.5 (compute-optimal), FP8 for H200/H100
torchrun --standalone --nproc_per_node=8 -m scripts.base_train \
    -- --depth=18 --target-param-data-ratio=10.5 --device-batch-size=16 \
    --fp8 --run="$WANDB_RUN"

# Evaluate base model
torchrun --standalone --nproc_per_node=8 -m scripts.base_eval \
    -- --device-batch-size=16

# -----------------------------------------------------------------------------
# SFT
curl -L -o "$NANOCHAT_BASE_DIR/identity_conversations.jsonl" \
    https://karpathy-public.s3.us-west-2.amazonaws.com/identity_conversations.jsonl

torchrun --standalone --nproc_per_node=8 -m scripts.chat_sft \
    -- --device-batch-size=16 --run="$WANDB_RUN"
torchrun --standalone --nproc_per_node=8 -m scripts.chat_eval -- -i sft

# -----------------------------------------------------------------------------
# Report
python -m nanochat.report generate

echo ""
echo "=== Darija training complete ==="
echo "Chat: python -m scripts.chat_cli -p 'كيفاش نقدر نتعلم الدارجة؟'"
echo "WebUI: python -m scripts.chat_web"
