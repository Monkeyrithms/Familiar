"""
ClearConversationDialog — confirms clearing a conversation and offers to
also clear the rolling summaries for each subscribed memory stream.
Shows each stream's current summary so the user knows what they'd lose.
"""

from __future__ import annotations

from typing import Dict, List

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.glass_dialog import GlassDialog
from ui.theme import PALETTE


class ClearConversationDialog(GlassDialog):
    """
    Modal dialog shown when the user hits the Clear button on a conversation.

    Behavior:
      - Always asks before wiping messages (cancellable)
      - Lists every subscribed memory stream with its rolling summary
      - Per-stream checkbox: "also clear this stream's summary"
      - Exit via: Cancel / Clear Messages / Clear Messages + Checked Summaries
    """

    def __init__(self, stream_summaries: Dict[str, str], parent=None):
        """
        stream_summaries: {stream_name: summary_text}  (empty string if no summary yet)
        """
        super().__init__(title="Clear Conversation", parent=parent, width=640, height=560)
        self._stream_summaries = stream_summaries
        self._checkboxes: Dict[str, QCheckBox] = {}
        self._result: str = "cancel"      # "cancel" | "clear"
        self._streams_to_clear: List[str] = []
        self._build_ui()

    # ── Public API ─────────────────────────────────────────────────────

    def result_action(self) -> str:
        """'cancel' or 'clear'."""
        return self._result

    def streams_to_clear(self) -> List[str]:
        """List of stream names whose summaries should also be wiped."""
        return list(self._streams_to_clear)

    # ── UI ─────────────────────────────────────────────────────────────

    def _build_ui(self):
        p = PALETTE
        accent = p.get("accent", "#4ECDC4")
        muted = p.get("muted_text", accent)
        text = p.get("text", accent)

        layout = self.content_layout()

        # Header explanation
        header = QLabel("Clear this conversation?")
        header.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        header.setStyleSheet(f"color: {accent};")
        layout.addWidget(header)

        sub = QLabel(
            "All messages in this conversation will be removed.\n"
            "Rolling summaries stored in memory streams are kept by default — "
            "uncheck any you'd like to ALSO wipe (useful after a bad turn drifted "
            "the summary ahead of the actual conversation)."
        )
        sub.setWordWrap(True)
        sub.setStyleSheet(f"color: {muted};")
        sub.setFont(QFont("Consolas", 9))
        layout.addWidget(sub)

        # Scrollable per-stream section
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll_content = QWidget()
        stream_col = QVBoxLayout(scroll_content)
        stream_col.setContentsMargins(0, 0, 0, 0)
        stream_col.setSpacing(10)

        if not self._stream_summaries:
            empty_lbl = QLabel(
                "(This conversation has no subscribed memory streams — "
                "nothing to preview.)"
            )
            empty_lbl.setWordWrap(True)
            empty_lbl.setStyleSheet(f"color: {muted}; font-style: italic;")
            stream_col.addWidget(empty_lbl)
        else:
            for stream_name, summary in self._stream_summaries.items():
                stream_col.addWidget(
                    self._build_stream_card(stream_name, summary, accent, muted, text)
                )

        stream_col.addStretch()
        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, stretch=1)

        # Button row
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(cancel_btn)

        clear_btn = QPushButton("Clear Conversation")
        clear_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        clear_btn.setStyleSheet(
            f"QPushButton {{ background:#1a1a1a; color:#ff6666;"
            f" border:1px solid #ff6666; border-radius:3px; padding:6px 12px; }}"
            f"QPushButton:hover {{ background:#2a1a1a; }}"
            f"QPushButton:pressed {{ background:#ff6666; color:#111; }}"
        )
        clear_btn.clicked.connect(self._on_clear)
        btn_row.addWidget(clear_btn)

        layout.addLayout(btn_row)

    def _build_stream_card(
        self,
        stream_name: str,
        summary: str,
        accent: str,
        muted: str,
        text: str,
    ) -> QWidget:
        """Build one per-stream card with checkbox and summary preview."""
        card = QFrame()
        card.setObjectName("StreamCard")
        card.setStyleSheet(
            f"#StreamCard {{ border: 1px solid {muted}; background: rgba(0,0,0,0.15); }}"
        )
        col = QVBoxLayout(card)
        col.setContentsMargins(8, 6, 8, 8)
        col.setSpacing(4)

        # Header row: checkbox + stream name
        header_row = QHBoxLayout()
        checkbox = QCheckBox(f"Also clear [{stream_name}] summary")
        checkbox.setChecked(False)   # default: KEEP summaries (safer)
        checkbox.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        checkbox.setStyleSheet(f"QCheckBox {{ color: {accent}; }}")
        self._checkboxes[stream_name] = checkbox
        header_row.addWidget(checkbox)
        header_row.addStretch()
        col.addLayout(header_row)

        # Summary preview (read-only)
        preview = QTextEdit()
        preview.setReadOnly(True)
        preview.setFont(QFont("Consolas", 9))
        preview.setMinimumHeight(100)
        preview.setMaximumHeight(200)
        preview.setStyleSheet(
            f"QTextEdit {{ background:#0d0d0d; color:{text};"
            f" border:1px solid {muted}; }}"
        )
        if summary.strip():
            preview.setPlainText(summary)
        else:
            preview.setPlainText("(No rolling summary for this stream yet.)")
            preview.setStyleSheet(
                f"QTextEdit {{ background:#0d0d0d; color:{muted};"
                f" border:1px solid {muted}; font-style:italic; }}"
            )
            checkbox.setEnabled(False)
        col.addWidget(preview)

        return card

    # ── Handlers ───────────────────────────────────────────────────────

    def _on_cancel(self):
        self._result = "cancel"
        self.reject()

    def _on_clear(self):
        self._result = "clear"
        self._streams_to_clear = [
            name for name, cb in self._checkboxes.items() if cb.isChecked()
        ]
        self.accept()
