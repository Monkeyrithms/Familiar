"""
Event bus — lightweight pub/sub for decoupling tools from side effects.

Tools publish events (e.g., "file.changed"), subscribers react without
the tool needing to know who's listening. This makes it easy to add
new integrations (LSP validation, UI refresh, logging) without editing
every tool.

Events:
  file.changed    - A file was created, written, or edited
  file.deleted    - A file was deleted
  tool.started    - A tool began execution
  tool.completed  - A tool finished execution
  tool.error      - A tool encountered an error

Usage:
  from core.event_bus import bus

  # Subscribe (at startup)
  bus.on("file.changed", lambda path, **kw: lint_file(path))

  # Publish (from any tool)
  bus.emit("file.changed", path="/some/file.py", tool="file_edit")
"""

import threading
from collections import defaultdict
from typing import Callable, Any


class EventBus:
    """Thread-safe publish/subscribe event bus."""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._lock = threading.Lock()

    def on(self, event: str, handler: Callable) -> None:
        """Subscribe a handler to an event.

        Handler signature: handler(**kwargs) — receives all keyword args
        passed to emit().
        """
        with self._lock:
            if handler not in self._handlers[event]:
                self._handlers[event].append(handler)

    def off(self, event: str, handler: Callable) -> None:
        """Unsubscribe a handler from an event."""
        with self._lock:
            try:
                self._handlers[event].remove(handler)
            except ValueError:
                pass

    def emit(self, event: str, **kwargs) -> None:
        """Publish an event to all subscribers.

        Handlers run synchronously in subscription order. Exceptions in
        handlers are caught and logged — one bad handler won't break others.
        """
        with self._lock:
            handlers = list(self._handlers.get(event, []))

        for handler in handlers:
            try:
                handler(**kwargs)
            except Exception as e:
                # Don't let subscriber errors propagate to the publisher
                print(f"[EventBus] Handler error on '{event}': {e}")

    def emit_async(self, event: str, **kwargs) -> None:
        """Publish an event asynchronously (fire-and-forget).

        Handlers run in a daemon thread — useful for non-critical side
        effects like UI updates or logging that shouldn't block the tool.
        """
        def _run():
            self.emit(event, **kwargs)

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    def clear(self, event: str | None = None) -> None:
        """Remove all handlers for an event, or all handlers if event is None."""
        with self._lock:
            if event is None:
                self._handlers.clear()
            else:
                self._handlers.pop(event, None)

    def list_events(self) -> dict[str, int]:
        """Return a dict of event names and their subscriber counts."""
        with self._lock:
            return {event: len(handlers)
                    for event, handlers in self._handlers.items()
                    if handlers}


# Singleton instance
bus = EventBus()


# ── Default subscribers (wired at import time) ─────────────────────────

_LSP_DEBOUNCE_S = 0.4
_lsp_debounce_lock = threading.Lock()
_lsp_debounce_timers: dict[str, threading.Timer] = {}


def _schedule_lsp_notify(abs_path: str) -> None:
    """Debounce LSP didChange — bursts of edits only read disk once per path."""
    def _fire():
        try:
            from core.lsp_client import lsp_manager
            from pathlib import Path
            server = lsp_manager.get_server(abs_path, str(Path(abs_path).parent))
            if server:
                server.notify_change(abs_path)
        except Exception:
            pass
        finally:
            with _lsp_debounce_lock:
                _lsp_debounce_timers.pop(abs_path, None)

    with _lsp_debounce_lock:
        existing = _lsp_debounce_timers.get(abs_path)
        if existing:
            existing.cancel()
        t = threading.Timer(_LSP_DEBOUNCE_S, _fire)
        t.daemon = True
        _lsp_debounce_timers[abs_path] = t
        t.start()


def _setup_default_subscribers():
    """Register default event handlers. Called once at import."""

    def _on_file_changed_viewer(path: str = "", original=None, tool: str = "", **_):
        """Notify the file viewer UI on file changes.

        ANY agent-attributed mutation (original is not None) routes through the
        edit handler, which surfaces the panel and applies the diff overlay.
        Only tool-less / watcher-triggered changes (original is None) take the
        lightweight refresh path. Listing specific tool names here creates a
        silent failure mode every time a new writing tool is added.
        """
        if not path:
            return
        try:
            if original is not None:
                from tools.file_viewer import notify_edit
                # Runs synchronously on the tool's worker thread, where
                # current_agent() is bound to the editing column's agent — pass
                # it through so the diff card lands in THAT column's chat pane.
                agent = None
                try:
                    from core.agent import current_agent
                    agent = current_agent()
                except Exception:
                    agent = None
                notify_edit(path, original, agent)
            else:
                from tools.file_viewer import notify_file_changed
                notify_file_changed(path)
        except Exception as e:
            print(f"[event_bus] file.changed viewer-notify failed for {path!r} "
                  f"(tool={tool!r}): {e}")

    def _on_file_changed_lsp(path: str = "", **_):
        """Notify LSP servers of file changes (debounced per path)."""
        if not path:
            return
        import os
        _schedule_lsp_notify(os.path.abspath(path))

    bus.on("file.changed", _on_file_changed_viewer)
    bus.on("file.changed", _on_file_changed_lsp)


_setup_default_subscribers()
