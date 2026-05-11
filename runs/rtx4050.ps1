# RTX 4050 Laptop GPU (6 GB VRAM) — single GPU Darija training
# depth=8 quick run, ~15 min
#
# Usage:
#   .\runs\rtx4050.ps1

$env:OMP_NUM_THREADS = "1"
$env:PYTHONPATH = "."
$env:WANDB_MODE = "offline"
$env:TORCHDYNAMO_DISABLE = "1"  # Triton not available on Windows
$env:NANOCHAT_DATA_DIR = "$env:USERPROFILE\.cache\nanochat\darija_data"

if (-not (Test-Path $env:NANOCHAT_DATA_DIR)) {
    Write-Error "NANOCHAT_DATA_DIR=$env:NANOCHAT_DATA_DIR does not exist. Run: python scripts/prep_benchmark_data.py first."
    exit 1
}

# Single-GPU training — depth=8 quick run
.\.venv\Scripts\python.exe scripts/base_train.py `
    --depth=8 `
    --head-dim=64 `
    --max-seq-len=512 `
    --window-pattern=L `
    --device-batch-size=4 `
    --total-batch-size=8192 `
    --num-iterations=1000 `
    --eval-every=100 `
    --eval-tokens=10240 `
    --core-metric-every=-1 `
    --sample-every=500 `
    --save-every=-1 `
    --warmup-steps=40 `
    --run=darija_4050
