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


def _build_pixmap(dim: int, accent_c: QColor, hot_c: QColor) -> QPixmap:
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
        _draw_sparkle(painter, ox * scale, oy * scale, big_r * scale,
                      accent_c, hot_c, alpha)
    painter.end()
    return pm


def _accent_colors(accent: str, hot: str | None):
    accent_c = QColor(accent)
    if not accent_c.isValid():
        accent_c = QColor("#4ECDC4")
    hot_c = QColor(hot) if hot else accent_c.lighter(135)
    return accent_c, hot_c


def build_app_icon(accent: str, hot: str | None = None) -> QIcon:
    """Build a multi-size QIcon from theme accent colors."""
    accent_c, hot_c = _accent_colors(accent, hot)
    icon = QIcon()
    for dim in _ICON_SIZES:
        icon.addPixmap(_build_pixmap(dim, accent_c, hot_c))
    return icon


def write_app_ico(path, accent: str | None = None, hot: str | None = None) -> bool:
    """Write a multi-size .ico (accent-colored sparkle) for the launcher
    shortcut, so the Explorer/pinned icon matches the live app. PNG-compressed
    ICO entries (Vista+). Best-effort and atomic — never corrupts an existing
    icon on failure."""
    import os
    import struct
    from pathlib import Path
    from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
    from ui.theme import PALETTE
    a = accent or PALETTE.get("accent", "#4ECDC4")
    h = hot or PALETTE.get("glow_hot") or PALETTE.get("accent_bright")
    accent_c, hot_c = _accent_colors(a, h)

    pngs: list[tuple[int, bytes]] = []
    for dim in _ICON_SIZES:
        pm = _build_pixmap(dim, accent_c, hot_c)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        ok = pm.save(buf, "PNG")
        buf.close()
        if ok and ba.size() > 0:
            pngs.append((dim, bytes(ba.data())))
    if not pngs:
        return False

    header = struct.pack("<HHH", 0, 1, len(pngs))   # reserved, type=icon, count
    offset = 6 + len(pngs) * 16
    entries = b""
    images = b""
    for dim, png in pngs:
        bw = 0 if dim >= 256 else dim     # 0 means 256 in the ICO directory
        bh = 0 if dim >= 256 else dim
        entries += struct.pack("<BBBBHHII", bw, bh, 0, 0, 1, 32, len(png), offset)
        images += png
        offset += len(png)
    blob = header + entries + images
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".ico.tmp")
        tmp.write_bytes(blob)
        os.replace(tmp, p)
        return True
    except Exception:
        return False


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
