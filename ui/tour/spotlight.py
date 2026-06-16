"""SpotlightOverlay — makes the target element itself appear to glow.

A single full-window overlay widget, transparent to the mouse, that paints:
  * a subtle dim veil over everything EXCEPT a cut-out hugging the target(s)
  * a neon edge-glow ON each target's own border: a crisp 1px line plus
    layered strokes fading outward, pulsing in brightness only — the
    element looks lit from within rather than lassoed by a floating ring.

Two modes:
  * focus(widget)        — glow one widget, tracked by reference.
  * focus_items(provider)— glow MANY rects at once, each in its own color,
    with a staggered shimmer so the colours cascade rather than blink in
    unison.

Targets are re-evaluated every pulse frame, so they stay glued through
window resizes, splitter animations, and list scrolling.
"""
from __future__ import annotations

from PyQt6.QtCore import QEasingCurve, QRectF, Qt, QVariantAnimation
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

from .colors import T

# Edge-glow profile: (outward offset px, stroke width px, alpha at full pulse).
# Tight falloff — the light belongs to the widget's edge, not the air around it.
_GLOW_LAYERS = [
    (0.0, 1.2, 255),
    (1.0, 2.0, 110),
    (2.5, 3.0, 55),
    (4.5, 4.0, 26),
    (7.0, 5.0, 12),
]


class SpotlightOverlay(QWidget):
    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._win = window
        self._target: QWidget | None = None
        # Multi-rect mode: a callable returning [(QRectF in window coords,
        # QColor), …], re-invoked every frame so rects track scroll/resize.
        self._rects_provider = None
        self._pad = 1               # hug the element — no loose border
        self._dim = True
        self._phase = 0.0           # 0..1 looping pulse phase
        self._veil_alpha = 0.0      # animated 0 → max on focus, → 0 on clear
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.hide()

        self._pulse = QVariantAnimation(self)
        self._pulse.setStartValue(0.0)
        self._pulse.setEndValue(1.0)
        self._pulse.setDuration(1600)
        self._pulse.setLoopCount(-1)
        self._pulse.valueChanged.connect(self._on_pulse)

        self._veil = QVariantAnimation(self)
        self._veil.setDuration(320)
        self._veil.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._veil.valueChanged.connect(self._on_veil)
        self._veil.finished.connect(self._on_veil_done)

    # ── public API ────────────────────────────────────────────────────
    def focus(self, target: QWidget | None, pad: int = 1,
              dim: bool = True) -> None:
        """Spotlight a single ``target`` widget; None clears."""
        if target is None or not target.isVisible():
            self.clear()
            return
        self._rects_provider = None
        self._target = target
        self._pad = pad
        self._dim = dim
        self._begin()

    def focus_items(self, provider, pad: int = 1, dim: bool = True) -> None:
        """Glow many rects at once, each in its own colour.

        ``provider()`` returns a list of (QRectF in window coordinates,
        QColor) and is called every frame, so the glow follows the items as
        the list scrolls or the window resizes."""
        self._target = None
        self._rects_provider = provider
        self._pad = pad
        self._dim = dim
        self._begin()

    def _begin(self) -> None:
        self.setGeometry(self._win.rect())
        self.show()
        self.raise_()
        if not self._pulse.state() == QVariantAnimation.State.Running:
            self._pulse.start()
        self._veil.stop()
        self._veil.setStartValue(self._veil_alpha)
        self._veil.setEndValue(1.0)
        self._veil.start()

    def clear(self) -> None:
        """Fade the veil out, then hide."""
        if not self.isVisible():
            return
        self._veil.stop()
        self._veil.setStartValue(self._veil_alpha)
        self._veil.setEndValue(0.0)
        self._veil.start()

    def shutdown(self) -> None:
        self._pulse.stop()
        self._veil.stop()
        self._target = None
        self._rects_provider = None
        self.hide()

    # ── targets ───────────────────────────────────────────────────────
    def _current_rects(self) -> list:
        """[(QRectF, QColor), …] for whichever mode is active."""
        if self._rects_provider is not None:
            try:
                rects = self._rects_provider() or []
            except RuntimeError:
                return []       # a tracked widget was deleted
            pad = self._pad
            return [(r.adjusted(-pad, -pad, pad, pad), c) for r, c in rects]
        r = self._target_rect()
        if r is None:
            return []
        return [(r, QColor(T["brand"]))]

    # ── animation plumbing ────────────────────────────────────────────
    def _glow_region(self) -> QRectF | None:
        """The glow rings' neighborhood — the only area the pulse dirties."""
        rects = self._current_rects()
        if not rects:
            return None
        m = _GLOW_LAYERS[-1][0] + _GLOW_LAYERS[-1][1] + 2
        union = None
        for r, _c in rects:
            grown = r.adjusted(-m, -m, m, m)
            union = grown if union is None else union.united(grown)
        return union

    def _on_pulse(self, v) -> None:
        self._phase = float(v)
        if not self.isVisible():
            return
        if self.geometry() != self._win.rect():
            self.setGeometry(self._win.rect())
        # Repaint ONLY around the glow. A translucent overlay update forces
        # Qt to re-composite every widget underneath it — full-window updates
        # at pulse rate hammer GL/webengine children (and on software GL each
        # composite spews "QPainter not active" warnings).
        region = self._glow_region()
        if region is not None:
            rect = region.toAlignedRect()
            # Union with the previous frame's spot — anchors move while
            # splitter/window animations are in flight; without this the old
            # position would keep a ghost ring.
            last = getattr(self, "_last_glow_rect", None)
            self.update(rect if last is None else rect.united(last))
            self._last_glow_rect = rect

    def _on_veil(self, v) -> None:
        # Veil alpha changes the whole overlay — but only during the short
        # focus/clear transitions, never while pulsing.
        self._veil_alpha = float(v)
        self.update()

    def _on_veil_done(self) -> None:
        if self._veil_alpha <= 0.001:
            self._target = None
            self._pulse.stop()
            self.hide()

    def _target_rect(self) -> QRectF | None:
        t = self._target
        if t is None:
            return None
        try:
            if not t.isVisible():
                return None
            top_left = t.mapTo(self._win, t.rect().topLeft())
        except RuntimeError:
            return None     # target deleted
        r = QRectF(float(top_left.x()), float(top_left.y()),
                   float(t.width()), float(t.height()))
        return r.adjusted(-self._pad, -self._pad, self._pad, self._pad)

    # ── painting ──────────────────────────────────────────────────────
    def paintEvent(self, _e) -> None:
        import math
        rects = self._current_rects()
        p = QPainter(self)
        if not p.isActive():
            return      # paint engine unavailable — never spam warnings
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        if not rects:
            p.end()
            return

        radius = 3.0
        if self._dim and self._veil_alpha > 0:
            veil = QPainterPath()
            veil.addRect(QRectF(self.rect()))
            holes = QPainterPath()
            for r, _c in rects:
                holes.addRoundedRect(r, radius, radius)
            p.fillPath(veil.subtracted(holes),
                       QColor(0, 0, 0, int(95 * self._veil_alpha)))

        # Each rect glows in its OWN colour. With many rects the pulse phase
        # is staggered per index, so the colours cascade as a travelling
        # shimmer instead of blinking in unison. Brightness-only: geometry
        # never moves.
        p.setBrush(Qt.BrushStyle.NoBrush)
        n = len(rects)
        for i, (rect, color) in enumerate(rects):
            phase = self._phase + (i / n * 0.7 if n > 1 else 0.0)
            glow = 0.45 + 0.55 * (math.sin(phase * 2 * math.pi) + 1) / 2
            strength = glow * max(self._veil_alpha, 0.001)
            for offset, width, alpha in _GLOW_LAYERS:
                c = QColor(color)
                c.setAlpha(max(0, min(255, int(alpha * strength))))
                p.setPen(QPen(c, width))
                p.drawRoundedRect(
                    rect.adjusted(-offset, -offset, offset, offset),
                    radius + offset, radius + offset)
        p.end()
