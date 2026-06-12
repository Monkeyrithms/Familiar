"""
Conversation selector — a single height-capped dropdown.

Replaces the old wrap/scroll brick bar: with many conversations a dropdown
scales cleanly. The popup is capped to the app window's height and scrolls
(styled scrollbar) beyond that. Public API (signals + set_conversations /
highlight / start_blink / stop_blink / set_mode / _apply_styles) is unchanged
so the rest of the app keeps working without edits.
"""

from PyQt6.QtWidgets import (
    QWidget, QHBoxLayout, QPushButton, QComboBox, QListView,
    QSizePolicy, QInputDialog, QMenu, QWidgetAction, QCheckBox,
    QStyledItemDelegate,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QRectF, QPointF
from PyQt6.QtGui import (
    QFont, QColor, QPainter, QFontMetrics, QLinearGradient, QBrush, QPen,
)
from ui.theme import PALETTE
from core.agent import load_config


class _ConvActionButton(QPushButton):
    """The +/− conversation buttons. The glyph is PAINTED as crisp vector
    strokes rather than rendered as a '+'/'-' font character — single
    punctuation glyphs read inconsistently (thin, off-center, low-contrast) at
    24px, which is why these kept looking illegible. Background, border, and
    hover still come from the #ConvAction stylesheet via super().paintEvent."""

    def __init__(self, kind: str, parent=None):
        super().__init__("", parent)  # no text — we paint the glyph ourselves
        self._kind = kind  # 'plus' or 'minus'
        self.setObjectName("ConvAction")

    def enterEvent(self, event):
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)  # styled background + border (no text)
        p = PALETTE
        col = QColor(p["accent"] if self.underMouse() else p["accent_muted"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(col)
        pen.setWidthF(1.3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        r = self.rect()
        cx = r.center().x() + 0.5
        cy = r.center().y() + 0.5
        half = 5.5
        painter.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
        if self._kind == "plus":
            painter.drawLine(QPointF(cx, cy - half), QPointF(cx, cy + half))
        painter.end()


class _HoverListView(QListView):
    """Dropdown list that tracks the row under the cursor itself and repaints,
    because QComboBox popups don't reliably deliver CSS :hover. The tracked row
    is read by _HoverDelegate to paint the highlight. This is the bulletproof
    fix after setMouseTracking + QSS :hover both failed on the combo popup."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_row = -1
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)

    def mouseMoveEvent(self, event):
        idx = self.indexAt(event.pos())
        row = idx.row() if idx.isValid() else -1
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        if self.hover_row != -1:
            self.hover_row = -1
            self.viewport().update()
        super().leaveEvent(event)


class _HoverDelegate(QStyledItemDelegate):
    """Paints each conversation row, drawing a highlight for the row the cursor
    is over (view.hover_row) and for the current/selected row — independent of
    Qt's flaky combo-popup hover state."""

    def __init__(self, view: "_HoverListView", parent=None):
        super().__init__(parent)
        self._view = view

    def paint(self, painter, option, index):
        p = PALETTE
        rect = option.rect
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        from PyQt6.QtWidgets import QStyle
        hovered = index.row() == getattr(self._view, "hover_row", -1)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if hovered:
            painter.fillRect(rect, QColor(p["accent_muted"]))
            text_color = QColor(p["glow_hot"])
        elif selected:
            painter.fillRect(rect, QColor(p["panel_alt"]))
            text_color = QColor(p["accent_bright"])
        else:
            painter.fillRect(rect, QColor(p["panel"]))
            text_color = QColor(p["text"])

        # Row divider (matches the QSS separators we already use).
        painter.setPen(QPen(QColor(p["border"])))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())

        text = index.data() or ""
        painter.setPen(QPen(text_color))
        painter.setFont(QFont("Consolas", 9))
        tr = rect.adjusted(10, 0, -10, 0)
        painter.drawText(tr, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         str(text))
        painter.restore()

    def sizeHint(self, option, index):
        sh = super().sizeHint(option, index)
        sh.setHeight(max(sh.height(), 26))
        return sh


class _MarqueeLabel(QWidget):
    """Dim, centered context text that:
      * collapses to width 0 first (so it yields space before the dropdown /
        buttons ever clip), and
      * when its text is wider than the available width, scrolls as a marquee
        with faded left/right edges instead of hard-clipping.
    When the text fits, it's drawn statically (centered), no scrolling."""

    _SPEED_PX = 1        # pixels per tick
    _TICK_MS = 33        # ~30fps
    _GAP = 48            # gap between repetitions of the looping text
    _FADE = 18           # px of edge fade

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = ""
        self._color = QColor(PALETTE["muted_text"])
        self._offset = 0
        self._font = QFont("Consolas", 8)
        # Yield ALL horizontal space before the fixed combo/buttons compress:
        # minimum width 0, expanding preferred.
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def setText(self, text: str):
        self._text = text or ""
        self._offset = 0
        self._update_scroll_state()
        self.update()

    def text(self) -> str:
        return self._text

    def set_color(self, color: QColor):
        self._color = QColor(color)
        self.update()

    def set_font(self, font: QFont):
        self._font = font
        self._update_scroll_state()
        self.update()

    def _text_width(self) -> int:
        return QFontMetrics(self._font).horizontalAdvance(self._text)

    def _needs_scroll(self) -> bool:
        return bool(self._text) and self._text_width() > self.width()

    def _update_scroll_state(self):
        if self._needs_scroll():
            if not self._timer.isActive():
                self._timer.start(self._TICK_MS)
        else:
            self._timer.stop()
            self._offset = 0

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_scroll_state()

    def _tick(self):
        period = self._text_width() + self._GAP
        if period <= 0:
            return
        self._offset = (self._offset + self._SPEED_PX) % period
        self.update()

    def paintEvent(self, event):
        if not self._text:
            return
        painter = QPainter(self)
        painter.setFont(self._font)
        painter.setPen(QPen(self._color))
        fm = QFontMetrics(self._font)
        tw = fm.horizontalAdvance(self._text)
        w, h = self.width(), self.height()
        y = (h + fm.ascent() - fm.descent()) // 2

        if not self._needs_scroll():
            # Fits: draw centered, static.
            x = max(0, (w - tw) // 2)
            painter.drawText(x, y, self._text)
            painter.end()
            return

        # Scrolling marquee: draw the text twice (text + gap + text) so it loops
        # seamlessly, offset leftward.
        period = tw + self._GAP
        start = -self._offset
        x = start
        while x < w:
            painter.drawText(x, y, self._text)
            x += period
        painter.end()

        # Fade the left/right edges so clipped text dissolves instead of
        # hard-cutting. Painted as a second pass using the panel background so
        # the gradient reads as a true fade over whatever's behind.
        self._paint_edge_fades(w, h)

    def _paint_edge_fades(self, w: int, h: int):
        bg = QColor(PALETTE["background"])
        painter = QPainter(self)
        # left fade
        lg = QLinearGradient(0, 0, self._FADE, 0)
        c0 = QColor(bg)
        c0.setAlpha(255)
        c1 = QColor(bg)
        c1.setAlpha(0)
        lg.setColorAt(0.0, c0)
        lg.setColorAt(1.0, c1)
        painter.fillRect(QRectF(0, 0, self._FADE, h), QBrush(lg))
        # right fade
        rg = QLinearGradient(w - self._FADE, 0, w, 0)
        rg.setColorAt(0.0, c1)
        rg.setColorAt(1.0, c0)
        painter.fillRect(QRectF(w - self._FADE, 0, self._FADE, h), QBrush(rg))
        painter.end()


class _CappedCombo(QComboBox):
    """Popup is capped to the window height AND always opens downward (Qt
    otherwise flips it up when near the screen bottom). The list scrolls (styled
    scrollbar) once it would overflow."""

    _PREFERRED_W = 300  # roomy resting width that shows the conversation name

    def sizeHint(self):
        # Prefer the roomy width (so the box rests at ~300px when there's space)
        # rather than the tiny content-based hint. With a Preferred size policy
        # the layout keeps this width until the window is too narrow, then
        # compresses toward minimumWidth — instead of starting small.
        sh = super().sizeHint()
        sh.setWidth(self._PREFERRED_W)
        return sh

    def minimumSizeHint(self):
        mh = super().minimumSizeHint()
        mh.setWidth(min(mh.width(), 120))
        return mh

    def showPopup(self):
        from PyQt6.QtGui import QGuiApplication
        from PyQt6.QtCore import QPoint
        below = self.mapToGlobal(QPoint(0, self.height()))
        screen = QGuiApplication.screenAt(below) or QGuiApplication.primaryScreen()
        avail_below = 400
        if screen is not None:
            avail_below = screen.availableGeometry().bottom() - below.y() - 6
        win = self.window()
        win_cap = (win.height() - 48) if win is not None else 400
        self.view().setMaximumHeight(max(120, min(win_cap, avail_below)))
        super().showPopup()
        # Pin the popup directly below the box so it never opens upward.
        container = self.view().window()
        if container is not None:
            container.move(below)


class ConversationBar(QWidget):
    conversation_selected = pyqtSignal(str)
    new_requested = pyqtSignal()
    rename_requested = pyqtSignal(str, str)
    delete_requested = pyqtSignal(str)
    order_changed = pyqtSignal(list)
    streams_changed = pyqtSignal(str, list)  # conv_id, new stream list
    # The dropdown lists WORKSPACES; its − / + add/remove a workspace. The
    # coordinator (ui/chat_window.py) owns the prompt/confirm + the actual
    # create/delete; the bar just signals intent. (Chat-column add/remove is a
    # separate −/+ pair to the right of the Conversation button.)
    ws_new_requested = pyqtSignal()
    ws_delete_requested = pyqtSignal()
    ws_cleanup_requested = pyqtSignal()  # remove empty workspaces (right-click menu)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._active_id: str = ""
        self._convs: list[dict] = []          # [{id, name}] in display order
        self._remote_groups: dict[str, list[dict]] = {}  # peer -> [{id, name}]
        self._blink_ids: set[str] = set()
        self._loading = False
        # The conversation whose name the user is actively editing in the combo.
        # Captured on the first keystroke so a commit (Enter/focus-out) renames
        # THAT conversation, even if the dropdown's current item changed since.
        self._editing_cid: str | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(4)

        self._combo = _CappedCombo(self)
        self._combo.setObjectName("ConvCombo")
        self._combo.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        # Editable: edit the current name + Enter to rename this conversation.
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        # Preferred ~300px (roomy enough to show the name) but allowed to shrink
        # to a floor on narrow windows. Crucially NOT Fixed: a Fixed combo forced
        # the layout to clip the action buttons on top of it when space ran out.
        # Preferred + a minimum lets the combo yield gracefully while the buttons
        # stay put and the marquee hint absorbs the rest.
        self._combo.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self._combo.setMinimumWidth(120)
        self._combo.setMaximumWidth(300)
        self._combo.setMaxVisibleItems(99)     # rely on the height cap, not a count
        # Custom view + delegate: paint the hover highlight ourselves, because
        # QComboBox popups don't reliably deliver CSS :hover (mouse-tracking +
        # QSS both failed). _HoverListView tracks the row under the cursor and
        # _HoverDelegate paints it.
        _view = _HoverListView()
        _view.setItemDelegate(_HoverDelegate(_view, _view))
        self._combo.setView(_view)
        self._combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo.activated.connect(self._on_activated)
        # Commit an inline rename on Enter AND on focus-out (editingFinished
        # covers both) — typing a new name and clicking away now actually saves,
        # instead of silently dropping the edit.
        self._combo.lineEdit().returnPressed.connect(self._rename_from_edit)
        self._combo.lineEdit().editingFinished.connect(self._rename_from_edit)
        self._combo.lineEdit().textEdited.connect(self._note_editing_target)
        self._combo.lineEdit().setPlaceholderText("Conversation")
        lay.addWidget(self._combo)

        # − delete (left) and + add (right). The glyphs are painted as vector
        # strokes (see _ConvActionButton) so they're crisp and legible. Rename
        # lives on right-click of the dropdown (see _open_context_menu).
        # − / + next to the dropdown add/remove WORKSPACES (the dropdown lists
        # workspaces). Chat-COLUMN add/remove lives to the right of the
        # "Conversation" button (see ui/chat_window.py).
        self._del_btn = _ConvActionButton("minus", self)
        self._del_btn.setFixedSize(24, 24)
        self._del_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._del_btn.setToolTip("Delete this workspace")
        self._del_btn.clicked.connect(lambda: self.ws_delete_requested.emit())
        lay.addWidget(self._del_btn)

        self._new_btn = _ConvActionButton("plus", self)
        self._new_btn.setFixedSize(24, 24)
        self._new_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._new_btn.setToolTip("New workspace")
        self._new_btn.clicked.connect(lambda: self.ws_new_requested.emit())
        lay.addWidget(self._new_btn)

        # Right-click the dropdown → rename / streams context menu.
        self._combo.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._combo.customContextMenuRequested.connect(self._open_context_menu)

        # Subdued context hint filling the leftover space: the conversation's
        # workspace, and (once networked) a "[Remote] <machine> · <workspace>"
        # tag when the selected conversation lives on another machine.
        # Marquee hint: collapses to 0 width first (so the dropdown + action
        # buttons never get clipped), and scrolls with faded edges when its text
        # is wider than the space left over.
        self._hint = _MarqueeLabel(self)
        self._hint.setObjectName("ConvHint")
        self._hint.set_font(QFont("Consolas", 8))
        lay.addWidget(self._hint, stretch=1)

        # Border pulse while another conversation has unread activity.
        self._pulse_on = False
        self._pulse_timer = QTimer(self)
        self._pulse_timer.timeout.connect(self._pulse_tick)

        self.setMaximumHeight(30)
        self._apply_styles()

    # ── Compatibility shims ──────────────────────────────────────────
    def set_mode(self, mode: str):
        """Overflow modes (wrap/scroll) are retired — the dropdown handles any
        conversation count. Kept as a no-op so existing callers don't break."""
        return

    def set_hint(self, text: str):
        """Subdued context line shown beside the dropdown (e.g. the active
        conversation's workspace, or '[Remote] <machine> · <workspace>')."""
        self._hint.setText(text or "")
        self._hint.setToolTip(text or "")  # full text on hover when clipped

    # ── Population ────────────────────────────────────────────────────
    def set_conversations(self, conversations: list[dict], active_id: str):
        self._active_id = active_id
        # Newest first, matching the previous bar's ordering.
        self._convs = [{"id": c["id"], "name": c["name"]}
                       for c in reversed(conversations)]
        self._rebuild_items()

    def set_remote_conversations(self, groups: dict):
        """Merge peer (networked) conversations into the dropdown, grouped by
        peer. `groups` is {peer_name: [{'id': 'remote::peer::cid', 'name': …}]}.
        Selecting one opens a live mirror (see ChatWindow._enter_remote_mirror)."""
        self._remote_groups = dict(groups or {})
        self._rebuild_items()

    def _rebuild_items(self):
        self._loading = True
        self._combo.clear()
        active_idx = 0
        idx = 0
        for c in self._convs:
            label = ("● " if c["id"] in self._blink_ids else "") + c["name"]
            self._combo.addItem(label, c["id"])
            if c["id"] == self._active_id:
                active_idx = idx
            idx += 1
        # Remote conversations, grouped per peer under a non-selectable header.
        for peer in sorted(self._remote_groups):
            convs = self._remote_groups.get(peer) or []
            if not convs:
                continue
            self._combo.insertSeparator(self._combo.count())
            idx += 1
            hdr = self._combo.count()
            self._combo.addItem(f"🌐 {peer}", None)
            self._combo.model().item(hdr).setEnabled(False)   # header, not selectable
            idx += 1
            for c in convs:
                self._combo.addItem(f"    {c['name']}", c["id"])
                if c["id"] == self._active_id:
                    active_idx = idx
                idx += 1
        if self._combo.count():
            self._combo.setCurrentIndex(active_idx)
        self._loading = False

    def highlight(self, active_id: str):
        """Select a conversation without emitting (programmatic switch)."""
        self._active_id = active_id
        self._loading = True
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == active_id:
                self._combo.setCurrentIndex(i)
                break
        self._loading = False

    def _on_activated(self, idx: int):
        if self._loading:
            return
        self._editing_cid = None  # selecting an item ends any in-progress rename
        cid = self._combo.itemData(idx)
        if cid:
            self._active_id = cid
            self.stop_blink(cid)
            self.conversation_selected.emit(cid)

    def _rename_from_edit(self):
        """Enter in the editable box renames the CURRENT conversation (the
        dropdown doubles as an inline rename field)."""
        if self._loading:
            return
        # Rename the conversation that was actually being edited (captured on the
        # first keystroke), so a focus-out after switching items can't rename the
        # wrong one. Fall back to current/active if nothing was tracked.
        cid = self._editing_cid or self._combo.currentData() or self._active_id
        self._editing_cid = None
        if not cid:
            return
        new_name = self._combo.lineEdit().text().strip().lstrip("●").strip()
        cur = next((c["name"] for c in self._convs if c["id"] == cid), "")
        if new_name and new_name != cur:
            self.rename_requested.emit(cid, new_name)

    def _note_editing_target(self, _text: str):
        """First keystroke into the editable combo — remember which conversation
        is being renamed (the current item) so a later commit targets it."""
        if self._loading:
            return
        if self._editing_cid is None:
            self._editing_cid = self._combo.currentData() or self._active_id

    # ── Rename / delete / streams menu (acts on the current conversation) ──
    def _current_id_name(self) -> tuple[str, str]:
        cid = self._combo.currentData() or self._active_id
        name = self._combo.currentText().lstrip("● ").strip() or cid
        return cid, name

    def _delete_current(self):
        """− button: delete the current conversation (with confirmation)."""
        cid, name = self._current_id_name()
        if not cid:
            return
        from ui.glass_dialog import GlassDialog
        if GlassDialog.confirm(self, "Delete Conversation",
                               f'Deleting record "{name}". Proceed?'):
            self.delete_requested.emit(cid)

    def _open_context_menu(self, pos):
        """Right-click on the dropdown → rename + per-conversation streams.
        Delete and Add are the dedicated − / + buttons."""
        cid, name = self._current_id_name()
        if not cid:
            return
        p = PALETTE
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {p['panel']}; color: {p['text']};
                border: 1px solid {p['border']};
                font-family: Consolas; font-size: 9pt;
            }}
            QMenu::item:selected {{ background: {p['accent_muted']}; }}
            QCheckBox {{ color: {p['text']}; font-family: Consolas; font-size: 9pt; padding: 4px 8px; }}
            QCheckBox::indicator {{ width: 12px; height: 12px; }}
            QCheckBox::indicator:checked {{ background: {p['accent']}; border: 1px solid {p['accent']}; }}
            QCheckBox::indicator:unchecked {{ background: transparent; border: 1px solid {p['border']}; }}
        """)
        rename_action = menu.addAction("Rename")
        cleanup_action = menu.addAction("Remove empty workspaces")

        cfg = load_config()
        all_streams = cfg.get("memory_streams", [{"name": "default"}])
        stream_checks = {}
        if all_streams:
            menu.addSeparator()
            streams_menu = menu.addMenu("Streams")
            streams_menu.setStyleSheet(menu.styleSheet())
            # The dropdown lists WORKSPACES; streams are a workspace-level
            # property shared by all of its columns.
            from core.database import get_workspace_streams
            current_streams = get_workspace_streams(cid)
            for s in all_streams:
                sname = s.get("name", "")
                cb = QCheckBox(sname)
                cb.setChecked(sname in current_streams)
                wa = QWidgetAction(streams_menu)
                wa.setDefaultWidget(cb)
                streams_menu.addAction(wa)
                stream_checks[sname] = cb

            def _save_streams():
                new_streams = [n for n, cb in stream_checks.items() if cb.isChecked()]
                from core.conversations import set_workspace_streams
                set_workspace_streams(cid, new_streams)
                self.streams_changed.emit(cid, new_streams)

            for cb in stream_checks.values():
                cb.stateChanged.connect(_save_streams)

        chosen = menu.exec(self._combo.mapToGlobal(pos))
        if chosen == rename_action:
            new_name, ok = QInputDialog.getText(
                self, "Rename Workspace", "Name:", text=name)
            if ok and new_name.strip():
                self.rename_requested.emit(cid, new_name.strip())
        elif chosen == cleanup_action:
            self.ws_cleanup_requested.emit()

    # ── Blink (unread activity in a non-active conversation) ───────────
    def start_blink(self, conv_id: str, interval_ms: int = 600):
        if conv_id == self._active_id or conv_id in self._blink_ids:
            return
        self._blink_ids.add(conv_id)
        self._mark_item(conv_id, True)
        if not self._pulse_timer.isActive():
            self._pulse_timer.start(interval_ms)

    def stop_blink(self, conv_id: str):
        if conv_id in self._blink_ids:
            self._blink_ids.discard(conv_id)
            self._mark_item(conv_id, False)
        if not self._blink_ids and self._pulse_timer.isActive():
            self._pulse_timer.stop()
            self._pulse_on = False
            self._apply_combo_border(False)

    def stop_all_blinks(self):
        for cid in list(self._blink_ids):
            self.stop_blink(cid)

    def _mark_item(self, conv_id: str, blinking: bool):
        for i in range(self._combo.count()):
            if self._combo.itemData(i) == conv_id:
                base = next((c["name"] for c in self._convs if c["id"] == conv_id),
                            self._combo.itemText(i).lstrip("● ").strip())
                self._combo.setItemText(i, ("● " + base) if blinking else base)
                break

    def _pulse_tick(self):
        self._pulse_on = not self._pulse_on
        self._apply_combo_border(self._pulse_on)

    def _apply_combo_border(self, hot: bool):
        # Animate ONLY the border color, never its width — a changing width
        # reflows the combo box and shifts the whole UI on every pulse. The
        # width stays constant (see _combo_qss) so the flash is purely color.
        p = PALETTE
        color = p["glow_hot"] if hot else p["accent"]
        self._combo.setStyleSheet(self._combo_qss(border_color=color))

    # ── Styling ───────────────────────────────────────────────────────
    def _brick_bg(self) -> str:
        p = PALETTE
        if QColor(p["background"]).lightness() > 140:
            return p["panel_alt"]
        a = QColor(p["accent"])
        return f"rgb({max(a.red()//6,12)},{max(a.green()//6,12)},{max(a.blue()//6,12)})"

    def _combo_qss(self, border_color: str = "", border_w: int = 1) -> str:
        # Resting border is a thin, muted hairline (1px, theme border color) so
        # the single active-conversation combo reads as a calm field, not a
        # perma-selected/alert box. The blink path passes an explicit
        # border_color (hot/accent) to flash attention; width stays constant at
        # 1px so the flash only changes color and never reflows/shifts the UI.
        p = PALETTE
        border_color = border_color or p["border"]
        return f"""
            QComboBox#ConvCombo {{
                background: {self._brick_bg()};
                color: {p['accent_bright']};
                border: {border_w}px solid {border_color};
                border-radius: 0px;
                padding: 2px 8px;
            }}
            QComboBox#ConvCombo:hover {{ border-color: {p['accent_bright']}; }}
            QComboBox#ConvCombo QLineEdit {{
                background: transparent;
                color: {p['accent_bright']};
                border: none;
                padding: 0px;
                selection-background-color: {p['accent_muted']};
                selection-color: {p['glow_hot']};
            }}
            QComboBox#ConvCombo::drop-down {{ border: none; width: 18px; }}
            QComboBox#ConvCombo::down-arrow {{
                width: 0; height: 0; margin-right: 6px;
                border-left: 4px solid transparent;
                border-right: 4px solid transparent;
                border-top: 5px solid {p['accent']};
            }}
        """

    def _view_qss(self) -> str:
        p = PALETTE
        return f"""
            QListView {{
                background: {p['panel']};
                color: {p['text']};
                border: 1px solid {p['accent_muted']};
                outline: none;
                font-family: Consolas; font-size: 9pt;
            }}
            /* Row backgrounds, dividers, hover + selected highlights are painted
               by _HoverDelegate (QSS :hover is unreliable in combo popups). */
            QScrollBar:vertical {{
                width: 10px; background: {p['panel_alt']}; margin: 0;
            }}
            QScrollBar::handle:vertical {{
                background: {p['accent_muted']}; min-height: 24px; border-radius: 0px;
            }}
            QScrollBar::handle:vertical:hover {{ background: {p['accent']}; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """

    def _action_qss(self) -> str:
        p = PALETTE
        return f"""
            QPushButton#ConvAction {{
                background: {self._brick_bg()};
                color: {p['accent_muted']};
                border: 1px solid {p['border']};
                border-radius: 0px;
            }}
            QPushButton#ConvAction:hover {{
                color: {p['accent_bright']}; border-color: {p['accent_bright']};
                background: {p['panel']};
            }}
        """

    def _apply_styles(self):
        self._combo.setStyleSheet(self._combo_qss())
        # Style the popup view directly — the popup is a separate top-level
        # widget, so the combo's own stylesheet doesn't reliably reach its
        # scrollbar.
        self._combo.view().setStyleSheet(self._view_qss())
        self._new_btn.setStyleSheet(self._action_qss())
        self._del_btn.setStyleSheet(self._action_qss())
        p = PALETTE
        mc = QColor(p["muted_text"])
        faded = QColor(mc.red(), mc.green(), mc.blue(), 135)  # extra-dim
        self._hint.set_color(faded)
