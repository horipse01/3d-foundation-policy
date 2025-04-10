"""
Contains torch Modules that correspond to basic network building blocks, like 
MLP, RNN, and CNN backbones.
"""

import math
import abc
import numpy as np
import textwrap
from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F
from torchvision import models as vision_models
from torchvision import transforms

import robomimic.utils.tensor_utils as TensorUtils

from robomimic.models.set_transformer.modules import ISAB, PMA, SAB
from robomimic.models.perceiverio import PoolingSetAttention
import timm
import os

CONV_ACTIVATIONS = {
    "relu": nn.ReLU,
    "None": None,
    None: None,
}


def rnn_args_from_config(rnn_config):
    """
    Takes a Config object corresponding to RNN settings
    (for example `config.algo.rnn` in BCConfig) and extracts
    rnn kwargs for instantiating rnn networks.
    """
    return dict(
        rnn_hidden_dim=rnn_config.hidden_dim,
        rnn_num_layers=rnn_config.num_layers,
        rnn_type=rnn_config.rnn_type,
        rnn_kwargs=dict(rnn_config.kwargs),
    )


def transformer_args_from_config(transformer_config):
    """
    Takes a Config object corresponding to Transformer settings
    (for example `config.algo.transformer` in BCConfig) and extracts
    transformer kwargs for instantiating transformer networks.
    """
    transformer_args = dict(
        transformer_context_length=transformer_config.context_length,
        transformer_embed_dim=transformer_config.embed_dim,
        transformer_num_heads=transformer_config.num_heads,
        transformer_emb_dropout=transformer_config.emb_dropout,
        transformer_attn_dropout=transformer_config.attn_dropout,
        transformer_block_output_dropout=transformer_config.block_output_dropout,
        transformer_sinusoidal_embedding=transformer_config.sinusoidal_embedding,
        transformer_activation=transformer_config.activation,
        transformer_nn_parameter_for_timesteps=transformer_config.nn_parameter_for_timesteps,
    )
    
    if "num_layers" in transformer_config:
        transformer_args["transformer_num_layers"] = transformer_config.num_layers

    return transformer_args


class Module(torch.nn.Module):
    """
    Base class for networks. The only difference from torch.nn.Module is that it
    requires implementing @output_shape.
    """
    @abc.abstractmethod
    def output_shape(self, input_shape=None):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        raise NotImplementedError


class Sequential(torch.nn.Sequential, Module):
    """
    Compose multiple Modules together (defined above).
    """
    def __init__(self, *args):
        for arg in args:
            assert isinstance(arg, Module)
        torch.nn.Sequential.__init__(self, *args)
        self.fixed = False

    def output_shape(self, input_shape=None):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        out_shape = input_shape
        for module in self:
            out_shape = module.output_shape(out_shape)
        return out_shape

    def freeze(self):
        self.fixed = True

    def train(self, mode):
        if self.fixed:
            super().train(False)
        else:
            super().train(mode)


class Parameter(Module):
    """
    A class that is a thin wrapper around a torch.nn.Parameter to make for easy saving
    and optimization.
    """
    def __init__(self, init_tensor):
        """
        Args:
            init_tensor (torch.Tensor): initial tensor
        """
        super(Parameter, self).__init__()
        self.param = torch.nn.Parameter(init_tensor)

    def output_shape(self, input_shape=None):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        return list(self.param.shape)

    def forward(self, inputs=None):
        """
        Forward call just returns the parameter tensor.
        """
        return self.param


class Unsqueeze(Module):
    """
    Trivial class that unsqueezes the input. Useful for including in a nn.Sequential network
    """
    def __init__(self, dim):
        super(Unsqueeze, self).__init__()
        self.dim = dim

    def output_shape(self, input_shape=None):
        assert input_shape is not None
        return input_shape + [1] if self.dim == -1 else input_shape[:self.dim + 1] + [1] + input_shape[self.dim + 1:]

    def forward(self, x):
        return x.unsqueeze(dim=self.dim)


class Squeeze(Module):
    """
    Trivial class that squeezes the input. Useful for including in a nn.Sequential network
    """

    def __init__(self, dim):
        super(Squeeze, self).__init__()
        self.dim = dim

    def output_shape(self, input_shape=None):
        assert input_shape is not None
        return input_shape[:self.dim] + input_shape[self.dim+1:] if input_shape[self.dim] == 1 else input_shape

    def forward(self, x):
        return x.squeeze(dim=self.dim)


class MLP(Module):
    """
    Base class for simple Multi-Layer Perceptrons.
    """
    def __init__(
        self,
        input_dim,
        output_dim,
        layer_dims=(),
        layer_func=nn.Linear,
        layer_func_kwargs=None,
        activation=nn.ReLU,
        dropouts=None,
        normalization=False,
        output_activation=None,
    ):
        """
        Args:
            input_dim (int): dimension of inputs

            output_dim (int): dimension of outputs

            layer_dims ([int]): sequence of integers for the hidden layers sizes

            layer_func: mapping per layer - defaults to Linear

            layer_func_kwargs (dict): kwargs for @layer_func

            activation: non-linearity per layer - defaults to ReLU

            dropouts ([float]): if not None, adds dropout layers with the corresponding probabilities
                after every layer. Must be same size as @layer_dims.

            normalization (bool): if True, apply layer normalization after each layer

            output_activation: if provided, applies the provided non-linearity to the output layer
        """
        super(MLP, self).__init__()
        layers = []
        dim = input_dim
        if layer_func_kwargs is None:
            layer_func_kwargs = dict()
        if dropouts is not None:
            assert(len(dropouts) == len(layer_dims))
        for i, l in enumerate(layer_dims):
            layers.append(layer_func(dim, l, **layer_func_kwargs))
            if normalization:
                layers.append(nn.LayerNorm(l))
            layers.append(activation())
            if dropouts is not None and dropouts[i] > 0.:
                layers.append(nn.Dropout(dropouts[i]))
            dim = l
        layers.append(layer_func(dim, output_dim))
        if output_activation is not None:
            layers.append(output_activation())
        self._layer_func = layer_func
        self.nets = layers
        self._model = nn.Sequential(*layers)

        self._layer_dims = layer_dims
        self._input_dim = input_dim
        self._output_dim = output_dim
        self._dropouts = dropouts
        self._act = activation
        self._output_act = output_activation

    def output_shape(self, input_shape=None):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        return [self._output_dim]

    def forward(self, inputs):
        """
        Forward pass.
        """
        return self._model(inputs)

    def __repr__(self):
        """Pretty print network."""
        header = str(self.__class__.__name__)
        act = None if self._act is None else self._act.__name__
        output_act = None if self._output_act is None else self._output_act.__name__

        indent = ' ' * 4
        msg = "input_dim={}\noutput_dim={}\nlayer_dims={}\nlayer_func={}\ndropout={}\nact={}\noutput_act={}".format(
            self._input_dim, self._output_dim, self._layer_dims,
            self._layer_func.__name__, self._dropouts, act, output_act
        )
        msg = textwrap.indent(msg, indent)
        msg = header + '(\n' + msg + '\n)'
        return msg


class PointNet(Module):
    """
    PointNet class for processing point clouds.
    """
    def __init__(
        self,
        input_dim,
        output_dim,
        layer_dims=(64, 128, 1024),
        activation=nn.ReLU,
        normalization=False,
        global_feature=True,
        output_activation=None
    ):
        """
        Args:
            input_dim (int): Dimension of inputs (number of channels per point).

            output_dim (int): Dimension of outputs.

            layer_dims ([int]): Sequence of integers for the hidden layers sizes.

            activation: Non-linearity per layer - defaults to ReLU.

            normalization (bool): If True, apply layer normalization after each layer.

            global_feature (bool): If True, use max pooling to get global feature.

            output_activation: If provided, applies the provided non-linearity to the output layer.
        """
        super(PointNet, self).__init__()

        self.layers = nn.ModuleList()
        prev_dim = input_dim

        for dim in layer_dims:
            self.layers.append(nn.Conv1d(prev_dim, dim, 1))
            if normalization:
                self.layers.append(nn.BatchNorm1d(dim))
            self.layers.append(activation())
            prev_dim = dim

        if global_feature:
            # self.layers.append(nn.AdaptiveAvgPool1d(1))
            self.layers.append(nn.AdaptiveMaxPool1d(1))

        self.fc_layers = nn.Sequential(
            nn.Linear(layer_dims[-1], output_dim),
            output_activation() if output_activation is not None else nn.Identity()
        )

        self._global_feature = global_feature
        self._output_dim = output_dim

    def forward(self, x):
        x = torch.permute(x, (0, 2, 1)).contiguous()

        #num_points = x.shape[2]  # Assuming input shape is [batch, channels, num_points]
        #if num_points > 3000:
        #    indices = torch.randperm(num_points)[:1000].to(x.device)
        #    x = x[:, :, indices]

        for layer in self.layers:
            x = layer(x)
        if self._global_feature:
            x = x.view(x.size(0), -1)
        x = self.fc_layers(x)

        return x

    def output_shape(self, input_shape=None):
        """
        Function to compute output shape from inputs to this module.

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        return [self._output_dim]

    def __repr__(self):
        """Pretty print network."""
        header = str(self.__class__.__name__)
        layers_info = [f"{type(layer).__name__}({layer.in_channels}, {layer.out_channels}, 1)" if isinstance(layer, nn.Conv1d) else layer.__class__.__name__ for layer in self.layers]
        fc_info = [f"{type(layer).__name__}({layer.in_features}, {layer.out_features})" if isinstance(layer, nn.Linear) else layer.__class__.__name__ for layer in self.fc_layers]
        msg = f"input_dim={self.layers[0].in_channels}\noutput_dim={self._output_dim}\nlayers={layers_info}\nfc_layers={fc_info}\nglobal_feature={self._global_feature}"
        msg = textwrap.indent(msg, ' ' * 4)
        return header + '(\n' + msg + '\n)'



class SetTransformer(Module):
    """
    PointNet class for processing point clouds.
    """
    def __init__(
        self,
        dim_input=6,
        num_outputs=1,
        dim_output=64,
        num_inds=32,
        dim_hidden=128,
        num_heads=4,
        ln=False,
        dropout_rate=0.1
    ):
        """
        Args:
            dim_input (int): Dimension of input features.

            num_outputs (int): Number of output sets.

            dim_output (int): Dimension of each output set.

            num_inds (int): Number of inducing points for ISAB blocks.

            dim_hidden (int): Hidden dimension size for self-attention layers.

            num_heads (int): Number of heads in multihead self-attention.

            ln (bool): If True, applies layer normalization.

            dropout_rate (float): Dropout rate for dropout layers.
        """
        super(SetTransformer, self).__init__()

        self.dim_input = dim_input
        self.dim_output = dim_output
        self.num_heads = num_heads
        self.ln = ln
        self.num_outputs = num_outputs

        # Encoder part: Two ISAB (Induced Set Attention Block) layers
        self.enc = nn.Sequential(
            ISAB(dim_input, dim_hidden, num_heads, num_inds, ln=ln),
            ISAB(dim_hidden, dim_hidden, num_heads, num_inds, ln=ln),
        )

        # Decoder part: PMA (Pooling by Multihead Attention) followed by linear layers
        self.dec = nn.Sequential(
            nn.Dropout(dropout_rate),
            PMA(dim_hidden, num_heads, num_outputs, ln=ln),
            nn.Dropout(dropout_rate),
            nn.Linear(dim_hidden, dim_output),
        )

    def forward(self, X):
        """
        Forward pass for SetTransformer.

        Args:
            X (tensor): Input tensor of shape [batch_size, num_elements, dim_input]

        Returns:
            tensor: Transformed set of shape [batch_size, num_outputs, dim_output]
        """

        #num_points = X.size()[1]  # Assuming input shape is [batch, channels, num_points]
        #if num_points > 3000:
        #    indices = torch.randperm(num_points)[:1000].to(X.device)
        #    X = X[:, indices, :]

        return_feat = self.dec(self.enc(X)).squeeze(1)
        return return_feat

    def output_shape(self, input_shape=None):
        """
        Function to compute output shape from inputs to this module.

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        return [self.dim_output]

    def __repr__(self):
        """
        Pretty print the SetTransformer network.
        """
        header = str(self.__class__.__name__)
        enc_info = [f"{type(layer).__name__}(...)" for layer in self.enc]
        dec_info = [f"{type(layer).__name__}(...)" if isinstance(layer, nn.Linear) else layer.__class__.__name__ for layer in self.dec]
        msg = f"dim_input={self.dim_input}\ndim_output={self.dim_output}\nencoder_layers={enc_info}\ndecoder_layers={dec_info}\nnum_heads={self.num_heads}\nlayer_norm={self.ln}"
        msg = textwrap.indent(msg, ' ' * 4)
        return header + '(\n' + msg + '\n)'


class SetXFPCDEncoder(nn.Module):
    def __init__(
        self,
        n_coordinates: int = 6,
        add_ee_embd: bool = False,
        ee_embd_dim: int = 64,
        hidden_dim: int = 128,
        output_dim: int = 64,
        set_xf_num_heads: int = 8,
        set_xf_num_queries: int = 8,
        set_xf_layer_norm: bool = False,
    ):
        super().__init__()

        # Store the parameters as instance attributes
        self.n_coordinates = n_coordinates
        self.add_ee_embd = add_ee_embd
        self.ee_embd_dim = ee_embd_dim
        self.hidden_dim = hidden_dim
        self.set_xf_num_heads = set_xf_num_heads
        self.set_xf_num_queries = set_xf_num_queries
        self.set_xf_layer_norm = set_xf_layer_norm

        self.linear = nn.Linear(n_coordinates, hidden_dim)
        self.num_queries = set_xf_num_queries
        self.set_xf = PoolingSetAttention(
            dim=hidden_dim,
            num_heads=set_xf_num_heads,
            num_queries=set_xf_num_queries,
            pool_type='max',
            layer_norm=set_xf_layer_norm,
        )
        self.output_linear = nn.Linear(hidden_dim, output_dim)
        self.output_dim = output_dim

    def forward(self, x):
        #num_points = x.size()[1]  # Assuming input shape is [batch, channels, num_points]
        #if num_points > 3000:
        #    indices = torch.randperm(num_points)[:1000].to(x.device)
        #    x = x[:, indices, :]

        x = self.linear(x)
        x = self.set_xf(x)
        output = self.output_linear(x)
        return output

    def output_shape(self, input_shape=None):

        return [self.output_dim]

    def __repr__(self):
        """
        Pretty print the SetXFPCDEncoder network.
        """
        header = str(self.__class__.__name__)
        linear_info = f"{type(self.linear).__name__}(...)"
        set_xf_info = f"{type(self.set_xf).__name__}(...)"
        msg = f"n_coordinates={self.n_coordinates}\nadd_ee_embd={self.add_ee_embd}\nee_embd_dim={self.ee_embd_dim}\nhidden_dim={self.hidden_dim}\nset_xf_num_heads={self.set_xf_num_heads}\nset_xf_num_queries={self.num_queries}\nset_xf_layer_norm={self.set_xf_layer_norm}\nlinear_layer={linear_info}\nset_xf_layer={set_xf_info}\noutput_dim={self.output_dim}"
        msg = textwrap.indent(msg, ' ' * 4)
        return header + '(\n' + msg + '\n)'


class RNN_Base(Module):
    """
    A wrapper class for a multi-step RNN and a per-step network.
    """
    def __init__(
        self,
        input_dim,
        rnn_hidden_dim,
        rnn_num_layers,
        rnn_type="LSTM",  # [LSTM, GRU]
        rnn_kwargs=None,
        per_step_net=None,
    ):
        """
        Args:
            input_dim (int): dimension of inputs

            rnn_hidden_dim (int): RNN hidden dimension

            rnn_num_layers (int): number of RNN layers

            rnn_type (str): [LSTM, GRU]

            rnn_kwargs (dict): kwargs for the torch.nn.LSTM / GRU

            per_step_net: a network that runs per time step on top of the RNN output
        """
        super(RNN_Base, self).__init__()
        self.per_step_net = per_step_net
        if per_step_net is not None:
            assert isinstance(per_step_net, Module), "RNN_Base: per_step_net is not instance of Module"

        assert rnn_type in ["LSTM", "GRU"]
        rnn_cls = nn.LSTM if rnn_type == "LSTM" else nn.GRU
        rnn_kwargs = rnn_kwargs if rnn_kwargs is not None else {}
        rnn_is_bidirectional = rnn_kwargs.get("bidirectional", False)

        self.nets = rnn_cls(
            input_size=input_dim,
            hidden_size=rnn_hidden_dim,
            num_layers=rnn_num_layers,
            batch_first=True,
            **rnn_kwargs,
        )

        self._hidden_dim = rnn_hidden_dim
        self._num_layers = rnn_num_layers
        self._rnn_type = rnn_type
        self._num_directions = int(rnn_is_bidirectional) + 1 # 2 if bidirectional, 1 otherwise

    @property
    def rnn_type(self):
        return self._rnn_type

    def get_rnn_init_state(self, batch_size, device):
        """
        Get a default RNN state (zeros)
        Args:
            batch_size (int): batch size dimension

            device: device the hidden state should be sent to.

        Returns:
            hidden_state (torch.Tensor or tuple): returns hidden state tensor or tuple of hidden state tensors
                depending on the RNN type
        """
        h_0 = torch.zeros(self._num_layers * self._num_directions, batch_size, self._hidden_dim).to(device)
        if self._rnn_type == "LSTM":
            c_0 = torch.zeros(self._num_layers * self._num_directions, batch_size, self._hidden_dim).to(device)
            return h_0, c_0
        else:
            return h_0

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """

        # infer time dimension from input shape and add to per_step_net output shape
        if self.per_step_net is not None:
            out = self.per_step_net.output_shape(input_shape[1:])
            if isinstance(out, dict):
                out = {k: [input_shape[0]] + out[k] for k in out}
            else:
                out = [input_shape[0]] + out
        else:
            out = [input_shape[0], self._num_layers * self._hidden_dim]
        return out

    def forward(self, inputs, rnn_init_state=None, return_state=False):
        """
        Forward a sequence of inputs through the RNN and the per-step network.

        Args:
            inputs (torch.Tensor): tensor input of shape [B, T, D], where D is the RNN input size

            rnn_init_state: rnn hidden state, initialize to zero state if set to None

            return_state (bool): whether to return hidden state

        Returns:
            outputs: outputs of the per_step_net

            rnn_state: return rnn state at the end if return_state is set to True
        """
        assert inputs.ndimension() == 3  # [B, T, D]
        batch_size, seq_length, inp_dim = inputs.shape
        if rnn_init_state is None:
            rnn_init_state = self.get_rnn_init_state(batch_size, device=inputs.device)

        outputs, rnn_state = self.nets(inputs, rnn_init_state)
        if self.per_step_net is not None:
            outputs = TensorUtils.time_distributed(outputs, self.per_step_net)

        if return_state:
            return outputs, rnn_state
        else:
            return outputs

    def forward_step(self, inputs, rnn_state):
        """
        Forward a single step input through the RNN and per-step network, and return the new hidden state.
        Args:
            inputs (torch.Tensor): tensor input of shape [B, D], where D is the RNN input size

            rnn_state: rnn hidden state, initialize to zero state if set to None

        Returns:
            outputs: outputs of the per_step_net

            rnn_state: return the new rnn state
        """
        assert inputs.ndimension() == 2
        inputs = TensorUtils.to_sequence(inputs)
        outputs, rnn_state = self.forward(
            inputs,
            rnn_init_state=rnn_state,
            return_state=True,
        )
        return outputs[:, 0], rnn_state


"""
================================================
Visual Backbone Networks
================================================
"""
class ConvBase(Module):
    """
    Base class for ConvNets.
    """
    def __init__(self):
        super(ConvBase, self).__init__()

    # dirty hack - re-implement to pass the buck onto subclasses from ABC parent
    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        raise NotImplementedError

    def forward(self, inputs):
        x = self.nets(inputs)
        if list(self.output_shape(list(inputs.shape)[1:])) != list(x.shape)[1:]:
            raise ValueError('Size mismatch: expect size %s, but got size %s' % (
                str(self.output_shape(list(inputs.shape)[1:])), str(list(x.shape)[1:]))
            )
        return x


class ResNet18Conv(ConvBase):
    """
    A ResNet18 block that can be used to process input images.
    """
    def __init__(
        self,
        input_channel=3,
        pretrained=False,
        input_coord_conv=False,
    ):
        """
        Args:
            input_channel (int): number of input channels for input images to the network.
                If not equal to 3, modifies first conv layer in ResNet to handle the number
                of input channels.
            pretrained (bool): if True, load pretrained weights for all ResNet layers.
            input_coord_conv (bool): if True, use a coordinate convolution for the first layer
                (a convolution where input channels are modified to encode spatial pixel location)
        """
        super(ResNet18Conv, self).__init__()
        net = vision_models.resnet18(pretrained=pretrained)

        if input_coord_conv:
            net.conv1 = CoordConv2d(input_channel, 64, kernel_size=7, stride=2, padding=3, bias=False)
        elif input_channel != 3:
            net.conv1 = nn.Conv2d(input_channel, 64, kernel_size=7, stride=2, padding=3, bias=False)

        # cut the last fc layer
        self._input_coord_conv = input_coord_conv
        self._input_channel = input_channel
        self.nets = torch.nn.Sequential(*(list(net.children())[:-2]))

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        assert(len(input_shape) == 3)
        out_h = int(math.ceil(input_shape[1] / 32.))
        out_w = int(math.ceil(input_shape[2] / 32.))
        return [512, out_h, out_w]

    def __repr__(self):
        """Pretty print network."""
        header = '{}'.format(str(self.__class__.__name__))
        return header + '(input_channel={}, input_coord_conv={})'.format(self._input_channel, self._input_coord_conv)


class ResNet50Conv(ConvBase):
    """
    A ResNet50 block that can be used to process input images.
    """
    def __init__(
        self,
        input_channel=3,
        pretrained=False,
        input_coord_conv=False,
    ):
        """
        Args:
            input_channel (int): number of input channels for input images to the network.
                If not equal to 3, modifies first conv layer in ResNet to handle the number
                of input channels.
            pretrained (bool): if True, load pretrained weights for all ResNet layers.
            input_coord_conv (bool): if True, use a coordinate convolution for the first layer
                (a convolution where input channels are modified to encode spatial pixel location)
        """
        super(ResNet50Conv, self).__init__()
        net = vision_models.resnet50(pretrained=pretrained)

        if input_coord_conv:
            # copied from ResNet18Conv. TODO: check if sizes need to be changed
            net.conv1 = CoordConv2d(input_channel, 64, kernel_size=7, stride=2, padding=3, bias=False)
        elif input_channel != 3:
            # copied from ResNet18Conv. TODO: check if sizes need to be changed
            net.conv1 = nn.Conv2d(input_channel, 64, kernel_size=7, stride=2, padding=3, bias=False)

        # cut the last fc layer
        self._input_coord_conv = input_coord_conv
        self._input_channel = input_channel
        self.nets = torch.nn.Sequential(*(list(net.children())[:-2]))

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        assert(len(input_shape) == 3)
        out_h = int(math.ceil(input_shape[1] / 32.))
        out_w = int(math.ceil(input_shape[2] / 32.))
        return [2048, out_h, out_w]

    def __repr__(self):
        """Pretty print network."""
        header = '{}'.format(str(self.__class__.__name__))
        return header + '(input_channel={}, input_coord_conv={})'.format(self._input_channel, self._input_coord_conv)


class R3MConv(ConvBase):
    """
    Base class for ConvNets pretrained with R3M (https://arxiv.org/abs/2203.12601)
    """
    def __init__(
        self,
        input_channel=3,
        r3m_model_class='resnet18',
        freeze=True,
    ):
        """
        Using R3M pretrained observation encoder network proposed by https://arxiv.org/abs/2203.12601
        Args:
            input_channel (int): number of input channels for input images to the network.
                If not equal to 3, modifies first conv layer in ResNet to handle the number
                of input channels.
            r3m_model_class (str): select one of the r3m pretrained model "resnet18", "resnet34" or "resnet50"
            freeze (bool): if True, use a frozen R3M pretrained model.
        """
        super(R3MConv, self).__init__()

        try:
            from r3m import load_r3m
        except ImportError:
            print("WARNING: could not load r3m library! Please follow https://github.com/facebookresearch/r3m to install R3M")

        net = load_r3m(r3m_model_class)

        assert input_channel == 3 # R3M only support input image with channel size 3
        assert r3m_model_class in ["resnet18", "resnet34", "resnet50"] # make sure the selected r3m model do exist

        # cut the last fc layer
        self._input_channel = input_channel
        self._r3m_model_class = r3m_model_class
        self._freeze = freeze
        self._input_coord_conv = False
        self._pretrained = True

        preprocess = nn.Sequential(
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
        )
        self.nets = Sequential(*([preprocess] + list(net.module.convnet.children())))
        if freeze:
            self.nets.freeze()

        self.weight_sum = np.sum([param.cpu().data.numpy().sum() for param in self.nets.parameters()])
        if freeze:
            for param in self.nets.parameters():
                param.requires_grad = False

        self.nets.eval()

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module.
        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend
                on the size of the input, or if they assume fixed size input.
        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        assert(len(input_shape) == 3)

        if self._r3m_model_class == 'resnet50':
            out_dim = 2048
        else:
            out_dim = 512

        return [out_dim, 1, 1]

    def __repr__(self):
        """Pretty print network."""
        header = '{}'.format(str(self.__class__.__name__))
        return header + '(input_channel={}, input_coord_conv={}, pretrained={}, freeze={})'.format(self._input_channel, self._input_coord_conv, self._pretrained, self._freeze)


class MVPConv(ConvBase):
    """
    Base class for ConvNets pretrained with MVP (https://arxiv.org/abs/2203.06173)
    """
    def __init__(
        self,
        input_channel=3,
        mvp_model_class='vitb-mae-egosoup',
        freeze=True,
    ):
        """
        Using MVP pretrained observation encoder network proposed by https://arxiv.org/abs/2203.06173
        Args:
            input_channel (int): number of input channels for input images to the network.
                If not equal to 3, modifies first conv layer in ResNet to handle the number
                of input channels.
            mvp_model_class (str): select one of the mvp pretrained model "vits-mae-hoi", "vits-mae-in", "vits-sup-in", "vitb-mae-egosoup" or "vitl-256-mae-egosoup"
            freeze (bool): if True, use a frozen MVP pretrained model.
        """
        super(MVPConv, self).__init__()

        try:
            import mvp
        except ImportError:
            print("WARNING: could not load mvp library! Please follow https://github.com/ir413/mvp to install MVP.")

        self.nets = mvp.load(mvp_model_class)
        if freeze:
            self.nets.freeze()

        assert input_channel == 3 # MVP only support input image with channel size 3
        assert mvp_model_class in ["vits-mae-hoi", "vits-mae-in", "vits-sup-in", "vitb-mae-egosoup", "vitl-256-mae-egosoup"] # make sure the selected r3m model do exist

        self._input_channel = input_channel
        self._freeze = freeze
        self._mvp_model_class = mvp_model_class
        self._input_coord_conv = False
        self._pretrained = True

        if '256' in mvp_model_class:
            input_img_size = 256
        else:
            input_img_size = 224
        self.preprocess = nn.Sequential(
            transforms.Resize(input_img_size)
        )

    def forward(self, inputs):
        x = self.preprocess(inputs)
        x = self.nets(x)
        if list(self.output_shape(list(inputs.shape)[1:])) != list(x.shape)[1:]:
            raise ValueError('Size mismatch: expect size %s, but got size %s' % (
                str(self.output_shape(list(inputs.shape)[1:])), str(list(x.shape)[1:]))
            )
        return x

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module.
        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend
                on the size of the input, or if they assume fixed size input.
        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        assert(len(input_shape) == 3)
        if 'vitb' in self._mvp_model_class:
            output_shape = [768]
        elif 'vitl' in self._mvp_model_class:
            output_shape = [1024]
        else:
            output_shape = [384]
        return output_shape

    def __repr__(self):
        """Pretty print network."""
        header = '{}'.format(str(self.__class__.__name__))
        return header + '(input_channel={}, input_coord_conv={}, pretrained={}, freeze={})'.format(self._input_channel, self._input_coord_conv, self._pretrained, self._freeze)


class CoordConv2d(nn.Conv2d, Module):
    """
    2D Coordinate Convolution

    Source: An Intriguing Failing of Convolutional Neural Networks and the CoordConv Solution
    https://arxiv.org/abs/1807.03247
    (e.g. adds 2 channels per input feature map corresponding to (x, y) location on map)
    """
    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size,
        stride=1,
        padding=0,
        dilation=1,
        groups=1,
        bias=True,
        padding_mode='zeros',
        coord_encoding='position',
    ):
        """
        Args:
            in_channels: number of channels of the input tensor [C, H, W]
            out_channels: number of output channels of the layer
            kernel_size: convolution kernel size
            stride: conv stride
            padding: conv padding
            dilation: conv dilation
            groups: conv groups
            bias: conv bias
            padding_mode: conv padding mode
            coord_encoding: type of coordinate encoding. currently only 'position' is implemented
        """

        assert(coord_encoding in ['position'])
        self.coord_encoding = coord_encoding
        if coord_encoding == 'position':
            in_channels += 2  # two extra channel for positional encoding
            self._position_enc = None  # position encoding
        else:
            raise Exception("CoordConv2d: coord encoding {} not implemented".format(self.coord_encoding))
        nn.Conv2d.__init__(
            self,
            in_channels=in_channels,
            out_channels=out_channels,
            kernel_size=kernel_size,
            stride=stride,
            padding=padding,
            dilation=dilation,
            groups=groups,
            bias=bias,
            padding_mode=padding_mode
        )

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """

        # adds 2 to channel dimension
        return [input_shape[0] + 2] + input_shape[1:]

    def forward(self, input):
        b, c, h, w = input.shape
        if self.coord_encoding == 'position':
            if self._position_enc is None:
                pos_y, pos_x = torch.meshgrid(torch.arange(h), torch.arange(w))
                pos_y = pos_y.float().to(input.device) / float(h)
                pos_x = pos_x.float().to(input.device) / float(w)
                self._position_enc = torch.stack((pos_y, pos_x)).unsqueeze(0)
            pos_enc = self._position_enc.expand(b, -1, -1, -1)
            input = torch.cat((input, pos_enc), dim=1)
        return super(CoordConv2d, self).forward(input)


class ShallowConv(ConvBase):
    """
    A shallow convolutional encoder from https://rll.berkeley.edu/dsae/dsae.pdf
    """
    def __init__(self, input_channel=3, output_channel=32):
        super(ShallowConv, self).__init__()
        self._input_channel = input_channel
        self._output_channel = output_channel
        self.nets = nn.Sequential(
            torch.nn.Conv2d(input_channel, 64, kernel_size=7, stride=2, padding=3),
            torch.nn.ReLU(),
            torch.nn.Conv2d(64, 32, kernel_size=1, stride=1, padding=0),
            torch.nn.ReLU(),
            torch.nn.Conv2d(32, 32, kernel_size=3, stride=1, padding=1),
            torch.nn.ReLU(),
            torch.nn.Conv2d(32, 32, kernel_size=3, stride=1, padding=1),
        )

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        assert(len(input_shape) == 3)
        assert(input_shape[0] == self._input_channel)
        out_h = int(math.floor(input_shape[1] / 2.))
        out_w = int(math.floor(input_shape[2] / 2.))
        return [self._output_channel, out_h, out_w]


class Conv1dBase(Module):
    """
    Base class for stacked Conv1d layers.

    Args:
        input_channel (int): Number of channels for inputs to this network
        activation (None or str): Per-layer activation to use. Defaults to "relu". Valid options are
            currently {relu, None} for no activation
        out_channels (list of int): Output channel size for each sequential Conv1d layer
        kernel_size (list of int): Kernel sizes for each sequential Conv1d layer
        stride (list of int): Stride sizes for each sequential Conv1d layer
        conv_kwargs (dict): additional nn.Conv1D args to use, in list form, where the ith element corresponds to the
            argument to be passed to the ith Conv1D layer.
            See https://pytorch.org/docs/stable/generated/torch.nn.Conv1d.html for specific possible arguments.
    """
    def __init__(
        self,
        input_channel=1,
        activation="relu",
        out_channels=(32, 64, 64),
        kernel_size=(8, 4, 2),
        stride=(4, 2, 1),
        **conv_kwargs,
    ):
        super(Conv1dBase, self).__init__()

        # Get activation requested
        activation = CONV_ACTIVATIONS[activation]

        # Generate network
        self.n_layers = len(out_channels)
        layers = OrderedDict()
        for i in range(self.n_layers):
            layer_kwargs = {k: v[i] for k, v in conv_kwargs.items()}
            layers[f'conv{i}'] = nn.Conv1d(
                in_channels=input_channel,
                **layer_kwargs,
            )
            if activation is not None:
                layers[f'act{i}'] = activation()
            input_channel = layer_kwargs["out_channels"]

        # Store network
        self.nets = nn.Sequential(layers)

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module.

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        channels, length = input_shape
        for i in range(self.n_layers):
            net = getattr(self.nets, f"conv{i}")
            channels = net.out_channels
            length = int((length + 2 * net.padding[0] - net.dilation[0] * (net.kernel_size[0] - 1) - 1) / net.stride[0]) + 1
        return [channels, length]

    def forward(self, inputs):
        x = self.nets(inputs)
        if list(self.output_shape(list(inputs.shape)[1:])) != list(x.shape)[1:]:
            raise ValueError('Size mismatch: expect size %s, but got size %s' % (
                str(self.output_shape(list(inputs.shape)[1:])), str(list(x.shape)[1:]))
            )
        return x


"""
================================================
Pooling Networks
================================================
"""
class SpatialSoftmax(ConvBase):
    """
    Spatial Softmax Layer.

    Based on Deep Spatial Autoencoders for Visuomotor Learning by Finn et al.
    https://rll.berkeley.edu/dsae/dsae.pdf
    """
    def __init__(
        self,
        input_shape,
        num_kp=32,
        temperature=1.,
        learnable_temperature=False,
        output_variance=False,
        noise_std=0.0,
    ):
        """
        Args:
            input_shape (list): shape of the input feature (C, H, W)
            num_kp (int): number of keypoints (None for not using spatialsoftmax)
            temperature (float): temperature term for the softmax.
            learnable_temperature (bool): whether to learn the temperature
            output_variance (bool): treat attention as a distribution, and compute second-order statistics to return
            noise_std (float): add random spatial noise to the predicted keypoints
        """
        super(SpatialSoftmax, self).__init__()
        assert len(input_shape) == 3
        self._in_c, self._in_h, self._in_w = input_shape # (C, H, W)

        if num_kp is not None:
            self.nets = torch.nn.Conv2d(self._in_c, num_kp, kernel_size=1)
            self._num_kp = num_kp
        else:
            self.nets = None
            self._num_kp = self._in_c
        self.learnable_temperature = learnable_temperature
        self.output_variance = output_variance
        self.noise_std = noise_std

        if self.learnable_temperature:
            # temperature will be learned
            temperature = torch.nn.Parameter(torch.ones(1) * temperature, requires_grad=True)
            self.register_parameter('temperature', temperature)
        else:
            # temperature held constant after initialization
            temperature = torch.nn.Parameter(torch.ones(1) * temperature, requires_grad=False)
            self.register_buffer('temperature', temperature)

        pos_x, pos_y = np.meshgrid(
                np.linspace(-1., 1., self._in_w),
                np.linspace(-1., 1., self._in_h)
                )
        pos_x = torch.from_numpy(pos_x.reshape(1, self._in_h * self._in_w)).float()
        pos_y = torch.from_numpy(pos_y.reshape(1, self._in_h * self._in_w)).float()
        self.register_buffer('pos_x', pos_x)
        self.register_buffer('pos_y', pos_y)

        self.kps = None

    def __repr__(self):
        """Pretty print network."""
        header = format(str(self.__class__.__name__))
        return header + '(num_kp={}, temperature={}, noise={})'.format(
            self._num_kp, self.temperature.item(), self.noise_std)

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        assert(len(input_shape) == 3)
        assert(input_shape[0] == self._in_c)
        return [self._num_kp, 2]

    def forward(self, feature):
        """
        Forward pass through spatial softmax layer. For each keypoint, a 2D spatial 
        probability distribution is created using a softmax, where the support is the 
        pixel locations. This distribution is used to compute the expected value of 
        the pixel location, which becomes a keypoint of dimension 2. K such keypoints
        are created.

        Returns:
            out (torch.Tensor or tuple): mean keypoints of shape [B, K, 2], and possibly
                keypoint variance of shape [B, K, 2, 2] corresponding to the covariance
                under the 2D spatial softmax distribution
        """
        assert(feature.shape[1] == self._in_c)
        assert(feature.shape[2] == self._in_h)
        assert(feature.shape[3] == self._in_w)
        if self.nets is not None:
            feature = self.nets(feature)

        # [B, K, H, W] -> [B * K, H * W] where K is number of keypoints
        feature = feature.reshape(-1, self._in_h * self._in_w)
        # 2d softmax normalization
        attention = F.softmax(feature / self.temperature, dim=-1)
        # [1, H * W] x [B * K, H * W] -> [B * K, 1] for spatial coordinate mean in x and y dimensions
        expected_x = torch.sum(self.pos_x * attention, dim=1, keepdim=True)
        expected_y = torch.sum(self.pos_y * attention, dim=1, keepdim=True)
        # stack to [B * K, 2]
        expected_xy = torch.cat([expected_x, expected_y], 1)
        # reshape to [B, K, 2]
        feature_keypoints = expected_xy.view(-1, self._num_kp, 2)

        if self.training:
            noise = torch.randn_like(feature_keypoints) * self.noise_std
            feature_keypoints += noise

        if self.output_variance:
            # treat attention as a distribution, and compute second-order statistics to return
            expected_xx = torch.sum(self.pos_x * self.pos_x * attention, dim=1, keepdim=True)
            expected_yy = torch.sum(self.pos_y * self.pos_y * attention, dim=1, keepdim=True)
            expected_xy = torch.sum(self.pos_x * self.pos_y * attention, dim=1, keepdim=True)
            var_x = expected_xx - expected_x * expected_x
            var_y = expected_yy - expected_y * expected_y
            var_xy = expected_xy - expected_x * expected_y
            # stack to [B * K, 4] and then reshape to [B, K, 2, 2] where last 2 dims are covariance matrix
            feature_covar = torch.cat([var_x, var_xy, var_xy, var_y], 1).reshape(-1, self._num_kp, 2, 2)
            feature_keypoints = (feature_keypoints, feature_covar)

        if isinstance(feature_keypoints, tuple):
            self.kps = (feature_keypoints[0].detach(), feature_keypoints[1].detach())
        else:
            self.kps = feature_keypoints.detach()
        return feature_keypoints


class SpatialMeanPool(Module):
    """
    Module that averages inputs across all spatial dimensions (dimension 2 and after),
    leaving only the batch and channel dimensions.
    """
    def __init__(self, input_shape):
        super(SpatialMeanPool, self).__init__()
        assert len(input_shape) == 3 # [C, H, W]
        self.in_shape = input_shape

    def output_shape(self, input_shape=None):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        return list(self.in_shape[:1]) # [C, H, W] -> [C]

    def forward(self, inputs):
        """Forward pass - average across all dimensions except batch and channel."""
        return TensorUtils.flatten(inputs, begin_axis=2).mean(dim=2)


class FeatureAggregator(Module):
    """
    Helpful class for aggregating features across a dimension. This is useful in 
    practice when training models that break an input image up into several patches
    since features can be extraced per-patch using the same encoder and then 
    aggregated using this module.
    """
    def __init__(self, dim=1, agg_type="avg"):
        super(FeatureAggregator, self).__init__()
        self.dim = dim
        self.agg_type = agg_type

    def set_weight(self, w):
        assert self.agg_type == "w_avg"
        self.agg_weight = w

    def clear_weight(self):
        assert self.agg_type == "w_avg"
        self.agg_weight = None

    def output_shape(self, input_shape):
        """
        Function to compute output shape from inputs to this module. 

        Args:
            input_shape (iterable of int): shape of input. Does not include batch dimension.
                Some modules may not need this argument, if their output does not depend 
                on the size of the input, or if they assume fixed size input.

        Returns:
            out_shape ([int]): list of integers corresponding to output shape
        """
        # aggregates on @self.dim, so it is removed from the output shape 
        return list(input_shape[:self.dim]) + list(input_shape[self.dim+1:])

    def forward(self, x):
        """Forward pooling pass."""
        if self.agg_type == "avg":
            # mean-pooling
            return torch.mean(x, dim=1)
        if self.agg_type == "w_avg":
            # weighted mean-pooling
            return torch.sum(x * self.agg_weight, dim=1)
        raise Exception("unexpected agg type: {}".forward(self.agg_type))

# Point cloud encoder from DP3: https://github.com/YanjieZe/3D-Diffusion-Policy

class PointNetEncoderXYZRGB(nn.Module):
    """Encoder for Pointcloud
    """

    def __init__(self,
                 in_channels: int=6,
                 out_channels: int=256,
                 use_layernorm: bool=True,
                 final_norm: str='layernorm',
                 use_projection: bool=True,
                 **kwargs
                 ):
        """_summary_

        Args:
            in_channels (int): feature size of input (3 or 6)
            input_transform (bool, optional): whether to use transformation for coordinates. Defaults to True.
            feature_transform (bool, optional): whether to use transformation for features. Defaults to True.
            is_seg (bool, optional): for segmentation or classification. Defaults to False.
        """
        super(PointNetEncoderXYZRGB,self).__init__()
        block_channel = [64, 128, 256, 512]
        
        self.mlp = nn.Sequential(
            nn.Linear(in_channels, block_channel[0]),
            nn.LayerNorm(block_channel[0]) if use_layernorm else nn.Identity(),
            nn.ReLU(),
            nn.Linear(block_channel[0], block_channel[1]),
            nn.LayerNorm(block_channel[1]) if use_layernorm else nn.Identity(),
            nn.ReLU(),
            nn.Linear(block_channel[1], block_channel[2]),
            nn.LayerNorm(block_channel[2]) if use_layernorm else nn.Identity(),
            nn.ReLU(),
            nn.Linear(block_channel[2], block_channel[3]),
        )
        
        if use_projection:
            if final_norm == 'layernorm':
                self.final_projection = nn.Sequential(
                    nn.Linear(block_channel[-1], out_channels),
                    nn.LayerNorm(out_channels)
                )
            elif final_norm == 'none':
                self.final_projection = nn.Linear(block_channel[-1], out_channels)
            else:
                raise NotImplementedError(f"final_norm: {final_norm}")
            
            self.output_dim = out_channels
        else:
            self.final_projection = nn.Identity()
            self.output_dim = block_channel[-1]
         
    def forward(self, x):
        x = self.mlp(x)
        x = torch.max(x, 1)[0]
        x = self.final_projection(x)
        return x

    def output_shape(self, input_shape=None):
        return [self.output_dim]
 
# Uni3D point cloud encoder from https://github.com/baaivision/Uni3D 
    
from openpoints.models.layers import furthest_point_sample
def fps(data, number):
    '''
        data B N 3
        number int
    '''
    fps_idx = furthest_point_sample(data[:, :, :3].contiguous(), number)
    fps_data = torch.gather(
        data, 1, fps_idx.unsqueeze(-1).long().expand(-1, -1, data.shape[-1]))
    return fps_data

# https://github.com/Strawberry-Eat-Mango/PCT_Pytorch/blob/main/util.py 
def knn_point(nsample, xyz, new_xyz):
    """
    Input:
        nsample: max sample number in local region
        xyz: all points, [B, N, C]
        new_xyz: query points, [B, S, C]
    Return:
        group_idx: grouped points index, [B, S, nsample]
    """
    sqrdists = square_distance(new_xyz, xyz)
    _, group_idx = torch.topk(sqrdists, nsample, dim = -1, largest=False, sorted=False)
    return group_idx

def square_distance(src, dst):
    """
    Calculate Euclid distance between each two points.
    src^T * dst = xn * xm + yn * ym + zn * zm;
    sum(src^2, dim=-1) = xn*xn + yn*yn + zn*zn;
    sum(dst^2, dim=-1) = xm*xm + ym*ym + zm*zm;
    dist = (xn - xm)^2 + (yn - ym)^2 + (zn - zm)^2
        = sum(src**2,dim=-1)+sum(dst**2,dim=-1)-2*src^T*dst
    Input:
        src: source points, [B, N, C]
        dst: target points, [B, M, C]
    Output:
        dist: per-point square distance, [B, N, M]
    """
    B, N, _ = src.shape
    _, M, _ = dst.shape
    dist = -2 * torch.matmul(src, dst.permute(0, 2, 1))
    dist += torch.sum(src ** 2, -1).view(B, N, 1)
    dist += torch.sum(dst ** 2, -1).view(B, 1, M)
    return dist    
def random_point_dropout(batch_pc, max_dropout_ratio=0.875):
    ''' batch_pc: BxNx3 '''
    B, N, _ = batch_pc.shape
    result = torch.clone(batch_pc)
    for b in range(B):
        dropout_ratio = torch.rand(1).item() * max_dropout_ratio  # 0 ~ 0.875
        drop_idx = torch.where(torch.rand(N) <= dropout_ratio)[0]
        if len(drop_idx) > 0:
            result[b, drop_idx, :] = batch_pc[b, 0, :].unsqueeze(0)  # set to the first point
    return result

class PatchDropout(nn.Module):
    """
    https://arxiv.org/abs/2212.00794
    """

    def __init__(self, prob, exclude_first_token=True):
        super().__init__()
        assert 0 <= prob < 1.
        self.prob = prob
        self.exclude_first_token = exclude_first_token  # exclude CLS token
        # logging.info("patch dropout prob is {}".format(prob))

    def forward(self, x):
        # if not self.training or self.prob == 0.:
        #     return x

        if self.exclude_first_token:
            cls_tokens, x = x[:, :1], x[:, 1:]
        else:
            cls_tokens = torch.jit.annotate(torch.Tensor, x[:, :1])

        batch = x.size()[0]
        num_tokens = x.size()[1]

        batch_indices = torch.arange(batch)
        batch_indices = batch_indices[..., None]

        keep_prob = 1 - self.prob
        num_patches_keep = max(1, int(num_tokens * keep_prob))

        rand = torch.randn(batch, num_tokens)
        patch_indices_keep = rand.topk(num_patches_keep, dim=-1).indices

        x = x[batch_indices, patch_indices_keep]

        if self.exclude_first_token:
            x = torch.cat((cls_tokens, x), dim=1)

        return x


class Group(nn.Module):
    def __init__(self, num_group, group_size):
        super().__init__()
        self.num_group = num_group
        self.group_size = group_size

    def forward(self, xyz, color):
        '''
            input: B N 3
            ---------------------------
            output: B G M 3
            center : B G 3
        '''
        batch_size, num_points, _ = xyz.shape
        # fps the centers out
        center = fps(xyz, self.num_group) # B G 3
        # knn to get the neighborhood
        # _, idx = self.knn(xyz, center) # B G M
        idx = knn_point(self.group_size, xyz, center) # B G M
        assert idx.size(1) == self.num_group
        assert idx.size(2) == self.group_size
        idx_base = torch.arange(0, batch_size, device=xyz.device).view(-1, 1, 1) * num_points
        idx = idx + idx_base
        idx = idx.view(-1)
        neighborhood = xyz.view(batch_size * num_points, -1)[idx, :]
        neighborhood = neighborhood.view(batch_size, self.num_group, self.group_size, 3).contiguous()

        neighborhood_color = color.view(batch_size * num_points, -1)[idx, :]
        neighborhood_color = neighborhood_color.view(batch_size, self.num_group, self.group_size, 3).contiguous()

        # normalize
        neighborhood = neighborhood - center.unsqueeze(2)

        features = torch.cat((neighborhood, neighborhood_color), dim=-1)
        return neighborhood, center, features

class Encoder(nn.Module):
    def __init__(self, encoder_channel):
        super().__init__()
        self.encoder_channel = encoder_channel
        self.first_conv = nn.Sequential(
            nn.Conv1d(6, 128, 1),
            nn.BatchNorm1d(128),
            nn.ReLU(inplace=True),
            nn.Conv1d(128, 256, 1)
        )
        self.second_conv = nn.Sequential(
            nn.Conv1d(512, 512, 1),
            nn.BatchNorm1d(512),
            nn.ReLU(inplace=True),
            nn.Conv1d(512, self.encoder_channel, 1)
        )
    def forward(self, point_groups):
        '''
            point_groups : B G N 3
            -----------------
            feature_global : B G C
        '''
        bs, g, n , _ = point_groups.shape
        point_groups = point_groups.reshape(bs * g, n, 6)
        # encoder
        feature = self.first_conv(point_groups.transpose(2,1))  # BG 256 n
        feature_global = torch.max(feature,dim=2,keepdim=True)[0]  # BG 256 1
        feature = torch.cat([feature_global.expand(-1,-1,n), feature], dim=1)# BG 512 n
        feature = self.second_conv(feature) # BG 1024 n
        feature_global = torch.max(feature, dim=2, keepdim=False)[0] # BG 1024
        return feature_global.reshape(bs, g, self.encoder_channel)

class PointcloudEncoder(nn.Module):
    def __init__(self, args = {
        'pc_model': 'eva02_large_patch14_448', #eva02_tiny_patch14_224
        # 'pc_model': 'eva02_tiny_patch14_224',
        'pc_feat_dim': 1024, #192
        # 'pc_feat_dim': 192,
        'embed_dim': 1024, 
        'group_size': 32,
        'num_group': 512,
        'patch_dropout': 0.5,
        'drop_path_rate': 0.2,
        'pretrained_pc': None,
        'pc_encoder_dim': 512
    }):
        super().__init__()
        point_transformer = timm.create_model(args['pc_model'], checkpoint_path=args['pretrained_pc'], drop_path_rate=args['drop_path_rate'])
        self.trans_dim = args['pc_feat_dim'] # 768
        self.embed_dim = args['embed_dim'] # 512
        self.group_size = args['group_size'] # 32
        self.num_group = args['num_group'] # 512
        # grouper
        self.group_divider = Group(num_group = self.num_group, group_size = self.group_size)
        # define the encoder
        self.encoder_dim =  args['pc_encoder_dim'] # 256
        self.encoder = Encoder(encoder_channel = self.encoder_dim)
    
        # bridge encoder and transformer
        self.encoder2trans = nn.Linear(self.encoder_dim,  self.trans_dim)
        
        # bridge transformer and clip embedding
        self.trans2embed = nn.Linear(self.trans_dim,  self.embed_dim)
        self.cls_token = nn.Parameter(torch.zeros(1, 1, self.trans_dim))
        self.cls_pos = nn.Parameter(torch.randn(1, 1, self.trans_dim))

        self.pos_embed = nn.Sequential(
            nn.Linear(3, 128),
            nn.GELU(),
            nn.Linear(128, self.trans_dim)
        )  
        # setting a patch_dropout of 0. would mean it is disabled and this function would be the identity fn
        self.patch_dropout = PatchDropout(args['patch_dropout']) if args['patch_dropout'] > 0. else nn.Identity()
        self.visual_pos_drop = point_transformer.pos_drop
        self.visual_blocks = point_transformer.blocks
        self.visual_norm = point_transformer.norm
        self.visual_fc_norm = point_transformer.fc_norm
        
        cur_dir =os.getcwd()
        state_dict = torch.load(os.path.join(cur_dir, 'droid_policy_learning/Uni3D_large/model.pt'))['module']
        
        for key in list(state_dict.keys()):
            state_dict[key.replace('point_encoder.', '').replace('visual.', 'visual_')] = state_dict[key]
        for key in list(state_dict.keys()):
            if key not in self.state_dict():
                del state_dict[key]
        self.load_state_dict(state_dict)


    def forward(self, pcd):
        pcd = random_point_dropout(pcd, max_dropout_ratio=0.8)
        pts = pcd[..., :3].contiguous()
        colors = pcd[..., 3:].contiguous()
        # divide the point cloud in the same form. This is important
        _, center, features = self.group_divider(pts, colors)

        # encoder the input cloud patches
        group_input_tokens = self.encoder(features)  #  B G N
        group_input_tokens = self.encoder2trans(group_input_tokens)
        # prepare cls
        cls_tokens = self.cls_token.expand(group_input_tokens.size(0), -1, -1)  
        cls_pos = self.cls_pos.expand(group_input_tokens.size(0), -1, -1)  
        # add pos embedding
        pos = self.pos_embed(center)
        # final input
        x = torch.cat((cls_tokens, group_input_tokens), dim=1)
        pos = torch.cat((cls_pos, pos), dim=1)
        # transformer
        x = x + pos
        # x = x.half()
        
        # a patch_dropout of 0. would mean it is disabled and this function would do nothing but return what was passed in
        x = self.patch_dropout(x)

        x = self.visual_pos_drop(x)

        # ModuleList not support forward
        for i, blk in enumerate(self.visual_blocks):
            x = blk(x)
        
        x = self.visual_norm(x[:, 0, :])
        x = self.visual_fc_norm(x)

        x = self.trans2embed(x)
        
        # total_sum = 0

        # for param in self.parameters():
        #     total_sum += param.sum()
        
        return x
    def output_shape(self, input_shape=None):
        return [self.embed_dim]
    
class Dino(nn.Module):
    def __init__(self):
        super().__init__()
        model = timm.create_model('vit_base_patch14_reg4_dinov2.lvd142m', checkpoint_path='/cephfs/shared/yangrujia/project/droid_policy_learning/dino/pytorch_model.bin', num_classes=0)
        data_config = timm.data.resolve_model_data_config(model)
        transforms = timm.data.create_transform(**data_config, is_training=True)
        self.model = model
        self.transforms = transforms
    
    def forward(self, x):
        return self.model(self.transforms(x))
    
    def output_shape(self, input_shape=None):
        return [768]
        