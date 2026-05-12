import argparse
import base64
import json
import pickle
import shutil
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, GenerationConfig, PreTrainedTokenizerFast
from transformers.integrations.tiktoken import convert_tiktoken_to_fast
from nanochat.configuration_nanochat import NanochatConfig
from nanochat.modeling_nanochat import NanochatForCausalLM

SPECIAL_TOKENS = [
    "<|bos|>",
    "<|user_start|>",
    "<|user_end|>",
    "<|assistant_start|>",
    "<|assistant_end|>",
    "<|python_start|>",
    "<|python_end|>",
    "<|output_start|>",
    "<|output_end|>",
]


DTYPE_MAP = {
    "float32": torch.float32,
    "float16": torch.float16,
    "bfloat16": torch.bfloat16,
}


def resolve_checkpoint_dir(source: str, model_tag: str | None) -> Path:
    from nanochat.checkpoint_manager import find_largest_model
    from nanochat.common import get_base_dir

    base_dir = Path(get_base_dir())
    model_root = {
        "base": "base_checkpoints",
        "sft": "chatsft_checkpoints",
        "rl": "chatrl_checkpoints",
    }[source]
    checkpoints_dir = base_dir / model_root
    if model_tag is None:
        model_tag = find_largest_model(str(checkpoints_dir))
    return checkpoints_dir / model_tag


def strip_compile_prefix(state_dict: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    return {key.removeprefix("_orig_mod."): value for key, value in state_dict.items()}


def remap_state_dict_for_hf(state_dict: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    remapped = {}
    for key, value in state_dict.items():
        if key == "lm_head.weight":
            remapped[key] = value
        else:
            remapped[f"model.{key}"] = value
    return remapped


def export_tokenizer(tokenizer_dir: Path, output_dir: Path, bos_token: str, eos_token: str | None, pad_token: str) -> None:
    with open(tokenizer_dir / "tokenizer.pkl", "rb") as handle:
        encoding = pickle.load(handle)

    # transformers delegates tiktoken BPE writing to blobfile. On Windows,
    # blobfile rejects normal absolute paths like C:\...\tokenizer.model.
    # Patch only that tiny writer to use pathlib; the on-disk format is the
    # same one tiktoken writes: base64(token_bytes) + rank per line.
    def dump_tiktoken_bpe_local(bpe_ranks, tiktoken_bpe_file):
        path = Path(str(tiktoken_bpe_file))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            for token, rank in sorted(bpe_ranks.items(), key=lambda item: item[1]):
                handle.write(base64.b64encode(token) + b" " +
                             str(rank).encode("utf-8") + b"\n")

    import tiktoken.load as tiktoken_load

    old_read_file = tiktoken_load.read_file
    sentinel = object()
    old_tiktoken_dump = tiktoken_load.dump_tiktoken_bpe
    old_convert_dump = convert_tiktoken_to_fast.__globals__.get(
        "dump_tiktoken_bpe", sentinel)

    def read_file_local(blobpath):
        path = Path(str(blobpath))
        if path.exists():
            return path.read_bytes()
        return old_read_file(blobpath)

    try:
        tiktoken_load.read_file = read_file_local
        tiktoken_load.dump_tiktoken_bpe = dump_tiktoken_bpe_local
        if old_convert_dump is not sentinel:
            convert_tiktoken_to_fast.__globals__["dump_tiktoken_bpe"] = dump_tiktoken_bpe_local
        convert_tiktoken_to_fast(encoding, str(output_dir))
    finally:
        tiktoken_load.read_file = old_read_file
        tiktoken_load.dump_tiktoken_bpe = old_tiktoken_dump
        if old_convert_dump is not sentinel:
            convert_tiktoken_to_fast.__globals__["dump_tiktoken_bpe"] = old_convert_dump

    tokenizer_path = output_dir / "tokenizer.json"

    additional_special_tokens = [token for token in SPECIAL_TOKENS if token not in {
        bos_token, eos_token, pad_token}]
    fast_tokenizer = PreTrainedTokenizerFast(
        tokenizer_file=str(tokenizer_path),
        bos_token=bos_token,
        eos_token=eos_token,
        pad_token=pad_token,
        additional_special_tokens=additional_special_tokens,
        clean_up_tokenization_spaces=False,
    )
    fast_tokenizer.chat_template = (
        "{% for message in messages %}"
        "{% if loop.first %}<|bos|>{% endif %}"
        "{% if message['role'] == 'user' %}<|user_start|>{{ message['content'] }}<|user_end|>{% endif %}"
        "{% if message['role'] == 'assistant' %}<|assistant_start|>{{ message['content'] }}<|assistant_end|>{% endif %}"
        "{% endfor %}"
        "{% if add_generation_prompt %}<|assistant_start|>{% endif %}"
    )
    fast_tokenizer.save_pretrained(output_dir)


def copy_remote_code(output_dir: Path) -> None:
    package_dir = Path(__file__).resolve().parents[1] / "nanochat"
    shutil.copyfile(package_dir / "configuration_nanochat.py",
                    output_dir / "configuration_nanochat.py")
    shutil.copyfile(package_dir / "modeling_nanochat.py",
                    output_dir / "modeling_nanochat.py")


def format_param_count(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def write_generation_config(output_dir: Path, source: str, eos_token_id: int | None, pad_token_id: int) -> None:
    if source == "base":
        config = GenerationConfig(
            max_new_tokens=256,
            do_sample=True,
            temperature=0.8,
            top_k=100,
            top_p=0.95,
            repetition_penalty=1.05,
            pad_token_id=pad_token_id,
            eos_token_id=eos_token_id,
            use_cache=False,
        )
    else:
        config = GenerationConfig(
            max_new_tokens=256,
            do_sample=True,
            temperature=0.6,
            top_k=100,
            top_p=0.85,
            repetition_penalty=1.1,
            pad_token_id=pad_token_id,
            eos_token_id=eos_token_id,
            use_cache=False,
        )
    config.save_pretrained(output_dir)


def write_model_card(
    output_dir: Path,
    *,
    repo_id: str | None,
    source: str,
    model_tag: str | None,
    step: int,
    meta_data: dict,
    num_parameters: int,
    dtype: torch.dtype,
    base_model_repo: str | None = None,
) -> None:
    model_config = meta_data.get("model_config", {})
    title = repo_id.split("/")[-1] if repo_id else f"nanochat-{source}-{model_tag or 'model'}"
    phase = "Instruction-tuned" if source != "base" else "Base"
    base_model_line = f"\nbase_model: {base_model_repo}" if base_model_repo else ""
    base_section = (
        f"- Base checkpoint: `{base_model_repo}`\n" if base_model_repo else ""
    )
    chat_usage = """
messages = [{"role": "user", "content": "جاوبني بالدارجة: شنو هي أحسن طريقة نتعلم بها البرمجة؟"}]
inputs = tokenizer.apply_chat_template(messages, add_generation_prompt=True, return_tensors="pt").to(model.device)
outputs = model.generate(
    inputs,
    max_new_tokens=256,
    temperature=0.6,
    top_k=100,
    top_p=0.85,
    repetition_penalty=1.1,
    do_sample=True,
)
print(tokenizer.decode(outputs[0], skip_special_tokens=False))
"""
    base_usage = """
inputs = tokenizer("المغرب بلد", return_tensors="pt").to(model.device)
outputs = model.generate(
    **inputs,
    max_new_tokens=128,
    temperature=0.8,
    top_k=100,
    top_p=0.95,
    do_sample=True,
)
print(tokenizer.decode(outputs[0], skip_special_tokens=False))
"""
    usage = chat_usage if source != "base" else base_usage
    training_text = (
        "Continued with supervised fine-tuning on Moroccan Darija instruction data."
        if source != "base"
        else "Pretrained on the cleaned Moroccan Darija FineWeb-Edu translation corpus."
    )

    readme = f"""---
language:
- ar
- ar-MA
license: mit
library_name: transformers
pipeline_tag: text-generation
tags:
- nanochat
- darija
- moroccan-arabic
- causal-lm
- custom-code{base_model_line}
---

# {title}

{phase} NanoChat causal language model for Moroccan Darija.

This repo is exported in Hugging Face Transformers format with custom model code. Load it with `trust_remote_code=True`.

## Model Details

- Parameters: **{format_param_count(num_parameters)}** ({num_parameters:,})
- Context length: `{model_config.get("sequence_len", "unknown")}`
- Vocab size: `{model_config.get("vocab_size", "unknown")}`
- Layers: `{model_config.get("n_layer", "unknown")}`
- Hidden size: `{model_config.get("n_embd", "unknown")}`
- Attention heads: `{model_config.get("n_head", "unknown")}`
- Checkpoint tag: `{model_tag or "unknown"}`
- Checkpoint step: `{step}`
- Export dtype: `{str(dtype).replace("torch.", "")}`
{base_section}
## Training

{training_text}

Base corpus summary: 4,856,133 cleaned Darija rows, approximately 1.53B tokens with the included tokenizer.

The instruction-tuned variant is small and experimental. It is useful for lightweight Darija chat tests, but it is not reliable for math, factuality, code debugging, translation fidelity, or safety-critical decisions.

## Usage

```python
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

model_id = "{repo_id or output_dir.name}"
tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
model = AutoModelForCausalLM.from_pretrained(
    model_id,
    trust_remote_code=True,
    torch_dtype=torch.bfloat16,
    device_map="auto",
)

{usage.strip()}
```

## Files

- `model.safetensors`: model weights
- `config.json`: NanoChat architecture config
- `generation_config.json`: default sampling config
- `tokenizer.json`, `tokenizer_config.json`, `special_tokens_map.json`: tokenizer files
- `configuration_nanochat.py`, `modeling_nanochat.py`: custom Transformers code
- `nanochat_export.json`: source checkpoint metadata

## Limitations

This is a tiny model. Expect fluent-looking but wrong answers, repetition on some prompts, and brittle instruction following. Use it as a research artifact or local baseline, not as a production assistant.
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def build_model_config(meta_data: dict, bos_token_id: int, eos_token_id: int | None, pad_token_id: int) -> NanochatConfig:
    from nanochat.checkpoint_manager import _patch_missing_config_keys

    model_config_kwargs = dict(meta_data["model_config"])
    _patch_missing_config_keys(model_config_kwargs)
    config = NanochatConfig(
        **model_config_kwargs,
        bos_token_id=bos_token_id,
        eos_token_id=eos_token_id,
        pad_token_id=pad_token_id,
        auto_map={
            "AutoConfig": "configuration_nanochat.NanochatConfig",
            "AutoModelForCausalLM": "modeling_nanochat.NanochatForCausalLM",
        },
        architectures=["NanochatForCausalLM"],
    )
    return config


def validate_export(output_dir: Path) -> None:
    tokenizer = AutoTokenizer.from_pretrained(
        output_dir, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        output_dir, trust_remote_code=True)
    sample = tokenizer("شنو هي الدارجة المغربية؟", return_tensors="pt")
    sample = {key: value for key, value in sample.items() if key in {
        "input_ids", "attention_mask"}}
    with torch.no_grad():
        outputs = model(**sample)
    print(f"Export validated. Logits shape: {tuple(outputs.logits.shape)}")


def push_folder_to_hub(output_dir: Path, repo_id: str, private: bool, commit_message: str | None = None) -> None:
    from huggingface_hub import HfApi

    api = HfApi()
    api.create_repo(repo_id=repo_id, repo_type="model",
                    private=private, exist_ok=True)
    api.upload_folder(
        folder_path=str(output_dir),
        repo_id=repo_id,
        repo_type="model",
        commit_message=commit_message or f"Upload NanoChat export from {output_dir.name}",
    )


def export_checkpoint(
    *,
    source: str,
    output_dir: Path,
    model_tag: str | None = None,
    step: int | None = None,
    tokenizer_dir: Path | None = None,
    checkpoint_dir: Path | None = None,
    dtype: str = "auto",
    eos_token: str | None = None,
    repo_id: str | None = None,
    base_model_repo: str | None = None,
    validate: bool = True,
    push_to_hub: str | None = None,
    private: bool = False,
    commit_message: str | None = None,
) -> dict:
    from nanochat.checkpoint_manager import _patch_missing_config_keys, _patch_missing_keys, find_last_step, load_checkpoint
    from nanochat.common import get_base_dir

    checkpoint_dir = checkpoint_dir if checkpoint_dir is not None else resolve_checkpoint_dir(
        source, model_tag)
    if step is None:
        step = find_last_step(str(checkpoint_dir))

    tokenizer_dir = tokenizer_dir if tokenizer_dir is not None else Path(
        get_base_dir()) / "tokenizer"
    output_dir.mkdir(parents=True, exist_ok=True)

    model_data, _, meta_data = load_checkpoint(
        str(checkpoint_dir), step, device="cpu", load_optimizer=False)
    model_data = strip_compile_prefix(model_data)
    model_config_kwargs = dict(meta_data["model_config"])
    _patch_missing_config_keys(model_config_kwargs)
    _patch_missing_keys(model_data, NanochatConfig(**model_config_kwargs))

    with open(tokenizer_dir / "tokenizer.pkl", "rb") as handle:
        encoding = pickle.load(handle)
    bos_token = "<|bos|>"
    eos_token = eos_token or (
        "<|assistant_end|>" if source != "base" else None)
    pad_token = bos_token
    bos_token_id = encoding.encode_single_token(bos_token)
    eos_token_id = encoding.encode_single_token(
        eos_token) if eos_token is not None else None
    pad_token_id = encoding.encode_single_token(pad_token)

    config = build_model_config(
        meta_data, bos_token_id, eos_token_id, pad_token_id)
    config.use_cache = False
    model = NanochatForCausalLM(config)

    source_dtype = model_data["transformer.wte.weight"].dtype
    if dtype != "auto":
        source_dtype = DTYPE_MAP[dtype]
    model = model.to(dtype=source_dtype)
    model_data = remap_state_dict_for_hf(model_data)
    missing_keys, unexpected_keys = model.load_state_dict(
        model_data, strict=False)
    if missing_keys or unexpected_keys:
        raise RuntimeError(
            f"State dict mismatch. Missing: {missing_keys}; unexpected: {unexpected_keys}")

    copy_remote_code(output_dir)
    export_tokenizer(tokenizer_dir, output_dir,
                     bos_token=bos_token, eos_token=eos_token, pad_token=pad_token)
    model.save_pretrained(output_dir, safe_serialization=True)
    write_generation_config(output_dir, source, eos_token_id, pad_token_id)

    export_meta = {
        "checkpoint_dir": str(checkpoint_dir),
        "step": step,
        "source": source,
        "model_tag": model_tag,
        "source_dtype": str(source_dtype),
        "repo_id": repo_id or push_to_hub,
    }
    with open(output_dir / "nanochat_export.json", "w", encoding="utf-8") as handle:
        json.dump(export_meta, handle, indent=2)

    num_parameters = sum(param.numel() for param in model.parameters())
    write_model_card(
        output_dir,
        repo_id=repo_id or push_to_hub,
        source=source,
        model_tag=model_tag,
        step=step,
        meta_data=meta_data,
        num_parameters=num_parameters,
        dtype=source_dtype,
        base_model_repo=base_model_repo,
    )

    if validate:
        validate_export(output_dir)

    if push_to_hub:
        push_folder_to_hub(output_dir, push_to_hub, private, commit_message)

    return {
        "output_dir": str(output_dir),
        "checkpoint_dir": str(checkpoint_dir),
        "step": step,
        "source": source,
        "repo_id": repo_id or push_to_hub,
        "num_parameters": num_parameters,
        "dtype": str(source_dtype),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export a nanochat checkpoint to Hugging Face format.")
    parser.add_argument(
        "--source", choices=["base", "sft", "rl"], default="sft")
    parser.add_argument("--model-tag", type=str, default=None)
    parser.add_argument("--step", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, default=None)
    parser.add_argument("--checkpoint-dir", type=Path, default=None)
    parser.add_argument(
        "--dtype", choices=["auto", "float32", "float16", "bfloat16"], default="auto")
    parser.add_argument("--push-to-hub", type=str, default=None)
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--eos-token", type=str, default=None)
    parser.add_argument("--repo-id", type=str, default=None,
                        help="Repo id to write into README metadata without pushing.")
    parser.add_argument("--base-model-repo", type=str, default=None)
    parser.add_argument("--no-validate", action="store_true")
    parser.add_argument("--commit-message", type=str, default=None)
    args = parser.parse_args()

    result = export_checkpoint(
        source=args.source,
        model_tag=args.model_tag,
        step=args.step,
        output_dir=args.output_dir,
        tokenizer_dir=args.tokenizer_dir,
        checkpoint_dir=args.checkpoint_dir,
        dtype=args.dtype,
        push_to_hub=args.push_to_hub,
        repo_id=args.repo_id,
        private=args.private,
        eos_token=args.eos_token,
        base_model_repo=args.base_model_repo,
        validate=not args.no_validate,
        commit_message=args.commit_message,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
