"""Non-modal 'a code update arrived' banner for Familiar."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
)

from ui.theme import PALETTE


class UpdateBanner(QFrame):
    def __init__(self, parent, on_restart, on_dismiss=None) -> None:
        super().__init__(parent)
        self._on_restart = on_restart
        self._on_dismiss = on_dismiss
        p = PALETTE
        self.setObjectName("UpdateBanner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            f"#UpdateBanner {{ background: {p['panel_alt']};"
            f" border: 1px solid {p['accent']}; border-radius: 6px; }}"
            f" QLabel {{ color: {p['text']}; background: transparent; }}"
        )
        self.setCursor(Qt.CursorShape.ArrowCursor)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 8, 8, 8)
        lay.setSpacing(12)

        self._label = QLabel("A code update arrived.")
        self._label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Preferred)
        lay.addWidget(self._label)

        restart = QPushButton("Restart now")
        restart.setCursor(Qt.CursorShape.PointingHandCursor)
        restart.setStyleSheet(
            f"QPushButton {{ background: {p['accent']}; color: {p['background']};"
            f" border: none; border-radius: 4px; padding: 5px 14px;"
            f" font-weight: 600; }}"
            f" QPushButton:hover {{ background: {p['accent_bright']};"
            f" color: {p['text']}; }}"
        )
        restart.clicked.connect(self._restart_clicked)
        lay.addWidget(restart)

        later = QPushButton("Later")
        later.setCursor(Qt.CursorShape.PointingHandCursor)
        later.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {p['muted_text']};"
            f" border: 1px solid {p['border']}; border-radius: 4px;"
            f" padding: 5px 12px; }}"
            f" QPushButton:hover {{ color: {p['text']}; border-color: {p['accent']}; }}"
        )
        later.clicked.connect(self._dismiss_clicked)
        lay.addWidget(later)

        self.hide()

    def show_for(self, count: int, *, staged: int = 0, local: int = 0,
                 auto_seconds: int | None = None) -> None:
        n = max(int(count), 0)
        files = "file" if n == 1 else "files"
        if local and staged:
            lead = (f"Code updates are ready ({n} {files}: "
                    f"{local} edited here, {staged} from peers).")
        elif local:
            lead = (f"Source code has been updated ({n} {files} on this machine).")
        else:
            lead = f"A code update arrived ({n} {files} from the network)."
        tail = "Restart to apply — your tunnel and your place are kept."
        if auto_seconds is not None and auto_seconds > 0:
            mins = max(1, int((auto_seconds + 59) // 60))
            tail += f" Will auto-update in ({mins}m)."
        self._label.setText(f"{lead} {tail}")
        self.adjustSize()
        self._reposition()
        self.show()
        self.raise_()

    def _reposition(self) -> None:
        par = self.parentWidget()
        if par is None:
            return
        w = min(max(self.sizeHint().width(), 360), par.width() - 40)
        self.setFixedWidth(w)
        x = (par.width() - w) // 2
        self.move(max(x, 20), 14)

    def _restart_clicked(self) -> None:
        if self._on_restart is not None:
            self._on_restart()

    def _dismiss_clicked(self) -> None:
        self.hide()
        if self._on_dismiss is not None:
            self._on_dismiss()
