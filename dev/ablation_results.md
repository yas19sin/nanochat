# NanoChat Ablation Benchmark Results

**Date:** 2026-04-11 14:41

## Configuration

| Parameter | Value |
|---|---|
| Depth | 4 |
| n_embd | 256 |
| Vocab | 15947 |
| Seq Length | 64 |
| Batch Size | 2 |
| Steps | 500 |
| LR | 0.0003 (cosine decay, 50 warmup) |
| Device | CPU |
| Data | Darija (Lyte/darija-pretraining-corpus, pure subset) |
| Train batches | 60 unique, cycled |

## Summary

| Config | Params | Init Loss | Final Train | Final Val | Best Val (step) | BPT (val) | Improvement | Converge@50% | Train-Val Gap | Time |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 19,529,810 | 9.6767 | 4.5771 | 10.2392 | 9.0927 (75) | 14.7720 | 52.7% | 197 | 5.6621 | 72s |
| attnres_only | 19,534,418 | 9.6767 | 4.5159 | 10.2342 | 9.0650 (50) | 14.7648 | 53.3% | 197 | 5.7183 | 91s |
| engram_only | 28,199,506 | 9.6782 | 4.5325 | 10.0805 | 9.0911 (50) | 14.5430 | 53.2% | 197 | 5.5480 | 88s |
| attnres_engram | 28,204,114 | 9.6782 | 4.5446 | 10.0775 | 9.0656 (50) | 14.5387 | 53.0% | 197 | 5.5328 | 97s |
| baseline_matched | 28,085,074 | 9.6783 | 4.1052 | 10.3170 | 9.0047 (50) | 14.8843 | 57.6% | 179 | 6.2118 | 91s |

## Relative Performance (vs baseline)

| Config | Val Loss Delta | Val BPT Delta | Speed Overhead |
|---|---|---|---|
| baseline | +0.0000 | +0.0000 | +0.0% |
| attnres_only | -0.0050 | -0.0072 | +27.0% |
| engram_only | -0.1587 | -0.2290 | +21.9% |
| attnres_engram | -0.1617 | -0.2333 | +34.9% |
| baseline_matched | +0.0779 | +0.1123 | +26.1% |

## Gradient Norms by Component (step 250)

| Config | Trunk | Embed | AttnRes | Engram |
|---|---|---|---|---|
| baseline | 0.3529 | 0.9356 | 0.0000 | 0.0000 |
| attnres_only | 0.2365 | 0.6553 | 0.7174 | 0.0000 |
| engram_only | 0.3159 | 0.9486 | 0.0000 | 0.0193 |
| attnres_engram | 0.1853 | 0.2148 | 0.9589 | 0.0069 |
| baseline_matched | 0.5420 | 0.8403 | 0.0000 | 0.0000 |

## Diagnostics (step 250)

### Engram Gate Activations

| Config | Layer 0 | Layer 1 | Layer 2 | Layer 3 |
|---|---|---|---|---|
| engram_only | 0.5150303244590759 | - | 0.5274338126182556 | - |
| attnres_engram | 0.503882646560669 | - | 0.5123903155326843 | - |

### Per-Layer Gradient Norms

| Config | Layer 0 | Layer 1 | Layer 2 | Layer 3 |
|---|---|---|---|---|
| baseline | 0.281306 | 0.151413 | 0.090118 | 0.089733 |
| attnres_only | 0.174036 | 0.128582 | 0.065799 | 0.046751 |
| engram_only | 0.219477 | 0.143014 | 0.074160 | 0.104368 |
| attnres_engram | 0.108755 | 0.109312 | 0.065480 | 0.048600 |
| baseline_matched | 0.327213 | 0.232092 | 0.203668 | 0.146150 |

## Loss Curves (sampled every 50 steps)

```
 Step          baseline      attnres_only       engram_only    attnres_engram  baseline_matched             (val)
-----------------------------------------------------------------------------------------------------------
    0            9.6767            9.6767            9.6782            9.6782            9.6783            9.6767
   50            8.4043            8.3406            8.3909            8.3417            8.0312            9.0656
  100            5.9857            5.9753            5.9859            5.9598            5.7967            9.4387
  150            5.5710            5.5956            5.5801            5.5778            5.1699            9.4412
  200            5.2852            5.1963            5.2762            5.2273            4.8790            9.4915
  250            5.3977            5.3385            5.3809            5.4005            5.0688            9.6654
  300            4.9945            4.9860            4.9578            4.8911            4.5547            9.9744
  350            4.9798            5.0003            5.0031            5.0448            4.6367            9.7521
  400            4.1299            4.0401            4.0931            4.0562            3.6371           10.0896
  450            4.3359            4.3484            4.2922            4.2906            3.8372            9.9215
  499            4.5771            4.5159            4.5325            4.5446            4.1052           10.0775
```

## Notes

- **BPT** = bits per token = cross-entropy loss / ln(2)
- **Converge@50%** = first step where train loss < 50% of initial
- **Train-Val Gap** = final val loss - final train loss (overfitting indicator)
- Data: Darija (Lyte/darija-pretraining-corpus, pure subset, 15947 vocab BPE)
- 60 unique train batches cycled over 500 steps — real language modeling
- AttnRes adds ~4.6K params (negligible). Engram adds ~8.7M params (hash tables)
