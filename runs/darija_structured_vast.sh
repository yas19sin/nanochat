#!/usr/bin/env bash
# Run the structured-data translation pipeline on a Vast/RunPod GPU.
#
# Prerequisites:
#   - HF_TOKEN exported (read access to source datasets + write to repo-id)
#   - HF_WRITE_TOKEN exported (or falls back to HF_TOKEN)
#   - vLLM, datasets, pandas, huggingface_hub installed
#
# Run this AFTER fineweb-edu translation completes (uses the same GPU).
#
# Default targets give ~55M tokens of Darija structured data, which is plenty
# to anchor cross-lingual transfer for SFT (capability comes from English mix).

set -euo pipefail

OUT_DIR="${OUT_DIR:-/workspace/darija_struct_out}"
REPO_ID="${REPO_ID:-Lyte/darija-structured-translated}"
MODEL="${MODEL:-Lyte/tiny-aya-darija-v5}"

N_CODE="${N_CODE:-50000}"
N_TOOLCALL="${N_TOOLCALL:-11000}"
N_MATH="${N_MATH:-50000}"

BATCH_SIZE="${BATCH_SIZE:-128}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
MAX_NEW_TOKENS="${MAX_NEW_TOKENS:-1024}"
GPU_MEM_UTIL="${GPU_MEM_UTIL:-0.90}"
SHARD_ROWS="${SHARD_ROWS:-2000}"
ENFORCE_EAGER="${ENFORCE_EAGER:-0}"
ATTENTION_BACKEND="${ATTENTION_BACKEND:-}"

mkdir -p "$OUT_DIR"

echo "[darija-struct] out=$OUT_DIR  repo=$REPO_ID"
echo "[darija-struct] targets: code=$N_CODE  toolcall=$N_TOOLCALL  math=$N_MATH"
echo "[darija-struct] batch_size=$BATCH_SIZE  max_model_len=$MAX_MODEL_LEN  enforce_eager=$ENFORCE_EAGER  backend=${ATTENTION_BACKEND:-default}"

EXTRA_ARGS=()
if [ "$ENFORCE_EAGER" = "1" ]; then
    EXTRA_ARGS+=(--enforce-eager)
fi
if [ -n "$ATTENTION_BACKEND" ]; then
    EXTRA_ARGS+=(--attention-backend "$ATTENTION_BACKEND")
fi

python -m dev.translate_experiments.translate_structured_vllm \
    --model "$MODEL" \
    --domain all \
    --n-code "$N_CODE" \
    --n-toolcall "$N_TOOLCALL" \
    --n-math "$N_MATH" \
    --batch-size "$BATCH_SIZE" \
    --max-new-tokens "$MAX_NEW_TOKENS" \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-mem-util "$GPU_MEM_UTIL" \
    --shard-rows "$SHARD_ROWS" \
    --out-dir "$OUT_DIR" \
    --repo-id "$REPO_ID" \
    "${EXTRA_ARGS[@]}"

echo "[darija-struct] complete"
