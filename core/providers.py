"""
LLM provider connections.

All providers use the OpenAI Python SDK with different base_url values,
except Anthropic which uses its native SDK and an adapter that maps
responses to the OpenAI format so the rest of the codebase stays unchanged.
"""

import json
import os
from pathlib import Path
# NOTE: ``from openai import OpenAI`` is deliberately NOT done at module level.
# The openai SDK (+ its httpx/pydantic deps) costs ~1.3s to import, and several
# tools import this module just for load_keys() at startup — which dragged that
# whole cost onto the pre-window startup path. OpenAI is only needed when a
# client is actually created (get_client, inference time), so we import it
# lazily there. Saves ~1.3s before the UI appears.

from core.model_quirks import (
    strip_unsupported as _strip_quirks,
    mark_unsupported as _mark_quirk,
    detect_rejected_param as _detect_rejected,
)

_APP_ROOT = Path(__file__).parent.parent
# API keys live under data/ — that directory is the user-state / .gitignore
# boundary, so secrets never ship with the source. A keys.json left at the old
# repo-root location from a previous version is migrated into data/ on first
# load (see _migrate_legacy_keys).
KEYS_PATH = _APP_ROOT / "data" / "keys.json"
_LEGACY_KEYS_PATH = _APP_ROOT / "keys.json"
CONFIG_PATH = _APP_ROOT / "config.json"


def _migrate_legacy_keys() -> None:
    """Move a root-level keys.json (old location) into data/ once, so existing
    installs keep their keys after the data/-boundary change."""
    try:
        if _LEGACY_KEYS_PATH.exists() and not KEYS_PATH.exists():
            KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
            KEYS_PATH.write_text(
                _LEGACY_KEYS_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            # Leave the legacy file in place but neutralized so a stale copy
            # can't be shipped; the active store is now data/keys.json.
            try:
                _LEGACY_KEYS_PATH.unlink()
            except Exception:
                pass
    except Exception:
        pass

# Provider metadata: display name, placeholder model, notes
PROVIDER_INFO = {
    "local":       {"name": "Local API",     "default_model": "default"},
    "openrouter":  {"name": "OpenRouter",    "default_model": "deepseek/deepseek-chat-v3-0324"},
    "openai":      {"name": "OpenAI",        "default_model": "gpt-4o"},
    "anthropic":   {"name": "Anthropic",     "default_model": "claude-sonnet-4-20250514"},
    "deepseek":    {"name": "DeepSeek",      "default_model": "deepseek-chat"},
    # Same Google AI Studio key as ChatBot/Hybrid; uses Gemini OpenAI-compatible endpoint.
    "google":      {"name": "Google GenAI",  "default_model": "gemini-2.5-flash"},
    "kimi":        {"name": "Kimi/Moonshot", "default_model": "moonshot-v1-auto"},
    "zai":         {"name": "Z.AI / GLM",    "default_model": "glm-4-plus"},
    "minimax":     {"name": "MiniMax",       "default_model": "MiniMax-M1"},
    "alibaba":     {"name": "Alibaba Cloud", "default_model": "qwen-max"},
    "huggingface": {"name": "Hugging Face",  "default_model": "deepseek/DeepSeek-V3-0324"},
}


# ── Model context limits ───────────────────────────────────────────────

def get_model_context_limit(model: str) -> int:
    """Look up the context window size for a model.

    Delegates to core.model_limits which auto-fetches from provider APIs
    and caches to disk. Falls back to a minimal static table for providers
    without metadata APIs (Anthropic, DeepSeek).
    """
    from core.model_limits import get_model_context_limit as _get
    return _get(model)


def load_keys(keys_path: Path | None = None) -> dict:
    """Load API keys JSON. Defaults to ``data/keys.json`` (the user-state /
    gitignore boundary); pass a path for app-local stores (e.g. Notebook
    ``Apps/Notebook/keys.json``)."""
    if keys_path is None:
        _migrate_legacy_keys()
    path = keys_path or KEYS_PATH
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_keys(keys: dict, keys_path: Path | None = None) -> None:
    path = keys_path or KEYS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(keys, indent=4, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _load_agent_config_dict() -> dict:
    """Minimal read of config.json for keys that also live outside keys.json."""
    if not CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_google_api_key(keys_file_value: str = "") -> str:
    """Google AI Studio / Gemini API key: keys.json, then config.json, then env."""
    v = (keys_file_value or "").strip()
    if v:
        return v
    cfg = _load_agent_config_dict()
    v = (cfg.get("google_api_key") or "").strip()
    if v:
        return v
    return (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY") or "").strip()


# ── Cross-provider reasoning / thinking ─────────────────────────────
# In 2025 a lot more than Anthropic exposes "thinking" / "reasoning effort".
# Each provider calls the knob something different. This module normalizes
# on a single string — "off" | "low" | "medium" | "high" — and maps to the
# right payload shape per provider+model at call time.

# Regex patterns identifying model families that actually SUPPORT a reasoning
# knob. For anything else we silently no-op so toggling the UI is harmless.
_REASONING_MODEL_PATTERNS = {
    "anthropic":  [r"claude-(3\.7|opus-4|sonnet-4|opus-4-1|4-1|4-5|4-6)",
                   r"claude-(opus|sonnet)-4"],
    "openai":     [r"^o\d", r"^gpt-5", r"^gpt-4\.1"],
    "openrouter": [r".+"],  # OpenRouter accepts `reasoning` for many backends; let it decide
    "google":     [r"gemini-2\.5", r"gemini-3"],
    "deepseek":   [r"deepseek-reasoner", r"deepseek-r1"],
    "kimi":       [r"kimi-thinking", r"kimi-k2"],
    "zai":        [r"glm-4\.[6-9]", r"glm-5"],
    "alibaba":    [r"qwen3", r"qwq", r"qwen-plus", r"qwen-max"],
    "minimax":    [r"^minimax-m"],
    "huggingface": [r".+"],
    "local":      [r".+"],
}


def _effort_to_budget(effort: str, ceiling: int = 16000) -> int:
    """Map the normalized effort string to a budget_tokens int for providers
    that want a numeric budget (Anthropic, Google Gemini)."""
    effort = (effort or "").strip().lower()
    mapping = {"off": 0, "low": 2048, "medium": 8000, "high": min(16000, ceiling)}
    return mapping.get(effort, 0)


def _model_supports_reasoning(provider: str, model: str) -> bool:
    import re
    patterns = _REASONING_MODEL_PATTERNS.get(provider, [])
    m = (model or "").lower()
    return any(re.search(p, m) for p in patterns)


# ── Per-(provider, model) reasoning-effort capability map ───────────
# The UI shows exactly the levels a model actually accepts. "off" (rendered as
# "None") is always first and always available — it means "send no reasoning
# override". A model with no reasoning knob returns just ["off"], so the UI
# shows a single locked "None" choice. Levels are kept current with each
# provider's documented conventions (Anthropic effort param, OpenAI
# reasoning_effort incl. gpt-5 minimal, OpenRouter unified reasoning.effort,
# Gemini 3 low/high-only, etc.).
REASONING_LEVEL_LABELS = {
    "off": "None", "minimal": "Minimal", "low": "Low",
    "medium": "Medium", "high": "High", "xhigh": "X-High", "max": "Max",
}


def _claude_reasoning_levels(m: str) -> list[str]:
    import re
    # Reasoning-capable Claude families (effort param OR extended-thinking budget).
    if not re.search(r"(opus-4|sonnet-4|haiku-4-5|3-7-sonnet|claude-3\.7)", m):
        return ["off"]
    lv = ["off", "low", "medium", "high"]
    # The GA effort param (low|medium|high|max, +xhigh on Opus 4.7+) is supported
    # on Opus 4.5/4.6/4.7/4.8 and Sonnet 4.6. "max" is Opus-tier only.
    if re.search(r"opus-4-(5|6|7|8)", m):
        lv.append("max")
        if re.search(r"opus-4-(7|8)", m):
            lv.insert(lv.index("max"), "xhigh")
    return lv


def _openai_reasoning_levels(m: str) -> list[str]:
    import re
    # gpt-5.1+ / gpt-6: 'none' (==off) | low | medium | high
    if re.search(r"gpt-5[.\-]1|gpt-5\.[1-9]|gpt-6", m):
        return ["off", "low", "medium", "high"]
    # gpt-5 base: minimal | low | medium | high (no true "none")
    if re.search(r"gpt-5", m):
        return ["off", "minimal", "low", "medium", "high"]
    # o-series reasoning models (o1/o3/o4-mini, ...): low | medium | high
    if re.search(r"(^|[/-])o[1-9]([.\-]|$)", m):
        return ["off", "low", "medium", "high"]
    return ["off"]


def _google_reasoning_levels(m: str) -> list[str]:
    import re
    # Gemini 3 maps reasoning_effort→thinking_level and accepts only low|high.
    if re.search(r"gemini-3", m):
        return ["off", "low", "high"]
    if re.search(r"gemini-2[.\-]5", m):
        return ["off", "low", "medium", "high"]
    return ["off"]


def reasoning_levels(provider: str, model: str) -> list[str]:
    """Ordered list of reasoning-effort levels a (provider, model) pair accepts.

    Always starts with "off" (UI label "None"). A model with no reasoning
    control returns just ["off"]. Use REASONING_LEVEL_LABELS for display text."""
    import re
    p = (provider or "").strip().lower()
    m = (model or "").strip().lower()

    if p == "anthropic":
        return _claude_reasoning_levels(m)
    if p == "openai":
        return _openai_reasoning_levels(m)
    if p == "google":
        return _google_reasoning_levels(m)
    if p == "deepseek":
        # deepseek-reasoner / r1 always reason; there is no adjustable knob.
        return ["off"]
    if p == "openrouter":
        sub = m.split("/", 1)[1] if "/" in m else m
        if "claude" in sub:
            return _claude_reasoning_levels(sub)
        if re.search(r"gpt-5|gpt-6|(^|[/-])o[1-9]([.\-]|$)", sub):
            return _openai_reasoning_levels(sub)
        if "gemini" in sub:
            return _google_reasoning_levels(sub)
        # Other OpenRouter backends route through its unified reasoning.effort.
        return ["off", "low", "medium", "high"]

    def _generic(pats: list[str]) -> list[str]:
        return (["off", "low", "medium", "high"]
                if any(re.search(x, m) for x in pats) else ["off"])

    if p == "kimi":
        return _generic([r"kimi-thinking", r"kimi-k2", r"moonshot"])
    if p == "zai":
        return _generic([r"glm-4\.[6-9]", r"glm-5", r"glm-z"])
    if p == "alibaba":
        return _generic([r"qwen3", r"qwq", r"qwen-plus", r"qwen-max"])
    if p == "minimax":
        return _generic([r"minimax-m"])
    if p in ("huggingface", "local"):
        return ["off", "low", "medium", "high"]
    return ["off"]


def _build_reasoning_openai_kwargs(provider: str, model: str, effort: str) -> dict:
    """Return a dict of kwargs to merge into an OpenAI-SDK .create() call so
    the underlying API runs in reasoning mode. Empty dict if unsupported /
    disabled. Caller is responsible for merging into api_kwargs."""
    effort = (effort or "").strip().lower()
    if effort in ("", "off"):
        return {}
    if not _model_supports_reasoning(provider, model):
        return {}

    # OpenAI native: top-level reasoning_effort
    if provider == "openai":
        return {"reasoning_effort": effort}

    # OpenRouter: unified `reasoning` block — it translates per-backend
    if provider == "openrouter":
        return {"extra_body": {"reasoning": {"effort": effort}}}

    # Google Gemini via OpenAI-compat endpoint: the compat layer maps top-level
    # reasoning_effort → Gemini's thinking_level (and avoids the thinking_budget
    # vs thinking_level conflict on Gemini 3, which rejects "medium").
    if provider == "google":
        return {"reasoning_effort": effort}

    # DeepSeek / Kimi / Z.AI / Qwen / MiniMax: most route through OpenRouter's
    # shape when self-hosted. These native endpoints are provider-specific and
    # not all well-documented; send both the OpenAI-style and OpenRouter-style
    # in extra_body and let the endpoint pick what it understands.
    if provider in ("deepseek", "kimi", "zai", "alibaba", "minimax",
                    "huggingface", "local"):
        return {"extra_body": {
            "reasoning_effort": effort,
            "reasoning": {"effort": effort},
        }}

    return {}


def _anthropic_supports_effort_param(model: str) -> bool:
    """True for Claude models that take the GA `output_config.effort` parameter
    (Opus 4.5/4.6/4.7/4.8 and Sonnet 4.6). Older thinking-capable Claudes use a
    `budget_tokens` budget instead; non-reasoning Claudes use neither."""
    import re
    m = (model or "").lower()
    return bool(re.search(r"opus-4-(5|6|7|8)", m) or re.search(r"sonnet-4-6", m))


def _anthropic_effort_value(effort: str, model: str) -> str:
    """Normalize a cross-provider effort string to an Anthropic effort value.
    `max` is Opus-tier only and `xhigh` is Opus 4.7+; downgrade on other models."""
    import re
    e = (effort or "").strip().lower()
    if e == "minimal":
        e = "low"
    if e not in ("low", "medium", "high", "xhigh", "max"):
        e = "high"
    m = (model or "").lower()
    is_opus = "opus-4" in m
    if e == "max" and not is_opus:
        e = "high"
    if e == "xhigh" and not re.search(r"opus-4-(7|8)", m):
        e = "high"
    return e


# ── Anthropic → OpenAI adapter ──────────────────────────────────────
# Wraps the native Anthropic SDK so it exposes client.chat.completions.create()
# returning objects with .choices[0].message.content / .tool_calls

class _AttrDict:
    """Lightweight object that allows attribute access on a dict.
    Supports model_dump() for Pydantic compatibility with OpenAI objects."""

    def __init__(self, d: dict):
        self.__dict__.update(d)

    def __repr__(self):
        return repr(self.__dict__)

    def __getattr__(self, name):
        # Only called when normal lookup fails — catches typos early
        raise AttributeError(
            f"Response has no attribute '{name}'. "
            f"Available: {', '.join(self.__dict__.keys())}"
        )

    def model_dump(self, exclude_none: bool = False, **kwargs) -> dict:
        """Pydantic-compatible serialization (used by agent context append)."""
        out = {}
        for k, v in self.__dict__.items():
            if exclude_none and v is None:
                continue
            if isinstance(v, _AttrDict):
                out[k] = v.model_dump(exclude_none=exclude_none)
            elif isinstance(v, list):
                out[k] = [
                    x.model_dump(exclude_none=exclude_none) if isinstance(x, _AttrDict) else x
                    for x in v
                ]
            else:
                out[k] = v
        return out


def _consume_openai_stream(stream, on_delta):
    """Consume an OpenAI-style streaming response, calling ``on_delta(text)`` for
    each answer-content token, and reconstruct a non-streaming-shaped response so
    downstream code (``response.choices[0].message`` / ``.usage`` / ``.tool_calls``)
    works unchanged. Reasoning/thinking deltas are collected but NOT streamed as
    answer text. Tool-call deltas are reassembled by index."""
    content_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    model = None
    usage_obj = None

    # Providers can inject a mid-stream error (e.g. OpenRouter/DeepSeek's
    # "JSON error injected into SSE stream" — an upstream hiccup AFTER the stream
    # opened). If we already received content/tool-calls, salvage them and return
    # a coherent partial response rather than throwing away the whole turn and
    # re-streaming (which would duplicate text in the UI). If NOTHING arrived yet,
    # re-raise so the caller's retry/fallback path can cleanly try again.
    _stream_error = None
    try:
        for chunk in stream:
            if getattr(chunk, "model", None):
                model = chunk.model
            cu = getattr(chunk, "usage", None)
            if cu is not None:
                usage_obj = cu
            choices = getattr(chunk, "choices", None) or []
            if not choices:
                continue
            choice = choices[0]
            if getattr(choice, "finish_reason", None):
                finish_reason = choice.finish_reason
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            # Reasoning tokens: OpenRouter exposes delta.reasoning; some backends
            # use reasoning_content. Collected for the thinking panel, not streamed.
            r = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
            if r:
                thinking_parts.append(r)
            text = getattr(delta, "content", None)
            if text:
                content_parts.append(text)
                if on_delta:
                    try:
                        on_delta(text)
                    except Exception:
                        pass
            for tc in (getattr(delta, "tool_calls", None) or []):
                idx = getattr(tc, "index", 0) or 0
                slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                if getattr(tc, "id", None):
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["args"] += fn.arguments
    except Exception as e:
        # Mid-stream failure. Salvage if we got anything; otherwise propagate so
        # the caller can retry/fallback cleanly (nothing streamed yet → no dupes).
        if not content_parts and not tool_calls:
            raise
        _stream_error = e
        finish_reason = finish_reason or "error"

    # ── Stream end without an explicit finish_reason ──
    # We get here only when the `for chunk in stream` loop completed WITHOUT
    # raising — i.e. the server ended the stream cleanly. A genuine mid-stream
    # cutoff raises and is marked "error" above. So a clean end with content but
    # no finish_reason is a COMPLETE response whose provider simply never emitted
    # one (common for Claude-via-proxy models). It must NOT be treated as a stall:
    # doing so made the agent append a "please continue" prompt and re-call the
    # API, which produced a SECOND full reply — the duplicate-response bug. Treat
    # a clean end as a normal stop.
    if finish_reason is None and (content_parts or tool_calls):
        finish_reason = "stop"

    tc_list = None
    if tool_calls:
        tc_list = [
            _AttrDict({
                "id": tool_calls[i]["id"],
                "type": "function",
                "function": _AttrDict({
                    "name": tool_calls[i]["name"],
                    "arguments": tool_calls[i]["args"],
                }),
            })
            for i in sorted(tool_calls)
        ]

    if _stream_error is not None:
        # Salvaged a partial reply after a mid-stream provider error. Surface a
        # short notice so the user/logs know the response may be truncated.
        print(f"[providers] Stream interrupted after partial output; "
              f"salvaged {sum(len(c) for c in content_parts)} chars "
              f"({type(_stream_error).__name__}: {str(_stream_error)[:120]})",
              flush=True)

    message = _AttrDict({
        "content": "".join(content_parts),
        "tool_calls": tc_list,
        "role": "assistant",
        "refusal": None,
        "_thinking": "".join(thinking_parts) if thinking_parts else None,
    })

    pt = ct = tt = cache_read = 0
    if usage_obj is not None:
        pt = getattr(usage_obj, "prompt_tokens", 0) or 0
        ct = getattr(usage_obj, "completion_tokens", 0) or 0
        tt = getattr(usage_obj, "total_tokens", 0) or 0
        details = getattr(usage_obj, "prompt_tokens_details", None)
        if details is not None:
            cache_read = getattr(details, "cached_tokens", 0) or 0

    return _AttrDict({
        "choices": [_AttrDict({"message": message, "finish_reason": finish_reason})],
        "model": model or "",
        "usage": _AttrDict({
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": tt or (pt + ct),
            "cache_read_tokens": cache_read,
            "cache_write_tokens": 0,
        }),
    })


class _AnthropicCompletions:
    """Implements .create(**kwargs) using the Anthropic messages API."""

    _CLAUDE_CODE_IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude."

    def __init__(self, anthropic_client, is_oauth: bool = False):
        self._client = anthropic_client
        self._is_oauth = is_oauth

    def create(self, **kwargs):
        # Optional streaming hook — not an Anthropic API param, so pop it first.
        stream_callback = kwargs.pop("stream_callback", None)
        model = kwargs["model"]
        messages = kwargs.get("messages", [])
        max_tokens = kwargs.get("max_tokens", 4096)
        temperature = kwargs.get("temperature", 0.7)
        tools = kwargs.get("tools", None)
        tool_choice = kwargs.get("tool_choice", None)
        thinking_budget = kwargs.get("thinking_budget", 0)  # 0 = disabled
        effort = str(kwargs.get("reasoning_effort", "") or "").strip().lower()
        # Modern Claude (Opus 4.5+/Sonnet 4.6) take the GA effort parameter via
        # output_config + adaptive thinking, and REJECT budget_tokens/temperature.
        # Older thinking-capable models still take a fixed budget_tokens. An
        # explicit thinking_budget (legacy pin) forces the budget path.
        use_effort_param = (
            _anthropic_supports_effort_param(model) and not thinking_budget
        )
        if not use_effort_param and not thinking_budget:
            # Legacy budget path: derive a budget from the effort string.
            if effort and effort != "off" and _model_supports_reasoning("anthropic", model):
                thinking_budget = _effort_to_budget(effort, ceiling=max_tokens - 1)

        # Separate system messages and sanitize tool orphans (both directions).
        # CRITICAL: collect ALL system messages — Anthropic has a single
        # `system` field, but we may have multiple (main prompt + memory hints
        # + recall notes). Overwriting loses the main system prompt entirely.
        # We keep the first (stable, cacheable) separate from later (transient).
        clean_messages = self._strip_orphaned_tool_results(messages)
        clean_messages = self._strip_orphaned_tool_uses(clean_messages)
        system_parts: list[str] = []
        conv_messages = []
        for m in clean_messages:
            if m["role"] == "system":
                c = m.get("content", "")
                if isinstance(c, list):
                    # Flatten text blocks
                    c = "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
                if c:
                    system_parts.append(str(c))
            else:
                conv_messages.append(self._convert_message(m))

        # Anthropic OAuth (Claude Code subscription tokens) disallows assistant
        # prefill — the array MUST end with a user message. Hosts that
        # interleave system messages between the user turn and the call
        # (vispy_dashboard's <system-reminder>, librarian recall blocks, etc.)
        # can leave the last conv message as `assistant` after we strip
        # systems above. Anthropic responds with HTTP 400:
        #   "This model does not support assistant message prefill.
        #    The conversation must end with a user message."
        # Fix it here so every caller (root Agent, Hybrid, vispy_dashboard)
        # gets the same robustness. We append a minimal continuation prompt
        # rather than mutating the host's chat history.
        if conv_messages and conv_messages[-1].get("role") == "assistant":
            conv_messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": "Please continue."},
                ],
            })

        # Apply cache breakpoints to last 3 conversation messages
        # (system prompt uses breakpoint #1, these use #2-4 — Anthropic max is 4)
        self._apply_cache_breakpoints(conv_messages)

        if use_effort_param and effort and effort != "off":
            # GA effort parameter + adaptive thinking. These models reject
            # temperature and budget_tokens, so we omit both. (When no effort is
            # selected we fall through to the plain path below, preserving the
            # existing temperature behavior for non-reasoning turns.)
            #
            # `output_config` / `thinking:{type:"adaptive"}` are newer than the
            # pinned Anthropic SDK (0.52.x) knows about, so we pass them via
            # extra_body — the SDK forwards it verbatim into the request body
            # rather than rejecting it as an unknown typed kwarg.
            api_kwargs = dict(
                model=model,
                messages=conv_messages,
                max_tokens=max_tokens,
                extra_body={
                    "thinking": {"type": "adaptive"},
                    "output_config": {"effort": _anthropic_effort_value(effort, model)},
                },
            )
        elif thinking_budget > 0:
            # Legacy extended-thinking path: temperature must be 1 and
            # budget_tokens < max_tokens.
            temperature = 1
            thinking_budget = min(thinking_budget, max_tokens - 1)
            api_kwargs = dict(
                model=model,
                messages=conv_messages,
                max_tokens=max_tokens,
                temperature=temperature,
                thinking={"type": "enabled", "budget_tokens": thinking_budget},
            )
        else:
            api_kwargs = dict(
                model=model,
                messages=conv_messages,
                max_tokens=max_tokens,
                temperature=temperature,
            )

        # Build Anthropic `system` blocks. First collected part = stable main
        # prompt (gets cache_control); remaining parts = transient (memory/recall)
        # and stay uncached so we don't thrash the cache each turn.
        system_blocks: list[dict] = []
        if self._is_oauth:
            system_blocks.append({"type": "text", "text": self._CLAUDE_CODE_IDENTITY})
        if system_parts:
            # Main (stable) system prompt
            system_blocks.append({
                "type": "text", "text": system_parts[0],
                "cache_control": {"type": "ephemeral"},
            })
            # Transient system content (memory hints / recall notes) — NOT cached
            for extra in system_parts[1:]:
                system_blocks.append({"type": "text", "text": extra})
        if system_blocks:
            api_kwargs["system"] = system_blocks
        if tools:
            api_kwargs["tools"] = [self._convert_tool(t) for t in tools]
        if tool_choice and tools:
            if tool_choice == "auto":
                api_kwargs["tool_choice"] = {"type": "auto"}
            elif tool_choice == "none":
                api_kwargs["tool_choice"] = {"type": "none"}

        # Drop any kwargs previously learned to be unsupported on this model.
        api_kwargs = _strip_quirks(api_kwargs, "anthropic", model)

        def _call():
            if stream_callback is not None:
                # Native Anthropic streaming. text_stream yields answer-text
                # deltas only (thinking/tool_use blocks are accumulated by the
                # SDK); get_final_message() returns the same Message shape the
                # non-streaming path produces, so _adapt_response is unchanged.
                with self._client.messages.stream(**api_kwargs) as _s:
                    for _text in _s.text_stream:
                        try:
                            stream_callback(_text)
                        except Exception:
                            pass
                    return _s.get_final_message()
            return self._client.messages.create(**api_kwargs)

        # Up to 3 retries for learned-on-the-fly quirk stripping (one per bad kwarg).
        for _ in range(3):
            try:
                response = _call()
                break
            except Exception as e:
                err_str = str(e)

                # OAuth expiry: refresh and retry once.
                if ("401" in err_str or "authentication" in err_str.lower()
                        or "expired" in err_str.lower()):
                    wrapper = getattr(self, '_wrapper', None)
                    if wrapper and hasattr(wrapper, '_refresh_if_needed'):
                        wrapper._refresh_if_needed()
                        self._client = wrapper._client
                        response = self._client.messages.create(**api_kwargs)
                        break
                    raise

                # Learn + strip a rejected parameter, then retry.
                bad = _detect_rejected(err_str)
                if bad and bad in api_kwargs:
                    _mark_quirk("anthropic", model, bad)
                    api_kwargs.pop(bad, None)
                    continue

                raise
        else:
            # Exhausted retries without success.
            raise RuntimeError(
                f"Anthropic call failed after stripping unsupported params: {api_kwargs.keys()}"
            )
        return self._adapt_response(response)

    @staticmethod
    def _apply_cache_breakpoints(conv_messages: list[dict]) -> None:
        """Add cache_control to the last 3 conversation messages (already in
        Anthropic format).  Combined with the system-prompt breakpoint this
        uses all 4 of Anthropic's allowed cache breakpoints, giving ~75%
        input-token savings on multi-turn conversations."""
        marker = {"type": "ephemeral"}
        for msg in conv_messages[-3:]:
            content = msg.get("content")
            if isinstance(content, list) and content:
                # Last block in the content array gets the marker
                content[-1]["cache_control"] = marker
            elif isinstance(content, str) and content:
                msg["content"] = [
                    {"type": "text", "text": content, "cache_control": marker}
                ]

    @staticmethod
    def _strip_orphaned_tool_uses(messages: list[dict]) -> list[dict]:
        """Remove tool_call entries from assistant messages that have no
        matching tool_result in the conversation. Anthropic rejects an
        assistant tool_use without a later matching tool_result."""
        result_ids = set()
        for m in messages:
            if m.get("role") == "tool":
                tc_id = m.get("tool_call_id", "")
                if tc_id:
                    result_ids.add(tc_id)

        cleaned = []
        for m in messages:
            if m.get("role") == "assistant" and m.get("tool_calls"):
                kept_calls = []
                for tc in m["tool_calls"]:
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                    if tc_id in result_ids:
                        kept_calls.append(tc)
                if kept_calls:
                    new_msg = dict(m)
                    new_msg["tool_calls"] = kept_calls
                    cleaned.append(new_msg)
                else:
                    # No surviving tool calls — keep only if the msg has text
                    content = m.get("content", "")
                    has_text = (isinstance(content, str) and content.strip()) or \
                               (isinstance(content, list) and any(
                                   isinstance(p, dict) and p.get("type") == "text" and p.get("text", "").strip()
                                   for p in content))
                    if has_text:
                        new_msg = dict(m)
                        new_msg.pop("tool_calls", None)
                        cleaned.append(new_msg)
                    # else: drop entirely — empty assistant msg is invalid
            else:
                cleaned.append(m)
        return cleaned

    @staticmethod
    def _strip_orphaned_tool_results(messages: list[dict]) -> list[dict]:
        """Remove tool-result messages whose tool_use_id has no matching
        tool_call in the preceding assistant message. Anthropic rejects these."""
        cleaned = []
        # Track tool_call IDs from the most recent assistant message
        active_tc_ids: set[str] = set()
        for m in messages:
            role = m.get("role", "")
            if role == "assistant":
                # Collect tool_call IDs from this assistant turn
                active_tc_ids = set()
                for tc in (m.get("tool_calls") or []):
                    tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                    if tc_id:
                        active_tc_ids.add(tc_id)
                cleaned.append(m)
            elif role == "tool":
                # Only keep if its ID matches a tool_call we've seen
                tc_id = m.get("tool_call_id", "")
                if tc_id in active_tc_ids:
                    cleaned.append(m)
                # else: orphaned, skip it
            else:
                # user / system — reset tracking (tool pairs don't span user turns)
                active_tc_ids = set()
                cleaned.append(m)
        return cleaned

    def _convert_message(self, msg: dict) -> dict:
        """Convert OpenAI-format message to Anthropic format."""
        role = msg["role"]
        content = msg.get("content", "")

        # tool results
        if role == "tool":
            return {
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else json.dumps(content),
                }],
            }

        # assistant messages with tool_calls
        if role == "assistant" and msg.get("tool_calls"):
            blocks = []
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in msg["tool_calls"]:
                args = tc["function"]["arguments"]
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {"raw": args}
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": args,
                })
            return {"role": "assistant", "content": blocks}

        # Regular text messages — handle multimodal content arrays
        if isinstance(content, list):
            blocks = []
            for part in content:
                if part.get("type") == "text":
                    text = part.get("text", "")
                    if text:  # skip empty text blocks (Anthropic rejects them)
                        blocks.append({"type": "text", "text": text})
                elif part.get("type") == "image_url":
                    url = part["image_url"]["url"]
                    if url.startswith("data:"):
                        # Parse data URI: data:image/png;base64,<data>
                        header, b64data = url.split(",", 1)
                        media_type = header.split(":")[1].split(";")[0]
                        blocks.append({
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": b64data,
                            },
                        })
            # Anthropic rejects empty content — inject a placeholder if needed
            if not blocks:
                blocks = [{"type": "text", "text": "(empty)"}]
            return {"role": role, "content": blocks}

        # Plain string content — Anthropic rejects empty strings on any message
        return {"role": role, "content": content if content else "(empty)"}

    def _convert_tool(self, tool: dict) -> dict:
        """Convert OpenAI tool schema to Anthropic format.

        Anthropic's input_schema rejects oneOf/allOf/anyOf at the top level
        (enforced strictly as of claude-opus-4-7). Strip them — the per-branch
        required[] lists are lost, but descriptions still note which args are
        required for which operation and the model handles it fine.
        """
        func = tool.get("function", tool)
        schema = func.get("parameters", {"type": "object", "properties": {}})
        if isinstance(schema, dict) and any(
            k in schema for k in ("oneOf", "allOf", "anyOf")
        ):
            schema = {k: v for k, v in schema.items()
                      if k not in ("oneOf", "allOf", "anyOf")}
        return {
            "name": func["name"],
            "description": func.get("description", ""),
            "input_schema": schema,
        }

    def _adapt_response(self, response) -> _AttrDict:
        """Convert Anthropic response to OpenAI-shaped object."""
        text_parts = []
        tool_calls = []
        thinking_parts = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking_parts.append(block.thinking)
            elif block.type == "tool_use":
                tool_calls.append(_AttrDict({
                    "id": block.id,
                    "type": "function",
                    "function": _AttrDict({
                        "name": block.name,
                        "arguments": json.dumps(block.input),
                    }),
                }))

        # Extract cache usage if available
        usage = response.usage
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0

        message = _AttrDict({
            "content": "\n".join(text_parts) if text_parts else "",
            "tool_calls": tool_calls if tool_calls else None,
            "role": "assistant",
            "refusal": None,
            "_thinking": "\n".join(thinking_parts) if thinking_parts else None,
        })

        return _AttrDict({
            "choices": [_AttrDict({"message": message, "finish_reason": response.stop_reason})],
            "model": response.model,
            "usage": _AttrDict({
                "prompt_tokens": usage.input_tokens,
                "completion_tokens": usage.output_tokens,
                "total_tokens": usage.input_tokens + usage.output_tokens,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
            }),
        })


class _AnthropicChat:
    def __init__(self, anthropic_client, is_oauth: bool = False):
        self.completions = _AnthropicCompletions(anthropic_client, is_oauth=is_oauth)


_ANTHROPIC_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_ANTHROPIC_TOKEN_ENDPOINTS = [
    "https://console.anthropic.com/v1/oauth/token",
]


def _get_claude_code_oauth_token() -> str | None:
    """Read a fresh OAuth token from Claude Code's credentials file.
    If expired, automatically refresh using the refresh token.
    Claude Code keeps these refreshed when running, but we handle it too."""
    import json
    import time
    from pathlib import Path
    creds_path = Path.home() / ".claude" / ".credentials.json"
    if not creds_path.exists():
        return None
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken", "")
        refresh_token = oauth.get("refreshToken", "")
        expires = oauth.get("expiresAt", 0)
        if expires > 1e12:
            expires = expires / 1000  # ms to seconds

        # Token still valid
        if token and time.time() < (expires - 60):
            return token

        # Token expired — try to refresh
        if refresh_token:
            refreshed = _refresh_anthropic_oauth(refresh_token)
            if refreshed:
                # Update the credentials file so future reads get the fresh token
                oauth["accessToken"] = refreshed["access_token"]
                if refreshed.get("refresh_token"):
                    oauth["refreshToken"] = refreshed["refresh_token"]
                oauth["expiresAt"] = refreshed["expires_at_ms"]
                try:
                    creds_path.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")
                except Exception:
                    pass
                return refreshed["access_token"]

        return None
    except Exception:
        return None


def _get_openai_codex_oauth_token() -> str | None:
    """Read a ChatGPT / Codex CLI access token from ``~/.codex/auth.json`` if present.

    Schema varies by Codex version; we accept common shapes. Returns a string
    suitable as ``api_key`` for ``openai.OpenAI``, or None if not found."""
    import json
    from pathlib import Path

    path = Path.home() / ".codex" / "auth.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None

    def _pick_token(obj) -> str | None:
        if isinstance(obj, str) and len(obj) > 12:
            return obj
        if not isinstance(obj, dict):
            return None
        for k in (
            "access_token",
            "ACCESS_TOKEN",
            "id_token",
            "token",
            "OPENAI_API_KEY",
            "api_key",
        ):
            v = obj.get(k)
            if isinstance(v, str) and len(v) > 12:
                return v
        inner = obj.get("openai")
        if isinstance(inner, dict):
            for k in ("access_token", "api_key", "token"):
                v = inner.get(k)
                if isinstance(v, str) and len(v) > 12:
                    return v
        return None

    tok = _pick_token(data)
    if tok:
        return tok
    # Sometimes nested under "tokens" or first string leaf
    if isinstance(data.get("tokens"), dict):
        tok = _pick_token(data["tokens"])
        if tok:
            return tok
    return None


def _refresh_anthropic_oauth(refresh_token: str) -> dict | None:
    """Refresh an Anthropic OAuth token. Returns {access_token, refresh_token, expires_at_ms} or None."""
    import json
    import time
    import urllib.parse
    import urllib.request

    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _ANTHROPIC_OAUTH_CLIENT_ID,
    }).encode()

    for endpoint in _ANTHROPIC_TOKEN_ENDPOINTS:
        req = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "agent-app/1.0",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())
            access_token = result.get("access_token", "")
            if not access_token:
                continue
            return {
                "access_token": access_token,
                "refresh_token": result.get("refresh_token", refresh_token),
                "expires_at_ms": int(time.time() * 1000) + (result.get("expires_in", 3600) * 1000),
            }
        except Exception:
            continue
    return None


class AnthropicClientWrapper:
    """Wraps the Anthropic SDK to look like an OpenAI client.
    Supports both API keys (sk-ant-api-*) and OAuth tokens (sk-ant-oat-*).
    OAuth tokens are auto-refreshed by reading from Claude Code's credentials."""

    def __init__(self, api_key: str):
        import anthropic
        token = api_key

        # If the provided key is an OAuth token, try to get a fresh one from Claude Code
        if token.startswith("sk-ant-oat"):
            fresh = _get_claude_code_oauth_token()
            if fresh:
                token = fresh

        # OAuth tokens need Bearer auth + special headers
        if token.startswith("sk-ant-oat"):
            self._client = anthropic.Anthropic(
                auth_token=token,
                default_headers={
                    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                    "x-app": "cli",
                    "anthropic-dangerous-direct-browser-access": "true",
                },
            )
            self._is_oauth = True
        else:
            # Standard API key
            self._client = anthropic.Anthropic(api_key=token)
            self._is_oauth = False

        self.chat = _AnthropicChat(self._client, is_oauth=self._is_oauth)
        self.chat.completions._wrapper = self  # Back-reference for token refresh

    def _refresh_if_needed(self):
        """Re-read OAuth token from Claude Code if current one may be stale."""
        if not self._is_oauth:
            return
        fresh = _get_claude_code_oauth_token()
        if fresh:
            import anthropic
            self._client = anthropic.Anthropic(
                auth_token=fresh,
                default_headers={
                    "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                    "x-app": "cli",
                    "anthropic-dangerous-direct-browser-access": "true",
                },
            )
            self.chat = _AnthropicChat(self._client, is_oauth=True)
            self.chat.completions._wrapper = self


# ── OpenRouter prompt-caching wrapper ───────────────────────────────
# OpenRouter passes cache_control through to Anthropic for Claude models.
# We inject the same system_and_3 breakpoints used by the native Anthropic path.

def _inject_openrouter_cache(messages: list[dict]) -> list[dict]:
    """Deep-copy messages and add cache_control breakpoints for Claude via OpenRouter.

    OpenAI-format system messages stay as role=system; the SDK serialises
    content blocks as plain dicts, so extra keys like cache_control pass through.
    Anthropic max is 4 breakpoints: system + last 3 conversation messages.
    """
    import copy
    messages = copy.deepcopy(messages)
    marker = {"type": "ephemeral"}

    # Breakpoint 1 — system message
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, str):
                msg["content"] = [{"type": "text", "text": content,
                                   "cache_control": marker}]
            elif isinstance(content, list) and content:
                content[-1]["cache_control"] = marker
            break

    # Breakpoints 2-4 — last 3 non-system messages
    non_sys = [m for m in messages if m.get("role") != "system"]
    for msg in non_sys[-3:]:
        content = msg.get("content", "")
        if isinstance(content, str) and content:
            msg["content"] = [{"type": "text", "text": content,
                               "cache_control": marker}]
        elif isinstance(content, list) and content:
            content[-1]["cache_control"] = marker

    return messages


class _OpenRouterCompletions:
    def __init__(self, inner):
        self._inner = inner

    def create(self, **kwargs):
        if "claude" in kwargs.get("model", "").lower():
            kwargs["messages"] = _inject_openrouter_cache(
                kwargs.get("messages", []))
        return self._inner.create(**kwargs)


class _OpenRouterChat:
    def __init__(self, inner_chat):
        self.completions = _OpenRouterCompletions(inner_chat.completions)


class OpenRouterClientWrapper:
    """Wraps the OpenAI client for OpenRouter, injecting prompt caching for Claude models."""

    def __init__(self, openai_client):
        self._inner = openai_client
        self.chat = _OpenRouterChat(openai_client.chat)


# ── Generic reasoning-injecting wrapper ─────────────────────────────
# Wraps any OpenAI-SDK-shaped client and, per call, consumes the agent's
# `reasoning_effort` kwarg, translating it to the right shape for the
# underlying provider. Non-reasoning models / effort="off" pass through
# unchanged. This is layered on top of the OpenRouter wrapper so OR still
# gets prompt caching for Claude models AND reasoning routing.

class _ReasoningCompletions:
    def __init__(self, inner, provider: str):
        self._inner = inner
        self._provider = provider

    def create(self, **kwargs):
        stream_callback = kwargs.pop("stream_callback", None)
        effort = kwargs.pop("reasoning_effort", None)
        # Also tolerate reasoning_effort coming through via extra_body (older
        # callers); normalize to a single pop.
        if effort is None:
            eb = kwargs.get("extra_body") or {}
            if isinstance(eb, dict) and "reasoning_effort" in eb:
                effort = eb.pop("reasoning_effort")

        if effort and str(effort).lower() not in ("", "off"):
            model = kwargs.get("model", "")
            extra = _build_reasoning_openai_kwargs(self._provider, model, str(effort))
            # Merge extra_body carefully so we don't clobber existing keys.
            if "extra_body" in extra:
                merged = dict(kwargs.get("extra_body") or {})
                for k, v in extra["extra_body"].items():
                    # Shallow-merge dict values
                    if isinstance(v, dict) and isinstance(merged.get(k), dict):
                        merged[k] = {**merged[k], **v}
                    else:
                        merged[k] = v
                kwargs["extra_body"] = merged
                extra = {k: v for k, v in extra.items() if k != "extra_body"}
            kwargs.update(extra)

        # Apply and learn per-model unsupported-kwarg quirks.
        model = kwargs.get("model", "")
        kwargs = _strip_quirks(kwargs, self._provider, model)
        for _ in range(3):
            try:
                if stream_callback is not None:
                    return self._stream_create(kwargs, stream_callback)
                return self._inner.create(**kwargs)
            except Exception as e:
                bad = _detect_rejected(str(e))
                if bad and bad in kwargs:
                    _mark_quirk(self._provider, model, bad)
                    kwargs.pop(bad, None)
                    continue
                raise
        raise RuntimeError(
            f"{self._provider} call failed after stripping unsupported params"
        )

    def _stream_create(self, kwargs, on_delta):
        """Request a streamed completion and reconstruct a normal response.
        ``include_usage`` is only requested for backends known to support it so
        we don't trip param validation on others (usage degrades to 0 there)."""
        call_kwargs = dict(kwargs, stream=True)
        if self._provider in ("openai", "openrouter"):
            call_kwargs["stream_options"] = {"include_usage": True}
        try:
            stream = self._inner.create(**call_kwargs)
        except Exception:
            # Some endpoints reject stream_options — retry once without it.
            call_kwargs.pop("stream_options", None)
            stream = self._inner.create(**call_kwargs)
        return _consume_openai_stream(stream, on_delta)


class _ReasoningChat:
    def __init__(self, inner_chat, provider: str):
        self.completions = _ReasoningCompletions(inner_chat.completions, provider)


class ReasoningClientWrapper:
    """Wraps any OpenAI-SDK client to route `reasoning_effort` to the right
    provider-specific payload shape. Transparent for non-reasoning models."""

    def __init__(self, inner_client, provider: str):
        self._inner = inner_client
        self.chat = _ReasoningChat(inner_client.chat, provider)


# ── Client factory ──────────────────────────────────────────────────

def get_client(provider: str, credentials: dict | None = None, keys_path: Path | None = None):
    """Return an OpenAI-compatible client for the given provider.

    ``credentials`` (optional) merges over the keys file for this call only —
    use ``{"api_key": ..., "base_url": ..., "auth_mode": ...}`` so hosts like
    vispy_dashboard can pass ``dashboard_config`` agent_* fields without
    writing ``keys.json``.

    ``keys_path`` (optional) selects which keys file to read (default: ``data/keys.json``).
    """
    keys = load_keys(keys_path)
    entry = dict(keys.get(provider, {}) or {})
    if credentials:
        for k, v in credentials.items():
            if v is not None and v != "":
                entry[k] = v

    auth_mode = (entry.get("auth_mode") or "api_key").strip().lower()
    api_key = entry.get("api_key", "")
    base_url = entry.get("base_url", "")

    # Anthropic: Claude Code OAuth does not require a keys.json API key.
    if provider == "anthropic":
        if auth_mode in ("claude_code_oauth", "anthropic_oauth", "oauth"):
            token = _get_claude_code_oauth_token()
            if not token:
                raise ValueError(
                    "Anthropic OAuth (Claude Code): no token found. Install Claude Code, "
                    "run a browser login so ~/.claude/.credentials.json contains "
                    "claudeAiOauth, or switch Auth to API key in Settings > API Keys."
                )
            return AnthropicClientWrapper(api_key=token)
        if not api_key:
            raise ValueError(
                f"No API key configured for {provider}. "
                f"Add it in Settings > API Keys, or choose Claude Code OAuth."
            )
        return AnthropicClientWrapper(api_key=api_key)

    if provider == "google":
        api_key = resolve_google_api_key(api_key)
        base_url = (base_url or "").strip() or (
            "https://generativelanguage.googleapis.com/v1beta/openai"
        )

    # Local API: key is optional, default endpoint is Ollama/LM Studio
    if provider == "local":
        base_url = base_url or "http://127.0.0.1:1234/v1"
        api_key = api_key or "local"

    # OpenAI: optional Codex / ChatGPT OAuth file (~/.codex/auth.json)
    if provider == "openai":
        if auth_mode in ("openai_codex_oauth", "codex_oauth", "oauth"):
            oat = _get_openai_codex_oauth_token()
            if not oat:
                raise ValueError(
                    "OpenAI OAuth (Codex / ChatGPT): no token in ~/.codex/auth.json. "
                    "Run `codex login` (file-based credential store) or use an API key."
                )
            api_key = oat
        base_url = (base_url or "").strip() or "https://api.openai.com/v1"

    if not api_key:
        if provider == "google":
            raise ValueError(
                "No API key configured for Google GenAI. Set google_api_key in "
                "config.json, paste the key under Google GenAI in Settings > API Keys, "
                "or set the environment variable GOOGLE_API_KEY or GEMINI_API_KEY."
            )
        raise ValueError(
            f"No API key configured for {provider}. "
            f"Add it in Settings > API Keys."
        )

    headers = {}
    if provider == "openrouter":
        headers = {
            "HTTP-Referer": "https://github.com/user/agent",
            "X-Title": "Agent",
        }

    from openai import OpenAI  # lazy: keeps the ~1.3s SDK import off startup
    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers=headers or None,
    )

    # Wrap OpenRouter to inject prompt caching for Claude models
    if provider == "openrouter":
        client = OpenRouterClientWrapper(client)

    # Route `reasoning_effort` to the right per-provider payload shape.
    # Anthropic has its own native adapter that handles thinking directly,
    # so this wrapper only covers OpenAI-SDK-shaped clients.
    return ReasoningClientWrapper(client, provider)


def _provider_credential_ready(pid: str, entry: dict) -> bool:
    entry = entry or {}
    if pid == "local":
        return True
    auth = (entry.get("auth_mode") or "api_key").strip().lower()
    if pid == "anthropic" and auth in ("claude_code_oauth", "anthropic_oauth", "oauth"):
        return _get_claude_code_oauth_token() is not None
    if pid == "openai" and auth in ("openai_codex_oauth", "codex_oauth", "oauth"):
        return _get_openai_codex_oauth_token() is not None
    if pid == "google":
        g = entry.get("api_key", "")
        return bool(resolve_google_api_key(g))
    return bool((entry.get("api_key") or "").strip())


def list_providers(keys_path: Path | None = None) -> list[str]:
    """Return provider IDs that have credentials configured (API key or OAuth)."""
    keys = load_keys(keys_path)
    result: list[str] = []
    for pid in PROVIDER_INFO:
        entry = keys.get(pid) or {}
        if _provider_credential_ready(pid, entry):
            result.append(pid)
    return result
