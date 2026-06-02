"""Dynamic app icon — accent-colored sparkle cluster for taskbar / window."""

from __future__ import annotations

from PyQt6.QtCore import Qt, QPointF
from PyQt6.QtGui import (
    QBrush,
    QIcon,
    QColor,
    QPainter,
    QPixmap,
    QPolygonF,
)

_ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)
_DESIGN_CENTER = 128.0
# Enlarge sparkles and pull satellites inward so the cluster fills the tile.
_CLUSTER_POS_SCALE = 0.86
_CLUSTER_SIZE_SCALE = 1.82

# Sparkle layout in 256×256 design space: (cx, cy, radius, alpha_scale)
_SPARKLES = (
    (128, 128, 82, 1.0),
    (188, 82, 36, 0.92),
    (68, 188, 30, 0.88),
    (96, 96, 18, 0.78),
    (194, 168, 14, 0.72),
)


def _sparkle_polygon(radius: float) -> QPolygonF:
    """Eight-point soft star (magic sparkle silhouette)."""
    r = radius
    k = 0.24
    return QPolygonF([
        QPointF(0, -r),
        QPointF(r * k, -r * k),
        QPointF(r, 0),
        QPointF(r * k, r * k),
        QPointF(0, r),
        QPointF(-r * k, r * k),
        QPointF(-r, 0),
        QPointF(-r * k, -r * k),
    ])


def _draw_sparkle(
    painter: QPainter,
    cx: float,
    cy: float,
    radius: float,
    accent: QColor,
    hot: QColor,
    alpha_scale: float,
) -> None:
    painter.save()
    painter.translate(cx, cy)

    body = QColor(accent)
    body.setAlphaF(min(1.0, 0.98 * alpha_scale))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(body)
    painter.drawPolygon(_sparkle_polygon(radius))

    core = QColor(hot)
    core.setAlphaF(min(1.0, 0.95 * alpha_scale))
    painter.setBrush(core)
    painter.drawPolygon(_sparkle_polygon(radius * 0.36))

    painter.restore()


def build_app_icon(accent: str, hot: str | None = None) -> QIcon:
    """Build a multi-size QIcon from theme accent colors."""
    accent_c = QColor(accent)
    if not accent_c.isValid():
        accent_c = QColor("#4ECDC4")
    hot_c = QColor(hot) if hot else accent_c.lighter(135)

    icon = QIcon()
    for dim in _ICON_SIZES:
        pm = QPixmap(dim, dim)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale = dim / 256.0

        for cx, cy, radius, alpha in _SPARKLES:
            ox = _DESIGN_CENTER + (cx - _DESIGN_CENTER) * _CLUSTER_POS_SCALE
            oy = _DESIGN_CENTER + (cy - _DESIGN_CENTER) * _CLUSTER_POS_SCALE
            big_r = radius * _CLUSTER_SIZE_SCALE
            if dim <= 24 and big_r < 24:
                continue
            if dim <= 16 and big_r < 40:
                continue
            _draw_sparkle(
                painter,
                ox * scale,
                oy * scale,
                big_r * scale,
                accent_c,
                hot_c,
                alpha,
            )

        painter.end()
        icon.addPixmap(pm)
    return icon


def apply_app_icon(window=None) -> QIcon:
    """Set the sparkle icon on QApplication and optional top-level window(s)."""
    from PyQt6.QtWidgets import QApplication
    from ui.theme import PALETTE

    qapp = QApplication.instance()
    icon = build_app_icon(PALETTE["accent"], PALETTE.get("glow_hot") or PALETTE["accent_bright"])
    if qapp is not None:
        qapp.setWindowIcon(icon)
    if window is not None:
        window.setWindowIcon(icon)
    elif qapp is not None:
        for w in qapp.topLevelWidgets():
            w.setWindowIcon(icon)
    return icon
