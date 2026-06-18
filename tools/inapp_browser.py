"""
In-app browser bridge — lets the `browser` tool drive the SAME visible
QtWebEngine panel the user is looking at (a dedicated "Agent" tab), instead of
a separate headless/popup Chromium.

Why this instead of CDP: QtWebEngine doesn't reliably expose a browser-level
CDP endpoint for Playwright's connect_over_cdp, and CDP-created pages aren't
attached to a visible widget anyway. Driving the panel's own QWebEnginePage on
the Qt main thread is guaranteed visible AND uses the user's logged-in session.

Pattern mirrors workspace_browser._BrowserGrabBridge: a QObject on the main
thread; the agent's worker thread emits a queued signal and blocks on an Event
until the main-thread handler returns a result dict.
"""

import threading

from PyQt6.QtCore import QObject, pyqtSignal


class _InAppBridge(QObject):
    """Main-thread bridge to BrowserWorkspacePanel.run_agent_action."""

    _request = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._handler = None          # set by ChatWindow → panel.run_agent_action
        self._pending: dict | None = None
        self._result: dict | None = None
        self._event = threading.Event()
        self._lock = threading.Lock()  # serialize actions (one tab, sequential)
        self._request.connect(self._handle)

    def _handle(self):
        """Runs on the GUI main thread (may nest a QEventLoop for JS readback)."""
        try:
            if self._handler is None:
                self._result = {"error": "in-app browser handler not wired"}
            else:
                self._result = self._handler(self._pending or {})
        except Exception as e:
            self._result = {"error": f"in-app browser error: {type(e).__name__}: {e}"}
        finally:
            self._event.set()

    def available(self) -> bool:
        return self._handler is not None

    def call(self, req: dict, timeout: float = 30) -> dict:
        with self._lock:
            self._pending = req
            self._result = None
            self._event.clear()
            self._request.emit()                 # queued → main thread
            if not self._event.wait(timeout):
                return {"error": f"in-app browser timeout after {timeout}s"}
            return self._result if isinstance(self._result, dict) else {"error": "no result"}


# Created at import time on the main thread (tools/__init__.py is imported from main.py).
_bridge = _InAppBridge()


def set_inapp_handler(fn):
    """Called by ChatWindow to wire BrowserWorkspacePanel.run_agent_action."""
    _bridge._handler = fn


def inapp_available() -> bool:
    return _bridge.available()


def inapp_call(req: dict, timeout: float = 30) -> dict:
    return _bridge.call(req, timeout)
