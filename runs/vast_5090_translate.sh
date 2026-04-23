#!/usr/bin/env bash
# runs/vast_5090_translate.sh - Phase 3 production translation launcher
# Usage on a rented vast.ai RTX 5090 box:
#   export HF_TOKEN=hf_xxx              # read (must have access to fineweb-edu)
#   export HF_WRITE_TOKEN=hf_yyy        # write (push to your dataset repo)
#   bash runs/vast_5090_translate.sh
set -euo pipefail

: "${HF_TOKEN:?set HF_TOKEN}"
: "${HF_WRITE_TOKEN:=${HF_TOKEN}}"
: "${REPO_ID:=Lyte/fineweb-edu-darija-translated}"
: "${TARGET_TOKENS:=2000000000}"     # 2B output tokens
: "${BATCH_SIZE:=384}"
: "${SHARD_ROWS:=10000}"
: "${OUT_DIR:=/workspace/darija_out}"

export HUGGINGFACE_HUB_TOKEN="${HF_TOKEN}"
# NOTE: hf_transfer hung on shard_00198 with a silent 0 B/s stall. The
# upload_shard() helper now has its own thread-based timeout + retry, but
# we also disable hf_transfer here so the underlying requests client can
# honour HF_HUB_DOWNLOAD_TIMEOUT and fail fast instead of spinning forever.
export HF_HUB_ENABLE_HF_TRANSFER=0
export HF_HUB_DOWNLOAD_TIMEOUT=60
export PYTHONUNBUFFERED=1

# 1. deps (only installs if missing) ---------------------------------------
if ! python -c "import vllm" 2>/dev/null; then
  pip install "vllm==0.11.0" --no-deps -q
  pip install "transformers>=4.51,<4.57" "tokenizers>=0.20" "numpy<2.3" \
              "datasets>=2.19" "huggingface_hub>=0.23" \
              "pandas" "pyarrow" "hf_transfer" -q
fi

# 2. repo ------------------------------------------------------------------
if [ ! -d /workspace/nanochat ]; then
  apt-get update -y && apt-get install -y git
  git clone https://github.com/yas19sin/nanochat /workspace/nanochat
fi
cd /workspace/nanochat
git pull

# 3. run (resumable) -------------------------------------------------------
exec python -m scripts.translate_vllm_darija \
  --model Lyte/tiny-aya-darija-v5 \
  --dataset HuggingFaceFW/fineweb-edu --config sample-10BT \
  --target-tokens "${TARGET_TOKENS}" \
  --batch-size "${BATCH_SIZE}" \
  --shard-rows "${SHARD_ROWS}" \
  --out-dir "${OUT_DIR}" \
  --repo-id "${REPO_ID}" \
  --progress-every 10 \
  2>&1 | tee -a /workspace/translate.log
