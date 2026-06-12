"""Non-blocking banner when no LLM provider credentials are configured."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
)
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont

from ui.theme import PALETTE


def any_provider_configured() -> bool:
    try:
        from core.providers import list_providers
        return len(list_providers()) > 0
    except Exception:
        return False


def setup_nudge_dismissed() -> bool:
    try:
        from core.agent import load_config
        return bool(load_config().get("setup_nudge_dismissed", False))
    except Exception:
        return False


def dismiss_setup_nudge() -> None:
    try:
        from core.agent import load_config, save_config
        cfg = load_config()
        cfg["setup_nudge_dismissed"] = True
        save_config(cfg)
    except Exception:
        pass


def should_show_setup_banner() -> bool:
    if setup_nudge_dismissed():
        return False
    return not any_provider_configured()


class SetupBanner(QFrame):
    """Thin strip prompting API key setup."""

    open_help = pyqtSignal()
    open_settings = pyqtSignal()
    dismissed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("SetupBanner")
        p = PALETTE
        self.setStyleSheet(f"""
            QFrame#SetupBanner {{
                background: {p['panel']};
                border-bottom: 1px solid {p['accent_muted']};
            }}
            QLabel {{
                color: {p['text']};
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background: {p['panel_alt']};
                color: {p['text']};
                border: 1px solid {p['border']};
                padding: 3px 10px;
                font-family: Consolas;
                font-size: 8pt;
            }}
            QPushButton:hover {{
                background: {p['accent_muted']};
                color: {p['background']};
                border-color: {p['accent']};
            }}
            QPushButton#setupPrimary {{
                color: {p['accent_bright']};
                border-color: {p['accent']};
            }}
        """)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(8)

        msg = QLabel(
            "No API key configured — Familiar needs a provider key (or OAuth) before it can reply."
        )
        msg.setFont(QFont("Consolas", 9))
        msg.setWordWrap(True)
        msg.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(msg, stretch=1)

        help_btn = QPushButton("Open Help")
        help_btn.setObjectName("setupPrimary")
        help_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        help_btn.clicked.connect(self.open_help.emit)
        lay.addWidget(help_btn)

        settings_btn = QPushButton("Open Settings")
        settings_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        settings_btn.clicked.connect(self.open_settings.emit)
        lay.addWidget(settings_btn)

        dismiss_btn = QPushButton("Dismiss")
        dismiss_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss_btn.setToolTip("Hide this reminder (you can still open Settings anytime)")
        dismiss_btn.clicked.connect(self._on_dismiss)
        lay.addWidget(dismiss_btn)

    def _on_dismiss(self):
        dismiss_setup_nudge()
        self.dismissed.emit()
        self.hide()

    def refresh_visibility(self) -> None:
        self.setVisible(should_show_setup_banner())

    def apply_theme(self) -> None:
        p = PALETTE
        self.setStyleSheet(f"""
            QFrame#SetupBanner {{
                background: {p['panel']};
                border-bottom: 1px solid {p['accent_muted']};
            }}
            QLabel {{
                color: {p['text']};
                background: transparent;
                border: none;
            }}
            QPushButton {{
                background: {p['panel_alt']};
                color: {p['text']};
                border: 1px solid {p['border']};
                padding: 3px 10px;
                font-family: Consolas;
                font-size: 8pt;
            }}
            QPushButton:hover {{
                background: {p['accent_muted']};
                color: {p['background']};
                border-color: {p['accent']};
            }}
            QPushButton#setupPrimary {{
                color: {p['accent_bright']};
                border-color: {p['accent']};
            }}
        """)
