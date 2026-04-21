# RTX 5090 Architecture Ablation — Results

**Date:** 2026-04-21
**Hardware:** 1× RTX 5090 (32 GB, SM 12.0), torch 2.9.1+cu128
**Config:** depth=8, seq=1024, total_batch=262144, iters=4000 (~1.05B tokens/run)
**Data:** mixed Darija (Lyte/darija-translation-data fineweb, Lyte/darija-pretraining-corpus pure)
          + English (HuggingFaceFW/finephrase) ≈ 1.5M docs after streaming
**Tokenizer:** vocab=32768, trained on mixed corpus; compression on fwe-train = 6.43 bytes/tok
          (+71% vs GPT-2, +61% vs GPT-4 — excellent for this domain)

## Summary

| run                    | min val bpb | final val bpb | CORE    | time     | peak VRAM |
|------------------------|-------------|---------------|---------|----------|-----------|
| d8_baseline (seed=42)  | **1.087**   | —             | —       | 37 min   | 4.8 GB    |
| d8_baseline_s2 (seed=2)| 1.105       | 1.135         | 0.0676  | 37 min   | 4.8 GB    |
| d8_attnres             | 1.123       | 1.136         | 0.0764  | 61 min   | 7.6 GB    |
| d8_engram              | 1.071 *    | 1.204         | 0.0736  | 131 min  | 15.4 GB   |
| d8_all                 | — (killed at step 1094, ETA 3.5h, tracking engram trajectory) |

`*` Engram's min bpb occurred early; final eval bpb regressed to 1.204 (train bpb 1.513 > val 1.204
    is an anomalous memorization signature).

## Noise floor

Seed-swap on baseline: **Δ val_bpb = 0.048** → any architecture effect must exceed ~0.05 bpb to be
real signal. None of the variants cleared this bar.

## Decision: ship plain **baseline** for 1B/10B production run

Reasons:
1. Beat (or matched) every variant on val_bpb within noise at 100M.
2. **3.5× faster than engram, 1.65× faster than attnres.** Compounds at 1B scale → saves ~$5-15 of
   H100 time.
3. **3× less VRAM than engram** → headroom for longer seq or larger batch at 1B.
4. No stability issues. Engram showed train>val bpb (hash memorization) which would get worse at
   10B tokens.
5. Simplest → fewer moving parts when a multi-hour H100 run goes wrong.

## Darija tokenizer sanity

| sample            | bytes | tokens | bytes/tok | chars/tok |
|-------------------|-------|--------|-----------|-----------|
| Darija Arabic     | 104   | 13     | 8.00      | **4.38**  |
| Darija Latin      | 63    | 31     | 2.03      | 2.03      |
| fwe-train (real)  | 2.5MB | 388k   | 6.43      | —         |

Darija Arabic at 4.38 chars/tok is essentially word-level compression — the tokenizer is keeping
this one for the production run.

## Artifacts

- `base_eval/base_model_004000.csv` — CORE task breakdown for the last run (engram)
- Tokenizer + wandb logs were on the cloud box but the machine was stopped before full rsync;
  the essential numbers are captured here.
