# -*- coding: utf-8 -*-
# =============================================================================
# Copyright 2022 HeliXon Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# =============================================================================
"""

"""
# =============================================================================
# Imports
# =============================================================================
import argparse
import math
import typing

import torch
from torch import nn

from omegafold import embedders, modules, utils


# =============================================================================
# Constants
# =============================================================================
# =============================================================================
# Functions
# =============================================================================
def _get_qk_scaling(num_res: torch.Tensor, attn_dim: int) -> torch.Tensor:
    """
    https://kexue.fm/archives/8823

    Args:
        num_res: [num_chunks]
        attn_dim

    Returns:

    """
    return num_res.clamp(min=4e-5).log() / (math.log(512) * attn_dim ** 0.5)


# =============================================================================
# Classes
# =============================================================================
class GatedAttentionUnit(modules.OFModule):
    """

    """

    def __init__(self, cfg: argparse.Namespace):
        super(GatedAttentionUnit, self).__init__(cfg)
        self.cfg = cfg
        self.gva_proj = nn.Sequential(
            nn.Linear(cfg.node, cfg.proj_dim * 2 + cfg.attn_dim),
            nn.SiLU()
        )
        self.multi_headed_scaling = modules.MultiHeadedScaling(
            cfg.attn_dim,
            num_heads=2,
            on_out_ready=lambda x: self.rope(x, x.ndim - 3)
        )
        self.rope = embedders.RoPE(cfg.attn_dim)
        self.relpos = embedders.RelPosEmbedder(cfg.num_relpos, embedding_dim=1)
        self.output_proj = nn.Linear(cfg.proj_dim, cfg.node)

    def forward(
            self,
            node: torch.Tensor,
            scaling: torch.Tensor,
            bias: torch.Tensor,
            fwd_cfg: typing.Optional[argparse.Namespace]
    ) -> typing.Tuple[torch.Tensor, torch.Tensor]:
        """
        The forward method of this class

        Args:
            node: the node representation
            scaling: logits scaling
            bias:
            fwd_cfg:

        Returns:

        """
        cfg = self.cfg
        # initial projection
        gates, values, base = self.gva_proj(node).split(
            [cfg.proj_dim, cfg.proj_dim, cfg.attn_dim], dim=-1
        )
        queries, keys = self.multi_headed_scaling(base)

        node, edge = modules.attention(
            query=queries,
            key=keys,
            scale=scaling,
            value=values,
            bias=bias + self.relpos(base.shape[-2])[..., 0],
            subbatch_size=fwd_cfg.subbatch_size,
            return_edge=True,
            edge_reduction='sum',
            edge_reduction_dim=-3,
        )

        # unflatten the values, base will be unflattened in self._forward
        node = node * gates
        node = self.output_proj(node)
        return node, edge


class OmegaPLMLayer(modules.OFModule):
    """One OmegaPLM Layer

    This layer baked the pre-layernorm configuration into the model

    Attributes:
        gau: the underlying GAU layer containing most of the computations

    """

    def __init__(self, cfg: argparse.Namespace) -> None:
        super(OmegaPLMLayer, self).__init__(cfg)
        self.gau = GatedAttentionUnit(cfg)
        self.AdaLN = nn.Sequential(
            nn.SiLU(),
            nn.Linear(cfg.node, cfg.node*3)
            )
        nn.init.constant_(self.AdaLN[-1].weight, 0)
        nn.init.constant_(self.AdaLN[-1].bias, 0)

    def forward(
            self,
            node: torch.Tensor,
            qk_scaling: torch.Tensor,
            bias: torch.Tensor,
            cond : torch.Tensor,
            fwd_cfg: typing.Optional[argparse.Namespace]
    ) -> typing.Tuple[torch.Tensor, torch.Tensor]:
        """Forward method for pre-layernorm

        One layer of OmegaPLM

        Args:
            node: the node representation
            qk_scaling:  the scaling of logits before attention
            bias: the bias for logits before attention
            fwd_cfg

        Returns:
            node and edge representation

        """
        gamma, beta, alpha = self.AdaLN(cond).chunk(3, dim=-1)

        shortcut, node = node, utils.normalize(node)
        node = scale_shift(node, gamma, beta)
        
        node, edge = self.gau(node, qk_scaling, bias, fwd_cfg)
        node = node * (1 + alpha.unsqueeze(1))
        node = node + shortcut
        return node, edge

@torch.jit.script #creates a kernal to make this a single opperation rather than 2 or 3 idk if 1+ counts
def scale_shift(x, scale, shift):
    return x * (1 + scale.unsqueeze(1)) + shift.unsqueeze(1)

class OmegaPLMBatchedchkp(torch.nn.Module): #to optimize chkping to decrease Gram usage for processing time
    def __init__(self, cfg, n_layers):
        super().__init__()
        self.layers = nn.ModuleList([OmegaPLMLayer(cfg) for _ in range(n_layers)])

    def forward(self,
            node: torch.Tensor,
            qk_scaling: torch.Tensor,
            bias: torch.Tensor,
            cond : torch.Tensor,
            fwd_cfg: typing.Optional[argparse.Namespace]
    ) -> typing.Tuple[torch.Tensor, torch.Tensor]:
        for layer in self.layers:
            node, _ = layer(node, qk_scaling, bias, cond, fwd_cfg)
        return node, _
    
    def chkp_forwad(self,
            node: torch.Tensor,
            qk_scaling: torch.Tensor,
            bias: torch.Tensor,
            cond : torch.Tensor,
            fwd_cfg: typing.Optional[argparse.Namespace]
    ) -> typing.Tuple[torch.Tensor, torch.Tensor]:
        return torch.utils.checkpoint.checkpoint(self.forward(node, qk_scaling, bias, cond, fwd_cfg))

class OmegaPLM(modules.OFModule):
    """Encoder GAU model

    This is the OmegaPLM model in Wu et al. 2022.

    Attributes:
        input_embedding: This is an embedding layer
        layers: the trunk of the network containing modified GAU layers
        output_norm: an output normalization layer

    """

    def __init__(self, cfg: argparse.Namespace) -> None:
        super(OmegaPLM, self).__init__(cfg) 
        # self.input_embedding = nn.Embedding(
        #     cfg.alphabet_size, cfg.node, padding_idx=cfg.padding_idx
        # )
        self.layers = nn.ModuleList(
            [OmegaPLMLayer(cfg) for _ in range(cfg.edge)]
        )        

        self.output_norm = nn.LayerNorm(cfg.node)
    
    def forward(
            self,
            node: torch.Tensor,
            mask: torch.Tensor,
            cond: torch.Tensor,
            fwd_cfg: typing.Optional[argparse.Namespace]
    ) -> typing.Tuple[torch.Tensor, torch.Tensor]:
        """Forward method

        Args:
            tokens: A tensor of input tokens,
                of shape [*, seq_len]
            mask: mask indicating the validity of the tokens,
                of shape [*, seq_len]
            fwd_cfg

        Returns:

        """
        # print(node.shape, cond.shape, mask.shape)
        qk_scaling = _get_qk_scaling(mask.sum(-1), self.cfg.attn_dim)
        qk_scaling = qk_scaling[..., None, None]
        bias = utils.mask2bias(mask[..., None, :])

        # node = self.input_embedding(tokens)
        # node *= self._get_finetuning_scale(mask, node)
        edges = torch.empty(
            len(self.layers), mask.shape[-1], mask.shape[-1],
            dtype=node.dtype, device=node.device
        )
        for i, layer in enumerate(self.layers):
            node, edges[i] = layer(node, qk_scaling, bias, cond, fwd_cfg)
        node = self.output_norm(node)

        # Taking the average
        edges /= (mask.any(-1).sum() + 1e-5)

        return node , edges


    def _get_finetuning_scale(
            self, mask: torch.Tensor, tokens: torch.Tensor
    ) -> torch.Tensor:
        """Token dropout scaling

        This computes the scaling from Rives et al. 2021

        Args:
            mask: the mask indicating the validity of the input sequence

        Returns:

        """
        un_masked_ratio_train = 1 - self.cfg.masked_ratio
        src_lengths = mask.sum(-1)
        mask_ratio_observed = tokens.eq(21).sum(-1).float() / src_lengths
        mask_ratio_observed = torch.where(
            mask_ratio_observed == 1.,
            torch.full_like(mask_ratio_observed, 0.99),
            mask_ratio_observed
        )
        return un_masked_ratio_train / (1 - mask_ratio_observed)[:, None, None]


# =============================================================================
# Tests
# =============================================================================
if __name__ == '__main__':
    pass
