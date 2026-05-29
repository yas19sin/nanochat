#!/bin/bash
# 8x A100 SXM launcher for the d10 (~200M) Darija pretraining run.
#
# Designed for Vast.ai instance with:
#   - 8x A100 SXM (80GB each)
#   - Ubuntu 22.04 + CUDA 12.4+ driver
#   - PyTorch base image preferred (pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel)
#
# A100 is Ampere (SM 8.0) — no FA3, uses PyTorch SDPA fallback automatically.
# Use --window-pattern=L to avoid inefficient sliding window with SDPA.
#
# Data + tokenizer are auto-downloaded from HuggingFace:
#   dataset:   Lyte/darija-nanochat-pretrain-mix     (~52 GB, ~101 parquet shards)
#   tokenizer: Lyte/darija-nanochat-tokenizer-32k    (~10 MB)
#
# Usage on the Vast box:
#   export HF_TOKEN=hf_xxx        # needed if either repo is private; also lets us upload checkpoint
#   export GITHUB_REPO=yas19sin/nanochat
#   export WANDB_API_KEY=...      # optional, omit to run wandb offline
#   git clone --depth 1 https://github.com/$GITHUB_REPO.git /workspace/nanochat
#   bash /workspace/nanochat/runs/vast_a100x8_darija.sh

set -euxo pipefail

# -----------------------------------------------------------------------------
# 0) env checks + paths

export NANOCHAT_BASE_DIR=/workspace/nanochat-cache
export NANOCHAT_DATA_DIR=/workspace/data/pretrain_mix_darija_english
export NANOCHAT_TOKENIZER_DIR="$NANOCHAT_BASE_DIR/tokenizer"
export OMP_NUM_THREADS=1
export TORCHINDUCTOR_COMPILE_THREADS=8
unset TORCH_COMPILE_DISABLE 2>/dev/null || true

mkdir -p "$NANOCHAT_BASE_DIR"
# eval bundle: link if uploaded, otherwise base_train will download it
if [ -d /workspace/data/eval_bundle ] && [ ! -e "$NANOCHAT_BASE_DIR/eval_bundle" ]; then
    ln -s /workspace/data/eval_bundle "$NANOCHAT_BASE_DIR/eval_bundle"
fi

if [ "${WANDB_API_KEY:-}" = "" ]; then
    export WANDB_MODE=offline
fi

echo "=== nvidia-smi ==="
nvidia-smi
nvidia-smi --query-gpu=name,memory.total --format=csv

# -----------------------------------------------------------------------------
# 1) system deps + python env (uv)
apt-get update -y
apt-get install -y --no-install-recommends git curl ca-certificates build-essential

cd /workspace/nanochat
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
[ -d .venv ] || uv venv
uv sync --extra gpu
source .venv/bin/activate

# -----------------------------------------------------------------------------
# 2) auto-download dataset + tokenizer from HF if not present
HF_AUTH=()
if [ -n "${HF_TOKEN:-}" ]; then
    export HUGGINGFACE_HUB_TOKEN="$HF_TOKEN"
    HF_AUTH=(--token "$HF_TOKEN")
fi

mkdir -p "$NANOCHAT_DATA_DIR" "$NANOCHAT_TOKENIZER_DIR"

N_SHARDS=$(find "$NANOCHAT_DATA_DIR" -maxdepth 1 -name "*.parquet" 2>/dev/null | wc -l)
if [ "$N_SHARDS" -lt 50 ]; then
    echo "=== downloading dataset from HF (Lyte/darija-nanochat-pretrain-mix) ==="
    hf download Lyte/darija-nanochat-pretrain-mix \
        --repo-type dataset \
        --local-dir "$NANOCHAT_DATA_DIR" \
        "${HF_AUTH[@]}"
    # flatten if HF dumped files into a subdir
    find "$NANOCHAT_DATA_DIR" -mindepth 2 -name "*.parquet" \
        -exec mv -n {} "$NANOCHAT_DATA_DIR"/ \;
fi
N_SHARDS=$(find "$NANOCHAT_DATA_DIR" -maxdepth 1 -name "*.parquet" 2>/dev/null | wc -l)
echo "Found $N_SHARDS parquet shards in $NANOCHAT_DATA_DIR"
[ "$N_SHARDS" -ge 50 ] || { echo "ERROR: dataset download failed"; exit 1; }

if [ ! -f "$NANOCHAT_TOKENIZER_DIR/tokenizer.pkl" ]; then
    echo "=== downloading tokenizer from HF (Lyte/darija-nanochat-tokenizer-32k) ==="
    hf download Lyte/darija-nanochat-tokenizer-32k \
        --local-dir "$NANOCHAT_TOKENIZER_DIR" \
        "${HF_AUTH[@]}"
fi
[ -f "$NANOCHAT_TOKENIZER_DIR/tokenizer.pkl" ] \
    || { echo "ERROR: tokenizer.pkl missing after download"; exit 1; }

# Sanity: confirm GPU setup (A100 = SM 8.0, no FA3 expected)
python -c "
import torch
from nanochat.flash_attention import HAS_FA3
print('CUDA:', torch.version.cuda, 'GPUs:', torch.cuda.device_count())
print('SM:', torch.cuda.get_device_capability(0))
print('FA3 available:', HAS_FA3)
assert torch.cuda.device_count() >= 8, f'Expected 8 GPUs, got {torch.cuda.device_count()}'
"

python -m nanochat.report reset

# -----------------------------------------------------------------------------
# 3) base pretraining on 8x A100 SXM, ~200M model (depth=10)
#
# Model: depth=10 (~200M params, n_embd=768, n_head=6, n_layer=10)
#
# Batch sizing on 8x A100 SXM (80GB each):
#   - device-batch-size=48 -> 48*1024*8 = 393216 tokens/step (no grad accum needed)
#   - total-batch-size=524288 -> grad_accum=2 with dbs=32, or ~1 with dbs=64
#   - 80GB VRAM fits dbs=48 comfortably at d=10 with SDPA + bf16
#   - Using dbs=32 with total-batch-size=524288 -> grad_accum=2
#
# Window pattern: L (full context) — SDPA has no efficient sliding window support.
#
# Data: num-iterations=46000 -> ~24B trained tokens = one full epoch over the mix

DEPTH="${DEPTH:-10}"

torchrun --standalone --nproc_per_node=8 -m scripts.base_train -- \
    --depth="$DEPTH" \
    --model-tag="d${DEPTH}_darija_a100" \
    --run="d${DEPTH}_darija_a100" \
    --max-seq-len=1024 \
    --device-batch-size=32 \
    --total-batch-size=524288 \
    --num-iterations=46000 \
    --eval-every=1000 \
    --eval-tokens=524288 \
    --core-metric-every=500 \
    --sample-every=2000 \
    --save-every=1000 \
    --window-pattern=L

# -----------------------------------------------------------------------------
# 4) base eval
torchrun --standalone --nproc_per_node=8 -m scripts.base_eval -- \
    --device-batch-size=32

# -----------------------------------------------------------------------------
# 5) report + optional checkpoint upload
python -m nanochat.report generate

echo ""
echo "=== checkpoints ==="
ls -lh "$NANOCHAT_BASE_DIR"/checkpoints/ || true

if [ -n "${HF_TOKEN:-}" ] && [ -n "${HF_USER:-}" ]; then
    echo "=== uploading checkpoint to HF ==="
    export HUGGINGFACE_HUB_TOKEN="$HF_TOKEN"
    LATEST_CKPT=$(ls -td "$NANOCHAT_BASE_DIR"/checkpoints/*/ | head -1)
    python -m huggingface_hub.commands.huggingface_cli upload \
        "${HF_USER}/nanochat-d${DEPTH}-darija-a100" \
        "$LATEST_CKPT" \
        . \
        --commit-message "d${DEPTH} darija pretrain checkpoint (A100x8)"
fi

echo "=== DONE ==="
