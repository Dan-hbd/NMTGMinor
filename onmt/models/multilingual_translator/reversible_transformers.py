import torch
import torch.nn as nn
import torch.nn.functional as F
from onmt.models.transformer_layers import PrePostProcessing
from onmt.modules.attention import MultiHeadAttention
from onmt.modules.optimized.relative_self_attention import RelativeSelfMultiheadAttn
from onmt.modules.optimized.encdec_attention import EncdecMultiheadAttn
from onmt.modules.optimized.feed_forward import PositionWiseFeedForward
from onmt.modules.layer_norm import LayerNorm
from torch.autograd.function import Function
from torch.utils.checkpoint import get_device_states, set_device_states
from onmt.modules.dropout import variational_dropout


class RelativeSelfAttention(nn.Module):

    def __init__(self, opt):
        super().__init__()
        # self.layer_norm = PrePostProcessing(opt.model_size, opt.dropout, sequence='n')
        self.layer_norm = LayerNorm((opt.model_size,), elementwise_affine=True)
        self.residual_dropout = opt.residual_dropout if opt.residual_dropout >= 0 else opt.dropout
        self.attn = RelativeSelfMultiheadAttn(opt.model_size, opt.n_heads, dropout=opt.attn_dropout,
                                              learnable_pos=opt.learnable_position_encoding,
                                              max_pos=opt.max_pos_length)
        self.variational = opt.variational_dropout

    def forward(self, input, pos, key_padding_mask=None, attn_mask=None, incremental=False,
                incremental_cache=None, cleaning=False):
        q = self.layer_norm(input)
        attn, coverage = self.attn(q, pos, key_padding_mask=key_padding_mask,
                                   attn_mask=attn_mask,
                                   incremental=incremental, incremental_cache=incremental_cache)

        if not self.variational:
            o = F.dropout(attn, p=self.residual_dropout, training=self.training, inplace=False)
        else:
            o = variational_dropout(attn, p=self.residual_dropout, inplace=False, training=self.training)

        if cleaning:
            del q, attn
        return o, coverage


class FeedForward(nn.Module):

    def __init__(self, opt):
        super().__init__()
        self.layer_norm = nn.LayerNorm((opt.model_size,), elementwise_affine=True)
        self.residual_dropout = opt.residual_dropout if opt.residual_dropout >= 0 else opt.dropout
        self.ffn_dropout = opt.ffn_dropout if opt.ffn_dropout >= 0 else opt.dropout
        self.feedforward = PositionWiseFeedForward(opt.model_size, opt.inner_size, self.ffn_dropout,
                                                   variational=opt.variational_dropout, glu=opt.ffn_glu,
                                                   activation=opt.ffn_activation)
        self.variational = opt.variational_dropout

    def forward(self, input, cleaning=False):

        x_norm = self.layer_norm(input)
        x_ff = self.feedforward(x_norm)

        if not self.variational:
            o = F.dropout(x_ff, p=self.residual_dropout, training=self.training, inplace=False)
        else:
            o = variational_dropout(x_ff, p=self.residual_dropout, inplace=False, training=self.training)

        if cleaning:
            del x_norm, x_ff

        return o


class SourceAttention(nn.Module):

    def __init__(self, opt):
        super().__init__()
        self.layer_norm = nn.LayerNorm((opt.model_size,), elementwise_affine=True)
        self.residual_dropout = opt.residual_dropout if opt.residual_dropout >= 0 else opt.dropout
        self.attn = EncdecMultiheadAttn(opt.n_heads, opt.model_size, attn_drop=opt.attn_dropout)
        self.dropout = opt.attn_dropout
        self.variational = opt.variational_dropout

    def forward(self, input, context, attn_mask=None, incremental=False, incremental_cache=None, cleaning=False):
        q = self.layer_norm(input)
        attn, coverage = self.attn(q, context, context, attn_mask, incremental, incremental_cache)

        if not self.variational:
            o = F.dropout(attn, p=self.residual_dropout, training=self.training, inplace=False)
        else:
            o = variational_dropout(attn, p=self.residual_dropout, inplace=False, training=self.training)

        if cleaning:
            del q, attn
        return o, coverage


class ReversibleEncoderFunction(Function):

    @staticmethod
    def forward(ctx, layers, hidden_states, pos, attn_mask):

        # attn_output, hidden_states = hidden_states, hidden_states # torch.chunk(hidden_states, 2, dim=-1)
        first_input, second_input = hidden_states, hidden_states

        # this block should be run under torch.no_grad()?
        with torch.no_grad():
            for layer in layers:
                # forward pass in the layer
                first_input, second_input = layer(
                    first_input, second_input, pos, attn_mask
                )

        # attach params to ctx for backward

        # why should we detach here? because Y1 Y2 were built within torch.no_grad()
        # so cutting the backward from these variables seems unnecessary

        # save_for_backward will release memory more efficiently
        ctx.save_for_backward(first_input.clone().detach(), second_input, pos)
        ctx.layers = layers
        ctx.attn_mask = attn_mask  # just in case attn_mask is None

        with torch.no_grad():
            output = first_input + second_input
            output.div_(2)

        # The only memory footprint is the last layer outputs and the "output".

        return output

    @staticmethod
    def backward(ctx, grad_output):

        grad_output.mul_(0.5)
        first_grad_output, second_grad_output = grad_output, grad_output

        # retrieve params from ctx
        first_output, second_output, pos = ctx.saved_tensors
        layers = ctx.layers
        attn_mask = ctx.attn_mask

        for idx, layer in enumerate(layers[::-1]):
            # backprop
            first_output, second_output, first_grad_output, second_grad_output = layer.backward_pass(
                first_output, second_output, first_grad_output, second_grad_output, pos, attn_mask
            )

        grad_hidden_states = first_grad_output + second_grad_output

        # the position encodings don't need embeddings
        return None, grad_hidden_states, None, None


def reversible_encoder(layers, hidden_states, pos, attn_mask):
    return ReversibleEncoderFunction.apply(layers, hidden_states, pos, attn_mask)


class ReversibleTransformerEncoderLayer(nn.Module):

    def __init__(self, opt, death_rate=0.0):
        super().__init__()
        self.self_attn = RelativeSelfAttention(opt)
        self.feedforward = FeedForward(opt)
        self.death_rate = death_rate
        self.forward_coin = True

    def _init_attention_seed(self, *args):
        """
            This function sets a new seed for the
            attention layer to make dropout deterministic
            for both forward calls: 1 normal forward
            call and 1 forward call in backward
            to recalculate activations.
        """

        self.attn_cpu_state = torch.get_rng_state()
        self.attn_gpu_devices, self.attn_gpu_states = get_device_states(*args)

    def _init_feedforward_seed(self, *args):
        """
                    This function sets a new seed for the
                    feed forward layer to make dropout deterministic
                    for both forward calls: 1 normal forward
                    call and 1 forward call in backward
                    to recalculate activations.
                """

        self.ffn_cpu_state = torch.get_rng_state()
        self.ffn_gpu_devices, self.ffn_gpu_states = get_device_states(*args)

    def forward(self, x1, x2, pos, attn_mask=None):
        """
        :param pos: position embeddings
        :param x2:
        :param x1:
        :param attn_mask:
        :return:
        """

        # every forward pass we sample a different seed
        # for dropout and save for forward fn in backward pass
        # to have correct dropout

        self._init_attention_seed(x2, pos, attn_mask)
        z1, coverage = self.self_attn(x2, pos, key_padding_mask=attn_mask, attn_mask=None, cleaning=True)

        y1 = z1 + x1

        self._init_feedforward_seed(y1)
        z2 = self.feedforward(y1, cleaning=True)

        y2 = z2 + x2

        del x1, x2, z1, z2

        """return Y1 and Y2"""
        return y1, y2

    def backward_pass(self, y1, y2, dy1, dy2, pos, attn_mask=None):
        """
        :param pos:
        :param y1:
        :param y2:
        :param dy1:
        :param dy2:
        :param attn_mask:
        :return:
        """
        """Implementation of the backward pass for reversible transformer encoder"""

        with torch.enable_grad():
            y1.requires_grad = True
            with torch.random.fork_rng(devices=self.ffn_gpu_devices, enabled=True):
                torch.set_rng_state(self.ffn_cpu_state)
                set_device_states(self.ffn_gpu_devices, self.ffn_gpu_states)

                gy1 = self.feedforward(y1)

            gy1.backward(dy2)

        with torch.no_grad():
            # restore X2 = Y2 - G(Y1)
            x2 = y2 - gy1
            del gy1, y2

            dx1 = dy1 + y1.grad
            del dy1
            y1.grad = None

        with torch.enable_grad():
            x2.requires_grad = True

            with torch.random.fork_rng(devices=self.attn_gpu_devices, enabled=True):
                torch.set_rng_state(self.attn_cpu_state)
                set_device_states(self.attn_gpu_devices, self.attn_gpu_states)

                fx2, _, = self.self_attn(x2, pos, key_padding_mask=attn_mask)

            fx2.backward(dx1)

        with torch.no_grad():
            # restore X1 = Y1 - F(X2)
            x1 = y1 - fx2
            del y1, fx2

            dx2 = dy2 + x2.grad
            x2.grad = None
            del dy2

        return x1, x2, dx1, dx2


##################################################
############## DECODER FUNCTION ##################
##################################################


class ReversibleDecoderFunction(Function):

    @staticmethod
    def forward(ctx, layers, hidden_states, pos, context, tgt_mask, src_mask,
                incremental=False, incremental_cache=None):

        bsz, seq_len = hidden_states.shape[0], hidden_states.shape[1]
        B = bsz * seq_len
        idx = 0
        x1, x2 = hidden_states, hidden_states
        coverages = []

        for layer in layers:
            idx = idx + 1
            # forward pass in the layer
            x1, x2, coverage_src = layer(
                x1, x2, pos, context, tgt_mask, src_mask,
                incremental=incremental, incremental_cache=incremental_cache
            )

            coverages.append(coverage_src)

        # attach params to ctx for backward

        # why should we detach here? because Y1 Y2 were built within torch.no_grad()
        # so cutting the backward from these variables seems unnecessary

        # save_for_backward will release memory more efficiently
        # detach() seems to be required especially for context ...
        ctx.save_for_backward(x1.clone().detach(), x2, context, pos)
        ctx.layers = layers
        ctx.src_mask = src_mask
        ctx.tgt_mask = tgt_mask

        with torch.no_grad():
            output = x1 + x2

        # concatenate 2 revnet outputs:
        return output.mul_(0.5), torch.stack(coverages)

    @staticmethod
    def backward(ctx, grad_hidden_states, grad_coverage):
        # We need three arguments because the forward pass returned 3 arguments
        # grad_attn_output, grad_hidden_states = torch.chunk(grad_hidden_states, 2, dim=-1)
        grad_hidden_states.mul_(0.5)
        dx1, dx2 = grad_hidden_states, grad_hidden_states

        # retrieve params from ctx
        x1, x2, context, pos = ctx.saved_tensors
        layers = ctx.layers
        src_mask = ctx.src_mask
        tgt_mask = ctx.tgt_mask
        grad_context = None  # we need to sum up the gradients of the context manually

        for idx, layer in enumerate(layers[::-1]):

            """Note: Here for each layer we detach the context once because we need to consider it
            as a separate variable and then later accumulate the gradients"""
            x1, x2, dx1, dx2, grad_context_ = layer.backward_pass(
                x1, x2, dx1, dx2,
                pos, context.detach(), tgt_mask, src_mask
            )

            if grad_context is None:
                grad_context = grad_context_
            elif grad_context_ is not None:  # prevent ignoring layer making this None
                grad_context.add_(grad_context_)
                del grad_context_

        grad_input = dx1 + dx2

        # grad pos is also None
        return None, grad_input, None, grad_context, None, None, None, None


def reversible_decoder(layers, hidden_states, pos, context, tgt_mask, src_mask, incremental, incremental_cache):
    return ReversibleDecoderFunction.apply(layers, hidden_states, pos, context,
                                           tgt_mask, src_mask, incremental, incremental_cache)


class ReversibleTransformerDecoderLayer(nn.Module):

    def __init__(self, opt, death_rate=0.0):
        super(ReversibleTransformerDecoderLayer, self).__init__()

        self.ignore_source = opt.ignore_source
        assert not self.ignore_source
        self.variational = opt.variational_dropout
        self.death_rate = death_rate
        self.dropout = opt.dropout

        self.self_attention = RelativeSelfAttention(opt)
        self.feed_forward_first = FeedForward(opt)
        if not self.ignore_source:
            self.src_attention = SourceAttention(opt)
        self.feed_forward_second = FeedForward(opt)

    def _init_src_attention_seed(self, *args):
        """
            This function sets a new seed for the
            attention layer to make dropout deterministic
            for both forward calls: 1 normal forward
            call and 1 forward call in backward
            to recalculate activations.
        """

        self.src_attn_cpu_state = torch.get_rng_state()
        self.src_attn_gpu_devices, self.src_attn_gpu_states = get_device_states(*args)

    def _init_attention_seed(self, *args):
        """
            This function sets a new seed for the
            attention layer to make dropout deterministic
            for both forward calls: 1 normal forward
            call and 1 forward call in backward
            to recalculate activations.
        """

        # randomize seeds
        self.attn_cpu_state = torch.get_rng_state()
        self.attn_gpu_devices, self.attn_gpu_states = get_device_states(*args)

    def _init_feedforward1_seed(self, *args):
        """
                    This function sets a new seed for the
                    feed forward layer to make dropout deterministic
                    for both forward calls: 1 normal forward
                    call and 1 forward call in backward
                    to recalculate activations.
                """

        # randomize seeds
        self.ffn1_cpu_state = torch.get_rng_state()
        self.ffn1_gpu_devices, self.ffn1_gpu_states = get_device_states(*args)

    def _init_feedforward2_seed(self, *args):
        """
                    This function sets a new seed for the
                    feed forward layer to make dropout deterministic
                    for both forward calls: 1 normal forward
                    call and 1 forward call in backward
                    to recalculate activations.
                """

        # randomize seeds
        self.ffn2_cpu_state = torch.get_rng_state()
        self.ffn2_gpu_devices, self.ffn2_gpu_states = get_device_states(*args)

    def forward(self, x1, x2, pos, context, mask_tgt, mask_src,
                incremental=False, incremental_cache=None, reuse_source=True):
        """
        :param pos:
        :param x1: X1
        :param x2: X2
        :param context:
        :param mask_tgt:
        :param mask_src:
        :param incremental:
        :param incremental_cache:
        :param reuse_source:
        :return:
        """
        # if self.training:
        #     coin = (torch.rand(1)[0].item() >= self.death_rate)
        #
        # self.forward_coin = coin

        with torch.no_grad():
            # prepare the state for the first function (att > src->att)
            self._init_attention_seed(x2, pos)
            f_x2, coverage, = self.self_attention(x2, pos,
                                                  key_padding_mask=None, attn_mask=mask_tgt,
                                                  incremental=incremental,
                                                  incremental_cache=incremental_cache,
                                                  cleaning=True)

            z1 = f_x2 + x1

            self._init_feedforward1_seed(z1)
            g_z1 = self.feed_forward_first(z1, cleaning=True)

            z2 = x2 + g_z1

            self._init_src_attention_seed(z2, context, mask_src)
            h_z2, coverage_src = self.src_attention(z2, context, mask_src,
                                                    incremental=incremental,
                                                    incremental_cache=incremental_cache)

            y1 = z1 + h_z2

            # prepare the state for the second function
            self._init_feedforward2_seed(y1)
            k_y1 = self.feed_forward_second(y1, cleaning=True)

            # if self.training and self.death_rate > 0:
            #     g_y1 = g_y1 / (1 - self.death_rate)

            y2 = z2 + k_y1

        """return Y1 and Y2"""
        return y1, y2, coverage_src

    def backward_pass(self, y1, y2, dy1, dy2, pos, context,
                      mask_tgt, mask_src,
                      incremental=False, incremental_cache=None, reuse_source=False):
        """
        :param pos:
        :param y1
        :param y2
        :param dy1: dL/dX2
        :param dy2: dL/dY2
        :param context:
        :param mask_tgt:
        :param mask_src:
        :param incremental:
        :param incremental_cache:
        :param reuse_source:
        :return:
        """

        # if not self.forward_coin:  # this layer was skipped, just return
        #     return y1, y2, dy1, dy2, None

        # first block: recompute the ffn transition function
        with torch.enable_grad():
            y1.requires_grad = True

            with torch.random.fork_rng(devices=self.ffn2_gpu_devices, enabled=True):
                torch.set_rng_state(self.ffn2_cpu_state)
                set_device_states(self.ffn2_gpu_devices, self.ffn2_gpu_states)

                k_y1 = self.feed_forward_second(y1)

            k_y1.backward(dy2)

        with torch.no_grad():
            z2 = y2 - k_y1
            del k_y1, y2

            # Dz1 = DY1 + Y1.grad
            dz1 = dy1 + y1.grad
            del dy1
            y1.grad = None

        # second block
        with torch.enable_grad():
            z2.requires_grad = True
            context.requires_grad = True

            with torch.random.fork_rng(devices=self.src_attn_gpu_devices, enabled=True):
                torch.set_rng_state(self.src_attn_cpu_state)
                set_device_states(self.src_attn_gpu_devices, self.src_attn_gpu_states)

                # if not self.ignore_source:
                h_z2, _ = self.src_attention(z2, context, mask_src,
                                             incremental=incremental,
                                             incremental_cache=incremental_cache)

            # torch.autograd.backward(h_z2, dz1)
            h_z2.backward(dz1)

        with torch.no_grad():
            z1 = y1 - h_z2
            del y1, h_z2

            dz2 = dy2 + z2.grad
            z2.grad = None
            del dy2

            grad_context = context.grad
            del context.grad

        # third block
        with torch.enable_grad():
            z1.requires_grad = True

            with torch.random.fork_rng(devices=self.ffn1_gpu_devices, enabled=True):
                torch.set_rng_state(self.ffn1_cpu_state)
                set_device_states(self.ffn1_gpu_devices, self.ffn1_gpu_states)

                g_z1 = self.feed_forward_first(z1)

            # torch.autograd.backward(g_z1, dz2)
            g_z1.backward(dz2)
        #
        with torch.no_grad():
            x2 = z2 - g_z1
            del z2, g_z1

            dx1 = dz1 + z1.grad

            z1.grad = None
            del dz1

        # fourth block
        with torch.enable_grad():
            x2.requires_grad = True

            with torch.random.fork_rng(devices=self.attn_gpu_devices, enabled=True):
                torch.set_rng_state(self.attn_cpu_state)
                set_device_states(self.attn_gpu_devices, self.attn_gpu_states)

                f_x2, _, = self.self_attention(x2, pos,
                                               key_padding_mask=None, attn_mask=mask_tgt,
                                               incremental=incremental,
                                               incremental_cache=incremental_cache)

            f_x2.backward(dx1)

        with torch.no_grad():
            x1 = z1 - f_x2
            del z1, f_x2

            dx2 = dz2 + x2.grad
            x2.grad = None

            del dz2

        return x1, x2, dx1, dx2, grad_context
