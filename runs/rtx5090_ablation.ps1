# -----------------------------------------------------------------------------
# RTX 5090 (32 GB) — Darija FineWeb ablation (Windows PowerShell port)
#
# Runs 4 configs back-to-back with identical compute budget:
#   1) baseline  2) +AttnRes  3) +Engram  4) +AttnRes +Engram
#
# Usage:
#   .\runs\rtx5090_ablation.ps1
# -----------------------------------------------------------------------------

$ErrorActionPreference = "Stop"

# SECURITY: token embedded per user request. Rotate after use.
if (-not $env:HF_TOKEN) { throw "Set `$env:HF_TOKEN before running" }
$env:HUGGINGFACE_HUB_TOKEN = $env:HF_TOKEN

$env:OMP_NUM_THREADS = "1"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONPATH = "."
$env:TORCHDYNAMO_DISABLE = "1"  # Triton flaky on Windows
if (-not $env:NANOCHAT_BASE_DIR) { $env:NANOCHAT_BASE_DIR = "$env:USERPROFILE\.cache\nanochat" }
if (-not $env:NANOCHAT_DATA_DIR) { $env:NANOCHAT_DATA_DIR = "$env:NANOCHAT_BASE_DIR\darija_fineweb" }
New-Item -ItemType Directory -Force -Path $env:NANOCHAT_BASE_DIR | Out-Null

$WandbPrefix = if ($env:WANDB_RUN_PREFIX) { $env:WANDB_RUN_PREFIX } else { "darija_5090" }

# ---- venv ------------------------------------------------------------------
if (-not (Test-Path ".venv")) { python -m venv .venv }
$Py = ".\.venv\Scripts\python.exe"
& $Py -c "import datasets, pyarrow" 2>$null
if ($LASTEXITCODE -ne 0) { & $Py -m pip install -q "datasets>=2.19" pyarrow huggingface_hub }

# ---- 1) data prep ----------------------------------------------------------
$ValShard = Join-Path $env:NANOCHAT_DATA_DIR "zzz_val_00000.parquet"
if (-not (Test-Path $ValShard)) {
    Write-Host "=== Preparing Darija FineWeb data ==="
    & $Py -m scripts.darija_fineweb_prep
    if ($LASTEXITCODE -ne 0) { throw "data prep failed" }
} else {
    Write-Host "=== Darija FineWeb data present ==="
}

# ---- 2) tokenizer ----------------------------------------------------------
$TokBin = Join-Path $env:NANOCHAT_BASE_DIR "tok32768.bin"
if (-not (Test-Path $TokBin)) {
    Write-Host "=== Training tokenizer ==="
    & $Py -m scripts.tok_train
    if ($LASTEXITCODE -ne 0) { throw "tokenizer training failed" }
    & $Py -m scripts.tok_eval
} else {
    Write-Host "=== Tokenizer present ==="
}

# ---- 3) shared hyperparams -------------------------------------------------
$DEPTH = 10
$SEQ = 1024
$DEV_BS = 16
$TOT_BS = 262144
$ITERS = 3000
$WARMUP = 40

$CommonTrain = @(
    "--depth=$DEPTH",
    "--max-seq-len=$SEQ",
    "--window-pattern=L",
    "--device-batch-size=$DEV_BS",
    "--total-batch-size=$TOT_BS",
    "--num-iterations=$ITERS",
    "--warmup-steps=$WARMUP",
    "--eval-every=500",
    "--eval-tokens=524288",
    "--core-metric-every=-1",
    "--sample-every=1000",
    "--save-every=-1"
)

$CommonEval = @(
    "--device-batch-size=$DEV_BS",
    "--eval=core,bpb",
    "--split-tokens=524288",
    "--max-per-task=200"
)

function Run-One {
    param([string]$Tag, [string[]]$Extra)

    Write-Host ""
    Write-Host "================================================================"
    Write-Host "  [$Tag] starting | iters=$ITERS depth=$DEPTH seq=$SEQ"
    Write-Host "================================================================"

    & $Py -m nanochat.report reset

    & $Py -m scripts.base_train @CommonTrain @Extra "--model-tag=$Tag" "--run=${WandbPrefix}_${Tag}"
    if ($LASTEXITCODE -ne 0) { throw "training failed for $Tag" }

    & $Py -m scripts.base_eval "--model-tag=$Tag" @CommonEval
    if ($LASTEXITCODE -ne 0) { throw "eval failed for $Tag" }

    & $Py -m nanochat.report generate
    $Report = Join-Path $env:NANOCHAT_BASE_DIR "report.md"
    if (Test-Path $Report) {
        Copy-Item $Report (Join-Path $env:NANOCHAT_BASE_DIR "report_${Tag}.md") -Force
    }
}

# ---- 4) four runs (+ second-seed baseline as noise floor) ------------------
Run-One "d${DEPTH}_baseline" @()

Run-One "d${DEPTH}_baseline_s2" @("--seed=2")

Run-One "d${DEPTH}_attnres" @(
    "--use-attn-res", "--attn-res-num-blocks=4"
)

Run-One "d${DEPTH}_engram" @(
    "--use-engram", "--engram-table-size=32768",
    "--engram-n-heads=4", "--engram-embed-dim=256"
)

Run-One "d${DEPTH}_all" @(
    "--use-attn-res", "--attn-res-num-blocks=4",
    "--use-engram", "--engram-table-size=32768",
    "--engram-n-heads=4", "--engram-embed-dim=256"
)

Write-Host ""
Write-Host "=================================================================="
Write-Host " Ablation complete. Per-run reports in $env:NANOCHAT_BASE_DIR :"
Write-Host "   report_d${DEPTH}_baseline.md"
Write-Host "   report_d${DEPTH}_baseline_s2.md   <- noise floor"
Write-Host "   report_d${DEPTH}_attnres.md"
Write-Host "   report_d${DEPTH}_engram.md"
Write-Host "   report_d${DEPTH}_all.md"
Write-Host " If (attnres - baseline) or (engram - baseline) is smaller than"
Write-Host " |baseline - baseline_s2|, the result is within single-run noise."
Write-Host "=================================================================="
