"""
CRT scanline overlay — a translucent child widget that paints rolling
scanlines + a border glow over the host window for a retro CRT look.

The scanline pattern is pre-rendered into a QPixmap and blitted each frame
(offset rolled) so per-tick cost is a single blit, not a per-line fill loop.

DESIGN NOTE: this is a CHILD widget of the host, raised above its siblings.
An earlier revision made it an OS-composited top-level ``Qt.Tool`` window to
avoid repainting widgets beneath it — but that separated window stopped staying
above the host reliably (the effect silently vanished everywhere). A child
widget with ``WA_TransparentForMouseEvents`` is what the sibling vispy_dashboard
uses and what actually works; with a 6-frame pre-rendered pattern and the
default ~600ms tick the repaint cost is negligible.

Mirrors vispy_dashboard/widgets/crt_overlay.py.

KNOWN LIMITATION: a QWebEngineView (the embedded browser) renders to its own
native/GPU surface that the OS composites ABOVE Qt's painting, so this overlay
cannot cover browser content. Tinting the browser is handled separately by the
CSS filter injected in ui/right_workspace.py.
"""
from __future__ import annotations
from PyQt6.QtCore import Qt, QTimer, QRectF
from PyQt6.QtGui import QColor, QPainter, QPixmap
from PyQt6.QtWidgets import QWidget


class CRTOverlay(QWidget):
    """Rolling CRT scanline + border glow, painted as a child of the host."""

    SCANLINE_HEIGHT = 2
    SCANLINE_ALPHA = 18
    PATTERN_ROWS = 6  # scanline offset wraps every 6 rows

    def __init__(self, host: QWidget, border_color: QColor, speed_ms: int = 600):
        super().__init__(host)
        self._host = host
        self._border_color = border_color
        self._offset = 0
        self._pattern: QPixmap | None = None
        self._last_size = (0, 0)

        # Pure cosmetic overlay: never steals mouse/focus, no opaque fill.
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(speed_ms)

    # ── Geometry ──────────────────────────────────────────────────────
    def sync_geometry(self):
        """Cover the host and sit above its siblings. Named for compatibility
        with the previous top-level revision; for a child widget it just
        matches the host's local rect and raises."""
        host = self._host
        if host is None:
            return
        try:
            self.setGeometry(host.rect())
            self.raise_()
        except RuntimeError:  # host being torn down
            return

    # ── Pattern ───────────────────────────────────────────────────────
    def _build_pattern(self):
        w, h = self.width(), self.height()
        if w < 2 or h < 2:
            return
        pat_h = h + self.PATTERN_ROWS * self.SCANLINE_HEIGHT * 2
        pm = QPixmap(w, pat_h)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        col = QColor(0, 0, 0, self.SCANLINE_ALPHA)
        for y in range(0, pat_h, self.SCANLINE_HEIGHT * 2):
            p.fillRect(0, y, w, self.SCANLINE_HEIGHT, col)
        p.end()
        self._pattern = pm
        self._last_size = (w, h)

    def _tick(self):
        self._offset = (self._offset + 1) % self.PATTERN_ROWS
        if self.isVisible() and self.width() >= 10:
            self.update()

    def paintEvent(self, event):
        w, h = self.width(), self.height()
        if w < 10 or h < 10:
            return
        if self._pattern is None or (w, h) != self._last_size:
            self._build_pattern()
        if self._pattern is None:
            return

        painter = QPainter(self)
        y_off = -(self._offset * self.SCANLINE_HEIGHT)
        painter.drawPixmap(0, y_off, self._pattern)

        # Border glow
        glow = QColor(self._border_color)
        glow.setAlpha(30)
        painter.setPen(glow)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(QRectF(1.5, 1.5, w - 3, h - 3))
        painter.end()

    def set_speed(self, speed_ms: int):
        self._timer.setInterval(speed_ms)

    def set_border_color(self, color: QColor):
        self._border_color = color
        if self.isVisible():
            self.update()

    def resizeEvent(self, event):
        self._pattern = None  # force rebuild on next paint
        super().resizeEvent(event)
