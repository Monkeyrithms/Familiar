"""
Conversation Settings dialog — per-conversation properties.

Tabs:
  1. Model — provider, model, workspace
  2. Prompt — system prompt override + rolling summaries
  3. Streams — subscribe/unsubscribe memory streams with read/write permissions
  4. Debug — full LLM context / tool rounds for this chat (persisted per conversation)
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget,
    QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit,
    QListWidget, QListWidgetItem, QCheckBox, QFrame, QSizePolicy,
    QCompleter,
)
from PyQt6.QtCore import Qt, pyqtSignal, QStringListModel
from PyQt6.QtGui import QFont, QColor
from ui.theme import PALETTE
from ui.glass_dialog import GlassDialog
from ui.rolling_summary_format import (
    rolling_summary_plain_from_edit,
    set_rolling_summary_content,
)
from core.agent import Agent, load_config
from core.debug_recorder import debug_recorder
from core.model_history import merge_stored_provider_model_memory, touch_provider_model_choice
from ui.debug_panel import DebugPanel


def _normalize_streams(raw) -> list[dict]:
    """Convert streams from old format (list of strings) to new format (list of dicts)."""
    if not raw:
        return []
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append({"name": item, "read": True, "write": True})
        elif isinstance(item, dict):
            result.append({
                "name": item.get("name", ""),
                "read": item.get("read", True),
                "write": item.get("write", True),
            })
    return [s for s in result if s["name"]]


class ConversationDialog(GlassDialog):
    """Per-conversation settings: model, prompt, memory streams."""

    def __init__(self, agent: Agent, conv_id: str = "", parent=None):
        super().__init__(title="Conversation", parent=parent, width=680, height=600)
        self.agent = agent
        self._conv_id = conv_id
        self._build_ui()

    def _build_ui(self):
        layout = self.content_layout()
        p = PALETTE
        mono9 = QFont("Consolas", 9)
        small = QFont("Consolas", 8)

        if self._conv_id:
            debug_recorder.load_conversation_from_db(self._conv_id)

        tabs = QTabWidget()

        # ═══════════════════════════════════════════════════════════
        # Tab 1: Model
        # ═══════════════════════════════════════════════════════════
        model_tab = QWidget()
        ml = QFormLayout(model_tab)
        ml.setSpacing(10)
        ml.setContentsMargins(10, 10, 10, 10)

        from core.providers import PROVIDER_INFO
        from core.agent import load_config
        cfg = load_config()
        merge_stored_provider_model_memory(cfg)
        self._provider_models: dict = dict(cfg.get("provider_models", {}))
        self._provider_model_history: dict[str, list[str]] = {
            k: list(v) for k, v in (cfg.get("provider_model_history") or {}).items()
        }
        if self.agent.model:
            self._provider_models.setdefault(self.agent.provider, self.agent.model)

        self._provider_combo = QComboBox()
        self._provider_combo.setFont(mono9)
        for pid, info in PROVIDER_INFO.items():
            self._provider_combo.addItem(info["name"], pid)
        idx = list(PROVIDER_INFO.keys()).index(self.agent.provider) \
            if self.agent.provider in PROVIDER_INFO else 0
        self._provider_combo.blockSignals(True)
        self._provider_combo.setCurrentIndex(idx)
        self._provider_combo.blockSignals(False)
        self._last_model_provider_pid = self._provider_combo.currentData()
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        ml.addRow(QLabel("Provider"), self._provider_combo)

        self._model_edit = QLineEdit(self.agent.model)
        self._model_edit.setFont(mono9)
        self._model_edit.setPlaceholderText("model name")
        ml.addRow(QLabel("Model"), self._model_edit)

        self._main_model_completer = QCompleter(self)
        self._main_model_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._main_model_completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._main_model_completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self._main_model_completer.setMaxVisibleItems(8)
        self._main_model_suggest_model = QStringListModel(self)
        self._main_model_completer.setModel(self._main_model_suggest_model)
        self._model_edit.setCompleter(self._main_model_completer)
        self._refresh_main_model_suggestions()

        self._ws_combo = QComboBox()
        self._ws_combo.setFont(mono9)
        workspaces = self.agent.config.get("workspaces", {})
        for name in workspaces:
            self._ws_combo.addItem(name)
        current_ws = self.agent._workspace_name
        for i in range(self._ws_combo.count()):
            if self._ws_combo.itemText(i) == current_ws:
                self._ws_combo.setCurrentIndex(i)
                break
        ml.addRow(QLabel("Workspace"), self._ws_combo)

        tabs.addTab(model_tab, "Model")

        # ═══════════════════════════════════════════════════════════
        # Tab 2: Prompt + Rolling Summary
        # ═══════════════════════════════════════════════════════════
        prompt_tab = QWidget()
        pl = QVBoxLayout(prompt_tab)
        pl.setContentsMargins(10, 10, 10, 10)
        pl.setSpacing(8)

        pl.addWidget(QLabel("Conversation Prompt"))
        hint = QLabel("Layered on top of the base prompt — this conversation only.")
        hint.setStyleSheet(f"color:{p['muted_text']};font-size:8pt;border:none;")
        pl.addWidget(hint)

        self._prompt_edit = QTextEdit()
        self._prompt_edit.setAcceptRichText(False)
        self._prompt_edit.setFont(mono9)
        # Show ONLY this conversation's overlay text, never the base/effective
        # prompt (showing agent.system_prompt here is what fed the overwrite bug).
        self._prompt_edit.setPlainText(self.agent.conversation_prompt)
        self._prompt_edit.setPlaceholderText("Extra instructions for this conversation (optional)")
        pl.addWidget(self._prompt_edit, stretch=1)

        self._replace_check = QCheckBox("Replace base system context")
        self._replace_check.setFont(small)
        self._replace_check.setChecked(getattr(self.agent, "_system_prompt_replace", False))
        self._replace_check.setToolTip(
            "On: this prompt becomes the ENTIRE system context — the base prompt "
            "from Settings is ignored (total control for this conversation).\n"
            "Off: layered on top of the base prompt.")
        pl.addWidget(self._replace_check)

        # Include timestamps in context
        self._timestamps_check = QCheckBox("Include Timestamps in Context")
        self._timestamps_check.setFont(small)
        self._timestamps_check.setChecked(
            getattr(self.agent, "_include_context_timestamps", True))
        pl.addWidget(self._timestamps_check)

        # Live token streaming (per-conversation). Off = only the final reply is
        # posted (no token-by-token streaming) — pairs well with self-review.
        self._stream_live_check = QCheckBox("Stream responses live (token by token)")
        self._stream_live_check.setFont(small)
        self._stream_live_check.setChecked(getattr(self.agent, "_stream_live", True))
        self._stream_live_check.setToolTip(
            "On: watch the response stream in as it's generated.\n"
            "Off: only the final, complete response is posted (recommended with "
            "self-review/reflect so you never see drafts being rewritten).")
        pl.addWidget(self._stream_live_check)

        tabs.addTab(prompt_tab, "Prompt")

        # ═══════════════════════════════════════════════════════════
        # Tab 3: Memory Streams — subscribe with read/write permissions
        # ═══════════════════════════════════════════════════════════
        streams_tab = QWidget()
        sl = QVBoxLayout(streams_tab)
        sl.setContentsMargins(10, 10, 10, 10)
        sl.setSpacing(8)

        # Available vs Subscribed
        lists_row = QHBoxLayout()

        # Available streams
        avail_panel = QVBoxLayout()
        avail_panel.addWidget(QLabel("Available"))
        self._avail_list = QListWidget()
        self._avail_list.setFont(mono9)
        avail_panel.addWidget(self._avail_list, stretch=1)
        lists_row.addLayout(avail_panel)

        # Arrow buttons
        arrow_panel = QVBoxLayout()
        arrow_panel.addStretch()
        add_btn = QPushButton("Add")
        add_btn.setFont(QFont("Consolas", 8))
        add_btn.setFixedWidth(50)
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self._subscribe_selected)
        arrow_panel.addWidget(add_btn)
        rem_btn = QPushButton("Remove")
        rem_btn.setFont(QFont("Consolas", 8))
        rem_btn.setFixedWidth(50)
        rem_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        rem_btn.clicked.connect(self._unsubscribe_selected)
        arrow_panel.addWidget(rem_btn)
        arrow_panel.addStretch()
        lists_row.addLayout(arrow_panel)

        # Subscribed streams
        sub_panel = QVBoxLayout()
        sub_panel.addWidget(QLabel("Subscribed"))
        self._sub_list = QListWidget()
        self._sub_list.setFont(mono9)
        self._sub_list.currentRowChanged.connect(self._on_sub_selected)
        sub_panel.addWidget(self._sub_list, stretch=1)
        lists_row.addLayout(sub_panel)

        sl.addLayout(lists_row, stretch=1)

        # Read/Write permissions for selected stream
        self._perms_group = QFrame()
        self._perms_group.setFrameShape(QFrame.Shape.NoFrame)
        perms_group = self._perms_group
        perms_layout = QHBoxLayout(perms_group)
        perms_layout.setContentsMargins(0, 4, 0, 0)
        perms_layout.setSpacing(12)

        self._perm_label = QLabel("Select a subscribed stream")
        self._perm_label.setFont(small)
        self._perm_label.setStyleSheet(f"color:{p['muted_text']};border:none;")
        perms_layout.addWidget(self._perm_label)
        perms_layout.addStretch()

        self._read_check = QCheckBox("Read From")
        self._read_check.setFont(small)
        self._read_check.setChecked(True)
        self._read_check.stateChanged.connect(self._on_perm_changed)
        perms_layout.addWidget(self._read_check)

        self._write_check = QCheckBox("Write Back To")
        self._write_check.setFont(small)
        self._write_check.setChecked(True)
        self._write_check.stateChanged.connect(self._on_perm_changed)
        perms_layout.addWidget(self._write_check)

        sl.addWidget(perms_group)

        # Rolling summaries per stream
        sl.addWidget(QLabel("Rolling Summaries"))
        self._summary_edits = {}
        summ = self.agent.summarizer
        stream_configs = self.agent._get_stream_configs()
        summ._ensure_streams(stream_configs)

        for sc in stream_configs:
            name = sc.get("name", "")
            ss = summ._streams.get(name)
            summary_text = ss.current_summary if ss else None

            lbl = QLabel(f"[{name}]")
            lbl.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            lbl.setStyleSheet(f"color:{p['accent']};border:none;")
            lbl.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            sl.addWidget(lbl)

            se = QTextEdit()
            se.setFont(QFont("Consolas", 8))
            se.setMaximumHeight(80)
            se.setPlaceholderText("No summary yet.")
            set_rolling_summary_content(se, summary_text or "", fg=p["text"])
            sl.addWidget(se)
            self._summary_edits[name] = se

        tabs.addTab(streams_tab, "Streams")

        # ═══════════════════════════════════════════════════════════
        # Tab 4: Debug — per-conversation LLM context (SQLite-backed)
        # ═══════════════════════════════════════════════════════════
        self._debug_panel = None
        if self._conv_id:
            self._debug_panel = DebugPanel(conversation_id=self._conv_id, parent=self)
            tabs.addTab(self._debug_panel, "Debug")

        layout.addWidget(tabs)

        # Save / Close
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_btn = QPushButton("Save")
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._save_and_close)
        btn_row.addWidget(save_btn)
        close_btn = QPushButton("Cancel")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Load stream data
        cfg = load_config()
        all_stream_names = [s["name"] for s in cfg.get("memory_streams", []) if s.get("name")]

        # Get current subscriptions with read/write flags
        raw_subs = getattr(self.agent, "_conversation_streams", None) or []
        if not raw_subs:
            raw_subs = [s["name"] for s in cfg.get("memory_streams", []) if s.get("auto_subscribe")]
        self._subscribed = _normalize_streams(raw_subs)
        self._all_stream_names = all_stream_names
        self._refresh_stream_lists()

    # ── Stream management ───────────────────────────────────────

    def _subscribed_names(self) -> list[str]:
        return [s["name"] for s in self._subscribed]

    def _refresh_stream_lists(self):
        self._sub_list.blockSignals(True)
        self._avail_list.clear()
        self._sub_list.clear()
        sub_names = self._subscribed_names()
        for name in self._all_stream_names:
            if name not in sub_names:
                self._avail_list.addItem(name)
        for s in self._subscribed:
            flags = []
            if s["read"]:
                flags.append("R")
            if s["write"]:
                flags.append("W")
            label = f"{s['name']}  [{'/'.join(flags)}]" if flags else s["name"]
            self._sub_list.addItem(label)
        self._sub_list.blockSignals(False)

        # Auto-select first subscribed if any, grey out perms if none
        if self._subscribed:
            self._sub_list.setCurrentRow(0)
            self._perms_group.setEnabled(True)
            self._on_sub_selected(0)
        else:
            self._perms_group.setEnabled(False)
            self._perm_label.setText("No streams subscribed")
            self._read_check.setChecked(False)
            self._write_check.setChecked(False)

    def _on_sub_selected(self, row):
        if row < 0 or row >= len(self._subscribed):
            self._perm_label.setText("Select a subscribed stream")
            self._read_check.blockSignals(True)
            self._write_check.blockSignals(True)
            self._read_check.setChecked(True)
            self._write_check.setChecked(True)
            self._read_check.blockSignals(False)
            self._write_check.blockSignals(False)
            return
        s = self._subscribed[row]
        self._perm_label.setText(f"{s['name']}:")
        self._read_check.blockSignals(True)
        self._write_check.blockSignals(True)
        self._read_check.setChecked(s.get("read", True))
        self._write_check.setChecked(s.get("write", True))
        self._read_check.blockSignals(False)
        self._write_check.blockSignals(False)

    def _on_perm_changed(self):
        row = self._sub_list.currentRow()
        if row < 0 or row >= len(self._subscribed):
            return
        self._subscribed[row]["read"] = self._read_check.isChecked()
        self._subscribed[row]["write"] = self._write_check.isChecked()
        self._refresh_stream_lists()
        self._sub_list.setCurrentRow(row)

    def _subscribe_selected(self):
        item = self._avail_list.currentItem()
        if not item:
            return
        name = item.text()
        if name not in self._subscribed_names():
            self._subscribed.append({"name": name, "read": True, "write": True})
        self._refresh_stream_lists()

    def _unsubscribe_selected(self):
        row = self._sub_list.currentRow()
        if row < 0 or row >= len(self._subscribed):
            return
        self._subscribed.pop(row)
        self._refresh_stream_lists()

    # ── Save ────────────────────────────────────────────────────

    def _refresh_main_model_suggestions(self):
        pid = self._provider_combo.currentData()
        models = list(self._provider_model_history.get(pid) or [])
        self._main_model_suggest_model.setStringList(models)

    def _on_provider_changed(self, idx):
        from core.providers import PROVIDER_INFO
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

    def _save_and_close(self):
        from core.providers import PROVIDER_INFO
        from core.agent import load_config, save_config

        # Model tab
        pid = self._provider_combo.currentData()
        model = self._model_edit.text().strip()
        if model:
            touch_provider_model_choice(
                self._provider_models,
                self._provider_model_history,
                pid,
                model,
            )
        cfg = load_config()
        cfg["provider_models"] = self._provider_models
        cfg["provider_model_history"] = self._provider_model_history
        save_config(cfg)

        if pid:
            self.agent.set_provider(pid)
        if model:
            self.agent.set_model(model)
        ws = self._ws_combo.currentText()
        if ws:
            self.agent.set_workspace(ws)

        # Prompt tab
        prompt = self._prompt_edit.toPlainText().strip()
        self.agent.set_system_prompt_override(prompt)
        self.agent._system_prompt_replace = self._replace_check.isChecked()
        self.agent._include_context_timestamps = self._timestamps_check.isChecked()
        # Per-conversation live streaming → apply to the agent + persist.
        self.agent._stream_live = self._stream_live_check.isChecked()
        if self._conv_id:
            try:
                from core.database import set_conversation_stream_live
                set_conversation_stream_live(self._conv_id, self.agent._stream_live)
            except Exception:
                pass

        # Streams tab — save summaries
        for name, se in self._summary_edits.items():
            edited = rolling_summary_plain_from_edit(se).strip()
            if name in self.agent.summarizer._streams:
                ss = self.agent.summarizer._streams[name]
                ss.current_summary = edited if edited else None
                ss.save_state()

        # Streams tab — save with read/write permissions
        self.agent.set_conversation_streams(self._subscribed)
        if self._conv_id:
            from core.database import set_conversation_streams
            import json
            set_conversation_streams(self._conv_id, self._subscribed)

        self.accept()
