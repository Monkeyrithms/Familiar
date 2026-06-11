"""
ChatWindow — the workspace coordinator.

Historically `ChatWindow` (now `ChatColumn`, in ui/chat_widget.py) was a single
monolithic chat surface. To support multiple concurrent conversations side-by-side
as COLUMNS (mirroring the Terminal tool's column mode), the per-conversation chat
was kept as `ChatColumn` and this thin coordinator was introduced to own the
WORKSPACE-WIDE pieces that must be shared across every column:

  • the conversation/workspace bar + "Conversation" button (one bar drives all),
  • the single shared right-side panel (terminals / file viewer / browser),
  • the horizontal splitter  [ columns-host | shared workspace ]  and the
    columns-host splitter that lays the chat columns out as columns.

A `ChatColumn` reaches back here for those shared widgets (via the `coordinator`
ref passed into it). Anything the rest of the app calls on the old ChatWindow
that is genuinely per-conversation (`input`, `_auto_save`, `_stop_inference`, …)
is delegated to the *focused* column through `__getattr__`, so this stays a
drop-in replacement for the former monolith.

Phase 1 hosts exactly ONE (primary) column — behaviour-identical to before.
Multi-column add/remove + unified focus arrive in a later phase.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QSplitter
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

from core.agent import Agent
from core.conversations import (
    list_workspaces, get_workspace, create_workspace, rename_workspace,
    delete_workspace, set_workspace_streams, get_workspace_streams,
    set_workspace_columns, set_workspace_active_conv,
    add_conversation_to_workspace, remove_conversation_from_workspace,
    workspace_for_conversation, list_conversations,
)
from ui.theme import PALETTE
from ui.conversation_bar import ConversationBar
from ui.right_workspace import RightWorkspacePanel
from ui.chat_widget import ChatColumn, HoverSoundSplitter


class ChatWindow(QWidget):
    """Coordinator hosting one-or-more `ChatColumn`s beside a shared workspace."""

    # Re-emitted from the primary column once its initial conversation hydrates —
    # main() listens on this to fade out the splash screen.
    initial_load_finished = pyqtSignal()

    def __init__(self, agent: Agent, parent=None):
        super().__init__(parent)
        self.agent = agent
        self._columns: list[ChatColumn] = []
        self._focused_column: ChatColumn | None = None
        self._shutting_down_flag = False
        # True only during startup, until _restore_workspace_columns has replayed
        # the saved columns. While set, _persist_current_workspace_columns is a
        # no-op so a column finishing its initial load (and calling _bind_column)
        # cannot overwrite the saved col_order_json with just the columns that
        # happen to exist yet — the bug that lost every extra column on restart.
        self._restoring = True
        # The dropdown selects a WORKSPACE; its columns are concurrent
        # conversations sharing the workspace's memory streams.
        self._current_workspace_id = self._determine_startup_workspace()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(4)

        # ── Workspace-wide bar ──
        #   [ workspace ▼ ][−ws][+ws] … hint … [ Conversation ][− col][+ col]
        # The dropdown's −/+ add/remove WORKSPACES; the −/+ to the RIGHT of the
        # Conversation button add/remove concurrent chat COLUMNS in the current
        # workspace.
        from ui.conversation_bar import _ConvActionButton
        conv_row = QHBoxLayout()
        conv_row.setContentsMargins(0, 0, 4, 0)
        conv_row.setSpacing(4)
        self.conv_bar = ConversationBar()
        conv_row.addWidget(self.conv_bar, stretch=1)
        self._conv_btn = QPushButton("Conversation")
        self._conv_btn.setObjectName("promptBtn")
        self._conv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        conv_row.addWidget(self._conv_btn)

        # Column controls — to the RIGHT of the Conversation button.
        self._col_del_btn = _ConvActionButton("minus", self)
        self._col_del_btn.setFixedSize(24, 24)
        self._col_del_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._col_del_btn.setToolTip("Close the focused chat column")
        self._col_del_btn.clicked.connect(self.remove_focused_column)
        conv_row.addWidget(self._col_del_btn)
        self._col_new_btn = _ConvActionButton("plus", self)
        self._col_new_btn.setFixedSize(24, 24)
        self._col_new_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._col_new_btn.setToolTip("New chat column")
        self._col_new_btn.clicked.connect(lambda: self.add_column("new"))
        conv_row.addWidget(self._col_new_btn)
        root.addLayout(conv_row)

        # ── Shared right-side tool panel (terminals / files / browser) ──
        self._right_workspace = RightWorkspacePanel()

        # ── Columns host: a horizontal splitter of ChatColumns (1 for now) ──
        self._columns_host = QSplitter(Qt.Orientation.Horizontal)
        self._columns_host.setObjectName("ChatColumnsHost")
        self._columns_host.setHandleWidth(8)
        self._columns_host.setChildrenCollapsible(False)

        # ── Outer split: [ columns-host | workspace ] (side configurable) ──
        ws_side = str(self.agent.config.get("workspace_side", "right") or "right").lower()
        self._ws_left = (ws_side == "left")
        self.chat_hsplitter = HoverSoundSplitter(Qt.Orientation.Horizontal)
        self.chat_hsplitter.setHandleWidth(10)
        self.chat_hsplitter.setContentsMargins(0, 0, 8, 0)  # clear window resize grip

        if self._ws_left:
            self.chat_hsplitter.addWidget(self._right_workspace)
            self.chat_hsplitter.addWidget(self._columns_host)
        else:
            self.chat_hsplitter.addWidget(self._columns_host)
            self.chat_hsplitter.addWidget(self._right_workspace)
        self.chat_hsplitter.setCollapsible(0, True)
        self.chat_hsplitter.setCollapsible(1, True)
        root.addWidget(self.chat_hsplitter, stretch=1)

        # ── Primary column ── loads the active workspace's first conversation
        # (or the app's last/most-recent when the workspace is empty/unknown).
        ws = get_workspace(self._current_workspace_id)
        # Snapshot the saved column order NOW, before the primary column's
        # _bind_column() can rewrite col_order_json. _restore_workspace_columns
        # replays this snapshot, so a concurrently-loading column can't clobber it.
        self._startup_columns = list(ws.get("columns", [])) if ws else []
        first_conv = (self._startup_columns[0] if self._startup_columns else None)
        primary = ChatColumn(
            agent, parent=self,
            shared_workspace=self._right_workspace,
            coordinator=self,
            is_primary=True,
            initial_conv=first_conv,
        )
        self._add_column_widget(primary)
        self._focused_column = primary

        # Style the shared splitter using the column's helper (needs a column).
        try:
            self.chat_hsplitter.setStyleSheet(primary._splitter_idle_ss(PALETTE))
        except Exception:
            pass

        # Wire the bar + Conversation button. The − / + buttons add/remove chat
        # COLUMNS; the dropdown selects a conversation into the focused column;
        # rename + the Conversation dialog act on the focused column.
        # Dropdown −/+ add/remove WORKSPACES; selecting one switches workspace;
        # rename / streams act on the workspace. (Column −/+ are wired above, to
        # the right of the Conversation button.)
        self.conv_bar.ws_new_requested.connect(self._new_workspace_prompt)
        self.conv_bar.ws_delete_requested.connect(self.delete_current_workspace)
        self.conv_bar.conversation_selected.connect(self.switch_workspace)
        self.conv_bar.rename_requested.connect(self._on_rename_workspace)
        self.conv_bar.streams_changed.connect(self._on_workspace_streams_changed)
        self.conv_bar.ws_cleanup_requested.connect(self._prompt_remove_empty_workspaces)
        # The "Conversation" button opens the focused column's conversation
        # settings dialog, exactly as before. (Workspace new/delete live on the
        # dropdown's −/+; rename + cleanup are on the dropdown's right-click.)
        self._conv_btn.clicked.connect(
            lambda: self._focused_column and self._focused_column._open_conversation_dialog())

        # Forward the primary column's initial-load signal for the splash screen.
        primary.initial_load_finished.connect(self.initial_load_finished.emit)

        self._style_bar()

        # Start with the workspace collapsed (chat gets full width), matching the
        # old single-window default.
        QTimer.singleShot(0, primary._collapse_workspace)
        # Recreate the active workspace's remaining columns + populate the bar,
        # once the primary column has loaded its conversation.
        QTimer.singleShot(400, self._restore_workspace_columns)

    # ── Workspace selection / startup ──────────────────────────────────

    def _determine_startup_workspace(self) -> str:
        """The workspace to open at launch: the one holding the app's last
        conversation, else the most-recent workspace."""
        try:
            from core.agent import load_config
            last = load_config().get("last_conversation_id", "") or ""
            if last:
                wid = workspace_for_conversation(last)
                if wid:
                    return wid
            wss = list_workspaces()
            if wss:
                return wss[0]["id"]
        except Exception:
            pass
        return ""

    def _restore_workspace_columns(self):
        """Open the active workspace's columns (beyond the primary) + refresh bar.

        Replays the column order snapshotted in __init__ (immune to interim
        col_order_json writes), then lifts the restore guard so normal
        persistence resumes."""
        try:
            valid = {c["id"] for c in list_conversations()}
        except Exception:
            valid = set()
        shown = {getattr(c, "_current_conv_id", "") for c in self._columns}
        saved = getattr(self, "_startup_columns", None)
        if saved is None:
            ws = get_workspace(self._current_workspace_id)
            saved = list(ws.get("columns", [])) if ws else []
        restored = list(saved)
        for cid in saved:
            if cid and cid not in shown and cid in valid:
                self.add_column(cid)
                shown.add(cid)
        # Restore is complete — resume persistence.
        self._restoring = False
        # Normalise col_order_json to the (validated) restored order. Deterministic
        # — uses the known conv ids, not the columns' still-loading _current_conv_id.
        restored = [c for c in restored if c in valid]
        if restored and self._current_workspace_id:
            try:
                set_workspace_columns(self._current_workspace_id, restored)
            except Exception:
                pass
        self._apply_workspace_streams_to_columns()
        self._update_bar_workspaces()

    def _update_bar_workspaces(self):
        """Populate the dropdown with WORKSPACES (id+name), highlighting the
        active one. (The bar's combo is id/name-generic.)"""
        try:
            items = [{"id": w["id"], "name": w["name"]} for w in list_workspaces()]
            self.conv_bar.set_conversations(items, self._current_workspace_id)
        except Exception:
            pass

    def switch_workspace(self, ws_id: str):
        """Dropdown selected a different workspace: persist the current one's
        columns, then rebuild the columns for `ws_id`."""
        if not ws_id or ws_id == self._current_workspace_id:
            return
        self._persist_current_workspace_columns()
        self._current_workspace_id = ws_id
        ws = get_workspace(ws_id)
        conv_ids = list(ws.get("columns", [])) if ws else []
        try:
            valid = {c["id"] for c in list_conversations()}
        except Exception:
            valid = set()
        conv_ids = [c for c in conv_ids if c in valid]
        # Drop the extra columns (keep the anchor widget; reuse it for col 0).
        for col in list(self._columns[1:]):
            self._discard_column(col)
        primary = self._primary_column
        if primary is not None:
            if conv_ids:
                primary._switch_conversation(conv_ids[0])
            else:
                primary._new_conversation()
        # Open the rest as extra columns.
        shown = {getattr(c, "_current_conv_id", "") for c in self._columns}
        for cid in conv_ids[1:]:
            if cid not in shown:
                self.add_column(cid)
                shown.add(cid)
        self._apply_workspace_streams_to_columns()
        self._update_bar_workspaces()

    # ── Workspace mutations from the bar ───────────────────────────────

    def _on_rename_workspace(self, ws_id: str, name: str):
        try:
            rename_workspace(ws_id, name)
        except Exception:
            pass
        self._update_bar_workspaces()

    def _on_workspace_streams_changed(self, ws_id: str, streams: list):
        # The bar already persisted via set_workspace_streams; push the new
        # streams onto every live column's agent in this workspace.
        if ws_id == self._current_workspace_id:
            self._apply_workspace_streams_to_columns()

    def _apply_workspace_streams_to_columns(self):
        """Make every column's agent read the workspace's shared streams."""
        try:
            streams = get_workspace_streams(self._current_workspace_id)
        except Exception:
            return
        for col in self._columns:
            try:
                col.agent._conversation_streams = list(streams)
            except Exception:
                pass
            try:
                col._refresh_stream_chips()
            except Exception:
                pass

    def _prompt_remove_empty_workspaces(self):
        """Delete every workspace whose conversations are all empty (no
        messages) — clears the 'New Chat N' clutter from testing in one shot.
        Never touches the current workspace or the last remaining one."""
        from ui.glass_dialog import GlassDialog
        empties = self._empty_workspace_ids()
        if not empties:
            GlassDialog.confirm(self, "Nothing to remove",
                                "There are no empty workspaces to clean up.")
            return
        if not GlassDialog.confirm(
                self, "Remove empty workspaces",
                f"Delete {len(empties)} empty workspace(s) and their blank "
                f"conversations? The current workspace is kept."):
            return
        removed = self.remove_empty_workspaces()
        self._update_bar_workspaces()
        GlassDialog.confirm(self, "Done", f"Removed {removed} empty workspace(s).")

    def _empty_workspace_ids(self) -> list[str]:
        """Workspace ids whose member conversations all have zero messages
        (excluding the current workspace; always leaving ≥1 workspace)."""
        try:
            counts = {c["id"]: c.get("message_count", 0) for c in list_conversations()}
            wss = list_workspaces()
        except Exception:
            return []

        def empty(ws):
            cols = ws.get("columns", [])
            return all(counts.get(cid, 0) == 0 for cid in cols)  # [] -> True

        ids = [w["id"] for w in wss
               if w["id"] != self._current_workspace_id and empty(w)]
        # Never delete the last surviving workspace.
        if len(wss) - len(ids) < 1 and ids:
            ids = ids[:-1]
        return ids

    def remove_empty_workspaces(self) -> int:
        removed = 0
        for wid in self._empty_workspace_ids():
            try:
                delete_workspace(wid)
                removed += 1
            except Exception:
                pass
        return removed

    def _new_workspace_prompt(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "New workspace", "Name:",
                                        text="Workspace")
        if ok and name.strip():
            self.create_and_switch_workspace(name.strip())

    def create_and_switch_workspace(self, name: str):
        """Create a fresh workspace (inheriting the current one's streams) and
        switch to it with a single new conversation."""
        try:
            streams = get_workspace_streams(self._current_workspace_id)
        except Exception:
            streams = []
        new_id = create_workspace(name, streams=streams)
        self._persist_current_workspace_columns()
        self._current_workspace_id = new_id
        for col in list(self._columns[1:]):
            self._discard_column(col)
        primary = self._primary_column
        if primary is not None:
            primary._new_conversation()
        self._apply_workspace_streams_to_columns()
        self._update_bar_workspaces()

    def delete_current_workspace(self):
        from ui.glass_dialog import GlassDialog
        ws = get_workspace(self._current_workspace_id)
        if ws is None:
            return
        others = [w for w in list_workspaces() if w["id"] != self._current_workspace_id]
        if not others:
            GlassDialog.confirm(
                self, "Cannot delete",
                "This is the only workspace — create another before deleting it.")
            return
        if not GlassDialog.confirm(
                self, "Delete workspace",
                f'Delete workspace "{ws["name"]}" and ALL its conversations? '
                f'This cannot be undone.'):
            return
        doomed = self._current_workspace_id
        # Switch to another workspace first, then delete the old one.
        self._current_workspace_id = ""   # force switch_workspace to act
        self.switch_workspace(others[0]["id"])
        try:
            delete_workspace(doomed)
        except Exception:
            pass
        self._update_bar_workspaces()

    # ── Column ↔ workspace persistence ─────────────────────────────────

    @staticmethod
    def _column_conv_id(col) -> str:
        """The conversation id a column represents: its loaded id, or — while it
        is still asynchronously hydrating — the id it was asked to open. Without
        this fallback, a save that lands before a restored column finishes
        loading reads an empty _current_conv_id and DROPS the column, which is
        exactly how the 'lost every extra column on restart' wipe happens."""
        cid = getattr(col, "_current_conv_id", "") or ""
        if cid:
            return cid
        pending = getattr(col, "_initial_conv", None)
        if pending and pending != "new":
            return pending
        return ""

    def _persist_current_workspace_columns(self):
        """Save the current workspace's ordered columns + focused conversation."""
        if not self._current_workspace_id:
            return
        # During startup restore the live column set is incomplete; a wholesale
        # write here would drop the not-yet-recreated columns. Membership is kept
        # correct in the meantime by add_conversation_to_workspace (merge-add) in
        # _bind_column, so skipping the overwrite is safe.
        if getattr(self, "_restoring", False):
            return
        try:
            ids = []
            for c in self._columns:
                cid = self._column_conv_id(c)
                if cid and cid not in ids:  # preserve order, drop dupes/blanks
                    ids.append(cid)
            # Safety net: never overwrite a saved multi-column layout with a
            # single column. If we somehow only see one column but the stored
            # layout has more, a still-loading column is being missed — skip the
            # write rather than clobber the user's columns.
            if len(ids) <= 1:
                try:
                    existing = get_workspace(self._current_workspace_id)
                    if existing and len(existing.get("columns", []) or []) > len(ids):
                        return
                except Exception:
                    pass
            set_workspace_columns(self._current_workspace_id, ids)
            focus_conv = self._column_conv_id(self._focused_column) if self._focused_column else ""
            if focus_conv:
                set_workspace_active_conv(self._current_workspace_id, focus_conv)
        except Exception:
            pass

    def _save_columns_state(self):
        """Persist the layout. Backed by the workspace model now."""
        self._persist_current_workspace_columns()

    def _discard_column(self, col: ChatColumn):
        """Remove a column from view WITHOUT deleting its conversation (used when
        switching workspaces). The anchor column is never discarded."""
        if col is self._primary_column or col not in self._columns:
            return
        try:
            col._stop_inference()
        except Exception:
            pass
        self._columns.remove(col)
        try:
            col.setParent(None)
            col.deleteLater()
        except Exception:
            pass
        if self._focused_column is col:
            self._focused_column = self._primary_column

    def _style_bar(self):
        """Style the coordinator-owned 'Conversation' button. The matching QSS
        used to live in ChatColumn._apply_styles (applied to the column), but the
        button now lives here, outside any column's stylesheet cascade."""
        p = PALETTE
        self._conv_btn.setStyleSheet(f"""
            QPushButton#promptBtn {{
                background: {p['panel']};
                color: {p['accent']};
                border: 1px solid {p['border']};
                padding: 3px 8px;
                font-family: Consolas, monospace;
                font-size: 9pt;
            }}
            QPushButton#promptBtn:hover {{
                color: {p['accent_bright']};
                border-color: {p['accent_bright']};
            }}
            QPushButton#promptBtn:pressed {{
                background: {p['accent_muted']};
                color: {p['background']};
            }}
        """)
        # The column −/+ buttons are #ConvAction QPushButtons just like the
        # workspace −/+ in the bar, but they live outside ConversationBar's
        # stylesheet cascade — give them the SAME background/border so they don't
        # render as bright default-grey buttons. Reuse the bar's own QSS builder.
        try:
            action_qss = self.conv_bar._action_qss()
            self._col_del_btn.setStyleSheet(action_qss)
            self._col_new_btn.setStyleSheet(action_qss)
        except Exception:
            pass

    # ── Column management ──────────────────────────────────────────────

    def _add_column_widget(self, col: ChatColumn) -> None:
        self._columns.append(col)
        self._columns_host.addWidget(col)

    @property
    def _primary_column(self) -> ChatColumn | None:
        return self._columns[0] if self._columns else None

    @property
    def columns(self) -> list[ChatColumn]:
        return list(self._columns)

    def column_for_agent(self, agent) -> ChatColumn | None:
        """Find the column whose Agent is `agent` (used to route global tool
        bridges to the column actually running the tool)."""
        for col in self._columns:
            if col.agent is agent:
                return col
        return None

    def add_column(self, initial_conv: str = "new") -> ChatColumn:
        """Add a new concurrent chat column with its OWN Agent so it runs
        independently of the others. `initial_conv` is "new" (fresh
        conversation) or an existing conversation id."""
        col = ChatColumn(
            Agent(), parent=self,
            shared_workspace=self._right_workspace,
            coordinator=self,
            is_primary=False,
            initial_conv=initial_conv,
        )
        self._add_column_widget(col)
        # Even out the column widths.
        n = len(self._columns)
        if n > 1:
            self._columns_host.setSizes([max(1, self._columns_host.width() // n)] * n)
        self.set_focused_column(col)
        # The column binds itself to the current workspace once it has loaded its
        # conversation (see ChatColumn._load_initial_conversation_for_column ->
        # _bind_column), so no fragile fixed-delay timer is needed.
        return col

    def _bind_column(self, col: ChatColumn):
        """Ensure a column's conversation belongs to the CURRENT workspace and
        shares its streams. Called by the column right after it loads/creates its
        conversation — the single reliable point that stops orphan conversations
        from spawning stray 'New Chat N' workspaces at the next startup."""
        if col not in self._columns:
            return
        conv_id = getattr(col, "_current_conv_id", "")
        if not conv_id or not self._current_workspace_id:
            return
        try:
            ws = get_workspace(self._current_workspace_id)
            if not ws or conv_id not in ws.get("columns", []):
                add_conversation_to_workspace(self._current_workspace_id, conv_id)
        except Exception:
            pass
        try:
            col.agent._conversation_streams = list(
                get_workspace_streams(self._current_workspace_id))
        except Exception:
            pass
        # Membership is already durable via add_conversation_to_workspace above
        # (a merge-add). Do NOT wholesale-rewrite col_order_json here: this runs
        # asynchronously as each column finishes loading, when sibling columns may
        # not have their _current_conv_id set yet — a full rewrite would drop them
        # (the bug that lost every extra column on restart). Order/removal are
        # persisted by the explicit structural paths (add/remove/switch/shutdown).
        self._update_bar_workspaces()

    def set_focused_column(self, col: ChatColumn | None) -> None:
        """Mark `col` as the focused column: it owns the shared workspace and the
        proxy surface, its composer border lights up and every other column's
        (and the terminals') borders dim — only one target is 'selected' at a
        time across chats AND terminals."""
        if col is None or col not in self._columns:
            return
        if self._focused_column is col:
            # Re-assert (e.g. composer re-focused): still make sure terminals dim.
            self._dim_terminals()
            return
        self._focused_column = col
        # Point the shared terminal panel at this column's conversation so its
        # terminals/viewer-state belong to the focused conversation.
        try:
            self._right_workspace.terminal_panel.set_active_conv(col._current_conv_id)
        except Exception:
            pass
        self._refresh_column_focus_styles()

    def _refresh_column_focus_styles(self):
        """Light the focused column's composer border (accent), dim the rest, and
        dim the terminals — unified one-selected-at-a-time focus."""
        for c in self._columns:
            try:
                c.set_input_focus_highlight(c is self._focused_column)
            except Exception:
                pass
        self._dim_terminals()

    def _dim_terminals(self):
        try:
            self._right_workspace.terminal_panel.clear_active_highlight()
        except Exception:
            pass

    def on_column_focused(self, col: ChatColumn):
        """A column's composer gained focus — make it the selected target."""
        self.set_focused_column(col)

    def on_terminal_focused(self):
        """A terminal gained focus — dim every chat column's composer border so
        the terminal is the single selected target."""
        for c in self._columns:
            try:
                c.set_input_focus_highlight(False)
            except Exception:
                pass

    # ── Column removal ─────────────────────────────────────────────────

    def remove_focused_column(self):
        """− button: close the focused chat column (and delete its conversation),
        after a confirm — mirroring the Terminal tool's close. The first column
        is a permanent anchor (it owns the app-level services + tool bridges) so
        there is always ≥1 column; closing it is a no-op."""
        col = self._focused_column or self._primary_column
        if col is None:
            return
        if col is self._primary_column:
            # Anchor column stays; nothing to close here.
            return
        from ui.glass_dialog import GlassDialog
        name = "this conversation"
        try:
            name = col._get_current_conv_name() or name
        except Exception:
            pass
        if not GlassDialog.confirm(
                self, "Close chat column",
                f'Close the column for "{name}" and delete its conversation? '
                f'This cannot be undone.'):
            return
        self._remove_column(col)

    def _remove_column(self, col: ChatColumn):
        conv_id = getattr(col, "_current_conv_id", "")
        try:
            col._stop_inference()
        except Exception:
            pass
        try:
            col._composer_draft_timer.stop()
        except Exception:
            pass
        if conv_id:
            try:
                if self._current_workspace_id:
                    remove_conversation_from_workspace(self._current_workspace_id, conv_id)
            except Exception:
                pass
            try:
                from core.conversations import delete_conversation
                delete_conversation(conv_id)
            except Exception:
                pass
        if col in self._columns:
            self._columns.remove(col)
        try:
            col.setParent(None)
            col.deleteLater()
        except Exception:
            pass
        # Refresh focus onto the anchor (or last remaining column).
        nxt = self._columns[-1] if self._columns else None
        self._focused_column = None
        self.set_focused_column(nxt)
        # Reflect the deletion in the bar.
        try:
            if nxt is not None:
                nxt._refresh_conv_bar()
        except Exception:
            pass
        self._save_columns_state()

    # ── Drop-in proxy surface ──────────────────────────────────────────

    @property
    def input(self):
        """The composer of the focused column (main() focuses this at startup)."""
        col = self._focused_column or self._primary_column
        return col.input if col is not None else None

    def __getattr__(self, name):
        """Delegate any attribute the coordinator doesn't define to the focused
        column, so the rest of the app keeps treating this as the old ChatWindow.
        Only invoked when normal lookup fails."""
        # Avoid recursion during __init__ before _columns exists.
        d = self.__dict__
        col = d.get("_focused_column") or (d.get("_columns") or [None])[0]
        if col is not None and name != "_columns":
            try:
                return getattr(col, name)
            except AttributeError:
                pass
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}")

    # ── Workspace-wide operations (fan out across all columns) ─────────

    def apply_theme(self):
        try:
            self._style_bar()
        except Exception:
            pass
        for col in self._columns:
            try:
                col.apply_theme()
            except Exception:
                pass

    @property
    def _shutting_down(self) -> bool:
        return self._shutting_down_flag

    @_shutting_down.setter
    def _shutting_down(self, value: bool):
        # main() sets `self.chat._shutting_down = True` at shutdown — fan it out
        # to every column so each suppresses question-board teardown etc.
        self._shutting_down_flag = bool(value)
        for col in self._columns:
            try:
                col._shutting_down = bool(value)
            except Exception:
                pass

    def _auto_save(self, immediate: bool = False):
        for col in self._columns:
            try:
                col._auto_save(immediate=immediate)
            except Exception:
                pass
        self._save_columns_state()

    def _stop_inference(self):
        for col in self._columns:
            try:
                col._stop_inference()
            except Exception:
                pass
