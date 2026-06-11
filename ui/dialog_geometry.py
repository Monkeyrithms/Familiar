"""Persist size/position of frameless glass dialogs (Help, Settings, …)."""

from __future__ import annotations

import json
from pathlib import Path

from PyQt6.QtCore import QRect

_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "dialog_state.json"


def _load_all() -> dict:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_all(data: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def restore_geometry(
    key: str,
    default_width: int,
    default_height: int,
    parent=None,
) -> QRect | None:
    """Return a QRect to apply, or None to use GlassDialog's parent-centered default."""
    entry = _load_all().get(key)
    if not isinstance(entry, dict):
        return None
    try:
        w = int(entry.get("w", default_width))
        h = int(entry.get("h", default_height))
        x = int(entry.get("x", 0))
        y = int(entry.get("y", 0))
    except (TypeError, ValueError):
        return None
    if w < 300 or h < 200:
        return None
    rect = QRect(x, y, w, h)
    if parent is not None:
        try:
            pg = parent.window().geometry()
            # Require at least part of the dialog on a plausible screen area.
            if not rect.intersects(pg.translated(-200, -200).adjusted(0, 0, 400, 400)):
                return None
        except Exception:
            pass
    return rect


def save_geometry(key: str, widget) -> None:
    try:
        geo = widget.geometry()
        data = _load_all()
        data[key] = {
            "x": geo.x(),
            "y": geo.y(),
            "w": geo.width(),
            "h": geo.height(),
        }
        _save_all(data)
    except Exception:
        pass


def apply_saved_geometry(
    dialog,
    key: str,
    default_width: int,
    default_height: int,
) -> None:
    rect = restore_geometry(key, default_width, default_height, parent=dialog.parent())
    if rect is not None:
        dialog.setGeometry(rect)
