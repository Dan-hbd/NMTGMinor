# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
"""PyTorch BERT model. """


import math

import torch
import torch.utils.checkpoint
from torch import nn
import torch.nn.functional as F
import numpy as np

from .activations import gelu, gelu_new, swish
from .configuration_bert import BertConfig

from .modeling_outputs import (
    BaseModelOutput,
)
from .modeling_utils import PreTrainedModel, find_pruneable_heads_and_indices
import onmt.constants
from collections import defaultdict


BERT_PRETRAINED_MODEL_ARCHIVE_LIST = [
    "bert-base-uncased",
    "bert-large-uncased",
    "bert-base-cased",
    "bert-large-cased",
    "bert-base-multilingual-uncased",
    "bert-base-multilingual-cased",
    "bert-base-chinese",
    "bert-base-german-cased",
    "bert-large-uncased-whole-word-masking",
    "bert-large-cased-whole-word-masking",
    # See all BERT models at https://huggingface.co/models?filter=bert
]


def mish(x):
    return x * torch.tanh(nn.functional.softplus(x))


ACT2FN = {"gelu": gelu, "relu": torch.nn.functional.relu, "swish": swish, "gelu_new": gelu_new, "mish": mish}


try:
    from apex.normalization.fused_layer_norm import FusedLayerNorm as BertLayerNorm

except ImportError:
    print("FusedLayerNorm is not available, we use torch.nn.LayerNorm")
    BertLayerNorm = torch.nn.LayerNorm


class BertEmbeddings(nn.Module):
    """Construct the embeddings from word, position and token_type embeddings.
    """

    def __init__(self, config):
        super().__init__()
        self.word_embeddings = nn.Embedding(config.vocab_size, config.hidden_size, padding_idx=config.pad_token_id)
        self.max_relative_pos_len = config.max_relative_pos_len
        self.pos_emb_type = config.pos_emb_type
        self.diff_head_pos = config.diff_head_pos
        if self.pos_emb_type == 'absolute':
            self.position_embeddings = nn.Embedding(config.max_position_embeddings, config.hidden_size)
        else:
            self.position_embeddings = None

        self.token_type_embeddings = nn.Embedding(config.type_vocab_size, config.hidden_size)
        self.max_position_id = config.max_position_embeddings
        self.bert_word_dropout = config.bert_word_dropout
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.bert_emb_dropout)
        self.register_buffer("position_ids", torch.arange(config.max_position_embeddings).expand((1, -1)))

    def forward(self, input_ids=None, token_type_ids=None, position_ids=None, inputs_embeds=None, no_emb_offset=False):
        seq_length = input_ids.size(1)
        position_ids = torch.arange(seq_length, dtype=torch.long, device=input_ids.device)
        if seq_length > self.max_position_id:
            position_ids = torch.clamp(position_ids, 0, self.max_position_id-1)
        position_ids = position_ids.unsqueeze(0).expand_as(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        embed = self.word_embeddings

        if self.bert_word_dropout and self.training:
            mask = embed.weight.data.new().resize_((embed.weight.size(0), 1)).bernoulli_(1 - self.bert_word_dropout).\
                       expand_as(embed.weight) / (1 - self.bert_word_dropout)

            masked_embed_weight = mask * embed.weight
        else:
            masked_embed_weight = embed.weight
        padding_idx = embed.padding_idx

        words_embeddings = F.embedding(
            input_ids, masked_embed_weight, padding_idx, embed.max_norm,
            embed.norm_type, embed.scale_grad_by_freq, embed.sparse)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)
        embeddings = words_embeddings + token_type_embeddings

        if self.position_embeddings is not None:
            position_embeddings = self.position_embeddings(position_ids)
            embeddings += position_embeddings

        embeddings = self.LayerNorm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings

    def emb_step(self, tgt_len, input_ids, token_type_ids=None):
        position_ids = torch.tensor(tgt_len-1, dtype=torch.long, device=input_ids.device)
        if tgt_len > self.max_position_id:
            position_ids = torch.tensor(self.max_position_id-1, dtype=torch.long, device=input_ids.device)

        position_ids = position_ids.unsqueeze(0).expand_as(input_ids)
        if token_type_ids is None:
            token_type_ids = torch.zeros_like(input_ids)

        embed = self.word_embeddings
        masked_embed_weight = embed.weight
        padding_idx = embed.padding_idx

        words_embeddings = F.embedding(
            input_ids, masked_embed_weight, padding_idx, embed.max_norm,
            embed.norm_type, embed.scale_grad_by_freq, embed.sparse)
        token_type_embeddings = self.token_type_embeddings(token_type_ids)

        embeddings = words_embeddings + token_type_embeddings
        if self.position_embeddings is not None:
            position_embeddings = self.position_embeddings(position_ids)
            embeddings += position_embeddings

        embeddings = self.LayerNorm(embeddings)
        return embeddings


class BertSelfAttention(nn.Module):
    def __init__(self, config):
        super().__init__()
        if config.hidden_size % config.num_attention_heads != 0 and not hasattr(config, "embedding_size"):
            raise ValueError(
                "The hidden size (%d) is not a multiple of the number of attention "
                "heads (%d)" % (config.hidden_size, config.num_attention_heads)
            )

        self.heads_num = config.num_attention_heads
        self.head_size = int(config.hidden_size / config.num_attention_heads)
        self.all_head_size = self.heads_num * self.head_size

        self.query = nn.Linear(config.hidden_size, self.all_head_size)
        self.key = nn.Linear(config.hidden_size, self.all_head_size)
        self.value = nn.Linear(config.hidden_size, self.all_head_size)
        self.dropout = nn.Dropout(config.bert_atten_dropout)

        # for relative attention
        self.max_relative_pos_len = config.max_relative_pos_len
        self.pos_emb_type = config.pos_emb_type
        self.diff_head_pos = config.diff_head_pos
        if self.pos_emb_type == "absolute":
            self.relative_pos_emb = None
        elif self.pos_emb_type == "relative_k":
            if self.diff_head_pos:
                self.relative_pos_embeddingk = nn.Embedding(2 * self.max_relative_pos_len + 1, self.all_head_size)
            else:
                self.relative_pos_embeddingk = nn.Embedding(2 * self.max_relative_pos_len + 1, self.head_size)
        elif self.pos_emb_type == "relative_kv":
            # diff_head_pos brings no improvement
            assert not self.diff_head_pos
            if self.diff_head_pos:
                self.relative_pos_embeddingk = nn.Embedding(2 * self.max_relative_pos_len + 1, self.all_head_size)
                self.relative_pos_embeddingv = nn.Embedding(2 * self.max_relative_pos_len + 1, self.all_head_size)
            else:
                self.relative_pos_embeddingk = nn.Embedding(2 * self.max_relative_pos_len + 1, self.head_size)
                self.relative_pos_embeddingv = nn.Embedding(2 * self.max_relative_pos_len + 1, self.head_size)
        else:
            print("The pos_emb_type is not supported")
            exit(-1)

    def transpose_for_scores(self, x):
        # x: [h_dim, len, all_head_size]
        # new_x_shape: [h_dim, len, h_num, h_dim]
        # return: [h_dim, h_num, len, h_dim]
        new_x_shape = x.size()[:-1] + (self.heads_num, self.head_size)
        x = x.view(*new_x_shape)
        return x.permute(0, 2, 1, 3)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
    ):
        # hidden_states: [bsz, len, H]
        mixed_query_layer = self.query(hidden_states)  # [bsz, len, all_head_size]

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.
        if encoder_hidden_states is not None:
            mixed_key_layer = self.key(encoder_hidden_states)
            mixed_value_layer = self.value(encoder_hidden_states)
            attention_mask = encoder_attention_mask
        else:
            # hidden_states [bsz, len_q, H]
            # mixed_key_layer: [bsz, len_k, all_head_size] with all_head_size = H
            mixed_key_layer = self.key(hidden_states)
            mixed_value_layer = self.value(hidden_states)

        query_layer = self.transpose_for_scores(mixed_query_layer)  # [bsz, h_num, q, h_dim]
        key_layer = self.transpose_for_scores(mixed_key_layer)  # [bsz, h_num, k, h_dim]
        value_layer = self.transpose_for_scores(mixed_value_layer)  # [bsz, h_num, len_v, h_dim]

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores_qk = torch.matmul(query_layer, key_layer.transpose(-1, -2))  # [bsz h_num q k]
        attention_scores = attention_scores_qk / math.sqrt(self.head_size)

        if self.pos_emb_type == "relative_k" or self.pos_emb_type == "relative_kv":
            klen = mixed_key_layer.shape[1]
            vlen = klen
            qlen = mixed_query_layer.shape[1]
            bsz = hidden_states.shape[0]
            range_vec_q = torch.arange(qlen, device=hidden_states.device)
            range_mat_q = range_vec_q.unsqueeze(-1).expand(-1, klen)
            range_vec_k = torch.arange(klen, device=hidden_states.device)
            distance_mat = range_mat_q - range_vec_k
            distance_mat_clamp = distance_mat.clamp_(-self.max_relative_pos_len, self.max_relative_pos_len)
            relative_position = distance_mat_clamp.add_(self.max_relative_pos_len)  # [qlen ,klen,h_dim]
            relative_pos_embk = self.relative_pos_embeddingk(relative_position)  # [q ,k,h_dim/all_h_dim]
            relative_pos_embk = relative_pos_embk.to(dtype=query_layer.dtype)  # fp16 compatibility

            # einsum"bhld,lrd->bhlr"
            if not self.diff_head_pos:
                #  query_layer   [bsz, h_num, qlen, h_dim]  =>  [bsz*h_num, q, h_dim]
                query_layer_rel = query_layer.reshape(bsz * self.heads_num, qlen, self.head_size)
                query_layer_rel = query_layer_rel.transpose(0, 1)  # [q, bsz*h_num, h_dim]
                # [q, b*h_num, h_dim] [q ,h_dim, k] => [q, b*h_num, bsz, k]
                attn_scores_rel_k = torch.bmm(query_layer_rel, relative_pos_embk.transpose(1, 2))
                attn_scores_rel_k = attn_scores_rel_k.transpose(0, 1).reshape(bsz, self.heads_num, qlen, klen)

            else:
                query_layer_rel = query_layer.reshape(bsz, self.heads_num * qlen, self.head_size)  # [b, h_num*q, h_dim]
                query_layer_rel = query_layer_rel.transpose(0, 1)  # [h_num*q, bsz, h_dim]

                relative_pos_embk = relative_pos_embk.transpose(0, 1)  # [k, q, all_h_dim]
                # [k, q, h_num, h_dim] => [k, h_num, q, h_dim]
                relative_pos_embk = relative_pos_embk.reshape(klen, qlen, self.heads_num, self.head_size).transpose(1, 2)
                # [klen, h_num, qlen, h_dim] =>[k, h_num*q, h_dim] => [h_num*q, k, h_dim]
                relative_pos_embk = relative_pos_embk.reshape(klen, self.heads_num * qlen, self.head_size).transpose(0, 1)
                # [h_num*q, bsz, h_dim] [h_num*q, h_dim, k] => [h_num*q, bsz, k]
                attn_scores_rel_k = torch.bmm(query_layer_rel, relative_pos_embk.transpose(1, 2))
                attn_scores_rel_k = attn_scores_rel_k.transpose(0, 1).reshape(bsz, self.heads_num, qlen, klen)

            attn_scores_rel_k = attn_scores_rel_k / math.sqrt(self.head_size)
            attention_scores += attn_scores_rel_k  # [b h_num q v]

        if attention_mask is not None:
            # Apply the attention mask is (precomputed for all layers in BertModel forward() function)
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)

        # Mask heads if we want to
        if head_mask is not None:
            attention_probs = attention_probs * head_mask
        # [b, h_num, q,v] [b, h_num, v, h_dim] => [b, h_num, q ,dim]
        context_layer = torch.matmul(attention_probs, value_layer)
        if self.pos_emb_type == "relative_kv":

            relative_pos_embv = self.relative_pos_embeddingv(relative_position)  # [q , v, h_dim]
            relative_pos_embv = relative_pos_embv.to(dtype=query_layer.dtype)  # fp16 compatibility
            #  [b h_num q v] -> [q, b*h_num v]
            attention_scores = attention_probs.reshape(bsz*self.heads_num, qlen, vlen).transpose(0, 1)
            # [q, b*h_num v] [q , v, h_dim] -> [q, b*h_num h_dim] ->[b, h_num, q, h_dim]
            context_rel_v = torch.matmul(attention_scores, relative_pos_embv).transpose(0, 1)
            context_rel_v = context_rel_v.reshape(bsz, self.heads_num, qlen, self.head_size)
            context_layer += context_rel_v

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)
        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)
        return outputs

    def selfattn_step(self,
                      hidden_states,
                      attention_mask,
                      head_mask,
                      encoder_hidden_states=None,
                      encoder_attention_mask=None,
                      output_attentions=False,
                      buffer=None
                      ):
        # hidden_size -> all_head_size: 767 -> 768
        proj_query = self.query(hidden_states)  # [beam*bsz, 1(always), H]

        # If this is instantiated as a cross-attention module, the keys
        # and values come from an encoder; the attention mask needs to be
        # such that the encoder's padding tokens are not attended to.

        # enc_dec_attention
        if encoder_hidden_states is not None:  # use src mask， otherwise use tgt mask
            attention_mask = encoder_attention_mask

            if buffer is not None and 'src_k' in buffer and 'src_v' in buffer:  # no repeated computation needed
                proj_key = buffer['src_k']
                proj_value = buffer['src_v']
            else:
                if buffer is None:
                    buffer = dict()
                proj_key = self.key(encoder_hidden_states)
                proj_value = self.value(encoder_hidden_states)
                buffer['src_k'] = proj_key
                buffer['src_v'] = proj_value

        # decoder self-attention
        # hidden_states [bsz*beam, 1(always), H], bsz will decrease if finished e.g. bsz*beam:128,124...
        else:
            proj_key = self.key(hidden_states)
            proj_value = self.value(hidden_states)
            if buffer is not None and 'k' in buffer and 'v' in buffer:
                proj_key = torch.cat([buffer['k'], proj_key], dim=1)  # concat with previous time_step result
                buffer['k'] = proj_key
                proj_value = torch.cat([buffer['v'], proj_value], dim=1)  # time second
                buffer['v'] = proj_value
            else:
                if buffer is None:
                    buffer = dict()
                buffer['k'] = proj_key
                buffer['v'] = proj_value

        query_layer = self.transpose_for_scores(proj_query)  # [beam*bsz, h_num, 1, head_size]
        key_layer = self.transpose_for_scores(proj_key)
        value_layer = self.transpose_for_scores(proj_value)

        # Take the dot product between "query" and "key" to get the raw attention scores.
        attention_scores_qk = torch.matmul(query_layer, key_layer.transpose(-1, -2))
        # dec self-attention: [beam*bsz, h_num, 1(always), step]) bsz -->1 step 1-->qlen(klen)
        # dec cross-attention [beam*bsz, h_num, 1(always), src_len(klen)(always)]) bsz -->1
        attention_scores = attention_scores_qk / math.sqrt(self.head_size)

        # relative attention
        if self.pos_emb_type == "relative_k" or self.pos_emb_type == "relative_kv":
            qlen = buffer['k'].shape[1]  # always k, not src_k, so the current inference position in decoder
            klen = proj_key.shape[1]  # change based on self-attn(constant) or cross-attn(increase) accordingly
            vlen = klen
            bbsz = attention_scores_qk.shape[0]
            range_vec_q = torch.arange(qlen, device=hidden_states.device)[-1].unsqueeze(-1)
            range_mat_q = range_vec_q.unsqueeze(-1).expand(-1, klen)  # [1, klen]
            range_vec_k = torch.arange(klen, device=hidden_states.device)  # [1, klen]
            distance_mat = range_mat_q - range_vec_k
            distance_mat_clamp = distance_mat.clamp_(-self.max_relative_pos_len, self.max_relative_pos_len)
            relative_position = distance_mat_clamp.add_(self.max_relative_pos_len)  # [1, k], q is always 1
            relative_pos_embk = self.relative_pos_embeddingk(relative_position)  # [1, k, h_dim/all_h_dim]
            relative_pos_embk = relative_pos_embk.to(dtype=query_layer.dtype)  # fp16 compatibility

            if not self.diff_head_pos:
                # query_layer   [bbsz(beam*bsz), h_num, 1, h_dim]
                query_layer_rel = query_layer.reshape(bbsz * self.heads_num, 1, self.head_size)
                query_layer_rel = query_layer_rel.transpose(0, 1)  # [1, bsz*h_num, h_dim]
                # [1, bsz*h_num, h_dim] [1, h_dim, k] => [1, bsz*h_num, k]
                attention_scores_rel = torch.bmm(query_layer_rel, relative_pos_embk.transpose(1, 2))

                # [1, bsz*h_num, k] => [bbsz, h_num, 1, k]
                attention_scores_rel = attention_scores_rel.transpose(0, 1).reshape(bbsz, self.heads_num, 1, klen)
            else:
                relative_pos_embk = relative_pos_embk.transpose(0, 1)  # [ k, 1, all_h_dim]
                relative_pos_embk = relative_pos_embk.reshape(klen, 1, self.heads_num, self.head_size).transpose(1, 2)  # [k, h_num, 1, h_dim]
                relative_pos_embk = relative_pos_embk.reshape(klen, self.heads_num * 1, self.head_size).transpose(0, 1)  # [h_num* 1, klen, h_dim]
                # query_layer   [bbsz(beam*bsz), h_num, 1, h_dim]
                query_layer_rel = query_layer.reshape(bbsz, self.heads_num * 1, self.head_size)  # [bbsz, num*1, h_dim]
                query_layer_rel = query_layer_rel.transpose(0, 1)  # [h_num*1, bbsz, h_dim]
                attention_scores_rel = torch.bmm(query_layer_rel, relative_pos_embk.transpose(1, 2))  # [num, bbsz, klen]
                # [bsz, h_num, qlen, klen]
                attention_scores_rel = attention_scores_rel.transpose(0, 1).reshape(bbsz, self.heads_num, 1, klen)

            attention_scores_rel = attention_scores_rel / math.sqrt(self.head_size)
            attention_scores += attention_scores_rel

        if attention_mask is not None:
            attention_scores = attention_scores + attention_mask

        # Normalize the attention scores to probabilities.
        attention_probs = nn.Softmax(dim=-1)(attention_scores)

        # This is actually dropping out entire tokens to attend to, which might
        # seem a bit unusual, but is taken from the original Transformer paper.
        attention_probs = self.dropout(attention_probs)
        if head_mask is not None:
            attention_probs = attention_probs * head_mask

        # score: [bbsz, h_num, 1, v]  # [bbsz, h_num, v, head_size] -> [bbsz, num, 1, h_dim]
        context_layer = torch.matmul(attention_probs, value_layer)

        if self.pos_emb_type == "relative_kv":
            relative_pos_embv = self.relative_pos_embeddingv(relative_position)  # [1 , v, h_dim]
            relative_pos_embv = relative_pos_embv.to(dtype=query_layer.dtype)  # fp16 compatibility
            #  [bbsz h_num 1 v] -> [1, b*h_num v]
            attention_scores = attention_probs.reshape(bbsz*self.heads_num, 1, vlen).transpose(0, 1)
            # [1, bb*h_num v] [1 , v, h_dim] -> [1, bb*h_num h_dim] ->[bb*h_num, 1, h_dim]
            context_rel_v = torch.matmul(attention_scores, relative_pos_embv).transpose(0, 1)
            context_rel_v = context_rel_v.reshape(bbsz, self.heads_num, 1, self.head_size)
            context_layer += context_rel_v

        context_layer = context_layer.permute(0, 2, 1, 3).contiguous()
        new_context_layer_shape = context_layer.size()[:-2] + (self.all_head_size,)
        context_layer = context_layer.view(*new_context_layer_shape)

        outputs = (context_layer, attention_probs) if output_attentions else (context_layer,)
        return outputs, buffer


class BertSelfOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.bert_hidden_dropout)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertAttention(nn.Module):
    def __init__(self, config):
        super().__init__()

        self.self = BertSelfAttention(config)
        self.output = BertSelfOutput(config)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
    ):
        self_outputs = self.self(
            hidden_states, attention_mask, head_mask, encoder_hidden_states, encoder_attention_mask, output_attentions,
        )
        attention_output = self.output(self_outputs[0], hidden_states)
        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs

    def attn_step(
            self,
            hidden_states,
            attention_mask=None,
            head_mask=None,
            encoder_hidden_states=None,
            encoder_attention_mask=None,
            output_attentions=False,
            buffer=None
            ):

        self_outputs, buffer = self.self.selfattn_step(
            hidden_states,
            attention_mask,
            head_mask,
            encoder_hidden_states,
            encoder_attention_mask,
            output_attentions,
            buffer
        )

        # output: BertSelfOutput dropout--> add--> LN
        attention_output = self.output(self_outputs[0], hidden_states)
        outputs = (attention_output,) + self_outputs[1:]  # add attentions if we output them
        return outputs, buffer


class BertIntermediate(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.hidden_size, config.intermediate_size)
        if isinstance(config.hidden_act, str):
            self.intermediate_act_fn = ACT2FN[config.hidden_act]
        else:
            self.intermediate_act_fn = config.hidden_act

    def forward(self, hidden_states):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.intermediate_act_fn(hidden_states)
        return hidden_states


class BertOutput(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.dense = nn.Linear(config.intermediate_size, config.hidden_size)
        self.LayerNorm = BertLayerNorm(config.hidden_size, eps=config.layer_norm_eps)
        self.dropout = nn.Dropout(config.bert_hidden_dropout)

    def forward(self, hidden_states, input_tensor):
        hidden_states = self.dense(hidden_states)
        hidden_states = self.dropout(hidden_states)
        hidden_states = self.LayerNorm(hidden_states + input_tensor)
        return hidden_states


class BertLayer(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.attention = BertAttention(config)
        self.is_decoder = config.is_decoder
        if self.is_decoder:
            self.crossattention = BertAttention(config)
        self.intermediate = BertIntermediate(config)
        self.output = BertOutput(config)

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
    ):
        self_attention_outputs = self.attention(
            hidden_states, attention_mask, head_mask, output_attentions=output_attentions,
        )
        attention_output = self_attention_outputs[0]
        outputs = self_attention_outputs[1:]  # add self attentions if we output attention weights

        if self.is_decoder and encoder_hidden_states is not None:
            cross_attention_outputs = self.crossattention(
                attention_output,
                attention_mask,
                head_mask,
                encoder_hidden_states,
                encoder_attention_mask,
                output_attentions,
            )
            attention_output = cross_attention_outputs[0]
            outputs = outputs + cross_attention_outputs[1:]  # add cross attentions if we output attention weights

        intermediate_output = self.intermediate(attention_output)
        layer_output = self.output(intermediate_output, attention_output)
        outputs = (layer_output,) + outputs
        return outputs

    def bertlayer_step(
            self,
            hidden_states,
            attention_mask,
            head_mask=None,
            encoder_hidden_states=None,
            encoder_attention_mask=None,
            output_attentions=False,
            buffer=None
        ):

        self_attention_outputs, buffer = self.attention.attn_step(
            hidden_states,
            attention_mask,
            head_mask,
            output_attentions=output_attentions,
            buffer=buffer
        )
        attention_output = self_attention_outputs[0]  # context_layer
        outputs = self_attention_outputs[1:]  # (attention_probs,)add self attentions if we output attention weights

        cross_attention_outputs, buffer = self.crossattention.attn_step(
            attention_output,
            attention_mask,
            head_mask,
            encoder_hidden_states,
            encoder_attention_mask,
            output_attentions,
            buffer=buffer
        )
        attention_output = cross_attention_outputs[0]
        outputs = outputs + cross_attention_outputs[1:]  # add cross attentions if we output attention weights
        intermediate_output = self.intermediate(attention_output)
        # 1.dropout(intermediate_output) 2. add(attention_output) 3.LN
        layer_output = self.output(intermediate_output, attention_output)
        outputs = (layer_output,) + outputs
        return outputs, buffer


class BertEncoder(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        self.layer = nn.ModuleList([BertLayer(config) for _ in range(config.num_hidden_layers)])

    def forward(
        self,
        hidden_states,
        attention_mask=None,
        head_mask=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=False,
        output_hidden_states=False,
        return_dict=False,
    ):
        all_hidden_states = () if output_hidden_states else None
        all_attentions = () if output_attentions else None
        for i, layer_module in enumerate(self.layer):
            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            if getattr(self.config, "gradient_checkpointing", False) and self.training:

                def create_custom_forward(module):
                    def custom_forward(*inputs):
                        return module(*inputs, output_attentions)

                    return custom_forward

                layer_outputs = torch.utils.checkpoint.checkpoint(
                    create_custom_forward(layer_module),
                    hidden_states,
                    attention_mask,
                    head_mask[i],
                    encoder_hidden_states,
                    encoder_attention_mask,
                )
            else:
                layer_outputs = layer_module(
                    hidden_states,
                    attention_mask,
                    head_mask[i],
                    encoder_hidden_states,
                    encoder_attention_mask,
                    output_attentions,
                )
            hidden_states = layer_outputs[0]
            if output_attentions:
                all_attentions = all_attentions + (layer_outputs[1],)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(v for v in [hidden_states, all_hidden_states, all_attentions] if v is not None)
        return BaseModelOutput(
            last_hidden_state=hidden_states, hidden_states=all_hidden_states, attentions=all_attentions
        )


class BertPreTrainedModel(PreTrainedModel):
    """ An abstract class to handle weights initialization and
        a simple interface for downloading and loading pretrained models.
    """

    config_class = BertConfig
    base_model_prefix = "bert"
    authorized_missing_keys = [r"position_ids"]

    def _init_weights(self, module):
        """ Initialize the weights """
        if isinstance(module, nn.Linear):
            # Slightly different from the TF version which uses truncated_normal for initialization
            # cf https://github.com/pytorch/pytorch/pull/5617
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.bias is not None:
                module.bias.data.zero_()
        elif isinstance(module, nn.Embedding):
            module.weight.data.normal_(mean=0.0, std=self.config.initializer_range)
            if module.padding_idx is not None:
                module.weight.data[module.padding_idx].zero_()
        elif isinstance(module, nn.LayerNorm):
            module.bias.data.zero_()
            module.weight.data.fill_(1.0)


class BertModel(BertPreTrainedModel):
    """
    The model can behave as an encoder (with only self-attention) as well
    as a decoder, in which case a layer of cross-attention is added between
    the self-attention layers, following the architecture described in `Attention is all you need`_ by Ashish Vaswani,
    Noam Shazeer, Niki Parmar, Jakob Uszkoreit, Llion Jones, Aidan N. Gomez, Lukasz Kaiser and Illia Polosukhin.
    To behave as an decoder the model needs to be initialized with the
    :obj:`is_decoder` argument of the configuration set to :obj:`True`; an
    :obj:`encoder_hidden_states` is expected as an input to the forward pass.
    .. _`Attention is all you need`:
        https://arxiv.org/abs/1706.03762
    """

    def __init__(self, config,
                 bert_word_dropout=None,
                 bert_emb_dropout=None,
                 bert_atten_dropout=None,
                 bert_hidden_dropout=None,
                 bert_hidden_size=None,
                 is_decoder=False,
                 before_plm_output_ln=False,
                 gradient_checkpointing=False,
                 **kwargs
                 ):

        super().__init__(config)
        self.config = config
        if bert_word_dropout is not None:
            self.config.bert_word_dropout = bert_word_dropout
        if bert_emb_dropout is not None:
            self.config.bert_emb_dropout = bert_emb_dropout
        if bert_atten_dropout is not None:
            self.config.bert_atten_dropout = bert_atten_dropout
        if bert_hidden_dropout is not None:
            self.config.bert_hidden_dropout = bert_hidden_dropout
        if bert_hidden_size is not None:
            self.config.bert_hidden_size = bert_hidden_size

        self.config.max_relative_pos_len = kwargs.pop('max_pos_len', 0)
        self.config.diff_head_pos = kwargs.pop('diff_head_pos', False)
        self.config.pos_emb_type = kwargs.pop('pos_emb_type', "absolute")
        self.config.is_decoder = is_decoder
        self.config.before_plm_output_ln = before_plm_output_ln
        self.config.gradient_checkpointing = gradient_checkpointing

        self.embeddings = BertEmbeddings(self.config)
        self.encoder = BertEncoder(self.config)
        
        if self.config.before_plm_output_ln:
            self.before_plm_output_ln = BertLayerNorm(self.config.hidden_size, eps=self.config.layer_norm_eps)
        else:
            self.before_plm_output_ln = None

        self.init_weights()

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def _prune_heads(self, heads_to_prune):
        """ Prunes heads of the model.
            heads_to_prune: dict of {layer_num: list of heads to prune in this layer}
            See base class PreTrainedModel
        """
        for layer, heads in heads_to_prune.items():
            self.encoder.layer[layer].attention.prune_heads(heads)

    def forward(
        self,
        input_ids=None,
        attention_mask=None,
        token_type_ids=None,
        position_ids=None,
        head_mask=None,
        inputs_embeds=None,
        encoder_hidden_states=None,
        encoder_attention_mask=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        no_offset=False
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is not None:
            input_shape = input_ids.size()  # [bsz, src_len]
        elif inputs_embeds is not None:
            input_shape = inputs_embeds.size()[:-1]
        else:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        device = input_ids.device if input_ids is not None else inputs_embeds.device

        if attention_mask is None:
            attention_mask = torch.ones(input_shape, device=device)
        if token_type_ids is None:
            token_type_ids = torch.zeros(input_shape, dtype=torch.long, device=device)

        # We can provide a self-attention mask of dimensions [batch_size, from_seq_length, to_seq_length]
        # ourselves in which case we just need to make it broadcastable to all heads.
        extended_attention_mask: torch.Tensor = self.get_extended_attention_mask(attention_mask, input_shape, device)

        # If a 2D ou 3D attention mask is provided for the cross-attention
        # we need to make broadcastabe to [batch_size, num_heads, seq_length, seq_length]
        if self.config.is_decoder and encoder_hidden_states is not None:
            encoder_batch_size, encoder_sequence_length, _ = encoder_hidden_states.size()
            encoder_hidden_shape = (encoder_batch_size, encoder_sequence_length)
            if encoder_attention_mask is None:
                encoder_attention_mask = torch.ones(encoder_hidden_shape, device=device)
            encoder_extended_attention_mask = self.invert_attention_mask(encoder_attention_mask)
        else:
            encoder_extended_attention_mask = None

        # Prepare head mask if needed
        # 1.0 in head_mask indicate we keep the head
        # attention_probs has shape bsz x n_heads x N x N
        # input head_mask has shape [num_heads] or [num_hidden_layers x num_heads]
        # and head_mask is converted to shape [num_hidden_layers x batch x num_heads x seq_length x seq_length]
        head_mask = self.get_head_mask(head_mask, self.config.num_hidden_layers)  # default [None] * n_head

        embedding_output = self.embeddings(
            input_ids=input_ids,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
            inputs_embeds=inputs_embeds,
            no_emb_offset=no_offset,
        )  # [bsz, src_len, hidden_dim]
        encoder_outputs = self.encoder(
            embedding_output,
            attention_mask=extended_attention_mask,
            head_mask=head_mask,
            encoder_hidden_states=encoder_hidden_states,
            encoder_attention_mask=encoder_extended_attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        if self.before_plm_output_ln is not None:
            sequence_output = self.before_plm_output_ln(encoder_outputs[0])
        else:
            sequence_output = encoder_outputs[0]
        return (sequence_output, ) + encoder_outputs[1:]

    def step(self, input_ids, decoder_state, streaming=False):
        device = input_ids.device

        if input_ids.size(1) > 1:
            input_ = input_ids[:, -1].unsqueeze(1)
        else:
            input_ = input_ids
        tgt_token_type = input_.ne(onmt.constants.TGT_PAD).long()  # [bsz, len]
        data_type = next(self.parameters()).dtype

        src_mask = decoder_state.src_mask.squeeze(1)  # [bsz, all_src_len]

        extended_src_mask = self.invert_attention_mask(src_mask)

        mask_tgt = input_ids.ne(onmt.constants.TGT_PAD).byte()
        input_shape = input_ids.size()  # [bsz, sent_len]
        cur_pos = input_shape[-1]
        extended_tgt_mask = self.get_extended_attention_mask(mask_tgt, input_shape, device=device)

        extended_tgt_mask = extended_tgt_mask[:, :, -1, :].unsqueeze(-2)
        encoder_hidden_states = decoder_state.context.transpose(0, 1)  # [b, l, de_model]
        encoder_batch_size, encoder_sequence_length, _ = encoder_hidden_states.size()

        head_mask = None
        head_mask = self.get_head_mask(head_mask, self.config.num_hidden_layers, data_type)

        if self.dec_pretrained_model == "bert" or self.dec_pretrained_model == "roberta":
            embedding_output = self.embeddings.emb_step(cur_pos, input_, tgt_token_type)
        else:
            print("Warning: check dec_pretrained_model", self.dec_pretrained_model)
            exit(-1)

        hidden_states = embedding_output
        output_attentions = False
        buffers = decoder_state.attention_buffers

        for i, layer in enumerate(self.encoder.layer):
            buffer = buffers[i] if i in buffers else None
            layer_outputs, buffer = layer.bertlayer_step(
                hidden_states,
                extended_tgt_mask,
                head_mask[i],
                encoder_hidden_states,  # decoder_state.context
                extended_src_mask,  # decoder_state.src_mask
                output_attentions,
                buffer
            )

            hidden_states = layer_outputs[0]
            decoder_state.update_attention_buffer(buffer, i)

        output_dict = defaultdict(lambda: None)
        output_dict["hidden"] = hidden_states
        # output_dict["coverage"] = buffers[i]

        return output_dict

    def renew_buffer(self, new_len):

        # not sure about this
        # self.positional_encoder.renew(new_len)
        mask = torch.ByteTensor(np.triu(np.ones((new_len+1, new_len+1)), k=1).astype('uint8'))
        self.register_buffer('mask', mask)
