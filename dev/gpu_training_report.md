# NanoChat Darija — RTX 4050 GPU Training Report

**Date:** 2026-04-11
**Run ID:** `darija_4050` (wandb: `offline-run-20260411_194332-6rsowo1u`)
**Status:** Completed successfully (1000/1000 steps)

---

## Hardware & Environment

| | |
|---|---|
| GPU | NVIDIA RTX 4050 Laptop (6,141 MiB VRAM) |
| OS | Windows 11 |
| Python | 3.13.3 |
| PyTorch | 2.9.1+cu128 |
| Flash Attention | SDPA fallback (no FA3 — Triton unavailable on Windows) |
| torch.compile | Disabled (`TORCHDYNAMO_DISABLE=1`) |
| Precision | bf16 (via manual casting, no autocast) |

## Model Architecture

| Parameter | Value |
|---|---|
| Depth (`n_layer`) | 8 |
| Width (`n_embd`) | 512 |
| Heads | 8 × 64 head_dim |
| Window pattern | `L` (all global attention) |
| Vocab size | 32,768 (RustBPE, retrained on Darija) |
| Sequence length | 512 |
| **Total params** | **134,779,818** (134.8M) |
| — Trunk | 125,829,546 (93.4%) |
| — Engram | 8,932,864 (6.6%) |
| — AttnRes | 17,408 (0.01%) |

### Feature Config

| Feature | Setting |
|---|---|
| Attention Residuals | `use_attn_res=True`, 4 blocks |
| Engram | `use_engram=True`, table=16384, 2 heads, dim=128, 3-gram |
| Residual structure | Per-layer `resid_lambdas` + `x0_lambdas` + smear + backout |
| Value embeddings | Alternating layers, 3×sigmoid gating |

## Training Config

| Parameter | Value |
|---|---|
| Total batch size | 8,192 tokens |
| Device batch size | 4 |
| Gradient accumulation | 4 steps |
| Iterations | 1,000 |
| Warmup | 40 steps |
| Warmdown ratio | 0.65 (starts at step 350) |
| Final LR fraction | 0.05 |
| Matrix LR | 0.02 (Muon) |
| Scalar LR | 0.5 (AdamW) |
| Embedding LR | 0.3 |
| Weight decay | 0.28 |
| Optimizer | MuonAdamW (Muon for matrices, AdamW for scalars/embeddings) |

## Data

| | |
|---|---|
| Source | `Lyte/darija-pretraining-corpus` (pure subset) |
| Train | 100,000 docs (38.4 MB parquet) |
| Validation | 5,000 docs (0.3 MB parquet) |
| Tokenizer | RustBPE, vocab=32,768, trained on corpus |
| Tokens processed | 8,192,000 (1000 × 8192) |
| Token/param ratio | **0.06×** (extremely data-starved) |
| Epochs | ~1.3 (data cycled with reshuffling) |

## Results

### Validation BPB Curve

| Step | val_bpb | Δ from init |
|---|---|---|
| 0 | 2.142 | — |
| 100 | 1.693 | −0.449 (−21.0%) |
| 200 | **1.293** | −0.849 (−39.6%) |
| 300 | 1.338 | −0.804 (−37.5%) |
| 400 | 1.348 | −0.794 (−37.1%) |
| 500 | 1.338 | −0.804 (−37.5%) |
| 600 | 1.320 | −0.822 (−38.4%) |
| 700 | 1.324 | −0.818 (−38.2%) |
| 900 | 1.311 | −0.831 (−38.8%) |
| 1000 | 1.309 | −0.833 (−38.9%) |

**Best val_bpb: 1.293** (step 200)
**Final val_bpb: 1.309** (step 1000)

### Training Loss Curve

| Phase | Steps | Loss Range | Notes |
|---|---|---|---|
| Init | 0 | 10.40 | Random initialization |
| Fast descent | 0–100 | 10.40 → 7.56 | Rapid learning |
| Convergence | 100–350 | 7.56 → 5.10 | Steady improvement |
| Epoch boundary | 350–400 | 5.10 → 5.43 | Data reshuffle spike |
| Recovery 1 | 400–725 | 5.43 → 4.07 | New minimum |
| Epoch boundary 2 | 725–800 | 4.07 → 5.43 | Second reshuffle spike |
| Recovery 2 | 800–1000 | 5.43 → 4.12 | Final convergence |

**Final smooth train loss: 4.21**

### Performance

| Metric | Value |
|---|---|
| Wall time | **13.38 minutes** |
| Avg throughput | ~10,080 tok/sec |
| Peak throughput | ~10,162 tok/sec (cold GPU) |
| Min throughput | ~9,843 tok/sec (thermal throttling) |
| Avg step time | 810 ms |
| Peak VRAM | **3,752 MiB** (61% of 6,141 MiB) |
| Total FLOPs | 2.29 × 10¹⁵ |
| bf16 MFU | 0.00% (reported, likely measurement issue with SDPA fallback) |

### Generated Text Samples (Step 1000)

```
<|bos|>The capital of France is the sing of the the sing of the the the the the the the
<|bos|>If yesterday was Friday, then tomorrow will been del.
السؤال والجواب: واش هاد الجملة صحيحة؟
الخيارات:
- الجملة
<|bos|>The planets of the solar system are: "راجل لابس قميجة كحلة لابس قميجة كحلة."
 واش هاد الجملة صحيحة؟
الخيارات:
<|bos|>If 5*x + 3 = 13, then x is a trov.
السؤال والجواب: 10 = 10 = 10
```

## Honest Assessment

### What worked

1. **AttnRes + Engram integration is stable.** Both features ran 1000 steps without numerical issues after dtype fixes. Gradients flow through all components (confirmed in earlier ablation).
2. **VRAM efficient.** 134.8M params fit comfortably in 3.75 GiB — only 61% of the RTX 4050's 6 GiB. Room to scale up.
3. **Throughput is solid.** ~10K tok/sec sustained, with only mild thermal throttling (3% dip). The laptop GPU handled the full run in under 14 minutes.
4. **Reproducible.** Three separate runs (attempts 8, 9, 10) all produced identical val_bpb at step 0/100/200 — confirms deterministic initialization and training.

### What didn't work well

1. **The model barely learned English.** Generated text is mostly repetitive ("the sing of the sing of") for English prompts. This is expected: 134M params on 8M tokens is a 0.06× token/param ratio — roughly 200× below the Chinchilla-optimal ~20× ratio. The model is catastrophically data-starved for English.
2. **Darija output is formulaic.** The model learned common Darija patterns ("واش هاد الجملة صحيحة؟", "الخيارات") but produces templated Q&A rather than coherent text. Again, data volume is the bottleneck.
3. **Overfitting is severe.** Best val_bpb occurred at step 200 (1.293) and never improved significantly afterward. The train-val gap widened throughout training. The two epoch boundary spikes (steps ~370, ~770) confirm the model is memorizing batch order.
4. **Epoch spikes are disruptive.** Each data reshuffle caused loss to spike from ~4.1 back to ~5.4, erasing ~200 steps of progress before recovering. With only 100K docs and 8192 batch size, each epoch is only ~370 steps.
5. **No Flash Attention 3.** Running SDPA fallback caused three separate dtype crashes during text generation (fixed in flash_attention.py and attn_res.py). This won't be an issue on Linux/H100 with proper FA3, but it consumed significant debugging time on Windows.

### Bugs Fixed During This Run

| # | File | Issue | Fix |
|---|---|---|---|
| 1 | `flash_attention.py` | SDPA query fp32 vs KV cache bf16 during generation | Cast q to k.dtype |
| 2 | `attn_res.py` | `w_l` (fp32) vs `K` (bf16) in logits einsum | Cast w_l to K.dtype |
| 3 | `attn_res.py` | softmax alpha (fp32) vs `V` (bf16) in weighted sum einsum | Cast alpha to V.dtype |

All three bugs share the same root cause: without autocast during text generation, model parameters remain fp32 while computed tensors are bf16. `torch.einsum` doesn't auto-promote types like standard ops do.

### Key Numbers for H100 Planning

| Metric | RTX 4050 (this run) | H100 projection |
|---|---|---|
| VRAM used | 3.75 GiB | ~3.75 GiB (same model) |
| VRAM available | 6 GiB | 80 GiB |
| Headroom | 2.25 GiB | 76 GiB |
| Throughput | 10K tok/sec | ~150K+ tok/sec (est.) |
| FA3 available | No (SDPA) | Yes |
| torch.compile | Disabled | Available |
| Max practical depth | ~8 | ~48+ |
| Max practical params | ~135M | ~1B+ |

### Recommendations

1. **Scale data first.** The 0.06× token/param ratio is the primary bottleneck. Before scaling the model, get to at least 5× (ideally 20×) tokens per parameter. For 134M params, that's ~670M–2.7B tokens.
2. **Longer warmup on H100.** With larger batch sizes and longer sequences, increase warmup beyond 40 steps.
3. **The dtype fixes are essential.** The three patches in `flash_attention.py` and `attn_res.py` must stay — they'll be needed on any system using SDPA fallback (e.g., CPU evaluation, older GPUs without FA3).
4. **Epoch boundary spikes suggest shuffling strategy matters.** Consider larger data pools or cross-epoch blending to smooth the transitions.

---

## Artifacts

| File | Path | Size |
|---|---|---|
| Model checkpoint | `~/.cache/nanochat/base_checkpoints/d8/model_001000.pt` | 354 MB |
| Optimizer state | `~/.cache/nanochat/base_checkpoints/d8/optim_001000_rank0.pt` | 613 MB |
| Metadata | `~/.cache/nanochat/base_checkpoints/d8/meta_001000.json` | <1 KB |
| wandb logs | `wandb/offline-run-20260411_194332-6rsowo1u/` | offline |
