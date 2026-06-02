try:
    from .configuration_nanochat import NanochatConfig
except ImportError:
    from configuration_nanochat import NanochatConfig


class AtlasionNanoConfig(NanochatConfig):
    model_type = "atlasion_nano"
