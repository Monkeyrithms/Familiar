"""
Async sub-agent orchestrator — structured task decomposition with
dependency-aware parallel execution.

Pattern (inspired by open-multi-agent-main):
  1. Agent calls subagent tool with a goal
  2. Orchestrator decomposes goal into tasks with dependency graph (1 LLM call)
  3. TaskQueue dispatches tasks in parallel rounds as dependencies resolve
  4. Each sub-agent gets its own LLM conversation + filtered tools
  5. Results flow into SharedMemory so downstream agents see upstream output
  6. UI is notified via signals for live status cards + terminal tabs

Can also be used in simple mode: fire a single sub-agent with no decomposition.
"""

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from concurrent.futures import ThreadPoolExecutor, Future
from pathlib import Path


def _strip_emoji_pictographs(s: str) -> str:
    """Remove emoji and common pictographic symbols from task strings."""
    if not s:
        return s
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if o == 0xFE0F:
            continue
        if 0x1F000 <= o <= 0x1FFFF:
            continue
        if 0x2600 <= o <= 0x26FF:
            continue
        if 0x2700 <= o <= 0x27BF:
            continue
        out.append(ch)
    return "".join(out).strip()


# ── Task status ─────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    BLOCKED = "blocked"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class SubTask:
    """A single task in the dependency graph."""
    task_id: str
    title: str
    description: str
    mode: str = "general"
    status: TaskStatus = TaskStatus.PENDING
    depends_on: list[str] = field(default_factory=list)
    assigned_agent: str = ""
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0
    extra: dict = field(default_factory=dict)  # mode-specific payload (explore: files+query)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "description": self.description[:200],
            "mode": self.mode,
            "status": self.status.value,
            "depends_on": self.depends_on,
            "elapsed": round(
                (self.completed_at or time.time()) - self.created_at, 1
            ),
        }


# ── Shared memory ───────────────────────────────────────────────────────

class SharedMemory:
    """Thread-safe shared context between sub-agents.

    Sub-agents write their results here. Downstream agents see upstream
    results injected into their prompts automatically.
    """

    def __init__(self):
        self._store: dict[str, str] = {}
        self._lock = threading.Lock()

    def write(self, key: str, value: str):
        with self._lock:
            self._store[key] = value

    def read(self, key: str) -> str:
        with self._lock:
            return self._store.get(key, "")

    def get_summary(self, max_chars: int = 8000) -> str:
        """Get a summary of all stored results for prompt injection."""
        with self._lock:
            if not self._store:
                return ""
            lines = ["## Results from completed sub-tasks:"]
            total = 0
            for key, value in self._store.items():
                entry = f"\n### {key}\n{value[:2000]}"
                if total + len(entry) > max_chars:
                    lines.append(f"\n... ({len(self._store) - len(lines) + 1} more results truncated)")
                    break
                lines.append(entry)
                total += len(entry)
            return "\n".join(lines)

    def clear(self):
        with self._lock:
            self._store.clear()


# ── Task queue with dependency tracking ─────────────────────────────────

class TaskQueue:
    """Manages tasks with dependency-aware dispatching."""

    def __init__(self):
        self._tasks: dict[str, SubTask] = {}
        self._lock = threading.Lock()

    def add(self, task: SubTask):
        with self._lock:
            self._tasks[task.task_id] = task
            # Auto-block if has unresolved dependencies
            if task.depends_on:
                unresolved = [d for d in task.depends_on
                              if d in self._tasks
                              and self._tasks[d].status != TaskStatus.COMPLETED]
                if unresolved:
                    task.status = TaskStatus.BLOCKED

    def get(self, task_id: str) -> SubTask | None:
        with self._lock:
            return self._tasks.get(task_id)

    def get_ready(self) -> list[SubTask]:
        """Return all tasks that are ready to dispatch (pending + deps met)."""
        with self._lock:
            ready = []
            for task in self._tasks.values():
                if task.status == TaskStatus.PENDING:
                    ready.append(task)
                elif task.status == TaskStatus.BLOCKED:
                    if self._deps_satisfied(task):
                        task.status = TaskStatus.PENDING
                        ready.append(task)
            return ready

    def complete(self, task_id: str, result: str):
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus.COMPLETED
                task.result = result
                task.completed_at = time.time()
                self._unblock_dependents(task_id)

    def fail(self, task_id: str, error: str):
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus.FAILED
                task.error = error
                task.completed_at = time.time()
                # Cascade failure to dependents
                self._cascade_fail(task_id)

    def mark_running(self, task_id: str):
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.status = TaskStatus.RUNNING
                task.started_at = time.time()

    def is_complete(self) -> bool:
        with self._lock:
            return all(t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
                       for t in self._tasks.values())

    def all_tasks(self) -> list[SubTask]:
        with self._lock:
            return list(self._tasks.values())

    def _deps_satisfied(self, task: SubTask) -> bool:
        for dep_id in task.depends_on:
            dep = self._tasks.get(dep_id)
            if not dep or dep.status != TaskStatus.COMPLETED:
                return False
        return True

    def _unblock_dependents(self, completed_id: str):
        for task in self._tasks.values():
            if task.status == TaskStatus.BLOCKED and completed_id in task.depends_on:
                if self._deps_satisfied(task):
                    task.status = TaskStatus.PENDING

    def _cascade_fail(self, failed_id: str):
        for task in self._tasks.values():
            if task.status in (TaskStatus.PENDING, TaskStatus.BLOCKED):
                if failed_id in task.depends_on:
                    task.status = TaskStatus.FAILED
                    task.error = f"Dependency '{failed_id}' failed"
                    task.completed_at = time.time()
                    self._cascade_fail(task.task_id)


# ── Sub-agent mode definitions ──────────────────────────────────────────

_MODE_PROMPTS = {
    "search": (
        "You are a search specialist sub-agent. Find information quickly "
        "and return a concise, factual summary. Use grep, file_read, and web_search. "
        "Do NOT modify any files."
    ),
    "code": (
        "You are a coding sub-agent. Implement the specific coding task assigned. "
        "Use file_read, file_edit, file_write, and terminal. Stay focused — "
        "do not expand scope beyond the task."
    ),
    "plan": (
        "You are a planning sub-agent. Analyze the codebase and produce a "
        "detailed implementation plan. Use file_read and grep. Do NOT modify files."
    ),
    "test": (
        "You are a testing sub-agent. Run tests, analyze failures, and report "
        "results. Use terminal and file_read. Fix simple test issues if possible."
    ),
    "general": (
        "You are a sub-agent handling a delegated task. Complete it thoroughly "
        "and return a concise result."
    ),
    # explore mode is single-shot: bypasses the agent loop entirely.
    # See _run_explore_single_shot — files are pre-read in Python, one LLM call
    # produces all summaries in delimited format. Prompt is constructed inline.
    "explore": "(single-shot mode — see _run_explore_single_shot)",
}

# Per-mode round budgets — the safety net, not the exit mechanism.
# Agents exit by returning a text response; these are just upper bounds.
_MODE_MAX_ROUNDS = {
    "search": 20,   # mostly reads, shouldn't need many rounds
    "plan":   20,   # analysis only, no iteration needed
    "code":   50,   # may need edit→test→fix cycles
    "test":   30,   # run→diagnose→fix loop
    "general": 40,  # unknown complexity — give headroom
    "explore": 1,   # single-shot, no agent loop — value unused
}

_MODE_TOOLS = {
    "search": {"file_read", "grep", "web_search", "http_client", "session_search",
               "vector_search", "project_loader"},
    "code": {"file_read", "file_write", "file_edit", "grep", "terminal",
             "diff_tool", "git_tool"},
    "plan": {"file_read", "grep", "project_loader", "git_tool", "session_search"},
    "test": {"file_read", "grep", "terminal", "git_tool"},
    "general": None,  # All tools
    "explore": set(),  # no tools — single-shot summarization
}


# ── Sub-agent execution ────────────────────────────────────────────────

# Callbacks for UI integration (set by the UI layer)
_on_task_status_changed: list = []  # (task_id, status, data) -> None
_on_terminal_requested: list = []   # (task_id, command, cwd) -> None


def on_status_change(callback):
    """Register a callback for task status changes."""
    _on_task_status_changed.append(callback)


def on_terminal_request(callback):
    """Register a callback when a sub-agent needs a terminal."""
    _on_terminal_requested.append(callback)


def _notify_status(task_id: str, status: str, data: dict = None):
    for cb in _on_task_status_changed:
        try:
            cb(task_id, status, data or {})
        except Exception:
            pass


def _notify_terminal(task_id: str, command: str, cwd: str = ""):
    for cb in _on_terminal_requested:
        try:
            cb(task_id, command, cwd)
        except Exception:
            pass


def _run_single_agent(task: SubTask, shared_mem: SharedMemory,
                      model: str = "", provider: str = "",
                      workspace: str = "") -> str:
    """Execute a single sub-agent task. Returns the result text."""
    from core.providers import get_client
    from tools.registry import registry

    # Load config defaults if needed, with per-mode model overrides.
    # Config keys: subagent_search_model, subagent_plan_model, subagent_code_model,
    #              subagent_test_model, subagent_model (fallback for all modes).
    # Provider keys: same pattern with _provider suffix.
    config_path = Path(__file__).parent.parent / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        config = {}
    if not provider or not model:
        mode_key = f"subagent_{task.mode}_model"
        mode_prov_key = f"subagent_{task.mode}_provider"
        provider = (provider
                    or config.get(mode_prov_key)
                    or config.get("subagent_provider")
                    or config.get("provider", "openrouter"))
        model = (model
                 or config.get(mode_key)
                 or config.get("subagent_model")
                 or config.get("model", ""))

    client = get_client(provider)

    # Build system prompt
    mode_prompt = _MODE_PROMPTS.get(task.mode, _MODE_PROMPTS["general"])
    shared_context = shared_mem.get_summary()

    system_parts = [
        mode_prompt,
        f"\nYour assigned task: {task.title}",
        f"Description: {task.description}",
    ]
    if shared_context:
        system_parts.append(f"\n{shared_context}")
    if workspace:
        system_parts.append(f"\nWorkspace: {workspace}")
        system_parts.append("Use absolute paths. Use cwd parameter for terminal commands.")

    system = "\n".join(system_parts)

    # Build tool list
    allowed = _MODE_TOOLS.get(task.mode)
    if allowed is not None:
        all_schemas = registry.get_schemas()
        tools_list = [t for t in all_schemas if t["function"]["name"] in allowed]
    else:
        tools_list = registry.get_schemas()

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": task.description},
    ]

    MAX_ROUNDS = _MODE_MAX_ROUNDS.get(task.mode, 40)
    activity_log: list[str] = []  # Human-readable log of what happened
    last_calls: list[str] = []    # Recent (tool+args_hash) for stuck detection

    def _log(entry: str):
        activity_log.append(entry)

    def _args_key(name: str, args_str: str) -> str:
        """Short fingerprint of a tool call for stuck detection."""
        try:
            a = json.loads(args_str)
            # Use only the most identifying fields
            sig = f"{name}:{list(a.items())[:3]}"
        except Exception:
            sig = f"{name}:{args_str[:80]}"
        return sig

    # Tool loop
    for round_num in range(1, MAX_ROUNDS + 1):
        # Emit round start so UI shows live progress
        _notify_status(task.task_id, "running", {
            "round": round_num,
            "max_rounds": MAX_ROUNDS,
            "activity": activity_log[-3:],
        })

        # Mid-run history pruning — keep context from ballooning.
        # Always preserve: system message [0], first user message [1],
        # and the last N tool-call exchanges. Drop the middle.
        _KEEP_TAIL = 10   # tool-result pairs to keep from recent history
        if len(messages) > 2 + _KEEP_TAIL * 2:
            head = messages[:2]
            tail = messages[-(  _KEEP_TAIL * 2):]
            dropped = len(messages) - len(head) - len(tail)
            summary_msg = {
                "role": "user",
                "content": (
                    f"[{dropped} earlier messages pruned to manage context. "
                    f"Activity so far: {'; '.join(activity_log[:-3])}]"
                ),
            }
            messages = head + [summary_msg] + tail
            _log(f"[round {round_num}] pruned {dropped} old messages from context")

        try:
            kwargs = {
                "model": model,
                "messages": messages,
                "max_tokens": 4096,
                "temperature": 0.3,
            }
            if tools_list:
                kwargs["tools"] = tools_list

            response = client.chat.completions.create(**kwargs)
        except Exception as api_err:
            _log(f"[round {round_num}] API error: {api_err}")
            raise RuntimeError(
                f"API call failed on round {round_num}: {api_err}\n\n"
                f"Activity log:\n" + "\n".join(activity_log)
            ) from api_err

        msg = response.choices[0].message

        if not getattr(msg, "tool_calls", None):
            result_text = msg.content or "(no output)"
            _log(f"[round {round_num}] Finished — {result_text[:80]}")
            return result_text

        # Serialize assistant message
        asst_dict = {"role": "assistant", "content": msg.content or ""}
        tc_list = []
        for tc in msg.tool_calls:
            tc_id = tc.id if hasattr(tc, "id") else tc.get("id", "")
            fn = tc.function if hasattr(tc, "function") else tc.get("function", {})
            fn_name = fn.name if hasattr(fn, "name") else fn.get("name", "")
            fn_args = fn.arguments if hasattr(fn, "arguments") else fn.get("arguments", "")
            tc_list.append({
                "id": tc_id, "type": "function",
                "function": {"name": fn_name, "arguments": fn_args},
            })
        asst_dict["tool_calls"] = tc_list
        messages.append(asst_dict)

        # Execute tool calls
        for tc_dict in tc_list:
            name = tc_dict["function"]["name"]
            args_str = tc_dict["function"]["arguments"]
            tc_id = tc_dict["id"]

            # Stuck detection — same call 3 times in a row = bail
            call_key = _args_key(name, args_str)
            last_calls.append(call_key)
            if len(last_calls) > 9:
                last_calls.pop(0)
            if last_calls.count(call_key) >= 3:
                _log(f"[round {round_num}] STUCK — '{name}' called identically 3+ times")
                raise RuntimeError(
                    f"Sub-agent stuck: tool '{name}' called with identical arguments 3 times in a row.\n\n"
                    f"Activity log:\n" + "\n".join(activity_log)
                )

            # Brief args summary for log/UI
            try:
                a = json.loads(args_str)
                args_summary = ", ".join(
                    f"{k}={str(v)[:40]}" for k, v in list(a.items())[:2]
                )
            except Exception:
                args_summary = args_str[:60]

            log_entry = f"[round {round_num}] {name}({args_summary})"
            _log(log_entry)

            # Notify UI with current tool
            _notify_status(task.task_id, "running", {
                "round": round_num,
                "max_rounds": MAX_ROUNDS,
                "current_tool": name,
                "current_args": args_summary,
                "activity": activity_log[-3:],
            })

            # Notify terminal tab if it's a shell command
            if name == "terminal":
                try:
                    a = json.loads(args_str) if args_str else {}
                    _notify_terminal(task.task_id, a.get("command", ""), a.get("cwd", workspace))
                except Exception:
                    pass

            if allowed is not None and name not in allowed:
                tool_result = json.dumps({"error": f"Tool '{name}' not available in {task.mode} mode"})
                _log(f"  -> blocked (not in {task.mode} mode)")
            else:
                try:
                    args = json.loads(args_str) if args_str else {}
                    # Inject workspace cwd for terminal
                    if name == "terminal" and "cwd" not in args and workspace:
                        args["cwd"] = workspace
                    # Inject workspace path for file tools
                    if name in ("file_read", "file_write", "file_edit") and "path" in args:
                        p = args["path"]
                        if p and not os.path.isabs(p):
                            args["path"] = os.path.join(workspace, p)
                    tool_result = registry.execute(name, args)
                    # Log a brief result preview
                    try:
                        rd = json.loads(tool_result)
                        preview = rd.get("error") or rd.get("output", "")[:60] or rd.get("results", "")[:60] or "ok"
                    except Exception:
                        preview = tool_result[:60]
                    _log(f"  -> {str(preview)[:80]}")
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)})
                    _log(f"  -> ERROR: {e}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc_id,
                "content": tool_result[:16000],
            })

    _log(f"[round {MAX_ROUNDS}] Hit max rounds limit")
    raise RuntimeError(
        f"Sub-agent reached {MAX_ROUNDS}-round limit without finishing.\n\n"
        f"Activity log:\n" + "\n".join(activity_log)
    )


def _run_explore_single_shot(task: SubTask, model: str, provider: str,
                              workspace: str = "") -> str:
    """Single-shot explore: pre-read files in Python, one cheap LLM call
    produces all summaries in delimited format. Bypasses the agent loop —
    no tool calls, no rounds, no context bloat.

    task.extra must contain:
      - "files": list[str] of absolute paths
      - "query": str (focus for the summary)
      - "max_per_file_chars": int (truncation limit per file, optional)
    """
    from core.providers import get_client

    files = task.extra.get("files", [])
    query = task.extra.get("query") or "Summarize the file's purpose and key contents."
    max_per_file = int(task.extra.get("max_per_file_chars", 60000))

    if not files:
        return "(no files provided to explore)"

    # Read all files in this batch (Python, free)
    blocks: list[str] = []
    for fp in files:
        try:
            content = Path(fp).read_text(encoding="utf-8", errors="replace")
            if len(content) > max_per_file:
                content = (content[:max_per_file]
                           + f"\n\n[... truncated, full file is {len(content)} chars]")
            blocks.append(f"=== FILE: {fp} ===\n{content}")
        except Exception as e:
            blocks.append(f"=== FILE: {fp} ===\n[ERROR reading: {e}]")

    files_payload = "\n\n".join(blocks)

    system_prompt = (
        "You are a fast file-summarizer. Read each file below and produce a tight, "
        "factual summary.\n\n"
        "CRITICAL RULES:\n"
        "1. Summarize each file INDEPENDENTLY. The files in this batch may be "
        "entirely unrelated. Do NOT synthesize a unified narrative across them. "
        "Do NOT speculate about how they relate to each other.\n"
        "2. Output format — use EXACTLY this structure, one section per file:\n"
        "### <full file path>\n"
        "<2-4 sentence summary>\n\n"
        "### <next full file path>\n"
        "<2-4 sentence summary>\n\n"
        "3. Focus each summary on the user's query. Skip details unrelated to it.\n"
        "4. No preamble, no closing remarks, no markdown other than the ### headers."
    )

    user_msg = (
        f"Query (focus each summary on this): {query}\n\n"
        f"Files to summarize ({len(files)}):\n\n{files_payload}"
    )

    # Notify UI we're working
    _notify_status(task.task_id, "running", {
        "round": 1,
        "max_rounds": 1,
        "current_tool": "summarize",
        "current_args": f"{len(files)} file(s)",
        "activity": [f"summarizing {len(files)} file(s)"],
    })

    client = get_client(provider)

    _MAX_RETRIES = 3
    _TRANSIENT_CODES = {429, 500, 502, 503, 504}

    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=4096,
                temperature=0.2,
            )
            return resp.choices[0].message.content or "(no output)"

        except Exception as e:
            status_code = (
                getattr(e, "status_code", None)
                or getattr(getattr(e, "response", None), "status_code", None)
            )
            err_str = str(e).lower()
            is_transient = (
                status_code in _TRANSIENT_CODES
                or "rate limit" in err_str
                or "too many requests" in err_str
                or "timeout" in err_str
                or "connection" in err_str
                or "service unavailable" in err_str
            )

            if not is_transient or attempt >= _MAX_RETRIES:
                raise

            wait = 2 ** attempt  # 1s, 2s, 4s
            _notify_status(task.task_id, "running", {
                "round": 1,
                "max_rounds": 1,
                "current_tool": "summarize",
                "current_args": f"{len(files)} file(s) [retry {attempt + 1}/{_MAX_RETRIES}]",
                "activity": [f"rate-limited, retrying in {wait}s (attempt {attempt + 1})"],
            })
            time.sleep(wait)


# ── Orchestrator ────────────────────────────────────────────────────────

class Orchestrator:
    """Decomposes a goal into tasks, dispatches them with dependency awareness."""

    def __init__(self, max_concurrent: int = 3):
        self.queue = TaskQueue()
        self.shared_mem = SharedMemory()
        self._pool = ThreadPoolExecutor(max_workers=max_concurrent,
                                         thread_name_prefix="subagent")
        self._job_id = str(uuid.uuid4())[:8]
        self._model = ""
        self._provider = ""
        self._workspace = ""
        self._tasks_by_id: dict[str, SubTask] = {}
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def decompose(self, goal: str, model: str = "", provider: str = "",
                  workspace: str = "") -> list[SubTask]:
        """Use an LLM to decompose a goal into structured tasks.

        Returns the list of SubTasks (also added to the queue).
        """
        self._model = model
        self._provider = provider
        self._workspace = workspace

        from core.providers import get_client

        if not provider or not model:
            config_path = Path(__file__).parent.parent / "config.json"
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                config = {}
            # decompose_model is a cheap fast model just for JSON task-graph generation
            provider = (provider
                        or config.get("subagent_decompose_provider")
                        or config.get("subagent_provider")
                        or config.get("provider", "openrouter"))
            model = (model
                     or config.get("subagent_decompose_model")
                     or config.get("subagent_model")
                     or config.get("model", ""))
            self._provider = provider
            self._model = model

        client = get_client(provider)

        decomp_prompt = f"""Decompose this goal into concrete sub-tasks for parallel execution.

Goal: {goal}

Return a JSON array of tasks. Each task has:
- "id": short unique string (e.g. "t1", "t2")
- "title": brief title
- "description": detailed instructions for the sub-agent
- "mode": one of "search", "code", "plan", "test", "general"
- "depends_on": array of task IDs this depends on (empty if independent)

Rules:
- Tasks with no dependencies will run in PARALLEL
- Keep it to 2-6 tasks (don't over-decompose)
- Make descriptions self-contained — the sub-agent won't see the original goal
- Use "search" mode for read-only investigation
- Use "code" mode for file modifications
- Use "test" mode for running and analyzing tests
- Use "plan" mode for analysis without changes
- Do not use emoji or decorative pictographs in titles or descriptions; plain text only

Return ONLY the JSON array, no other text."""

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You decompose goals into parallel sub-tasks. Return only valid JSON."},
                    {"role": "user", "content": decomp_prompt},
                ],
                max_tokens=2048,
                temperature=0.2,
            )
            raw = resp.choices[0].message.content or "[]"

            # Extract JSON from response (might have markdown fences)
            raw = raw.strip()
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
                if raw.endswith("```"):
                    raw = raw[:-3]
                raw = raw.strip()

            tasks_data = json.loads(raw)
        except Exception as e:
            # Fallback: single task
            tasks_data = [{
                "id": "t1",
                "title": _strip_emoji_pictographs(goal[:80]),
                "description": _strip_emoji_pictographs(goal),
                "mode": "general",
                "depends_on": [],
            }]

        tasks = []
        for td in tasks_data:
            raw_title = _strip_emoji_pictographs(str(td.get("title", "") or ""))
            raw_desc = _strip_emoji_pictographs(str(td.get("description", "") or ""))
            task = SubTask(
                task_id=f"{self._job_id}-{td.get('id', str(uuid.uuid4())[:4])}",
                title=raw_title or "task",
                description=raw_desc or raw_title or "task",
                mode=td.get("mode", "general"),
                depends_on=[f"{self._job_id}-{d}" for d in td.get("depends_on", [])],
            )
            tasks.append(task)
            self.queue.add(task)
            self._tasks_by_id[task.task_id] = task

        return tasks

    def add_single_task(self, title: str, description: str,
                        mode: str = "general") -> SubTask:
        """Add a single task without decomposition (simple mode)."""
        if not self._model:
            config_path = Path(__file__).parent.parent / "config.json"
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self._provider = self._provider or config.get("provider", "openrouter")
            self._model = self._model or config.get("model", "")

        task = SubTask(
            task_id=f"{self._job_id}-{str(uuid.uuid4())[:4]}",
            title=_strip_emoji_pictographs(title or ""),
            description=_strip_emoji_pictographs(description or ""),
            mode=mode,
        )
        self.queue.add(task)
        self._tasks_by_id[task.task_id] = task
        return task

    def add_explore_task(self, files: list, query: str,
                         max_per_file_chars: int = 60000) -> SubTask:
        """Add a single-shot explore task — pre-reads files, one cheap LLM call,
        no agent loop. Caller is responsible for batching."""
        if not self._model or not self._provider:
            config_path = Path(__file__).parent.parent / "config.json"
            try:
                config = json.loads(config_path.read_text(encoding="utf-8"))
            except Exception:
                config = {}
            self._provider = (self._provider
                              or config.get("subagent_explore_provider")
                              or config.get("subagent_provider")
                              or config.get("provider", "openrouter"))
            self._model = (self._model
                           or config.get("subagent_explore_model")
                           or config.get("subagent_model")
                           or config.get("model", ""))

        if len(files) == 1:
            title = f"Read {Path(files[0]).name}"
        else:
            title = f"Read {len(files)} files"

        task = SubTask(
            task_id=f"{self._job_id}-{str(uuid.uuid4())[:4]}",
            title=title,
            description=f"Summarize {len(files)} file(s) for: {query[:120]}",
            mode="explore",
            extra={
                "files": list(files),
                "query": query,
                "max_per_file_chars": max_per_file_chars,
            },
        )
        self.queue.add(task)
        self._tasks_by_id[task.task_id] = task
        return task

    def execute(self) -> dict:
        """Run the task queue to completion. Returns summary dict.

        Dispatches tasks in rounds: each round, all ready tasks run in
        parallel. When a round completes, newly unblocked tasks become
        ready for the next round.
        """
        round_num = 0
        max_rounds = 20

        while not self.queue.is_complete() and round_num < max_rounds:
            round_num += 1
            ready = self.queue.get_ready()
            if not ready:
                # No tasks ready but not complete — stuck (circular deps or all failed)
                break

            # Dispatch all ready tasks in parallel
            futures = {}
            for task in ready:
                self.queue.mark_running(task.task_id)
                _notify_status(task.task_id, "running", task.to_dict())

                future = self._pool.submit(
                    self._execute_task, task
                )
                futures[task.task_id] = future

            # Wait for this round to complete
            for task_id, future in futures.items():
                try:
                    future.result(timeout=300)  # 5 min per task max
                except Exception as e:
                    task = self._tasks_by_id.get(task_id)
                    if task and task.status == TaskStatus.RUNNING:
                        self.queue.fail(task_id, str(e))
                        _notify_status(task_id, "failed", {"error": str(e)})

        # Build summary
        all_tasks = self.queue.all_tasks()
        completed = [t for t in all_tasks if t.status == TaskStatus.COMPLETED]
        failed = [t for t in all_tasks if t.status == TaskStatus.FAILED]

        summary = {
            "job_id": self._job_id,
            "total_tasks": len(all_tasks),
            "completed": len(completed),
            "failed": len(failed),
            "rounds": round_num,
            "tasks": [t.to_dict() for t in all_tasks],
            "results": {t.task_id: t.result[:500] for t in completed},
        }

        # Release thread-pool workers now — no need to keep them alive
        self._pool.shutdown(wait=False)

        # Evict from registry after a short delay so status/wait queries still work
        def _evict():
            time.sleep(30)
            with _orch_lock:
                _orchestrators.pop(self._job_id, None)
        threading.Thread(target=_evict, daemon=True).start()

        return summary

    def _execute_task(self, task: SubTask):
        """Execute a single task in the pool."""
        try:
            if task.mode == "explore":
                # Single-shot path: pre-read files, one LLM call, no tool loop.
                result = _run_explore_single_shot(
                    task,
                    model=self._model,
                    provider=self._provider,
                    workspace=self._workspace,
                )
            else:
                result = _run_single_agent(
                    task,
                    self.shared_mem,
                    model=self._model,
                    provider=self._provider,
                    workspace=self._workspace,
                )
            self.queue.complete(task.task_id, result)
            self.shared_mem.write(f"task:{task.task_id}", result)
            _notify_status(task.task_id, "completed", {
                **task.to_dict(), "result_preview": result[:200],
            })
        except Exception as e:
            err_msg = str(e)
            self.queue.fail(task.task_id, err_msg)
            _notify_status(task.task_id, "failed", {"error": err_msg, "full_error": err_msg})

    def get_status(self) -> dict:
        """Get current status of all tasks."""
        return {
            "job_id": self._job_id,
            "tasks": [t.to_dict() for t in self.queue.all_tasks()],
            "complete": self.queue.is_complete(),
        }

    def shutdown(self):
        self._pool.shutdown(wait=False)


# ── Global orchestrator registry ────────────────────────────────────────

_orchestrators: dict[str, Orchestrator] = {}
_orch_lock = threading.Lock()


def get_orchestrator(job_id: str = "") -> Orchestrator:
    """Get or create an orchestrator."""
    with _orch_lock:
        if job_id and job_id in _orchestrators:
            return _orchestrators[job_id]
        orch = Orchestrator()
        _orchestrators[orch._job_id] = orch
        return orch


def get_existing(job_id: str) -> Orchestrator | None:
    with _orch_lock:
        return _orchestrators.get(job_id)


def cleanup_old(max_age: float = 3600):
    """Remove old completed orchestrators."""
    now = time.time()
    with _orch_lock:
        stale = []
        for jid, orch in _orchestrators.items():
            if orch.queue.is_complete():
                tasks = orch.queue.all_tasks()
                if tasks and all(now - t.completed_at > max_age
                                 for t in tasks if t.completed_at):
                    stale.append(jid)
        for jid in stale:
            _orchestrators[jid].shutdown()
            del _orchestrators[jid]
