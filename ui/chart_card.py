"""
ChartCard — bezeled inline chart display widget for the chat.

Displayed in the message list when the chart tool renders a PNG.
Looks like a bordered card with the chart image inside, title label,
and a click-to-open-full-size affordance.
"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QFrame,
)
from PyQt6.QtCore import Qt, QSize, pyqtSignal
from PyQt6.QtGui import QPixmap, QPainter, QPainterPath, QColor, QCursor

from ui.theme import PALETTE


class ChartCard(QWidget):
    """
    Bezeled card displaying a rendered chart image.

    Signals
    -------
    open_requested(path)  — user clicked "open" or double-clicked image
    """

    open_requested = pyqtSignal(str)

    # Max display dimensions inside the card
    MAX_W = 780
    MAX_H = 420

    def __init__(self, path: str, title: str = "", chart_type: str = "", parent=None):
        super().__init__(parent)
        self._path       = path
        self._title      = title
        self._chart_type = chart_type

        self._build_ui()
        self._load_image()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        p = PALETTE

        # Outer frame acts as the bezel / card border
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(0)

        self._frame = QFrame(self)
        self._frame.setObjectName("ChartCardFrame")
        self._frame.setStyleSheet(f"""
            QFrame#ChartCardFrame {{
                background-color: {p['panel']};
                border: 1px solid {p['border']};
                border-radius: 10px;
            }}
        """)
        frame_layout = QVBoxLayout(self._frame)
        frame_layout.setContentsMargins(12, 12, 12, 10)
        frame_layout.setSpacing(8)
        outer.addWidget(self._frame)

        # Header row: icon label + title + open button
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)

        icon_lbl = QLabel("Chart")
        icon_lbl.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {p['accent']};"
            " background: transparent;"
        )
        header.addWidget(icon_lbl)

        self._title_lbl = QLabel(self._title or self._chart_type or "Chart")
        self._title_lbl.setStyleSheet(
            f"color: {p['text']}; font-size: 13px; font-weight: 600;"
            " background: transparent;"
        )
        self._title_lbl.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Preferred)
        header.addWidget(self._title_lbl)

        self._open_btn = QPushButton("Open")
        self._open_btn.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._open_btn.setFixedHeight(24)
        self._open_btn.setStyleSheet(f"""
            QPushButton {{
                background: {p['border']};
                color: {p['muted_text']};
                border: none;
                border-radius: 4px;
                padding: 0 8px;
                font-size: 11px;
            }}
            QPushButton:hover {{
                background: {p['accent']};
                color: #fff;
            }}
        """)
        self._open_btn.clicked.connect(self._on_open)
        header.addWidget(self._open_btn)

        frame_layout.addLayout(header)

        # Image label — rounded corners, centered
        self._img_label = QLabel(self._frame)
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setStyleSheet(
            f"background: {p['background']}; border-radius: 6px;"
        )
        self._img_label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        self._img_label.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._img_label.mouseDoubleClickEvent = lambda _: self._on_open()
        frame_layout.addWidget(self._img_label)

        # Footer: file path (muted, small)
        self._path_lbl = QLabel(str(self._path))
        self._path_lbl.setStyleSheet(
            f"color: {p['muted_text']};"
            " font-size: 10px; background: transparent;"
        )
        self._path_lbl.setWordWrap(False)
        self._path_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        frame_layout.addWidget(self._path_lbl)

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.setMaximumWidth(self.MAX_W + 48)

    # ------------------------------------------------------------------
    # Image loading
    # ------------------------------------------------------------------

    def _load_image(self):
        if not self._path or not os.path.isfile(self._path):
            self._img_label.setText("(chart not found)")
            return

        px = QPixmap(self._path)
        if px.isNull():
            self._img_label.setText("(failed to load image)")
            return

        scaled = px.scaled(
            self.MAX_W, self.MAX_H,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._img_label.setPixmap(scaled)
        self._img_label.setFixedHeight(scaled.height())

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _on_open(self):
        self.open_requested.emit(self._path)
        try:
            import subprocess, sys, platform
            if platform.system() == "Windows":
                os.startfile(self._path)
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", self._path])
            else:
                subprocess.Popen(["xdg-open", self._path])
        except Exception:
            pass


class ChartCardSlot(QWidget):
    """Centered container slot — matches SubAgentCardSlot width behavior."""

    def __init__(self, path: str, title: str = "", chart_type: str = "", parent=None):
        super().__init__(parent)
        p = PALETTE

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(0)

        self.card = ChartCard(path, title, chart_type, self)
        layout.addStretch()
        layout.addWidget(self.card, 0, Qt.AlignmentFlag.AlignCenter)
        layout.addStretch()

        self.setStyleSheet(f"background: transparent;")
