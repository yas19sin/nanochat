"""Verify that Hermes-FC tool-call data is compatible with real model output.

Loads samples from NousResearch/hermes-function-calling-v1, extracts:
  - tools list from <tools>[...]</tools> in the system prompt
  - user query from the HUMAN turn
  - ground-truth tool calls from the ASSISTANT turn's <tool_call> tags

Sends (system_prompt, user_query, tools) to lfm2.5-350m via standard OpenAI
tool-calling API. Compares model's tool_calls with ground-truth.

Goal: confirm the Hermes-FC dataset represents the SAME schema lfm2.5 (and all
modern tool-calling LLMs) actually use. If they line up, training nanochat on
Hermes-FC will teach it the correct schema.
"""

from __future__ import annotations

import json
import os
import re

from datasets import load_dataset
from openai import OpenAI


TOOLS_RE = re.compile(r"<tools>\s*(\[[\s\S]*?\])\s*</tools>")
TOOL_CALL_RE = re.compile(r"<tool_call>\s*(\{[\s\S]*?\})\s*</tool_call>")


def extract_tools(system_text: str) -> list[dict] | None:
    m = TOOLS_RE.search(system_text)
    if not m:
        return None
    try:
        raw = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    # Normalize to OpenAI tool format
    out = []
    for t in raw:
        if "function" in t:
            out.append({"type": "function", "function": t["function"]})
        elif "name" in t:
            out.append({"type": "function", "function": t})
    return out


def extract_tool_calls(assistant_text: str) -> list[dict]:
    out = []
    for m in TOOL_CALL_RE.finditer(assistant_text):
        try:
            out.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            pass
    return out


def compare_calls(gt: list[dict], got: list[dict]) -> dict:
    """Compare ground-truth and model tool_calls by function name."""
    gt_names = sorted([c.get("name", "") for c in gt])
    got_names = sorted([c.get("function", {}).get("name", "") for c in got])
    # Arguments: compare by name
    gt_args = {c.get("name"): c.get("arguments", {}) for c in gt}
    got_args = {}
    for c in got:
        name = c.get("function", {}).get("name", "")
        raw_args = c.get("function", {}).get("arguments", "{}")
        try:
            got_args[name] = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            got_args[name] = raw_args

    same_names = gt_names == got_names
    arg_match = {}
    for name in set(gt_names) | set(got_names):
        arg_match[name] = gt_args.get(name) == got_args.get(name)

    return {
        "same_names": same_names,
        "gt_names": gt_names,
        "got_names": got_names,
        "arg_match": arg_match,
    }


def main() -> None:
    hf_token = os.environ.get("HF_TOKEN")
    client = OpenAI(base_url="http://127.0.0.1:11434/v1", api_key="lm-studio")

    ds = load_dataset(
        "NousResearch/hermes-function-calling-v1",
        split="train",
        streaming=True,
        token=hf_token,
    )

    samples = []
    for row in ds:
        convs = row.get("conversations") or []
        if not convs:
            continue
        sys_text = next((t["value"] for t in convs if t.get("from") == "system"), "")
        human_text = next((t["value"] for t in convs if t.get("from") == "human"), "")
        gpt_text = next((t["value"] for t in convs if t.get("from") == "gpt"), "")
        if not (sys_text and human_text and gpt_text):
            continue
        if "<tool_call>" not in gpt_text:
            continue
        tools = extract_tools(sys_text)
        if not tools:
            continue
        gt_calls = extract_tool_calls(gpt_text)
        if not gt_calls:
            continue
        samples.append({
            "human": human_text,
            "tools": tools,
            "gt_calls": gt_calls,
        })
        if len(samples) >= 10:
            break

    print(f"Loaded {len(samples)} Hermes-FC samples with valid tools + calls\n")
    print("=" * 70)
    print("Testing lfm2.5-350m against Hermes-FC ground-truth tool calls")
    print("=" * 70)

    results = []
    for i, s in enumerate(samples, 1):
        print(f"\n--- sample {i} ---")
        print(f"user: {s['human'][:150]}...")
        print(f"tools available: {[t['function']['name'] for t in s['tools']]}")
        print(f"ground-truth calls: {[c.get('name') for c in s['gt_calls']]}")
        try:
            resp = client.chat.completions.create(
                model="lfm2.5-350m",
                messages=[
                    {"role": "system", "content": "You are a helpful assistant with access to tools."},
                    {"role": "user", "content": s["human"]},
                ],
                tools=s["tools"],
                tool_choice="auto",
                temperature=0.2,
                max_tokens=512,
            )
            msg = resp.choices[0].message
            got_calls = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    got_calls.append({
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    })
            print(f"model emitted calls: {[c['function']['name'] for c in got_calls]}")
            cmp = compare_calls(s["gt_calls"], got_calls)
            print(f"names match: {cmp['same_names']}")
            if not cmp["same_names"]:
                print(f"  gt  = {cmp['gt_names']}")
                print(f"  got = {cmp['got_names']}")
            for name, ok in cmp["arg_match"].items():
                if not ok:
                    print(f"  args mismatch for {name}: gt={_get_args(s['gt_calls'], name)} "
                          f"got={_get_got_args(got_calls, name)}")
            results.append({
                "names_match": cmp["same_names"],
                "all_args_match": all(cmp["arg_match"].values()),
            })
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: {exc}")
            results.append({"names_match": False, "all_args_match": False})

    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    n = len(results)
    names_ok = sum(1 for r in results if r["names_match"])
    args_ok = sum(1 for r in results if r["names_match"] and r["all_args_match"])
    print(f"function-name match:     {names_ok}/{n}")
    print(f"full (name + args) match: {args_ok}/{n}")


def _get_args(calls, name):
    for c in calls:
        if c.get("name") == name:
            return c.get("arguments")
    return None


def _get_got_args(calls, name):
    for c in calls:
        if c["function"]["name"] == name:
            return c["function"]["arguments"]
    return None


if __name__ == "__main__":
    main()
