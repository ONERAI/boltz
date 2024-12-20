from typing import Dict, Tuple

from fairscale.nn.checkpoint.checkpoint_activations import checkpoint_wrapper
import torch
from torch import Tensor, nn

from boltz.data import const
from boltz.model.layers.attention import AttentionPairBias
from boltz.model.layers.dropout import get_dropout_mask
from boltz.model.layers.outer_product_mean import OuterProductMean
from boltz.model.layers.pair_averaging import PairWeightedAveraging
from boltz.model.layers.transition import Transition
from boltz.model.layers.triangular_attention.attention import (
    TriangleAttentionEndingNode,
    TriangleAttentionStartingNode,
)
from boltz.model.layers.triangular_mult import (
    TriangleMultiplicationIncoming,
    TriangleMultiplicationOutgoing,
)
from boltz.model.modules.encoders import AtomAttentionEncoder


class InputEmbedder(nn.Module):
    """Input embedder."""

    def __init__(
        self,
        atom_s: int,
        atom_z: int,
        token_s: int,
        token_z: int,
        atoms_per_window_queries: int,
        atoms_per_window_keys: int,
        atom_feature_dim: int,
        atom_encoder_depth: int,
        atom_encoder_heads: int,
        no_atom_encoder: bool = False,
    ) -> None:
        """Initialize the input embedder.

        Parameters
        ----------
        atom_s : int
            The atom single representation dimension.
        atom_z : int
            The atom pair representation dimension.
        token_s : int
            The single token representation dimension.
        token_z : int
            The pair token representation dimension.
        atoms_per_window_queries : int
            The number of atoms per window for queries.
        atoms_per_window_keys : int
            The number of atoms per window for keys.
        atom_feature_dim : int
            The atom feature dimension.
        atom_encoder_depth : int
            The atom encoder depth.
        atom_encoder_heads : int
            The atom encoder heads.
        no_atom_encoder : bool, optional
            Whether to use the atom encoder, by default False

        """
        super().__init__()
        self.token_s = token_s
        self.no_atom_encoder = no_atom_encoder

        if not no_atom_encoder:
            self.atom_attention_encoder = AtomAttentionEncoder(
                atom_s=atom_s,
                atom_z=atom_z,
                token_s=token_s,
                token_z=token_z,
                atoms_per_window_queries=atoms_per_window_queries,
                atoms_per_window_keys=atoms_per_window_keys,
                atom_feature_dim=atom_feature_dim,
                atom_encoder_depth=atom_encoder_depth,
                atom_encoder_heads=atom_encoder_heads,
                structure_prediction=False,
            )

    def forward(self, feats: Dict[str, Tensor]) -> Tensor:
        """Perform the forward pass.

        Parameters
        ----------
        feats : Dict[str, Tensor]
            Input features

        Returns
        -------
        Tensor
            The embedded tokens.

        """
        # Load relevant features
        res_type = feats["res_type"]
        profile = feats["profile"]
        deletion_mean = feats["deletion_mean"].unsqueeze(-1)
        pocket_feature = feats["pocket_feature"]

        # Compute input embedding
        if self.no_atom_encoder:
            a = torch.zeros(
                (res_type.shape[0], res_type.shape[1], self.token_s),
                device=res_type.device,
            )
        else:
            a, _, _, _, _ = self.atom_attention_encoder(feats)
        s = torch.cat([a, res_type, profile, deletion_mean, pocket_feature], dim=-1)
        return s


class MSAModule(nn.Module):
    """MSA module."""

    def __init__(
        self,
        msa_s: int,
        token_z: int,
        s_input_dim: int,
        msa_blocks: int,
        msa_dropout: float,
        z_dropout: float,
        pairwise_head_width: int = 32,
        pairwise_num_heads: int = 4,
        activation_checkpointing: bool = False,
        use_paired_feature: bool = False,
        offload_to_cpu: bool = False,
        chunk_heads_pwa: bool = False,
        chunk_size_transition_z: int = None,
        chunk_size_transition_msa: int = None,
        chunk_size_outer_product: int = None,
        chunk_size_tri_attn: int = None,
        **kwargs,
    ) -> None:
        """Initialize the MSA module.

        Parameters
        ----------
        msa_s : int
            The MSA embedding size.
        token_z : int
            The token pairwise embedding size.
        s_input_dim : int
            The input sequence dimension.
        msa_blocks : int
            The number of MSA blocks.
        msa_dropout : float
            The MSA dropout.
        z_dropout : float
            The pairwise dropout.
        pairwise_head_width : int, optional
            The pairwise head width, by default 32
        pairwise_num_heads : int, optional
            The number of pairwise heads, by default 4
        activation_checkpointing : bool, optional
            Whether to use activation checkpointing, by default False
        use_paired_feature : bool, optional
            Whether to use the paired feature, by default False
        offload_to_cpu : bool, optional
            Whether to offload to CPU, by default False
        chunk_heads_pwa : bool, optional
            Chunk heads for PWA, by default False
        chunk_size_transition_z : int, optional
            Chunk size for transition Z, by default None
        chunk_size_transition_msa : int, optional
            Chunk size for transition MSA, by default None
        chunk_size_outer_product : int, optional
            Chunk size for outer product, by default None
        chunk_size_tri_attn : int, optional
            Chunk size for triangle attention, by default None

        """
        super().__init__()
        self.msa_blocks = msa_blocks
        self.msa_dropout = msa_dropout
        self.z_dropout = z_dropout
        self.use_paired_feature = use_paired_feature

        self.s_proj = nn.Linear(s_input_dim, msa_s, bias=False)
        self.msa_proj = nn.Linear(
            const.num_tokens + 2 + int(use_paired_feature),
            msa_s,
            bias=False,
        )
        self.layers = nn.ModuleList()
        for i in range(msa_blocks):
            if activation_checkpointing:
                self.layers.append(
                    checkpoint_wrapper(
                        MSALayer(
                            msa_s,
                            token_z,
                            msa_dropout,
                            z_dropout,
                            pairwise_head_width,
                            pairwise_num_heads,
                            chunk_heads_pwa=chunk_heads_pwa,
                            chunk_size_transition_z=chunk_size_transition_z,
                            chunk_size_transition_msa=chunk_size_transition_msa,
                            chunk_size_outer_product=chunk_size_outer_product,
                            chunk_size_tri_attn=chunk_size_tri_attn,
                        ),
                        offload_to_cpu=offload_to_cpu,
                    )
                )
            else:
                self.layers.append(
                    MSALayer(
                        msa_s,
                        token_z,
                        msa_dropout,
                        z_dropout,
                        pairwise_head_width,
                        pairwise_num_heads,
                        chunk_heads_pwa=chunk_heads_pwa,
                        chunk_size_transition_z=chunk_size_transition_z,
                        chunk_size_transition_msa=chunk_size_transition_msa,
                        chunk_size_outer_product=chunk_size_outer_product,
                        chunk_size_tri_attn=chunk_size_tri_attn,
                    )
                )

    def forward(self, z: Tensor, emb: Tensor, feats: Dict[str, Tensor]) -> Tensor:
        """Perform the forward pass.

        Parameters
        ----------
        z : Tensor
            The pairwise embeddings
        emb : Tensor
            The input embeddings
        feats : Dict[str, Tensor]
            Input features

        Returns
        -------
        Tensor
            The output pairwise embeddings.

        """
        # Load relevant features
        msa = feats["msa"]
        has_deletion = feats["has_deletion"].unsqueeze(-1)
        deletion_value = feats["deletion_value"].unsqueeze(-1)
        is_paired = feats["msa_paired"].unsqueeze(-1)
        msa_mask = feats["msa_mask"]
        token_mask = feats["token_pad_mask"].float()
        token_mask = token_mask[:, :, None] * token_mask[:, None, :]

        # Compute MSA embeddings
        if self.use_paired_feature:
            m = torch.cat([msa, has_deletion, deletion_value, is_paired], dim=-1)
        else:
            m = torch.cat([msa, has_deletion, deletion_value], dim=-1)

        # Compute input projections
        m = self.msa_proj(m)
        m = m + self.s_proj(emb).unsqueeze(1)

        # Perform MSA blocks
        for i in range(self.msa_blocks):
            z, m = self.layers[i](z, m, token_mask, msa_mask)
        return z


class MSALayer(nn.Module):
    """MSA module."""

    def __init__(
        self,
        msa_s: int,
        token_z: int,
        msa_dropout: float,
        z_dropout: float,
        pairwise_head_width: int = 32,
        pairwise_num_heads: int = 4,
        chunk_heads_pwa: bool = False,
        chunk_size_transition_z: int = None,
        chunk_size_transition_msa: int = None,
        chunk_size_outer_product: int = None,
        chunk_size_tri_attn: int = None,
    ) -> None:
        """Initialize the MSA module.

        Parameters
        ----------

        msa_s : int
            The MSA embedding size.
        token_z : int
            The pair representation dimention.
        msa_dropout : float
            The MSA dropout.
        z_dropout : float
            The pair dropout.
        pairwise_head_width : int, optional
            The pairwise head width, by default 32
        pairwise_num_heads : int, optional
            The number of pairwise heads, by default 4
        chunk_heads_pwa : bool, optional
            Chunk heads for PWA, by default False
        chunk_size_transition_z : int, optional
            Chunk size for transition Z, by default None
        chunk_size_transition_msa : int, optional
            Chunk size for transition MSA, by default None
        chunk_size_outer_product : int, optional
            Chunk size for outer product, by default None
        chunk_size_tri_attn : int, optional
            Chunk size for triangle attention, by default None

        """
        super().__init__()
        self.msa_dropout = msa_dropout
        self.z_dropout = z_dropout
        self.chunk_size_tri_attn = chunk_size_tri_attn
        self.msa_transition = Transition(
            dim=msa_s, hidden=msa_s * 4, chunk_size=chunk_size_transition_msa
        )
        self.pair_weighted_averaging = PairWeightedAveraging(
            c_m=msa_s,
            c_z=token_z,
            c_h=32,
            num_heads=8,
            chunk_heads=chunk_heads_pwa,
        )

        self.tri_mul_out = TriangleMultiplicationOutgoing(token_z)
        self.tri_mul_in = TriangleMultiplicationIncoming(token_z)
        self.tri_att_start = TriangleAttentionStartingNode(
            token_z, pairwise_head_width, pairwise_num_heads, inf=1e9
        )
        self.tri_att_end = TriangleAttentionEndingNode(
            token_z, pairwise_head_width, pairwise_num_heads, inf=1e9
        )
        self.z_transition = Transition(
            dim=token_z,
            hidden=token_z * 4,
            chunk_size=chunk_size_transition_z,
        )
        self.outer_product_mean = OuterProductMean(
            c_in=msa_s,
            c_hidden=32,
            c_out=token_z,
            chunk_size=chunk_size_outer_product,
        )

    def forward(
        self, z: Tensor, m: Tensor, token_mask: Tensor, msa_mask: Tensor
    ) -> Tuple[Tensor, Tensor]:
        """Perform the forward pass.

        Parameters
        ----------
        z : Tensor
            The pair representation
        m : Tensor
            The msa representation
        token_mask : Tensor
            The token mask
        msa_mask : Dict[str, Tensor]
            The MSA mask

        Returns
        -------
        Tensor
            The output pairwise embeddings.
        Tensor
            The output MSA embeddings.

        """
        # Communication to MSA stack
        msa_dropout = get_dropout_mask(self.msa_dropout, m, self.training)
        m = m + msa_dropout * self.pair_weighted_averaging(m, z, token_mask)
        m = m + self.msa_transition(m)

        # Communication to pairwise stack
        z = z + self.outer_product_mean(m, msa_mask)

        # Compute pairwise stack
        dropout = get_dropout_mask(self.z_dropout, z, self.training)
        z = z + dropout * self.tri_mul_out(z, mask=token_mask)

        dropout = get_dropout_mask(self.z_dropout, z, self.training)
        z = z + dropout * self.tri_mul_in(z, mask=token_mask)

        dropout = get_dropout_mask(self.z_dropout, z, self.training)
        z = z + dropout * self.tri_att_start(
            z,
            mask=token_mask,
            chunk_size=self.chunk_size_tri_attn if not self.training else None,
        )

        dropout = get_dropout_mask(self.z_dropout, z, self.training, columnwise=True)
        z = z + dropout * self.tri_att_end(
            z,
            mask=token_mask,
            chunk_size=self.chunk_size_tri_attn if not self.training else None,
        )

        z = z + self.z_transition(z)

        return z, m


class PairformerModule(nn.Module):
    """Pairformer module."""

    def __init__(
        self,
        token_s: int,
        token_z: int,
        num_blocks: int,
        num_heads: int = 16,
        dropout: float = 0.25,
        pairwise_head_width: int = 32,
        pairwise_num_heads: int = 4,
        activation_checkpointing: bool = False,
        no_update_s: bool = False,
        no_update_z: bool = False,
        offload_to_cpu: bool = False,
        chunk_size_tri_attn: int = None,
        **kwargs,
    ) -> None:
        """Initialize the Pairformer module.

        Parameters
        ----------
        token_s : int
            The token single embedding size.
        token_z : int
            The token pairwise embedding size.
        num_blocks : int
            The number of blocks.
        num_heads : int, optional
            The number of heads, by default 16
        dropout : float, optional
            The dropout rate, by default 0.25
        pairwise_head_width : int, optional
            The pairwise head width, by default 32
        pairwise_num_heads : int, optional
            The number of pairwise heads, by default 4
        activation_checkpointing : bool, optional
            Whether to use activation checkpointing, by default False
        no_update_s : bool, optional
            Whether to update the single embeddings, by default False
        no_update_z : bool, optional
            Whether to update the pairwise embeddings, by default False
        offload_to_cpu : bool, optional
            Whether to offload to CPU, by default False
        chunk_size_tri_attn : int, optional
            The chunk size for triangle attention, by default None

        """
        super().__init__()
        self.token_z = token_z
        self.num_blocks = num_blocks
        self.dropout = dropout
        self.num_heads = num_heads

        self.layers = nn.ModuleList()
        for i in range(num_blocks):
            if activation_checkpointing:
                self.layers.append(
                    checkpoint_wrapper(
                        PairformerLayer(
                            token_s,
                            token_z,
                            num_heads,
                            dropout,
                            pairwise_head_width,
                            pairwise_num_heads,
                            no_update_s,
                            False if i < num_blocks - 1 else no_update_z,
                            chunk_size_tri_attn,
                        ),
                        offload_to_cpu=offload_to_cpu,
                    )
                )
            else:
                self.layers.append(
                    PairformerLayer(
                        token_s,
                        token_z,
                        num_heads,
                        dropout,
                        pairwise_head_width,
                        pairwise_num_heads,
                        no_update_s,
                        False if i < num_blocks - 1 else no_update_z,
                        chunk_size_tri_attn,
                    )
                )

    def forward(
        self,
        s: Tensor,
        z: Tensor,
        mask: Tensor,
        pair_mask: Tensor,
    ) -> Tuple[Tensor, Tensor]:
        """Perform the forward pass.

        Parameters
        ----------
        s : Tensor
            The sequence embeddings
        z : Tensor
            The pairwise embeddings
        mask : Tensor
            The token mask
        pair_mask : Tensor
            The pairwise mask
        Returns
        -------
        Tensor
            The updated sequence embeddings.
        Tensor
            The updated pairwise embeddings.

        """
        for layer in self.layers:
            s, z = layer(s, z, mask, pair_mask)
        return s, z


class PairformerLayer(nn.Module):
    """Pairformer module."""

    def __init__(
        self,
        token_s: int,
        token_z: int,
        num_heads: int = 16,
        dropout: float = 0.25,
        pairwise_head_width: int = 32,
        pairwise_num_heads: int = 4,
        no_update_s: bool = False,
        no_update_z: bool = False,
        chunk_size_tri_attn: int = None,
    ) -> None:
        """Initialize the Pairformer module.

        Parameters
        ----------
        token_s : int
            The token single embedding size.
        token_z : int
            The token pairwise embedding size.
        num_heads : int, optional
            The number of heads, by default 16
        dropout : float, optiona
            The dropout rate, by default 0.25
        pairwise_head_width : int, optional
            The pairwise head width, by default 32
        pairwise_num_heads : int, optional
            The number of pairwise heads, by default 4
        no_update_s : bool, optional
            Whether to update the single embeddings, by default False
        no_update_z : bool, optional
            Whether to update the pairwise embeddings, by default False
        chunk_size_tri_attn : int, optional
            The chunk size for triangle attention, by default None

        """
        super().__init__()
        self.token_z = token_z
        self.dropout = dropout
        self.num_heads = num_heads
        self.no_update_s = no_update_s
        self.no_update_z = no_update_z
        self.chunk_size_tri_attn = chunk_size_tri_attn
        if not self.no_update_s:
            self.attention = AttentionPairBias(token_s, token_z, num_heads)
        self.tri_mul_out = TriangleMultiplicationOutgoing(token_z)
        self.tri_mul_in = TriangleMultiplicationIncoming(token_z)
        self.tri_att_start = TriangleAttentionStartingNode(
            token_z, pairwise_head_width, pairwise_num_heads, inf=1e9
        )
        self.tri_att_end = TriangleAttentionEndingNode(
            token_z, pairwise_head_width, pairwise_num_heads, inf=1e9
        )
        if not self.no_update_s:
            self.transition_s = Transition(token_s, token_s * 4)
        self.transition_z = Transition(token_z, token_z * 4)

    def forward(
        self,
        s: Tensor,
        z: Tensor,
        mask: Tensor,
        pair_mask: Tensor,
    ) -> Tuple[Tensor, Tensor]:
        """Perform the forward pass."""
        # Compute pairwise stack
        dropout = get_dropout_mask(self.dropout, z, self.training)
        z = z + dropout * self.tri_mul_out(z, mask=pair_mask)

        dropout = get_dropout_mask(self.dropout, z, self.training)
        z = z + dropout * self.tri_mul_in(z, mask=pair_mask)

        dropout = get_dropout_mask(self.dropout, z, self.training)
        z = z + dropout * self.tri_att_start(
            z,
            mask=pair_mask,
            chunk_size=self.chunk_size_tri_attn if not self.training else None,
        )

        dropout = get_dropout_mask(self.dropout, z, self.training, columnwise=True)
        z = z + dropout * self.tri_att_end(
            z,
            mask=pair_mask,
            chunk_size=self.chunk_size_tri_attn if not self.training else None,
        )

        z = z + self.transition_z(z)

        # Compute sequence stack
        if not self.no_update_s:
            s = s + self.attention(s, z, mask)
            s = s + self.transition_s(s)

        return s, z


class DistogramModule(nn.Module):
    """Distogram Module."""

    def __init__(self, token_z: int, num_bins: int) -> None:
        """Initialize the distogram module.

        Parameters
        ----------
        token_z : int
            The token pairwise embedding size.
        num_bins : int
            The number of bins.

        """
        super().__init__()
        self.distogram = nn.Linear(token_z, num_bins)

    def forward(self, z: Tensor) -> Tensor:
        """Perform the forward pass.

        Parameters
        ----------
        z : Tensor
            The pairwise embeddings

        Returns
        -------
        Tensor
            The predicted distogram.

        """
        z = z + z.transpose(1, 2)
        return self.distogram(z)
