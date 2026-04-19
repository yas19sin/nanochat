#!/bin/bash

# Darija data preparation — run ONCE before any training script.
# Downloads Darija corpus, writes parquet shards, trains tokenizer.
# Designed for 8×H100/H200 (or any Linux box with internet).
#
# Usage:
#   bash runs/darija_prep.sh
#
# Then run any of:
#   bash runs/darija_baseline.sh
#   bash runs/darija_engram.sh
#   bash runs/darija_attnres.sh
#   bash runs/darija_attnres_engram.sh

set -euo pipefail
export OMP_NUM_THREADS=1
export NANOCHAT_BASE_DIR="${NANOCHAT_BASE_DIR:-$HOME/.cache/nanochat}"
mkdir -p "$NANOCHAT_BASE_DIR"

export NANOCHAT_DATA_DIR="${NANOCHAT_DATA_DIR:-$NANOCHAT_BASE_DIR/darija_data}"

# -----------------------------------------------------------------------------
# Python venv
command -v uv &> /dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -d ".venv" ] || uv venv
uv sync --extra gpu
source .venv/bin/activate

# -----------------------------------------------------------------------------
# Download & shard the Darija pretraining corpus
echo "=== Preparing Darija data shards ==="
python -m scripts.darija_data_prep

# -----------------------------------------------------------------------------
# Train tokenizer on the Darija data
echo "=== Training tokenizer ==="
python -m scripts.tok_train

# Evaluate tokenizer (compression ratio etc.)
python -m scripts.tok_eval

echo ""
echo "=== Darija prep complete ==="
echo "Data:      $NANOCHAT_DATA_DIR"
echo "Tokenizer: $NANOCHAT_BASE_DIR/tok32768.bin"
echo ""
echo "Now run one of:"
echo "  bash runs/darija_baseline.sh"
echo "  bash runs/darija_engram.sh"
echo "  bash runs/darija_attnres.sh"
echo "  bash runs/darija_attnres_engram.sh"
