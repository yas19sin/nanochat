# -----------------------------------------------------------------------------
# LOCAL SMOKE TEST for runs/rtx5090_ablation.sh on an RTX 4050 (6 GB).
#
# Mirrors the real ablation: MIXED Darija + English data, 5 runs
# (baseline, baseline_s2, attnres, engram, all), eval=core,bpb.
# Scaled DOWN to tiny depth/iters/data so each run completes in ~1 min.
# Goal: validate that the exact same pipeline that will run on the 5090
# completes end-to-end here first.
#
# Usage:
#   .\runs\rtx5090_ablation_smoke.ps1
# -----------------------------------------------------------------------------
$ErrorActionPreference = "Stop"

if (-not $env:HF_TOKEN) { throw "Set `$env:HF_TOKEN before running" }
$env:HUGGINGFACE_HUB_TOKEN = $env:HF_TOKEN

$env:OMP_NUM_THREADS = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONPATH = "."
$env:TORCHDYNAMO_DISABLE = "1"
$env:WANDB_MODE = "offline"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# Dedicated smoke cache so we don't clobber the main run's cache
$SmokeRoot = "$env:USERPROFILE\.cache\nanochat_smoke"
$env:NANOCHAT_BASE_DIR = $SmokeRoot
$env:NANOCHAT_DATA_DIR = Join-Path $SmokeRoot "ablation_mixed"
New-Item -ItemType Directory -Force -Path $env:NANOCHAT_BASE_DIR | Out-Null

$Py = ".\.venv\Scripts\python.exe"

# ---- 1) TINY mixed data prep (all 3 sources, 5k rows each) -----------------
$ValShard = Join-Path $env:NANOCHAT_DATA_DIR "zzz_val_00000.parquet"
if (-not (Test-Path $ValShard)) {
    Write-Host "=== [smoke] Preparing TINY mixed ablation data (darija + english) ==="
    & $Py -m scripts.ablation_data_prep --max-rows 5000 --val-size 200 --shard-size 20000
    if ($LASTEXITCODE -ne 0) { throw "data prep failed" }
} else {
    Write-Host "=== [smoke] Mixed data present at $env:NANOCHAT_DATA_DIR ==="
}

# ---- 2) tiny tokenizer -----------------------------------------------------
$TokBin = Join-Path $env:NANOCHAT_BASE_DIR "tok32768.bin"
if (-not (Test-Path $TokBin)) {
    Write-Host "=== [smoke] Training tokenizer (small) ==="
    & $Py -m scripts.tok_train --max-chars 30000000 --doc-cap 4000
    if ($LASTEXITCODE -ne 0) { throw "tokenizer failed" }
} else {
    Write-Host "=== [smoke] Tokenizer present ==="
}

# ---- 3) shared TINY hyperparams (RTX 4050 / 6 GB) --------------------------
$DEPTH = 4
$SEQ = 512
$DEV_BS = 2
$TOT_BS = 4096
$ITERS = 40
$WARMUP = 5

$CommonTrain = @(
    "--depth=$DEPTH",
    "--head-dim=64",
    "--max-seq-len=$SEQ",
    "--window-pattern=L",
    "--device-batch-size=$DEV_BS",
    "--total-batch-size=$TOT_BS",
    "--num-iterations=$ITERS",
    "--warmup-steps=$WARMUP",
    "--eval-every=20",
    "--eval-tokens=4096",
    "--core-metric-every=-1",
    "--sample-every=-1",
    "--save-every=-1"
)

# core eval is slow on a 4050; keep it cheap but still exercise the path
$CommonEval = @(
    "--device-batch-size=$DEV_BS",
    "--eval=core,bpb",
    "--split-tokens=4096",
    "--max-per-task=20"
)

function Run-One {
    param([string]$Tag, [string[]]$Extra)
    Write-Host ""
    Write-Host "=== [smoke] $Tag ==="
    & $Py -m nanochat.report reset
    & $Py -m scripts.base_train @CommonTrain @Extra "--model-tag=$Tag" "--run=dummy"
    if ($LASTEXITCODE -ne 0) { throw "train failed: $Tag" }
    & $Py -m scripts.base_eval "--model-tag=$Tag" @CommonEval
    if ($LASTEXITCODE -ne 0) { throw "eval failed: $Tag" }
    & $Py -m nanochat.report generate
    $Report = Join-Path $env:NANOCHAT_BASE_DIR "report.md"
    if (Test-Path $Report) {
        Copy-Item $Report (Join-Path $env:NANOCHAT_BASE_DIR "report_${Tag}.md") -Force
    }
}

# ---- 4) run the 5 configs (same set as the real 5090 ablation) -------------
Run-One "smoke_baseline" @()

Run-One "smoke_baseline_s2" @("--seed=2")

Run-One "smoke_attnres" @(
    "--use-attn-res", "--attn-res-num-blocks=2"
)

Run-One "smoke_engram" @(
    "--use-engram", "--engram-table-size=4096",
    "--engram-n-heads=2", "--engram-embed-dim=128"
)

Run-One "smoke_all" @(
    "--use-attn-res", "--attn-res-num-blocks=2",
    "--use-engram", "--engram-table-size=4096",
    "--engram-n-heads=2", "--engram-embed-dim=128"
)

Write-Host ""
Write-Host "=== [smoke] All 5 configs completed. Pipeline is green on this GPU. ==="
Write-Host "Reports: $env:NANOCHAT_BASE_DIR\report_smoke_*.md"
