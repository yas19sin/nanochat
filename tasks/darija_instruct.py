"""
Darija instruction-following datasets for SFT.

Datasets:
- Lyte/Moroccan-Darija-Instruct-573K (train/test)
- GemMaroc/TULU-3-50k-darija-english (train/validation)
"""

from datasets import load_dataset
from tasks.common import Task


def normalize_messages(messages):
    """
    Normalize message structure to `{"role": user|assistant|system, "content": str}`
    and drop entries without usable text.
    """
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role") or message.get("speaker") or message.get("from")
        if role is None:
            continue
        role = role.lower()
        if role in ["assistant", "gpt", "model"]:
            role = "assistant"
        elif role == "system":
            role = "system"
        else:
            role = "user"
        content = message.get("content") or message.get("text") or message.get("value")
        if not isinstance(content, str):
            continue
        normalized.append({"role": role, "content": content})
    return normalized


def validate_conversation(messages, dataset_name):
    # Require at least one user/assistant exchange; allow optional leading system.
    if not messages or len(messages) < 2:
        raise ValueError(f"{dataset_name}: conversation too short ({len(messages)})")
    # Strip optional leading system from role checks
    start_idx = 1 if messages[0]["role"] == "system" else 0
    trimmed = messages[start_idx:]
    if len(trimmed) < 2:
        raise ValueError(f"{dataset_name}: need at least one user/assistant turn")
    for i, message in enumerate(trimmed):
        expected = "user" if i % 2 == 0 else "assistant"
        assert message["role"] == expected, f"{dataset_name}: expected {expected} at position {i}, got {message['role']}"
        assert isinstance(message["content"], str)


class DarijaInstruct(Task):
    """Lyte/Moroccan-Darija-Instruct-573K"""

    def __init__(self, split, **kwargs):
        super().__init__(**kwargs)
        assert split in ["train", "test"], "split must be train|test"
        self.ds = load_dataset("Lyte/Moroccan-Darija-Instruct-573K", split=split).shuffle(seed=42)
        self.length = len(self.ds)
        self.dataset_name = "Lyte/Moroccan-Darija-Instruct-573K"

    def num_examples(self):
        return self.length

    def get_example(self, index):
        row = self.ds[index]
        messages = normalize_messages(row["messages"])
        validate_conversation(messages, self.dataset_name)
        return {"messages": messages}


class DarijaTulu(Task):
    """GemMaroc/TULU-3-50k-darija-english"""

    def __init__(self, split, **kwargs):
        super().__init__(**kwargs)
        assert split in ["train", "validation"], "split must be train|validation"
        self.ds = load_dataset("GemMaroc/TULU-3-50k-darija-english", split=split).shuffle(seed=42)
        self.length = len(self.ds)
        self.dataset_name = "GemMaroc/TULU-3-50k-darija-english"

    def num_examples(self):
        return self.length

    def get_example(self, index):
        row = self.ds[index]
        messages = row.get("dialogs") or row.get("messages") or []
        messages = normalize_messages(messages)
        validate_conversation(messages, self.dataset_name)
        return {"messages": messages}
