# Darija A100x8 Run Notes

## 2026-05-30 d10 A100x8 Curriculum Run

- Launcher: `runs/vast_a100x8_darija.sh`
- Dataset: `Lyte/darija-nanochat-pretrain-mix`
- Tokenizer: `Lyte/darija-nanochat-tokenizer-32k`
- Model tag: `d10_darija_a100`
- Hardware: Vast 8x A100 SXM4 80GB
- Batch: `device_batch_size=64`, `max_seq_len=1024`, `total_batch_size=524288`, `grad_accum=1`
- Throughput:
  - English/general/code/Darija shards: about `2.5M tok/sec`
  - Arabic raw block: often `1.4M-1.8M tok/sec`, likely CPU tokenization/packing bound

## Dataset Order

The base dataloader sorts parquet filenames lexicographically, uses all files except
the final parquet as train, and uses the final parquet as validation.

Observed sorted train order:

- `pq 0-8`: English general
- `pq 9-11`: English code
- `pq 12-14`: StackExchange math
- `pq 15`: algorithmic reasoning
- `pq 16`: formal logic
- `pq 17`: economics
- `pq 18-20`: multiple-choice reasoning
- `pq 21-81`: Arabic raw
- `pq 82-93`: Darija bilingual
- `pq 94-95`: Darija pure
- `pq 96-100`: Darija FineWeb Edu clean
- `zzzz_val_mix.parquet`: validation

The first data epoch wrapped at about `step 27528` when the log switched to
`epoch: 2 pq: 0`. With `save_every=1000`, the last checkpoint before wrap was
`step 27000`, and it was exported/evaluated as the usable checkpoint for this run.

## Final Evaluated Checkpoint

Checkpoint:

```text
/workspace/nanochat-cache/base_checkpoints/d10_darija_a100/model_027000.pt
```

Base eval at `step 27000`:

- Train BPB: `1.092973`
- Val BPB: `0.862431`
- CORE metric: `0.0813`

The checkpoint is a base LM, not SFT/instruct. It can produce Darija/Arabic text,
but it is not reliable for factuality, instruction following, safety, math, or code.

## Future Run Fix

Use `--stop-after-data-epoch=1` in `scripts.base_train`. It saves and stops as
soon as the dataloader completes the first full pass over the sorted train parquets,
before training on epoch 2 data. Keep `--num-iterations=46000` as the LR schedule
and safety ceiling unless intentionally retuning the schedule.

For a balanced multilingual/general model, rebuild the pretraining data with
`LAYOUT=interleaved`. For a Darija-ending curriculum, keep `LAYOUT=curriculum`.

## HF-Compatible Export

Use `scripts.export_hf` for a Transformers-compatible repo with custom remote code:

```bash
export NANOCHAT_BASE_DIR=/workspace/nanochat-cache
export NANOCHAT_DATA_DIR=/workspace/data/pretrain_mix_darija_english
export HUGGINGFACE_HUB_TOKEN="$HF_TOKEN"

python -m scripts.export_hf \
  --source base \
  --model-tag d10_darija_a100 \
  --step 27000 \
  --output-dir /workspace/nanochat-cache/hf_exports/nanochat-d10-darija-a100-base \
  --dtype bfloat16 \
  --push-to-hub Lyte/nanochat-d10-darija-a100-base \
  --private \
  --commit-message "Export d10 Darija A100 base checkpoint"
```
