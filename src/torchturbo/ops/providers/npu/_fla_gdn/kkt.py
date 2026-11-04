"""Reuse the FLA KKT kernel across packed chunk-count alignment classes.

fla-npu 26.9.1 drops the top-level ``fla`` package (the vendored triton_core is
shipped only as ``fla_npu.ops.triton.triton_core``) and splits the kernel's
single ``H`` argument into ``H_K``/``H_V`` for GQA.
"""

import torch
import triton

from fla_npu.ops.triton import chunk_scaled_dot_kkt_fwd as _upstream_fwd
from fla_npu.ops.triton.triton_core.chunk_scaled_dot_kkt import (
    chunk_scaled_dot_kkt_fwd_kernel as _upstream_kernel,
)

# Keep the upstream computation while excluding packed work counts from the
# JIT key. A change in NT divisibility must not compile another binary.
_kkt_fwd_kernel = triton.jit(
    _upstream_kernel.fn.fn, do_not_specialize=["T", "NT", "TOTAL_TASKS"]
)


def chunk_scaled_dot_kkt_fwd(
    k: torch.Tensor,
    g: torch.Tensor,
    beta: torch.Tensor,
    cu_seqlens: torch.Tensor | None = None,
    chunk_indices: torch.Tensor | None = None,
    chunk_size: int = 64,
    output_dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    """Compute gated KKT for the adapter's head-first keys and packed metadata."""
    if chunk_size != 64:
        return _upstream_fwd(
            k=k,
            g=g,
            beta=beta,
            cu_seqlens=cu_seqlens,
            chunk_indices=chunk_indices,
            chunk_size=chunk_size,
            output_dtype=output_dtype,
        )
    batch, key_heads, tokens, dim = k.shape
    value_heads = beta.shape[-1]
    if value_heads % key_heads != 0:
        raise ValueError(
            "Expected beta/g head count Hv to be a multiple of Hk, "
            f"got Hk={key_heads}, Hv={value_heads}."
        )
    num_chunks = (
        triton.cdiv(tokens, chunk_size) if cu_seqlens is None else len(chunk_indices)
    )
    beta = beta.transpose(1, 2).contiguous()
    g = g.transpose(1, 2).contiguous()
    output = torch.empty(
        batch, tokens, value_heads, chunk_size, device=k.device, dtype=output_dtype
    )
    _kkt_fwd_kernel[(24,)](
        k=k,
        g=g,
        beta=beta,
        A=output,
        cu_seqlens=cu_seqlens,
        chunk_indices=chunk_indices,
        T=tokens,
        H_K=key_heads,
        H_V=value_heads,
        K=dim,
        BT=chunk_size,
        BK=128,
        IS_VARLEN=cu_seqlens is not None,
        USE_G=True,
        NT=num_chunks,
        B=batch,
        TOTAL_TASKS=batch * num_chunks,
    )
    return output
