#!/bin/bash

# Build the Darija/Arabic/English pretraining shard directory for nanochat.
#
# Designed for a Linux box/Vast instance with fast disk and internet.
# The output directory is directly usable as NANOCHAT_DATA_DIR.
#
# Usage:
#   export HF_TOKEN=hf_...
#   bash runs/pretraining_mix_prep.sh
#
# Optional:
#   # Full warehouse dataset, then upload private:
#   HF_DATASET_REPO=your-namespace/darija-pretrain-mix bash runs/pretraining_mix_prep.sh
#   # Focused curriculum for a ~10B-token first run:
#   LAYOUT=curriculum ENGLISH_BUDGET_TOKENS=5500000000 ARABIC_BUDGET_TOKENS=1500000000 DARIJA_BUDGET_TOKENS=3000000000 bash runs/pretraining_mix_prep.sh
#   # Exact token counting after tokenizer exists:
#   TOKENIZER_DIR=$NANOCHAT_BASE_DIR/tokenizer LAYOUT=curriculum ... bash runs/pretraining_mix_prep.sh
#   TRAIN_TOKENIZER=1 bash runs/pretraining_mix_prep.sh

set -euo pipefail

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export NANOCHAT_BASE_DIR="${NANOCHAT_BASE_DIR:-$HOME/.cache/nanochat}"
export NANOCHAT_DATA_DIR="${NANOCHAT_DATA_DIR:-$NANOCHAT_BASE_DIR/pretrain_mix_darija_english}"
export HF_HOME="${HF_HOME:-/workspace/hf-cache}"

mkdir -p "$NANOCHAT_BASE_DIR" "$HF_HOME" "$NANOCHAT_DATA_DIR"

if [ -z "${HF_TOKEN:-${HUGGINGFACE_HUB_TOKEN:-}}" ]; then
    echo "WARNING: HF_TOKEN/HUGGINGFACE_HUB_TOKEN is not set. Gated datasets may fail."
fi

command -v uv &> /dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -d ".venv" ] || uv venv
uv sync --extra gpu
source .venv/bin/activate

TOKENIZER_ARGS=()
if [ -n "${TOKENIZER_DIR:-}" ]; then
    TOKENIZER_ARGS=(--tokenizer-dir "$TOKENIZER_DIR")
fi

python -m scripts.build_pretraining_mix \
    --output-dir "$NANOCHAT_DATA_DIR" \
    --layout "${LAYOUT:-curriculum}" \
    --cache-dir "$HF_HOME/datasets" \
    --english-budget-tokens "${ENGLISH_BUDGET_TOKENS:--1}" \
    --arabic-budget-tokens "${ARABIC_BUDGET_TOKENS:--1}" \
    --darija-budget-tokens "${DARIJA_BUDGET_TOKENS:--1}" \
    --shard-size "${SHARD_SIZE:-250000}" \
    --val-size "${VAL_SIZE:-50000}" \
    "${TOKENIZER_ARGS[@]}" \
    "$@"

echo ""
echo "=== Pretraining data ready ==="
echo "Data: $NANOCHAT_DATA_DIR"
echo "Manifest: $NANOCHAT_DATA_DIR/manifest.json"
echo ""

if [ -n "${HF_DATASET_REPO:-}" ]; then
    echo "=== Uploading private Hugging Face dataset: $HF_DATASET_REPO ==="
    hf auth whoami >/dev/null
    hf repos create "$HF_DATASET_REPO" --type dataset --private --exist-ok
    hf upload-large-folder "$HF_DATASET_REPO" "$NANOCHAT_DATA_DIR" \
        --type dataset \
        --private \
        --num-workers "${HF_UPLOAD_WORKERS:-8}"
    echo "Uploaded: https://huggingface.co/datasets/$HF_DATASET_REPO"
    echo ""
fi

if [ "${TRAIN_TOKENIZER:-0}" = "1" ]; then
    echo "=== Training tokenizer on mixed pretraining data ==="
    python -m scripts.tok_train
    python -m scripts.tok_eval
fi
