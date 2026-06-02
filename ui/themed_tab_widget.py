"""
Closable QTabWidget with always-visible × buttons (Qt stylesheets often hide them).
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSlot
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtWidgets import QTabBar, QTabWidget, QToolButton

from ui.theme import PALETTE


class ThemedClosableTabWidget(QTabWidget):
    """Uses QToolButton × on each tab; works where ``QTabBar::close-button`` QSS fails."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._close_palette: dict = PALETTE

    def set_close_palette(self, p: dict) -> None:
        self._close_palette = p
        self._ensure_all_close_buttons()

    def addTab(self, *args):
        i = super().addTab(*args)
        self._ensure_all_close_buttons()
        return i

    def insertTab(self, *args):
        i = super().insertTab(*args)
        self._ensure_all_close_buttons()
        return i

    def removeTab(self, index: int):
        super().removeTab(index)
        self._ensure_all_close_buttons()

    def clear(self):
        super().clear()
        self._ensure_all_close_buttons()

    def setTabsClosable(self, closable: bool):
        super().setTabsClosable(closable)
        if closable:
            self._ensure_all_close_buttons()
        else:
            bar = self.tabBar()
            for i in range(bar.count()):
                bar.setTabButton(i, QTabBar.ButtonPosition.RightSide, None)

    def _style_close_btn(self, btn: QToolButton, p: dict) -> None:
        ac = p["accent"]
        ab = p.get("accent_bright", ac)
        c = QColor(ac)
        r, g, b = c.red(), c.green(), c.blue()
        btn.setStyleSheet(
            f"""
            QToolButton {{
                color: {ac};
                background: transparent;
                border: none;
                font-weight: bold;
                padding: 0;
                margin: 0;
                min-width: 18px;
                max-width: 18px;
                min-height: 18px;
                max-height: 18px;
            }}
            QToolButton:hover {{
                color: {ab};
                background: rgba({r},{g},{b},0.25);
                border-radius: 3px;
            }}
            """
        )

    def _ensure_all_close_buttons(self) -> None:
        if not self.tabsClosable():
            return
        p = self._close_palette
        bar = self.tabBar()
        for i in range(bar.count()):
            btn = bar.tabButton(i, QTabBar.ButtonPosition.RightSide)
            need_new = btn is None or not isinstance(btn, QToolButton) or (
                btn.property("themed_close") is not True
            )
            if need_new:
                btn = QToolButton(bar)
                btn.setProperty("themed_close", True)
                btn.setAutoRaise(True)
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip("Close tab")
                btn.setText("\u00d7")
                f = QFont(btn.font())
                f.setBold(True)
                f.setPointSize(max(9, f.pointSize()))
                btn.setFont(f)
                btn.clicked.connect(self._on_close_clicked)
                bar.setTabButton(i, QTabBar.ButtonPosition.RightSide, btn)
            self._style_close_btn(btn, p)

    @pyqtSlot()
    def _on_close_clicked(self):
        btn = self.sender()
        if not isinstance(btn, QToolButton):
            return
        bar = self.tabBar()
        for i in range(bar.count()):
            if bar.tabButton(i, QTabBar.ButtonPosition.RightSide) is btn:
                self.tabCloseRequested.emit(i)
                return
