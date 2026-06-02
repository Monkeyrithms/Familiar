"""
Context-aware clarification mode ("deep-interview").

When the user opens a turn with a non-trivial coding ask, inject a one-shot
system fragment telling the model to interview scope BEFORE writing code.
When the turn is anything else (chat, a pointed one-line fix, an answer to
our earlier question, a pasted stack trace to diagnose), inject nothing.

Transient by design: the fragment is attached to working_messages for this
turn only — never persisted to self.context — so it doesn't accumulate or
pollute future turns where it no longer applies.
"""

from __future__ import annotations

import re


# Verbs that signal "the user wants something built / changed". Matched as
# whole words, case-insensitive. Broad on purpose — we'd rather ask one
# unnecessary question than skip the interview on a real project.
_CODING_VERBS = re.compile(
    r"\b(implement|build|design|develop|scaffold|architect|bootstrap|prototype|"
    r"create|add|make|write|set\s+up|stand\s+up|spin\s+up|"
    r"refactor|restructure|redesign|rewrite|modernize|"
    r"migrate|port|convert|integrate|wire|hook)\b",
    re.I,
)

# "Build me a / create the / add a new" — strong build intent even inside
# what otherwise looks like a question ("Can you build me a ...?").
_STRONG_BUILD_INTENT = re.compile(
    r"\b(build|implement|create|add|make|write|refactor|design)\s+"
    r"(me|a|an|the|some|new)\b",
    re.I,
)

# Info-seeking question shapes — suppress clarification when the turn is
# primarily "explain to me", unless there's a strong build intent inside.
_INFO_QUESTION_START = re.compile(
    r"^\s*(what|how|why|who|where|when|does|is|are|was|were|will|would|"
    r"should|could|can\s+you\s+(explain|describe|show|tell|walk|clarify))\b",
    re.I,
)

# Words that tell us to skip the interview — strong opt-out.
_OPT_OUT_PHRASES = (
    "just do",
    "just go",
    "no questions",
    "don't ask",
    "dont ask",
    "skip questions",
    "skip the interview",
    "without asking",
    "go ahead",
    "no interview",
    "no preamble",
    "straight to it",
    "just implement",
    "just build",
    "just make",
)

# Pointed, specific edits — user already nailed scope for us.
_POINTED_PATTERNS = [
    r"\bline\s+\d+\b",                           # "line 42"
    r"\b[\w/.]+:\d+(?:-\d+)?\b",                 # "file.py:17" / ":17-25"
    r"\bL\d+(?:-L?\d+)?\b",                      # "L42-L51"
    r"\bchange\s+[\"'`].+?[\"'`]\s+to\b",         # "change 'foo' to 'bar'"
    r"\breplace\s+[\"'`].+?[\"'`]\s+with\b",      # "replace 'x' with 'y'"
    r"\brename\s+\w+\s+to\s+\w+\b",              # "rename foo to bar"
]


CLARIFICATION_FRAGMENT = (
    "CLARIFICATION MODE (this turn only)\n"
    "\n"
    "The user just asked for a non-trivial coding task. BEFORE writing any "
    "code or calling any mutation tool (file_write / file_edit / apply_patch / "
    "multi_edit / terminal-that-mutates), verify you know all four of:\n"
    "\n"
    "1. TARGETS  — which specific files, modules, or components change.\n"
    "2. SUCCESS  — how the user will recognize it's done: tests that must "
    "pass, behavior they'll observe, a demo, a screenshot.\n"
    "3. CONSTRAINTS — any existing behavior that MUST NOT change, compat "
    "requirements, perf bounds, dependencies not to touch.\n"
    "4. EDGE CASES — the inputs or states a naive attempt would get wrong.\n"
    "\n"
    "If two or more of those are unclear from what the user said, your ENTIRE "
    "response THIS TURN must be a short numbered question list (2-5 items) in "
    "order of which answer would most change your approach. No code, no plan, "
    "no tool calls — just the questions and a one-line reason the answer "
    "matters (e.g. '— tells me whether to extend the existing X or build a new one').\n"
    "\n"
    "If three or four are clear and only one detail is fuzzy, state the "
    "assumption you'll make, then proceed ('I'll assume X unless you say "
    "otherwise — starting now.').\n"
    "\n"
    "If all four are clear (the user gave specific files, a success criterion, "
    "and constraints), skip the interview and execute.\n"
    "\n"
    "Read-only investigation (file_read, grep, glob, project_loader) before "
    "asking is fine and encouraged — you may already find the answers in the "
    "code itself. A mix is fine: investigate, then ask only the questions "
    "investigation couldn't answer."
)


def _last_assistant_text(context: list[dict] | None) -> str:
    """Return the most recent assistant text from conversation context, or ''."""
    if not context:
        return ""
    for m in reversed(context):
        if m.get("role") != "assistant":
            continue
        if m.get("tool_calls"):
            continue
        content = m.get("content") or ""
        if isinstance(content, list):
            parts = [c.get("text", "") for c in content
                     if isinstance(c, dict) and c.get("type") == "text"]
            content = " ".join(parts)
        if content and content.strip():
            return content
    return ""


def should_clarify(user_msg: str, context: list[dict] | None = None) -> bool:
    """Decide whether to inject the clarification fragment this turn."""
    if not user_msg:
        return False
    msg = user_msg.strip()
    low = msg.lower()
    words = msg.split()

    # Too short — there's no scope left to narrow
    if len(words) < 6:
        return False
    # Code / stack trace pasted — user brought their own context
    if "```" in msg:
        return False
    # Explicit opt-out
    if any(p in low for p in _OPT_OUT_PHRASES):
        return False
    # Pointed edit — already specific
    for p in _POINTED_PATTERNS:
        if re.search(p, low):
            return False
    # Info-seeking question ("what does X do?") without a build intent
    # — don't interrogate someone who just wants an explanation.
    if _INFO_QUESTION_START.match(low) and not _STRONG_BUILD_INTENT.search(low):
        return False
    # They're probably answering our own question from last turn
    last = _last_assistant_text(context)
    if last and last.rstrip().endswith("?") and len(words) < 40:
        return False

    # Actual trigger: coding verb present
    return bool(_CODING_VERBS.search(low))


def maybe_fragment(user_msg: str, context: list[dict] | None = None) -> str:
    """Return the clarification fragment if this turn needs it, else ''."""
    if should_clarify(user_msg, context):
        return CLARIFICATION_FRAGMENT
    return ""
