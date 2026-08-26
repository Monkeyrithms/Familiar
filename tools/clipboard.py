"""
Clipboard tool — read/write the system clipboard.

QClipboard is GUI-thread-only. Tools execute on worker threads, so all
clipboard access is marshaled to the main thread via a signal bridge
(same pattern as tools/screenshot.py). Touching QClipboard off-thread
was one of the native heap-corruption sources behind long-run crashes.
"""

import json
import threading

from PyQt6.QtCore import QObject, pyqtSignal
from tools.registry import registry


class _ClipboardBridge(QObject):
    """Lives on the main thread (created at import time from main.py)."""

    _request = pyqtSignal(str, str)  # action, text

    def __init__(self):
        super().__init__()
        self._result: str | None = None
        self._error: str | None = None
        self._event = threading.Event()
        self._request.connect(self._handle)

    def _handle(self, action: str, text: str):
        """Runs on the main thread."""
        self._result = None
        self._error = None
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if not app:
                self._error = "No QApplication instance"
            else:
                cb = app.clipboard()
                if action == "read":
                    self._result = cb.text()
                else:  # write
                    cb.setText(text)
                    self._result = ""
        except Exception as e:
            self._error = str(e)
        self._event.set()

    def run(self, action: str, text: str = "") -> tuple[str | None, str | None]:
        """Call from any thread. Returns (result, error)."""
        self._event.clear()
        self._request.emit(action, text)
        if not self._event.wait(timeout=5):
            return None, "Clipboard access timed out (GUI thread busy)"
        return self._result, self._error


_bridge = _ClipboardBridge()


def clipboard(action: str, text: str = "") -> str:
    """Read or write the system clipboard (GUI-thread-marshaled)."""
    if action == "read":
        content, err = _bridge.run("read")
        if err:
            return json.dumps({"error": err})
        content = content or ""
        return json.dumps({"content": content, "length": len(content)})
    elif action == "write":
        if not text:
            return json.dumps({"error": "text required for write"})
        _, err = _bridge.run("write", text)
        if err:
            return json.dumps({"error": err})
        return json.dumps({
            "status": "done",
            "message": f"Wrote {len(text)} chars to the system clipboard. "
                       f"No further tool calls — confirm to the user in text.",
            "written": True, "length": len(text),
        })
    return json.dumps({"error": "action must be 'read' or 'write'"})


registry.register(
    name="clipboard",
    description="Read|write system clipboard.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write"], "description": "read | write."},
            "text": {"type": "string", "description": "Text for write."},
        },
        "required": ["action"],
    },
    execute=clipboard,
)
