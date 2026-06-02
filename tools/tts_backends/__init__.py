"""
TTS backend dispatcher. Picks the engine based on config['tts_backend']:
    - 'edge' (default): Microsoft Edge Neural voices, free, internet required
    - 'elevenlabs': ElevenLabs API, premium quality, API key required
    - 'chatterbox': local neural TTS with voice cloning, self-hosted

Each backend exposes a synth(text, voice, speed, output_path) -> (path, error) contract.
"""

from pathlib import Path
from typing import Optional, Tuple


def synth(text: str, *, backend: str, voice: str = "", speed: int = 0,
          output_path: str = "", **kwargs) -> Tuple[str, Optional[str]]:
    """Generate TTS via the named backend. Returns (audio_path, error_or_None)."""
    backend = (backend or "edge").lower()
    if backend == "edge":
        from . import edge
        return edge.synth(text, voice=voice, speed=speed, output_path=output_path)
    if backend == "elevenlabs":
        from . import elevenlabs_backend
        return elevenlabs_backend.synth(text, voice=voice, speed=speed, output_path=output_path)
    if backend == "chatterbox":
        from . import chatterbox_backend
        return chatterbox_backend.synth(text, voice=voice, speed=speed, output_path=output_path)
    return "", f"Unknown TTS backend: {backend}"


def is_installed(backend: str) -> bool:
    """Quick check whether a backend is ready to use."""
    backend = (backend or "edge").lower()
    if backend == "edge":
        try:
            import edge_tts  # noqa: F401
            return True
        except Exception:
            return False
    if backend == "elevenlabs":
        from . import elevenlabs_backend
        return bool(elevenlabs_backend.get_api_key())
    if backend == "chatterbox":
        from . import chatterbox_installer
        return chatterbox_installer.is_installed()
    return False
