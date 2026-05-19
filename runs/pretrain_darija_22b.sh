#!/bin/bash

# One-pass-ish Darija/Arabic/English base pretraining run.
#
# Defaults target the exact counted train set:
#   train tokens: 22,493,640,355
#   batch tokens: 1,048,576
#   iterations:   21,452
#
# Usage:
#   WANDB_RUN=darija_d24_22b screen -L -Logfile runs/pretrain_darija_22b.log \
#     -S darija22b bash runs/pretrain_darija_22b.sh

set -euo pipefail

export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export NANOCHAT_BASE_DIR="${NANOCHAT_BASE_DIR:-/workspace/nanochat-cache}"
export NANOCHAT_DATA_DIR="${NANOCHAT_DATA_DIR:-$NANOCHAT_BASE_DIR/pretrain_mix_darija_english}"

DEPTH="${DEPTH:-24}"
MODEL_TAG="${MODEL_TAG:-darija_d24_22b}"
WANDB_RUN="${WANDB_RUN:-dummy}"
NPROC_PER_NODE="${NPROC_PER_NODE:-$(nvidia-smi -L 2>/dev/null | wc -l)}"
DEVICE_BATCH_SIZE="${DEVICE_BATCH_SIZE:-16}"
TOTAL_BATCH_SIZE="${TOTAL_BATCH_SIZE:-1048576}"
NUM_ITERATIONS="${NUM_ITERATIONS:-21452}"
EVAL_TOKENS="${EVAL_TOKENS:-524288}"
EVAL_EVERY="${EVAL_EVERY:-250}"
SAVE_EVERY="${SAVE_EVERY:-2000}"
CORE_METRIC_EVERY="${CORE_METRIC_EVERY:--1}"
SAMPLE_EVERY="${SAMPLE_EVERY:-2000}"
WINDOW_PATTERN="${WINDOW_PATTERN:-L}"
FP8_FLAG="${FP8_FLAG---fp8}"

if [ "$NPROC_PER_NODE" -lt 1 ]; then
    echo "ERROR: no GPUs detected. Set NPROC_PER_NODE manually if needed."
    exit 1
fi

if [ ! -d "$NANOCHAT_DATA_DIR" ]; then
    echo "ERROR: NANOCHAT_DATA_DIR not found: $NANOCHAT_DATA_DIR"
    exit 1
fi

for path in \
    "$NANOCHAT_DATA_DIR/zzzz_val_mix.parquet" \
    "$NANOCHAT_BASE_DIR/tokenizer/tokenizer.pkl" \
    "$NANOCHAT_BASE_DIR/tokenizer/token_bytes.pt"; do
    if [ ! -e "$path" ]; then
        echo "ERROR: required file not found: $path"
        exit 1
    fi
done

command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync --extra gpu
source .venv/bin/activate

python -m nanochat.report reset

echo "=== Darija base pretrain ==="
echo "data:              $NANOCHAT_DATA_DIR"
echo "tokenizer:         $NANOCHAT_BASE_DIR/tokenizer"
echo "depth:             $DEPTH"
echo "model tag:         $MODEL_TAG"
echo "GPUs:              $NPROC_PER_NODE"
echo "device batch size: $DEVICE_BATCH_SIZE"
echo "total batch size:  $TOTAL_BATCH_SIZE"
echo "iterations:        $NUM_ITERATIONS"
echo "train tokens:      $((TOTAL_BATCH_SIZE * NUM_ITERATIONS))"
echo "eval tokens:       $EVAL_TOKENS"
echo "window pattern:    $WINDOW_PATTERN"
echo "fp8 flag:          $FP8_FLAG"

torchrun --standalone --nproc_per_node="$NPROC_PER_NODE" -m scripts.base_train -- \
    --depth="$DEPTH" \
    --model-tag="$MODEL_TAG" \
    --run="$WANDB_RUN" \
    --num-iterations="$NUM_ITERATIONS" \
    --total-batch-size="$TOTAL_BATCH_SIZE" \
    --device-batch-size="$DEVICE_BATCH_SIZE" \
    --eval-tokens="$EVAL_TOKENS" \
    --eval-every="$EVAL_EVERY" \
    --save-every="$SAVE_EVERY" \
    --core-metric-every="$CORE_METRIC_EVERY" \
    --sample-every="$SAMPLE_EVERY" \
    --window-pattern="$WINDOW_PATTERN" \
    $FP8_FLAG

python -m nanochat.report generate

echo ""
echo "=== Base pretraining complete ==="
echo "checkpoint dir: $NANOCHAT_BASE_DIR/base_checkpoints/$MODEL_TAG"
