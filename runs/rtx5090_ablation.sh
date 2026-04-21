#!/bin/bash
# -----------------------------------------------------------------------------
# Cloud RTX 5090 (32 GB, single GPU) — Darija FineWeb architecture ablation.
#
# Purpose: decide the winning arch (baseline / +AttnRes / +Engram / +both)
# before committing to a 1B-param, 8–10B-token production run.
#
# Tests 5 runs back-to-back on the SAME data / tokenizer / budget:
#   1) baseline                    (reference)
#   2) baseline_s2 (seed=2)        (single-seed NOISE FLOOR)
#   3) baseline + AttnRes
#   4) baseline + Engram
#   5) baseline + AttnRes + Engram
#
# Fairness invariants:
#   - fixed --num-iterations across all runs (Engram has more params; we do NOT
#     scale the horizon by param count).
#   - identical depth / seq / batch / warmup / schedule; only feature flags change.
#   - same tokenizer, same train/val shards.
#
# Usage (fresh cloud box):
#   git clone <repo> && cd nanochat
#   export HF_TOKEN=hf_xxx        # or use the fallback below
#   chmod +x runs/rtx5090_ablation.sh
#   ITERS=4000 WANDB_RUN_PREFIX=darija_5090 bash runs/rtx5090_ablation.sh
#
# Tunable via env:
#   ITERS              (default 4000, ~1.05B tokens @ batch=256K)
#   DEPTH              (default 10,  ~200M trunk params)
#   SEQ                (default 1024)
#   DEV_BS             (default 16)
#   TOT_BS             (default 262144)
#   WANDB_RUN_PREFIX   (default darija_5090)
#   WANDB_MODE         (set to 'offline' to avoid network / login)
# -----------------------------------------------------------------------------
set -euo pipefail

# --- HF token (user-provided fallback; rotate after ablation) ----------------
if [ -z "${HF_TOKEN:-}" ]; then echo "ERROR: set HF_TOKEN env var before running" >&2; exit 1; fi
export HUGGINGFACE_HUB_TOKEN="$HF_TOKEN"

export OMP_NUM_THREADS=1
export PYTHONUNBUFFERED=1
export NANOCHAT_BASE_DIR="${NANOCHAT_BASE_DIR:-$HOME/.cache/nanochat}"
export NANOCHAT_DATA_DIR="${NANOCHAT_DATA_DIR:-$NANOCHAT_BASE_DIR/ablation_mixed}"
mkdir -p "$NANOCHAT_BASE_DIR"

WANDB_RUN_PREFIX="${WANDB_RUN_PREFIX:-darija_5090}"

# -----------------------------------------------------------------------------
# Python env
if [ ! -d ".venv" ]; then
    if command -v uv &> /dev/null; then
        uv venv
        uv sync --extra gpu
    else
        python -m venv .venv
        # shellcheck disable=SC1091
        source .venv/bin/activate
        pip install -U pip
        pip install -e ".[gpu]" 2>/dev/null || pip install -e .
    fi
fi
# shellcheck disable=SC1091
source .venv/bin/activate

python -c "import datasets, pyarrow" 2>/dev/null || pip install -q "datasets>=2.19" pyarrow huggingface_hub

# -----------------------------------------------------------------------------
# Sanity check: CUDA visible and correct GPU
python - <<'PY'
import torch, sys
if not torch.cuda.is_available():
    print("ERROR: CUDA not available. Aborting."); sys.exit(1)
name = torch.cuda.get_device_name(0)
props = torch.cuda.get_device_properties(0)
mem = props.total_memory / 1e9
print(f"GPU: {name} | SM {props.major}.{props.minor} | {mem:.1f} GB | torch {torch.__version__}")
if mem < 20:
    print("WARNING: <20 GB VRAM detected. Hyperparams below assume ≥32 GB (5090/A100/H100).")
PY

# -----------------------------------------------------------------------------
# 1) Data prep — once.  Mixed: Darija (fineweb split of translation-data + pure
# subset of pretraining-corpus) + English (FinePhrase source text).
if [ ! -f "$NANOCHAT_DATA_DIR/zzz_val_00000.parquet" ]; then
    echo "=== Preparing mixed ablation data (Darija + English) ==="
    python -m scripts.ablation_data_prep
else
    echo "=== Ablation data already present at $NANOCHAT_DATA_DIR ==="
fi

# -----------------------------------------------------------------------------
# 2) Tokenizer — once.
if [ ! -f "$NANOCHAT_BASE_DIR/tok32768.bin" ]; then
    echo "=== Training tokenizer ==="
    python -m scripts.tok_train
    python -m scripts.tok_eval || true
else
    echo "=== Tokenizer already present at $NANOCHAT_BASE_DIR/tok32768.bin ==="
fi

# -----------------------------------------------------------------------------
# 3) Shared hyperparameters
#    depth=8     -> n_embd=512, 4 heads × 128 head_dim, ~105M trunk params
#    seq=1024, dev_bs=16, tot_bs=262144, iters=4000 => ~1.05B tokens (Chinchilla)
#    --window-pattern=L keeps SDPA efficient (FA3 is Hopper-only; not on 5090)
#
# Why depth=8 instead of 10?  Architecture ablation signal is cleanest near
# Chinchilla-optimal.  depth=8 at 1B tokens is past Chinchilla; depth=10 at 1B
# is undertrained.  Cost per run ~2x lower.  AttnRes/Engram gains are known to
# show at ≤200M so decisions here still transfer to your 1B production run.
# -----------------------------------------------------------------------------
DEPTH="${DEPTH:-8}"
SEQ="${SEQ:-1024}"
DEV_BS="${DEV_BS:-16}"
TOT_BS="${TOT_BS:-262144}"
ITERS="${ITERS:-4000}"
WARMUP="${WARMUP:-60}"

total_tokens=$(( ITERS * TOT_BS ))
total_tokens_b=$(python -c "print(f'{${total_tokens}/1e9:.2f}')")
echo "=== Plan: depth=$DEPTH seq=$SEQ iters=$ITERS => ${total_tokens_b}B tokens per run, 5 runs total ==="

COMMON_TRAIN=(
    --depth="$DEPTH"
    --max-seq-len="$SEQ"
    --window-pattern=L
    --device-batch-size="$DEV_BS"
    --total-batch-size="$TOT_BS"
    --num-iterations="$ITERS"
    --warmup-steps="$WARMUP"
    --eval-every=500
    --eval-tokens=1048576
    --core-metric-every=-1
    --sample-every=2000
    --save-every=-1
)

COMMON_EVAL=(
    --device-batch-size="$DEV_BS"
    --eval=core,bpb
    --split-tokens=1048576
    --max-per-task=200
)

run_one() {
    local tag="$1"
    shift
    local extra=("$@")

    echo ""
    echo "================================================================"
    echo "  [$tag] starting | iters=$ITERS depth=$DEPTH seq=$SEQ"
    echo "================================================================"
    local t0=$(date +%s)

    python -m nanochat.report reset

    python -m scripts.base_train \
        "${COMMON_TRAIN[@]}" "${extra[@]}" \
        --model-tag="$tag" \
        --run="${WANDB_RUN_PREFIX}_${tag}"

    python -m scripts.base_eval \
        --model-tag="$tag" \
        "${COMMON_EVAL[@]}"

    python -m nanochat.report generate || true
    if [ -f "$NANOCHAT_BASE_DIR/report.md" ]; then
        cp "$NANOCHAT_BASE_DIR/report.md" "$NANOCHAT_BASE_DIR/report_${tag}.md"
    fi
    local t1=$(date +%s)
    echo "  [$tag] done in $(( (t1 - t0) / 60 )) min"
}

# -----------------------------------------------------------------------------
# 4) Run the five configurations (baseline + noise-floor + 3 variants)
# -----------------------------------------------------------------------------
run_one "d${DEPTH}_baseline"

run_one "d${DEPTH}_baseline_s2" --seed=2

run_one "d${DEPTH}_attnres" \
    --use-attn-res --attn-res-num-blocks=4

run_one "d${DEPTH}_engram" \
    --use-engram --engram-table-size=32768 --engram-n-heads=4 --engram-embed-dim=256

run_one "d${DEPTH}_all" \
    --use-attn-res --attn-res-num-blocks=4 \
    --use-engram --engram-table-size=32768 --engram-n-heads=4 --engram-embed-dim=256

# -----------------------------------------------------------------------------
# 5) Aggregate summary — pulls val bpb + CORE out of each report_*.md
# -----------------------------------------------------------------------------
echo ""
echo "=================================================================="
echo " ABLATION SUMMARY (val bpb lower = better, CORE higher = better)"
echo "=================================================================="
python - <<PY
import os, re, glob
base = os.environ["NANOCHAT_BASE_DIR"]
paths = sorted(glob.glob(os.path.join(base, "report_d${DEPTH}_*.md")))
print(f"{'tag':<28} {'val_bpb':>10} {'CORE':>10}")
print("-" * 52)
rows = []
for p in paths:
    tag = os.path.basename(p)[len("report_"):-len(".md")]
    txt = open(p, encoding="utf-8", errors="ignore").read()
    m_bpb = re.search(r"val bpb[^\d-]*([-\d\.]+)", txt)
    m_core = re.search(r"CORE[^\d-]*([-\d\.]+)", txt)
    bpb = float(m_bpb.group(1)) if m_bpb else float("nan")
    core = float(m_core.group(1)) if m_core else float("nan")
    rows.append((tag, bpb, core))
    print(f"{tag:<28} {bpb:>10.4f} {core:>10.4f}")
print()
if len(rows) >= 2:
    b1 = next((r for r in rows if r[0].endswith("_baseline")), None)
    b2 = next((r for r in rows if r[0].endswith("_baseline_s2")), None)
    if b1 and b2:
        noise_bpb = abs(b1[1] - b2[1])
        noise_core = abs(b1[2] - b2[2]) if not (b1[2] != b1[2] or b2[2] != b2[2]) else float("nan")
        print(f"Noise floor |baseline - baseline_s2|: bpb={noise_bpb:.4f} CORE={noise_core:.4f}")
        print("A feature is a real win only if its delta vs baseline exceeds this.")
PY
echo "=================================================================="
echo " Per-run reports live in $NANOCHAT_BASE_DIR/report_d${DEPTH}_*.md"
echo "=================================================================="
