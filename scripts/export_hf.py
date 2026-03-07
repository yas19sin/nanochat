import argparse
import json
import os
import pickle
import shutil
from pathlib import Path

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, PreTrainedTokenizerFast
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

    convert_tiktoken_to_fast(encoding, str(output_dir))

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


def main() -> None:
    from nanochat.checkpoint_manager import _patch_missing_config_keys, _patch_missing_keys, find_last_step, load_checkpoint
    from nanochat.common import get_base_dir

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
    args = parser.parse_args()

    checkpoint_dir = args.checkpoint_dir if args.checkpoint_dir is not None else resolve_checkpoint_dir(
        args.source, args.model_tag)
    if args.step is None:
        args.step = find_last_step(str(checkpoint_dir))

    tokenizer_dir = args.tokenizer_dir if args.tokenizer_dir is not None else Path(
        get_base_dir()) / "tokenizer"
    args.output_dir.mkdir(parents=True, exist_ok=True)

    model_data, _, meta_data = load_checkpoint(
        str(checkpoint_dir), args.step, device="cpu", load_optimizer=False)
    model_data = strip_compile_prefix(model_data)
    model_config_kwargs = dict(meta_data["model_config"])
    _patch_missing_config_keys(model_config_kwargs)
    _patch_missing_keys(model_data, NanochatConfig(**model_config_kwargs))

    with open(tokenizer_dir / "tokenizer.pkl", "rb") as handle:
        encoding = pickle.load(handle)
    bos_token = "<|bos|>"
    eos_token = args.eos_token or (
        "<|assistant_end|>" if args.source != "base" else None)
    pad_token = bos_token
    bos_token_id = encoding.encode_single_token(bos_token)
    eos_token_id = encoding.encode_single_token(
        eos_token) if eos_token is not None else None
    pad_token_id = encoding.encode_single_token(pad_token)

    config = build_model_config(
        meta_data, bos_token_id, eos_token_id, pad_token_id)
    model = NanochatForCausalLM(config)

    source_dtype = model_data["transformer.wte.weight"].dtype
    if args.dtype != "auto":
        source_dtype = {
            "float32": torch.float32,
            "float16": torch.float16,
            "bfloat16": torch.bfloat16,
        }[args.dtype]
    model = model.to(dtype=source_dtype)
    model_data = remap_state_dict_for_hf(model_data)
    missing_keys, unexpected_keys = model.load_state_dict(
        model_data, strict=False)
    if missing_keys or unexpected_keys:
        raise RuntimeError(
            f"State dict mismatch. Missing: {missing_keys}; unexpected: {unexpected_keys}")

    copy_remote_code(args.output_dir)
    export_tokenizer(tokenizer_dir, args.output_dir,
                     bos_token=bos_token, eos_token=eos_token, pad_token=pad_token)
    model.save_pretrained(args.output_dir, safe_serialization=True)

    export_meta = {
        "checkpoint_dir": str(checkpoint_dir),
        "step": args.step,
        "source": args.source,
        "source_dtype": str(source_dtype),
    }
    with open(args.output_dir / "nanochat_export.json", "w", encoding="utf-8") as handle:
        json.dump(export_meta, handle, indent=2)

    tokenizer = AutoTokenizer.from_pretrained(
        args.output_dir, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.output_dir, trust_remote_code=True)
    sample = tokenizer("شنو هي الدارجة المغربية؟", return_tensors="pt")
    sample = {key: value for key, value in sample.items() if key in {
        "input_ids", "attention_mask"}}
    with torch.no_grad():
        outputs = model(**sample)
    print(f"Export validated. Logits shape: {tuple(outputs.logits.shape)}")

    if args.push_to_hub:
        model.push_to_hub(args.push_to_hub, private=args.private)
        tokenizer.push_to_hub(args.push_to_hub, private=args.private)
        for filename in ["configuration_nanochat.py", "modeling_nanochat.py", "nanochat_export.json"]:
            path = args.output_dir / filename
            if path.exists():
                from huggingface_hub import HfApi

                HfApi().upload_file(
                    path_or_fileobj=str(path),
                    path_in_repo=filename,
                    repo_id=args.push_to_hub,
                    repo_type="model",
                )


if __name__ == "__main__":
    main()
