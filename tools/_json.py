"""
Fast JSON serialization helper.

Wraps orjson (Rust-backed, ~5-10x faster than stdlib `json` on large payloads)
with a graceful fall back to stdlib when it isn't installed. Used by the tools
whose results can be large (file_search, diff). orjson always emits UTF-8, which
matches `ensure_ascii=False`.
"""

import json as _json

try:
    import orjson as _orjson
    _HAVE_ORJSON = True
except ImportError:  # pragma: no cover - depends on environment
    _orjson = None
    _HAVE_ORJSON = False


def dumps(obj, *, default=None) -> str:
    """Serialize *obj* to a JSON string (UTF-8, non-ASCII preserved)."""
    if _HAVE_ORJSON:
        return _orjson.dumps(obj, default=default).decode("utf-8")
    return _json.dumps(obj, ensure_ascii=False, default=default)
