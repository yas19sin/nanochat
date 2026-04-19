#!/bin/bash

# Darija 1B + AttnRes + Engram — both features enabled.
# Pretraining + eval only. Run darija_prep.sh first for data + tokenizer.
# Designed for 8×H100/H200.
#
# Usage:
#   bash runs/darija_prep.sh          # once: data shards + tokenizer
#   bash runs/darija_attnres_engram.sh # this script
#   WANDB_RUN=darija_all screen -L -Logfile runs/darija_attnres_engram.log -S all bash runs/darija_attnres_engram.sh

set -euo pipefail
export OMP_NUM_THREADS=1
export NANOCHAT_BASE_DIR="${NANOCHAT_BASE_DIR:-$HOME/.cache/nanochat}"
export NANOCHAT_DATA_DIR="${NANOCHAT_DATA_DIR:-$NANOCHAT_BASE_DIR/darija_data}"

for check in "$NANOCHAT_DATA_DIR" "$NANOCHAT_BASE_DIR/tok32768.bin"; do
    if [ ! -e "$check" ]; then
        echo "ERROR: $check not found. Run: bash runs/darija_prep.sh first."
        exit 1
    fi
done

source .venv/bin/activate
WANDB_RUN="${WANDB_RUN:-dummy}"

python -m nanochat.report reset

# -----------------------------------------------------------------------------
# Base model pretraining — depth=21 (~1.1B trunk + ~137M Engram + AttnRes)
torchrun --standalone --nproc_per_node=8 -m scripts.base_train \
    -- --depth=21 --target-param-data-ratio=12 --device-batch-size=16 \
    --fp8 \
    --use-attn-res --attn-res-num-blocks=4 \
    --use-engram --engram-table-size=131072 --engram-n-heads=4 --engram-embed-dim=256 \
    --model-tag=d21_attnres_engram --run="$WANDB_RUN"

torchrun --standalone --nproc_per_node=8 -m scripts.base_eval \
    -- --device-batch-size=16

# -----------------------------------------------------------------------------
python -m nanochat.report generate

echo ""
echo "=== Darija + AttnRes + Engram (1B) complete ==="
