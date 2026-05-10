"""
Clean Lyte/fineweb-edu-darija-translated into training-ready parquet shards.

The source dataset is private in the current Hub metadata. This script loads
HF_TOKEN from the environment or from a local .env file, streams the dataset,
applies conservative translation-quality filters, and writes:

  - nanochat shards with a single `text` column, suitable for NANOCHAT_DATA_DIR
  - optional cleaned paired shards with `en` + `darija` columns for audit/upload
  - manifest.json, cleaning_report.json, and a small rejects JSONL sample

Usage:
    python -m scripts.clean_fineweb_edu_darija --max-rows 5000 --dry-run

    python -m scripts.clean_fineweb_edu_darija \
        --output-dir ~/.cache/nanochat/darija_fineweb_edu_clean \
        --output-mode both
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable


HF_DATASET = "Lyte/fineweb-edu-darija-translated"
HF_SPLIT = "train"

TRAIN_SHARD_SIZE = 250_000
PAIRED_SHARD_SIZE = 100_000
VAL_SIZE = 20_000
TRAIN_ROW_GROUP_SIZE = 25_000
VAL_ROW_GROUP_SIZE = 2_000

ARABIC_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufeff]")
LATIN_RE = re.compile(r"[A-Za-z]")
SPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")
REPEATED_PUNCT_RE = re.compile(r"([!?.,:;،؛])\1{4,}")
CHAT_MARKER_RE = re.compile(r"(?im)^\s*(?:system|user|assistant)\s*:")
URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
BOILERPLATE_RE = re.compile(
    r"(?is)"
    r"^\s*(?:"
    r"here(?:'s| is)\s+(?:the\s+)?(?:moroccan\s+)?darija"
    r"|translation\s*:"
    r"|darija(?:\s+translation)?\s*:"
    r"|الترجمة\s*:"
    r"|ها\s+(?:هي\s+)?الترجمة"
    r")"
    r"|(?:as an ai|i cannot|i can't|i am unable|sorry,?\s+i)",
)


@dataclass(frozen=True)
class CleanConfig:
    min_en_chars: int = 32
    max_en_chars: int = 2_000
    min_darija_chars: int = 20
    max_darija_chars: int = 6_000
    min_arabic_chars: int = 10
    min_arabic_fraction: float = 0.20
    min_length_ratio: float = 0.35
    max_length_ratio: float = 3.00
    max_url_count: int = 4
    drop_number_mismatches: bool = False


@dataclass
class CleanRow:
    src_idx: int
    en: str
    darija: str
    output_tokens: int | None
    en_chars: int
    darija_chars: int
    arabic_chars: int
    latin_chars: int
    length_ratio: float
    split: str


def load_dotenv(path: Path) -> None:
    """Small .env loader so this script does not require python-dotenv."""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip().strip('"').strip("'")
        os.environ[key] = value


def env_token(cli_token: str | None) -> str | None:
    return (
        cli_token
        or os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    )


def default_output_dir() -> Path:
    base = os.environ.get(
        "NANOCHAT_BASE_DIR",
        os.path.join(os.path.expanduser("~"), ".cache", "nanochat"),
    )
    return Path(base) / "darija_fineweb_edu_clean"


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", " ").replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(SPACE_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def count_arabic(text: str) -> int:
    return len(ARABIC_RE.findall(text))


def count_latin(text: str) -> int:
    return len(LATIN_RE.findall(text))


def stable_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def number_set(text: str) -> set[str]:
    return {match.group(0).replace(",", ".") for match in NUMBER_RE.finditer(text)}


def check_row(
    row: dict[str, Any],
    source_ordinal: int,
    cfg: CleanConfig,
    val_full: bool,
) -> tuple[CleanRow | None, str]:
    en = normalize_text(row.get("en"))
    darija = normalize_text(row.get("darija"))

    if not en:
        return None, "missing_en"
    if not darija:
        return None, "missing_darija"
    if len(en) < cfg.min_en_chars:
        return None, "short_en"
    if len(en) > cfg.max_en_chars:
        return None, "long_en"
    if len(darija) < cfg.min_darija_chars:
        return None, "short_darija"
    if len(darija) > cfg.max_darija_chars:
        return None, "long_darija"

    arabic_chars = count_arabic(darija)
    latin_chars = count_latin(darija)
    if arabic_chars < cfg.min_arabic_chars:
        return None, "low_arabic_chars"

    arabic_fraction = arabic_chars / max(len(darija), 1)
    if arabic_fraction < cfg.min_arabic_fraction:
        return None, "low_arabic_fraction"

    ratio = len(darija) / max(len(en), 1)
    if ratio < cfg.min_length_ratio:
        return None, "low_length_ratio"
    if ratio > cfg.max_length_ratio:
        return None, "high_length_ratio"

    if normalize_for_compare(en) == normalize_for_compare(darija):
        return None, "english_echo"
    if REPEATED_PUNCT_RE.search(darija):
        return None, "repeated_punctuation"
    if CHAT_MARKER_RE.search(darija):
        return None, "chat_marker"
    if BOILERPLATE_RE.search(darija):
        return None, "translator_boilerplate"
    if len(URL_RE.findall(darija)) > cfg.max_url_count:
        return None, "too_many_urls"
    if EMAIL_RE.search(darija):
        return None, "contains_email"
    if cfg.drop_number_mismatches and number_set(en) != number_set(darija):
        return None, "number_mismatch"

    src_idx = row.get("src_idx", source_ordinal)
    try:
        src_idx = int(src_idx)
    except (TypeError, ValueError):
        src_idx = source_ordinal

    output_tokens = row.get("output_tokens")
    try:
        output_tokens = int(output_tokens) if output_tokens is not None else None
    except (TypeError, ValueError):
        output_tokens = None

    split = "val" if not val_full else "train"
    return (
        CleanRow(
            src_idx=src_idx,
            en=en,
            darija=darija,
            output_tokens=output_tokens,
            en_chars=len(en),
            darija_chars=len(darija),
            arabic_chars=arabic_chars,
            latin_chars=latin_chars,
            length_ratio=round(ratio, 4),
            split=split,
        ),
        "accepted",
    )


def normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def write_parquet(path: Path, rows: list[Any], row_group_size: int) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    if rows and isinstance(rows[0], str):
        table = pa.table({"text": rows})
    else:
        table = pa.Table.from_pylist(rows)
    pq.write_table(table, tmp_path, row_group_size=row_group_size)
    os.replace(tmp_path, path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


def append_reject_sample(
    samples: list[dict[str, Any]],
    row: dict[str, Any],
    reason: str,
    source_ordinal: int,
    limit: int,
) -> None:
    if len(samples) >= limit:
        return
    samples.append({
        "source_ordinal": source_ordinal,
        "src_idx": row.get("src_idx"),
        "reason": reason,
        "en": normalize_text(row.get("en"))[:500],
        "darija": normalize_text(row.get("darija"))[:500],
    })


def prepare_output_dir(output_dir: Path, output_mode: str, overwrite: bool) -> None:
    generated_patterns = [
        "fineweb_edu_train_*.parquet",
        "zzz_val_00000.parquet",
        "manifest.json",
        "cleaning_report.json",
        "reject_samples.jsonl",
    ]
    if output_mode in {"paired", "both"}:
        generated_patterns.extend(["paired/clean_pair_*.parquet"])

    existing = [path for pattern in generated_patterns for path in output_dir.glob(pattern)]
    if existing and not overwrite:
        examples = ", ".join(str(path) for path in existing[:3])
        raise SystemExit(
            f"Output directory already contains generated files: {examples}. "
            "Use --overwrite or choose a different --output-dir."
        )
    if existing:
        for path in existing:
            path.unlink()
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_mode in {"paired", "both"}:
        (output_dir / "paired").mkdir(parents=True, exist_ok=True)


def iter_dataset(
    repo_id: str,
    config: str | None,
    split: str,
    streaming: bool,
    cache_dir: str | None,
    token: str | None,
    retries: int,
) -> Iterable[dict[str, Any]]:
    from datasets import load_dataset

    kwargs: dict[str, Any] = {
        "split": split,
        "streaming": streaming,
        "cache_dir": cache_dir,
        "token": token,
    }
    for attempt in range(1, max(retries, 1) + 1):
        try:
            if config:
                return load_dataset(repo_id, config, **kwargs)
            return load_dataset(repo_id, **kwargs)
        except Exception:
            if attempt >= max(retries, 1):
                raise
            wait_s = min(2 ** attempt, 30)
            print(f"[load retry {attempt}/{retries}] dataset metadata fetch failed; retrying in {wait_s}s")
            time.sleep(wait_s)
    raise RuntimeError("unreachable")


def maybe_push_to_hub(output_dir: Path, repo_id: str, token: str) -> None:
    from huggingface_hub import HfApi

    api = HfApi(token=token)
    api.create_repo(repo_id, repo_type="dataset", exist_ok=True)
    api.upload_folder(
        repo_id=repo_id,
        repo_type="dataset",
        folder_path=str(output_dir),
        commit_message="Add cleaned FineWeb-Edu Darija shards",
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Clean Lyte/fineweb-edu-darija-translated into parquet shards."
    )
    p.add_argument("--repo-id", default=HF_DATASET)
    p.add_argument("--config", default=None)
    p.add_argument("--split", default=HF_SPLIT)
    p.add_argument("--output-dir", type=Path, default=None)
    p.add_argument("--output-mode", choices=["nanochat", "paired", "both"], default="nanochat")
    p.add_argument("--env-file", type=Path, default=Path(".env"))
    p.add_argument("--hf-token", default=None)
    p.add_argument("--cache-dir", default=None)
    p.add_argument("--no-streaming", action="store_true",
                   help="Download/cache the dataset before iterating.")
    p.add_argument("--max-rows", type=int, default=-1,
                   help="Stop after N source rows; useful for audits/smoke tests.")
    p.add_argument("--dry-run", action="store_true",
                   help="Apply filters and print stats without writing parquet.")
    p.add_argument("--overwrite", action="store_true",
                   help="Remove previously generated files in --output-dir first.")

    p.add_argument("--val-size", type=int, default=VAL_SIZE)
    p.add_argument("--shard-size", type=int, default=TRAIN_SHARD_SIZE)
    p.add_argument("--paired-shard-size", type=int, default=PAIRED_SHARD_SIZE)
    p.add_argument("--darija-only", action="store_true",
                   help="For nanochat output, skip English documents.")
    p.add_argument("--dedupe", choices=["none", "darija", "pair"], default="darija")
    p.add_argument("--reject-samples", type=int, default=200)
    p.add_argument("--progress-every", type=int, default=100_000)
    p.add_argument("--load-retries", type=int, default=5,
                   help="Retry transient Hub metadata/load failures.")
    p.add_argument("--push-repo-id", default=None,
                   help="Optional HF dataset repo to upload output-dir to after cleaning.")

    p.add_argument("--min-en-chars", type=int, default=CleanConfig.min_en_chars)
    p.add_argument("--max-en-chars", type=int, default=CleanConfig.max_en_chars)
    p.add_argument("--min-darija-chars", type=int, default=CleanConfig.min_darija_chars)
    p.add_argument("--max-darija-chars", type=int, default=CleanConfig.max_darija_chars)
    p.add_argument("--min-arabic-chars", type=int, default=CleanConfig.min_arabic_chars)
    p.add_argument("--min-arabic-fraction", type=float, default=CleanConfig.min_arabic_fraction)
    p.add_argument("--min-length-ratio", type=float, default=CleanConfig.min_length_ratio)
    p.add_argument("--max-length-ratio", type=float, default=CleanConfig.max_length_ratio)
    p.add_argument("--max-url-count", type=int, default=CleanConfig.max_url_count)
    p.add_argument("--drop-number-mismatches", action="store_true")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(args.env_file)
    token = env_token(args.hf_token)
    if token:
        os.environ["HF_TOKEN"] = token
        os.environ["HUGGINGFACE_HUB_TOKEN"] = token
        print("Using HF token from " + ("--hf-token flag" if args.hf_token else "environment/.env"))
    else:
        print("No HF token found; private datasets will fail.", file=sys.stderr)

    cfg = CleanConfig(
        min_en_chars=args.min_en_chars,
        max_en_chars=args.max_en_chars,
        min_darija_chars=args.min_darija_chars,
        max_darija_chars=args.max_darija_chars,
        min_arabic_chars=args.min_arabic_chars,
        min_arabic_fraction=args.min_arabic_fraction,
        min_length_ratio=args.min_length_ratio,
        max_length_ratio=args.max_length_ratio,
        max_url_count=args.max_url_count,
        drop_number_mismatches=args.drop_number_mismatches,
    )

    output_dir = args.output_dir or default_output_dir()
    if not args.dry_run:
        prepare_output_dir(output_dir, args.output_mode, args.overwrite)

    print(f"Source: {args.repo_id} split={args.split} ({'local' if args.no_streaming else 'streaming'})")
    print(f"Output: {output_dir} mode={args.output_mode}" + (" [dry-run]" if args.dry_run else ""))
    print(f"Filters: {asdict(cfg)}")

    dataset = iter_dataset(
        repo_id=args.repo_id,
        config=args.config,
        split=args.split,
        streaming=not args.no_streaming,
        cache_dir=args.cache_dir,
        token=token,
        retries=args.load_retries,
    )

    reject_counts: Counter[str] = Counter()
    seen_hashes: set[str] = set()
    reject_samples: list[dict[str, Any]] = []
    nano_train_buf: list[str] = []
    nano_val_rows: list[str] = []
    paired_buf: list[dict[str, Any]] = []
    train_shards: list[dict[str, Any]] = []
    paired_shards: list[dict[str, Any]] = []

    seen = 0
    accepted = 0
    duplicate = 0
    nano_train_docs = 0
    nano_val_docs = 0
    train_idx = 0
    paired_idx = 0
    t0 = time.time()

    def flush_train(final: bool = False) -> None:
        nonlocal nano_train_buf, train_idx
        if not nano_train_buf or args.dry_run or args.output_mode == "paired":
            return
        path = output_dir / f"fineweb_edu_train_{train_idx:05d}.parquet"
        write_parquet(path, nano_train_buf, TRAIN_ROW_GROUP_SIZE)
        entry = {
            "file": path.name,
            "rows": len(nano_train_buf),
            "final": final,
        }
        train_shards.append(entry)
        print(f"  wrote {path} ({len(nano_train_buf):,} rows)" + (" [FINAL]" if final else ""))
        train_idx += 1
        nano_train_buf = []

    def flush_paired(final: bool = False) -> None:
        nonlocal paired_buf, paired_idx
        if not paired_buf or args.dry_run or args.output_mode == "nanochat":
            return
        path = output_dir / "paired" / f"clean_pair_{paired_idx:05d}.parquet"
        write_parquet(path, paired_buf, TRAIN_ROW_GROUP_SIZE)
        entry = {
            "file": str(path.relative_to(output_dir)).replace("\\", "/"),
            "rows": len(paired_buf),
            "final": final,
        }
        paired_shards.append(entry)
        print(f"  wrote {path} ({len(paired_buf):,} rows)" + (" [FINAL]" if final else ""))
        paired_idx += 1
        paired_buf = []

    row_iter = itertools.islice(dataset, args.max_rows) if args.max_rows > 0 else dataset
    for source_ordinal, row in enumerate(row_iter):
        seen += 1

        clean_row, reason = check_row(
            row=row,
            source_ordinal=source_ordinal,
            cfg=cfg,
            val_full=nano_val_docs >= args.val_size,
        )
        if clean_row is None:
            reject_counts[reason] += 1
            append_reject_sample(reject_samples, row, reason, source_ordinal, args.reject_samples)
            continue

        if args.dedupe != "none":
            dedupe_text = clean_row.darija if args.dedupe == "darija" else clean_row.en + "\n" + clean_row.darija
            digest = stable_hash(normalize_for_compare(dedupe_text))
            if digest in seen_hashes:
                duplicate += 1
                reject_counts["duplicate"] += 1
                append_reject_sample(reject_samples, row, "duplicate", source_ordinal, args.reject_samples)
                continue
            seen_hashes.add(digest)

        accepted += 1

        if args.output_mode in {"paired", "both"} and not args.dry_run:
            paired_buf.append(asdict(clean_row))
            if len(paired_buf) >= args.paired_shard_size:
                flush_paired()

        if args.output_mode in {"nanochat", "both"}:
            if nano_val_docs < args.val_size:
                if not args.dry_run:
                    nano_val_rows.append(clean_row.darija)
                nano_val_docs += 1
                if not args.darija_only:
                    nano_train_docs += 1
                    if not args.dry_run:
                        nano_train_buf.append(clean_row.en)
            else:
                nano_train_docs += 1
                if not args.dry_run:
                    nano_train_buf.append(clean_row.darija)
                if not args.darija_only:
                    nano_train_docs += 1
                    if not args.dry_run:
                        nano_train_buf.append(clean_row.en)
            if len(nano_train_buf) >= args.shard_size:
                flush_train()

        if args.progress_every > 0 and seen % args.progress_every == 0:
            elapsed = time.time() - t0
            rate = seen / max(elapsed, 1e-6)
            rejected = sum(reject_counts.values())
            print(
                f"  ...seen={seen:,} accepted={accepted:,} rejected={rejected:,} "
                f"duplicates={duplicate:,} rate={rate:,.0f} rows/s"
            )

    flush_train(final=True)
    flush_paired(final=True)

    if args.output_mode in {"nanochat", "both"} and nano_val_rows and not args.dry_run:
        val_path = output_dir / "zzz_val_00000.parquet"
        write_parquet(val_path, nano_val_rows, VAL_ROW_GROUP_SIZE)
        print(f"  wrote {val_path} ({len(nano_val_rows):,} rows, val)")

    elapsed = time.time() - t0
    report = {
        "source": {
            "repo_id": args.repo_id,
            "config": args.config,
            "split": args.split,
        },
        "output": {
            "dir": str(output_dir),
            "mode": args.output_mode,
            "darija_only": args.darija_only,
        },
        "filters": asdict(cfg),
        "counts": {
            "source_rows_seen": seen,
            "accepted_pairs": accepted,
            "rejected_rows": int(sum(reject_counts.values())),
            "duplicates": duplicate,
            "nanochat_train_docs": nano_train_docs,
            "nanochat_val_docs": nano_val_docs,
            "elapsed_seconds": round(elapsed, 2),
        },
        "reject_counts": dict(reject_counts.most_common()),
        "train_shards": train_shards,
        "paired_shards": paired_shards,
    }

    if not args.dry_run:
        write_json(output_dir / "cleaning_report.json", report)
        write_json(output_dir / "manifest.json", {
            "source_repo": args.repo_id,
            "source_config": args.config,
            "source_split": args.split,
            "output_mode": args.output_mode,
            "train_shards": train_shards,
            "paired_shards": paired_shards,
            "val_file": "zzz_val_00000.parquet" if nano_val_rows and args.output_mode in {"nanochat", "both"} else None,
            "counts": report["counts"],
        })
        rejects_path = output_dir / "reject_samples.jsonl"
        rejects_path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in reject_samples)
            + ("\n" if reject_samples else ""),
            encoding="utf-8",
        )

    print("\nSummary")
    print(json.dumps(report["counts"], ensure_ascii=False, indent=2))
    if reject_counts:
        print("Reject counts:", dict(reject_counts.most_common(10)))

    if args.push_repo_id:
        if args.dry_run:
            raise SystemExit("--push-repo-id cannot be used with --dry-run")
        if not token:
            raise SystemExit("--push-repo-id requires HF_TOKEN/HUGGINGFACE_HUB_TOKEN")
        maybe_push_to_hub(output_dir, args.push_repo_id, token)
        print(f"Uploaded cleaned output to {args.push_repo_id}")


if __name__ == "__main__":
    main()
