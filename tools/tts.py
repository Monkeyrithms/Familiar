"""
Text-to-Speech tool — dispatches to a backend chosen in config['tts_backend']:
    - 'edge' (default): Microsoft Edge Neural voices, free, internet required
    - 'elevenlabs': ElevenLabs API, premium quality
    - 'chatterbox': local neural TTS, installed into an isolated venv

Backends live in tools/tts_backends/. Each speaks a common synth() contract;
this file keeps the markdown-stripping, chunking wrappers, playback, and the
registered agent tool.
"""

import json
import re
import shutil
import subprocess
import time
from pathlib import Path
from tools.registry import registry
from tools import tts_backends
from core.proc import NO_WINDOW

CACHE_DIR = Path(__file__).parent.parent / "data" / "audio_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_VOICE = "en-US-AriaNeural"


def _strip_markdown(text: str) -> str:
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'__([^_]+)__', r'\1', text)
    text = re.sub(r'_([^_]+)_', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', text)
    text = re.sub(r'^[\s]*[-*+]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[\s]*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _play_audio(path: str, blocking: bool = False):
    """Play an audio file.

    If *blocking* is False (default), launch in background and return the
    ``subprocess.Popen`` handle (or *None* on failure) so callers can
    ``.wait()`` or ``.kill()`` it.

    If *blocking* is True, wait for the player to finish before returning
    and return True/False for success.
    """
    import sys
    players = []
    if sys.platform == "win32":
        try:
            size = Path(path).stat().st_size
            est_seconds = max(10, int(size / 3000) + 5)
        except Exception:
            est_seconds = 300
        players.append(
            ["powershell", "-c",
             f'Add-Type -AssemblyName PresentationCore; '
             f'$p = New-Object System.Windows.Media.MediaPlayer; '
             f'$p.Open([uri]"{path}"); $p.Play(); '
             f'Start-Sleep -Seconds {est_seconds}'])
    elif sys.platform == "darwin":
        players.append(["afplay", path])
    if shutil.which("mpv"):
        players.append(["mpv", "--no-video", "--really-quiet", path])
    if shutil.which("ffplay"):
        players.append(["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path])
    if shutil.which("vlc") or shutil.which("cvlc"):
        vlc = shutil.which("cvlc") or shutil.which("vlc")
        players.append([vlc, "--play-and-exit", "--intf", "dummy", "--quiet", path])
    for cmd in players:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW)  # no console flash on Windows
            if blocking:
                proc.wait()
                return True
            return proc
        except Exception:
            continue
    return None if not blocking else False


def _load_tts_config() -> dict:
    try:
        cfg_path = Path(__file__).parent.parent / "config.json"
        if cfg_path.exists():
            return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _voice_for_backend(cfg: dict, backend: str) -> str:
    if backend == "elevenlabs":
        return cfg.get("elevenlabs_voice_id", "") or cfg.get("tts_voice_elevenlabs", "")
    if backend == "chatterbox":
        return cfg.get("chatterbox_voice_ref", "") or ""
    return cfg.get("tts_voice", DEFAULT_VOICE)


def synthesize_audio(text: str, voice: str = "") -> str | None:
    """Synthesize text to an audio file WITHOUT playing it.

    Returns the file path on success, or None on failure.
    Used by the voice queue manager in the UI.
    """
    clean = _strip_markdown(text)
    if not clean:
        return None
    cfg = _load_tts_config()
    backend = (cfg.get("tts_backend") or "edge").lower()
    resolved_voice = voice or _voice_for_backend(cfg, backend)
    speed = cfg.get("tts_speed", 0)
    ts = time.strftime("%Y%m%d_%H%M%S")
    ext = "wav" if backend == "chatterbox" else "mp3"
    output_path = str(CACHE_DIR / f"tts_{ts}_{id(text) % 10000}.{ext}")
    path, err = tts_backends.synth(clean, backend=backend, voice=resolved_voice,
                                   speed=speed, output_path=output_path)
    if err:
        return None
    return path


def text_to_speech(text: str = "", voice: str = "", play: bool = None,
                   action: str = "speak", language: str = "en") -> str:
    if action == "list_voices" or (not text and not voice):
        return list_voices(language)

    if not text or not text.strip():
        return json.dumps({"error": "No text provided"})

    clean = _strip_markdown(text)
    if not clean:
        return json.dumps({"error": "Text was empty after stripping markdown"})

    cfg = _load_tts_config()
    backend = (cfg.get("tts_backend") or "edge").lower()
    resolved_voice = voice or _voice_for_backend(cfg, backend)
    if play is None:
        play = cfg.get("tts_autoplay", True)
    speed = cfg.get("tts_speed", 0)
    ts = time.strftime("%Y%m%d_%H%M%S")
    ext = "wav" if backend == "chatterbox" else "mp3"
    output_path = str(CACHE_DIR / f"tts_{ts}.{ext}")

    path, err = tts_backends.synth(clean, backend=backend, voice=resolved_voice,
                                   speed=speed, output_path=output_path)
    if err:
        return json.dumps({"error": err, "backend": backend})

    result = {
        "audio_path": path,
        "backend": backend,
        "voice": resolved_voice,
        "chars": len(clean),
        "size_bytes": Path(path).stat().st_size if Path(path).exists() else 0,
    }
    if play:
        result["played"] = _play_audio(path)
    return json.dumps(result)


def list_voices(language: str = "en") -> str:
    """List voices for the currently selected backend."""
    cfg = _load_tts_config()
    backend = (cfg.get("tts_backend") or "edge").lower()
    if backend == "edge":
        from tools.tts_backends import edge
        voices = [{"name": v["ShortName"], "gender": v["Gender"], "locale": v["Locale"]}
                  for v in edge.list_voices(language)]
        return json.dumps({"backend": "edge", "voices": voices, "count": len(voices)})
    if backend == "elevenlabs":
        from tools.tts_backends import elevenlabs_backend
        voices = [{"voice_id": v.get("voice_id", ""), "name": v.get("name", ""),
                   "category": v.get("category", "")} for v in elevenlabs_backend.list_voices()]
        return json.dumps({"backend": "elevenlabs", "voices": voices, "count": len(voices)})
    return json.dumps({"backend": backend, "voices": [], "count": 0,
                       "note": "Chatterbox uses a reference audio clip instead of a voice list."})


registry.register(
    name="text_to_speech",
    description=(
        "TTS + voice mgmt. Default: speak text aloud. "
        "action='list_voices' → list available voices."
    ),
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to speak."},
            "voice": {"type": "string", "description": "Voice name/ID (backend-specific)."},
            "play": {"type": "boolean", "description": "Auto-play (default true)."},
            "action": {"type": "string", "enum": ["speak", "list_voices"],
                       "description": "speak (default) | list_voices."},
            "language": {"type": "string", "description": "Lang filter for list_voices."},
        },
    },
    execute=text_to_speech,
)
