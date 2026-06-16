"""Animation primitives for the tour's "origami" reveals.

All helpers are fire-and-forget: they keep a module-level reference to every
running animation (Qt does not own QVariantAnimation/QPropertyAnimation
created without a parent — without a reference they are garbage-collected
mid-flight and the widget freezes half-revealed), and they always finish by
snapping the widget to its final, unconstrained state, so an interrupted or
failed animation can never leave the UI broken.
"""
from __future__ import annotations

from PyQt6.QtCore import (
    QEasingCurve, QRect, QTimer, QVariantAnimation,
)
from PyQt6.QtWidgets import QGraphicsOpacityEffect, QSplitter, QWidget

# Qt's "no maximum" sentinel (QWIDGETSIZE_MAX).
_SIZE_MAX = 16777215

# Live animations — see module docstring.
_running: set = set()


def _track(anim) -> None:
    _running.add(anim)
    anim.destroyed.connect(lambda *_: _running.discard(anim))
    anim.finished.connect(lambda: _running.discard(anim))


def _after(ms: int, fn, parent=None) -> None:
    """singleShot wrapper — 0ms still defers to the next event-loop tick."""
    QTimer.singleShot(max(0, int(ms)), fn)


def fade_in(w: QWidget, duration: int = 350, delay: int = 0,
            on_done=None) -> None:
    """Show ``w`` and fade its opacity 0 → 1, removing the effect at the end.

    The QGraphicsOpacityEffect is removed on finish — leaving one installed
    permanently breaks some custom paintEvents (proxy/webengine widgets).
    """
    def start():
        if w is None:
            return
        eff = QGraphicsOpacityEffect(w)
        eff.setOpacity(0.0)
        w.setGraphicsEffect(eff)
        w.show()
        anim = QVariantAnimation(w)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.valueChanged.connect(
            lambda v: eff.setOpacity(float(v)) if w.graphicsEffect() is eff else None)

        def finish():
            if w.graphicsEffect() is eff:
                w.setGraphicsEffect(None)
            if on_done:
                on_done()
        anim.finished.connect(finish)
        _track(anim)
        anim.start()
    _after(delay, start)


def grow_in(w: QWidget, axis: str = "h", duration: int = 420,
            delay: int = 0, on_done=None) -> None:
    """Show ``w`` by growing it from zero size along ``axis`` ('h'|'v'|'both').

    Works on widgets inside layouts by animating max-width/height, then
    restoring the widget's original constraints exactly (fixed-size buttons
    keep their fixed size; free widgets go back to unconstrained).
    """
    def start():
        if w is None:
            return
        orig_max_w, orig_max_h = w.maximumWidth(), w.maximumHeight()
        orig_min_w, orig_min_h = w.minimumWidth(), w.minimumHeight()
        target_w = orig_max_w if orig_max_w < _SIZE_MAX else max(
            w.sizeHint().width(), w.width(), 1)
        target_h = orig_max_h if orig_max_h < _SIZE_MAX else max(
            w.sizeHint().height(), w.height(), 1)
        do_w = axis in ("h", "both")
        do_h = axis in ("v", "both")
        if do_w:
            w.setMinimumWidth(0)
            w.setMaximumWidth(0)
        if do_h:
            w.setMinimumHeight(0)
            w.setMaximumHeight(0)
        w.show()

        anim = QVariantAnimation(w)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.OutBack)

        def tick(v):
            t = max(0.0, float(v))   # OutBack overshoots; clamp below zero only
            if do_w:
                w.setMaximumWidth(int(target_w * t))
            if do_h:
                w.setMaximumHeight(int(target_h * t))

        def finish():
            w.setMinimumWidth(orig_min_w)
            w.setMaximumWidth(orig_max_w)
            w.setMinimumHeight(orig_min_h)
            w.setMaximumHeight(orig_max_h)
            if on_done:
                on_done()

        anim.valueChanged.connect(tick)
        anim.finished.connect(finish)
        _track(anim)
        anim.start()
    _after(delay, start)


def stagger(widgets, fn=grow_in, gap_ms: int = 110, **kw) -> int:
    """Run ``fn`` over widgets with a cascading delay. Returns total ms."""
    delay = 0
    for w in widgets:
        if w is not None:
            fn(w, delay=delay, **kw)
        delay += gap_ms
    return delay + kw.get("duration", 420)


def animate_splitter(splitter: QSplitter, target_sizes: list,
                     duration: int = 520, delay: int = 0,
                     on_done=None) -> None:
    """Glide a splitter from its current sizes to ``target_sizes``."""
    def start():
        if splitter is None:
            return
        src = splitter.sizes()
        if len(src) != len(target_sizes):
            splitter.setSizes([int(s) for s in target_sizes])
            if on_done:
                on_done()
            return
        anim = QVariantAnimation(splitter)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def tick(v):
            t = float(v)
            splitter.setSizes([
                int(a + (b - a) * t) for a, b in zip(src, target_sizes)])

        def finish():
            splitter.setSizes([int(s) for s in target_sizes])
            if on_done:
                on_done()

        anim.valueChanged.connect(tick)
        anim.finished.connect(finish)
        _track(anim)
        anim.start()
    _after(delay, start)


def animate_geometry(w: QWidget, target: QRect, duration: int = 650,
                     delay: int = 0, on_done=None) -> None:
    """Glide a top-level window's geometry (pos + size) to ``target``."""
    def start():
        if w is None:
            return
        src = w.geometry()
        anim = QVariantAnimation(w)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

        def lerp(a, b, t):
            return int(a + (b - a) * t)

        def tick(v):
            t = float(v)
            w.setGeometry(QRect(
                lerp(src.x(), target.x(), t),
                lerp(src.y(), target.y(), t),
                lerp(src.width(), target.width(), t),
                lerp(src.height(), target.height(), t),
            ))

        def finish():
            w.setGeometry(target)
            if on_done:
                on_done()

        anim.valueChanged.connect(tick)
        anim.finished.connect(finish)
        _track(anim)
        anim.start()
    _after(delay, start)
