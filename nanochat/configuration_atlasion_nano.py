try:
    from .configuration_nanochat import NanochatConfig
except ImportError:
    try:
        from configuration_nanochat import NanochatConfig
    except ImportError:
        # Handle case where loaded from transformers cache with custom module loading
        import sys
        import importlib.util
        from pathlib import Path
        
        cache_dir = Path(__file__).parent
        config_file = cache_dir / "configuration_nanochat.py"
        if config_file.exists():
            spec = importlib.util.spec_from_file_location("configuration_nanochat", config_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["configuration_nanochat"] = mod
                spec.loader.exec_module(mod)
                NanochatConfig = mod.NanochatConfig
        else:
            raise ImportError("Could not locate configuration_nanochat module")


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
            self, "pad_vocab_size_to", pad_vocab_size_to)
        self.ve_gate_channels = getattr(
            self, "ve_gate_channels", ve_gate_channels)
        self.smear_gate_channels = getattr(
            self, "smear_gate_channels", smear_gate_channels)
