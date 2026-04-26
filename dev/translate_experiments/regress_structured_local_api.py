#!/usr/bin/env python3
"""Regression-test structured translation via a local OpenAI-compatible API.

This exercises the production planners from translate_structured_vllm.py while
calling a locally loaded model through LM Studio's /v1/chat/completions API.

Example:
  python dev/translate_experiments/regress_structured_local_api.py \
      --base-url http://localhost:11434/v1 \
      --model tiny-aya-darija-v5 \
      --n-code 20 --n-toolcall 20 --n-math 20
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from dataclasses import dataclass, field

from openai import OpenAI

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from translate_structured import (  # noqa: E402
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_STRICT,
    SYSTEM_PROMPT_PROSE_ONLY,
    load_code_items,
    load_toolcall_items,
    load_math_items,
    restitch,
    verify_structures,
)
from translate_structured_vllm import (  # noqa: E402
    DOMAIN_PLANNERS,
    Job,
    SamplePlan,
    _clean_prose_output,
    _completeness_errors,
    _repetition_errors,
    _verify_unfenced_code_spans,
    assemble_sample,
)


@dataclass
class DomainStats:
    total: int = 0
    ok: int = 0
    failed: int = 0
    seconds: float = 0.0
    errors: list[str] = field(default_factory=list)


def system_prompt_for(kind: str) -> str:
    return {
        "default": SYSTEM_PROMPT,
        "strict": SYSTEM_PROMPT_STRICT,
        "prose_only": SYSTEM_PROMPT_PROSE_ONLY,
    }[kind]


def load_sample_pool(domain: str, pool_size: int, hf_token: str | None) -> list[str]:
    loader = {
        "code": load_code_items,
        "toolcall": load_toolcall_items,
        "math": load_math_items,
    }[domain]
    return loader(pool_size, hf_token, 0)


def run_job(
    client: OpenAI,
    model: str,
    job: Job,
    *,
    kind: str,
    max_tokens: int,
) -> None:
    prompt_kind = job.prompt_kind if kind == "default" else "strict"
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt_for(prompt_kind)},
            {"role": "user", "content": job.masked},
        ],
        temperature=0.3,
        top_p=0.98,
        max_tokens=max_tokens,
        extra_body={
            "top_k": 300,
            "repetition_penalty": 1.15,
        },
    )
    text = (resp.choices[0].message.content or "").strip()
    text = text.replace("<|END_RESPONSE|>", "").strip()
    if job.prompt_kind == "prose_only" or job.domain == "code":
        text = _clean_prose_output(text)

    if job.originals:
        restitched, errs = restitch(text, job.originals)
    else:
        restitched, errs = text, []

    all_errs = errs + verify_structures(job.original_text, restitched, job.domain)
    all_errs += _completeness_errors(job, restitched)
    all_errs += _repetition_errors(job, restitched)

    if kind == "default" or len(all_errs) < len(job.errors):
        job.result = restitched
        job.errors = all_errs

    job.retry_needed = bool(all_errs)


def process_sample(
    client: OpenAI,
    model: str,
    domain: str,
    text: str,
    sample_id: int,
    max_tokens: int,
) -> SamplePlan:
    sp = DOMAIN_PLANNERS[domain](sample_id, text)
    for job in sp.jobs:
        run_job(client, model, job, kind="default", max_tokens=max_tokens)
    for job in [j for j in sp.jobs if j.retry_needed]:
        run_job(client, model, job, kind="strict", max_tokens=max_tokens)

    assemble_sample(sp)
    if domain == "code" and sp.ok:
        errs = _verify_unfenced_code_spans(sp.en, sp.dr)
        if errs:
            sp.structure_errs.extend(errs)
            sp.ok = False
    return sp


def run_domain(
    client: OpenAI,
    model: str,
    domain: str,
    n: int,
    *,
    pool_size: int,
    seed: int,
    hf_token: str | None,
    max_tokens: int,
) -> DomainStats:
    rng = random.Random(seed)
    pool = load_sample_pool(domain, max(pool_size, n), hf_token)
    if len(pool) > n:
        items = rng.sample(pool, n)
    else:
        items = pool

    stats = DomainStats(total=len(items))
    t0 = time.time()
    for i, text in enumerate(items, 1):
        st = time.time()
        try:
            sp = process_sample(client, model, domain, text, i, max_tokens)
        except Exception as exc:  # noqa: BLE001
            sp = SamplePlan(sample_id=i, en=text, domain=domain)
            sp.ok = False
            sp.structure_errs = [f"{type(exc).__name__}: {exc}"]
        dt = time.time() - st
        if sp.ok:
            stats.ok += 1
            status = "ok"
        else:
            stats.failed += 1
            status = "FAIL"
            if len(stats.errors) < 8:
                stats.errors.append(
                    f"sample={i} src_len={len(text)} dr_len={len(sp.dr)} "
                    f"errors={sp.structure_errs[:3]}"
                )
        print(f"  [{domain} {i:>3}/{len(items)}] {status} {dt:.1f}s")
    stats.seconds = time.time() - t0
    return stats


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", default="http://localhost:11434/v1")
    p.add_argument("--model", default="tiny-aya-darija-v5")
    p.add_argument("--api-key", default="lm-studio")
    p.add_argument("--n-code", type=int, default=12)
    p.add_argument("--n-toolcall", type=int, default=12)
    p.add_argument("--n-math", type=int, default=12)
    p.add_argument("--pool-size", type=int, default=200)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--max-tokens", type=int, default=1024)
    args = p.parse_args()

    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
    client = OpenAI(base_url=args.base_url, api_key=args.api_key)

    targets = {
        "code": args.n_code,
        "toolcall": args.n_toolcall,
        "math": args.n_math,
    }
    all_stats: dict[str, DomainStats] = {}
    for domain, n in targets.items():
        if n <= 0:
            continue
        print(f"\n[{domain}] n={n} pool={args.pool_size}")
        all_stats[domain] = run_domain(
            client,
            args.model,
            domain,
            n,
            pool_size=args.pool_size,
            seed=args.seed,
            hf_token=hf_token,
            max_tokens=args.max_tokens,
        )

    print("\nsummary")
    for domain, stats in all_stats.items():
        rate = stats.ok / max(stats.total, 1)
        print(
            f"{domain}: {stats.ok}/{stats.total} ok "
            f"({rate:.1%}), failed={stats.failed}, elapsed={stats.seconds/60:.1f}m"
        )
        for err in stats.errors:
            print(f"  - {err}")


if __name__ == "__main__":
    main()
