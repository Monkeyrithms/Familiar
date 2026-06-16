"""Cross-encoder reranking — precision stage on top of RRF fusion.

The retrieval pipeline's bi-encoder (vector search) embeds the query and each
chunk SEPARATELY, so cosine similarity only ever sees "are these two vectors
near each other," never "does this chunk actually answer this query." A
cross-encoder fixes exactly that: it takes (query, chunk) as ONE concatenated
input and scores them with full cross-attention — it reads the question and the
code together. That is a large precision win, and because it only runs on the
~50 candidates the cheap RRF stage already pulled, it costs tens of ms, not a
full-corpus scan.

Backends, resolved lazily in priority order (first that imports wins):

  1. sentence-transformers CrossEncoder  — the real thing, if installed.
  2. lexical reranker                     — zero-dependency fallback that ALWAYS
                                            works. Scores on full-token overlap,
                                            exact-identifier hits, phrase
                                            proximity, and name/path matches —
                                            signals RRF throws away. Not as good
                                            as a neural cross-encoder, but a
                                            genuine improvement over raw fusion,
                                            and it needs nothing installed.

So reranking is ALWAYS on the table: neural when the model is present, lexical
otherwise. `pip install sentence-transformers` upgrades the backend live with
no other change. Never raises into the search path — any failure returns the
candidates untouched.
"""

from __future__ import annotations

import re
import threading

# Default neural model — small (~22M params), CPU-friendly, strong on
# (query, passage) relevance. Only used if sentence-transformers is importable.
DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_BACKEND = None          # resolved CrossEncoder instance or the string "lexical"
_BACKEND_LOCK = threading.Lock()
_BACKEND_NAME = None     # human-readable, for logging/status


def _resolve_backend(model_name: str):
    """Lazily pick and cache a backend. Thread-safe, runs once. Returns either a
    loaded CrossEncoder, or the sentinel string 'lexical'."""
    global _BACKEND, _BACKEND_NAME
    if _BACKEND is not None:
        return _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is not None:
            return _BACKEND
        try:
            from sentence_transformers import CrossEncoder
            _BACKEND = CrossEncoder(model_name)
            _BACKEND_NAME = f"cross-encoder:{model_name}"
        except Exception:
            _BACKEND = "lexical"
            _BACKEND_NAME = "lexical"
        return _BACKEND


def backend_name() -> str:
    """Name of the active backend, resolving it if needed. For status/logging."""
    if _BACKEND is None:
        _resolve_backend(DEFAULT_MODEL)
    return _BACKEND_NAME or "lexical"


# ── tokenization shared by the lexical backend ──────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
# Split snake_case / camelCase / PascalCase into word pieces so a query word
# "workspace" matches an identifier "set_workspace" or "workspacePath".
_CAMEL_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_STOP = frozenset({
    "a", "an", "the", "of", "for", "to", "in", "on", "is", "are", "how",
    "do", "i", "what", "where", "when", "and", "or", "does", "the", "this",
    "that", "with", "it", "be", "as", "by", "at", "from",
})


def _subwords(token: str) -> list[str]:
    return [p.lower() for p in _CAMEL_RE.findall(token) if p]


def _tokenize(text: str, *, expand: bool = True) -> list[str]:
    out: list[str] = []
    for raw in _TOKEN_RE.findall(text):
        low = raw.lower()
        out.append(low)
        if expand and (("_" in raw) or any(c.isupper() for c in raw[1:])):
            out.extend(_subwords(raw))
    return out


def _lexical_score(query: str, cand: dict) -> float:
    """Token-overlap relevance with structural boosts. Bounded ~0..1+ (the cap
    isn't strict — it's only ever used to *order* candidates, never gated).

    Signals RRF discards but a reranker should reward:
      - fraction of query terms present in the chunk
      - exact identifier hit (a query word IS the chunk's name)        [+big]
      - query word appears in the file path                            [+small]
      - adjacent query bigram appears in the chunk (phrase proximity)  [+med]
    """
    q_tokens = [t for t in _tokenize(query) if t not in _STOP and len(t) > 1]
    if not q_tokens:
        return 0.0
    q_set = set(q_tokens)

    text = cand.get("text", "") or ""
    name = (cand.get("name") or "")
    path = (cand.get("file") or "")

    chunk_tokens = set(_tokenize(text))
    name_tokens = set(_tokenize(name))
    path_tokens = set(_tokenize(path))

    overlap = len(q_set & chunk_tokens) / len(q_set)
    score = overlap

    # Exact identifier hit: a query term equals (a subword of) the symbol name.
    if q_set & name_tokens:
        score += 0.5
    # Path signal: query term in the file path (module/dir names matter in code).
    if q_set & path_tokens:
        score += 0.15

    # Phrase proximity: reward adjacent query bigrams that survive in the text.
    low_text = text.lower()
    for a, b in zip(q_tokens, q_tokens[1:]):
        if f"{a} {b}" in low_text or f"{a}_{b}" in low_text:
            score += 0.1

    return score


def rerank(query: str, candidates: list[dict], top_k: int,
           *, model_name: str = DEFAULT_MODEL, log=None) -> list[dict]:
    """Reorder *candidates* by (query, chunk) relevance and return the best
    `top_k`. Each candidate is a fusion dict carrying at least 'text'; the
    chosen backend scores it and we attach `rerank_score` + `rerank_backend`
    for transparency. NEVER raises into the caller — on any failure the original
    order is preserved (trimmed to top_k).

    Candidates' prior `score`/`vec_score` are left intact so a caller can still
    gate on absolute cosine (the reranker reorders; it doesn't manufacture an
    absolute-relevance number the way cosine does)."""
    if not candidates:
        return candidates
    if len(candidates) <= 1:
        return candidates[:top_k]

    backend = _resolve_backend(model_name)
    try:
        if backend == "lexical":
            scored = [(c, _lexical_score(query, c)) for c in candidates]
            tag = "lexical"
        else:
            pairs = [[query, c.get("text", "") or ""] for c in candidates]
            raw = backend.predict(pairs)
            scored = [(c, float(s)) for c, s in zip(candidates, raw)]
            tag = "cross-encoder"
    except Exception as e:
        if log:
            log(f"[rerank] backend error ({backend_name()}): {e} — passthrough")
        return candidates[:top_k]

    scored.sort(key=lambda cs: cs[1], reverse=True)
    out = []
    for cand, s in scored[:top_k]:
        cand = dict(cand)
        cand["rerank_score"] = round(s, 4)
        cand["rerank_backend"] = tag
        out.append(cand)
    if log:
        top = scored[0][1] if scored else 0.0
        log(f"[rerank] {tag} scored {len(candidates)} -> top_k={top_k} "
            f"(best={top:.3f})")
    return out
