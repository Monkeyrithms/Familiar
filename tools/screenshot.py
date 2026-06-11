"""
Screenshot tool — capture Familiar's own window, the whole desktop/a monitor, or
a specific external window, and surface it in chat.

All grabs run on the GUI thread via a signal-slot bridge (Qt requires it), even
when called from the inference background thread. The captured image is shown as
a card in chat; when the conversation is being mirrored over Familiar-Net, the
host also pushes the image to viewers so a remote operator can SEE the host's
screen ("ask the VPS to surface a screenshot").
"""

import base64
import json
import sys
import tempfile
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from tools.registry import registry

MAX_WIDTH = 1600   # roomier than the old 900 — desktop shots need the detail


class _GrabBridge(QObject):
    """Lives on the main thread. When signaled from any thread, grabs the
    requested target (self window / screen / external window)."""
    _request = pyqtSignal()
    flash_requested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._result = None
        self._target = "self"
        self._event = threading.Event()
        self._request.connect(self._handle)
        self.flash_requested.connect(self._do_flash)

    # ── main-thread grab dispatch ──
    def _handle(self):
        target = (self._target or "self").strip()
        self._result = None
        try:
            low = target.lower()
            if low in ("", "self", "app", "familiar"):
                self._result = self._grab_self()
            elif low in ("desktop", "all", "monitors"):
                self._result = self._grab_all_screens()
            elif low in ("screen", "primary") or low.startswith("screen:"):
                self._result = self._grab_screen(low)
            elif low.startswith("window:"):
                self._result = self._grab_window(target.split(":", 1)[1].strip())
            else:
                # Bare text → treat as a window title to find.
                self._result = self._grab_window(target)
        except Exception:
            self._result = None
        self._event.set()

    @staticmethod
    def _grab_self():
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            for w in app.topLevelWidgets():
                if w.__class__.__name__ == "MainWindow" and w.isVisible():
                    return w.grab()
        return None

    @staticmethod
    def _grab_screen(low: str):
        from PyQt6.QtGui import QGuiApplication
        screens = QGuiApplication.screens()
        if low.startswith("screen:"):
            try:
                idx = int(low.split(":", 1)[1])
            except ValueError:
                idx = 0
            scr = screens[idx] if 0 <= idx < len(screens) else QGuiApplication.primaryScreen()
        else:
            scr = QGuiApplication.primaryScreen()
        return scr.grabWindow(0) if scr else None      # WId 0 = the whole screen

    @staticmethod
    def _grab_all_screens():
        """Whole virtual desktop — stitch every monitor into one image."""
        from PyQt6.QtGui import QGuiApplication, QPixmap, QPainter
        from PyQt6.QtCore import QRect
        screens = QGuiApplication.screens()
        if not screens:
            return None
        if len(screens) == 1:
            return screens[0].grabWindow(0)
        virt = QRect()
        for s in screens:
            virt = virt.united(s.geometry())
        canvas = QPixmap(virt.width(), virt.height())
        canvas.fill()
        painter = QPainter(canvas)
        for s in screens:
            shot = s.grabWindow(0)
            g = s.geometry()
            painter.drawPixmap(g.x() - virt.x(), g.y() - virt.y(), shot)
        painter.end()
        return canvas

    @staticmethod
    def _grab_window(title: str):
        """Grab an external top-level window whose title contains `title`
        (case-insensitive). Windows-only (HWND); returns None elsewhere."""
        if not title:
            return None
        from PyQt6.QtGui import QGuiApplication
        if sys.platform != "win32":
            return None
        import ctypes
        user32 = ctypes.windll.user32
        found = []

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if title.lower() in buf.value.lower():
                        found.append(hwnd)
            return True

        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(WNDENUMPROC(_cb), 0)
        if not found:
            return None
        scr = QGuiApplication.primaryScreen()
        return scr.grabWindow(int(found[0])) if scr else None

    def _do_flash(self):
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            for w in app.topLevelWidgets():
                if w.__class__.__name__ == "MainWindow" and w.isVisible():
                    if hasattr(w, "_do_camera_flash"):
                        w._do_camera_flash()
                    break

    def grab(self, target: str = "self"):
        self._target = target
        self._result = None
        self._event.clear()
        self._request.emit()
        self._event.wait(timeout=8)
        return self._result


_bridge = _GrabBridge()


def screenshot(prompt: str = "", analyze: bool = True, target: str = "self") -> str:
    """Capture a screenshot of `target` and surface it in chat.

    target: 'self' (Familiar window), 'desktop'/'all' (whole virtual desktop),
    'screen'/'screen:N' (a monitor), or 'window:<title>' (an external window)."""
    from PyQt6.QtCore import Qt, QBuffer, QIODevice

    pixmap = _bridge.grab(target)
    if pixmap is None or pixmap.isNull():
        return json.dumps({"error": f"Could not capture target '{target}'. "
                           "On a headless/Linux host, desktop capture needs a "
                           "display; external-window capture is Windows-only."})

    if pixmap.width() > MAX_WIDTH:
        pixmap = pixmap.scaledToWidth(MAX_WIDTH, Qt.TransformationMode.SmoothTransformation)

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buf, "JPEG", 80)
    jpeg_bytes = bytes(buf.data())
    buf.close()

    try:
        from core.sounds import play
        play("snapshot.mp3")
    except Exception:
        pass
    try:
        _bridge.flash_requested.emit()
    except Exception:
        pass

    # Same temp path the chat screenshot-card watcher reads.
    tmp_path = Path(tempfile.gettempdir()) / "agent_screenshot.jpg"
    tmp_path.write_bytes(jpeg_bytes)

    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
    result = {
        "captured": True,
        "target": target,
        "image_path": str(tmp_path),
        "size_kb": round(len(jpeg_bytes) / 1024, 1),
        "data_url": f"data:image/jpeg;base64,{b64}",
    }

    if analyze:
        try:
            from tools.vision import vision_analyze
            analysis_prompt = prompt or (
                "Describe what you see in this screenshot. Note anything "
                "notable about the layout, windows, or content."
            )
            analysis = json.loads(vision_analyze(result["data_url"], analysis_prompt))
            result["analysis"] = analysis.get("analysis", "")
            if "error" in analysis:
                result["analysis_error"] = analysis["error"]
        except Exception as e:
            result["analysis_error"] = str(e)

    result.pop("data_url", None)   # don't bloat the model's context with base64
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="screenshot",
    description=(
        "Capture a screenshot and show it in chat. Targets: 'self' (Familiar's "
        "own window — UI checks), 'desktop' (the whole screen / all monitors), "
        "'screen:N' (a specific monitor), or 'window:<title>' (an external app "
        "window by title, e.g. 'window:Chrome'). Optional vision analysis. When "
        "this conversation is being mirrored over Familiar-Net, the image is "
        "shared with the remote viewer too — so a remote operator can see this "
        "machine's screen. (Desktop capture needs a display; external-window "
        "capture is Windows-only.)"
    ),
    parameters={
        "type": "object",
        "properties": {
            "target": {
                "type": "string",
                "description": "What to capture: 'self', 'desktop', 'screen' / "
                               "'screen:1', or 'window:<title>'. Default 'self'.",
            },
            "prompt": {"type": "string", "description": "What to look for (vision analysis)."},
            "analyze": {"type": "boolean", "description": "Run vision analysis (default true)."},
        },
    },
    execute=screenshot,
)
