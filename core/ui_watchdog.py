"""
Main-thread stall detection: a fast QTimer heartbeat measures gaps between
ticks; if the GUI thread is blocked longer than ``stall_threshold_s``,
logs to stderr and plays ``error.mp3`` (with cooldown).

Same pattern as Vispy_dashboard: interval 200ms, 1s stall threshold, 60s
startup warmup, 60s between alert sounds. Uses ``play()`` so alerts still
fire when ``ui_sounds`` is disabled and to avoid extra config reads during
recovery from a stall.
"""

from __future__ import annotations

import os
import sys
import time

from PyQt6.QtCore import QObject, QTimer

from core.sounds import play

DEFAULT_INTERVAL_MS = 200
DEFAULT_STALL_THRESHOLD_S = 1.0
DEFAULT_WARMUP_S = 60.0
DEFAULT_SOUND_COOLDOWN_S = 60.0


class UiPerformanceWatchdog(QObject):
    """Detects delayed timer callbacks (main thread not pumping the event loop)."""

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        stall_threshold_s: float = DEFAULT_STALL_THRESHOLD_S,
        warmup_s: float = DEFAULT_WARMUP_S,
        sound_cooldown_s: float = DEFAULT_SOUND_COOLDOWN_S,
        sound_name: str = "error.mp3",
    ) -> None:
        super().__init__(parent)
        self._interval_ms = interval_ms
        self._stall_threshold_s = stall_threshold_s
        self._warmup_s = warmup_s
        self._sound_cooldown_s = sound_cooldown_s
        self._sound_name = sound_name
        self._started_at = time.monotonic()
        self._last_tick = self._started_at
        self._last_sound_at = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._started_at = time.monotonic()
        self._last_tick = self._started_at
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        now = time.monotonic()
        elapsed_since_start = now - self._started_at
        gap = now - self._last_tick
        self._last_tick = now

        if elapsed_since_start < self._warmup_s:
            return
        if gap <= self._stall_threshold_s:
            return

        print(
            f"[ui-watchdog] UI stall detected: gap={gap:.3f}s "
            f"(threshold={self._stall_threshold_s:.3f}s, "
            f"uptime={elapsed_since_start:.1f}s)",
            flush=True,
        )
        if os.environ.get("FAMILIAR_UI_STALL_TRACE", "").strip() in ("1", "true", "yes"):
            try:
                import faulthandler
                faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            except Exception:
                pass

        if now - self._last_sound_at >= self._sound_cooldown_s:
            self._last_sound_at = now
            try:
                play(self._sound_name)
            except Exception:
                pass
