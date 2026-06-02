try:
    from .configuration_atlasion_nano import AtlasionNanoConfig
    from .modeling_nanochat import NanochatForCausalLM
except ImportError:
    from configuration_atlasion_nano import AtlasionNanoConfig
    from modeling_nanochat import NanochatForCausalLM


class AtlasionNanoForCausalLM(NanochatForCausalLM):
    config_class = AtlasionNanoConfig
