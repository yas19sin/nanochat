"""
Export a NanoChat downstream text-embedder checkpoint to Hugging Face.

The exported model loads with `AutoModel` and returns normalized embeddings in
`outputs.pooler_output`.
"""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path

import torch
from transformers import AutoModel, AutoTokenizer

from nanochat.configuration_nanochat import NanochatConfig
from nanochat.downstream import load_downstream_checkpoint
from nanochat.modeling_nanochat import NanochatForTextEmbedding
from scripts.export_hf import DTYPE_MAP, copy_remote_code, export_tokenizer, push_folder_to_hub


def remap_state_dict(state_dict: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    remapped = {}
    for key, value in state_dict.items():
        key = key.removeprefix("_orig_mod.")
        if key.startswith("backbone."):
            backbone_key = key.removeprefix("backbone.")
            if backbone_key.startswith("lm_head."):
                continue
            remapped[f"model.{backbone_key}"] = value
        elif key.startswith("projection."):
            remapped[key] = value
    return remapped


def build_config(meta: dict, tokenizer_dir: Path, dtype: torch.dtype) -> NanochatConfig:
    model_config = dict(meta["base_meta"]["model_config"])
    with (tokenizer_dir / "tokenizer.pkl").open("rb") as handle:
        encoding = pickle.load(handle)
    bos_token = "<|bos|>"
    eos_token = "<|assistant_end|>"
    pad_token = bos_token
    config = NanochatConfig(
        **model_config,
        bos_token_id=encoding.encode_single_token(bos_token),
        eos_token_id=encoding.encode_single_token(eos_token),
        pad_token_id=encoding.encode_single_token(pad_token),
        embedding_pooling=meta.get("pooling", "mean"),
        projection_dim=int(meta.get("projection_dim", 0) or 0),
        normalize_embeddings=bool(meta.get("normalize", True)),
        auto_map={
            "AutoConfig": "configuration_nanochat.NanochatConfig",
            "AutoModel": "modeling_nanochat.NanochatForTextEmbedding",
        },
        architectures=["NanochatForTextEmbedding"],
        torch_dtype=str(dtype).replace("torch.", ""),
    )
    config.use_cache = False
    return config


def write_readme(output_dir: Path, repo_id: str | None, meta: dict, num_parameters: int) -> None:
    title = repo_id.split("/")[-1] if repo_id else output_dir.name
    readme = f"""---
language:
- ary
- ar
license: other
library_name: transformers
pipeline_tag: feature-extraction
tags:
- nanochat
- darija
- moroccan-arabic
- embeddings
- sentence-similarity
- custom-code
---

# {title}

NanoChat text embedding model. Load with `trust_remote_code=True`; embeddings are
returned in `outputs.pooler_output`.

- Pooling: `{meta.get("pooling", "mean")}`
- Projection dim: `{meta.get("projection_dim", 0)}`
- Normalize embeddings: `{meta.get("normalize", True)}`
- Parameters: `{num_parameters:,}`

```python
import torch
from transformers import AutoModel, AutoTokenizer

model_id = "{repo_id or output_dir.name}"
tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
model = AutoModel.from_pretrained(model_id, trust_remote_code=True, device_map="auto")
inputs = tokenizer(["السلام عليكم", "وعليكم السلام"], return_tensors="pt", padding=True).to(model.device)
with torch.no_grad():
    emb = model(**inputs).pooler_output
print(torch.nn.functional.cosine_similarity(emb[0:1], emb[1:2]))
```
"""
    (output_dir / "README.md").write_text(readme, encoding="utf-8")


def validate_export(output_dir: Path) -> None:
    tokenizer = AutoTokenizer.from_pretrained(output_dir, trust_remote_code=True)
    model = AutoModel.from_pretrained(output_dir, trust_remote_code=True)
    sample = tokenizer(["هاد نص", "هاد نص آخر"], return_tensors="pt", padding=True)
    with torch.no_grad():
        out = model(**sample)
    print(f"Export validated. Embedding shape: {tuple(out.pooler_output.shape)}")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--step", type=int, default=None)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--tokenizer-dir", type=Path, default=None)
    parser.add_argument("--dtype", choices=["auto", "float32", "float16", "bfloat16"], default="auto")
    parser.add_argument("--push-to-hub", default=None)
    parser.add_argument("--repo-id", default=None)
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--no-validate", action="store_true")
    parser.add_argument("--commit-message", default=None)
    return parser.parse_args()


def main():
    args = parse_args()
    from nanochat.common import get_base_dir

    tokenizer_dir = args.tokenizer_dir or Path(get_base_dir()) / "tokenizer"
    state, meta, step = load_downstream_checkpoint(args.checkpoint_dir, args.step, map_location="cpu")
    dtype = next(iter(state.values())).dtype
    if args.dtype != "auto":
        dtype = DTYPE_MAP[args.dtype]

    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = build_config(meta, tokenizer_dir, dtype)
    model = NanochatForTextEmbedding(config).to(dtype=dtype)
    missing, unexpected = model.load_state_dict(remap_state_dict(state), strict=False)
    if missing or unexpected:
        raise RuntimeError(f"State dict mismatch. Missing: {missing}; unexpected: {unexpected}")

    copy_remote_code(args.output_dir)
    export_tokenizer(
        tokenizer_dir,
        args.output_dir,
        bos_token="<|bos|>",
        eos_token="<|assistant_end|>",
        pad_token="<|bos|>",
    )
    model.save_pretrained(args.output_dir, safe_serialization=True)
    (args.output_dir / "nanochat_embedder_export.json").write_text(
        json.dumps({"checkpoint_dir": str(args.checkpoint_dir), "step": step, "task_meta": meta}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    num_parameters = sum(param.numel() for param in model.parameters())
    write_readme(args.output_dir, args.repo_id or args.push_to_hub, meta, num_parameters)
    if not args.no_validate:
        validate_export(args.output_dir)
    if args.push_to_hub:
        push_folder_to_hub(
            args.output_dir,
            args.push_to_hub,
            args.private,
            args.commit_message or f"Upload NanoChat embedder step {step}",
        )
    print(json.dumps({
        "output_dir": str(args.output_dir),
        "step": step,
        "num_parameters": num_parameters,
        "repo_id": args.repo_id or args.push_to_hub,
    }, indent=2))


if __name__ == "__main__":
    main()
