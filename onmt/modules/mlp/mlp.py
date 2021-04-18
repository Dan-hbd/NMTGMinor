from copy import copy
import math
import torch
from torch import nn

try:
    import apex.amp as amp
    from apex.amp import half_function
except (ModuleNotFoundError, ImportError) as e:
    amp = None
    from ..optimized.compat import half_function

try:
    from torch.cuda.amp import custom_fwd, custom_bwd
except (ModuleNotFoundError, ImportError) as e:
    from .compat import custom_fwd, custom_bwd

import fused_mlp


class MlpFunction(torch.autograd.Function):
    @staticmethod
    @custom_fwd(cast_inputs=torch.float16)
    def forward(ctx, bias, dropout_prob, activation, *args):
        output = fused_mlp.forward(bias, dropout_prob, activation, args)
        ctx.save_for_backward(*args)
        ctx.outputs = output
        ctx.bias = bias
        ctx.dropout_prob = dropout_prob
        ctx.activation = activation
        return output[0]

    @staticmethod
    @custom_bwd
    def backward(ctx, grad_o):
        grads = fused_mlp.backward(ctx.bias, ctx.dropout_prob, ctx.activation, grad_o, ctx.outputs, ctx.saved_tensors)
        del ctx.outputs
        return (None, None, None, *grads)


mlp_function = half_function(MlpFunction.apply)
# @half_function
# def mlp_function(bias, dropout_prob, activation, *args):
#     return MlpFunction.apply(bias, dropout_prob, activation, args)


class MLP(torch.nn.Module):
    """Launch MLP in C++

    Args:
        mlp_sizes (list of int): MLP sizes. Example: [1024,1024,1024] will create 2 MLP layers with shape 1024x1024
        bias (bool): Default True:
        relu (bool): Default True
    """

    def __init__(self, mlp_sizes, bias=True, activation='relu', dropout_prob=0.0):
        super(MLP, self).__init__()
        self.num_layers = len(mlp_sizes) - 1
        self.mlp_sizes = copy(mlp_sizes)
        self.bias = 1 if bias else 0
        self.dropout_prob = dropout_prob

        if activation is 'none':
            self.activation = 0
        elif activation is 'relu':
            self.activation = 1
        elif activation is 'sigmoid':
            self.activation = 2
        else:
            raise TypeError("activation must be relu or none.")

        self.weights = []
        self.biases = []
        for i in range(self.num_layers):
            w = torch.nn.Parameter(torch.empty(mlp_sizes[i + 1], mlp_sizes[i]))
            self.weights.append(w)
            name = 'weight_{}'.format(i)
            setattr(self, name, w)
            if self.bias:
                b = torch.nn.Parameter(torch.empty(mlp_sizes[i + 1]))
                self.biases.append(b)
                name = 'bias_{}'.format(i)
                setattr(self, name, b)

        self.reset_parameters()

    def reset_parameters(self):
        for weight in self.weights:
            dimsum = weight.size(0) + weight.size(1)
            std = math.sqrt(2. / float(dimsum))
            nn.init.normal_(weight, 0., std)
        if self.bias:
            for bias in self.biases:
                std = math.sqrt(1. / float(bias.size(0)))
                nn.init.normal_(bias, 0., std)

    def forward(self, input):
        return mlp_function(self.bias, self.dropout_prob, self.activation, input, *self.weights, *self.biases)

    def extra_repr(self):
        # TODO add dropout probability
        s = F"MLP sizes: {self.mlp_sizes}, Bias={self.bias}, activation={self.activation}"
        return s
