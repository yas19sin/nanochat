# AtlasionNano Runbook

This document captures the full Darija NanoChat workflow we went through: dataset construction, tokenizer checks, base pretraining experiments, SFT/debugging, downstream heads, validation rebuild, and the commands to rerun the important pieces.

The project goal is a small but useful Moroccan Darija-first NanoChat family:

- a base LM trained on Darija, Arabic, and English text
- an instruction-tuned assistant variant
- downstream variants for classification, embeddings, sentence similarity, feature extraction, and zero-shot classification
- a future full-data model around the byte-optimal size for the corpus

## Repository State

Key scripts added or changed during this work:

| Path | Purpose |
| --- | --- |
| `scripts/build_pretraining_mix.py` | Builds the full Darija/Arabic/English pretraining shard directory. |
| `scripts/clean_pretraining_val.py` | Cleans an existing validation parquet. Useful for old runs, but does not create a new train holdout. |
| `scripts/rebuild_pretraining_validation.py` | Builds a fresh validation set from train shards and rewrites train without held-out rows. Use this for new full-data runs. |
| `scripts/count_pretraining_tokens.py` | Exact token count pass with BOS prepended per document. |
| `scripts/tokenizer_efficiency_report.py` | Corpus/source tokenizer efficiency report, plus sample-only tokenizer inspection. |
| `scripts/build_darija_focus_pretrain.py` | Builds a 1.5B-token Darija/Arabic-focused subset for small-model experiments. |
| `scripts/chat_sft.py` | SFT trainer for Darija chat, toxicity classification, and mixed tasks. |
| `tasks/darija_sft.py` | Darija instruction/Tulu/structured-QA SFT tasks. |
| `scripts/export_hf.py` | Exports NanoChat checkpoints to HF-compatible Transformers custom-code repos. |
| `nanochat/downstream_heads.py` and related scripts | Classification and embedding-head training/export support. |

Do not stage unrelated local files such as `.gitignore`, `.claude/settings.local.json`, paper `.txt` files, or `local_artifacts/` unless you explicitly want them in git.

## Timeline

### 1. Full Pretraining Dataset

We built a flat NanoChat parquet directory:

- numeric train files sort first
- final file is `zzzz_val_mix.parquet`
- every parquet contains exactly one column: `text`

The full exact token count completed successfully:

```json
{
  "docs": 97127102,
  "tokens": 22494048032,
  "utf8_bytes": 103323134057,
  "splits": {
    "train": {
      "docs": 97123253,
      "chars": 87841532720,
      "utf8_bytes": 103320712859,
      "tokens": 22493640355
    },
    "validation": {
      "docs": 3849,
      "chars": 1337564,
      "utf8_bytes": 2421198,
      "tokens": 407677
    }
  }
}
```

The old validation split was tiny and noisy. We later added `scripts/rebuild_pretraining_validation.py` so new runs can use a larger held-out validation split and remove those rows from train.

### 2. Source Lineup

Full train exact token distribution:

| Group/source | Exact train tokens | Share |
| --- | ---: | ---: |
| English total | 16,912,093,265 | 75.19% |
| Arabic raw | 2,865,645,426 | 12.74% |
| Darija total | 2,715,901,664 | 12.07% |
| Arabic + Darija | 5,581,547,090 | 24.81% |

Source-level details:

| Source | Tokens |
| --- | ---: |
| `eng_general_finepdfs_dclm_fwe` | 13,598,306,726 |
| `eng_code_nemotron` | 1,115,295,414 |
| `eng_math_stackexchange` | 524,270,210 |
| `eng_reason_algorithmic` | 177,687,479 |
| `eng_reason_formal_logic` | 125,775,647 |
| `eng_reason_economics` | 73,364,291 |
| `eng_reason_multiple_choice` | 1,297,393,498 |
| `arabic_raw` | 2,865,645,426 |
| `darija_bilingual` | 1,049,370,668 |
| `darija_pure` | 318,187,114 |
| `darija_fineweb_edu_clean` | 1,348,343,882 |

This is not a Darija-only dataset. It is a Darija-targeted multilingual mix. The two papers we inspected support this: low-resource target languages benefit from high-resource auxiliary-language mixing, and byte-level data size is a better planning unit than token count.

### 3. Tokenizer

Tokenizer repo:

```text
Lyte/darija-nanochat-tokenizer-32k
```

Tokenizer properties:

- RustBPE/tiktoken tokenizer
- vocab size: `32,768`
- special tokens:
  - `<|bos|>`
  - `<|user_start|>`
  - `<|user_end|>`
  - `<|assistant_start|>`
  - `<|assistant_end|>`
  - `<|python_start|>`
  - `<|python_end|>`
  - `<|output_start|>`
  - `<|output_end|>`

Local sample-only tokenizer measurements, without BOS:

| Sample | Tokens | chars/token | bytes/token |
| --- | ---: | ---: | ---: |
| Arabic-script Darija | 11 | 4.00 | 7.27 |
| MSA Arabic | 16 | 3.50 | 6.38 |
| Mixed Darija/English | 11 | 3.91 | 5.18 |
| English | 17 | 3.77 | 3.77 |
| Latin Darija | 19 | 2.11 | 2.11 |
| Code | 10 | 2.80 | 2.80 |

The bundled `tokenizer_eval.txt` showed this tokenizer is strong on Arabic/Darija validation-like text:

| Eval item | GPT-4 bytes/token | Ours bytes/token | Result |
| --- | ---: | ---: | --- |
| `fwe-val` | 2.56 | 5.44 | Ours much better |
| `math` | 2.20 | 2.30 | Ours slightly better |
| English/news/code | GPT-4 better | Ours lower | Expected for 32k vocab |

Vocab rough split from local inspection:

| Kind | Tokens |
| --- | ---: |
| Latin | 18,017 |
| Arabic | 13,185 |
| punctuation/symbol | 1,416 |
| digit | 125 |
| special | 9 |

Takeaway: keep this tokenizer for now. It is good for Arabic-script Darija and Arabic. The weakness is Latinized Darija, so later SFT/eval should include Latinized Darija if that input style matters.

Sample-only tokenizer report command:

```powershell
$env:PYTHONIOENCODING = "utf-8"
python -m scripts.tokenizer_efficiency_report `
  --samples-only `
  --tokenizer-dir "$env:NANOCHAT_TOKENIZER_DIR" `
  --inspect-vocab `
  --output-json "$env:NANOCHAT_TOKENIZER_DIR\local_tokenizer_report.json"
```

Full corpus tokenizer report, after exact counts exist:

```powershell
python -m scripts.tokenizer_efficiency_report `
  --data-dir "$env:NANOCHAT_DATA_DIR" `
  --tokenizer-dir "$env:NANOCHAT_TOKENIZER_DIR" `
  --inspect-vocab `
  --output-json "$env:NANOCHAT_DATA_DIR\tokenizer_efficiency_report.json"
```

### 4. Exact Token Counting

The exact counter prepends BOS per document, matching NanoChat base training:

```bash
python -m scripts.count_pretraining_tokens \
  --resume \
  --batch-size 8192 \
  --threads 16 \
  --progress-every 250000
```

If it is interrupted, rerun the same command with `--resume`. It skips counted files and continues.

### 5. Initial Smoke Training

We first ran very small base training jobs to verify:

- dataloader works
- tokenizer works
- model builds
- validation bpb runs
- checkpoints save/load
- sampling works

Example smoke command:

```bash
python -m scripts.base_train \
  --depth=4 \
  --model-tag=smoke_5090 \
  --run=dummy \
  --max-seq-len=512 \
  --device-batch-size=4 \
  --total-batch-size=32768 \
  --num-iterations=10 \
  --eval-every=5 \
  --eval-tokens=8192 \
  --core-metric-every=-1 \
  --sample-every=-1 \
  --save-every=-1 \
  --window-pattern=L
```

The smoke run showed a 36.7M param model running correctly. Later `d8` showed about 125.8M params.

### 6. Darija-First and Focused Experiments

We saw that curriculum ordering matters for small models. Starting with Darija shards helped early behavior, but a better small-model solution was the focused interleaved dataset.

Focused dataset build:

- train weights:
  - `darija_fineweb_edu_clean`: 4.0
  - `darija_pure`: 3.0
  - `darija_bilingual`: 2.5
  - `arabic_raw`: 1.0
- validation fractions:
  - `darija_fineweb_edu_clean`: 0.35
  - `darija_pure`: 0.25
  - `darija_bilingual`: 0.25
  - `arabic_raw`: 0.15
- target train size: about 1.5B approximate tokens
- output docs: 8,883,966
- chars: 6,013,289,867
- UTF-8 bytes: 10,747,971,699
- validation docs: 30,000

This focus run was nearly byte-optimal for the 125M model:

```text
focus bytes / d8 total params = 85.4
```

### 7. 125M Focus Base Training

Important d8 config:

```json
{
  "sequence_len": 512,
  "vocab_size": 32768,
  "n_layer": 8,
  "n_head": 4,
  "n_kv_head": 4,
  "n_embd": 512,
  "window_pattern": "L"
}
```

Parameter counts:

```text
wte                  : 16,777,216
value_embeds         : 67,108,864
lm_head              : 16,777,216
transformer_matrices : 25,166,016
total                : 125,829,354
```

The focused d8 run:

```bash
python -m scripts.base_train \
  --depth=8 \
  --model-tag=darija_d8_focus \
  --run=dummy \
  --max-seq-len=512 \
  --device-batch-size=32 \
  --total-batch-size=65536 \
  --num-iterations=23000 \
  --eval-every=1000 \
  --eval-tokens=32768 \
  --core-metric-every=-1 \
  --sample-every=-1 \
  --save-every=1000 \
  --window-pattern=L
```

Validation bpb progression:

```text
step 02000: 0.814585
step 04000: 0.795966
step 05000: 0.786104
step 06000: 0.781883
step 09000: 0.782648
step 10000: 0.774161
step 11000: 0.761461
step 14000: 0.738422
step 15000: 0.732272
step 19000: 0.707955
step 20000: 0.702013
step 21000: 0.696749
step 22000: 0.691758
step 23000: 0.687253
```

Peak memory was about 4.8GB on a 3090 at `device_batch_size=32`, `seq_len=512`.

Generation after this run was fluent but still hallucinated and repeated. This is expected for a small base LM. Sampling should stay conservative:

```text
temperature <= 0.3
top_k = 300
top_p = 0.9
min_p = 0.1
repetition_penalty = 1.1
```

### 8. Full Dataset vs 125M

For `d8`:

```text
total params: 125,829,354
full train tokens: 22,493,640,355
full train bytes: 103,320,712,859
tokens / total params = 178.76
bytes / total params = 821.12
```

By the byte-optimal paper, Arabic-like optimum is about `75.8 bytes/param`.

So full-data `d8` is roughly:

```text
821.12 / 75.8 = 10.8x overtrained
```

This is not compute-optimal, but it can still be useful as a small, heavily trained local model.

### 9. Byte-Optimal d24 Planning

`d24` in this NanoChat fork maps to:

```text
n_layer = 24
n_embd = 1536
n_head = 12
total params ~= 1,384,122,098
```

Full corpus:

```text
UTF-8 bytes = 103,323,134,057
bytes / d24 total params = 74.65
```

This is almost exactly the Arabic byte-optimal target from the paper:

```text
Arabic optimum ~= 75.8 bytes/param
multilingual optimum ~= 70 bytes/param
```

So the full 103GB dataset naturally fits a `d24` / 1.38B model.

The full corpus token horizon is:

```text
train tokens = 22,493,640,355
total batch = 2,097,152 tokens
iterations ~= 10,726
```

Use:

```text
--target-param-data-ratio 30.82
--total-batch-size 2097152
--num-iterations 10726
```

The default `--target-param-data-ratio=12` is not appropriate for this tokenizer/corpus/model plan.

## Paper Takeaways

### Mixture Pretraining Under Data Constraints

Local extract: `2605.13225v1.txt`.

Relevant lessons:

- Low-resource target-language pretraining should not rely only on repeated target-language text.
- Mixing in a high-resource auxiliary language can help more than heavy hyperparameter tuning.
- Target-language validation loss can understate the real downstream value of auxiliary-language mixing.
- A good shared tokenizer matters for Arabic-English transfer.

Our match:

- target: Darija
- auxiliaries: Arabic and English
- tokenizer: Darija/Arabic-biased 32k with decent English support
- full mix: enough high-resource text to reduce overfitting/repetition

### Compute Optimal Tokenization

Local extract: `2605.01188v1.txt`.

Relevant lessons:

- Data/model planning should use bytes, not just tokens.
- Tokenizer compression changes what a token means.
- English compute-optimal planning is about `60 bytes/param`.
- Arabic monolingual result is about `75.8 bytes/param`.
- Multilingual result is around `70 bytes/param`.
- Arabic optimal compression in the paper is about `4.58 bytes/token`.

Our aggregate full-corpus compression:

```text
103,323,134,057 bytes / 22,494,048,032 tokens = 4.59 bytes/token
```

That is almost exactly the paper's Arabic compression result. This is the strongest argument not to restart tokenizer training right now.

## Validation Rebuild

The old full validation was too small:

```text
3,849 docs
407,677 exact tokens
```

Use `scripts/rebuild_pretraining_validation.py` for new runs. It:

1. samples clean validation rows from existing train shards
2. writes `zzzz_val_mix.parquet`
3. rewrites train shards into a new output directory with held-out rows removed by normalized exact text match

Default validation mix:

```text
Darija: 50%
Arabic: 20%
English/code/math/reasoning: 30%
```

PowerShell:

```powershell
$env:NANOCHAT_BASE_DIR = "D:\nanochat-cache"
$SRC = "$env:NANOCHAT_BASE_DIR\pretrain_mix_darija_english"
$DST = "$env:NANOCHAT_BASE_DIR\pretrain_mix_darija_english_valfix"

python -m scripts.rebuild_pretraining_validation `
  --source-dir $SRC `
  --output-dir $DST `
  --target-docs 50000 `
  --overwrite
```

Then train against:

```powershell
$env:NANOCHAT_DATA_DIR = "$env:NANOCHAT_BASE_DIR\pretrain_mix_darija_english_valfix"
```

Important: this needs extra disk roughly equal to the dataset size. Avoid `--validation-only` for real training, because it does not remove held-out rows from train.

## Commands

### Pull Latest Code

PowerShell:

```powershell
git pull origin master
```

### Common Environment on Windows

Use actual paths for your machine:

```powershell
$env:NANOCHAT_BASE_DIR = "D:\nanochat-cache"
$env:NANOCHAT_DATA_DIR = "$env:NANOCHAT_BASE_DIR\pretrain_mix_darija_english_valfix"
$env:NANOCHAT_TOKENIZER_DIR = "$env:NANOCHAT_BASE_DIR\tokenizer"
$env:CUDA_VISIBLE_DEVICES = "0"
```

Download tokenizer if needed:

```powershell
hf download Lyte/darija-nanochat-tokenizer-32k --local-dir $env:NANOCHAT_TOKENIZER_DIR
```

### Full-Data 125M Pretrain on 6GB 4070 Laptop

This is intentionally overtrained for 125M. Use it when the goal is a small local model, not compute optimality.

Start conservative:

```powershell
python -m scripts.base_train `
  --depth=8 `
  --model-tag=atlastionnano_125m_full `
  --run=atlastionnano_125m_full `
  --max-seq-len=512 `
  --device-batch-size=4 `
  --total-batch-size=262144 `
  --num-iterations=85806 `
  --eval-every=1000 `
  --eval-tokens=1048576 `
  --core-metric-every=-1 `
  --sample-every=-1 `
  --save-every=5000 `
  --window-pattern=L
```

If memory allows, increase only the microbatch:

```powershell
--device-batch-size=8
```

Keep `--total-batch-size=262144`; changing microbatch only changes gradient accumulation and speed/memory.

### Full-Data d24 Pretrain on 8x H100/H200

Use Linux/bash on the instance.

Set environment:

```bash
export NANOCHAT_BASE_DIR=/workspace/nanochat-cache
export NANOCHAT_DATA_DIR=$NANOCHAT_BASE_DIR/pretrain_mix_darija_english_valfix
export NANOCHAT_TOKENIZER_DIR=$NANOCHAT_BASE_DIR/tokenizer
```

Run a 100-step smoke first:

```bash
torchrun --standalone --nproc_per_node=8 -m scripts.base_train \
  --depth=24 \
  --model-tag=atlastionnano_d24_smoke \
  --run=dummy \
  --max-seq-len=2048 \
  --device-batch-size=4 \
  --total-batch-size=2097152 \
  --num-iterations=100 \
  --target-param-data-ratio=30.82 \
  --eval-every=50 \
  --eval-tokens=4194304 \
  --core-metric-every=-1 \
  --sample-every=-1 \
  --save-every=-1 \
  --window-pattern=SSSL
```

Then full run:

```bash
torchrun --standalone --nproc_per_node=8 -m scripts.base_train \
  --depth=24 \
  --model-tag=atlastionnano_d24_base \
  --run=atlastionnano_d24_base \
  --max-seq-len=2048 \
  --device-batch-size=4 \
  --total-batch-size=2097152 \
  --num-iterations=10726 \
  --target-param-data-ratio=30.82 \
  --eval-every=250 \
  --eval-tokens=8388608 \
  --core-metric-every=-1 \
  --sample-every=1000 \
  --save-every=500 \
  --window-pattern=SSSL
```

H100 is fine. H200 is faster and has more memory, but H100 80GB should handle `d24`. If memory is clearly low, try `--device-batch-size=8`; if it OOMs, stay at `4`.

On non-Hopper GPUs, use `--window-pattern=L`; PyTorch SDPA does not support the sliding-window path efficiently.

### SFT on All Instruction Datasets

Use `--load-optimizer 0`. Loading the base optimizer state caused NaNs during an earlier smoke SFT, because the pretraining optimizer state is not a good SFT start.

For 125M full base:

```powershell
python -m scripts.chat_sft `
  --model-tag atlastionnano_125m_full `
  --model-step 85806 `
  --output-tag AtlasionNano-125M-Instruct-Full `
  --run AtlasionNano-125M-Instruct-Full `
  --max-seq-len 512 `
  --device-batch-size 4 `
  --total-batch-size 32768 `
  --eval-every 200 `
  --eval-tokens 262144 `
  --chatcore-every -1 `
  --save-every 1000 `
  --load-optimizer 0 `
  --init-lr-frac 0.1 `
  --darija-instruct-epochs 1 `
  --darija-tulu-epochs 4 `
  --darija-structured-epochs 1
```

For d24, change the model tag/step and likely run on the multi-GPU instance:

```bash
torchrun --standalone --nproc_per_node=8 -m scripts.chat_sft \
  --model-tag atlastionnano_d24_base \
  --model-step 10726 \
  --output-tag AtlasionNano-d24-Instruct \
  --run AtlasionNano-d24-Instruct \
  --max-seq-len 2048 \
  --device-batch-size 2 \
  --total-batch-size 262144 \
  --eval-every 200 \
  --eval-tokens 4194304 \
  --chatcore-every -1 \
  --save-every 1000 \
  --load-optimizer 0 \
  --init-lr-frac 0.1 \
  --darija-instruct-epochs 1 \
  --darija-tulu-epochs 1 \
  --darija-structured-epochs 1
```

When `--num-iterations` is omitted, `chat_sft.py` runs a full dataset-driven epoch over the mixed SFT dataset.

### Test Generation

Base model test:

```powershell
python -m scripts.chat_cli `
  -i base `
  -g atlastionnano_125m_full `
  -s 85806 `
  -p "المغرب هو" `
  -t 0.3 `
  -k 300 `
  --top-p 0.9 `
  --min-p 0.1 `
  --repetition-penalty 1.1
```

SFT prompt loop:

```powershell
$TAG = "AtlasionNano-125M-Instruct-Full"
$BEST_STEP = 1000

foreach ($p in @(
  "جاوبني بالدارجة: عطيني نصائح باش نتعلم البرمجة.",
  "شرح ليا بالدارجة الفرق بين الفصحى والدارجة المغربية.",
  "كتب ليا رسالة قصيرة بالدارجة كنعتذر فيها لصاحبي.",
  "ترجم للدارجة: I am going to the market tomorrow.",
  "صنف الجملة واش إيجابية ولا سلبية: هاد الخدمة ما عجباتنيش"
)) {
  "=============================="
  $p
  python -m scripts.chat_cli `
    -i sft `
    -g $TAG `
    -s $BEST_STEP `
    -p $p `
    -t 0.3 `
    -k 300 `
    --top-p 0.9 `
    --min-p 0.1 `
    --repetition-penalty 1.1
}
```

Do not use high temperature for these small checkpoints. `0.3` is a good default.

### Export to Hugging Face

Base export:

```powershell
python -m scripts.export_hf `
  --source base `
  --model-tag atlastionnano_125m_full `
  --step 85806 `
  --output-dir "$env:NANOCHAT_BASE_DIR\hf_exports\AtlasionNano-125M-Base-Full" `
  --tokenizer-dir "$env:NANOCHAT_TOKENIZER_DIR" `
  --dtype bfloat16 `
  --push-to-hub Lyte/AtlasionNano-125M-Base-Full `
  --private
```

SFT export:

```powershell
python -m scripts.export_hf `
  --source sft `
  --model-tag AtlasionNano-125M-Instruct-Full `
  --step 1000 `
  --output-dir "$env:NANOCHAT_BASE_DIR\hf_exports\AtlasionNano-125M-Instruct-Full" `
  --tokenizer-dir "$env:NANOCHAT_TOKENIZER_DIR" `
  --dtype bfloat16 `
  --base-model-repo Lyte/AtlasionNano-125M-Base-Full `
  --push-to-hub Lyte/AtlasionNano-125M-Instruct-Full `
  --private
```

Adjust `--step` to the checkpoint you actually want.

## SFT Data Notes

Instruction datasets used:

- `Lyte/Moroccan-Darija-Instruct-573K`
- `Lyte/Moroccan-Darija-Instruct-573K-English`
- `GemMaroc/TULU-3-50k-darija-english`
- `Lyte/darija-structured-translated`, column `dr`

The English companion dataset is opt-in. It has paired columns:

- `english_question` -> `darija_answer` for English-input/Darija-output QA
- `english_answer` -> `darija_answer` for direct translation supervision
- `english_question` -> `darija_question` for question translation supervision

For the 200M A100 checkpoint, the aggressive cross-lingual SFT attempt keeps
the 1K base context and adds both English QA and English-answer translation:

```bash
export NANOCHAT_BASE_DIR=/workspace/nanochat-cache
export HF_TOKEN="$HF_TOKEN"

torchrun --standalone --nproc_per_node=8 -m scripts.chat_sft -- \
  --model-source base \
  --model-tag d10_darija_a100_annealed \
  --model-step 27529 \
  --output-tag AtlasionNano-200M-Instruct-b64k-enqa-trans \
  --run AtlasionNano-200M-Instruct-b64k-enqa-trans \
  --task darija_chat \
  --num-iterations -1 \
  --max-seq-len 1024 \
  --device-batch-size 8 \
  --total-batch-size 65536 \
  --eval-every 500 \
  --eval-tokens 4194304 \
  --chatcore-every -1 \
  --save-every 500 \
  --load-optimizer 0 \
  --init-lr-frac 0.05 \
  --darija-instruct-epochs 1 \
  --darija-english-qa-epochs 1 \
  --darija-english-translate-answer-epochs 1 \
  --darija-english-translate-question-epochs 0 \
  --darija-tulu-epochs 1 \
  --darija-structured-epochs 1
```

Use `--darija-english-max-examples 100000` to do a smaller probe before the
full paired run.

The structured dataset rows use Markdown blocks:

```text
### Question
...
### Answer
...
```

We fixed the loader so split slicing happens after filtering parseable Q/A rows. This avoids the earlier error where `train[99%:]` contained no parseable rows.

Toxic classification dataset plan:

- source: `mteb/toxic_conversations_50k`
- translate to Darija with `scripts/translate_toxic_conversations_darija.py`
- train either as generative classification prompts or with the downstream classification head

For classification/similarity/feature extraction, a task-specific head is usually better than forcing every task through the LM head.

## Pitfalls

- Validation must be a real holdout. Use `scripts/rebuild_pretraining_validation.py`, not just `clean_pretraining_val.py`, for new full-data runs.
- On Windows PowerShell, use backticks for multi-line commands, not backslashes.
- Keep `NANOCHAT_DATA_DIR` pointed at the validation-fixed data directory for new pretraining.
- Keep `NANOCHAT_TOKENIZER_DIR` pointed at the downloaded 32k tokenizer.
- For SFT from a base checkpoint, use `--load-optimizer 0`.
- For 125M laptop runs, change microbatch if OOM, not total batch.
- For non-Hopper GPUs, use `--window-pattern=L`.
- For H100/H200, `SSSL` is fine if Flash Attention 3 is installed and active.
- Small base models will hallucinate. Judge them with controlled prompts and low-temperature sampling.
- Validation bpb alone is not enough; native Darija evals and downstream tests matter.
- Full dataset is byte-optimal for d24, not d8. Running d8 on full data is a deliberate overtraining/local-model choice.

## Recommended Next Steps

1. Pull latest code on the training machine.
2. Rebuild validation into `pretrain_mix_darija_english_valfix`.
3. Run 125M full-data pretrain on the laptop only if you accept the long overtrained run.
4. For a serious full-data base model, use 8x H100/H200 and train `d24`.
5. SFT with all instruction datasets using `--load-optimizer 0`.
6. Export base and SFT checkpoints to private HF repos.
7. Build task-specific downstream models using the classification/embedding heads.
