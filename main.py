import sys
import signal
import time
import faulthandler

# ── Crash supervisor ────────────────────────────────────────────────────
# Runs BEFORE any Qt import. The first process to start becomes a tiny
# supervisor: it spawns the real app as a marked child and relaunches it
# whenever it dies with a non-zero exit code. This is the only way to survive
# native Qt/C++ aborts (qFatal, segfaults) — those kill the process below
# Python, so no in-process hook can recover. Clean exit (code 0) ends the
# supervisor too. A crash-loop cap (default 3 crashes in 5 minutes) prevents
# runaway restart storms; `--no-supervise` or FAMILIAR_SUPERVISED=1 opts out.


def _supervisor_log(msg: str):
    try:
        import os
        from datetime import datetime
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "supervisor.log"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {msg}\n")
    except Exception:
        pass


def _rotate_log(path, max_bytes=5 * 1024 * 1024):
    """Rename an append-only log to .1 once it exceeds max_bytes."""
    try:
        import os
        if os.path.exists(path) and os.path.getsize(path) > max_bytes:
            backup = str(path) + ".1"
            if os.path.exists(backup):
                os.remove(backup)
            os.replace(path, backup)
    except Exception:
        pass


def _acquire_single_instance_lock():
    """Hold an exclusive lock on data/familiar.lock for the process lifetime.

    Returns the open file handle on success (keep a reference!), or None if
    another instance already holds it. README/help have always advertised a
    single-instance guard; this is it — two instances sharing conversations.db
    and the tunnel port corrupt state.
    """
    import os
    try:
        import msvcrt
        lock_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
        os.makedirs(lock_dir, exist_ok=True)
        fh = open(os.path.join(lock_dir, "familiar.lock"), "a+")
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            fh.close()
            return None
        return fh
    except Exception:
        # Lock machinery itself failing must never block startup.
        return object()


def _supervise():
    """Supervisor loop. Returns only in the supervised child."""
    import os
    if os.environ.get("FAMILIAR_SUPERVISED") == "1" or "--no-supervise" in sys.argv:
        return None

    # Retry briefly: the auto-update path launches the replacement instance
    # BEFORE the old one exits, so the lock may be held for a moment during
    # the handoff.
    lock = None
    _deadline = time.monotonic() + 10
    while True:
        lock = _acquire_single_instance_lock()
        if lock is not None:
            break
        if time.monotonic() >= _deadline:
            sys.stderr.write("[Familiar] Another instance is already running — exiting.\n")
            _supervisor_log("second instance blocked by single-instance lock")
            os._exit(0)
        time.sleep(0.5)

    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    _rotate_log(os.path.join(here, "logs", "supervisor.log"))
    env = dict(os.environ, FAMILIAR_SUPERVISED="1")
    crash_times: list[float] = []
    MAX_CRASHES, WINDOW_SEC = 3, 300

    while True:
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), *sys.argv[1:]],
                env=env, cwd=here)
            code = proc.wait()
        except KeyboardInterrupt:
            try:
                code = proc.wait(timeout=15)
            except Exception:
                proc.kill()
                code = 0
        if code == 0:
            os._exit(0)

        ran_for = time.monotonic() - started
        _supervisor_log(f"app exited with code {code} after {ran_for:.0f}s — relaunching")
        now = time.monotonic()
        crash_times = [t for t in crash_times if now - t < WINDOW_SEC] + [now]
        if len(crash_times) > MAX_CRASHES:
            _supervisor_log(
                f"{len(crash_times)} crashes within {WINDOW_SEC}s — giving up. "
                f"Check logs/errors.log and logs/native_crash.log.")
            os._exit(code)
        time.sleep(2 * len(crash_times))  # 2s, 4s, 6s backoff


_supervise()

# Rotate crash logs before faulthandler pins native_crash.log open.
try:
    from pathlib import Path as _PathRot
    _logs = _PathRot(__file__).resolve().parent / "logs"
    _rotate_log(_logs / "native_crash.log")
    _rotate_log(_logs / "errors.log")
except Exception:
    pass

# Native-crash forensics. A Qt/C++ abort() or a segfault (e.g. a worker thread
# touching a widget directly, or a C++ object deleted mid-signal) kills the
# process BELOW Python — sys.excepthook and threading.excepthook never run, so
# errors.log stays empty and the window just vanishes. faulthandler dumps the
# C-level stack of EVERY thread to native_crash.log the instant that happens,
# turning a silent disappearance into a named, timestamped traceback. The log
# file is held open for the life of the process (faulthandler writes to the
# raw fd at fault time, when the interpreter may be too broken to open files).
try:
    from pathlib import Path as _Path
    _native_log_path = _Path(__file__).resolve().parent / "logs" / "native_crash.log"
    _native_log_path.parent.mkdir(parents=True, exist_ok=True)
    _native_log = _native_log_path.open("a", encoding="utf-8")
    _native_log.write(
        f"\n=== session start {__import__('datetime').datetime.now().isoformat()} ===\n")
    _native_log.flush()
    faulthandler.enable(file=_native_log, all_threads=True)
except Exception:
    # Never let instrumentation setup break startup — fall back to stderr.
    try:
        faulthandler.enable(all_threads=True)
    except Exception:
        pass

# Qt imports
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
)
from PyQt6.QtCore import (
    Qt, QSize, QRect, QPoint, QEvent, QObject, QTimer, QAbstractItemModel, pyqtSignal,
)
from PyQt6.QtGui import QIcon, QFont, QMouseEvent, QPainter, QPen, QColor

# Familiar modules
from ui.title_bar import TitleBar
# NOTE: temporary fallback — the multi-column coordinator (ui/chat_window.py)
# needs `ChatColumn`, which lived in the wiped chat_widget.py. Until that's
# recovered, run the self-contained single-pane ChatWindow directly so the app
# boots and damage can be assessed.
from ui.chat_widget import ChatWindow
from ui.help_dialog import HelpDialog
from ui.tasks_dialog import TasksDialog
from ui.memory_dialog import MemoryDialog
from ui.theme import PALETTE, refresh_palette

from core.agent import Agent, load_config
from core.database import init_conversations_db

APP_NAME = "Familiar"

# Pixels from each edge that count as a resize grip (frameless window).
GRIP = 6

_AUTO_UPDATE_IDLE_SEC = 10 * 60
_AUTO_UPDATE_WAIT_SEC = 5 * 60
_AUTO_UPDATE_POLL_MS = 30 * 1000
_USER_ACTIVITY_EVENTS = frozenset({
    QEvent.Type.MouseButtonPress,
    QEvent.Type.MouseButtonRelease,
    QEvent.Type.KeyPress,
    QEvent.Type.Wheel,
    QEvent.Type.TouchBegin,
    QEvent.Type.TouchUpdate,
})

# Patches for known issues
def _patch_qt():
    """Patch some minor Qt quirks before we start."""
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app:
        # Closing the main window MUST quit the app. (It previously stayed alive
        # because this was False, leaving the process running in the terminal.)
        # Non-modal dialogs are separate top-level windows, so the app still
        # only quits once the main window AND any open dialogs are closed.
        app.setQuitOnLastWindowClosed(True)

class MainWindow(QMainWindow):
    """Main app window: title bar over the chat coordinator (which owns the
    chat columns and the shared right-side workspace)."""

    _source_update_notify = pyqtSignal()

    def __init__(self, agent: Agent):
        super().__init__()
        # Frameless: the custom TitleBar IS the window chrome. Set before show().
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setMouseTracking(True)

        self.agent = agent
        self._dialog_count = 0  # For unique dialog windows
        self._refresh_debounce = None
        self._always_on_top_enabled = False

        # Frameless resize state (no native borders → we drag the edges ourselves)
        self._resize_edge = 0
        self._resize_start_geom = None
        self._resize_start_pos = None
        self._last_resize_hover_edge = None

        # Geometry / state
        self._maximized = False
        self._geom_key = "window_geom"
        self._state_key = "window_state"
        self._max_key = "window_maximized"

        # Build UI
        self.setWindowTitle(APP_NAME)
        # Accent-colored sparkle (matches the agent theme) — this is the icon the
        # Windows taskbar shows for the running app.
        try:
            from ui.app_icon import apply_app_icon
            apply_app_icon(self)
        except Exception:
            self.setWindowIcon(QIcon())

        # Central widget: title bar + chat/workspace split
        central = QWidget()
        central.setMouseTracking(True)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        
        # Title bar
        self.title_bar = TitleBar(APP_NAME)
        self.title_bar.help_clicked.connect(self._open_help)
        self.title_bar.settings_clicked.connect(self._open_settings)
        self.title_bar.tasks_clicked.connect(self._open_tasks)
        self.title_bar.refresh_clicked.connect(self._refresh_ui)
        self.title_bar.memory_clicked.connect(self._open_memory)
        self.title_bar.screenshot_clicked.connect(self._screenshot_to_clipboard)
        self.title_bar.always_on_top_clicked.connect(self._toggle_always_on_top)
        self.title_bar.minimize_clicked.connect(self.showMinimized)
        self.title_bar.maximize_clicked.connect(self._toggle_maximize)
        self.title_bar.close_clicked.connect(self.close)
        layout.addWidget(self.title_bar)
        
        # Chat coordinator. It owns the chat columns AND the shared right-side
        # workspace (terminals / file viewer / browser) in its own internal
        # splitter — the host window no longer manages a separate workspace pane.
        self.chat = ChatWindow(self.agent)

        layout.addWidget(self.chat)
        self.setCentralWidget(central)
        
        # Styling
        self._apply_styles()
        self.title_bar.apply_theme()
        self.chat.apply_theme()
        
        # Restore window geometry / state
        self._restore_geometry()
        self._source_update_notify.connect(self._on_source_update_notify)
        self._init_update_banner()

        # App-wide event filter: catches mouse events anywhere in the window so
        # the frameless edges work as resize grips (and don't get swallowed by
        # child widgets). Mirrors the root Familiar window.
        QApplication.instance().installEventFilter(self)

        # SDL audio initialization belongs on the UI thread. Starting its mixer
        # from a short-lived worker can leave Windows with a stale device handle.
        QTimer.singleShot(0, self._preload_sounds)
        # Warm the Settings dialog's import chain (tools.registry → mcp/httpx
        # → numpy, ~1s cold) so the first click on Settings opens instantly.
        # Module import only — no QWidget is constructed off-thread.
        import threading
        threading.Thread(target=self._preload_settings_module,
                         daemon=True).start()

    # ── Frameless resize (no native borders) ─────────────────────────────

    def _edge_at_global(self, global_pos: QPoint) -> int:
        """Return the resize-edge bitmask if a global point is within the grip."""
        if self.isMaximized():
            return 0
        geo = self.geometry()
        x, y = global_pos.x(), global_pos.y()
        edge = 0
        if x < geo.left() + GRIP:
            edge |= Qt.Edge.LeftEdge.value
        if x > geo.right() - GRIP:
            edge |= Qt.Edge.RightEdge.value
        if y < geo.top() + GRIP:
            edge |= Qt.Edge.TopEdge.value
        if y > geo.bottom() - GRIP:
            edge |= Qt.Edge.BottomEdge.value
        return edge

    def _cursor_for_edge(self, edge: int):
        if edge in (Qt.Edge.LeftEdge.value | Qt.Edge.TopEdge.value,
                    Qt.Edge.RightEdge.value | Qt.Edge.BottomEdge.value):
            return Qt.CursorShape.SizeFDiagCursor
        if edge in (Qt.Edge.RightEdge.value | Qt.Edge.TopEdge.value,
                    Qt.Edge.LeftEdge.value | Qt.Edge.BottomEdge.value):
            return Qt.CursorShape.SizeBDiagCursor
        if edge in (Qt.Edge.LeftEdge.value, Qt.Edge.RightEdge.value):
            return Qt.CursorShape.SizeHorCursor
        if edge in (Qt.Edge.TopEdge.value, Qt.Edge.BottomEdge.value):
            return Qt.CursorShape.SizeVerCursor
        return Qt.CursorShape.ArrowCursor

    def _is_my_widget(self, obj) -> bool:
        """True if obj belongs to this window (not a dialog), so the resize
        filter doesn't hijack mouse events meant for modal dialogs."""
        w = obj
        while w is not None:
            if w is self:
                return True
            if isinstance(w, QWidget):
                from PyQt6.QtWidgets import QDialog
                if isinstance(w, QDialog):
                    return False
                w = w.parentWidget()
                continue
            if isinstance(w, QAbstractItemModel):
                w = super(QAbstractItemModel, w).parent()
                continue
            if isinstance(w, QObject):
                w = w.parent()
                continue
            return False
        return False

    def eventFilter(self, obj, event):
        if event.type() in _USER_ACTIVITY_EVENTS:
            self._note_user_activity()

        if self._resize_edge:
            if event.type() == QEvent.Type.MouseMove:
                self._do_resize(event.globalPosition().toPoint())
                return True
            if event.type() == QEvent.Type.MouseButtonRelease:
                self._resize_edge = 0
                self._resize_start_geom = None
                self._resize_start_pos = None
                QApplication.restoreOverrideCursor()
                return True

        if not self._is_my_widget(obj):
            return super().eventFilter(obj, event)

        if event.type() == QEvent.Type.MouseButtonPress:
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                gp = event.globalPosition().toPoint()
                edge = self._edge_at_global(gp)
                if edge:
                    self._resize_edge = edge
                    self._resize_start_geom = QRect(self.geometry())
                    self._resize_start_pos = gp
                    QApplication.setOverrideCursor(self._cursor_for_edge(edge))
                    return True

        if event.type() == QEvent.Type.MouseMove and not self._resize_edge:
            if isinstance(event, QMouseEvent):
                gp = event.globalPosition().toPoint()
                edge = self._edge_at_global(gp)
                if edge != self._last_resize_hover_edge:
                    self._last_resize_hover_edge = edge
                    if edge:
                        self.setCursor(self._cursor_for_edge(edge))
                    else:
                        self.unsetCursor()

        return super().eventFilter(obj, event)

    def _do_resize(self, global_pos: QPoint):
        delta = global_pos - self._resize_start_pos
        g = QRect(self._resize_start_geom)
        min_w, min_h = self.minimumWidth(), self.minimumHeight()

        if self._resize_edge & Qt.Edge.LeftEdge.value:
            new_left = g.left() + delta.x()
            if g.right() - new_left >= min_w:
                g.setLeft(new_left)
        if self._resize_edge & Qt.Edge.RightEdge.value:
            g.setRight(g.right() + delta.x())
        if self._resize_edge & Qt.Edge.TopEdge.value:
            new_top = g.top() + delta.y()
            if g.bottom() - new_top >= min_h:
                g.setTop(new_top)
        if self._resize_edge & Qt.Edge.BottomEdge.value:
            g.setBottom(g.bottom() + delta.y())

        if g.width() >= min_w and g.height() >= min_h:
            self.setGeometry(g)

    def paintEvent(self, event):
        """Thin accent border so the frameless window reads as a framed panel."""
        super().paintEvent(event)
        p = PALETTE
        painter = QPainter(self)
        pen = QPen(QColor(p["accent_muted"]))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def changeEvent(self, event):
        """Refocus the chat input when the window is (re)activated."""
        if event.type() == QEvent.Type.WindowActivate:
            QTimer.singleShot(0, lambda: self._focus_chat_input())
        super().changeEvent(event)

    def _focus_chat_input(self):
        try:
            self.chat.input.setFocus()
        except Exception:
            pass

    def _preload_sounds(self):
        """Warm the sound cache so the first shutter/click isn't delayed."""
        try:
            from core.sounds import preload_all
            preload_all()
        except Exception:
            pass

    def _preload_settings_module(self):
        """Import ui.settings_dialog ahead of first use (pure module import)."""
        try:
            import ui.settings_dialog  # noqa: F401
        except Exception:
            pass

    def _apply_styles(self):
        """Apply the window-level background from the current palette. The chat
        coordinator and its columns style themselves via their own apply_theme."""
        p = PALETTE
        self.setStyleSheet(
            f"QMainWindow {{ background: {p['background']}; }}"
            f" QWidget {{ background: {p['background']}; color: {p['accent']}; }}"
        )
        # Font
        try:
            font = QFont("Consolas", 11)
            font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
            self.setFont(font)
        except Exception:
            pass
    
    def _restore_geometry(self):
        """Restore the window to EXACTLY where it last was — including which
        monitor and the maximized state. Multi-monitor aware: a window left on
        screen 2 reopens on screen 2. Only falls back to a centered default if
        the saved monitor is gone (so the window can't strand off-screen)."""
        # Don't let the window ever be smaller than this — a tiny restored size
        # can bury the title bar under the screen edge.
        self.setMinimumSize(800, 600)
        try:
            app = QApplication.instance()
            cfg = load_config()
            geom = cfg.get(self._geom_key)
            maximized = bool(cfg.get(self._max_key, False))

            rect = None
            if geom and len(geom) == 4:
                x, y, w, h = (int(v) for v in geom)
                rect = QRect(x, y, max(800, w), max(600, h))

            # Which monitor does the saved rect belong to? screenAt() walks the
            # real layout, so screen-2 coordinates resolve to screen 2 — NOT the
            # primary screen (the old bug that always pulled it back to screen 1).
            screen = None
            if rect is not None:
                screen = app.screenAt(rect.center()) or app.screenAt(rect.topLeft())
                if screen is None:
                    for s in app.screens():
                        if s.availableGeometry().intersects(rect):
                            screen = s
                            break

            if rect is None or screen is None:
                # No saved geom, or its monitor was disconnected — center on primary.
                scr = app.primaryScreen().availableGeometry()
                w, h = min(1400, scr.width()), min(900, scr.height())
                rect = QRect(0, 0, w, h)
                rect.moveCenter(scr.center())
            else:
                # Keep the rect ON its monitor; only nudge so the title bar stays
                # reachable. Crucially, clamp to THIS screen's bounds — never the
                # primary's — so the window doesn't jump monitors.
                avail = screen.availableGeometry()
                w = min(rect.width(), avail.width())
                h = min(rect.height(), avail.height())
                x = max(avail.left(), min(rect.x(), avail.right() - w + 1))
                y = max(avail.top(), min(rect.y(), avail.bottom() - h + 1))
                rect = QRect(x, y, w, h)

            self.setGeometry(rect)
            if maximized:
                # Maximizes on the monitor the (normal) rect now sits on.
                self._maximized = True
                self.showMaximized()
        except Exception as e:
            print(f"[MainWindow] Failed to restore geometry: {e}", flush=True)
            self.resize(1200, 800)

    def _save_geometry(self):
        """Persist the EXACT restore state: when maximized, save the NORMAL rect
        (which monitor + size to come back to) plus the maximized flag — never
        the maximized rect itself, which would lose the monitor and restore size."""
        try:
            from core.agent import save_config
            cfg = load_config()
            is_max = self.isMaximized()
            g = self.normalGeometry() if is_max else self.geometry()
            cfg[self._geom_key] = [g.x(), g.y(), g.width(), g.height()]
            cfg[self._max_key] = is_max
            save_config(cfg)
        except Exception as e:
            print(f"[MainWindow] Failed to save geometry: {e}", flush=True)

    # -- Source-update banner (Familiar-Net source sync) --------------------
    def _init_update_banner(self) -> None:
        from ui.update_banner import UpdateBanner
        self._update_banner = UpdateBanner(
            self, on_restart=self._apply_update_and_restart)
        self._update_settle = QTimer(self)
        self._update_settle.setSingleShot(True)
        self._update_settle.timeout.connect(self._show_update_banner_if_pending)
        self._last_user_activity = time.monotonic()
        self._last_agent_activity = 0.0
        self._remote_update_notice_ts: float | None = None
        self._auto_update_timer = QTimer(self)
        self._auto_update_timer.setInterval(_AUTO_UPDATE_POLL_MS)
        self._auto_update_timer.timeout.connect(self._check_auto_remote_update)
        self._auto_update_timer.start()
        QTimer.singleShot(5000, self._show_update_banner_if_pending)

    def _on_source_update_notify(self) -> None:
        if getattr(self, "_update_settle", None) is not None:
            self._update_settle.start(4000)

    def _note_user_activity(self) -> None:
        aw = QApplication.activeWindow()
        if aw is not None and aw is not self and not self.isAncestorOf(aw):
            return
        self._last_user_activity = time.monotonic()

    def _note_agent_activity(self) -> None:
        self._last_agent_activity = time.monotonic()

    def _activity_clock(self) -> float:
        return max(self._last_user_activity, self._last_agent_activity)

    def _agent_work_active(self) -> bool:
        chat = getattr(self, "chat", None)
        # Authoritative first: the chat window's own busy test. `_thread` alone
        # is not safe — it gets nulled while run() is still executing (late
        # signals, watchdog recovery, conversation backgrounding), which would
        # let the auto-updater restart the app MID-TURN and lose the work.
        try:
            probe = getattr(chat, "_agent_busy", None)
            if callable(probe) and probe():
                return True
        except Exception:
            pass
        try:
            th = getattr(chat, "_thread", None)
            if th is not None and th.isRunning():
                return True
        except Exception:
            pass
        try:
            for th in list(getattr(chat, "_zombie_threads", []) or []):
                if th is not None and th.isRunning():
                    return True
        except Exception:
            pass
        try:
            for rec in (getattr(chat, "_conv_threads", {}) or {}).values():
                th = rec.get("thread") if isinstance(rec, dict) else None
                if th is not None and th.isRunning():
                    return True
        except Exception:
            pass
        try:
            for th in list(getattr(chat, "_task_threads", []) or []):
                if th is not None and th.isRunning():
                    return True
        except Exception:
            pass
        return False

    def _record_remote_update_notice(self) -> None:
        self._remote_update_notice_ts = time.monotonic()

    def _auto_update_countdown_seconds(self, staged_count: int) -> int | None:
        if staged_count <= 0 or self._remote_update_notice_ts is None:
            return None
        now = time.monotonic()
        if self._agent_work_active():
            return None
        if now - self._activity_clock() < _AUTO_UPDATE_IDLE_SEC:
            return None
        remaining = int(_AUTO_UPDATE_WAIT_SEC - (now - self._remote_update_notice_ts))
        return max(0, remaining)

    def _refresh_update_banner_countdown(self) -> None:
        try:
            from core.source_sync import source_sync
            staged, local = source_sync.pending_detail()
            pending = source_sync.pending()
        except Exception:
            return
        if not pending:
            return
        banner = getattr(self, "_update_banner", None)
        if banner is not None and banner.isVisible():
            banner.show_for(
                len(pending), staged=len(staged), local=len(local),
                auto_seconds=self._auto_update_countdown_seconds(len(staged)))

    def _check_auto_remote_update(self) -> None:
        try:
            from core.source_sync import source_sync
            staged, _local = source_sync.pending_detail()
        except Exception:
            return
        if not staged:
            self._remote_update_notice_ts = None
            return
        notice = self._remote_update_notice_ts
        if notice is None:
            return
        now = time.monotonic()
        if self._agent_work_active():
            self._note_agent_activity()
            self._refresh_update_banner_countdown()
            return
        if now - self._activity_clock() < _AUTO_UPDATE_IDLE_SEC:
            self._refresh_update_banner_countdown()
            return
        remaining = _AUTO_UPDATE_WAIT_SEC - (now - notice)
        if remaining > 0:
            self._refresh_update_banner_countdown()
            return
        print("[update] auto-applying remote update (idle recipient)", flush=True)
        banner = getattr(self, "_update_banner", None)
        if banner is not None:
            banner.hide()
        self._apply_update_and_restart()

    def _show_update_banner_if_pending(self) -> None:
        try:
            from core.source_sync import source_sync
            staged, local = source_sync.pending_detail()
            pending = source_sync.pending()
        except Exception:
            staged, local, pending = [], [], []
        if staged:
            self._record_remote_update_notice()
        elif not pending:
            self._remote_update_notice_ts = None
        if pending and getattr(self, "_update_banner", None) is not None:
            self._update_banner.show_for(
                len(pending), staged=len(staged), local=len(local),
                auto_seconds=self._auto_update_countdown_seconds(len(staged)))

    def _apply_update_and_restart(self) -> None:
        if self._agent_work_active():
            print("[update] restart deferred — agent is running", flush=True)
            return
        import os
        import subprocess
        try:
            from core.source_sync import source_sync
            had_pending = bool(source_sync.pending())
            applied = source_sync.apply_staged()
        except Exception as e:
            print(f"[update] apply failed: {e}")
            return
        if not had_pending:
            banner = getattr(self, "_update_banner", None)
            if banner is not None:
                banner.hide()
            return
        if applied:
            print(f"[update] applied {len(applied)} staged file(s)", flush=True)
        try:
            self._save_geometry()
            self.chat._auto_save(immediate=True)
        except Exception as e:
            print(f"[update] state save before restart failed: {e}")
        try:
            from core.network import network_manager
            network_manager.release_for_restart()
        except Exception as e:
            print(f"[update] tunnel handoff warning: {e}")
        try:
            main_py = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
            args = [sys.executable, main_py] + sys.argv[1:]
            creationflags = 0
            if sys.platform == "win32":
                creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
            subprocess.Popen(args, cwd=os.path.dirname(main_py),
                             creationflags=creationflags, close_fds=True)
        except Exception as e:
            print(f"[update] relaunch failed: {e}")
            return
        os._exit(0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        banner = getattr(self, "_update_banner", None)
        if banner is not None and banner.isVisible():
            banner._reposition()

    def closeEvent(self, event):
        """Save state and tear down every background process/thread so the
        process actually exits instead of lingering in the terminal."""
        self._save_geometry()
        # Persist each conversation's terminal layout (tabs/names/view mode)
        # BEFORE the shells are killed below — otherwise deletions never stick.
        try:
            self.chat._right_workspace.terminal_panel.save_all(deep=True)
        except Exception as e:
            print(f"[MainWindow] Terminal layout save failed: {e}", flush=True)
        # Fan a shutdown flag out to every column and stop inference.
        try:
            self.chat._shutting_down = True
            self.chat._stop_inference()
            self.chat._shutdown_workers()
            self.chat._auto_save(immediate=True)
        except Exception as e:
            print(f"[MainWindow] Chat shutdown failed: {e}", flush=True)
        try:
            self.chat._right_workspace.shutdown_for_exit()
        except Exception:
            pass
        # Kill all sub-agent orchestrators.
        try:
            from core.subagent import _orchestrators
            for orch in list(_orchestrators.values()):
                try:
                    orch.shutdown()
                except Exception:
                    pass
        except Exception:
            pass
        # Stop the inbound network server + cloudflared tunnel subprocess.
        try:
            from core.network import network_manager
            network_manager.stop()
        except Exception:
            pass
        # Kill every agent-spawned subprocess tree (terminal shells, bg jobs).
        try:
            from tools.terminal import shutdown_all_processes
            shutdown_all_processes()
        except Exception:
            pass
        # Shut down any LSP servers.
        try:
            from core.lsp_client import lsp_manager
            lsp_manager.shutdown_all()
        except Exception:
            pass
        event.accept()
        super().closeEvent(event)
        # The main window IS the app — closing it ends the event loop, even if
        # some stray top-level widget would otherwise keep it alive. main() then
        # force-exits so no lingering thread strands the process in the terminal.
        QApplication.instance().quit()
    
    def _toggle_always_on_top(self, enabled: bool = None):
        """Toggle always-on-top WITHOUT the hide/reshow flicker.

        Changing Qt window flags on a visible window forces a full top-level
        reconfiguration (hide + show) — that's the flicker. On Windows we flip
        the topmost bit natively via SetWindowPos, which never reshows the
        window. `enabled` comes from the title-bar button's checked state."""
        if enabled is None:  # called without the checked state — just invert
            enabled = not getattr(self, "_always_on_top_enabled", False)
        self._always_on_top_enabled = bool(enabled)
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes

                SWP_NOSIZE = 0x0001
                SWP_NOMOVE = 0x0002
                SWP_NOACTIVATE = 0x0010
                SWP_FRAMECHANGED = 0x0020
                SWP_NOOWNERZORDER = 0x0200
                SWP_NOSENDCHANGING = 0x0400

                HWND_TOPMOST = wintypes.HWND(-1)
                HWND_NOTOPMOST = wintypes.HWND(-2)

                hwnd = wintypes.HWND(int(self.winId()))
                insert_after = HWND_TOPMOST if enabled else HWND_NOTOPMOST
                flags = (SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
                         | SWP_FRAMECHANGED | SWP_NOOWNERZORDER | SWP_NOSENDCHANGING)
                ctypes.windll.user32.SetWindowPos(
                    hwnd, insert_after, 0, 0, 0, 0, flags)
            except Exception:
                self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
                self.show()
        else:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
            self.show()
        try:
            from core.sounds import play_ui
            play_ui("beep.mp3")
        except Exception:
            pass
    
    def _toggle_maximize(self):
        """Toggle between normal and maximized."""
        if self.isMaximized():
            self.showNormal()
            self._maximized = False
        else:
            self.showMaximized()
            self._maximized = True
    
    def _screenshot_to_clipboard(self):
        """Grab the whole window, copy it to the clipboard (as PNG + image),
        play the shutter sound, and fire the camera-flash overlay."""
        from PyQt6.QtCore import QBuffer, QIODevice, QMimeData
        try:
            pixmap = self.grab()
            data = QMimeData()
            buf = QBuffer()
            buf.open(QIODevice.OpenModeFlag.WriteOnly)
            pixmap.save(buf, "PNG")
            buf.close()
            data.setData("image/png", buf.data())
            data.setImageData(pixmap.toImage())
            QApplication.clipboard().setMimeData(data)
        except Exception as e:
            print(f"[MainWindow] Screenshot failed: {e}", flush=True)
            return
        try:
            from core.sounds import play_ui
            play_ui("snapshot.mp3")
        except Exception:
            pass
        self._do_camera_flash()

    def _do_camera_flash(self):
        """White flash overlay that fades out — simulates a camera flash."""
        flash = QWidget(self)
        flash.setGeometry(self.rect())
        flash.setStyleSheet("background: white;")
        flash.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        flash.show()
        flash.raise_()

        # Step the opacity down via stylesheet (no Q_PROPERTY animation needed).
        steps = [0.85, 0.65, 0.45, 0.30, 0.18, 0.08, 0.0]
        self._flash_widget = flash  # prevent GC
        self._flash_step = 0
        self._flash_steps = steps

        def _tick():
            if self._flash_step >= len(self._flash_steps):
                flash.hide()
                flash.deleteLater()
                self._flash_widget = None
                return
            opacity = self._flash_steps[self._flash_step]
            flash.setStyleSheet(f"background: rgba(255,255,255,{int(opacity * 255)});")
            self._flash_step += 1
            QTimer.singleShot(30, _tick)

        QTimer.singleShot(30, _tick)
    
    # Dialog handlers
    def _open_help(self):
        """Show help dialog."""
        dlg = HelpDialog(self, on_take_tour=lambda: self._start_tour(replay=True))
        dlg.exec()

    def _start_tour(self, replay: bool = True):
        """Launch (or replay) the first-run tour: fold the app down to the chat
        card and unfold it again. Forced — runs regardless of saved tour_state."""
        try:
            from ui.tour import TourDirector
            self._tour_director = TourDirector(self)
            self._tour_director.prepare_genesis()
            QTimer.singleShot(200, self._tour_director.begin)
        except Exception as e:
            print(f"[Familiar] start tour failed: {e}", flush=True)
    
    def _open_settings(self):
        """Open Settings via the focused chat column (non-modal). The column
        owns the dialog; we pass an on_accept callback so the whole window
        repaints with the new palette when the user applies changes."""
        def _on_accept():
            refresh_palette()
            self._apply_styles()
            self.title_bar.apply_theme()
            self.chat.apply_theme()
            self.update()
        self.chat._open_settings(on_accept=_on_accept)

    def _open_tasks(self):
        """Show tasks dialog (bound to the chat window)."""
        dlg = TasksDialog(self.chat, self)
        dlg.exec()

    def _open_memory(self):
        """Show memory/notes dialog."""
        dlg = MemoryDialog(self)
        dlg.exec()
    
    def _refresh_ui(self):
        """
        Hot-reload UI modules + theme without restarting the Agent backend.
        Terminal tabs, workspaces, and the Agent backend stay alive and untouched.
        """
        import sys
        import importlib

        try:
            from core.sounds import play_ui
            play_ui("beep.mp3")
        except Exception:
            pass

        try:
            # Modules to NOT reload (singletons / live backend bridges):
            # - core.agent, core.subagent, core.database (hold Agent/tool state)
            # - core.tools (have registered tool instances)
            # - core.lsp_client (live LSP connections)
            # - core.sounds (audio playback state)
            SKIP_MODULES = {
                "core.agent", "core.subagent", "core.database",
                "core.lsp_client", "core.sounds", "core.ui_watchdog", "core.tools",
                "core.file_viewer_state",
            }

            # Reload theme first so re-imported ui.* modules bind the fresh palette.
            import ui.theme as theme_mod
            try:
                importlib.reload(theme_mod)
            except Exception as e:
                print(f"[Refresh] Warning: failed to reload ui.theme: {e}", flush=True)
            theme_mod.refresh_palette()

            modules_to_reload = sorted(
                name for name in sys.modules.keys()
                if (name.startswith("ui.") or name.startswith("core."))
                and name not in SKIP_MODULES
                and name != "ui.theme"
            )

            for mod_name in modules_to_reload:
                try:
                    mod = sys.modules.get(mod_name)
                    if mod is not None:
                        importlib.reload(mod)
                except Exception as e:
                    print(f"[Refresh] Warning: failed to reload {mod_name}: {e}", flush=True)

            # Re-read config.json and push into the live PALETTE dict.
            theme_mod.refresh_palette()
            self._rebind_hot_reload_widgets()

            self._apply_styles()
            if self.title_bar:
                self.title_bar.apply_theme()
            if self.chat:
                self.chat.apply_theme()
            self.update()

            print("[Refresh] UI reloaded successfully", flush=True)
        except Exception as e:
            print(f"[Refresh] Error during hot-reload: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def _rebind_hot_reload_widgets(self):
        """Point live shell widgets at reloaded classes so updated method bodies
        take effect without tearing down terminals or conversations."""
        try:
            import ui.title_bar as title_mod
            if self.title_bar is not None:
                self.title_bar.__class__ = title_mod.TitleBar
        except Exception as e:
            print(f"[Refresh] Warning: title bar rebind failed: {e}", flush=True)
        try:
            import ui.chat_widget as chat_mod
            if self.chat is not None:
                self.chat.__class__ = chat_mod.ChatWindow
                ws = getattr(self.chat, "_right_workspace", None)
                if ws is not None:
                    import ui.right_workspace as rw_mod
                    ws.__class__ = rw_mod.RightWorkspacePanel
        except Exception as e:
            print(f"[Refresh] Warning: chat/workspace rebind failed: {e}", flush=True)


def _global_kill_all():
    """Belt-and-suspenders teardown of everything that can keep the process
    alive after the window closes: agent subprocess trees + the network
    server/cloudflared tunnel. Safe to call multiple times."""
    try:
        from tools.terminal import shutdown_all_processes
        shutdown_all_processes()
    except Exception:
        pass
    try:
        from core.network import network_manager
        network_manager.stop()
    except Exception:
        pass


def main():
    """Main entry point."""
    # Global crash guard. PyQt6 treats any unhandled Python exception that
    # escapes a slot/signal handler as fatal (qFatal → process abort). One
    # exhausted API retry or a bad callback must NOT take down the whole app:
    # log it to logs/errors.log + stderr and keep the event loop running.
    def _crash_guard(exc_type, exc_value, exc_tb):
        if exc_type is KeyboardInterrupt:
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        import traceback
        from datetime import datetime
        tb = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        sys.stderr.write(tb)
        sys.stderr.flush()
        try:
            from pathlib import Path
            log_path = Path(__file__).resolve().parent / "logs" / "errors.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now().isoformat()} — unhandled exception ===\n{tb}\n")
        except Exception:
            pass
    sys.excepthook = _crash_guard
    # Same guard for non-Qt worker threads (threading module).
    import threading
    threading.excepthook = lambda args: _crash_guard(
        args.exc_type, args.exc_value, args.exc_traceback)

    # Qt's own message stream (warnings + critical + FATAL). A qFatal prints a
    # one-line reason and then aborts the process in C++ — by default that
    # reason goes nowhere, so the window vanishes with no clue why. Capturing it
    # gives the breadcrumb that names the culprit (e.g. "QObject: Cannot create
    # children for a parent in a different thread", the classic worker-thread-
    # touches-a-widget abort). We log Critical/Fatal to errors.log; Warning and
    # below stay on stderr to avoid spamming the file with benign Qt chatter.
    from PyQt6.QtCore import qInstallMessageHandler, QtMsgType

    def _qt_message_handler(mode, context, message):
        try:
            from datetime import datetime
            from pathlib import Path
            sys.stderr.write(f"[Qt] {message}\n")
            sys.stderr.flush()
            if mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                tag = "FATAL" if mode == QtMsgType.QtFatalMsg else "CRITICAL"
                loc = ""
                if context is not None and context.file:
                    loc = f" ({context.file}:{context.line})"
                log_path = Path(__file__).resolve().parent / "logs" / "errors.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(f"\n=== {datetime.now().isoformat()} — Qt {tag} ===\n"
                            f"{message}{loc}\n")
        except Exception:
            pass
    qInstallMessageHandler(_qt_message_handler)

    # Windows taskbar identity. Set BEFORE any window: without an explicit
    # AppUserModelID, Windows groups us under "pythonw.exe" (generic Python icon,
    # and pinning pins pythonw, not Familiar). Giving the process its own ID makes
    # the taskbar treat Familiar as its own app, so the live window's accent icon
    # is what shows and what pins.
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "Lamport.Familiar.Agent")
        except Exception:
            pass

    # Qt app
    # The embedded Browser (QtWebEngine) requires AA_ShareOpenGLContexts to be
    # set BEFORE the QApplication is created. The workspace panel imports
    # QtWebEngineWidgets lazily during window construction (after this point), so
    # without this attribute that import raises "QtWebEngineWidgets must be
    # imported ... before a QCoreApplication instance is created" and the Browser
    # tab silently falls back to "needs PyQt6 WebEngine" even though it's installed.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(sys.argv)
    # Force Fusion. Qt 6's default "windows11" native style ignores QSS borders
    # on QAbstractScrollArea frames (QTextEdit, QGraphicsView, QScrollArea), so
    # the whole QSS theme renders EXCEPT those borders (the composer outline
    # vanishes). Fusion honors QSS fully — the right base for a QSS-driven theme.
    app.setStyle("Fusion")
    _patch_qt()
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    # Accent-colored sparkle on the QApplication, so every window and dialog
    # inherits it (and it's what the taskbar shows).
    try:
        import os as _os
        import threading as _threading
        from ui.app_icon import apply_app_icon, write_app_ico
        apply_app_icon()
        # Refresh the launcher shortcut's .ico to the current accent (best-effort).
        # Written to data/ (NOT assets/) because it bakes in the USER'S personal
        # accent color — data/ is excluded from packaging, so a personalized icon
        # never ships. START.bat prefers data/agent.ico, falling back to the
        # committed neutral assets/agent.ico. Off the UI hot path.
        _ico = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                             "data", "agent.ico")
        _threading.Thread(target=lambda: write_app_ico(_ico),
                          daemon=True, name="app-ico-refresh").start()
    except Exception:
        pass

    # Register teardown on every exit path: aboutToQuit (graceful close),
    # atexit (Ctrl-C / sys.exit / uncaught). Without this, the inbound network
    # server thread and cloudflared subprocess outlive the window and the
    # process hangs in the terminal.
    import atexit
    app.aboutToQuit.connect(_global_kill_all)
    atexit.register(_global_kill_all)

    # Familiar agent + db
    try:
        init_conversations_db()
        agent = Agent()
    except Exception as e:
        print(f"[Familiar] Failed to init Agent/Database: {e}", flush=True)
        sys.exit(1)

    # Main window
    try:
        window = MainWindow(agent)
        # First-run "digital origami" tour: the app boots as a bare chat card
        # and unfolds itself, narrated by the agent. Hardcoded, zero-cost,
        # replayable from ? → Take Tour. Genesis is set up BEFORE show so the
        # window appears already folded down.
        try:
            from ui.tour import TourDirector
            import ui.tour.director as _tour_dir
            if "--tour" in sys.argv:
                _tour_dir.FORCE_TOUR = True
            if TourDirector.needed():
                window._tour_director = TourDirector(window)
                window._tour_director.prepare_genesis()
        except Exception as e:
            print(f"[Familiar] tour init failed: {e}", flush=True)
        window.show()
        if getattr(window, "_tour_director", None) is not None:
            QTimer.singleShot(250, window._tour_director.begin)
    except Exception as e:
        print(f"[Familiar] Failed to create MainWindow: {e}", flush=True)
        sys.exit(1)

    _source_sync = None
    try:
        from core.source_sync import source_sync as _source_sync
        _source_sync.attach_window(window)
        _source_sync.set_notify(lambda: window._source_update_notify.emit())
        _source_sync.start()
    except Exception as e:
        print(f"[Familiar] source-update watch not started: {e}", flush=True)

    try:
        from core.network import network_manager
        _prior_sync = network_manager.on_sync
        def _sync_chained(data, _p=_prior_sync, _ss=_source_sync):
            if _ss is not None:
                out = _ss.handle(data)
                if out is not None:
                    return out
            if _p is not None:
                return _p(data)
            return None
        network_manager.on_sync = _sync_chained
    except Exception as e:
        print(f"[Familiar] source-sync inbound chain failed: {e}", flush=True)

    if _source_sync is not None:
        atexit.register(_source_sync.stop)

    # Signal handlers
    def sigint_handler(sig, frame):
        print("\n[Familiar] Ctrl-C detected, shutting down gracefully...", flush=True)
        window.close()
        app.quit()

    signal.signal(signal.SIGINT, sigint_handler)

    # Go. After the event loop ends, let Qt finish tearing down QThreads /
    # WebEngine workers before force-exit — otherwise stderr gets
    # "QThreadStorage: entry N destroyed before end of thread" noise.
    exit_code = app.exec()
    _global_kill_all()
    try:
        from PyQt6.QtCore import QThread
        for _ in range(6):
            app.processEvents()
            QThread.msleep(40)
    except Exception:
        pass
    import os
    os._exit(exit_code)


if __name__ == "__main__":
    main()
