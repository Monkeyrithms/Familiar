"""
Agent - LLM chat interface with modular tool support.
"""

import json
import subprocess
import sys
import threading
from pathlib import Path
from PyQt6.QtWidgets import QApplication, QMainWindow, QVBoxLayout, QWidget
from PyQt6.QtCore import Qt, QRect, QPoint, QTimer, QEvent, QObject, QAbstractItemModel
from PyQt6.QtGui import QPainter, QColor, QPen, QMouseEvent, QImage
import tools  # noqa: F401 — triggers tool self-registration

WINDOW_STATE_PATH = Path(__file__).parent / "data" / "window_state.json"
from ui.title_bar import TitleBar
from ui.chat_widget import ChatWindow
from ui.theme import PALETTE, refresh_palette, apply_selection_palette
from ui.app_icon import apply_app_icon
from core.agent import Agent
from core.sounds import preload_all, play_ui
from core.ui_watchdog import UiPerformanceWatchdog

GRIP = 6

APP_NAME = "Familiar"
# Unique per app — keeps this out of the generic python.exe taskbar group.
APP_USER_MODEL_ID = "Casey.Familiar"

# Single-instance guard keys. A shared-memory segment marks "primary alive";
# a local socket lets a second launch ask the primary to surface its window.
_SINGLE_MEM_KEY = "Familiar-single-instance-v1"
_SINGLE_IPC_KEY = "Familiar-ipc-v1"
_single_shm = None  # module-global so the segment lives for the whole process


def _configure_windows_app_identity() -> None:
    """Taskbar grouping + hover label when running under python.exe on Windows."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def _repair_agent_shortcut() -> None:
    """Re-point shipped Agent.lnk to this folder (portable copy / new machine)."""
    if sys.platform != "win32":
        return
    script = Path(__file__).resolve().parent / "repair_agent_shortcut.ps1"
    if not script.is_file():
        return
    try:
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(script),
                "-Root",
                str(script.parent),
            ],
            cwd=script.parent,
            capture_output=True,
            timeout=15,
            check=False,
        )
    except Exception:
        pass


def _disable_console_quick_edit() -> None:
    """Windows: Quick Edit Mode freezes the entire process when the console is
    clicked (text selection). Pressing Enter in the terminal unfreezes it —
    a common false 'app hung' report for GUI apps launched via ``py main.py``."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ENABLE_QUICK_EDIT = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        STD_INPUT_HANDLE = -10
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if handle in (0, -1):
            return
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        new_mode = (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass


# ── Single-instance guard ──────────────────────────────────────────────
# Familiar must never open two windows. A second launch (double-click, a
# stray relaunch, etc.) detects the running instance, asks it to come to the
# front, and exits instead of spawning a duplicate UI.

def _live_instance_running(*, command: bytes = b"raise") -> bool:
    """True ONLY if a *responsive* Familiar is already listening on the IPC
    socket — and, if so, sends it ``command`` ("raise" to surface it, "quit" to
    ask it to exit).

    Why IPC and not shared memory: the IPC server exists only while a real Qt
    event loop is running, so a successful connect is authoritative proof of a
    live instance. A held shared-memory lock is NOT trusted on its own — an
    orphaned segment or an invisible ``pythonw`` process that died without
    releasing it would otherwise block every future launch (the "it keeps
    surfacing stale code / won't start fresh" bug). If we can't connect, there
    is no live primary and we should take over.
    """
    try:
        from PyQt6.QtNetwork import QLocalSocket
    except Exception:
        return False  # can't probe — fail open rather than block startup
    s = QLocalSocket()
    s.connectToServer(_SINGLE_IPC_KEY)
    if not s.waitForConnected(600):
        s.abort()
        return False
    try:
        s.write(command)
        s.flush()
        s.waitForBytesWritten(600)
        if command == b"quit":
            # Give the old instance a moment to release the socket/lock.
            s.waitForDisconnected(2000)
        else:
            s.disconnectFromServer()
    except Exception:
        pass
    return True


def _claim_singleton_marker(app) -> None:
    """Best-effort: hold the shared-memory marker for this process's lifetime.
    Purely a secondary hint now (the IPC server is the real liveness signal),
    so a failure here never blocks startup."""
    global _single_shm
    try:
        from PyQt6.QtCore import QSharedMemory
    except Exception:
        return
    try:
        shm = QSharedMemory(_SINGLE_MEM_KEY)
        # Clear a segment orphaned by a crash (POSIX; no-op on Windows).
        if shm.attach():
            shm.detach()
        if shm.create(1):
            _single_shm = shm
            app._familiar_shm = shm  # extra reference; freed on process exit
    except Exception:
        pass


def _raise_window(window) -> None:
    """Bring the primary window to the foreground (Windows-aware)."""
    try:
        if window.isMinimized():
            window.showNormal()
        window.show()
        window.raise_()
        window.activateWindow()
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.user32.SetForegroundWindow(int(window.winId()))
            except Exception:
                pass
    except Exception:
        pass


def _install_singleton_server(app, window) -> None:
    """Listen for pings from future launches and raise the window when one
    arrives — so clicking the icon again focuses Familiar instead of nothing."""
    try:
        from PyQt6.QtNetwork import QLocalServer
        QLocalServer.removeServer(_SINGLE_IPC_KEY)  # clear a stale socket file
        srv = QLocalServer()
        # Reclaim a name a crashed instance left bound, so our IPC server (the
        # authoritative liveness signal) always comes up — otherwise the next
        # launch can't detect or replace us.
        try:
            srv.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        except Exception:
            pass
        if not srv.listen(_SINGLE_IPC_KEY):
            QLocalServer.removeServer(_SINGLE_IPC_KEY)
            if not srv.listen(_SINGLE_IPC_KEY):
                print(f"[{APP_NAME}] WARNING: single-instance IPC server failed to "
                      f"bind; --replace may not work until restart.", flush=True)
                return

        def _on_conn():
            conn = srv.nextPendingConnection()
            if conn is None:
                return
            try:
                conn.waitForReadyRead(200)
                msg = bytes(conn.readAll()).strip()
            except Exception:
                msg = b""
            if msg == b"quit":
                # A new launch asked us (the old instance) to step aside.
                try:
                    conn.disconnectFromServer()
                except Exception:
                    pass
                from PyQt6.QtWidgets import QApplication
                QApplication.instance().quit()
                return
            _raise_window(window)

        srv.newConnection.connect(_on_conn)
        app._familiar_ipc_server = srv
    except Exception:
        pass


class MainWindow(QMainWindow):
    def __init__(self):
        # Set before super(): Qt can invoke resizeEvent while QMainWindow inits.
        self._crt_overlay = None
        super().__init__()
        self._always_on_top_enabled = False
        self.setWindowTitle(APP_NAME)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setMinimumSize(600, 500)
        self.setMouseTracking(True)
        self._restore_geometry()

        self._resize_edge = 0
        self._resize_start_geom = None
        self._resize_start_pos = None
        self._last_resize_hover_edge = None  # throttle cursor updates on MouseMove

        self.agent = Agent()

        central = QWidget()
        central.setMouseTracking(True)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)

        self.title_bar = TitleBar(APP_NAME)
        self.title_bar.settings_clicked.connect(self._open_settings)
        self.title_bar.tasks_clicked.connect(self._open_tasks)
        self.title_bar.memory_clicked.connect(self._open_memory)
        self.title_bar.screenshot_clicked.connect(self._screenshot_to_clipboard)
        self.title_bar.always_on_top_clicked.connect(self._toggle_always_on_top)
        self.title_bar.minimize_clicked.connect(self.showMinimized)
        self.title_bar.maximize_clicked.connect(self._toggle_maximize)
        self.title_bar.close_clicked.connect(self.close)
        layout.addWidget(self.title_bar)

        self.chat = ChatWindow(self.agent, parent=central)
        layout.addWidget(self.chat)

        self.setCentralWidget(central)
        self._apply_styles()

        # CRT scanline overlay (off by default; _crt_overlay initialized above)
        self._apply_crt()

        # Install event filter on the whole app to catch resize at edges
        QApplication.instance().installEventFilter(self)

        QTimer.singleShot(100, lambda: self.chat.input.setFocus())

        # Preload all sounds in background so first play is instant
        threading.Thread(target=self._preload_sounds, daemon=True).start()

        # Main-thread stall watchdog (same behavior as Vispy_dashboard).
        self._ui_watchdog = UiPerformanceWatchdog(self)
        self._ui_watchdog.start()

    # ── Resize edge detection ────────────────────────────────────────

    def _edge_at_global(self, global_pos: QPoint) -> int:
        """Check if a global position is within the resize grip of this window."""
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

    # ── App-wide event filter for resize ─────────────────────────────

    def _is_my_widget(self, obj) -> bool:
        """Check if obj belongs to this window (not a dialog)."""
        from PyQt6.QtWidgets import QDialog, QWidget

        w = obj
        while w is not None:
            if w is self:
                return True
            if isinstance(w, QDialog):
                return False
            # QWidget: walk the widget hierarchy (handles layouts / native parents).
            if isinstance(w, QWidget):
                w = w.parentWidget()
                continue
            # QAbstractItemModel.parent(index) shadows QObject.parent() — use QObject's parent.
            if isinstance(w, QAbstractItemModel):
                w = super(QAbstractItemModel, w).parent()
                continue
            if isinstance(w, QObject):
                w = w.parent()
                continue
            return False
        return False

    def eventFilter(self, obj, event):
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

        et = event.type()
        # Resize logic only cares about mouse events. Skipping the widget-tree walk
        # for keyboard/focus/timer traffic avoids per-keystroke overhead in the
        # composer (and everywhere else) when the user is typing.
        if et not in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseMove,
            QEvent.Type.MouseButtonRelease,
        ):
            return super().eventFilter(obj, event)

        # Only intercept events from our own widgets, not dialogs
        if not self._is_my_widget(obj):
            return super().eventFilter(obj, event)

        if et == QEvent.Type.MouseButtonPress:
            if isinstance(event, QMouseEvent) and event.button() == Qt.MouseButton.LeftButton:
                gp = event.globalPosition().toPoint()
                edge = self._edge_at_global(gp)
                if edge:
                    self._resize_edge = edge
                    self._resize_start_geom = QRect(self.geometry())
                    self._resize_start_pos = gp
                    QApplication.setOverrideCursor(self._cursor_for_edge(edge))
                    return True

        if et == QEvent.Type.MouseMove and not self._resize_edge:
            if isinstance(event, QMouseEvent):
                gp = event.globalPosition().toPoint()
                edge = self._edge_at_global(gp)
                if edge != getattr(self, "_last_resize_hover_edge", None):
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

    # ── Focus input on window activate ────────────────────────────────

    def changeEvent(self, event):
        if event.type() == QEvent.Type.WindowActivate:
            QTimer.singleShot(0, lambda: self.chat.input.setFocus())
            # Keep the CRT layer pinned above us when we regain focus.
            co = getattr(self, "_crt_overlay", None)
            if co is not None and co.isVisible():
                co.raise_()
        elif event.type() == QEvent.Type.WindowStateChange:
            # Restore/maximize/normal: re-glue the overlay once geometry settles.
            co = getattr(self, "_crt_overlay", None)
            if co is not None and not self.isMinimized():
                QTimer.singleShot(0, lambda: (co.sync_geometry(), co.raise_()))
        super().changeEvent(event)

    # ── Paint border ─────────────────────────────────────────────────

    def paintEvent(self, event):
        super().paintEvent(event)
        p = PALETTE
        painter = QPainter(self)
        pen = QPen(QColor(p["accent_muted"]))
        pen.setWidth(1)
        painter.setPen(pen)
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    # ── Window controls ──────────────────────────────────────────────

    # ── Window geometry persistence ────────────────────────────────

    @staticmethod
    def _on_any_screen(rect: QRect) -> bool:
        """True when rect overlaps a visible desktop area enough to be usable."""
        min_visible = 120  # px
        for scr in QApplication.screens():
            avail = scr.availableGeometry()
            overlap = rect.intersected(avail)
            if overlap.width() >= min_visible and overlap.height() >= min_visible:
                return True
        return False

    @staticmethod
    def _fallback_geometry(width: int, height: int) -> QRect:
        """Centered fallback on primary screen with sane bounds."""
        scr = QApplication.primaryScreen()
        if scr is None:
            return QRect(100, 100, max(800, width), max(700, height))
        avail = scr.availableGeometry()
        w = max(600, min(width, avail.width()))
        h = max(500, min(height, avail.height()))
        x = avail.x() + (avail.width() - w) // 2
        y = avail.y() + (avail.height() - h) // 2
        return QRect(x, y, w, h)

    def _restore_geometry(self):
        try:
            state = json.loads(WINDOW_STATE_PATH.read_text(encoding="utf-8"))
            restored = QRect(
                int(state.get("x", 100)),
                int(state.get("y", 100)),
                int(state.get("w", 800)),
                int(state.get("h", 700)),
            )
            if restored.width() < 600 or restored.height() < 500 or not self._on_any_screen(restored):
                restored = self._fallback_geometry(restored.width(), restored.height())
            self.setGeometry(restored)
            if state.get("maximized"):
                self.showMaximized()
            if state.get("always_on_top"):
                self.title_bar.always_on_top_btn.setChecked(True)
                self._toggle_always_on_top(True)
        except Exception:
            self.resize(800, 700)

    def _save_geometry(self):
        WINDOW_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        geo = self.geometry()
        state = {
            "x": geo.x(), "y": geo.y(),
            "w": geo.width(), "h": geo.height(),
            "maximized": self.isMaximized(),
            "always_on_top": bool(getattr(self, "_always_on_top_enabled", False)),
        }
        WINDOW_STATE_PATH.write_text(
            json.dumps(state) + "\n", encoding="utf-8")

    # ── Window controls ──────────────────────────────────────────────

    def _toggle_maximize(self):
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def _toggle_always_on_top(self, enabled: bool):
        self._always_on_top_enabled = bool(enabled)
        # Avoid full top-level reconfiguration flicker on Windows.
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
                flags = (
                    SWP_NOSIZE
                    | SWP_NOMOVE
                    | SWP_NOACTIVATE
                    | SWP_FRAMECHANGED
                    | SWP_NOOWNERZORDER
                    | SWP_NOSENDCHANGING
                )
                ctypes.windll.user32.SetWindowPos(
                    hwnd,
                    insert_after,
                    0,
                    0,
                    0,
                    0,
                    flags,
                )
            except Exception:
                self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
                self.show()
        else:
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, enabled)
            self.show()
        try:
            play_ui("beep.mp3")
        except Exception:
            pass

    @staticmethod
    def _preload_sounds():
        try:
            preload_all()
        except Exception:
            pass

    def _screenshot_to_clipboard(self):
        """Grab the entire window as a screenshot and copy to clipboard."""
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtCore import QBuffer, QIODevice, QMimeData

        pixmap = self.grab()
        data = QMimeData()
        buf = QBuffer()
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        pixmap.save(buf, "PNG")
        buf.close()
        data.setData("image/png", buf.data())
        data.setImageData(pixmap.toImage())
        QApplication.clipboard().setMimeData(data)
        try:
            from core.sounds import play_ui
            play_ui("snapshot.mp3")
        except Exception:
            pass
        self._do_camera_flash()

    def _do_camera_flash(self):
        """White flash overlay that fades out — simulates a camera flash."""
        from PyQt6.QtWidgets import QWidget
        from PyQt6.QtCore import QPropertyAnimation
        flash = QWidget(self)
        flash.setGeometry(self.rect())
        flash.setStyleSheet("background: white;")
        flash.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        flash.show()
        flash.raise_()

        # Use a QTimer-driven step-down because QPropertyAnimation needs
        # a Q_PROPERTY; simpler to just tick the opacity via stylesheet.
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
            flash.setStyleSheet(f"background: rgba(255,255,255,{int(opacity*255)});")
            self._flash_step += 1
            QTimer.singleShot(30, _tick)

        QTimer.singleShot(30, _tick)

    def _open_settings(self):
        # Non-modal now: pass the theme/CRT refresh as an on-accept callback
        # instead of relying on a blocking exec() return value, so the main
        # window (always-on-top, screenshot, chat) stays usable while Settings
        # is open.
        def _on_accept():
            refresh_palette()
            apply_app_icon(window=self)
            self._apply_styles()
            self.title_bar.apply_theme()
            self.chat.apply_theme()
            self._apply_crt()
            self.update()

        self.chat._open_settings(on_accept=_on_accept)

    def _open_tasks(self):
        from ui.tasks_dialog import TasksDialog
        dlg = TasksDialog(self.chat, parent=self)
        dlg.exec()

    def _open_memory(self):
        from ui.memory_dialog import MemoryDialog
        dlg = MemoryDialog(parent=self)
        dlg.exec()

    def _apply_crt(self):
        """Create or destroy CRT overlay based on config."""
        from core.agent import load_config
        cfg = load_config()
        enabled = cfg.get("crt_enabled", False)
        speed = cfg.get("crt_speed", 600)

        if enabled and not self._crt_overlay:
            from ui.crt_overlay import CRTOverlay
            from PyQt6.QtGui import QColor
            # Parent to the CENTRAL WIDGET, not the QMainWindow. A child of the
            # main window sits behind the central widget (which fills the client
            # area and paints over it); a child of the central widget, raised,
            # reliably layers above all the app content.
            host = self.centralWidget() or self
            self._crt_overlay = CRTOverlay(
                host, QColor(PALETTE["accent"]), speed_ms=speed)
            self._crt_overlay.sync_geometry()
            self._crt_overlay.show()
            self._crt_overlay.raise_()
            # _apply_crt can run before the window is shown/laid out, when the
            # central widget's rect is still tiny — that left the overlay stuck
            # as a small box in the top-left (the "ugly square") with no visible
            # scanlines until a manual resize re-synced it. Re-sync once the
            # event loop has settled the real geometry.
            QTimer.singleShot(0, self._sync_crt_overlay)
            QTimer.singleShot(150, self._sync_crt_overlay)
        elif not enabled and self._crt_overlay:
            self._crt_overlay.hide()
            self._crt_overlay.deleteLater()
            self._crt_overlay = None
        elif enabled and self._crt_overlay:
            from PyQt6.QtGui import QColor
            self._crt_overlay.set_border_color(QColor(PALETTE["accent"]))
            self._crt_overlay.set_speed(speed)

    def _sync_crt_overlay(self):
        """Re-glue the CRT overlay to the current host rect. Safe to call any
        time; no-op when the overlay is off."""
        co = getattr(self, "_crt_overlay", None)
        if co is not None:
            try:
                co.sync_geometry()
            except RuntimeError:
                pass

    def showEvent(self, event):
        super().showEvent(event)
        # First real geometry arrives with the show; sync the overlay so it
        # covers the whole window instead of the tiny pre-layout rect.
        QTimer.singleShot(0, self._sync_crt_overlay)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_crt_overlay()

    def moveEvent(self, event):
        super().moveEvent(event)
        # CRT overlay is a child widget (follows the parent automatically); the
        # re-sync here is a cheap, harmless safety net for edge cases.
        self._sync_crt_overlay()

    def _apply_styles(self):
        p = PALETTE
        self.setStyleSheet(f"""
            QMainWindow {{
                background: {p['background']};
            }}
        """)

    def closeEvent(self, event):
        """Persist geometry, stop watchdog, release processes and timers."""
        self._save_geometry()
        try:
            self.chat._composer_draft_timer.stop()
            self.chat._persist_current_composer_draft()
        except Exception:
            pass
        try:
            self.chat._auto_save(immediate=True)
        except Exception:
            pass
        try:
            from core.database import flush_pending_conversation_saves
            flush_pending_conversation_saves()
        except Exception:
            pass
        try:
            self._ui_watchdog.stop()
        except Exception:
            pass
        # Stop inference if running
        try:
            self.chat._stop_inference()
        except Exception:
            pass
        # Kill all sub-agent orchestrators
        try:
            from core.subagent import _orchestrators
            for orch in list(_orchestrators.values()):
                try:
                    orch.shutdown()
                except Exception:
                    pass
        except Exception:
            pass
        # Persist terminal tabs for restart resume (live cwd + last command,
        # e.g. `claude --continue`) BEFORE any shells are killed — deep detection
        # inspects the live shell trees via psutil. Covers BOTH the Terminal
        # workspace (per conversation) and the File viewer's terminals.
        try:
            self.chat._right_workspace.terminal_panel.save_all(deep=True)
        except Exception:
            pass
        try:
            self.chat._file_viewer.save_terminal_state(deep=True)
        except Exception:
            pass
        # Stop all terminal shell processes across every conversation panel
        # (fixes QProcess destroyed warnings). Per-conv panels each registered
        # their own aboutToQuit handler, but call close here too for the
        # synchronous shutdown path where closeEvent runs first.
        try:
            multi = self.chat._right_workspace.terminal_panel
            for panel in list(getattr(multi, "_panels", {}).values()):
                try:
                    panel.close_all_sessions()
                except Exception:
                    pass
        except Exception:
            pass
        # Kill ALL agent-spawned subprocess trees (foreground + background).
        # `taskkill /T /F` on Windows reaches descendants — proc.kill() only
        # kills the cmd.exe wrapper, orphaning python.exe and friends.
        try:
            from tools.terminal import shutdown_all_processes
            shutdown_all_processes()
        except Exception:
            pass
        # Shut down LSP servers
        try:
            from core.lsp_client import lsp_manager
            lsp_manager.shutdown_all()
        except Exception:
            pass
        super().closeEvent(event)


def main():
    _configure_windows_app_identity()
    _repair_agent_shortcut()
    _disable_console_quick_edit()

    # WebEngine (right workspace browser) expects shared GL on many platforms.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    # Wrap Fusion in a proxy style that swallows the dotted focus rectangle.
    # QLabel's rich-text engine draws anchor focus rects via the APPLICATION
    # style (not a widget's per-instance style), so suppressing it here is what
    # actually removes the 90s-hyperlink box around clicked tool-call chips —
    # including while a modal dialog freezes the app behind it. The whole app is
    # custom-themed, so native focus rects are unwanted everywhere anyway.
    from PyQt6.QtWidgets import QProxyStyle, QStyle, QStyleFactory

    class _NoFocusRectAppStyle(QProxyStyle):
        def drawPrimitive(self, element, option, painter, widget=None):
            if element == QStyle.PrimitiveElement.PE_FrameFocusRect:
                return
            super().drawPrimitive(element, option, painter, widget)

    app.setStyle(_NoFocusRectAppStyle(QStyleFactory.create("Fusion")))
    apply_selection_palette()  # accent-colored text/list selection, not grey
    apply_app_icon()

    # Single-instance guard, now keyed on a LIVE IPC probe (not a possibly-stale
    # shared-memory lock). --replace/--force tells any existing instance to quit
    # and takes over — the escape hatch for dev restarts where an invisible
    # pythonw orphan was holding the lock and serving stale code.
    replace = any(a in ("--replace", "--force", "--restart") for a in sys.argv[1:])
    if replace:
        if _live_instance_running(command=b"quit"):
            print(f"[{APP_NAME}] Asked the running instance to quit; taking over.")
            # Wait for the old IPC server to actually free the socket name.
            import time as _time
            for _ in range(20):  # up to ~2s
                if not _live_instance_running(command=b"raise"):
                    break
                _time.sleep(0.1)
    elif _live_instance_running(command=b"raise"):
        print(f"[{APP_NAME}] Already running — bringing the existing window to the front.")
        print(f"[{APP_NAME}] (Run 'py main.py --replace' to force a fresh instance.)")
        return

    _claim_singleton_marker(app)

    # ── Splash: show immediately so startup (tool imports + conversation
    # hydration) doesn't stare at a blank screen. Non-blocking; faded out once
    # the first conversation has rendered. Only the surviving instance shows it.
    splash = None
    try:
        from ui.splash_screen import SplashScreen
        splash = SplashScreen()
        splash.show()
        app.processEvents()  # paint it before the heavy work begins
    except Exception:
        splash = None

    def _splash_status(text: str):
        if splash is not None:
            splash.set_status(text)
            app.processEvents()

    # Prerequisite check runs with the splash up (it does ~7 PATH probes).
    _splash_status("Checking prerequisites…")
    from core.prereqs import check_prerequisites
    check_prerequisites()

    # Belt-and-suspenders cleanup: register shutdown on every exit path we can.
    # closeEvent runs on a graceful X-close. aboutToQuit also covers programmatic
    # quit. atexit catches the rest (Ctrl+C in launcher, sys.exit, uncaught
    # exceptions). Every layer kills the full subprocess tree, not just the
    # cmd.exe wrapper, so python scripts/servers don't get orphaned.
    import atexit
    def _global_kill_all():
        try:
            from tools.terminal import shutdown_all_processes
            shutdown_all_processes()
        except Exception:
            pass
    app.aboutToQuit.connect(_global_kill_all)
    atexit.register(_global_kill_all)

    # Start hot-reload watcher for tools/
    _splash_status("Loading tools…")
    from tools.hot_reload import ToolWatcher
    watcher = ToolWatcher()
    watcher.start()

    _splash_status("Building interface…")
    window = MainWindow()
    window.setWindowIcon(app.windowIcon())
    # Opening ceremony: the wordmark starts dark (bulb off) behind the splash;
    # it ignites shortly after the splash clears (see _dismiss below).
    try:
        window.title_bar.set_unlit()
    except Exception:
        pass
    _install_singleton_server(app, window)

    _splash_status("Restoring conversation…")
    window.show()

    # Fade the splash once the window is up and the first conversation has had a
    # moment to hydrate (it loads async). The chat exposes a one-shot signal when
    # its initial conversation finishes rendering; fall back to a timer so the
    # splash never lingers if that signal doesn't fire.
    if splash is not None:
        dismissed = {"v": False}

        def _focus_input():
            # Land the cursor in the message box so the user can type immediately
            # (the +100ms focus at startup gets stolen by the splash teardown +
            # window activation that happen later). Deferred so it runs after the
            # activation settles.
            try:
                window.chat.input.setFocus()
            except Exception:
                pass

        def _dismiss():
            if dismissed["v"]:
                return
            dismissed["v"] = True
            window.raise_()
            window.activateWindow()
            QTimer.singleShot(0, _focus_input)
            splash.fade_out()
            # Opening ceremony: 0.5s after the splash clears, chime (start.mp3);
            # then 1.3s after that, play the lightbulb sound and ignite the
            # wordmark from dark to glow-hot.
            def _lights_on():
                # The bulb ignites and, in the same beat, the pristine intro
                # hint fades in over the empty chat.
                try:
                    window.title_bar.ignite()
                except Exception:
                    pass
                try:
                    window.chat.show_intro_hint_if_pristine()
                except Exception:
                    pass
                _focus_input()  # ensure the box still holds focus after the ceremony

            def _chime_then_ignite():
                try:
                    from core.sounds import play_ui
                    play_ui("start.mp3", volume=0.5)  # softer launch chime
                except Exception:
                    pass
                try:
                    QTimer.singleShot(1300, _lights_on)
                except Exception:
                    pass
            QTimer.singleShot(500, _chime_then_ignite)

        ready = getattr(getattr(window, "chat", None), "initial_load_finished", None)
        if ready is not None:
            try:
                ready.connect(lambda *_: _dismiss())
            except Exception:
                pass
        # Safety net: dismiss after a short delay regardless (covers the
        # no-signal path and very fast loads).
        QTimer.singleShot(1200, _dismiss)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()