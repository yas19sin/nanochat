#!/usr/bin/env bash
# Train a nanochat RustBPE tokenizer from the full Darija production mix and
# upload it to Hugging Face.
#
# Expected local data on the Vast box:
#   /workspace/darija_out         fineweb translated shards (en + darija)
#   /workspace/darija_struct_out  structured shards (code + math + toolcall)
#
# Required:
#   export HF_TOKEN=hf_xxx
#   export HF_WRITE_TOKEN=hf_yyy        # optional; falls back to HF_TOKEN
#   export TOKENIZER_REPO=Lyte/your-tokenizer-repo

set -euo pipefail

: "${HF_TOKEN:?set HF_TOKEN}"
: "${HF_WRITE_TOKEN:=${HF_TOKEN}}"
: "${TOKENIZER_REPO:=Lyte/nanochat-darija-multisource-tokenizer-v1}"

export HUGGINGFACE_HUB_TOKEN="${HF_TOKEN}"
export PYTHONUNBUFFERED=1

OUT_DIR="${OUT_DIR:-/workspace/nanochat_tokenizer_multisource}"
FINEWEB_OUT_DIR="${FINEWEB_OUT_DIR:-/workspace/darija_out}"
STRUCTURED_OUT_DIR="${STRUCTURED_OUT_DIR:-/workspace/darija_struct_out}"
LEGACY_REPO="${LEGACY_REPO:-Lyte/darija-pretraining-corpus}"
LEGACY_CONFIGS="${LEGACY_CONFIGS:-arabic_raw bilingual pure}"
VOCAB_SIZE="${VOCAB_SIZE:-32768}"
DOC_CAP="${DOC_CAP:-10000}"
THREADS="${THREADS:-16}"

cd /workspace/nanochat
git pull

python -m scripts.train_multisource_tokenizer \
  --mode train-and-count \
  --output-dir "${OUT_DIR}" \
  --fineweb-out-dir "${FINEWEB_OUT_DIR}" \
  --structured-out-dir "${STRUCTURED_OUT_DIR}" \
  --legacy-repo "${LEGACY_REPO}" \
  --legacy-configs ${LEGACY_CONFIGS} \
  --vocab-size "${VOCAB_SIZE}" \
  --doc-cap "${DOC_CAP}" \
  --threads "${THREADS}" \
  --push-to-hub "${TOKENIZER_REPO}"

echo "[tokenizer] complete: ${OUT_DIR} -> ${TOKENIZER_REPO}"
