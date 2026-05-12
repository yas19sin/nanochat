"""Export and optionally push the Darija NanoChat base and instruct checkpoints.

Example:
    python -m scripts.push_hf_models \
        --namespace Lyte \
        --base-repo nanochat-darija-73m-base \
        --instruct-repo nanochat-darija-73m-instruct \
        --base-model-tag d6_target12 \
        --base-step 1062 \
        --instruct-model-tag d6_target12 \
        --instruct-step 10000 \
        --push
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def full_repo_id(namespace: str | None, repo: str) -> str:
    if "/" in repo:
        return repo
    if not namespace:
        raise ValueError(
            f"Repo '{repo}' has no namespace. Pass --namespace or use a full repo id.")
    return f"{namespace}/{repo}"


def clean_output_dir(path: Path, output_root: Path) -> None:
    resolved_path = path.resolve()
    resolved_root = output_root.resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(
            f"Refusing to clean {resolved_path}; it is outside {resolved_root}") from exc
    if resolved_path.exists():
        shutil.rmtree(resolved_path)


def export_job(
    *,
    source: str,
    repo_id: str,
    output_root: Path,
    model_tag: str | None,
    step: int | None,
    tokenizer_dir: Path | None,
    dtype: str,
    private: bool,
    push: bool,
    validate: bool,
    clean: bool,
    base_model_repo: str | None = None,
) -> dict:
    from scripts.export_hf import export_checkpoint

    output_dir = output_root / repo_id.split("/")[-1]
    if clean:
        clean_output_dir(output_dir, output_root)

    return export_checkpoint(
        source=source,
        model_tag=model_tag,
        step=step,
        tokenizer_dir=tokenizer_dir,
        output_dir=output_dir,
        dtype=dtype,
        repo_id=repo_id,
        base_model_repo=base_model_repo,
        validate=validate,
        push_to_hub=repo_id if push else None,
        private=private,
        commit_message=f"Upload {repo_id.split('/')[-1]} NanoChat HF export",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export/push base and instruct Darija NanoChat checkpoints to Hugging Face.")
    parser.add_argument("--namespace", type=str, default=os.environ.get("HF_NAMESPACE"))
    parser.add_argument("--base-repo", type=str, default="nanochat-darija-73m-base")
    parser.add_argument("--instruct-repo", type=str, default="nanochat-darija-73m-instruct")
    parser.add_argument("--base-model-tag", type=str, default="d6_target12")
    parser.add_argument("--instruct-model-tag", type=str, default="d6_target12")
    parser.add_argument("--base-step", type=int, default=None)
    parser.add_argument("--instruct-step", type=int, default=None)
    parser.add_argument("--output-root", type=Path, default=Path("dev/hf_exports"))
    parser.add_argument("--tokenizer-dir", type=Path, default=None)
    parser.add_argument("--dtype", choices=["auto", "float32", "float16", "bfloat16"], default="bfloat16")
    parser.add_argument("--only", choices=["base", "instruct", "both"], default="both")
    parser.add_argument("--push", action="store_true",
                        help="Create/update private HF repos and upload the exported folders.")
    parser.add_argument("--public", action="store_true",
                        help="Push public repos. Default is private.")
    parser.add_argument("--no-clean", action="store_true",
                        help="Do not delete previous export folders before writing.")
    parser.add_argument("--no-validate", action="store_true",
                        help="Skip local AutoModel/AutoTokenizer load validation.")
    parser.add_argument("--env-file", type=Path, default=Path(".env"),
                        help="Optional .env file to load HF_TOKEN from without printing it.")
    args = parser.parse_args()

    load_env_file(args.env_file)
    args.output_root.mkdir(parents=True, exist_ok=True)

    private = not args.public
    validate = not args.no_validate
    clean = not args.no_clean

    base_repo_id = None
    instruct_repo_id = None
    if args.only in {"base", "both"} or args.namespace or "/" in args.base_repo:
        base_repo_id = full_repo_id(args.namespace, args.base_repo)
    if args.only in {"instruct", "both"}:
        instruct_repo_id = full_repo_id(args.namespace, args.instruct_repo)

    results = []
    if args.only in {"base", "both"}:
        results.append(export_job(
            source="base",
            repo_id=base_repo_id,
            output_root=args.output_root,
            model_tag=args.base_model_tag,
            step=args.base_step,
            tokenizer_dir=args.tokenizer_dir,
            dtype=args.dtype,
            private=private,
            push=args.push,
            validate=validate,
            clean=clean,
        ))
    if args.only in {"instruct", "both"}:
        results.append(export_job(
            source="sft",
            repo_id=instruct_repo_id,
            output_root=args.output_root,
            model_tag=args.instruct_model_tag,
            step=args.instruct_step,
            tokenizer_dir=args.tokenizer_dir,
            dtype=args.dtype,
            private=private,
            push=args.push,
            validate=validate,
            clean=clean,
            base_model_repo=base_repo_id,
        ))

    summary_path = args.output_root / "export_summary.json"
    summary_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))
    print(f"Wrote {summary_path}")


if __name__ == "__main__":
    main()
