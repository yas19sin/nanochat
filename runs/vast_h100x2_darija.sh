#!/bin/bash
# 2x H100 SXM launcher for the d8 / full-pretraining-mix run.
#
# Designed for Vast.ai instance with:
#   - 2x H100 SXM (80GB each)
#   - Ubuntu 22.04 + CUDA 12.4+ driver
#   - PyTorch base image preferred (pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel)
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
#   bash /workspace/nanochat/runs/vast_h100x2_darija.sh

set -euxo pipefail

# -----------------------------------------------------------------------------
# 0) env checks + paths
# GITHUB_REPO not required at runtime (clone is done before invoking this script),
# but exported is fine: we use it for any HF checkpoint upload tagging.

export NANOCHAT_BASE_DIR=/workspace/nanochat-cache
export NANOCHAT_DATA_DIR=/workspace/data/pretrain_mix_darija_english
export NANOCHAT_TOKENIZER_DIR=/workspace/data/tokenizer
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
# 1) system deps + python env (uv) -- needed before we can call hf CLI
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

N_SHARDS=$(ls "$NANOCHAT_DATA_DIR"/*.parquet 2>/dev/null | wc -l)
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
N_SHARDS=$(ls "$NANOCHAT_DATA_DIR"/*.parquet 2>/dev/null | wc -l)
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

# Sanity: confirm FA3 is available on Hopper
python -c "
import torch
from nanochat.flash_attention import HAS_FLASH_ATTN_3
print('CUDA:', torch.version.cuda, 'GPUs:', torch.cuda.device_count())
print('SM:', torch.cuda.get_device_capability(0))
print('FA3 available:', HAS_FLASH_ATTN_3)
"

python -m nanochat.report reset

# -----------------------------------------------------------------------------
# 3) base pretraining on 2x H100 SXM, full epoch over the mix
#
# Model: depth=12 (~290M params, n_embd=768, n_head=6)
#   - This is the nanochat REFERENCE model: all hyperparams (LR, weight decay,
#     muP scaling) are tuned at d=12 and transferred to other depths.
#   - To switch sizes: DEPTH=10 -> ~200M, DEPTH=11 -> ~265M, DEPTH=12 -> ~290M.
#
# Batch sizing on 2x H100 SXM (80GB each):
#   - device-batch-size=64 -> 64*1024*2 = 131072 tokens/step
#   - total-batch-size=524288 -> grad_accum=4 (good signal-to-noise at ~290M)
#   - 80GB VRAM easily fits dbs=64 at d=12 with FA3 + bf16
#   - If OOM, drop dbs to 48 and bump grad_accum proportionally
#
# Data: num-iterations=46000 -> ~24B trained tokens = one full epoch over the mix
#   (262144 * 92000 == 524288 * 46000 == 24.1B trained tokens)

DEPTH="${DEPTH:-12}"

torchrun --standalone --nproc_per_node=2 -m scripts.base_train -- \
    --depth="$DEPTH" \
    --model-tag="d${DEPTH}_full_h100" \
    --run="d${DEPTH}_full_h100" \
    --max-seq-len=1024 \
    --device-batch-size=64 \
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
torchrun --standalone --nproc_per_node=2 -m scripts.base_eval -- \
    --device-batch-size=64

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
        "${HF_USER}/nanochat-d8-darija-mix" \
        "$LATEST_CKPT" \
        . \
        --token "$HF_TOKEN" || echo "upload failed (continuing)"
fi

echo ""
echo "=== DONE. Total wallclock above. ==="
echo "Don't forget to STOP/DESTROY the vast instance."
