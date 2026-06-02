"""
Startup splash — a non-blocking, animated "cyber-fairy" plaque shown while the
app does its (sometimes slow) startup work: importing the tool stack and
hydrating the last conversation from SQLite.

Theme: a cross of cyberpunk and fairy ("Familiar" — think Navi the fairy, but a
cyber agent). Wispy + whimsical over a CRT/monocolor base:
  * faint scanlines + corner brackets + vignette  → the cyber bones
  * drifting, twinkling fairy motes (Navi-style glowing dust)
  * a couple of soft wandering wisps with bright cores + halos and faint trails
  * a gentle breathing halo behind the wordmark

Self-contained: uses the app PALETTE, no sibling-app deps. Frameless Tool
window, sized to the last-known main-window geometry, faded out once the first
conversation has rendered.
"""
from __future__ import annotations

import math
import random

from PyQt6.QtCore import Qt, QPropertyAnimation, QEasingCurve, QTimer, QPointF
from PyQt6.QtGui import (
    QFont, QPainter, QColor, QBrush, QLinearGradient, QRadialGradient, QPen,
)
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QLabel, QApplication, QGraphicsDropShadowEffect,
)

from ui.theme import PALETTE


def _neon(hex_color: str) -> str:
    c = QColor(hex_color)
    h, s, v, a = c.getHsv()
    return QColor.fromHsv(h, max(s, 200), 255, a).name()


def _rgba(c: QColor, a: int) -> QColor:
    return QColor(c.red(), c.green(), c.blue(), max(0, min(255, a)))


class _Mote:
    """A single drifting fairy mote (glowing dust particle)."""
    __slots__ = ("x", "y", "r", "drift", "rise", "phase", "twinkle", "hue_shift")

    def __init__(self, rng: random.Random, w: int, h: int):
        self.x = rng.uniform(0, w)
        self.y = rng.uniform(0, h)
        self.r = rng.uniform(0.8, 2.6)
        self.drift = rng.uniform(-0.20, 0.20)      # gentle horizontal sway speed
        self.rise = rng.uniform(0.12, 0.5)         # upward drift speed (px/tick)
        self.phase = rng.uniform(0, math.tau)      # twinkle phase
        self.twinkle = rng.uniform(0.6, 1.8)       # twinkle rate
        self.hue_shift = rng.uniform(-18, 18)      # slight per-mote hue variation


class _Wisp:
    """A soft wandering wisp: bright core + halo, lazy Lissajous path, faint trail."""
    __slots__ = ("ax", "ay", "fx", "fy", "px", "py", "speed", "core_r", "halo_r",
                 "trail", "bright")

    def __init__(self, rng: random.Random):
        self.ax = rng.uniform(0.30, 0.46)   # path amplitude (fraction of w/h)
        self.ay = rng.uniform(0.24, 0.40)
        # Higher frequencies so the wisp actually flits around within the few
        # seconds the splash is up (a fairy darting, not a slow orbit). The two
        # axes use incommensurate rates → an ever-changing, non-repeating path.
        self.fx = rng.uniform(0.45, 0.75)
        self.fy = rng.uniform(0.55, 0.95)
        self.px = rng.uniform(0, math.tau)  # phase offsets
        self.py = rng.uniform(0, math.tau)
        self.speed = rng.uniform(0.9, 1.3)
        self.core_r = rng.uniform(2.5, 4.5)
        self.halo_r = rng.uniform(16, 30)
        self.trail: list[tuple[float, float]] = []
        self.bright = rng.uniform(0.8, 1.0)


class SplashScreen(QWidget):
    """Animated cyber-fairy startup splash."""

    _W = 460
    _H = 240
    _TICK_MS = 33  # ~30fps

    def __init__(self) -> None:
        super().__init__(None)
        self.setObjectName("SplashScreen")
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self._fade = None
        self._t = 0.0  # animation time (seconds-ish)
        self._rng = random.Random(1337)  # fixed seed → consistent look per launch
        self._motes: list[_Mote] = []
        self._wisps: list[_Wisp] = []
        self._build_ui()
        self._match_window_geometry()
        self._seed_particles()

        self._anim = QTimer(self)
        self._anim.timeout.connect(self._tick)
        self._anim.start(self._TICK_MS)

    # ── layout ────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        p = PALETTE
        accent = p.get("accent", "#00fff7")
        bright = _neon(accent)

        # No QSS background — paintEvent owns the whole surface so the animation
        # reads as one continuous scene. Just a thin accent frame.
        self.setStyleSheet(
            f"QWidget#SplashScreen {{ border: 1px solid {accent}; }}")

        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.setSpacing(10)
        lay.setContentsMargins(40, 36, 40, 36)

        self._title = QLabel("Familiar")
        tf = QFont("Gabriola", 44)
        tf.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        tf.setFamilies(["Gabriola", "Segoe Script", "Palatino Linotype"])
        self._title.setFont(tf)
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setStyleSheet(
            f"color: {bright}; background: transparent; border: none;")
        glow = QGraphicsDropShadowEffect()
        glow.setColor(QColor(bright))
        glow.setBlurRadius(46)
        glow.setOffset(0, 0)
        self._title.setGraphicsEffect(glow)
        lay.addWidget(self._title)

        self._status = QLabel("Starting up…")
        self._status.setFont(QFont("Consolas", 10))
        self._status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._status.setStyleSheet(
            f"color: {p.get('accent_bright', accent)}; background: transparent;"
            f" border: none;")
        lay.addWidget(self._status)

    # ── geometry ──────────────────────────────────────────────────────
    def _match_window_geometry(self) -> None:
        """Size + position the splash to match the last-known main-window
        geometry (data/window_state.json). Falls back to a small centered
        plaque if state is missing, off-screen, or unreadable."""
        from pathlib import Path
        import json
        from PyQt6.QtCore import QRect
        try:
            state_path = Path(__file__).resolve().parent.parent / "data" / "window_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("maximized"):
                scr = QApplication.primaryScreen()
                if scr is not None:
                    self.setGeometry(scr.availableGeometry())
                    return
            rect = QRect(
                int(state.get("x", 100)), int(state.get("y", 100)),
                int(state.get("w", self._W)), int(state.get("h", self._H)),
            )
            if rect.width() >= 200 and rect.height() >= 150 and self._on_any_screen(rect):
                self.setGeometry(rect)
                return
        except Exception:
            pass
        self.resize(self._W, self._H)
        self._center_on_screen()

    def _on_any_screen(self, rect) -> bool:
        try:
            for scr in QApplication.screens():
                if scr.availableGeometry().intersects(rect):
                    return True
        except Exception:
            return True
        return False

    def _center_on_screen(self) -> None:
        try:
            scr = QApplication.primaryScreen()
            if scr is not None:
                geo = scr.availableGeometry()
                self.move(geo.center().x() - self._W // 2,
                          geo.center().y() - self._H // 2)
        except Exception:
            pass

    # ── particles ─────────────────────────────────────────────────────
    def _seed_particles(self) -> None:
        w = max(self.width(), self._W)
        h = max(self.height(), self._H)
        # Scale mote count to area so big windows aren't sparse, small ones dense.
        n = max(28, min(90, int(w * h / 9000)))
        self._motes = [_Mote(self._rng, w, h) for _ in range(n)]
        self._wisps = [_Wisp(self._rng) for _ in range(3)]

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Reseed so motes fill the new area (geometry may be set after __init__).
        self._seed_particles()

    def _tick(self) -> None:
        self._t += self._TICK_MS / 1000.0
        w, h = self.width(), self.height()
        for m in self._motes:
            m.y -= m.rise
            m.x += m.drift + math.sin(self._t * 0.6 + m.phase) * 0.15
            if m.y < -4:  # respawn at the bottom when it floats off the top
                m.y = h + 4
                m.x = self._rng.uniform(0, w)
            if m.x < -4:
                m.x = w + 4
            elif m.x > w + 4:
                m.x = -4
        self.update()

    # ── painting ──────────────────────────────────────────────────────
    def paintEvent(self, event) -> None:
        p = PALETTE
        accent = QColor(p.get("accent", "#00fff7"))
        bright = QColor(p.get("glow_hot", p.get("accent_bright", "#aeffff")))
        bg = QColor(p.get("background", "#0b0d0d"))
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h * 0.42  # visual center (slightly above middle)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 1) Background gradient — deep, with a faint accent breath at the center.
        grad = QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0.0, QColor(max(0, bg.red() - 4), max(0, bg.green() - 4), max(0, bg.blue() - 4)))
        grad.setColorAt(1.0, QColor(max(0, bg.red() - 12), max(0, bg.green() - 12), max(0, bg.blue() - 12)))
        painter.fillRect(0, 0, w, h, QBrush(grad))

        # 2) Breathing central aura behind the wordmark.
        breath = 0.5 + 0.5 * math.sin(self._t * 1.1)
        aura_r = min(w, h) * (0.34 + 0.04 * breath)
        aura = QRadialGradient(QPointF(cx, cy), aura_r)
        aura.setColorAt(0.0, _rgba(accent, int(34 + 26 * breath)))
        aura.setColorAt(0.5, _rgba(accent, int(12 + 10 * breath)))
        aura.setColorAt(1.0, _rgba(accent, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(aura)
        painter.drawEllipse(QPointF(cx, cy), aura_r, aura_r)

        # 3) Scanlines (cyber bones) — very faint.
        painter.setBrush(QColor(0, 0, 0, 22))
        y = 0
        while y < h:
            painter.drawRect(0, y, w, 1)
            y += 3

        # 4) Wisps: trails, halos, bright cores.
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        for wi in self._wisps:
            wx = cx + math.sin(self._t * wi.fx * wi.speed + wi.px) * (w * wi.ax)
            wy = cy + math.sin(self._t * wi.fy * wi.speed + wi.py) * (h * wi.ay)
            wi.trail.append((wx, wy))
            if len(wi.trail) > 26:  # longer comet-tail now that wisps flit faster
                wi.trail.pop(0)
            # trail
            for i, (tx, ty) in enumerate(wi.trail):
                frac = i / max(1, len(wi.trail) - 1)
                tr = wi.core_r * (0.4 + 0.6 * frac)
                painter.setBrush(_rgba(accent, int(20 * frac * wi.bright)))
                painter.drawEllipse(QPointF(tx, ty), tr, tr)
            # halo
            halo = QRadialGradient(QPointF(wx, wy), wi.halo_r)
            halo.setColorAt(0.0, _rgba(accent, int(70 * wi.bright)))
            halo.setColorAt(1.0, _rgba(accent, 0))
            painter.setBrush(halo)
            painter.drawEllipse(QPointF(wx, wy), wi.halo_r, wi.halo_r)
            # bright core
            painter.setBrush(_rgba(bright, int(220 * wi.bright)))
            painter.drawEllipse(QPointF(wx, wy), wi.core_r, wi.core_r)

        # 5) Fairy motes: twinkling glowing dust.
        for m in self._motes:
            tw = 0.5 + 0.5 * math.sin(self._t * m.twinkle + m.phase)
            a = int(40 + 150 * tw)
            mc = QColor(accent)
            # tiny per-mote hue variation for a more organic shimmer
            hh, ss, vv, aa = mc.getHsv()
            mc = QColor.fromHsv(int((hh + m.hue_shift) % 360), ss, vv, aa)
            # soft glow
            painter.setBrush(_rgba(mc, int(a * 0.35)))
            painter.drawEllipse(QPointF(m.x, m.y), m.r * 3.0, m.r * 3.0)
            # bright dot
            painter.setBrush(_rgba(QColor(bright), a))
            painter.drawEllipse(QPointF(m.x, m.y), m.r, m.r)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        # 6) Vignette to focus the center.
        vig = QRadialGradient(QPointF(cx, h / 2), max(w, h) * 0.75)
        vig.setColorAt(0.0, QColor(0, 0, 0, 0))
        vig.setColorAt(0.72, QColor(0, 0, 0, 0))
        vig.setColorAt(1.0, QColor(0, 0, 0, 120))
        painter.setBrush(vig)
        painter.drawRect(0, 0, w, h)

        # 7) Corner brackets (cyber frame).
        pen = QPen(accent)
        pen.setWidth(2)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        s, mrg = 24, 8
        painter.drawLine(mrg, mrg, mrg + s, mrg)
        painter.drawLine(mrg, mrg, mrg, mrg + s)
        painter.drawLine(w - mrg, mrg, w - mrg - s, mrg)
        painter.drawLine(w - mrg, mrg, w - mrg, mrg + s)
        painter.drawLine(mrg, h - mrg, mrg + s, h - mrg)
        painter.drawLine(mrg, h - mrg, mrg, h - mrg - s)
        painter.drawLine(w - mrg, h - mrg, w - mrg - s, h - mrg)
        painter.drawLine(w - mrg, h - mrg, w - mrg, h - mrg - s)

        painter.end()

    # ── API ───────────────────────────────────────────────────────────
    def set_status(self, text: str) -> None:
        try:
            self._status.setText(text)
            self._status.repaint()
        except Exception:
            pass

    def show(self) -> None:  # noqa: A003
        super().show()
        self.raise_()

    def fade_out(self, duration: int = 420, callback=None) -> None:
        try:
            if self._fade is not None:
                self._fade.stop()
            self._fade = QPropertyAnimation(self, b"windowOpacity")
            self._fade.setDuration(duration)
            self._fade.setStartValue(1.0)
            self._fade.setEndValue(0.0)
            self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

            def _done():
                try:
                    self._anim.stop()
                except Exception:
                    pass
                self.hide()
                self.deleteLater()
                if callable(callback):
                    callback()

            self._fade.finished.connect(_done)
            self._fade.start()
        except Exception:
            try:
                self._anim.stop()
            except Exception:
                pass
            self.hide()
            self.deleteLater()
            if callable(callback):
                callback()
