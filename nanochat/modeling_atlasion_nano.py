try:
    from .configuration_atlasion_nano import AtlasionNanoConfig
    from .modeling_nanochat import NanochatForCausalLM
except ImportError:
    try:
        from configuration_atlasion_nano import AtlasionNanoConfig
        from modeling_nanochat import NanochatForCausalLM
    except ImportError:
        # Handle case where loaded from transformers cache with custom module loading
        import sys
        import importlib.util
        from pathlib import Path
        
        cache_dir = Path(__file__).parent
        
        # Load configuration_atlasion_nano
        config_file = cache_dir / "configuration_atlasion_nano.py"
        if config_file.exists():
            spec = importlib.util.spec_from_file_location("configuration_atlasion_nano", config_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["configuration_atlasion_nano"] = mod
                spec.loader.exec_module(mod)
                AtlasionNanoConfig = mod.AtlasionNanoConfig
        
        # Load modeling_nanochat
        model_file = cache_dir / "modeling_nanochat.py"
        if model_file.exists():
            spec = importlib.util.spec_from_file_location("modeling_nanochat", model_file)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules["modeling_nanochat"] = mod
                spec.loader.exec_module(mod)
                NanochatForCausalLM = mod.NanochatForCausalLM
        
        if not (config_file.exists() and model_file.exists()):
            raise ImportError("Could not locate required modules")


class AtlasionNanoForCausalLM(NanochatForCausalLM):
    config_class = AtlasionNanoConfig
