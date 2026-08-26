"""
Plan tool — in-flight work planning with live progress tracking.

The agent creates a plan at the start of complex tasks and updates it
as it works. The UI displays the plan in real-time so the user can
see progress and intervene if needed.

Plan state is held in a module-level dict so the UI can poll it without
tool calls, AND mirrored to disk (atomic write) after every mutation so a
long multi-step job survives a crash/restart — durable workflows. On import
the last in-flight plan is reloaded, so the agent can resume where it left
off instead of starting the task over.
"""

import json
import time
from pathlib import Path
from tools.registry import registry

# Durable mirror of the live plan. Sits beside tasks.json (same persistence
# pattern). Holds the single active plan; cleared when the plan finishes.
PLAN_PATH = Path(__file__).parent.parent / "active_plan.json"

# Live plan state — keyed by conversation (only one active at a time)
_current_plan: dict | None = None
# Snapshot of the most recently finished plan. The UI's tool signal crosses
# threads, so by the time it handles 'finish' the live plan may already be
# cleared — this keeps the final state readable for the persisted card.
_last_finished_plan: dict | None = None


def _save() -> None:
    """Atomically mirror the live plan to disk (or remove it when cleared)."""
    try:
        if _current_plan is None:
            PLAN_PATH.unlink(missing_ok=True)
            return
        tmp = PLAN_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(_current_plan, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(PLAN_PATH)
    except OSError:
        pass  # persistence is best-effort; never break a tool call over it


def _load() -> None:
    """Reload the last in-flight plan on startup so work can resume."""
    global _current_plan
    try:
        if PLAN_PATH.exists():
            data = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("steps"):
                _current_plan = data
    except (OSError, json.JSONDecodeError):
        pass


def get_current_plan() -> dict | None:
    """Get the current plan (called by UI for live display)."""
    return _current_plan


def get_last_finished_plan() -> dict | None:
    """Final state of the most recently finished plan (UI persistence)."""
    return _last_finished_plan


def clear_plan():
    """Clear the current plan (called on conversation switch)."""
    global _current_plan, _last_finished_plan
    _current_plan = None
    _last_finished_plan = None
    _save()


def plan(action: str, title: str = "", steps: list = None,
         step_index: int = -1, status: str = "", label: str = "") -> str:
    """Manage an in-flight work plan."""
    global _current_plan, _last_finished_plan

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
        _save()
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
        _save()
        return json.dumps({"updated": step_index, "status": status})

    elif action == "add_step":
        if not _current_plan:
            return json.dumps({"error": "no active plan"})
        if not label:
            return json.dumps({"error": "label required"})
        insert_at = step_index if 0 <= step_index <= len(_current_plan["steps"]) else len(_current_plan["steps"])
        _current_plan["steps"].insert(insert_at, {"label": label, "status": "pending"})
        _current_plan["updated_at"] = time.time()
        _save()
        return json.dumps({"added": label, "at": insert_at, "total": len(_current_plan["steps"])})

    elif action == "remove_step":
        if not _current_plan:
            return json.dumps({"error": "no active plan"})
        if step_index < 0 or step_index >= len(_current_plan["steps"]):
            return json.dumps({"error": f"invalid step_index {step_index}"})
        removed = _current_plan["steps"].pop(step_index)
        _current_plan["updated_at"] = time.time()
        _save()
        return json.dumps({"removed": removed["label"], "remaining": len(_current_plan["steps"])})

    elif action == "finish":
        if not _current_plan:
            return json.dumps({"error": "no active plan"})
        title = _current_plan["title"]
        done = sum(1 for s in _current_plan["steps"] if s["status"] == "done")
        total = len(_current_plan["steps"])
        _last_finished_plan = _current_plan
        _current_plan = None
        _save()
        return json.dumps({"finished": title, "completed": done, "total": total})

    elif action == "get":
        if not _current_plan:
            return json.dumps({"plan": None})
        return json.dumps({"plan": _current_plan}, ensure_ascii=False)

    elif action == "resume":
        # Read back the durable plan after a restart/crash so the agent can
        # see exactly where it left off (which steps are done vs pending).
        if not _current_plan:
            return json.dumps({"plan": None, "note": "no in-flight plan to resume"})
        done = sum(1 for s in _current_plan["steps"] if s["status"] == "done")
        total = len(_current_plan["steps"])
        nxt = next((i for i, s in enumerate(_current_plan["steps"])
                    if s["status"] in ("pending", "in_progress")), None)
        return json.dumps({"plan": _current_plan, "completed": done, "total": total,
                           "next_step_index": nxt}, ensure_ascii=False)

    else:
        return json.dumps({
            "error": f"Unknown action: {action}. Use: create, update, add_step, remove_step, finish, get, resume"
        })


registry.register(
    name="plan",
    description=(
        "Live work plan visible to user. ✓ at start of complex multi-step tasks.\n"
        "- create: title + steps[].\n"
        "- update: step_index + status (pending|in_progress|done|skipped|blocked).\n"
        "- add_step: label + optional step_index.\n"
        "- remove_step: step_index. finish: close. get: read current.\n"
        "- resume: re-read the durable plan after a restart (survives crashes); "
        "returns next_step_index so you can pick up where you left off."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "update", "add_step", "remove_step", "finish", "get", "resume"],
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

# Reload any in-flight plan from the last session so work can resume.
_load()
