from transformers import PretrainedConfig


class NanochatConfig(PretrainedConfig):
    model_type = "nanochat"

    def __init__(
        self,
        sequence_len=2048,
        vocab_size=32768,
        n_layer=12,
        n_head=6,
        n_kv_head=6,
        n_embd=768,
        window_pattern="SSSL",
        pad_vocab_size_to=64,
        use_cache=True,
        output_hidden_states=False,
        bos_token_id=None,
        eos_token_id=None,
        pad_token_id=None,
        tie_word_embeddings=False,
        **kwargs,
    ):
        self.sequence_len = sequence_len
        self.vocab_size = vocab_size
        self.n_layer = n_layer
        self.n_head = n_head
        self.n_kv_head = n_kv_head
        self.n_embd = n_embd
        self.window_pattern = window_pattern
        self.pad_vocab_size_to = pad_vocab_size_to
        self.use_cache = use_cache
        self.output_hidden_states = output_hidden_states
        super().__init__(
            bos_token_id=bos_token_id,
            eos_token_id=eos_token_id,
            pad_token_id=pad_token_id,
            tie_word_embeddings=tie_word_embeddings,
            **kwargs,
        )

    @property
    def num_hidden_layers(self):
        return self.n_layer

    @property
    def head_dim(self):
        return self.n_embd // self.n_head

    @property
    def padded_vocab_size(self):
        multiple = self.pad_vocab_size_to
        return ((self.vocab_size + multiple - 1) // multiple) * multiple
