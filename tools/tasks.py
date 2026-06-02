"""
Task scheduler — persistent scheduled prompts that run automatically.

Each task has a prompt, schedule, description, and a target conversation
where results are delivered. Tasks run in isolated Agent sessions.
"""

import json
import re
import time
import uuid
from pathlib import Path
from tools.registry import registry

TASKS_PATH = Path(__file__).parent.parent / "tasks.json"


# ── Persistence ──────────────────────────────────────────────────────

def load_tasks() -> list[dict]:
    if TASKS_PATH.exists():
        try:
            return json.loads(TASKS_PATH.read_text(encoding="utf-8")).get("tasks", [])
        except (json.JSONDecodeError, KeyError):
            pass
    return []


def save_tasks(tasks: list[dict]):
    tmp = TASKS_PATH.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"tasks": tasks, "updated_at": time.time()}, indent=2),
        encoding="utf-8")
    tmp.replace(TASKS_PATH)


# ── Schedule parsing ─────────────────────────────────────────────────

def _parse_datetime(s: str):
    """Try to parse a string as a specific date/time. Returns datetime or None."""
    from datetime import datetime, timedelta

    # ISO format: 2026-04-09T16:00, 2026-04-09 16:00, 2026-04-09
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except ValueError:
            continue

    # Natural language: "next thursday 4pm", "tomorrow 2:30pm", "friday 16:00"
    text = s.strip().lower()

    # Parse time component first (e.g. "4pm", "16:00", "2:30pm")
    hour, minute = None, 0
    time_match = re.search(r'(\d{1,2}):(\d{2})\s*(am|pm)?', text)
    if time_match:
        hour = int(time_match.group(1))
        minute = int(time_match.group(2))
        if time_match.group(3) == "pm" and hour < 12:
            hour += 12
        elif time_match.group(3) == "am" and hour == 12:
            hour = 0
    else:
        time_match = re.search(r'(\d{1,2})\s*(am|pm)', text)
        if time_match:
            hour = int(time_match.group(1))
            if time_match.group(2) == "pm" and hour < 12:
                hour += 12
            elif time_match.group(2) == "am" and hour == 12:
                hour = 0

    now = datetime.now()

    # "today", "tomorrow"
    if "today" in text:
        target = now.replace(hour=hour or 12, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target
    if "tomorrow" in text:
        target = (now + timedelta(days=1)).replace(hour=hour or 12, minute=minute, second=0, microsecond=0)
        return target

    # Day names: "next thursday", "thursday", "friday 4pm"
    day_names = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                 "friday": 4, "saturday": 5, "sunday": 6,
                 "mon": 0, "tue": 1, "wed": 2, "thu": 3,
                 "fri": 4, "sat": 5, "sun": 6}
    for name, dow in day_names.items():
        if name in text:
            days_ahead = dow - now.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            target = (now + timedelta(days=days_ahead)).replace(
                hour=hour or 12, minute=minute, second=0, microsecond=0)
            return target

    # "in 3 days", "in 2 hours"
    in_match = re.match(r'in\s+(\d+)\s+(day|hour|minute|min)s?', text)
    if in_match:
        val = int(in_match.group(1))
        unit = in_match.group(2)
        if unit == "day":
            return now + timedelta(days=val)
        elif unit == "hour":
            return now + timedelta(hours=val)
        elif unit in ("minute", "min"):
            return now + timedelta(minutes=val)

    return None


def parse_schedule(schedule: str) -> dict:
    """Parse schedule string into normalized dict.

    Formats:
        '30m', '2h', '1d'                      -> one-shot delay
        'every 30m', 'every 2h'                 -> recurring interval
        '0 9 * * *'                             -> cron expression
        '2026-04-09 16:00'                      -> specific datetime (ISO)
        'next thursday 4pm', 'tomorrow 2:30pm'  -> natural language datetime
    """
    s = schedule.strip().lower()
    if s in ("startup", "on startup", "onstartup"):
        return {"kind": "startup", "display": "on startup"}
    recurring = False
    if s.startswith("every "):
        recurring = True
        s = s[6:].strip()

    m = re.match(r'^(\d+)\s*(m|min|h|hr|hour|d|day)s?$', s)
    if m:
        val, unit = int(m.group(1)), m.group(2)[0]
        minutes = val * {"m": 1, "h": 60, "d": 1440}[unit]
        return {"kind": "interval" if recurring else "once",
                "minutes": minutes, "display": schedule.strip()}

    parts = schedule.strip().split()
    if len(parts) == 5 and all(re.match(r'^[\d*/,-]+$', p) for p in parts):
        return {"kind": "cron", "expr": schedule.strip(),
                "display": schedule.strip()}

    # Try specific date/time
    dt = _parse_datetime(schedule)
    if dt:
        return {"kind": "once", "run_at": dt.isoformat(),
                "display": schedule.strip()}

    return {"kind": "unknown", "display": schedule.strip()}


def next_run_time(schedule: dict) -> float:
    """Compute next run timestamp from a parsed schedule."""
    from datetime import datetime
    now = time.time()
    kind = schedule.get("kind", "")

    # Startup tasks have no time trigger — the startup runner fires them.
    if kind == "startup":
        return None

    # Specific datetime (ISO string from _parse_datetime)
    if "run_at" in schedule:
        dt = datetime.fromisoformat(schedule["run_at"])
        return dt.timestamp()

    if kind in ("once", "interval"):
        return now + schedule.get("minutes", 10) * 60
    if kind == "cron":
        try:
            from croniter import croniter
            from datetime import datetime
            local_now = datetime.now()
            nxt = croniter(schedule["expr"], local_now).get_next(datetime)
            return nxt.timestamp()
        except ImportError:
            return now + 3600
    return now + 600


# ── Task CRUD ────────────────────────────────────────────────────────

def create_task(prompt: str, schedule: str, name: str = "",
                description: str = "", conversation_id: str = "",
                repeat: int = None, enabled: bool = True,
                conditions: list = None, actions: list = None,
                deliver_to_type: str = "conversation",
                deliver_to_stream: str = "") -> dict:
    """Create and persist a new task. Returns the task dict."""
    parsed = parse_schedule(schedule)
    if parsed["kind"] == "unknown":
        return {"error": f"Could not parse schedule: {schedule}"}

    task = {
        "id": uuid.uuid4().hex[:12],
        "name": name or prompt[:40],
        "description": description,
        "prompt": prompt,
        "schedule": parsed,
        "conversation_id": conversation_id,
        "repeat": {"times": repeat, "completed": 0},
        "enabled": enabled,
        "conditions": conditions or [],
        "actions": actions or [],
        "deliver_to_type": deliver_to_type,
        "deliver_to_stream": deliver_to_stream,
        "created_at": time.time(),
        "next_run_at": next_run_time(parsed),
        "last_run_at": None,
        "last_status": None,
    }
    tasks = load_tasks()
    tasks.append(task)
    save_tasks(tasks)
    return task


def remove_task(task_id: str) -> bool:
    tasks = load_tasks()
    before = len(tasks)
    tasks = [t for t in tasks if t["id"] != task_id]
    if len(tasks) == before:
        return False
    save_tasks(tasks)
    return True


def update_task(task_id: str, **kwargs) -> bool:
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            if "prompt" in kwargs and kwargs["prompt"]:
                t["prompt"] = kwargs["prompt"]
            if "schedule" in kwargs and kwargs["schedule"]:
                parsed = parse_schedule(kwargs["schedule"])
                if parsed["kind"] != "unknown":
                    t["schedule"] = parsed
                    t["next_run_at"] = next_run_time(parsed)
            if "name" in kwargs and kwargs["name"]:
                t["name"] = kwargs["name"]
            if "description" in kwargs:
                t["description"] = kwargs["description"]
            if "conversation_id" in kwargs:
                t["conversation_id"] = kwargs["conversation_id"]
            if "enabled" in kwargs:
                t["enabled"] = kwargs["enabled"]
            save_tasks(tasks)
            return True
    return False


def pause_task(task_id: str) -> bool:
    return update_task(task_id, enabled=False)


def resume_task(task_id: str) -> bool:
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["enabled"] = True
            sched = t["schedule"]
            # For one-shot delays and specific datetimes, recalculate from now
            if sched.get("kind") == "once":
                if "minutes" in sched:
                    t["next_run_at"] = time.time() + sched["minutes"] * 60
                else:
                    # Specific datetime that already passed — re-run the same delay
                    t["next_run_at"] = time.time() + 600  # 10min default
            else:
                t["next_run_at"] = next_run_time(sched)
            t["repeat"]["completed"] = 0
            save_tasks(tasks)
            return True
    return False


# ── Ticker ───────────────────────────────────────────────────────────

def tick_due_tasks() -> list[dict]:
    """Return tasks that are due. Advances their next_run_at.
    Returns list of {"id", "name", "prompt", "conversation_id"} for caller to execute."""
    tasks = load_tasks()
    now = time.time()
    due = []

    for t in tasks:
        if not t.get("enabled", True):
            continue
        if t.get("next_run_at") and t["next_run_at"] <= now:
            due.append({
                "id": t["id"],
                "name": t["name"],
                "prompt": t["prompt"],
                "conversation_id": t.get("conversation_id", ""),
            })
            kind = t["schedule"].get("kind", "")
            if kind == "once":
                t["enabled"] = False
            elif kind == "interval":
                t["next_run_at"] = now + t["schedule"].get("minutes", 10) * 60
            elif kind == "cron":
                t["next_run_at"] = next_run_time(t["schedule"])

            rep = t.get("repeat", {})
            rep["completed"] = rep.get("completed", 0) + 1
            if rep.get("times") and rep["completed"] >= rep["times"]:
                t["enabled"] = False

            t["last_run_at"] = now

    if due:
        save_tasks(tasks)
    return due


def mark_task_result(task_id: str, success: bool, error: str = None):
    tasks = load_tasks()
    for t in tasks:
        if t["id"] == task_id:
            t["last_status"] = "ok" if success else f"error: {error}"
            break
    save_tasks(tasks)


# ── Agent tool ───────────────────────────────────────────────────────

def task_scheduler(action: str, task_id: str = "", prompt: str = "",
                   schedule: str = "", name: str = "", description: str = "",
                   conversation_id: str = "", repeat: int = None,
                   conditions: list = None, actions: list = None,
                   deliver_to_type: str = "conversation",
                   deliver_to_stream: str = "") -> str:
    """Manage scheduled tasks."""

    # Default to current conversation if none specified
    if not conversation_id:
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app:
                for w in app.topLevelWidgets():
                    if w.__class__.__name__ == "MainWindow":
                        conversation_id = getattr(w.chat, '_current_conv_id', '')
                        break
        except Exception:
            pass

    if action == "create":
        if not schedule and not conditions:
            return json.dumps({"error": "schedule or conditions required. "
                               "Examples: schedule='every 2h', or conditions=[{\"kind\":\"interval\",\"value\":2,\"unit\":\"h\",\"display\":\"every 2h\"}]"})
        if not prompt and not actions:
            return json.dumps({"error": "prompt or actions required. "
                               "Examples: prompt='say hi', or actions=[{\"type\":\"prompt\",\"content\":\"say hi\"}, {\"type\":\"sound\",\"content\":\"alert.mp3\"}, {\"type\":\"visual\"}]"})
        if not name:
            content = prompt or (actions[0].get("content", "") if actions else "Task")
            name = content[:40]

        # Build conditions from schedule if not provided directly
        if not conditions and schedule:
            parsed = parse_schedule(schedule)
            c = {"kind": parsed.get("kind", "delay"), "display": parsed.get("display", schedule)}
            if "minutes" in parsed:
                c["value"] = int(parsed["minutes"])
                c["unit"] = "m"
            if "expr" in parsed:
                c["expr"] = parsed["expr"]
            if "run_at" in parsed:
                c["datetime"] = parsed["run_at"]
            conditions = [c]

        # Build actions from prompt if not provided directly
        if not actions and prompt:
            actions = [{"type": "prompt", "content": prompt,
                        "display": f"[LLM] {prompt[:40]}"}]
        elif actions:
            # Ensure each action has a display label
            labels = {"prompt": "LLM", "visual": "VIS", "audio": "TTS", "sound": "SND", "execute": "RUN"}
            for a in actions:
                if "display" not in a:
                    atype = a.get("type", "prompt")
                    content = a.get("content", "")
                    a["display"] = f'[{labels.get(atype, "?")}] {content[:40]}' if content else f'[{labels.get(atype, "?")}] {atype.title()}'

        # Derive schedule string for backend from conditions
        sched_str = schedule
        if not sched_str and conditions:
            c0 = conditions[0]
            kind = c0.get("kind", "")
            if kind in ("delay", "once"):
                sched_str = f'{c0.get("value", 30)}{c0.get("unit", "m")}'
            elif kind == "interval":
                sched_str = f'every {c0.get("value", 2)}{c0.get("unit", "h")}'
            elif kind == "cron":
                sched_str = c0.get("expr", "")
            elif kind == "datetime":
                sched_str = c0.get("datetime", "")
            else:
                sched_str = c0.get("display", "10m")

        # Primary prompt for backend compat — first prompt action's content, or any content
        primary_prompt = prompt
        if not primary_prompt:
            primary_prompt = next((a.get("content", "") for a in actions if a.get("type") == "prompt" and a.get("content")), "")
        if not primary_prompt:
            primary_prompt = next((a.get("content", "") for a in actions if a.get("content")), "")
        if not primary_prompt:
            primary_prompt = name or "Task"

        task = create_task(
            primary_prompt, sched_str, name, description, conversation_id, repeat,
            enabled=True, conditions=conditions, actions=actions,
            deliver_to_type=deliver_to_type, deliver_to_stream=deliver_to_stream)
        if "error" in task:
            return json.dumps(task)
        from core.sounds import list_sounds
        result = {"status": "done",
                  "message": f"Task '{task['name']}' created and scheduled. "
                             f"It is now visible in the user's Tasks panel. "
                             f"No further tool calls are needed — confirm to the user in text.",
                  "created": task["id"], "name": task["name"],
                  "next_run": task["next_run_at"],
                  "conditions": len(task.get("conditions", [])),
                  "actions": len(task.get("actions", [])),
                  "available_sounds": list_sounds()}
        return json.dumps(result)

    elif action == "list":
        tasks = load_tasks()
        now = time.time()
        visible = []
        for t in tasks:
            next_run = t.get("next_run_at", 0)
            last_run = t.get("last_run_at") or t.get("created_at", 0)
            total_window = next_run - last_run if next_run > last_run else 0
            elapsed = now - last_run if now > last_run else 0
            progress_pct = min(100.0, (elapsed / total_window * 100)) if total_window > 0 else 0
            secs_remaining = max(0, next_run - now) if t.get("enabled") else None
            visible.append({
                "id": t["id"], "name": t["name"],
                "description": t.get("description", ""),
                "schedule": t["schedule"]["display"],
                "enabled": t["enabled"],
                "conversation_id": t.get("conversation_id", ""),
                "deliver_to_type": t.get("deliver_to_type", "conversation"),
                "deliver_to_stream": t.get("deliver_to_stream", ""),
                "next_run_at": next_run,
                "last_status": t.get("last_status"),
                "countdown_pct": round(progress_pct, 1),
                "seconds_remaining": round(secs_remaining) if secs_remaining is not None else None,
            })
        from core.sounds import list_sounds
        return json.dumps({"tasks": visible, "count": len(visible),
                           "available_sounds": list_sounds()})

    elif action == "list_my_tasks":
        # Filter to tasks relevant to the current conversation or its streams
        tasks = load_tasks()
        now = time.time()
        relevant = []
        for t in tasks:
            is_match = False
            # Direct conversation match
            if conversation_id and t.get("conversation_id") == conversation_id:
                is_match = True
            # Stream match — task targets a stream, caller passes stream names via description
            if t.get("deliver_to_type") == "stream" and t.get("deliver_to_stream"):
                # description field is repurposed to pass comma-separated stream names
                if description:
                    caller_streams = [s.strip() for s in description.split(",")]
                    if t["deliver_to_stream"] in caller_streams:
                        is_match = True
            if not is_match:
                continue
            next_run = t.get("next_run_at", 0)
            last_run = t.get("last_run_at") or t.get("created_at", 0)
            total_window = next_run - last_run if next_run > last_run else 0
            elapsed = now - last_run if now > last_run else 0
            progress_pct = min(100.0, (elapsed / total_window * 100)) if total_window > 0 else 0
            secs_remaining = max(0, next_run - now) if t.get("enabled") else None
            relevant.append({
                "id": t["id"], "name": t["name"],
                "description": t.get("description", ""),
                "schedule": t["schedule"]["display"],
                "enabled": t["enabled"],
                "countdown_pct": round(progress_pct, 1),
                "seconds_remaining": round(secs_remaining) if secs_remaining is not None else None,
                "last_status": t.get("last_status"),
            })
        return json.dumps({"tasks": relevant, "count": len(relevant)})

    elif action == "remove":
        if not task_id:
            return json.dumps({"error": "task_id is required"})
        ok = remove_task(task_id)
        return json.dumps({
            "status": "done",
            "message": f"Task {task_id} removed. The user's Tasks panel is updated. "
                       f"No further tool calls — reply to the user in text.",
            "removed": task_id,
        } if ok else {"error": f"Task {task_id} not found"})

    elif action == "pause":
        ok = pause_task(task_id)
        return json.dumps({
            "status": "done",
            "message": f"Task {task_id} paused. No further tool calls needed.",
            "paused": task_id,
        } if ok else {"error": f"Task {task_id} not found"})

    elif action == "resume":
        ok = resume_task(task_id)
        return json.dumps({
            "status": "done",
            "message": f"Task {task_id} resumed. No further tool calls needed.",
            "resumed": task_id,
        } if ok else {"error": f"Task {task_id} not found"})

    elif action == "update":
        if not task_id:
            return json.dumps({"error": "task_id is required"})
        ok = update_task(task_id, prompt=prompt, schedule=schedule, name=name,
                         description=description, conversation_id=conversation_id)
        # Update conditions/actions if provided
        if ok and (conditions or actions):
            all_tasks = load_tasks()
            for t in all_tasks:
                if t["id"] == task_id:
                    if conditions:
                        t["conditions"] = conditions
                    if actions:
                        t["actions"] = actions
                    if deliver_to_type:
                        t["deliver_to_type"] = deliver_to_type
                    if deliver_to_stream:
                        t["deliver_to_stream"] = deliver_to_stream
                    break
            save_tasks(all_tasks)
        return json.dumps({
            "status": "done",
            "message": f"Task {task_id} updated. The user's Tasks panel reflects the change. "
                       f"No further tool calls — reply to the user in text.",
            "updated": task_id,
        } if ok else {"error": f"Task {task_id} not found"})

    else:
        return json.dumps({"error": f"Unknown action: {action}. "
                           "Use: create, list, list_my_tasks, remove, pause, resume, update"})


registry.register(
    name="task_scheduler",
    description=(
        "Scheduled tasks. create|list|list_my_tasks|remove|pause|resume|update.\n"
        "Types: prompt(LLM)|visual(blink UI)|audio(TTS)|sound(mp3 from sounds/).\n"
        "'ask me'/'remind me' → visual+request_response:true+prompt.\n"
        "Schedule: '1m'|'every 2h'|'0 9 * * *'|'next thursday 4pm'.\n"
        "list_my_tasks+conversation_id → this conv's tasks w/ countdown."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "description": "create | list | list_my_tasks | remove | pause | resume | update",
            },
            "task_id": {
                "type": "string",
                "description": "Task ID (remove|pause|resume|update).",
            },
            "prompt": {
                "type": "string",
                "description": "Simple mode prompt. Ignored if actions[] given.",
            },
            "schedule": {
                "type": "string",
                "description": "'30m' | 'every 2h' | cron '0 9 * * *' (min hr day mon dow). Ignored if conditions[] given.",
            },
            "conditions": {
                "type": "array",
                "description": "Advanced condition objs. {kind,value,unit,display} delay|interval; {kind,expr,display} cron; {kind,datetime,display} datetime.",
                "items": {"type": "object"},
            },
            "actions": {
                "type": "array",
                "description": "Advanced action objs {type,content,display}. Types: prompt|visual|audio|sound. Multiple fire together.",
                "items": {"type": "object"},
            },
            "name": {
                "type": "string",
                "description": "Short task name.",
            },
            "description": {
                "type": "string",
                "description": "What task does + why.",
            },
            "conversation_id": {
                "type": "string",
                "description": "Target conv (empty → current).",
            },
            "deliver_to_type": {
                "type": "string",
                "description": "'conversation' (default) | 'stream'.",
            },
            "deliver_to_stream": {
                "type": "string",
                "description": "Stream name (when deliver_to_type='stream').",
            },
            "repeat": {
                "type": "integer",
                "description": "Max repeats (omit → unlimited).",
            },
        },
        "required": ["action"],
    },
    execute=task_scheduler,
)
