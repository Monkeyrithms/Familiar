"""
Context compressor — model-aware safety net behind the rolling summarizer.

The rolling summarizer (core/summarizer.py) proactively compresses conversation
history between turns. This compressor is the DEFENSE layer — it runs within
the tool loop to prune old tool results when the working context approaches
the model's actual context window limit.

Two-phase approach:
  Phase 1: Prune old tool outputs (cheap, no LLM call)
  Phase 2: Fix any orphaned tool_call/tool_result pairs

Thresholds are derived from the model's real context limit, not hardcoded.
"""

import json


# ── Token-based thresholds (relative to model context limit) ────────────

# Reserve this fraction of context for the model's output + safety margin
RESERVE_FRACTION = 0.15

# Protect this many tokens of recent tool output from pruning
PRUNE_PROTECT_TOKENS = 40_000

# Don't bother pruning unless we'd recover at least this many tokens
PRUNE_MINIMUM_SAVINGS = 20_000

# Tool results shorter than this aren't worth pruning
MIN_PRUNE_LEN = 300

# Tools whose output should never be pruned (they carry persistent context)
PROTECTED_TOOLS = {"skill", "plan", "lsp"}

# Placeholder for pruned results. All pruned placeholders begin with
# PRUNED_PREFIX so the pruners can recognise an already-cleared slot
# regardless of the per-result identity tail (see _make_placeholder).
PRUNED_PREFIX = "[Cleared to save context"
PRUNED_PLACEHOLDER = "[Cleared to save context — re-call the tool if you still need this]"

# Rough chars-per-token estimate
CHARS_PER_TOKEN = 4

# ── Age-based stale-result pruning ──────────────────────────────────────
# Historically this ran on EVERY tool round and kept only the K most recent
# tool results, blanking everything older REGARDLESS of how much context room
# was actually left. On a 200K-class model that threw away ~80% of the window
# voluntarily and forced the model to re-read files it had just seen — the
# classic "read loop". It is now BUDGET-GATED (see STALE_PRUNE_TRIGGER): below
# the trigger we mutate nothing, which also keeps the KV-cache prefix stable.
#
# When it does run, it protects the most recent STALE_PROTECT_TOKENS of tool
# output (opencode parity) with KEEP_RECENT_TOOL_RESULTS as a hard count floor.
KEEP_RECENT_TOOL_RESULTS = 12

# Protect at least this many tokens of recent tool output during age pruning.
STALE_PROTECT_TOKENS = 40_000

# Age pruning only runs once the working context exceeds this fraction of the
# usable window. Below it, every recent read stays verbatim (no read loops,
# stable cache). Set to 0.0 in config to restore always-on pruning.
STALE_PRUNE_TRIGGER = 0.50

# Compact trigger as a fraction of the usable window (below hard-cap pruning).
# Fires earlier than prune_old_tool_results so the model gets a nudge before
# we start dropping things.
SOFT_COMPACT_TRIGGER = 0.70


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate from character count."""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    total += len(json.dumps(item))
        if msg.get("tool_calls"):
            total += len(json.dumps(msg["tool_calls"], default=str))
    return total // CHARS_PER_TOKEN


def _get_tool_call_info(messages: list[dict], tool_call_id: str) -> tuple[str, dict]:
    """Return (tool_name, parsed_args) for a tool result by matching its
    call_id against the assistant tool_calls. Args parse-failures yield {}."""
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id != tool_call_id:
                    continue
                if isinstance(tc, dict):
                    fn = tc.get("function", {}) or {}
                    name = fn.get("name", "") if isinstance(fn, dict) else ""
                    raw = fn.get("arguments", "") if isinstance(fn, dict) else ""
                else:
                    fnobj = getattr(tc, "function", None)
                    name = getattr(fnobj, "name", "")
                    raw = getattr(fnobj, "arguments", "")
                args: dict = {}
                if isinstance(raw, dict):
                    args = raw
                elif isinstance(raw, str) and raw.strip():
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            args = parsed
                    except (json.JSONDecodeError, TypeError):
                        args = {}
                return name, args
    return "", {}


def _get_tool_name_for_result(messages: list[dict], tool_call_id: str) -> str:
    """Find the tool name for a tool result by matching its call_id in assistant messages."""
    return _get_tool_call_info(messages, tool_call_id)[0]


def _is_pruned(content) -> bool:
    """True if this content is an already-cleared placeholder."""
    return isinstance(content, str) and content.startswith(PRUNED_PREFIX)


def _make_placeholder(messages: list[dict], tool_call_id: str) -> str:
    """Build a SELF-IDENTIFYING placeholder so the model knows exactly what was
    cleared (and can re-request it precisely) instead of re-exploring blindly."""
    name, args = _get_tool_call_info(messages, tool_call_id)
    if not name:
        return PRUNED_PLACEHOLDER
    bits = []
    for k in ("path", "file", "pattern", "query", "url", "command", "cwd"):
        v = args.get(k)
        if v:
            s = str(v)
            if len(s) > 80:
                s = s[:77] + "..."
            bits.append(f"{k}={s}")
    for k in ("offset", "limit"):
        if args.get(k) is not None:
            bits.append(f"{k}={args[k]}")
    desc = (name + " " + " ".join(bits)).strip() if bits else name
    return f"[Cleared to save context: {desc} — re-call if you still need it]"


def read_still_live(working_messages: list[dict], path: str) -> bool:
    """True if the most-recent file_read of `path` is still verbatim in context
    (not yet pruned). Used by the read-ledger to decide whether a re-read is
    genuinely redundant (content still visible) vs. necessary (already cleared)."""
    for i in range(len(working_messages) - 1, -1, -1):
        msg = working_messages[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue
        name, a = _get_tool_call_info(working_messages, msg.get("tool_call_id", ""))
        if name != "file_read" or a.get("path") != path:
            continue
        # Most recent read of this path found — live only if not a placeholder.
        return not _is_pruned(content)
    return False


def prune_old_tool_results(messages: list[dict],
                           context_limit_tokens: int = 128_000) -> tuple[list[dict], int]:
    """Replace old tool result contents with a placeholder.

    Uses token-based protection: walks backward through messages, protects
    the most recent PRUNE_PROTECT_TOKENS of tool output, then prunes
    everything older if it would save at least PRUNE_MINIMUM_SAVINGS tokens.

    Never prunes tools in PROTECTED_TOOLS.
    """
    if len(messages) < 3:
        return messages, 0

    # Walk backward, accumulate recent tool output tokens
    recent_tool_tokens = 0
    prunable = []  # (index, token_size) — candidates for pruning

    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue

        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= MIN_PRUNE_LEN:
            continue
        if _is_pruned(content):
            continue

        # Check if this tool is protected
        tool_call_id = msg.get("tool_call_id", "")
        tool_name = _get_tool_name_for_result(messages, tool_call_id)
        if tool_name in PROTECTED_TOOLS:
            continue

        token_size = len(content) // CHARS_PER_TOKEN

        if recent_tool_tokens + token_size <= PRUNE_PROTECT_TOKENS:
            # Fits in the protected zone — keep this one
            recent_tool_tokens += token_size
        else:
            # Would exceed or has exceeded the protected zone — candidate
            prunable.append((i, token_size))

    # Only prune if we'd recover enough
    total_savings = sum(size for _, size in prunable)
    if total_savings < PRUNE_MINIMUM_SAVINGS:
        return messages, 0

    # Prune
    pruned_count = 0
    for i, _ in prunable:
        msg = messages[i]
        tcid = msg.get("tool_call_id", "")
        messages[i] = {
            "role": "tool",
            "tool_call_id": tcid,
            "content": _make_placeholder(messages, tcid),
        }
        pruned_count += 1

    return messages, pruned_count


def fix_orphaned_tool_pairs(messages: list[dict]) -> list[dict]:
    """Fix messages where tool_call/tool_result pairs are broken.

    - Removes tool results with no matching tool_call in a preceding assistant msg
    - Adds stub results for assistant tool_calls with no matching result
    """
    call_ids_by_pos: dict[str, int] = {}
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id:
                    call_ids_by_pos[tc_id] = i

    cleaned = []
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id", "")
            if tc_id in call_ids_by_pos:
                cleaned.append(msg)
        else:
            cleaned.append(msg)

    calls_with_results = {msg.get("tool_call_id", "")
                          for msg in cleaned if msg.get("role") == "tool"}
    for call_id, pos in call_ids_by_pos.items():
        if call_id not in calls_with_results:
            insert_at = pos + 1
            for j in range(pos + 1, len(cleaned)):
                if cleaned[j].get("role") != "tool":
                    insert_at = j
                    break
                insert_at = j + 1
            cleaned.insert(insert_at, {
                "role": "tool",
                "tool_call_id": call_id,
                "content": PRUNED_PLACEHOLDER,
            })

    return cleaned


def prune_stale_tool_results(messages: list[dict],
                             keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
                             protect_tokens: int = STALE_PROTECT_TOKENS) -> tuple[list[dict], int]:
    """Replace old tool result contents with a self-identifying placeholder.

    Protects a recent zone defined by BOTH a count floor (``keep_recent``) and a
    token budget (``protect_tokens``): a result stays verbatim while it is within
    the most-recent ``keep_recent`` results OR still inside the ``protect_tokens``
    budget. Everything older is cleared. Caller is responsible for gating this on
    a real budget threshold (see ``compress_if_needed``) — it is no longer meant
    to run unconditionally on every round.
    """
    if len(messages) < keep_recent * 2:
        return messages, 0

    # Walk backward, protect the recent zone by count AND tokens
    seen = 0
    recent_tokens = 0
    pruned = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= MIN_PRUNE_LEN:
            continue
        if _is_pruned(content):
            continue
        tool_call_id = msg.get("tool_call_id", "")
        tool_name = _get_tool_name_for_result(messages, tool_call_id)
        if tool_name in PROTECTED_TOOLS:
            continue

        seen += 1
        tok = len(content) // CHARS_PER_TOKEN
        if seen <= keep_recent or recent_tokens + tok <= protect_tokens:
            # Within the protected recent zone (count floor or token budget)
            recent_tokens += tok
            continue
        # Older than the protected zone — prune
        messages[i] = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": _make_placeholder(messages, tool_call_id),
        }
        pruned += 1
    return messages, pruned


def compress_if_needed(messages: list[dict],
                       model: str = "",
                       max_tokens_override: int = 0,
                       config: dict | None = None) -> list[dict]:
    """Run compression layered from cheap-to-expensive.

    Two budget-gated tiers (no compression happens while there is plenty of room
    — that headroom is what stops read loops and keeps the KV-cache prefix stable):
      1. Age-based stale pruning, gated on STALE_PRUNE_TRIGGER. Keeps a recent
         zone (count floor + token budget) verbatim, clears older. Cheap, no LLM.
      2. Hard token-aware pruning + orphan repair, gated on SOFT_COMPACT_TRIGGER,
         only fires near the real context limit.

    Args:
        messages: The working message list (mutated in place).
        model: Model name for context limit lookup. Empty = use default.
        max_tokens_override: If > 0, use this instead of looking up the model.
        config: Optional config dict for runtime overrides of the knobs:
            keep_recent_tool_results, stale_prune_trigger, stale_protect_tokens.
    """
    cfg = config or {}
    keep_recent = int(cfg.get("keep_recent_tool_results", KEEP_RECENT_TOOL_RESULTS))
    stale_trigger = float(cfg.get("stale_prune_trigger", STALE_PRUNE_TRIGGER))
    protect_tokens = int(cfg.get("stale_protect_tokens", STALE_PROTECT_TOKENS))

    # Determine the actual context limit
    if max_tokens_override > 0:
        context_limit = max_tokens_override
    else:
        from core.providers import get_model_context_limit
        context_limit = get_model_context_limit(model)

    usable = int(context_limit * (1.0 - RESERVE_FRACTION))
    tokens = estimate_tokens(messages)

    # Tier 1: age-based pruning — BUDGET-GATED. Below the trigger we mutate
    # nothing, leaving every recent read verbatim (no read loops) and the
    # cache prefix intact. stale_trigger=0.0 restores always-on behaviour.
    if tokens >= int(usable * stale_trigger):
        messages, stale_pruned = prune_stale_tool_results(
            messages, keep_recent, protect_tokens)
        if stale_pruned > 0:
            messages = fix_orphaned_tool_pairs(messages)

    # Tier 2: hard budget pruning near the ceiling
    if tokens < int(usable * SOFT_COMPACT_TRIGGER):
        return messages

    messages, pruned = prune_old_tool_results(messages, context_limit)
    if pruned > 0:
        messages = fix_orphaned_tool_pairs(messages)

    return messages
