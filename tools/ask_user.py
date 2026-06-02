"""
ask_user_question — let the agent pause and ask the user a structured,
multiple-choice question instead of guessing on ambiguous requests.

The UI (ChatWindow) registers a callback via set_question_callback(). When the
agent calls this tool, we hand the validated question spec to that callback,
which raises an in-place answer board where the composer normally sits, blocks
until the user submits (or cancels / aborts), and returns their selections.

Registered as `ask_user_question`; the agent's fuzzy tool-name repair maps
common permutations (ask_user, AskUserQuestion, user_query, ask_question, …)
onto it, and ALIASES below documents the intent.
"""

import json

from tools.registry import registry


# The UI installs this. Signature: callback(questions: list[dict]) -> dict | None
# Returns {question_text: answer} on submit, or None if cancelled/aborted.
_question_callback = None

# Call-sign permutations the model might emit; all resolve to this one tool.
ALIASES = (
    "ask_user_question", "ask_user", "askuserquestion", "user_query",
    "ask_question", "ask", "clarify", "question_user",
)

MAX_QUESTIONS = 4
MAX_OPTIONS = 4


def set_question_callback(fn):
    """Install the UI handler that presents questions and returns answers.
    Called once by ChatWindow at startup (same pattern as set_approval_callback)."""
    global _question_callback
    _question_callback = fn


def _normalize(questions) -> tuple[list, str]:
    """Coerce the model's input into a clean spec. Returns (questions, error)."""
    if not isinstance(questions, list) or not questions:
        return [], "questions must be a non-empty list"
    if len(questions) > MAX_QUESTIONS:
        questions = questions[:MAX_QUESTIONS]

    clean = []
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            return [], f"question {i + 1} is not an object"
        text = (q.get("question") or "").strip()
        if not text:
            return [], f"question {i + 1} missing 'question' text"
        raw_opts = q.get("options") or []
        if not isinstance(raw_opts, list) or len(raw_opts) < 2:
            return [], f"question {i + 1} needs at least 2 options"
        opts = []
        for o in raw_opts[:MAX_OPTIONS]:
            if isinstance(o, str):
                opts.append({"label": o, "description": ""})
            elif isinstance(o, dict) and o.get("label"):
                opts.append({"label": str(o["label"]),
                             "description": str(o.get("description", ""))})
        if len(opts) < 2:
            return [], f"question {i + 1} needs at least 2 valid options"
        clean.append({
            "header": str(q.get("header", ""))[:24],
            "question": text,
            "options": opts,
            "multiSelect": bool(q.get("multiSelect", False)),
        })
    return clean, ""


def ask_user_question(questions: list, ctx=None) -> str:
    """Present multiple-choice question(s) to the user and return their answers.

    An "Other…" freeform choice is always added per question by the UI, so the
    user is never boxed in by the options the model picked.
    """
    clean, err = _normalize(questions)
    if err:
        return json.dumps({"error": err})

    if _question_callback is None:
        # Headless / no UI (e.g. scheduled task run): can't ask a human.
        return json.dumps({
            "error": "No UI available to ask the user. Proceed with your best "
                     "judgment and state the assumption you made."
        })

    try:
        answers = _question_callback(clean)
    except Exception as e:
        return json.dumps({"error": f"Question UI failed: {e}"})

    if not answers:
        # User cancelled or hit Stop.
        return json.dumps({
            "status": "cancelled",
            "note": "User dismissed the question without answering. Do not re-ask; "
                    "proceed with a sensible default or ask in plain text if essential."
        })

    return json.dumps({"status": "answered", "answers": answers}, ensure_ascii=False)


registry.register(
    name="ask_user_question",
    description=(
        "Pause and ask the USER a structured multiple-choice question when the "
        "request is genuinely ambiguous and the answer changes what you do. "
        "An 'Other…' freeform option is always added automatically.\n"
        "Use SPARINGLY — only for decisions you can't resolve from the request, "
        "the code, or sensible defaults. Don't ask permission to start work, and "
        "don't ask what you can verify yourself. 1–4 questions, 2–4 options each."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_QUESTIONS,
                "description": "1–4 questions to ask at once.",
                "items": {
                    "type": "object",
                    "properties": {
                        "header": {
                            "type": "string",
                            "description": "Very short label/chip (max ~12 chars), e.g. 'Audience'.",
                        },
                        "question": {
                            "type": "string",
                            "description": "The full question text.",
                        },
                        "options": {
                            "type": "array",
                            "minItems": 2,
                            "maxItems": MAX_OPTIONS,
                            "description": "2–4 distinct choices.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string", "description": "Short choice text."},
                                    "description": {"type": "string", "description": "What this choice means / its trade-off."},
                                },
                                "required": ["label"],
                            },
                        },
                        "multiSelect": {
                            "type": "boolean",
                            "description": "Allow selecting multiple options. Default false.",
                        },
                    },
                    "required": ["question", "options"],
                },
            },
        },
        "required": ["questions"],
    },
    execute=ask_user_question,
)
