"""
Darija SFT datasets from HuggingFace.

- Lyte/Moroccan-Darija-Instruct-573K: column `messages`
- Lyte/Moroccan-Darija-Instruct-573K-English: paired Darija/English Q/A columns
- GemMaroc/TULU-3-50k-darija-english: column `dialogs`
- Lyte/darija-structured-translated: column `dr`, Markdown Q/A format
"""

import os
import re

from datasets import load_dataset

from tasks.common import Task


QA_RE = re.compile(
    r"^\s*###\s*Question\s*(?P<question>.*?)\s*###\s*Answer\s*(?P<answer>.*)\s*$",
    re.DOTALL | re.IGNORECASE,
)
SPLIT_SLICE_RE = re.compile(r"^(?P<base>[^\[]+)\[(?P<slice>[^\]]+)\]$")


def parse_structured_qa(text):
    match = QA_RE.match((text or "").strip())
    if not match:
        return None
    question = match.group("question").strip()
    answer = match.group("answer").strip()
    if not question or not answer:
        return None
    return question, answer


def parse_split_after_filter(split):
    """Return (base_split, slice_spec) for HF-like split slices.

    The structured dataset is sorted, and some tail slices contain no Q/A rows.
    For this task we therefore load the base split first, filter parseable Q/A
    rows, then apply the slice over that filtered index list.
    """
    match = SPLIT_SLICE_RE.match(split)
    if not match:
        return split, None
    return match.group("base").strip(), match.group("slice").strip()


def _slice_bound(value, n_items, default):
    value = value.strip()
    if not value:
        return default
    if value.endswith("%"):
        pct = float(value[:-1]) / 100.0
        return int(n_items * pct)
    return int(value)


def apply_filtered_slice(indices, slice_spec):
    if slice_spec is None:
        return indices
    n_items = len(indices)
    if ":" in slice_spec:
        start_s, stop_s = slice_spec.split(":", 1)
        start = _slice_bound(start_s, n_items, 0)
        stop = _slice_bound(stop_s, n_items, n_items)
        start = max(0, min(start, n_items))
        stop = max(start, min(stop, n_items))
        return indices[start:stop]

    pos = _slice_bound(slice_spec, n_items, 0)
    if pos < 0:
        pos += n_items
    if pos < 0 or pos >= n_items:
        return []
    return [indices[pos]]


class HFConversationTask(Task):
    def __init__(self, dataset_name, split, column_name, **kwargs):
        super().__init__(**kwargs)
        try:
            self.ds = load_dataset(dataset_name, split=split)
        except Exception as e:
            raise RuntimeError(
                f"Failed to load dataset {dataset_name} with split {split}: {e}"
            ) from e
        self.column_name = column_name
        self.length = len(self.ds)

    @property
    def eval_type(self):
        return "generative"

    def num_examples(self):
        return self.length

    def get_example(self, index):
        row = self.ds[index]
        messages = row[self.column_name]
        assert isinstance(
            messages, list), f"Expected list of messages, got {type(messages)}"
        assert len(
            messages) >= 2, f"Conversation must have at least 2 messages, got {len(messages)}"

        if messages[0]["role"] == "system":
            rest_messages = messages[1:]
        else:
            rest_messages = messages

        assert len(
            rest_messages) >= 2, "Conversation must have at least one user/assistant turn"
        for i, message in enumerate(rest_messages):
            assert "role" in message, f"Message {i} missing role"
            assert "content" in message, f"Message {i} missing content"
            expected_role = "user" if i % 2 == 0 else "assistant"
            assert message["role"] == expected_role, (
                f"Message {i} has role {message['role']} but should be {expected_role}"
            )
            assert isinstance(
                message["content"], str), f"Message {i} content must be a string"

        return {"messages": messages}


class MoroccanDarijaInstruct573K(HFConversationTask):
    def __init__(self, split, **kwargs):
        super().__init__(
            dataset_name="Lyte/Moroccan-Darija-Instruct-573K",
            split=split,
            column_name="messages",
            **kwargs,
        )


class MoroccanDarijaInstruct573KEnglish(Task):
    MODES = {
        "qa": {
            "source_column": "english_question",
            "target_column": "darija_answer",
            "prompt_template": "Answer in Moroccan Darija. جاوب بالدارجة المغربية:\n{english_question}",
        },
        "translate_answer": {
            "source_column": "english_answer",
            "target_column": "darija_answer",
            "prompt_template": "Translate this to Moroccan Darija. ترجم للدارجة المغربية:\n{english_answer}",
        },
        "translate_question": {
            "source_column": "english_question",
            "target_column": "darija_question",
            "prompt_template": "Translate this question to Moroccan Darija. ترجم هاد السؤال للدارجة المغربية:\n{english_question}",
        },
    }

    def __init__(
        self,
        split,
        dataset_name="Lyte/Moroccan-Darija-Instruct-573K-English",
        mode="qa",
        prompt_template=None,
        max_examples=-1,
        **kwargs,
    ):
        super().__init__(**kwargs)
        if mode not in self.MODES:
            raise ValueError(
                f"Unknown English Darija mode {mode!r}; expected one of {sorted(self.MODES)}"
            )
        token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_HUB_TOKEN")
        load_kwargs = {"split": split}
        if token:
            load_kwargs["token"] = token
        try:
            self.ds = load_dataset(dataset_name, **load_kwargs)
        except Exception as e:
            raise RuntimeError(
                f"Failed to load dataset {dataset_name} with split {split}: {e}"
            ) from e

        self.mode = mode
        self.source_column = self.MODES[mode]["source_column"]
        self.target_column = self.MODES[mode]["target_column"]
        self.prompt_template = prompt_template or self.MODES[mode]["prompt_template"]
        self.valid_indices = []
        for idx, row in enumerate(self.ds):
            source = row.get(self.source_column)
            target = row.get(self.target_column)
            if isinstance(source, str) and source.strip() and isinstance(target, str) and target.strip():
                self.valid_indices.append(idx)
                if max_examples > 0 and len(self.valid_indices) >= max_examples:
                    break

        if not self.valid_indices:
            raise RuntimeError(
                f"No usable rows found in {dataset_name} split {split!r} for mode {mode!r} "
                f"with source {self.source_column!r} and target {self.target_column!r}"
            )

    @property
    def eval_type(self):
        return "generative"

    def num_examples(self):
        return len(self.valid_indices)

    def get_example(self, index):
        row = dict(self.ds[self.valid_indices[index]])
        row = {key: value.strip() if isinstance(value, str) else value for key, value in row.items()}
        try:
            prompt = self.prompt_template.format(**row)
        except KeyError as e:
            raise KeyError(
                f"Prompt template references missing column {e} for English Darija mode {self.mode!r}"
            ) from e
        return {
            "messages": [
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": row[self.target_column]},
            ]
        }


class TuluDarijaEnglish(HFConversationTask):
    def __init__(self, split, **kwargs):
        super().__init__(
            dataset_name="GemMaroc/TULU-3-50k-darija-english",
            split=split,
            column_name="dialogs",
            **kwargs,
        )


class DarijaStructuredTranslated(Task):
    def __init__(
        self,
        split,
        dataset_name="Lyte/darija-structured-translated",
        column_name="dr",
        **kwargs,
    ):
        super().__init__(**kwargs)
        base_split, slice_spec = parse_split_after_filter(split)
        load_kwargs = {"split": base_split}
        token = os.environ.get("HF_TOKEN")
        if token:
            load_kwargs["token"] = token
        try:
            self.ds = load_dataset(dataset_name, **load_kwargs)
        except Exception as e:
            raise RuntimeError(
                f"Failed to load dataset {dataset_name} with split {split}: {e}"
            ) from e

        self.column_name = column_name
        self.valid_indices = []
        for idx, row in enumerate(self.ds):
            if parse_structured_qa(row.get(self.column_name)):
                self.valid_indices.append(idx)
        self.valid_indices = apply_filtered_slice(self.valid_indices, slice_spec)

        if not self.valid_indices:
            raise RuntimeError(
                f"No parseable ### Question / ### Answer rows found in "
                f"{dataset_name} split {split!r} column {column_name!r} "
                f"(loaded base split {base_split!r})"
            )

    @property
    def eval_type(self):
        return "generative"

    def num_examples(self):
        return len(self.valid_indices)

    def get_example(self, index):
        row = self.ds[self.valid_indices[index]]
        parsed = parse_structured_qa(row.get(self.column_name))
        if parsed is None:
            raise RuntimeError(f"Unexpected unparsable row at index {index}")
        question, answer = parsed
        return {
            "messages": [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
        }
