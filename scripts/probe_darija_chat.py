"""
Probe a chat checkpoint on a varied Darija prompt suite.

Example:
python -m scripts.probe_darija_chat -i sft -g d6_target12 -s 10000
"""

import argparse
import json
from pathlib import Path

from nanochat.checkpoint_manager import load_model
from nanochat.common import autodetect_device_type, compute_init, get_base_dir
from nanochat.engine import Engine


DEFAULT_PROMPTS = [
    {
        "id": "programming_advice",
        "prompt": "جاوبني بالدارجة: شنو هي أحسن طريقة نتعلم بها البرمجة؟",
    },
    {
        "id": "python_plan",
        "prompt": "بغيت نتعلم Python من الصفر. عطيني خطة ديال 4 أسابيع، بالدارجة وبلا هضرة زايدة.",
    },
    {
        "id": "explain_simple",
        "prompt": "شرح ليا بالدارجة وبطريقة بسيطة: شنو هو الذكاء الاصطناعي؟",
    },
    {
        "id": "math_word_problem",
        "prompt": "جاوب بالدارجة: إلا كان عندي 120 درهم وصرفت 35 درهم ومن بعد زدت 50 درهم، شحال بقى عندي؟",
    },
    {
        "id": "practical_steps",
        "prompt": "عندي امتحان غدا وما واجدتش مزيان. عطيني خطة واقعية لليلة وحدة.",
    },
    {
        "id": "summarize",
        "prompt": "لخص هاد الفكرة بالدارجة ف جوج جمل: القراءة كل نهار كتعاون الإنسان يوسع المفردات ديالو ويحسن التركيز.",
    },
    {
        "id": "rewrite",
        "prompt": "عاود كتب هاد الجملة بدارجة طبيعية: يجب عليك مراجعة الدروس بانتظام لكي تحقق نتائج جيدة.",
    },
    {
        "id": "translate_to_darija",
        "prompt": "ترجم للدارجة: I need to update my computer before installing the new program.",
    },
    {
        "id": "debug_reasoning",
        "prompt": "علاش هاد الكود ما خدامش؟ print('salam' عطيني الجواب بالدارجة وباختصار.",
    },
    {
        "id": "compare",
        "prompt": "قارن بالدارجة بين تعلم البرمجة من يوتيوب وتعلمها من كتاب. عطيني المزايا والعيوب.",
    },
    {
        "id": "refusal_safe",
        "prompt": "شي واحد طلب مني نعطيه كلمة السر ديالي. شنو ندير؟ جاوب بالدارجة.",
    },
    {
        "id": "long_form",
        "prompt": "كتب ليا جواب منظم بالدارجة على: كيفاش نبني عادة القراءة؟ استعمل نقاط قصيرة.",
    },
]

GRID_TEMPS = [0.0, 0.1, 0.3, 0.6]
GRID_TOP_PS = [0.95, 0.99, 0.9, 0.85]
GRID_TOP_KS = [300, 200, 150, 100, 50]

DEFAULT_PRESETS = [
    ("greedy_p95_k200", 0.0, 0.95, 200),
    ("very_tight", 0.1, 0.9, 100),
    ("tight", 0.3, 0.9, 150),
    ("balanced", 0.3, 0.95, 200),
    ("wide_07b_style", 0.6, 0.85, 200),
]


def parse_preset(text: str) -> tuple[str, float, float, int]:
    parts = text.split(":")
    if len(parts) == 3:
        name, temp, top_k = parts
        return name, float(temp), 1.0, int(top_k)
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "preset must be name:temperature:top_p:top_k, e.g. tight:0.3:0.9:150")
    name, temp, top_p, top_k = parts
    return name, float(temp), float(top_p), int(top_k)


def full_sampling_grid() -> list[tuple[str, float, float, int]]:
    presets = []
    for temp in GRID_TEMPS:
        for top_p in GRID_TOP_PS:
            for top_k in GRID_TOP_KS:
                name = f"t{temp:g}_p{top_p:g}_k{top_k}"
                presets.append((name, temp, top_p, top_k))
    return presets


def load_prompts(path: str | None) -> list[dict[str, str]]:
    if path is None:
        return DEFAULT_PROMPTS
    prompts = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                prompt = obj["prompt"]
                prompt_id = obj.get("id", f"prompt_{i:04d}")
            except json.JSONDecodeError:
                prompt = line
                prompt_id = f"prompt_{i:04d}"
            prompts.append({"id": prompt_id, "prompt": prompt})
    return prompts


def render_prompt(tokenizer, prompt: str) -> list[int]:
    bos = tokenizer.get_bos_token_id()
    user_start = tokenizer.encode_special("<|user_start|>")
    user_end = tokenizer.encode_special("<|user_end|>")
    assistant_start = tokenizer.encode_special("<|assistant_start|>")
    return [
        bos,
        user_start,
        *tokenizer.encode(prompt),
        user_end,
        assistant_start,
    ]


def clean_response(text: str) -> str:
    for special in [
        "<|assistant_end|>",
        "<|assistant_start|>",
        "<|user_start|>",
        "<|user_end|>",
        "<|bos|>",
    ]:
        text = text.replace(special, "")
    return text.strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe a Darija chat checkpoint with varied prompts and sampling settings.")
    parser.add_argument("-i", "--source", default="sft",
                        help="checkpoint source: sft|rl|base")
    parser.add_argument("-g", "--model-tag", required=True)
    parser.add_argument("-s", "--step", type=int, required=True)
    parser.add_argument("--device-type", default="",
                        choices=["", "cuda", "cpu", "mps"])
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--num-samples", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--repetition-penalty", type=float, default=1.1)
    parser.add_argument("--prompts-file", default=None,
                        help="optional JSONL with {id,prompt}, or plain one prompt per line")
    parser.add_argument("--preset", action="append", type=parse_preset,
                        help="sampling preset name:temp:top_p:top_k; can be repeated")
    parser.add_argument("--full-grid", action="store_true",
                        help="run all temp x top_p x top_k combinations requested for sampling search")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    prompts = load_prompts(args.prompts_file)
    presets = args.preset or (full_sampling_grid() if args.full_grid else DEFAULT_PRESETS)

    device_type = autodetect_device_type() if args.device_type == "" else args.device_type
    _ddp, _rank, _local_rank, _world_size, device = compute_init(device_type)
    model, tokenizer, _meta = load_model(
        args.source, device, phase="eval", model_tag=args.model_tag, step=args.step)
    engine = Engine(model, tokenizer)

    out_dir = Path(args.out_dir) if args.out_dir else (
        Path(get_base_dir()) / "chat_probes" / f"{args.source}_{args.model_tag}_{args.step:06d}")
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / "probe_outputs.jsonl"
    md_path = out_dir / "probe_outputs.md"

    rows = []
    with open(jsonl_path, "w", encoding="utf-8") as jf:
        for p_idx, item in enumerate(prompts):
            prompt_id = item["id"]
            prompt = item["prompt"]
            prefix = render_prompt(tokenizer, prompt)
            prefix_len = len(prefix)

            for preset_idx, (preset_name, temperature, top_p, top_k) in enumerate(presets):
                seed = args.seed + p_idx * 1000 + preset_idx * 100
                outputs, _masks = engine.generate_batch(
                    prefix,
                    num_samples=args.num_samples,
                    max_tokens=args.max_tokens,
                    temperature=temperature,
                    top_p=top_p,
                    top_k=top_k,
                    repetition_penalty=args.repetition_penalty,
                    seed=seed,
                )
                for sample_idx, tokens in enumerate(outputs):
                    response = clean_response(tokenizer.decode(tokens[prefix_len:]))
                    row = {
                        "prompt_id": prompt_id,
                        "prompt": prompt,
                        "preset": preset_name,
                        "temperature": temperature,
                        "top_p": top_p,
                        "top_k": top_k,
                        "repetition_penalty": args.repetition_penalty,
                        "sample_idx": sample_idx,
                        "seed": seed,
                        "response": response,
                    }
                    rows.append(row)
                    jf.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(
                        f"\n[{prompt_id} | {preset_name} | t={temperature} p={top_p} k={top_k} rp={args.repetition_penalty}]\n{response}")

    with open(md_path, "w", encoding="utf-8") as mf:
        mf.write(f"# Darija Chat Probe: {args.source}/{args.model_tag}@{args.step}\n\n")
        for item in prompts:
            mf.write(f"## {item['id']}\n\n")
            mf.write(f"**Prompt:** {item['prompt']}\n\n")
            for row in [r for r in rows if r["prompt_id"] == item["id"]]:
                mf.write(
                    f"### {row['preset']} (t={row['temperature']}, p={row['top_p']}, k={row['top_k']}, rp={row['repetition_penalty']}, sample={row['sample_idx']})\n\n")
                mf.write(row["response"].strip() + "\n\n")

    print(f"\nWrote {jsonl_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
