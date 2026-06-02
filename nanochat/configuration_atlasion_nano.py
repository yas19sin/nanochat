from __future__ import annotations

from .configuration_nanochat import NanochatConfig


class AtlasionNanoConfig(NanochatConfig):
    model_type = "atlasion_nano"

    def __init__(
        self,
        *args,
        pad_vocab_size_to=64,
        ve_gate_channels=12,
        smear_gate_channels=24,
        **kwargs,
    ):
        super().__init__(
            *args,
            pad_vocab_size_to=pad_vocab_size_to,
            ve_gate_channels=ve_gate_channels,
            smear_gate_channels=smear_gate_channels,
            **kwargs,
        )
        self.pad_vocab_size_to = getattr(
            self, "pad_vocab_size_to", pad_vocab_size_to
        )
        self.ve_gate_channels = getattr(
            self, "ve_gate_channels", ve_gate_channels
        )
        self.smear_gate_channels = getattr(
            self, "smear_gate_channels", smear_gate_channels
        )
