"""
reflect — let the agent engage a self-review / reflection loop.

Distinct from set_thinking (provider chain-of-thought). This designates
synthetic reasoning stages around the agent's own reply:

  when="before"  → a guided reasoning pass before answering (pre-thoughts)
  when="after"   → critique the drafted reply against `criteria` and silently
                   rewrite until it passes, before the user sees it (post-thoughts)
  when="both"    → both

  scope="turn"          → just the next response
  scope="conversation"  → persist until ceased (survives reload)

The user only sees the FINAL reply (post-review rewrites are silent — cleanest
with token streaming off). Call with action="cease" to turn an active rule off.
"""

import json

from tools.registry import registry


def reflect(when: str = "after", scope: str = "turn",
            criteria: str = "", action: str = "enable") -> str:
    from core.agent import current_agent
    agent = current_agent()
    if agent is None:
        return json.dumps({"error": "No active agent to attach reflection to."})

    if action == "cease":
        agent.clear_reflection()
        return json.dumps({"status": "Reflection ceased."})

    when = (when or "after").lower().strip()
    if when not in ("before", "after", "both"):
        when = "after"
    scope = (scope or "turn").lower().strip()
    if scope not in ("turn", "conversation"):
        scope = "turn"
    if not (criteria or "").strip():
        return json.dumps({
            "error": "criteria is required — describe what to check the response "
                     "against (e.g. \"don't write the user's character\")."
        })

    agent.set_reflection(when, scope, criteria)
    phase = {"before": "before answering",
             "after": "after drafting (silent rewrite)",
             "both": "before and after"}[when]
    return json.dumps({
        "status": f"Reflection on: {phase}, scope={scope}.",
        "note": "Post-review rewrites are silent; the user sees only the final reply.",
    })


registry.register(
    name="reflect",
    description=(
        "Engage a self-review loop around your OWN reply (NOT provider CoT — "
        "that's set_thinking). Use when the user asks you to review/check your "
        "work, or sets a boundary you must self-enforce (e.g. 'only play your "
        "character, rewrite if you write mine').\n"
        "- when: 'before' (reason first), 'after' (draft → critique → silently "
        "rewrite until it passes, default), or 'both'.\n"
        "- scope: 'turn' (next reply only) or 'conversation' (until ceased).\n"
        "- criteria: what to check against (required).\n"
        "- action='cease' to turn an active rule off.\n"
        "Post-review is silent — the user sees only the final reply."
    ),
    parameters={
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "enum": ["before", "after", "both"],
                "description": "Reasoning stage. Default 'after'.",
            },
            "scope": {
                "type": "string",
                "enum": ["turn", "conversation"],
                "description": "'turn' = next reply; 'conversation' = until ceased.",
            },
            "criteria": {
                "type": "string",
                "description": "What to check the response against. Required unless action=cease.",
            },
            "action": {
                "type": "string",
                "enum": ["enable", "cease"],
                "description": "'cease' turns an active reflection rule off.",
            },
        },
    },
    execute=reflect,
)
