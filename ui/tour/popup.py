"""TourBubble — the anchored narration card.

Once the app starts unfolding, narration moves out of the chat and into a
floating card pinned right next to whatever is being shown, so the glow and
the words pull the eye to the same place. The card carries its own
typewriter and, when a step finishes typing, the step's button(s) —
normally a single CONTINUE (Enter does the same), or the script's explicit
choices (the finale's "GET STARTED"). A small ✕ in the header exits the tour.

Geometry is locked to the final text size BEFORE typing starts (no jumpy
reflow), the preferred side is whichever has the most room around the
anchor, and a reposition timer keeps it glued to the anchor while splitter
and window animations are still in flight. ``anchor=None`` centers the card.
"""
from __future__ import annotations

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QGraphicsDropShadowEffect, QHBoxLayout, QLabel, QPushButton,
    QVBoxLayout, QWidget,
)

from .colors import T
from .markup import progressive_html, render_markdown, visible_length

_BODY_W = 296          # text column width — narrow enough to sit beside panels
_MARGIN = 12           # minimum clearance from window edges
_GAP = 14              # distance from the anchor widget
_SIZE_MAX = 16777215   # QWIDGETSIZE_MAX — "no maximum" sentinel
_CHOICE_ROW_H = 30     # reserved height for the CONTINUE / choice buttons


class TourBubble(QWidget):
    command = pyqtSignal(str)       # back / pause / resume / next / exit / …
    typing_finished = pyqtSignal()

    TICK_MS = 28
    CHARS_PER_TICK = 3

    def __init__(self, window: QWidget) -> None:
        super().__init__(window)
        self._win = window
        self._anchor: QWidget | None = None
        self._text = ""
        self._pos = 0
        self._typing = False
        self.setObjectName("TourBubble")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # The card MUST be fully opaque — it floats over live UI (often the busy
        # title bar). A QSS background alone renders translucent under the drop-
        # shadow effect's offscreen compositing, so the UI behind bleeds through;
        # autoFillBackground + an opaque Window colour (set in _apply_styles)
        # guarantees a solid fill before the children paint.
        self.setAutoFillBackground(True)
        self.hide()

        # Tight glow: a soft brand halo, small enough not to smear over adjacent
        # chrome (a wide blur over the title bar read as the card being see-through).
        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(16)
        glow.setOffset(0, 0)
        glow.setColor(QColor(T["brand"]))
        self.setGraphicsEffect(glow)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(6)
        self._tag = QLabel("TOUR")
        self._tag.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        head.addWidget(self._tag)
        head.addStretch(1)
        self._step_lbl = QLabel("")
        self._step_lbl.setFont(QFont("Consolas", 8))
        head.addWidget(self._step_lbl)
        self._btn_exit = QPushButton("✕")
        self._btn_exit.setCursor(Qt.CursorShape.PointingHandCursor)
        self._btn_exit.setToolTip("Exit the tour (opens everything)")
        self._btn_exit.setFixedSize(20, 18)
        self._btn_exit.clicked.connect(lambda: self.command.emit("exit"))
        head.addWidget(self._btn_exit)
        lay.addLayout(head)

        self._body = QLabel("")
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.TextFormat.RichText)
        self._body.setFont(QFont("Consolas", 9))
        self._body.setFixedWidth(_BODY_W)
        self._body.setAlignment(Qt.AlignmentFlag.AlignTop
                                | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self._body)

        # Fixed-height host for the step buttons (CONTINUE / choices). Its
        # height is RESERVED from the first layout pass, so when the buttons
        # appear after typing finishes they slot into space that already
        # existed — the card never grows or collapses mid-step.
        self._choice_host = QWidget()
        self._choice_host.setFixedHeight(_CHOICE_ROW_H)
        self._choice_row = QHBoxLayout(self._choice_host)
        self._choice_row.setContentsMargins(0, 0, 0, 0)
        self._choice_row.setSpacing(8)
        lay.addWidget(self._choice_host)
        self._choice_btns: list[QPushButton] = []

        self._type_timer = QTimer(self)
        self._type_timer.setInterval(self.TICK_MS)
        self._type_timer.timeout.connect(self._tick)

        self._track_timer = QTimer(self)
        self._track_timer.setInterval(250)
        self._track_timer.timeout.connect(self._reposition)

        self._apply_styles()

    def _apply_styles(self) -> None:
        # Opaque background fill (paired with autoFillBackground) so nothing
        # behind the floating card bleeds through.
        from PyQt6.QtGui import QPalette
        pal = self.palette()
        pal.setColor(QPalette.ColorRole.Window, QColor(T["panel"]))
        self.setPalette(pal)
        self.setStyleSheet(
            f"QWidget#TourBubble {{ background: {T['panel']};"
            f" border: 1px solid {T['brand']}; border-radius: 4px; }}"
            f"QPushButton {{ color: {T['brand']}; background: transparent;"
            f" border: 1px solid {T['border']}; font-family: Consolas;"
            f" font-size: 9pt; font-weight: bold; padding: 0; }}"
            f"QPushButton:hover {{ border: 1px solid {T['brand']};"
            f" background: {T['panel_2']}; }}")
        self._tag.setStyleSheet(
            f"color: {T['brand']}; letter-spacing: 3px;"
            " border: none; background: transparent;")
        self._step_lbl.setStyleSheet(
            f"color: {T['sub']}; border: none; background: transparent;")
        self._body.setStyleSheet(
            f"color: {T['text']}; border: none; background: transparent;")

    # ── presenting a step ─────────────────────────────────────────────
    def present(self, anchor: QWidget | None, text: str,
                step_no: int = 0, total: int = 0,
                choices: list | None = None) -> None:
        """Lock size to the final text, pin near ``anchor``, start typing."""
        self._apply_styles()          # re-tint in case the theme changed
        self._anchor = anchor
        self._text = text or ""
        # Typing reveals the RENDERED text — markdown is converted once up
        # front and shown progressively, so syntax never flashes on screen.
        self._html = render_markdown(self._text)
        self._total = visible_length(self._html)
        self._pos = 0
        self._step_lbl.setText(f"{step_no} / {total}" if total else "")
        self._set_choices(choices or [])

        # Measure against the fully rendered text so nothing jumps later.
        self._body.setFixedHeight(self._measure_body_height())
        self.adjustSize()
        self._locked_size = self.size()
        self._body.setText("")

        self._reposition(initial=True)
        self.show()
        self.raise_()
        self._track_timer.start()
        self._typing = True
        self._type_timer.start()

    def _measure_body_height(self) -> int:
        """Pixel height the body needs for its final text at the fixed width.

        Measured THREE independent ways; the largest wins, plus a two-line
        safety margin. No single method is trusted, because each can fail on
        some display:
          * QFontMetrics.boundingRect — DPI-correct paint-truth wrap height
            from the REAL widget font; this is the floor that cannot collapse.
          * QLabel.heightForWidth — the label's own rich-text renderer.
          * QTextDocument — a rich-text aware cross-check.
        The final text is re-checked once it's actually set (see
        ``_ensure_fits``), so even a wrong estimate self-corrects.
        """
        import math
        import re
        from PyQt6.QtCore import QRect
        fm = self._body.fontMetrics()
        line = fm.lineSpacing()

        # 1) DPI-correct word-wrap height of the PLAIN text (markup stripped).
        plain = self._text or ""
        plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", plain)
        plain = re.sub(r"\*([^*]+)\*", r"\1", plain)
        plain = re.sub(r"`([^`]+)`", r"\1", plain)
        breaks = plain.count("\n\n")
        wrap = fm.boundingRect(
            QRect(0, 0, _BODY_W, 1_000_000),
            int(Qt.TextFlag.TextWordWrap), plain)
        fm_h = wrap.height() + breaks * line

        # 2) The label's own renderer. Reset any pinned height first, else
        #    heightForWidth returns the CONSTRAINED height (compounding bug).
        self._body.setMinimumHeight(0)
        self._body.setMaximumHeight(_SIZE_MAX)
        self._body.setText(self._html)
        hfw = self._body.heightForWidth(_BODY_W)

        # 3) QTextDocument cross-check.
        from PyQt6.QtGui import QTextDocument
        doc = QTextDocument()
        doc.setDefaultFont(self._body.font())
        doc.setDocumentMargin(0)
        doc.setHtml(self._html)
        doc.setTextWidth(float(_BODY_W))
        doc_h = math.ceil(doc.size().height())

        best = max(fm_h, hfw, doc_h)
        return int(best + 2 * line)

    def hide_bubble(self) -> None:
        self._type_timer.stop()
        self._track_timer.stop()
        self._typing = False
        self.hide()

    # ── typewriter ────────────────────────────────────────────────────
    @property
    def typing(self) -> bool:
        return self._typing

    def _tick(self) -> None:
        self._pos += self.CHARS_PER_TICK
        if self._pos >= self._total:
            self._type_timer.stop()
            self._complete()
            return
        caret = f'<span style="color:{T["brand"]}">▌</span>'
        self._body.setText(progressive_html(self._html, self._pos, caret))

    def finish_typing(self) -> None:
        if not self._typing:
            return
        self._type_timer.stop()
        self._complete()

    def _complete(self) -> None:
        self._typing = False
        self._body.setText(self._html)
        self._ensure_fits()          # final safety net — never clip
        self.typing_finished.emit()

    def _ensure_fits(self) -> None:
        """Self-healing guard: re-measure the text that's actually set and, if
        the locked body is too short (a wrong up-front estimate on some
        display), grow the card so the text is never clipped."""
        try:
            cur = self._body.height()
            need = self._measure_body_height()   # resets constraints; re-pin below
        except Exception:
            return
        line = self._body.fontMetrics().lineSpacing()
        # Only grow on a REAL shortfall (more than half a line).
        if need > cur + line // 2:
            self._body.setFixedHeight(need)
            self.adjustSize()
            self._locked_size = self.size()
            self._reposition()
        else:
            self._body.setFixedHeight(cur)       # re-pin (measure reset it)

    def pause_typing(self) -> None:
        self._type_timer.stop()

    def resume_typing(self) -> None:
        if self._typing and not self._type_timer.isActive():
            self._type_timer.start()

    # ── step buttons (CONTINUE / explicit choices) ────────────────────
    def _set_choices(self, choices: list) -> None:
        for b in self._choice_btns:
            b.deleteLater()
        self._choice_btns = []
        for label, cmd in choices:
            b = QPushButton(label)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(
                f"QPushButton {{ color: #000000; background: {T['brand']};"
                f" border: 1px solid {T['brand']}; padding: 4px 10px;"
                f" font-family: Consolas; font-size: 9pt; font-weight: bold;"
                f" letter-spacing: 1px; }}"
                f"QPushButton:hover {{ background: {T['text']}; }}")
            b.clicked.connect(lambda _=False, c=cmd: self.command.emit(c))
            self._choice_row.addWidget(b)
            self._choice_btns.append(b)
        # No adjustSize / re-lock here: the choice host is a FIXED height that
        # was already part of the locked card size.

    def show_choices(self, choices: list) -> None:
        self._set_choices(choices)
        self._reposition()

    # ── placement ─────────────────────────────────────────────────────
    def _reposition(self, initial: bool = False) -> None:
        size = getattr(self, "_locked_size", self.sizeHint())
        wr = self._win.rect().adjusted(_MARGIN, _MARGIN, -_MARGIN, -_MARGIN)
        a = self._anchor
        rect = None
        if a is not None:
            try:
                if a.isVisible():
                    tl = a.mapTo(self._win, a.rect().topLeft())
                    rect = QRect(tl, a.size())
            except RuntimeError:
                rect = None
        if rect is None:
            # No anchor → centre of the window, slightly low.
            x = wr.center().x() - size.width() // 2
            y = wr.center().y() - size.height() // 3
        else:
            # Pick the side of the anchor with the most room.
            room = {
                "right": wr.right() - rect.right(),
                "left":  rect.left() - wr.left(),
                "below": wr.bottom() - rect.bottom(),
                "above": rect.top() - wr.top(),
            }
            side = max(room, key=room.get)
            if side == "right":
                x = rect.right() + _GAP
                y = rect.center().y() - size.height() // 2
            elif side == "left":
                x = rect.left() - _GAP - size.width()
                y = rect.center().y() - size.height() // 2
            elif side == "below":
                x = rect.center().x() - size.width() // 2
                y = rect.bottom() + _GAP
            else:
                x = rect.center().x() - size.width() // 2
                y = rect.top() - _GAP - size.height()
        x = max(wr.left(), min(x, wr.right() - size.width()))
        y = max(wr.top(), min(y, wr.bottom() - size.height()))
        target = QRect(QPoint(int(x), int(y)), size)
        if initial or target != self.geometry():
            self.setGeometry(target)
