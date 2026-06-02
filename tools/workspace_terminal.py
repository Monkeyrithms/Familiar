"""
Workspace integrated terminal — Qt signal bridge + agent tools to show / send / read.

The UI owns real shells in ``TerminalWorkspacePanel``; this module emits
thread-safe signals that ChatWindow connects to the panel.
"""

import json
import threading

from PyQt6.QtCore import QObject, pyqtSignal
from tools.registry import registry


class _WorkspaceTerminalBridge(QObject):
    show_requested = pyqtSignal()
    new_tab_requested = pyqtSignal()
    send_requested = pyqtSignal(str)


bridge = _WorkspaceTerminalBridge()


class _TerminalReadBridge(QObject):
    """Blocking read bridge — inference thread signals, main thread reads, event unlocks."""
    _request = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._result: str = ""
        self._event = threading.Event()
        self._read_fn = None   # set by ChatWindow: TerminalWorkspacePanel.get_active_text
        self._request.connect(self._handle)

    def _handle(self):
        self._result = ""
        if self._read_fn:
            try:
                self._result = self._read_fn() or ""
            except Exception:
                pass
        self._event.set()

    def read(self) -> str:
        self._result = ""
        self._event.clear()
        self._request.emit()
        self._event.wait(timeout=5)
        return self._result


read_bridge = _TerminalReadBridge()


def set_read_handler(fn):
    """Called by ChatWindow to wire up TerminalWorkspacePanel.get_active_text."""
    read_bridge._read_fn = fn


# ──────────────────────────────────────────────────────────────────────
# Agent background tabs — dedicated terminal tabs for agent-launched
# long-running processes. Each command gets its own tab; the shell inside
# the tab runs the command. "Kill" = close the tab (the tab's stop()
# already does taskkill /T /F → kills the whole tree).
#
# This replaces the old Popen-based _bg_processes registry: backgrounding
# is now a real terminal tab the user can see, type in, and close.
# ──────────────────────────────────────────────────────────────────────


class _AgentBgBridge(QObject):
    """Cross-thread bridge for agent bg-tab operations. Inference thread
    emits a request signal, waits on a threading.Event for the UI thread to
    handle it and populate _result, then continues."""

    _start_request = pyqtSignal(int, str, str)        # bg_id, command, cwd
    _kill_request = pyqtSignal(int)                   # bg_id
    _check_request = pyqtSignal(int)                  # bg_id
    _list_request = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()
        self._next_id = 1
        self._tabs: dict = {}     # bg_id -> {"session": IntegratedTerminalSession,
                                  #           "command": str, "cwd": str}
        self._result: dict = {}
        self._event = threading.Event()
        self._panel_resolver = None  # set by ChatWindow → returns TerminalWorkspacePanel
        self._cwd_resolver = None    # default cwd (workspace folder)

        self._start_request.connect(self._handle_start)
        self._kill_request.connect(self._handle_kill)
        self._check_request.connect(self._handle_check)
        self._list_request.connect(self._handle_list)

    # ── UI-thread handlers ──────────────────────────────────────────

    def _handle_start(self, bg_id: int, command: str, cwd: str):
        try:
            panel = self._panel_resolver() if self._panel_resolver else None
            if panel is None:
                self._result = {"error": "Workspace terminal panel not available."}
                return
            # Compose a tab name with bg_id + command preview
            preview = command.split()[0] if command.split() else "agent"
            preview = preview.replace("\\", "/").rsplit("/", 1)[-1][:20]
            tab_name = f"[bg{bg_id}] {preview}"
            session = panel.create_named_tab(tab_name, cwd or "")
            # Write the command into the shell as if the user typed it
            session.send_raw_line(command)
            self._tabs[bg_id] = {
                "session": session,
                "command": command,
                "cwd": cwd or "",
                "tab_name": tab_name,
            }
            self._result = {
                "bg_id": bg_id,
                "command": command,
                "tab": tab_name,
            }
        except Exception as e:
            self._result = {"error": f"Failed to start bg tab: {e}"}
        finally:
            self._event.set()

    def _handle_kill(self, bg_id: int):
        try:
            entry = self._tabs.get(bg_id)
            if not entry:
                self._result = {
                    "error": f"No background tab with bg_id={bg_id}.",
                    "active": self._summarize_locked(),
                }
                return
            session = entry["session"]
            panel = self._panel_resolver() if self._panel_resolver else None
            closed = False
            if panel is not None:
                closed = panel.close_session(session)
            if not closed:
                # Last-ditch: stop the session directly (kills shell tree)
                try:
                    session.stop()
                    closed = True
                except Exception:
                    pass
            self._tabs.pop(bg_id, None)
            self._result = {
                "killed": bg_id,
                "command": entry["command"][:120],
                "closed_tab": closed,
            }
        except Exception as e:
            self._result = {"error": f"Failed to kill bg tab: {e}"}
        finally:
            self._event.set()

    def _handle_check(self, bg_id: int):
        try:
            entry = self._tabs.get(bg_id)
            if not entry:
                self._result = {
                    "error": f"No background tab with bg_id={bg_id}.",
                    "active": self._summarize_locked(),
                }
                return
            session = entry["session"]
            running = self._session_running(session)
            text = ""
            try:
                text = session._term.toPlainText() or ""
            except Exception:
                pass
            tail = "\n".join(text.splitlines()[-30:])
            self._result = {
                "bg_id": bg_id,
                "command": entry["command"][:120],
                "tab": entry["tab_name"],
                "running": running,
                "recent_output": tail,
            }
        except Exception as e:
            self._result = {"error": f"Failed to check bg tab: {e}"}
        finally:
            self._event.set()

    def _handle_list(self):
        try:
            self._result = {
                "processes": self._summarize_locked(),
                "count": len(self._tabs),
            }
        except Exception as e:
            self._result = {"error": f"Failed to list bg tabs: {e}"}
        finally:
            self._event.set()

    @staticmethod
    def _session_running(session) -> bool:
        # Backend-agnostic: PTY sessions have no QProcess. is_running() covers both.
        try:
            return bool(session.is_running())
        except Exception:
            return False

    def _summarize_locked(self) -> list:
        out = []
        for bg_id, entry in list(self._tabs.items()):
            session = entry["session"]
            out.append({
                "bg_id": bg_id,
                "command": entry["command"][:120],
                "tab": entry["tab_name"],
                "running": self._session_running(session),
            })
        return out

    # Called by chat_widget when the user closes a tab themselves —
    # drop the registry entry so subsequent agent calls don't reference
    # a dead session.
    def forget_session(self, session):
        for bg_id, entry in list(self._tabs.items()):
            if entry.get("session") is session:
                self._tabs.pop(bg_id, None)

    # ── Inference-thread API (blocking) ─────────────────────────────

    def _allocate_id(self) -> int:
        with self._lock:
            bg_id = self._next_id
            self._next_id += 1
            return bg_id

    def _await(self, signal_emit) -> dict:
        with self._lock:
            self._result = {}
            self._event.clear()
        signal_emit()
        self._event.wait(timeout=10)
        return dict(self._result)

    def start_bg(self, command: str, cwd: str = "") -> dict:
        bg_id = self._allocate_id()
        return self._await(lambda: self._start_request.emit(bg_id, command, cwd))

    def kill_bg(self, bg_id: int) -> dict:
        return self._await(lambda: self._kill_request.emit(bg_id))

    def check_bg(self, bg_id: int) -> dict:
        return self._await(lambda: self._check_request.emit(bg_id))

    def list_bg(self) -> dict:
        return self._await(lambda: self._list_request.emit())

    def shutdown_all(self) -> int:
        """Close every registered agent bg tab. Called at app shutdown."""
        count = 0
        panel = self._panel_resolver() if self._panel_resolver else None
        for bg_id, entry in list(self._tabs.items()):
            session = entry.get("session")
            if session is not None:
                try:
                    if panel is not None:
                        panel.close_session(session)
                    else:
                        session.stop()
                    count += 1
                except Exception:
                    pass
        self._tabs.clear()
        return count


bg_bridge = _AgentBgBridge()


def set_panel_resolver(fn):
    """Called by ChatWindow to wire bg_bridge to the live TerminalWorkspacePanel."""
    bg_bridge._panel_resolver = fn


def workspace_terminal(action: str, command: str = "") -> str:
    """
    Control the right-panel integrated terminal (VS Code–style tabs).

    * ``show`` — bring the Terminal workspace page forward (user sees it).
    * ``new_tab`` — open another shell tab in the active workspace folder.
    * ``send`` — type one line into the active tab's shell (same as user pressing Enter).
    """
    action = (action or "").strip().lower()
    if action == "show":
        bridge.show_requested.emit()
        return json.dumps({"ok": True, "message": "Workspace terminal shown."}, ensure_ascii=False)
    if action == "new_tab":
        bridge.new_tab_requested.emit()
        return json.dumps({"ok": True, "message": "New terminal tab requested."}, ensure_ascii=False)
    if action == "send":
        cmd = (command or "").strip()
        if not cmd:
            return json.dumps({"error": "command is required for action=send."}, ensure_ascii=False)
        bridge.send_requested.emit(cmd)
        return json.dumps({"ok": True, "sent": cmd}, ensure_ascii=False)
    return json.dumps(
        {"error": f"Unknown action: {action!r}. Use show, new_tab, or send."},
        ensure_ascii=False,
    )


registry.register(
    name="workspace_terminal",
    description=(
        "Right-panel terminal. show|new_tab|send. "
        "Alt: terminal(to_workspace=true). ✗ when JSON capture needed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["show", "new_tab", "send"],
                "description": "show=focus page; new_tab=extra shell; send=line to active shell.",
            },
            "command": {
                "type": "string",
                "description": "Req for send: 1 line (e.g. 'dir' | 'ls -la').",
            },
        },
        "required": ["action"],
    },
    execute=workspace_terminal,
)


def read_terminal(lines: int = 200) -> str:
    """Read the scrollback of the active workspace terminal tab."""
    text = read_bridge.read()
    if not text:
        return json.dumps({"error": "Terminal is empty or not available."}, ensure_ascii=False)
    # Trim to the last N lines so the agent gets recent output without drowning in history
    all_lines = text.splitlines()
    if len(all_lines) > lines:
        all_lines = all_lines[-lines:]
        trimmed = True
    else:
        trimmed = False
    return json.dumps(
        {
            "lines": len(all_lines),
            "trimmed_to_last": lines if trimmed else None,
            "content": "\n".join(all_lines),
        },
        ensure_ascii=False,
    )


registry.register(
    name="read_terminal",
    description=(
        "Read scrollback of active workspace terminal tab. Default: last 200 lines. "
        "✓ check build output, error msgs, stack traces, anything visible in "
        "right-panel terminal — esp. when build/cmd running. "
        "Pair w/ `terminal` (run cmds) + `workspace_terminal send` (type into shell)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "lines": {
                "type": "integer",
                "description": "# trailing lines (default 200, max 500).",
            },
        },
        "required": [],
    },
    execute=read_terminal,
)
