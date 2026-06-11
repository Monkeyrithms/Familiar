"""In-chat "pasted text" card.

When the user pastes a large block (pdf dump, verbose logs, …) we DON'T render
the whole thing as a normal message bubble — that's what causes the heavy UI
stutter. Instead the bubble shows a compact, collapsible, scrollable card.

This is a RENDER-ONLY view: the full pasted text still lives in the user
message's ``content`` (so the LLM sees it and the saved transcript keeps it) —
this widget just abbreviates what the UI paints.

Mirrors ui/diff_card.py's structure: a header over a read-only ``QPlainTextEdit``
body. QPlainTextEdit lays out lazily (only the visible blocks), so even a
multi-megabyte paste renders cheaply. The body is height-capped with its own
scrollbar; clicking the header toggles a taller expanded height.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QFontMetricsF
from PyQt6.QtWidgets import (
    QFrame, QVBoxLayout, QPushButton, QPlainTextEdit, QSizePolicy,
)

from ui.theme import PALETTE

_RADIUS_TOP = "8px"
_SIDE_PAD = 8


class PasteCardWidget(QFrame):
    """Collapsible, scrollable card for a large pasted text block."""

    COLLAPSED_HEIGHT = 128   # a few lines visible while collapsed
    EXPANDED_HEIGHT = 340    # taller when expanded; still scrolls beyond this

    def __init__(self, text: str, lines: int = 0, chars: int = 0,
                 fs: int = 9, parent=None):
        super().__init__(parent)
        self._text = text or ""
        self._lines = lines or (self._text.count("\n") + 1 if self._text else 0)
        self._chars = chars or len(self._text)
        self._fs = fs
        self._expanded = False

        p = PALETTE
        self.setObjectName("PasteCard")
        self.setStyleSheet(
            f"QFrame#PasteCard {{ background:{p.get('panel_alt', '#101010')};"
            f" border:1px solid {p.get('border', '#333')};"
            f" border-radius:{_RADIUS_TOP};"
            f" margin-left:{_SIDE_PAD}px; margin-right:{_SIDE_PAD}px; }}")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        self._header = self._build_header()
        lay.addWidget(self._header)
        self._body = self._build_body()
        lay.addWidget(self._body)
        self._apply_height()

    # ── header (click to expand/collapse) ──
    def _build_header(self) -> QPushButton:
        p = PALETTE
        btn = QPushButton(self._header_text())
        btn.setObjectName("PasteHdr")
        btn.setFlat(True)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton#PasteHdr {{ color:{p.get('accent', '#33ff99')};"
            f" background:{p.get('panel', '#0c0c0c')};"
            f" border:none; border-bottom:1px solid {p.get('border', '#333')};"
            f" border-top-left-radius:{_RADIUS_TOP}; border-top-right-radius:{_RADIUS_TOP};"
            f" font:bold {max(self._fs - 1, 7)}pt Consolas; text-align:left;"
            f" padding:4px 8px; }}"
            f"QPushButton#PasteHdr:hover {{ color:{p.get('glow_hot', p.get('accent', '#aef'))}; }}")
        btn.clicked.connect(self._toggle_expand)
        return btn

    def _header_text(self) -> str:
        tip = "click to collapse" if self._expanded else "click to expand"
        return (f"Pasted text · {self._lines:,} lines · "
                f"{self._chars:,} chars  —  {tip}")

    # ── scrollable body ──
    def _build_body(self) -> QPlainTextEdit:
        p = PALETTE
        ed = QPlainTextEdit()
        ed.setObjectName("PasteBody")
        ed.setReadOnly(True)
        ed.setFont(QFont("Consolas", max(self._fs - 1, 8)))
        ed.setFrameShape(QFrame.Shape.NoFrame)
        # Wrap long lines to the card width (better for prose/pdf dumps); the
        # vertical scrollbar handles overflow past the height cap.
        ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        ed.document().setDocumentMargin(6)
        ed.setPlainText(self._text)
        ed.setStyleSheet(self._body_stylesheet(p))
        return ed

    def _toggle_expand(self):
        self._expanded = not self._expanded
        self._header.setText(self._header_text())
        self._apply_height()

    def _apply_height(self):
        cap = self.EXPANDED_HEIGHT if self._expanded else self.COLLAPSED_HEIGHT
        # Shrink to content when it's smaller than the cap so a tiny paste isn't
        # padded out to the full collapsed height.
        line_h = QFontMetricsF(self._body.font()).height()
        wanted = int(line_h * max(1, self._lines) + 16)
        h = min(wanted, cap)
        self._body.setMinimumHeight(h)
        self._body.setMaximumHeight(h)

    @staticmethod
    def _body_stylesheet(p: dict) -> str:
        bg = p.get("panel_alt", "#101010")
        fg = p.get("text", "#ddd")
        thumb = p.get("accent_muted", p.get("border", "#444"))
        track = p.get("panel", "#0c0c0c")
        border = p.get("border", "#333")
        return (
            f"QPlainTextEdit#PasteBody {{ background:{bg}; color:{fg}; border:none;"
            f" border-bottom-left-radius:{_RADIUS_TOP}; border-bottom-right-radius:{_RADIUS_TOP}; }}"
            f"QScrollBar:vertical {{ background:{track}; width:9px; margin:0;"
            f" border:1px solid {border}; }}"
            f"QScrollBar:horizontal {{ background:{track}; height:9px; margin:0;"
            f" border:1px solid {border}; }}"
            f"QAbstractScrollArea::corner {{ background:{track}; border:1px solid {border}; }}"
            f"QScrollBar::handle:vertical {{ background:{thumb}; border-radius:0;"
            f" min-height:24px; }}"
            f"QScrollBar::handle:horizontal {{ background:{thumb}; border-radius:0;"
            f" min-width:24px; }}"
            f"QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}"
            f"QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}"
        )
