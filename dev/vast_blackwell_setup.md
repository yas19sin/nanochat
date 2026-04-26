# Vast.ai RTX 5090 (Blackwell, sm_120) — vLLM bootstrap

Deployment notes from the structured-translation job, April 2026. These cover
the gotchas you hit on a fresh CUDA-13 / Blackwell Vast box so you don't lose
hours debugging next time.

## TL;DR — one-shot setup

```bash
# 0. sanity check the box
python --version
python -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda); print('cap', torch.cuda.get_device_capability(0))"
nvidia-smi --query-gpu=name,driver_version,compute_cap --format=csv
ls /usr/local/ | grep cuda

# Expect: torch 2.11+cu130, cap (12,0) RTX 5090, /usr/local/cuda-13.0 only

# 1. install vLLM nightly (matches torch cu13)
pip install --pre vllm --extra-index-url https://wheels.vllm.ai/nightly
pip install datasets pandas pyarrow
python -c "import vllm, torch; print('vllm', vllm.__version__, 'torch', torch.__version__, torch.version.cuda)"

# 2. fast HF download + cache placement
pip install -U "huggingface_hub[hf_transfer]" hf_transfer
export HF_HUB_ENABLE_HF_TRANSFER=1
export HF_HOME=/workspace/.hf_home               # CRITICAL — vLLM must read same cache
export HF_TOKEN=hf_xxx                            # read token
export HF_WRITE_TOKEN=hf_yyy                      # write token
hf download Lyte/tiny-aya-darija-v5

# 3. clone repo
cd /workspace
git clone https://github.com/yas19sin/nanochat.git
cd nanochat

# 4. launch in tmux (so SSH drop won't kill it)
tmux new -s struct
ATTENTION_BACKEND=FLASH_ATTN bash runs/darija_structured_vast.sh
# detach: Ctrl-b d   |   reattach: tmux attach -t struct
```

## Common failure modes & fixes

### 1. `ImportError: libcudart.so.12` on `import vllm`
**Cause:** stable vLLM (0.19.x) wheels are built against CUDA 12. Vast 5090
templates ship CUDA 13 only — no `libcudart.so.12` anywhere.

**Wrong fixes:**
- Symlinking `libcudart.so.13` → `.so.12` (will crash at runtime, ABI differs).
- `pip install nvidia-cuda-runtime-cu12 ...` — this works for import but causes
  hangs when stable vLLM 0.19.1 (cu12) shares CUDA context with PyTorch 2.10+
  (cu13). Different runtimes deadlock on first kernel launch.

**Right fix:** install vLLM **nightly**, which has cu13 wheels matching PyTorch:
```bash
pip uninstall -y vllm
pip install --pre vllm --extra-index-url https://wheels.vllm.ai/nightly
```

### 2. vLLM hangs silently after `Using AttentionBackendEnum.X backend.` log
**Cause:** vLLM is downloading the model from HF and **does not log progress**.
On a slow Vast network this looks identical to a deadlock — could be 10–15 min.

**How to diagnose:**
```bash
ls -la ~/.cache/huggingface/hub/ | grep <model_name>
# or check the EngineCore process state
cat /proc/$(pgrep -f EngineCore)/wchan; echo
```
- If model dir empty → it's downloading. Wait or use `hf download` first.
- If `wchan` shows `futex_wait_queue` for >5 min after weights cached → real hang.

**Right fix:** always `hf download <model>` BEFORE launching vLLM. Pre-warming
the cache makes vLLM init transparent (and the download has a progress bar).

### 3. `HF_HOME` mismatch — vLLM re-downloads the model
**Cause:** if you `hf download` with `HF_HOME=/workspace/.hf_home` set, but
launch vLLM without that env var, vLLM looks at `~/.cache/huggingface` (empty)
and re-downloads.

**Fix:** export `HF_HOME` in the same shell as the launcher, or globally in
`~/.bashrc` for the session.

### 4. `VLLM_ATTENTION_BACKEND` env var rejected as "Unknown"
**Cause:** in vLLM nightly, the attention backend is no longer set via env
var — it's an `LLM(...)` constructor kwarg (`attention_backend="FLASH_ATTN"`).

**Fix:** use the script's `--attention-backend` flag (and the launcher's
`ATTENTION_BACKEND=FLASH_ATTN` env var passthrough).

### 5. Slow HF download (~10 MB/s)
**Cause:** HF Hub default downloader is single-threaded and rate-limited.

**Fix:**
```bash
pip install -U "huggingface_hub[hf_transfer]" hf_transfer
export HF_HUB_ENABLE_HF_TRANSFER=1
hf download <model>
```
Bumps to 100–700 MB/s on EU/US datacenter boxes.

## Hardware selection (Vast 5090 listings)

For LLM-inference workloads, optimize in this order:

1. **Avoid CN datacenters** (HF Hub blocked / very slow).
2. **Bandwidth ≥ 500 Mbps** (model downloads). Sweden/EU usually best in $/perf.
3. **Reliability ≥ 99%** + **Max Duration ≥ 1 month** for multi-day jobs.
4. **CPU RAM ≥ 200 GB** for batch translation throughput (vLLM caches batches).

Reference: chosen Sweden m:38795 ($0.41/hr, 99.72% rel, 933 Mbps, 384 GB RAM).
Same box did the structured-translation run: model load 14 s, vLLM init 126 s,
sustained ~5 rows/s on 350M Cohere2 model.

## Verified config for this run

```
GPU:        RTX 5090, driver 580.126.09, compute_cap 12.0
OS:         CUDA 13.0 only (no CUDA 12 anywhere)
PyTorch:    2.11.0+cu130
vLLM:       0.19.2rc1.dev211+gc798593f0 (nightly)
Model:      Lyte/tiny-aya-darija-v5 (350M Cohere2)
Backend:    FLASH_ATTN (FlashAttention 2)
Init time:  ~126 s end-to-end
KV cache:   20.27 GiB available, 295k tokens, 71x concurrency at 4096 ctx
```
