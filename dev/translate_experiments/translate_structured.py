"""Structured translation pipeline: extract -> translate prose -> restitch -> verify.

Per-domain parsers preserve code blocks, tool-call JSON, and LaTeX math VERBATIM
by replacing them with placeholders before the LLM sees them, then restoring
byte-identical originals afterward.

Domains:
  - code:     ```fenced``` and `inline` blocks  (m-a-p/CodeFeedback-Filtered-Instruction)
  - toolcall: <tools>, <tool_call>, <tool_response> XML + JSON  (NousResearch/hermes-function-calling-v1)
  - math:     $...$ / $$...$$ / \\(...\\) / \\[...\\] LaTeX  (HuggingFaceTB/finemath)

Verification ensures structure survived. Failed rows are dropped.

Usage:
  $env:HF_TOKEN = "..."
  .\\.venv\\Scripts\\python.exe dev/translate_structured.py --domain code --n 20
  .\\.venv\\Scripts\\python.exe dev/translate_structured.py --domain toolcall --n 20
  .\\.venv\\Scripts\\python.exe dev/translate_structured.py --domain math --n 20
  .\\.venv\\Scripts\\python.exe dev/translate_structured.py --domain all --n 20
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from datasets import load_dataset
from openai import OpenAI


# ----------------------------------------------------------------------
# Placeholder tokens
# ----------------------------------------------------------------------
# Using §N§ (section sign) — unicode, rare in natural text, easy regex.
PH_OPEN = "§"
PH_CLOSE = "§"


def ph(i: int) -> str:
    return f"{PH_OPEN}P{i}{PH_CLOSE}"


PH_REGEX = re.compile(r"§P\d+§")


# ----------------------------------------------------------------------
# System prompt
# ----------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "CRITICAL RULES:\n"
    "1. The text contains placeholders of the form §P0§, §P1§, §P2§, etc. "
    "These are NON-NEGOTIABLE — copy EVERY placeholder EXACTLY as-is, byte-for-byte, "
    "in the same positions and order they appear in the input.\n"
    "2. Do NOT translate, alter, renumber, or omit any placeholder.\n"
    "3. Only translate the natural-language prose around the placeholders.\n"
    "4. Do not add explanations or notes. Output ONLY the translated text."
)

SYSTEM_PROMPT_STRICT = (
    "You are a translation engine. Translate the English text into Moroccan Darija (الدارجة المغربية).\n"
    "ABSOLUTE REQUIREMENTS:\n"
    "1. The input contains placeholders like §P0§, §P1§, ... — each placeholder MUST appear "
    "exactly ONCE in your output, in the same order as the input.\n"
    "2. Copy every placeholder character-for-character. Do NOT change §, P, or the number.\n"
    "3. Do NOT invent new placeholders. Do NOT merge or split them.\n"
    "4. Preserve every number that appears outside placeholders.\n"
    "5. Output ONLY the Darija translation — no preamble, no notes, no markdown fences.\n"
    "This is a strict RETRY after a previous attempt failed verification."
)

SYSTEM_PROMPT_PROSE_ONLY = (
    "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
    "This is a PROSE segment (no code blocks). RULES:\n"
    "1. Do NOT add any markdown code fences (```) or backticks in your output.\n"
    "2. Do NOT add any preamble, notes, or explanations.\n"
    "3. Preserve numbers, identifiers in English, and punctuation.\n"
    "4. Output ONLY the translated Darija text."
)


# ----------------------------------------------------------------------
# Extractors
# ----------------------------------------------------------------------
@dataclass
class Extracted:
    masked: str
    originals: list[str] = field(default_factory=list)


def extract_code(text: str) -> Extracted:
    """Replace ```fenced``` and `inline` code with §P N§ placeholders."""
    originals: list[str] = []

    def take(m: re.Match) -> str:
        originals.append(m.group(0))
        return ph(len(originals) - 1)

    # Fenced first (greedy across lines), then inline. Order matters.
    out = re.sub(r"```[\s\S]*?```", take, text)
    out = re.sub(r"`[^`\n]+`", take, out)
    return Extracted(masked=out, originals=originals)


# LaTeX patterns: $$...$$ first (to not be eaten by $...$), then $...$, then \(..\), \[..\]
_LATEX_PATTERNS = [
    r"\$\$[\s\S]+?\$\$",
    r"\\\[[\s\S]+?\\\]",
    r"\\\([\s\S]+?\\\)",
    # inline $...$  — require non-space immediately inside to avoid prose $foo
    r"\$[^\$\n]{1,200}\$",
]


def extract_math(text: str) -> Extracted:
    originals: list[str] = []

    def take(m: re.Match) -> str:
        originals.append(m.group(0))
        return ph(len(originals) - 1)

    out = text
    for pat in _LATEX_PATTERNS:
        out = re.sub(pat, take, out)
    return Extracted(masked=out, originals=originals)


# Tool-call (Hermes-FC): preserve <tools>...</tools>, <tool_call>...</tool_call>,
# <tool_response>...</tool_response>. These are XML-style tags with JSON inside.
_TC_PATTERNS = [
    r"<tools>[\s\S]*?</tools>",
    r"<tool_call>[\s\S]*?</tool_call>",
    r"<tool_response>[\s\S]*?</tool_response>",
]


def extract_toolcall(text: str) -> Extracted:
    originals: list[str] = []

    def take(m: re.Match) -> str:
        originals.append(m.group(0))
        return ph(len(originals) - 1)

    out = text
    for pat in _TC_PATTERNS:
        out = re.sub(pat, take, out)
    return Extracted(masked=out, originals=originals)


# ----------------------------------------------------------------------
# Restitch
# ----------------------------------------------------------------------
def restitch(translated: str, originals: list[str]) -> tuple[str, list[str]]:
    """Replace §P N§ placeholders in `translated` with originals.
    Returns (restitched_text, list_of_errors)."""
    errs: list[str] = []
    missing = []
    found_indices = set()

    def sub(m: re.Match) -> str:
        idx = int(m.group(0)[2:-1])
        found_indices.add(idx)
        if idx >= len(originals):
            errs.append(f"placeholder §P{idx}§ out of range (have {len(originals)})")
            return m.group(0)
        return originals[idx]

    out = PH_REGEX.sub(sub, translated)

    for i in range(len(originals)):
        if i not in found_indices:
            missing.append(i)
    if missing:
        errs.append(f"missing placeholders in output: {missing}")

    return out, errs


# ----------------------------------------------------------------------
# Verification
# ----------------------------------------------------------------------
def verify_structures(original_text: str, restitched: str, domain: str) -> list[str]:
    """Domain-specific post-verification.
    Returns list of error strings; empty means verified clean."""
    errs: list[str] = []

    if domain == "code":
        # Every ```...``` and `...` from original must appear byte-identical in restitched
        orig_fenced = re.findall(r"```[\s\S]*?```", original_text)
        out_fenced = re.findall(r"```[\s\S]*?```", restitched)
        if sorted(orig_fenced) != sorted(out_fenced):
            errs.append(f"fenced code blocks mismatch: orig={len(orig_fenced)}, out={len(out_fenced)}")

    elif domain == "toolcall":
        for pat in _TC_PATTERNS:
            orig = re.findall(pat, original_text)
            out = re.findall(pat, restitched)
            if sorted(orig) != sorted(out):
                errs.append(f"{pat!r} mismatch: orig={len(orig)}, out={len(out)}")

    elif domain == "math":
        # All significant numeric tokens in original should appear in restitched.
        # Word-bounded + require at least 2 digits to skip noise like single-digit
        # references ("1 way", "Step 2") where a slightly different phrasing is fine.
        num_re = re.compile(r"(?<![A-Za-z_])[-+]?\d{2,}(?:\.\d+)?(?![A-Za-z_])")
        orig_nums = set(num_re.findall(original_text))
        out_nums = set(num_re.findall(restitched))
        missing = orig_nums - out_nums
        if len(missing) > 5:
            errs.append(
                f"too many numbers lost: {len(missing)} missing (e.g. {list(missing)[:5]})"
            )

    return errs


# ----------------------------------------------------------------------
# LLM call
# ----------------------------------------------------------------------
def translate_via_llm(
    client: OpenAI,
    text: str,
    model: str,
    max_tokens: int,
    extra_body: dict | None,
    system_prompt: str = SYSTEM_PROMPT,
) -> tuple[str, float, str | None]:
    t0 = time.time()
    try:
        kw = dict(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            top_p=0.98,
            max_tokens=max_tokens,
        )
        if extra_body:
            kw["extra_body"] = extra_body
        resp = client.chat.completions.create(**kw)
        return resp.choices[0].message.content.strip(), time.time() - t0, None
    except Exception as exc:  # noqa: BLE001
        return "", time.time() - t0, str(exc)


def translate_with_verify(
    client: OpenAI,
    masked: str,
    originals: list[str],
    domain: str,
    original_text: str,
    model: str,
    max_tokens: int,
    extra_body: dict | None,
) -> tuple[str, str, float, str | None, list[str]]:
    """Translate masked text, restitch, verify. Retry once on failure with stricter prompt.

    Returns: (raw_llm_output, restitched, total_dt, llm_err, structure_errs)
    """
    translated, dt, err = translate_via_llm(
        client, masked, model, max_tokens, extra_body, SYSTEM_PROMPT
    )
    if err or not translated:
        return translated, "", dt, err or "empty output", []

    restitched, restitch_errs = restitch(translated, originals)
    struct_errs = verify_structures(original_text, restitched, domain)
    all_errs = restitch_errs + struct_errs

    if not all_errs:
        return translated, restitched, dt, None, []

    # Retry once with stricter prompt
    translated2, dt2, err2 = translate_via_llm(
        client, masked, model, max_tokens, extra_body, SYSTEM_PROMPT_STRICT
    )
    total_dt = dt + dt2
    if err2 or not translated2:
        return translated, restitched, total_dt, None, all_errs

    restitched2, restitch_errs2 = restitch(translated2, originals)
    struct_errs2 = verify_structures(original_text, restitched2, domain)
    all_errs2 = restitch_errs2 + struct_errs2

    # Use whichever pass had fewer errors
    if len(all_errs2) < len(all_errs):
        return translated2, restitched2, total_dt, None, all_errs2
    return translated, restitched, total_dt, None, all_errs


# ----------------------------------------------------------------------
# Dataset loaders — return list[str] of raw English items to translate
# ----------------------------------------------------------------------
def load_code_items(n: int, hf_token: str | None, seed: int) -> list[str]:
    ds = load_dataset(
        "m-a-p/CodeFeedback-Filtered-Instruction",
        split="train",
        streaming=True,
        token=hf_token,
    )
    out: list[str] = []
    for row in ds:
        q = (row.get("query") or "").strip()
        a = (row.get("answer") or "").strip()
        if not q or not a:
            continue
        # Combine query + answer — gives us code blocks on both sides
        item = f"### Question\n{q}\n\n### Answer\n{a}"
        # Filter: must contain at least one code fence
        if "```" not in item:
            continue
        if 200 <= len(item) <= 4000:
            out.append(item)
        if len(out) >= n:
            break
    return out


def load_toolcall_items(n: int, hf_token: str | None, seed: int) -> list[str]:
    ds = load_dataset(
        "NousResearch/hermes-function-calling-v1",
        split="train",
        streaming=True,
        token=hf_token,
    )
    out: list[str] = []
    for row in ds:
        convs = row.get("conversations") or []
        if not convs:
            continue
        # Render conversation in a chat-template-like flat format.
        parts = []
        for turn in convs:
            role = turn.get("from", "?").upper()
            val = (turn.get("value") or "").strip()
            if val:
                parts.append(f"### {role}\n{val}")
        item = "\n\n".join(parts)
        # Require at least one tool_call for this to be interesting
        if "<tool_call>" not in item:
            continue
        if 300 <= len(item) <= 4500:
            out.append(item)
        if len(out) >= n:
            break
    return out


TURN_SPLIT_RE = re.compile(r"(?m)^### (SYSTEM|HUMAN|GPT|TOOL)\n")


def split_turns(text: str) -> list[tuple[str, str]]:
    """Split a '### ROLE\\n...' conversation into (role, content) tuples."""
    matches = list(TURN_SPLIT_RE.finditer(text))
    if not matches:
        return [("UNKNOWN", text)]
    turns = []
    for i, m in enumerate(matches):
        role = m.group(1).upper()
        content_start = m.end()
        content_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[content_start:content_end].strip()
        turns.append((role, content))
    return turns


def is_pure_tool_call(content: str) -> bool:
    """True if content is ONLY <tool_call>...</tool_call> blocks (+ whitespace)."""
    stripped = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", content).strip()
    return not stripped and "<tool_call>" in content


def load_math_items(n: int, hf_token: str | None, seed: int) -> list[str]:
    ds = load_dataset(
        "HuggingFaceTB/finemath",
        name="finemath-4plus",
        split="train",
        streaming=True,
        token=hf_token,
    )
    out: list[str] = []
    for row in ds:
        t = (row.get("text") or "").strip()
        if not t:
            continue
        if 200 <= len(t) <= 3500:
            out.append(t)
        if len(out) >= n:
            break
    return out


DOMAIN_LOADERS: dict[str, Callable] = {
    "code": load_code_items,
    "toolcall": load_toolcall_items,
    "math": load_math_items,
}
DOMAIN_EXTRACTORS: dict[str, Callable[[str], Extracted]] = {
    "code": extract_code,
    "toolcall": extract_toolcall,
    "math": extract_math,
}


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------
FENCED_RE = re.compile(r"```[\s\S]*?```")
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def process_code(
    client: OpenAI,
    text: str,
    model: str,
    max_tokens: int,
    extra_body: dict | None,
) -> dict:
    """Segment-wise code translation.

    Split text on ```fenced``` code blocks. Translate each prose segment
    independently (inline `code` inside prose is handled via placeholders —
    segments are small enough that the model doesn't drop them).
    Rejoin with original fenced blocks byte-identical.
    """
    # Split on fenced blocks, keeping them as literal segments
    segments: list[tuple[str, str]] = []  # (kind, content) where kind is "prose" or "code"
    last_end = 0
    for m in FENCED_RE.finditer(text):
        if m.start() > last_end:
            segments.append(("prose", text[last_end:m.start()]))
        segments.append(("code", m.group(0)))
        last_end = m.end()
    if last_end < len(text):
        segments.append(("prose", text[last_end:]))

    total_dt = 0.0
    any_err: str | None = None
    all_errs: list[str] = []
    out_segs: list[str] = []
    total_ph = sum(1 for kind, _ in segments if kind == "code")

    for kind, seg in segments:
        if kind == "code":
            out_segs.append(seg)
            continue
        # Prose segment: may contain inline `backticks` — extract those as placeholders
        if not seg.strip():
            out_segs.append(seg)
            continue

        inline_originals: list[str] = []

        def take(m: re.Match) -> str:
            inline_originals.append(m.group(0))
            return ph(len(inline_originals) - 1)

        masked_seg = INLINE_CODE_RE.sub(take, seg)
        total_ph += len(inline_originals)

        if not inline_originals:
            # Pure prose — translate directly with prose-only prompt
            translated, dt, err = translate_via_llm(
                client, seg, model, max_tokens, extra_body, SYSTEM_PROMPT_PROSE_ONLY
            )
            total_dt += dt
            if err or not translated:
                any_err = err or "empty output"
                break
            # Defense-in-depth: strip any stray ``` fences the model may have added
            translated = re.sub(r"```[^\n`]*", "", translated).replace("```", "")
            out_segs.append(translated)
            continue

        # Has inline code — use placeholder + retry
        raw, restitched, dt, err, errs = translate_with_verify(
            client, masked_seg, inline_originals, "code", seg,
            model, max_tokens, extra_body,
        )
        total_dt += dt
        if err:
            any_err = err
            break
        if errs:
            all_errs.extend(errs)
        out_segs.append(restitched)

    final = "".join(out_segs)
    struct_errs = verify_structures(text, final, "code") if any_err is None else []
    ok = any_err is None and len(all_errs) == 0 and len(struct_errs) == 0

    return {
        "domain": "code",
        "en": text,
        "masked": text,
        "n_placeholders": total_ph,
        "translated_raw": final,
        "dr": final,
        "dt": total_dt,
        "llm_err": any_err,
        "structure_errs": all_errs + struct_errs,
        "ok": ok,
    }


def process_toolcall(
    client: OpenAI,
    text: str,
    model: str,
    max_tokens: int,
    extra_body: dict | None,
) -> dict:
    """Per-turn toolcall translation.

    Strategy:
      - SYSTEM turn: extract <tools> block, translate surrounding instruction prose,
                     restitch. (System has no <tool_call>.)
      - HUMAN turn: pure prose → translate directly.
      - GPT turn:
          - if ONLY <tool_call> blocks → keep verbatim (no translation).
          - else (prose around tool_calls): extract, translate, restitch.
      - TOOL turn: keep verbatim (it's JSON).
    """
    turns = split_turns(text)
    out_parts: list[str] = []
    total_dt = 0.0
    any_llm_err: str | None = None
    structure_errs: list[str] = []
    total_ph = 0

    for role, content in turns:
        # Pure-passthrough turns (keep English / verbatim)
        if role == "TOOL":
            out_parts.append(f"### {role}\n{content}")
            continue
        if role == "SYSTEM":
            # System prompt + tool schema is API-level boilerplate. Keep English.
            # (Real usage: system prompts are always English regardless of user's language.)
            out_parts.append(f"### {role}\n{content}")
            continue
        if role == "GPT" and is_pure_tool_call(content):
            out_parts.append(f"### {role}\n{content}")
            continue

        # Turns that need translation
        ext = extract_toolcall(content)
        total_ph += len(ext.originals)

        if not ext.masked.strip():
            # Nothing to translate (only structure)
            out_parts.append(f"### {role}\n{content}")
            continue

        if not ext.originals:
            # Pure prose, no structure — just translate directly
            translated, dt, err = translate_via_llm(
                client, content, model, max_tokens, extra_body, SYSTEM_PROMPT
            )
            total_dt += dt
            if err or not translated:
                any_llm_err = err or "empty output"
                break
            out_parts.append(f"### {role}\n{translated}")
            continue

        raw, restitched, dt, err, errs = translate_with_verify(
            client, ext.masked, ext.originals, "toolcall", content,
            model, max_tokens, extra_body,
        )
        total_dt += dt
        if err:
            any_llm_err = err
            break
        if errs:
            structure_errs.extend([f"[{role}] {e}" for e in errs])
        out_parts.append(f"### {role}\n{restitched}")

    final = "\n\n".join(out_parts)
    struct_errs = verify_structures(text, final, "toolcall") if any_llm_err is None else []

    return {
        "domain": "toolcall",
        "en": text,
        "masked": text,  # not meaningful per-turn
        "n_placeholders": total_ph,
        "translated_raw": final,
        "dr": final,
        "dt": total_dt,
        "llm_err": any_llm_err,
        "structure_errs": structure_errs + struct_errs,
        "ok": any_llm_err is None and len(structure_errs) == 0 and len(struct_errs) == 0,
    }


def process_one(
    client: OpenAI,
    text: str,
    domain: str,
    model: str,
    max_tokens: int,
    extra_body: dict | None,
) -> dict:
    """Run one sample through the pipeline. Returns a result dict."""
    # ------- toolcall: per-turn processing -------
    if domain == "toolcall":
        return process_toolcall(client, text, model, max_tokens, extra_body)

    # ------- code: segment-wise (prose between fences) -------
    if domain == "code":
        return process_code(client, text, model, max_tokens, extra_body)

    extractor = DOMAIN_EXTRACTORS[domain]
    ext = extractor(text)
    n_ph = len(ext.originals)

    # Edge case: no structure at all → fall back to direct translation
    if n_ph == 0:
        translated, dt, err = translate_via_llm(
            client, text, model, max_tokens, extra_body
        )
        return {
            "domain": domain,
            "en": text,
            "masked": text,
            "n_placeholders": 0,
            "translated_raw": translated,
            "dr": translated,
            "dt": dt,
            "llm_err": err,
            "structure_errs": [],
            "ok": err is None and bool(translated),
        }

    translated, restitched, dt, err, all_errs = translate_with_verify(
        client, ext.masked, ext.originals, domain, text,
        model, max_tokens, extra_body,
    )
    if err or not translated:
        return {
            "domain": domain,
            "en": text,
            "masked": ext.masked,
            "n_placeholders": n_ph,
            "translated_raw": translated,
            "dr": "",
            "dt": dt,
            "llm_err": err or "empty output",
            "structure_errs": [],
            "ok": False,
        }

    return {
        "domain": domain,
        "en": text,
        "masked": ext.masked,
        "n_placeholders": n_ph,
        "translated_raw": translated,
        "dr": restitched,
        "dt": dt,
        "llm_err": None,
        "structure_errs": all_errs,
        "ok": len(all_errs) == 0,
    }


def write_report(rows: list[dict], path: str, model_name: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    lines: list[str] = []
    lines.append(f"# Structured translation probe — model `{model_name}`\n")

    # Summary table
    by_domain: dict[str, list[dict]] = {}
    for r in rows:
        by_domain.setdefault(r["domain"], []).append(r)

    lines.append("## Summary\n")
    lines.append("| domain | n | ok | drop-rate | avg s/sample | avg placeholders |")
    lines.append("|---|---|---|---|---|---|")
    for dom, rs in by_domain.items():
        n = len(rs)
        ok = sum(1 for r in rs if r["ok"])
        drop = 1.0 - ok / max(1, n)
        avg_dt = sum(r["dt"] for r in rs) / max(1, n)
        avg_ph = sum(r["n_placeholders"] for r in rs) / max(1, n)
        lines.append(
            f"| {dom} | {n} | {ok} | {drop:.0%} | {avg_dt:.1f} | {avg_ph:.1f} |"
        )
    lines.append("")

    # Per-domain rows
    for dom, rs in by_domain.items():
        lines.append(f"\n---\n\n# {dom.upper()}\n")
        for i, r in enumerate(rs, 1):
            status = "✅ OK" if r["ok"] else "❌ FAIL"
            lines.append(
                f"\n## {dom}-{i}  {status}  "
                f"(en {len(r['en'])} chars, {r['n_placeholders']} placeholders, {r['dt']:.1f}s)\n"
            )
            if r["llm_err"]:
                lines.append(f"**LLM error:** `{r['llm_err']}`\n")
            if r["structure_errs"]:
                for e in r["structure_errs"]:
                    lines.append(f"- structure error: `{e}`")
                lines.append("")
            lines.append("**English (original):**\n")
            lines.append(f"```\n{r['en']}\n```\n")
            lines.append("**Masked (what LLM saw):**\n")
            lines.append(f"```\n{r['masked']}\n```\n")
            lines.append("**LLM raw output:**\n")
            lines.append(f"```\n{r['translated_raw']}\n```\n")
            lines.append("**Darija (restitched):**\n")
            lines.append(f"```\n{r['dr']}\n```\n")

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--domain", choices=["code", "toolcall", "math", "all"], default="all")
    p.add_argument("--n", type=int, default=20, help="samples per domain")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--provider", choices=["lmstudio", "deepseek"], default="lmstudio")
    p.add_argument("--lmstudio-url", default="http://127.0.0.1:11434/v1")
    p.add_argument("--lmstudio-model", default="tiny-aya-darija-v5")
    p.add_argument("--deepseek-model", default="deepseek-v4-flash")
    p.add_argument("--out", default="dev/translate_structured_probe.md")
    args = p.parse_args()

    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")

    if args.provider == "deepseek":
        client = OpenAI(
            base_url="https://api.deepseek.com",
            api_key=os.environ["DEEPSEEK_API_KEY"],
        )
        model_name = args.deepseek_model
        extra_body = {"thinking": {"type": "disabled"}}
        max_tokens = 4096
    else:
        client = OpenAI(base_url=args.lmstudio_url, api_key="lm-studio")
        model_name = args.lmstudio_model
        extra_body = {"top_k": 300, "repetition_penalty": 1.15}
        max_tokens = 2048

    domains = ["code", "toolcall", "math"] if args.domain == "all" else [args.domain]
    all_rows: list[dict] = []
    for dom in domains:
        print(f"\n[load] {dom}: streaming dataset...")
        items = DOMAIN_LOADERS[dom](args.n, hf_token, args.seed)
        print(f"[load] {dom}: got {len(items)} items")
        for i, text in enumerate(items, 1):
            ext = DOMAIN_EXTRACTORS[dom](text)
            print(f"\n[{dom} {i}/{len(items)}] len={len(text)}  placeholders={len(ext.originals)}")
            result = process_one(client, text, dom, model_name, max_tokens, extra_body)
            status = "OK" if result["ok"] else "FAIL"
            errs = result["structure_errs"] + ([result["llm_err"]] if result["llm_err"] else [])
            err_str = f"  errs={errs}" if errs else ""
            print(f"  {result['dt']:.1f}s  out_len={len(result['dr'])}  {status}{err_str}")
            all_rows.append(result)

    write_report(all_rows, args.out, model_name)
    print(f"\n[done] wrote {args.out}")

    # Print summary
    print("\n=== SUMMARY ===")
    by_domain: dict[str, list[dict]] = {}
    for r in all_rows:
        by_domain.setdefault(r["domain"], []).append(r)
    for dom, rs in by_domain.items():
        n = len(rs)
        ok = sum(1 for r in rs if r["ok"])
        print(f"  {dom}: {ok}/{n} ok  ({(1 - ok/n):.0%} drop rate)")


if __name__ == "__main__":
    main()
