"""
Probe whether our local Q8 Darija translator can handle math / code / tool-call data.

Samples N rows from each domain, translates via LM Studio (OpenAI-compatible API),
and writes a markdown report for eyeball review. Each domain uses a prompt that
instructs the model to PRESERVE math symbols, code, and JSON verbatim while
translating the surrounding natural-language prose.

Datasets (streamed):
  - math:      HuggingFaceTB/finemath  (finemath-4plus)   -> `text`
  - code:      m-a-p/CodeFeedback-Filtered-Instruction    -> `query` + ``` blocks
  - toolcall:  Salesforce/xlam-function-calling-60k       -> `query` + `tools` + `answers`

Usage:
  $env:HF_TOKEN = "..."
  .\\.venv\\Scripts\\python.exe dev/probe_domain_translate.py --n 10
"""

import argparse
import json
import os
import random
import time

from datasets import load_dataset
from openai import OpenAI

MAX_INPUT_CHARS = 2000

# Per-domain system prompts. Core rule: translate natural-language prose,
# keep everything else EXACTLY as-is (LaTeX, code, JSON, numbers, identifiers).
SYSTEM_PROMPTS = {
    "math": (
        "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
        "CRITICAL RULES:\n"
        "1. Preserve ALL math symbols, equations, LaTeX ($...$, \\\\(...\\\\), \\\\[...\\\\]), and numbers EXACTLY.\n"
        "2. Only translate the natural-language prose around the math.\n"
        "3. Do not solve or alter the math. Do not re-order terms.\n"
        "Output ONLY the translated text."
    ),
    "code": (
        "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
        "CRITICAL RULES:\n"
        "1. Preserve ALL code blocks (``` fenced and `inline`) EXACTLY — do not translate code, "
        "variable names, function names, or identifiers.\n"
        "2. Only translate the natural-language prose around the code.\n"
        "3. Comments INSIDE code blocks: leave them in English (do not translate).\n"
        "Output ONLY the translated text."
    ),
    "toolcall": (
        "Translate the following English text into Moroccan Darija (الدارجة المغربية). "
        "CRITICAL RULES:\n"
        "1. Preserve ALL JSON blocks, function names, parameter names, and string VALUES that look like identifiers or URLs EXACTLY.\n"
        "2. Only translate natural-language prose (the user's query, descriptions).\n"
        "3. Do not reformat JSON. Keep quotes, braces, and commas exactly.\n"
        "Output ONLY the translated text."
    ),
}


def reservoir_sample(stream, k, rng, reservoir_size):
    pool = []
    for i, row in enumerate(stream):
        if i >= reservoir_size:
            break
        pool.append(row)
    rng.shuffle(pool)
    return pool[:k]


# ---------------------------------------------------------------- builders
def build_math_sample(row):
    text = (row.get("text") or "").strip()
    return text if text else None


def build_code_sample(row):
    # m-a-p/CodeFeedback-Filtered-Instruction has `query` (english instruction
    # with code context/snippets) and `answer` (code solution).
    query = (row.get("query") or "").strip()
    # Keep the user-visible instruction (most interesting to test translation on)
    return query if query else None


def build_toolcall_sample(row):
    # glaive-function-calling-v2 schema: `system` (tool schema + sys prompt),
    # `chat` (conversation transcript with USER:/ASSISTANT:/FUNCTION RESPONSE:).
    system = (row.get("system") or "").strip()
    chat = (row.get("chat") or "").strip()
    if not chat:
        return None
    block = f"{system}\n\n---\n\n{chat}" if system else chat
    return block


SOURCES = {
    "math": {
        "id": "HuggingFaceTB/finemath",
        "config": "finemath-4plus",
        "split": "train",
        "build": build_math_sample,
        "reservoir": 300,
    },
    "code": {
        "id": "m-a-p/CodeFeedback-Filtered-Instruction",
        "config": None,
        "split": "train",
        "build": build_code_sample,
        "reservoir": 300,
    },
    "toolcall": {
        "id": "glaiveai/glaive-function-calling-v2",
        "config": None,
        "split": "train",
        "build": build_toolcall_sample,
        "reservoir": 300,
    },
}


def collect_samples(domain, n, rng, hf_token):
    s = SOURCES[domain]
    print(f"[data] {domain}: streaming {s['id']}...")
    kw = dict(split=s["split"], streaming=True, token=hf_token)
    if s["config"]:
        kw["name"] = s["config"]
    ds = load_dataset(s["id"], **kw)
    pool = reservoir_sample(ds, n * 3, rng, s["reservoir"])
    out = []
    for row in pool:
        built = s["build"](row)
        if built and 100 <= len(built) <= MAX_INPUT_CHARS * 6:
            out.append(built[:MAX_INPUT_CHARS])
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------- translate
def translate_lmstudio(client, text, system_prompt, model_name, extra_body=None, max_tokens=1024):
    t0 = time.time()
    try:
        kwargs = dict(
            model=model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            temperature=0.3,
            top_p=0.98,
            max_tokens=max_tokens,
        )
        if extra_body:
            kwargs["extra_body"] = extra_body
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content.strip(), time.time() - t0, None
    except Exception as exc:  # noqa: BLE001
        return "", time.time() - t0, str(exc)


# ---------------------------------------------------------------- main
def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=10, help="samples per domain")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--provider", choices=["lmstudio", "deepseek"], default="lmstudio")
    p.add_argument("--lmstudio-url", default="http://127.0.0.1:1234/v1")
    p.add_argument("--lmstudio-model", default="tiny-aya-darija-v5")
    p.add_argument("--deepseek-model", default="deepseek-v4-flash",
                   help="deepseek-v4-flash or deepseek-v4-pro")
    p.add_argument("--domains", nargs="+",
                   default=["math", "code", "toolcall"])
    p.add_argument("--out", default="dev/domain_translate_probe.md")
    args = p.parse_args()

    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    rng = random.Random(args.seed)

    if args.provider == "deepseek":
        ds_key = os.environ.get("DEEPSEEK_API_KEY")
        if not ds_key:
            raise SystemExit("set DEEPSEEK_API_KEY for --provider deepseek")
        lm = OpenAI(base_url="https://api.deepseek.com", api_key=ds_key)
        model_name = args.deepseek_model
        # Disable thinking — this is a translation task, not reasoning. Saves
        # massive output-token budget (thinking tokens count toward max_tokens).
        extra_body = {"thinking": {"type": "disabled"}}
        max_tokens = 4096
    else:
        lm = OpenAI(base_url=args.lmstudio_url, api_key="lm-studio")
        model_name = args.lmstudio_model
        extra_body = {"top_k": 300, "repetition_penalty": 1.15}
        max_tokens = 1024

    all_rows = []
    for domain in args.domains:
        samples = collect_samples(domain, args.n, rng, hf_token)
        print(f"[data] {domain}: got {len(samples)} samples")
        sys_prompt = SYSTEM_PROMPTS[domain]
        for i, text in enumerate(samples, 1):
            print(f"\n[{domain} {i}/{len(samples)}] len={len(text)}")
            out, dt, err = translate_lmstudio(lm, text, sys_prompt, model_name,
                                              extra_body=extra_body, max_tokens=max_tokens)
            print(f"  {dt:.1f}s  out_len={len(out)}" + (f"  ERR={err}" if err else ""))
            all_rows.append({
                "domain": domain, "idx": i,
                "en": text, "dr": out, "dt": dt, "err": err,
            })

    # ---- write markdown ----
    out_path = args.out
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    lines = []
    lines.append("# Domain translation probe — math / code / tool-call\n")
    lines.append(f"Seed: {args.seed} | Samples/domain: {args.n} | "
                 f"Provider: `{args.provider}` | Model: `{model_name}`\n")
    lines.append("\nGoal: check whether our Q8 Aya-Darija translator "
                 "preserves math / code / JSON structure while translating surrounding prose. "
                 "If structure survives, we can translate these domains into Darija for "
                 "pretraining. If not, keep them in English (still useful via cross-lingual transfer).\n")

    for domain in args.domains:
        rows = [r for r in all_rows if r["domain"] == domain]
        if not rows:
            continue
        avg_dt = sum(r["dt"] for r in rows) / len(rows)
        lines.append(f"\n---\n\n# {domain.upper()}  ({len(rows)} samples, avg {avg_dt:.1f}s)\n")
        for r in rows:
            lines.append(f"\n## {domain}-{r['idx']}  (en {len(r['en'])} chars, {r['dt']:.1f}s)\n")
            lines.append(f"**English:**\n\n```\n{r['en']}\n```\n")
            lines.append(f"\n**Darija:**\n\n```\n{r['dr'] or r['err']}\n```\n")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n[done] wrote {out_path}")


if __name__ == "__main__":
    main()
