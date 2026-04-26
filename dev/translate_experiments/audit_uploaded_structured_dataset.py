#!/usr/bin/env python3
"""Audit an uploaded structured Darija dataset on the Hugging Face Hub.

This is read-only. It uses HF_TOKEN/HUGGINGFACE_HUB_TOKEN or the cached
Hugging Face login token and reports structural/repetition suspicious rows.

Example:
  python dev/translate_experiments/audit_uploaded_structured_dataset.py \
      --repo-id Lyte/darija-structured-translated \
      --max-rows 10000 \
      --issues-out /workspace/darija_struct_out_v7/upload_audit_issues.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics as stats
import sys
from collections import Counter, defaultdict
from pathlib import Path

from datasets import load_dataset
from huggingface_hub import get_token

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from translate_structured import verify_structures  # noqa: E402
from translate_structured_vllm import (  # noqa: E402
    Job,
    _repetition_errors,
    _verify_unfenced_code_spans,
)


_LINE_WS_RE = re.compile(r"\s+")
_HAS_ARABIC_RE = re.compile(r"[\u0600-\u06FF]")
_CODEISH_LINE_RE = re.compile(
    r"^\s*(?:return|if|for|while|def|class|import|from|print|else|elif|try|except|"
    r"finally|with|assert|raise|break|continue|pass|[A-Za-z_]\w*\s*[=({\[])"
)


def repeated_line_issue(source: str, target: str) -> str | None:
    source_lines = [_LINE_WS_RE.sub(" ", line.strip()) for line in source.splitlines()]
    source_counts = Counter(line for line in source_lines if line)

    target_lines = [_LINE_WS_RE.sub(" ", line.strip()) for line in target.splitlines()]
    target_lines = [
        line
        for line in target_lines
        if len(line) >= 12
        and not _CODEISH_LINE_RE.match(line)
        and _HAS_ARABIC_RE.search(line)
    ]
    if not target_lines:
        return None
    line, count = Counter(target_lines).most_common(1)[0]
    if count >= 4 and count > source_counts.get(line, 0) + 2:
        return f"repeated line {count}x: {line[:90]!r}"
    return None


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = int((len(ordered) - 1) * pct)
    return ordered[idx]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser()
    p.add_argument("--repo-id", default="Lyte/darija-structured-translated")
    p.add_argument("--split", default="train")
    p.add_argument("--max-rows", type=int, default=10_000)
    p.add_argument("--issues-out", default=None)
    args = p.parse_args()

    token = (
        os.environ.get("HF_TOKEN")
        or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        or get_token()
    )
    if not token:
        sys.exit("ERROR: no HF token found in env or cached login")

    ds = load_dataset(args.repo_id, split=args.split, streaming=True, token=token)

    by_domain: Counter[str] = Counter()
    issue_counts: Counter[str] = Counter()
    issue_rows = 0
    src_by_domain: dict[str, list[int]] = defaultdict(list)
    ratio_by_domain: dict[str, list[float]] = defaultdict(list)
    dr_len_by_domain: dict[str, list[int]] = defaultdict(list)
    sample_issues: list[dict] = []
    issues_out = Path(args.issues_out) if args.issues_out else None
    if issues_out:
        issues_out.parent.mkdir(parents=True, exist_ok=True)
        issues_out.write_text("", encoding="utf-8")

    rows_read = 0
    for row_i, row in enumerate(ds):
        if row_i >= args.max_rows:
            break
        rows_read += 1
        domain = str(row.get("domain") or "")
        en = row.get("en") or ""
        dr = row.get("dr") or ""
        src_idx = row.get("src_idx")

        by_domain[domain] += 1
        if src_idx is not None:
            src_by_domain[domain].append(int(src_idx))
        ratio = len(dr) / max(len(en), 1)
        ratio_by_domain[domain].append(ratio)
        dr_len_by_domain[domain].append(len(dr))

        errs: list[str] = []
        errs.extend(verify_structures(en, dr, domain))
        if domain == "code":
            errs.extend(_verify_unfenced_code_spans(en, dr))

        rep_line = repeated_line_issue(en, dr)
        if rep_line:
            errs.append(rep_line)

        rep_errs = _repetition_errors(
            Job(row_i, 0, en, [], en, domain, "prose_only"),
            dr,
        )
        errs.extend([e for e in rep_errs if "output too long" in e])

        if ratio > 2.8 and len(dr) > 1000:
            errs.append(f"high dr/en char ratio: {ratio:.2f}")

        if not errs:
            continue

        issue_rows += 1
        for err in errs:
            issue_counts[err.split(":")[0]] += 1
        rec = {
            "row_i": row_i,
            "src_idx": src_idx,
            "domain": domain,
            "en_len": len(en),
            "dr_len": len(dr),
            "ratio": round(ratio, 2),
            "errors": errs[:8],
            "en_preview": en[:240].replace("\n", "\\n"),
            "dr_preview": dr[:420].replace("\n", "\\n"),
        }
        if len(sample_issues) < 20:
            sample_issues.append(rec)
        if issues_out:
            with issues_out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    summary = {
        "repo_id": args.repo_id,
        "split": args.split,
        "rows_read": rows_read,
        "by_domain": dict(by_domain),
        "src_idx_ranges": {
            d: [min(v), max(v)] for d, v in src_by_domain.items() if v
        },
        "ratio_summary": {
            d: {
                "median": round(stats.median(v), 2),
                "p95": round(percentile(v, 0.95), 2),
                "max": round(max(v), 2),
            }
            for d, v in ratio_by_domain.items()
            if v
        },
        "dr_len_summary": {
            d: {
                "median": int(stats.median(v)),
                "p95": int(percentile(v, 0.95)),
                "max": int(max(v)),
            }
            for d, v in dr_len_by_domain.items()
            if v
        },
        "issue_rows": issue_rows,
        "issue_counts": dict(issue_counts.most_common(12)),
        "sample_issues": sample_issues,
        "issues_out": str(issues_out) if issues_out else None,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
