"""
Settings dialog - API keys, model, system prompt, workspaces, tools.
"""

import os
import subprocess
import sys
from pathlib import Path
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit,
    QFormLayout, QScrollArea, QFrame, QFileDialog, QListWidget,
    QListWidgetItem, QMessageBox, QGroupBox, QSlider, QColorDialog,
    QSpinBox, QCheckBox, QSizePolicy, QSpacerItem, QCompleter,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QPlainTextEdit, QApplication, QMenu,
)
from PyQt6.QtCore import Qt, QStringListModel, QObject, pyqtSignal


class _CfDownloadWorker(QObject):
    """Runs the cloudflared download off the UI thread; signals are delivered
    back to the GUI thread by Qt (queued connections)."""
    progress = pyqtSignal(str)
    done = pyqtSignal(bool, str)

    def run(self):
        from core.network import download_cloudflared
        ok, msg = download_cloudflared(progress=self.progress.emit)
        self.done.emit(ok, msg)
from PyQt6.QtGui import QColor, QFont, QShortcut, QKeySequence
from ui.theme import PALETTE, OVERRIDE_KEYS, build_palette
from ui.glass_dialog import GlassDialog
from core.providers import load_keys, save_keys, PROVIDER_INFO, resolve_google_api_key
from core.agent import Agent, load_config, save_config
from core.network import network_manager
from core.workspace_paths import resolve_workspace_entry_path, to_config_workspace_path
from core.sounds import invalidate_ui_sounds_cache
from tools.workspace_sound_watch import invalidate_settings_cache
from core.model_history import merge_stored_provider_model_memory, touch_provider_model_choice


class _NumItem(QTableWidgetItem):
    """Right-aligned numeric cell that sorts numerically (not lexically)."""
    def __init__(self, value: int):
        super().__init__(f"{value:,}")
        self._val = int(value)
        self.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def __lt__(self, other):
        try:
            return self._val < other._val
        except AttributeError:
            return super().__lt__(other)


class _ToolNameItem(QTableWidgetItem):
    """Tool-name cell with the tool description as tooltip."""
    def __init__(self, name: str, description: str = ""):
        super().__init__(name)
        if description:
            short = description.replace("\n", " ").strip()
            if len(short) > 400:
                short = short[:400] + "..."
            self.setToolTip(short)


class SettingsDialog(GlassDialog):
    def __init__(self, agent: Agent, parent=None):
        super().__init__(
            title="Settings", parent=parent, width=680, height=720,
            geometry_key="settings",
        )
        # Non-modal: GlassDialog defaults to modal, but Settings is opened with
        # show() (not exec()) so the main window's title-bar controls
        # (always-on-top, screenshot) and chat stay usable while it's open.
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.agent = agent
        self._dirty = False
        self._build_ui()
        self._wire_dirty_tracking()
        # Authoritative change detection: snapshot every input's value now, and
        # on cancel compare against it. A bare _dirty flag was tripped by things
        # that aren't edits — switching tabs, or browsing the notes/workspace/
        # stream sub-editors (which populate line-edits via signals) — so Cancel
        # nagged even when nothing was actually changed.
        self._initial_signature = self._settings_signature()

    def _build_ui(self):
        layout = self.content_layout()

        tabs = QTabWidget()
        tabs.setUsesScrollButtons(False)  # wrap tabs instead of scroll arrows
        tabs.addTab(self._build_ui_tab(), "UI")
        tabs.addTab(self._build_keys_tab(), "API Keys")
        tabs.addTab(self._build_model_tab(), "Model")
        tabs.addTab(self._build_workspaces_tab(), "Workspaces")
        tabs.addTab(self._build_prompt_tab(), "System Prompt")
        tabs.addTab(self._build_voice_tab(), "Audio")
        tabs.addTab(self._build_tools_tab(), "Tools")
        tabs.addTab(self._build_network_tab(), "Network")
        layout.addWidget(tabs)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save_and_close)
        btn_row.addWidget(save_btn)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self._cancel_clicked)
        btn_row.addWidget(cancel_btn)
        layout.addLayout(btn_row)

        self.tabs = tabs
        self._apply_default_styles()

    def _apply_default_styles(self) -> None:
        """Apply consistent dark-theme styling to all input widgets in Settings."""
        p = PALETTE
        
        # QSpinBox (Chat Font Size, Display Char Limit, CRT Speed, Tool Audit Threshold, etc.)
        spinbox_style = f"""
            QSpinBox {{
                background-color: {p['panel_alt']};
                color: {p['text']};
                border: 1px solid {p['border']};
                border-radius: 3px;
                padding: 2px 4px;
                font-size: 9pt;
            }}
            QSpinBox::up-button, QSpinBox::down-button {{
                background-color: {p['panel']};
                border: none;
                width: 16px;
                padding: 0;
            }}
            QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
                background-color: {p['accent_muted']};
            }}
        """
        
        # QComboBox (Chat Role Contrast, Stream Display, Chat Output Mode, Workspace Side, etc.)
        combo_style = f"""
            QComboBox {{
                background-color: {p['panel_alt']};
                color: {p['text']};
                border: 1px solid {p['border']};
                border-radius: 3px;
                padding: 3px 6px;
                font-size: 9pt;
            }}
            QComboBox::drop-down {{
                background-color: {p['panel']};
                border-left: 1px solid {p['border']};
                width: 20px;
            }}
            QComboBox:hover {{
                border-color: {p['accent_muted']};
            }}
            QComboBox QListView {{
                background-color: {p['panel']};
                color: {p['text']};
                border: 1px solid {p['border']};
                padding: 2px;
            }}
            QComboBox QListView::item {{
                padding: 3px 6px;
            }}
            QComboBox QListView::item:selected {{
                background-color: {p['accent_muted']};
            }}
        """
        
        # QLineEdit (Color hex, Model, Temperature, API keys, etc.)
        lineedit_style = f"""
            QLineEdit {{
                background-color: {p['panel_alt']};
                color: {p['text']};
                border: 1px solid {p['border']};
                border-radius: 3px;
                padding: 3px 6px;
                font-size: 9pt;
            }}
            QLineEdit:focus {{
                border: 1px solid {p['accent']};
            }}
        """
        
        # QTextEdit & QPlainTextEdit (System Prompt, Code snippets, etc.)
        textedit_style = f"""
            QTextEdit, QPlainTextEdit {{
                background-color: {p['panel_alt']};
                color: {p['text']};
                border: 1px solid {p['border']};
                border-radius: 3px;
                padding: 4px;
                font-size: 9pt;
            }}
            QTextEdit:focus, QPlainTextEdit:focus {{
                border: 1px solid {p['accent']};
            }}
        """
        
        # QCheckBox (Animate Ellipsis, Show Tools Called, Show Timestamps, etc.)
        checkbox_style = f"""
            QCheckBox {{
                color: {p['text']};
                spacing: 6px;
            }}
            QCheckBox::indicator {{
                width: 14px;
                height: 14px;
                border: 1px solid {p['border']};
                border-radius: 2px;
                background-color: {p['panel_alt']};
            }}
            QCheckBox::indicator:hover {{
                border-color: {p['accent']};
                background-color: {p['panel']};
            }}
            QCheckBox::indicator:checked {{
                background-color: {p['accent']};
                border-color: {p['accent']};
            }}
        """
        
        # QSlider (Brightness, TTS Speed, CRT Speed, etc.)
        slider_style = f"""
            QSlider::groove:horizontal {{
                background-color: {p['panel_alt']};
                height: 6px;
                border: 1px solid {p['border']};
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                background-color: {p['accent_muted']};
                width: 12px;
                margin: -3px 0;
                border: 1px solid {p['border']};
                border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{
                background-color: {p['accent']};
            }}
        """
        
        # Apply to all instances
        for widget in self.findChildren(QSpinBox):
            widget.setStyleSheet(spinbox_style)
        for widget in self.findChildren(QComboBox):
            widget.setStyleSheet(combo_style)
        for widget in self.findChildren(QLineEdit):
            widget.setStyleSheet(lineedit_style)
        for widget in self.findChildren(QTextEdit):
            widget.setStyleSheet(textedit_style)
        for widget in self.findChildren(QPlainTextEdit):
            widget.setStyleSheet(textedit_style)
        for widget in self.findChildren(QCheckBox):
            widget.setStyleSheet(checkbox_style)
        for widget in self.findChildren(QSlider):
            widget.setStyleSheet(slider_style)
    def _mark_dirty(self, *_args) -> None:
        # Count a change as a USER edit only when it happened in the widget
        # that currently has focus. Async/programmatic population (network
        # status fills, lazily-loaded combo items, sub-editors syncing
        # line-edits) fires the exact same signals but never on the focused
        # widget — those used to flip _dirty and trigger the discard prompt
        # on a dialog the user never touched.
        w = self.sender()
        fw = QApplication.focusWidget()
        if w is not None and fw is not None and (fw is w or w.isAncestorOf(fw)):
            self._dirty = True

    def _wire_dirty_tracking(self) -> None:
        # NOTE: kept as a coarse hint only. The discard prompt is gated on an
        # actual value comparison (_settings_signature), so a stray signal here
        # — or a tab switch — never falsely nags. Tab changes are deliberately
        # NOT tracked (navigation isn't an edit).
        for w in self.findChildren(QLineEdit):
            w.textChanged.connect(self._mark_dirty)
        for w in self.findChildren(QTextEdit):
            w.textChanged.connect(self._mark_dirty)
        for w in self.findChildren(QPlainTextEdit):
            w.textChanged.connect(self._mark_dirty)
        for w in self.findChildren(QComboBox):
            w.currentIndexChanged.connect(self._mark_dirty)
        for w in self.findChildren(QCheckBox):
            w.toggled.connect(self._mark_dirty)
        for w in self.findChildren(QSpinBox):
            w.valueChanged.connect(self._mark_dirty)
        for w in self.findChildren(QSlider):
            w.valueChanged.connect(self._mark_dirty)

    def _settings_signature(self) -> tuple:
        """An order-independent snapshot of every input's value. Two equal
        signatures mean nothing the user can edit has actually changed.

        Compared as a SORTED multiset because Qt's findChildren() returns
        widgets in an unstable order across calls (showing a tab relays its
        children), which made a positional snapshot report phantom changes. A
        sorted multiset is invariant to that reordering yet still flips the
        moment any single value differs. Each entry is (tag, value) as STRINGS
        so the mixed value types sort cleanly."""
        sig: list = []
        for w in self.findChildren(QLineEdit):
            sig.append(("le", w.text()))
        for w in self.findChildren(QTextEdit):
            sig.append(("te", w.toPlainText()))
        for w in self.findChildren(QPlainTextEdit):
            sig.append(("pte", w.toPlainText()))
        for w in self.findChildren(QComboBox):
            sig.append(("cb", str(w.currentIndex())))
        for w in self.findChildren(QCheckBox):
            sig.append(("ck", str(w.isChecked())))
        for w in self.findChildren(QSpinBox):
            sig.append(("sp", str(w.value())))
        for w in self.findChildren(QSlider):
            sig.append(("sl", str(w.value())))
        for w in self.findChildren(QListWidget):
            if w is getattr(self, "_net_share_list", None):
                continue  # folder listing (environment state), not a setting
            for i in range(w.count()):
                sig.append(("li", w.item(i).text()))
        return tuple(sorted(sig))

    def _cancel_clicked(self) -> None:
        self.reject()

    def _confirm_discard(self) -> bool:
        # Prompt only when BOTH hold: the user actually interacted with an
        # input (_dirty, focus-gated) AND some value genuinely differs from
        # when the dialog opened. Signature drift alone is not enough — async
        # population shifts it on dialogs the user never touched.
        if not self._dirty:
            return True
        if self._settings_signature() == getattr(self, "_initial_signature", None):
            return True
        return GlassDialog.confirm(
            self,
            "Discard changes?",
            "Settings have unsaved changes. Close without saving?",
        )

    def reject(self) -> None:
        if not self._confirm_discard():
            return
        self._persist_geometry()
        self._dirty = False
        super().reject()

    def closeEvent(self, event) -> None:
        if self._dirty and not self._confirm_discard():
            event.ignore()
            return
        self._dirty = False
        super().closeEvent(event)

    # ------------------------------------------------------------------
    # UI tab
    # ------------------------------------------------------------------

    def _build_ui_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        cfg = load_config()
        p = PALETTE

        # Color picker row: swatch button + hex field
        color_row = QHBoxLayout()
        color_row.setSpacing(6)

        self._color_swatch = QPushButton()
        self._color_swatch.setFixedSize(32, 32)
        self._color_swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color_swatch.clicked.connect(self._pick_color)
        color_row.addWidget(self._color_swatch)

        self._base_color_edit = QLineEdit(cfg.get("base_color", "#4ECDC4"))
        self._base_color_edit.setPlaceholderText("#4ECDC4")
        self._base_color_edit.setMaximumWidth(100)
        self._base_color_edit.textChanged.connect(self._update_swatch)
        color_row.addWidget(self._base_color_edit)
        color_row.addStretch()

        self._update_swatch()
        layout.addRow(QLabel("Base Color"), color_row)

        # Brightness slider row: slider + value label
        bright_row = QHBoxLayout()
        bright_row.setSpacing(8)

        self._brightness_slider = QSlider(Qt.Orientation.Horizontal)
        self._brightness_slider.setMinimum(0)
        self._brightness_slider.setMaximum(300)
        self._brightness_slider.setValue(int(cfg.get("brightness", 0.25) * 100))
        self._brightness_slider.setTickInterval(10)
        self._brightness_slider.setStyleSheet(f"""
            QSlider::groove:horizontal {{
                background: transparent;
                height: 6px;
                border: 1px solid {p['border']};
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                background: transparent;
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border: 2px solid {p['accent']};
                border-radius: 7px;
            }}
            QSlider::handle:horizontal:hover {{
                border-color: {p['accent_bright']};
            }}
            QSlider::sub-page:horizontal {{
                background: transparent;
                border: 1px solid {p['accent_muted']};
                border-radius: 3px;
            }}
        """)
        self._brightness_slider.valueChanged.connect(self._on_brightness_changed)
        bright_row.addWidget(self._brightness_slider, stretch=1)

        self._brightness_label = QLabel(f"{cfg.get('brightness', 0.25):.2f}")
        self._brightness_label.setFixedWidth(40)
        self._brightness_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bright_row.addWidget(self._brightness_label)

        layout.addRow(QLabel("Brightness"), bright_row)

        # Chat font size
        self._font_size_spin = QSpinBox()
        self._font_size_spin.setRange(7, 18)
        self._font_size_spin.setValue(cfg.get("chat_font_size", 10))
        self._font_size_spin.setSuffix(" pt")
        layout.addRow(QLabel("Chat Font Size"), self._font_size_spin)

        from ui.theme import CHAT_CONTRAST_AGENT_BRIGHT, CHAT_CONTRAST_USER_BRIGHT
        self._chat_role_contrast_combo = QComboBox()
        self._chat_role_contrast_combo.addItem(
            "User bright / Agent dim (default)", CHAT_CONTRAST_USER_BRIGHT)
        self._chat_role_contrast_combo.addItem(
            "Agent bright / User dim", CHAT_CONTRAST_AGENT_BRIGHT)
        contrast = cfg.get("chat_role_contrast", CHAT_CONTRAST_USER_BRIGHT)
        self._chat_role_contrast_combo.setCurrentIndex(
            0 if contrast != CHAT_CONTRAST_AGENT_BRIGHT else 1)
        self._chat_role_contrast_combo.setToolTip(
            "Alternating brightness between your messages and the agent's "
            "replies in the chat transcript.")
        layout.addRow(QLabel("Chat Role Contrast"), self._chat_role_contrast_combo)

        # Display char limit (controls how many messages render at once)
        self._char_limit_spin = QSpinBox()
        self._char_limit_spin.setRange(2000, 200000)
        self._char_limit_spin.setSingleStep(2000)
        self._char_limit_spin.setValue(cfg.get("display_char_limit", 15000))
        self._char_limit_spin.setSuffix(" chars")
        layout.addRow(QLabel("Display Char Limit"), self._char_limit_spin)

        # Animate ellipsis
        self._animate_ellipsis_check = QCheckBox("Animate Ellipsis")
        self._animate_ellipsis_check.setChecked(cfg.get("animate_ellipsis", True))
        layout.addRow("", self._animate_ellipsis_check)

        # Live stream display — in-chat bubbles vs preview panel
        self._stream_display_combo = QComboBox()
        self._stream_display_combo.addItem(
            "In chat (live assistant bubbles)", "chat")
        self._stream_display_combo.addItem(
            "Preview panel (final result in chat)", "preview")
        stream_mode = cfg.get("stream_display", "chat")
        self._stream_display_combo.setCurrentIndex(
            0 if stream_mode != "preview" else 1)
        self._stream_display_combo.setToolTip(
            "While the agent is working: show streamed narration as live chat "
            "bubbles, or in a muted preview panel below the transcript. "
            "Internal model reasoning is never shown in either mode.")
        layout.addRow(QLabel("Live Stream Display"), self._stream_display_combo)

        # Chat output mode (Fancy vs Plain)
        self._chat_mode_combo = QComboBox()
        self._chat_mode_combo.addItem("Fancy (layouts, animations)", "fancy")
        self._chat_mode_combo.addItem("Plain (text only)", "plain")
        current_mode = cfg.get("chat_mode", "fancy")
        self._chat_mode_combo.setCurrentIndex(0 if current_mode == "fancy" else 1)
        layout.addRow(QLabel("Chat Output Mode"), self._chat_mode_combo)

        # Which side the tools/workspace splitter docks on
        self._workspace_side_combo = QComboBox()
        self._workspace_side_combo.addItem("Right", "right")
        self._workspace_side_combo.addItem("Left", "left")
        ws_side = str(cfg.get("workspace_side", "right") or "right").lower()
        for i in range(self._workspace_side_combo.count()):
            if self._workspace_side_combo.itemData(i) == ws_side:
                self._workspace_side_combo.setCurrentIndex(i)
                break
        self._workspace_side_combo.setToolTip(
            "Dock the tools/workspace panel (file viewer, browser, terminal) on "
            "the left or right side of the chat.")
        layout.addRow(QLabel("Workspace Side"), self._workspace_side_combo)



        # Show tool-call summary chips above agent messages
        self._show_tools_called_check = QCheckBox("Show Tools Called")
        self._show_tools_called_check.setChecked(cfg.get("show_tools_called", True))
        layout.addRow("", self._show_tools_called_check)

        self._tool_display_combo = QComboBox()
        self._tool_display_combo.addItem("Chips (square borders)", "chips")
        self._tool_display_combo.addItem("Bubbles (rounded borders)", "bubbles")
        self._tool_display_combo.addItem("Comma-separated list", "comma")
        tool_mode = cfg.get("tool_display_mode", "chips")
        for i in range(self._tool_display_combo.count()):
            if self._tool_display_combo.itemData(i) == tool_mode:
                self._tool_display_combo.setCurrentIndex(i)
                break
        layout.addRow(QLabel("Tool call display"), self._tool_display_combo)

        self._show_tools_hint_check = QCheckBox('Show "Tools:" hint prefix')
        self._show_tools_hint_check.setChecked(cfg.get("show_tools_hint", False))
        self._show_tools_hint_check.setToolTip(
            "Prefix inline tool rows with “Tools:” before chips, bubbles, or names."
        )
        layout.addRow("", self._show_tools_hint_check)

        self._trailing_ellipsis_check = QCheckBox("Trailing ellipsis on running commentary")
        self._trailing_ellipsis_check.setChecked(cfg.get("trailing_ellipsis", False))
        self._trailing_ellipsis_check.setToolTip(
            "While the agent is working, turn commentary like “Now I'll check this.” "
            "into “Now I'll check this...” (uses Animate Ellipsis when enabled). "
            "Works even with Show Tools Called off — a minimal activity cue."
        )
        layout.addRow("", self._trailing_ellipsis_check)

        # Show timestamps
        self._show_timestamps_check = QCheckBox("Show Timestamps")
        self._show_timestamps_check.setChecked(cfg.get("show_timestamps", True))
        layout.addRow("", self._show_timestamps_check)

        # Show usage stats
        self._show_usage_check = QCheckBox("Show Usage Stats")
        self._show_usage_check.setChecked(cfg.get("show_usage", False))
        layout.addRow("", self._show_usage_check)

        # Cyberpunk styling (monocolor)
        cyber_group = QGroupBox("Make it Cyberpunk")
        cyber_layout = QVBoxLayout(cyber_group)
        cyber_layout.setSpacing(8)

        self._monocolor_check = QCheckBox("Monocolor")
        self._monocolor_check.setChecked(cfg.get("monocolor", True))
        self._monocolor_check.setToolTip(
            "Accent-tinted monochrome look. When enabled, you can also tint "
            "the workspace browser to match."
        )
        cyber_layout.addWidget(self._monocolor_check)

        mono_browser_row = QWidget()
        mono_browser_layout = QHBoxLayout(mono_browser_row)
        mono_browser_layout.setContentsMargins(24, 0, 0, 0)
        mono_browser_layout.setSpacing(8)
        self._monocolor_browser_check = QCheckBox("Monocolor Browser")
        if "monocolor_browser" in cfg:
            mono_browser_default = bool(cfg.get("monocolor_browser", True))
        else:
            mono_browser_default = bool(cfg.get("browser_tint", True))
        self._monocolor_browser_check.setChecked(mono_browser_default)
        self._monocolor_browser_check.setToolTip(
            "Apply the accent-tinted monochrome filter to pages in the "
            "workspace browser (requires Monocolor)."
        )
        mono_browser_layout.addWidget(self._monocolor_browser_check)
        mono_browser_layout.addStretch()
        cyber_layout.addWidget(mono_browser_row)

        mono_images_row = QWidget()
        mono_images_layout = QHBoxLayout(mono_images_row)
        mono_images_layout.setContentsMargins(24, 0, 0, 0)
        mono_images_layout.setSpacing(8)
        self._monocolor_images_check = QCheckBox("Images")
        self._monocolor_images_check.setChecked(bool(cfg.get("monocolor_images", False)))
        self._monocolor_images_check.setToolTip(
            "Apply the accent-tinted monochrome filter to images shown in chat "
            "(attached, pasted, or produced). Requires Monocolor."
        )
        mono_images_layout.addWidget(self._monocolor_images_check)
        mono_images_layout.addStretch()
        cyber_layout.addWidget(mono_images_row)

        self._monocolor_check.toggled.connect(self._sync_monocolor_browser_visibility)
        self._sync_monocolor_browser_visibility()

        layout.addRow(cyber_group)

        # ── Colors — per-element overrides, shown only when Monocolor is OFF ──
        # When Monocolor is on the whole UI is one accent-tinted scheme. Turn it
        # off and you can paint individual elements; anything left blank still
        # derives from the base color, so partial overrides blend with the theme.
        self._colors_group = QGroupBox("Colors")
        colors_layout = QFormLayout(self._colors_group)
        colors_layout.setSpacing(8)
        colors_layout.setContentsMargins(10, 10, 10, 10)

        hint = QLabel("Override individual UI colors. Leave a field blank to keep "
                      "deriving it from the base color. Takes effect on Save.")
        hint.setWordWrap(True)
        hint.setStyleSheet(f"color: {p['muted_text']};")
        colors_layout.addRow(hint)

        saved_overrides = (cfg.get("color_overrides") or {}) if not cfg.get("monocolor", True) else {}
        if not isinstance(saved_overrides, dict):
            saved_overrides = {}
        # Show current effective palette values as the starting point so the
        # pickers begin from what's actually on screen.
        eff = build_palette(cfg.get("base_color", "#4ECDC4"),
                            cfg.get("brightness", 0.25), saved_overrides)

        self._color_override_edits: dict = {}
        for key, label in OVERRIDE_KEYS:
            initial = saved_overrides.get(key, eff.get(key, ""))
            row = self._make_color_override_row(key, initial)
            colors_layout.addRow(QLabel(label), row)

        layout.addRow(self._colors_group)
        self._monocolor_check.toggled.connect(self._sync_colors_section_visibility)
        self._sync_colors_section_visibility()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(w)
        return scroll

    def _pick_color(self):
        current = QColor(self._base_color_edit.text().strip() or "#4ECDC4")
        color = QColorDialog.getColor(current, self, "Choose Base Color",
                                       QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if color.isValid():
            self._base_color_edit.setText(color.name())

    def _update_swatch(self):
        text = self._base_color_edit.text().strip()
        c = QColor(text)
        if c.isValid():
            self._color_swatch.setStyleSheet(
                f"QPushButton {{ background: {text}; border: 2px solid {PALETTE['accent_bright']}; }}"
                f"QPushButton:hover {{ border-color: #ffffff; }}"
            )
        else:
            self._color_swatch.setStyleSheet(
                f"QPushButton {{ background: #333; border: 2px solid {PALETTE['border']}; }}"
            )

    def _on_brightness_changed(self, val):
        self._brightness_label.setText(f"{val / 100:.2f}")

    def _sync_monocolor_browser_visibility(self):
        visible = self._monocolor_check.isChecked()
        for chk in (self._monocolor_browser_check, self._monocolor_images_check):
            row = chk.parentWidget()
            if row is not None:
                row.setVisible(visible)

    def _sync_colors_section_visibility(self):
        # The Colors overrides only matter when the monochrome look is OFF.
        self._colors_group.setVisible(not self._monocolor_check.isChecked())

    def _make_color_override_row(self, key: str, initial: str) -> QWidget:
        """A swatch button + hex field for one overridable palette key. The edit
        is registered in self._color_override_edits[key]."""
        row = QWidget()
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(6)

        swatch = QPushButton()
        swatch.setFixedSize(28, 28)
        swatch.setCursor(Qt.CursorShape.PointingHandCursor)
        h.addWidget(swatch)

        edit = QLineEdit(initial or "")
        edit.setPlaceholderText("(derive from base)")
        edit.setMaximumWidth(120)
        h.addWidget(edit)
        h.addStretch()

        def _update():
            self._update_override_swatch(swatch, edit.text().strip())
        edit.textChanged.connect(_update)
        swatch.clicked.connect(lambda: self._pick_override_color(edit))
        _update()

        self._color_override_edits[key] = edit
        return row

    def _update_override_swatch(self, swatch: QPushButton, text: str):
        c = QColor(text)
        if text and c.isValid():
            swatch.setStyleSheet(
                f"QPushButton {{ background: {text}; border: 2px solid {PALETTE['accent_bright']}; }}"
                f"QPushButton:hover {{ border-color: #ffffff; }}")
        else:
            # Blank/invalid → checker-ish neutral, signalling "derive from base".
            swatch.setStyleSheet(
                f"QPushButton {{ background: {PALETTE['panel_alt']}; "
                f"border: 2px dashed {PALETTE['border']}; }}")

    def _pick_override_color(self, edit: QLineEdit):
        current = QColor(edit.text().strip() or PALETTE.get("accent", "#4ECDC4"))
        color = QColorDialog.getColor(current, self, "Choose Color",
                                       QColorDialog.ColorDialogOption.DontUseNativeDialog)
        if color.isValid():
            edit.setText(color.name())

    # ------------------------------------------------------------------
    # API Keys tab
    # ------------------------------------------------------------------

    def _build_keys_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        form = QFormLayout(container)
        form.setSpacing(10)
        form.setContentsMargins(10, 10, 10, 10)

        self._key_fields = {}
        self._auth_mode_combos: dict[str, QComboBox] = {}
        keys = load_keys()
        cfg_keys = load_config()

        for pid, info in PROVIDER_INFO.items():
            if pid == "local":
                continue
            entry = keys.get(pid, {})
            if pid == "google":
                initial = resolve_google_api_key(entry.get("api_key", "")) or (
                    cfg_keys.get("google_api_key") or ""
                )
            else:
                initial = entry.get("api_key", "")
            field = QLineEdit(initial)
            field.setPlaceholderText(f"Enter {info['name']} API key...")
            field.setEchoMode(QLineEdit.EchoMode.Password)
            self._key_fields[pid] = field
            form.addRow(QLabel(info["name"]), field)

            if pid == "anthropic":
                auth = QComboBox()
                auth.addItem("API key", "api_key")
                auth.addItem("Claude Code (OAuth)", "claude_code_oauth")
                amode = (entry.get("auth_mode") or "api_key").strip().lower()
                idx = 1 if amode in ("claude_code_oauth", "anthropic_oauth", "oauth") else 0
                auth.setCurrentIndex(idx)
                self._auth_mode_combos[pid] = auth
                form.addRow(QLabel("  └ Auth"), auth)
            elif pid == "openai":
                auth = QComboBox()
                auth.addItem("API key", "api_key")
                auth.addItem("Codex / ChatGPT (OAuth file)", "openai_codex_oauth")
                amode = (entry.get("auth_mode") or "api_key").strip().lower()
                idx = 1 if amode in ("openai_codex_oauth", "codex_oauth", "oauth") else 0
                auth.setCurrentIndex(idx)
                self._auth_mode_combos[pid] = auth
                form.addRow(QLabel("  └ Auth"), auth)

        # Tool keys
        sep = QLabel("--- Tool API Keys ---")
        sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow(sep)

        tavily_entry = keys.get("tavily", {})
        tavily_field = QLineEdit(tavily_entry.get("api_key", ""))
        tavily_field.setPlaceholderText("Enter Tavily API key (web search)...")
        tavily_field.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_fields["tavily"] = tavily_field
        form.addRow(QLabel("Tavily"), tavily_field)

        scroll.setWidget(container)
        return scroll

    # ------------------------------------------------------------------
    # Model tab
    # ------------------------------------------------------------------

    def _build_model_tab(self) -> QWidget:
        w = QWidget()
        layout = QFormLayout(w)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        self._provider_combo = QComboBox()
        for pid, info in PROVIDER_INFO.items():
            self._provider_combo.addItem(info["name"], pid)
        current_idx = list(PROVIDER_INFO.keys()).index(self.agent.provider) \
            if self.agent.provider in PROVIDER_INFO else 0
        self._provider_combo.blockSignals(True)
        self._provider_combo.setCurrentIndex(current_idx)
        self._provider_combo.blockSignals(False)
        self._last_model_provider_pid = self._provider_combo.currentData()
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        layout.addRow(QLabel("Provider"), self._provider_combo)

        cfg = load_config()
        merge_stored_provider_model_memory(cfg)
        # Per-provider model memory: {pid: last_used_model}
        self._provider_models: dict = dict(cfg.get("provider_models", {}))
        self._provider_model_history: dict[str, list[str]] = {
            k: list(v) for k, v in (cfg.get("provider_model_history") or {}).items()
        }
        # Seed with the currently active model so switching away preserves it
        if self.agent.model:
            self._provider_models.setdefault(self.agent.provider, self.agent.model)

        # Main model + temperature on one row
        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        self._model_edit = QLineEdit(self.agent.model)
        self._model_edit.setPlaceholderText("e.g. deepseek/deepseek-chat-v3-0324")
        model_row.addWidget(self._model_edit, stretch=1)
        temp_label = QLabel("Temp")
        temp_label.setFixedWidth(32)
        model_row.addWidget(temp_label)
        self._temp_edit = QLineEdit(str(cfg.get("temperature", 0.7)))
        self._temp_edit.setFixedWidth(45)
        model_row.addWidget(self._temp_edit)
        layout.addRow(QLabel("Model"), model_row)

        self._main_model_completer = QCompleter(self)
        self._main_model_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._main_model_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._main_model_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._main_model_completer.setMaxVisibleItems(8)
        self._main_model_suggest_model = QStringListModel(self)
        self._main_model_completer.setModel(self._main_model_suggest_model)
        self._model_edit.setCompleter(self._main_model_completer)
        self._refresh_main_model_suggestions()

        # Summary model + temperature on one row
        summary_row = QHBoxLayout()
        summary_row.setSpacing(6)
        self._summary_model_edit = QLineEdit(cfg.get("summary_model", ""))
        self._summary_model_edit.setPlaceholderText("(blank = use main model)")
        summary_row.addWidget(self._summary_model_edit, stretch=1)
        stemp_label = QLabel("Temp")
        stemp_label.setFixedWidth(32)
        summary_row.addWidget(stemp_label)
        self._summary_temp_edit = QLineEdit(str(cfg.get("summary_temperature", 0.3)))
        self._summary_temp_edit.setFixedWidth(45)
        summary_row.addWidget(self._summary_temp_edit)
        layout.addRow(QLabel("Summary Model"), summary_row)

        # Vision model (for image-attached messages)
        vision_row = QHBoxLayout()
        vision_row.setSpacing(6)
        self._vision_check = QCheckBox("Enable")
        self._vision_check.setChecked(bool(cfg.get("vision_enabled", False)))
        vision_row.addWidget(self._vision_check)
        self._vision_provider_combo = QComboBox()
        for vpid, vinfo in PROVIDER_INFO.items():
            self._vision_provider_combo.addItem(vinfo["name"], vpid)
        vp_cur = (cfg.get("vision_provider") or "openrouter").strip()
        if vp_cur in PROVIDER_INFO:
            self._vision_provider_combo.setCurrentIndex(
                list(PROVIDER_INFO.keys()).index(vp_cur))
        else:
            self._vision_provider_combo.setCurrentIndex(0)
        vision_row.addWidget(self._vision_provider_combo)
        self._vision_model_edit = QLineEdit(cfg.get("vision_model", ""))
        self._vision_model_edit.setPlaceholderText("e.g. qwen/qwen2.5-vl-72b-instruct")
        vision_row.addWidget(self._vision_model_edit, stretch=1)
        layout.addRow(QLabel("Vision Model"), vision_row)

        # Explore-files model — cheap parallel summarizer used by explore_files tool
        explore_row = QHBoxLayout()
        explore_row.setSpacing(6)
        self._explore_provider_combo = QComboBox()
        for epid, einfo in PROVIDER_INFO.items():
            self._explore_provider_combo.addItem(einfo["name"], epid)
        ep_cur = (cfg.get("subagent_explore_provider") or "anthropic").strip()
        if ep_cur in PROVIDER_INFO:
            self._explore_provider_combo.setCurrentIndex(
                list(PROVIDER_INFO.keys()).index(ep_cur))
        else:
            self._explore_provider_combo.setCurrentIndex(0)
        explore_row.addWidget(self._explore_provider_combo)
        self._explore_model_edit = QLineEdit(cfg.get("subagent_explore_model", "claude-haiku-4-5"))
        self._explore_model_edit.setPlaceholderText("cheap fast model — e.g. claude-haiku-4-5")
        explore_row.addWidget(self._explore_model_edit, stretch=1)
        layout.addRow(QLabel("Explore Model"), explore_row)

        self._max_tokens_edit = QLineEdit(str(cfg.get("max_tokens", 4096)))
        layout.addRow(QLabel("Max Tokens"), self._max_tokens_edit)

        # Reasoning / extended thinking (cross-provider)
        sep_think = QLabel("--- Reasoning / Extended Thinking ---")
        sep_think.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addRow(sep_think)

        thinking_row = QHBoxLayout()
        thinking_row.setSpacing(6)
        self._thinking_check = QCheckBox("Enable")
        self._thinking_check.setChecked(bool(cfg.get("thinking_enabled", False)))
        thinking_row.addWidget(self._thinking_check)
        thinking_row.addStretch()
        thinking_row.addWidget(QLabel("Effort"))
        from PyQt6.QtWidgets import QComboBox as _QComboBox
        self._reasoning_effort_combo = _QComboBox()
        self._reasoning_effort_combo.addItems(["low", "medium", "high"])
        _cur_effort = str(cfg.get("reasoning_effort", "medium")).lower()
        if _cur_effort not in ("low", "medium", "high"):
            _cur_effort = "medium"
        self._reasoning_effort_combo.setCurrentText(_cur_effort)
        self._reasoning_effort_combo.setFixedWidth(90)
        thinking_row.addWidget(self._reasoning_effort_combo)
        thinking_row.addSpacing(8)
        thinking_row.addWidget(QLabel("Anthropic budget"))
        self._thinking_budget_spin = QSpinBox()
        self._thinking_budget_spin.setRange(0, 100000)
        self._thinking_budget_spin.setSingleStep(1000)
        self._thinking_budget_spin.setValue(int(cfg.get("thinking_budget", 0)))
        self._thinking_budget_spin.setSpecialValueText("auto")
        self._thinking_budget_spin.setFixedWidth(80)
        thinking_row.addWidget(self._thinking_budget_spin)
        layout.addRow(QLabel("Thinking"), thinking_row)

        # Embedding model (for vector search in memory/recall)
        sep_embed = QLabel("--- Embeddings (Memory Search) ---")
        sep_embed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addRow(sep_embed)

        self._embed_model_edit = QLineEdit(cfg.get("embedding_model", "openai/text-embedding-3-small"))
        self._embed_model_edit.setPlaceholderText("openai/text-embedding-3-small")
        layout.addRow(QLabel("Embedding Model"), self._embed_model_edit)

        # Fallback models (used on refusal)
        sep = QLabel("--- Fallback Models (on refusal) ---")
        sep.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addRow(sep)

        self._fallback_edits: list[QLineEdit] = []
        self._fallback_provider_combos: list[QComboBox] = []
        for i in range(1, 4):
            row = QHBoxLayout()
            row.setSpacing(6)
            pc = QComboBox()
            for fpid, finfo in PROVIDER_INFO.items():
                pc.addItem(finfo["name"], fpid)
            fp_stored = (cfg.get(f"fallback_{i}_provider") or "").strip()
            if fp_stored in PROVIDER_INFO:
                pc.setCurrentIndex(list(PROVIDER_INFO.keys()).index(fp_stored))
            elif self.agent.provider in PROVIDER_INFO:
                pc.setCurrentIndex(list(PROVIDER_INFO.keys()).index(self.agent.provider))
            else:
                pc.setCurrentIndex(0)
            self._fallback_provider_combos.append(pc)
            edit = QLineEdit(cfg.get(f"fallback_model_{i}", ""))
            edit.setPlaceholderText(f"Model id (provider {i})")
            self._fallback_edits.append(edit)
            row.addWidget(pc)
            row.addWidget(edit, stretch=1)
            wrap = QWidget()
            wrap.setLayout(row)
            layout.addRow(QLabel(f"Fallback {i}"), wrap)

        return w

    # ------------------------------------------------------------------
    # Workspaces tab
    # ------------------------------------------------------------------

    def _build_workspaces_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Workspaces define where the agent operates. "
                                "All file and terminal tools run inside the active workspace."))

        # Workspace list
        self._ws_list = QListWidget()
        self._ws_list.currentRowChanged.connect(self._on_ws_selected)
        layout.addWidget(self._ws_list, stretch=1)

        # Buttons row
        btn_row = QHBoxLayout()
        add_folder_btn = QPushButton("Add Folder")
        add_folder_btn.clicked.connect(self._add_folder_workspace)
        btn_row.addWidget(add_folder_btn)

        new_folder_btn = QPushButton("New Folder")
        new_folder_btn.clicked.connect(self._create_new_folder)
        btn_row.addWidget(new_folder_btn)

        new_venv_btn = QPushButton("New + venv")
        new_venv_btn.clicked.connect(self._create_folder_with_venv)
        btn_row.addWidget(new_venv_btn)

        remove_btn = QPushButton("Remove")
        remove_btn.clicked.connect(self._remove_workspace)
        btn_row.addWidget(remove_btn)

        layout.addLayout(btn_row)

        # Detail group
        detail = QGroupBox("Workspace Details")
        detail_layout = QFormLayout(detail)
        detail_layout.setSpacing(6)

        self._ws_name_edit = QLineEdit()
        self._ws_name_edit.setPlaceholderText("Workspace name")
        detail_layout.addRow(QLabel("Name"), self._ws_name_edit)

        path_row = QHBoxLayout()
        self._ws_path_edit = QLineEdit()
        self._ws_path_edit.setPlaceholderText(
            "Apps/… path under Agent root, or full path (Windows drive / UNC)"
        )
        path_row.addWidget(self._ws_path_edit)
        browse_btn = QPushButton("...")
        browse_btn.setFixedWidth(30)
        browse_btn.clicked.connect(self._browse_ws_path)
        path_row.addWidget(browse_btn)
        detail_layout.addRow(QLabel("Path"), path_row)

        venv_row = QHBoxLayout()
        self._ws_venv_edit = QLineEdit()
        self._ws_venv_edit.setPlaceholderText("(optional) path to Python venv")
        venv_row.addWidget(self._ws_venv_edit)
        venv_browse_btn = QPushButton("...")
        venv_browse_btn.setFixedWidth(30)
        venv_browse_btn.clicked.connect(self._browse_venv_path)
        venv_row.addWidget(venv_browse_btn)
        detail_layout.addRow(QLabel("venv"), venv_row)

        layout.addWidget(detail)

        # Load existing workspaces (normalize to agent-relative when under install root)
        self._ws_data = {}
        for name, entry in (self.agent.config.get("workspaces") or {}).items():
            if isinstance(entry, dict):
                p = (entry.get("path") or "").strip()
                v = (entry.get("venv") or "").strip()
                self._ws_data[name] = {
                    "path": (
                        to_config_workspace_path(resolve_workspace_entry_path(p))
                        if p
                        else ""
                    ),
                    "venv": (
                        to_config_workspace_path(resolve_workspace_entry_path(v))
                        if v
                        else ""
                    ),
                }
            else:
                raw = str(entry).strip()
                self._ws_data[name] = {
                    "path": (
                        to_config_workspace_path(resolve_workspace_entry_path(raw))
                        if raw
                        else ""
                    ),
                    "venv": "",
                }
        self._active_ws = self.agent._workspace_name
        self._refresh_ws_list()

        return w

    def _refresh_ws_list(self):
        self._ws_list.clear()
        for name, ws in self._ws_data.items():
            prefix = ">> " if name == self._active_ws else "   "
            item = QListWidgetItem(f"{prefix}{name}  -  {ws.get('path', '')}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._ws_list.addItem(item)

    def _on_ws_selected(self, row):
        if row < 0:
            return
        item = self._ws_list.item(row)
        name = item.data(Qt.ItemDataRole.UserRole)
        ws = self._ws_data.get(name, {})
        self._ws_name_edit.setText(name)
        self._ws_path_edit.setText(ws.get("path", ""))
        self._ws_venv_edit.setText(ws.get("venv", ""))
        self._active_ws = name

    def _add_folder_workspace(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Workspace Folder")
        if not folder:
            return
        name = Path(folder).name
        # Avoid duplicates
        base = name
        i = 2
        while name in self._ws_data:
            name = f"{base}_{i}"
            i += 1
        self._ws_data[name] = {"path": to_config_workspace_path(folder), "venv": ""}
        self._active_ws = name
        self._refresh_ws_list()
        self._mark_dirty()

    def _create_new_folder(self):
        parent = QFileDialog.getExistingDirectory(self, "Choose Parent Directory")
        if not parent:
            return
        name = self._ws_name_edit.text().strip() or "new_workspace"
        full_path = os.path.join(parent, name)
        os.makedirs(full_path, exist_ok=True)
        self._ws_data[name] = {"path": to_config_workspace_path(full_path), "venv": ""}
        self._active_ws = name
        self._refresh_ws_list()
        self._mark_dirty()

    def _create_folder_with_venv(self):
        parent = QFileDialog.getExistingDirectory(self, "Choose Parent Directory")
        if not parent:
            return
        name = self._ws_name_edit.text().strip() or "new_workspace"
        full_path = os.path.join(parent, name)
        venv_path = os.path.join(full_path, ".venv")
        os.makedirs(full_path, exist_ok=True)
        # Create venv
        subprocess.run([sys.executable, "-m", "venv", venv_path],
                       capture_output=True, timeout=60)
        self._ws_data[name] = {
            "path": to_config_workspace_path(full_path),
            "venv": to_config_workspace_path(venv_path),
        }
        self._active_ws = name
        self._refresh_ws_list()
        self._mark_dirty()

    def _remove_workspace(self):
        row = self._ws_list.currentRow()
        if row < 0:
            return
        item = self._ws_list.item(row)
        name = item.data(Qt.ItemDataRole.UserRole)
        self._ws_data.pop(name, None)
        if self._active_ws == name:
            self._active_ws = next(iter(self._ws_data), "")
        self._refresh_ws_list()
        self._mark_dirty()

    def _browse_ws_path(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder:
            stored = to_config_workspace_path(folder)
            self._ws_path_edit.setText(stored)
            # Update data for selected workspace
            row = self._ws_list.currentRow()
            if row >= 0:
                item = self._ws_list.item(row)
                name = item.data(Qt.ItemDataRole.UserRole)
                self._ws_data[name]["path"] = stored

    def _browse_venv_path(self):
        folder = QFileDialog.getExistingDirectory(self, "Select venv Directory")
        if folder:
            stored = to_config_workspace_path(folder)
            self._ws_venv_edit.setText(stored)
            row = self._ws_list.currentRow()
            if row >= 0:
                item = self._ws_list.item(row)
                name = item.data(Qt.ItemDataRole.UserRole)
                self._ws_data[name]["venv"] = stored

    # ------------------------------------------------------------------
    # System Prompt tab
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Memory tab
    # ------------------------------------------------------------------

    def _build_memory_tab(self) -> QWidget:
        from PyQt6.QtWidgets import QSplitter, QTreeWidget, QTreeWidgetItem

        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(6)
        layout.setContentsMargins(8, 8, 8, 8)

        cfg = load_config()
        p = PALETTE
        small_font = QFont("Consolas", 8)
        mono9 = QFont("Consolas", 9)
        muted = f"color:{p['muted_text']};font-size:8pt;background:transparent;border:none;"

        # ═══════════════════════════════════════════════════════════════
        # Section 1: Memory Streams selector (top)
        # ═══════════════════════════════════════════════════════════════
        streams_group = QGroupBox("Memory Streams")
        streams_layout = QVBoxLayout(streams_group)
        streams_layout.setSpacing(4)
        streams_layout.setContentsMargins(6, 12, 6, 6)

        # Stream list + buttons
        stream_top = QHBoxLayout()
        self._stream_list = QListWidget()
        self._stream_list.setFixedHeight(72)
        self._stream_list.setFont(mono9)
        self._stream_list.currentRowChanged.connect(self._on_stream_selected)
        stream_top.addWidget(self._stream_list, stretch=1)

        stream_btns = QVBoxLayout()
        stream_btns.setSpacing(2)
        for label, slot in [("+ Add", "_add_stream"), ("Duplicate", "_duplicate_stream"),
                            ("- Remove", "_remove_stream")]:
            btn = QPushButton(label)
            btn.setFont(small_font)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedWidth(90)
            btn.clicked.connect(getattr(self, slot))
            stream_btns.addWidget(btn)
            if label == "- Remove":
                self._stream_remove_btn = btn
        stream_btns.addStretch()
        stream_top.addLayout(stream_btns)
        streams_layout.addLayout(stream_top)

        # Stream details (inline, compact)
        detail = QFormLayout()
        detail.setSpacing(4)

        name_row = QHBoxLayout()
        name_row.setSpacing(6)
        self._stream_name_edit = QLineEdit()
        self._stream_name_edit.setFont(mono9)
        self._stream_name_edit.setPlaceholderText("Stream name")
        self._stream_name_edit.textChanged.connect(self._on_stream_name_changed)
        name_row.addWidget(self._stream_name_edit, stretch=1)
        self._stream_auto_check = QCheckBox("Auto-subscribe")
        self._stream_auto_check.setFont(small_font)
        self._stream_auto_check.stateChanged.connect(self._on_stream_detail_changed)
        name_row.addWidget(self._stream_auto_check)
        detail.addRow(QLabel("Name"), name_row)

        self._stream_desc_edit = QLineEdit()
        self._stream_desc_edit.setFont(mono9)
        self._stream_desc_edit.setPlaceholderText("What this stream retains (e.g. user preferences, project state)")
        self._stream_desc_edit.textChanged.connect(self._on_stream_detail_changed)
        detail.addRow(QLabel("Focus"), self._stream_desc_edit)

        streams_layout.addLayout(detail)
        layout.addWidget(streams_group)

        # ═══════════════════════════════════════════════════════════════
        # Section 2: Notes browser/editor (middle, takes most space)
        # ═══════════════════════════════════════════════════════════════
        from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QStackedWidget

        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_layout.setSpacing(4)
        notes_layout.setContentsMargins(6, 12, 6, 6)

        notes_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left: category tree + tree buttons
        tree_panel = QWidget()
        tree_layout = QVBoxLayout(tree_panel)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(4)

        self._notes_tree = QTreeWidget()
        self._notes_tree.setHeaderHidden(True)
        self._notes_tree.setFont(mono9)
        self._notes_tree.setMinimumWidth(150)
        self._notes_tree.currentItemChanged.connect(self._on_note_tree_click)
        tree_layout.addWidget(self._notes_tree, stretch=1)

        tree_btn_row = QHBoxLayout()
        tree_btn_row.setSpacing(3)
        self._tree_add_cat_btn = QPushButton("+ Category")
        self._tree_add_cat_btn.setFont(small_font)
        self._tree_add_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tree_add_cat_btn.clicked.connect(self._add_category)
        tree_btn_row.addWidget(self._tree_add_cat_btn)
        self._tree_del_cat_btn = QPushButton("- Category")
        self._tree_del_cat_btn.setFont(small_font)
        self._tree_del_cat_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tree_del_cat_btn.clicked.connect(self._delete_category)
        tree_btn_row.addWidget(self._tree_del_cat_btn)
        tree_btn_row.addStretch()
        tree_layout.addLayout(tree_btn_row)

        notes_splitter.addWidget(tree_panel)

        # Right: stacked widget — list view (page 0) or editor view (page 1)
        self._notes_stack = QStackedWidget()

        # ── Page 0: note list view ──
        list_page = QWidget()
        lp_layout = QVBoxLayout(list_page)
        lp_layout.setContentsMargins(0, 0, 0, 0)
        lp_layout.setSpacing(4)

        self._note_list_header = QLabel("Select a category")
        self._note_list_header.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        self._note_list_header.setStyleSheet(f"color:{p['accent']};border:none;")
        lp_layout.addWidget(self._note_list_header)

        self._note_list = QListWidget()
        self._note_list.setFont(mono9)
        self._note_list.setStyleSheet(f"""
            QListWidget::item {{
                border-bottom: 1px solid {p['border']};
                padding: 4px 2px;
            }}
            QListWidget::item:hover {{
                background: rgba({QColor(p['accent']).red()}, {QColor(p['accent']).green()}, {QColor(p['accent']).blue()}, 0.10);
            }}
            QListWidget::item:selected {{
                background: rgba({QColor(p['accent']).red()}, {QColor(p['accent']).green()}, {QColor(p['accent']).blue()}, 0.20);
                color: {p['accent']};
            }}
        """)
        self._note_list.itemDoubleClicked.connect(self._on_note_list_double_click)
        lp_layout.addWidget(self._note_list, stretch=1)

        list_btn_row = QHBoxLayout()
        list_btn_row.setSpacing(4)
        list_btn_row.addStretch()
        self._add_note_btn = QPushButton("+ Note")
        self._add_note_btn.setFont(small_font)
        self._add_note_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_note_btn.clicked.connect(self._new_note_at_category)
        list_btn_row.addWidget(self._add_note_btn)
        self._del_note_btn = QPushButton("- Note")
        self._del_note_btn.setFont(small_font)
        self._del_note_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._del_note_btn.clicked.connect(self._delete_selected_note_from_list)
        list_btn_row.addWidget(self._del_note_btn)
        lp_layout.addLayout(list_btn_row)

        self._notes_stack.addWidget(list_page)

        # ── Page 1: note editor view ──
        edit_page = QWidget()
        ep_layout = QVBoxLayout(edit_page)
        ep_layout.setContentsMargins(0, 0, 0, 0)
        ep_layout.setSpacing(4)

        back_row = QHBoxLayout()
        self._note_back_btn = QPushButton("< Back")
        self._note_back_btn.setFont(small_font)
        self._note_back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._note_back_btn.clicked.connect(self._note_back_to_list)
        back_row.addWidget(self._note_back_btn)
        back_row.addStretch()
        ep_layout.addLayout(back_row)

        path_row = QHBoxLayout()
        path_row.setSpacing(4)
        self._note_category_edit = QLineEdit()
        self._note_category_edit.setFont(mono9)
        self._note_category_edit.setPlaceholderText("category/path")
        path_row.addWidget(self._note_category_edit, stretch=1)
        path_row.addWidget(QLabel("/"))
        self._note_title_edit = QLineEdit()
        self._note_title_edit.setFont(mono9)
        self._note_title_edit.setPlaceholderText("title")
        path_row.addWidget(self._note_title_edit, stretch=1)
        ep_layout.addLayout(path_row)

        self._note_content_edit = QTextEdit()
        self._note_content_edit.setFont(mono9)
        self._note_content_edit.setAcceptRichText(False)
        self._note_content_edit.setPlaceholderText("Note content (max 2000 chars)")
        ep_layout.addWidget(self._note_content_edit, stretch=1)

        edit_btn_row = QHBoxLayout()
        edit_btn_row.setSpacing(4)
        save_btn = QPushButton("Save")
        save_btn.setFont(small_font)
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._save_current_note)
        edit_btn_row.addWidget(save_btn)
        del_btn = QPushButton("Delete")
        del_btn.setFont(small_font)
        del_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        del_btn.clicked.connect(self._delete_current_note)
        edit_btn_row.addWidget(del_btn)
        self._note_status = QLabel("")
        self._note_status.setFont(small_font)
        self._note_status.setStyleSheet(muted)
        edit_btn_row.addWidget(self._note_status, stretch=1)
        ep_layout.addLayout(edit_btn_row)

        self._notes_stack.addWidget(edit_page)

        notes_splitter.addWidget(self._notes_stack)
        notes_splitter.setSizes([180, 420])
        notes_layout.addWidget(notes_splitter)

        self._notes_listed = []  # [(cat, title, content), ...]
        self._notes_current_cat = ""
        layout.addWidget(notes_group, stretch=1)

        # ═══════════════════════════════════════════════════════════════
        # Section 3: Rolling Summary config (bottom, per-stream)
        # ═══════════════════════════════════════════════════════════════
        summary_group = QGroupBox("Rolling Summary (per stream)")
        summary_layout = QVBoxLayout(summary_group)
        summary_layout.setSpacing(4)
        summary_layout.setContentsMargins(6, 12, 6, 6)

        sum_top = QHBoxLayout()
        sum_top.setSpacing(8)
        self._enable_summary_combo = QComboBox()
        self._enable_summary_combo.addItem("Enabled", True)
        self._enable_summary_combo.addItem("Disabled", False)
        self._enable_summary_combo.setCurrentIndex(
            0 if cfg.get("enable_summarization", True) else 1)
        sum_top.addWidget(QLabel("Status"))
        sum_top.addWidget(self._enable_summary_combo)
        sum_top.addSpacing(12)
        sum_top.addWidget(QLabel("Char Limit"))
        self._summary_char_limit = QLineEdit(str(cfg.get("summary_char_limit", 15000)))
        self._summary_char_limit.setFixedWidth(60)
        sum_top.addWidget(self._summary_char_limit)
        sum_top.addSpacing(12)
        sum_top.addWidget(QLabel("Refresh After"))
        self._summary_refresh = QLineEdit(str(cfg.get("summary_refresh_chars", 5000)))
        self._summary_refresh.setFixedWidth(60)
        sum_top.addWidget(self._summary_refresh)
        sum_top.addStretch()
        summary_layout.addLayout(sum_top)

        self._stream_guidance_edit = QTextEdit()
        self._stream_guidance_edit.setFont(QFont("Consolas", 8))
        self._stream_guidance_edit.setFixedHeight(50)
        self._stream_guidance_edit.setAcceptRichText(False)
        self._stream_guidance_edit.setPlaceholderText(
            "Summary guidance for this stream (what to emphasize/preserve)..."
        )
        self._stream_guidance_edit.textChanged.connect(self._on_stream_detail_changed)
        summary_layout.addWidget(self._stream_guidance_edit)

        layout.addWidget(summary_group)

        # ── Load data ──
        _DEFAULT_GUIDANCE = (
            "Preserve ALL important details: decisions made, "
            "files discussed, code written, problems solved, user preferences, and "
            "unresolved questions."
        )
        self._streams_data = cfg.get("memory_streams", [
            {"name": "General", "description": "General-purpose conversation memory",
             "auto_subscribe": True, "summary_guidance": _DEFAULT_GUIDANCE}
        ])
        if not self._streams_data:
            self._streams_data = [{"name": "General", "description": "General-purpose conversation memory",
                                   "auto_subscribe": True, "summary_guidance": _DEFAULT_GUIDANCE}]
        for s in self._streams_data:
            if not s.get("summary_guidance"):
                s["summary_guidance"] = _DEFAULT_GUIDANCE
        self._populate_stream_list()

        return w

    # ── Notes browser (v2 — morphing list/editor) ─────────────────

    def _current_stream_name(self) -> str:
        row = self._stream_list.currentRow()
        if row < 0 or row >= len(self._streams_data):
            return ""
        return self._streams_data[row].get("name", "")

    def _refresh_notes_tree(self):
        """Rebuild the category tree for the selected stream using / paths."""
        from PyQt6.QtWidgets import QTreeWidgetItem
        self._notes_tree.clear()
        stream = self._current_stream_name()
        if not stream:
            return
        try:
            from core.database import list_note_categories
            cats = list_note_categories(stream)
        except Exception:
            return

        # Collect all category paths (including intermediate)
        all_paths = set()
        cat_counts = {}  # path -> direct note count
        for cat in cats:
            cat_counts[cat["category"]] = cat["count"]
            parts = cat["category"].split("/")
            for i in range(len(parts)):
                all_paths.add("/".join(parts[:i + 1]))

        nodes = {}
        for path in sorted(all_paths):
            parts = path.split("/")
            label = parts[-1]
            # Count notes at and below this path
            count = sum(c for p, c in cat_counts.items() if p == path or p.startswith(path + "/"))
            item = QTreeWidgetItem([f"{label} ({count})"])
            item.setData(0, Qt.ItemDataRole.UserRole, {"type": "category", "path": path})

            parent_path = "/".join(parts[:-1])
            parent = nodes.get(parent_path) if len(parts) > 1 else None
            if parent:
                parent.addChild(item)
            else:
                self._notes_tree.addTopLevelItem(item)
            nodes[path] = item

        self._notes_tree.expandAll()

    def _on_note_tree_click(self, current, previous):
        """Category selected in tree — show notes list."""
        if not current:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        path = data.get("path", "")
        self._notes_current_cat = path
        self._show_notes_for_path(path)
        self._notes_stack.setCurrentIndex(0)

    def _show_notes_for_path(self, path):
        """Populate the note list for a category path (including sub-paths)."""
        self._note_list.clear()
        self._notes_listed = []
        self._note_list_header.setText(path or "All Notes")
        stream = self._current_stream_name()
        if not stream:
            return
        try:
            from core.database import list_note_categories, list_notes_in_category, read_note
            cats = list_note_categories(stream)
            for cat in cats:
                cat_path = cat["category"]
                if cat_path == path or cat_path.startswith(path + "/"):
                    notes = list_notes_in_category(stream, cat_path)
                    for n in notes:
                        full = read_note(stream, cat_path, n["title"])
                        content = full["content"] if full else ""
                        # Display: relative path if sub-category
                        if cat_path == path:
                            display = n["title"]
                        else:
                            rel = cat_path[len(path) + 1:]
                            display = f"{rel}/{n['title']}"
                        preview = content[:80].replace("\n", " ")
                        item = QListWidgetItem(f"{display}\n  {preview}...")
                        item.setFont(QFont("Consolas", 9))
                        self._note_list.addItem(item)
                        self._notes_listed.append((cat_path, n["title"], content))
        except Exception as e:
            print(f"[Settings] Note list error: {e}")

    def _on_note_list_double_click(self, item):
        """Double-click a note in the list → open in editor."""
        row = self._note_list.row(item)
        if row < 0 or row >= len(self._notes_listed):
            return
        cat, title, content = self._notes_listed[row]
        self._open_note_editor(cat, title, content)

    def _open_note_editor(self, cat="", title="", content=""):
        self._note_category_edit.setText(cat)
        self._note_title_edit.setText(title)
        self._note_content_edit.setPlainText(content)
        self._note_status.setText(f"{len(content)} chars" if content else "")
        self._notes_stack.setCurrentIndex(1)

    def _note_back_to_list(self):
        self._notes_stack.setCurrentIndex(0)
        self._show_notes_for_path(self._notes_current_cat)

    def _new_note_at_category(self):
        self._open_note_editor(cat=self._notes_current_cat)
        self._note_title_edit.setFocus()

    def _delete_selected_note_from_list(self):
        """Delete the selected note from the list view."""
        row = self._note_list.currentRow()
        if row < 0 or row >= len(self._notes_listed):
            return
        cat, title, _ = self._notes_listed[row]
        stream = self._current_stream_name()
        if not stream:
            return
        try:
            from core.database import delete_note
            delete_note(stream, cat, title)
            self._refresh_notes_tree()
            self._show_notes_for_path(self._notes_current_cat)
        except Exception:
            pass

    def _save_current_note(self):
        stream = self._current_stream_name()
        if not stream:
            return
        category = self._note_category_edit.text().strip()
        title = self._note_title_edit.text().strip()
        content = self._note_content_edit.toPlainText().strip()
        if not category or not title:
            self._note_status.setText("Need category + title")
            return
        if not content:
            self._note_status.setText("Note is empty")
            return
        try:
            from core.database import save_note
            save_note(stream, category, title, content)
            self._note_status.setText("Saved")
            self._refresh_notes_tree()
        except Exception as e:
            self._note_status.setText(f"Error: {e}")

    def _delete_current_note(self):
        stream = self._current_stream_name()
        if not stream:
            return
        category = self._note_category_edit.text().strip()
        title = self._note_title_edit.text().strip()
        if not category or not title:
            return
        try:
            from core.database import delete_note
            delete_note(stream, category, title)
            self._note_category_edit.clear()
            self._note_title_edit.clear()
            self._note_content_edit.clear()
            self._note_status.setText("Deleted")
            self._refresh_notes_tree()
            self._note_back_to_list()
        except Exception as e:
            self._note_status.setText(f"Error: {e}")

    def _add_category(self):
        """Prompt for a new category name, create it with a placeholder note, show in tree."""
        from PyQt6.QtWidgets import QInputDialog
        parent_path = self._notes_current_cat
        name, ok = QInputDialog.getText(self, "New Category",
                                         f"Category name{' (under ' + parent_path + ')' if parent_path else ''}:")
        if not ok or not name.strip():
            return
        new_path = f"{parent_path}/{name.strip()}" if parent_path else name.strip()
        self._notes_current_cat = new_path
        self._refresh_notes_tree()
        self._show_notes_for_path(new_path)
        self._notes_stack.setCurrentIndex(0)

    def _delete_category(self):
        """Delete all notes in the selected category (and sub-categories)."""
        path = self._notes_current_cat
        if not path:
            return
        stream = self._current_stream_name()
        if not stream:
            return
        from ui.glass_dialog import GlassDialog
        if not GlassDialog.confirm(self, "Delete Category",
                                    f"Delete all notes in '{path}' and its sub-categories?"):
            return
        try:
            from core.database import list_note_categories, list_notes_in_category, delete_note
            cats = list_note_categories(stream)
            for cat in cats:
                cat_path = cat["category"]
                if cat_path == path or cat_path.startswith(path + "/"):
                    notes = list_notes_in_category(stream, cat_path)
                    for n in notes:
                        delete_note(stream, cat_path, n["title"])
            self._refresh_notes_tree()
            self._notes_current_cat = ""
            self._note_list.clear()
            self._notes_listed = []
            self._note_list_header.setText("Select a category")
        except Exception as e:
            print(f"[Settings] Delete category error: {e}")

    def _populate_stream_list(self):
        self._stream_list.blockSignals(True)
        self._stream_list.clear()
        for s in self._streams_data:
            item = QListWidgetItem(s["name"])
            item.setFont(QFont("Consolas", 9))
            self._stream_list.addItem(item)
        self._stream_list.blockSignals(False)
        if self._streams_data:
            self._stream_list.setCurrentRow(0)
            self._on_stream_selected(0)

    def _on_stream_selected(self, row):
        if row < 0 or row >= len(self._streams_data):
            self._stream_name_edit.clear()
            self._stream_desc_edit.clear()
            self._stream_guidance_edit.clear()
            self._stream_auto_check.setChecked(False)
            return
        s = self._streams_data[row]
        self._stream_name_edit.blockSignals(True)
        self._stream_desc_edit.blockSignals(True)
        self._stream_guidance_edit.blockSignals(True)
        self._stream_auto_check.blockSignals(True)
        self._stream_name_edit.setText(s.get("name", ""))
        self._stream_desc_edit.setText(s.get("description", ""))
        self._stream_guidance_edit.setPlainText(s.get("summary_guidance", ""))
        self._stream_auto_check.setChecked(s.get("auto_subscribe", False))
        self._stream_name_edit.blockSignals(False)
        self._stream_desc_edit.blockSignals(False)
        self._stream_guidance_edit.blockSignals(False)
        self._stream_auto_check.blockSignals(False)
        # First stream can't be deleted (always need at least one)
        is_first = (row == 0)
        self._stream_remove_btn.setEnabled(not is_first)
        # Refresh notes for this stream
        self._refresh_notes_tree()
        self._notes_current_cat = ""
        self._note_list.clear()
        self._notes_listed = []
        self._note_list_header.setText("Select a category")
        self._notes_stack.setCurrentIndex(0)

    def _on_stream_name_changed(self, text):
        row = self._stream_list.currentRow()
        if row < 0 or row >= len(self._streams_data):
            return
        self._streams_data[row]["name"] = text.strip()
        item = self._stream_list.item(row)
        if item:
            item.setText(text.strip())

    def _on_stream_detail_changed(self):
        row = self._stream_list.currentRow()
        if row < 0 or row >= len(self._streams_data):
            return
        self._streams_data[row]["description"] = self._stream_desc_edit.text().strip()
        self._streams_data[row]["summary_guidance"] = self._stream_guidance_edit.toPlainText().strip()
        self._streams_data[row]["auto_subscribe"] = self._stream_auto_check.isChecked()

    def _add_stream(self):
        name = f"stream_{len(self._streams_data)}"
        self._streams_data.append({
            "name": name, "description": "", "summary_guidance": "", "auto_subscribe": False
        })
        self._populate_stream_list()
        self._stream_list.setCurrentRow(len(self._streams_data) - 1)
        self._stream_name_edit.setFocus()
        self._stream_name_edit.selectAll()

    def _duplicate_stream(self):
        row = self._stream_list.currentRow()
        if row < 0 or row >= len(self._streams_data):
            return
        source = self._streams_data[row]
        # Find a unique name
        base = source["name"]
        n = 1
        while any(s["name"] == f"{base} {n}" for s in self._streams_data):
            n += 1
        clone = {
            "name": f"{base} {n}",
            "description": source.get("description", ""),
            "summary_guidance": source.get("summary_guidance", ""),
            "auto_subscribe": False,
        }
        self._streams_data.append(clone)
        self._populate_stream_list()
        self._stream_list.setCurrentRow(len(self._streams_data) - 1)
        self._stream_name_edit.setFocus()
        self._stream_name_edit.selectAll()

    def _remove_stream(self):
        row = self._stream_list.currentRow()
        if row < 0 or row >= len(self._streams_data):
            return
        if len(self._streams_data) <= 1:
            return  # Must have at least one
        self._streams_data.pop(row)
        self._populate_stream_list()

    # ------------------------------------------------------------------
    # System Prompt tab
    # ------------------------------------------------------------------

    def _build_prompt_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(QLabel("System Prompt (base — applies to every conversation)"))
        self._prompt_edit = QTextEdit()
        # Edit the BASE prompt only — NOT agent.system_prompt (which includes any
        # active conversation overlay). Reading the property here is exactly what
        # let a conversation prompt leak into — and overwrite — the global base.
        self._prompt_edit.setPlainText(self.agent.default_system_prompt)
        self._prompt_edit.setAcceptRichText(False)
        layout.addWidget(self._prompt_edit)

        cfg = load_config()
        self._full_tools_check = QCheckBox("Send full tools list each turn")
        self._full_tools_check.setChecked(cfg.get("full_tools_list", True))
        self._full_tools_check.setToolTip(
            "ON (default): sends all tool schemas every turn.\n"
            "  Best for providers with prompt caching (Anthropic, OpenAI) — the full list\n"
            "  gets cached after the first turn and costs ~10% on subsequent reads.\n\n"
            "OFF (experimental): two-pass category routing — ~85% fewer tool-schema tokens.\n"
            "  Best for providers without prompt caching (OpenRouter free tier, DeepSeek, etc.)\n"
            "  where you pay full price for the tool list every single turn.\n"
            "  Adds one small routing call and may occasionally pick the wrong category."
        )
        layout.addWidget(self._full_tools_check)
        return w

    # ------------------------------------------------------------------
    # Voice tab
    # ------------------------------------------------------------------

    def _build_voice_tab(self) -> QWidget:
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(10)

        cfg = load_config()
        p = PALETTE

        # ── Top row: backend selector + universal toggles ──
        top_form = QFormLayout()
        top_form.setSpacing(10)

        self._ui_sounds_check = QCheckBox("UI Sounds")
        self._ui_sounds_check.setChecked(cfg.get("ui_sounds", True))
        top_form.addRow("", self._ui_sounds_check)

        ws_box = QGroupBox("Workspace sounds")
        ws_form = QFormLayout(ws_box)
        ws_form.setSpacing(8)

        self._workspace_edit_sounds_check = QCheckBox("Edit sounds (agent file writes)")
        self._workspace_edit_sounds_check.setChecked(cfg.get("workspace_edit_sounds", True))
        self._workspace_edit_sounds_check.setToolTip(
            "Play sounds when the agent writes files via file_write, file_edit, "
            "apply_patch, or multi_edit."
        )
        ws_form.addRow("", self._workspace_edit_sounds_check)

        self._viewer_typing_sounds_check = QCheckBox("File viewer typing sounds")
        self._viewer_typing_sounds_check.setChecked(cfg.get("viewer_typing_sounds", True))
        self._viewer_typing_sounds_check.setToolTip(
            "While typing in the built-in file viewer, play rapid typing sounds."
        )
        ws_form.addRow("", self._viewer_typing_sounds_check)

        self._sound_exempt_edit = QPlainTextEdit()
        self._sound_exempt_edit.setFont(QFont("Consolas", 9))
        self._sound_exempt_edit.setPlaceholderText(
            "One pattern per line — matching paths stay silent for edit sounds.\n"
            "Examples:\n"
            "  /scripts*      any folder named scripts\n"
            "  flip-o-tron    folder name anywhere in the path\n"
            "  *.test.py      glob on filename\n"
            "  main.py        exact filename"
        )
        exempt_patterns = cfg.get("sound_exempt_patterns") or []
        if isinstance(exempt_patterns, str):
            exempt_patterns = [ln.strip() for ln in exempt_patterns.splitlines() if ln.strip()]
        self._sound_exempt_edit.setPlainText("\n".join(exempt_patterns))
        self._sound_exempt_edit.setFixedHeight(88)
        ws_form.addRow(QLabel("Mute patterns"), self._sound_exempt_edit)

        top_form.addRow(ws_box)

        self._tts_autoplay_check = QCheckBox("Use Voice")
        self._tts_autoplay_check.setChecked(cfg.get("tts_autoplay", True))
        top_form.addRow("", self._tts_autoplay_check)

        self._tts_backend_combo = QComboBox()
        self._tts_backend_combo.addItem("Microsoft Edge (online, free)", "edge")
        self._tts_backend_combo.addItem("ElevenLabs (online, premium API)", "elevenlabs")
        self._tts_backend_combo.addItem("Chatterbox (local, voice cloning)", "chatterbox")
        current_backend = (cfg.get("tts_backend") or "edge").lower()
        for i in range(self._tts_backend_combo.count()):
            if self._tts_backend_combo.itemData(i) == current_backend:
                self._tts_backend_combo.setCurrentIndex(i); break
        self._tts_backend_combo.currentIndexChanged.connect(self._on_tts_backend_changed)
        top_form.addRow(QLabel("Voice Engine"), self._tts_backend_combo)
        outer.addLayout(top_form)

        # Backend-specific sections (only one visible at a time)
        self._edge_section = self._build_edge_section(cfg)
        self._eleven_section = self._build_eleven_section(cfg)
        self._chatter_section = self._build_chatterbox_section(cfg)
        outer.addWidget(self._edge_section)
        outer.addWidget(self._eleven_section)
        outer.addWidget(self._chatter_section)
        outer.addStretch(1)

        self._on_tts_backend_changed(self._tts_backend_combo.currentIndex())
        return w

    def _on_tts_backend_changed(self, _idx):
        be = self._tts_backend_combo.currentData()
        self._edge_section.setVisible(be == "edge")
        self._eleven_section.setVisible(be == "elevenlabs")
        self._chatter_section.setVisible(be == "chatterbox")

    # ---- Edge section (existing controls, now wrapped) ----
    def _build_edge_section(self, cfg) -> QWidget:
        p = PALETTE
        box = QGroupBox("Microsoft Edge Neural Voices")
        layout = QFormLayout(box)
        layout.setSpacing(10)

        # Voice dropdown
        self._voice_combo = QComboBox()
        self._voice_combo.setFont(QFont("Consolas", 9))
        self._voice_combo.setMinimumWidth(250)
        current_voice = cfg.get("tts_voice", "en-US-AriaNeural")

        # Populate voices in background (async) — start with current
        self._voice_combo.addItem(current_voice, current_voice)
        self._voice_combo.setCurrentIndex(0)
        # Load full list async to not block the dialog
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(100, self._load_voices)

        layout.addRow(QLabel("Default Voice"), self._voice_combo)

        # Speed slider
        speed_row = QHBoxLayout()
        speed_row.setSpacing(8)
        self._tts_speed_slider = QSlider(Qt.Orientation.Horizontal)
        self._tts_speed_slider.setMinimum(-50)  # -50% slower
        self._tts_speed_slider.setMaximum(100)  # +100% faster
        self._tts_speed_slider.setValue(cfg.get("tts_speed", 10))
        self._tts_speed_slider.setTickInterval(25)
        self._tts_speed_slider.valueChanged.connect(self._on_tts_speed_changed)
        speed_row.addWidget(self._tts_speed_slider, stretch=1)
        self._tts_speed_label = QLabel(self._format_speed(cfg.get("tts_speed", 10)))
        self._tts_speed_label.setFixedWidth(50)
        self._tts_speed_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        speed_row.addWidget(self._tts_speed_label)
        layout.addRow(QLabel("Speed"), speed_row)


        # Preview + Rename row
        preview_row = QHBoxLayout()
        self._preview_btn = QPushButton("Preview")
        self._preview_btn.clicked.connect(self._preview_voice)
        preview_row.addWidget(self._preview_btn)

        self._rename_edit = QLineEdit()
        self._rename_edit.setFont(QFont("Consolas", 9))
        self._rename_edit.setPlaceholderText("Nickname for this voice")
        current_voice = cfg.get("tts_voice", "en-US-AriaNeural")
        nicknames = cfg.get("tts_voice_nicknames", {})
        self._rename_edit.setText(nicknames.get(current_voice, ""))
        preview_row.addWidget(self._rename_edit, stretch=1)

        rename_btn = QPushButton("Save Name")
        rename_btn.clicked.connect(self._save_voice_nickname)
        preview_row.addWidget(rename_btn)

        self._preview_status = QLabel("")
        self._preview_status.setFont(QFont("Consolas", 8))
        self._preview_status.setStyleSheet(f"color: {p['muted_text']}; background: transparent; border: none;")
        preview_row.addWidget(self._preview_status)
        layout.addRow("", preview_row)

        # Update nickname field when voice selection changes
        self._voice_combo.currentIndexChanged.connect(self._on_voice_selection_changed)

        return box

    # ---- ElevenLabs section ----
    def _build_eleven_section(self, cfg) -> QWidget:
        p = PALETTE
        box = QGroupBox("ElevenLabs")
        layout = QFormLayout(box)
        layout.setSpacing(10)

        # API key
        keys = load_keys()
        current_key = (keys.get("elevenlabs") or {}).get("api_key", "")
        self._eleven_key_edit = QLineEdit(current_key)
        self._eleven_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._eleven_key_edit.setPlaceholderText("xi-api-key from elevenlabs.io")
        show_btn = QPushButton("Show")
        show_btn.setCheckable(True)
        def _toggle_show():
            self._eleven_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if show_btn.isChecked() else QLineEdit.EchoMode.Password)
        show_btn.toggled.connect(_toggle_show)
        key_row = QHBoxLayout(); key_row.setSpacing(6)
        key_row.addWidget(self._eleven_key_edit, 1); key_row.addWidget(show_btn)
        layout.addRow(QLabel("API Key"), key_row)

        # Voice dropdown (populated via API when key is present)
        self._eleven_voice_combo = QComboBox()
        self._eleven_voice_combo.setFont(QFont("Consolas", 9))
        self._eleven_voice_combo.setMinimumWidth(260)
        saved_vid = cfg.get("elevenlabs_voice_id", "pNInz6obpgDQGcFmaJgB")
        self._eleven_voice_combo.addItem(saved_vid or "(default) Adam", saved_vid)
        refresh_btn = QPushButton("Refresh List")
        refresh_btn.clicked.connect(self._load_eleven_voices)
        v_row = QHBoxLayout(); v_row.setSpacing(6)
        v_row.addWidget(self._eleven_voice_combo, 1); v_row.addWidget(refresh_btn)
        layout.addRow(QLabel("Voice"), v_row)

        # Preview
        self._eleven_preview_btn = QPushButton("Preview")
        self._eleven_preview_btn.clicked.connect(self._preview_eleven)
        self._eleven_status = QLabel("")
        self._eleven_status.setFont(QFont("Consolas", 8))
        self._eleven_status.setStyleSheet(f"color: {p['muted_text']}; background: transparent; border: none;")
        pv_row = QHBoxLayout(); pv_row.setSpacing(6)
        pv_row.addWidget(self._eleven_preview_btn); pv_row.addWidget(self._eleven_status, 1)
        layout.addRow("", pv_row)

        return box

    def _load_eleven_voices(self):
        """Fetch voice list from ElevenLabs using the key currently typed."""
        import threading, json
        key_typed = self._eleven_key_edit.text().strip()
        if not key_typed:
            self._eleven_status.setText("Enter an API key first.")
            return
        self._eleven_status.setText("Fetching voices...")

        def _go():
            # Temporarily save the key so the backend picks it up
            try:
                keys = load_keys()
                keys.setdefault("elevenlabs", {})["api_key"] = key_typed
                save_keys(keys)
                from tools.tts_backends import elevenlabs_backend
                voices = elevenlabs_backend.list_voices()
                self._eleven_voices_fetched = voices
            except Exception as e:
                self._eleven_voices_fetched = []
                self._eleven_voices_error = str(e)
        threading.Thread(target=_go, daemon=True).start()
        from PyQt6.QtCore import QTimer
        self._eleven_poll = QTimer(self)
        self._eleven_poll.timeout.connect(self._check_eleven_voices_loaded)
        self._eleven_poll.start(250)

    def _check_eleven_voices_loaded(self):
        if not hasattr(self, "_eleven_voices_fetched"):
            return
        self._eleven_poll.stop()
        voices = self._eleven_voices_fetched
        if not voices:
            self._eleven_status.setText(getattr(self, "_eleven_voices_error",
                                                "No voices (check key / network).") or "No voices.")
            return
        current = self._eleven_voice_combo.currentData()
        self._eleven_voice_combo.blockSignals(True)
        self._eleven_voice_combo.clear()
        for v in voices:
            vid = v.get("voice_id", ""); name = v.get("name", vid)
            cat = v.get("category", "")
            label = f"{name}  [{cat}]" if cat else name
            self._eleven_voice_combo.addItem(label, vid)
        for i in range(self._eleven_voice_combo.count()):
            if self._eleven_voice_combo.itemData(i) == current:
                self._eleven_voice_combo.setCurrentIndex(i); break
        self._eleven_voice_combo.blockSignals(False)
        self._eleven_status.setText(f"Loaded {len(voices)} voices.")

    def _preview_eleven(self):
        import threading, json
        self._eleven_status.setText("Generating...")
        self._eleven_preview_btn.setEnabled(False)
        # Persist key + voice so the backend reads them
        keys = load_keys()
        keys.setdefault("elevenlabs", {})["api_key"] = self._eleven_key_edit.text().strip()
        save_keys(keys)
        cfg = load_config()
        cfg["tts_backend"] = "elevenlabs"
        cfg["elevenlabs_voice_id"] = self._eleven_voice_combo.currentData() or ""
        save_config(cfg)

        def _go():
            try:
                from tools.tts import text_to_speech
                r = json.loads(text_to_speech("Hello! This is ElevenLabs speaking.", play=True))
                self._eleven_preview_result = r.get("error") or "Playing..."
            except Exception as e:
                self._eleven_preview_result = f"Error: {e}"
        threading.Thread(target=_go, daemon=True).start()
        from PyQt6.QtCore import QTimer
        self._eleven_prev_poll = QTimer(self)
        self._eleven_prev_poll.timeout.connect(self._check_eleven_preview_done)
        self._eleven_prev_poll.start(250)

    def _check_eleven_preview_done(self):
        if not hasattr(self, "_eleven_preview_result"):
            return
        self._eleven_prev_poll.stop()
        self._eleven_status.setText(self._eleven_preview_result)
        self._eleven_preview_btn.setEnabled(True)
        del self._eleven_preview_result

    # ---- Chatterbox section ----
    def _build_chatterbox_section(self, cfg) -> QWidget:
        p = PALETTE
        box = QGroupBox("Chatterbox (local)")
        self._chatter_box = box  # so we can blink it
        layout = QFormLayout(box)
        layout.setSpacing(10)

        from tools.tts_backends import chatterbox_installer as inst
        installed = inst.is_installed()

        self._chatter_status_label = QLabel()
        self._chatter_status_label.setFont(QFont("Consolas", 9))
        # Show cheap state immediately; run the heavy GPU probe in a thread.
        self._chatter_set_status_label(installed, False, "detecting...", -1.0)
        layout.addRow(QLabel("Status"), self._chatter_status_label)
        self._chatter_probe_status(installed)

        # Voice reference library: dropdown of files in data/voice_refs/, plus
        # an Import button that copies a chosen audio file into that folder.
        self._chatter_ref_combo = QComboBox()
        self._chatter_ref_combo.setFont(QFont("Consolas", 9))
        self._chatter_ref_combo.setMinimumWidth(250)
        self._chatter_reload_voice_refs(cfg.get("chatterbox_voice_ref", ""))

        import_btn = QPushButton("Import...")
        open_folder_btn = QPushButton("Open Folder")

        def _import_clip():
            path, _ = QFileDialog.getOpenFileName(self, "Import reference voice clip", "",
                                                  "Audio (*.wav *.mp3 *.flac *.ogg)")
            if not path:
                return
            try:
                import shutil as _sh
                from tools.tts_backends.voice_refs import VOICE_REFS_DIR, ensure_dir
                ensure_dir()
                dst = VOICE_REFS_DIR / Path(path).name
                # Avoid overwriting: append _2, _3, ... if collision
                stem, suf = dst.stem, dst.suffix
                i = 2
                while dst.exists():
                    dst = VOICE_REFS_DIR / f"{stem}_{i}{suf}"
                    i += 1
                _sh.copy2(path, dst)
                self._chatter_reload_voice_refs(str(dst))
                self._chatter_phase_label.setText(f"Imported {dst.name}")
            except Exception as e:
                self._chatter_phase_label.setText(f"Import failed: {e}")

        def _open_folder():
            try:
                from tools.tts_backends.voice_refs import VOICE_REFS_DIR, ensure_dir
                ensure_dir()
                if sys.platform == "win32":
                    os.startfile(str(VOICE_REFS_DIR))
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", str(VOICE_REFS_DIR)])
                else:
                    subprocess.Popen(["xdg-open", str(VOICE_REFS_DIR)])
            except Exception as e:
                self._chatter_phase_label.setText(f"Open folder failed: {e}")

        import_btn.clicked.connect(_import_clip)
        open_folder_btn.clicked.connect(_open_folder)
        ref_row = QHBoxLayout(); ref_row.setSpacing(6)
        ref_row.addWidget(self._chatter_ref_combo, 1)
        ref_row.addWidget(import_btn)
        ref_row.addWidget(open_folder_btn)
        layout.addRow(QLabel("Voice Clip"), ref_row)

        # Install / Uninstall / Preview row
        self._chatter_install_btn = QPushButton("Install Chatterbox..." if not installed else "Reinstall...")
        self._chatter_install_btn.clicked.connect(self._chatter_prompt_install)
        self._chatter_uninstall_btn = QPushButton("Uninstall")
        self._chatter_uninstall_btn.clicked.connect(self._chatter_uninstall)
        self._chatter_uninstall_btn.setEnabled(installed)
        self._chatter_preview_btn = QPushButton("Preview")
        self._chatter_preview_btn.clicked.connect(self._preview_chatter)
        self._chatter_preview_btn.setEnabled(installed)
        btn_row = QHBoxLayout(); btn_row.setSpacing(6)
        btn_row.addWidget(self._chatter_install_btn)
        btn_row.addWidget(self._chatter_uninstall_btn)
        btn_row.addWidget(self._chatter_preview_btn)
        btn_row.addStretch(1)
        layout.addRow("", btn_row)

        # Load / Unload model row (runtime memory control)
        self._chatter_load_label = QLabel("")
        self._chatter_load_label.setFont(QFont("Consolas", 9))
        self._chatter_load_btn = QPushButton("Load Model")
        self._chatter_load_btn.clicked.connect(self._chatter_load_model)
        self._chatter_unload_btn = QPushButton("Unload Model")
        self._chatter_unload_btn.clicked.connect(self._chatter_unload_model)
        load_row = QHBoxLayout(); load_row.setSpacing(6)
        load_row.addWidget(self._chatter_load_btn)
        load_row.addWidget(self._chatter_unload_btn)
        load_row.addWidget(self._chatter_load_label, 1)
        layout.addRow(QLabel("Model"), load_row)
        self._chatter_refresh_load_state()
        from PyQt6.QtCore import QTimer
        self._chatter_load_poll = QTimer(self)
        self._chatter_load_poll.timeout.connect(self._chatter_refresh_load_state)
        self._chatter_load_poll.start(2000)

        # Progress label + bar
        self._chatter_phase_label = QLabel("")
        self._chatter_phase_label.setFont(QFont("Consolas", 8))
        self._chatter_phase_label.setStyleSheet(
            f"color: {p['muted_text']}; background: transparent; border: none;")
        layout.addRow("", self._chatter_phase_label)

        from PyQt6.QtWidgets import QProgressBar
        self._chatter_progress = QProgressBar()
        self._chatter_progress.setRange(0, 100)
        self._chatter_progress.setValue(0)
        self._chatter_progress.setTextVisible(True)
        self._chatter_progress.setFixedHeight(14)
        self._chatter_progress.setStyleSheet(f"""
            QProgressBar {{
                background: {p['panel_alt'] if 'panel_alt' in p else p['panel']};
                border: 1px solid {p['border']};
                border-radius: 3px;
                color: {p['text']};
                text-align: center;
                font-family: Consolas, monospace;
                font-size: 8pt;
            }}
            QProgressBar::chunk {{
                background: {p['accent']};
                border-radius: 2px;
            }}
        """)
        self._chatter_progress.setVisible(False)
        layout.addRow("", self._chatter_progress)

        self._chatter_installer = None
        return box

    def _chatter_probe_status(self, installed: bool):
        """Run the GPU/disk probe off-thread so it can't stall the dialog."""
        import threading
        def _probe():
            from tools.tts_backends import chatterbox_installer as inst
            hg, name = inst.has_gpu()
            free = inst.free_disk_gb()
            self._chatter_probe_result = (installed, hg, name, free)
        threading.Thread(target=_probe, daemon=True).start()
        from PyQt6.QtCore import QTimer
        self._chatter_probe_poll = QTimer(self)
        self._chatter_probe_poll.timeout.connect(self._chatter_check_probe)
        self._chatter_probe_poll.start(200)

    def _chatter_check_probe(self):
        if not hasattr(self, "_chatter_probe_result"):
            return
        self._chatter_probe_poll.stop()
        installed, hg, name, free = self._chatter_probe_result
        self._chatter_set_status_label(installed, hg, name, free)
        del self._chatter_probe_result

    def _chatter_set_status_label(self, installed, has_gpu, gpu_name, free_gb):
        p = PALETTE
        lines = []
        if installed:
            lines.append("✓ Installed")
            color = p.get("glow_cool", "#61d0ff")
        else:
            lines.append("Not installed (about 4 GB download)")
            color = p.get("muted_text", "#888")
        if has_gpu:
            lines.append(f"GPU: {gpu_name or 'detected'}")
        else:
            lines.append("GPU: not detected (will run on CPU, slow)")
        if free_gb > 0:
            lines.append(f"Disk free: {free_gb:.1f} GB")
        self._chatter_status_label.setText("   |   ".join(lines))
        self._chatter_status_label.setStyleSheet(
            f"color: {color}; background: transparent; border: none;")

    def _chatter_prompt_install(self):
        from tools.tts_backends import chatterbox_installer as inst
        has_gpu, gpu_name = inst.has_gpu()
        free_gb = inst.free_disk_gb()
        body = (f"Chatterbox will download about 4 GB of Python components and voice "
                f"model weights into the Agent folder (data/chatterbox_env and "
                f"data/chatterbox_models). This runs entirely on your machine.\n\n"
                f"Disk free: {free_gb:.1f} GB\n"
                f"GPU: {gpu_name or 'not detected — will run on CPU (slow)'}\n\n"
                f"Proceed?")
        r = QMessageBox.question(self, "Install Chatterbox?", body,
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self._chatter_start_install()

    def _chatter_start_install(self):
        from tools.tts_backends.chatterbox_installer import ChatterboxInstaller
        self._chatter_installer = ChatterboxInstaller(self)
        self._chatter_installer.progress.connect(self._on_chatter_progress)
        self._chatter_installer.log.connect(self._on_chatter_log)
        self._chatter_installer.finished.connect(self._on_chatter_finished)
        self._chatter_install_btn.setEnabled(False)
        self._chatter_uninstall_btn.setEnabled(False)
        self._chatter_preview_btn.setEnabled(False)
        self._chatter_progress.setVisible(True)
        self._chatter_progress.setValue(0)
        self._chatter_phase_label.setText("Starting...")
        self._chatter_installer.start()

    def _on_chatter_progress(self, phase: str, pct: int):
        self._chatter_phase_label.setText(phase)
        if pct >= 0:
            self._chatter_progress.setRange(0, 100)
            self._chatter_progress.setValue(pct)
        else:
            self._chatter_progress.setRange(0, 0)  # indeterminate

    def _on_chatter_log(self, line: str):
        """Show the latest installer output line so the user sees live activity
        (pip 'Downloading…', heartbeats) instead of a frozen bar."""
        line = (line or "").strip()
        if line:
            self._chatter_phase_label.setText(line[-100:])

    def _on_chatter_finished(self, ok: bool, err: str):
        from tools.tts_backends import chatterbox_installer as inst
        self._chatter_install_btn.setEnabled(True)
        self._chatter_install_btn.setText("Reinstall..." if ok else "Install Chatterbox...")
        self._chatter_uninstall_btn.setEnabled(inst.is_installed())
        self._chatter_preview_btn.setEnabled(inst.is_installed())
        self._chatter_progress.setRange(0, 100)
        self._chatter_progress.setValue(100 if ok else 0)
        self._chatter_phase_label.setText("Done ✓" if ok else f"Failed: {err}")
        has_gpu, gpu_name = inst.has_gpu()
        self._chatter_set_status_label(inst.is_installed(), has_gpu, gpu_name, inst.free_disk_gb())
        if ok:
            try:
                from core.sounds import play_ui
                play_ui("alert.mp3")
            except Exception:
                pass
            self._blink_chatter_ready()

    def _blink_chatter_ready(self):
        """Flash the Chatterbox groupbox border a few times to signal completion."""
        from PyQt6.QtCore import QTimer
        p = PALETTE
        self._chatter_blink_i = 0
        self._chatter_blink_timer = QTimer(self)

        def _tick():
            self._chatter_blink_i += 1
            hot = self._chatter_blink_i % 2 == 1
            col = p.get("glow_hot", "#ff6b6b") if hot else p.get("glow_cool", "#61d0ff")
            self._chatter_box.setStyleSheet(
                f"QGroupBox {{ border: 2px solid {col}; border-radius: 4px; margin-top: 8px; }}"
                f"QGroupBox::title {{ subcontrol-origin: margin; left: 8px; padding: 0 4px; }}")
            if self._chatter_blink_i >= 8:
                self._chatter_blink_timer.stop()
                self._chatter_box.setStyleSheet("")
        self._chatter_blink_timer.timeout.connect(_tick)
        self._chatter_blink_timer.start(400)

    def _chatter_uninstall(self):
        r = QMessageBox.question(self, "Uninstall Chatterbox?",
                                 "This removes data/chatterbox_env and data/chatterbox_models "
                                 "(frees ~4 GB). Continue?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r != QMessageBox.StandardButton.Yes:
            return
        from tools.tts_backends import chatterbox_installer as inst
        inst.uninstall()
        has_gpu, gpu_name = inst.has_gpu()
        self._chatter_set_status_label(False, has_gpu, gpu_name, inst.free_disk_gb())
        self._chatter_install_btn.setText("Install Chatterbox...")
        self._chatter_uninstall_btn.setEnabled(False)
        self._chatter_preview_btn.setEnabled(False)
        self._chatter_phase_label.setText("Uninstalled.")

    def _chatter_reload_voice_refs(self, selected_path: str = ""):
        """Rebuild the voice-ref dropdown from data/voice_refs/. The "Default"
        entry sits at index 0 (empty value = use Chatterbox's built-in voice).
        If `selected_path` matches a file in the folder, that entry is selected.
        If it's an absolute path outside the folder (legacy config), it's kept
        as a one-off entry labeled '[external]'."""
        from tools.tts_backends.voice_refs import list_voice_refs, VOICE_REFS_DIR
        self._chatter_ref_combo.blockSignals(True)
        self._chatter_ref_combo.clear()
        self._chatter_ref_combo.addItem("(Default voice — no clone)", "")
        for p in list_voice_refs():
            self._chatter_ref_combo.addItem(p.name, str(p))
        # Legacy path support: keep a file that lives outside the folder as a
        # one-off entry so the user's existing config doesn't get silently dropped.
        if selected_path:
            sp = Path(selected_path)
            try:
                is_inside = sp.resolve().parent == VOICE_REFS_DIR.resolve()
            except Exception:
                is_inside = False
            if sp.exists() and not is_inside:
                self._chatter_ref_combo.addItem(f"{sp.name}  [external]", str(sp))
        # Restore selection
        target = selected_path or ""
        for i in range(self._chatter_ref_combo.count()):
            if self._chatter_ref_combo.itemData(i) == target:
                self._chatter_ref_combo.setCurrentIndex(i); break
        self._chatter_ref_combo.blockSignals(False)

    def _chatter_refresh_load_state(self):
        """Poll daemon state and update the Load/Unload row."""
        try:
            from tools.tts_backends import chatterbox_backend as cb
            from tools.tts_backends import chatterbox_installer as inst
        except Exception:
            return
        loaded = cb.is_loaded()
        installed = inst.is_installed()
        self._chatter_load_btn.setEnabled(installed and not loaded
                                          and not getattr(self, "_chatter_loading", False))
        self._chatter_unload_btn.setEnabled(installed and loaded)
        p = PALETTE
        if getattr(self, "_chatter_loading", False):
            txt = "Loading (first load ~15–30s)…"
            color = p.get("muted_text", "#888")
        elif loaded:
            txt = "● Loaded — uses ~2 GB VRAM (or ~4–6 GB RAM on CPU)"
            color = p.get("glow_cool", "#61d0ff")
        elif installed:
            txt = "○ Not loaded — loads on first TTS call"
            color = p.get("muted_text", "#888")
        else:
            txt = ""
            color = p.get("muted_text", "#888")
        self._chatter_load_label.setText(txt)
        self._chatter_load_label.setStyleSheet(
            f"color: {color}; background: transparent; border: none;")

    def _chatter_load_model(self):
        """Pre-warm the daemon in a thread so the UI stays responsive."""
        import threading
        self._chatter_loading = True
        self._chatter_refresh_load_state()

        def _go():
            try:
                from tools.tts_backends import chatterbox_backend as cb
                ok, err = cb.load()
                self._chatter_load_result = (ok, err)
            except Exception as e:
                self._chatter_load_result = (False, str(e))
        threading.Thread(target=_go, daemon=True).start()
        from PyQt6.QtCore import QTimer
        self._chatter_load_result_poll = QTimer(self)
        self._chatter_load_result_poll.timeout.connect(self._chatter_check_load_done)
        self._chatter_load_result_poll.start(300)

    def _chatter_check_load_done(self):
        if not hasattr(self, "_chatter_load_result"):
            return
        self._chatter_load_result_poll.stop()
        ok, err = self._chatter_load_result
        del self._chatter_load_result
        self._chatter_loading = False
        self._chatter_refresh_load_state()
        if not ok and err:
            self._chatter_phase_label.setText(f"Load failed: {err[:120]}")

    def _chatter_unload_model(self):
        try:
            from tools.tts_backends import chatterbox_backend as cb
            cb.unload()
        except Exception as e:
            self._chatter_phase_label.setText(f"Unload error: {e}")
        self._chatter_refresh_load_state()

    def _preview_chatter(self):
        import threading, json
        self._chatter_phase_label.setText("Generating...")
        self._chatter_preview_btn.setEnabled(False)
        cfg = load_config()
        cfg["tts_backend"] = "chatterbox"
        cfg["chatterbox_voice_ref"] = self._chatter_ref_combo.currentData() or ""
        save_config(cfg)

        def _go():
            try:
                from tools.tts import text_to_speech
                r = json.loads(text_to_speech("Hello, this is Chatterbox running locally.",
                                              play=True))
                self._chatter_preview_result = r.get("error") or "Playing..."
            except Exception as e:
                self._chatter_preview_result = f"Error: {e}"
        threading.Thread(target=_go, daemon=True).start()
        from PyQt6.QtCore import QTimer
        self._chatter_prev_poll = QTimer(self)
        self._chatter_prev_poll.timeout.connect(self._check_chatter_preview_done)
        self._chatter_prev_poll.start(300)

    def _check_chatter_preview_done(self):
        if not hasattr(self, "_chatter_preview_result"):
            return
        self._chatter_prev_poll.stop()
        self._chatter_phase_label.setText(self._chatter_preview_result)
        self._chatter_preview_btn.setEnabled(True)
        del self._chatter_preview_result

    @staticmethod
    def _format_speed(val: int) -> str:
        if val == 0:
            return "Normal"
        return f"+{val}%" if val > 0 else f"{val}%"

    def _on_tts_speed_changed(self, val):
        self._tts_speed_label.setText(self._format_speed(val))

    def _save_voice_nickname(self):
        """Save a custom nickname for the currently selected voice."""
        voice_id = self._voice_combo.currentData()
        nickname = self._rename_edit.text().strip()
        if not voice_id:
            return
        cfg = load_config()
        nicknames = cfg.get("tts_voice_nicknames", {})
        if nickname:
            nicknames[voice_id] = nickname
        else:
            nicknames.pop(voice_id, None)
        cfg["tts_voice_nicknames"] = nicknames
        save_config(cfg)
        # Update the dropdown label for this voice
        idx = self._voice_combo.currentIndex()
        if idx >= 0:
            base = self._voice_base_labels.get(voice_id, voice_id)
            label = f"{nickname}  [{base}]" if nickname else base
            self._voice_combo.setItemText(idx, label)
        self._preview_status.setText("Name saved")

    def _on_voice_selection_changed(self, idx):
        """Update nickname field when a different voice is selected."""
        if idx < 0:
            return
        voice_id = self._voice_combo.itemData(idx)
        cfg = load_config()
        nicknames = cfg.get("tts_voice_nicknames", {})
        self._rename_edit.setText(nicknames.get(voice_id, ""))

    def _load_voices(self):
        """Load TTS voices in background thread."""
        import threading

        def _fetch():
            try:
                import asyncio
                import edge_tts

                async def _list():
                    return await edge_tts.list_voices()

                voices = asyncio.run(_list())
                en_voices = [v for v in voices if v["Locale"].startswith("en-")]
                # Sort: US first, then by name
                en_voices.sort(key=lambda v: (0 if v["Locale"].startswith("en-US") else 1, v["ShortName"]))
                self._fetched_voices = en_voices
            except Exception:
                self._fetched_voices = []

        t = threading.Thread(target=_fetch, daemon=True)
        t.start()
        # Poll for completion
        from PyQt6.QtCore import QTimer
        self._voice_poll = QTimer(self)
        self._voice_poll.timeout.connect(self._check_voices_loaded)
        self._voice_poll.start(200)

    def _check_voices_loaded(self):
        if not hasattr(self, "_fetched_voices"):
            return
        self._voice_poll.stop()
        voices = self._fetched_voices
        current = self._voice_combo.currentData()
        cfg = load_config()
        nicknames = cfg.get("tts_voice_nicknames", {})
        self._voice_base_labels = {}  # voice_id -> base display label
        self._voice_combo.blockSignals(True)
        self._voice_combo.clear()
        for v in voices:
            vid = v["ShortName"]
            base = f"{vid}  ({v['Gender']}, {v['Locale']})"
            self._voice_base_labels[vid] = base
            nick = nicknames.get(vid, "")
            label = f"{nick}  [{base}]" if nick else base
            self._voice_combo.addItem(label, vid)
        # Restore selection
        for i in range(self._voice_combo.count()):
            if self._voice_combo.itemData(i) == current:
                self._voice_combo.setCurrentIndex(i)
                break
        self._voice_combo.blockSignals(False)
        # Update nickname field for current selection
        self._on_voice_selection_changed(self._voice_combo.currentIndex())

    def _preview_voice(self):
        """Generate and play a short preview of the selected voice at current speed."""
        voice = self._voice_combo.currentData()
        if not voice:
            return
        self._preview_status.setText("Generating...")
        self._preview_btn.setEnabled(False)
        speed = self._tts_speed_slider.value()

        import threading

        def _gen():
            try:
                from tools.tts import text_to_speech, _load_tts_config
                import json
                # Temporarily write speed so the tool picks it up
                from core.agent import load_config, save_config
                cfg = load_config()
                cfg["tts_speed"] = speed
                save_config(cfg)
                result = json.loads(text_to_speech(
                    "Hello! I'm your AI assistant. This is how I sound.",
                    voice=voice, play=True))
                if "error" in result:
                    self._preview_result = f"Error: {result['error']}"
                else:
                    self._preview_result = "Playing..."
            except Exception as e:
                self._preview_result = f"Error: {e}"

        t = threading.Thread(target=_gen, daemon=True)
        t.start()
        from PyQt6.QtCore import QTimer
        self._preview_poll = QTimer(self)
        self._preview_poll.timeout.connect(self._check_preview_done)
        self._preview_poll.start(200)

    def _check_preview_done(self):
        if not hasattr(self, "_preview_result"):
            return
        self._preview_poll.stop()
        self._preview_status.setText(self._preview_result)
        self._preview_btn.setEnabled(True)
        del self._preview_result

    # ------------------------------------------------------------------
    # Tools tab
    # ------------------------------------------------------------------

    def _build_tools_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(10, 10, 10, 10)
        from tools.registry import registry
        from core import tool_stats
        p = PALETTE

        tools = registry.list_tools()
        stats = tool_stats.get_all()

        # Map every LLM-touching tool to its config keys.
        # Format: tool_name -> (model_key, provider_key | None, default, kind)
        #   kind: "llm"        — chat-completion model, model + provider editable
        #         "embedding"  — embeddings model, model only (provider auto-routed)
        #         "shared"     — read-only display, points at another tool's keys
        self._tool_llm_config_map = {
            # Direct chat-completion users
            "vision":        ("vision_model", "vision_provider", "", "llm"),
            "subagent":      ("subagent_model", "subagent_provider", "", "llm"),
            # Read-only in the table — edited via Models → Explore Model (avoids
            # a second QLineEdit that overwrote the saved value on dialog close).
            "explore_files": ("subagent_explore_model", "subagent_explore_provider",
                              "claude-haiku-4-5", "shared"),
            "memory":        ("memory_model", "memory_provider", "", "llm"),
            # Embeddings (no provider — auto-routed to OpenRouter or OpenAI by core/embeddings.py)
            "vector_search": ("embedding_model", None, "openai/text-embedding-3-small", "embedding"),
            "session_search": ("embedding_model", None, "openai/text-embedding-3-small", "embedding"),
            # Shared / fallback users — display the borrowed model, no inline edit
            "ocr":           ("vision_model", "vision_provider", "", "shared"),
        }
        cfg = load_config()
        disabled_set = set(cfg.get("disabled_tools", []))

        # Header row
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 6)
        t = tool_stats.totals()
        summary_label = QLabel(
            f"Tools: {len(tools)}  |  Total calls: {t['calls']:,}  |  "
            f"Tokens in: {t['tokens_in']:,}  |  Tokens out: {t['tokens_out']:,}  |  "
            f"Errors: {t['errors']:,}"
        )
        summary_label.setStyleSheet(f"color: {p['muted_text']};")
        header_row.addWidget(summary_label, stretch=1)
        reset_btn = QPushButton("Reset Stats")
        reset_btn.setFixedHeight(22)
        reset_btn.clicked.connect(self._on_reset_tool_stats)
        header_row.addWidget(reset_btn)
        layout.addLayout(header_row)

        cols = ["Tool", "Calls", "Tokens In", "Tokens Out", "Errors", "Model", "Provider", "On"]
        table = QTableWidget(len(tools) if tools else 1, len(cols))
        table.setHorizontalHeaderLabels(cols)
        table.setFont(QFont("Consolas", 9))
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.verticalHeader().setVisible(False)
        table.setSortingEnabled(True)
        table.setStyleSheet(f"""
            QTableWidget {{
                background: {p['panel_alt']};
                border: 1px solid {p['border']};
                color: {p['text']};
                gridline-color: {p['border']};
            }}
            QTableWidget::item {{ padding: 3px 6px; }}
            QTableWidget::item:selected {{
                background: rgba({QColor(p['accent']).red()}, {QColor(p['accent']).green()}, {QColor(p['accent']).blue()}, 0.20);
                color: {p['accent']};
            }}
            QHeaderView::section {{
                background: {p['panel']};
                color: {p['muted_text']};
                padding: 4px 6px;
                border: 0px;
                border-right: 1px solid {p['border']};
                border-bottom: 1px solid {p['border']};
            }}
        """)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for i in range(1, 5):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)

        # Track edit widgets so _save_and_close can persist any changes
        self._tool_model_edits: dict[str, QLineEdit] = {}
        self._tool_provider_combos: dict[str, QComboBox] = {}
        # name -> QCheckBox for the per-tool enable toggle ("On" column).
        self._tool_enable_checks: dict[str, QCheckBox] = {}

        if not tools:
            table.setItem(0, 0, QTableWidgetItem("No tools registered yet."))
            for c in range(1, len(cols)):
                table.setItem(0, c, QTableWidgetItem(""))
        else:
            table.setSortingEnabled(False)  # populate then re-enable
            for row, tool in enumerate(sorted(tools, key=lambda x: x["name"])):
                name = tool["name"]
                s = stats.get(name, {})

                # Numeric columns — use NumericItem subclass for correct sort
                table.setItem(row, 0, _ToolNameItem(name, tool.get("description", "")))
                table.setItem(row, 1, _NumItem(s.get("calls", 0)))
                table.setItem(row, 2, _NumItem(s.get("tokens_in", 0)))
                table.setItem(row, 3, _NumItem(s.get("tokens_out", 0)))
                table.setItem(row, 4, _NumItem(s.get("errors", 0)))

                # "On" column — per-tool enable toggle. ALWAYS_ON tools are
                # shown checked + disabled so the agent can't be bricked.
                from tools.registry import ALWAYS_ON
                chk = QCheckBox()
                chk.setChecked(name not in disabled_set)
                if name in ALWAYS_ON:
                    chk.setChecked(True)
                    chk.setEnabled(False)
                    chk.setToolTip("Core tool — always on.")
                else:
                    chk.setToolTip("Uncheck to hide this tool from the model "
                                   "(saves schema tokens).")
                cell = QWidget()
                cell_lay = QHBoxLayout(cell)
                cell_lay.setContentsMargins(0, 0, 0, 0)
                cell_lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
                cell_lay.addWidget(chk)
                table.setCellWidget(row, 7, cell)
                self._tool_enable_checks[name] = chk

                # Model + Provider — depends on the tool's kind
                cfg_entry = self._tool_llm_config_map.get(name)
                if cfg_entry is None:
                    dash = QTableWidgetItem("—")
                    dash.setForeground(QColor(p['muted_text']))
                    table.setItem(row, 5, dash)
                    dash2 = QTableWidgetItem("—")
                    dash2.setForeground(QColor(p['muted_text']))
                    table.setItem(row, 6, dash2)
                    continue

                mkey, pkey, default_model, kind = cfg_entry

                if kind == "llm":
                    # Editable model + provider combo
                    model_edit = QLineEdit(cfg.get(mkey, default_model))
                    model_edit.setStyleSheet(
                        f"background:{p['panel']}; color:{p['text']}; "
                        f"border:1px solid {p['border']}; padding:2px;"
                    )
                    table.setCellWidget(row, 5, model_edit)
                    self._tool_model_edits[name] = model_edit

                    pcombo = QComboBox()
                    for prov_id, prov_info in PROVIDER_INFO.items():
                        pcombo.addItem(prov_info["name"], prov_id)
                    cur_prov = (cfg.get(pkey) or self.agent.provider).strip()
                    if cur_prov in PROVIDER_INFO:
                        pcombo.setCurrentIndex(
                            list(PROVIDER_INFO.keys()).index(cur_prov))
                    table.setCellWidget(row, 6, pcombo)
                    self._tool_provider_combos[name] = pcombo

                elif kind == "embedding":
                    # Editable model, provider shows "auto"
                    model_edit = QLineEdit(cfg.get(mkey, default_model))
                    model_edit.setStyleSheet(
                        f"background:{p['panel']}; color:{p['text']}; "
                        f"border:1px solid {p['border']}; padding:2px;"
                    )
                    model_edit.setToolTip(
                        "Embedding model. Provider is auto-routed (OpenRouter "
                        "if you have a key, else OpenAI)."
                    )
                    table.setCellWidget(row, 5, model_edit)
                    self._tool_model_edits[name] = model_edit
                    auto = QTableWidgetItem("auto")
                    auto.setForeground(QColor(p['muted_text']))
                    auto.setToolTip("Auto-routed: OpenRouter > OpenAI")
                    table.setItem(row, 6, auto)

                elif kind == "shared":
                    # Read-only display — point user at the canonical tool's row
                    shared_model = cfg.get(mkey, default_model) or "(unset)"
                    item = QTableWidgetItem(shared_model)
                    item.setForeground(QColor(p['muted_text']))
                    item.setToolTip(
                        f"Read-only — uses the same model as another tool "
                        f"(config key: {mkey}). Edit there to change."
                    )
                    table.setItem(row, 5, item)
                    if pkey:
                        prov_label = cfg.get(pkey, self.agent.provider) or self.agent.provider
                        pitem = QTableWidgetItem(prov_label)
                        pitem.setForeground(QColor(p['muted_text']))
                        pitem.setToolTip(
                            f"Read-only — provider for {mkey} (config key: {pkey})."
                        )
                        table.setItem(row, 6, pitem)
                    else:
                        dash2 = QTableWidgetItem("—")
                        dash2.setForeground(QColor(p['muted_text']))
                        table.setItem(row, 6, dash2)
            table.setSortingEnabled(True)

        layout.addWidget(table, stretch=1)

        # ── Tool self-audit settings ──
        cfg = load_config()
        audit_group = QGroupBox("Tool Self-Audit")
        audit_group.setFont(QFont("Consolas", 9))
        audit_group.setStyleSheet(f"""
            QGroupBox {{
                color: {p['muted_text']};
                border: 1px solid {p['border']};
                margin-top: 12px;
                padding: 10px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }}
        """)
        audit_form = QFormLayout(audit_group)
        audit_form.setSpacing(8)

        self._audit_enabled_check = QCheckBox("Enable self-audit on repeated tool failures")
        self._audit_enabled_check.setChecked(cfg.get("tool_audit_enabled", False))
        audit_form.addRow(self._audit_enabled_check)

        self._audit_threshold_spin = QSpinBox()
        self._audit_threshold_spin.setRange(2, 20)
        self._audit_threshold_spin.setValue(cfg.get("tool_audit_threshold", 3))
        self._audit_threshold_spin.setSuffix(" failures")
        audit_form.addRow("Trigger after:", self._audit_threshold_spin)

        self._audit_conv_combo = QComboBox()
        self._audit_conv_combo.setMinimumWidth(300)
        from core.database import list_conversations
        convs = list_conversations()
        current_target = cfg.get("tool_audit_target_conv", "")
        for conv in convs:
            label = conv.get("name", conv["id"][:8])
            self._audit_conv_combo.addItem(label, conv["id"])
        # Select the saved target if it exists
        idx = self._audit_conv_combo.findData(current_target)
        if idx >= 0:
            self._audit_conv_combo.setCurrentIndex(idx)
        audit_form.addRow("Route findings to:", self._audit_conv_combo)

        layout.addWidget(audit_group)

        # ── Tool result truncation ──
        trunc_group = QGroupBox("Tool Result Truncation")
        trunc_group.setFont(QFont("Consolas", 9))
        trunc_group.setStyleSheet(f"""
            QGroupBox {{
                color: {p['muted_text']};
                border: 1px solid {p['border']};
                margin-top: 12px;
                padding: 10px;
            }}
            QGroupBox::title {{
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }}
        """)
        trunc_vlayout = QVBoxLayout(trunc_group)
        trunc_vlayout.setSpacing(8)

        trunc_form = QFormLayout()
        trunc_form.setSpacing(6)

        self._max_tool_result_spin = QSpinBox()
        self._max_tool_result_spin.setRange(1000, 200000)
        self._max_tool_result_spin.setSingleStep(1000)
        self._max_tool_result_spin.setSuffix(" chars")
        self._max_tool_result_spin.setValue(cfg.get("max_tool_result", 12000))
        self._max_tool_result_spin.setToolTip(
            "Global cap applied to every tool result before it enters the context window.\n"
            "12 000 chars (~3K tokens) matches the claw-code-agent default.\n"
            "Per-tool overrides below take precedence when set."
        )
        trunc_form.addRow("Global cap:", self._max_tool_result_spin)
        trunc_vlayout.addLayout(trunc_form)

        # Per-tool overrides table
        overrides_label = QLabel("Per-tool overrides  (tool name → cap chars; leave blank to use global):")
        overrides_label.setStyleSheet(f"color: {p['muted_text']}; font-size: 9pt;")
        trunc_vlayout.addWidget(overrides_label)

        self._caps_table = QTableWidget(0, 2)
        self._caps_table.setHorizontalHeaderLabels(["Tool", "Cap (chars)"])
        self._caps_table.setFont(QFont("Consolas", 9))
        self._caps_table.setFixedHeight(110)
        self._caps_table.verticalHeader().setVisible(False)
        self._caps_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._caps_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._caps_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._caps_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self._caps_table.setStyleSheet(f"""
            QTableWidget {{
                background: {p['panel_alt']};
                border: 1px solid {p['border']};
                color: {p['text']};
                gridline-color: {p['border']};
            }}
            QTableWidget::item {{ padding: 2px 6px; }}
            QHeaderView::section {{
                background: {p['panel']};
                color: {p['muted_text']};
                padding: 3px 6px;
                border: 0px;
                border-right: 1px solid {p['border']};
                border-bottom: 1px solid {p['border']};
            }}
        """)

        # Populate from config
        existing_caps: dict = cfg.get("tool_result_caps", {})
        for tool_name, cap_val in existing_caps.items():
            self._caps_table_add_row(tool_name, str(cap_val))

        trunc_vlayout.addWidget(self._caps_table)

        caps_btn_row = QHBoxLayout()
        add_cap_btn = QPushButton("+ Add Override")
        add_cap_btn.setFixedHeight(22)
        add_cap_btn.clicked.connect(lambda: self._caps_table_add_row("", ""))
        remove_cap_btn = QPushButton("Remove")
        remove_cap_btn.setFixedHeight(22)
        remove_cap_btn.clicked.connect(self._caps_table_remove_row)
        caps_btn_row.addWidget(add_cap_btn)
        caps_btn_row.addWidget(remove_cap_btn)
        caps_btn_row.addStretch()
        trunc_vlayout.addLayout(caps_btn_row)

        layout.addWidget(trunc_group)
        return w

    def _caps_table_add_row(self, tool: str, cap: str):
        row = self._caps_table.rowCount()
        self._caps_table.insertRow(row)
        self._caps_table.setItem(row, 0, QTableWidgetItem(tool))
        self._caps_table.setItem(row, 1, QTableWidgetItem(cap))

    def _caps_table_remove_row(self):
        rows = {idx.row() for idx in self._caps_table.selectedIndexes()}
        for row in sorted(rows, reverse=True):
            self._caps_table.removeRow(row)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _on_reset_tool_stats(self):
        """Wipe per-tool usage stats; refresh the Tools tab in place."""
        from core import tool_stats
        confirm = QMessageBox.question(
            self, "Reset Tool Stats",
            "Wipe all per-tool usage counters? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        tool_stats.reset()
        QMessageBox.information(self, "Reset Tool Stats",
                                "Tool stats cleared. Reopen Settings to see the refreshed table.")

    def _refresh_main_model_suggestions(self):
        pid = self._provider_combo.currentData()
        models = list(self._provider_model_history.get(pid) or [])
        self._main_model_suggest_model.setStringList(models)

    def _on_provider_changed(self, idx):
        old_pid = self._last_model_provider_pid
        current_model = self._model_edit.text().strip()
        if current_model and old_pid:
            touch_provider_model_choice(
                self._provider_models,
                self._provider_model_history,
                old_pid,
                current_model,
            )

        pid = self._provider_combo.itemData(idx)
        info = PROVIDER_INFO.get(pid, {})
        remembered = self._provider_models.get(pid, "")
        self._model_edit.setText(remembered or info.get("default_model", ""))
        self._last_model_provider_pid = pid
        self._refresh_main_model_suggestions()

    def _build_network_tab(self) -> QWidget:
        import socket as _socket
        w = QWidget()
        outer = QVBoxLayout(w)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)
        cfg = load_config()
        net = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}

        intro = QLabel(
            "Connect this Familiar to peers on other machines. Chat messages "
            "sync across connected peers; tool actions, file edits, and terminals "
            "stay local to each machine.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{PALETTE['muted_text']};")
        outer.addWidget(intro)

        form = QFormLayout()
        form.setSpacing(6)
        self._net_enabled_check = QCheckBox("Enable networking")
        self._net_enabled_check.setChecked(bool(net.get("enabled", False)))
        form.addRow(self._net_enabled_check)
        self._net_name_edit = QLineEdit(net.get("node_name") or _socket.gethostname())
        form.addRow(QLabel("This machine's name"), self._net_name_edit)
        self._net_secret_edit = QLineEdit(net.get("secret", ""))
        self._net_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._net_secret_edit.setPlaceholderText("Shared secret — must match on every peer")
        form.addRow(QLabel("Shared secret"), self._net_secret_edit)
        self._net_autorespond_check = QCheckBox(
            "Auto-respond — let this Familiar's agent answer inbound peer messages")
        self._net_autorespond_check.setChecked(bool(net.get("auto_respond", False)))
        form.addRow(self._net_autorespond_check)
        outer.addLayout(form)

        # ── Inbound ──
        in_box = QGroupBox("Inbound — let peers reach this machine")
        in_form = QFormLayout(in_box)
        self._net_inbound_check = QCheckBox("Accept connections from peers")
        self._net_inbound_check.setChecked(bool(net.get("inbound_enabled", False)))
        in_form.addRow(self._net_inbound_check)
        self._net_port_spin = QSpinBox()
        self._net_port_spin.setRange(1024, 65535)
        self._net_port_spin.setValue(int(net.get("port", 8787)))
        in_form.addRow(QLabel("Local port"), self._net_port_spin)
        self._net_tunnel_check = QCheckBox("Auto-start cloudflared tunnel for inbound")
        self._net_tunnel_check.setChecked(bool(net.get("auto_tunnel", True)))
        in_form.addRow(self._net_tunnel_check)

        # cloudflared isn't bundled (it's a large binary) — fetch it on demand,
        # straight into the app folder where the tunnel looks for it.
        cf_row = QHBoxLayout()
        self._cf_download_btn = QPushButton("Download cloudflared")
        self._cf_download_btn.clicked.connect(self._download_cloudflared_clicked)
        cf_row.addWidget(self._cf_download_btn)
        self._cf_status = QLabel("")
        self._cf_status.setStyleSheet(f"color:{PALETTE['muted_text']};")
        cf_row.addWidget(self._cf_status, 1)
        in_form.addRow("", cf_row)
        self._cf_worker = None
        self._refresh_cf_status()

        # Use an EXISTING public address instead of starting cloudflared. Any
        # https URL that ultimately reaches this machine's inbound port works
        # (own tunnel, reverse proxy) — no service is special-cased.
        self._net_override_edit = QLineEdit(net.get("public_url_override", ""))
        self._net_override_edit.setPlaceholderText(
            "https://your-address…  (blank = start cloudflared automatically)")
        self._net_override_edit.setToolTip(
            "Paste any https URL that reaches this machine's inbound port —\n"
            "your own tunnel or reverse proxy. It must forward to THIS\n"
            "machine's port above. Leave blank to let Familiar start its own\n"
            "cloudflared (recommended).")
        in_form.addRow(QLabel("Public address"), self._net_override_edit)

        self._net_public_edit = QLineEdit(net.get("public_url", ""))
        self._net_public_edit.setReadOnly(True)
        self._net_public_edit.setPlaceholderText("(populated once networking starts)")
        in_form.addRow(QLabel("Your public address"), self._net_public_edit)
        outer.addWidget(in_box)

        # Start / stop controls + live status.
        ctl = QHBoxLayout()
        self._net_start_btn = QPushButton("Start / Restart")
        self._net_start_btn.clicked.connect(self._net_start_clicked)
        self._net_stop_btn = QPushButton("Stop")
        self._net_stop_btn.clicked.connect(self._net_stop_clicked)
        self._net_check_btn = QPushButton("Check peers")
        self._net_check_btn.clicked.connect(self._net_check_peers_clicked)
        ctl.addWidget(self._net_start_btn)
        ctl.addWidget(self._net_stop_btn)
        ctl.addWidget(self._net_check_btn)
        ctl.addStretch(1)
        self._net_status = QLabel("")
        self._net_status.setStyleSheet(f"color:{PALETTE['muted_text']};")
        ctl.addWidget(self._net_status)
        outer.addLayout(ctl)
        # Reflect a tunnel that's already up from a prior start.
        if network_manager.public_url:
            self._net_public_edit.setText(network_manager.public_url)
            self._net_status.setText("online")

        # ── Peers (allowlist for BOTH directions) ──
        peers_box = QGroupBox("Peers — allowlist (contacted outbound AND accepted inbound)")
        peers_lay = QVBoxLayout(peers_box)
        self._net_peers_list = QListWidget()
        self._net_peers_list.setMaximumHeight(120)
        # Rows are editable in place (double-click / F2) and copyable
        # (Ctrl+C, right-click menu) — the display text "name  —  url" is
        # the editing format too, reparsed into UserRole on commit so the
        # save paths keep reading the same {"name", "url"} dict.
        self._net_peers_list.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed)
        self._net_peers_list.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        # The glass theme paints QLineEdit semi-transparent, but the inline
        # item editor sits directly over the painted item text — with a
        # translucent editor both texts show at once. Force a SOLID,
        # theme-matched background on editors spawned inside this list.
        self._net_peers_list.setStyleSheet(
            f"QListWidget QLineEdit {{"
            f" background: {PALETTE['panel']};"
            f" color: {PALETTE['text']};"
            f" border: 1px solid {PALETTE['accent']};"
            f" padding: 1px;"
            f"}}"
        )
        for peer in net.get("peers", []):
            if isinstance(peer, dict) and peer.get("url"):
                nm, url = peer.get("name", ""), peer["url"]
                it = QListWidgetItem(f"{nm or url}  —  {url}")
                it.setData(Qt.ItemDataRole.UserRole, {"name": nm or url, "url": url})
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
                self._net_peers_list.addItem(it)
        peers_lay.addWidget(self._net_peers_list)

        def _parse_peer_text(text: str) -> dict:
            # "name  —  url" (em dash); name may be omitted. URLs can't
            # contain "—", so split on the LAST one.
            t = text.strip()
            if "—" in t:
                nm, _sep, url = t.rpartition("—")
                nm, url = nm.strip(), url.strip()
            else:
                nm, url = "", t
            return {"name": nm or url, "url": url}

        def _peer_item_changed(it):
            d = _parse_peer_text(it.text())
            self._net_peers_list.blockSignals(True)
            it.setData(Qt.ItemDataRole.UserRole, d)
            it.setText(f"{d['name']}  —  {d['url']}")
            self._net_peers_list.blockSignals(False)
            self._dirty = True  # list isn't in the signature snapshot

        self._net_peers_list.itemChanged.connect(_peer_item_changed)

        def _peer_menu(pos):
            it = self._net_peers_list.itemAt(pos)
            if it is None:
                return
            d = it.data(Qt.ItemDataRole.UserRole) or {}
            menu = QMenu(self._net_peers_list)
            act_url = menu.addAction("Copy URL")
            act_name = menu.addAction("Copy name")
            act_row = menu.addAction("Copy row")
            menu.addSeparator()
            act_edit = menu.addAction("Edit")
            chosen = menu.exec(self._net_peers_list.mapToGlobal(pos))
            if chosen is act_url:
                QApplication.clipboard().setText(d.get("url", ""))
            elif chosen is act_name:
                QApplication.clipboard().setText(d.get("name", ""))
            elif chosen is act_row:
                QApplication.clipboard().setText(it.text())
            elif chosen is act_edit:
                self._net_peers_list.editItem(it)

        self._net_peers_list.customContextMenuRequested.connect(_peer_menu)

        def _peer_copy_shortcut():
            it = self._net_peers_list.currentItem()
            if it is not None:
                d = it.data(Qt.ItemDataRole.UserRole) or {}
                QApplication.clipboard().setText(d.get("url", "") or it.text())

        _peer_copy = QShortcut(QKeySequence.StandardKey.Copy, self._net_peers_list)
        _peer_copy.setContext(Qt.ShortcutContext.WidgetShortcut)
        _peer_copy.activated.connect(_peer_copy_shortcut)

        add_row = QHBoxLayout()
        self._net_peer_name = QLineEdit()
        self._net_peer_name.setPlaceholderText("peer name")
        self._net_peer_url = QLineEdit()
        self._net_peer_url.setPlaceholderText("https://peer.trycloudflare.com")

        def _add_peer():
            nm = self._net_peer_name.text().strip()
            url = self._net_peer_url.text().strip()
            if not url:
                return
            it = QListWidgetItem(f"{nm or url}  —  {url}")
            it.setData(Qt.ItemDataRole.UserRole, {"name": nm or url, "url": url})
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
            self._net_peers_list.blockSignals(True)
            self._net_peers_list.addItem(it)
            self._net_peers_list.blockSignals(False)
            self._net_peer_name.clear()
            self._net_peer_url.clear()
            self._dirty = True

        def _rm_peer():
            row = self._net_peers_list.currentRow()
            if row >= 0:
                self._net_peers_list.takeItem(row)
                self._dirty = True

        add_btn = QPushButton("Add")
        add_btn.clicked.connect(_add_peer)
        rm_btn = QPushButton("Remove")
        rm_btn.clicked.connect(_rm_peer)
        add_row.addWidget(self._net_peer_name, 1)
        add_row.addWidget(self._net_peer_url, 2)
        add_row.addWidget(add_btn)
        add_row.addWidget(rm_btn)
        peers_lay.addLayout(add_row)
        outer.addWidget(peers_box)

        # ── Shared files (file_share/) ──
        share_box = QGroupBox("Shared files — file_share/ (synced to all peers)")
        share_lay = QVBoxLayout(share_box)
        self._net_share_list = QListWidget()
        self._net_share_list.setMaximumHeight(140)
        self._net_share_list.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        share_lay.addWidget(self._net_share_list)

        def _fmt_size(n) -> str:
            n = float(n)
            for unit in ("B", "KB", "MB"):
                if n < 1024:
                    return f"{n:.0f} B" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024.0
            return f"{n:.1f} GB"

        def _refresh_share_list():
            from datetime import datetime
            from core.file_share import list_share_files
            self._net_share_list.clear()
            try:
                files = list_share_files()
            except Exception as e:
                self._net_share_list.addItem(f"(error listing file_share: {e})")
                return
            if not files:
                ph = QListWidgetItem("(file_share/ is empty)")
                ph.setFlags(Qt.ItemFlag.NoItemFlags)
                self._net_share_list.addItem(ph)
                return
            for f in files:
                when = datetime.fromtimestamp(f["mtime"]).strftime("%Y-%m-%d %H:%M")
                it = QListWidgetItem(
                    f"{f['rel']}  —  {_fmt_size(f['size'])}  —  {when}")
                it.setData(Qt.ItemDataRole.UserRole, f["rel"])
                self._net_share_list.addItem(it)

        def _delete_shared_clicked():
            rels = [it.data(Qt.ItemDataRole.UserRole)
                    for it in self._net_share_list.selectedItems()]
            rels = [r for r in rels if r]
            if not rels:
                return
            preview = "\n".join(rels[:8]) + ("\n…" if len(rels) > 8 else "")
            if not GlassDialog.confirm(
                    self,
                    "Delete everywhere?",
                    f"Delete {len(rels)} file(s) from file_share on THIS machine "
                    f"AND every connected peer?\n\n{preview}\n\n"
                    "A tombstone propagates over Familiar-Net so peers delete "
                    "their copy too instead of re-seeding it."):
                return
            from core.file_share import delete_shared_file
            failed = [r for r in rels if not delete_shared_file(r)]
            _refresh_share_list()
            if failed:
                QMessageBox.warning(
                    self, "Delete failed",
                    "Could not delete:\n" + "\n".join(failed))

        def _open_share_folder():
            from core.file_share import SHARE_DIR, _ensure_dir
            _ensure_dir()
            try:
                os.startfile(str(SHARE_DIR))            # Windows
            except AttributeError:
                subprocess.Popen(
                    ["open" if sys.platform == "darwin" else "xdg-open",
                     str(SHARE_DIR)])

        share_btns = QHBoxLayout()
        share_del_btn = QPushButton("Delete selected (everywhere)")
        share_del_btn.clicked.connect(_delete_shared_clicked)
        share_refresh_btn = QPushButton("Refresh")
        share_refresh_btn.clicked.connect(_refresh_share_list)
        share_open_btn = QPushButton("Open folder")
        share_open_btn.clicked.connect(_open_share_folder)
        share_btns.addWidget(share_del_btn)
        share_btns.addWidget(share_refresh_btn)
        share_btns.addWidget(share_open_btn)
        share_btns.addStretch()
        share_lay.addLayout(share_btns)
        outer.addWidget(share_box)
        _refresh_share_list()

        note = QLabel(
            "The shared secret authenticates every message (HMAC, 30s replay window) — "
            "it is the gate for inbound traffic; keep it strong and private. The peers "
            "list is the outbound address book: it's who 'Check peers', broadcasts, and "
            "the agent's peer_network tool talk to. Inbound messages appear in a "
            "“Network: <node>” conversation; enable Auto-respond above to let "
            "the agent answer them (and reply to the sender) automatically.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{PALETTE['muted_text']}; font-size:8pt;")
        outer.addWidget(note)
        outer.addStretch(1)
        # The Network tab outgrew the dialog (inbound + outbound + peers +
        # shared files); without a scroll area Qt compresses every input to
        # fit, squashing single-line edits into illegibility. Same treatment
        # as the UI / API Keys tabs.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(w)
        return scroll

    def _gather_network_cfg(self) -> dict:
        peers = []
        for i in range(self._net_peers_list.count()):
            d = self._net_peers_list.item(i).data(Qt.ItemDataRole.UserRole)
            if isinstance(d, dict) and d.get("url"):
                peers.append({"name": d.get("name", ""), "url": d["url"]})
        return {"network": {
            "enabled": self._net_enabled_check.isChecked(),
            "node_name": self._net_name_edit.text().strip(),
            "secret": self._net_secret_edit.text(),
            "auto_respond": self._net_autorespond_check.isChecked(),
            "inbound_enabled": self._net_inbound_check.isChecked(),
            "port": self._net_port_spin.value(),
            "auto_tunnel": self._net_tunnel_check.isChecked(),
            "public_url_override": self._net_override_edit.text().strip(),
            "peers": peers,
        }}

    def _refresh_cf_status(self):
        """Reflect whether cloudflared is already available."""
        try:
            from core.network import cloudflared_present
            present = cloudflared_present()
        except Exception:
            present = False
        if present:
            self._cf_status.setText("installed ✓")
            self._cf_download_btn.setText("cloudflared installed")
            self._cf_download_btn.setEnabled(False)
        else:
            self._cf_status.setText("not found — needed for the tunnel")
            self._cf_download_btn.setText("Download cloudflared")
            self._cf_download_btn.setEnabled(True)

    def _download_cloudflared_clicked(self):
        import threading
        self._cf_download_btn.setEnabled(False)
        self._cf_status.setText("starting download…")
        worker = _CfDownloadWorker()
        self._cf_worker = worker  # keep a ref so it isn't GC'd mid-download
        worker.progress.connect(self._cf_status.setText)

        def _on_done(ok: bool, msg: str):
            self._cf_status.setText(msg)
            self._refresh_cf_status()  # flip to "installed ✓" on success
            self._cf_worker = None
        worker.done.connect(_on_done)
        threading.Thread(target=worker.run, daemon=True,
                         name="cloudflared-download").start()

    def _net_start_clicked(self):
        from PyQt6.QtCore import QTimer
        self._net_public_edit.setText("")
        self._net_status.setText("starting…")
        network_manager.start(self._gather_network_cfg())
        self._net_poll_n = 0
        if not hasattr(self, "_net_poll_timer"):
            self._net_poll_timer = QTimer(self)
            self._net_poll_timer.timeout.connect(self._net_poll_tick)
        self._net_poll_timer.start(700)

    def _net_poll_tick(self):
        self._net_poll_n = getattr(self, "_net_poll_n", 0) + 1
        if network_manager.public_url:
            self._net_public_edit.setText(network_manager.public_url)
            self._net_status.setText("online")
            self._net_poll_timer.stop()
            try:
                c = load_config()
                c.setdefault("network", {})["public_url"] = network_manager.public_url
                save_config(c)
            except Exception:
                pass
        elif network_manager.running and network_manager._cf is None:
            self._net_status.setText("inbound up (tunnel off)")
            self._net_poll_timer.stop()
        elif self._net_poll_n > 30:
            self._net_status.setText("no URL — check cloudflared")
            self._net_poll_timer.stop()

    def _net_stop_clicked(self):
        if hasattr(self, "_net_poll_timer"):
            self._net_poll_timer.stop()
        network_manager.stop()
        self._net_public_edit.setText("")
        self._net_status.setText("stopped")

    def _net_check_peers_clicked(self):
        """Probe each peer off-thread on BOTH levels: an unauthenticated /ping
        (reachable?) AND a signed /conv/list (does the shared secret match and
        are clocks in sync?). Reporting only reachability hid secret/clock
        mismatches — the actual cause of 'reachable but nothing syncs'."""
        import threading
        from PyQt6.QtCore import QTimer
        cfg = self._gather_network_cfg()["network"]
        peers = cfg["peers"]
        secret = cfg["secret"]
        node = cfg["node_name"] or "familiar"
        if not peers:
            self._net_status.setText("no peers configured")
            return
        self._net_check_btn.setEnabled(False)
        self._net_status.setText("checking peers…")
        self._net_peer_check_result = None

        def _probe():
            import json as _json, time as _time, urllib.request, urllib.error
            from core.network import sign
            total = len(peers)
            reachable = authed = 0
            for p in peers:
                url = p["url"].rstrip("/")
                try:
                    with urllib.request.urlopen(url + "/ping", timeout=4) as r:
                        if r.status == 200:
                            reachable += 1
                except Exception:
                    continue  # unreachable → can't be authed either
                # Signed probe of an authenticated endpoint.
                try:
                    body = _json.dumps({"from": node, "sent_at": _time.time()}).encode()
                    ts = str(_time.time())
                    req = urllib.request.Request(
                        url + "/conv/list", data=body, method="POST",
                        headers={"Content-Type": "application/json", "X-Timestamp": ts,
                                 "X-Signature": sign(secret, body, ts)})
                    with urllib.request.urlopen(req, timeout=5) as r:
                        if r.status == 200:
                            authed += 1
                except Exception:
                    pass
            self._net_peer_check_result = (reachable, authed, total)

        threading.Thread(target=_probe, daemon=True, name="net-peer-check").start()

        def _poll():
            res = getattr(self, "_net_peer_check_result", None)
            if res is None:
                QTimer.singleShot(200, _poll)
                return
            self._net_check_btn.setEnabled(True)
            reachable, authed, total = res
            if authed == total:
                msg = f"✓ {authed}/{total} connected & authenticated"
            elif reachable and not authed:
                msg = (f"reachable {reachable}/{total} but AUTH FAILED — shared "
                       f"secret must match exactly on both machines, and clocks "
                       f"within 30s")
            elif reachable:
                msg = f"reachable {reachable}/{total}, authenticated {authed}/{total}"
            else:
                msg = f"unreachable 0/{total} — check the peer URL / tunnel"
            self._net_status.setText(msg)
        QTimer.singleShot(200, _poll)

    def _save_and_close(self):
        # Save keys
        keys = load_keys()
        for pid, field in self._key_fields.items():
            val = field.text().strip()
            if pid not in keys:
                keys[pid] = {}
            keys[pid]["api_key"] = val
            combo = self._auth_mode_combos.get(pid)
            if combo is not None:
                keys[pid]["auth_mode"] = combo.currentData() or "api_key"
        save_keys(keys)

        # Save model config
        pid = self._provider_combo.currentData()
        model = self._model_edit.text().strip()
        if model:
            touch_provider_model_choice(
                self._provider_models,
                self._provider_model_history,
                pid,
                model,
            )
        for i, edit in enumerate(self._fallback_edits, 1):
            fm = edit.text().strip()
            if not fm:
                continue
            fpid = self._fallback_provider_combos[i - 1].currentData() or pid
            touch_provider_model_choice(
                self._provider_models,
                self._provider_model_history,
                fpid,
                fm,
            )
        self.agent.set_provider(pid)
        self.agent.set_model(model)
        self.agent.set_system_prompt(self._prompt_edit.toPlainText())

        cfg = load_config()
        # Keep Google key in config.json so it is easy to find/edit alongside other Agent settings.
        gkey = (keys.get("google") or {}).get("api_key", "").strip()
        cfg["google_api_key"] = gkey

        # UI settings
        color = self._base_color_edit.text().strip()
        if color.startswith("#") and QColor(color).isValid():
            cfg["base_color"] = color
        cfg["brightness"] = self._brightness_slider.value() / 100
        cfg["animate_ellipsis"] = self._animate_ellipsis_check.isChecked()
        cfg["stream_display"] = self._stream_display_combo.currentData() or "chat"
        cfg["chat_mode"] = self._chat_mode_combo.currentData() or "fancy"
        cfg["workspace_side"] = self._workspace_side_combo.currentData() or "right"
        cfg["show_tools_called"] = self._show_tools_called_check.isChecked()
        cfg["tool_display_mode"] = self._tool_display_combo.currentData() or "chips"
        cfg["show_tools_hint"] = self._show_tools_hint_check.isChecked()
        cfg["trailing_ellipsis"] = self._trailing_ellipsis_check.isChecked()
        cfg["show_timestamps"] = self._show_timestamps_check.isChecked()
        cfg["show_usage"] = self._show_usage_check.isChecked()
        cfg["monocolor"] = self._monocolor_check.isChecked()
        cfg["monocolor_browser"] = self._monocolor_browser_check.isChecked()
        cfg["monocolor_images"] = self._monocolor_images_check.isChecked()
        # Per-element color overrides (honored only when Monocolor is off). Only
        # rewrite them when Monocolor is OFF, so toggling Monocolor on and saving
        # doesn't clobber a saved custom palette. Store valid, non-blank entries
        # only; blanks fall back to base-color derivation.
        if not self._monocolor_check.isChecked():
            overrides = {}
            for key, edit in getattr(self, "_color_override_edits", {}).items():
                val = edit.text().strip()
                if val and QColor(val).isValid():
                    overrides[key] = val
            cfg["color_overrides"] = overrides

        # Audio settings
        cfg["ui_sounds"] = self._ui_sounds_check.isChecked()
        cfg["workspace_edit_sounds"] = self._workspace_edit_sounds_check.isChecked()
        cfg["viewer_typing_sounds"] = self._viewer_typing_sounds_check.isChecked()
        cfg["sound_exempt_patterns"] = [
            ln.strip()
            for ln in self._sound_exempt_edit.toPlainText().splitlines()
            if ln.strip()
        ]
        cfg["tts_voice"] = self._voice_combo.currentData() or "en-US-AriaNeural"
        cfg["tts_autoplay"] = self._tts_autoplay_check.isChecked()
        cfg["tts_speed"] = self._tts_speed_slider.value()
        cfg["tts_backend"] = self._tts_backend_combo.currentData() or "edge"
        cfg["elevenlabs_voice_id"] = self._eleven_voice_combo.currentData() or ""
        cfg["chatterbox_voice_ref"] = self._chatter_ref_combo.currentData() or ""
        # Persist ElevenLabs API key into keys.json (reuse already-loaded `keys` dict)
        try:
            keys.setdefault("elevenlabs", {})["api_key"] = self._eleven_key_edit.text().strip()
            keys["elevenlabs"].setdefault("base_url", "https://api.elevenlabs.io")
            save_keys(keys)
        except Exception:
            pass
        from ui.theme import CHAT_CONTRAST_USER_BRIGHT as _CHAT_USER_BRIGHT
        cfg["chat_font_size"] = self._font_size_spin.value()
        cfg["chat_role_contrast"] = (
            self._chat_role_contrast_combo.currentData() or _CHAT_USER_BRIGHT)
        cfg["display_char_limit"] = self._char_limit_spin.value()

        # Memory settings are now in the Memory dialog — not touched here

        try:
            cfg["temperature"] = float(self._temp_edit.text())
        except ValueError:
            pass
        try:
            cfg["max_tokens"] = int(self._max_tokens_edit.text())
        except ValueError:
            pass

        # Reasoning / extended thinking (cross-provider)
        cfg["thinking_enabled"] = self._thinking_check.isChecked()
        cfg["reasoning_effort"] = self._reasoning_effort_combo.currentText()
        cfg["thinking_budget"] = self._thinking_budget_spin.value()

        # Summary model + temperature
        cfg["summary_model"] = self._summary_model_edit.text().strip()
        try:
            cfg["summary_temperature"] = float(self._summary_temp_edit.text())
        except ValueError:
            pass

        # Per-provider model memory + recent model IDs (autocomplete)
        cfg["provider_models"] = self._provider_models
        cfg["provider_model_history"] = self._provider_model_history

        # Embedding model
        cfg["embedding_model"] = self._embed_model_edit.text().strip()

        # Network — preserve any engine-written fields (e.g. live public_url)
        # we didn't surface as editable.
        net = cfg.get("network") if isinstance(cfg.get("network"), dict) else {}
        peers = []
        for i in range(self._net_peers_list.count()):
            d = self._net_peers_list.item(i).data(Qt.ItemDataRole.UserRole)
            if isinstance(d, dict) and d.get("url"):
                peers.append({"name": d.get("name", ""), "url": d["url"]})
        net.update({
            "enabled": self._net_enabled_check.isChecked(),
            "node_name": self._net_name_edit.text().strip(),
            "secret": self._net_secret_edit.text(),
            "auto_respond": self._net_autorespond_check.isChecked(),
            "inbound_enabled": self._net_inbound_check.isChecked(),
            "port": self._net_port_spin.value(),
            "auto_tunnel": self._net_tunnel_check.isChecked(),
            "public_url_override": self._net_override_edit.text().strip(),
            "peers": peers,
        })
        cfg["network"] = net

        # Vision model
        cfg["vision_enabled"] = self._vision_check.isChecked()
        cfg["vision_provider"] = self._vision_provider_combo.currentData() or "openrouter"
        cfg["vision_model"] = self._vision_model_edit.text().strip()

        # Per-tool model/provider overrides edited inline in the Tools tab.
        # These mirror the canonical config keys (vision_*, subagent_*,
        # memory_*, embedding_model, etc.) so other code paths pick them up
        # unchanged. Multiple tool rows may write the same key (e.g. both
        # vector_search and session_search → embedding_model); same value, no harm.
        for tool_name, edit in getattr(self, "_tool_model_edits", {}).items():
            entry = self._tool_llm_config_map.get(tool_name)
            if not entry:
                continue
            mkey, _pkey, default, _kind = entry
            if mkey:
                val = edit.text().strip()
                if val:
                    cfg[mkey] = val
                elif default:
                    cfg[mkey] = default
        for tool_name, combo in getattr(self, "_tool_provider_combos", {}).items():
            entry = self._tool_llm_config_map.get(tool_name)
            if not entry:
                continue
            _mkey, pkey, _default, _kind = entry
            if pkey:
                cfg[pkey] = combo.currentData() or self.agent.provider

        # Per-tool enable toggles ("On" column). Persist the disabled set and
        # sync the live registry so the change takes effect without a restart.
        disabled = sorted(
            name for name, chk in getattr(self, "_tool_enable_checks", {}).items()
            if not chk.isChecked()
        )
        cfg["disabled_tools"] = disabled
        try:
            from tools.registry import registry as _reg
            _reg.set_disabled(set(disabled))
        except Exception:
            pass

        # Explore-files model — after the tool loop so nothing overwrites it.
        cfg["subagent_explore_provider"] = (
            self._explore_provider_combo.currentData() or "anthropic"
        )
        cfg["subagent_explore_model"] = (
            self._explore_model_edit.text().strip() or "claude-haiku-4-5"
        )

        # Fallback models (per-slot provider + model)
        for i, edit in enumerate(self._fallback_edits, 1):
            cfg[f"fallback_model_{i}"] = edit.text().strip()
            pc = self._fallback_provider_combos[i - 1]
            cfg[f"fallback_{i}_provider"] = pc.currentData() or self.agent.provider

        # Save workspace edits (update selected workspace's name/path/venv)
        row = self._ws_list.currentRow()
        if row >= 0:
            item = self._ws_list.item(row)
            old_name = item.data(Qt.ItemDataRole.UserRole)
            new_name = self._ws_name_edit.text().strip() or old_name
            new_path = self._ws_path_edit.text().strip()
            new_venv = self._ws_venv_edit.text().strip()
            if new_path:
                new_path = to_config_workspace_path(
                    resolve_workspace_entry_path(new_path))
            if new_venv:
                new_venv = to_config_workspace_path(
                    resolve_workspace_entry_path(new_venv))
            if new_name != old_name:
                self._ws_data.pop(old_name, None)
                if self._active_ws == old_name:
                    self._active_ws = new_name
            self._ws_data[new_name] = {"path": new_path, "venv": new_venv}

        cfg["workspaces"] = self._ws_data

        # Tool self-audit
        cfg["tool_audit_enabled"] = self._audit_enabled_check.isChecked()
        cfg["full_tools_list"] = self._full_tools_check.isChecked()
        cfg["tool_audit_threshold"] = self._audit_threshold_spin.value()
        cfg["tool_audit_target_conv"] = self._audit_conv_combo.currentData() or ""

        # Tool result truncation caps
        cfg["max_tool_result"] = self._max_tool_result_spin.value()
        caps: dict[str, int] = {}
        for row in range(self._caps_table.rowCount()):
            name_item = self._caps_table.item(row, 0)
            cap_item = self._caps_table.item(row, 1)
            tool_name = (name_item.text().strip() if name_item else "")
            cap_text = (cap_item.text().strip() if cap_item else "")
            if tool_name and cap_text:
                try:
                    caps[tool_name] = int(cap_text)
                except ValueError:
                    pass
        cfg["tool_result_caps"] = caps

        # Workspace selection is per-conversation, handled by chat_widget
        save_config(cfg)
        self.agent.config = cfg
        invalidate_ui_sounds_cache()
        invalidate_settings_cache()

        self._dirty = False
        self._persist_geometry()
        win = self.window()
        if win is not None and hasattr(win, "_refresh_setup_banner"):
            try:
                win._refresh_setup_banner()
            except Exception:
                pass
        self.accept()

    # Styles are inherited from GlassDialog's container stylesheet
