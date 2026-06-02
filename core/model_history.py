"""
Per-provider recent main-model IDs for autocomplete (Settings / Conversation).

Persists under config key ``provider_model_history`` as ``{provider_id: [str, ...]}``
(newest first, capped). Legacy ``provider_models`` (last string per provider) is
merged in when loading so existing installs keep their remembered model as the
first suggestion.
"""

from __future__ import annotations

MAX_RECENT_MODELS_PER_PROVIDER = 5


def merge_stored_provider_model_memory(cfg: dict) -> dict[str, list[str]]:
    """
    Normalize ``cfg["provider_model_history"]`` from stored history plus
    ``provider_models`` strings. Mutates ``cfg``. Returns the history mapping.
    """
    pm = cfg.get("provider_models") or {}
    if not isinstance(pm, dict):
        pm = {}
    hist_in = cfg.get("provider_model_history") or {}
    if not isinstance(hist_in, dict):
        hist_in = {}

    result: dict[str, list[str]] = {}
    for pid, seq in hist_in.items():
        if not isinstance(pid, str):
            continue
        if not isinstance(seq, list):
            continue
        seen: list[str] = []
        for x in seq:
            s = str(x).strip()
            if s and s not in seen:
                seen.append(s)
        if seen:
            result[pid] = seen[:MAX_RECENT_MODELS_PER_PROVIDER]

    for pid, last in pm.items():
        if not isinstance(pid, str) or not isinstance(last, str):
            continue
        last = last.strip()
        if not last:
            continue
        cur = list(result.get(pid) or [])
        if last in cur:
            cur = [last] + [x for x in cur if x != last]
        else:
            cur = [last] + cur
        result[pid] = cur[:MAX_RECENT_MODELS_PER_PROVIDER]

    cfg["provider_model_history"] = result
    return result


def touch_provider_model_choice(
    provider_models: dict,
    provider_model_history: dict[str, list],
    pid: str,
    model: str,
) -> None:
    """Record a model choice for *pid*: update last-used string and MRU history."""
    model = (model or "").strip()
    if not model or not pid:
        return
    provider_models[pid] = model
    cur = list(provider_model_history.get(pid) or [])
    if model in cur:
        cur.remove(model)
    cur.insert(0, model)
    provider_model_history[pid] = cur[:MAX_RECENT_MODELS_PER_PROVIDER]
