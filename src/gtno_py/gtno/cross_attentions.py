from typing import final, override
import torch
import torch.nn as nn
import torch.nn.functional as F
from gtno_py.gtno.mlps import MLP
from gtno_py.gtno.shape_utils import flatten_spatiotemporal
from e3nn import o3


@final
class TemporalRoPEWithOffset(nn.Module):
    """
    Time-only Rotary Positional Embedding (RoPE) with per-head learnable offsets.

    ### Input:
    - tensor: [B, n_heads, seq_len, d_head], where:
    - `seq_len = num_nodes * num_timesteps`
    - `d_head` must be even.

    ### Process:
    1. Generate time indices such that groups of `num_nodes` share the same timestep.
    2. Compute cos/sin embeddings for `num_timesteps`, adjusted by per-head offsets.
    3. Apply RoPE by rotating even/odd tensor components using the cos/sin values.

    ### Output:
    - Rotated tensor of the same shape [B, n_heads, seq_len, d_head].

    ### Features:
    - Per-head phase offsets allow temporal alignment for each attention head.
    - Consistent rotations across nodes within the same timestep.
    """

    def __init__(self, num_timesteps: int, d_head: int, n_heads: int, base: float = 1000.0, learnable_offset: bool = False):
        super().__init__()
        assert d_head % 2 == 0, "d_head must be even for standard RoPE."

        self.num_timesteps = num_timesteps
        self.d_head = d_head
        self.n_heads = n_heads
        self.base = base

        self.half_dim = d_head // 2

        if learnable_offset:
            # Each of n_heads gets its own offset, initialised to 0
            self.offset = nn.Parameter(torch.zeros(n_heads, device="cuda"))
        else:
            # A fixed buffer, all zeros by default
            self.register_buffer("offset", torch.zeros(n_heads, device="cuda"), persistent=False)

        self.freqs = (1.0 / (self.base ** (2 * torch.arange(0, self.half_dim, device=self.offset.device).float() / d_head))).unsqueeze(0).unsqueeze(0)  # [1, 1, half_dim]

    @override
    def forward(self, tensor: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        """
        Args:
            tensor: shape [B, n_heads, seq_len, d_head].
                  ` seq_len = num_nodes * num_timesteps
                  ` B = batch size
                  ` d_head = hidden dimension
                  ` n_heads = number of attention heads

        Returns:
            rotated: shape [B, n_heads, seq_len, d_head] tensor with RoPE applied to each group of `num_nodes`.
        """
        B, H, seq_len, d_head = tensor.shape
        num_nodes = seq_len // self.num_timesteps
        assert H == self.n_heads, f"Expected n_heads={self.n_heads}, got {H}"
        assert d_head == self.d_head, f"Expected d_head={self.d_head}, got {d_head}"
        assert seq_len % self.num_timesteps == 0, f"seq_len={seq_len} must be divisible by num_timesteps={self.num_timesteps}."

        # 1) Create integer time indices for each chunk of num_nodes => shape [seq_len]
        #    e.g., times = [0,0,...,0,1,1,...,1,..., T-1, T-1,..., T-1], each repeated num_nodes times.
        times = torch.arange(self.num_timesteps, device=tensor.device).unsqueeze(1)  # [T,1]
        positions = torch.repeat_interleave(times, num_nodes, dim=1).flatten(0, 1)  # [N*T=seq_len]

        # 3) Construct angles per head: shape => [H, seq_len, half_dim].
        #    For each head i, angle_i = (positions + offset[i]) * freqs
        #    Broadcast offset[i] across all positions.
        #    offset: [H], positions: [seq_len]
        #    => positions + offset[i] => shape [H, seq_len], then multiply by freqs => shape [H, seq_len, half_dim].
        offset_broadcast = self.offset.unsqueeze(-1)  # [H, 1], this adds the head dim
        positions_broadcast = positions.unsqueeze(0)  # [1, seq_len]
        # shape => [H, seq_len]
        shifted_positions = positions_broadcast + offset_broadcast
        # shape => [H, seq_len, half_dim]
        angle = shifted_positions.unsqueeze(-1) * self.freqs

        # 4) cos, sin => each [1, H, seq_len, half_dim]
        cos_t = angle.cos().unsqueeze(0)
        sin_t = angle.sin().unsqueeze(0)

        # 5) Expand cos_t/sin_t to [B, H, seq_len, half_dim]
        cos_t = cos_t.expand(B, -1, seq_len, self.half_dim)
        sin_t = sin_t.expand(B, -1, seq_len, self.half_dim)

        # Avoid rotating padded nodes
        mask_seq = mask.expand(B, self.num_timesteps, num_nodes, 1).reshape(B, 1, seq_len, 1)
        cos_t = torch.where(mask_seq.bool(), cos_t, torch.ones_like(cos_t))
        sin_t = torch.where(mask_seq.bool(), sin_t, torch.zeros_like(sin_t))

        # 6) Apply the rotation to the last dimension of 'tensor'
        #    Even indices => [0::2], odd => [1::2]
        t1 = tensor[..., 0::2]  # [B, H, seq_len, half_dim]
        t2 = tensor[..., 1::2]  # [B, H, seq_len, half_dim]

        rotated_0 = t1 * cos_t - t2 * sin_t
        rotated_1 = t1 * sin_t + t2 * cos_t

        # Re-interleave - view_as does the interleaving
        # [B, H, seq_len, d_head]
        rotated = torch.stack([rotated_0, rotated_1], dim=-1).view_as(tensor)

        return rotated


@final
class SphericalHarmonicsAttentionBias(nn.Module):
    """
    Computes a bias for attention logits from the relative node coordinates.
    For each pair of sequence elements, the module computes the relative difference,
    encodes it using spherical harmonics up to a chosen maximum degree, and then maps
    the concatenated coefficients through an MLP to produce a scalar bias per head.

    Args:
        max_degree (int): Maximum spherical harmonics degree (l) to compute (default: 1).
        num_heads (int): Number of attention heads.
        hidden_dim (int): Hidden dimension in the intermediate MLP.
    """

    def __init__(self, num_timesteps: int, max_degree: int, num_heads: int, hidden_dim: int):
        super().__init__()
        self.max_degree = max_degree
        self.num_heads = num_heads
        # Total number of coefficients from l=0 to max_degree.
        self.num_coeff = sum(2 * l + 1 for l in range(max_degree + 1))
        self.num_timesteps = num_timesteps
        self.mlp = MLP(in_features=self.num_coeff, out_features=num_heads, hidden_features=hidden_dim, hidden_layers=2, activation=nn.SiLU(), dropout_p=0.1)
        self.eps = 1e-6

    @override
    def forward(self, coords: torch.Tensor) -> torch.Tensor:
        """
        Args:
            coords: shape [B, seq_len, 3].
                  ` seq_len = num_nodes * num_timesteps
                  ` B = batch size
                  ` 3 = x, y, z coordinates

        Returns:
            bias: shape [B, num_heads, seq_len, seq_len] tensor to be added to attention logits.
        """
        coords = coords.clone()[..., :3]
        B, S, _ = coords.shape
        coords = flatten_spatiotemporal(coords, self.num_timesteps)  # now shape [B, N*T, 3]
        # Compute pairwise relative differences: r_ij = coords_i - coords_j.
        relative_distance: torch.Tensor = coords.unsqueeze(2) - coords.unsqueeze(1)  # [B, S, S, 3]
        # Compute the norm (magnitude) and normalized direction.
        norm: torch.Tensor = relative_distance.norm(dim=-1, keepdim=True)  # [B, S, S, 1]
        unit_rel = relative_distance / (norm + self.eps)  # [B, S, S, 3]

        sh_features = []
        # For each degree l = 0, 1, ..., max_degree, compute spherical harmonics.
        for l in range(self.max_degree + 1):
            # o3.spherical_harmonics returns shape [B, S, S, 2l+1].
            Y_l = o3.spherical_harmonics(l, unit_rel, normalize=True)
            sh_features.append(Y_l)
        # Concatenate coefficients over l to form shape [B, S, S, num_coeff].
        sh_cat = torch.cat(sh_features, dim=-1)
        # Map the concatenated coefficients to a bias per head.
        bias: torch.Tensor = self.mlp(sh_cat)  # [B, S, S, num_heads]
        # Rearrange to [B, num_heads, S, S].
        bias = bias.permute(0, 3, 1, 2)
        return bias


@final
class QuadraticHeterogenousCrossAttention(nn.Module):
    def __init__(
        self,
        num_hetero_feats: int,
        lifting_dim: int,
        num_heads: int,
        num_timesteps: int,
        use_rope: bool,
        use_spherical_harmonics: bool,
        learnable_attention_denom: bool = False,
        attention_dropout: float = 0.2,
    ) -> None:
        """
        Heterogenous graph cross attention. We construct separate K/V projections
        for each heterogeneous feature, then perform cross attention on queries
        generated from the q_data ("trunk").

        - The code is unchanged except for rope_on logic.
        - We do NOT alter your custom attention formula.
        - RoPE is optional; if rope_on=True, we apply it to Q and K in the last dimension.

        Args:
          num_hetero_feats: number of heterogeneous features
          lifting_dim: dimension for Q,K,V
          num_heads: number of attention heads
          num_timesteps: used for flatten_spatiotemporal
          rope_on: if True, apply RoPE to Q and K
          max_seq_len: maximum sequence length for precomputed RoPE
        """
        super().__init__()

        self.num_heads = num_heads
        self.num_hetero_feats = num_hetero_feats
        self.lifting_dim = lifting_dim
        self.num_timesteps = num_timesteps
        self.use_rope = use_rope
        self.use_spherical_harmonics = use_spherical_harmonics
        self.d_head = self.lifting_dim // self.num_heads

        assert self.d_head % 2 == 0, "d_head must be even"

        # Query projection (applied to node embeddings)
        self.query = nn.Linear(lifting_dim, lifting_dim)

        # Keys/Values for heterogeneous features
        self.kv_projs = nn.ModuleList([nn.Linear(lifting_dim, 2 * lifting_dim) for _ in range(num_hetero_feats)])
        self.out_proj = nn.Linear(lifting_dim, lifting_dim)
        self.attention_dropout = nn.Dropout(attention_dropout)

        denom_init = torch.full((num_heads,), float(self.d_head))
        if learnable_attention_denom:
            self.attention_denom = nn.Parameter(denom_init)
        else:
            self.register_buffer("attention_denom", denom_init, persistent=False)

        self.feature_weights = nn.Parameter(torch.randn(self.num_hetero_feats) * 0.1)

        if use_rope:
            self.rope = TemporalRoPEWithOffset(num_timesteps=self.num_timesteps, d_head=self.d_head, n_heads=self.num_heads, base=1000.0, learnable_offset=False)

        if use_spherical_harmonics:
            self.spherical_harmonics = SphericalHarmonicsAttentionBias(num_timesteps=self.num_timesteps, max_degree=1, num_heads=self.num_heads, hidden_dim=16)

    @override
    def forward(self, x_0: torch.Tensor, v_0: torch.Tensor, concatenated_features: torch.Tensor, q_data: torch.Tensor, mask: torch.Tensor | None = None) -> torch.Tensor:
        """
        1. Flatten queries => [B, seq_q, d], then project to [B, heads, seq_q, d_head].
        2. For each heterogeneous feature, do:
           - Flatten => [B, seq_k, d],
           - Project K => [B, heads, seq_k, d_head] and V => same shape,
           - (optionally apply RoPE),
           - Compute Q·K^T, softmax over seq_k, multiply by V,
           - Gate and accumulate to `out_sum`.
        3. Reshape the final heads => [B, seq_q, d], project, unflatten => [B*T, N, d].
        4. Store result in batch["x_0"] and return.
        """
        # Flatten Q data: [B, T, N, d] -> [B, N * T (seq_q), d]
        B, T, N, d = q_data.shape
        q_data = q_data.view(B, T * N, d)

        if mask is not None:
            # Expand to [B, 1, T*N, T*N] to mask attention matrix properly
            mask_expanded = mask.expand(B, T, N, 1).reshape(B, T * N, 1)
            key_mask = mask_expanded.transpose(1, 2)
            key_mask = key_mask.unsqueeze(1)

        # Project Q => [B, heads, seq_q, d_head]
        q_proj: torch.Tensor = self.query(q_data).view(B, T * N, self.num_heads, self.d_head).permute(0, 2, 1, 3)  # [B, heads, seq_q, d_head]

        if self.use_rope:
            q_proj = self.rope(q_proj, mask)

        if self.use_spherical_harmonics:
            bias: torch.Tensor = self.spherical_harmonics(x_0[..., :3])

        # We'll accumulate over multiple heterogeneous features
        out_sum = torch.zeros_like(q_proj)

        # Collect the features of shape [B, N*T, d]
        hetero_features = [
            x_0,
            v_0,
            concatenated_features,
        ]
        assert len(hetero_features) == self.num_hetero_feats

        gates = F.softmax(self.feature_weights, dim=0)  # Precompute gates; ∑ gates = 1
        for i, h_feat in enumerate(hetero_features):
            # h_feat: [B, T * N, d]
            assert h_feat.shape[-1] == self.lifting_dim, f"Expected {self.lifting_dim}, got {h_feat.shape[-1]}"

            # Project K and V => [B, heads, seq_k, d_head]
            kv: torch.Tensor = self.kv_projs[i](h_feat)
            k_proj, v_proj = torch.chunk(kv, 2, dim=-1)
            k_proj = k_proj.view(B, N * T, self.num_heads, self.d_head).permute(0, 2, 1, 3)
            v_proj = v_proj.view(B, N * T, self.num_heads, self.d_head).permute(0, 2, 1, 3)

            if self.use_rope:
                k_proj: torch.Tensor = self.rope(k_proj, mask)

            # 1) scores = Q·K^T / sqrt(d_head)
            scores = q_proj @ k_proj.transpose(-2, -1) / self.attention_denom.view(1, -1, 1, 1)  # Broadcasts over heads
            if mask is not None:
                # scores shape is [B, heads, seq_q, seq_k] = [B, heads, T*N, T*N]
                assert key_mask.shape == (B, 1, 1, T * N), f"Expected mask shape (B,1,1,T*N) but got {key_mask.shape}"
                scores = scores.masked_fill(key_mask == 0, float("-inf"))

            # 2) softmax over seq_k dimension (dim=-1)
            if self.use_spherical_harmonics:
                scores = scores + bias
            attn_weights: torch.Tensor = self.attention_dropout(F.softmax(scores, dim=-1))
            # 3) multiply by V
            out_i = attn_weights @ v_proj

            # Gate
            out_sum = out_sum + gates[i] * out_i

        out_sum = out_sum.permute(0, 2, 1, 3).reshape(B, T * N, self.lifting_dim)
        out_sum: torch.Tensor = self.out_proj(out_sum)
        # Unflatten => [B, T, N, d]
        out = out_sum.view(B, T, N, self.lifting_dim)

        # Unflatten => [B*T, N, d]
        return out
