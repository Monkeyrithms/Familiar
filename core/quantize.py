"""Vector compression — Matryoshka truncation + binary quantization.

Two independent, composable speedups on the SAME embeddings, no model change:

1. MATRYOSHKA TRUNCATION. text-embedding-3 models are trained so that the first
   N dimensions are themselves a valid (lower-res) embedding — the information is
   front-loaded, Russian-doll style. So you can keep the first 512 of 1536 dims
   and retain the great majority of retrieval quality at 1/3 the storage and 3x
   faster distance math. IMPORTANT: after truncating you must RE-NORMALIZE to
   unit length, or the cosine = 1 - d^2/2 identity (which the whole pipeline
   relies on) breaks.

2. BINARY QUANTIZATION. Map each dimension to a single bit (>=0 -> 1, <0 -> 0).
   A 1536-d float vector (6144 bytes) becomes 192 bytes, and similarity becomes
   Hamming distance — a popcount on XOR, which is absurdly fast. Recall drops
   somewhat, so binary is used as a wide CHEAP RECALL NET: Hamming-rank a big
   candidate pool, then re-score the survivors with full-precision cosine. Net:
   most of the speed of binary, most of the accuracy of float.

This module is PURE MATH — it computes signatures and distances. It does not
touch the index schema or change any default. Wiring it into code_index is a
separate, opt-in step precisely because it implies a storage-format migration
that must be validated against live embeddings first. Tested standalone with
synthetic vectors so the math is proven even when the embedding API is down.
"""

from __future__ import annotations

import math
from typing import Sequence


def truncate(vec: Sequence[float], dims: int) -> list[float]:
    """Keep the first `dims` components and re-normalize to unit length. Returns
    the input unchanged if it's already <= dims. Re-normalization preserves the
    unit-norm invariant the cosine identity depends on."""
    if dims <= 0 or len(vec) <= dims:
        return _normalize(list(vec))
    return _normalize(list(vec[:dims]))


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0:
        return vec
    return [x / norm for x in vec]


def binary_signature(vec: Sequence[float]) -> bytes:
    """Pack a vector into a 1-bit-per-dimension signature (sign bits). MSB-first
    within each byte; trailing bits zero-padded if len(vec) % 8 != 0."""
    out = bytearray((len(vec) + 7) // 8)
    for i, x in enumerate(vec):
        if x >= 0.0:
            out[i >> 3] |= 1 << (7 - (i & 7))
    return bytes(out)


# Precomputed popcount table for fast Hamming distance without per-call bin().
_POPCOUNT = bytes(bin(i).count("1") for i in range(256))


def hamming(a: bytes, b: bytes) -> int:
    """Bit-difference count between two equal-length signatures. Lower = more
    similar. This is the cheap recall-net distance."""
    if len(a) != len(b):
        raise ValueError("signature length mismatch")
    total = 0
    for x, y in zip(a, b):
        total += _POPCOUNT[x ^ y]
    return total


def hamming_to_cosine_estimate(dist: int, nbits: int) -> float:
    """Rough cosine estimate from Hamming distance for unit vectors. Under the
    sign-random-projection view, P(bit differs) ~= angle/pi, so
    cos ~= cos(pi * dist / nbits). Approximate — only for coarse ranking of the
    binary pool, never for the final gate (that uses true float cosine)."""
    if nbits <= 0:
        return 0.0
    return math.cos(math.pi * dist / nbits)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Exact cosine for the re-scoring stage. Assumes neither is zero-length."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def two_stage_search(query: Sequence[float], corpus: list[dict], *,
                     pool: int, top_k: int) -> list[dict]:
    """Reference two-stage retrieval over an in-memory corpus, the pattern the
    index would use at scale:
      stage 1 — rank ALL items by Hamming distance on binary sigs (fast), keep
                the top `pool`.
      stage 2 — re-rank that pool by exact float cosine, keep `top_k`.

    Each corpus item must carry 'sig' (bytes) and 'vec' (list[float]). Returns
    the items with an added 'cos' score. This proves the recall-net design; the
    SQLite wiring mirrors it."""
    q_sig = binary_signature(query)
    stage1 = sorted(corpus, key=lambda it: hamming(q_sig, it["sig"]))[:pool]
    for it in stage1:
        it["cos"] = cosine(query, it["vec"])
    stage2 = sorted(stage1, key=lambda it: it["cos"], reverse=True)[:top_k]
    return stage2
