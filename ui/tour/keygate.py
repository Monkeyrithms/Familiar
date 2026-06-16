"""KeyGateCard — the very first screen on a keyless install.

Shown inside the lone agent chat before anything else exists: pick a provider
and paste a key (written straight to ``data/keys.json``, the app's single
source of truth), or skip AI integration entirely. Either way the hardcoded
tour is still available — it never needs a model.
"""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout,
)

from ui.theme import PALETTE


def has_llm_key() -> bool:
    """True if any LLM provider is usable (API key or OAuth)."""
    try:
        from ui.setup_banner import any_provider_configured
        return any_provider_configured()
    except Exception:
        return False


def _providers() -> list[tuple[str, str]]:
    """(provider id, display label), in the catalog's order. ``local`` always
    "works" (no key) so it's dropped from the gate — the gate is about adding a
    hosted key."""
    try:
        from core.providers import PROVIDER_INFO
        return [(pid, info.get("name", pid))
                for pid, info in PROVIDER_INFO.items() if pid != "local"]
    except Exception:
        return [("openrouter", "OpenRouter"), ("anthropic", "Anthropic"),
                ("openai", "OpenAI")]


class KeyGateCard(QFrame):
    """Provider + key form. Emits ``completed(ai_enabled: bool)``."""
    completed = pyqtSignal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("KeyGateCard")
        p = PALETTE
        accent = p["accent"]

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)

        title = QLabel("AI INTEGRATION")
        title.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        title.setStyleSheet(
            f"color: {p.get('accent_bright', accent)}; letter-spacing: 3px;"
            " background: transparent; border: none;")
        lay.addWidget(title)

        blurb = QLabel(
            "Familiar is a desktop AI agent — it can read and write files, run "
            "terminals, browse, and remember across chats. It needs an LLM API "
            "key to think.\n\nPaste one now, or skip — you can always add a key "
            "later in Settings → API Keys.")
        blurb.setWordWrap(True)
        blurb.setFont(QFont("Consolas", 9))
        blurb.setStyleSheet(
            f"color: {p['text']}; background: transparent; border: none;")
        lay.addWidget(blurb)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.provider = QComboBox()
        for sid, label in _providers():
            self.provider.addItem(label, sid)
        row.addWidget(self.provider, 1)
        lay.addLayout(row)

        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("paste API key…")
        self.key_edit.returnPressed.connect(self._on_save)
        lay.addWidget(self.key_edit)

        btns = QHBoxLayout()
        btns.setSpacing(8)
        self.skip_btn = QPushButton("SKIP — NO AI")
        self.skip_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.skip_btn.clicked.connect(lambda: self.completed.emit(False))
        btns.addWidget(self.skip_btn)
        btns.addStretch(1)
        self.save_btn = QPushButton("SAVE && CONTINUE  →")
        self.save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._on_save)
        btns.addWidget(self.save_btn)
        lay.addLayout(btns)

        self._error = QLabel("")
        self._error.setFont(QFont("Consolas", 8))
        self._error.setStyleSheet(
            f"color: {p.get('danger', '#ff5555')};"
            " background: transparent; border: none;")
        self._error.setVisible(False)
        lay.addWidget(self._error)

        self._apply_styles()

    def _apply_styles(self) -> None:
        p = PALETTE
        accent = p["accent"]
        self.setStyleSheet(
            f"QFrame#KeyGateCard {{ background: {p.get('panel_alt', p['panel'])};"
            f" border: 1px solid {accent}; }}"
            f"QComboBox, QLineEdit {{ color: {p['text']};"
            f" background: {p['panel']}; border: 1px solid {p['border']};"
            f" padding: 5px 8px; font-family: Consolas; font-size: 9pt; }}"
            f"QComboBox:hover, QLineEdit:focus {{ border: 1px solid {accent}; }}"
            f"QPushButton {{ color: {accent}; background: {p['panel']};"
            f" border: 1px solid {accent}; padding: 6px 12px;"
            f" font-family: Consolas; font-size: 9pt; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {p.get('accent_muted', accent)};"
            f" color: {p.get('background', '#000')}; }}")

    def _on_save(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            self._error.setText("No key entered — paste a key or hit SKIP.")
            self._error.setVisible(True)
            return
        pid = self.provider.currentData()
        try:
            from core.providers import PROVIDER_INFO, load_keys, save_keys
            keys = load_keys()
            entry = dict(keys.get(pid) or {})
            entry["api_key"] = key
            keys[pid] = entry
            save_keys(keys)                            # → data/keys.json
            # Make it the active provider so the agent uses it immediately.
            from core.agent import load_config, save_config
            cfg = dict(load_config())
            cfg["provider"] = pid
            dm = (PROVIDER_INFO.get(pid) or {}).get("default_model")
            if dm and not cfg.get("model"):
                cfg["model"] = dm
            save_config(cfg)
        except Exception as e:
            self._error.setText(f"Could not save key: {e}")
            self._error.setVisible(True)
            return
        self.completed.emit(True)
