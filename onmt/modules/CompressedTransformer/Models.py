import numpy as np
import torch, math
import torch.nn as nn
import onmt
from onmt.modules.Transformer.Models import TransformerEncoder, TransformerDecoder, TransformerDecodingState
from onmt.modules.Transformer.Layers import EncoderLayer, DecoderLayer
from onmt.modules.BaseModel import NMTModel
from onmt.modules.WordDrop import embedded_dropout
from torch.utils.checkpoint import checkpoint
from onmt.modules.Utilities import mean_with_mask_backpropable as mean_with_mask
from onmt.modules.Utilities import max_with_mask
from onmt.modules.Transformer.Layers import PrePostProcessing
from onmt.modules.GlobalAttention import MultiHeadAttention
from onmt.modules.Linear import FeedForward


def custom_layer(module):
    def custom_forward(*args):
        output = module(*args)
        return output
    return custom_forward


class CompressedTransformerEncoder(TransformerEncoder):
    """Encoder in 'Attention is all you need'

    Args:
        opt
        dicts


    """

    def __init__(self, opt, embedding, positional_encoder, share=None):

        self.layers = opt.layers
        self.n_encoder_heads = opt.n_encoder_heads
        self.model_size = opt.model_size
        self.pooling = opt.var_pooling

        # build_modules will be called from the inherited constructor
        super(CompressedTransformerEncoder, self).__init__(opt, embedding, positional_encoder, share=share)


    def build_modules(self, shared_encoder=None):

        self.dropout_layer = nn.Dropout(self.dropout)

        if shared_encoder is not None:
            assert(isinstance(shared_encoder, self.__class__))
            print("* This encoder is Sharing parameters with another encoder")
            self.layer_modules = shared_encoder.layer_modules

            self.postprocess_layer = shared_encoder.postprocess_layer

            # self.projector = shared_encoder.projector

            self.post_attention = shared_encoder.post_attention
            self.query = shared_encoder.query

            self.ffn_norm = shared_encoder.ffn_norm
            self.final_norm = shared_encoder.final_norm

            self.feed_forward = shared_encoder.feed_forward
        else:
            print("* Creating a compressed attention encoder layer")
            self.layer_modules = nn.ModuleList([EncoderLayer(self.n_heads, self.model_size, self.dropout,
                                                             self.inner_size, self.attn_dropout, self.residual_dropout)
                                                for _ in range(self.layers)])

            # self.postprocess_layer = PrePostProcessing(self.model_size, 0, sequence='n')

            # learnable query
            self.query = nn.Parameter(torch.randn(self.n_encoder_heads, 1, self.model_size))

            self.post_attention = MultiHeadAttention(self.n_heads, self.model_size, self.dropout, share=2)
            self.final_norm = PrePostProcessing(self.model_size, self.dropout, sequence='n')
            self.ffn_norm = PrePostProcessing(self.model_size, self.dropout, sequence='n')

            # init the learnable query
            torch.nn.init.kaiming_uniform_(self.query, a=math.sqrt(5))

            self.feed_forward = FeedForward(self.model_size, self.inner_size, self.dropout)

    def forward(self, input, freeze_embedding=False, return_stack=False, additional_sequence=None, **kwargs):
        """
        Inputs Shapes:
            input: batch_size x len_src (wanna tranpose)

        Outputs Shapes:
            out: batch_size x len_src x d_model
            mask_src

        """

        """ Embedding: batch_size x len_src x d_model """

        add_emb = None
        if freeze_embedding:
            with torch.no_grad():
                emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)

                """ Scale the emb by sqrt(d_model) """
                emb = emb * math.sqrt(self.model_size)

                if additional_sequence is not None:
                    add_input = additional_sequence
                    add_emb = embedded_dropout(self.word_lut, add_input,
                                               dropout=self.word_dropout if self.training else 0)

                    # emb = torch.cat([emb, add_emb], dim=0)
        else:
            emb = embedded_dropout(self.word_lut, input, dropout=self.word_dropout if self.training else 0)

            """ Scale the emb by sqrt(d_model) """
            emb = emb * math.sqrt(self.model_size)

            if additional_sequence is not None:
                add_input = additional_sequence
                add_emb = embedded_dropout(self.word_lut, add_input, dropout=self.word_dropout if self.training else 0)

                # emb = torch.cat([emb, add_emb], dim=0)

        """ Adding positional encoding """
        emb = self.time_transformer(emb)

        if add_emb is not None:
            add_emb = self.time_transformer(add_emb)

            # batch first
            emb = torch.cat([emb, add_emb], dim=1)
            input = torch.cat([input, additional_sequence], dim=1)

        emb = self.preprocess_layer(emb)

        mask_src = input.eq(onmt.Constants.PAD).unsqueeze(1)  # batch_size x 1 x len_src for broadcasting

        # time first
        context = emb.transpose(0, 1).contiguous()

        if return_stack == False:

            for i, layer in enumerate(self.layer_modules):

                if len(self.layer_modules) - i <= onmt.Constants.checkpointing and self.training:
                    context = checkpoint(custom_layer(layer), context, mask_src)

                else:
                    context = layer(context, mask_src)  # batch_size x len_src x d_model

            # From Google T2T
            # if normalization is done in layer_preprocess, then it should also be done
            # on the output, since the output can grow very large, being the sum of
            # a whole stack of unnormalized layer outputs.
            context = self.postprocess_layer(context)

        else:
            raise NotImplementedError

        batch_size = context.size(1)

        # expand to batch size
        query = self.query.expand(self.n_encoder_heads, batch_size, self.model_size)

        # attention
        attn_output, _ = self.post_attention(query, context, context, mask_src)

        ffn_input = self.ffn_norm(attn_output)

        ffn_output = self.feed_forward(ffn_input)

        # dropout and residual
        output = self.dropout_layer(ffn_output) + attn_output
        output = self.final_norm(output)

        mask_src = output.new(output.size(1), output.size(0)).fill_(onmt.Constants.EOS)

        return output, mask_src
