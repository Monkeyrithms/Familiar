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

# Placeholder for pruned results
PRUNED_PLACEHOLDER = "[Previous tool output cleared to save context space]"

# Rough chars-per-token estimate
CHARS_PER_TOKEN = 4

# ── Age-based stale-result pruning ──────────────────────────────────────
# These run cheaply on EVERY tool round, not just when near the limit.
# Keep the most recent K unprotected tool results in full; trim everything
# older. This is what Codex calls "reference context diffing" in spirit —
# old activity doesn't need to stay verbatim if you haven't referenced it
# in several turns.
KEEP_RECENT_TOOL_RESULTS = 6

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


def _get_tool_name_for_result(messages: list[dict], tool_call_id: str) -> str:
    """Find the tool name for a tool result by matching its call_id in assistant messages."""
    for msg in messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
                if tc_id == tool_call_id:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        return fn.get("name", "") if isinstance(fn, dict) else ""
                    return getattr(getattr(tc, "function", None), "name", "")
    return ""


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
        if content == PRUNED_PLACEHOLDER:
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
        messages[i] = {
            "role": "tool",
            "tool_call_id": msg.get("tool_call_id", ""),
            "content": PRUNED_PLACEHOLDER,
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
                             keep_recent: int = KEEP_RECENT_TOOL_RESULTS) -> tuple[list[dict], int]:
    """Replace old tool result contents with a placeholder based on AGE.

    Unlike ``prune_old_tool_results`` which only runs when the context is
    near its limit, this runs on EVERY round. It keeps the ``keep_recent``
    most recent (non-protected, non-tiny) tool results in full and trims
    everything older. Cheap, no LLM call, prevents stale tool spam.
    """
    if len(messages) < keep_recent * 2:
        return messages, 0

    # Walk backward, count unprotected tool results
    seen = 0
    pruned = 0
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content", "")
        if not isinstance(content, str) or len(content) <= MIN_PRUNE_LEN:
            continue
        if content == PRUNED_PLACEHOLDER:
            continue
        tool_call_id = msg.get("tool_call_id", "")
        tool_name = _get_tool_name_for_result(messages, tool_call_id)
        if tool_name in PROTECTED_TOOLS:
            continue

        seen += 1
        if seen <= keep_recent:
            # Within the keep-zone — leave it alone
            continue
        # Older than the keep-zone — prune
        messages[i] = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": PRUNED_PLACEHOLDER,
        }
        pruned += 1
    return messages, pruned


def compress_if_needed(messages: list[dict],
                       model: str = "",
                       max_tokens_override: int = 0) -> list[dict]:
    """Run compression layered from cheap-to-expensive.

    Three tiers:
      1. Age-based stale pruning (runs every call — keeps only the N most
         recent tool outputs in full). Cheap, no LLM call.
      2. Token-aware pruning (fires above SOFT_COMPACT_TRIGGER — head+tail
         truncation of older tool outputs when budget gets tight).
      3. Hard pruning + orphan repair (fires at the real context limit).

    Args:
        messages: The working message list (mutated in place).
        model: Model name for context limit lookup. Empty = use default.
        max_tokens_override: If > 0, use this instead of looking up the model.
    """
    # Tier 1: age-based pruning — always on, prevents stale spam
    messages, stale_pruned = prune_stale_tool_results(messages)
    if stale_pruned > 0:
        messages = fix_orphaned_tool_pairs(messages)

    # Determine the actual context limit
    if max_tokens_override > 0:
        context_limit = max_tokens_override
    else:
        from core.providers import get_model_context_limit
        context_limit = get_model_context_limit(model)

    usable = int(context_limit * (1.0 - RESERVE_FRACTION))
    tokens = estimate_tokens(messages)

    # Tier 2/3: budget-based pruning
    if tokens < int(usable * SOFT_COMPACT_TRIGGER):
        return messages

    messages, pruned = prune_old_tool_results(messages, context_limit)
    if pruned > 0:
        messages = fix_orphaned_tool_pairs(messages)

    return messages
