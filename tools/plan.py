"""
Plan tool — in-flight work planning with live progress tracking.

The agent creates a plan at the start of complex tasks and updates it
as it works. The UI displays the plan in real-time so the user can
see progress and intervene if needed.

Plan state is stored per-conversation in a module-level dict so the
UI can poll it without tool calls.
"""

import json
import time
from tools.registry import registry

# Live plan state — keyed by conversation (only one active at a time)
_current_plan: dict | None = None


def get_current_plan() -> dict | None:
    """Get the current plan (called by UI for live display)."""
    return _current_plan


def clear_plan():
    """Clear the current plan (called on conversation switch)."""
    global _current_plan
    _current_plan = None


def plan(action: str, title: str = "", steps: list = None,
         step_index: int = -1, status: str = "", label: str = "") -> str:
    """Manage an in-flight work plan."""
    global _current_plan

    if action == "create":
        if not title:
            return json.dumps({"error": "title required"})
        if not steps:
            return json.dumps({"error": "steps required (list of strings)"})
        _current_plan = {
            "title": title,
            "steps": [{"label": s, "status": "pending"} for s in steps],
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        return json.dumps({"created": title, "steps": len(steps)})

    elif action == "update":
        if not _current_plan:
            return json.dumps({"error": "no active plan"})
        if step_index < 0 or step_index >= len(_current_plan["steps"]):
            return json.dumps({"error": f"invalid step_index {step_index}"})
        if status not in ("pending", "in_progress", "done", "skipped", "blocked"):
            return json.dumps({"error": "status must be: pending, in_progress, done, skipped, blocked"})
        _current_plan["steps"][step_index]["status"] = status
        if label:
            _current_plan["steps"][step_index]["label"] = label
        _current_plan["updated_at"] = time.time()
        return json.dumps({"updated": step_index, "status": status})

    elif action == "add_step":
        if not _current_plan:
            return json.dumps({"error": "no active plan"})
        if not label:
            return json.dumps({"error": "label required"})
        insert_at = step_index if 0 <= step_index <= len(_current_plan["steps"]) else len(_current_plan["steps"])
        _current_plan["steps"].insert(insert_at, {"label": label, "status": "pending"})
        _current_plan["updated_at"] = time.time()
        return json.dumps({"added": label, "at": insert_at, "total": len(_current_plan["steps"])})

    elif action == "remove_step":
        if not _current_plan:
            return json.dumps({"error": "no active plan"})
        if step_index < 0 or step_index >= len(_current_plan["steps"]):
            return json.dumps({"error": f"invalid step_index {step_index}"})
        removed = _current_plan["steps"].pop(step_index)
        _current_plan["updated_at"] = time.time()
        return json.dumps({"removed": removed["label"], "remaining": len(_current_plan["steps"])})

    elif action == "finish":
        if not _current_plan:
            return json.dumps({"error": "no active plan"})
        title = _current_plan["title"]
        done = sum(1 for s in _current_plan["steps"] if s["status"] == "done")
        total = len(_current_plan["steps"])
        _current_plan = None
        return json.dumps({"finished": title, "completed": done, "total": total})

    elif action == "get":
        if not _current_plan:
            return json.dumps({"plan": None})
        return json.dumps({"plan": _current_plan}, ensure_ascii=False)

    else:
        return json.dumps({
            "error": f"Unknown action: {action}. Use: create, update, add_step, remove_step, finish, get"
        })


registry.register(
    name="plan",
    description=(
        "Live work plan visible to user. ✓ at start of complex multi-step tasks.\n"
        "- create: title + steps[].\n"
        "- update: step_index + status (pending|in_progress|done|skipped|blocked).\n"
        "- add_step: label + optional step_index.\n"
        "- remove_step: step_index. finish: close. get: read current."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "add_step", "remove_step", "finish", "get"],
                "description": "Plan op.",
            },
            "title": {
                "type": "string",
                "description": "Title (create).",
            },
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Step strings (create).",
            },
            "step_index": {
                "type": "integer",
                "description": "Index for update|remove|insert.",
            },
            "status": {
                "type": "string",
                "enum": ["pending", "in_progress", "done", "skipped", "blocked"],
                "description": "New status (update).",
            },
            "label": {
                "type": "string",
                "description": "Step label (add_step|update).",
            },
        },
        "required": ["action"],
    },
    execute=plan,
)
