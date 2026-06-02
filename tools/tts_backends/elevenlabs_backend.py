"""ElevenLabs TTS backend. Premium quality via API. Requires key in data/keys.json under 'elevenlabs'."""

import json
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional, Tuple

CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "audio_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
KEYS_PATH = Path(__file__).parent.parent.parent / "data" / "keys.json"

DEFAULT_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # Adam
DEFAULT_MODEL = "eleven_multilingual_v2"
API_BASE = "https://api.elevenlabs.io/v1"


def get_api_key() -> str:
    try:
        data = json.loads(KEYS_PATH.read_text(encoding="utf-8"))
        return (data.get("elevenlabs") or {}).get("api_key", "").strip()
    except Exception:
        return ""


def _speed_multiplier(speed: int) -> float:
    """Map our -50..+100 percent speed slider to ElevenLabs 0.5..2.0 multiplier."""
    return max(0.5, min(2.0, 1.0 + speed / 100.0))


def synth(text: str, *, voice: str = "", speed: int = 0,
          output_path: str = "") -> Tuple[str, Optional[str]]:
    api_key = get_api_key()
    if not api_key:
        return "", "ElevenLabs API key not set. Add it in Settings → Voice."
    voice_id = voice or DEFAULT_VOICE_ID
    if not output_path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        output_path = str(CACHE_DIR / f"tts_{ts}.mp3")

    body = {
        "text": text,
        "model_id": DEFAULT_MODEL,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
            "speed": _speed_multiplier(speed),
        },
    }
    url = f"{API_BASE}/text-to-speech/{voice_id}?output_format=mp3_44100_128"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            Path(output_path).write_bytes(resp.read())
    except urllib.error.HTTPError as e:
        try:
            err = e.read().decode("utf-8", errors="ignore")
        except Exception:
            err = str(e)
        return "", f"ElevenLabs HTTP {e.code}: {err[:300]}"
    except Exception as e:
        return "", f"ElevenLabs request failed: {e}"

    if not Path(output_path).exists() or Path(output_path).stat().st_size == 0:
        return "", "ElevenLabs returned empty audio"
    return output_path, None


def list_voices() -> list:
    """Fetch available voices from ElevenLabs. Returns [] on failure."""
    api_key = get_api_key()
    if not api_key:
        return []
    req = urllib.request.Request(f"{API_BASE}/voices", headers={"xi-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("voices", [])
    except Exception:
        return []
