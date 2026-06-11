"""
Workspace notes tool — read/write the user's Notes tab (right workspace).

The Notes panel persists to data/workspace_notes.json. Until now the agent
had no way to see or update those notes, even though they're often exactly
the scratchpad the user is referring to ("add that to my notes", "what did
I write down about X?"). Storage format matches ui/workspace_notes_calendar.py
exactly: {"notes": [{"id", "content", "created_at", "updated_at"}, ...]}.

Mutations emit a "notes.changed" event so an open Notes tab refreshes live.
"""

import json
import time
import uuid
from pathlib import Path

from tools.registry import registry

_ROOT = Path(__file__).resolve().parent.parent
_NOTES_PATH = _ROOT / "data" / "workspace_notes.json"

_PREVIEW_LEN = 60


def _load_notes() -> list[dict]:
    try:
        raw = json.loads(_NOTES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    notes = raw.get("notes", [])
    return [dict(n) for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []


def _save_notes(notes: list[dict]) -> None:
    # Same atomic write the Notes panel uses (tmp + replace).
    _NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _NOTES_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"notes": notes, "updated_at": time.time()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(_NOTES_PATH)
    try:
        from core.event_bus import bus
        bus.emit("notes.changed")
    except Exception:
        pass


def _preview(content: str) -> str:
    first = next((ln for ln in (content or "").splitlines() if ln.strip()), "")
    return first[:_PREVIEW_LEN] if first else "(empty)"


def _find(notes: list[dict], note_id: str) -> dict | None:
    nid = str(note_id or "")
    for n in notes:
        if str(n.get("id") or "") == nid:
            return n
    return None


def _summary(n: dict) -> dict:
    return {
        "id": n.get("id", ""),
        "preview": _preview(n.get("content", "")),
        "chars": len(n.get("content", "") or ""),
        "updated_at": n.get("updated_at", 0),
    }


def notes(action: str, note_id: str = "", content: str = "") -> str:
    """Read/write the user's Notes tab (the right-workspace scratchpad)."""
    try:
        items = _load_notes()
        items.sort(key=lambda n: float(n.get("updated_at") or 0), reverse=True)

        if action == "list":
            return json.dumps({
                "count": len(items),
                "notes": [_summary(n) for n in items],
            }, ensure_ascii=False)

        if action == "read":
            if not items:
                return json.dumps({"error": "No notes exist yet."})
            if note_id in ("", "latest"):
                n = items[0]
            else:
                n = _find(items, note_id)
                if n is None:
                    return json.dumps({"error": f"No note with id '{note_id}'. "
                                                f"Use action='list' to see ids."})
            return json.dumps({
                "id": n.get("id", ""),
                "content": n.get("content", ""),
                "created_at": n.get("created_at", 0),
                "updated_at": n.get("updated_at", 0),
            }, ensure_ascii=False)

        if action == "write":
            if not (content or "").strip():
                return json.dumps({"error": "content required for write"})
            now = time.time()
            n = {"id": uuid.uuid4().hex[:12], "content": content,
                 "created_at": now, "updated_at": now}
            items.append(n)
            _save_notes(items)
            return json.dumps({"status": "created", **_summary(n)},
                              ensure_ascii=False)

        if action in ("append", "update"):
            if not (content or "").strip():
                return json.dumps({"error": f"content required for {action}"})
            if not items:
                return json.dumps({"error": "No notes exist yet — use action='write'."})
            n = items[0] if note_id in ("", "latest") else _find(items, note_id)
            if n is None:
                return json.dumps({"error": f"No note with id '{note_id}'. "
                                            f"Use action='list' to see ids."})
            if action == "append":
                base = (n.get("content", "") or "").rstrip()
                n["content"] = (base + "\n" + content) if base else content
            else:
                n["content"] = content
            n["updated_at"] = time.time()
            _save_notes(items)
            return json.dumps({"status": action, **_summary(n)},
                              ensure_ascii=False)

        if action == "delete":
            if not note_id:
                return json.dumps({"error": "note_id required for delete"})
            n = _find(items, note_id)
            if n is None:
                return json.dumps({"error": f"No note with id '{note_id}'."})
            items.remove(n)
            _save_notes(items)
            return json.dumps({"status": "deleted", "id": note_id})

        return json.dumps({"error": "action must be one of: "
                                    "list, read, write, append, update, delete"})
    except Exception as e:
        return json.dumps({"error": str(e)})


registry.register(
    name="notes",
    description=(
        "Read/write the user's Notes tab (their personal scratchpad in the right "
        "workspace — NOT agent memory). Use when the user says things like 'add "
        "that to my notes' or 'what's in my notes?'. If the user is actively "
        "typing in the same note, their editor wins on conflict."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "read", "write", "append", "update", "delete"],
                "description": (
                    "list=ids+previews | read=full note | write=new note | "
                    "append=add lines to a note | update=replace a note's text | "
                    "delete=remove a note."
                ),
            },
            "note_id": {
                "type": "string",
                "description": "Target note id from list. Empty or 'latest' = "
                               "most recently updated note.",
            },
            "content": {
                "type": "string",
                "description": "Text for write/append/update.",
            },
        },
        "required": ["action"],
    },
    execute=notes,
)
