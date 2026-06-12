import sys
import signal

# Qt imports
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout,
)
from PyQt6.QtCore import (
    Qt, QSize, QRect, QPoint, QEvent, QObject, QTimer, QAbstractItemModel,
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

        # App-wide event filter: catches mouse events anywhere in the window so
        # the frameless edges work as resize grips (and don't get swallowed by
        # child widgets). Mirrors the root Familiar window.
        QApplication.instance().installEventFilter(self)

        # Preload all sounds in the background so the first play is instant.
        import threading
        threading.Thread(target=self._preload_sounds, daemon=True).start()
        # Warm the Settings dialog's import chain (tools.registry → mcp/httpx
        # → numpy, ~1s cold) so the first click on Settings opens instantly.
        # Module import only — no QWidget is constructed off-thread.
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
        dlg = HelpDialog(self)
        dlg.exec()
    
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
        window.show()
    except Exception as e:
        print(f"[Familiar] Failed to create MainWindow: {e}", flush=True)
        sys.exit(1)

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
