"""
Durable record of an in-flight ask_user_question.

When the agent raises a question board and blocks on the user, the board lives
only in memory — a crash or a deliberate shutdown while it's open would lose the
question entirely (the tool call never returns, so nothing is ever persisted to
the conversation). To make the question survive a restart, the UI writes the
pending spec here the moment the board appears and clears it the moment the
board resolves (answered, cancelled, or aborted).

One question at a time: the agent blocks on a single board, so a single-slot
JSON file is all we need. Best-effort and crash-tolerant — a malformed or
missing file simply means "no pending question".
"""

from __future__ import annotations

import json
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "data" / "pending_question.json"


def save_pending_question(conv_id: str, questions: list) -> None:
    """Record the open question board so it can be restored after a restart."""
    try:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        payload = {"conv_id": conv_id or "", "questions": questions}
        tmp = _PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_PATH)  # atomic swap so a crash mid-write can't corrupt it
    except Exception:
        pass


def load_pending_question() -> dict | None:
    """Return {'conv_id': str, 'questions': list} if a board was open at last
    shutdown, else None."""
    try:
        if not _PATH.is_file():
            return None
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        if (isinstance(data, dict)
                and isinstance(data.get("questions"), list)
                and data["questions"]):
            return data
    except Exception:
        pass
    return None


def clear_pending_question() -> None:
    """Forget any recorded question — the board has resolved."""
    try:
        _PATH.unlink(missing_ok=True)
    except Exception:
        pass
