
from __future__ import absolute_import, division, print_function, unicode_literals

from collections.abc import Iterable
from itertools import repeat
from typing import Callable, Optional, Tuple
import torch
import torch.nn as nn
from fairseq import utils

from fairseq.modules.fairseq_dropout import FairseqDropout
from fairseq.modules import LayerNorm

from fairseq.modules import (
    FairseqDropout,
    LayerDropModuleList,
    LayerNorm,
    PositionalEmbedding)

def init_bert_params(module):
    """
    Initialize the weights specific to the BERT Model.
    This overrides the default initializations depending on the specified arguments.
        1. If normal_init_linear_weights is set then weights of linear
           layer will be initialized using the normal distribution and
           bais will be set to the specified value.
        2. If normal_init_embed_weights is set then weights of embedding
           layer will be initialized using the normal distribution.
        3. If normal_init_proj_weights is set then weights of
           in_project_weight for MultiHeadAttention initialized using
           the normal distribution (to be validated).
    """

    if isinstance(module, nn.Linear):
        module.weight.data.normal_(mean=0.0, std=0.02)
        if module.bias is not None:
            module.bias.data.zero_()
    if isinstance(module, nn.Conv1d):
        scale = module.kernel_size[0]
        module.weight.data.normal_(mean=0.0, std=0.02 / scale)
    if isinstance(module, nn.Embedding):
        module.weight.data.normal_(mean=0.0, std=0.02)
        if module.padding_idx is not None:
            module.weight.data[module.padding_idx].zero_()


class MPConvLayer(nn.Module):
    def __init__(self,
                 channels,
                 kernel_range,
                 use_bias=True):
        super().__init__()
        self.mods = nn.ModuleList()
        self.use_bias = use_bias
        for i in range(kernel_range + 1):
            if i % 2 == 0:
                continue
            conv_op = nn.Conv1d(
                channels,
                channels,
                i,
                1,
                i // 2,
                1,
                1,
                False
            )
            self.mods.append(conv_op)
        if use_bias:
            self.register_parameter(
                "bias",
                nn.parameter.Parameter(
                    torch.zeros(channels)
                )
            )

    
    def forward(self, x):
        out = x
        for conv_op in self.mods:
            out += conv_op(x)
        if self.use_bias:
            out = out + self.bias
        return out


class MPConvSentenceEncoderLayer(nn.Module):
    def __init__(
        self,
        channels: int = 768,
        kernel_range: int = 5,
        dropout: float = 0.1,
        activation_dropout: float = 0.1,
        activation_fn: str = "gelu",
        use_bias: bool = True,
        use_extra_residual: bool = True,
        export: bool = False,
        init_fn: Callable = None,
    ) -> None:
        super().__init__()
        if init_fn is not None:
            init_fn()
        self.dropout_module = FairseqDropout(
            dropout, module_name=self.__class__.__name__
        )
        self.activation1_dropout_module = FairseqDropout(
            activation_dropout, module_name=self.__class__.__name__
        )
        #self.activation2_dropout_module = FairseqDropout(
        #    activation_dropout, module_name=self.__class__.__name__
        #)
        self.use_extra_residual = use_extra_residual
        self.final_layer_norm = LayerNorm(channels, export=export)
        self.activation_fn = utils.get_activation_fn(activation_fn)
        self.fc1 = MPConvLayer(channels, kernel_range, use_bias)
        #self.fc2 = MPConvLayer(channels, kernel_range, use_bias)
        self.fc3 = MPConvLayer(channels, kernel_range, use_bias)
    
    def forword(
        self,
        x: torch.Tensor,
    ):
        residual = x
        x = self.activation_fn(self.fc1(x))
        x = self.activation1_dropout_module(x)
        #x = self.activation_fn(self.fc2(x))
        #x = self.activation2_dropout_module(x)
        x = self.activation_fn(self.fc3(x))
        x = self.dropout_module(x)
        if self.use_extra_residual:
            x = residual + x
        x = self.final_layer_norm(x)


class MPConvSentenceEncoder(nn.Module):
    """
    Implementation for a Bi-directional Transformer based Sentence Encoder used
    in BERT/XLM style pre-trained models.

    This first computes the token embedding using the token embedding matrix,
    position embeddings (if specified) and segment embeddings
    (if specified). After applying the specified number of
    TransformerEncoderLayers, it outputs all the internal states of the
    encoder as well as the final representation associated with the first
    token (usually CLS token).

    Input:
        - tokens: B x T matrix representing sentences
        - segment_labels: B x T matrix representing segment label for tokens

    Output:
        - a tuple of the following:
            - a list of internal model states used to compute the
              predictions where each tensor has shape T x B x C
            - sentence representation associated with first input token
              in format B x C.
    """
    def __init__(
        self,
        padding_idx: int,
        vocab_size: int,
        kernel_range: int = 5,
        encoder_extra_residual = True,
        num_encoder_layers: int = 6,
        embedding_dim: int = 768,
        ffn_channel_dim: int = 768,
        dropout: float = 0.1,
        activation_dropout: float = 0.1,
        layerdrop: float = 0.0,
        max_seq_len: int = 256,
        num_segments: int = 2,
        use_position_embeddings: bool = True,
        offset_positions_by_padding: bool = True,
        encoder_normalize_before: bool = False,
        apply_bert_init: bool = False,
        activation_fn: str = "relu",
        learned_pos_embedding: bool = True,
        embed_scale: float = None,
        freeze_embeddings: bool = False,
        n_trans_layers_to_freeze: int = 0,
        export: bool = False,
    ) -> None:
        super().__init__()
        self.padding_idx = padding_idx
        self.vocab_size = vocab_size
        self.dropout_module = FairseqDropout(
            dropout, module_name=self.__class__.__name__
        )
        self.layerdrop = layerdrop
        self.max_seq_len = max_seq_len
        self.embedding_dim = embedding_dim
        self.num_segments = num_segments
        self.use_position_embeddings = use_position_embeddings
        self.apply_bert_init = apply_bert_init
        self.learned_pos_embedding = learned_pos_embedding

        self.embed_tokens = self.build_embedding(
            self.vocab_size, self.embedding_dim, self.padding_idx
        )
        self.embed_scale = embed_scale
        self.quant_noise = None
        self.segment_embeddings = (
            nn.Embedding(self.num_segments, self.embedding_dim, padding_idx=None)
            if self.num_segments > 0
            else None
        )

        self.embed_positions = (
            PositionalEmbedding(
                self.max_seq_len,
                self.embedding_dim,
                padding_idx=(self.padding_idx if offset_positions_by_padding else None),
                learned=self.learned_pos_embedding,
            )
            if self.use_position_embeddings
            else None
        )
        self.embed_proj = nn.Linear(
            embedding_dim,
            ffn_channel_dim
        )
        if encoder_normalize_before:
            self.emb_layer_norm = LayerNorm(self.embedding_dim, export=export)
        else:
            self.emb_layer_norm = None

        if self.layerdrop > 0.0:
            self.layers = LayerDropModuleList(p=self.layerdrop)
        else:
            self.layers = nn.ModuleList([])
        self.layers.extend(
            [
                self.build_mpconv_sentence_encoder_layer(
                    kernel_range=kernel_range,
                    ffn_channel_dim=ffn_channel_dim,
                    dropout=self.dropout_module.p,
                    activation_dropout=activation_dropout,
                    activation_fn=activation_fn,
                    export=export,
                    extra_residual=encoder_extra_residual
                )
                for _ in range(num_encoder_layers)
            ]
        )
        if self.apply_bert_init:
            self.apply(init_bert_params)

        def freeze_module_params(m):
            if m is not None:
                for p in m.parameters():
                    p.requires_grad = False

        if freeze_embeddings:
            freeze_module_params(self.embed_tokens)
            freeze_module_params(self.segment_embeddings)
            freeze_module_params(self.embed_positions)
            freeze_module_params(self.emb_layer_norm)

        for layer in range(n_trans_layers_to_freeze):
            freeze_module_params(self.layers[layer])
    def build_embedding(self, vocab_size, embedding_dim, padding_idx):
        return nn.Embedding(vocab_size, embedding_dim, padding_idx)

    def build_mpconv_sentence_encoder_layer(
        self,
        kernel_range,
        ffn_channel_dim,
        dropout,
        activation_dropout,
        activation_fn,
        export,
        extra_residual,
    ):
        return MPConvSentenceEncoderLayer(
            channels=ffn_channel_dim,
            kernel_range=kernel_range,
            dropout=dropout,
            activation_dropout=activation_dropout,
            activation_fn=activation_fn,
            export=export,
            use_extra_residual=extra_residual
        )

    def forward(
        self,
        tokens: torch.Tensor,
        segment_labels: torch.Tensor = None,
        last_state_only: bool = False,
        positions: Optional[torch.Tensor] = None,
        token_embeddings: Optional[torch.Tensor] = None,
        attn_mask: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        is_tpu = tokens.device.type == "xla"

        # compute padding mask. This is needed for multi-head attention
        padding_mask = tokens.eq(self.padding_idx)
        if not self.traceable and not is_tpu and not padding_mask.any():
            padding_mask = None

        if token_embeddings is not None:
            x = token_embeddings
        else:
            x = self.embed_tokens(tokens)

        if self.embed_scale is not None:
            x = x * self.embed_scale

        if self.embed_positions is not None:
            x = x + self.embed_positions(tokens, positions=positions)

        if self.segment_embeddings is not None and segment_labels is not None:
            x = x + self.segment_embeddings(segment_labels)
        x = self.embed_proj(x)
        # if self.quant_noise is not None:
        #     x = self.quant_noise(x)

        if self.emb_layer_norm is not None:
            x = self.emb_layer_norm(x)

        x = self.dropout_module(x)

        # account for padding while computing the representation
        if padding_mask is not None:
            x = x * (1 - padding_mask.unsqueeze(-1).type_as(x))

        # B x T x C -> B x C x T
        x = x.transpose(2, 1)

        inner_states = []
        if not last_state_only:
            inner_states.append(x)

        for layer in self.layers:
            x, _ = layer(x)
            if not last_state_only:
                inner_states.append(x)

        # Note: BERT use TBC layout
        # sentence_rep = x[0, :, :]

        if last_state_only:
            inner_states = [x]

        # if self.traceable:
        #     return torch.stack(inner_states), sentence_rep
        # else:
        #     return inner_states, sentence_rep