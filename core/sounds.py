"""
Sound engine — instant, non-blocking MP3/WAV playback via pygame.mixer.

Uses cached pygame.mixer.Sound objects with 8-channel round-robin pooling
for overlapping playback. Same pattern as Zenithrix2/Brikwerx.
"""

import fnmatch
import json
import os
import threading
import time
from pathlib import Path

SOUNDS_DIR = Path(__file__).parent.parent / "sounds"

# Deferred queue — sounds queued by the play_sound tool, drained on response delivery
_deferred_queue: list[str] = []
_queue_lock = threading.Lock()


def queue_deferred(name: str):
    """Queue a sound for playback when the LLM response is delivered."""
    with _queue_lock:
        _deferred_queue.append(name)


def drain_deferred():
    """Play all queued deferred sounds. Called from the UI after response delivery."""
    with _queue_lock:
        queued = list(_deferred_queue)
        _deferred_queue.clear()
    for name in queued:
        play(name)


def list_sounds() -> list[str]:
    """List available sound files in the sounds/ directory."""
    if not SOUNDS_DIR.exists():
        return []
    return sorted(
        p.name for p in SOUNDS_DIR.iterdir()
        if p.suffix.lower() in ('.mp3', '.wav', '.ogg')
    )


# ── pygame.mixer backend ───────────────────────────────────────────

_initialized = False
_sounds: dict[str, object] = {}
_channel_index = 0
_num_channels = 8

EDIT_SOUNDS = ["editFile1.mp3", "editFile2.mp3", "editFile3.mp3"]

# Cached config flags (config.json) — refreshed when mtime changes.
_ui_sounds_cache: bool | None = None
_edit_sounds_cache: bool | None = None
_exempt_patterns_cache: list[str] | None = None
_cfg_mtime: float | None = None

_edit_last_play = 0.0
_EDIT_MIN_INTERVAL_S = 0.15


def invalidate_ui_sounds_cache() -> None:
    """Call after saving settings so sound toggles/patterns reload."""
    global _ui_sounds_cache, _edit_sounds_cache, _exempt_patterns_cache, _cfg_mtime
    _ui_sounds_cache = None
    _edit_sounds_cache = None
    _exempt_patterns_cache = None
    _cfg_mtime = None


def _load_sound_config() -> tuple[bool, bool, list[str]]:
    """Return (ui_sounds, workspace_edit_sounds, sound_exempt_patterns)."""
    global _ui_sounds_cache, _edit_sounds_cache, _exempt_patterns_cache, _cfg_mtime

    cfg_path = SOUNDS_DIR.parent / "config.json"
    try:
        st = cfg_path.stat()
        mtime = st.st_mtime
        if (
            _ui_sounds_cache is not None
            and _edit_sounds_cache is not None
            and _exempt_patterns_cache is not None
            and _cfg_mtime == mtime
        ):
            return _ui_sounds_cache, _edit_sounds_cache, _exempt_patterns_cache
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        patterns = cfg.get("sound_exempt_patterns") or []
        if isinstance(patterns, str):
            patterns = [ln.strip() for ln in patterns.splitlines() if ln.strip()]
        _ui_sounds_cache = bool(cfg.get("ui_sounds", True))
        _edit_sounds_cache = bool(cfg.get("workspace_edit_sounds", True))
        _exempt_patterns_cache = [str(p).strip() for p in patterns if str(p).strip()]
        _cfg_mtime = mtime
        return _ui_sounds_cache, _edit_sounds_cache, _exempt_patterns_cache
    except Exception:
        _ui_sounds_cache = True
        _edit_sounds_cache = True
        _exempt_patterns_cache = []
        _cfg_mtime = None
        return True, True, []


def _ui_sounds_enabled() -> bool:
    """Return whether UI sound effects are allowed (defaults True)."""
    ui_sounds, _, _ = _load_sound_config()
    return ui_sounds


def _norm_path(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _matches_exempt_pattern(path: str, pattern: str) -> bool:
    """Return True if ``pattern`` exempts ``path``."""
    pat = pattern.strip()
    if not pat:
        return False

    p = Path(path)
    parts_lower = [part.lower() for part in p.parts]
    base_lower = p.name.lower()
    norm_lower = _norm_path(path).lower()
    pat_lower = pat.lower()

    folder_rule = pat
    if folder_rule.startswith(("/", "\\")):
        folder_rule = folder_rule[1:]
    if (
        folder_rule.endswith("*")
        and folder_rule.count("*") == 1
        and "/" not in folder_rule
        and "\\" not in folder_rule
    ):
        folder_name = folder_rule[:-1].lower()
        if folder_name and folder_name in parts_lower:
            return True

    if fnmatch.fnmatch(base_lower, pat_lower):
        return True
    if fnmatch.fnmatch(norm_lower, pat_lower):
        return True
    if pat_lower == base_lower:
        return True
    return False


def _path_exempt(path: str) -> bool:
    _, _, patterns = _load_sound_config()
    for pattern in patterns:
        if _matches_exempt_pattern(path, pattern):
            return True
    return False


def play_edit_sound(path: str = "") -> bool:
    """Play a random edit sound after an agent file-write tool succeeds."""
    global _edit_last_play

    ui_sounds, edit_sounds, _ = _load_sound_config()
    if not ui_sounds or not edit_sounds:
        return False
    if path and _path_exempt(path):
        return False

    now = time.monotonic()
    if now - _edit_last_play < _EDIT_MIN_INTERVAL_S:
        return False
    _edit_last_play = now
    return play_ui_random(EDIT_SOUNDS)


def _ensure_init():
    global _initialized
    if _initialized:
        return True
    try:
        import pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            pygame.mixer.set_num_channels(_num_channels)
        _initialized = True
        return True
    except Exception:
        return False


def preload_all():
    """Pre-load all sound files into cache. Call at app startup."""
    if not _ensure_init() or not SOUNDS_DIR.exists():
        return
    import pygame
    for p in SOUNDS_DIR.iterdir():
        if p.suffix.lower() in ('.mp3', '.wav', '.ogg'):
            key = p.name
            if key not in _sounds:
                try:
                    _sounds[key] = pygame.mixer.Sound(str(p))
                except Exception:
                    pass


def play_ui(name: str, volume: float = 1.0) -> bool:
    """Play a UI sound effect, respecting the 'ui_sounds' setting.
    For UI-generated sounds only (selections, messages, snapshots).
    LLM-called sounds bypass this and use play() directly.
    ``volume`` is 0.0–1.0 (e.g. 0.5 for a softer launch chime)."""
    if not _ui_sounds_enabled():
        return False
    return play(name, volume=volume)


def play_ui_random(names) -> bool:
    """Play a random sound from a list of candidate filenames (UI-gated).
    Silently no-ops if none of the candidates exist in sounds/."""
    import random
    if not names:
        return False
    existing = [n for n in names if (SOUNDS_DIR / n).exists()]
    if not existing:
        return False
    return play_ui(random.choice(existing))


def play(name: str, volume: float = 1.0) -> bool:
    """Play a sound file from sounds/. Non-blocking, overlapping OK.
    Always plays — not gated by ui_sounds setting.

    Args:
        name: Filename (with or without extension) in the sounds/ directory.
        volume: Playback level 0.0–1.0 for this play only (per-channel, so it
            doesn't affect other sounds sharing the cached Sound object).
    Returns:
        True if playback started successfully.
    """
    global _channel_index

    if not _ensure_init():
        return False

    # Resolve path
    path = SOUNDS_DIR / name
    if not path.suffix:
        for ext in ('.mp3', '.wav', '.ogg'):
            candidate = path.with_suffix(ext)
            if candidate.exists():
                path = candidate
                name = candidate.name
                break
    if not path.exists():
        return False

    # Get or cache the Sound object
    if name not in _sounds:
        try:
            import pygame
            _sounds[name] = pygame.mixer.Sound(str(path))
        except Exception:
            return False

    # Play on next channel (round-robin for overlap)
    try:
        import pygame
        channel = pygame.mixer.Channel(_channel_index)
        _channel_index = (_channel_index + 1) % _num_channels
        # Set per-channel volume each play so a softer sound can't leak its
        # level onto the next sound that reuses this round-robin channel.
        channel.set_volume(max(0.0, min(1.0, float(volume))))
        channel.play(_sounds[name])
        return True
    except Exception:
        return False
