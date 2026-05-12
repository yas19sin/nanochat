"""Smoke-test Hugging Face exports for local inference.

Examples:
    # Test local HF export folders.
    python -m scripts.test_hf_exports

    # Also compare exported HF logits against native NanoChat checkpoints.
    python -m scripts.test_hf_exports --compare-native --base-step 1062 --instruct-step 10000
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


BASE_PROMPTS = [
    "المغرب بلد",
    "الدارجة المغربية هي",
]

CHAT_PROMPTS = [
    "جاوبني بالدارجة: شنو هي أحسن طريقة نتعلم بها البرمجة؟",
    "شرح ليا بالدارجة شنو هو الذكاء الاصطناعي فثلاث جمل.",
    "عطيني نصيحة قصيرة باش نحافظ على كلمة السر ديالي.",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run local generation and optional native-logit checks for exported HF NanoChat models.")
    parser.add_argument("--base-model", type=Path,
                        default=Path("dev/hf_exports/nanochat-darija-73m-base"))
    parser.add_argument("--instruct-model", type=Path,
                        default=Path("dev/hf_exports/nanochat-darija-73m-instruct"))
    parser.add_argument("--skip-base", action="store_true")
    parser.add_argument("--skip-instruct", action="store_true")
    parser.add_argument("--device", choices=["auto", "cuda", "cpu", "mps"], default="auto")
    parser.add_argument("--dtype", choices=["auto", "float32", "float16", "bfloat16"], default="bfloat16")
    parser.add_argument("--max-new-tokens", type=int, default=160)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--top-k", type=int, default=100)
    parser.add_argument("--top-p", type=float, default=0.85)
    parser.add_argument("--repetition-penalty", type=float, default=1.1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--compare-native", action="store_true")
    parser.add_argument("--nanochat-base-dir", type=Path, default=None,
                        help="Sets NANOCHAT_BASE_DIR before loading native checkpoints.")
    parser.add_argument("--base-model-tag", type=str, default="d6_target12")
    parser.add_argument("--instruct-model-tag", type=str, default="d6_target12")
    parser.add_argument("--base-step", type=int, default=1062)
    parser.add_argument("--instruct-step", type=int, default=10000)
    return parser.parse_args()


def torch_dtype(dtype_name: str):
    import torch

    return {
        "auto": "auto",
        "float32": torch.float32,
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
    }[dtype_name]


def detect_device(device_name: str):
    import torch

    if device_name != "auto":
        return torch.device(device_name)
    if torch.cuda.is_available():
        return torch.device("cuda")
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return torch.device("mps")
    return torch.device("cpu")


def generation_kwargs(args: argparse.Namespace) -> dict:
    kwargs = {
        "max_new_tokens": args.max_new_tokens,
        "top_k": args.top_k,
        "top_p": args.top_p,
        "repetition_penalty": args.repetition_penalty,
        "pad_token_id": None,
    }
    if args.temperature <= 0:
        kwargs["do_sample"] = False
    else:
        kwargs["do_sample"] = True
        kwargs["temperature"] = args.temperature
    return kwargs


def load_hf(model_path: Path, device, dtype_name: str):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if not model_path.exists():
        raise FileNotFoundError(f"HF export folder not found: {model_path}")
    tokenizer = AutoTokenizer.from_pretrained(
        model_path, trust_remote_code=True, local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_path,
        trust_remote_code=True,
        local_files_only=True,
        torch_dtype=torch_dtype(dtype_name),
    )
    model.to(device)
    model.eval()
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.bos_token
    return model, tokenizer


def check_forward(model, tokenizer, device, label: str) -> None:
    import torch

    text = "شنو هي الدارجة المغربية؟"
    inputs = tokenizer(text, return_tensors="pt").to(device)
    inputs = {key: value for key, value in inputs.items()
              if key in {"input_ids", "attention_mask"}}
    with torch.no_grad():
        logits = model(**inputs).logits
    if not torch.isfinite(logits).all():
        raise RuntimeError(f"{label}: logits contain NaN or Inf")
    print(f"[{label}] forward ok | logits={tuple(logits.shape)} | max={logits.max().item():.3f}")


def generate_base(model, tokenizer, device, args: argparse.Namespace) -> None:
    import torch

    print("\n== Base model generations ==")
    kwargs = generation_kwargs(args)
    kwargs["pad_token_id"] = tokenizer.pad_token_id
    torch.manual_seed(args.seed)
    for prompt in BASE_PROMPTS:
        inputs = tokenizer(prompt, return_tensors="pt").to(device)
        inputs = {key: value for key, value in inputs.items()
                  if key in {"input_ids", "attention_mask"}}
        with torch.no_grad():
            output = model.generate(**inputs, **kwargs)
        continuation = output[0, inputs["input_ids"].shape[1]:].tolist()
        print(f"\nPrompt: {prompt}")
        print(tokenizer.decode(continuation, skip_special_tokens=False).strip())


def render_chat(tokenizer, prompt: str, device):
    messages = [{"role": "user", "content": prompt}]
    rendered = tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        return_tensors="pt",
    )
    return rendered.to(device)


def generate_instruct(model, tokenizer, device, args: argparse.Namespace) -> None:
    import torch

    print("\n== Instruct model generations ==")
    kwargs = generation_kwargs(args)
    kwargs["pad_token_id"] = tokenizer.pad_token_id
    eos_id = tokenizer.convert_tokens_to_ids("<|assistant_end|>")
    if isinstance(eos_id, int) and eos_id >= 0:
        kwargs["eos_token_id"] = eos_id
    torch.manual_seed(args.seed)
    for prompt in CHAT_PROMPTS:
        input_ids = render_chat(tokenizer, prompt, device)
        with torch.no_grad():
            output = model.generate(input_ids=input_ids, **kwargs)
        continuation = output[0, input_ids.shape[1]:].tolist()
        print(f"\nUser: {prompt}")
        print("Assistant:", tokenizer.decode(
            continuation, skip_special_tokens=False).strip())


def compare_native_logits(hf_model, hf_tokenizer, device, source: str, model_tag: str, step: int) -> None:
    import torch
    from nanochat.checkpoint_manager import load_model

    native_model, native_tokenizer, _meta = load_model(
        source, device, phase="eval", model_tag=model_tag, step=step)
    text = "شنو هي الدارجة المغربية؟ هاد النص غير للتجربة."
    native_ids = [native_tokenizer.get_bos_token_id()] + native_tokenizer.encode(text)
    hf_ids = hf_tokenizer.encode("<|bos|>" + text, add_special_tokens=False)
    if native_ids != hf_ids:
        print(f"[{source}] tokenizer mismatch")
        print(f"  native={native_ids[:24]}")
        print(f"  hf    ={hf_ids[:24]}")
        return

    ids = torch.tensor([native_ids], dtype=torch.long, device=device)
    with torch.no_grad():
        native_logits = native_model(ids)
        hf_logits = hf_model(input_ids=ids).logits
    diff = (native_logits.float() - hf_logits.float()).abs()
    print(
        f"[{source}] native-vs-HF logits | max_abs={diff.max().item():.6f} | mean_abs={diff.mean().item():.6f}"
    )


def main() -> None:
    args = parse_args()
    if args.nanochat_base_dir is not None:
        os.environ["NANOCHAT_BASE_DIR"] = str(args.nanochat_base_dir)

    device = detect_device(args.device)
    print(f"device={device} dtype={args.dtype}")

    base = None
    instruct = None
    if not args.skip_base:
        base = load_hf(args.base_model, device, args.dtype)
        check_forward(base[0], base[1], device, "base")
        generate_base(base[0], base[1], device, args)
    if not args.skip_instruct:
        instruct = load_hf(args.instruct_model, device, args.dtype)
        check_forward(instruct[0], instruct[1], device, "instruct")
        generate_instruct(instruct[0], instruct[1], device, args)

    if args.compare_native:
        if base is not None:
            compare_native_logits(
                base[0], base[1], device, "base", args.base_model_tag, args.base_step)
        if instruct is not None:
            compare_native_logits(
                instruct[0], instruct[1], device, "sft", args.instruct_model_tag, args.instruct_step)


if __name__ == "__main__":
    main()
