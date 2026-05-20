"""
Darija toxicity classification SFT task.

Expected translated dataset columns:
- text_darija or text: Moroccan Darija input text
- label or label_text: toxicity label

The assistant target is intentionally one of two English labels:
`toxic` or `non-toxic`. Keeping labels short and stable makes this usable as a
small generative classifier after SFT.
"""

from __future__ import annotations

import math
import os
import random
from pathlib import Path
from typing import Any

from datasets import load_dataset

from tasks.common import Task


NON_TOXIC_LABELS = {"0", "false", "non-toxic", "nontoxic", "not-toxic", "not toxic", "clean"}
TOXIC_LABELS = {"1", "true", "toxic", "toxicity", "unsafe"}


def normalize_label(row: dict[str, Any], label_column: str = "label_text") -> str:
    raw = row.get(label_column)
    if raw is None:
        raw = row.get("label_text")
    if raw is None:
        raw = row.get("label")

    if isinstance(raw, bool):
        return "toxic" if raw else "non-toxic"
    if isinstance(raw, int):
        return "toxic" if raw == 1 else "non-toxic"

    text = str(raw).strip().lower().replace("_", "-")
    if text in TOXIC_LABELS:
        return "toxic"
    if text in NON_TOXIC_LABELS:
        return "non-toxic"
    raise ValueError(f"Unsupported toxicity label: {raw!r}")


def _load_local_or_hf_dataset(dataset: str, split: str):
    token = os.environ.get("HF_TOKEN")
    path = Path(dataset)

    if path.exists():
        if path.is_dir():
            candidates = [
                path / f"{split}.jsonl",
                path / f"{split}.json",
                path / f"{split}.parquet",
                path / "data" / f"{split}.jsonl",
                path / "data" / f"{split}.json",
                path / "data" / f"{split}.parquet",
            ]
            for candidate in candidates:
                if candidate.exists():
                    path = candidate
                    break
            else:
                raise FileNotFoundError(
                    f"No local file for split {split!r} found under {dataset}"
                )

        suffix = path.suffix.lower()
        if suffix in {".jsonl", ".json"}:
            return load_dataset("json", data_files={split: str(path)}, split=split)
        if suffix == ".parquet":
            return load_dataset("parquet", data_files={split: str(path)}, split=split)
        raise ValueError(f"Unsupported local dataset file type: {path}")

    kwargs = {"split": split}
    if token:
        kwargs["token"] = token
    return load_dataset(dataset, **kwargs)


class DarijaToxicClassification(Task):
    def __init__(
        self,
        dataset: str = "Lyte/darija-toxic-conversations-50k",
        split: str = "train",
        text_column: str = "text_darija",
        label_column: str = "label_text",
        balance: bool = False,
        toxic_multiplier: int | None = None,
        max_examples: int = -1,
        seed: int = 42,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.ds = _load_local_or_hf_dataset(dataset, split)
        self.text_column = text_column
        self.label_column = label_column
        self.seed = seed
        self.indices = self._build_indices(balance, toxic_multiplier)
        if max_examples > 0:
            self.indices = self.indices[:max_examples]

    @property
    def eval_type(self):
        return "generative"

    def _build_indices(self, balance: bool, toxic_multiplier: int | None) -> list[int]:
        all_indices = list(range(len(self.ds)))
        if not balance:
            return all_indices

        toxic = []
        non_toxic = []
        for idx in all_indices:
            label = normalize_label(self.ds[idx], self.label_column)
            if label == "toxic":
                toxic.append(idx)
            else:
                non_toxic.append(idx)

        if not toxic or not non_toxic:
            return all_indices

        if toxic_multiplier is None or toxic_multiplier < 1:
            toxic_multiplier = max(1, math.ceil(len(non_toxic) / len(toxic)))

        balanced = non_toxic + toxic * toxic_multiplier
        rng = random.Random(self.seed)
        rng.shuffle(balanced)
        return balanced

    def num_examples(self):
        return len(self.indices)

    def get_example(self, index):
        row = self.ds[self.indices[index]]
        text = (
            row.get(self.text_column)
            or row.get("text_darija")
            or row.get("text")
            or row.get("darija")
        )
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"Missing Darija text at row {self.indices[index]}")
        label = normalize_label(row, self.label_column)
        prompt = (
            "صنف هاد النص واش فيه خطاب سام ولا لا.\n"
            "جاوب غير بواحد من هاد الجوج: toxic أو non-toxic.\n\n"
            f"النص:\n{text.strip()}"
        )
        return {
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": label},
            ]
        }

    def evaluate(self, problem, completion):
        expected = normalize_label(problem, self.label_column)
        pred = str(completion).strip().lower().splitlines()[0]
        pred = pred.replace("_", "-")
        if pred.startswith("non-toxic") or pred.startswith("not toxic") or pred.startswith("clean"):
            pred = "non-toxic"
        elif pred.startswith("toxic"):
            pred = "toxic"
        return pred == expected
