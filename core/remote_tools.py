"""
Remote workspace TOOLS — let a peer mirror the host's Notes, Calendar, and
Browser state over the authenticated channel, alongside the file viewer.

These read/write the host's real stores (the same ones the local panels use):
  * Notes    — data/workspace_notes.json (global scratchpad)
  * Calendar — scheduled-task events for a month (read-only)
  * Browser  — the conversation's current browser URL (from viewer_state.json)

Stdlib-only where possible; the calendar event computation lives in a UI module
(imported lazily, guarded) since the host is always the full app.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
_NOTES_PATH = APP_DIR / "data" / "workspace_notes.json"


# ── Notes (global scratchpad, shared with tools/notes.py + the Notes panel) ──

def notes_list() -> list[dict]:
    try:
        from tools.notes import _load_notes
        return _load_notes()
    except Exception:
        try:
            raw = json.loads(_NOTES_PATH.read_text(encoding="utf-8"))
            return [n for n in raw.get("notes", []) if isinstance(n, dict)]
        except Exception:
            return []


def _notes_save(notes: list[dict]) -> None:
    try:
        from tools.notes import _save_notes
        _save_notes(notes)
    except Exception:
        _NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _NOTES_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"notes": notes, "updated_at": time.time()},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_NOTES_PATH)


def notes_save_one(note_id: str, content: str) -> dict:
    """Create (blank note_id) or update a note. Returns the saved note."""
    notes = notes_list()
    now = time.time()
    if note_id:
        for n in notes:
            if str(n.get("id") or "") == str(note_id):
                n["content"] = content
                n["updated_at"] = now
                _notes_save(notes)
                return n
    note = {"id": uuid.uuid4().hex[:12], "content": content,
            "created_at": now, "updated_at": now}
    notes.append(note)
    _notes_save(notes)
    return note


def notes_delete(note_id: str) -> bool:
    notes = notes_list()
    kept = [n for n in notes if str(n.get("id") or "") != str(note_id)]
    if len(kept) == len(notes):
        return False
    _notes_save(kept)
    return True


# ── Calendar (read-only: scheduled-task events for a month) ──

def calendar_events(year: int, month: int) -> dict:
    """{ 'YYYY-MM-DD': [{id, title, time, source}, ...] } for the month, from the
    host's scheduled tasks. Read-only."""
    try:
        from ui.workspace_notes_calendar import _task_events_for_month
        return _task_events_for_month(int(year), int(month)) or {}
    except Exception:
        return {}


# ── Browser (the conversation's current URL) ──

def browser_url(conv_id: str) -> str:
    """The current browser URL for a conversation, from its persisted viewer
    state. '' when none or when the conversation is private."""
    try:
        from core.conversations import is_conversation_private
        if is_conversation_private(conv_id):
            return ""
    except Exception:
        pass
    try:
        vs = json.loads((APP_DIR / "data" / "viewer_state.json")
                        .read_text(encoding="utf-8"))
        br = (vs.get(conv_id) or {}).get("browser") or {}
        urls = br.get("urls") or []
        active = br.get("active", 0)
        if 0 <= active < len(urls):
            return urls[active] or ""
        return urls[0] if urls else ""
    except Exception:
        return ""
