"""
Tasks dialog — Zenithrix-inspired flow layout with pipeline validity highlighting.
3-panel: Tasks | Conditions | Actions
Bright fill = valid/ready, border-only = incomplete.
"""

import time as _time

from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QLineEdit,
    QTextEdit, QComboBox, QListWidget, QListWidgetItem, QFormLayout,
    QWidget, QCheckBox, QSpinBox, QDateTimeEdit, QStackedWidget,
    QProgressBar, QSizePolicy, QFrame,
)
from PyQt6.QtCore import Qt, QDateTime, QTimer
from PyQt6.QtGui import QFont, QColor

from PyQt6.QtWidgets import QStyledItemDelegate, QStyle

from ui.glass_dialog import GlassDialog
from ui.theme import PALETTE
from tools.tasks import load_tasks, save_tasks, create_task, remove_task, update_task
from core.conversations import list_conversations


VALID_ROLE = Qt.ItemDataRole.UserRole + 100  # per-item validity flag
COUNTDOWN_ROLE = Qt.ItemDataRole.UserRole + 101  # 0.0-1.0 countdown remaining


class _ValidityDelegate(QStyledItemDelegate):
    """Paints background fill + selection border directly via QPainter.
    Checks VALID_ROLE on each item individually."""

    def __init__(self, accent_color: str, parent=None):
        super().__init__(parent)
        p = PALETTE
        c = QColor(accent_color)
        self._valid_bg = QColor(c.red(), c.green(), c.blue(), 55)
        self._valid_sel_bg = QColor(c.red(), c.green(), c.blue(), 90)
        self._sel_border = QColor(accent_color)
        self._text_color = QColor(p["text"])
        self._glow_color = QColor(p["glow_hot"])
        self._border_color = QColor(p["border"])
        self._muted_color = QColor(p["muted_text"])
        self._bar_bg = QColor(p["panel_alt"])
        self._bar_color = QColor(c.red(), c.green(), c.blue(), 120)

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        # If item has countdown data, add room for the bar
        if index.data(COUNTDOWN_ROLE) is not None:
            sz.setHeight(max(sz.height(), 28))
        return sz

    def paint(self, painter, option, index):
        painter.save()
        r = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        valid = bool(index.data(VALID_ROLE))
        countdown = index.data(COUNTDOWN_ROLE)  # 0.0-1.0 or None

        # 1) Background fill
        if valid:
            painter.fillRect(r, self._valid_sel_bg if selected else self._valid_bg)
        elif selected:
            painter.fillRect(r, QColor(255, 255, 255, 15))

        # 2) Border
        if selected:
            pen = painter.pen()
            pen.setColor(self._sel_border)
            pen.setWidth(2)
            painter.setPen(pen)
            painter.drawRect(r.adjusted(1, 1, -1, -1))
        else:
            pen = painter.pen()
            pen.setColor(self._border_color)
            pen.setWidth(1)
            painter.setPen(pen)
            painter.drawRect(r.adjusted(0, 0, -1, -1))

        # 3) Text
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        text_color = self._glow_color if selected else (self._text_color if valid else self._muted_color)
        painter.setPen(text_color)
        painter.setFont(option.font)
        bar_h = 3 if countdown is not None else 0
        text_rect = r.adjusted(6, 0, -4, -bar_h)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

        # 4) Countdown bar (bottom of item)
        if countdown is not None and countdown > 0:
            bar_y = r.bottom() - bar_h
            bar_w = int((r.width() - 4) * countdown)
            painter.fillRect(r.x() + 2, bar_y, r.width() - 4, bar_h, self._bar_bg)
            painter.fillRect(r.x() + 2, bar_y, bar_w, bar_h, self._bar_color)

        painter.restore()


def _rgba(hex_color, alpha_pct):
    c = QColor(hex_color)
    return f"rgba({c.red()},{c.green()},{c.blue()},{alpha_pct}%)"


class TasksDialog(GlassDialog):
    def __init__(self, chat_window, parent=None):
        super().__init__(title="Tasks", parent=parent, width=820, height=600)
        self._chat = chat_window
        self._current_task_id = None
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.timeout.connect(self._save_task)
        self._loading = False  # suppress auto-save during selection load
        self._cond_edit_row: int | None = None
        self._action_edit_row: int | None = None
        self._build_ui()
        self._refresh_task_list()

    def _build_ui(self):
        layout = self.content_layout()
        p = PALETTE
        mono9 = QFont("Consolas", 9)
        mono8 = QFont("Consolas", 8)

        self._border_css = f"border: 1px solid {p['accent_muted']}; border-radius: 3px;"
        self._subtle_btn_css = f"background: transparent; border: 1px solid {p['border']}; padding: 3px 8px;"

        # Delegates paint backgrounds directly, bypassing all CSS conflicts
        self._task_delegate = _ValidityDelegate(p["accent"])
        self._cond_delegate = _ValidityDelegate(p["accent"])
        self._action_delegate = _ValidityDelegate(p["accent"])
        self._stream_delegate = _ValidityDelegate(p["accent"])
        self._conv_delegate = _ValidityDelegate(p["accent"])

        # ── 3-panel horizontal split ──
        panels = QHBoxLayout()
        panels.setSpacing(6)

        # ═══ Panel 1: Tasks list ═══
        tasks_panel = QVBoxLayout()
        tasks_panel.setSpacing(4)
        tasks_panel.addWidget(self._lbl("Tasks", mono9, p))

        self._task_list = QListWidget()
        self._task_list.setFont(mono9)
        self._task_list.setItemDelegate(self._task_delegate)
        self._task_list.currentRowChanged.connect(self._on_task_select)
        tasks_panel.addWidget(self._task_list, stretch=1)

        # Task name + enabled (auto-save on change)
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText("Task name")
        self._name_edit.setFont(mono9)
        self._name_edit.textChanged.connect(self._schedule_autosave)
        tasks_panel.addWidget(self._name_edit)

        self._enabled_check = QCheckBox("Enabled")
        self._enabled_check.setChecked(True)
        self._enabled_check.setFont(mono8)
        self._enabled_check.stateChanged.connect(self._on_enabled_changed)
        tasks_panel.addWidget(self._enabled_check)

        self._status_label = QLabel("")
        self._status_label.setFont(mono8)
        self._status_label.setStyleSheet(f"color:{p['accent_muted']};border:none;")

        # Task buttons
        tbtn_row = QHBoxLayout()
        tbtn_row.setSpacing(4)
        for label, slot in [("Delete -", "_delete_task"),
                            ("Add +", "_add_task")]:
            btn = QPushButton(label)
            btn.setFont(mono8)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(getattr(self, slot))
            tbtn_row.addWidget(btn)
        tasks_panel.addLayout(tbtn_row)

        panels.addLayout(tasks_panel, 1)  # thin

        # ═══ Panel 2: Conditions (schedule) ═══
        cond_panel = QVBoxLayout()
        cond_panel.setSpacing(4)
        cond_panel.addWidget(self._lbl("Conditions", mono9, p))

        # Conditions list (top 1/3)
        self._cond_list = QListWidget()
        self._cond_list.setFont(mono9)
        self._cond_list.setItemDelegate(self._cond_delegate)
        self._cond_list.setMaximumHeight(160)
        self._cond_list.itemDoubleClicked.connect(self._on_cond_item_double_clicked)
        cond_panel.addWidget(self._cond_list)

        rm_cond = QPushButton("Remove Selected")
        rm_cond.setFont(mono8)
        rm_cond.setStyleSheet(self._subtle_btn_css)
        rm_cond.setCursor(Qt.CursorShape.PointingHandCursor)
        rm_cond.clicked.connect(self._remove_condition)
        cond_panel.addWidget(rm_cond)

        # Bordered "Add" area
        cond_add_frame = QFrame()
        cond_add_frame.setStyleSheet(self._border_css)
        cond_add_layout = QVBoxLayout(cond_add_frame)
        cond_add_layout.setContentsMargins(6, 6, 6, 6)
        cond_add_layout.setSpacing(4)

        self._cond_add_btn = QPushButton("Add \u2191")
        self._cond_add_btn.setFont(mono8)
        self._cond_add_btn.setStyleSheet(self._subtle_btn_css)
        self._cond_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cond_add_btn.clicked.connect(self._add_condition)
        cond_add_layout.addWidget(self._cond_add_btn)

        cond_edit_row = QHBoxLayout()
        cond_edit_row.setSpacing(6)
        cond_edit_row.addStretch(1)
        self._cond_cancel_btn = QPushButton("Cancel")
        self._cond_cancel_btn.setFont(mono8)
        self._cond_cancel_btn.setStyleSheet(self._subtle_btn_css)
        self._cond_cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cond_cancel_btn.hide()
        self._cond_cancel_btn.clicked.connect(self._cancel_cond_edit)
        cond_edit_row.addWidget(self._cond_cancel_btn)
        self._cond_update_btn = QPushButton("Update")
        self._cond_update_btn.setFont(mono8)
        self._cond_update_btn.setStyleSheet(self._subtle_btn_css)
        self._cond_update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cond_update_btn.hide()
        self._cond_update_btn.clicked.connect(self._commit_cond_update)
        cond_edit_row.addWidget(self._cond_update_btn)
        cond_edit_row.addStretch(1)
        cond_add_layout.addLayout(cond_edit_row)

        self._cond_type = QComboBox()
        self._cond_type.setFont(mono9)
        self._cond_type.addItem("One-time delay", "delay")
        self._cond_type.addItem("Recurring interval", "interval")
        self._cond_type.addItem("Specific date/time", "datetime")
        self._cond_type.addItem("Cron expression", "cron")
        self._cond_type.addItem("On startup", "startup")
        self._cond_type.currentIndexChanged.connect(self._on_cond_type_changed)
        cond_add_layout.addWidget(self._cond_type)

        self._cond_stack = QStackedWidget()

        # Delay
        dw = QWidget()
        dlay = QHBoxLayout(dw)
        dlay.setContentsMargins(0, 0, 0, 0)
        dlay.setSpacing(4)
        self._delay_spin = QSpinBox()
        self._delay_spin.setRange(1, 9999)
        self._delay_spin.setValue(30)
        self._delay_spin.setFont(mono9)
        dlay.addWidget(self._delay_spin)
        self._delay_unit = QComboBox()
        self._delay_unit.setFont(mono9)
        self._delay_unit.addItem("min", "m")
        self._delay_unit.addItem("hr", "h")
        self._delay_unit.addItem("day", "d")
        dlay.addWidget(self._delay_unit)
        dlay.addStretch()
        self._cond_stack.addWidget(dw)

        # Interval
        iw = QWidget()
        ilay = QHBoxLayout(iw)
        ilay.setContentsMargins(0, 0, 0, 0)
        ilay.setSpacing(4)
        ilay.addWidget(QLabel("Every"))
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, 9999)
        self._interval_spin.setValue(2)
        self._interval_spin.setFont(mono9)
        ilay.addWidget(self._interval_spin)
        self._interval_unit = QComboBox()
        self._interval_unit.setFont(mono9)
        self._interval_unit.addItem("min", "m")
        self._interval_unit.addItem("hr", "h")
        self._interval_unit.addItem("day", "d")
        self._interval_unit.setCurrentIndex(1)
        ilay.addWidget(self._interval_unit)
        ilay.addStretch()
        self._cond_stack.addWidget(iw)

        # Datetime
        dtw = QWidget()
        dtlay = QHBoxLayout(dtw)
        dtlay.setContentsMargins(0, 0, 0, 0)
        self._datetime_edit = QDateTimeEdit()
        self._datetime_edit.setFont(mono9)
        self._datetime_edit.setCalendarPopup(True)
        self._datetime_edit.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self._datetime_edit.setDisplayFormat("yyyy-MM-dd hh:mm AP")
        dtlay.addWidget(self._datetime_edit)
        dtlay.addStretch()
        self._cond_stack.addWidget(dtw)

        # Cron
        cronw = QWidget()
        cronlay = QHBoxLayout(cronw)
        cronlay.setContentsMargins(0, 0, 0, 0)
        self._cron_edit = QLineEdit()
        self._cron_edit.setFont(mono9)
        self._cron_edit.setPlaceholderText("0 9 * * *")
        cronlay.addWidget(self._cron_edit)
        self._cond_stack.addWidget(cronw)

        # Startup (no parameters)
        suw = QWidget()
        sulay = QVBoxLayout(suw)
        sulay.setContentsMargins(0, 0, 0, 0)
        su_lbl = QLabel("Runs once each time Familiar starts.")
        su_lbl.setWordWrap(True)
        su_lbl.setFont(mono8)
        su_lbl.setStyleSheet(f"color:{p['muted_text']};border:none;")
        sulay.addWidget(su_lbl)
        sulay.addStretch()
        self._cond_stack.addWidget(suw)

        cond_add_layout.addWidget(self._cond_stack)
        cond_add_layout.addStretch()
        cond_panel.addWidget(cond_add_frame, stretch=1)

        panels.addLayout(cond_panel, 2)

        # ═══ Panel 3: Actions ═══
        act_panel = QVBoxLayout()
        act_panel.setSpacing(4)
        act_panel.addWidget(self._lbl("Actions", mono9, p))

        # Actions list (top 1/3)
        self._action_list = QListWidget()
        self._action_list.setFont(mono9)
        self._action_list.setItemDelegate(self._action_delegate)
        self._action_list.setMaximumHeight(160)
        self._action_list.itemDoubleClicked.connect(self._on_action_item_double_clicked)
        act_panel.addWidget(self._action_list)

        rm_act = QPushButton("Remove Selected")
        rm_act.setFont(mono8)
        rm_act.setStyleSheet(self._subtle_btn_css)
        rm_act.setCursor(Qt.CursorShape.PointingHandCursor)
        rm_act.clicked.connect(self._remove_action)
        act_panel.addWidget(rm_act)

        # Bordered "Add" area
        act_add_frame = QFrame()
        act_add_frame.setStyleSheet(self._border_css)
        act_add_layout = QVBoxLayout(act_add_frame)
        act_add_layout.setContentsMargins(6, 6, 6, 6)
        act_add_layout.setSpacing(4)

        self._action_add_btn = QPushButton("Add \u2191")
        self._action_add_btn.setFont(mono8)
        self._action_add_btn.setStyleSheet(self._subtle_btn_css)
        self._action_add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action_add_btn.clicked.connect(self._add_action)
        act_add_layout.addWidget(self._action_add_btn)

        act_edit_row = QHBoxLayout()
        act_edit_row.setSpacing(6)
        act_edit_row.addStretch(1)
        self._action_cancel_btn = QPushButton("Cancel")
        self._action_cancel_btn.setFont(mono8)
        self._action_cancel_btn.setStyleSheet(self._subtle_btn_css)
        self._action_cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action_cancel_btn.hide()
        self._action_cancel_btn.clicked.connect(self._cancel_action_edit)
        act_edit_row.addWidget(self._action_cancel_btn)
        self._action_update_btn = QPushButton("Update")
        self._action_update_btn.setFont(mono8)
        self._action_update_btn.setStyleSheet(self._subtle_btn_css)
        self._action_update_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action_update_btn.hide()
        self._action_update_btn.clicked.connect(self._commit_action_update)
        act_edit_row.addWidget(self._action_update_btn)
        act_edit_row.addStretch(1)
        act_add_layout.addLayout(act_edit_row)

        self._action_type = QComboBox()
        self._action_type.setFont(mono9)
        self._action_type.addItem("Prompt (LLM call)", "prompt")
        self._action_type.addItem("Visual Alert", "visual")
        self._action_type.addItem("Audio Alert (TTS)", "audio")
        self._action_type.addItem("Play Sound", "sound")
        self._action_type.addItem("Execute (.py/.exe)", "execute")
        self._action_type.currentIndexChanged.connect(self._on_action_type_changed)
        act_add_layout.addWidget(self._action_type)

        # Stacked: text content (prompt/visual/audio) vs sound picker
        self._action_content_stack = QStackedWidget()

        self._action_content = QTextEdit()
        self._action_content.setPlaceholderText("Prompt or alert message")
        self._action_content.setFont(mono9)
        self._action_content_stack.addWidget(self._action_content)  # index 0

        self._sound_picker = QComboBox()
        self._sound_picker.setFont(mono9)
        from core.sounds import list_sounds
        for s in list_sounds():
            self._sound_picker.addItem(s, s)
        self._action_content_stack.addWidget(self._sound_picker)  # index 1

        act_add_layout.addWidget(self._action_content_stack, stretch=1)

        # Request Response checkbox (only for visual alerts)
        self._request_response_check = QCheckBox("Request Response (blink input)")
        self._request_response_check.setFont(mono8)
        self._request_response_check.hide()
        act_add_layout.addWidget(self._request_response_check)

        act_add_layout.addStretch()
        act_panel.addWidget(act_add_frame, stretch=1)

        panels.addLayout(act_panel, 2)

        # ═══ Panel 4: Targets (Streams + Conversations) ═══
        target_panel = QVBoxLayout()
        target_panel.setSpacing(4)

        # Memory Streams section
        target_panel.addWidget(self._lbl("Memory Streams", mono9, p))
        self._stream_list = QListWidget()
        self._stream_list.setFont(mono9)
        self._stream_list.setItemDelegate(self._stream_delegate)
        self._stream_list.setMaximumHeight(120)
        target_panel.addWidget(self._stream_list)

        stream_btn_row = QHBoxLayout()
        stream_btn_row.setSpacing(4)
        self._stream_combo = QComboBox()
        self._stream_combo.setFont(mono8)
        stream_btn_row.addWidget(self._stream_combo, stretch=1)
        _btn_css = "padding: 2px 4px;"
        add_stream_btn = QPushButton("+")
        add_stream_btn.setFont(mono9)
        add_stream_btn.setFixedSize(22, 22)
        add_stream_btn.setStyleSheet(_btn_css)
        add_stream_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_stream_btn.clicked.connect(self._add_stream_target)
        stream_btn_row.addWidget(add_stream_btn)
        rm_stream_btn = QPushButton("-")
        rm_stream_btn.setFont(mono9)
        rm_stream_btn.setFixedSize(22, 22)
        rm_stream_btn.setStyleSheet(_btn_css)
        rm_stream_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rm_stream_btn.clicked.connect(self._remove_stream_target)
        stream_btn_row.addWidget(rm_stream_btn)
        target_panel.addLayout(stream_btn_row)

        # Conversations section
        target_panel.addWidget(self._lbl("Conversations", mono9, p))
        self._conv_list = QListWidget()
        self._conv_list.setFont(mono9)
        self._conv_list.setItemDelegate(self._conv_delegate)
        target_panel.addWidget(self._conv_list, stretch=1)

        conv_btn_row = QHBoxLayout()
        conv_btn_row.setSpacing(4)
        self._conv_combo = QComboBox()
        self._conv_combo.setFont(mono8)
        conv_btn_row.addWidget(self._conv_combo, stretch=1)
        add_conv_btn = QPushButton("+")
        add_conv_btn.setFont(mono9)
        add_conv_btn.setFixedSize(22, 22)
        add_conv_btn.setStyleSheet(_btn_css)
        add_conv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_conv_btn.clicked.connect(self._add_conv_target)
        conv_btn_row.addWidget(add_conv_btn)
        rm_conv_btn = QPushButton("-")
        rm_conv_btn.setFont(mono9)
        rm_conv_btn.setFixedSize(22, 22)
        rm_conv_btn.setStyleSheet(_btn_css)
        rm_conv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rm_conv_btn.clicked.connect(self._remove_conv_target)
        conv_btn_row.addWidget(rm_conv_btn)
        target_panel.addLayout(conv_btn_row)

        panels.addLayout(target_panel, 1)  # slightly thin

        layout.addLayout(panels, stretch=1)

        # Bottom bar: status left, close right
        bottom = QHBoxLayout()
        bottom.addWidget(self._status_label)
        bottom.addStretch()
        close_btn = QPushButton("Close")
        close_btn.setFont(mono8)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        # Auto-refresh countdown bars
        self._bar_timer = QTimer(self)
        self._bar_timer.timeout.connect(self._update_countdown)
        self._bar_timer.start(500)

    # ── Helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _lbl(text, font, p):
        l = QLabel(text)
        l.setFont(font)
        l.setStyleSheet(f"color:{p['glow_hot']};border:none;font-weight:bold;")
        return l

    def _schedule_autosave(self, *_):
        """Debounced auto-save — 500ms after last change."""
        if self._loading or not self._current_task_id:
            return
        self._autosave_timer.start(500)

    def _on_enabled_changed(self, *_):
        """Enabled toggled — update pipeline immediately + auto-save."""
        self._update_pipeline()
        self._schedule_autosave()


    # ── Condition config ────────────────────────────────────────────

    def _on_cond_type_changed(self, idx):
        self._cond_stack.setCurrentIndex(idx)

    def _reset_cond_widgets_default(self):
        """Default widget state for adding a new condition."""
        self._cond_type.blockSignals(True)
        self._cond_type.setCurrentIndex(0)
        self._cond_type.blockSignals(False)
        self._cond_stack.setCurrentIndex(0)
        self._delay_spin.setValue(30)
        self._delay_unit.setCurrentIndex(0)
        self._interval_spin.setValue(2)
        self._interval_unit.setCurrentIndex(1)
        self._datetime_edit.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        self._cron_edit.clear()

    def _apply_cond_dict_to_widgets(self, c: dict):
        kind = c.get("kind", "delay")
        kind_to_idx = {"delay": 0, "interval": 1, "datetime": 2, "cron": 3, "startup": 4}
        idx = kind_to_idx.get(kind, 0)
        self._cond_type.blockSignals(True)
        self._cond_type.setCurrentIndex(idx)
        self._cond_type.blockSignals(False)
        self._cond_stack.setCurrentIndex(idx)

        if kind == "delay":
            self._delay_spin.setValue(int(c.get("value", 30)))
            unit = c.get("unit", "m")
            for i in range(self._delay_unit.count()):
                if self._delay_unit.itemData(i) == unit:
                    self._delay_unit.setCurrentIndex(i)
                    break
        elif kind == "interval":
            self._interval_spin.setValue(int(c.get("value", 2)))
            unit = c.get("unit", "h")
            for i in range(self._interval_unit.count()):
                if self._interval_unit.itemData(i) == unit:
                    self._interval_unit.setCurrentIndex(i)
                    break
        elif kind == "datetime":
            dt_str = (c.get("datetime") or "").strip()
            parsed = QDateTime.fromString(dt_str, "yyyy-MM-dd HH:mm")
            if parsed.isValid():
                self._datetime_edit.setDateTime(parsed)
            else:
                self._datetime_edit.setDateTime(QDateTime.currentDateTime().addSecs(3600))
        elif kind == "cron":
            self._cron_edit.setText(c.get("expr", ""))

    def _set_cond_edit_mode_ui(self, editing: bool):
        self._cond_add_btn.setVisible(not editing)
        self._cond_cancel_btn.setVisible(editing)
        self._cond_update_btn.setVisible(editing)

    def _exit_cond_edit_mode(self, *, reset_widgets: bool):
        self._cond_edit_row = None
        self._set_cond_edit_mode_ui(False)
        if reset_widgets:
            self._reset_cond_widgets_default()

    def _on_cond_item_double_clicked(self, item: QListWidgetItem):
        if not item or not self._current_task_id:
            return
        row = self._cond_list.row(item)
        if row < 0:
            return
        c = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(c, dict):
            return
        self._cond_edit_row = row
        self._apply_cond_dict_to_widgets(c)
        self._set_cond_edit_mode_ui(True)

    def _cancel_cond_edit(self):
        self._exit_cond_edit_mode(reset_widgets=True)

    def _commit_cond_update(self):
        if self._cond_edit_row is None:
            return
        row = self._cond_edit_row
        if row < 0 or row >= self._cond_list.count():
            self._exit_cond_edit_mode(reset_widgets=True)
            return
        c = self._get_cond_dict()
        if not c.get("display"):
            return
        item = self._cond_list.item(row)
        if not item:
            self._exit_cond_edit_mode(reset_widgets=True)
            return
        item.setText(self._cond_label(c))
        item.setData(Qt.ItemDataRole.UserRole, c)
        self._exit_cond_edit_mode(reset_widgets=True)
        self._update_pipeline()
        self._schedule_autosave()

    def _get_cond_dict(self) -> dict:
        """Build a condition dict from the current widgets."""
        ctype = self._cond_type.currentData()
        if ctype == "delay":
            val = self._delay_spin.value()
            unit = self._delay_unit.currentData()
            return {"kind": "delay", "value": val, "unit": unit,
                    "display": f"{val}{unit}"}
        elif ctype == "interval":
            val = self._interval_spin.value()
            unit = self._interval_unit.currentData()
            return {"kind": "interval", "value": val, "unit": unit,
                    "display": f"every {val}{unit}"}
        elif ctype == "datetime":
            dt = self._datetime_edit.dateTime().toPyDateTime()
            return {"kind": "datetime", "datetime": dt.strftime("%Y-%m-%d %H:%M"),
                    "display": dt.strftime("%Y-%m-%d %H:%M")}
        elif ctype == "cron":
            expr = self._cron_edit.text().strip()
            return {"kind": "cron", "expr": expr, "display": expr}
        elif ctype == "startup":
            return {"kind": "startup", "display": "on startup"}
        return {}

    def _cond_label(self, c: dict) -> str:
        kind = c.get("kind", "?")
        icons = {"delay": "\u23f1", "interval": "\u21bb", "datetime": "\U0001f4c5",
                 "cron": "\u2699", "startup": "\U0001f680"}
        return f'{icons.get(kind, "?")} {c.get("display", "?")}'

    def _add_condition(self):
        c = self._get_cond_dict()
        if not c.get("display"):
            return
        item = QListWidgetItem(self._cond_label(c))
        item.setData(Qt.ItemDataRole.UserRole, c)
        self._cond_list.addItem(item)
        self._reset_cond_widgets_default()
        self._update_pipeline()
        self._schedule_autosave()

    def _remove_condition(self):
        row = self._cond_list.currentRow()
        if row < 0:
            return
        if self._cond_edit_row is not None:
            if row == self._cond_edit_row:
                self._exit_cond_edit_mode(reset_widgets=True)
            elif row < self._cond_edit_row:
                self._cond_edit_row -= 1
        self._cond_list.takeItem(row)
        self._update_pipeline()
        self._schedule_autosave()

    # ── Action config ───────────────────────────────────────────────

    def _on_action_type_changed(self, idx):
        atype = self._action_type.currentData()
        if atype == "sound":
            self._action_content_stack.setCurrentIndex(1)
            self._action_content_stack.show()
        elif atype == "visual":
            self._action_content_stack.hide()
        else:
            self._action_content_stack.setCurrentIndex(0)
            self._action_content_stack.show()
            if atype == "prompt":
                self._action_content.setPlaceholderText("Prompt for LLM")
            elif atype == "execute":
                self._action_content.setPlaceholderText(
                    r'Path to a .py/.exe, e.g.  C:\bots\run.py --flag')
            else:
                self._action_content.setPlaceholderText("Alert message text")
        self._request_response_check.setVisible(atype == "visual")

    def _get_action_dict(self) -> dict:
        atype = self._action_type.currentData()
        if atype == "sound":
            content = self._sound_picker.currentData() or ""
        else:
            content = self._action_content.toPlainText().strip()
        labels = {"prompt": "LLM", "visual": "VIS", "audio": "TTS",
                  "sound": "\U0001f50a", "execute": "RUN"}
        display = f'[{labels.get(atype, "?")}] {content[:40]}' if content else f'[{labels.get(atype, "?")}] Visual Alert'
        d = {"type": atype, "content": content, "display": display}
        if atype == "visual" and self._request_response_check.isChecked():
            d["request_response"] = True
            d["display"] += " (req resp)"
        return d

    def _action_label(self, a: dict) -> str:
        return a.get("display", "?")

    def _add_action(self):
        a = self._get_action_dict()
        atype = a.get("type", "")
        # Visual alerts need no content; sounds need a picker selection; others need text
        if atype == "visual":
            pass  # always OK
        elif atype == "sound":
            if not self._sound_picker.currentData():
                return
        elif not a.get("content"):
            return
        item = QListWidgetItem(self._action_label(a))
        item.setData(Qt.ItemDataRole.UserRole, a)
        self._action_list.addItem(item)
        self._reset_action_widgets_default()
        self._update_pipeline()
        self._schedule_autosave()

    def _remove_action(self):
        row = self._action_list.currentRow()
        if row < 0:
            return
        if self._action_edit_row is not None:
            if row == self._action_edit_row:
                self._exit_action_edit_mode(reset_widgets=True)
            elif row < self._action_edit_row:
                self._action_edit_row -= 1
        self._action_list.takeItem(row)
        self._update_pipeline()
        self._schedule_autosave()

    def _reset_action_widgets_default(self):
        self._action_type.blockSignals(True)
        self._action_type.setCurrentIndex(0)
        self._action_type.blockSignals(False)
        self._action_content_stack.setCurrentIndex(0)
        self._action_content_stack.show()
        self._action_content.clear()
        self._action_content.setPlaceholderText("Prompt for LLM")
        if self._sound_picker.count():
            self._sound_picker.setCurrentIndex(0)
        self._request_response_check.setChecked(False)
        self._request_response_check.hide()
        self._on_action_type_changed(0)

    def _apply_action_dict_to_widgets(self, a: dict):
        atype = a.get("type", "prompt")
        idx = 0
        for i in range(self._action_type.count()):
            if self._action_type.itemData(i) == atype:
                idx = i
                break
        self._action_type.blockSignals(True)
        self._action_type.setCurrentIndex(idx)
        self._action_type.blockSignals(False)
        self._on_action_type_changed(idx)

        if atype == "sound":
            sound = a.get("content", "")
            found = False
            for i in range(self._sound_picker.count()):
                if self._sound_picker.itemData(i) == sound:
                    self._sound_picker.setCurrentIndex(i)
                    found = True
                    break
            if not found and sound:
                self._sound_picker.insertItem(0, sound, sound)
                self._sound_picker.setCurrentIndex(0)
        else:
            self._action_content.setPlainText(a.get("content", ""))

        self._request_response_check.setChecked(bool(a.get("request_response")))

    def _set_action_edit_mode_ui(self, editing: bool):
        self._action_add_btn.setVisible(not editing)
        self._action_cancel_btn.setVisible(editing)
        self._action_update_btn.setVisible(editing)

    def _exit_action_edit_mode(self, *, reset_widgets: bool):
        self._action_edit_row = None
        self._set_action_edit_mode_ui(False)
        if reset_widgets:
            self._reset_action_widgets_default()

    def _on_action_item_double_clicked(self, item: QListWidgetItem):
        if not item or not self._current_task_id:
            return
        row = self._action_list.row(item)
        if row < 0:
            return
        a = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(a, dict):
            return
        self._action_edit_row = row
        self._apply_action_dict_to_widgets(a)
        self._set_action_edit_mode_ui(True)

    def _cancel_action_edit(self):
        self._exit_action_edit_mode(reset_widgets=True)

    def _commit_action_update(self):
        if self._action_edit_row is None:
            return
        row = self._action_edit_row
        if row < 0 or row >= self._action_list.count():
            self._exit_action_edit_mode(reset_widgets=True)
            return
        a = self._get_action_dict()
        atype = a.get("type", "")
        if atype == "visual":
            pass
        elif atype == "sound":
            if not self._sound_picker.currentData():
                return
        elif not a.get("content"):
            return
        item = self._action_list.item(row)
        if not item:
            self._exit_action_edit_mode(reset_widgets=True)
            return
        item.setText(self._action_label(a))
        item.setData(Qt.ItemDataRole.UserRole, a)
        self._exit_action_edit_mode(reset_widgets=True)
        self._update_pipeline()
        self._schedule_autosave()

    # ── Pipeline validity ───────────────────────────────────────────

    def _update_pipeline(self):
        """Set VALID_ROLE on each item individually and repaint."""
        task_enabled = self._enabled_check.isChecked()
        has_conds = self._cond_list.count() > 0
        has_actions = self._action_list.count() > 0
        has_targets = self._has_targets()
        # Only prompt actions deliver to a conversation/stream, so only they
        # require a target. Execute/sound/visual/audio tasks are self-contained.
        has_prompt_action = any(
            (self._action_list.item(i).data(Qt.ItemDataRole.UserRole) or {}).get("type") == "prompt"
            for i in range(self._action_list.count()))
        target_ok = has_targets or not has_prompt_action

        cond_valid = has_conds and task_enabled
        action_valid = has_conds and has_actions and task_enabled
        target_valid = has_conds and has_actions and target_ok and task_enabled
        task_valid = target_valid

        # Update the currently selected task's validity in the task list
        row = self._task_list.currentRow()
        if row >= 0:
            item = self._task_list.item(row)
            if item:
                item.setData(VALID_ROLE, task_valid)

        for i in range(self._cond_list.count()):
            self._cond_list.item(i).setData(VALID_ROLE, cond_valid)

        for i in range(self._action_list.count()):
            self._action_list.item(i).setData(VALID_ROLE, action_valid)

        for i in range(self._stream_list.count()):
            self._stream_list.item(i).setData(VALID_ROLE, target_valid)

        for i in range(self._conv_list.count()):
            self._conv_list.item(i).setData(VALID_ROLE, target_valid)

        self._task_list.viewport().repaint()
        self._cond_list.viewport().repaint()
        self._action_list.viewport().repaint()
        self._stream_list.viewport().repaint()
        self._conv_list.viewport().repaint()

    # ── Targets (Streams + Conversations) ─────────────────────────

    def _refresh_target_combos(self):
        """Populate the add-target combo boxes."""
        import json
        from pathlib import Path

        self._stream_combo.clear()
        cfg_path = Path(__file__).parent.parent / "config.json"
        try:
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for s in cfg.get("memory_streams", []):
                self._stream_combo.addItem(s["name"], s["name"])
        except Exception:
            pass

        self._conv_combo.clear()
        for conv in list_conversations():
            name = conv.get("name", conv["id"][:12])
            self._conv_combo.addItem(name, conv["id"])

    def _add_stream_target(self):
        name = self._stream_combo.currentData()
        if not name:
            return
        # Don't add duplicates
        for i in range(self._stream_list.count()):
            if self._stream_list.item(i).data(Qt.ItemDataRole.UserRole) == name:
                return
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, name)
        item.setData(VALID_ROLE, True)
        self._stream_list.addItem(item)
        self._update_pipeline()
        self._schedule_autosave()

    def _remove_stream_target(self):
        row = self._stream_list.currentRow()
        if row >= 0:
            self._stream_list.takeItem(row)
            self._update_pipeline()
            self._schedule_autosave()

    def _add_conv_target(self):
        conv_id = self._conv_combo.currentData()
        if not conv_id:
            return
        for i in range(self._conv_list.count()):
            if self._conv_list.item(i).data(Qt.ItemDataRole.UserRole) == conv_id:
                return
        name = self._conv_combo.currentText()
        item = QListWidgetItem(name)
        item.setData(Qt.ItemDataRole.UserRole, conv_id)
        item.setData(VALID_ROLE, True)
        self._conv_list.addItem(item)
        self._update_pipeline()
        self._schedule_autosave()

    def _remove_conv_target(self):
        row = self._conv_list.currentRow()
        if row >= 0:
            self._conv_list.takeItem(row)
            self._update_pipeline()
            self._schedule_autosave()

    def _collect_targets(self) -> dict:
        """Collect stream and conversation targets."""
        streams = []
        for i in range(self._stream_list.count()):
            streams.append(self._stream_list.item(i).data(Qt.ItemDataRole.UserRole))
        convs = []
        for i in range(self._conv_list.count()):
            convs.append(self._conv_list.item(i).data(Qt.ItemDataRole.UserRole))
        return {"streams": streams, "conversations": convs}

    def _has_targets(self) -> bool:
        return self._stream_list.count() > 0 or self._conv_list.count() > 0

    # ── Task list ───────────────────────────────────────────────────

    def _refresh_task_list(self):
        self._refresh_target_combos()
        self._task_list.clear()
        tasks = load_tasks()
        for t in tasks:
            enabled = t.get("enabled", False)
            has_conds = bool(t.get("conditions") or t.get("schedule"))
            has_actions = bool(t.get("actions") or t.get("prompt"))
            targets = t.get("targets", {})
            has_targets = bool(targets.get("streams") or targets.get("conversations")
                               or t.get("conversation_id") or t.get("deliver_to_stream"))
            has_prompt_action = any((a or {}).get("type") == "prompt" for a in t.get("actions", [])) \
                or (bool(t.get("prompt")) and not t.get("actions"))
            valid = enabled and has_conds and has_actions and (has_targets or not has_prompt_action)

            status = "\u25cf" if enabled else "\u25cb"  # ● / ○
            item = QListWidgetItem(f"{status} {t['name']}")
            item.setData(Qt.ItemDataRole.UserRole, t["id"])
            item.setData(VALID_ROLE, valid)
            self._task_list.addItem(item)
        if tasks:
            self._task_list.setCurrentRow(0)
        self._status_label.setText(f"{len(tasks)} task(s)")
        self._update_pipeline()

    def _on_task_select(self, row):
        if row < 0:
            self._current_task_id = None
            self._exit_cond_edit_mode(reset_widgets=True)
            self._exit_action_edit_mode(reset_widgets=True)
            return
        item = self._task_list.item(row)
        if not item:
            return
        self._exit_cond_edit_mode(reset_widgets=True)
        self._exit_action_edit_mode(reset_widgets=True)
        self._loading = True
        task_id = item.data(Qt.ItemDataRole.UserRole)
        self._current_task_id = task_id
        tasks = load_tasks()
        task = next((t for t in tasks if t["id"] == task_id), None)
        if not task:
            return

        self._name_edit.setText(task.get("name", ""))
        self._enabled_check.setChecked(task.get("enabled", True))

        # Load targets
        self._stream_list.clear()
        self._conv_list.clear()
        targets = task.get("targets", {})
        for s in targets.get("streams", []):
            item = QListWidgetItem(f"\u2b24 {s}")
            item.setData(Qt.ItemDataRole.UserRole, s)
            self._stream_list.addItem(item)
        for cid in targets.get("conversations", []):
            # Resolve name
            cname = cid[:12]
            for conv in list_conversations():
                if conv["id"] == cid:
                    cname = conv.get("name", cid[:12])
                    break
            item = QListWidgetItem(cname)
            item.setData(Qt.ItemDataRole.UserRole, cid)
            self._conv_list.addItem(item)

        # Backward compat: old deliver_to_type/deliver_to_stream/conversation_id
        if not targets:
            if task.get("deliver_to_stream"):
                s = task["deliver_to_stream"]
                item = QListWidgetItem(f"\u2b24 {s}")
                item.setData(Qt.ItemDataRole.UserRole, s)
                self._stream_list.addItem(item)
            elif task.get("conversation_id"):
                cid = task["conversation_id"]
                cname = cid[:12]
                for conv in list_conversations():
                    if conv["id"] == cid:
                        cname = conv.get("name", cid[:12])
                        break
                item = QListWidgetItem(cname)
                item.setData(Qt.ItemDataRole.UserRole, cid)
                self._conv_list.addItem(item)

        # Load conditions
        self._cond_list.clear()
        for c in task.get("conditions", []):
            item = QListWidgetItem(self._cond_label(c))
            item.setData(Qt.ItemDataRole.UserRole, c)
            self._cond_list.addItem(item)

        # Backward compat: old tasks have schedule + action_type instead of conditions/actions
        if not task.get("conditions") and task.get("schedule"):
            sched = task["schedule"]
            c = {"kind": sched.get("kind", "delay"),
                 "display": sched.get("display", "?")}
            if "minutes" in sched:
                c["value"] = int(sched["minutes"])
                c["unit"] = "m"
            if "expr" in sched:
                c["expr"] = sched["expr"]
            if "run_at" in sched:
                c["datetime"] = sched["run_at"]
            item = QListWidgetItem(self._cond_label(c))
            item.setData(Qt.ItemDataRole.UserRole, c)
            self._cond_list.addItem(item)

        # Load actions
        self._action_list.clear()
        for a in task.get("actions", []):
            item = QListWidgetItem(self._action_label(a))
            item.setData(Qt.ItemDataRole.UserRole, a)
            self._action_list.addItem(item)

        # Backward compat: old task with single action_type + prompt
        if not task.get("actions") and task.get("prompt"):
            atype = task.get("action_type", "prompt")
            labels = {"prompt": "LLM", "visual": "VIS", "audio": "TTS", "sound": "SND", "execute": "RUN"}
            a = {"type": atype, "content": task["prompt"],
                 "display": f'[{labels.get(atype, "?")}] {task["prompt"][:40]}'}
            item = QListWidgetItem(self._action_label(a))
            item.setData(Qt.ItemDataRole.UserRole, a)
            self._action_list.addItem(item)

        # Status
        last_run = task.get("last_run_at")
        last_status = task.get("last_status", "")
        if last_run:
            import time
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(last_run))
            self._status_label.setText(f"Last: {ts} — {last_status or '?'}")
        else:
            self._status_label.setText("Never run")

        self._update_pipeline()
        self._loading = False

    # ── CRUD ────────────────────────────────────────────────────────

    def _collect_conditions(self) -> list[dict]:
        return [self._cond_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self._cond_list.count())]

    def _collect_actions(self) -> list[dict]:
        return [self._action_list.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(self._action_list.count())]

    def _build_schedule_from_conditions(self, conditions: list[dict]) -> str:
        """Convert the first condition to a schedule string for the backend."""
        if not conditions:
            return ""
        c = conditions[0]
        kind = c.get("kind", "")
        if kind == "delay":
            return f'{c.get("value", 30)}{c.get("unit", "m")}'
        elif kind == "interval":
            return f'every {c.get("value", 2)}{c.get("unit", "h")}'
        elif kind == "datetime":
            return c.get("datetime", "")
        elif kind == "cron":
            return c.get("expr", "")
        elif kind == "startup":
            return "startup"
        return c.get("display", "")

    def _add_task(self):
        conditions = self._collect_conditions()
        actions = self._collect_actions()
        targets = self._collect_targets()
        if not conditions:
            self._status_label.setText("Add at least one condition")
            return
        if not actions:
            self._status_label.setText("Add at least one action")
            return

        name = self._name_edit.text().strip() or actions[0].get("content", "Task")[:40]
        schedule_str = self._build_schedule_from_conditions(conditions)
        prompt = next((a["content"] for a in actions if a["type"] == "prompt"), "")
        if not prompt:
            prompt = next((a["content"] for a in actions), "")

        # Use first conversation target for backend compat
        conv_id = targets["conversations"][0] if targets["conversations"] else ""

        result = create_task(prompt, schedule_str, name, "", conv_id,
                             enabled=False, conditions=conditions, actions=actions)
        if isinstance(result, dict) and "error" in result:
            self._status_label.setText(f"Error: {result['error']}")
            return

        # Store targets
        if isinstance(result, dict) and "id" in result:
            all_tasks = load_tasks()
            for t in all_tasks:
                if t["id"] == result["id"]:
                    t["targets"] = targets
                    break
            save_tasks(all_tasks)

        self._status_label.setText(f"Created: {name}")
        self._refresh_task_list()
        self._task_list.setCurrentRow(self._task_list.count() - 1)

    def _save_task(self):
        if not self._current_task_id:
            self._status_label.setText("Select a task first")
            return

        conditions = self._collect_conditions()
        actions = self._collect_actions()
        targets = self._collect_targets()
        schedule_str = self._build_schedule_from_conditions(conditions)
        prompt = next((a["content"] for a in actions if a["type"] == "prompt"), "")
        if not prompt:
            prompt = next((a["content"] for a in actions), "")

        conv_id = targets["conversations"][0] if targets["conversations"] else ""

        update_task(
            self._current_task_id,
            name=self._name_edit.text().strip(),
            prompt=prompt,
            schedule=schedule_str,
            conversation_id=conv_id,
            enabled=self._enabled_check.isChecked(),
        )

        all_tasks = load_tasks()
        for t in all_tasks:
            if t["id"] == self._current_task_id:
                t["conditions"] = conditions
                t["actions"] = actions
                t["targets"] = targets
                break
        save_tasks(all_tasks)

        # Update the task list item text in-place (no full refresh)
        row = self._task_list.currentRow()
        if row >= 0:
            item = self._task_list.item(row)
            if item:
                enabled = self._enabled_check.isChecked()
                name = self._name_edit.text().strip() or "Task"
                status = "\u25cf" if enabled else "\u25cb"
                item.setText(f"{status} {name}")
                has_conds = len(conditions) > 0
                has_actions = len(actions) > 0
                has_targets = self._has_targets()
                has_prompt_action = any(a.get("type") == "prompt" for a in actions)
                item.setData(VALID_ROLE, enabled and has_conds and has_actions
                             and (has_targets or not has_prompt_action))
        self._task_list.viewport().update()

    def _delete_task(self):
        if not self._current_task_id:
            return
        name = self._name_edit.text().strip() or "this task"
        if not GlassDialog.confirm(self, "Delete Task", f"Delete '{name}'?"):
            return
        remove_task(self._current_task_id)
        self._current_task_id = None
        self._status_label.setText(f"Deleted: {name}")
        self._refresh_task_list()

    # ── Countdown ───────────────────────────────────────────────────

    def _update_countdown(self):
        """Set COUNTDOWN_ROLE on each target list item and repaint."""
        if not self._current_task_id:
            return
        tasks = load_tasks()
        t = next((t for t in tasks if t["id"] == self._current_task_id), None)
        if not t or not t.get("enabled"):
            # Clear countdowns
            for lst in [self._stream_list, self._conv_list]:
                for i in range(lst.count()):
                    lst.item(i).setData(COUNTDOWN_ROLE, None)
                lst.viewport().update()
            return

        now = _time.time()
        next_run = t.get("next_run_at", 0)
        remaining = max(0, next_run - now)
        sched = t.get("schedule", {})
        total = sched.get("minutes", 0) * 60
        if not total and next_run > 0:
            last_run = t.get("last_run_at") or t.get("created_at", 0)
            total = next_run - last_run if next_run > last_run else 0
        progress = min(1.0, remaining / total) if total > 0 else 0.0

        # Set countdown on all target items
        for lst in [self._stream_list, self._conv_list]:
            for i in range(lst.count()):
                lst.item(i).setData(COUNTDOWN_ROLE, progress)
            lst.viewport().update()
