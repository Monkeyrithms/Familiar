"""
Worker script that runs INSIDE the isolated chatterbox venv.
Reads JSON {text, voice_ref, output_path} from stdin, writes audio to output_path,
prints JSON result to stdout. Keeps main agent Python env free of heavy deps.
"""

import json
import os
import sys
import traceback


def _preflight():
    # Cache HF models inside the app so uninstall = delete-a-folder.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    hf_cache = os.path.join(root, "data", "chatterbox_models")
    os.makedirs(hf_cache, exist_ok=True)
    os.environ.setdefault("HF_HOME", hf_cache)
    os.environ.setdefault("HUGGINGFACE_HUB_CACHE", hf_cache)


def _pick_device():
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _log(msg: str):
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        log_dir = os.path.join(root, "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "chatterbox_worker.log"), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


_ONES = ["zero","one","two","three","four","five","six","seven","eight","nine",
         "ten","eleven","twelve","thirteen","fourteen","fifteen","sixteen",
         "seventeen","eighteen","nineteen"]
_TENS = ["","","twenty","thirty","forty","fifty","sixty","seventy","eighty","ninety"]


def _num_to_words(n: int) -> str:
    """0-99 → words. Used for time minutes/hours so Chatterbox doesn't mumble digits."""
    if n < 0 or n > 99:
        return str(n)
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (f" {_ONES[o]}" if o else "")


def _speak_time(hh: int, mm: int, ampm: str = "") -> str:
    """'3:24 PM' → 'three twenty four PM', '3:00' → 'three o'clock',
    '3:05' → 'three oh five'. Keeps AM/PM as letters — Chatterbox reads them fine."""
    h = _num_to_words(hh)
    if mm == 0:
        core = f"{h} o'clock" if not ampm else h
    elif 1 <= mm <= 9:
        core = f"{h} oh {_num_to_words(mm)}"
    else:
        core = f"{h} {_num_to_words(mm)}"
    return f"{core} {ampm}".strip() if ampm else core


def _normalize_for_tts(text: str) -> str:
    """Scrub symbols and convert numeric patterns into spoken forms. Runs
    before chunking so the model never sees raw glyphs like '~' or '3:24'."""
    import re
    t = text
    # Approximation tilde: "~3:24" → "about 3:24", standalone "~" → dropped
    t = re.sub(r'~\s*(?=\d)', 'about ', t)
    t = t.replace('~', '')
    # Times: "3:24 PM", "12:05", "3:00am" — case-insensitive AM/PM with optional space/dots
    def _time_sub(m):
        hh = int(m.group(1)); mm = int(m.group(2))
        ampm = (m.group(3) or "").upper().replace(".", "")
        if hh > 23 or mm > 59:
            return m.group(0)
        return _speak_time(hh, mm, ampm)
    t = re.sub(r'\b(\d{1,2}):(\d{2})\s*(a\.?m\.?|p\.?m\.?)?\b', _time_sub, t,
               flags=re.IGNORECASE)
    # Common glyph substitutions
    t = t.replace('&', ' and ')
    t = re.sub(r'(?<=\w)@(?=\w)', ' at ', t)   # email-ish @ becomes "at"
    t = t.replace('@', ' at ')
    t = re.sub(r'\s+', ' ', t).strip()
    return t


def _chunk_text_for_chatterbox(text: str, limit: int = 280) -> list:
    """Split long text at sentence boundaries so each piece fits under
    Chatterbox's ~30s / ~1000-token per-generate cap. `limit` is conservative
    chars/chunk; most real sentences are shorter."""
    import re
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks, current = [], ""
    for s in sentences:
        if not s.strip():
            continue
        if len(s) > limit:
            # Sentence itself too long — split on commas / semicolons
            if current:
                chunks.append(current.strip()); current = ""
            for part in re.split(r'(?<=[,;])\s+', s):
                if len(current) + len(part) + 1 > limit and current:
                    chunks.append(current.strip()); current = ""
                current = (current + " " + part).strip() if current else part
            continue
        if len(current) + len(s) + 1 > limit:
            chunks.append(current.strip()); current = s
        else:
            current = (current + " " + s).strip() if current else s
    if current.strip():
        chunks.append(current.strip())
    return [c for c in chunks if c]


def _check_perth():
    """Perth's watermarker silently becomes None if pkg_resources is unavailable
    (setuptools>=81). Catch this early with a useful message."""
    try:
        import perth
        if getattr(perth, "PerthImplicitWatermarker", None) is None:
            return ("Chatterbox dependency 'perth' is broken "
                    "(pkg_resources missing — setuptools too new). "
                    "Run Settings → Voice → Reinstall.")
    except Exception as e:
        return f"perth import failed: {e}"
    return None


def _daemon_loop():
    """Load model once, then serve one JSON request per stdin line, emit one JSON
    response per stdout line. Exit on EOF or {"action":"shutdown"}."""
    _preflight()
    _log(f"=== daemon start pid={os.getpid()} ===")
    pre_err = _check_perth()
    if pre_err:
        _log(f"preflight: {pre_err}")
        sys.stdout.write(json.dumps({"error": pre_err}) + "\n"); sys.stdout.flush()
        return
    try:
        import torchaudio as ta
        from chatterbox.tts import ChatterboxTTS
        device = _pick_device()
        _log(f"daemon device={device}")
        model = ChatterboxTTS.from_pretrained(device=device)
        _log(f"daemon model loaded sr={model.sr}")
    except Exception as e:
        tb = traceback.format_exc()
        _log(f"daemon load error: {e}\n{tb}")
        sys.stdout.write(json.dumps({"error": str(e), "trace": tb}) + "\n")
        sys.stdout.flush()
        return

    # Ready signal so the backend can stop waiting
    sys.stdout.write(json.dumps({"ready": True, "sr": model.sr}) + "\n")
    sys.stdout.flush()

    _log("daemon: entering request loop")
    while True:
        line = sys.stdin.readline()
        if not line:
            _log("daemon stdin EOF"); return
        line = line.strip()
        if not line:
            continue
        _log(f"daemon: got request len={len(line)}")
        try:
            req = json.loads(line)
        except Exception as e:
            sys.stdout.write(json.dumps({"error": f"bad json: {e}"}) + "\n")
            sys.stdout.flush(); continue
        if req.get("action") == "shutdown":
            _log("daemon shutdown requested"); return
        text = req.get("text", "")
        voice_ref = req.get("voice_ref") or ""
        output_path = req.get("output_path", "")
        if not text or not output_path:
            sys.stdout.write(json.dumps({"error": "text/output_path required"}) + "\n")
            sys.stdout.flush(); continue
        try:
            kwargs = {}
            if voice_ref and os.path.exists(voice_ref):
                kwargs["audio_prompt_path"] = voice_ref
            normalized = _normalize_for_tts(text)
            if normalized != text:
                _log(f"daemon: normalized text (example: {normalized[:80]!r})")
            chunks = _chunk_text_for_chatterbox(normalized)
            _log(f"daemon: generate start text_len={len(normalized)} chunks={len(chunks)}")
            import time as _t
            t0 = _t.time()
            if len(chunks) <= 1:
                wav = model.generate(chunks[0] if chunks else text, **kwargs)
            else:
                import torch
                pieces = []
                for i, c in enumerate(chunks):
                    ts = _t.time()
                    piece = model.generate(c, **kwargs)
                    _log(f"daemon: chunk {i+1}/{len(chunks)} "
                         f"len={len(c)} -> {piece.shape} in {_t.time()-ts:.2f}s")
                    pieces.append(piece)
                wav = torch.cat(pieces, dim=-1)
            _log(f"daemon: generate done in {_t.time()-t0:.2f}s wav={type(wav).__name__}")
            ta.save(output_path, wav, model.sr)
            _log(f"daemon: saved {output_path}")
            sys.stdout.write(json.dumps({"ok": True, "path": output_path}) + "\n")
        except Exception as e:
            tb = traceback.format_exc()
            _log(f"daemon synth error: {e}\n{tb}")
            sys.stdout.write(json.dumps({"error": str(e), "trace": tb}) + "\n")
        sys.stdout.flush()


def main():
    _preflight()
    _log(f"=== worker start pid={os.getpid()} ===")
    # Daemon mode: -d or --daemon as first arg → persistent loop
    if len(sys.argv) > 1 and sys.argv[1] in ("-d", "--daemon"):
        _daemon_loop(); return
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        _log(f"bad payload: {e}")
        print(json.dumps({"error": f"bad payload: {e}"})); return
    _log(f"payload action={payload.get('action')}")

    text = payload.get("text", "")
    voice_ref = payload.get("voice_ref") or ""
    output_path = payload.get("output_path", "")
    action = payload.get("action", "synth")

    if action == "warmup":
        try:
            from chatterbox.tts import ChatterboxTTS  # noqa: F401
            ChatterboxTTS.from_pretrained(device=_pick_device())
            print(json.dumps({"ok": True}))
        except Exception as e:
            print(json.dumps({"error": str(e), "trace": traceback.format_exc()}))
        return

    if not text or not output_path:
        print(json.dumps({"error": "text and output_path are required"})); return

    try:
        import torchaudio as ta
        from chatterbox.tts import ChatterboxTTS
        device = _pick_device()
        _log(f"device={device}")
        model = ChatterboxTTS.from_pretrained(device=device)
        _log(f"model loaded, sr={getattr(model, 'sr', '?')}")
        kwargs = {}
        if voice_ref and os.path.exists(voice_ref):
            kwargs["audio_prompt_path"] = voice_ref
        _log(f"generate text_len={len(text)} kwargs={list(kwargs)}")
        wav = model.generate(text, **kwargs)
        _log(f"wav type={type(wav).__name__}")
        ta.save(output_path, wav, model.sr)
        _log(f"saved {output_path}")
        print(json.dumps({"ok": True, "path": output_path, "sr": model.sr}))
    except Exception as e:
        tb = traceback.format_exc()
        _log(f"ERROR: {e}\n{tb}")
        print(json.dumps({"error": str(e), "trace": tb}))


if __name__ == "__main__":
    main()
