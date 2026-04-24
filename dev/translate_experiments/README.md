# dev/translate_experiments

Scratch folder for the Darija structured-data translation pipeline (code / toolcall / math).

## Scripts

- `translate_structured.py` — main pipeline. Per-domain processors: segment-based for code, per-turn for toolcall, placeholder-based for math. CLI: `--domain {code,toolcall,math,all} --n N --provider {lmstudio,deepseek}`.
- `verify_toolcall_compat.py` — sends Hermes-FC tool specs to a real tool-calling model (lfm2.5-350m) and compares emitted `tool_calls` against the dataset's ground-truth. Confirms the dataset's `<tool_call>` schema matches the OpenAI standard.
- `probe_domain_translate.py`, `probe_deepseek_toolcall.py` — earlier exploratory probes.
- `inspect_datasets.py` — quick dataset column/sample inspection.
- `count_corpus_exact.py`, `count_pretrain_corpus_tokens.py` — token counters for the existing Darija pretraining corpus (4.36 B tokens).

## Reports / logs

- `translate_structured_n100.md` — n=100 per-domain run. code 98/100, toolcall 100/100, math 90/100.
- `translate_structured_{code,toolcall,math,all}.md` — earlier n=10 / n=20 runs.
- `domain_translate_probe*.md` — initial feasibility probes (local Aya vs DS-flash).
- `n100_run.log`, `toolcall_compat.log` — raw stdout captures.
- `timings.csv` — per-row timing data extracted from the n=100 run.

## Throughput (tiny-aya-darija-v5 on LM Studio, serial)

| domain | avg time/row | drop rate |
|---|---|---|
| code | 12.6 s | 2% |
| toolcall | 7.3 s | 0% |
| math | 21.6 s | 10% |
