"""
Thinking tool — let the agent adjust its own extended thinking level at runtime.
Only takes effect on the Anthropic provider with Claude 3.5+ Sonnet/Opus.
"""

import json
from core.agent import load_config, save_config
from tools.registry import registry

# Named presets: label -> budget_tokens (0 = disabled)
_PRESETS = {
    "off":     0,
    "low":     4000,
    "medium":  8000,
    "high":    16000,
    "max":     32000,
}


def set_thinking(level: str = None, budget: int = None) -> str:
    """Set extended thinking level by name or exact token budget."""
    cfg = load_config()

    if level is not None:
        level = level.lower().strip()
        if level not in _PRESETS:
            return json.dumps({
                "error": f"Unknown level '{level}'. Use: {', '.join(_PRESETS.keys())}"
            })
        budget_tokens = _PRESETS[level]
        if budget_tokens == 0:
            cfg["thinking_enabled"] = False
            cfg["thinking_budget"] = 0
            save_config(cfg)
            return json.dumps({"status": "Thinking disabled."})
        else:
            cfg["thinking_enabled"] = True
            cfg["thinking_budget"] = budget_tokens
            save_config(cfg)
            return json.dumps({
                "status": f"Thinking set to {level} ({budget_tokens:,} budget tokens)."
            })

    if budget is not None:
        if budget <= 0:
            cfg["thinking_enabled"] = False
            cfg["thinking_budget"] = 0
            save_config(cfg)
            return json.dumps({"status": "Thinking disabled."})
        cfg["thinking_enabled"] = True
        cfg["thinking_budget"] = budget
        save_config(cfg)
        return json.dumps({"status": f"Thinking enabled with {budget:,} budget tokens."})

    # Status query
    enabled = cfg.get("thinking_enabled", False)
    current_budget = cfg.get("thinking_budget", 8000)
    if not enabled:
        return json.dumps({"thinking": "off", "budget": 0})
    # Find matching preset name if any
    label = next((k for k, v in _PRESETS.items() if v == current_budget), "custom")
    return json.dumps({
        "thinking": label,
        "budget_tokens": current_budget,
        "note": "Only active on Anthropic provider with Claude 3.5+ Sonnet/Opus."
    })


registry.register(
    name="set_thinking",
    description=(
        "Enable|disable|adjust extended thinking (CoT) for Claude. "
        "level='off' → disable; 'low'|'medium'|'high'|'max' → presets. "
        "budget= exact tokens. No args → status. "
        "Only Anthropic provider + Claude 3.5+ Sonnet/Opus."
    ),
    parameters={
        "type": "object",
        "properties": {
            "level": {
                "type": "string",
                "enum": ["off", "low", "medium", "high", "max"],
                "description": "Preset. off=disable. max=32k tokens.",
            },
            "budget": {
                "type": "integer",
                "description": "Exact tokens (overrides level).",
            },
        },
    },
    execute=set_thinking,
)
