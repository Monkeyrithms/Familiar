"""
Context fragments — scaffolding content injected into the conversation
that should NOT feed into rolling summaries or memory commits.

Examples: recalled memory notes, workspace file surveys, AGENTS.md contents,
project skill inventories, environmental hints. These are useful to the
model RIGHT NOW but pollute the summarizer if they get rolled into "what
happened in this conversation."

Each fragment is wrapped with matching start/end markers. Callers:
- ``wrap(name, text)`` to build one
- ``strip_all(text)`` to remove every fragment block from a blob
- ``contains(text)`` to check whether a blob is purely fragment content
"""

import re

# Marker pairs — keep stable across releases; don't rename without migrating
# any long-lived stored content that used the old names.
FRAGMENTS: dict[str, tuple[str, str]] = {
    "workspace_context": ("<<<WORKSPACE_CONTEXT>>>", "<<<END_WORKSPACE_CONTEXT>>>"),
    "recall_notes":      ("<<<RECALL_NOTES>>>",      "<<<END_RECALL_NOTES>>>"),
    "agents_md":         ("<<<AGENTS_MD>>>",         "<<<END_AGENTS_MD>>>"),
    "env_info":          ("<<<ENV_INFO>>>",          "<<<END_ENV_INFO>>>"),
    "skill_inventory":   ("<<<SKILL_INVENTORY>>>",   "<<<END_SKILL_INVENTORY>>>"),
    "project_survey":    ("<<<PROJECT_SURVEY>>>",    "<<<END_PROJECT_SURVEY>>>"),
    "clarification":     ("<<<CLARIFICATION>>>",     "<<<END_CLARIFICATION>>>"),
    "reflection":        ("<<<REFLECTION>>>",        "<<<END_REFLECTION>>>"),
}

# Pre-compiled regex that matches any fragment block (DOTALL for newlines)
_ALL_BLOCKS = re.compile(
    "|".join(
        re.escape(s) + r".*?" + re.escape(e) for s, e in FRAGMENTS.values()
    ),
    re.DOTALL,
)


def wrap(name: str, text: str) -> str:
    """Wrap *text* with the named fragment's markers."""
    if name not in FRAGMENTS:
        raise KeyError(f"Unknown fragment: {name}. Known: {', '.join(FRAGMENTS)}")
    start, end = FRAGMENTS[name]
    return f"{start}\n{text}\n{end}"


def strip_all(text: str) -> str:
    """Remove every fragment block from *text*. Used before summarization
    and memory commits so scaffolding content doesn't pollute long-term state."""
    if not text:
        return text
    return _ALL_BLOCKS.sub("", text).strip()


def contains(text: str) -> bool:
    """Return True if *text* contains any fragment markers."""
    if not text:
        return False
    return any(start in text for start, _ in FRAGMENTS.values())


def is_pure_fragment(text: str) -> bool:
    """Return True if *text* is entirely fragment content (nothing meaningful
    would survive stripping). Used to drop whole messages that are scaffolding."""
    if not contains(text):
        return False
    return not strip_all(text)
