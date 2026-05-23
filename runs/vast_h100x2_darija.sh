#!/bin/bash
# 2x H100 SXM launcher for the d8 / full-pretraining-mix run.
#
# Designed for Vast.ai instance with:
#   - 2x H100 SXM (80GB each)
#   - Ubuntu 22.04 + CUDA 12.4+ driver
#   - PyTorch base image preferred (pytorch/pytorch:2.5.1-cuda12.4-cudnn9-devel)
#
# Before running this script, upload the dataset + tokenizer to the box:
#
#   # On YOUR machine (Windows PowerShell):
#   $REMOTE="root@<vast-ip>:/workspace"
#   $PORT=<vast-ssh-port>
#   scp -P $PORT -r D:\Dev\AI\Datasets\nanochat-cache\pretrain_mix_darija_english $REMOTE/data/
#   scp -P $PORT -r D:\Dev\AI\Datasets\nanochat-cache\tokenizer                   $REMOTE/data/
#   scp -P $PORT -r D:\Dev\AI\Datasets\nanochat-cache\eval_bundle                 $REMOTE/data/
#
#   # ~52 GB data + ~1 GB tokenizer; at 800 Mbps upload ~10 min
#
# Then on the Vast box:
#   export HF_TOKEN=hf_xxx        # only needed if you want to push the checkpoint to HF
#   export GITHUB_REPO=yas19sin/nanochat
#   export WANDB_API_KEY=...      # optional, omit to run wandb offline
#   bash vast_h100x2_darija_d8.sh

set -euxo pipefail

# -----------------------------------------------------------------------------
# 0) env checks + paths
: "${GITHUB_REPO:?set GITHUB_REPO, e.g. yas19sin/nanochat}"

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

echo "=== data check ==="
N_SHARDS=$(ls "$NANOCHAT_DATA_DIR"/*.parquet 2>/dev/null | wc -l)
echo "Found $N_SHARDS parquet shards in $NANOCHAT_DATA_DIR"
if [ "$N_SHARDS" -lt 10 ]; then
    echo "ERROR: dataset not uploaded. See header of this script for scp command."
    exit 1
fi
ls "$NANOCHAT_TOKENIZER_DIR"/tokenizer.pkl >/dev/null \
    || { echo "ERROR: tokenizer.pkl not found at $NANOCHAT_TOKENIZER_DIR"; exit 1; }

# -----------------------------------------------------------------------------
# 1) system deps + repo
apt-get update -y
apt-get install -y --no-install-recommends git curl ca-certificates build-essential

if [ ! -d /workspace/nanochat ]; then
    git clone --depth 1 "https://github.com/${GITHUB_REPO}.git" /workspace/nanochat
fi
cd /workspace/nanochat

# -----------------------------------------------------------------------------
# 2) python env (uv)
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
[ -d .venv ] || uv venv
uv sync --extra gpu
source .venv/bin/activate

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
