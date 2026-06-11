"""
Right-workspace Notes + Calendar panels for the root Agent app.

- Notes: local-disk only JSON persistence (no server calls).
- Calendar: month grid that overlays task schedules from:
  1) root Agent tasks (`Agent/tasks.json`)
  2) vispy_dashboard tasks (`Agent/Apps/vispy_dashboard/tasks.json`) when present
  3) Notebook task store (if importable) as an additional source
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import time
import uuid
from pathlib import Path

from PyQt6.QtCore import QDate, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import (
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ui.theme import PALETTE


_ROOT = Path(__file__).resolve().parent.parent
_NOTES_PATH = _ROOT / "data" / "workspace_notes.json"
_ROOT_TASKS_PATH = _ROOT / "tasks.json"
_VISPY_TASKS_PATH = _ROOT / "Apps" / "vispy_dashboard" / "tasks.json"
_NOTEBOOK_TASK_STORE = _ROOT / "Apps" / "Notebook" / "task_store.py"


def _scrollbar_qss(p: dict) -> str:
    return (
        f"QScrollBar:vertical{{background:{p['panel']};width:10px;border:1px solid {p['border']};margin:0px;}}"
        f"QScrollBar::handle:vertical{{background:{p['accent_muted']};min-height:20px;border-radius:2px;}}"
        f"QScrollBar::handle:vertical:hover{{background:{p['accent']};}}"
        "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0px;border:none;background:transparent;}"
        "QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical{background:transparent;}"
        f"QScrollBar:horizontal{{background:{p['panel']};height:10px;border:1px solid {p['border']};margin:0px;}}"
        f"QScrollBar::handle:horizontal{{background:{p['accent_muted']};min-width:20px;border-radius:2px;}}"
        f"QScrollBar::handle:horizontal:hover{{background:{p['accent']};}}"
        "QScrollBar::add-line:horizontal,QScrollBar::sub-line:horizontal{width:0px;border:none;background:transparent;}"
        "QScrollBar::add-page:horizontal,QScrollBar::sub-page:horizontal{background:transparent;}"
    )


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load_notes() -> list[dict]:
    raw = _read_json(_NOTES_PATH)
    notes = raw.get("notes", [])
    if not isinstance(notes, list):
        return []
    out = []
    for n in notes:
        if isinstance(n, dict):
            out.append(dict(n))
    return out


def _save_notes(notes: list[dict]) -> None:
    _write_json(_NOTES_PATH, {"notes": notes, "updated_at": time.time()})


def _parse_datetime(text: str) -> dt.datetime | None:
    s = (text or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _cron_field_match(field: str, value: int) -> bool:
    f = field.strip()
    if f == "*":
        return True
    for part in f.split(","):
        part = part.strip()
        if not part:
            continue
        if part.startswith("*/"):
            try:
                step = int(part[2:])
                if step > 0 and value % step == 0:
                    return True
            except ValueError:
                pass
            continue
        if "-" in part:
            try:
                lo, hi = (int(x) for x in part.split("-", 1))
                if lo <= value <= hi:
                    return True
            except ValueError:
                pass
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            pass
    return False


def _cron_matches(moment: dt.datetime, expr: str) -> bool:
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    cron_dow = (moment.weekday() + 1) % 7
    return (
        _cron_field_match(minute, moment.minute)
        and _cron_field_match(hour, moment.hour)
        and _cron_field_match(dom, moment.day)
        and _cron_field_match(month, moment.month)
        and (_cron_field_match(dow, cron_dow) or (cron_dow == 0 and _cron_field_match(dow, 7)))
    )


def _iter_task_rows() -> list[tuple[str, dict]]:
    rows: list[tuple[str, dict]] = []
    seen: set[tuple[str, str]] = set()

    for source, path in (("agent", _ROOT_TASKS_PATH), ("vispy", _VISPY_TASKS_PATH)):
        payload = _read_json(path)
        tasks = payload.get("tasks", [])
        if not isinstance(tasks, list):
            continue
        for t in tasks:
            if not isinstance(t, dict):
                continue
            tid = str(t.get("id") or "")
            if (source, tid) in seen:
                continue
            seen.add((source, tid))
            rows.append((source, dict(t)))

    if _NOTEBOOK_TASK_STORE.exists():
        try:
            spec = importlib.util.spec_from_file_location("agent_notebook_task_store", str(_NOTEBOOK_TASK_STORE))
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                loaded = getattr(mod, "load_tasks", None)
                if callable(loaded):
                    notebook_tasks = loaded() or []
                    for t in notebook_tasks:
                        if not isinstance(t, dict):
                            continue
                        tid = str(t.get("id") or "")
                        if ("notebook", tid) in seen:
                            continue
                        seen.add(("notebook", tid))
                        rows.append(("notebook", dict(t)))
        except Exception:
            pass
    return rows


def _task_title(task: dict) -> str:
    base = (task.get("name") or "Task").strip() or "Task"
    if task.get("actions") and isinstance(task["actions"], list):
        first = task["actions"][0] if task["actions"] else {}
        if isinstance(first, dict):
            atype = str(first.get("type") or "").strip()
            if atype:
                return f"{base} · {atype}"
    return base


def _task_events_for_month(year: int, month: int) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    first_day = dt.datetime(year, month, 1)
    if month == 12:
        next_month = dt.datetime(year + 1, 1, 1)
    else:
        next_month = dt.datetime(year, month + 1, 1)
    days = (next_month - first_day).days

    for source, t in _iter_task_rows():
        if not t.get("enabled", True):
            continue
        title = _task_title(t)
        task_id = str(t.get("id") or uuid.uuid4().hex[:8])

        sched = t.get("schedule")
        if isinstance(sched, dict):
            kind = str(sched.get("kind") or "").lower()
            if kind == "cron":
                expr = str(sched.get("expr") or "").strip()
                rep_hour = 0
                rep_min = 0
                parts = expr.split()
                if len(parts) == 5:
                    if parts[1].isdigit():
                        rep_hour = int(parts[1])
                    if parts[0].isdigit():
                        rep_min = int(parts[0])
                for day in range(1, days + 1):
                    when = dt.datetime(year, month, day, rep_hour, rep_min)
                    if not _cron_matches(when, expr):
                        continue
                    key = when.strftime("%Y-%m-%d")
                    out.setdefault(key, []).append(
                        {"id": f"{source}:{task_id}:{day}", "title": title, "time": when.strftime("%H:%M"), "source": source}
                    )
                continue

            if kind in ("once", "datetime"):
                run_at = sched.get("run_at")
                when = _parse_datetime(str(run_at or ""))
                if when and when.year == year and when.month == month:
                    key = when.strftime("%Y-%m-%d")
                    out.setdefault(key, []).append(
                        {"id": f"{source}:{task_id}", "title": title, "time": when.strftime("%H:%M"), "source": source}
                    )
                continue

            if kind == "interval":
                minutes = int(sched.get("minutes") or 0)
                if minutes <= 0:
                    continue
                every_label = f"every {minutes}m" if minutes < 60 else f"every {max(1, minutes // 60)}h"
                for day in range(1, days + 1):
                    key = dt.datetime(year, month, day).strftime("%Y-%m-%d")
                    out.setdefault(key, []).append(
                        {"id": f"{source}:{task_id}:{day}", "title": title, "time": every_label, "source": source}
                    )
                continue

        schedule_text = str(sched or t.get("schedule") or "").strip()
        if schedule_text:
            parsed_dt = _parse_datetime(schedule_text)
            if parsed_dt and parsed_dt.year == year and parsed_dt.month == month:
                key = parsed_dt.strftime("%Y-%m-%d")
                out.setdefault(key, []).append(
                    {"id": f"{source}:{task_id}", "title": title, "time": parsed_dt.strftime("%H:%M"), "source": source}
                )
                continue

            if len(schedule_text.split()) == 5:
                expr = schedule_text
                rep_hour = 0
                rep_min = 0
                parts = expr.split()
                if parts[1].isdigit():
                    rep_hour = int(parts[1])
                if parts[0].isdigit():
                    rep_min = int(parts[0])
                for day in range(1, days + 1):
                    when = dt.datetime(year, month, day, rep_hour, rep_min)
                    if _cron_matches(when, expr):
                        key = when.strftime("%Y-%m-%d")
                        out.setdefault(key, []).append(
                            {"id": f"{source}:{task_id}:{day}", "title": title, "time": when.strftime("%H:%M"), "source": source}
                        )
                continue

            m = re.match(r"^every\s+(\d+)\s*([smhd])$", schedule_text, re.IGNORECASE)
            if m:
                val = int(m.group(1))
                unit = m.group(2).lower()
                for day in range(1, days + 1):
                    key = dt.datetime(year, month, day).strftime("%Y-%m-%d")
                    out.setdefault(key, []).append(
                        {"id": f"{source}:{task_id}:{day}", "title": title, "time": f"every {val}{unit}", "source": source}
                    )
    return out


class NotesWorkspacePanel(QFrame):
    # Emitted (possibly from a tool worker thread) when the notes JSON was
    # changed externally — e.g. the agent's `notes` tool. Cross-thread signal
    # delivery queues _reload onto the GUI thread for free.
    _external_change = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("NotesWorkspacePanel")
        self._notes: list[dict] = []
        self._current_id: str | None = None
        self._suppress_save = False
        # When mirroring a peer, notes load/save/delete route to the host's
        # notes over the network instead of the local store.
        self._remote: dict | None = None

        self._external_change.connect(self._reload)
        try:
            from core.event_bus import bus

            # If this panel is ever destroyed, emitting on the dead QObject
            # raises RuntimeError — the bus catches and logs it, nothing breaks.
            bus.on("notes.changed", lambda **kw: self._external_change.emit())
        except Exception:
            pass

        root = QHBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        left = QVBoxLayout()
        left.setSpacing(4)
        self._list = QListWidget()
        self._list.setFont(QFont("Consolas", 9))
        self._list.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self._list, 1)

        row = QHBoxLayout()
        row.setSpacing(4)
        self._new_btn = QPushButton("+ New")
        self._new_btn.clicked.connect(self._new_note)
        self._del_btn = QPushButton("Delete")
        self._del_btn.clicked.connect(self._delete_note)
        row.addWidget(self._new_btn)
        row.addWidget(self._del_btn)
        left.addLayout(row)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setMinimumWidth(220)
        left_w.setMaximumWidth(320)

        self._editor = QTextEdit()
        self._editor.setFont(QFont("Consolas", 10))
        self._editor.setAcceptRichText(False)
        self._editor.setCursorWidth(3)
        self._editor.setPlaceholderText("Click + New to create your first note...")
        self._editor.textChanged.connect(self._schedule_save)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(350)
        self._save_timer.timeout.connect(self._save_current)

        root.addWidget(left_w, 0)
        root.addWidget(self._editor, 1)
        self._reload()
        self.apply_theme()

    # ── Remote mirroring ──────────────────────────────────────────────
    def _switch_store(self, remote: dict | None) -> None:
        """Flip between local and remote note stores WITHOUT leaking the
        currently-open note across the boundary. Clearing the editor first is
        essential: _reload()'s pre-save would otherwise re-create the old store's
        open note in the new store."""
        self._suppress_save = True
        self._editor.clear()
        self._current_id = None
        self._suppress_save = False
        self._remote = remote
        self._reload()

    def enter_remote(self, peer_url: str, conv_id: str) -> None:
        """Show/edit a peer's notes (host commits them)."""
        self._switch_store({"url": peer_url, "conv_id": conv_id})

    def exit_remote(self) -> None:
        if self._remote is None:
            return
        self._switch_store(None)

    def _r_load(self) -> list[dict]:
        if self._remote:
            try:
                from core.network import peer_notes_list
                ok, notes = peer_notes_list(self._remote["url"])
                return notes if ok else []
            except Exception:
                return []
        return _load_notes()

    def _r_save_one(self, note_id: str, content: str) -> dict | None:
        if self._remote:
            try:
                from core.network import peer_notes_save
                ok, note = peer_notes_save(self._remote["url"], note_id, content)
                return note if ok else None
            except Exception:
                return None
        notes = _load_notes()
        now = time.time()
        if note_id:
            for n in notes:
                if str(n.get("id") or "") == str(note_id):
                    n["content"] = content
                    n["updated_at"] = now
                    _save_notes(notes)
                    return n
        note = {"id": uuid.uuid4().hex[:12], "content": content,
                "created_at": now, "updated_at": now}
        notes.append(note)
        _save_notes(notes)
        return note

    def _r_delete(self, note_id: str) -> None:
        if self._remote:
            try:
                from core.network import peer_notes_delete
                peer_notes_delete(self._remote["url"], note_id)
            except Exception:
                pass
            return
        notes = [n for n in _load_notes() if str(n.get("id") or "") != str(note_id)]
        _save_notes(notes)

    def _schedule_save(self) -> None:
        if self._suppress_save:
            return
        self._save_timer.start()

    def _reload(self) -> None:
        if not self._suppress_save:
            self._save_current()
        self._notes = sorted(self._r_load(), key=lambda n: float(n.get("updated_at") or 0), reverse=True)
        prev_id = self._current_id
        self._suppress_save = True
        self._list.clear()
        for note in self._notes:
            text = str(note.get("content") or "")
            first = text.splitlines()[0] if text.splitlines() else ""
            preview = first[:48] if first.strip() else "(empty)"
            item = QListWidgetItem(preview)
            item.setData(Qt.ItemDataRole.UserRole, str(note.get("id") or ""))
            self._list.addItem(item)
        if self._list.count() > 0:
            target_row = 0
            if prev_id:
                for i in range(self._list.count()):
                    item = self._list.item(i)
                    if item and str(item.data(Qt.ItemDataRole.UserRole)) == str(prev_id):
                        target_row = i
                        break
            self._list.setCurrentRow(target_row)
        else:
            self._editor.clear()
            self._current_id = None
        self._suppress_save = False

    def _on_row_changed(self, row: int) -> None:
        if row < 0:
            return
        self._save_current()
        item = self._list.item(row)
        note_id = str(item.data(Qt.ItemDataRole.UserRole)) if item else ""
        self._current_id = note_id or None
        for n in self._notes:
            if str(n.get("id") or "") == note_id:
                self._suppress_save = True
                self._editor.setPlainText(str(n.get("content") or ""))
                self._suppress_save = False
                return
        self._editor.clear()

    def _save_current(self) -> None:
        if self._suppress_save:
            return
        text = self._editor.toPlainText()
        if not self._current_id:
            if not text.strip():
                return
            note = self._r_save_one("", text)
            if note:
                self._current_id = note.get("id")
            self._reload()
            return
        note = self._r_save_one(str(self._current_id), text)
        if note:
            first = text.splitlines()[0] if text.splitlines() else ""
            preview = first[:48] if first.strip() else "(empty)"
            for i in range(self._list.count()):
                it = self._list.item(i)
                if it and str(it.data(Qt.ItemDataRole.UserRole)) == str(self._current_id):
                    it.setText(preview)
                    break

    def _new_note(self) -> None:
        self._save_current()
        note = self._r_save_one("", "")
        self._current_id = note.get("id") if note else None
        self._suppress_save = True
        self._editor.clear()
        self._suppress_save = False
        self._reload()
        self._editor.setFocus()
        self._editor.moveCursor(QTextCursor.MoveOperation.End)

    def _delete_note(self) -> None:
        row = self._list.currentRow()
        if row < 0:
            return
        item = self._list.item(row)
        note_id = str(item.data(Qt.ItemDataRole.UserRole)) if item else ""
        if not note_id:
            return
        self._r_delete(note_id)
        self._suppress_save = True
        self._editor.clear()
        self._current_id = None
        self._suppress_save = False
        self._reload()

    def apply_theme(self) -> None:
        p = PALETTE
        sb = _scrollbar_qss(p)
        self.setStyleSheet(f"QFrame#NotesWorkspacePanel{{background:{p['panel_alt']};border:none;}}")
        self._editor.setStyleSheet(
            f"QTextEdit{{background:{p['panel']};color:{p['text']};border:1px solid {p['border']};font-family:Consolas;font-size:10pt;}}{sb}"
        )
        self._list.setStyleSheet(
            f"QListWidget{{background:{p['panel']};color:{p['text']};border:1px solid {p['border']};font-family:Consolas;}}"
            f"QListWidget::item:selected{{background:{p['accent_soft']};color:{p['accent']};}}"
            f"{sb}"
        )
        btn = (
            f"QPushButton{{color:{p['text']};background:{p['panel_alt']};border:1px solid {p['border']};padding:2px 8px;}}"
            f"QPushButton:hover{{background:{p['accent_soft']};color:{p['accent']};border-color:{p['accent']};}}"
        )
        self._new_btn.setStyleSheet(btn)
        self._del_btn.setStyleSheet(btn)


class CalendarWorkspacePanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("CalendarWorkspacePanel")
        self._selected_iso = QDate.currentDate().toString("yyyy-MM-dd")
        self._view_year = QDate.currentDate().year()
        self._view_month = QDate.currentDate().month()
        self._remote: dict | None = None   # {url} when mirroring a peer's calendar

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        self._prev_btn = QPushButton("‹")
        self._next_btn = QPushButton("›")
        self._title = QLabel("")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setFont(QFont("Consolas", 13, QFont.Weight.Bold))
        self._today_btn = QPushButton("Today")
        self._tasks_btn = QPushButton("Tasks")
        for b in (self._prev_btn, self._next_btn):
            b.setFixedSize(28, 24)
        self._today_btn.setFixedHeight(24)
        self._tasks_btn.setFixedHeight(24)
        self._prev_btn.clicked.connect(lambda: self._shift_month(-1))
        self._next_btn.clicked.connect(lambda: self._shift_month(1))
        self._today_btn.clicked.connect(self._go_today)
        self._tasks_btn.clicked.connect(self._open_tasks_popup)
        header.addWidget(self._prev_btn)
        header.addWidget(self._title, 1)
        header.addWidget(self._next_btn)
        header.addSpacing(8)
        header.addWidget(self._today_btn)
        header.addWidget(self._tasks_btn)
        root.addLayout(header)

        body = QVBoxLayout()
        body.setSpacing(6)
        root.addLayout(body, 1)

        self._grid = QTableWidget(6, 7)
        self._grid.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._grid.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._grid.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectItems)
        self._grid.verticalHeader().setVisible(False)
        self._grid.horizontalHeader().setVisible(True)
        self._grid.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._grid.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._grid.horizontalScrollBar().setEnabled(False)
        self._grid.verticalScrollBar().setEnabled(False)
        self._grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._grid.setSizeAdjustPolicy(QTableWidget.SizeAdjustPolicy.AdjustToContents)
        self._grid.cellClicked.connect(self._on_day_clicked)
        body.addWidget(self._grid, 3)

        side = QVBoxLayout()
        side.setSpacing(4)
        self._side_title = QLabel("")
        self._side_title.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        self._events = QListWidget()
        self._events.setFont(QFont("Consolas", 9))
        hint = QLabel("Task edits stay in the top-left Tasks dialog.")
        hint.setWordWrap(True)
        hint.setFont(QFont("Consolas", 8))
        side.addWidget(self._side_title)
        side.addWidget(self._events, 1)
        side.addWidget(hint)
        side_w = QWidget()
        side_w.setLayout(side)
        body.addWidget(side_w, 2)

        self.apply_theme()
        self._refresh_calendar()

    def enter_remote(self, peer_url: str) -> None:
        """Show a peer's scheduled-task calendar (read-only)."""
        self._remote = {"url": peer_url}
        self._refresh_calendar()

    def exit_remote(self) -> None:
        if self._remote is None:
            return
        self._remote = None
        self._refresh_calendar()

    def _events_for_month(self, year: int, month: int) -> dict:
        if self._remote:
            try:
                from core.network import peer_calendar_events
                ok, ev = peer_calendar_events(self._remote["url"], year, month)
                return ev if ok else {}
            except Exception:
                return {}
        return _task_events_for_month(year, month)

    def _open_tasks_popup(self) -> None:
        opener = getattr(self.window(), "_open_tasks", None)
        if callable(opener):
            opener()

    def _shift_month(self, delta: int) -> None:
        month = self._view_month + delta
        year = self._view_year
        while month < 1:
            month += 12
            year -= 1
        while month > 12:
            month -= 12
            year += 1
        self._view_year = year
        self._view_month = month
        self._refresh_calendar()

    def _go_today(self) -> None:
        today = QDate.currentDate()
        self._view_year = today.year()
        self._view_month = today.month()
        self._selected_iso = today.toString("yyyy-MM-dd")
        self._refresh_calendar()

    def _refresh_calendar(self) -> None:
        first = dt.date(self._view_year, self._view_month, 1)
        first_weekday = first.weekday()
        if self._view_month == 12:
            days = (dt.date(self._view_year + 1, 1, 1) - first).days
        else:
            days = (dt.date(self._view_year, self._view_month + 1, 1) - first).days
        self._title.setText(QDate(self._view_year, self._view_month, 1).toString("MMMM yyyy"))
        task_events = self._events_for_month(self._view_year, self._view_month)

        self._grid.clearContents()
        self._grid.setHorizontalHeaderLabels(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
        day = 1
        for r in range(6):
            for c in range(7):
                cell_index = r * 7 + c
                if cell_index < first_weekday or day > days:
                    it = QTableWidgetItem("")
                    it.setFlags(Qt.ItemFlag.NoItemFlags)
                    self._grid.setItem(r, c, it)
                    continue
                iso = dt.date(self._view_year, self._view_month, day).strftime("%Y-%m-%d")
                count = len(task_events.get(iso, []))
                text = f"{day}\n{count} task" + ("" if count == 1 else "s") if count else str(day)
                it = QTableWidgetItem(text)
                it.setData(Qt.ItemDataRole.UserRole, iso)
                it.setTextAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                if iso == self._selected_iso:
                    it.setBackground(Qt.GlobalColor.transparent)
                self._grid.setItem(r, c, it)
                day += 1

        self._refresh_side()

    def _on_day_clicked(self, row: int, col: int) -> None:
        item = self._grid.item(row, col)
        if not item:
            return
        iso = item.data(Qt.ItemDataRole.UserRole)
        if not iso:
            return
        self._selected_iso = str(iso)
        self._refresh_calendar()

    def _refresh_side(self) -> None:
        events = _task_events_for_month(self._view_year, self._view_month).get(self._selected_iso, [])
        events.sort(key=lambda e: (str(e.get("time") or "zz"), str(e.get("title") or "")))
        self._side_title.setText(self._selected_iso or "—")
        self._events.clear()
        for e in events:
            tm = str(e.get("time") or "")
            src = str(e.get("source") or "task")
            prefix = f"[{tm}] " if tm else ""
            label = f"{prefix}{e.get('title', '')} ({src})"
            self._events.addItem(QListWidgetItem(label))

    def apply_theme(self) -> None:
        p = PALETTE
        sb = _scrollbar_qss(p)
        self.setStyleSheet(f"QFrame#CalendarWorkspacePanel{{background:{p['panel_alt']};border:none;}}")
        btn = (
            f"QPushButton{{color:{p['text']};background:{p['panel_alt']};border:1px solid {p['border']};padding:2px 10px;font-family:Consolas;font-size:9pt;}}"
            f"QPushButton:hover{{background:{p['accent_soft']};color:{p['accent']};border-color:{p['accent']};}}"
        )
        for b in (self._prev_btn, self._next_btn, self._today_btn, self._tasks_btn):
            b.setStyleSheet(btn)
        self._title.setStyleSheet(f"color:{p.get('accent_bright', p['accent'])};background:transparent;")
        self._side_title.setStyleSheet(f"color:{p['accent']};background:transparent;")
        self._events.setStyleSheet(
            f"QListWidget{{background:{p['panel']};color:{p['text']};border:1px solid {p['border']};font-family:Consolas;}}"
            f"QListWidget::item:selected{{background:{p['accent_soft']};color:{p['accent']};}}"
            f"{sb}"
        )
        self._grid.setStyleSheet(
            f"QTableWidget{{background:{p['panel']};color:{p['text']};gridline-color:{p['border']};border:1px solid {p['border']};font-family:Consolas;font-size:9pt;}}"
            f"QHeaderView::section{{background:{p['panel_alt']};color:{p['muted_text']};border:1px solid {p['border']};padding:2px;}}"
            f"QTableWidget::item:selected{{background:{p['accent_soft']};color:{p['accent']};}}"
            f"{sb}"
        )
