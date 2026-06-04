"""
Sub-agent tool — lets the main agent delegate work to parallel sub-agents.

Two modes:
  1. "delegate" — Decompose a goal into tasks, execute in parallel with deps
  2. "spawn"    — Fire a single sub-agent for a specific task
  3. "status"   — Check on a running job
  4. "wait"     — Block until a job completes

The UI bridge emits Qt signals so the chat window can show live status cards
and manage per-agent terminal tabs.
"""

import json
import threading
from tools.registry import registry

# ── UI Signal Bridge ────────────────────────────────────────────────────
# Tools run on inference thread; UI runs on main thread.
# The bridge emits Qt signals for cross-thread safety.

_bridge = None  # Set by UI layer (see _init_bridge)

try:
    from PyQt6.QtCore import QObject, pyqtSignal

    class SubAgentBridge(QObject):
        """Qt signal bridge for sub-agent UI integration.

        Uses str/object types for cross-thread safety. Qt can't reliably
        marshal dict/list across threads — use 'object' for compound types.
        """
        # Emitted when a sub-agent job starts (insert card into chat)
        job_started = pyqtSignal(str, str)   # (job_id, tasks_json_str)
        # Emitted when a task status changes (update card)
        task_updated = pyqtSignal(str, str, str)  # (task_id, status, data_json_str)
        # Emitted when a sub-agent needs a terminal tab
        terminal_requested = pyqtSignal(str, str, str)  # (task_id, command, cwd)
        # Emitted when entire job completes
        job_completed = pyqtSignal(str, str)  # (job_id, summary_json_str)

    _bridge = SubAgentBridge()

except ImportError:
    _bridge = None


def get_bridge():
    """Get the signal bridge (for UI connection). Returns None if no Qt."""
    return _bridge


# ── Wire bridge to orchestrator callbacks ───────────────────────────────

def _init_callbacks():
    """Connect orchestrator callbacks to bridge signals."""
    from core.subagent import on_status_change, on_terminal_request

    def _on_status(task_id, status, data):
        if _bridge:
            _bridge.task_updated.emit(task_id, status, json.dumps(data or {}))

    def _on_terminal(task_id, command, cwd):
        if _bridge:
            _bridge.terminal_requested.emit(task_id, command, cwd)

    on_status_change(_on_status)
    on_terminal_request(_on_terminal)

_init_callbacks()


# ── Tool implementation ─────────────────────────────────────────────────

def subagent(action: str, goal: str = "", mode: str = "general",
             job_id: str = "", title: str = "", description: str = "",
             ctx=None) -> str:
    """Delegate work to parallel sub-agents."""
    from core.subagent import get_orchestrator, get_existing

    workspace = ""
    if ctx:
        workspace = ctx.cwd
    if not workspace:
        try:
            config_path = __import__("pathlib").Path(__file__).parent.parent / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            ws = config.get("workspaces", {})
            if ws:
                first = next(iter(ws.values()))
                raw = first.get("path", first) if isinstance(first, dict) else first
                from core.workspace_paths import resolve_workspace_entry_path
                workspace = str(resolve_workspace_entry_path(str(raw) if raw else ""))
        except Exception:
            pass

    # ── Delegate: decompose + execute ──
    if action == "delegate":
        if not goal:
            return json.dumps({"error": "goal is required for delegate action"})

        orch = get_orchestrator()

        # Coherent (provider, model). Inherits the main agent's working pair
        # unless a coherent subagent override is configured. (A provider-only
        # override with no matching model used to fail every sub-agent.)
        from core.subagent import resolve_subagent_llm
        provider, model = resolve_subagent_llm(mode="decompose")

        orch._workspace = workspace

        # Decompose the goal into tasks
        tasks = orch.decompose(goal, model=model, provider=provider,
                               workspace=workspace)

        # Notify UI to insert job card
        if _bridge:
            _bridge.job_started.emit(orch._job_id, json.dumps([t.to_dict() for t in tasks]))

        # Execute in background thread (non-blocking to the main agent)
        def _run():
            summary = orch.execute()
            if _bridge:
                _bridge.job_completed.emit(orch._job_id, json.dumps(summary))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        return json.dumps({
            "job_id": orch._job_id,
            "status": "dispatched",
            "tasks": [t.to_dict() for t in tasks],
            "message": (
                f"Decomposed into {len(tasks)} tasks. Running in parallel. "
                f"Use action='status' with job_id='{orch._job_id}' to check progress, "
                f"or action='wait' to block until complete."
            ),
        })

    # ── Spawn: single task, no decomposition ──
    elif action == "spawn":
        if not title and not description:
            return json.dumps({"error": "title or description required for spawn"})

        orch = get_orchestrator()

        from core.subagent import resolve_subagent_llm
        provider, model = resolve_subagent_llm(mode=mode)

        orch._workspace = workspace
        orch._model = model
        orch._provider = provider

        task = orch.add_single_task(
            title=title or description[:80],
            description=description or title,
            mode=mode,
        )

        if _bridge:
            _bridge.job_started.emit(orch._job_id, json.dumps([task.to_dict()]))

        def _run():
            summary = orch.execute()
            if _bridge:
                _bridge.job_completed.emit(orch._job_id, json.dumps(summary))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        return json.dumps({
            "job_id": orch._job_id,
            "task_id": task.task_id,
            "status": "spawned",
            "message": f"Sub-agent '{task.title}' running in background ({mode} mode).",
        })

    # ── Status: check on a job ──
    elif action == "status":
        if not job_id:
            # List all active jobs
            from core.subagent import _orchestrators
            jobs = []
            for jid, orch in _orchestrators.items():
                jobs.append(orch.get_status())
            return json.dumps({"jobs": jobs})

        orch = get_existing(job_id)
        if not orch:
            return json.dumps({"error": f"No job with id '{job_id}'"})
        return json.dumps(orch.get_status())

    # ── Wait: check if job is done, return results if so ──
    elif action == "wait":
        if not job_id:
            return json.dumps({"error": "job_id required for wait action"})

        orch = get_existing(job_id)
        if not orch:
            return json.dumps({"error": f"No job with id '{job_id}'"})

        if not orch.queue.is_complete():
            # NOT done yet — return current status so agent can continue
            # doing other work and check back later
            status = orch.get_status()
            running = [t for t in status["tasks"] if t["status"] == "running"]
            pending = [t for t in status["tasks"] if t["status"] in ("pending", "blocked")]
            return json.dumps({
                "job_id": job_id,
                "status": "still_running",
                "running": len(running),
                "pending": len(pending),
                "message": (
                    f"Job not complete yet ({len(running)} running, {len(pending)} pending). "
                    f"Continue with other work and check back with action='wait' again, "
                    f"or use action='status' for full details."
                ),
                "tasks": status["tasks"],
            })

        # Job is complete — collect all results
        all_tasks = orch.queue.all_tasks()
        results = {}
        for task in all_tasks:
            if task.status.value == "completed":
                results[task.title] = task.result

        return json.dumps({
            "job_id": job_id,
            "status": "completed",
            "tasks": [t.to_dict() for t in all_tasks],
            "results": results,
        }, ensure_ascii=False)

    else:
        return json.dumps({
            "error": f"Unknown action: {action}. Use: delegate, spawn, status, wait"
        })


registry.register(
    name="subagent",
    description=(
        "Parallel sub-agents. "
        "delegate: goal→parallel tasks w/deps. spawn: single task. "
        "status: job_id (omit→all). wait: job_id→progress|results."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["delegate", "spawn", "status", "wait"],
                "description": "Sub-agent op.",
            },
            "goal": {
                "type": "string",
                "description": "delegate: complex goal to decompose.",
            },
            "title": {
                "type": "string",
                "description": "spawn: brief task title.",
            },
            "description": {
                "type": "string",
                "description": "spawn: detailed task desc.",
            },
            "mode": {
                "type": "string",
                "enum": ["search", "code", "plan", "test", "general"],
                "description": "spawn: mode (tools scope). Default general.",
            },
            "job_id": {
                "type": "string",
                "description": "status|wait: job ID.",
            },
        },
        "required": ["action"],
    },
    execute=subagent,
)
