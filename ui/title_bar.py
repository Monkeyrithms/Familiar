"""
Custom titlebar — Help / Settings / Tasks / Memory on left, centered title, min/max/close on right.
Supports window dragging.
"""

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QLabel, QGraphicsDropShadowEffect,
)
from PyQt6.QtCore import Qt, pyqtSignal, QVariantAnimation, QEasingCurve
from PyQt6.QtGui import QFont, QPainter, QColor, QPen
from ui.theme import PALETTE, dim_accent_edge

_TITLE_ICON_PX = 20


def _lerp_color(a: QColor, b: QColor, t: float) -> QColor:
    """Linear blend a→b at t∈[0,1]."""
    t = max(0.0, min(1.0, t))
    return QColor(
        round(a.red()   + (b.red()   - a.red())   * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue()  + (b.blue()  - a.blue())  * t),
    )


class TitleBar(QWidget):
    help_clicked = pyqtSignal()
    settings_clicked = pyqtSignal()
    tasks_clicked = pyqtSignal()
    memory_clicked = pyqtSignal()
    screenshot_clicked = pyqtSignal()
    always_on_top_clicked = pyqtSignal(bool)
    refresh_clicked = pyqtSignal()
    minimize_clicked = pyqtSignal()
    maximize_clicked = pyqtSignal()
    close_clicked = pyqtSignal()

    @staticmethod
    def _wordmark_font() -> QFont:
        """Calligraphic wordmark for the 'familiar' feel. Gabriola ships with
        Windows; fall back through other script/serif faces if it's missing."""
        f = QFont()
        f.setFamilies(["Gabriola", "Segoe Script", "Palatino Linotype",
                       "Georgia", "serif"])
        f.setPointSize(17)
        f.setBold(True)
        f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 103)
        return f

    def __init__(self, title="Familiar", parent=None):
        super().__init__(parent)
        self._title = title
        self._dragging = False
        self._drag_pos = None
        self.setFixedHeight(32)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 0, 4, 0)
        layout.setSpacing(4)

        # Help button (left, before Settings)
        self.help_btn = QPushButton("?")
        self.help_btn.setObjectName("titleBtn")
        self.help_btn.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        self.help_btn.setFixedSize(24, 24)
        self.help_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.help_btn.setToolTip("Help & setup guide")
        self.help_btn.clicked.connect(self.help_clicked.emit)
        layout.addWidget(self.help_btn)

        # Settings button (left)
        self.settings_btn = QPushButton("Settings")
        self.settings_btn.setObjectName("titleBtn")
        self.settings_btn.setFont(QFont("Consolas", 8))
        self.settings_btn.setFixedHeight(24)
        self.settings_btn.setToolTip("API keys, model, workspaces, tools, and UI options")
        self.settings_btn.clicked.connect(self.settings_clicked.emit)
        layout.addWidget(self.settings_btn)

        # Tasks button
        self.tasks_btn = QPushButton("Tasks")
        self.tasks_btn.setObjectName("titleBtn")
        self.tasks_btn.setFont(QFont("Consolas", 8))
        self.tasks_btn.setFixedHeight(24)
        self.tasks_btn.setToolTip("Scheduled prompts — cron-style reminders and automations")
        self.tasks_btn.clicked.connect(self.tasks_clicked.emit)
        layout.addWidget(self.tasks_btn)

        # Memory button
        self.memory_btn = QPushButton("Memory")
        self.memory_btn.setObjectName("titleBtn")
        self.memory_btn.setFont(QFont("Consolas", 8))
        self.memory_btn.setFixedHeight(24)
        self.memory_btn.setToolTip("Memory streams, notes, and rolling summaries")
        self.memory_btn.clicked.connect(self.memory_clicked.emit)
        layout.addWidget(self.memory_btn)

        layout.addStretch()

        # Title + sparkle icon (center)
        self.title_label = QLabel(title)
        self.title_label.setFont(self._wordmark_font())
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setWordWrap(False)
        # Enchanted glow around the wordmark (color tracks the theme — set in
        # apply_theme). Offset 0 makes it a halo/bloom rather than a drop shadow.
        self._title_glow = QGraphicsDropShadowEffect(self)
        self._title_glow.setBlurRadius(18)
        self._title_glow.setOffset(0, 0)
        self.title_label.setGraphicsEffect(self._title_glow)

        self.title_icon_label = QLabel()
        self.title_icon_label.setFixedSize(_TITLE_ICON_PX, _TITLE_ICON_PX)
        self.title_icon_label.setScaledContents(True)
        self.title_icon_label.setStyleSheet("background: transparent; border: none;")

        self._title_center = QWidget()
        title_row = QHBoxLayout(self._title_center)
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(6)
        title_row.addWidget(self.title_label)
        title_row.addWidget(self.title_icon_label)
        self._title_center.setStyleSheet("background: transparent; border: none;")
        layout.addWidget(self._title_center)

        layout.addStretch()

        # Always-on-top toggle (left of screenshot)
        self.always_on_top_btn = QPushButton("\u2191")  # ↑
        self.always_on_top_btn.setObjectName("alwaysOnTopBtn")
        self.always_on_top_btn.setFont(QFont("Consolas", 10))
        self.always_on_top_btn.setFixedSize(24, 24)
        self.always_on_top_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.always_on_top_btn.setToolTip("Always on top")
        self.always_on_top_btn.setCheckable(True)
        self.always_on_top_btn.clicked.connect(
            lambda checked: self.always_on_top_clicked.emit(checked)
        )
        layout.addWidget(self.always_on_top_btn)

        # Refresh button (between always-on-top and screenshot)
        self.refresh_btn = QPushButton("\u27f3")  # ↻
        self.refresh_btn.setObjectName("titleBtn")
        self.refresh_btn.setFont(QFont("Consolas", 10))
        self.refresh_btn.setFixedSize(24, 24)
        self.refresh_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.refresh_btn.setToolTip("Refresh UI — reload code changes + re-apply theme")
        self.refresh_btn.clicked.connect(self.refresh_clicked.emit)
        layout.addWidget(self.refresh_btn)

        # Screenshot button (right, before window controls)
        self.screenshot_btn = QPushButton("\u25a3")           # ▣
        self.screenshot_btn.setObjectName("titleBtn")
        self.screenshot_btn.setFont(QFont("Consolas", 12))
        self.screenshot_btn.setFixedSize(24, 24)
        self.screenshot_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.screenshot_btn.setToolTip("Screenshot to clipboard")
        self.screenshot_btn.clicked.connect(self.screenshot_clicked.emit)
        layout.addWidget(self.screenshot_btn)

        # Window buttons (right)
        for text, signal, obj_name in [
            ("\u2014", self.minimize_clicked, "titleBtn"),    # —
            ("\u25a1", self.maximize_clicked, "titleBtn"),    # □
            ("\u00d7", self.close_clicked, "closeBtn"),       # ×
        ]:
            btn = QPushButton(text)
            btn.setObjectName(obj_name)
            btn.setFont(QFont("Consolas", 12))
            btn.setFixedSize(24, 24)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(signal.emit)
            layout.addWidget(btn)

        # Wordmark "lit" level: 1.0 = fully glowing (normal), 0.0 = dark/unlit
        # (the bulb is off). The opening ceremony starts unlit and ignites a
        # couple seconds in. apply_theme() re-applies at the current level so a
        # theme change mid-ceremony doesn't snap the bulb on.
        self._lit_level = 1.0
        self._ignite_anim = None

        self.apply_theme()

    # ── Wordmark "lightbulb" state ─────────────────────────────────────
    def _unlit_color(self) -> QColor:
        """The wordmark's 'off' color — glow_hot dragged most of the way to the
        background so it reads as a dark, unpowered filament."""
        p = PALETTE
        hot = QColor(p.get("glow_hot") or p["accent_bright"])
        return _lerp_color(hot, QColor(p["background"]), 0.88)

    # Turn-on flash shaping (fractions of the ignite, 0..1).
    _FLASH_PEAK = 0.22   # when the white-hot flash peaks
    _FLASH_AMT = 0.92    # how close to white the flash gets (0=hot, 1=white)

    def _apply_lit_level(self, t: float) -> None:
        """Paint the title text, glow, and sparkle icon at lit level t∈[0,1].
        t=0 → dark/unlit; t=1 → steady glow_hot. In between, the colour blooms
        PAST glow_hot toward white at _FLASH_PEAK then decays back — a bright
        flash as the bulb switches on, drawing the eye, before it settles."""
        from ui.app_icon import build_app_icon
        t = max(0.0, min(1.0, t))
        self._lit_level = t
        p = PALETTE
        hot = QColor(p.get("glow_hot") or p["accent_bright"])
        white = QColor(255, 255, 255)

        # base level snaps up to the "on" colour by the flash peak; the flash is
        # a triangular pulse (0 → _FLASH_AMT at the peak → 0 at full on).
        peak = self._FLASH_PEAK
        ramp = min(1.0, t / peak) if peak > 0 else 1.0
        if t <= peak:
            flash = self._FLASH_AMT * (t / peak if peak > 0 else 1.0)
        else:
            flash = self._FLASH_AMT * (1.0 - (t - peak) / (1.0 - peak))
        flash_n = (flash / self._FLASH_AMT) if self._FLASH_AMT else 0.0

        base = _lerp_color(self._unlit_color(), hot, ramp)
        col = _lerp_color(base, white, flash)

        # Title text colour: dark → hot, flaring white-hot at the peak.
        self.title_label.setStyleSheet(
            f"color: {col.name()}; background: transparent; border: none;")
        # Glow blooms hard at the flash (blur + full white-hot alpha), settling
        # to the steady blur of 18 at full power.
        self._title_glow.setBlurRadius(18.0 * ramp + 28.0 * flash_n)
        gcol = QColor(col)
        gcol.setAlpha(int(255 * ramp))
        self._title_glow.setColor(gcol)

        # Sparkle icon tracks the same ramp + flash (same colour, body and core).
        icon = build_app_icon(col.name(), col.name())
        self.title_icon_label.setPixmap(
            icon.pixmap(_TITLE_ICON_PX, _TITLE_ICON_PX))

    def set_unlit(self) -> None:
        """Snap the wordmark + icon dark (bulb off). Call before the ignite."""
        if self._ignite_anim is not None:
            self._ignite_anim.stop()
            self._ignite_anim = None
        self._apply_lit_level(0.0)

    def ignite(self, *, duration: int = 700) -> None:
        """Animate the wordmark + icon from dark to full glow — like a bulb
        flicking on — and play the lightbulb chime as it lights."""
        try:
            from core.sounds import play_ui
            play_ui("light.mp3")
        except Exception:
            pass
        if self._ignite_anim is not None:
            self._ignite_anim.stop()
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(duration)
        # Linear so the flash shaping in _apply_lit_level controls the timing
        # (an eased curve would compress the flash peak to near-instant).
        anim.setEasingCurve(QEasingCurve.Type.Linear)
        anim.valueChanged.connect(lambda v: self._apply_lit_level(float(v)))
        anim.finished.connect(lambda: self._apply_lit_level(1.0))
        self._ignite_anim = anim
        anim.start()

    def apply_theme(self):
        p = PALETTE
        bg = QColor(p["background"])
        is_light = bg.lightness() > 140
        hover_bg = p["accent_bright"] if is_light else p["accent_soft"]
        hover_text = p["background"] if is_light else p["accent"]

        # Re-apply title/glow/icon at the current lit level (1.0 normally; <1
        # only during the opening ceremony).
        self._apply_lit_level(self._lit_level)
        self.setStyleSheet(f"""
            QPushButton#titleBtn {{
                background: transparent;
                border: 1px solid {p['border']};
                color: {p['text']};
                padding: 2px 8px;
            }}
            QPushButton#titleBtn:hover {{
                background: {hover_bg};
                color: {hover_text};
                border-color: {p['accent']};
            }}
            QPushButton#alwaysOnTopBtn {{
                background: transparent;
                border: 1px solid {p['border']};
                color: {p['text']};
                padding: 2px 8px;
            }}
            QPushButton#alwaysOnTopBtn:hover {{
                background: {hover_bg};
                color: {hover_text};
                border-color: {p['accent']};
            }}
            QPushButton#alwaysOnTopBtn:checked {{
                background: transparent;
                color: {p['accent_bright']};
                border-color: {p['accent_bright']};
            }}
            QPushButton#closeBtn {{
                background: transparent;
                border: 1px solid {p['border']};
                color: {p['text']};
            }}
            QPushButton#closeBtn:hover {{
                background: {hover_bg};
                color: {hover_text};
                border-color: {p['accent']};
            }}
        """)

    def paintEvent(self, event):
        painter = QPainter(self)
        p = PALETTE
        bg = QColor(p["panel_alt"])
        painter.fillRect(self.rect(), bg)
        # Faint accent-tinted frame — dimmed variant of the chosen UI color.
        edge = QColor(dim_accent_edge())
        painter.setPen(QPen(edge, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRect(0, 0, self.width() - 1, self.height() - 1)

    # ── Dragging ─────────────────────────────────────────────────────

    def _is_over_button(self, pos) -> bool:
        for child in self.findChildren(QPushButton):
            if child.geometry().contains(pos):
                return True
        return False

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._is_over_button(event.pos()):
                self._dragging = True
                self._drag_pos = event.globalPosition().toPoint()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and self._drag_pos is not None:
            win = self.window()
            gpos = event.globalPosition().toPoint()
            if win.isMaximized():
                # Dragging a MAXIMIZED window: calling move() on it is clamped
                # back to full screen by Windows, leaving the window stuck
                # maximized. Restore it to normal size positioned under the
                # cursor first (standard Windows behaviour), then keep dragging.
                cur_w = max(win.width(), 1)
                ratio = (gpos.x() - win.x()) / cur_w  # cursor's fractional x
                restore_w = win.normalGeometry().width() or 1200
                win.showNormal()
                if hasattr(win, "_maximized"):
                    win._maximized = False  # keep the host's bookkeeping in sync
                new_x = int(gpos.x() - ratio * restore_w)
                new_y = gpos.y() - self.height() // 2
                win.move(max(0, new_x), max(0, new_y))
                self._drag_pos = gpos
                event.accept()
                return
            delta = gpos - self._drag_pos
            win.move(win.pos() + delta)
            self._drag_pos = gpos
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if not self._is_over_button(event.pos()):
            self.maximize_clicked.emit()