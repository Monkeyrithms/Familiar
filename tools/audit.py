"""
audit — agent-callable token/cost auditor.

Reads the existing per-turn DebugRecorder data and returns a structured
breakdown so the agent (or the user via the agent) can answer "where did
my tokens go?". No new instrumentation — just a smart reader over the
data we already capture.

Actions:
  - summary: high-level per-turn counts for the last N turns
  - turn:    full breakdown of a single turn including per-message sizes
  - hotspots: top-K largest messages across recent turns (the cache-busters)
  - tools:   per-tool call counts + token totals from data/tool_stats.json
"""

import json
from pathlib import Path
from tools.registry import registry


_AGENT_ROOT = Path(__file__).parent.parent
_CHARS_PER_TOKEN = 4


def _agent():
    """Lazy import to avoid circular load at registration time."""
    from core import agent as _agent_mod
    return _agent_mod


def _conv_id_from_ctx(ctx) -> str:
    """Resolve the current conversation id from agent state."""
    try:
        from core.agent import Agent  # noqa: F401
    except Exception:
        return ""
    # Walk through globals to find any active Agent instance — fall back to
    # ctx.session_id which we set in tool dispatch.
    sid = getattr(ctx, "session_id", "") if ctx else ""
    return sid or ""


def _msg_size(msg: dict) -> tuple[int, str]:
    """Return (char_count, role_label) for a single message."""
    role = msg.get("role", "?")
    content = msg.get("content", "")
    if isinstance(content, str):
        chars = len(content)
    elif isinstance(content, list):
        chars = 0
        for part in content:
            if isinstance(part, dict):
                chars += len(str(part.get("text", "")))
                # also count tool_use/tool_result inputs roughly
                if "input" in part:
                    chars += len(json.dumps(part["input"]))
                if "content" in part and isinstance(part["content"], str):
                    chars += len(part["content"])
    else:
        chars = len(str(content))
    # Tool calls in assistant messages also count
    for tc in msg.get("tool_calls", []) or []:
        try:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            chars += len(fn.get("name", ""))
            chars += len(str(fn.get("arguments", "")))
        except Exception:
            pass
    return chars, role


def _tag_message(msg: dict) -> str:
    """Best-effort label: 'system', 'user', 'assistant', 'tool:<name>'."""
    role = msg.get("role", "?")
    if role == "tool":
        # Walk back to find which tool — tool_call_id matches an earlier assistant call
        return f"tool:{msg.get('name', '?')}"
    if role == "assistant" and msg.get("tool_calls"):
        names = []
        for tc in msg["tool_calls"]:
            try:
                fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                names.append(fn.get("name", "?"))
            except Exception:
                pass
        if names:
            return f"assistant→tool({','.join(names[:3])})"
    return role


def _resolve_tool_msg_names(messages: list) -> list:
    """Backfill 'name' on tool messages by matching tool_call_id from prior
    assistant messages. Returns a new list with annotated messages (does not
    mutate the originals)."""
    # Build id → tool name map
    id_to_name = {}
    for m in messages:
        if m.get("role") == "assistant":
            for tc in m.get("tool_calls", []) or []:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    id_to_name[tc.get("id", "")] = fn.get("name", "?")
    out = []
    for m in messages:
        if m.get("role") == "tool":
            mm = dict(m)
            mm.setdefault("name", id_to_name.get(m.get("tool_call_id", ""), "?"))
            out.append(mm)
        else:
            out.append(m)
    return out


def _summarize_turn(turn: dict) -> dict:
    """Compact per-turn summary."""
    totals = turn.get("totals") or {}
    steps = turn.get("steps") or []
    base_ctx = turn.get("base_context") or []

    # Largest message in any step's context
    largest = {"role": "", "tag": "", "tokens": 0}
    for step in steps:
        for m in step.get("context") or []:
            chars, role = _msg_size(m)
            est_tokens = chars // _CHARS_PER_TOKEN
            if est_tokens > largest["tokens"]:
                largest = {
                    "role": role,
                    "tag": _tag_message(m),
                    "tokens": est_tokens,
                }

    return {
        "turn_id": turn.get("id"),
        "started_at": turn.get("started_at"),
        "model": turn.get("model_name"),
        "user_message_preview": (turn.get("user_message") or "")[:120],
        "steps": len(steps),
        "tokens_in": totals.get("tokens_context", 0),
        "tokens_out": totals.get("tokens_response", 0),
        "tokens_all": totals.get("tokens_all", 0),
        "base_context_messages": len(base_ctx),
        "largest_msg": largest,
        "error": turn.get("error"),
    }


def audit(action: str, n: int = 5, turn_index: int = -1, top_k: int = 10,
          ctx=None) -> str:
    """Inspect per-turn tokens + tool sizes from DebugRecorder."""
    from core.debug_recorder import debug_recorder

    # Best-effort conversation id resolution.
    conv_id = _conv_id_from_ctx(ctx)
    # Final fallback: probe the singleton's most populated bucket
    if not conv_id:
        try:
            buckets = getattr(debug_recorder, "_by_conv", {}) or {}
            if buckets:
                conv_id = max(buckets.items(), key=lambda kv: len(kv[1]))[0]
                if conv_id == "__no_conv__":
                    conv_id = ""
        except Exception:
            pass

    if action == "summary":
        # Per-turn high-level for the last N turns
        count = debug_recorder.turn_count(conv_id)
        if count == 0:
            return json.dumps({"error": "No debug turns recorded for this conversation."})
        n = max(1, min(int(n), count))
        turns = []
        for i in range(count - n, count):
            t = debug_recorder.get_turn(i, conv_id)
            if t:
                turns.append(_summarize_turn(t))
        # Aggregate
        agg_in = sum(t["tokens_in"] for t in turns)
        agg_out = sum(t["tokens_out"] for t in turns)
        return json.dumps({
            "conversation_id": conv_id,
            "turn_count": count,
            "showing_last": len(turns),
            "aggregate_tokens_in": agg_in,
            "aggregate_tokens_out": agg_out,
            "aggregate_tokens_all": agg_in + agg_out,
            "turns": turns,
        }, ensure_ascii=False)

    elif action == "turn":
        # Full per-message breakdown for one turn
        idx = int(turn_index)
        t = debug_recorder.get_turn(idx if idx >= 0 else
                                     debug_recorder.turn_count(conv_id) + idx,
                                     conv_id)
        if not t:
            return json.dumps({"error": f"No turn at index {turn_index}"})

        # Compute per-message size for the LAST step's context (richest snapshot)
        steps = t.get("steps") or []
        if not steps:
            return json.dumps({"error": "Turn has no steps recorded."})
        last_ctx = _resolve_tool_msg_names(steps[-1].get("context") or [])
        per_msg = []
        for i, m in enumerate(last_ctx):
            chars, _role = _msg_size(m)
            per_msg.append({
                "i": i,
                "tag": _tag_message(m),
                "chars": chars,
                "est_tokens": chars // _CHARS_PER_TOKEN,
            })
        # Sort biggest-first
        per_msg.sort(key=lambda x: x["est_tokens"], reverse=True)

        return json.dumps({
            "turn_id": t.get("id"),
            "started_at": t.get("started_at"),
            "model": t.get("model_name"),
            "user_message_preview": (t.get("user_message") or "")[:200],
            "steps": len(steps),
            "totals": t.get("totals"),
            "error": t.get("error"),
            "messages_in_final_context": len(last_ctx),
            "per_message_sizes": per_msg[:30],
            "_note": "per_message_sizes sorted descending by est_tokens (top 30 only).",
        }, ensure_ascii=False)

    elif action == "hotspots":
        # Top-K largest messages across the last N turns — the cache-busters
        count = debug_recorder.turn_count(conv_id)
        if count == 0:
            return json.dumps({"error": "No debug turns recorded."})
        n = max(1, min(int(n), count))
        seen = []  # list of (tokens, tag, turn_index, msg_index)
        for ti in range(count - n, count):
            t = debug_recorder.get_turn(ti, conv_id)
            if not t:
                continue
            for step in t.get("steps") or []:
                resolved = _resolve_tool_msg_names(step.get("context") or [])
                for mi, m in enumerate(resolved):
                    chars, _ = _msg_size(m)
                    if chars < 1000:  # skip noise
                        continue
                    seen.append({
                        "turn_index": ti,
                        "msg_index": mi,
                        "tag": _tag_message(m),
                        "chars": chars,
                        "est_tokens": chars // _CHARS_PER_TOKEN,
                    })
        seen.sort(key=lambda x: x["est_tokens"], reverse=True)
        return json.dumps({
            "scanned_turns": n,
            "top_messages": seen[: max(1, int(top_k))],
            "_note": (
                "These are the largest messages re-sent across the scanned turns. "
                "Each one shipped on every turn after it appeared until pruned/summarized. "
                "If 'tag' looks like 'tool:<name>' those are the offending tool results."
            ),
        }, ensure_ascii=False)

    elif action == "tools":
        # Cumulative per-tool stats from data/tool_stats.json
        from core import tool_stats
        all_stats = tool_stats.get_all()
        totals = tool_stats.totals()
        # Sort by tokens_out descending — that's what's flooding context
        rows = sorted(
            ({"tool": k, **v} for k, v in all_stats.items()),
            key=lambda r: r.get("tokens_out", 0),
            reverse=True,
        )
        return json.dumps({
            "totals": totals,
            "tools": rows,
            "_note": (
                "Cumulative across all sessions (see data/tool_stats.json). "
                "tokens_out = tool result size — the main contributor to ongoing "
                "context bloat across turns. Sort by tokens_out to find the worst offenders."
            ),
        }, ensure_ascii=False)

    return json.dumps({
        "error": f"Unknown action: {action}. Use: summary, turn, hotspots, tools."
    })


registry.register(
    name="audit",
    description=(
        "Token usage audit. "
        "summary: per-turn totals [N=5]. "
        "turn: per-msg sizes bigfirst [idx=-1]. "
        "hotspots: heaviest msgs across turns. "
        "tools: cumulative per-tool stats; sort tokens_out to find ctx floods."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["summary", "turn", "hotspots", "tools"],
            },
            "n": {
                "type": "integer",
                "description": "summary|hotspots: how many recent turns to scan. Default 5.",
            },
            "turn_index": {
                "type": "integer",
                "description": "turn: index of turn to inspect. -1 = latest. Default -1.",
            },
            "top_k": {
                "type": "integer",
                "description": "hotspots: how many top messages to return. Default 10.",
            },
        },
        "required": ["action"],
    },
    execute=audit,
)
