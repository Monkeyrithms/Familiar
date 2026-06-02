"""
Per-tool usage tracking — calls, token estimates (in/out), and error counts.

Persisted to data/tool_stats.json so the Settings/Tools page can show
cumulative usage across sessions. Cheap char/4 token estimates (consistent
with the rest of the codebase).
"""

import json
import threading
from pathlib import Path

_AGENT_ROOT = Path(__file__).parent.parent
_STATS_PATH = _AGENT_ROOT / "data" / "tool_stats.json"

_CHARS_PER_TOKEN = 4

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    """Load the on-disk stats blob. Returns {} on first run or corruption."""
    global _cache
    if _cache is not None:
        return _cache
    try:
        if _STATS_PATH.exists():
            _cache = json.loads(_STATS_PATH.read_text(encoding="utf-8"))
            if not isinstance(_cache, dict):
                _cache = {}
        else:
            _cache = {}
    except Exception:
        _cache = {}
    return _cache


def _save():
    """Persist the in-memory stats to disk."""
    if _cache is None:
        return
    try:
        _STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATS_PATH.write_text(
            json.dumps(_cache, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def record(name: str, args_chars: int, result_chars: int, is_error: bool):
    """Record one tool call. Cheap; hot-path safe."""
    if not name:
        return
    with _lock:
        stats = _load()
        entry = stats.setdefault(name, {
            "calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "errors": 0,
        })
        entry["calls"] += 1
        entry["tokens_in"] += max(0, args_chars) // _CHARS_PER_TOKEN
        entry["tokens_out"] += max(0, result_chars) // _CHARS_PER_TOKEN
        if is_error:
            entry["errors"] += 1
        _save()


def get_all() -> dict:
    """Return a defensive copy of all per-tool stats."""
    with _lock:
        stats = _load()
        return {k: dict(v) for k, v in stats.items()}


def reset():
    """Wipe all stats. Used by the Settings 'Reset' button."""
    global _cache
    with _lock:
        _cache = {}
        try:
            if _STATS_PATH.exists():
                _STATS_PATH.unlink()
        except Exception:
            pass


def totals() -> dict:
    """Aggregate totals across all tools."""
    with _lock:
        stats = _load()
        out = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "errors": 0}
        for v in stats.values():
            out["calls"] += v.get("calls", 0)
            out["tokens_in"] += v.get("tokens_in", 0)
            out["tokens_out"] += v.get("tokens_out", 0)
            out["errors"] += v.get("errors", 0)
        return out
