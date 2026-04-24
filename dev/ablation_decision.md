# Ablation Decision: AttnRes + Engram (Dropped)

**Date:** 2026-04-24
**Decision:** Drop Block Attention Residuals (Chen et al. 2026) and Engram n-gram memory (TinyEngram 2026) from the Darija 1B pretraining plan. Proceed with pure upstream `karpathy/nanochat` baseline.

## Background

We experimented with two additions to vanilla nanochat:
- **AttnRes** (`nanochat/attn_res.py`, removed) — learned depth-wise softmax over block representations replacing fixed residual connections.
- **Engram** (`nanochat/engram.py`, removed) — hash-based n-gram memory with gated injection into layer outputs.

Both were integrated into `nanochat/gpt.py` behind `use_attn_res` / `use_engram` flags in `GPTConfig`.

## Evidence

### 1. CPU ablation (depth=4, 500 steps, 19–28M params)

See `dev/ablation_results.md`. With an **iso-parameter control** (`baseline_matched`, same param count as Engram by widening `n_embd`), the story collapsed:

| Config | Params | Val Loss | vs baseline |
|---|---|---|---|
| baseline | 19.5M | 10.239 | — |
| attnres_only | 19.5M | 10.234 | −0.005 (noise) |
| engram_only | 28.2M | 10.081 | −0.16 |
| attnres_engram | 28.2M | 10.078 | −0.16 |
| **baseline_matched** | **28.1M** | **10.317** | **+0.08** |

The "wins" came from the extra ~8.7M params, not the n-gram mechanism. The iso-param control was actually worse than plain baseline (wider model memorized train better, overfit harder).

### 2. RTX 4050 run (depth=8, 134M params, 1000 steps)

See `dev/gpu_training_report.md`. Full AttnRes + Engram enabled. Best `val_bpb` at **step 200** (1.293), no meaningful improvement through step 1000. Severe overfitting, epoch-boundary loss spikes from 4.1 → 5.4 on reshuffles. Output samples were formulaic. Data-starved (0.06× token/param), not a clean test — but also no signal that the extras were helping.

### 3. RTX 5090 ablation (commit `fbef27f`)

Mixed Darija + English data, 5 configs, CORE eval. Commit message explicitly: *"decision: ship baseline for 1B/10B production"*. Archived (removed) with this cleanup.

## Decision Rationale

1. Iso-param control in CPU ablation shows no mechanism-level benefit
2. Larger-scale 4050 and 5090 runs did not produce a signal worth the code complexity
3. Upstream `nanochat/gpt.py` is a fast-moving target (Karpathy landing fp8, batch-size autoscaling, etc.). Carrying 400+ lines of non-working divergence is expensive.
4. Darija performance is data-bound, not architecture-bound, at our compute scale

## Action Taken

**Removed files:**
- `nanochat/attn_res.py`
- `nanochat/engram.py`
- `runs/darija_attnres.sh`, `runs/darija_engram.sh`, `runs/darija_attnres_engram.sh`
- `runs/rtx5090_ablation.sh`, `.ps1`, `_smoke.ps1`
- `scripts/ab_test_params.py`, `scripts/ablation_data_prep.py`
- `ablation_results_5090/`

**Reset to upstream `karpathy/nanochat`:**
- `nanochat/gpt.py`
- `scripts/base_train.py`

**Preserved (historical):**
- `dev/ablation_results.md`, `dev/ablation_results.json`, `dev/ablation_changelog.md`, `dev/gpu_training_report.md` — experimental record
- `Attention-Residuals/`, `TinyEngram/` — gitignored reference repos, kept locally for reference

**Safety:** git branch `pre-attnres-removal` points at HEAD before the cleanup commit. If we ever want to revisit, it's one `git checkout` away.

## Going Forward

- Baseline 1B run uses `runs/darija_baseline.sh` (depth=21, upstream architecture, no extras).
- Data gap to close: extend `scripts/darija_data_prep.py` to include `Lyte/fineweb-edu-darija-translated` alongside `Lyte/darija-pretraining-corpus`.
- Future architecture experiments: evaluate against a meaningful iso-param, iso-data, iso-compute baseline before integrating.
