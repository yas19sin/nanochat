#!/bin/bash
# Phase 1 benchmark launcher for rented 5090 (vast.ai / runpod).
#
# Prereqs on the box:
#   - Ubuntu 22.04 w/ CUDA 12.4+ driver
#   - Pytorch-CUDA base image preferred (e.g. pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime)
#
# Usage:
#   export HF_TOKEN=hf_xxx       # your HF read token (for model download + dataset)
#   export GITHUB_REPO=<user>/nanochat   # e.g. yas19sin/nanochat
#   bash vast_5090_bench.sh
#
# Expected runtime: ~30-45 min. Ends by printing bench_results.json.

set -euxo pipefail

# -----------------------------------------------------------------------------
# 0) env checks
: "${HF_TOKEN:?set HF_TOKEN (your HF read token)}"
: "${GITHUB_REPO:?set GITHUB_REPO, e.g. user/nanochat}"
export HUGGINGFACE_HUB_TOKEN="$HF_TOKEN"

echo "=== nvidia-smi ==="
nvidia-smi || { echo "no GPU visible"; exit 1; }

# -----------------------------------------------------------------------------
# 1) system packages
apt-get update -y
apt-get install -y --no-install-recommends git curl ca-certificates

# -----------------------------------------------------------------------------
# 2) clone repo
if [ ! -d /workspace/nanochat ]; then
    git clone --depth 1 "https://github.com/${GITHUB_REPO}.git" /workspace/nanochat
fi
cd /workspace/nanochat

# -----------------------------------------------------------------------------
# 3) python deps
# Use a separate venv so we don't fight the base image
python -m pip install --upgrade pip

# vLLM 0.7.x supports sm_120 (Blackwell) via PyTorch 2.5+
# If on RTX 5090 / B200, use nightly wheel if 0.7.x doesn't have the Blackwell kernels
pip install "vllm>=0.7.3" "transformers>=4.46" "datasets>=3.0" \
            "huggingface_hub>=0.26" pandas pyarrow

# -----------------------------------------------------------------------------
# 4) pre-download the model so we can see download speed and disk usage
echo "=== downloading model ==="
python -m huggingface_hub.commands.huggingface_cli download \
    Lyte/tiny-aya-darija-v5 \
    --token "$HF_TOKEN" \
    || { echo "model download failed"; exit 1; }

df -h /workspace

# -----------------------------------------------------------------------------
# 5) run benchmark
echo "=== running benchmark ==="
python -m scripts.bench_translate_vllm \
    --model Lyte/tiny-aya-darija-v5 \
    --dataset HuggingFaceFW/fineweb-edu \
    --config sample-10BT \
    --n-samples 200 \
    --batches 8,16,32,64 \
    --max-model-len 4096 \
    --gpu-mem-util 0.90 \
    --out /workspace/bench_results.json

# -----------------------------------------------------------------------------
# 6) print + upload result
echo "=== RESULT ==="
cat /workspace/bench_results.json

# try to publish to HF as a private repo so you don't lose it if the box dies
python -m huggingface_hub.commands.huggingface_cli upload \
    "${HF_USER:-Lyte}/bench-aya-darija-5090" \
    /workspace/bench_results.json \
    "bench_$(date -u +%Y%m%d_%H%M).json" \
    --repo-type dataset \
    --token "$HF_TOKEN" \
    --create-pr=False 2>/dev/null \
    || echo "upload skipped (repo may not exist yet, local file saved)"

echo "=== DONE. copy bench_results.json contents ==="
