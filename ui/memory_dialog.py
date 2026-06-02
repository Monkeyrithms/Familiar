"""
Memory dialog — dedicated window for managing memory streams, notes, and summaries.

Layout (top to bottom):
  1. Memory Streams selector (stream list + name/focus/auto-subscribe)
  2. Notes browser/editor (category tree ↔ morphing list/editor)
  3. Rolling Summary config (per-stream guidance, char limits)
"""

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFormLayout, QGroupBox,
    QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit, QCheckBox,
    QListWidget, QListWidgetItem, QSplitter, QTreeWidget, QTreeWidgetItem,
    QStackedWidget, QInputDialog, QSizePolicy, QHeaderView,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from ui.theme import PALETTE
from ui.glass_dialog import GlassDialog
from ui.rolling_summary_format import (
    rolling_summary_plain_from_edit,
    set_rolling_summary_content,
)
from core.agent import load_config, save_config


class MemoryDialog(GlassDialog):
    def __init__(self, parent=None, initial_stream: str = ""):
        super().__init__(title="Memory", parent=parent, width=950, height=880)
        self._initial_stream = initial_stream
        self._build_ui()

    def _build_ui(self):
        layout = self.content_layout()
        layout.setSpacing(6)

        cfg = load_config()
        p = PALETTE
        small_font = QFont("Consolas", 8)
        mono9 = QFont("Consolas", 9)
        muted = f"color:{p['muted_text']};font-size:8pt;background:transparent;border:none;"

        # ═══════════════════════════════════════════════════════════════
        # Section 1: Memory Streams selector
        # ═══════════════════════════════════════════════════════════════
        streams_group = QGroupBox("Memory Streams")
        streams_layout = QVBoxLayout(streams_group)
        streams_layout.setSpacing(4)
        streams_layout.setContentsMargins(6, 12, 6, 6)

        stream_top = QHBoxLayout()
        self._stream_list = QListWidget()
        self._stream_list.setFixedHeight(72)
        self._stream_list.setFont(mono9)
        self._stream_list.currentRowChanged.connect(self._on_stream_selected)
        stream_top.addWidget(self._stream_list, stretch=1)

        stream_btns = QVBoxLayout()
        stream_btns.setSpacing(2)
        for label, slot in [("+", "_add_stream"), ("Duplicate", "_duplicate_stream"),
                            ("\u2212", "_remove_stream")]:
            btn = QPushButton(label)
            btn.setFont(small_font)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedWidth(90)
            btn.clicked.connect(getattr(self, slot))
            stream_btns.addWidget(btn)
            if label == "\u2212":
                self._stream_remove_btn = btn
        stream_btns.addStretch()
        stream_top.addLayout(stream_btns)
        streams_layout.addLayout(stream_top)

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
        # Section 2: Notes — tree above, detail editor below (Hybrid-style)
        # ═══════════════════════════════════════════════════════════════
        notes_group = QGroupBox("Notes")
        notes_layout = QVBoxLayout(notes_group)
        notes_layout.setSpacing(0)
        notes_layout.setContentsMargins(6, 12, 6, 6)

        notes_vsplit = QSplitter(Qt.Orientation.Vertical)

        # ── Upper: tree + buttons ──
        tree_panel = QWidget()
        tree_lay = QVBoxLayout(tree_panel)
        tree_lay.setContentsMargins(0, 0, 0, 0)
        tree_lay.setSpacing(4)

        self._notes_tree = QTreeWidget()
        self._notes_tree.setHeaderHidden(True)
        self._notes_tree.setColumnCount(2)
        self._notes_tree.setFont(mono9)
        self._notes_tree.setRootIsDecorated(True)
        self._notes_tree.setColumnWidth(0, 180)
        self._notes_tree.setMinimumHeight(160)
        self._notes_tree.setWordWrap(False)
        self._notes_tree.setTextElideMode(Qt.TextElideMode.ElideRight)
        self._notes_tree.setUniformRowHeights(True)
        self._notes_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._notes_tree.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        hdr = self._notes_tree.header()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._notes_tree.currentItemChanged.connect(self._on_note_tree_click)
        tree_lay.addWidget(self._notes_tree, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(4)
        for label, slot in [("+", "_add_item"), ("Rename", "_rename_selected"),
                            ("\u2212", "_delete_selected")]:
            b = QPushButton(label)
            b.setFont(small_font)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(getattr(self, slot))
            btn_row.addWidget(b)
        btn_row.addStretch()
        tree_lay.addLayout(btn_row)
        notes_vsplit.addWidget(tree_panel)

        # ── Lower: detail editor (shows when a note leaf is selected) ──
        detail_panel = QWidget()
        det_lay = QVBoxLayout(detail_panel)
        det_lay.setContentsMargins(0, 4, 0, 0)
        det_lay.setSpacing(4)

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
        det_lay.addLayout(path_row)

        self._note_content_edit = QTextEdit()
        self._note_content_edit.setFont(mono9)
        self._note_content_edit.setAcceptRichText(False)
        self._note_content_edit.setPlaceholderText("Select a note above, or click + to create one")
        det_lay.addWidget(self._note_content_edit, stretch=1)

        kw_row = QHBoxLayout()
        kw_row.setSpacing(4)
        kw_label = QLabel("Keywords")
        kw_label.setFont(small_font)
        kw_label.setFixedWidth(55)
        kw_row.addWidget(kw_label)
        self._note_keywords_edit = QLineEdit()
        self._note_keywords_edit.setFont(mono9)
        self._note_keywords_edit.setPlaceholderText("comma-separated regex patterns")
        kw_row.addWidget(self._note_keywords_edit, stretch=1)
        det_lay.addLayout(kw_row)

        kw_hint = QLabel(
            "Keywords: comma-separated regex patterns. While you chat, the Agent scans each "
            "message; if any pattern matches, this note is pulled into context as a recall hint "
            "(regex only — no extra LLM call)."
        )
        kw_hint.setWordWrap(True)
        kw_hint.setFont(small_font)
        kw_hint.setStyleSheet(muted)
        det_lay.addWidget(kw_hint)

        edit_btn_row = QHBoxLayout()
        edit_btn_row.setSpacing(4)
        save_btn = QPushButton("Save")
        save_btn.setFont(small_font)
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.clicked.connect(self._save_current_note)
        edit_btn_row.addWidget(save_btn)
        self._note_status = QLabel("")
        self._note_status.setFont(small_font)
        self._note_status.setStyleSheet(muted)
        edit_btn_row.addWidget(self._note_status, stretch=1)
        det_lay.addLayout(edit_btn_row)

        notes_vsplit.addWidget(detail_panel)
        notes_vsplit.setChildrenCollapsible(False)
        notes_vsplit.setStretchFactor(0, 1)
        notes_vsplit.setStretchFactor(1, 2)
        notes_vsplit.setSizes([260, 240])
        notes_layout.addWidget(notes_vsplit)

        self._notes_current_cat = ""
        layout.addWidget(notes_group, stretch=1)

        # ═══════════════════════════════════════════════════════════════
        # Section 3: Rolling Summary (per stream)
        # ═══════════════════════════════════════════════════════════════
        summary_group = QGroupBox("Stream Summaries")
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

        # ── High-level overview (cross-session, per stream) ──
        overview_row = QHBoxLayout()
        overview_row.setSpacing(6)
        overview_row.addWidget(QLabel("High-Level Overview (cross-session)"))
        self._stream_overview_status = QLabel("")
        self._stream_overview_status.setFont(small_font)
        overview_row.addWidget(self._stream_overview_status)
        overview_row.addStretch()
        clear_overview_btn = QPushButton("Clear")
        clear_overview_btn.setFont(small_font)
        clear_overview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        clear_overview_btn.clicked.connect(self._clear_stream_overview)
        overview_row.addWidget(clear_overview_btn)
        save_overview_btn = QPushButton("Save Overview")
        save_overview_btn.setFont(small_font)
        save_overview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_overview_btn.clicked.connect(self._save_stream_overview)
        overview_row.addWidget(save_overview_btn)
        summary_layout.addLayout(overview_row)

        self._stream_overview_edit = QTextEdit()
        self._stream_overview_edit.setFont(QFont("Consolas", 8))
        self._stream_overview_edit.setFixedHeight(80)
        self._stream_overview_edit.setAcceptRichText(False)
        self._stream_overview_edit.setPlaceholderText(
            "Enduring priorities/observations for this stream. Fed to the LLM "
            "every turn as the stream's high-level memory. Leave empty to fall "
            "back to the latest prior-conversation summary."
        )
        summary_layout.addWidget(self._stream_overview_edit)

        # ── Per-conversation (low-level recent activity) summary viewer/editor ──
        sum_conv_row = QHBoxLayout()
        sum_conv_row.setSpacing(6)
        sum_conv_row.addWidget(QLabel("Low-Level Recent Activity \u2014 Conversation"))
        self._summary_conv_combo = QComboBox()
        self._summary_conv_combo.setFont(mono9)
        self._summary_conv_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._summary_conv_combo.currentIndexChanged.connect(self._on_summary_conv_changed)
        sum_conv_row.addWidget(self._summary_conv_combo, stretch=1)
        save_sum_btn = QPushButton("Save Summary")
        save_sum_btn.setFont(small_font)
        save_sum_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_sum_btn.clicked.connect(self._save_summary_edit)
        sum_conv_row.addWidget(save_sum_btn)
        summary_layout.addLayout(sum_conv_row)

        self._summary_edit = QTextEdit()
        self._summary_edit.setFont(QFont("Consolas", 8))
        self._summary_edit.setFixedHeight(90)
        self._summary_edit.setAcceptRichText(True)
        self._summary_edit.setPlaceholderText("No summary for this conversation yet.")
        summary_layout.addWidget(self._summary_edit)

        layout.addWidget(summary_group)

        # ── Save button ──
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        save_all = QPushButton("Save")
        save_all.setCursor(Qt.CursorShape.PointingHandCursor)
        save_all.clicked.connect(self._save_and_close)
        close_btn = QPushButton("Close")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)
        btn_row.addWidget(save_all)
        layout.addLayout(btn_row)

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

        # Jump to the requested stream if provided
        if self._initial_stream:
            for i, s in enumerate(self._streams_data):
                if s.get("name") == self._initial_stream:
                    self._stream_list.setCurrentRow(i)
                    break

    # ── Stream management ───────────────────────────────────────────

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
        self._stream_remove_btn.setEnabled(row != 0)
        self._refresh_notes_tree()
        self._refresh_summary_combo()
        self._load_stream_overview_into_editor(s.get("name", ""))
        self._notes_current_cat = ""
        self._note_category_edit.clear()
        self._note_title_edit.clear()
        self._note_content_edit.clear()
        self._note_status.setText("")

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
        self._streams_data.append({
            "name": f"stream_{len(self._streams_data)}", "description": "",
            "summary_guidance": "", "auto_subscribe": False
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
        base = source["name"]
        n = 1
        while any(s["name"] == f"{base} {n}" for s in self._streams_data):
            n += 1
        new_name = f"{base} {n}"
        self._streams_data.append({
            "name": new_name, "description": source.get("description", ""),
            "summary_guidance": source.get("summary_guidance", ""), "auto_subscribe": False,
        })

        # Copy the source stream's database (all summaries, notes, etc.)
        try:
            import shutil
            from core.database import _stream_db_path, init_stream_db
            src_path = _stream_db_path(base)
            dst_path = _stream_db_path(new_name)
            if src_path.exists():
                shutil.copy2(str(src_path), str(dst_path))
            else:
                init_stream_db(new_name)
        except Exception:
            pass

        self._populate_stream_list()
        self._stream_list.setCurrentRow(len(self._streams_data) - 1)

    def _remove_stream(self):
        row = self._stream_list.currentRow()
        if row < 0 or len(self._streams_data) <= 1:
            return
        self._streams_data.pop(row)
        self._populate_stream_list()

    # ── Notes browser (Hybrid-style: tree above, editor below) ────

    def _current_stream_name(self) -> str:
        row = self._stream_list.currentRow()
        if row < 0 or row >= len(self._streams_data):
            return ""
        return self._streams_data[row].get("name", "")

    def _refresh_notes_tree(self):
        """Rebuild tree: categories as branches, notes as leaves with preview.
        Notes are inserted BEFORE subcategories so they don't visually nest under them."""
        self._notes_tree.clear()
        stream = self._current_stream_name()
        if not stream:
            return
        try:
            from core.database import list_note_categories, list_notes_in_category, read_note
            cats = list_note_categories(stream)
        except Exception:
            return

        # Collect all category paths and their direct notes
        all_paths = set()
        cat_counts = {}
        cat_notes = {}  # path -> [(title, preview)]
        for cat in cats:
            path = cat["category"]
            cat_counts[path] = cat["count"]
            parts = path.split("/")
            for i in range(len(parts)):
                all_paths.add("/".join(parts[:i + 1]))
            # Load direct notes for this category
            try:
                note_list = list_notes_in_category(stream, path)
                notes_for_cat = []
                for n in note_list:
                    full = read_note(stream, path, n["title"])
                    preview = ""
                    if full and full.get("content"):
                        raw = full["content"].replace("\n", " ").replace("\r", " ").strip()
                        max_len = 48
                        preview = raw if len(raw) <= max_len else raw[: max_len - 1] + "\u2026"
                    notes_for_cat.append((n["title"], preview))
                if notes_for_cat:
                    cat_notes[path] = notes_for_cat
            except Exception:
                pass

        # Build tree: for each category, add its direct notes first, then subcategories
        # Process paths shortest-first so parents exist before children
        nodes = {}
        for path in sorted(all_paths):
            parts = path.split("/")
            label = parts[-1]
            count = sum(c for p, c in cat_counts.items() if p == path or p.startswith(path + "/"))
            cat_item = QTreeWidgetItem([f"{label} ({count})", ""])
            cat_item.setData(0, Qt.ItemDataRole.UserRole, {"type": "category", "path": path})

            parent_path = "/".join(parts[:-1])
            parent = nodes.get(parent_path) if len(parts) > 1 else None
            if parent:
                parent.addChild(cat_item)
            else:
                self._notes_tree.addTopLevelItem(cat_item)
            nodes[path] = cat_item

        # Now add note leaves — insert at position 0 so they appear ABOVE subcategories
        for path in sorted(all_paths):
            cat_item = nodes.get(path)
            if not cat_item or path not in cat_notes:
                continue
            for title, preview in cat_notes[path]:
                leaf = QTreeWidgetItem([title, preview])
                leaf.setData(0, Qt.ItemDataRole.UserRole, {
                    "type": "note", "category": path, "title": title
                })
                cat_item.insertChild(0, leaf)

        self._notes_tree.expandAll()

    def _on_note_tree_click(self, current, previous):
        """Click a tree item — if note, populate editor below. If category, clear editor."""
        if not current:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return

        if data["type"] == "note":
            stream = self._current_stream_name()
            if not stream:
                return
            self._notes_current_cat = data.get("category", "")
            try:
                from core.database import read_note
                note = read_note(stream, data["category"], data["title"])
                if note:
                    self._note_category_edit.setText(note["category"])
                    self._note_title_edit.setText(note["title"])
                    self._note_content_edit.setPlainText(note["content"])
                    self._note_keywords_edit.setText(note.get("keywords", ""))
                    self._note_status.setText(f"{len(note['content'])} chars")
            except Exception:
                pass
        else:
            # Category selected — clear editor, update current cat
            self._notes_current_cat = data.get("path", "")
            self._note_category_edit.clear()
            self._note_title_edit.clear()
            self._note_content_edit.clear()
            self._note_keywords_edit.clear()
            self._note_status.setText("")

    def _get_selected_data(self) -> dict | None:
        current = self._notes_tree.currentItem()
        if not current:
            return None
        return current.data(0, Qt.ItemDataRole.UserRole)

    def _selected_category_path(self) -> str:
        """Get the category path from whatever is selected (category or note)."""
        data = self._get_selected_data()
        if not data:
            return ""
        return data["path"] if data["type"] == "category" else data.get("category", "")

    def _add_item(self):
        """+ button: context-aware add."""
        stream = self._current_stream_name()
        if not stream:
            return
        parent = self._selected_category_path()

        if not parent:
            # Nothing selected — top-level category
            name, ok = QInputDialog.getText(self, "New Category", "Top-level category name:")
            if ok and name.strip():
                from core.database import create_category
                create_category(stream, name.strip())
                self._notes_current_cat = name.strip()
                self._refresh_notes_tree()
            return

        # Something selected — ask what to add
        from PyQt6.QtWidgets import QMessageBox
        msg = QMessageBox(self)
        msg.setWindowTitle("Add")
        msg.setText(f"Add to '{parent}':")
        cat_btn = msg.addButton("Sub-category", QMessageBox.ButtonRole.ActionRole)
        note_btn = msg.addButton("Note", QMessageBox.ButtonRole.ActionRole)
        msg.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        msg.exec()

        if msg.clickedButton() == cat_btn:
            name, ok = QInputDialog.getText(self, "New Sub-category",
                                             f"Name (under '{parent}'):")
            if ok and name.strip():
                new_path = f"{parent}/{name.strip()}"
                from core.database import create_category
                create_category(stream, new_path)
                self._notes_current_cat = new_path
                self._refresh_notes_tree()
        elif msg.clickedButton() == note_btn:
            self._note_category_edit.setText(parent)
            self._note_title_edit.clear()
            self._note_content_edit.clear()
            self._note_status.setText(f"New note in {parent}")
            self._note_title_edit.setFocus()

    def _rename_selected(self):
        """Rename whatever is selected."""
        data = self._get_selected_data()
        if not data:
            return
        stream = self._current_stream_name()
        if not stream:
            return

        if data["type"] == "category":
            old_path = data["path"]
            old_leaf = old_path.rsplit("/", 1)[-1]
            new_name, ok = QInputDialog.getText(self, "Rename", f"Rename '{old_leaf}':", text=old_leaf)
            if not ok or not new_name.strip() or new_name.strip() == old_leaf:
                return
            parts = old_path.rsplit("/", 1)
            new_path = f"{parts[0]}/{new_name.strip()}" if len(parts) > 1 else new_name.strip()
            from core.database import rename_category, delete_category_entry, create_category
            rename_category(stream, old_path, new_path)
            delete_category_entry(stream, old_path)
            create_category(stream, new_path)
            self._notes_current_cat = new_path
            self._refresh_notes_tree()

        elif data["type"] == "note":
            old_title = data["title"]
            new_title, ok = QInputDialog.getText(self, "Rename", f"Rename '{old_title}':", text=old_title)
            if not ok or not new_title.strip() or new_title.strip() == old_title:
                return
            from core.database import rename_note
            rename_note(stream, data["category"], old_title, new_title.strip())
            self._note_title_edit.setText(new_title.strip())
            self._refresh_notes_tree()

    def _delete_selected(self):
        """Delete whatever is selected."""
        data = self._get_selected_data()
        if not data:
            return
        stream = self._current_stream_name()
        if not stream:
            return

        if data["type"] == "category":
            path = data["path"]
            if not GlassDialog.confirm(self, "Delete", f"Delete '{path}' and everything inside?"):
                return
            from core.database import list_note_categories, list_notes_in_category, delete_note, delete_category_entry
            for cat in list_note_categories(stream):
                cp = cat["category"]
                if cp == path or cp.startswith(path + "/"):
                    for n in list_notes_in_category(stream, cp):
                        delete_note(stream, cp, n["title"])
            delete_category_entry(stream, path)
            self._notes_current_cat = ""
            self._note_category_edit.clear()
            self._note_title_edit.clear()
            self._note_content_edit.clear()
            self._note_status.setText("")
            self._refresh_notes_tree()

        elif data["type"] == "note":
            from core.database import delete_note
            delete_note(stream, data["category"], data["title"])
            self._note_category_edit.clear()
            self._note_title_edit.clear()
            self._note_content_edit.clear()
            self._note_status.setText("Deleted")
            self._refresh_notes_tree()

    def _save_current_note(self):
        stream = self._current_stream_name()
        if not stream:
            return
        category = self._note_category_edit.text().strip()
        title = self._note_title_edit.text().strip()
        content = self._note_content_edit.toPlainText().strip()
        keywords = self._note_keywords_edit.text().strip()
        if not category or not title:
            self._note_status.setText("Need category + title")
            return
        if not content:
            self._note_status.setText("Note is empty")
            return
        from core.database import save_note
        save_note(stream, category, title, content, keywords=keywords)
        self._note_status.setText("Saved")
        self._refresh_notes_tree()

    # ── Summary viewer/editor ────────────────────────────────────────

    def _refresh_summary_combo(self):
        """Populate the conversation combo with all summaries for the current stream."""
        self._summary_conv_combo.blockSignals(True)
        self._summary_conv_combo.clear()
        stream = self._current_stream_name()
        if not stream:
            self._summary_conv_combo.blockSignals(False)
            return
        try:
            from core.database import list_stream_summaries, list_conversations
            summaries = list_stream_summaries(stream)
            if not summaries:
                set_rolling_summary_content(self._summary_edit, "", fg=PALETTE["text"])
                self._summary_conv_combo.blockSignals(False)
                return
            # Build a conv_id -> name map
            conv_names = {c["id"]: c["name"] for c in list_conversations()}
            for s in summaries:
                cid = s["conversation_id"]
                label = conv_names.get(cid, cid)
                self._summary_conv_combo.addItem(label, userData=s)
        except Exception:
            pass
        self._summary_conv_combo.blockSignals(False)
        self._on_summary_conv_changed(self._summary_conv_combo.currentIndex())

    def _on_summary_conv_changed(self, index: int):
        if index < 0:
            set_rolling_summary_content(self._summary_edit, "", fg=PALETTE["text"])
            return
        data = self._summary_conv_combo.itemData(index)
        if data:
            set_rolling_summary_content(
                self._summary_edit, data.get("summary", ""), fg=PALETTE["text"])
        else:
            set_rolling_summary_content(self._summary_edit, "", fg=PALETTE["text"])

    def _save_summary_edit(self):
        stream = self._current_stream_name()
        if not stream:
            return
        index = self._summary_conv_combo.currentIndex()
        if index < 0:
            return
        data = self._summary_conv_combo.itemData(index)
        if not data:
            return
        conv_id = data["conversation_id"]
        new_text = rolling_summary_plain_from_edit(self._summary_edit)
        try:
            from core.database import update_stream_summary_text
            update_stream_summary_text(stream, conv_id, new_text)
            # Update the cached data in the combo too
            data["summary"] = new_text
            self._summary_conv_combo.setItemData(index, data)
        except Exception:
            pass

    # ── High-level overview (cross-session) ─────────────────────────
    def _load_stream_overview_into_editor(self, stream_name: str):
        text = ""
        if stream_name:
            try:
                from core.database import load_stream_overview
                text = load_stream_overview(stream_name)
            except Exception:
                pass
        self._stream_overview_edit.blockSignals(True)
        self._stream_overview_edit.setPlainText(text)
        self._stream_overview_edit.blockSignals(False)
        self._stream_overview_status.setText(
            "(authored)" if text.strip() else "(empty \u2014 no overview in context)")

    def _save_stream_overview(self):
        stream = self._current_stream_name()
        if not stream:
            return
        text = self._stream_overview_edit.toPlainText().strip()
        try:
            from core.database import save_stream_overview
            save_stream_overview(stream, text)
        except Exception:
            return
        self._load_stream_overview_into_editor(stream)

    def _clear_stream_overview(self):
        stream = self._current_stream_name()
        if not stream:
            return
        try:
            from core.database import save_stream_overview
            save_stream_overview(stream, "")
        except Exception:
            return
        self._load_stream_overview_into_editor(stream)

    # ── Save ────────────────────────────────────────────────────────

    def _save_and_close(self):
        cfg = load_config()
        streams = [s for s in self._streams_data if s.get("name", "").strip()]
        if not streams:
            streams = [{"name": "General", "description": "General-purpose conversation memory",
                        "summary_guidance": "", "auto_subscribe": True}]
        cfg["memory_streams"] = streams
        cfg["enable_summarization"] = self._enable_summary_combo.currentData()
        try:
            cfg["summary_char_limit"] = max(1000, int(self._summary_char_limit.text()))
        except ValueError:
            pass
        try:
            cfg["summary_refresh_chars"] = max(500, int(self._summary_refresh.text()))
        except ValueError:
            pass
        save_config(cfg)
        self.accept()
