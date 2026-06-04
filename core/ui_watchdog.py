"""
Main-thread stall detection: a fast QTimer heartbeat measures gaps between
ticks; if the GUI thread is blocked longer than ``stall_threshold_s``, it logs
a single line to stderr (silent — no alert sound).

Interval 200ms, 1s stall threshold, 60s startup warmup. Detection is purely
diagnostic: set ``FAMILIAR_UI_STALL_TRACE=1`` to also dump a full traceback of
all threads when a stall is caught.
"""

from __future__ import annotations

import os
import sys
import time

from PyQt6.QtCore import QObject, QTimer

DEFAULT_INTERVAL_MS = 200
DEFAULT_STALL_THRESHOLD_S = 1.0
DEFAULT_WARMUP_S = 60.0


class UiPerformanceWatchdog(QObject):
    """Detects delayed timer callbacks (main thread not pumping the event loop)."""

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        interval_ms: int = DEFAULT_INTERVAL_MS,
        stall_threshold_s: float = DEFAULT_STALL_THRESHOLD_S,
        warmup_s: float = DEFAULT_WARMUP_S,
    ) -> None:
        super().__init__(parent)
        self._interval_ms = interval_ms
        self._stall_threshold_s = stall_threshold_s
        self._warmup_s = warmup_s
        self._started_at = time.monotonic()
        self._last_tick = self._started_at
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
