"""
Model context limit resolver — auto-fetches limits from provider APIs,
caches to disk, falls back to a minimal static table only for providers
that don't expose model metadata (Anthropic, DeepSeek).

Cache lives at data/model_limits_cache.json and is refreshed at most
once per day. The cache stores {model_id: context_limit} pairs.
"""

import json
import os
import time
import threading
from pathlib import Path

CACHE_PATH = Path(__file__).parent.parent / "data" / "model_limits_cache.json"
CACHE_MAX_AGE = 86400  # 24 hours

DEFAULT_CONTEXT_LIMIT = 128_000

# Fallback for providers whose metadata API doesn't expose context windows.
# OpenAI's /v1/models returns NO context_window field, so without these
# entries every gpt-* model silently resolved to the 128k default — gpt-5.6-sol
# (real window 400k) was treated as 128k, and worse, any model whose real
# window is SMALLER than 128k overflowed with no local compaction.
_STATIC_FALLBACK = {
    # Anthropic (no model list API)
    "claude-opus-4":            200_000,
    "claude-sonnet-4":          200_000,
    "claude-3-5-sonnet":        200_000,
    "claude-3-5-haiku":         200_000,
    "claude-3-opus":            200_000,
    "claude-3-sonnet":          200_000,
    "claude-3-haiku":           200_000,
    "claude-haiku-4":           200_000,
    # OpenAI (metadata API omits context_window)
    "gpt-5.6":                  400_000,   # covers gpt-5.6-sol etc.
    "gpt-5":                    400_000,
    "gpt-4.1":                  1_000_000,
    "gpt-4o":                   128_000,
    "gpt-4-turbo":              128_000,
    "o3":                       200_000,
    "o4":                       200_000,
    # DeepSeek (no metadata endpoint)
    "deepseek-chat":            128_000,
    "deepseek-coder":           128_000,
    "deepseek-reasoner":        128_000,
}

# ── Cache management ────────────────────────────────────────────────────

_cache: dict[str, int] = {}
_cache_loaded = False
_fetch_lock = threading.Lock()


def _load_cache():
    """Load cached limits from disk."""
    global _cache, _cache_loaded
    if _cache_loaded:
        return
    try:
        if CACHE_PATH.exists():
            data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                ts = data.get("_timestamp", 0)
                if time.time() - ts < CACHE_MAX_AGE:
                    _cache = {k: v for k, v in data.items()
                              if k != "_timestamp" and isinstance(v, int)}
    except Exception:
        pass
    _cache_loaded = True


def _save_cache():
    """Persist cache to disk."""
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {**_cache, "_timestamp": int(time.time())}
        CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


# ── Provider API fetchers ───────────────────────────────────────────────

def _fetch_openrouter_models(api_key: str) -> dict[str, int]:
    """Fetch model limits from OpenRouter's /api/v1/models endpoint."""
    import urllib.request
    import urllib.error

    results = {}
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/user/agent",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        for model in data.get("data", []):
            model_id = model.get("id", "")
            ctx = model.get("context_length", 0)
            if model_id and ctx > 0:
                results[model_id] = ctx
    except Exception as e:
        print(f"[ModelLimits] OpenRouter fetch failed: {e}")

    return results


def _fetch_openai_models(api_key: str, base_url: str = "") -> dict[str, int]:
    """Fetch model limits from an OpenAI-compatible /v1/models endpoint."""
    import urllib.request

    url = (base_url.rstrip("/") if base_url else "https://api.openai.com/v1") + "/models"
    results = {}
    try:
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        for model in data.get("data", []):
            model_id = model.get("id", "")
            # OpenAI returns context_window on some models
            ctx = model.get("context_window", 0)
            if not ctx:
                # Some endpoints use different field names
                ctx = model.get("context_length", 0)
            if model_id and ctx > 0:
                results[model_id] = ctx
    except Exception as e:
        print(f"[ModelLimits] OpenAI-compat fetch failed ({url}): {e}")

    return results


def _fetch_google_models(api_key: str) -> dict[str, int]:
    """Fetch model limits from Google's generative AI API."""
    import urllib.request

    results = {}
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        for model in data.get("models", []):
            # Google returns "models/gemini-2.5-flash" — strip prefix
            model_id = model.get("name", "").replace("models/", "")
            ctx = model.get("inputTokenLimit", 0)
            if model_id and ctx > 0:
                results[model_id] = ctx
    except Exception as e:
        print(f"[ModelLimits] Google fetch failed: {e}")

    return results


# ── Refresh orchestrator ────────────────────────────────────────────────

def refresh_cache(force: bool = False):
    """Fetch model limits from all configured providers and update the cache.

    Called lazily on first lookup if cache is stale, or explicitly at startup.
    Runs in a background thread to avoid blocking.
    """
    global _cache

    if not _fetch_lock.acquire(blocking=False):
        return  # Another thread is already fetching

    try:
        _load_cache()

        # Check if cache is fresh enough
        if not force and _cache:
            try:
                if CACHE_PATH.exists():
                    data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                    if time.time() - data.get("_timestamp", 0) < CACHE_MAX_AGE:
                        return
            except Exception:
                pass

        from core.providers import load_keys, resolve_google_api_key

        keys = load_keys()
        new_models: dict[str, int] = {}

        # OpenRouter — most valuable, covers everything they route
        or_key = (keys.get("openrouter") or {}).get("api_key", "")
        if or_key:
            new_models.update(_fetch_openrouter_models(or_key))

        # OpenAI direct
        oai_entry = keys.get("openai") or {}
        oai_key = oai_entry.get("api_key", "")
        if oai_key:
            new_models.update(_fetch_openai_models(
                oai_key, oai_entry.get("base_url", "")))

        # Google
        g_key = resolve_google_api_key((keys.get("google") or {}).get("api_key", ""))
        if g_key:
            new_models.update(_fetch_google_models(g_key))

        if new_models:
            _cache.update(new_models)
            _save_cache()
            print(f"[ModelLimits] Cached {len(new_models)} model limits "
                  f"({len(_cache)} total)")

    finally:
        _fetch_lock.release()


_last_stale_check = 0.0
_STALE_CHECK_EVERY = 300  # seconds between on-disk staleness probes


def _ensure_cache():
    """Make sure the in-memory cache is loaded. NEVER blocks on network:
    a cold/stale cache kicks a background refresh and lookups fall through
    to the static table meanwhile. (The old synchronous refresh here made the
    first turn after a model switch hang up to ~35s on serial provider
    fetches — the 'changing model froze the app' bug.)"""
    global _last_stale_check
    _load_cache()
    now = time.time()
    if now - _last_stale_check < _STALE_CHECK_EVERY:
        return
    _last_stale_check = now
    stale = not _cache
    if not stale:
        try:
            if CACHE_PATH.exists():
                data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
                stale = now - data.get("_timestamp", 0) >= CACHE_MAX_AGE
            else:
                stale = True
        except Exception:
            stale = True
    if stale:
        threading.Thread(target=refresh_cache, kwargs={"force": True},
                         daemon=True, name="model-limits-refresh").start()


# ── Public API ──────────────────────────────────────────────────────────

def get_model_context_limit(model: str) -> int:
    """Look up the context window size for a model.

    Resolution order:
      1. Exact match in API-fetched cache
      2. Prefix match in API-fetched cache (for dated model IDs)
      3. Exact match in static fallback (Anthropic, DeepSeek)
      4. Prefix match in static fallback
      5. DEFAULT_CONTEXT_LIMIT (128k)
    """
    if not model:
        return DEFAULT_CONTEXT_LIMIT

    _ensure_cache()

    # 1. Exact match in cache
    if model in _cache:
        return _cache[model]

    # 2. Prefix match in cache (longest wins)
    best_match = ""
    best_limit = 0
    for key, limit in _cache.items():
        if model.startswith(key) and len(key) > len(best_match):
            best_match = key
            best_limit = limit
    if best_limit > 0:
        return best_limit

    # 3. Exact match in static fallback
    if model in _STATIC_FALLBACK:
        return _STATIC_FALLBACK[model]

    # 4. Prefix match in static fallback
    best_match = ""
    best_limit = DEFAULT_CONTEXT_LIMIT
    for prefix, limit in _STATIC_FALLBACK.items():
        if model.startswith(prefix) and len(prefix) > len(best_match):
            best_match = prefix
            best_limit = limit

    return best_limit
