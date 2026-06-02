"""Microsoft Edge Neural TTS backend. Free, no API key, requires internet."""

import asyncio
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "audio_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_VOICE = "en-US-AriaNeural"
CHUNK_CHARS = 5000


def _chunk_text(text: str, limit: int = CHUNK_CHARS) -> list:
    import re
    if len(text) <= limit:
        return [text]
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks, current = [], ""
    for s in sentences:
        if not s.strip():
            continue
        if len(s) > limit:
            if current:
                chunks.append(current.strip()); current = ""
            for part in re.split(r'(?<=[,;\n])\s*', s):
                if len(current) + len(part) + 1 > limit and current:
                    chunks.append(current.strip()); current = ""
                current += (" " if current else "") + part
            continue
        if len(current) + len(s) + 1 > limit:
            chunks.append(current.strip()); current = s
        else:
            current += (" " if current else "") + s
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


async def _generate(chunks: list, voice: str, output_path: str, rate: str) -> str:
    import edge_tts
    if len(chunks) == 1:
        await edge_tts.Communicate(chunks[0], voice, rate=rate).save(output_path)
        return output_path
    tmp = Path(tempfile.mkdtemp(prefix="tts_chunks_"))
    try:
        parts = []
        for i, c in enumerate(chunks):
            p = str(tmp / f"chunk_{i:03d}.mp3")
            await edge_tts.Communicate(c, voice, rate=rate).save(p)
            if Path(p).exists() and Path(p).stat().st_size > 0:
                parts.append(p)
        if not parts:
            raise RuntimeError("No audio chunks were generated")
        with open(output_path, "wb") as out:
            for pth in parts:
                out.write(Path(pth).read_bytes())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return output_path


def synth(text: str, *, voice: str = "", speed: int = 0,
          output_path: str = "") -> Tuple[str, Optional[str]]:
    if not output_path:
        ts = time.strftime("%Y%m%d_%H%M%S")
        output_path = str(CACHE_DIR / f"tts_{ts}.mp3")
    voice = voice or DEFAULT_VOICE
    rate = f"+{speed}%" if speed >= 0 else f"{speed}%"
    try:
        asyncio.run(_generate(_chunk_text(text), voice, output_path, rate))
    except Exception as e:
        return "", f"Edge TTS failed: {e}"
    if not Path(output_path).exists():
        return "", "Edge TTS produced no file"
    return output_path, None


def list_voices(language: str = "en") -> list:
    import edge_tts

    async def _list():
        voices = await edge_tts.list_voices()
        return [v for v in voices if v["Locale"].startswith(language)]

    try:
        return asyncio.run(_list())
    except Exception:
        return []
