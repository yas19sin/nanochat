"""
Darija instruction-following evaluation task.

Uses a small held-out set of Darija conversational examples to evaluate
whether the model can follow instructions in Moroccan Darija.
"""

from datasets import load_dataset
from tasks.common import Task


class DarijaInstruct(Task):
    """
    A lightweight eval task that loads Darija instruction data from
    HuggingFace and wraps it in the nanochat Task interface.
    """

    HF_DATASET = "Lyte/darija-pretraining-corpus"
    SUBSET = "pure"

    def __init__(self, split="train", max_examples=500, **kwargs):
        super().__init__(**kwargs)
        assert split in ["train", "test"], "split must be train|test"
        # Load a small slice of pure Darija text
        ds = load_dataset(self.HF_DATASET, self.SUBSET, split="train")
        ds = ds.shuffle(seed=42)
        # Use the tail as "test" to avoid overlap with training data
        total = len(ds)
        cutoff = total - max_examples
        if split == "test":
            self.ds = ds.select(range(cutoff, total))
        else:
            self.ds = ds.select(range(min(max_examples, cutoff)))
        self.length = len(self.ds)

    @property
    def eval_type(self):
        return "generative"

    def num_examples(self):
        return self.length

    def get_example(self, index):
        row = self.ds[index]
        text = row["text"]
        # Wrap as a simple user->assistant conversation for chat eval
        conversation = {
            "messages": [
                # prompt = first 200 chars
                {"role": "user", "content": text[:200]},
                {"role": "assistant", "content": text},    # reference = full text
            ],
        }
        return conversation
