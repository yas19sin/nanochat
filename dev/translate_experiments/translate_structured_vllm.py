#!/usr/bin/env python3
"""Batched vLLM version of translate_structured.py.

Takes the same per-domain pipeline (extract -> translate prose -> restitch ->
verify) but uses vLLM's in-process batched generation instead of one-at-a-time
HTTP calls. Writes resumable parquet shards and pushes to HF, same pattern as
scripts/translate_vllm_darija.py.

Key ideas:
  1. Each sample is turned into a "plan": a list of segments, where each
     segment is either a verbatim literal (code block, tool_call JSON, etc.)
     or a "job" (a prose span that needs translation).
  2. All jobs from a batch of samples are flattened into ONE llm.generate()
     call — vLLM's continuous-batching scheduler keeps the GPU saturated.
  3. Failed jobs (verification errors) get a single strict-prompt retry pass,
     also batched.
  4. Each sample is restitched and verified; failures are dropped.

Recommended config (RTX 5090):
    --batch-size 128         # samples per wave (each sample has 1-10+ jobs)
    --max-model-len 4096
    --gpu-mem-util 0.90

Env:
    HF_TOKEN            read token for source datasets
    HF_WRITE_TOKEN      write token for pushing; falls back to HF_TOKEN

Example:
    python -m dev.translate_experiments.translate_structured_vllm \\
        --domain all \\
        --n-code 50000 --n-toolcall 11000 --n-math 50000 \\
        --batch-size 128 \\
        --out-dir /workspace/darija_struct_out \\
        --repo-id Lyte/darija-structured-translated

Resuming: re-run with the same --out-dir. Per-domain manifests track progress.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# Import shared helpers from the sibling module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from translate_structured import (  # noqa: E402
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_STRICT,
    SYSTEM_PROMPT_PROSE_ONLY,
    FENCED_RE,
    INLINE_CODE_RE,
    ph,
    restitch,
    verify_structures,
    extract_toolcall,
    split_turns,
    is_pure_tool_call,
    extract_math,
    load_code_items,
    load_toolcall_items,
    load_math_items,
)


# ----------------------------------------------------------------------
# Plan / Job structures
# ----------------------------------------------------------------------
@dataclass
class Job:
    """One prose span to translate."""
    sample_id: int
    seg_id: int                  # index in the sample's plan
    masked: str                  # text sent to LLM (may contain §P0§... placeholders)
    originals: list[str]         # for restitch (empty if prose-only)
    original_text: str           # for domain verification
    domain: str                  # "code" | "toolcall" | "math"
    prompt_kind: str             # "default" | "prose_only"
    role: str | None = None      # for toolcall
    # Filled in after generation:
    result: str = ""             # restitched output
    errors: list[str] = field(default_factory=list)
    retry_needed: bool = False


@dataclass
class SamplePlan:
    sample_id: int
    en: str
    domain: str
    # plan: list of ("lit", str) or ("job", seg_id)
    plan: list[tuple[str, object]] = field(default_factory=list)
    jobs: list[Job] = field(default_factory=list)
    # Filled after assembly:
    dr: str = ""
    structure_errs: list[str] = field(default_factory=list)
    ok: bool = False


# ----------------------------------------------------------------------
# Planners
# ----------------------------------------------------------------------
def _plan_prose_segment(prose: str, sample_id: int, seg_id: int, domain: str) -> Job:
    """Handle one prose segment that may contain inline `backticks`."""
    inline_originals: list[str] = []

    def take(m):
        inline_originals.append(m.group(0))
        return ph(len(inline_originals) - 1)

    masked = INLINE_CODE_RE.sub(take, prose)
    return Job(
        sample_id=sample_id,
        seg_id=seg_id,
        masked=masked if inline_originals else prose,
        originals=inline_originals,
        original_text=prose,
        domain=domain,
        prompt_kind="default" if inline_originals else "prose_only",
    )


def _append_translation_job(sp: SamplePlan, prose: str) -> None:
    if prose.strip():
        seg_id = len(sp.plan)
        sp.jobs.append(_plan_prose_segment(prose, sp.sample_id, seg_id, "code"))
        sp.plan.append(("job", seg_id))
    elif prose:
        sp.plan.append(("lit", prose))


_CODE_KEYWORD_RE = re.compile(
    r"^(?:async\s+def|def|class|for|while|if|elif|else|try|except|finally|with)\b.*:\s*(?:#.*)?$"
)
_CODE_STMT_RE = re.compile(
    r"^(?:return|yield|break|continue|pass|import|from|print|assert|raise)\b"
)
_CODE_ASSIGN_RE = re.compile(
    r"^[A-Za-z_]\w*(?:\[[^\]]+\]|\.[A-Za-z_]\w*)?\s*(?:=|\+=|-=|\*=|/=|//=|%=)"
)
_CODE_CALL_RE = re.compile(
    r"^[a-z_]\w*(?:\.[a-z_]\w*)?\s*\([^)]*\)\s*(?:#.*)?$"
)


def _unfenced_code_score(line: str) -> int:
    """Return a heuristic code-likeness score for a non-fenced line."""
    s = line.strip()
    if not s or s.startswith("### "):
        return 0
    if s.startswith("#"):
        return 2
    if _CODE_KEYWORD_RE.match(s):
        return 3
    if _CODE_STMT_RE.match(s):
        return 3
    if _CODE_ASSIGN_RE.match(s):
        return 3
    if _CODE_CALL_RE.match(s):
        return 2
    if re.match(r"^[\[\{\(].*[\]\}\),]\s*$", s):
        return 2
    if re.match(r"^[\]\}\)],;]+$", s):
        return 1
    if line[:1].isspace() and re.search(r"[=(){}\[\]:]", s):
        return 2
    return 0


def _should_keep_unfenced_code(lines: list[str]) -> bool:
    scores = [_unfenced_code_score(line) for line in lines if line.strip()]
    if not scores:
        return False
    strong = sum(score >= 2 for score in scores)
    if len(scores) == 1:
        line = next(line.strip() for line in lines if line.strip())
        return scores[0] >= 3 or line.startswith("#") or _CODE_CALL_RE.match(line) is not None
    return strong >= 2 and strong / len(scores) >= 0.5


def _unfenced_code_spans(text: str) -> list[str]:
    """Find raw code-looking spans outside fenced markdown blocks."""
    spans: list[str] = []

    def scan(prose: str) -> None:
        code_buf: list[str] = []

        def flush_code() -> None:
            if not code_buf:
                return
            if _should_keep_unfenced_code(code_buf):
                spans.append("".join(code_buf))
            code_buf.clear()

        for line in prose.splitlines(keepends=True):
            stripped = line.strip()
            if not stripped or stripped.startswith("### "):
                flush_code()
                continue
            if _unfenced_code_score(line) > 0:
                code_buf.append(line)
            else:
                flush_code()
        flush_code()

    last_end = 0
    for m in FENCED_RE.finditer(text):
        scan(text[last_end:m.start()])
        last_end = m.end()
    scan(text[last_end:])
    return spans


def _verify_unfenced_code_spans(original_text: str, restitched: str) -> list[str]:
    missing = [span for span in _unfenced_code_spans(original_text) if span not in restitched]
    if not missing:
        return []
    preview = missing[0].strip().splitlines()[0][:80]
    return [f"unfenced code span mismatch: missing={len(missing)} first={preview!r}"]


def _append_code_prose_units(sp: SamplePlan, prose: str) -> None:
    """Split code-domain prose into small units before translation.

    CodeFeedback rows are rendered as "### Question" + prose + "### Answer" +
    prose/code/prose. The source also contains some long Python snippets that
    are not fenced. Small translation models mangle those if they see them as
    prose, so protect obvious raw code runs as literals too.
    """
    prose_buf: list[str] = []
    code_buf: list[str] = []

    def flush_prose() -> None:
        if not prose_buf:
            return
        _append_translation_job(sp, "".join(prose_buf))
        prose_buf.clear()

    def flush_code() -> None:
        if not code_buf:
            return
        chunk = "".join(code_buf)
        if _should_keep_unfenced_code(code_buf):
            flush_prose()
            sp.plan.append(("lit", chunk))
        else:
            prose_buf.extend(code_buf)
        code_buf.clear()

    for line in prose.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            flush_code()
            flush_prose()
            sp.plan.append(("lit", line))
            continue
        if stripped.startswith("### "):
            flush_code()
            flush_prose()
            sp.plan.append(("lit", line))
            continue
        if stripped.startswith(("- ", "* ")) or re.match(r"^\d+\.\s+", stripped):
            flush_code()
            flush_prose()
            _append_translation_job(sp, line)
            continue
        if _unfenced_code_score(line) > 0:
            flush_prose()
            code_buf.append(line)
        else:
            flush_code()
            prose_buf.append(line)

    flush_code()
    flush_prose()


def plan_code(sample_id: int, text: str) -> SamplePlan:
    sp = SamplePlan(sample_id=sample_id, en=text, domain="code")
    last_end = 0
    for m in FENCED_RE.finditer(text):
        if m.start() > last_end:
            prose = text[last_end:m.start()]
            _append_code_prose_units(sp, prose)
        sp.plan.append(("lit", m.group(0)))
        last_end = m.end()
    if last_end < len(text):
        prose = text[last_end:]
        _append_code_prose_units(sp, prose)
    return sp


def plan_toolcall(sample_id: int, text: str) -> SamplePlan:
    sp = SamplePlan(sample_id=sample_id, en=text, domain="toolcall")
    turns = split_turns(text)

    for role, content in turns:
        # Pure-passthrough turns (keep English / verbatim)
        if role in ("SYSTEM", "TOOL") or (role == "GPT" and is_pure_tool_call(content)):
            sp.plan.append(("lit", f"### {role}\n{content}"))
            continue

        ext = extract_toolcall(content)

        if not ext.masked.strip():
            sp.plan.append(("lit", f"### {role}\n{content}"))
            continue

        # Needs translation — create a job
        seg_id = len(sp.plan)
        if ext.originals:
            job = Job(
                sample_id=sample_id,
                seg_id=seg_id,
                masked=ext.masked,
                originals=ext.originals,
                original_text=content,
                domain="toolcall",
                prompt_kind="default",
                role=role,
            )
        else:
            job = Job(
                sample_id=sample_id,
                seg_id=seg_id,
                masked=content,
                originals=[],
                original_text=content,
                domain="toolcall",
                prompt_kind="default",  # full content, no inline code stripping
                role=role,
            )
        sp.jobs.append(job)
        sp.plan.append(("job", seg_id))

    return sp


_MATH_LITERAL_CHARS_RE = re.compile(r"[0-9=+\-*/^|<>()[\]{}.,:%]")
_MATH_WORD_RE = re.compile(r"[A-Za-z]{4,}")
_MATH_SIG_NUM_RE = re.compile(r"(?<![A-Za-z_])[-+]?\d{2,}(?:\.\d+)?(?![A-Za-z_])")
_PURE_PLACEHOLDERS_RE = re.compile(r"^(?:\s*§P\d+§\s*)+$")


def _is_math_literal_line(line: str) -> bool:
    """Keep compact formula/table/value lines verbatim in math examples."""
    s = line.strip()
    if not s:
        return False
    if (
        s.startswith(("$$", "$", r"\(", r"\[", r"\begin"))
        or s.endswith(("$$", "$", r"\)", r"\]"))
    ):
        return True
    if len(_MATH_SIG_NUM_RE.findall(s)) >= 2:
        return True
    if len(s) > 180:
        return False
    math_chars = len(_MATH_LITERAL_CHARS_RE.findall(s))
    if math_chars == 0:
        return False
    word_count = len(_MATH_WORD_RE.findall(s))
    math_ratio = math_chars / max(len(s), 1)
    if word_count <= 1 and math_ratio >= 0.20:
        return True
    if word_count <= 3 and math_ratio >= 0.35:
        return True
    if re.match(r"^(?:answer|answers|partial answers?|output|calculations?)\s*[:=]", s, re.I):
        return True
    return False


def _append_math_job(sp: SamplePlan, text: str) -> None:
    if not text:
        return
    if not text.strip():
        sp.plan.append(("lit", text))
        return
    ext = extract_math(text)
    if ext.originals and _PURE_PLACEHOLDERS_RE.match(ext.masked.strip()):
        sp.plan.append(("lit", text))
        return

    seg_id = len(sp.plan)
    sp.jobs.append(Job(
        sample_id=sp.sample_id,
        seg_id=seg_id,
        masked=ext.masked,
        originals=ext.originals,
        original_text=text,
        domain="math",
        prompt_kind="default",
    ))
    sp.plan.append(("job", seg_id))


def _append_math_units(sp: SamplePlan, text: str, max_chars: int = 600) -> None:
    buf: list[str] = []

    def flush_buf() -> None:
        if not buf:
            return
        _append_math_job(sp, "".join(buf))
        buf.clear()

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            flush_buf()
            sp.plan.append(("lit", line))
            continue
        if _is_math_literal_line(line):
            flush_buf()
            sp.plan.append(("lit", line))
            continue
        if sum(len(x) for x in buf) + len(line) > max_chars:
            flush_buf()
        buf.append(line)

    flush_buf()


def plan_math(sample_id: int, text: str) -> SamplePlan:
    sp = SamplePlan(sample_id=sample_id, en=text, domain="math")
    _append_math_units(sp, text)
    return sp


DOMAIN_PLANNERS = {
    "code": plan_code,
    "toolcall": plan_toolcall,
    "math": plan_math,
}


# ----------------------------------------------------------------------
# Prompt building
# ----------------------------------------------------------------------
def system_prompt_for(kind: str) -> str:
    return {
        "default": SYSTEM_PROMPT,
        "strict": SYSTEM_PROMPT_STRICT,
        "prose_only": SYSTEM_PROMPT_PROSE_ONLY,
    }[kind]


def build_prompt(tokenizer, user_text: str, kind: str) -> str:
    return tokenizer.apply_chat_template(
        [
            {"role": "system", "content": system_prompt_for(kind)},
            {"role": "user", "content": user_text},
        ],
        tokenize=False,
        add_generation_prompt=True,
    )


# ----------------------------------------------------------------------
# Job runner
# ----------------------------------------------------------------------
def _clean_prose_output(txt: str) -> str:
    """Strip stray ``` fences the model may emit on prose-only segments."""
    return re.sub(r"```[^\n`]*", "", txt).replace("```", "")


_WORD_RE = re.compile(r"[A-Za-z0-9_\u0600-\u06FF]+")


def _completeness_errors(job: Job, restitched: str) -> list[str]:
    """Catch severe prose omissions that structural checks cannot see."""
    if job.domain != "code":
        return []

    src = job.original_text.strip()
    out = restitched.strip()
    if len(src) < 180:
        return []

    src_words = _WORD_RE.findall(src)
    out_words = _WORD_RE.findall(out)
    if len(src_words) < 25:
        return []

    char_ratio = len(out) / max(len(src), 1)
    word_ratio = len(out_words) / max(len(src_words), 1)
    if char_ratio < 0.50 and word_ratio < 0.65:
        return [
            "possible prose omission: "
            f"chars {len(out)}/{len(src)} ({char_ratio:.2f}), "
            f"words {len(out_words)}/{len(src_words)} ({word_ratio:.2f})"
        ]
    return []


def run_jobs_pass(llm, sampling_params, tokenizer, jobs: list[Job], kind: str) -> None:
    """Run one batched generation pass over `jobs`, updating each job in place."""
    if not jobs:
        return
    prompts = []
    for j in jobs:
        # prose_only jobs stay as prose_only on default pass; everything else uses `kind`
        prompt_kind = j.prompt_kind if kind == "default" else "strict"
        prompts.append(build_prompt(tokenizer, j.masked, prompt_kind))
    outputs = llm.generate(prompts, sampling_params, use_tqdm=False)

    for j, out in zip(jobs, outputs):
        text = out.outputs[0].text.strip()
        if j.prompt_kind == "prose_only" or j.domain == "code":
            text = _clean_prose_output(text)

        if j.originals:
            restitched, errs = restitch(text, j.originals)
        else:
            restitched, errs = text, []

        struct_errs = verify_structures(j.original_text, restitched, j.domain)
        struct_errs += _completeness_errors(j, restitched)
        all_errs = errs + struct_errs

        # Only replace if this pass has fewer errors than what we already stored
        if kind == "default" or len(all_errs) < len(j.errors):
            j.result = restitched
            j.errors = all_errs

        j.retry_needed = bool(all_errs)


def assemble_sample(sp: SamplePlan) -> None:
    """Build final restitched sample text from plan + job results."""
    # If any job has un-recoverable error, mark sample as failed
    parts: list[str] = []
    all_errs: list[str] = []

    # For toolcall we need role prefix on translated segments
    role_map = {}
    for j in sp.jobs:
        if j.role:
            role_map[j.seg_id] = j.role

    for kind, val in sp.plan:
        if kind == "lit":
            parts.append(val)  # type: ignore[arg-type]
            continue
        seg_id = val  # type: ignore[assignment]
        job = next((j for j in sp.jobs if j.seg_id == seg_id), None)
        if job is None:
            continue
        if job.errors:
            all_errs.extend(job.errors)
        if job.role:
            parts.append(f"### {job.role}\n{job.result}")
        else:
            parts.append(job.result)

    if sp.domain == "toolcall":
        sp.dr = "\n\n".join(parts)
    else:
        sp.dr = "".join(parts)

    final_errs = verify_structures(sp.en, sp.dr, sp.domain) if not all_errs else []
    if sp.domain == "code" and not all_errs:
        final_errs += _verify_unfenced_code_spans(sp.en, sp.dr)
    sp.structure_errs = all_errs + final_errs
    sp.ok = not sp.structure_errs


# ----------------------------------------------------------------------
# Manifest / IO (mirrors translate_vllm_darija.py)
# ----------------------------------------------------------------------
def load_manifest(out_dir: Path) -> dict:
    mpath = out_dir / "manifest.json"
    if mpath.exists():
        return json.loads(mpath.read_text(encoding="utf-8"))
    return {
        "per_domain": {
            "code": {"rows_consumed": 0, "rows_accepted": 0, "next_shard_idx": 0, "shards": []},
            "toolcall": {"rows_consumed": 0, "rows_accepted": 0, "next_shard_idx": 0, "shards": []},
            "math": {"rows_consumed": 0, "rows_accepted": 0, "next_shard_idx": 0, "shards": []},
        },
        "started_at": time.time(),
    }


def save_manifest(out_dir: Path, manifest: dict) -> None:
    mpath = out_dir / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False),
                     encoding="utf-8")


def write_shard(out_dir: Path, domain: str, shard_idx: int, rows: list[dict]) -> dict:
    import pandas as pd
    sub = out_dir / domain
    sub.mkdir(parents=True, exist_ok=True)
    fname = f"{domain}_shard_{shard_idx:05d}.parquet"
    fpath = sub / fname
    df = pd.DataFrame(rows)
    df.to_parquet(fpath, index=False)
    return {
        "file": f"{domain}/{fname}",
        "rows": len(rows),
        "chars": int(df["dr"].str.len().sum()) if "dr" in df else 0,
    }


def _upload_with_retry(api, *, path_or_fileobj, path_in_repo, repo_id,
                       commit_message, max_attempts: int = 4, timeout_s: int = 300):
    import threading
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        result: dict = {}

        def _work():
            try:
                api.upload_file(
                    path_or_fileobj=path_or_fileobj,
                    path_in_repo=path_in_repo,
                    repo_id=repo_id, repo_type="dataset",
                    commit_message=commit_message,
                )
                result["ok"] = True
            except Exception as exc:
                result["exc"] = exc

        t = threading.Thread(target=_work, daemon=True)
        t.start()
        t.join(timeout_s)
        if result.get("ok"):
            return
        if t.is_alive():
            last_exc = TimeoutError(f"upload stalled > {timeout_s}s ({path_in_repo})")
        else:
            last_exc = result.get("exc") or RuntimeError("unknown upload failure")
        backoff = min(30, 5 * attempt)
        print(f"    !! upload retry {attempt}/{max_attempts} for {path_in_repo}: "
              f"{last_exc}; sleeping {backoff}s")
        time.sleep(backoff)
    raise last_exc  # type: ignore[misc]


def upload_shard(repo_id: str, out_dir: Path, entry: dict, hf_write_token: str) -> None:
    try:
        from huggingface_hub import HfApi
        api = HfApi(token=hf_write_token)
        _upload_with_retry(
            api,
            path_or_fileobj=out_dir / entry["file"],
            path_in_repo=f"data/{entry['file']}",
            repo_id=repo_id,
            commit_message=f"add {entry['file']} ({entry['rows']} rows)",
        )
        _upload_with_retry(
            api,
            path_or_fileobj=out_dir / "manifest.json",
            path_in_repo="manifest.json",
            repo_id=repo_id,
            commit_message=f"manifest after {entry['file']}",
        )
        print(f"    uploaded {entry['file']} -> {repo_id}")
    except Exception as exc:
        print(f"    !! upload failed ({exc}); will retry on next shard")


def ensure_repo(repo_id: str, hf_write_token: str) -> None:
    from huggingface_hub import HfApi
    api = HfApi(token=hf_write_token)
    api.create_repo(repo_id=repo_id, repo_type="dataset",
                    exist_ok=True, private=False)


# ----------------------------------------------------------------------
# Domain runner
# ----------------------------------------------------------------------
def process_domain(
    *,
    domain: str,
    target_rows: int,
    batch_size: int,
    shard_rows: int,
    llm,
    sampling_params,
    tokenizer,
    manifest: dict,
    out_dir: Path,
    repo_id: str | None,
    hf_token: str | None,
    hf_write_token: str,
    stop_flag: dict,
    progress_every: int,
    reject_sample_rows: int,
) -> None:
    dm = manifest["per_domain"][domain]
    rows_already = dm["rows_accepted"]
    if rows_already >= target_rows:
        print(f"[{domain}] already at target ({rows_already}/{target_rows}), skipping")
        return

    print(f"\n[{domain}] target={target_rows}  already_done={rows_already}")
    skip = dm["rows_consumed"]

    # Load items. The existing loaders don't support streaming-skip, so we
    # stream the dataset ourselves and use the loader signature for the
    # filter logic. For simplicity and consistency with the validated
    # pipeline, we call load_* with (target - already) as n and skip by
    # slicing. That re-reads skipped rows from HF, but HF streaming is cheap.
    need = target_rows - rows_already
    over_fetch = int(need * 1.15) + batch_size  # buffer for filtered drops
    loader = {
        "code": load_code_items,
        "toolcall": load_toolcall_items,
        "math": load_math_items,
    }[domain]
    print(f"[{domain}] streaming {over_fetch} source items (skip={skip})...")
    all_items = loader(skip + over_fetch, hf_token, 0)
    items = all_items[skip:]
    print(f"[{domain}] got {len(items)} items to process")

    shard_buffer: list[dict] = []
    shard_idx = dm["next_shard_idx"]
    rows_consumed = skip
    rows_accepted = rows_already
    t_loop = time.time()
    batches_done = 0
    drops_total = 0
    drop_reasons: Counter[str] = Counter()
    reject_samples_written = 0

    def note_reject(sp: SamplePlan, src_idx: int) -> None:
        nonlocal reject_samples_written
        reason = sp.structure_errs[0] if sp.structure_errs else "unknown"
        if len(reason) > 180:
            reason = reason[:177] + "..."
        drop_reasons[reason] += 1

        if reject_samples_written >= reject_sample_rows:
            return
        sub = out_dir / domain
        sub.mkdir(parents=True, exist_ok=True)
        rpath = sub / f"{domain}_reject_samples.jsonl"
        rec = {
            "src_idx": src_idx,
            "domain": domain,
            "errors": sp.structure_errs,
            "en": sp.en,
            "dr": sp.dr,
        }
        with rpath.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        reject_samples_written += 1

    def flush_shard(final: bool = False) -> None:
        nonlocal shard_idx
        if not shard_buffer:
            return
        entry = write_shard(out_dir, domain, shard_idx, shard_buffer)
        dm["shards"].append(entry)
        dm["next_shard_idx"] = shard_idx + 1
        dm["rows_consumed"] = rows_consumed
        dm["rows_accepted"] = rows_accepted
        save_manifest(out_dir, manifest)
        print(f"  wrote {entry['file']} ({entry['rows']} rows, {entry['chars']:,} chars)"
              + (" [FINAL]" if final else ""))
        if repo_id:
            upload_shard(repo_id, out_dir, entry, hf_write_token)
        shard_buffer.clear()
        shard_idx += 1

    # ------------------------------------------------------------ wave loop
    idx = 0
    planner = DOMAIN_PLANNERS[domain]
    while idx < len(items) and rows_accepted < target_rows:
        if stop_flag["v"]:
            print("[stop] flushing and exiting")
            break

        wave = items[idx: idx + batch_size]
        idx += len(wave)
        rows_consumed += len(wave)

        # 1. plan every sample in the wave
        plans: list[SamplePlan] = [planner(i, text) for i, text in enumerate(wave)]
        all_jobs: list[Job] = []
        for sp in plans:
            all_jobs.extend(sp.jobs)

        # 2. pass 1 — default prompt
        run_jobs_pass(llm, sampling_params, tokenizer, all_jobs, kind="default")

        # 3. retry pass — strict prompt for anything still flagged
        retries = [j for j in all_jobs if j.retry_needed]
        if retries:
            run_jobs_pass(llm, sampling_params, tokenizer, retries, kind="strict")

        # 4. assemble each sample, push successful ones to shard buffer
        kept = 0
        for sp in plans:
            assemble_sample(sp)
            src_idx = skip + (idx - len(wave)) + sp.sample_id
            if sp.ok:
                shard_buffer.append({
                    "src_idx": src_idx,
                    "domain": domain,
                    "en": sp.en,
                    "dr": sp.dr,
                })
                rows_accepted += 1
                kept += 1
                if rows_accepted >= target_rows:
                    break
            else:
                drops_total += 1
                note_reject(sp, src_idx)

        batches_done += 1
        if batches_done % progress_every == 0:
            elapsed = time.time() - t_loop
            rps = (rows_accepted - rows_already) / max(elapsed, 1e-6)
            remain = target_rows - rows_accepted
            eta_s = remain / max(rps, 1e-6)
            print(f"  [{domain} wave {batches_done:>4}] "
                  f"kept={rows_accepted:>6}/{target_rows}  "
                  f"drops={drops_total}  "
                  f"rows/s={rps:.1f}  "
                  f"eta={eta_s/3600:.1f}h  "
                  f"buf={len(shard_buffer)}/{shard_rows}")
            if drop_reasons:
                top = "; ".join(
                    f"{count}x {reason}"
                    for reason, count in drop_reasons.most_common(3)
                )
                print(f"    top_drop_reasons: {top}")

        if len(shard_buffer) >= shard_rows:
            flush_shard()

    # final flush
    flush_shard(final=True)
    elapsed = time.time() - t_loop
    print(f"[{domain}] done: kept={rows_accepted}/{target_rows}  "
          f"drops={drops_total}  elapsed={elapsed/60:.1f} min")
    if drop_reasons:
        top = "; ".join(
            f"{count}x {reason}" for reason, count in drop_reasons.most_common(8)
        )
        print(f"[{domain}] top drop reasons: {top}")


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="Lyte/tiny-aya-darija-v5")
    p.add_argument("--domain", choices=["code", "toolcall", "math", "all"], default="all")
    p.add_argument("--n-code", type=int, default=50_000)
    p.add_argument("--n-toolcall", type=int, default=11_000)
    p.add_argument("--n-math", type=int, default=50_000)
    p.add_argument("--batch-size", type=int, default=128,
                   help="samples per wave (jobs/wave will be 1-10x this)")
    p.add_argument("--max-new-tokens", type=int, default=1024)
    p.add_argument("--max-model-len", type=int, default=4096)
    p.add_argument("--gpu-mem-util", type=float, default=0.90)
    p.add_argument("--enforce-eager", action="store_true")
    p.add_argument("--attention-backend", default=None,
                   help="vLLM attention backend (FLASH_ATTN, FLASHINFER, TRITON_ATTN, FLEX_ATTENTION). None = vLLM default.")
    p.add_argument("--shard-rows", type=int, default=2_000)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--repo-id", default=None)
    p.add_argument("--progress-every", type=int, default=1)
    p.add_argument("--reject-sample-rows", type=int, default=200,
                   help="per-domain failed rows to save under OUT_DIR/<domain>/*_reject_samples.jsonl")
    args = p.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    hf_write_token = os.environ.get("HF_WRITE_TOKEN") or hf_token
    if not hf_token:
        sys.exit("ERROR: HF_TOKEN not set")

    if args.repo_id:
        print(f"[hub] ensuring dataset repo exists: {args.repo_id}")
        ensure_repo(args.repo_id, hf_write_token)

    # -------- load vLLM
    print(f"[model] loading {args.model}...")
    t0 = time.time()
    from vllm import LLM, SamplingParams
    from transformers import AutoTokenizer
    llm_kwargs = dict(
        model=args.model,
        dtype="bfloat16",
        max_model_len=args.max_model_len,
        gpu_memory_utilization=args.gpu_mem_util,
        enforce_eager=args.enforce_eager,
        trust_remote_code=True,
    )
    if args.attention_backend:
        llm_kwargs["attention_backend"] = args.attention_backend
    llm = LLM(**llm_kwargs)
    tokenizer = AutoTokenizer.from_pretrained(args.model, token=hf_token)
    sampling_params = SamplingParams(
        temperature=0.3, top_p=0.98, top_k=300,
        repetition_penalty=1.15,
        max_tokens=args.max_new_tokens,
    )
    print(f"[model] ready in {time.time()-t0:.1f}s")

    manifest = load_manifest(out_dir)

    stop_flag = {"v": False}

    def _stop(signum, frame):
        print(f"\n[signal] got {signum}, will flush after current wave")
        stop_flag["v"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    domains_to_run = (
        ["code", "toolcall", "math"] if args.domain == "all" else [args.domain]
    )
    target_map = {
        "code": args.n_code,
        "toolcall": args.n_toolcall,
        "math": args.n_math,
    }

    for domain in domains_to_run:
        if stop_flag["v"]:
            break
        process_domain(
            domain=domain,
            target_rows=target_map[domain],
            batch_size=args.batch_size,
            shard_rows=args.shard_rows,
            llm=llm,
            sampling_params=sampling_params,
            tokenizer=tokenizer,
            manifest=manifest,
            out_dir=out_dir,
            repo_id=args.repo_id,
            hf_token=hf_token,
            hf_write_token=hf_write_token,
            stop_flag=stop_flag,
            progress_every=args.progress_every,
            reject_sample_rows=args.reject_sample_rows,
        )


if __name__ == "__main__":
    main()
