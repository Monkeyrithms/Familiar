"""
Tool context system — gives tools access to session state, abort signals,
and live metadata streaming.

Inspired by opencode-dev's Tool.Context pattern. Every tool execution now
receives a ToolContext that carries:
  - abort_signal: threading.Event that fires when the user hits Stop
  - metadata():   push live progress updates to the UI during execution
  - session_id / message_id: conversation tracking
  - agent_name:   which agent is running
  - cwd:          current working directory
  - messages:     read-only access to conversation history (last N messages)

Tools opt-in by accepting a `ctx` keyword argument. The registry detects
this automatically and injects the context at call time.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Any


@dataclass
class ToolContext:
    """Context object passed to tools during execution."""

    # ── Identity ──
    session_id: str = ""
    message_id: str = ""
    agent_name: str = "Agent"
    tool_name: str = ""
    call_id: str = ""

    # ── Working directory ──
    cwd: str = ""

    # ── Abort signal ──
    # Set by the agent when the user hits Stop. Tools should check
    # `ctx.abort_signal.is_set()` periodically during long operations
    # and exit gracefully when True.
    abort_signal: threading.Event = field(default_factory=threading.Event)

    # ── Metadata streaming ──
    # The agent sets this callback. Tools call ctx.metadata({...}) to push
    # live updates (progress bars, partial output, status changes) to the UI.
    _metadata_callback: Callable[[dict], None] | None = None

    # ── Permission callback ──
    # Tools call ctx.ask(description) to request user approval for dangerous
    # actions. Returns True if approved, False if denied.
    _ask_callback: Callable[[str], bool] | None = None

    # ── Conversation context (read-only, last N messages) ──
    messages: list[dict] = field(default_factory=list)

    # ── Timing ──
    start_time: float = field(default_factory=time.time)

    @property
    def aborted(self) -> bool:
        """Check if the user has requested cancellation."""
        return self.abort_signal.is_set()

    def metadata(self, update: dict) -> None:
        """Push a live metadata update to the UI.

        Common fields:
          - output: str       partial output to display
          - description: str  what the tool is doing right now
          - progress: float   0.0-1.0 completion estimate
          - status: str       "running" | "waiting" | "done"
        """
        if self._metadata_callback:
            try:
                self._metadata_callback(update)
            except Exception:
                pass

    def ask(self, description: str) -> bool:
        """Request user permission for a dangerous action.

        Args:
            description: Human-readable description of what the tool wants to do.

        Returns:
            True if approved, False if denied. If no callback is set, returns True
            (permissive by default — the agent loop handles blocking).
        """
        if self._ask_callback:
            try:
                return self._ask_callback(description)
            except Exception:
                return True
        return True

    @property
    def elapsed(self) -> float:
        """Seconds since this tool execution started."""
        return time.time() - self.start_time

    def check_abort(self) -> None:
        """Raise InterruptedError if the user has requested cancellation.
        Convenience method for tools that want exception-based abort handling."""
        if self.aborted:
            raise InterruptedError(f"Tool '{self.tool_name}' aborted by user")


# ── Global abort signal (shared across all tools in a turn) ──

_global_abort = threading.Event()


def get_global_abort() -> threading.Event:
    """Get the global abort signal. The agent loop sets this when Stop is pressed."""
    return _global_abort


def reset_global_abort():
    """Clear the abort signal at the start of each turn."""
    _global_abort.clear()


def trigger_abort():
    """Signal all running tools to stop."""
    _global_abort.set()


# ── Factory for creating contexts ──

def make_context(
    tool_name: str = "",
    cwd: str = "",
    session_id: str = "",
    message_id: str = "",
    agent_name: str = "Agent",
    call_id: str = "",
    abort_signal: threading.Event | None = None,
    metadata_callback: Callable[[dict], None] | None = None,
    ask_callback: Callable[[str], bool] | None = None,
    messages: list[dict] | None = None,
) -> ToolContext:
    """Create a ToolContext for a tool execution."""
    return ToolContext(
        session_id=session_id,
        message_id=message_id,
        agent_name=agent_name,
        tool_name=tool_name,
        call_id=call_id,
        cwd=cwd,
        abort_signal=abort_signal or _global_abort,
        _metadata_callback=metadata_callback,
        _ask_callback=ask_callback,
        messages=messages or [],
    )
