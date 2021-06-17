# coding=utf-8
# Copyright 2021 T5 Authors and HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
""" Flax T5 model. """


import copy
from typing import Tuple, Optional, Callable
import numpy as np

import flax.linen as nn
from flax.linen.attention import dot_product_attention_weights
from flax.linen import combine_masks, make_causal_mask
from flax.core.frozen_dict import FrozenDict
import jax.numpy as jnp
from jax.random import PRNGKey
import jax

from ...modeling_flax_utils import ACT2FN
from ...file_utils import (
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    replace_return_docstrings,
)
from ...modeling_outputs import (
    BaseModelOutput,
    BaseModelOutputWithPastAndCrossAttentions,
    Seq2SeqLMOutput,
    Seq2SeqModelOutput,
)
from ...modeling_flax_utils import FlaxPreTrainedModel
from ...utils import logging
from .configuration_t5 import T5Config


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "T5Config"
_TOKENIZER_FOR_DOC = "T5Tokenizer"

####################################################
# This dict contains ids and associated url
# for the pretrained weights provided with the models
####################################################
T5_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "t5-small",
    "t5-base",
    "t5-large",
    "t5-3b",
    "t5-11b",
    # See all T5 models at https://huggingface.co/models?filter=t5
]


####################################################
# PyTorch Models are constructed by sub-classing
# - torch.nn.Module for the layers and
# - PreTrainedModel for the models (it-self a sub-class of torch.nn.Module)
####################################################
# TODO: pass dtype everywhere

class FlaxT5LayerNorm(nn.Module):
    hidden_size: int
    dtype: jnp.dtype = jnp.float32
    eps: float = 1e-6
    weight_init: Callable[..., np.ndarray] = jax.nn.initializers.ones

    def setup(self):
        self.weight = self.param("weight", self.weight_init, (self.hidden_size,))

    def __call__(self, hidden_states):
        """
        Construct a layernorm module in the T5 style; No bias and no subtraction of mean.
        """
        # layer norm should always be calculated in float32
        variance = jnp.power(hidden_states.astype("f4"), 2).mean(axis=-1, keepdims=True)
        hidden_states = hidden_states / jnp.sqrt(variance + self.eps)

        return self.weight * hidden_states


class FlaxT5DenseReluDense(nn.Module):
    config: T5Config
    dtype: jnp.dtype = jnp.float32

    def setup(self):
        self.wi = nn.Dense(self.config.d_ff, use_bias=False, dtype=self.dtype)
        self.wo = nn.Dense(self.config.d_model, use_bias=False, dtype=self.dtype)
        self.dropout = nn.Dropout(self.config.dropout_rate)

    def __call__(self, hidden_states, deterministic=True):
        hidden_states = self.wi(hidden_states)
        hidden_states = jax.nn.relu(hidden_states)
        hidden_states = self.dropout(hidden_states, deterministic)
        hidden_states = self.wo(hidden_states)
        return hidden_states


class FlaxT5DenseGatedGeluDense(nn.Module):
    config: T5Config
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.wi_0 = nn.Dense(self.config.d_ff, use_bias=False, dtype=self.dtype)
        self.wi_1 = nn.Dense(self.config.d_ff, use_bias=False, dtype=self.dtype)
        self.wo = nn.Dense(self.config.d_model, use_bias=False, dtype=self.dtype)
        self.dropout = nn.Dropout(self.config.dropout_rate)
        self.gelu_act = ACT2FN["gelu_new"]

    def forward(self, hidden_states):
        hidden_gelu = self.gelu_act(self.wi_0(hidden_states))
        hidden_linear = self.wi_1(hidden_states)
        hidden_states = hidden_gelu * hidden_linear
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.wo(hidden_states)
        return hidden_states


class FlaxT5LayerFF(nn.Module):
    config: T5Config
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        if self.config.feed_forward_proj == "relu":
            self.DenseReluDense = FlaxT5DenseReluDense(self.config, dtype=self.dtype)
        elif self.dropoutconfig.feed_forward_proj == "gated-gelu":
            self.DenseReluDense = FlaxT5DenseGatedGeluDense(self.config, dtype=self.dtype)
        else:
            raise ValueError(
                f"{self.config.feed_forward_proj} is not supported. Choose between `relu` and `gated-gelu`"
            )

        self.layer_norm = FlaxT5LayerNorm(self.config.d_model, eps=self.config.layer_norm_epsilon, dtype=self.dtype)
        self.dropout = nn.Dropout(self.config.dropout_rate)

    def __call__(self, hidden_states, deterministic=True):
        forwarded_states = self.layer_norm(hidden_states)
        forwarded_states = self.DenseReluDense(forwarded_states, deterministic=deterministic)
        hidden_states = hidden_states + self.dropout(forwarded_states, deterministic=deterministic)
        return hidden_states


class FlaxT5Attention(nn.Module):
    config: T5Config
    has_relative_attention_bias: bool = False
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.is_decoder = self.config.is_decoder
        self.relative_attention_num_buckets = self.config.relative_attention_num_buckets
        self.d_model = self.config.d_model
        self.key_value_proj_dim = self.config.d_kv
        self.n_heads = self.config.num_heads
        self.dropout = self.config.dropout_rate
        self.inner_dim = self.n_heads * self.key_value_proj_dim

        # Mesh TensorFlow initialization to avoid scaling before softmax
        self.q = nn.Dense(self.inner_dim, use_bias=False, dtype=self.dtype)
        self.k = nn.Dense(self.inner_dim, use_bias=False, dtype=self.dtype)
        self.v = nn.Dense(self.inner_dim, use_bias=False, dtype=self.dtype)
        self.o = nn.Dense(self.d_model, use_bias=False, dtype=self.dtype)

        if self.has_relative_attention_bias:
            self.relative_attention_bias = nn.Embed(self.relative_attention_num_buckets, self.n_heads, dtype=self.dtype)

    @staticmethod
    def _relative_position_bucket(relative_position, bidirectional=True, num_buckets=32, max_distance=128):
        """
        Adapted from Mesh Tensorflow:
        https://github.com/tensorflow/mesh/blob/0cb87fe07da627bf0b7e60475d59f95ed6b5be3d/mesh_tensorflow/transformer/transformer_layers.py#L593

        Translate relative position to a bucket number for relative attention. The relative position is defined as
        memory_position - query_position, i.e. the distance in tokens from the attending position to the attended-to
        position. If bidirectional=False, then positive relative positions are invalid. We use smaller buckets for
        small absolute relative_position and larger buckets for larger absolute relative_positions. All relative
        positions >=max_distance map to the same bucket. All relative positions <=-max_distance map to the same bucket.
        This should allow for more graceful generalization to longer sequences than the model has been trained on

        Args:
            relative_position: an int32 Tensor
            bidirectional: a boolean - whether the attention is bidirectional
            num_buckets: an integer
            max_distance: an integer

        Returns:
            a Tensor with the same shape as relative_position, containing int32 values in the range [0, num_buckets)
        """
        relative_buckets = 0
        if bidirectional:
            num_buckets //= 2
            relative_buckets += (relative_position > 0) * num_buckets
            relative_position = jnp.abs(relative_position)
        else:
            relative_position = -jnp.clip(relative_position, a_max=0)
        # now relative_position is in the range [0, inf)

        # half of the buckets are for exact increments in positions
        max_exact = num_buckets // 2
        is_small = relative_position < max_exact

        # The other half of the buckets are for logarithmically bigger bins in positions up to max_distance
        relative_position_if_large = max_exact + (
            jnp.log(relative_position / max_exact)
            / jnp.log(max_distance / max_exact)
            * (num_buckets - max_exact)
        )
        relative_position_if_large = jnp.clip(relative_position_if_large, a_max=num_buckets - 1)

        relative_buckets += jnp.where(is_small, relative_position, relative_position_if_large)

        return relative_buckets.astype("i4")

    def compute_bias(self, query_length, key_length):
        """Compute binned relative position bias"""
        context_position = jnp.arange(query_length, dtype="i4")[:, None]
        memory_position = jnp.arange(key_length, dtype="i4")[None, :]
        relative_position = memory_position - context_position  # shape (query_length, key_length)
        relative_position_bucket = self._relative_position_bucket(
            relative_position,  # shape (query_length, key_length)
            bidirectional=(not self.is_decoder),
            num_buckets=self.relative_attention_num_buckets,
        )
        values = self.relative_attention_bias(relative_position_bucket)  # shape (query_length, key_length, num_heads)
        values = values.transpose((2, 0, 1))[None, :, :, :]  # shape (1, num_heads, query_length, key_length)
        return values

    def _split_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.n_heads, self.key_value_proj_dim))

    def _merge_heads(self, hidden_states):
        return hidden_states.reshape(hidden_states.shape[:2] + (self.inner_dim,))

    def __call__(
        self,
        hidden_states,
        mask=None,
        key_value_states=None,
        position_bias=None,
        query_length=None,
        use_cache=False,
        output_attentions=False,
        deterministic=True,
    ):
        """
        Self-attention (if key_value_states is None) or attention over source sentence (provided by key_value_states).
        """
        # Input is (batch_size, seq_length, dim)
        # Mask is (batch_size, key_length) (non-causal) or (batch_size, key_length, key_length)
        # past_key_value[0] is (batch_size, n_heads, q_len - 1, dim_per_head)
        batch_size, seq_length = hidden_states.shape[:2]

#        int_seq_length = int(seq_length)

        real_seq_length = seq_length

#        real_seq_length += query_length

        key_length = real_seq_length if key_value_states is None else key_value_states.shape[1]

        # get query states
        query_states = self.q(hidden_states)  # (batch_size, n_heads, seq_length, dim_per_head)
        key_states = self.k(hidden_states)
        value_states = self.v(hidden_states)

        query_states = self._split_heads(query_states)
        key_states = self._split_heads(key_states)
        value_states = self._split_heads(value_states)

        if self.is_decoder:
            causal_mask = make_causal_mask(mask, dtype="bool")
            causal_mask = jnp.broadcast_to(causal_mask, (batch_size,) + causal_mask.shape[1:])

        # combine masks if needed
        if mask is not None and self.is_decoder:
            mask = jnp.broadcast_to(jnp.expand_dims(mask, axis=(-3, -2)), causal_mask.shape)
            mask = combine_masks(mask, causal_mask)
        elif self.is_decoder:
            mask = causal_mask
        elif mask is not None:
            mask = jnp.expand_dims(mask, axis=(-3, -2))

        dropout_rng = None
        if not deterministic and self.dropout > 0.0:
            dropout_rng = self.make_rng("dropout")

        if position_bias is None:
            if not self.has_relative_attention_bias:
                position_bias = jnp.zeros(
                    (1, self.n_heads, real_seq_length, key_length), dtype=self.dtype
                )
            else:
                position_bias = self.compute_bias(real_seq_length, key_length)

            # if key and values are already calculated
            # we want only the last query position bias
#            if past_key_value is not None:
#                position_bias = position_bias[:, :, -int_seq_length:, :]

            if mask is not None:
                position_bias = position_bias + mask  # (batch_size, n_heads, seq_length, key_length)

        attn_weights = dot_product_attention_weights(
            query_states,
            key_states,
            bias=position_bias,
            dropout_rng=dropout_rng,
            dropout_rate=self.dropout,
            broadcast_dropout=True,
            deterministic=deterministic,
            dtype=self.dtype,
        )

#        scores += position_bias
#        attn_weights = jax.nn.softmax(scores.float(), dim=-1).type_as(
#            scores
#        )  # (batch_size, n_heads, seq_length, key_length)
#        attn_weights = flax.nn.dropout(
#            attn_weights, p=self.dropout, training=self.training
#        )  # (batch_size, n_heads, seq_length, key_length)

        attn_output = jnp.einsum("...hqk,...khd->...qhd", attn_weights, value_states)
        attn_output = self._merge_heads(attn_output)
        attn_output = self.o(attn_output)

        outputs = (attn_output, position_bias)

        if output_attentions:
            outputs = outputs + (attn_weights,)
        return outputs


class FlaxT5LayerSelfAttention(nn.Module):
    config: T5Config
    has_relative_attention_bias: bool = False
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.SelfAttention = FlaxT5Attention(self.config, has_relative_attention_bias=self.has_relative_attention_bias, dtype=self.dtype)
        self.layer_norm = FlaxT5LayerNorm(self.config.d_model, eps=self.config.layer_norm_epsilon, dtype=self.dtype)
        self.dropout = nn.Dropout(self.config.dropout_rate)

    def __call__(
        self,
        hidden_states,
        attention_mask=None,
        position_bias=None,
        output_attentions=False,
        deterministic=True,
    ):
        normed_hidden_states = self.layer_norm(hidden_states)
        attention_output = self.SelfAttention(
            normed_hidden_states,
            mask=attention_mask,
            position_bias=position_bias,
            output_attentions=output_attentions,
            deterministic=deterministic,
        )
        hidden_states = hidden_states + self.dropout(attention_output[0], deterministic=deterministic)
        outputs = (hidden_states,) + attention_output[1:]  # add attentions if we output them
        return outputs


class FlaxT5LayerCrossAttention(nn.Module):
    config: T5Config

    def setup(self):
        self.EncDecAttention = FlaxT5Attention(self.config, has_relative_attention_bias=False)
        self.layer_norm = FlaxT5LayerNorm(self.config.d_model, eps=self.config.layer_norm_epsilon)
        self.dropout = nn.Dropout(self.config.dropout_rate)

    def __call__(
        self,
        hidden_states,
        key_value_states,
        attention_mask=None,
        position_bias=None,
        query_length=None,
        output_attentions=False,
        deterministic=True,
    ):
        normed_hidden_states = self.layer_norm(hidden_states)
        attention_output = self.EncDecAttention(
            normed_hidden_states,
            mask=attention_mask,
            key_value_states=key_value_states,
            position_bias=position_bias,
            query_length=query_length,
            output_attentions=output_attentions,
        )
        layer_output = hidden_states + self.dropout(attention_output[0], deterministic=deterministic)
        outputs = (layer_output,) + attention_output[1:]  # add attentions if we output them
        return outputs


class FlaxT5Block(nn.Module):
    config: T5Config
    has_relative_attention_bias: bool = False
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.is_decoder = self.config.is_decoder
        self.layer = (FlaxT5LayerSelfAttention(self.config, has_relative_attention_bias=self.has_relative_attention_bias, name=str(0)),)
        feed_forward_index = 1
        if self.is_decoder:
            self.layer += (FlaxT5LayerCrossAttention(self.config, name=str(1)),)
            feed_forward_index = 2

        self.layer += (FlaxT5LayerFF(self.config, name=str(feed_forward_index)),)

    def __call__(
        self,
        hidden_states,
        attention_mask=None,
        position_bias=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        encoder_decoder_position_bias=None,
        cross_attn_layer_head_mask=None,
        output_attentions=False,
        return_dict=True,
        deterministic=True,
    ):
        self_attention_outputs = self.layer[0](
            hidden_states,
            attention_mask=attention_mask,
            position_bias=position_bias,
            output_attentions=output_attentions,
            deterministic=deterministic,
        )
        hidden_states = self_attention_outputs[0]
        attention_outputs = self_attention_outputs[1:]  # Keep self-attention outputs and relative position weights

        do_cross_attention = self.is_decoder and encoder_hidden_states is not None
        if do_cross_attention:
            # the actual query length is unknown for cross attention
            # if using past key value states. Need to inject it here
            query_length = None

            cross_attention_outputs = self.layer[1](
                hidden_states,
                key_value_states=encoder_hidden_states,
                attention_mask=encoder_attention_mask,
                position_bias=encoder_decoder_position_bias,
                query_length=query_length,
                output_attentions=output_attentions,
                deterministic=deterministic,
            )
            hidden_states = cross_attention_outputs[0]

            # Keep cross-attention outputs and relative position weights
            attention_outputs = attention_outputs + cross_attention_outputs[1:]

        # Apply Feed Forward layer
        hidden_states = self.layer[-1](hidden_states, deterministic=deterministic)

        outputs = (hidden_states,)

        outputs = outputs + attention_outputs

        return outputs  # hidden-states, present_key_value_states, (self-attention position bias), (self-attention weights), (cross-attention position bias), (cross-attention weights)


class FlaxT5Stack(nn.Module):
    config: T5Config
    embed_tokens: Optional[nn.Embed] = None
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.is_decoder = self.config.is_decoder

        self.block = [FlaxT5Block(self.config, has_relative_attention_bias=bool(i == 0), name=str(i), dtype=self.dtype) for i in range(self.config.num_layers)]
        self.final_layer_norm = FlaxT5LayerNorm(self.config.d_model, eps=self.config.layer_norm_epsilon, dtype=self.dtype)
        self.dropout = nn.Dropout(self.config.dropout_rate)

    def __call__(
        self,
        input_ids=None,
        attention_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions: bool = False,
        output_hidden_states: bool = False,
        return_dict: bool = True,
        deterministic: bool = True,
    ):

        # Prepare head mask if needed
        all_hidden_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None
        all_cross_attentions = () if (output_attentions and self.is_decoder) else None
        position_bias = None
        encoder_decoder_position_bias = None

        hidden_states = self.embed_tokens(input_ids)
        hidden_states = self.dropout(hidden_states, deterministic=deterministic)

        for i, layer_module in enumerate(self.block):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            layer_outputs = layer_module(
                hidden_states,
                attention_mask=attention_mask,
                position_bias=position_bias,
                encoder_hidden_states=encoder_hidden_states,
                encoder_attention_mask=encoder_attention_mask,
                encoder_decoder_position_bias=encoder_decoder_position_bias,
                output_attentions=output_attentions,
                deterministic=deterministic,
            )

            hidden_states = layer_outputs[0]

            # We share the position biases between the layers - the first layer store them
            # layer_outputs = hidden-states, key-value-states (self-attention position bias), (self-attention weights),
            # (cross-attention position bias), (cross-attention weights)
            position_bias = layer_outputs[1]

            if self.is_decoder and encoder_hidden_states is not None:
                encoder_decoder_position_bias = layer_outputs[3 if output_attentions else 2]

            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[2],)
                if self.is_decoder:
                    all_cross_attentions = all_cross_attentions + (layer_outputs[4],)

        hidden_states = self.final_layer_norm(hidden_states)
        hidden_states = self.dropout(hidden_states, deterministic=deterministic)

        # Add last layer
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [
                    hidden_states,
                    all_hidden_states,
                    all_attentions,
                    all_cross_attentions,
                ]
                if v is not None
            )
        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            hidden_states=all_hidden_states,
            attentions=all_attentions,
            cross_attentions=all_cross_attentions,
        )


class FlaxT5PreTrainedModel(FlaxPreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """
    config_class = T5Config
    base_model_prefix = "transformer"
    module_class: nn.Module = None

    def __init__(
        self,
        config: T5Config,
        input_shape: Tuple[int] = (1, 1),
        seed: int = 0,
        dtype: jnp.dtype = jnp.float32,
        **kwargs
    ):
        module = self.module_class(config=config, dtype=dtype, **kwargs)
        super().__init__(config, module, input_shape=input_shape, seed=seed, dtype=dtype)

    def init_weights(self, rng: jax.random.PRNGKey, input_shape: Tuple) -> FrozenDict:
        # init input tensors
        input_ids = jnp.zeros(input_shape, dtype="i4")
        # make sure initialization pass will work for FlaxBartForSequenceClassificationModule
        input_ids = jax.ops.index_update(input_ids, (..., -1), self.config.eos_token_id)
        attention_mask = jnp.ones_like(input_ids)
        decoder_input_ids = input_ids
        decoder_attention_mask = jnp.ones_like(input_ids)

        batch_size, sequence_length = input_ids.shape

        params_rng, dropout_rng = jax.random.split(rng)
        rngs = {"params": params_rng, "dropout": dropout_rng}

        return self.module.init(
            rngs,
            input_ids,
            attention_mask,
            decoder_input_ids,
            decoder_attention_mask,
        )["params"]

    def __call__(
        self,
        input_ids: jnp.ndarray,
        attention_mask: Optional[jnp.ndarray] = None,
        decoder_input_ids: Optional[jnp.ndarray] = None,
        decoder_attention_mask: Optional[jnp.ndarray] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        train: bool = False,
        params: dict = None,
        dropout_rng: PRNGKey = None,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        # prepare encoder inputs
        if attention_mask is None:
            attention_mask = jnp.ones_like(input_ids)

        # prepare decoder inputs
        if decoder_attention_mask is None:
            decoder_attention_mask = jnp.ones_like(decoder_input_ids)

        # Handle any PRNG if needed
        rngs = {"dropout": dropout_rng} if dropout_rng is not None else {}

        return self.module.apply(
            {"params": params or self.params},
            input_ids=jnp.array(input_ids, dtype="i4"),
            attention_mask=jnp.array(attention_mask, dtype="i4"),
            decoder_input_ids=jnp.array(decoder_input_ids, dtype="i4"),
            decoder_attention_mask=jnp.array(decoder_attention_mask, dtype="i4"),
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=not train,
            rngs=rngs,
        )


T5_START_DOCSTRING = r"""
    The T5 model was proposed in `Exploring the Limits of Transfer Learning with a Unified Text-to-Text Transformer
    <https://arxiv.org/abs/1910.10683>`__ by Colin Raffel, Noam Shazeer, Adam Roberts, Katherine Lee, Sharan Narang,
    Michael Matena, Yanqi Zhou, Wei Li, Peter J. Liu. It's an encoder decoder transformer pre-trained in a text-to-text
    denoising generative setting.

    This model inherits from :class:`~transformers.PreTrainedModel`. Check the superclass documentation for the generic
    methods the library implements for all its model (such as downloading or saving, resizing the input embeddings,
    pruning heads etc.)

    This model is also a PyTorch `torch.nn.Module <https://pytorch.org/docs/stable/nn.html#torch.nn.Module>`__
    subclass. Use it as a regular PyTorch Module and refer to the PyTorch documentation for all matter related to
    general usage and behavior.

    Parameters:
        config (:class:`~transformers.T5Config`): Model configuration class with all the parameters of the model.
            Initializing with a config file does not load the weights associated with the model, only the
            configuration. Check out the :meth:`~transformers.PreTrainedModel.from_pretrained` method to load the model
            weights.
"""

T5_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary. T5 is a model with relative position embeddings so you
            should be able to pad the inputs on both the right and the left.

            Indices can be obtained using :class:`~transformers.T5Tokenizer`. See
            :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__` for
            detail.

            `What are input IDs? <../glossary.html#input-ids>`__

            To know more on how to prepare :obj:`input_ids` for pretraining take a look a `T5 Training
            <./t5.html#training>`__.
        attention_mask (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            `What are attention masks? <../glossary.html#attention-mask>`__
        decoder_input_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, target_sequence_length)`, `optional`):
            Indices of decoder input sequence tokens in the vocabulary.

            Indices can be obtained using :class:`~transformers.T5Tokenizer`. See
            :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__` for
            details.

            `What are decoder input IDs? <../glossary.html#decoder-input-ids>`__

            T5 uses the :obj:`pad_token_id` as the starting token for :obj:`decoder_input_ids` generation. If
            :obj:`past_key_values` is used, optionally only the last :obj:`decoder_input_ids` have to be input (see
            :obj:`past_key_values`).

            To know more on how to prepare :obj:`decoder_input_ids` for pretraining take a look at `T5 Training
            <./t5.html#training>`__.
        decoder_attention_mask (:obj:`torch.BoolTensor` of shape :obj:`(batch_size, target_sequence_length)`, `optional`):
            Default behavior: generate a tensor that ignores pad tokens in :obj:`decoder_input_ids`. Causal mask will
            also be used by default.
        head_mask (:obj:`torch.FloatTensor` of shape :obj:`(num_heads,)` or :obj:`(num_layers, num_heads)`, `optional`):
            Mask to nullify selected heads of the self-attention modules in the encoder. Mask values selected in ``[0,
            1]``:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        decoder_head_mask (:obj:`torch.FloatTensor` of shape :obj:`(num_heads,)` or :obj:`(num_layers, num_heads)`, `optional`):
            Mask to nullify selected heads of the self-attention modules in the decoder. Mask values selected in ``[0,
            1]``:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        cross_attn_head_mask (:obj:`torch.Tensor` of shape :obj:`(num_heads,)` or :obj:`(num_layers, num_heads)`, `optional`):
                Mask to nullify selected heads of the cross-attention modules in the decoder. Mask values selected in
                ``[0, 1]``:

                - 1 indicates the head is **not masked**,
                - 0 indicates the head is **masked**.

        encoder_outputs (:obj:`tuple(tuple(torch.FloatTensor)`, `optional`):
            Tuple consists of (:obj:`last_hidden_state`, :obj:`optional`: `hidden_states`, :obj:`optional`:
            `attentions`) :obj:`last_hidden_state` of shape :obj:`(batch_size, sequence_length, hidden_size)` is a
            sequence of hidden states at the output of the last layer of the encoder. Used in the cross-attention of
            the decoder.
        past_key_values (:obj:`tuple(tuple(torch.FloatTensor))` of length :obj:`config.n_layers` with each tuple having 4 tensors of shape :obj:`(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`):
            Contains precomputed key and value hidden states of the attention blocks. Can be used to speed up decoding.

            If :obj:`past_key_values` are used, the user can optionally input only the last :obj:`decoder_input_ids`
            (those that don't have their past key value states given to this model) of shape :obj:`(batch_size, 1)`
            instead of all :obj:`decoder_input_ids` of shape :obj:`(batch_size, sequence_length)`.
        inputs_embeds (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded representation.
            This is useful if you want more control over how to convert :obj:`input_ids` indices into associated
            vectors than the model's internal embedding lookup matrix.
        decoder_inputs_embeds (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, target_sequence_length, hidden_size)`, `optional`):
            Optionally, instead of passing :obj:`decoder_input_ids` you can choose to directly pass an embedded
            representation. If :obj:`past_key_values` is used, optionally only the last :obj:`decoder_inputs_embeds`
            have to be input (see :obj:`past_key_values`). This is useful if you want more control over how to convert
            :obj:`decoder_input_ids` indices into associated vectors than the model's internal embedding lookup matrix.

            If :obj:`decoder_input_ids` and :obj:`decoder_inputs_embeds` are both unset, :obj:`decoder_inputs_embeds`
            takes the value of :obj:`inputs_embeds`.

        use_cache (:obj:`bool`, `optional`):
            If set to :obj:`True`, :obj:`past_key_values` key value states are returned and can be used to speed up
            decoding (see :obj:`past_key_values`).

        output_attentions (:obj:`bool`, `optional`):
            Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under returned
            tensors for more detail.
        output_hidden_states (:obj:`bool`, `optional`):
            Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors for
            more detail.
        return_dict (:obj:`bool`, `optional`):
            Whether or not to return a :class:`~transformers.file_utils.ModelOutput` instead of a plain tuple.
"""

T5_ENCODER_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary. T5 is a model with relative position embeddings so you
            should be able to pad the inputs on both the right and the left.

            Indices can be obtained using :class:`~transformers.T5Tokenizer`. See
            :meth:`transformers.PreTrainedTokenizer.encode` and :meth:`transformers.PreTrainedTokenizer.__call__` for
            detail.

            To know more on how to prepare :obj:`input_ids` for pretraining take a look a `T5 Training
            <./t5.html#training>`__.
        attention_mask (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            `What are attention masks? <../glossary.html#attention-mask>`__
        head_mask (:obj:`torch.FloatTensor` of shape :obj:`(num_heads,)` or :obj:`(num_layers, num_heads)`, `optional`):
            Mask to nullify selected heads of the self-attention modules. Mask values selected in ``[0, 1]``:

            - 1 indicates the head is **not masked**,
            - 0 indicates the head is **masked**.

        inputs_embeds (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Optionally, instead of passing :obj:`input_ids` you can choose to directly pass an embedded representation.
            This is useful if you want more control over how to convert :obj:`input_ids` indices into associated
            vectors than the model's internal embedding lookup matrix.
        output_attentions (:obj:`bool`, `optional`):
            Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under returned
            tensors for more detail.
        output_hidden_states (:obj:`bool`, `optional`):
            Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors for
            more detail.
        return_dict (:obj:`bool`, `optional`):
            Whether or not to return a :class:`~transformers.file_utils.ModelOutput` instead of a plain tuple.
"""


@add_start_docstrings(
    "The bare T5 Model transformer outputting raw hidden-states" "without any specific head on top.",
    T5_START_DOCSTRING,
)
class FlaxT5Module(nn.Module):
    config: T5Config
    dtype: jnp.dtype = jnp.float32  # the dtype of the computation

    def setup(self):
        self.shared = nn.Embed(
            self.config.vocab_size,
            self.config.d_model,
            embedding_init=jax.nn.initializers.normal(self.config.initializer_factor * 1.0, self.dtype),
            dtype=self.dtype,
        )

        encoder_config = copy.deepcopy(self.config)
        encoder_config.is_decoder = False
        self.encoder = FlaxT5Stack(encoder_config, embed_tokens=self.shared, dtype=self.dtype)

        decoder_config = copy.deepcopy(self.config)
        decoder_config.is_decoder = True
        decoder_config.num_layers = self.config.num_decoder_layers
        self.decoder = FlaxT5Stack(decoder_config, embed_tokens=self.shared, dtype=self.dtype)

    @add_start_docstrings_to_model_forward(T5_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=Seq2SeqModelOutput, config_class=_CONFIG_FOR_DOC)
    def __call__(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        encoder_outputs=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        deterministic: bool = True,
    ):
        r"""
        Returns:

        Example::

            >>> from transformers import T5Tokenizer, T5Model

            >>> tokenizer = T5Tokenizer.from_pretrained('t5-small')
            >>> model = T5Model.from_pretrained('t5-small')

            >>> input_ids = tokenizer("Studies have been shown that owning a dog is good for you", return_tensors="pt").input_ids  # Batch size 1
            >>> decoder_input_ids = tokenizer("Studies show that", return_tensors="pt").input_ids  # Batch size 1
            >>> outputs = model(input_ids=input_ids, decoder_input_ids=decoder_input_ids)

            >>> last_hidden_states = outputs.last_hidden_state
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # Encode if needed (training, first prediction pass)
        encoder_outputs = self.encoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        # Decode
        decoder_outputs = self.decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            encoder_hidden_states=encoder_outputs[0],
            encoder_attention_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic,
        )

        if not return_dict:
            return decoder_outputs + encoder_outputs

        return Seq2SeqModelOutput(
            last_hidden_state=decoder_outputs.last_hidden_state,
            past_key_values=decoder_outputs.past_key_values,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
            encoder_last_hidden_state=encoder_outputs.last_hidden_state,
            encoder_hidden_states=encoder_outputs.hidden_states,
            encoder_attentions=encoder_outputs.attentions,
        )


class FlaxT5Model(FlaxT5PreTrainedModel):
    module_class = FlaxT5Module


@add_start_docstrings("""T5 Model with a `language modeling` head on top. """, T5_START_DOCSTRING)
class FlaxT5ForConditionalGenerationModule(nn.Module):
    config: T5Config

    def setup(self):
        self.model_dim = self.config.d_model

        self.shared = nn.Embed(self.config.vocab_size, self.config.d_model)

        encoder_config = copy.deepcopy(self.config)
        encoder_config.is_decoder = False
        encoder_config.use_cache = False
        encoder_config.is_encoder_decoder = False
        self.encoder = FlaxT5Stack(encoder_config, self.shared)

        decoder_config = copy.deepcopy(self.config)
        decoder_config.is_decoder = True
        decoder_config.is_encoder_decoder = False
        decoder_config.num_layers = self.config.num_decoder_layers
        self.decoder = FlaxT5Stack(decoder_config, self.shared)

        self.lm_head = nn.Dense(self.config.vocab_size, use_bias=False)

    def get_encoder(self):
        return self.encoder

    def get_decoder(self):
        return self.decoder

    @add_start_docstrings_to_model_forward(T5_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=Seq2SeqLMOutput, config_class=_CONFIG_FOR_DOC)
    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        head_mask=None,
        decoder_head_mask=None,
        cross_attn_head_mask=None,
        encoder_outputs=None,
        past_key_values=None,
        inputs_embeds=None,
        decoder_inputs_embeds=None,
        labels=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
    ):
        r"""
        labels (:obj:`torch.LongTensor` of shape :obj:`(batch_size,)`, `optional`):
            Labels for computing the sequence classification/regression loss. Indices should be in :obj:`[-100, 0, ...,
            config.vocab_size - 1]`. All labels set to ``-100`` are ignored (masked), the loss is only computed for
            labels in ``[0, ..., config.vocab_size]``

        Returns:

        Examples::

            >>> from transformers import T5Tokenizer, T5ForConditionalGeneration

            >>> tokenizer = T5Tokenizer.from_pretrained('t5-small')
            >>> model = T5ForConditionalGeneration.from_pretrained('t5-small')

            >>> input_ids = tokenizer('The <extra_id_0> walks in <extra_id_1> park', return_tensors='pt').input_ids
            >>> labels = tokenizer('<extra_id_0> cute dog <extra_id_1> the <extra_id_2> </s>', return_tensors='pt').input_ids
            >>> outputs = model(input_ids=input_ids, labels=labels)
            >>> loss = outputs.loss
            >>> logits = outputs.logits

            >>> input_ids = tokenizer("summarize: studies have shown that owning a dog is good for you ", return_tensors="pt").input_ids  # Batch size 1
            >>> outputs = model.generate(input_ids)
        """
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # Encode if needed (training, first prediction pass)
        if encoder_outputs is None:
            # Convert encoder inputs in embeddings if needed
            encoder_outputs = self.encoder(
                input_ids=input_ids,
                attention_mask=attention_mask,
                inputs_embeds=inputs_embeds,
                head_mask=head_mask,
                output_attentions=output_attentions,
                output_hidden_states=output_hidden_states,
                return_dict=return_dict,
            )
        elif return_dict and not isinstance(encoder_outputs, BaseModelOutput):
            encoder_outputs = BaseModelOutput(
                last_hidden_state=encoder_outputs[0],
                hidden_states=encoder_outputs[1] if len(encoder_outputs) > 1 else None,
                attentions=encoder_outputs[2] if len(encoder_outputs) > 2 else None,
            )

        hidden_states = encoder_outputs[0]

        # if self.model_parallel:
        #     torch.cuda.set_device(self.decoder.first_device)

        if labels is not None and decoder_input_ids is None and decoder_inputs_embeds is None:
            # get decoder inputs from shifting lm labels to the right
            decoder_input_ids = self._shift_right(labels)

        # If decoding with past key value states, only the last tokens
        # should be given as an input
        if past_key_values is not None:
            assert labels is None, "Decoder should not use cached key value states when training."
            if decoder_input_ids is not None:
                decoder_input_ids = decoder_input_ids[:, -1:]
            if decoder_inputs_embeds is not None:
                decoder_inputs_embeds = decoder_inputs_embeds[:, -1:]

        # Decode
        decoder_outputs = self.decoder(
            input_ids=decoder_input_ids,
            attention_mask=decoder_attention_mask,
            inputs_embeds=decoder_inputs_embeds,
            past_key_values=past_key_values,
            encoder_hidden_states=hidden_states,
            encoder_attention_mask=attention_mask,
            head_mask=decoder_head_mask,
            cross_attn_head_mask=cross_attn_head_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = decoder_outputs[0]

        # Set device for model parallelism
        # if self.model_parallel:
        #     torch.cuda.set_device(self.encoder.first_device)
        #     self.lm_head = self.lm_head.to(self.encoder.first_device)
        #     sequence_output = sequence_output.to(self.lm_head.weight.device)

        if self.config.tie_word_embeddings:
            # Rescale output before projecting on vocab
            # See https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/transformer/transformer.py#L586
            sequence_output = sequence_output * (self.model_dim ** -0.5)

        lm_logits = self.lm_head(sequence_output)

        loss = None
        # if labels is not None:
        #     loss_fct = CrossEntropyLoss(ignore_index=-100)
        #     loss = loss_fct(lm_logits.view(-1, lm_logits.size(-1)), labels.view(-1))
        #     # TODO(thom): Add z_loss https://github.com/tensorflow/mesh/blob/fa19d69eafc9a482aff0b59ddd96b025c0cb207d/mesh_tensorflow/layers.py#L666

        if not return_dict:
            output = (lm_logits,) + decoder_outputs[1:] + encoder_outputs
            return ((loss,) + output) if loss is not None else output

        return Seq2SeqLMOutput(
            loss=loss,
            logits=lm_logits,
            past_key_values=decoder_outputs.past_key_values,
            decoder_hidden_states=decoder_outputs.hidden_states,
            decoder_attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
            encoder_last_hidden_state=encoder_outputs.last_hidden_state,
            encoder_hidden_states=encoder_outputs.hidden_states,
            encoder_attentions=encoder_outputs.attentions,
        )


class FlaxT5ForConditionalGeneration(FlaxT5PreTrainedModel):
    module_class = FlaxT5ForConditionalGenerationModule
