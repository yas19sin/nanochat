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

DEFAULT_PRESETS = [
    ("greedy", 0.0, 0),
    ("tight", 0.25, 20),
    ("balanced", 0.5, 50),
    ("wide", 0.75, 80),
]


def parse_preset(text: str) -> tuple[str, float, int]:
    parts = text.split(":")
    if len(parts) != 3:
        raise argparse.ArgumentTypeError(
            "preset must be name:temperature:top_k, e.g. tight:0.25:20")
    name, temp, top_k = parts
    return name, float(temp), int(top_k)


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
    parser.add_argument("--max-tokens", type=int, default=192)
    parser.add_argument("--num-samples", type=int, default=1)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--prompts-file", default=None,
                        help="optional JSONL with {id,prompt}, or plain one prompt per line")
    parser.add_argument("--preset", action="append", type=parse_preset,
                        help="sampling preset name:temp:top_k; can be repeated")
    parser.add_argument("--out-dir", default=None)
    args = parser.parse_args()

    prompts = load_prompts(args.prompts_file)
    presets = args.preset or DEFAULT_PRESETS

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

            for preset_idx, (preset_name, temperature, top_k) in enumerate(presets):
                seed = args.seed + p_idx * 1000 + preset_idx * 100
                outputs, _masks = engine.generate_batch(
                    prefix,
                    num_samples=args.num_samples,
                    max_tokens=args.max_tokens,
                    temperature=temperature,
                    top_k=top_k,
                    seed=seed,
                )
                for sample_idx, tokens in enumerate(outputs):
                    response = clean_response(tokenizer.decode(tokens[prefix_len:]))
                    row = {
                        "prompt_id": prompt_id,
                        "prompt": prompt,
                        "preset": preset_name,
                        "temperature": temperature,
                        "top_k": top_k,
                        "sample_idx": sample_idx,
                        "seed": seed,
                        "response": response,
                    }
                    rows.append(row)
                    jf.write(json.dumps(row, ensure_ascii=False) + "\n")
                    print(
                        f"\n[{prompt_id} | {preset_name} | t={temperature} k={top_k}]\n{response}")

    with open(md_path, "w", encoding="utf-8") as mf:
        mf.write(f"# Darija Chat Probe: {args.source}/{args.model_tag}@{args.step}\n\n")
        for item in prompts:
            mf.write(f"## {item['id']}\n\n")
            mf.write(f"**Prompt:** {item['prompt']}\n\n")
            for row in [r for r in rows if r["prompt_id"] == item["id"]]:
                mf.write(
                    f"### {row['preset']} (t={row['temperature']}, k={row['top_k']}, sample={row['sample_idx']})\n\n")
                mf.write(row["response"].strip() + "\n\n")

    print(f"\nWrote {jsonl_path}")
    print(f"Wrote {md_path}")


if __name__ == "__main__":
    main()
