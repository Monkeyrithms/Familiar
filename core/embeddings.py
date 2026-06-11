"""
Embedding helper — generates text embeddings for vector search.

Reads the embedding model from config.json. Tries OpenRouter first,
then OpenAI. Falls back gracefully — hybrid search degrades to FTS5-only
if no embedding provider is available or the model field is blank.
"""

import json
import struct
from collections import OrderedDict
from pathlib import Path
from typing import Optional

_DEFAULT_MODEL = "openai/text-embedding-3-small"
_DEFAULT_DIMS = 1536  # text-embedding-3-small default

# Embeddings are deterministic for a given (model, input), so an identical
# string never needs a second network round-trip. A repeated search query,
# memory recall over stable text, or a re-run vector_search would otherwise
# pay full latency + token cost to recompute a vector we already have. Bounded
# LRU keyed on (model, text) — capped so it can't grow unbounded in a long
# session.
_EMBED_CACHE: "OrderedDict[tuple[str, str], list[float]]" = OrderedDict()
_EMBED_CACHE_MAX = 2048


def _cache_get(model: str, text: str):
    key = (model, text)
    vec = _EMBED_CACHE.get(key)
    if vec is not None:
        _EMBED_CACHE.move_to_end(key)  # mark as most-recently used
    return vec


def _cache_put(model: str, text: str, vec: list[float]) -> None:
    if vec is None:
        return
    key = (model, text)
    _EMBED_CACHE[key] = vec
    _EMBED_CACHE.move_to_end(key)
    while len(_EMBED_CACHE) > _EMBED_CACHE_MAX:
        _EMBED_CACHE.popitem(last=False)  # evict least-recently used

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
# Keys live under data/ (user-state / gitignore boundary). Match providers.py.
KEYS_PATH = Path(__file__).parent.parent / "data" / "keys.json"


def _load_embed_dims() -> int:
    """Read embedding dimension from config. Override via `embedding_dims` key
    when switching to a larger/different model (e.g. text-embedding-3-large = 3072)."""
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            d = int(cfg.get("embedding_dims", 0) or 0)
            if d > 0:
                return d
    except Exception:
        pass
    return _DEFAULT_DIMS


# Module-level constant for backward compatibility (database.py, memory_agent use it).
# Computed once at import; if you change `embedding_dims` in config.json, restart the agent.
EMBED_DIMS = _load_embed_dims()


def _load_embed_config() -> tuple[str, str, str]:
    """Returns (model, api_key, base_url). Any may be empty."""
    model = _DEFAULT_MODEL
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            m = cfg.get("embedding_model", "").strip()
            if m:
                model = m
    except Exception:
        pass

    if not model:
        return ("", "", "")

    # Find an API key — try OpenRouter first, then OpenAI
    api_key, base_url = "", ""
    try:
        if KEYS_PATH.exists():
            keys = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
            or_key = keys.get("openrouter", {}).get("api_key", "")
            oai_key = keys.get("openai", {}).get("api_key", "")
            if or_key:
                api_key = or_key
                base_url = "https://openrouter.ai/api/v1"
            elif oai_key:
                api_key = oai_key
                base_url = ""  # default OpenAI endpoint
    except Exception:
        pass

    return (model, api_key, base_url)


_cached_client = None
_cached_client_key = None  # (model, api_key, base_url) tuple for cache invalidation


def _get_embed_client():
    """Get an OpenAI client configured for embeddings. Cached singleton."""
    global _cached_client, _cached_client_key
    model, api_key, base_url = _load_embed_config()
    if not model or not api_key:
        return None, None
    cache_key = (model, api_key[:10], base_url)
    if _cached_client and _cached_client_key == cache_key:
        return _cached_client, model
    try:
        from openai import OpenAI
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        _cached_client = OpenAI(**kwargs)
        _cached_client_key = cache_key
        return _cached_client, model
    except Exception:
        return None, None


def _embeddings_enabled(purpose: str = "general") -> bool:
    """Two-level kill switch.
        embeddings_enabled               — master (default true). False kills everything.
        conversation_embeddings_enabled  — controls per-turn calls from
                                           database.py (message vectorization, memory
                                           recall, note storage). Default false —
                                           conversational recall worked fine on FTS5
                                           keyword search alone for months while the
                                           model was misconfigured, and embedding
                                           every message is the bulk of the per-turn cost.

    Code index calls (purpose='code') are always allowed unless the master
    is off — they only fire on actual file edits, not every turn."""
    try:
        if CONFIG_PATH.exists():
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not bool(cfg.get("embeddings_enabled", True)):
                return False
            if purpose == "conversation":
                return bool(cfg.get("conversation_embeddings_enabled", False))
    except Exception:
        pass
    return True


def embed_text(text: str, purpose: str = "general") -> Optional[list[float]]:
    """Generate an embedding vector for a text string. Returns None on failure
    or when embeddings are disabled in config for this purpose.

    `purpose` controls the toggle group: 'conversation' for per-turn memory/
    message embeddings (default off), anything else for explicit feature use
    like code search (default on)."""
    if not _embeddings_enabled(purpose):
        return None
    client, model = _get_embed_client()
    if not client:
        return None
    try:
        text = text[:8000]
        cached = _cache_get(model, text)
        if cached is not None:
            return cached
        resp = client.embeddings.create(model=model, input=text)
        vec = resp.data[0].embedding
        _cache_put(model, text, vec)
        return vec
    except Exception as e:
        print(f"[Embeddings] Error: {e}")
        return None


def embed_batch(texts: list[str],
                purpose: str = "general") -> list[Optional[list[float]]]:
    """Batch version of embed_text. See embed_text docstring for `purpose`."""
    if not _embeddings_enabled(purpose):
        return [None] * len(texts)
    client, model = _get_embed_client()
    if not client:
        return [None] * len(texts)
    try:
        capped = [t[:8000] for t in texts]
        result: list = [None] * len(texts)
        # Serve any cache hits locally; only send the misses to the API.
        miss_idx = []
        miss_text = []
        for i, t in enumerate(capped):
            hit = _cache_get(model, t)
            if hit is not None:
                result[i] = hit
            else:
                miss_idx.append(i)
                miss_text.append(t)
        if miss_text:
            resp = client.embeddings.create(model=model, input=miss_text)
            for item in resp.data:
                orig_i = miss_idx[item.index]
                result[orig_i] = item.embedding
                _cache_put(model, miss_text[item.index], item.embedding)
        return result
    except Exception as e:
        print(f"[Embeddings] Batch error: {e}")
        return [None] * len(texts)


def vec_to_bytes(vec: list[float]) -> bytes:
    """Pack a float vector into bytes for sqlite-vec storage."""
    return struct.pack(f"{len(vec)}f", *vec)


def bytes_to_vec(data: bytes) -> list[float]:
    """Unpack bytes from sqlite-vec back to float list."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def load_sqlite_vec(conn):
    """Load the sqlite-vec extension into a connection. Returns True on success."""
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        return True
    except Exception:
        return False
