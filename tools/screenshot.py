"""
Screenshot tool — capture the Agent's own window via Qt grab().

Uses the same MainWindow.grab() as the titlebar screenshot button.
A signal-slot bridge created at import time (main thread) ensures
grab() always runs on the main thread, even when called from the
inference background thread.
"""

import base64
import json
import tempfile
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from tools.registry import registry

MAX_WIDTH = 900


class _GrabBridge(QObject):
    """Lives on the main thread. When signaled from any thread, grabs the MainWindow."""
    _request = pyqtSignal()
    flash_requested = pyqtSignal()  # fires camera flash on MainWindow

    def __init__(self):
        super().__init__()
        self._result = None
        self._event = threading.Event()
        self._request.connect(self._handle)
        self.flash_requested.connect(self._do_flash)

    def _handle(self):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        self._result = None
        if app:
            for w in app.topLevelWidgets():
                if w.__class__.__name__ == "MainWindow" and w.isVisible():
                    self._result = w.grab()
                    break
        self._event.set()

    def _do_flash(self):
        """Trigger the camera flash on the MainWindow (must run on main thread)."""
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            for w in app.topLevelWidgets():
                if w.__class__.__name__ == "MainWindow" and w.isVisible():
                    if hasattr(w, "_do_camera_flash"):
                        w._do_camera_flash()
                    break

    def grab(self):
        self._result = None
        self._event.clear()
        self._request.emit()
        self._event.wait(timeout=5)
        return self._result


# Created at import time on the main thread (import tools in main.py)
_bridge = _GrabBridge()


def screenshot(prompt: str = "", analyze: bool = True) -> str:
    """Capture a screenshot of the Agent window and return it as image content."""
    from PyQt6.QtCore import Qt, QBuffer, QIODevice

    pixmap = _bridge.grab()
    if pixmap is None or pixmap.isNull():
        return json.dumps({"error": "Could not capture the Agent window."})

    # Resize if needed
    if pixmap.width() > MAX_WIDTH:
        pixmap = pixmap.scaledToWidth(MAX_WIDTH, Qt.TransformationMode.SmoothTransformation)

    # Encode as JPEG
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "JPEG", 80)
    jpeg_bytes = bytes(buf.data())
    buf.close()

    # Play snapshot sound + camera flash
    try:
        from core.sounds import play
        play("snapshot.mp3")
    except Exception:
        pass
    try:
        _bridge.flash_requested.emit()
    except Exception:
        pass

    # Save to temp file so it can be used as image_path in chat
    tmp_path = Path(tempfile.gettempdir()) / "agent_screenshot.jpg"
    tmp_path.write_bytes(jpeg_bytes)

    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")

    result = {
        "captured": True,
        "image_path": str(tmp_path),
        "size_kb": round(len(jpeg_bytes) / 1024, 1),
        "data_url": f"data:image/jpeg;base64,{b64}",
    }

    if analyze:
        try:
            from tools.vision import vision_analyze
            analysis_prompt = prompt or (
                "Describe what you see in this screenshot of the Agent UI. "
                "Note any issues with layout, styling, or content."
            )
            analysis = json.loads(vision_analyze(result["data_url"], analysis_prompt))
            result["analysis"] = analysis.get("analysis", "")
            if "error" in analysis:
                result["analysis_error"] = analysis["error"]
        except Exception as e:
            result["analysis_error"] = str(e)

    # Strip the data_url from the result to avoid bloating context
    result.pop("data_url", None)

    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="screenshot",
    description=(
        "Capture screenshot of the Agent's own window (UI inspection: layout, styling, rendering). "
        "Optional vision-model analysis. Saved to temp file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to look for (default: general UI assessment)."},
            "analyze": {"type": "boolean", "description": "Run vision analysis (default true)."},
        },
    },
    execute=screenshot,
)
