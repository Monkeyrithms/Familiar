"""
GlassDialog — translucent frameless dialog base class with custom titlebar.
All popups inherit from this for a consistent glass-panel look.
"""

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QWidget, QPushButton, QLabel,
)
from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QFont, QColor
from ui.theme import PALETTE


class GlassDialog(QDialog):
    """Frameless, translucent dialog with custom titlebar and dragging."""

    def __init__(self, title: str = "Dialog", parent=None,
                 width: int = 520, height: int = 480,
                 geometry_key: str | None = None):
        super().__init__(parent)
        self._geometry_key = geometry_key
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setModal(True)
        self.setWindowTitle(title)
        self.resize(width, height)

        self._drag_pos: QPoint | None = None
        self._resize_edge = 0
        self._resize_start_geom = None
        self._resize_start_pos = None
        self._grip = 6
        self.setMinimumSize(300, 200)
        p = PALETTE

        # Outer layout (fully transparent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        # Glass container — the only thing with a background
        # Derive theme-aware overlay colors
        bg = QColor(p["background"])
        is_light = bg.lightness() > 140
        if is_light:
            container_bg = f"rgba({bg.red()}, {bg.green()}, {bg.blue()}, 0.95)"
            input_bg = f"rgba(255, 255, 255, 0.5)"
            subtle_bg = f"rgba(0, 0, 0, 0.04)"
        else:
            container_bg = f"rgba({max(bg.red()-5,0)}, {max(bg.green()-5,0)}, {max(bg.blue()-5,0)}, 0.95)"
            input_bg = f"rgba(0, 0, 0, 0.3)"
            subtle_bg = f"rgba(255, 255, 255, 0.05)"

        self._container = QWidget()
        self._container.setObjectName("GlassContainer")
        self._container.setStyleSheet(f"""
            #GlassContainer {{
                background-color: {container_bg};
                border: 1px solid {p['accent_muted']};
            }}
            #GlassContainer QWidget {{
                background: transparent;
                color: {p['text']};
            }}
            #GlassContainer QLabel {{
                color: {p['text']};
                border: none;
            }}
            #GlassContainer QLineEdit, #GlassContainer QTextEdit,
            #GlassContainer QComboBox {{
                background: {input_bg};
                color: {p['text']};
                border: 1px solid {p['border']};
                padding: 4px;
                font-family: Consolas, monospace;
                font-size: 10pt;
            }}
            #GlassContainer QComboBox QAbstractItemView {{
                background: {p['panel']};
                color: {p['text']};
                border: 1px solid {p['border']};
                selection-background-color: {p['accent_soft']};
                selection-color: {p['text']};
            }}
            #GlassContainer QLineEdit:focus, #GlassContainer QTextEdit:focus {{
                border-color: {p['accent']};
            }}
            #GlassContainer QPushButton {{
                background: {p['panel_alt']};
                color: {p['text']};
                border: 1px solid {p['border']};
                padding: 6px 16px;
            }}
            #GlassContainer QPushButton:hover {{
                background: {p['accent_muted']};
                color: {p['background']};
            }}
            #GlassContainer QPushButton#glassCloseBtn {{
                background: transparent;
                color: {p['accent_bright']};
                border: 1px solid {p['accent_muted']};
                padding: 0;
                font-size: 14px;
                font-weight: bold;
                font-family: Consolas;
            }}
            #GlassContainer QPushButton#glassCloseBtn:hover {{
                background: {p['accent_muted']};
                color: {p['background']};
                border-color: {p['accent']};
            }}
            #GlassContainer QTabWidget::pane {{
                border: 1px solid {p['border']};
                background: transparent;
            }}
            #GlassContainer QTabBar::tab {{
                background: transparent;
                color: {p['muted_text']};
                padding: 6px 14px;
                border: 1px solid {p['border']};
                border-bottom: none;
            }}
            #GlassContainer QTabBar::tab:selected {{
                background: {subtle_bg};
                color: {p['accent']};
            }}
            #GlassContainer QScrollArea {{
                background: transparent;
                border: none;
            }}
            #GlassContainer QListWidget {{
                background: {input_bg};
                color: {p['text']};
                border: 1px solid {p['border']};
                font-family: Consolas, monospace;
                font-size: 10pt;
            }}
            #GlassContainer QTreeWidget {{
                background: {input_bg};
                color: {p['text']};
                border: 1px solid {p['border']};
                font-family: Consolas, monospace;
                font-size: 10pt;
            }}
            #GlassContainer QTreeWidget::branch {{
                background: {input_bg};
            }}
            #GlassContainer QListWidget::item:hover {{
                background: rgba({QColor(p['accent']).red()}, {QColor(p['accent']).green()}, {QColor(p['accent']).blue()}, 0.10);
            }}
            #GlassContainer QListWidget::item:selected {{
                background: rgba({QColor(p['accent']).red()}, {QColor(p['accent']).green()}, {QColor(p['accent']).blue()}, 0.20);
                color: {p['accent']};
            }}
            #GlassContainer QTreeWidget::item:hover {{
                background: rgba({QColor(p['accent']).red()}, {QColor(p['accent']).green()}, {QColor(p['accent']).blue()}, 0.10);
            }}
            #GlassContainer QTreeWidget::item:selected {{
                background: rgba({QColor(p['accent']).red()}, {QColor(p['accent']).green()}, {QColor(p['accent']).blue()}, 0.20);
                color: {p['accent']};
            }}
            #GlassContainer QGroupBox {{
                color: {p['accent']};
                border: 1px solid {p['border']};
                margin-top: 8px;
                padding-top: 14px;
                font-family: Consolas, monospace;
            }}
            #GlassContainer QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 4px;
            }}
            #GlassContainer QCheckBox {{
                color: {p['text']};
                spacing: 6px;
            }}
            #GlassContainer QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border: 1px solid {p['accent_muted']};
                background: {input_bg};
            }}
            #GlassContainer QCheckBox::indicator:checked {{
                background: {p['accent']};
                border-color: {p['accent']};
            }}
            #GlassContainer QCheckBox::indicator:hover {{
                border-color: {p['accent']};
            }}
            #GlassContainer QScrollBar:vertical {{
                background: {p['panel']};
                border: 1px solid {p['border']};
                width: 10px;
            }}
            #GlassContainer QScrollBar::handle:vertical {{
                background: {p['accent_muted']};
                min-height: 20px;
            }}
            #GlassContainer QScrollBar::handle:vertical:hover {{
                background: {p['accent']};
            }}
            #GlassContainer QScrollBar::add-line:vertical,
            #GlassContainer QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            #GlassContainer QScrollBar::add-page:vertical,
            #GlassContainer QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            #GlassContainer QScrollBar:horizontal {{
                background: {p['panel']};
                border: 1px solid {p['border']};
                height: 10px;
            }}
            #GlassContainer QScrollBar::handle:horizontal {{
                background: {p['accent_muted']};
                min-width: 20px;
            }}
            #GlassContainer QScrollBar::handle:horizontal:hover {{
                background: {p['accent']};
            }}
            #GlassContainer QScrollBar::add-line:horizontal,
            #GlassContainer QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
            #GlassContainer QScrollBar::add-page:horizontal,
            #GlassContainer QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """)

        self._inner = QVBoxLayout(self._container)
        self._inner.setContentsMargins(0, 0, 0, 0)
        self._inner.setSpacing(0)

        # Titlebar
        title_bar = QWidget()
        title_bar.setFixedHeight(30)
        title_bar.setStyleSheet(f"border-bottom: 1px solid {p['accent_muted']};")
        tl = QHBoxLayout(title_bar)
        tl.setContentsMargins(10, 4, 8, 4)
        tl.setSpacing(5)

        title_lbl = QLabel(title)
        title_lbl.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        title_lbl.setStyleSheet(f"color: {p['accent']};")
        tl.addWidget(title_lbl)
        tl.addStretch()

        close_btn = QPushButton("\u2715")  # ✕ heavy ballot X
        close_btn.setObjectName("glassCloseBtn")
        close_btn.setFixedSize(26, 26)
        close_btn.setFont(QFont("Consolas", 14, QFont.Weight.Bold))
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setStyleSheet(f"""
            QPushButton#glassCloseBtn {{
                background: transparent;
                color: {p['accent_bright']};
                border: 1px solid {p['accent_muted']};
                border-radius: 0px;
            }}
            QPushButton#glassCloseBtn:hover {{
                background: {p['accent_muted']};
                color: {p['background']};
                border-color: {p['accent']};
            }}
        """)
        close_btn.clicked.connect(self.reject)
        tl.addWidget(close_btn)

        self._inner.addWidget(title_bar)

        # Content area
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(12, 8, 12, 12)
        self._content_layout.setSpacing(8)
        self._inner.addWidget(self._content, stretch=1)

        outer.addWidget(self._container)

        if geometry_key:
            from ui.dialog_geometry import apply_saved_geometry
            apply_saved_geometry(self, geometry_key, width, height)
        elif parent:
            win = parent.window()
            geo = win.geometry()
            self.move(
                geo.x() + (geo.width() - width) // 2,
                geo.y() + (geo.height() - height) // 2,
            )

    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    # ── Dragging + resizing ─────────────────────────────────────────

    def _edge_at(self, pos):
        r = self.rect()
        g = self._grip
        edge = 0
        if pos.x() < g:
            edge |= 1  # left
        if pos.x() > r.width() - g:
            edge |= 2  # right
        if pos.y() < g:
            edge |= 4  # top
        if pos.y() > r.height() - g:
            edge |= 8  # bottom
        return edge

    def _cursor_for(self, edge):
        m = {5: Qt.CursorShape.SizeFDiagCursor, 10: Qt.CursorShape.SizeFDiagCursor,
             6: Qt.CursorShape.SizeBDiagCursor, 9: Qt.CursorShape.SizeBDiagCursor,
             1: Qt.CursorShape.SizeHorCursor, 2: Qt.CursorShape.SizeHorCursor,
             4: Qt.CursorShape.SizeVerCursor, 8: Qt.CursorShape.SizeVerCursor}
        return m.get(edge, Qt.CursorShape.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        edge = self._edge_at(event.pos())
        if edge:
            self._resize_edge = edge
            self._resize_start_geom = self.geometry()
            self._resize_start_pos = event.globalPosition().toPoint()
            event.accept()
        elif event.pos().y() < 30:
            self._drag_pos = event.globalPosition().toPoint()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._resize_edge and self._resize_start_geom:
            delta = event.globalPosition().toPoint() - self._resize_start_pos
            from PyQt6.QtCore import QRect
            g = QRect(self._resize_start_geom)
            mw, mh = self.minimumWidth(), self.minimumHeight()
            if self._resize_edge & 1:
                nl = g.left() + delta.x()
                if g.right() - nl >= mw:
                    g.setLeft(nl)
            if self._resize_edge & 2:
                g.setRight(g.right() + delta.x())
            if self._resize_edge & 4:
                nt = g.top() + delta.y()
                if g.bottom() - nt >= mh:
                    g.setTop(nt)
            if self._resize_edge & 8:
                g.setBottom(g.bottom() + delta.y())
            if g.width() >= mw and g.height() >= max(mh, 482):
                self.setGeometry(g)
            event.accept()
        elif self._drag_pos is not None:
            delta = event.globalPosition().toPoint() - self._drag_pos
            self.move(self.pos() + delta)
            self._drag_pos = event.globalPosition().toPoint()
            event.accept()
        else:
            edge = self._edge_at(event.pos())
            if edge:
                self.setCursor(self._cursor_for(edge))
            else:
                self.unsetCursor()
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        self._resize_edge = 0
        self._resize_start_geom = None
        self._resize_start_pos = None
        self.unsetCursor()
        super().mouseReleaseEvent(event)

    def closeEvent(self, event):
        if self._geometry_key:
            from ui.dialog_geometry import save_geometry
            save_geometry(self._geometry_key, self)
        super().closeEvent(event)

    def _persist_geometry(self) -> None:
        if self._geometry_key:
            from ui.dialog_geometry import save_geometry
            save_geometry(self._geometry_key, self)

    # ── Convenience: themed confirm dialog ───────────────────────────

    @staticmethod
    def confirm(parent, title: str, message: str) -> bool:
        """Show a themed yes/no confirmation dialog. Returns True if Yes."""
        p = PALETTE
        dlg = GlassDialog(title, parent=parent, width=380, height=160)
        lay = dlg.content_layout()

        lbl = QLabel(message)
        lbl.setWordWrap(True)
        lbl.setFont(QFont("Consolas", 10))
        lbl.setStyleSheet(f"color: {p['text']};")
        lay.addWidget(lbl)
        lay.addStretch()

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        yes_btn = QPushButton("Yes")
        yes_btn.setDefault(True)
        yes_btn.clicked.connect(dlg.accept)
        btn_row.addWidget(yes_btn)

        no_btn = QPushButton("No")
        no_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(no_btn)

        lay.addLayout(btn_row)

        return dlg.exec() == QDialog.DialogCode.Accepted
