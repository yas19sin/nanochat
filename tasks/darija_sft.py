"""
Darija SFT datasets from HuggingFace.

- Lyte/Moroccan-Darija-Instruct-573K: column `messages`
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


def parse_structured_qa(text):
    match = QA_RE.match((text or "").strip())
    if not match:
        return None
    question = match.group("question").strip()
    answer = match.group("answer").strip()
    if not question or not answer:
        return None
    return question, answer


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
        load_kwargs = {"split": split}
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

        if not self.valid_indices:
            raise RuntimeError(
                f"No parseable ### Question / ### Answer rows found in "
                f"{dataset_name} split {split!r} column {column_name!r}"
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
