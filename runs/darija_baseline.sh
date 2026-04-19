#!/bin/bash

# Darija 1B baseline — no Engram, no AttnRes.
# Pretraining + eval only. Run darija_prep.sh first for data + tokenizer.
# Designed for 8×H100/H200.
#
# Usage:
#   bash runs/darija_prep.sh          # once: data shards + tokenizer
#   bash runs/darija_baseline.sh      # this script
#   WANDB_RUN=darija_baseline screen -L -Logfile runs/darija_baseline.log -S baseline bash runs/darija_baseline.sh

set -euo pipefail
export OMP_NUM_THREADS=1
export NANOCHAT_BASE_DIR="${NANOCHAT_BASE_DIR:-$HOME/.cache/nanochat}"
export NANOCHAT_DATA_DIR="${NANOCHAT_DATA_DIR:-$NANOCHAT_BASE_DIR/darija_data}"

# Verify prep has been done
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
# Base model pretraining — depth=21 (~1.1B params), no extras
torchrun --standalone --nproc_per_node=8 -m scripts.base_train \
    -- --depth=21 --target-param-data-ratio=12 --device-batch-size=16 \
    --fp8 --model-tag=d21_baseline --run="$WANDB_RUN"

torchrun --standalone --nproc_per_node=8 -m scripts.base_eval \
    -- --device-batch-size=16

# -----------------------------------------------------------------------------
python -m nanochat.report generate

echo ""
echo "=== Darija baseline (1B, no extras) complete ==="
