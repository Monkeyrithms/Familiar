"""
Tool-call metadata store — a lightweight, on-disk record of each tool call's
full args + result, keyed by the call_id.

This is deliberately SEPARATE from the LLM context: the model never sees this,
so we can keep richer detail (full question + answer for ask_user_question, full
args, the raw result) than we'd ever cram into the conversation. The UI reads it
when the user clicks a tool chip.

Storage: one JSON file per UTC day under data/tool_call_meta/, holding a dict of
{call_id: record}. Cheap append-style upserts; pruned to RETENTION_DAYS.
"""

import json
import threading
import time
from pathlib import Path

META_DIR = Path(__file__).parent.parent / "data" / "tool_call_meta"
RETENTION_DAYS = 14  # keep clickable metadata for the last two weeks

# Cap stored strings so a giant tool result can't bloat the store. The UI shows
# a "(truncated)" note when it hits this.
_MAX_FIELD_CHARS = 20000

_lock = threading.Lock()
# In-process index: call_id -> record, for instant lookups without disk reads.
_index: dict[str, dict] = {}
_loaded = False


def _day_file(ts: float) -> Path:
    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    return META_DIR / f"{day}.json"


def _clip(s) -> str:
    if not isinstance(s, str):
        try:
            s = json.dumps(s, ensure_ascii=False)
        except Exception:
            s = str(s)
    if len(s) > _MAX_FIELD_CHARS:
        return s[:_MAX_FIELD_CHARS] + f"\n…(truncated, {len(s)} chars total)"
    return s


def _ensure_loaded():
    """Lazily load all retained day-files into the in-memory index once."""
    global _loaded
    if _loaded:
        return
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - RETENTION_DAYS * 86400
        for f in META_DIR.glob("*.json"):
            try:
                # Prune day-files older than retention by filename date.
                day_ts = time.mktime(time.strptime(f.stem, "%Y-%m-%d"))
            except Exception:
                day_ts = None
            if day_ts is not None and day_ts < cutoff:
                try:
                    f.unlink()
                except Exception:
                    pass
                continue
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    _index.update(data)
            except Exception:
                pass
    except Exception:
        pass
    _loaded = True


def record(call_id: str, name: str, args: dict, result: str,
           extra: dict | None = None, ts: float | None = None):
    """Store one tool call's metadata. Safe to call from any thread; never raises."""
    if not call_id:
        return
    ts = ts if ts is not None else time.time()
    rec = {
        "call_id": call_id,
        "name": name,
        "args": args if isinstance(args, dict) else {},
        "result": _clip(result),
        "ts": ts,
    }
    if extra:
        # Caller-supplied display fields (e.g. ask_user_question Q&A).
        rec["extra"] = {k: _clip(v) for k, v in extra.items()}
    try:
        with _lock:
            _ensure_loaded()
            _index[call_id] = rec
            f = _day_file(ts)
            f.parent.mkdir(parents=True, exist_ok=True)
            # Merge into the day-file (small; one file per day).
            existing = {}
            if f.exists():
                try:
                    existing = json.loads(f.read_text(encoding="utf-8"))
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    existing = {}
            existing[call_id] = rec
            f.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get(call_id: str) -> dict | None:
    """Look up a stored tool call by id. Returns the record or None."""
    if not call_id:
        return None
    with _lock:
        _ensure_loaded()
        return _index.get(call_id)


def get_latest_by_name(name: str) -> dict | None:
    """Most recent stored call for a tool name. Used by clickable chips, which
    only know the tool name (call_ids aren't threaded through the live UI).
    With repeated same-tool calls in a turn this returns the latest one."""
    if not name:
        return None
    with _lock:
        _ensure_loaded()
        best = None
        for rec in _index.values():
            if rec.get("name") == name:
                if best is None or rec.get("ts", 0) > best.get("ts", 0):
                    best = rec
        return best
