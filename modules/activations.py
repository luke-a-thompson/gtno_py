import torch
import torch.nn as nn
from enum import Enum


class FFNActivation(str, Enum):
    RELU = "relu"
    RELU2 = "relu2"
    GELU = "gelu"
    SWIGLU = "swiglu"


# ReLU² Activation Function
class ReLU2(nn.Module):
    def __init__(self):
        super(ReLU2, self).__init__()

    def forward(self, x):
        return torch.relu(x).pow(2)


# SwiGLU Activation Function
class SwiGLU(nn.Module):
    def __init__(self):
        super(SwiGLU, self).__init__()

    def forward(self, x):
        # Split the input tensor into two halves along the last dimension
        x1, x2 = x.chunk(2, dim=-1)
        return x1 * torch.nn.functional.silu(x2)
