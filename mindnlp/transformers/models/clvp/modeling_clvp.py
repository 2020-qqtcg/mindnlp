# coding=utf-8
# Copyright 2023 The HuggingFace Team. All rights reserved.
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

"""MindSpore CLVP model."""

import copy
import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, Union

import mindspore
from mindspore.common.initializer import initializer, Normal
import mindnlp.core.nn.functional as F
from mindnlp.core import nn, ops

from ...activations import ACT2FN
from ...generation import GenerationConfig
from ...modeling_attn_mask_utils import _prepare_4d_attention_mask, _prepare_4d_causal_attention_mask
from ...modeling_outputs import (
    BaseModelOutput,
    BaseModelOutputWithPastAndCrossAttentions,
    BaseModelOutputWithPooling,
    CausalLMOutputWithCrossAttentions,
)
from ...modeling_utils import PreTrainedModel, SequenceSummary
from ...ms_utils import Conv1D
from ....utils import (
    ModelOutput,
    logging,
)
from .configuration_clvp import (
    ClvpConfig,
    ClvpDecoderConfig,
    ClvpEncoderConfig,
)


logger = logging.get_logger(__name__)

_CHECKPOINT_FOR_DOC = "susnato/clvp_dev"


# Copied from transformers.models.clip.modeling_clip.contrastive_loss
def contrastive_loss(logits: mindspore.Tensor) -> mindspore.Tensor:
    return nn.functional.cross_entropy(logits, ops.arange(len(logits)))


# Copied from transformers.models.clip.modeling_clip.clip_loss with clip->clvp, image_loss->speech_loss
def clvp_loss(similarity: mindspore.Tensor) -> mindspore.Tensor:
    caption_loss = contrastive_loss(similarity)
    speech_loss = contrastive_loss(similarity.t())
    return (caption_loss + speech_loss) / 2.0


# Copied from transformers.models.llama.modeling_llama.rotate_half
def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2:]
    return ops.cat((-x2, x1), dim=-1)


def apply_rotary_pos_emb(q, k, v, cos, sin, position_ids, unsqueeze_dim=1):
    """Applies Rotary Position Embedding to the query and key tensors.

    Args:
        q (`mindspore.Tensor`): The query tensor.
        k (`mindspore.Tensor`): The key tensor.
        cos (`mindspore.Tensor`): The cosine part of the rotary embedding.
        sin (`mindspore.Tensor`): The sine part of the rotary embedding.
        position_ids (`mindspore.Tensor`):
            The position indices of the tokens corresponding to the query and key tensors. For example, this can be
            used to pass offsetted position ids when working with a KV-cache.
        unsqueeze_dim (`int`, *optional*, defaults to 1):
            The 'unsqueeze_dim' argument specifies the dimension along which to unsqueeze cos[position_ids] and
            sin[position_ids] so that they can be properly broadcasted to the dimensions of q and k. For example, note
            that cos[position_ids] and sin[position_ids] have the shape [batch_size, seq_len, head_dim]. Then, if q and
            k have the shape [batch_size, heads, seq_len, head_dim], then setting unsqueeze_dim=1 makes
            cos[position_ids] and sin[position_ids] broadcastable to the shapes of q and k. Similarly, if q and k have
            the shape [batch_size, seq_len, heads, head_dim], then set unsqueeze_dim=2.
    Returns:
        `tuple(mindspore.Tensor)` comprising of the query and key tensors rotated using the Rotary Position Embedding.
    """
    cos = cos[position_ids].unsqueeze(unsqueeze_dim)
    sin = sin[position_ids].unsqueeze(unsqueeze_dim)
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    v_embed = (v * cos) + (rotate_half(v) * sin)
    return q_embed, k_embed, v_embed


def _pad_extra_bos_eos_tokens(
    input_ids,
    attention_mask=None,
    pad_token_id=0,
    bos_token_id=255,
    eos_token_id=0,
    add_bos_token=True,
    add_eos_token=True,
):
    """
    This method adds extra bos and eos tokens to input_ids and accordingly modifies the attention_mask which is used in
    `ClvpConditioningEncoder` and the generation loop of the `ClvpModelForConditionalGeneration`.
    """

    # add the bos token at the beginning
    if add_bos_token:
        input_ids = ops.pad(input_ids, (1, 0), value=bos_token_id)
        attention_mask = (
            ops.pad(attention_mask, (1, 0),
                    value=1) if attention_mask is not None else attention_mask
        )

    modified_input_ids = input_ids
    if add_eos_token:
        modified_input_ids = ops.zeros(
            (input_ids.shape[0], input_ids.shape[1] + 1), dtype=input_ids.dtype
        )
        for i, each_input_id in enumerate(input_ids):
            # locate where the valid tokens end and then add the eos token
            each_input_id = mindspore.tensor(each_input_id)
            pad_token_id = mindspore.tensor(pad_token_id)
            if ops.isin(each_input_id, pad_token_id).sum():
                pos = ops.where(each_input_id == pad_token_id,
                                each_input_id, pad_token_id)[0].min()
                modified_input_ids[i] = ops.concatenate(
                    [each_input_id[:pos], mindspore.tensor(
                        [eos_token_id]), each_input_id[pos:]]
                )
            else:
                # if there are no pad tokens present, then add eos to the end
                modified_input_ids[i] = ops.pad(
                    each_input_id, (0, 1), value=eos_token_id)
        attention_mask = (
            ops.pad(attention_mask, (1, 0),
                    value=1) if attention_mask is not None else attention_mask
        )

    return modified_input_ids, attention_mask


@dataclass
class ClvpEncoderOutput(ModelOutput):
    """
    Base class for CLVP encoder's outputs that contains a pooling of the last hidden states as well as a projection
    output (a linear layer on top of the pooled output).

    Args:
        embeds (`mindspore.Tensor` of shape `(batch_size, output_dim)`, *optional*, returned when model is initialized with `with_projection=True`):
            The embeddings obtained by applying the projection layer to the pooler_output.
        last_hidden_state (`mindspore.Tensor` of shape `(batch_size, sequence_length, hidden_size)`):
            The hidden state of the last layer of the model.
        pooler_output (`mindspore.Tensor` of shape `(batch_size, hidden_size)`):
            Pooled output of the `last_hidden_state`.
        hidden_states (`tuple(mindspore.Tensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `mindspore.Tensor` (one for the output of the embeddings, if the model has an embedding layer, +
            one for the output of each layer) of shape `(batch_size, sequence_length, hidden_size)`. Hidden-states of
            the model at the output of each layer plus the optional initial embedding outputs.
        attentions (`tuple(mindspore.Tensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `mindspore.Tensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`. Attentions weights after the attention softmax, used to compute the weighted average in
            the self-attention heads.
    """

    embeds: Optional[mindspore.Tensor] = None
    last_hidden_state: mindspore.Tensor = None
    pooler_output: Optional[mindspore.Tensor] = None
    hidden_states: Optional[Tuple[mindspore.Tensor]] = None
    attentions: Optional[Tuple[mindspore.Tensor]] = None


@dataclass
class ClvpOutput(ModelOutput):
    """
    Args:
        loss (`mindspore.Tensor` of shape `(1,)`, *optional*, returned when `return_loss` is `True`):
            Contrastive loss for speech-text similarity.
        speech_ids (`mindspore.Tensor`, *optional*):
            speech_ids (or speech candidates) generated by the `ClvpForCausalLM` model.
        logits_per_speech (`mindspore.Tensor` of shape `(speech_batch_size, text_batch_size)`):
            The scaled dot product scores between `speech_embeds` and `text_embeds`. This represents the speech-text
            similarity scores.
        logits_per_text (`mindspore.Tensor` of shape `(text_batch_size, speech_batch_size)`):
            The scaled dot product scores between `text_embeds` and `speech_embeds`. This represents the text-speech
            similarity scores.
        text_embeds (`mindspore.Tensor` of shape `(batch_size, output_dim`):
            The text embeddings obtained by applying the projection layer to the pooled output of the text encoder
            model.
        speech_embeds (`mindspore.Tensor` of shape `(batch_size, output_dim`):
            The speech embeddings obtained by applying the projection layer to the pooled output of the speech encoder
            model.
        text_model_output (`BaseModelOutputWithPooling`):
            The pooled output of the `last_hidden_state` of the text encoder Model.
        speech_model_output (`BaseModelOutputWithPooling`):
            The pooled output of the `last_hidden_state` of the speech encoder Model.
        decoder_hidden_states (`mindspore.Tensor`, *optional*):
            The hidden states of the decoder model.
        text_encoder_hidden_states (`mindspore.Tensor`, *optional*):
            The hidden states of the text encoder model.
        speech_encoder_hidden_states (`mindspore.Tensor`, *optional*):
            The hidden states of the speech encoder model.
    """

    loss: Optional[mindspore.Tensor] = None
    speech_ids: Optional[mindspore.Tensor] = None
    logits_per_speech: mindspore.Tensor = None
    logits_per_text: mindspore.Tensor = None
    text_embeds: mindspore.Tensor = None
    speech_embeds: mindspore.Tensor = None
    text_model_output: BaseModelOutputWithPooling = None
    speech_model_output: BaseModelOutputWithPooling = None
    decoder_hidden_states: mindspore.Tensor = None
    text_encoder_hidden_states: mindspore.Tensor = None
    speech_encoder_hidden_states: mindspore.Tensor = None


# Copied from transformers.models.llama.modeling_llama.LlamaRMSNorm with Llama->Clvp
class ClvpRMSNorm(nn.Module):
    def __init__(self, hidden_size, eps=1e-6):
        """
        ClvpRMSNorm is equivalent to T5LayerNorm
        """
        super().__init__()
        self.weight = nn.Parameter(ops.ones(hidden_size))
        self.variance_epsilon = eps

    def forward(self, hidden_states):
        input_dtype = hidden_states.dtype
        hidden_states = hidden_states.to(mindspore.float32)
        variance = hidden_states.pow(2).mean(-1, keep_dims=True)
        hidden_states = hidden_states * \
            ops.rsqrt(variance + self.variance_epsilon)
        return self.weight * hidden_states.to(input_dtype)

    def extra_repr(self):
        return f"{tuple(self.weight.shape)}, eps={self.variance_epsilon}"


class ClvpRotaryPositionalEmbedding(nn.Module):
    """
    Rotary Position Embedding Class for CLVP. It was proposed in the paper 'ROFORMER: ENHANCED TRANSFORMER WITH ROTARY
    POSITION EMBEDDING', Please see https://arxiv.org/pdf/2104.09864v1.pdf .
    """

    def __init__(self, config):
        super().__init__()
        dim = max(config.projection_dim //
                  (config.num_attention_heads * 2), 32)
        inv_freq = 1.0 / \
            (10000 ** (ops.arange(0, dim, 2, dtype=mindspore.int64).float() / dim))

        self.register_buffer("inv_freq", inv_freq)
        self.cached_sequence_length = None
        self.cached_rotary_positional_embedding = None

    def forward(self, hidden_states: mindspore.Tensor) -> mindspore.Tensor:
        sequence_length = hidden_states.shape[1]

        if sequence_length == self.cached_sequence_length and self.cached_rotary_positional_embedding is not None:
            return self.cached_rotary_positional_embedding

        self.cached_sequence_length = sequence_length
        time_stamps = ops.arange(sequence_length).type_as(self.inv_freq)
        freqs = ops.einsum("i,j->ij", time_stamps, self.inv_freq)
        embeddings = ops.cat((freqs, freqs), dim=-1)

        self.cached_rotary_positional_embedding = embeddings.unsqueeze(0)
        return self.cached_rotary_positional_embedding


class ClvpSelfAttention(nn.Module):
    """
    Multi-headed attention to combine Absolute and Rotary Positional Embeddings into a single Attention module.
    """

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.embed_dim = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.embed_dim // self.num_heads
        if self.head_dim * self.num_heads != self.embed_dim:
            raise ValueError(
                f"embed_dim must be divisible by num_heads (got `embed_dim`: {self.embed_dim} and `num_heads`:"
                f" {self.num_heads})."
            )
        self.scale = self.head_dim**-0.5
        self.dropout = config.attention_dropout

        if hasattr(config, "max_position_embeddings"):
            max_positions = config.max_position_embeddings
            bias = ops.tril(
                ops.ones((max_positions, max_positions), dtype=mindspore.bool_))
            bias = bias.view(1, 1, max_positions, max_positions)
            self.register_buffer("bias", bias, persistent=False)

        self.k_proj = nn.Linear(
            self.embed_dim, self.embed_dim, bias=config.use_attention_bias)
        self.v_proj = nn.Linear(
            self.embed_dim, self.embed_dim, bias=config.use_attention_bias)
        self.q_proj = nn.Linear(
            self.embed_dim, self.embed_dim, bias=config.use_attention_bias)
        self.out_proj = nn.Linear(self.embed_dim, self.embed_dim)

    # Copied from transformers.models.clip.modeling_clip.CLIPAttention._shape
    def _shape(self, tensor: mindspore.Tensor, seq_len: int, bsz: int):
        return tensor.view(bsz, seq_len, self.num_heads, self.head_dim).swapaxes(1, 2)

    def forward(
        self,
        hidden_states: mindspore.Tensor,
        rotary_pos_emb: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        past_key_value: Optional[Tuple[mindspore.Tensor]] = None,
        use_cache: Optional[bool] = False,
        head_mask: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = False,
    ) -> Tuple[mindspore.Tensor, Optional[mindspore.Tensor], Optional[Tuple[mindspore.Tensor]]]:
        # Raise error when position_ids is None but rotary_pos_emb is provided, because we need that when applying
        # rotary_pos_emb to query and key states.
        if rotary_pos_emb is not None and position_ids is None:
            raise ValueError(
                "`position_ids` must be provided when `rotary_pos_emb` is not None.")

        bsz, _, embed_dim = hidden_states.shape

        # get query proj
        query_states = self._shape(self.q_proj(
            hidden_states), -1, bsz) * self.scale
        key_states = self._shape(self.k_proj(hidden_states), -1, bsz)
        value_states = self._shape(self.v_proj(hidden_states), -1, bsz)

        if past_key_value is not None:
            past_key, past_value = past_key_value
            key_states = ops.cat((past_key, key_states), dim=-2)
            value_states = ops.cat((past_value, value_states), dim=-2)

        if use_cache is True:
            present = (key_states, value_states)
        else:
            present = None

        if rotary_pos_emb is not None:
            rotary_emb_dim = rotary_pos_emb.shape[-1]

            # Partial rotary embedding
            query_rot, query_pass = (
                query_states[..., :rotary_emb_dim],
                query_states[..., rotary_emb_dim:],
            )
            key_rot, key_pass = (
                key_states[..., :rotary_emb_dim],
                key_states[..., rotary_emb_dim:],
            )
            value_rot, value_pass = (
                value_states[..., :rotary_emb_dim],
                value_states[..., rotary_emb_dim:],
            )

            cos, sin = rotary_pos_emb.cos().squeeze(0), rotary_pos_emb.sin().squeeze(0)
            query_rot, key_rot, value_rot = apply_rotary_pos_emb(
                query_rot, key_rot, value_rot, cos, sin, position_ids)

            # [batch_size, num_heads, seq_length, head_dim]
            query_states = ops.cat((query_rot, query_pass), dim=-1)
            key_states = ops.cat((key_rot, key_pass), dim=-1)
            value_states = ops.cat((value_rot, value_pass), dim=-1)

        tgt_len = query_states.shape[2]
        src_len = key_states.shape[2]
        attn_weights = ops.matmul(query_states, key_states.swapaxes(2, 3))

        if attention_mask is not None:
            if attention_mask.shape != (bsz, 1, tgt_len, src_len):
                raise ValueError(
                    f"Attention mask should be of size {(bsz, 1, tgt_len, src_len)}, but is {attention_mask.shape}"
                )
            attn_weights = attn_weights + attention_mask

        attn_weights = F.softmax(attn_weights, dim=-1)

        # Mask heads if we want to
        if head_mask is not None:
            attn_weights = attn_weights * head_mask

        attn_probs = F.dropout(
            attn_weights, p=self.dropout, training=self.training)
        attn_output = ops.matmul(attn_probs, value_states)

        if attn_output.shape != (bsz, self.num_heads, tgt_len, self.head_dim):
            raise ValueError(
                f"`attn_output` should be of size {(bsz, self.num_heads, tgt_len, self.head_dim)}, but is"
                f" {attn_output.shape}"
            )

        attn_output = attn_output.swapaxes(1, 2)
        attn_output = attn_output.reshape(bsz, tgt_len, self.embed_dim)

        attn_output = self.out_proj(attn_output)

        if not output_attentions:
            attn_weights = None

        return attn_output, present, attn_weights


class ClvpGatedLinearUnit(nn.Module):
    """
    `ClvpGatedLinearUnit` uses the second half of the `hidden_states` to act as a gate for the first half of the
    `hidden_states` which controls the flow of data from the first of the tensor.
    """

    def __init__(self, config):
        super().__init__()
        self.activation_fn = ACT2FN[config.hidden_act]
        self.proj = nn.Linear(config.hidden_size, config.intermediate_size * 2)

    def forward(self, hidden_states: mindspore.Tensor) -> mindspore.Tensor:
        hidden_states, gate = self.proj(hidden_states).chunk(2, axis=-1)
        return hidden_states * self.activation_fn(gate)


class ClvpEncoderMLP(nn.Module):
    """
    This MLP is used in CLVP speech or text encoder models.
    """

    def __init__(self, config):
        super().__init__()
        self.config = config

        self.fc1 = ClvpGatedLinearUnit(config)
        self.fc2 = nn.Linear(config.intermediate_size, config.hidden_size)
        self.dropout_layer = nn.Dropout(config.dropout)

    def forward(self, hidden_states: mindspore.Tensor) -> mindspore.Tensor:
        hidden_states = self.fc1(hidden_states)
        hidden_states = self.dropout_layer(hidden_states)
        hidden_states = self.fc2(hidden_states)
        return hidden_states


class ClvpEncoderLayer(nn.Module):
    def __init__(self, config: ClvpConfig):
        super().__init__()
        self.config = config
        self.embed_dim = config.hidden_size
        self.self_attn = ClvpSelfAttention(config)
        self.mlp = ClvpEncoderMLP(config)

        self.input_rmsnorm = ClvpRMSNorm(
            self.embed_dim, eps=config.layer_norm_eps)
        self.post_attention_rmsnorm = ClvpRMSNorm(
            self.embed_dim, eps=config.layer_norm_eps)

    def forward(
        self,
        hidden_states: mindspore.Tensor,
        rotary_pos_emb: mindspore.Tensor,
        attention_mask: mindspore.Tensor,
        position_ids: mindspore.Tensor,
        output_attentions: Optional[bool] = False,
    ) -> Tuple[mindspore.Tensor]:
        """
        Args:
            hidden_states (`mindspore.Tensor` of shape `(batch, seq_len, embed_dim)`):
                input to the layer.
            rotary_pos_emb (`mindspore.Tensor`):
                rotary position embeddings generated by `ClvpRotaryPositionalEmbedding` module.
            attention_mask (`mindspore.Tensor` of shape `(batch, 1, tgt_len, src_len)`):
                attention mask where padding elements are indicated by very large negative values.
            position_ids (`mindspore.Tensor`):
                Denotes position ids of the input tokens.
            output_attentions (`bool`, *optional*, defaults to `False`):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
        """
        residual = hidden_states

        hidden_states = self.input_rmsnorm(hidden_states)

        attention_outputs = self.self_attn(
            hidden_states=hidden_states,
            rotary_pos_emb=rotary_pos_emb,
            attention_mask=attention_mask,
            position_ids=position_ids,
            output_attentions=output_attentions,
        )

        hidden_states = attention_outputs[0]

        hidden_states = residual + hidden_states

        residual = hidden_states
        hidden_states = self.post_attention_rmsnorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if output_attentions:
            outputs += (attention_outputs[-1],)

        return outputs


# Copied from transformers.models.gpt2.modeling_gpt2.GPT2MLP with GPT2->ClvpDecoderMLP
class ClvpDecoderMLP(nn.Module):
    def __init__(self, intermediate_size, config):
        super().__init__()
        embed_dim = config.hidden_size
        self.c_fc = Conv1D(intermediate_size, embed_dim)
        self.c_proj = Conv1D(embed_dim, intermediate_size)
        self.act = ACT2FN[config.activation_function]
        self.dropout = nn.Dropout(config.resid_pdrop)

    def forward(self, hidden_states: Optional[Tuple[mindspore.Tensor]]) -> mindspore.Tensor:
        hidden_states = self.c_fc(hidden_states)
        hidden_states = self.act(hidden_states)
        hidden_states = self.c_proj(hidden_states)
        hidden_states = self.dropout(hidden_states)
        return hidden_states


class ClvpDecoderLayer(nn.Module):
    def __init__(self, config):
        super().__init__()
        hidden_size = config.hidden_size
        inner_dim = config.n_inner if config.n_inner is not None else 4 * hidden_size

        self.input_layernorm = nn.LayerNorm(
            hidden_size, eps=config.layer_norm_epsilon)
        self.attn = ClvpSelfAttention(config)
        self.post_attention_layernorm = nn.LayerNorm(
            hidden_size, eps=config.layer_norm_epsilon)

        self.mlp = ClvpDecoderMLP(inner_dim, config)

    def forward(
        self,
        hidden_states: Optional[Tuple[mindspore.Tensor]],
        past_key_value: Optional[Tuple[mindspore.Tensor]] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        head_mask: Optional[mindspore.Tensor] = None,
        use_cache: Optional[bool] = False,
        output_attentions: Optional[bool] = False,
    ) -> Union[Tuple[mindspore.Tensor], Optional[Tuple[mindspore.Tensor, Tuple[mindspore.Tensor, ...]]]]:
        residual = hidden_states
        hidden_states = self.input_layernorm(hidden_states)
        attn_outputs = self.attn(
            hidden_states,
            past_key_value=past_key_value,
            attention_mask=attention_mask,
            position_ids=position_ids,
            head_mask=head_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
        )
        attn_output = attn_outputs[0]
        outputs = attn_outputs[1:]
        # residual connection
        hidden_states = attn_output + residual

        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        feed_forward_hidden_states = self.mlp(hidden_states)
        # residual connection
        hidden_states = residual + feed_forward_hidden_states

        if use_cache:
            outputs = (hidden_states,) + outputs
        else:
            outputs = (hidden_states,) + outputs[1:]

        return outputs


class ClvpConditioningEncoder(nn.Module):
    """
    This class processes the log-mel spectrograms(extracted by the Feature Extractor) and text tokens(produced by the
    tokenizer) as inputs for the decoder model.

    First each log-mel spectrogram is processed into a single vector which captures valuable characteristics from each
    of them, then the text tokens are converted into token embeddings and position embeddings are added afterwards.
    Both of these vectors are concatenated and then passed to the decoder model.

    The text tokens helps to incorporate the "text information" and the log-mel spectrogram is used to specify the
    "voice characteristics" into the generated mel tokens.
    """

    def __init__(self, config: ClvpConfig):
        super().__init__()

        self.text_config = config.text_config
        self.decoder_config = config.decoder_config

        self.text_token_embedding = nn.Embedding(
            self.text_config.vocab_size, self.decoder_config.hidden_size)
        self.text_position_embedding = nn.Embedding(
            self.decoder_config.max_text_tokens, self.decoder_config.hidden_size
        )

        self.mel_conv = nn.Conv1d(
            self.decoder_config.feature_size, self.decoder_config.hidden_size, kernel_size=1)

        # define group norms to be used before each attention layer
        num_groups = self.compute_groupnorm_groups(
            self.decoder_config.hidden_size)
        self.group_norms = nn.ModuleList(
            [
                nn.GroupNorm(
                    num_groups, self.decoder_config.hidden_size, eps=1e-5, affine=True)
                for _ in range(self.decoder_config.num_mel_attn_blocks)
            ]
        )

        # define the attention layers
        self.mel_attn_blocks = nn.ModuleList(
            [ClvpSelfAttention(self.decoder_config) for _ in range(
                self.decoder_config.num_mel_attn_blocks)]
        )

        self.gradient_checkpointing = False

    def compute_groupnorm_groups(self, channels: int, groups: int = 32):
        """
        Calculates the value of `num_groups` for nn.GroupNorm. This logic is taken from the official tortoise
        repository. link :
        https://github.com/neonbjb/tortoise-tts/blob/4003544b6ff4b68c09856e04d3eff9da26d023c2/tortoise/models/arch_util.py#L26
        """
        if channels <= 16:
            groups = 8
        elif channels <= 64:
            groups = 16
        while channels % groups != 0:
            groups = int(groups / 2)

        if groups <= 2:
            raise ValueError(
                f"Number of groups for the GroupNorm must be greater than 2, but it is {groups}."
                f"Please consider using a different `hidden_size`"
            )

        return groups

    def forward(
        self,
        input_features: mindspore.Tensor,
        input_ids: Optional[mindspore.Tensor] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
    ):
        # process text
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            batch_size, seq_length = input_ids.shape
        elif inputs_embeds is not None:
            batch_size, seq_length = inputs_embeds.shape[:-1]
        else:
            raise ValueError(
                "You have to specify either input_ids or inputs_embeds")

        # construct attention mask if not given
        if attention_mask is None:
            attention_mask = ops.ones(
                (batch_size, seq_length), dtype=mindspore.int64)

        # We add bos and eos input_ids in the modeling file instead of the tokenizer file to keep the logic simple
        # This logic is specific to ClvpConditioningEncoder and not used by other modules.
        input_ids, attention_mask = _pad_extra_bos_eos_tokens(
            input_ids,
            attention_mask,
            bos_token_id=self.text_config.bos_token_id,
            eos_token_id=self.text_config.eos_token_id,
        )

        inputs_embeds = self.text_token_embedding(input_ids)
        position_ids = attention_mask.cumsum(-1) - 1
        position_embeds = self.text_position_embedding(position_ids)
        text_embeds = inputs_embeds + position_embeds

        # process each log-mel spectrogram into a single vector
        mel_spec = self.mel_conv(input_features)

        for i, mel_attn_block in enumerate(self.mel_attn_blocks):
            residual_mel_spec = mel_spec.swapaxes(1, 2)

            mel_spec = self.group_norms[i](mel_spec).swapaxes(1, 2)
            mel_spec = mel_attn_block(mel_spec)[0] + residual_mel_spec
            mel_spec = mel_spec.swapaxes(1, 2)

        mel_spec = mel_spec[:, :, 0]
        mel_spec = mel_spec.unsqueeze(1)

        # repeat if there is either (1 text vs N audios) or (N texts vs 1 audio)
        if text_embeds.shape[0] == 1 and mel_spec.shape[0] != 1:
            text_embeds = text_embeds.tile((mel_spec.shape[0], 1, 1))
        elif text_embeds.shape[0] != 1 and mel_spec.shape[0] == 1:
            mel_spec = mel_spec.tile((text_embeds.shape[0], 1, 1))
        # If there is N texts and M audios we will raise error since the number of text and audio must be same.
        elif text_embeds.shape[0] != mel_spec.shape[0]:
            raise ValueError(
                f"The number of texts and number of audios must be same. "
                f"Found {text_embeds.shape[0]} texts vs {mel_spec.shape[0]} audios"
            )

        return ops.concat([mel_spec, text_embeds], dim=1)


class ClvpPreTrainedModel(PreTrainedModel):
    """
    An abstract class to handle weights initialization and a simple interface for downloading and loading pretrained
    models.
    """

    config_class = ClvpConfig
    base_model_prefix = "clvp"
    supports_gradient_checkpointing = True
    _skip_keys_device_placement = "past_key_values"

    def _init_weights(self, cell):
        """Initialize the weights"""
        factor = self.config.initializer_factor
        if isinstance(cell, nn.Embedding):
            cell.weight.set_data(initializer(Normal(mean=0.0, sigma=factor * 0.02),
                                             cell.weight.shape, cell.weight.dtype))
        elif isinstance(cell, (nn.Linear, Conv1D, nn.Conv1d)):
            cell.weight.set_data(initializer(Normal(mean=0.0, sigma=factor * 0.02),
                                             cell.weight.shape, cell.weight.dtype))
            if cell.bias is not None:
                cell.bias.set_data(
                    initializer(
                        "zeros",
                        cell.bias.shape,
                        cell.bias.dtype,
                    )
                )
        elif isinstance(cell, ClvpEncoderMLP):
            factor = self.config.initializer_factor
            in_proj_std = (cell.config.hidden_size**-0.5) * \
                ((2 * cell.config.num_hidden_layers) ** -0.5) * factor
            fc_std = (2 * cell.config.hidden_size) ** -0.5 * factor
            (cell.fc1.proj.weight if getattr(cell.fc1, "proj") else cell.fc1.weight).set_data(initializer(Normal(fc_std), (cell.fc1.proj.weight if getattr(cell.fc1, "proj") else cell.fc1.weight).shape,
                                                                                                          (cell.fc1.proj.weight if getattr(cell.fc1, "proj") else cell.fc1.weight).dtype))
            cell.fc2.weight.set_data(initializer(Normal(in_proj_std),
                                                 cell.fc2.weight.shape, cell.fc2.weight.dtype))
        elif isinstance(cell, ClvpEncoder):
            config = self.config.text_config if hasattr(
                self.config, "text_config") else self.config
            factor = config.initializer_factor
            cell.projection.weight.set_data(initializer(Normal(factor * (config.hidden_size**-0.5)),
                                                        cell.projection.weight.shape, cell.projection.weight.dtype))
        elif isinstance(cell, ClvpConditioningEncoder):
            cell.mel_conv.weight.set_data(initializer(Normal(factor),
                                                      cell.mel_conv.weight.shape, cell.mel_conv.weight.dtype))
            cell.mel_conv.bias.set_data(initializer(
                'zeros', cell.mel_conv.bias.shape, cell.mel_conv.bias.dtype))
        elif isinstance(cell, ClvpForCausalLM):
            for name, p in cell.parameters_and_names():
                if name == "c_proj.weight":
                    p.set_data(initializer(Normal
                                           (self.config.initializer_range /
                                            math.sqrt(2 * self.config.num_hidden_layers)),
                                           p.shape, p.dtype)
                               )
        if isinstance(cell, nn.LayerNorm):
            cell.weight.set_data(initializer(
                'ones', cell.weight.shape, cell.weight.dtype))
            cell.bias.set_data(initializer(
                'zeros', cell.bias.shape, cell.bias.dtype))


class ClvpEncoder(ClvpPreTrainedModel):
    """
    Transformer encoder consisting of `config.num_hidden_layers` self attention layers. Each layer is a
    [`ClvpEncoderLayer`].

    Args:
        config: ClvpConfig
    """

    def __init__(self, config: ClvpConfig):
        super().__init__(config)

        self.config = config
        self.token_embedding = nn.Embedding(
            config.vocab_size, config.hidden_size)
        self.rotary_pos_emb = ClvpRotaryPositionalEmbedding(
            config) if config.use_rotary_embedding else None
        self.layers = nn.ModuleList(
            [ClvpEncoderLayer(config) for _ in range(config.num_hidden_layers)])

        self.sequence_summary = SequenceSummary(config)
        self.final_layer_norm = nn.LayerNorm(
            config.hidden_size, eps=config.layer_norm_eps)

        self.projection = nn.Linear(
            config.hidden_size, config.projection_dim, bias=False)
        self.gradient_checkpointing = False

        self.post_init()

    def get_input_embeddings(self):
        return self.token_embedding

    def set_input_embeddings(self, value):
        self.token_embedding = value

    def forward(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutput]:
        r"""
        Args:
            input_ids (`mindspore.Tensor` of shape `(batch_size, input_ids_length)`, *optional*):
                Indices of input sequence tokens in the vocabulary.

                Indices can be obtained using [`AutoTokenizer`]. See [`PreTrainedTokenizer.encode`] and
                [`PreTrainedTokenizer.__call__`] for details.

                [What are input IDs?](../glossary#input-ids)
            inputs_embeds (`mindspore.Tensor` of shape `(batch_size, sequence_length, hidden_size)`, *optional*):
                input embeddings for the model. This bypasses the model's internal embedding lookup matrix.
            attention_mask (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:

                - 1 for tokens that are **not masked**,
                - 0 for tokens that are **masked**.

                [What are attention masks?](../glossary#attention-mask)
            position_ids (`mindspore.Tensor`, *optional*):
                Denotes the position ids of `input_ids`.
            output_attentions (`bool`, *optional*):
                Whether or not to return the attentions tensors of all attention layers. See `attentions` under
                returned tensors for more detail.
            output_hidden_states (`bool`, *optional*):
                Whether or not to return the hidden states of all layers. See `hidden_states` under returned tensors
                for more detail.
            return_dict (`bool`, *optional*):
                Whether or not to return a [`~utils.ModelOutput`] instead of a plain tuple.
        """
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            self.warn_if_padding_and_no_attention_mask(
                input_ids, attention_mask)
            input_shape = input_ids.shape
            input_ids = input_ids.view(-1, input_shape[-1])
            inputs_embeds = self.token_embedding(input_ids)
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.shape[:-1]
        else:
            raise ValueError(
                "You have to specify either input_ids or inputs_embeds")

        # expand attention_mask and create position_ids if needed
        if attention_mask is not None:
            # [bsz, seq_len] -> [bsz, 1, tgt_seq_len, src_seq_len]
            attention_mask = _prepare_4d_attention_mask(
                attention_mask, inputs_embeds.dtype)

        if position_ids is None:
            # device = input_ids.device if input_ids is not None else inputs_embeds.device
            position_ids = ops.arange(input_shape[1], dtype=mindspore.int64)
            position_ids = position_ids.unsqueeze(0)

        encoder_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None

        rotary_pos_emb = self.rotary_pos_emb(
            inputs_embeds) if self.rotary_pos_emb is not None else None

        hidden_states = inputs_embeds
        for idx, encoder_layer in enumerate(self.layers):
            if output_hidden_states:
                encoder_states = encoder_states + (hidden_states,)
            layer_outputs = encoder_layer(
                hidden_states,
                rotary_pos_emb,
                attention_mask,
                position_ids,
                output_attentions=output_attentions,
            )

            hidden_states = layer_outputs[0]

            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[1],)

        if output_hidden_states:
            encoder_states = encoder_states + (hidden_states,)

        last_hidden_state = hidden_states
        last_hidden_state = self.final_layer_norm(last_hidden_state)

        # take the mean over axis 1 and get pooled output
        pooled_output = self.sequence_summary(last_hidden_state)

        # apply the projection layer
        embeds = self.projection(pooled_output)

        if not return_dict:
            return tuple(
                v for v in [embeds, last_hidden_state, pooled_output, encoder_states, all_attentions] if v is not None
            )

        return ClvpEncoderOutput(
            embeds=embeds,
            last_hidden_state=last_hidden_state,
            pooler_output=pooled_output,
            hidden_states=encoder_states,
            attentions=all_attentions,
        )


class ClvpDecoder(ClvpPreTrainedModel):
    """
    Transformer decoder consisting of *config.num_hidden_layers* layers. Each layer is a [`ClvpDecoderLayer`]
    """

    def __init__(self, config):
        super().__init__(config)

        self.config = config

        self.input_embeds_layer = nn.Embedding(
            self.config.vocab_size, self.config.hidden_size)
        self.position_embeds_layer = nn.Embedding(
            self.config.max_position_embeddings, self.config.hidden_size)

        self.drop = nn.Dropout(self.config.embd_pdrop)
        self.layers = nn.ModuleList(
            [ClvpDecoderLayer(self.config) for _ in range(self.config.num_hidden_layers)])
        self.layer_norm = nn.LayerNorm(
            self.config.hidden_size, eps=self.config.layer_norm_epsilon)

        self.gradient_checkpointing = False

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.input_embeds_layer

    def set_input_embeddings(self, new_embeddings):
        self.input_embeds_layer = new_embeddings

    def _prune_heads(self, heads_to_prune):
        """
        Prunes heads of the model. heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
        """
        for layer, heads in heads_to_prune.items():
            self.layers[layer].attn.prune_heads(heads)

    def forward(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        token_type_ids: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        head_mask: Optional[mindspore.Tensor] = None,
        past_key_values: Optional[Tuple[Tuple[mindspore.Tensor]]] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPastAndCrossAttentions]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            self.warn_if_padding_and_no_attention_mask(
                input_ids, attention_mask)
            input_shape = input_ids.shape
            input_ids = input_ids.view(-1, input_shape[-1])
            # input_ids.shape[0]
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.shape[:-1]
            # inputs_embeds.shape[0]
        else:
            raise ValueError(
                "You have to specify either input_ids or inputs_embeds")

        if token_type_ids is not None:
            token_type_ids = token_type_ids.view(-1, input_shape[-1])

        if past_key_values is None:
            past_key_values_length = 0
            past_key_values = tuple([None] * len(self.layers))
        else:
            past_key_values_length = past_key_values[0][0].shape[-2]
        if position_ids is None:
            position_ids = ops.arange(
                past_key_values_length, input_shape[-1] + past_key_values_length, dtype=mindspore.int64
            )
            position_ids = position_ids.unsqueeze(0).view(-1, input_shape[-1])

        if inputs_embeds is None:
            inputs_embeds = self.input_embeds_layer(input_ids)
        position_embeds = self.position_embeds_layer(position_ids)
        inputs_embeds = inputs_embeds + position_embeds

        attention_mask = _prepare_4d_causal_attention_mask(
            attention_mask, input_shape, inputs_embeds, past_key_values_length
        )

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x num_attention_heads x N x N
        # head_mask has shape num_hidden_layers x batch x num_attention_heads x N x N
        head_mask = self.get_head_mask(
            head_mask, self.config.num_hidden_layers)

        hidden_states = inputs_embeds

        if token_type_ids is not None:
            token_type_embeds = self.input_embeds_layer(token_type_ids)
            hidden_states = hidden_states + token_type_embeds

        hidden_states = self.drop(hidden_states)

        output_shape = (-1,) + input_shape[1:] + (hidden_states.shape[-1],)

        if self.gradient_checkpointing and self.training:
            if use_cache:
                logger.warning_once(
                    "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`..."
                )
                use_cache = False

        presents = () if use_cache else None
        all_self_attentions = () if output_attentions else None
        all_cross_attentions = () if output_attentions and self.config.add_cross_attention else None
        all_hidden_states = () if output_hidden_states else None
        for i, (block, past_key_value) in enumerate(zip(self.layers, past_key_values)):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            outputs = block(
                hidden_states,
                past_key_value=past_key_value,
                attention_mask=attention_mask,
                position_ids=position_ids,
                head_mask=head_mask[i],
                use_cache=use_cache,
                output_attentions=output_attentions,
            )

            hidden_states = outputs[0]
            if use_cache is True:
                presents = presents + (outputs[1],)

            if output_attentions:
                all_self_attentions = all_self_attentions + \
                    (outputs[2 if use_cache else 1],)
                if self.config.add_cross_attention:
                    all_cross_attentions = all_cross_attentions + \
                        (outputs[3 if use_cache else 2],)

        hidden_states = self.layer_norm(hidden_states)

        hidden_states = hidden_states.view(output_shape)

        # Add last hidden state
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(
                v
                for v in [hidden_states, presents, all_hidden_states, all_self_attentions, all_cross_attentions]
                if v is not None
            )

        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=hidden_states,
            past_key_values=presents,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
            cross_attentions=all_cross_attentions,
        )


class ClvpModel(ClvpPreTrainedModel):
    def __init__(self, config: ClvpDecoderConfig):
        super().__init__(config)
        self.config = config
        self.decoder = ClvpDecoder(self.config)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.decoder.input_embeds_layer

    def set_input_embeddings(self, value):
        self.decoder.input_embeds_layer = value

    def get_decoder(self):
        return self.decoder

    def forward(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        token_type_ids: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        head_mask: Optional[mindspore.Tensor] = None,
        past_key_values: Optional[Tuple[Tuple[mindspore.Tensor]]] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, BaseModelOutputWithPastAndCrossAttentions]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # decoder outputs consists of (dec_features, past_key_value, dec_hidden, dec_attn)
        decoder_outputs = self.decoder(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            past_key_values=past_key_values,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        if not return_dict:
            return decoder_outputs

        return BaseModelOutputWithPastAndCrossAttentions(
            last_hidden_state=decoder_outputs.last_hidden_state,
            past_key_values=decoder_outputs.past_key_values,
            hidden_states=decoder_outputs.hidden_states,
            attentions=decoder_outputs.attentions,
            cross_attentions=decoder_outputs.cross_attentions,
        )


class ClvpForCausalLM(ClvpPreTrainedModel):
    def __init__(self, config):
        super().__init__(config)

        self.config = config
        self.model = ClvpModel(self.config)

        self.final_norm = nn.LayerNorm(self.config.hidden_size)
        self.lm_head = nn.Linear(
            self.config.hidden_size, self.config.vocab_size, bias=True)

        # Initialize weights and apply final processing
        self.post_init()

    def get_input_embeddings(self):
        return self.model.decoder.input_embeds_layer

    def set_input_embeddings(self, new_embeddings):
        self.model.decoder.input_embeds_layer = new_embeddings

    def _prepare_model_inputs(
        self,
        inputs: Optional[mindspore.Tensor] = None,
        bos_token_id: Optional[int] = None,
        model_kwargs: Optional[Dict[str, mindspore.Tensor]] = None,
    ) -> Tuple[mindspore.Tensor, Optional[str], Dict[str, mindspore.Tensor]]:
        """
        This function extracts the model-specific `inputs` for generation.
        """
        input_name = self.main_input_name

        model_kwargs = {k: v for k, v in model_kwargs.items() if v is not None}

        inputs_kwarg = model_kwargs.pop(input_name, None)
        if inputs_kwarg is not None and inputs is not None:
            raise ValueError(
                f"`inputs`: {inputs}` were passed alongside {input_name} which is not allowed."
                f"Make sure to either pass {inputs} or {input_name}=..."
            )
        elif inputs_kwarg is not None:
            inputs = inputs_kwarg

        if input_name == "input_ids" and "inputs_embeds" in model_kwargs:
            model_kwargs["input_ids"] = self._maybe_initialize_input_ids_for_generation(
                inputs, bos_token_id, model_kwargs=model_kwargs
            )
            inputs, input_name = model_kwargs["inputs_embeds"], "inputs_embeds"

        # Check if conditioning_embeds are provided or not, if yes then concatenate the bos_token_id at the end of the conditioning_embeds.
        # Then we must subtract the positional_ids because during the forward pass it will be added anyways, so we must cancel them out here.
        conditioning_embeds = model_kwargs.get("conditioning_embeds", None)

        if conditioning_embeds is not None:
            mel_start_token_embedding = self.model.decoder.input_embeds_layer(
                ops.full(
                    (conditioning_embeds.shape[0], 1),
                    fill_value=self.config.bos_token_id,
                )
            )
            mel_start_token_embedding += self.model.decoder.position_embeds_layer(
                ops.full((conditioning_embeds.shape[0], 1), fill_value=0)
            )
            conditioning_embeds = ops.concat(
                [conditioning_embeds, mel_start_token_embedding], dim=1)

            # subtract the positional_ids here
            if hasattr(model_kwargs, "attention_mask"):
                position_ids = model_kwargs["attention_mask"].long(
                ).cumsum(-1) - 1
            else:
                position_ids = ops.range(
                    0, conditioning_embeds.shape[1] - 1, dtype=mindspore.int64
                )
            position_ids = position_ids.unsqueeze(
                0).tile((conditioning_embeds.shape[0], 1))

            model_kwargs["inputs_embeds"] = conditioning_embeds - self.model.decoder.position_embeds_layer(
                position_ids
            )
            model_kwargs["input_ids"] = (
                ops.ones(
                    (model_kwargs["inputs_embeds"].shape[0], 1), dtype=mindspore.int64)
                * self.config.bos_token_id
            )

            return model_kwargs["inputs_embeds"], "inputs_embeds", model_kwargs

        inputs = self._maybe_initialize_input_ids_for_generation(
            inputs, bos_token_id, model_kwargs)
        return inputs, input_name, model_kwargs

    def prepare_inputs_for_generation(
        self, input_ids, past_key_values=None, inputs_embeds=None, conditioning_embeds=None, **kwargs
    ):
        input_ids_length = input_ids.shape[-1]
        token_type_ids = kwargs.get("token_type_ids", None)
        # only last token for inputs_ids if past is defined in kwargs
        if past_key_values:
            past_length = past_key_values[0][0].shape[2]

            # Some generation methods already pass only the last input ID
            if input_ids.shape[1] > past_length:
                remove_prefix_length = past_length
            else:
                # Default to old behavior: keep only final ID
                remove_prefix_length = input_ids.shape[1] - 1

            input_ids = input_ids[:, remove_prefix_length:]
            if token_type_ids is not None:
                token_type_ids = token_type_ids[:, -input_ids.shape[1]:]

        attention_mask = kwargs.get("attention_mask", None)
        position_ids = kwargs.get("position_ids", None)

        if attention_mask is not None and position_ids is None:
            # create position_ids on the fly for batch generation
            position_ids = attention_mask.long().cumsum(-1) - 1
            position_ids.masked_fill(attention_mask == 0, 1)
            if past_key_values:
                position_ids = position_ids[:, -1].unsqueeze(-1)
        else:
            position_ids = None

        if conditioning_embeds is not None and past_key_values is not None:
            position_ids = mindspore.tensor(
                [input_ids_length], dtype=mindspore.int64)

        # if `inputs_embeds` are passed, we only want to use them in the 1st generation step
        if inputs_embeds is not None and past_key_values is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs.update(
            {
                "past_key_values": past_key_values,
                "use_cache": kwargs.get("use_cache"),
                "position_ids": position_ids,
                "token_type_ids": token_type_ids,
            }
        )
        return model_inputs

    def forward(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        past_key_values: Optional[Tuple[Tuple[mindspore.Tensor]]] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        token_type_ids: Optional[mindspore.Tensor] = None,
        position_ids: Optional[mindspore.Tensor] = None,
        head_mask: Optional[mindspore.Tensor] = None,
        inputs_embeds: Optional[mindspore.Tensor] = None,
        labels: Optional[mindspore.Tensor] = None,
        use_cache: Optional[bool] = None,
        output_attentions: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, CausalLMOutputWithCrossAttentions]:
        r"""
        labels (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
            Labels for language modeling. Note that the labels **are shifted** inside the model, i.e. you can set
            `labels = input_ids` Indices are selected in `[-100, 0, ..., config.vocab_size]` All labels set to `-100`
            are ignored (masked), the loss is only computed for labels in `[0, ..., config.vocab_size]`
        """

        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        outputs = self.model(
            input_ids=input_ids,
            past_key_values=past_key_values,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        hidden_states = outputs[0]

        lm_logits = self.final_norm(hidden_states)
        lm_logits = self.lm_head(lm_logits)

        loss = None
        if labels is not None:
            # Shift so that tokens < n predict n
            shift_logits = lm_logits[..., :-1, :]
            shift_labels = labels[..., 1:]
            # Flatten the tokens
            loss_fct = nn.CrossEntropyLoss()
            loss = loss_fct(
                shift_logits.view(-1, shift_logits.shape[-1]), shift_labels.view(-1))

        if not return_dict:
            output = (lm_logits,) + outputs[1:]
            return ((loss,) + output) if loss is not None else output

        return CausalLMOutputWithCrossAttentions(
            loss=loss,
            logits=lm_logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
            cross_attentions=outputs.cross_attentions,
        )

    @staticmethod
    # Copied from transformers.models.gpt2.modeling_gpt2.GPT2LMHeadModel._reorder_cache
    def _reorder_cache(
        past_key_values: Tuple[Tuple[mindspore.Tensor]], beam_idx: mindspore.Tensor
    ) -> Tuple[Tuple[mindspore.Tensor]]:
        """
        This function is used to re-order the `past_key_values` cache if [`~PreTrainedModel.beam_search`] or
        [`~PreTrainedModel.beam_sample`] is called. This is required to match `past_key_values` with the correct
        beam_idx at every generation step.
        """
        return tuple(
            tuple(past_state.index_select(0, beam_idx)
                  for past_state in layer_past)
            for layer_past in past_key_values
        )


class ClvpModelForConditionalGeneration(ClvpPreTrainedModel):
    config_class = ClvpConfig

    def __init__(self, config: ClvpConfig):
        super().__init__(config)

        if not isinstance(config.text_config, ClvpEncoderConfig):
            raise TypeError(
                "config.text_config is expected to be of type `ClvpEncoderConfig` but is of type"
                f" {type(config.text_config)}."
            )

        if not isinstance(config.speech_config, ClvpEncoderConfig):
            raise TypeError(
                "config.speech_config is expected to be of type `ClvpEncoderConfig` but is of type"
                f" {type(config.speech_config)}."
            )

        if not isinstance(config.decoder_config, ClvpDecoderConfig):
            raise TypeError(
                "config.decoder_config is expected to be of type `ClvpDecoderConfig` but is of type"
                f" {type(config.decoder_config)}."
            )

        self.conditioning_encoder = ClvpConditioningEncoder(config)

        self.speech_decoder_model = ClvpForCausalLM(config.decoder_config)

        self.text_encoder_model = ClvpEncoder(config.text_config)
        self.speech_encoder_model = ClvpEncoder(config.speech_config)

        self.logit_scale = nn.Parameter(
            mindspore.tensor(self.config.logit_scale_init_value))

        # Initialize weights and apply final processing
        self.post_init()

    # taken from the original repo,
    # link : https://github.com/neonbjb/tortoise-tts/blob/4003544b6ff4b68c09856e04d3eff9da26d023c2/tortoise/api.py#L117
    def fix_speech_decoder_output(self, speech_ids: mindspore.Tensor) -> mindspore.Tensor:
        """
        This method modifies the output of the decoder model, such as replacing the `eos_token_id` and changing the
        last few tokens of each sequence.

        Args:
            speech_ids (`mindspore.Tensor`):
                This refers to the output of the decoder model.
        """
        decoder_fixing_codes = self.config.decoder_config.decoder_fixing_codes
        speech_ids = speech_ids[:, 1:]

        stop_token_indices = ops.where(
            speech_ids == self.speech_decoder_model.config.eos_token_id, mindspore.tensor(1), 0)
        speech_ids = ops.masked_fill(
            speech_ids, mask=stop_token_indices.bool(), value=decoder_fixing_codes[0])

        for i, each_seq_stop_token_index in enumerate(stop_token_indices):
            # This means that no stop tokens were found so the sentence was still being generated, in that case we don't need
            # to apply any padding so just skip to the next sequence of tokens.
            if each_seq_stop_token_index.sum() == 0:
                continue

            stm = each_seq_stop_token_index.argmax()
            speech_ids[i, stm:] = decoder_fixing_codes[0]
            if stm - 3 < speech_ids.shape[1]:
                speech_ids[i, -3:] = mindspore.tensor(
                    [decoder_fixing_codes[1:]], dtype=mindspore.int64
                )

        return speech_ids

    def get_text_features(
        self,
        input_ids: Optional[mindspore.Tensor] = None,
        text_encoder_inputs_embeds: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
    ) -> mindspore.Tensor:
        r"""
        This method can be used to extract text_embeds from a text. The text embeddings obtained by applying the
        projection layer to the pooled output of the CLVP text encoder model.

        Args:
            input_ids (`mindspore.Tensor` of shape `(batch_size, sequence_length)`):
                Indices of input sequence tokens in the vocabulary. Padding will be ignored by default should you
                provide it.

                [What are input IDs?](../glossary#input-ids)
            text_encoder_inputs_embeds (`mindspore.Tensor`, *optional*):
                inputs_embeds for the text encoder model passed in place of `input_ids`.
            attention_mask (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Mask to avoid performing attention on padding token indices. Mask values selected in `[0, 1]`:

                - 1 for tokens that are **not masked**,
                - 0 for tokens that are **masked**.

                [What are attention masks?](../glossary#attention-mask)

        Returns:
            `mindspore.Tensor` of shape `(batch_size, output_dim)`:
                The text embeddings obtained by applying the projection layer to the pooled output of the CLVP Text
                Model.

        Examples:

        ```python
        >>> from transformers import ClvpProcessor, ClvpModelForConditionalGeneration

        >>> # Define the Text
        >>> text = "This is an example text."

        >>> # Define processor and model
        >>> processor = ClvpProcessor.from_pretrained("susnato/clvp_dev")
        >>> model = ClvpModelForConditionalGeneration.from_pretrained("susnato/clvp_dev")

        >>> # Generate processor output and text embeds
        >>> processor_output = processor(text=text, return_tensors="pt")
        >>> text_embeds = model.get_text_features(input_ids=processor_output["input_ids"])
        ```
        """

        outputs = self.text_encoder_model(
            input_ids=input_ids,
            inputs_embeds=text_encoder_inputs_embeds,
            attention_mask=attention_mask,
        )

        return outputs[0]

    def get_speech_features(
        self,
        speech_ids: Optional[mindspore.Tensor] = None,
        input_ids: Optional[mindspore.Tensor] = None,
        input_features: Optional[mindspore.Tensor] = None,
        conditioning_encoder_inputs_embeds: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        generation_config: Optional[GenerationConfig] = None,
        **kwargs,
    ) -> mindspore.Tensor:
        r"""
        This method can be used to extract speech_embeds. The speech embeddings are obtained by applying the speech
        model on speech_ids. If speech_ids is not present but both input_ids and input_features are given then the
        decoder model will be used to first generate the speech_ids and then applying the speech model.

        Args:
            speech_ids (`mindspore.Tensor` of shape `(batch_size, num_speech_ids)`, *optional*):
                Speech Tokens. Padding will be ignored by default should you provide it. If speech_ids are provided
                then input_ids and input_features will be automatically ignored.
            input_ids (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Input text Tokens. Processed from the [`ClvpTokenizer`]. If speech_ids is not provided, then input_ids
                and input_features will be used.
            input_features (`mindspore.Tensor` of shape `(batch_size, feature_size, time_dim)`, *optional*):
                Indicates log-melspectrogram representations for audio returned by [`ClvpFeatureExtractor`]. If
                speech_ids is not provided, then input_ids and input_features will be used.
            conditioning_encoder_inputs_embeds (`mindspore.Tensor`, *optional*):
                inputs_embeds for `ClvpConditioningEncoder`. Can be used in place of `input_ids`.
            attention_mask (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Mask to avoid performing attention on padding speech token indices. Mask values selected in `[0, 1]`:

                - 1 for tokens that are **not masked**,
                - 0 for tokens that are **masked**.

                [What are attention masks?](../glossary#attention-mask)
            generation_config (`GenerationConfig`, *optional*):
                generation config to control the generation of speech_ids if they are not provided.

        Returns:
            `mindspore.Tensor` of shape `(batch_size, output_dim)`:
                The speech embeddings obtained by applying the projection layer to the pooled output of the CLVP Speech
                Model.

        Examples:

        ```python
        >>> import datasets
        >>> from transformers import ClvpProcessor, ClvpModelForConditionalGeneration

        >>> # Define the Text and Load the Audio (We are taking an audio example from HuggingFace Hub using `datasets` library)
        >>> text = "This is an example text."
        >>> ds = datasets.load_dataset("hf-internal-testing/librispeech_asr_dummy", "clean", split="validation")
        >>> ds = ds.cast_column("audio", datasets.Audio(sampling_rate=22050))
        >>> _, audio, sr = ds.sort("id").select(range(1))[:1]["audio"][0].values()

        >>> # Define processor and model
        >>> processor = ClvpProcessor.from_pretrained("susnato/clvp_dev")
        >>> model = ClvpModelForConditionalGeneration.from_pretrained("susnato/clvp_dev")

        >>> # Generate processor output and model output
        >>> processor_output = processor(raw_speech=audio, sampling_rate=sr, text=text, return_tensors="pt")
        >>> speech_embeds = model.get_speech_features(
        ...     input_ids=processor_output["input_ids"], input_features=processor_output["input_features"]
        ... )
        ```
        """

        if speech_ids is None:
            if (input_ids is None and conditioning_encoder_inputs_embeds is None) or input_features is None:
                raise ValueError(
                    "Either speech_ids or input_ids/conditioning_encoder_inputs_embeds and input_features must be provided."
                )

            if generation_config is None:
                generation_config = self.generation_config
            generation_config.update(**kwargs)

            conditioning_embeds = self.conditioning_encoder(
                input_features=input_features,
                input_ids=input_ids,
                inputs_embeds=conditioning_encoder_inputs_embeds,
                attention_mask=attention_mask,
            )

            speech_ids = self.speech_decoder_model.generate(
                conditioning_embeds=conditioning_embeds,
                generation_config=generation_config,
            )

            speech_ids = self.fix_speech_decoder_output(speech_ids[0])

        outputs = self.speech_encoder_model(
            input_ids=speech_ids,
            attention_mask=attention_mask,
        )

        return outputs[0]

    def forward(
        self,
        input_ids: mindspore.Tensor = None,
        input_features: mindspore.Tensor = None,
        conditioning_encoder_inputs_embeds: Optional[mindspore.Tensor] = None,
        text_encoder_inputs_embeds: Optional[mindspore.Tensor] = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        return_loss: Optional[bool] = None,
        output_hidden_states: Optional[bool] = None,
        output_attentions: Optional[bool] = False,
        return_dict: Optional[bool] = None,
    ) -> Union[Tuple, ClvpOutput]:
        r"""
        Returns:

        Examples:

        ```python
        >>> import datasets
        >>> from transformers import ClvpProcessor, ClvpModelForConditionalGeneration

        >>> # Define the Text and Load the Audio (We are taking an audio example from HuggingFace Hub using `datasets` library)
        >>> text = "This is an example text."

        >>> ds = datasets.load_dataset("hf-internal-testing/librispeech_asr_dummy", "clean", split="validation")
        >>> ds = ds.cast_column("audio", datasets.Audio(sampling_rate=22050))
        >>> _, audio, sr = ds.sort("id").select(range(1))[:1]["audio"][0].values()

        >>> # Define processor and model
        >>> processor = ClvpProcessor.from_pretrained("susnato/clvp_dev")
        >>> model = ClvpModelForConditionalGeneration.from_pretrained("susnato/clvp_dev")

        >>> # processor outputs and model outputs
        >>> processor_output = processor(raw_speech=audio, sampling_rate=sr, text=text, return_tensors="pt")
        >>> outputs = model(
        ...     input_ids=processor_output["input_ids"],
        ...     input_features=processor_output["input_features"],
        ...     return_dict=True,
        ... )
        ```
        """

        # Use CLVP model's config for some fields (if specified) instead of those of speech & text components.
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        conditioning_embeds = self.conditioning_encoder(
            input_features=input_features,
            input_ids=input_ids,
            inputs_embeds=conditioning_encoder_inputs_embeds,
            attention_mask=attention_mask,
        )

        decoder_outputs = self.speech_decoder_model(
            inputs_embeds=conditioning_embeds,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        speech_ids = decoder_outputs[0]

        # since we will get the embeds of shape `(batch_size, seq_len, embedding_dim)` during the forward pass
        # we must convert it to tokens, to make it compaitable with speech_transformer
        if speech_ids.ndim == 3:
            speech_ids = speech_ids.argmax(2)
        speech_ids = self.fix_speech_decoder_output(speech_ids)

        speech_outputs = self.speech_encoder_model(
            input_ids=speech_ids,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        text_outputs = self.text_encoder_model(
            input_ids=input_ids,
            inputs_embeds=text_encoder_inputs_embeds,
            attention_mask=attention_mask,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        speech_embeds = speech_outputs[0]
        text_embeds = text_outputs[0]

        # normalized features
        speech_embeds = speech_embeds / speech_embeds.norm(2, -1, True)
        text_embeds = text_embeds / text_embeds.norm(2, -1, True)

        # cosine similarity as logits
        logit_scale = self.logit_scale.exp()
        logits_per_text = ops.matmul(
            text_embeds, speech_embeds.t()) * logit_scale
        logits_per_speech = logits_per_text.t()

        loss = None
        if return_loss:
            loss = clvp_loss(logits_per_text)

        if not return_dict:
            output = (
                logits_per_speech,
                logits_per_text,
                text_embeds,
                speech_embeds,
                text_outputs[2],
                speech_outputs[2],
            )
            if output_hidden_states:
                output += (
                    decoder_outputs[-1],
                    text_outputs[-1],
                    speech_outputs[-1],
                )

            return ((loss,) + output) if loss is not None else output

        return ClvpOutput(
            loss=loss,
            logits_per_speech=logits_per_speech,
            logits_per_text=logits_per_text,
            text_embeds=text_embeds,
            speech_embeds=speech_embeds,
            text_model_output=text_outputs[2],
            speech_model_output=speech_outputs[2],
            decoder_hidden_states=decoder_outputs.hidden_states,
            text_encoder_hidden_states=text_outputs.hidden_states,
            speech_encoder_hidden_states=speech_outputs.hidden_states,
        )

    # @torch.no_grad()
    def generate(
        self,
        input_ids: mindspore.Tensor = None,
        input_features: mindspore.Tensor = None,
        attention_mask: Optional[mindspore.Tensor] = None,
        generation_config: Optional[GenerationConfig] = None,
        pad_to_max_mel_tokens: Optional[int] = None,
        output_hidden_states: Optional[bool] = None,
        **kwargs,
    ):
        """
        Generate method for `ClvpModelForConditionalGeneration`, this method calls the `generate` method of
        `ClvpForCausalLM` and then uses those generated `speech_ids` to process `text_embeds` and `speech_embeds` using
        `ClvpEncoder`.

        Args:
            input_ids (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Input text Tokens. Processed from the [`ClvpTokenizer`].
            input_features (`mindspore.Tensor` of shape `(batch_size, feature_size, time_dim)`, *optional*):
                Indicates log-melspectrogram representations for audio returned by [`ClvpFeatureExtractor`].
            attention_mask (`mindspore.Tensor` of shape `(batch_size, sequence_length)`, *optional*):
                Mask to avoid performing attention on padding text token indices. Mask values selected in `[0, 1]`:

                - 1 for tokens that are **not masked**,
                - 0 for tokens that are **masked**.

                [What are attention masks?](../glossary#attention-mask)
            generation_config (`~generation.GenerationConfig`, *optional*):
                The generation configuration to be used as base parametrization for the generation call. `**kwargs`
                passed to generate matching the attributes of `generation_config` will override them. If
                `generation_config` is not provided, the default will be used, which had the following loading
                priority: 1) from the `generation_config.json` model file, if it exists; 2) from the model
                configuration. Please note that unspecified parameters will inherit [`~generation.GenerationConfig`]'s
                default values, whose documentation should be checked to parameterize generation.
            pad_to_max_mel_tokens (`int`, *optional*):
                Pads generated speech_ids to the specified value. This is to implement the same logic from the official
                repo, link: https://github.com/neonbjb/tortoise-tts/blob/80f89987a5abda5e2b082618cd74f9c7411141dc/tortoise/api.py#L430
                and to make sure the logits are same.
                This does not affect generation quality so please don't consider using it since it is less efficient.
            output_hidden_states (`bool`, *optional*):
                Whether or not to return the hidden states of decoder model, text encoder and speech encoder models.

        Returns:
            `ClvpOutput` or tuple: A `ClvpOutput` (if `return_dict_in_generate=True` or when
            `config.return_dict_in_generate=True`) or a tuple.
        """

        # If the input sequences are larger than (self.config.decoder_config.max_text_tokens - 3) then raise error,
        # because we need to add 3 tokens ( 1 bos tokens and 2 eos tokens) to the input_ids in ClvpConditioningEncoder to
        # properly sample
        sequence_length = input_ids.shape[-1]
        if sequence_length > (self.config.decoder_config.max_text_tokens - 3):
            raise ValueError(
                f"Maximum sequence length reached! Found input_ids of length {sequence_length}."
                f"Please make sure that the maximum length of input_ids is {self.config.decoder_config.max_text_tokens - 3}"
            )

        if generation_config is None:
            generation_config = self.generation_config

        generation_config = copy.deepcopy(generation_config)
        # All unused kwargs must be model kwargs
        model_kwargs = generation_config.update(**kwargs)
        generation_config.validate()
        self._validate_model_kwargs(model_kwargs.copy())

        # pad input_ids as specified in the original repo
        # link: https://github.com/neonbjb/tortoise-tts/blob/80f89987a5abda5e2b082618cd74f9c7411141dc/tortoise/api.py#L380
        input_ids, attention_mask = _pad_extra_bos_eos_tokens(
            input_ids,
            attention_mask,
            add_bos_token=False,
            bos_token_id=self.config.text_config.bos_token_id,
            eos_token_id=self.config.text_config.eos_token_id,
        )

        conditioning_embeds = self.conditioning_encoder(
            input_features=input_features,
            input_ids=input_ids,
            attention_mask=attention_mask,
        )

        decoder_outputs = self.speech_decoder_model.generate(
            conditioning_embeds=conditioning_embeds,
            generation_config=generation_config,
            output_hidden_states=output_hidden_states,
            return_dict=generation_config.return_dict_in_generate,
        )
        if isinstance(decoder_outputs, ModelOutput):
            speech_ids = decoder_outputs.sequences

        # pad to pad_to_max_mel_tokens if given, to replicate the original repo logic
        # link: https://github.com/neonbjb/tortoise-tts/blob/80f89987a5abda5e2b082618cd74f9c7411141dc/tortoise/api.py#L430
        if pad_to_max_mel_tokens is not None:
            padding_needed = pad_to_max_mel_tokens - speech_ids.shape[-1]
            speech_ids = ops.pad(
                speech_ids, (0, padding_needed), value=self.generation_config.eos_token_id
            )

        speech_ids = self.fix_speech_decoder_output(speech_ids)

        speech_outputs = self.speech_encoder_model(
            input_ids=speech_ids,
            output_hidden_states=output_hidden_states,
            return_dict=generation_config.return_dict_in_generate,
        )
        text_outputs = self.text_encoder_model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            output_hidden_states=output_hidden_states,
            return_dict=generation_config.return_dict_in_generate,
        )

        speech_embeds = speech_outputs[0]
        text_embeds = text_outputs[0]

        # normalized features
        speech_embeds = speech_embeds / \
            speech_embeds.norm(p=2, dim=-1, keepdim=True)
        text_embeds = text_embeds / text_embeds.norm(p=2, dim=-1, keepdim=True)

        # cosine similarity as logits
        logit_scale = self.logit_scale.exp()
        logits_per_text = ops.matmul(
            text_embeds, speech_embeds.t()) * logit_scale
        logits_per_speech = logits_per_text.t()

        if not generation_config.return_dict_in_generate:
            output = (
                speech_ids,
                logits_per_speech,
                logits_per_text,
                text_embeds,
                speech_embeds,
                text_outputs[2],
                speech_outputs[2],
            )
            if output_hidden_states:
                output += (
                    decoder_outputs[-1],
                    text_outputs[-1],
                    speech_outputs[-1],
                )

            return output

        return ClvpOutput(
            speech_ids=speech_ids,
            logits_per_speech=logits_per_speech,
            logits_per_text=logits_per_text,
            text_embeds=text_embeds,
            speech_embeds=speech_embeds,
            text_model_output=text_outputs[2],
            speech_model_output=speech_outputs[2],
            decoder_hidden_states=decoder_outputs.hidden_states,
            text_encoder_hidden_states=text_outputs.hidden_states,
            speech_encoder_hidden_states=speech_outputs.hidden_states,
        )


__all__ = [
    "ClvpModelForConditionalGeneration",
    "ClvpForCausalLM",
    "ClvpModel",
    "ClvpPreTrainedModel",
    "ClvpEncoder",
    "ClvpDecoder",
]
