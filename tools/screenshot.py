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
import io
import json
import sys
import tempfile
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal
from tools.registry import registry

MAX_WIDTH = 1600   # roomier than the old 900 — desktop shots need the detail


def _capture_diagnostics(data: bytes | None) -> dict:
    """Describe a capture and identify the all-black/uniform failure mode."""
    if not data:
        return {"valid": False, "black_frame": False, "reason": "empty capture"}
    try:
        from PIL import Image, ImageStat
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            gray = image.convert("L")
            gray.thumbnail((256, 256))
            stat = ImageStat.Stat(gray)
            mean = float(stat.mean[0])
            deviation = float(stat.stddev[0])
            lo, hi = gray.getextrema()
            black = hi <= 4 or (mean <= 3.0 and deviation <= 1.0)
            return {
                "valid": image.width >= 16 and image.height >= 16 and not black,
                "black_frame": black,
                "width": image.width, "height": image.height,
                "luma_mean": round(mean, 2),
                "luma_stddev": round(deviation, 2),
                "luma_range": [int(lo), int(hi)],
            }
    except Exception as exc:
        return {"valid": False, "black_frame": False,
                "reason": f"decode failed: {type(exc).__name__}: {exc}"}


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
        self._error = ""
        self._metadata = {}
        self._request.connect(self._handle)
        self.flash_requested.connect(self._do_flash)

    # ── main-thread grab dispatch ──
    def _handle(self):
        target = (self._target or "self").strip()
        self._result = None
        self._error = ""
        self._metadata = {}
        try:
            low = target.lower()
            if sys.platform == "win32" and low not in ("", "self", "app", "familiar"):
                native, native_error = self._safe_grab_pillow(target)
                if native_error:
                    self._error = native_error
                native_diag = _capture_diagnostics(native)
                if native_diag.get("valid"):
                    self._result = native
                    self._metadata = {"backend": "pillow_imagegrab",
                                      **native_diag}
                    self._event.set()
                    return
                self._error = f"native capture invalid: {native_diag}"
            if low in ("", "self", "app", "familiar"):
                pixmap = self._grab_self()
            elif low in ("desktop", "all", "monitors"):
                pixmap = self._grab_all_screens()
            elif low in ("screen", "primary") or low.startswith("screen:"):
                pixmap = self._grab_screen(low)
            elif low.startswith("window:"):
                pixmap = self._grab_window(target.split(":", 1)[1].strip())
            else:
                # Bare text → treat as a window title to find.
                pixmap = self._grab_window(target)
            # Scale + encode HERE, still on the GUI thread. QPixmap is a
            # QPaintDevice — touching it from the worker thread (the old
            # behavior) corrupts the native heap. Only plain bytes cross the
            # thread boundary.
            encoded = self._encode_jpeg(pixmap)
            diagnostics = _capture_diagnostics(encoded)
            if sys.platform == "win32" and not diagnostics.get("valid"):
                native, native_error = self._safe_grab_pillow(target)
                if native_error:
                    self._error = native_error
                native_diag = _capture_diagnostics(native)
                if native_diag.get("valid"):
                    encoded, diagnostics = native, native_diag
                    self._metadata = {"backend": "pillow_imagegrab", **diagnostics}
                else:
                    self._error = ("capture backend returned a black or invalid frame; "
                                   f"qt={diagnostics}, native={native_diag}")
            if not self._metadata:
                self._metadata = {"backend": "qt", **diagnostics}
            self._result = encoded if diagnostics.get("valid") else None
        except Exception as exc:
            self._error = f"{type(exc).__name__}: {exc}"
            self._result = None
        self._event.set()

    @staticmethod
    def _encode_jpeg(pixmap) -> bytes | None:
        if pixmap is None or pixmap.isNull():
            return None
        from PyQt6.QtCore import Qt, QBuffer, QIODevice
        if pixmap.width() > MAX_WIDTH:
            pixmap = pixmap.scaledToWidth(
                MAX_WIDTH, Qt.TransformationMode.SmoothTransformation)
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buf, "JPEG", 80)
        data = bytes(buf.data())
        buf.close()
        return data or None

    @staticmethod
    def _pil_jpeg(image) -> bytes | None:
        if image is None:
            return None
        image = image.convert("RGB")
        if image.width > MAX_WIDTH:
            from PIL import Image
            height = max(1, round(image.height * MAX_WIDTH / image.width))
            image = image.resize((MAX_WIDTH, height), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        image.save(buf, "JPEG", quality=88, optimize=True)
        return buf.getvalue() or None

    @staticmethod
    def _find_window(title: str) -> int | None:
        if not title or sys.platform != "win32":
            return None
        import ctypes
        user32 = ctypes.windll.user32
        found: list[int] = []

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    if title.lower() in buf.value.lower():
                        found.append(int(hwnd))
            return not bool(found)

        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        user32.EnumWindows(proc(_cb), 0)
        return found[0] if found else None

    @classmethod
    def _grab_pillow(cls, target: str) -> bytes | None:
        """Windows-native fallback that handles layered/accelerated surfaces."""
        if sys.platform != "win32":
            return None
        from PIL import ImageGrab
        from PyQt6.QtGui import QGuiApplication
        low = (target or "self").strip().lower()
        kwargs = {"include_layered_windows": True}
        if low in ("desktop", "all", "monitors"):
            image = ImageGrab.grab(all_screens=True, **kwargs)
        elif low in ("screen", "primary") or low.startswith("screen:"):
            screens = QGuiApplication.screens()
            try:
                idx = int(low.split(":", 1)[1]) if ":" in low else 0
            except ValueError:
                idx = 0
            screen = screens[idx] if 0 <= idx < len(screens) else QGuiApplication.primaryScreen()
            if screen is None:
                return None
            g = screen.geometry()
            image = ImageGrab.grab(bbox=(g.x(), g.y(), g.x() + g.width(),
                                         g.y() + g.height()), all_screens=True, **kwargs)
        else:
            if low in ("", "self", "app", "familiar"):
                from PyQt6.QtWidgets import QApplication
                app = QApplication.instance()
                window = next((w for w in app.topLevelWidgets()
                               if w.__class__.__name__ == "MainWindow" and w.isVisible()), None) if app else None
                hwnd = int(window.winId()) if window else None
            else:
                title = target.split(":", 1)[1].strip() if low.startswith("window:") else target
                hwnd = cls._find_window(title)
            if not hwnd:
                return None
            image = ImageGrab.grab(window=hwnd, **kwargs)
        return cls._pil_jpeg(image)
    @classmethod
    def _safe_grab_pillow(cls, target: str) -> tuple[bytes | None, str]:
        try:
            return cls._grab_pillow(target), ""
        except Exception as exc:
            return None, ("Pillow ImageGrab failed: "
                          f"{type(exc).__name__}: {exc}")



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
        hwnd = _GrabBridge._find_window(title)
        if not hwnd:
            return None
        self._error = ""
        self._metadata = {}
        scr = QGuiApplication.primaryScreen()
        return scr.grabWindow(hwnd) if scr else None

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
    tmp_path = Path(tempfile.gettempdir()) / "agent_screenshot.jpg"
    try:
        tmp_path.unlink(missing_ok=True)
    except OSError:
        pass

    jpeg_bytes = _bridge.grab(target)  # plain bytes — encoded on the GUI thread
    if not jpeg_bytes:
        return json.dumps({"error": f"Could not capture target '{target}': "
                           f"{_bridge._error or 'no image data'}. "
                           "On a headless/Linux host, desktop capture needs a "
                           "display; external-window capture is Windows-only.",
                           "vision_attempted": False,
                           "rate_limited": False})

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
    tmp_path.write_bytes(jpeg_bytes)

    b64 = base64.b64encode(jpeg_bytes).decode("utf-8")
    result = {
        "captured": True,
        "target": target,
        "image_path": str(tmp_path),
        "size_kb": round(len(jpeg_bytes) / 1024, 1),
        "data_url": f"data:image/jpeg;base64,{b64}",
        "capture": dict(_bridge._metadata),
        "rate_limited": False,
        "vision_attempted": bool(analyze),
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
            result["vision_attempted"] = bool(analysis.get("vision_attempted"))
            result["vision_provider"] = analysis.get("provider")
            result["vision_model"] = analysis.get("model")
            result["rate_limited"] = bool(analysis.get("rate_limited", False))
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
