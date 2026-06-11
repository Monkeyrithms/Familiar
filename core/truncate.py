"""
Unified output truncation — head+tail strategy for tool outputs.

Instead of blindly cutting at a byte limit (losing the end of build logs,
test failures, etc.), this keeps the FIRST and LAST portions of output
with a clear truncation marker in the middle that ALSO tells the model
how to retrieve the cut portion (offset/limit pagination on file_read,
narrower grep, etc.).

Used by the agent loop to cap tool results before sending to the model.

Default cap: 12,000 chars (~3K tokens) — matches claw-code-agent's universal
ceiling. Per-tool overrides via config['tool_result_caps'] in agent.py.
"""

DEFAULT_MAX_CHARS = 12_000


# Hints surfaced inside the truncation marker for specific tools, so the
# model knows the EXACT next move when its result was clipped. Keep these
# short — they ship every time a result is truncated.
_TOOL_REREAD_HINTS = {
    "file_read": (
        "Re-read with offset+limit to page through. Or grep first to "
        "narrow to the right line range."
    ),
    "grep": (
        "Narrow with a more specific pattern, smaller `path`, or `glob` "
        "filter (e.g. only *.py)."
    ),
    "terminal": (
        "If you need the middle, re-run with redirection to a file, "
        "then file_read with offset/limit."
    ),
    "web_fetch": (
        "Re-fetch with extract_text=true, or grep the cached page for "
        "the section you need."
    ),
    "glob": (
        "Too many matches. Narrow the pattern (more path segments, "
        "specific extension)."
    ),
    "http_client": (
        "Body too large. Pass a more specific request, or use a server "
        "endpoint that returns less."
    ),
}


def truncate_output(text: str, max_chars: int = DEFAULT_MAX_CHARS,
                    head_ratio: float = 0.3,
                    tool_name: str = "") -> tuple[str, bool]:
    """Truncate text using head+tail strategy.

    Keeps the first `head_ratio` of the budget and the last `(1-head_ratio)`
    of the budget, with a marker showing how much was cut and how to recover
    the cut portion (when the tool supports it).

    Args:
        text: The full output text.
        max_chars: Maximum characters to return. Default 12K (claw-code parity).
        head_ratio: Fraction of budget for the head (0.0-1.0). Default 0.3
            because the END of output (errors, summaries, exit codes) is
            usually more useful than the beginning.
        tool_name: Optional — name of the tool whose output this is. Used to
            inject a tool-specific hint into the truncation marker so the
            model knows how to re-fetch the cut portion.

    Returns:
        (truncated_text, was_truncated) tuple.
    """
    if not text or len(text) <= max_chars:
        return text, False

    head_budget = int(max_chars * head_ratio)
    tail_budget = max_chars - head_budget - 300  # Reserve space for marker

    # Find clean line boundaries — CLAMPED: on single-giant-line content the
    # nearest newline can sit thousands of chars from the boundary, silently
    # discarding most of the budget. If snapping costs more than half the
    # budget on either end, cut mid-line instead.
    head_end = text.rfind("\n", 0, head_budget)
    if head_end == -1 or head_end < head_budget // 2:
        head_end = head_budget

    tail_start = text.find("\n", len(text) - tail_budget)
    if tail_start == -1 or tail_start > len(text) - tail_budget // 2:
        tail_start = len(text) - tail_budget

    removed = tail_start - head_end
    removed_lines = text[head_end:tail_start].count("\n")

    hint = _TOOL_REREAD_HINTS.get(tool_name, "")
    hint_line = f"\n   HINT: {hint}" if hint else ""
    marker = (
        f"\n\n... [TRUNCATED {removed:,} chars / ~{removed_lines:,} lines]"
        f"{hint_line}\n\n"
    )

    return text[:head_end] + marker + text[tail_start:], True


def truncate_tool_result(result_json: str, max_chars: int = DEFAULT_MAX_CHARS,
                         tool_name: str = "") -> str:
    """Truncate a JSON tool result string, preserving structure.

    If the result is valid JSON with an 'output' field, truncates just the
    output value. Otherwise truncates the whole string.
    """
    import json

    if len(result_json) <= max_chars:
        return result_json

    # Try to parse as JSON and truncate the output field specifically
    try:
        parsed = json.loads(result_json)
        if isinstance(parsed, dict):
            # Find the dominant string field — usually 'output' or 'content'.
            for candidate in ("output", "content", "result", "text", "body"):
                val = parsed.get(candidate)
                if isinstance(val, str) and len(val) > max_chars - 200:
                    parsed[candidate], was_truncated = truncate_output(
                        val, max_chars - 200, tool_name=tool_name)
                    if was_truncated:
                        parsed["_truncated"] = True
                    return json.dumps(parsed, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass

    # Fallback: truncate the raw string
    truncated, _ = truncate_output(result_json, max_chars, tool_name=tool_name)
    return truncated
