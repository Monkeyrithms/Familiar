"""
Voice reference clip library for Chatterbox cloning.

Clips live in data/voice_refs/. Any .wav/.mp3/.flac/.ogg file in that folder
is exposed as a selectable voice in the Settings UI. One clip = one voice;
Chatterbox does zero-shot cloning at inference time, no training needed.
"""

from pathlib import Path

VOICE_REFS_DIR = Path(__file__).parent.parent.parent / "data" / "voice_refs"
SUPPORTED_EXTS = {".wav", ".mp3", ".flac", ".ogg"}


def ensure_dir() -> None:
    VOICE_REFS_DIR.mkdir(parents=True, exist_ok=True)


def list_voice_refs() -> list:
    """Return absolute Paths of all supported audio files in the folder,
    sorted by name."""
    ensure_dir()
    out = []
    for p in sorted(VOICE_REFS_DIR.iterdir(), key=lambda x: x.name.lower()):
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS:
            out.append(p)
    return out
