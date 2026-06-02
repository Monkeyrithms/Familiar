"""
Per-model API quirks — kwargs that a given model rejects.

Newer Anthropic / OpenAI reasoning models sometimes drop support for
parameters older models required (e.g. `temperature` on reasoning models).
Rather than hard-code a list that rots, this module:

1. Remembers quirks observed at runtime in data/model_quirks.json
2. Exposes strip_unsupported(kwargs, model) to pre-filter before a call
3. Exposes mark_unsupported(model, param) after an API rejection, so the
   next call to that model skips the offending kwarg.

Provider is included in the key because the same model name can appear
behind multiple gateways (e.g. openrouter vs native).
"""

import json
import re
import threading
from pathlib import Path

_PATH = Path(__file__).parent.parent / "data" / "model_quirks.json"
_LOCK = threading.Lock()
_CACHE: dict[str, list[str]] | None = None


def _key(provider: str, model: str) -> str:
    return f"{(provider or '').lower()}::{(model or '').lower()}"


def _load() -> dict[str, list[str]]:
    global _CACHE
    if _CACHE is not None:
        return _CACHE
    try:
        if _PATH.exists():
            _CACHE = json.loads(_PATH.read_text(encoding="utf-8"))
            if not isinstance(_CACHE, dict):
                _CACHE = {}
        else:
            _CACHE = {}
    except Exception:
        _CACHE = {}
    return _CACHE


def _save() -> None:
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(
            json.dumps(_CACHE, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def strip_unsupported(kwargs: dict, provider: str, model: str) -> dict:
    """Return kwargs with any known-unsupported params removed for this model."""
    with _LOCK:
        cache = _load()
        bad = cache.get(_key(provider, model)) or []
    if not bad:
        return kwargs
    out = dict(kwargs)
    for p in bad:
        out.pop(p, None)
    return out


def mark_unsupported(provider: str, model: str, param: str) -> None:
    """Record that `param` is not supported by `model` on `provider`."""
    if not param:
        return
    with _LOCK:
        cache = _load()
        k = _key(provider, model)
        entry = cache.get(k) or []
        if param not in entry:
            entry.append(param)
            cache[k] = entry
            _save()


# Error-message patterns that tell us which param was rejected.
# Each entry: (regex with a named group `param`, or fixed param name).
_REJECTION_PATTERNS = [
    # Anthropic: "`temperature` is deprecated for this model."
    re.compile(r"`(?P<param>[a-zA-Z_][a-zA-Z0-9_]*)`\s+is\s+deprecated", re.I),
    # OpenAI o-series: "Unsupported parameter: 'temperature'..."
    re.compile(r"[Uu]nsupported\s+parameter:\s*['\"](?P<param>[^'\"]+)['\"]"),
    # Generic "X is not supported" / "X is not allowed"
    re.compile(r"`(?P<param>[a-zA-Z_][a-zA-Z0-9_]*)`\s+is\s+not\s+(supported|allowed)", re.I),
    re.compile(r"parameter\s+['\"]?(?P<param>[a-zA-Z_][a-zA-Z0-9_]*)['\"]?\s+is\s+not\s+(supported|allowed)", re.I),
]


def detect_rejected_param(error_message: str) -> str | None:
    """Parse an API error message and return the name of the rejected param, if any."""
    if not error_message:
        return None
    for rx in _REJECTION_PATTERNS:
        m = rx.search(error_message)
        if m:
            return m.group("param")
    return None
