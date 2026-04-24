# NanoChat — Copilot Instructions

## Project Overview

NanoChat is a minimal full-stack LLM training harness (fork with Darija/Moroccan Arabic extensions). Single complexity dial: `--depth` controls layers, width, heads — everything else auto-scales. Built for single GPU nodes, designed to be hackable.

This fork tracks `karpathy/nanochat` upstream. Core model code (`nanochat/gpt.py`, `scripts/base_train.py`) is kept **identical to upstream** — Darija work lives in separate data prep, task, SFT, and run scripts.

## Architecture

- **`nanochat/gpt.py`** — Core model: `GPTConfig`, `GPT`, `Block`, `CausalSelfAttention`, `MLP`. Upstream-identical.
- **`nanochat/engine.py`** — Training engine (gradient accumulation, DDP, scheduling).
- **`nanochat/flash_attention.py`** — FA3 on Hopper+ / PyTorch SDPA fallback. Small dtype fix for SDPA generation.
- **`nanochat/dataset.py`** — Parquet shard loader. Reads `$NANOCHAT_DATA_DIR` (Darija-friendly override).
- **`nanochat/modeling_nanochat.py`, `configuration_nanochat.py`** — HF-compatible model wrapper for export.
- **`scripts/base_train.py`** — Pretraining entry point. Upstream-identical.
- **`scripts/tok_train.py`** — Tokenizer training. Extended with subset-weighted sampling for mixed Darija/English corpora.
- **`scripts/chat_sft.py`** — SFT entry point. Extended with Darija SFT dataset options.
- **`scripts/darija_data_prep.py`, `darija_fineweb_prep.py`** — Darija pretraining data prep.
- **`scripts/translate_*.py`, `scripts/translate_vllm_darija.py`** — vLLM translation pipeline (English → Darija).
- **`scripts/refine_darija.py`, `correct_vllm.py`** — Post-translation filtering/refinement.
- **`scripts/export_hf.py`, `diagnose_hf.py`** — HuggingFace export + diagnostics.
- **`tasks/darija_instruct.py`, `tasks/darija_sft.py`** — Darija evaluation / SFT tasks.
- **`runs/darija_baseline.sh`, `darija_prep.sh`, `darija.sh`** — Darija training run scripts.
- **`runs/vast_5090_*.sh`, `rtx4050.ps1`** — Cloud/local launchers.

## Code Style

- Python 3.10+. No type stubs, minimal type annotations — match existing style.
- `from dataclasses import dataclass` for configs. `nn.ModuleDict`/`nn.ModuleList` for dynamic module collections.
- Custom `Linear` class that casts weights to input dtype (replaces autocast).
- `norm(x)` is a bare function using `F.rms_norm` — no learnable params, runs in bf16.
- `print0()` from `nanochat.common` for rank-0-only printing in DDP.
- No bias in linear layers. No learnable RMSNorm params.
- Prefer short, flat code with minimal abstraction. No ABC classes, no registries.

## Conventions

- **Upstream first**: Do not modify `nanochat/gpt.py` or `scripts/base_train.py` without explicit direction. They must stay in sync with `karpathy/nanochat` upstream so we can pull fixes cleanly.
- **Residual structure**: NanoChat does NOT use simple `x = x + block(x)`. It uses `x = resid_lambdas[i] * x + x0_lambdas[i] * x0` before each block, plus smear and backout.
- **Value embeddings**: Alternating layers via `has_ve(layer_idx, n_layer)`. Gated with input-dependent 3×sigmoid.
- **Sliding window attention**: Per-layer `window_sizes` from `window_pattern` string ("SSSL").
- **Meta device init**: `GPT.__init__` runs on meta device (shapes only). All real initialization happens in `init_weights()`.
- **Optimizer**: `MuonAdamW` / `DistMuonAdamW` — Muon for matrix params, AdamW for everything else.
- **Flash Attention**: `nanochat.flash_attention` auto-selects FA3 on Hopper+ or PyTorch SDPA fallback.

## Build & Test

```bash
uv sync --extra gpu --group dev   # Install with CUDA
uv sync --extra cpu --group dev   # Install CPU-only
pytest tests/                     # Run tests
python scripts/base_train.py --depth=6 --num-iterations=10  # Quick smoke test
```

## Key Constraints

- Parameter counting must be exact — `num_scaling_params()` must match `sum(p.numel())`. The assert will catch mismatches.
- Keep VRAM awareness — this codebase targets both consumer GPUs (6GB) and H100s (80GB). Configs must scale.
- Darija additions live in `scripts/darija_*.py`, `scripts/translate_*.py`, `tasks/darija_*.py`, `runs/darija*.sh`. Do not inline Darija-specific logic into upstream files.
- See `dev/ablation_decision.md` for the history of AttnRes / Engram experiments (removed — did not improve over baseline at our scale).
# NanoChat — Copilot Instructions

## Project Overview

NanoChat is a minimal full-stack LLM training harness (fork with Darija/Moroccan Arabic extensions). Single complexity dial: `--depth` controls layers, width, heads — everything else auto-scales. Built for single GPU nodes, designed to be hackable.

## Architecture

- **`nanochat/gpt.py`** — Core model: `GPTConfig`, `GPT`, `Block`, `CausalSelfAttention`, `MLP`. The forward loop lives in `GPT.forward()` and has a complex residual structure with per-layer `resid_lambdas`, `x0_lambdas`, smear, and backout.
- **`nanochat/attn_res.py`** — Block Attention Residuals (Chen et al., 2026). Replaces fixed residuals with learned depth-wise softmax over block representations.
- **`nanochat/engram.py`** — Engram conditional n-gram memory (TinyEngram, 2026). Hash-based O(1) lookup with gated injection.
- **`nanochat/engine.py`** — Training engine (gradient accumulation, DDP, scheduling).
- **`scripts/base_train.py`** — Main training entry point. All CLI args defined here.
- **`tasks/`** — Evaluation tasks (ARC, MMLU, GSM8K, HumanEval, custom Darija).
- **`Attention-Residuals/`** — READ-ONLY reference repo (MoonshotAI).
- **`TinyEngram/`** — READ-ONLY reference repo (AutoArk).

## Code Style

- Python 3.10+. No type stubs, minimal type annotations — match existing style.
- `from dataclasses import dataclass` for configs. `nn.ModuleDict`/`nn.ModuleList` for dynamic module collections.
- Custom `Linear` class that casts weights to input dtype (replaces autocast).
- `norm(x)` is a bare function using `F.rms_norm` — no learnable params, runs in bf16.
- `print0()` from `nanochat.common` for rank-0-only printing in DDP.
- No bias in linear layers. No learnable RMSNorm params in the main model (but AttnRes norms have learnable weights).
- Prefer short, flat code with minimal abstraction. No ABC classes, no registries.

## Conventions

- **Residual structure**: NanoChat does NOT use simple `x = x + block(x)`. It uses `x = resid_lambdas[i] * x + x0_lambdas[i] * x0` before each block, plus smear and backout. Understand this before modifying the forward loop.
- **Value embeddings**: Alternating layers via `has_ve(layer_idx, n_layer)`. Gated with input-dependent 3×sigmoid.
- **Sliding window attention**: Per-layer `window_sizes` from `window_pattern` string ("SSSL").
- **Meta device init**: `GPT.__init__` runs on meta device (shapes only). All real initialization happens in `init_weights()`.
- **Optimizer**: `MuonAdamW` / `DistMuonAdamW` — Muon for matrix params, AdamW for everything else. Parameter groups are carefully separated in `setup_optimizer()`.
- **Feature toggles**: Use `bool` flags in `GPTConfig` (e.g. `use_attn_res`, `use_engram`). When disabled, zero code path differences — no extra computation.
- **Flash Attention**: `nanochat.flash_attention` auto-selects FA3 on Hopper+ or PyTorch SDPA fallback.

## Build & Test

```bash
uv sync --extra gpu --group dev   # Install with CUDA
uv sync --extra cpu --group dev   # Install CPU-only
pytest tests/                     # Run tests
python scripts/base_train.py --depth=6 --num-iterations=10  # Quick smoke test
```

## Key Constraints

- Reference repos (`Attention-Residuals/`, `TinyEngram/`) are READ-ONLY. Port ideas into `nanochat/` source files.
- When adding experimental modules, add config flags so they can be toggled off for clean ablation.
- Parameter counting must be exact — `num_scaling_params()` must match `sum(p.numel())`. The assert will catch mismatches.
- Comments referencing papers are encouraged: `# AttnRes (Chen et al., 2026): ...`
- Keep VRAM awareness — this codebase targets both consumer GPUs (6GB) and H100s (80GB). Configs must scale.
