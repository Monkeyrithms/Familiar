"""
Audio transcription tool — convert speech to text.
Uses OpenAI Whisper API via OpenRouter, or local whisper if installed.
"""

import json
from pathlib import Path
from tools.registry import registry


def transcribe(audio_path: str, language: str = "", model: str = "whisper-1") -> str:
    """Transcribe audio to text."""
    p = Path(audio_path)
    if not p.exists():
        return json.dumps({"error": f"Audio file not found: {audio_path}"})

    # Try local whisper first (pip install openai-whisper)
    try:
        import whisper
        local_model = whisper.load_model("base")
        result = local_model.transcribe(str(p), language=language or None)
        return json.dumps({
            "text": result["text"].strip(),
            "language": result.get("language", ""),
            "engine": "local_whisper",
        }, ensure_ascii=False)
    except ImportError:
        pass
    except Exception as e:
        # Fall through to API
        pass

    # Try OpenAI API (works via OpenRouter too)
    try:
        from core.providers import load_keys
        keys = load_keys()  # data/keys.json (with legacy-root migration)
        api_key = keys.get("openai", {}).get("api_key") or keys.get("openrouter", {}).get("api_key")
        if not api_key:
            return json.dumps({"error": "No OpenAI or OpenRouter key for Whisper API. Install local whisper: pip install openai-whisper"})

        from openai import OpenAI
        base_url = "https://openrouter.ai/api/v1" if not keys.get("openai", {}).get("api_key") else None
        client = OpenAI(api_key=api_key, **({"base_url": base_url} if base_url else {}))

        with open(audio_path, "rb") as f:
            result = client.audio.transcriptions.create(model=model, file=f, language=language or None)

        return json.dumps({
            "text": result.text.strip(),
            "engine": "api_whisper",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Transcription failed: {e}. Install local whisper: pip install openai-whisper"})


registry.register(
    name="transcribe",
    description=(
        "Audio → text. Local Whisper if installed → OpenAI Whisper API. "
        "Supports mp3, wav, m4a, etc."
    ),
    parameters={
        "type": "object",
        "properties": {
            "audio_path": {"type": "string", "description": "Audio file path."},
            "language": {"type": "string", "description": "Lang code (opt, auto-detect)."},
        },
        "required": ["audio_path"],
    },
    execute=transcribe,
)
