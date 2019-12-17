import torch
import torch.nn as nn
import onmt

from onmt.models.transformer_layers import PrePostProcessing, MultiHeadAttention, Linear
from onmt.modules.relative_attention import RelPartialLearnableMultiHeadAttn
from onmt.utils import flip
from onmt.modules.bottle import Bottle
from onmt.modules.linear import XavierLinear as Linear
from onmt.modules.linear import XavierLinear
from onmt.modules.linear import group_linear, FeedForwardSwish, FeedForward
from onmt.modules.attention import MultiHeadAttention
from onmt.modules.dropout import VariationalDropout
from onmt.modules.relative_attention import RelPartialLearnableMultiHeadAttn


class RelativeTransformerEncoderLayer(nn.Module):
    def __init__(self, h, d_model, p, d_ff, attn_p=0.1, variational=False, death_rate=0.0, **kwargs):
        super(RelativeTransformerEncoderLayer, self).__init__()
        self.variational = variational
        self.death_rate = death_rate

        self.preprocess_attn = PrePostProcessing(d_model, p, sequence='n')
        self.postprocess_attn = PrePostProcessing(d_model, p, sequence='da', variational=self.variational)
        self.preprocess_ffn = PrePostProcessing(d_model, p, sequence='n')
        self.postprocess_ffn = PrePostProcessing(d_model, p, sequence='da', variational=self.variational)
        # self.multihead = MultiHeadAttention(h, d_model, attn_p=attn_p, share=2)
        d_head = d_model // h
        self.multihead = RelPartialLearnableMultiHeadAttn(h, d_model, d_head, dropatt=attn_p)

        if onmt.constants.activation_layer == 'linear_relu_linear':
            ff_p = p
            feedforward = FeedForward(d_model, d_ff, ff_p, variational=self.variational)
        elif onmt.constants.activation_layer == 'maxout':
            k = int(math.ceil(d_ff / d_model))
            feedforward = MaxOut(d_model, d_model, k)
        elif onmt.constants.activation_layer == 'linear_swish_linear':
            ff_p = p
            feedforward = FeedForwardSwish(d_model, d_ff, ff_p, variational=self.variational)
        else:
            raise NotImplementedError

        self.feedforward = Bottle(feedforward)

    # def forward(self, input, pos_emb, r_w_bias, r_r_bias, attn_mask):
    def forward(self, input, pos_emb, attn_mask):

        coin = True
        if self.training and self.death_rate > 0:
            coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:

            query = self.preprocess_attn(input)
            out, _ = self.multihead(query, pos_emb, attn_mask=attn_mask)

            # rescaling before residual
            if self.training and self.death_rate > 0:
                out = out / (1 - self.death_rate)

            input = self.postprocess_attn(out, input)

            """ Feed forward layer 
                layernorm > ffn > dropout > residual
            """
            out = self.feedforward(self.preprocess_ffn(input))

            # rescaling before residual
            if self.training and self.death_rate > 0:
                out = out / (1 - self.death_rate)

            input = self.postprocess_ffn(out, input)

        return input


class RelativeTransformerDecoderLayer(nn.Module):

    def __init__(self, h, d_model, p, d_ff, attn_p=0.1, version=1.0, ignore_source=False,
                 variational=False, death_rate=0.0):
        super(RelativeTransformerDecoderLayer, self).__init__()
        self.version = version
        self.ignore_source = ignore_source
        self.variational = variational
        self.death_rate = death_rate

        self.preprocess_attn = PrePostProcessing(d_model, p, sequence='n')
        self.postprocess_attn = PrePostProcessing(d_model, p, sequence='da', variational=self.variational)

        if not self.ignore_source:
            self.preprocess_src_attn = PrePostProcessing(d_model, p, sequence='n')
            self.postprocess_src_attn = PrePostProcessing(d_model, p, sequence='da', variational=self.variational)
            self.multihead_src = MultiHeadAttention(h, d_model, attn_p=attn_p, share=2)

        self.preprocess_ffn = PrePostProcessing(d_model, p, sequence='n')
        self.postprocess_ffn = PrePostProcessing(d_model, p, sequence='da', variational=self.variational)

        d_head = d_model // h
        self.multihead_tgt = RelPartialLearnableMultiHeadAttn(h, d_model, d_head, dropatt=attn_p)
        # self.multihead_tgt = MultiHeadAttention(h, d_model, attn_p=attn_p, share=1)

        if onmt.constants.activation_layer == 'linear_relu_linear':
            ff_p = p
            feedforward = FeedForward(d_model, d_ff, ff_p, variational=self.variational)
        elif onmt.constants.activation_layer == 'maxout':
            k = int(math.ceil(d_ff / d_model))
            feedforward = MaxOut(d_model, d_model, k)
        elif onmt.constants.activation_layer == 'linear_swish_linear':
            ff_p = p
            feedforward = FeedForwardSwish(d_model, d_ff, ff_p)
        else:
            raise NotImplementedError
        self.feedforward = Bottle(feedforward)

    # def forward(self, input, context, pos_emb, r_w_bias, r_r_bias, mask_tgt, mask_src):
    def forward(self, input, context, pos_emb, mask_tgt, mask_src):

        """ Self attention layer
            layernorm > attn > dropout > residual
        """

        coin = True
        if self.training and self.death_rate > 0:
            coin = (torch.rand(1)[0].item() >= self.death_rate)

        if coin:
            # input and context should be time first ?
            query = self.preprocess_attn(input)

            # out, _ = self.multihead_tgt(query, pos_emb, r_w_bias, r_r_bias, attn_mask=mask_tgt)
            out, _ = self.multihead_tgt(query, pos_emb, attn_mask=mask_tgt)

            # rescaling before residual
            if self.training and self.death_rate > 0:
                out = out / (1 - self.death_rate)

            input = self.postprocess_attn(out, input)

            """ Context Attention layer 
                layernorm > attn > dropout > residual
            """
            if not self.ignore_source:
                query = self.preprocess_src_attn(input)
                out, coverage = self.multihead_src(query, context, context, mask_src)

                # rescaling before residual
                if self.training and self.death_rate > 0:
                    out = out / (1 - self.death_rate)

                input = self.postprocess_src_attn(out, input)
            else:
                coverage = None

            """ Feed forward layer 
                layernorm > ffn > dropout > residual
            """
            out = self.feedforward(self.preprocess_ffn(input))

            # rescaling before residual
            if self.training and self.death_rate > 0:
                out = out / (1 - self.death_rate)

            input = self.postprocess_ffn(out, input)
        else:
            coverage = None

        return input, coverage

    def step(self, input, context, pos_emb, r_w_bias, r_r_bias, mask_tgt, mask_src, buffer=None):
        """ Self attention layer
            layernorm > attn > dropout > residual
        """
        raise NotImplementedError

        query = self.preprocess_attn(input)

        out, _, buffer = self.multihead_tgt.step(query, pos_emb, r_w_bias, r_r_bias, attn_mask=mask_tgt, buffer=buffer)

        input = self.postprocess_attn(out, input)

        """ Context Attention layer
            layernorm > attn > dropout > residual
        """
        if not self.ignore_source:
            query = self.preprocess_src_attn(input)
            out, coverage, buffer = self.multihead_src.step(query, context, context, mask_src, buffer=buffer)
            input = self.postprocess_src_attn(out, input)
        else:
            coverage = None

        """ Feed forward layer
            layernorm > ffn > dropout > residual
        """
        out = self.feedforward(self.preprocess_ffn(input))
        input = self.postprocess_ffn(out, input)

        return input, coverage, buffer
#
#     def forward(self, input, pos, r_w_bias, r_r_bias, mask, mems=None):
#
#         """
#         :param mems: The hidden layers from the previous segments
#         :param mask: The attention mask to avoid padding
#         :param input: Embedding (from the last layer) T x B x H
#         :param pos: Positional Encoding T x B x H
#         :return:
#         """
#
#         # input and context should be time first ?
#
#         if mems is not None:
#             cat = torch.cat([mems, input], 0)
#             query = self.preprocess_attn(cat)
#         else:
#             query = self.preprocess_attn(input)
#
#         out, coverage = self.multihead_tgt(query, pos, r_w_bias, r_r_bias, mask)
#
#         # dropout + residual
#         input = self.postprocess_attn(out, input)
#
#         """ Feed forward layer
#             layernorm > ffn > dropout > residual
#         """
#         out = self.feedforward(self.preprocess_ffn(input))
#         input = self.postprocess_ffn(out, input)
#
#         return input, coverage

    # def step(self, input, pos, context, mask_tgt, mask_src, buffer=None):
    #     """ Self attention layer
    #         layernorm > attn > dropout > residual
    #     """
    #
    #     query = self.preprocess_attn(input)
    #
    #     out, _, buffer = self.multihead_tgt.step(query, pos, mask_tgt, buffer=buffer)
    #
    #     input = self.postprocess_attn(out, input)
    #
    #     """ Context Attention layer
    #         layernorm > attn > dropout > residual
    #     """
    #     if not self.ignore_source:
    #         query = self.preprocess_src_attn(input)
    #         out, coverage, buffer = self.multihead_src.step(query, context, context, mask_src, buffer=buffer)
    #         input = self.postprocess_src_attn(out, input)
    #     else:
    #         coverage = None
    #
    #     """ Feed forward layer
    #         layernorm > ffn > dropout > residual
    #     """
    #     out = self.feedforward(self.preprocess_ffn(input))
    #     input = self.postprocess_ffn(out, input)
    #
    #     return input, coverage, buffer


# class RelativeEncoderLayer(EncoderLayer):