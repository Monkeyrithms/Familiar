"""
File viewer typing sounds.

Plays rapid terminal clips while the user types in the built-in file viewer.
Edit sounds are handled separately by agent file-write tools (see core.sounds).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from core.sounds import play_ui_random
from core.workspace_paths import AGENT_ROOT

TYPING_SOUNDS = [f"terminal{i}.mp3" for i in range(1, 7)]

_TYPING_MIN_INTERVAL_S = 0.07

_CONFIG_PATH = AGENT_ROOT / "config.json"

_typing_lock = threading.Lock()
_typing_last_play = 0.0
_viewer_typing_armed = False

_settings_cache: dict | None = None
_settings_mtime: float | None = None


def invalidate_settings_cache() -> None:
    """Call after saving settings so typing toggles reload."""
    global _settings_cache, _settings_mtime
    _settings_cache = None
    _settings_mtime = None


def _load_settings() -> dict:
    global _settings_cache, _settings_mtime
    try:
        st = _CONFIG_PATH.stat()
        mtime = st.st_mtime
        if _settings_cache is not None and _settings_mtime == mtime:
            return _settings_cache
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        _settings_cache = {
            "ui_sounds": bool(cfg.get("ui_sounds", True)),
            "viewer_typing_sounds": bool(cfg.get("viewer_typing_sounds", True)),
        }
        _settings_mtime = mtime
        return _settings_cache
    except Exception:
        return {
            "ui_sounds": True,
            "viewer_typing_sounds": True,
        }


def mark_viewer_ready():
    """Call once the file viewer has finished its startup tab restore."""
    global _viewer_typing_armed
    _viewer_typing_armed = True


def note_viewer_keystroke(path: str):
    """Play a rate-limited typing sound while editing in the file viewer."""
    global _typing_last_play
    if not path or not _viewer_typing_armed:
        return
    try:
        from PyQt6.QtWidgets import QApplication
        if QApplication.activeModalWidget() is not None:
            return
    except Exception:
        pass
    settings = _load_settings()
    if not settings["ui_sounds"] or not settings["viewer_typing_sounds"]:
        return

    now = time.monotonic()
    play_now = False
    with _typing_lock:
        if now - _typing_last_play >= _TYPING_MIN_INTERVAL_S:
            _typing_last_play = now
            play_now = True
    if play_now:
        play_ui_random(TYPING_SOUNDS)
