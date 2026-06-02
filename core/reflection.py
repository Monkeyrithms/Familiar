"""
Reflection / self-review directives.

Distinct from provider chain-of-thought (the `set_thinking` tool): this is a
user-steerable self-review loop. The model can produce a draft, critique it
against criteria, and silently rewrite before the user sees it ("post"
thoughts), and/or run a guided reasoning pass before answering ("pre" thoughts).

This module is the natural-language TRIGGER side: it detects when a user message
is asking for self-review and parses out {when, scope, criteria}. The actual
loop lives in core/agent.py. The model can also engage reflection explicitly via
the `reflect` tool — this just lets plain phrasing ("review your work", "don't
write my character") engage it too.

Mirrors core/clarification.py's regex/phrase-detection style.
"""

from __future__ import annotations

import re


# Phrases that ask the model to check/review its own output before sending.
_REVIEW_INTENT = re.compile(
    r"\b(review|reflect|double[\s-]?check|proof[\s-]?read|sanity[\s-]?check|"
    r"verify|validate|audit|critique|self[\s-]?review|second[\s-]?thoughts?|"
    r"think (?:about|over|through) (?:your|the) (?:response|reply|answer|work)|"
    r"make sure (?:your|the|it|you))\b",
    re.I,
)

# Roleplay / boundary phrasing that implies a post-response check even without
# the word "review" — e.g. "only play your character", "don't write my lines".
_BOUNDARY_INTENT = re.compile(
    r"\b((?:only|just) (?:play|control|speak|act as) (?:your|the) (?:character|role|part)|"
    r"do(?:n't| not) (?:write|play|control|speak|narrate|act as|decide) (?:my|the user'?s?|for me|for the user))\b",
    re.I,
)

# "before answering / before you respond" → pre-thoughts.
_BEFORE_INTENT = re.compile(
    r"\b(before (?:you )?(?:answer|respond|reply|writing|posting)|"
    r"think first|reason (?:first|through this) before)\b",
    re.I,
)

# "after / before sending / review your reply" → post-thoughts (the default).
_AFTER_INTENT = re.compile(
    r"\b(before (?:you )?(?:send|post|submit)|after (?:you )?(?:draft|write)|"
    r"review your (?:response|reply|answer|work)|check your (?:work|answer|reply))\b",
    re.I,
)

# Standing-rule phrasing → persist for the whole conversation, not just one turn.
_STANDING_RULE = re.compile(
    r"\b(from now on|going forward|for the rest of|whenever|every time|each time|"
    r"always|when(?:ever)? we(?:'re| are)? (?:roleplay|playing|rp)|"
    r"for this (?:whole |entire )?(?:conversation|session|chat|game))\b",
    re.I,
)

# Turn off an active reflection rule.
_CEASE_INTENT = re.compile(
    r"\b(stop (?:reviewing|reflecting|the review|self[\s-]?review)|"
    r"cease (?:reflect|review)|no more (?:review|reflection|second thoughts)|"
    r"turn off (?:reflect|review|self[\s-]?review)|"
    r"(?:you )?can stop (?:reviewing|reflecting))\b",
    re.I,
)


def detect_cease(user_msg: str) -> bool:
    """True if the message asks to stop an active reflection rule."""
    if not user_msg:
        return False
    return bool(_CEASE_INTENT.search(user_msg))


def detect_directive(user_msg: str) -> dict | None:
    """Parse a self-review directive from the user's message.

    Returns ``{"when": "before"|"after"|"both", "scope": "turn"|"conversation",
    "criteria": str}`` if the message engages reflection, else ``None``.
    Cease requests are handled separately via ``detect_cease``.
    """
    if not user_msg or not user_msg.strip():
        return None
    msg = user_msg.strip()

    has_review = bool(_REVIEW_INTENT.search(msg))
    has_boundary = bool(_BOUNDARY_INTENT.search(msg))
    if not (has_review or has_boundary):
        return None

    before = bool(_BEFORE_INTENT.search(msg))
    after = bool(_AFTER_INTENT.search(msg)) or has_boundary
    if before and after:
        when = "both"
    elif before and not after:
        when = "before"
    else:
        # Default to post-review — the headline behavior, and what "review your
        # work" / boundary rules imply.
        when = "after"

    scope = "conversation" if _STANDING_RULE.search(msg) else "turn"

    # The whole instruction IS the criteria — the model reads it to know what to
    # check against. Trim to keep the injected prompt lean.
    criteria = msg if len(msg) <= 600 else msg[:600] + "…"

    return {"when": when, "scope": scope, "criteria": criteria}


def pre_fragment(criteria: str) -> str:
    """Guided reasoning prompt injected BEFORE the model answers (pre-thoughts).
    Transient (wrapped as a 'reflection' fragment, stripped from memory)."""
    return (
        "REFLECTION — THINK FIRST (this turn)\n"
        "\n"
        "Before you write your reply, reason privately about how to satisfy the "
        "user's standing instruction:\n"
        f'  «{criteria}»\n'
        "\n"
        "Work through the key considerations, constraints, and any traps the "
        "instruction is guarding against. Then write ONLY your final reply — do "
        "not show this reasoning to the user."
    )


def critique_prompt(criteria: str, draft: str) -> str:
    """Prompt for the self-critique pass over a draft reply (post-thoughts)."""
    return (
        "SELF-REVIEW. You just drafted a reply (below). Critique it ONLY against "
        "this instruction — nothing else:\n"
        f'  «{criteria}»\n'
        "\n"
        "--- YOUR DRAFT ---\n"
        f"{draft}\n"
        "--- END DRAFT ---\n"
        "\n"
        "Respond with EXACTLY one of:\n"
        "  PASS  — the draft fully satisfies the instruction, send as-is.\n"
        "  REVISE: <one short line on what violates the instruction>\n"
        "Do not rewrite it here; just judge it."
    )


def rewrite_prompt(criteria: str, draft: str, critique: str) -> str:
    """Prompt to regenerate a corrected reply after a REVISE critique."""
    return (
        "Your draft reply failed self-review. Rewrite it so it fully satisfies "
        "the instruction. Keep everything that was good; fix only what's flagged.\n"
        "\n"
        f"INSTRUCTION: «{criteria}»\n"
        f"WHAT TO FIX: {critique}\n"
        "\n"
        "--- DRAFT TO FIX ---\n"
        f"{draft}\n"
        "--- END ---\n"
        "\n"
        "Output ONLY the corrected reply — no preamble, no explanation, no mention "
        "of this review."
    )
