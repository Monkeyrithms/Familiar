"""
Workspace browser — read the current page in the right-panel embedded browser.

Uses a signal bridge (same pattern as screenshot.py) so the inference thread can
safely trigger view.grab() on the main thread and block until the result is ready.

Key difference from the `browser` tool:
- `browser` launches a NEW headless Playwright session (no logged-in cookies).
- `read_browser` captures the SAME browser the user is looking at, with their session.
  Use this whenever the user asks about something they already have open.
"""

import base64
import json
import os
import tempfile
import threading

from PyQt6.QtCore import QObject, pyqtSignal
from tools.registry import registry

MAX_WIDTH = 1200  # resize before vision to avoid token bloat


class _BrowserGrabBridge(QObject):
    """Lives on the main thread; grabs the active workspace-browser tab on request."""

    _request = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._result = None          # pixmap or None
        self._event = threading.Event()
        self._grab_fn = None         # set by ChatWindow → BrowserWorkspacePanel.grab_current_view
        self._request.connect(self._handle)

    def _handle(self):
        """Runs on the main thread."""
        self._result = None
        if self._grab_fn:
            try:
                self._result = self._grab_fn()
            except Exception:
                pass
        self._event.set()

    def grab(self):
        """Block the calling thread until the main thread delivers a pixmap (or None)."""
        self._result = None
        self._event.clear()
        self._request.emit()           # queued → delivered to main thread
        self._event.wait(timeout=5)
        return self._result


# Created at import time on the main thread (tools/__init__.py is imported from main.py)
_grab_bridge = _BrowserGrabBridge()

# Page-context accessor set by ChatWindow
_context_fn = None


def set_context_provider(fn):
    """Called by ChatWindow to wire up BrowserWorkspacePanel.get_current_page_context."""
    global _context_fn
    _context_fn = fn


def set_grab_handler(fn):
    """Called by ChatWindow to wire up BrowserWorkspacePanel.grab_current_view."""
    _grab_bridge._grab_fn = fn


# ─────────────────────────────────────────────────────────────────────────────


def _pixmap_to_jpeg_path(pixmap) -> str:
    """Save a QPixmap as a JPEG temp file and return the path."""
    from PyQt6.QtCore import Qt
    if pixmap.width() > MAX_WIDTH:
        pixmap = pixmap.scaledToWidth(MAX_WIDTH, Qt.TransformationMode.SmoothTransformation)
    path = os.path.join(tempfile.gettempdir(), "ws_browser_grab.jpg")
    pixmap.save(path, "JPEG", 85)
    return path


def _analyze_screenshot(path: str, prompt: str) -> str:
    """Run vision analysis on a local screenshot file."""
    from tools.vision import vision_analyze
    result = json.loads(vision_analyze(image_url=path, prompt=prompt))
    return result.get("analysis", result.get("error", "Vision analysis failed."))


def read_browser(prompt: str = "") -> str:
    """
    Read the current page in the workspace browser using the user's session.
    When page text is sparse (login walls, JS-rendered content), automatically
    grabs a live screenshot and runs vision analysis — no second tool call needed.
    """
    # ── Step 1: try cached text context ──────────────────────────────────────
    ctx: dict = {}
    if _context_fn:
        try:
            ctx = _context_fn() or {}
        except Exception:
            pass

    url = ctx.get("url", "")
    title = ctx.get("title", "")
    text = ctx.get("text", "")

    vision_prompt = prompt or (
        "Read and transcribe all visible text on this page. "
        "Include the full content of any posts, articles, tweets, or main content visible."
    )

    # ── Step 2: if text is good enough, return it directly ───────────────────
    if len(text.strip()) >= 200:
        return json.dumps(
            {"url": url, "title": title, "text": text},
            ensure_ascii=False,
        )

    # ── Step 3: text is sparse — grab a live screenshot from the main thread ─
    pixmap = _grab_bridge.grab()
    if pixmap is None or pixmap.isNull():
        # Fall back to the cached screenshot if the grab failed
        cached_path = ctx.get("screenshot_path", "")
        if cached_path and os.path.isfile(cached_path):
            analysis = _analyze_screenshot(cached_path, vision_prompt)
            return json.dumps(
                {"url": url, "title": title, "source": "cached_screenshot", "content": analysis},
                ensure_ascii=False,
            )
        return json.dumps(
            {"url": url, "title": title, "text": text or "(page appears empty)",
             "note": "Could not capture screenshot — workspace browser may not be visible."},
            ensure_ascii=False,
        )

    # ── Step 4: save pixmap and run vision ────────────────────────────────────
    try:
        path = _pixmap_to_jpeg_path(pixmap)
    except Exception as e:
        return json.dumps({"error": f"Screenshot save failed: {e}"})

    analysis = _analyze_screenshot(path, vision_prompt)
    return json.dumps(
        {"url": url, "title": title, "source": "live_screenshot", "content": analysis},
        ensure_ascii=False,
    )


registry.register(
    name="read_browser",
    description=(
        "Read user's logged-in browser page (right panel). "
        "✓ auth/paywall/JS content headless can't see. "
        "✓ when user says 'read this'|'what does this say'. Sparse text → auto vision."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "Opt: what to extract. Default: transcribe all visible text + main content."
                ),
            },
        },
        "required": [],
    },
    execute=read_browser,
)
