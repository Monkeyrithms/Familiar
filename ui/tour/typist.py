"""Typist — plays hardcoded narration through the chat as a live agent line.

The opening beats (while the app is still folded into a bare chat card) are
narrated by the agent *in the chat itself*, so it reads like Familiar is
talking. The text is known up front, so it is rendered to HTML once and
revealed progressively — formatting is visible from the first character,
never raw ``**markdown**`` syntax — then committed into a real, persistent
agent bubble exactly the way a finished inference would.

Drives a small set of tour-only hooks on ``ChatWindow`` (see chat_widget.py):
``tour_open_line`` / ``tour_set_line`` / ``tour_discard_line`` /
``tour_commit_line``. Kept entirely separate from the virtualized message
stream until commit, so it never races ``_recalc_and_sync``.
"""
from __future__ import annotations

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .markup import progressive_html, render_markdown, visible_length

_CARET = '<span style="opacity:0.55">▌</span>'


class Typist(QObject):
    finished = pyqtSignal()

    # ~110 chars/sec — quick enough to feel alive, slow enough to read along.
    TICK_MS = 28
    CHARS_PER_TICK = 3

    def __init__(self, chat_window) -> None:
        super().__init__(chat_window)
        self._cw = chat_window
        self._text = ""
        self._html = ""
        self._total = 0
        self._pos = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def say(self, text: str) -> None:
        """Start typing ``text``. Any in-flight line is finalized first."""
        self.finish_now(emit=False)
        self._text = text or ""
        if not self._text:
            self.finished.emit()
            return
        self._html = render_markdown(self._text)
        self._total = visible_length(self._html)
        self._pos = 0
        if self._cw is None:
            self.finished.emit()
            return
        try:
            self._cw.tour_open_line()
        except Exception:
            pass
        self._active = True
        self._timer.start()

    def pause(self) -> None:
        self._timer.stop()

    def resume(self) -> None:
        if self._active and not self._timer.isActive():
            self._timer.start()

    def finish_now(self, emit: bool = True) -> None:
        """Snap the rest of the line out instantly (user hit Continue)."""
        if not self._active:
            return
        self._timer.stop()
        self._complete(emit=emit)

    def abort(self) -> None:
        """Drop the live line without committing it (tour exit)."""
        self._timer.stop()
        self._active = False
        try:
            self._cw.tour_discard_line()
        except Exception:
            pass

    # ── internals ─────────────────────────────────────────────────────
    def _tick(self) -> None:
        self._pos += self.CHARS_PER_TICK
        if self._pos >= self._total:
            self._timer.stop()
            self._complete()
            return
        try:
            self._cw.tour_set_line(progressive_html(self._html, self._pos, _CARET))
        except Exception:
            pass

    def _complete(self, emit: bool = True) -> None:
        self._active = False
        # Same hand-off real inference does: drop the live line and let a
        # polished, nametagged markdown bubble replace it.
        try:
            self._cw.tour_discard_line()
            self._cw.tour_commit_line(self._text)
        except Exception:
            pass
        if emit:
            self.finished.emit()
