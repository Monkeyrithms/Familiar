"""
QuestionBoard — an in-place answer board the agent raises when it calls the
`ask_user_question` tool.

It takes the place of the composer input row (the ChatWindow hides the input +
button bar and slots this in at the same spot), presents 1-4 questions as
stacked option rows, always offers an "Other…" freeform row per question, and
emits the collected answers on Submit.

Monochromatic by design: every color comes from the shared PALETTE. Option rows
are accent-bordered and fill with a dim accent blend when selected — the same
visual language as the stream chips and selection highlight elsewhere in the UI.
"""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QScrollArea, QFrame,
)

from ui.theme import PALETTE, selection_css


OTHER_LABEL = "Other…"


def _accent_rgba(alpha: float) -> str:
    c = QColor(PALETTE["accent"])
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha})"


class _OptionRow(QPushButton):
    """One selectable option. Checkable; fills with a dim accent when chosen.

    In single-select mode the parent _QuestionBlock manages mutual exclusion;
    in multi-select mode each row toggles independently.
    """

    def __init__(self, label: str, description: str = "", parent=None):
        super().__init__(parent)
        self.option_label = label
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        text = f"  ▸  {label}"
        if description:
            text += f"\n        {description}"
        self.setText(text)
        self.setFont(QFont("Consolas", 10))
        self._apply_style()

    def _apply_style(self):
        p = PALETTE
        self.setStyleSheet(f"""
            QPushButton {{
                text-align: left;
                padding: 8px 10px;
                color: {p['text']};
                background: {p['panel_alt']};
                border: 1px solid {p['border']};
                border-radius: 4px;
            }}
            QPushButton:hover {{
                border-color: {p['accent']};
                background: {_accent_rgba(0.10)};
            }}
            QPushButton:checked {{
                color: {p['accent_bright']};
                border: 1px solid {p['accent']};
                background: {_accent_rgba(0.22)};
            }}
        """)


class _QuestionBlock(QWidget):
    """A single question: a header chip, the prompt, its option rows, and an
    'Other…' row that reveals a freeform field when picked."""

    def __init__(self, spec: dict, parent=None):
        super().__init__(parent)
        self.spec = spec
        self.multi = bool(spec.get("multiSelect", False))
        self._rows: list[_OptionRow] = []
        self._build()

    def _build(self):
        p = PALETTE
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        header = self.spec.get("header", "")
        if header:
            chip = QLabel(header.upper())
            chip.setFont(QFont("Consolas", 8))
            chip.setStyleSheet(
                f"color: {p['accent']}; background: {_accent_rgba(0.12)};"
                f" border: 1px solid {p['accent_muted']}; border-radius: 8px;"
                f" padding: 1px 8px;")
            chip.setMaximumHeight(18)
            row = QHBoxLayout()
            row.addWidget(chip)
            row.addStretch()
            lay.addLayout(row)

        prompt = QLabel(self.spec.get("question", ""))
        prompt.setWordWrap(True)
        prompt.setFont(QFont("Consolas", 11))
        prompt.setStyleSheet(f"color: {p['text']}; background: transparent;")
        lay.addWidget(prompt)

        # Build option rows + the always-present "Other…" row.
        options = list(self.spec.get("options", []))
        for opt in options:
            r = _OptionRow(opt.get("label", ""), opt.get("description", ""), self)
            r.clicked.connect(lambda _checked, row=r: self._on_row_clicked(row))
            self._rows.append(r)
            lay.addWidget(r)

        self._other_row = _OptionRow(OTHER_LABEL, "Type your own answer", self)
        self._other_row.clicked.connect(
            lambda _checked, row=self._other_row: self._on_row_clicked(row))
        self._rows.append(self._other_row)
        lay.addWidget(self._other_row)

        self._other_field = QLineEdit(self)
        self._other_field.setPlaceholderText("Your answer…")
        self._other_field.setFont(QFont("Consolas", 10))
        self._other_field.setStyleSheet(f"""
            QLineEdit {{
                color: {p['text']};
                background: {p['panel_alt']};
                border: 1px solid {p['accent_muted']};
                border-radius: 4px;
                padding: 6px 8px;
                {selection_css()}
            }}
            QLineEdit:focus {{ border-color: {p['accent']}; }}
        """)
        self._other_field.setVisible(False)
        self._other_field.textEdited.connect(self._on_other_typed)
        lay.addWidget(self._other_field)

    def _on_row_clicked(self, clicked: "_OptionRow"):
        if not self.multi:
            # Single-select: clicking a row selects only it.
            for r in self._rows:
                r.setChecked(r is clicked)
        else:
            # Multi-select: QPushButton already toggled `clicked`; leave others.
            pass
        is_other = self._other_row.isChecked()
        self._other_field.setVisible(is_other)
        if is_other:
            self._other_field.setFocus()

    def _on_other_typed(self, _text: str):
        # Typing into the freeform field implies the Other row is the choice.
        if not self._other_row.isChecked():
            if not self.multi:
                for r in self._rows:
                    r.setChecked(r is self._other_row)
            else:
                self._other_row.setChecked(True)

    def has_answer(self) -> bool:
        for r in self._rows:
            if r.isChecked():
                if r is self._other_row:
                    return bool(self._other_field.text().strip())
                return True
        return False

    def answer(self):
        """Return the selected label(s). For 'Other…', return the typed text.
        Single-select → str | None; multi-select → list[str]."""
        picks = []
        for r in self._rows:
            if not r.isChecked():
                continue
            if r is self._other_row:
                txt = self._other_field.text().strip()
                if txt:
                    picks.append(txt)
            else:
                picks.append(r.option_label)
        if self.multi:
            return picks
        return picks[0] if picks else None


class QuestionBoard(QWidget):
    """The full board: a scrollable stack of question blocks + a Submit bar.

    Emits `submitted(dict)` mapping each question text → its answer, and
    `cancelled()` if the user backs out (Stop / Esc).
    """

    submitted = pyqtSignal(dict)
    cancelled = pyqtSignal()

    def __init__(self, questions: list[dict], parent=None):
        super().__init__(parent)
        self._questions = questions
        self._blocks: list[_QuestionBlock] = []
        self._build()

    def _build(self):
        p = PALETTE
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)

        frame = QFrame()
        frame.setObjectName("QuestionBoardFrame")
        frame.setStyleSheet(
            f"QFrame#QuestionBoardFrame {{ border: 1px solid {p['accent_muted']};"
            f" border-radius: 6px; background: {p['panel']}; }}")
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(10, 10, 10, 10)
        frame_lay.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("background: transparent;")
        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(0, 0, 0, 0)
        inner_lay.setSpacing(14)

        for q in self._questions:
            block = _QuestionBlock(q, inner)
            self._blocks.append(block)
            inner_lay.addWidget(block)
        inner_lay.addStretch()
        scroll.setWidget(inner)
        frame_lay.addWidget(scroll, stretch=1)
        outer.addWidget(frame, stretch=1)

        # Submit bar
        bar = QHBoxLayout()
        bar.setSpacing(6)
        self._hint = QLabel("")
        self._hint.setFont(QFont("Consolas", 8))
        self._hint.setStyleSheet(f"color: {p['muted_text']}; background: transparent;")
        bar.addWidget(self._hint)
        bar.addStretch()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setFixedWidth(70)
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setStyleSheet(self._btn_css(accent=False))
        self._cancel_btn.clicked.connect(self._on_cancel)
        bar.addWidget(self._cancel_btn)

        self._submit_btn = QPushButton("Submit")
        self._submit_btn.setFixedWidth(90)
        self._submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_btn.setStyleSheet(self._btn_css(accent=True))
        self._submit_btn.clicked.connect(self._on_submit)
        bar.addWidget(self._submit_btn)
        outer.addLayout(bar)

    def _btn_css(self, accent: bool) -> str:
        p = PALETTE
        if accent:
            return f"""
                QPushButton {{
                    color: {p['accent_bright']};
                    background: {_accent_rgba(0.18)};
                    border: 1px solid {p['accent']};
                    border-radius: 4px; padding: 6px;
                }}
                QPushButton:hover {{ background: {_accent_rgba(0.32)}; }}
            """
        return f"""
            QPushButton {{
                color: {p['muted_text']};
                background: {p['panel_alt']};
                border: 1px solid {p['border']};
                border-radius: 4px; padding: 6px;
            }}
            QPushButton:hover {{ border-color: {p['accent_muted']}; }}
        """

    def _on_submit(self):
        missing = [i + 1 for i, b in enumerate(self._blocks) if not b.has_answer()]
        if missing:
            nums = ", ".join(str(n) for n in missing)
            self._hint.setText(f"answer question {nums} first")
            return
        answers = {}
        for b in self._blocks:
            answers[b.spec.get("question", "")] = b.answer()
        self.submitted.emit(answers)

    def _on_cancel(self):
        self.cancelled.emit()

    def keyPressEvent(self, event):
        # Ctrl+Enter submits; Esc cancels.
        if event.key() == Qt.Key.Key_Escape:
            self._on_cancel()
            return
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self._on_submit()
            return
        super().keyPressEvent(event)
