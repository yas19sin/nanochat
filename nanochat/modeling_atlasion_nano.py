from __future__ import annotations

# Keep these as direct relative imports.
# Transformers' trust_remote_code dependency scanner sees these and copies
# the files into the dynamic module cache.
from .configuration_atlasion_nano import AtlasionNanoConfig
from .configuration_nanochat import NanochatConfig  # noqa: F401
from .modeling_nanochat import NanochatForCausalLM


class AtlasionNanoForCausalLM(NanochatForCausalLM):
    config_class = AtlasionNanoConfig
