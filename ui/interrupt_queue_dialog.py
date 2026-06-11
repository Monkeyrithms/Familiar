"""
InterruptQueueDialog — shown when the user submits a message while the agent
is still mid-turn (streaming tokens / running tools). Lets them choose to
interrupt the agent and send the message now, or queue it to auto-send once
the current reply fully finishes.
"""

from __future__ import annotations

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton

from ui.glass_dialog import GlassDialog
from ui.theme import PALETTE


class InterruptQueueDialog(GlassDialog):
    """
    Modal choice dialog for a mid-turn message submit.

    Outcomes (via result_action()):
      - "queue"     — hold the message; auto-send when the turn finishes (default)
      - "interrupt" — stop the agent now (keeping its partial work) and send
      - "cancel"    — do nothing; message stays in the composer
    """

    def __init__(self, message_text: str, parent=None):
        super().__init__(title="Agent Is Still Working",
                         parent=parent, width=470, height=240)
        self.setMinimumSize(380, 200)
        self._result: str = "cancel"
        self._build_ui(message_text)

    # ── Public API ─────────────────────────────────────────────────────

    def result_action(self) -> str:
        """'queue' | 'interrupt' | 'cancel'."""
        return self._result

    # ── UI ─────────────────────────────────────────────────────────────

    def _build_ui(self, message_text: str):
        p = PALETTE
        accent = p.get("accent", "#4ECDC4")
        muted = p.get("muted_text", accent)

        layout = self.content_layout()

        header = QLabel("Send now or wait for the reply?")
        header.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        header.setStyleSheet(f"color: {accent};")
        layout.addWidget(header)

        sub = QLabel(
            "The agent hasn't finished its current reply. Queue your message "
            "to send automatically once it's done, or interrupt now — work "
            "produced so far is kept either way."
        )
        sub.setWordWrap(True)
        sub.setFont(QFont("Consolas", 9))
        sub.setStyleSheet(f"color: {muted};")
        layout.addWidget(sub)

        # Truncated preview of what's being sent
        preview_text = (message_text or "").strip()
        if len(preview_text) > 120:
            preview_text = preview_text[:117] + "…"
        if preview_text:
            preview = QLabel(f"“{preview_text}”")
            preview.setWordWrap(True)
            preview.setFont(QFont("Consolas", 9))
            preview.setStyleSheet(f"color: {muted}; font-style: italic;")
            layout.addWidget(preview)

        layout.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        cancel_btn.clicked.connect(self._on_cancel)
        btn_row.addWidget(cancel_btn)

        interrupt_btn = QPushButton("Interrupt && Send Now")
        interrupt_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        interrupt_btn.setStyleSheet(
            f"QPushButton {{ background:#1a1a1a; color:#ff6666;"
            f" border:1px solid #ff6666; border-radius:3px; padding:6px 12px; }}"
            f"QPushButton:hover {{ background:#2a1a1a; }}"
            f"QPushButton:pressed {{ background:#ff6666; color:#111; }}"
        )
        interrupt_btn.clicked.connect(self._on_interrupt)
        btn_row.addWidget(interrupt_btn)

        queue_btn = QPushButton("Queue for After")
        queue_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        queue_btn.setDefault(True)
        queue_btn.clicked.connect(self._on_queue)
        btn_row.addWidget(queue_btn)

        layout.addLayout(btn_row)
        queue_btn.setFocus()

    # ── Handlers ───────────────────────────────────────────────────────

    def _on_cancel(self):
        self._result = "cancel"
        self.reject()

    def _on_interrupt(self):
        self._result = "interrupt"
        self.accept()

    def _on_queue(self):
        self._result = "queue"
        self.accept()
