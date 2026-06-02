"""
Model-family behavioral corrections — short injection blocks that
compensate for known weaknesses in different model families.

These are NOT full system prompts (that's the user's config). These are
targeted corrections appended to the system prompt based on which model
family is active. Each block addresses specific failure modes observed
in that model family.

To fine-tune further, edit the blocks below or add new model families.
"""


# ── Behavioral correction blocks ────────────────────────────────────────
# Each block should be short (5-15 lines) and focus on CORRECTING known
# failure modes, not on personality or general instructions.

_CORRECTIONS = {
    "gpt-4": """
GPT-4/o-series:
- Persist until task done. \u2717 Stop mid-task asking "continue?".
- Error \u2192 fix and continue. \u2717 Present error and wait.
- \u2717 Placeholder comments ("// ... rest of code"). Write complete code.
""",

    "o1": """
o1/o3/o4 reasoning models:
- Plan first, then act with tools. Reasoning alone doesn't change state.
- Persist through full task. \u2717 Confirm mid-task unless truly ambiguous.
""",

    "claude": """
Claude:
- Multi-step task \u2192 plan, work through it systematically.
- Prefer editing existing code over rewriting files.
- Clear request \u2192 act immediately. \u2717 Ask for confirmation.
""",

    "gemini": """
Gemini:
- \u2717 Assume library available. Verify via package.json/requirements.txt before importing.
- Read surrounding code before editing \u2014 match style exactly.
- \u2717 Revert changes unless user asks. Fix forward.
- JSON tool args: verify escaping and bracket balance.
""",

    "deepseek": """
DeepSeek:
- Provide ALL required params in tool calls. Missing params = failure.
- Respond concisely. \u2717 Repeat user's Q or explain intent \u2014 just do it.
""",

    "qwen": """
Qwen:
- Valid JSON in all tool args. Check brackets and string escaping.
- Tool fails \u2192 read error, try different approach. \u2717 Retry same call.
- Persist through task. Complete fully before responding.
""",

    "llama": """
Llama:
- One complete tool call at a time with valid JSON args.
- Focused, concise responses.
""",

    "mistral": """
Mistral:
- Complete, well-formed JSON in all tool args.
- Persist through task. \u2717 Stop for confirmation unless genuinely needed.
""",

    "_default": """
Critical (all models):
- Trust the system prompt's platform, OS, and workspace paths \u2014 don't guess others.
- "No such file/directory" \u2192 wrong path. Check workspace list, \u2717 retry with guessed path.
- Persist through tasks. Complete fully before responding.
""",
}


def get_behavior_block(model: str) -> str:
    """Return the behavioral correction block for a model, or empty string.

    Matches on model ID substrings. Checks in specificity order so
    'gpt-4o' matches 'gpt-4' and 'o1-preview' matches 'o1'.
    """
    if not model:
        return ""

    model_lower = model.lower()

    # Check in specificity order (most specific first)
    # o-series before gpt-4 (o1/o3/o4 are distinct from base gpt-4)
    if any(x in model_lower for x in ("o1", "o3", "o4-mini")):
        return _CORRECTIONS["o1"]
    if any(x in model_lower for x in ("gpt-4", "gpt-3.5")):
        return _CORRECTIONS["gpt-4"]
    if "claude" in model_lower:
        return _CORRECTIONS["claude"]
    if "gemini" in model_lower:
        return _CORRECTIONS["gemini"]
    if "deepseek" in model_lower:
        return _CORRECTIONS["deepseek"]
    if "qwen" in model_lower:
        return _CORRECTIONS["qwen"]
    if "llama" in model_lower:
        return _CORRECTIONS["llama"]
    if "mistral" in model_lower or "mixtral" in model_lower:
        return _CORRECTIONS["mistral"]

    # OpenRouter model IDs include provider prefix: "openai/gpt-4o", "anthropic/claude-..."
    for key in _CORRECTIONS:
        if key.startswith("_"):
            continue  # skip meta-keys like _default
        if key in model_lower:
            return _CORRECTIONS[key]

    # Unknown model family — return the generic safety block
    return _CORRECTIONS["_default"]
