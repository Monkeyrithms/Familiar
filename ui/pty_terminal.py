"""
Real pseudo-terminal (PTY) backend + VT/ANSI emulator widget.

The legacy workspace terminal (``ui/terminal_workspace.py``) drives ``cmd.exe``
through ``QProcess`` pipes. Pipes are not a TTY, so full-screen TUIs — Claude
Code, vim, htop — detect ``isatty() == False`` and refuse to run interactively
(Claude falls into ``--print`` mode and exits). This module gives the shell a
real **ConPTY** (via ``pywinpty`` on Windows, ``ptyprocess`` on POSIX) and renders
its raw ANSI/VT byte stream through a ``pyte`` screen, so any terminal program
runs exactly as it would in a native console.

Two pieces:

* :class:`PtyBackend` — owns the pty child + a blocking reader thread, surfaced
  to the GUI thread via Qt signals.
* :class:`PtyTerminalView` — a ``QWidget`` that feeds bytes into a
  ``pyte.HistoryScreen`` and paints the cell grid, translating key/mouse input
  back into the escape sequences the program expects.

Availability is probed at import (:data:`PTY_AVAILABLE`); ``IntegratedTerminalSession``
falls back to the legacy ``QProcess`` path when the deps are absent.
"""

from __future__ import annotations

import os
import sys
import time
import threading
import subprocess

from PyQt6.QtCore import Qt, QObject, QTimer, QRect, pyqtSignal
from PyQt6.QtGui import (
    QPainter, QColor, QFont, QFontMetricsF, QKeyEvent, QTextOption,
)
from PyQt6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PyQt6.QtWidgets import QWidget, QApplication, QMenu

from ui.theme import PALETTE


# ── Dependency probe ──────────────────────────────────────────────────
try:
    import pyte  # VT100/ANSI screen emulator
    if sys.platform == "win32":
        from winpty import PtyProcess as _PtyProcess  # ConPTY wrapper
    else:
        from ptyprocess import PtyProcess as _PtyProcess  # type: ignore
    PTY_AVAILABLE = True
except Exception:  # pragma: no cover - depends on host
    pyte = None  # type: ignore
    _PtyProcess = None  # type: ignore
    PTY_AVAILABLE = False


# ── ANSI 16-color palette (xterm-ish). pyte names per pyte.graphics. ───
# "brown" is pyte's name for yellow; "bright*" come from the AIXTERM range.
_ANSI = {
    "black":         "#1e1e1e",
    "red":           "#cd3131",
    "green":         "#0dbc79",
    "brown":         "#e5e510",  # yellow
    "blue":          "#2472c8",
    "magenta":       "#bc3fbc",
    "cyan":          "#11a8cd",
    "white":         "#e5e5e5",
    "brightblack":   "#666666",
    "brightred":     "#f14c4c",
    "brightgreen":   "#23d18b",
    "brightbrown":   "#f5f543",  # bright yellow
    "brightblue":    "#3b8eea",
    "brightmagenta": "#d670d6",
    "brightcyan":    "#29b8db",
    "brightwhite":   "#ffffff",
}


def _env_with_refreshed_path() -> dict:
    """Child env with PATH re-read from the Windows registry.

    The app inherits its PATH at launch; a CLI installed afterwards (e.g.
    ``cursor-agent``, whose installer appends its dir to the *user* PATH) stays
    invisible to shells we spawn until the app restarts — and even a restart can
    miss it if Explorer hasn't picked up the change yet. Merging the live
    HKLM + HKCU 'Path' here makes such tools resolve in a freshly-opened
    terminal. Defensive: any failure falls back to the inherited environment."""
    env = dict(os.environ)
    if sys.platform != "win32":
        return env
    try:
        import winreg
        parts: list[str] = []
        for root, sub in (
            (winreg.HKEY_LOCAL_MACHINE,
             r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            (winreg.HKEY_CURRENT_USER, "Environment"),
        ):
            try:
                with winreg.OpenKey(root, sub) as k:
                    val, _ = winreg.QueryValueEx(k, "Path")
                for p in os.path.expandvars(val).split(os.pathsep):
                    if p:
                        parts.append(p)
            except OSError:
                pass  # hive/value absent — skip it
        merged, seen = [], set()
        for p in parts + env.get("PATH", "").split(os.pathsep):
            key = p.rstrip("\\").lower()
            if p and key not in seen:
                seen.add(key)
                merged.append(p)
        if merged:
            env["PATH"] = os.pathsep.join(merged)
    except Exception:
        pass
    return env


# ══════════════════════════════════════════════════════════════════════
# PTY backend
# ══════════════════════════════════════════════════════════════════════
class PtyBackend(QObject):
    """Owns a pty child process and pumps its output to the GUI thread."""

    data_received = pyqtSignal(str)
    finished = pyqtSignal(int)

    def __init__(self, cwd: str, rows: int = 24, cols: int = 80, parent=None):
        super().__init__(parent)
        self._cwd = cwd or os.getcwd()
        self._proc = None
        self._reader: threading.Thread | None = None
        self._stop = False
        self._rows, self._cols = max(1, rows), max(1, cols)
        # Input written before the shell's line editor is ready (e.g. an agent
        # bg-tab command fired right after spawn) gets eaten by ConPTY. Buffer
        # writes until the prompt appears (see _note_output), then flush in order.
        self._ready = False
        self._lock = threading.Lock()
        self._pending: list[str] = []
        # Bounded raw-output ring so a remote viewer attaching mid-session can
        # replay recent scrollback (with ANSI intact) and see the live state —
        # e.g. an agent already running in this shell.
        self._raw_history = ""
        self._raw_cap = 256 * 1024

    # ── Lifecycle ──────────────────────────────────────────────────
    def start(self) -> bool:
        if not os.path.isdir(self._cwd):
            self._cwd = os.getcwd()
        if sys.platform == "win32":
            argv = os.environ.get("COMSPEC", "cmd.exe")
        else:
            argv = ["/bin/bash", "-i"] if os.path.exists("/bin/bash") else ["/bin/sh", "-i"]
        env = _env_with_refreshed_path()
        env.setdefault("TERM", "xterm-256color")
        try:
            # pywinpty/ptyprocess both take dimensions as (rows, cols).
            self._proc = _PtyProcess.spawn(
                argv, cwd=self._cwd, env=env, dimensions=(self._rows, self._cols)
            )
        except Exception as e:
            self.data_received.emit(f"[error] could not start pty shell: {e}\r\n")
            return False
        # Flush queued writes once the shell is up — on the MAIN thread.
        # (Writing from the reader thread and then reading on it stalls
        # pywinpty; all writes must happen on the thread that owns start().)
        # Injecting input while cmd.exe is still initializing wedges it, so we
        # wait until output goes idle (prompt printed, shell waiting) before
        # flushing, with a hard backstop in case output never settles.
        self._outbuf = ""
        self.data_received.connect(self._note_output)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # Hard backstop: cmd.exe's ConPTY cold start can stay silent for ~3s
        # before printing its prompt, so this must be generous. Prompt
        # detection (below) opens the gate first in the normal case.
        QTimer.singleShot(8000, self._flush_pending)
        return True

    def _note_output(self, data: str):
        # Open the gate once the shell prompt appears — its real "ready"
        # signal. Injecting input before then wedges cmd.exe's startup, so we
        # never flush on timing alone.
        if self._ready:
            return
        self._outbuf = (self._outbuf + data)[-4096:]
        if self._has_prompt(self._outbuf):
            self._flush_pending()

    @staticmethod
    def _has_prompt(text: str) -> bool:
        # cmd ends a prompt with ">"; POSIX shells with "$ "/"# ". The escape-
        # only init banner contains none of these.
        for line in reversed(text.splitlines()):
            s = line.rstrip()
            if not s:
                continue
            return s.endswith(">") or s.endswith("$") or s.endswith("#") \
                or s.endswith("$ ") or s.endswith("# ")
        return False

    def _flush_pending(self):
        with self._lock:
            if self._ready:
                return
            pend, self._pending = self._pending, []
            self._ready = True
        for s in pend:
            try:
                self._proc.write(s)
            except Exception:
                pass

    def _read_loop(self):
        proc = self._proc
        while not self._stop:
            try:
                # NOTE: keep this modest. pywinpty's socket-backed read stalls
                # with very large sizes (e.g. 65536) — output past the first
                # packet never arrives. 4096 reads reliably; the loop drains
                # bursts in multiple passes.
                data = proc.read(4096)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                # Non-blocking empty read — yield briefly to avoid a busy spin.
                time.sleep(0.005)
                continue
            # Queued writes are flushed on the main thread (_note_output) —
            # never write from this reader thread; it stalls pywinpty.
            if self._stop:
                break
            with self._lock:
                self._raw_history = (self._raw_history + data)[-self._raw_cap:]
            try:
                self.data_received.emit(data)
            except RuntimeError:
                # Backend QObject deleted out from under us during shutdown.
                break
        code = 0
        try:
            code = int(proc.exitstatus) if proc.exitstatus is not None else 0
        except Exception:
            code = 0
        # On a deliberate kill() the QObject may already be torn down; skip the
        # signal (nobody is listening during shutdown) and never touch a deleted
        # C++ object — that's the RuntimeError seen on close.
        if not self._stop:
            try:
                self.finished.emit(code)
            except RuntimeError:
                pass

    # ── Input / control (called on GUI thread) ─────────────────────
    def write(self, data: str):
        proc = self._proc
        if proc is None:
            return
        if not self._ready:
            # Queue until the shell prompt appears (flushed by _flush_pending).
            with self._lock:
                if not self._ready:
                    self._pending.append(data)
                    return
        try:
            proc.write(data)
        except Exception:
            pass

    def resize(self, rows: int, cols: int):
        self._rows, self._cols = max(1, rows), max(1, cols)
        proc = self._proc
        if proc is None:
            return
        try:
            proc.setwinsize(self._rows, self._cols)
        except Exception:
            pass

    def raw_history(self) -> str:
        """Recent raw output (with ANSI) for replaying to an attaching viewer."""
        with self._lock:
            return self._raw_history

    def dimensions(self) -> tuple[int, int]:
        return (self._rows, self._cols)

    def is_alive(self) -> bool:
        proc = self._proc
        if proc is None:
            return False
        try:
            return bool(proc.isalive())
        except Exception:
            return False

    def pid(self) -> int:
        try:
            return int(getattr(self._proc, "pid", 0) or 0)
        except Exception:
            return 0

    def kill(self):
        self._stop = True
        proc = self._proc
        if proc is None:
            return
        pid = self.pid()
        if pid > 0 and sys.platform == "win32":
            # Reap the whole tree (cmd.exe -> node/claude/...), matching the
            # legacy session's taskkill /T /F semantics so nothing orphans.
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,  # no popup console
                    timeout=10,
                )
                return
            except Exception:
                pass
        try:
            proc.terminate(force=True) if sys.platform == "win32" else proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════
# Terminal emulator widget
# ══════════════════════════════════════════════════════════════════════
class PtyTerminalView(QWidget):
    """Paints a ``pyte`` screen grid and translates input to escape sequences.

    Exposes ``append_process_output`` / ``toPlainText`` / ``apply_theme`` so the
    rest of the app (agent display injection, ``read_terminal``, theming) drives
    it identically to the legacy ``TerminalSurface``.
    """

    key_input = pyqtSignal(str)
    resize_requested = pyqtSignal(int, int)
    # (command, cwd_hint) captured from the prompt line when the user hits Enter.
    command_submitted = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        # Accept drag-and-drop of files (e.g. drop an image onto the terminal
        # and its path is typed at the cursor — what Claude Code's "drag an
        # image in" hint relies on). A plain QWidget rejects drops until this
        # flag is set, so the OS never even offered the drop to us before.
        self.setAcceptDrops(True)

        self._font = QFont("Consolas", 10)
        self._fm = QFontMetricsF(self._font)
        self._cw = max(1.0, self._fm.horizontalAdvance("M"))
        self._ch = max(1.0, self._fm.height())

        self._cols, self._rows = 80, 24
        self._screen = pyte.HistoryScreen(self._cols, self._rows, history=5000, ratio=0.5)
        self._stream = pyte.Stream(self._screen)

        # Selection in ABSOLUTE scrollback coords: (hist_top + row, col),
        # inclusive in reading order. Absolute rows stay glued to their text
        # while the view pages through history (wheel) or new output scrolls
        # lines into history.top, so a drag can wheel past one screenful and
        # the selection survives for copy.
        self._sel_anchor: tuple[int, int] | None = None
        self._sel_head: tuple[int, int] | None = None

        # Coalesced repaint.
        self._paint_pending = False

        # Bounded output draining. A heavy producer (e.g. Claude Code dumping a
        # big diff) can emit megabytes of ANSI; feeding all of it to pyte inline
        # froze the UI for tens of seconds. Instead we BUFFER incoming output and
        # parse it in capped slices, yielding to the event loop between slices so
        # the UI stays responsive while the terminal catches up.
        self._feed_pending: list[str] = []
        self._MAX_FEED_PER_TICK = 65536  # chars of pyte parsing per drain pass
        self._feed_timer = QTimer(self)
        self._feed_timer.setSingleShot(True)
        self._feed_timer.timeout.connect(self._drain_feed)

        # Render cache: _screen_has_color() and the regex token-classifier in
        # _themed_row_colors() are expensive (full-grid scan + 24 regex passes).
        # _color_cache_dirty forces a FULL rebuild (resize/theme/scroll). Normal
        # output only touches a few lines, so feed() records exactly which rows
        # changed (via pyte's dirty set) and we reclassify + repaint just those —
        # that's what keeps per-keystroke cost flat instead of O(whole grid).
        self._color_cache_dirty = True
        self._cached_plain = True
        self._cached_themed: dict[int, dict[int, str]] = {}
        self._dirty_content_rows: set[int] = set()
        self._last_cursor = (0, 0)

        # Cursor blink.
        self._cursor_on = True
        self._blink = QTimer(self)
        self._blink.setInterval(530)
        self._blink.timeout.connect(self._toggle_cursor)
        self._blink.start()

    # ── Theming ─────────────────────────────────────────────────────
    def apply_theme(self):
        self._color_cache_dirty = True  # themed colors bake in PALETTE values
        self.update()

    # ── Feed / output ───────────────────────────────────────────────
    def feed(self, data: str):
        """Queue raw terminal output. Parsing happens in _drain_feed (capped per
        tick) so a large burst can't monopolize the GUI thread."""
        if not data:
            return
        self._feed_pending.append(data)
        if not self._feed_timer.isActive():
            # start(0): drain on the next event-loop pass, never inline with the
            # signal — so other queued events (input, paint, watchdog) interleave.
            self._feed_timer.start(0)

    def _drain_feed(self):
        if not self._feed_pending:
            return
        blob = "".join(self._feed_pending)
        self._feed_pending.clear()
        cap = self._MAX_FEED_PER_TICK
        if len(blob) > cap:
            # Defer the overflow to the next pass. pyte.Stream keeps parser state
            # across feed() calls, so splitting mid-escape-sequence is safe.
            self._feed_pending.append(blob[cap:])
            blob = blob[:cap]
        try:
            self._stream.feed(blob)
        except Exception:
            pass
        # Record only the rows pyte marked changed, then clear pyte's set so the
        # next feed reports just its own delta (the set accumulates otherwise,
        # which would re-dirty the whole grid and defeat the optimization).
        try:
            dirty_rows = set(self._screen.dirty)
            self._screen.dirty.clear()
            self._dirty_content_rows |= dirty_rows
            # Scrolling (many blank lines / prompts) moves content between rows but
            # pyte often marks only a subset dirty — partial repaints then leave
            # stale pixels (WA_OpaquePaintEvent) and the feed looks blank.
            if (
                len(dirty_rows) > 1
                or "\n" in blob
                or "\r" in blob
                or len(dirty_rows) >= max(3, self._rows // 4)
            ):
                self._color_cache_dirty = True
        except Exception:
            self._color_cache_dirty = True  # fall back to a full rebuild
        self._schedule_paint()
        if self._feed_pending and not self._feed_timer.isActive():
            self._feed_timer.start(0)  # more buffered — continue after a yield

    def append_process_output(self, text: str):
        """Agent display injection: render *text* as terminal output.

        Bare ``\\n`` would only line-feed (no carriage return) in raw VT, so
        normalize to CRLF first.
        """
        if not text:
            return
        self.feed(text.replace("\r\n", "\n").replace("\n", "\r\n"))

    def _schedule_paint(self):
        if self._paint_pending:
            return
        self._paint_pending = True
        QTimer.singleShot(16, self._flush_paint)

    def _flush_paint(self):
        self._paint_pending = False
        # New output snaps the view back to the live page.
        self._cursor_on = True

        # A full rebuild pending (resize/theme/scroll) → repaint everything.
        if self._color_cache_dirty:
            self.update()
            return

        rows = set(self._dirty_content_rows)
        # The cursor isn't "content", so a bare move (arrow keys) won't mark a
        # row dirty — repaint the old and new cursor rows so the block follows.
        try:
            cx, cy = self._screen.cursor.x, self._screen.cursor.y
        except Exception:
            cx = cy = 0
        lx, ly = self._last_cursor
        self._last_cursor = (cx, cy)
        rows.add(cy)
        rows.add(ly)

        if not rows or len(rows) >= self._rows:
            self.update()
            return
        for y in rows:
            if 0 <= y < self._rows:
                self.update(QRect(0, int(y * self._ch),
                                  self.width(), int(self._ch) + 1))

    def _toggle_cursor(self):
        self._cursor_on = not self._cursor_on
        # Repaint ONLY the cursor cell, not the whole grid — the blink fires
        # every 530ms and a full repaint here was a big idle cost.
        try:
            cx, cy = self._screen.cursor.x, self._screen.cursor.y
        except Exception:
            self.update()
            return
        self.update(QRect(int(cx * self._cw), int(cy * self._ch),
                          int(self._cw) + 1, int(self._ch) + 1))

    # ── Size <-> grid ───────────────────────────────────────────────

    # Reflow at most this many scrollback lines on a width change. Reflowing
    # is O(cells); the full 5000-line history would make interactive splitter
    # drags stutter, and scrollback that far up rarely matters visually.
    _REFLOW_HISTORY_CAP = 600

    def _reflow(self, old_cols: int, new_cols: int, new_rows: int) -> None:
        """Rewrap screen + recent history to a new width.

        pyte has no soft-wrap tracking: its resize() just pads or clips each
        row, so after widening the widget all existing text stays wrapped at
        the old width. We approximate reflow the way emulators without wrap
        flags do: a row whose content runs exactly to the old right edge is
        treated as a soft-wrap continuation and joined with the next row;
        the joined logical lines are then re-cut at the new width. A hard
        line that happens to be exactly full-width gets joined too — rare,
        and far less annoying than nothing ever rewrapping.
        """
        from pyte.screens import StaticDefaultDict
        scr = self._screen

        def cells(line) -> list:
            """The occupied prefix of a row (holes filled with defaults)."""
            if not line:
                return []
            ext = -1
            for x, ch in line.items():
                if x >= old_cols:
                    continue
                # Styled blanks (bg fills, reverse video) count as content so
                # colored bars survive; plain default spaces don't.
                if ch.data != " " or getattr(ch, "bg", "default") != "default" \
                        or getattr(ch, "reverse", False):
                    if x > ext:
                        ext = x
            if ext < 0:
                return []
            return [line[x] for x in range(ext + 1)]

        top = list(scr.history.top)
        keep_top = top[:-self._REFLOW_HISTORY_CAP] \
            if len(top) > self._REFLOW_HISTORY_CAP else []
        flow_top = top[len(keep_top):]
        visible = [scr.buffer.get(y) for y in range(scr.lines)]
        # history.bottom is only populated while paged up; flatten it back in.
        phys = flow_top + visible + list(scr.history.bottom)

        logical: list[list] = []
        cur: list = []
        joined_prev = False
        for line in phys:
            cs = cells(line)
            cur.extend(cs)
            if len(cs) == old_cols:
                joined_prev = True
                continue  # ran to the right edge → soft-wrap continuation
            logical.append(cur)
            cur = []
            joined_prev = False
        if cur or joined_prev:
            logical.append(cur)

        # Keep the prompt at the same distance from the bottom: remember how
        # many blank rows trailed the visible screen, strip the blank tail,
        # and re-append that many after rewrapping.
        trailing_blanks = 0
        for line in reversed(visible):
            if cells(line):
                break
            trailing_blanks += 1
        while logical and not logical[-1]:
            logical.pop()

        new_phys: list = []
        for lg in logical:
            if not lg:
                new_phys.append(None)
                continue
            for i in range(0, len(lg), new_cols):
                new_phys.append(lg[i:i + new_cols])
        new_phys.extend([None] * trailing_blanks)

        overflow = max(0, len(new_phys) - new_rows)
        hist_rows, screen_rows = new_phys[:overflow], new_phys[overflow:]

        def to_line(seq) -> StaticDefaultDict:
            d = StaticDefaultDict(scr.default_char)
            for x, ch in enumerate(seq or ()):
                d[x] = ch
            return d

        # Commit (plain assignments — nothing here can half-apply).
        scr.buffer.clear()
        for y, seq in enumerate(screen_rows):
            if seq:
                scr.buffer[y] = to_line(seq)
        scr.history.top.clear()
        scr.history.top.extend(keep_top + [to_line(s) for s in hist_rows])
        scr.history.bottom.clear()
        try:
            scr.history = scr.history._replace(position=scr.history.size)
        except Exception:
            pass
        scr.lines, scr.columns = new_rows, new_cols
        scr.margins = None
        scr.tabstops = set(range(8, new_cols, 8))
        # Park the cursor on the last content row; the shell repaints its
        # prompt there once the PTY-side resize lands.
        content_rows = len(screen_rows) - trailing_blanks
        scr.cursor.y = max(0, min(new_rows - 1, max(content_rows - 1, 0)))
        scr.cursor.x = min(scr.cursor.x, max(0, new_cols - 1))
        scr.dirty.update(range(new_rows))

    def resizeEvent(self, ev):
        cols = max(1, int(self.width() / self._cw))
        rows = max(1, int(self.height() / self._ch))
        if cols == self._cols and rows == self._rows:
            return
        old_cols = self._cols
        self._cols, self._rows = cols, rows
        try:
            if cols != old_cols:
                self._reflow(old_cols, cols, rows)
            else:
                self._screen.resize(rows, cols)
        except Exception:
            # Reflow is best-effort cosmetics — never let it take the
            # terminal down; fall back to pyte's pad/clip resize.
            try:
                self._screen.resize(rows, cols)
            except Exception:
                pass
        self._color_cache_dirty = True
        self.resize_requested.emit(rows, cols)
        self.update()

    def showEvent(self, ev):
        super().showEvent(ev)
        # When re-shown after being hidden (QStackedWidget page switch, conversation
        # switch, Cols/Grid reparent, minimize/restore), Qt can drop our backing
        # store without delivering a full expose. The next blink/output repaint only
        # touches a few rows (WA_OpaquePaintEvent = no auto-erase), so the rest of the
        # screen stays blank — the "text disappeared" glitch. The pyte buffer is
        # intact; force a full rebuild + repaint so it comes right back.
        self._color_cache_dirty = True
        self._dirty_content_rows.clear()
        self.update()

    # ── Reading scrollback (agent / read_terminal) ──────────────────
    def toPlainText(self) -> str:
        # Parse any buffered-but-undrained output so the reader sees the latest
        # screen (an explicit read wants complete output, not a stale frame).
        if self._feed_pending:
            try:
                self._stream.feed("".join(self._feed_pending))
                self._feed_pending.clear()
            except Exception:
                pass
        lines: list[str] = []
        try:
            for row in self._screen.history.top:
                lines.append("".join(row[x].data for x in sorted(row)).rstrip())
        except Exception:
            pass
        try:
            lines.extend(line.rstrip() for line in self._screen.display)
        except Exception:
            pass
        # Drop trailing blank lines so callers see real content.
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    # ── Color helpers ───────────────────────────────────────────────
    @staticmethod
    def _resolve_color(name: str, default_hex: str) -> QColor:
        if not name or name == "default":
            return QColor(default_hex)
        if name in _ANSI:
            return QColor(_ANSI[name])
        # pyte stores 256-color / truecolor as a 6-char hex string.
        if len(name) == 6:
            try:
                return QColor("#" + name)
            except Exception:
                pass
        return QColor(default_hex)

    def _screen_has_color(self) -> bool:
        """True if any visible cell uses a non-default color/reverse — i.e. a
        real colored program is drawing. When False we apply the app's themed
        syntax coloring to plain output (dir/ls), matching the legacy look."""
        buf = self._screen.buffer
        for y in range(self._rows):
            row = buf[y]
            for x in range(self._cols):
                c = row[x]
                if c.fg != "default" or c.bg != "default" or c.reverse:
                    return True
        return False

    def _classify_row(self, y: int) -> dict[int, str]:
        """Per-column override color hex for one plain-output row, via the
        terminal token classifier. The hot path — kept to a single row."""
        try:
            from ui.terminal_workspace import _term_classify_runs, _TERM_ROLE_TO_PALETTE
        except Exception:
            return {}
        line = "".join(self._screen.buffer[y][x].data for x in range(self._cols))
        if not line.strip():
            return {}
        col = 0
        row_map: dict[int, str] = {}
        for run_text, role in _term_classify_runs(line.rstrip()):
            key = _TERM_ROLE_TO_PALETTE.get(role, "text")
            color = PALETTE.get(key, PALETTE["text"])
            for _ in run_text:
                row_map[col] = color
                col += 1
        return row_map

    def _themed_row_colors(self) -> dict[int, dict[int, str]]:
        """Full-grid classification (every row). Used only on a full rebuild."""
        return {y: self._classify_row(y) for y in range(self._rows)}

    # ── Painting ────────────────────────────────────────────────────
    def _ensure_color_cache(self):
        """Refresh the plain/themed classification. Full rebuild on
        _color_cache_dirty (resize/theme/scroll); otherwise reclassify ONLY the
        rows that changed this frame. A cursor-blink/selection repaint touches
        neither flag, so it costs nothing here."""
        if self._color_cache_dirty:
            try:
                self._cached_plain = not self._screen_has_color()
                self._cached_themed = (
                    self._themed_row_colors() if self._cached_plain else {})
            except Exception:
                self._cached_plain, self._cached_themed = True, {}
            self._color_cache_dirty = False
            self._dirty_content_rows.clear()
            return

        if not self._dirty_content_rows:
            return
        try:
            plain = not self._screen_has_color()
            if plain != self._cached_plain:
                # Mode flip (plain<->colored program) — rebuild everything.
                self._cached_plain = plain
                self._cached_themed = self._themed_row_colors() if plain else {}
            elif plain:
                for y in self._dirty_content_rows:
                    if 0 <= y < self._rows:
                        self._cached_themed[y] = self._classify_row(y)
        except Exception:
            pass
        self._dirty_content_rows.clear()

    def paintEvent(self, ev):
        p = QPainter(self)
        bg_default = QColor(PALETTE.get("panel_alt", "#101010"))
        fg_default = QColor(PALETTE.get("text", "#dddddd"))
        # Only clear the damaged region — keeps the cursor-blink repaint cheap.
        p.fillRect(ev.rect(), bg_default)
        p.setFont(self._font)

        try:
            buf = self._screen.buffer
        except Exception:
            return

        self._ensure_color_cache()
        plain = self._cached_plain
        themed = self._cached_themed

        ascent = self._fm.ascent()
        cur_x, cur_y = self._screen.cursor.x, self._screen.cursor.y
        cursor_hidden = bool(getattr(self._screen.cursor, "hidden", False))

        sel = self._normalized_selection()
        hist_top = self._hist_top()  # selection rows are scrollback-absolute

        # Restrict the cell loop to the rows/cols intersecting the damage rect.
        r = ev.rect()
        y_start = max(0, int(r.top() / self._ch))
        y_end = min(self._rows - 1, int(r.bottom() / self._ch))
        x_start = max(0, int(r.left() / self._cw))
        x_end = min(self._cols - 1, int(r.right() / self._cw))

        for y in range(y_start, y_end + 1):
            row = buf[y]
            ytop = y * self._ch
            for x in range(x_start, x_end + 1):
                c = row[x]
                ch = c.data or " "
                xleft = x * self._cw
                cell = QRect(int(xleft), int(ytop), int(self._cw) + 1, int(self._ch) + 1)

                fg = self._resolve_color(c.fg, PALETTE.get("text", "#dddddd"))
                bg = self._resolve_color(c.bg, "") if c.bg != "default" else None

                if c.reverse:
                    fg, bg = (bg or bg_default), fg
                if plain and y in themed and x in themed[y] and c.fg == "default":
                    fg = QColor(themed[y][x])

                selected = sel is not None and self._cell_in_selection(hist_top + y, x, sel)
                if selected:
                    bg = QColor(PALETTE.get("border", "#444444"))
                    fg = QColor(PALETTE.get("text", "#ffffff"))

                if bg is not None:
                    p.fillRect(cell, bg)

                # Draw cursor as a filled block (when focused, live, visible).
                is_cursor = (
                    x == cur_x and y == cur_y
                    and not cursor_hidden
                    and self.hasFocus()
                    and self._cursor_on
                )
                if is_cursor:
                    p.fillRect(cell, QColor(PALETTE.get("accent", "#33ff99")))
                    p.setPen(bg_default)
                else:
                    p.setPen(fg if fg != bg_default else fg_default)

                if c.bold or c.underscore:
                    f = QFont(self._font)
                    f.setBold(bool(c.bold))
                    f.setUnderline(bool(c.underscore))
                    p.setFont(f)
                else:
                    p.setFont(self._font)

                if ch != " ":
                    p.drawText(int(xleft), int(ytop + ascent), ch)

        p.end()

    # ── Selection ───────────────────────────────────────────────────
    def _hist_top(self) -> int:
        """Lines of scrollback above the visible grid — the absolute row of
        visible row 0. Stable across prev_page/next_page AND as new output
        pushes lines into history (both just move lines between the deques
        and the grid, so absolute row == text identity)."""
        try:
            return len(self._screen.history.top)
        except Exception:
            return 0

    def _cell_at(self, pos) -> tuple[int, int]:
        col = max(0, min(self._cols - 1, int(pos.x() / self._cw)))
        row = max(0, min(self._rows - 1, int(pos.y() / self._ch)))
        return row, col

    def _abs_cell_at(self, pos) -> tuple[int, int]:
        """Cell under `pos` in absolute scrollback coordinates."""
        row, col = self._cell_at(pos)
        return self._hist_top() + row, col

    def _row_cells(self, abs_row: int):
        """Char cells for an absolute row — from history.top, the visible
        grid, or history.bottom — or None when out of range."""
        top = self._hist_top()
        if abs_row < 0:
            return None
        if abs_row < top:
            return self._screen.history.top[abs_row]
        if abs_row < top + self._rows:
            return self._screen.buffer[abs_row - top]
        i = abs_row - top - self._rows
        bottom = self._screen.history.bottom
        return bottom[i] if i < len(bottom) else None

    def _normalized_selection(self):
        if self._sel_anchor is None or self._sel_head is None:
            return None
        a, b = self._sel_anchor, self._sel_head
        if (a[0], a[1]) <= (b[0], b[1]):
            return a, b
        return b, a

    @staticmethod
    def _cell_in_selection(y: int, x: int, sel) -> bool:
        (r0, c0), (r1, c1) = sel
        if y < r0 or y > r1:
            return False
        if r0 == r1:
            return c0 <= x <= c1
        if y == r0:
            return x >= c0
        if y == r1:
            return x <= c1
        return True

    def _selected_text(self) -> str:
        sel = self._normalized_selection()
        if sel is None:
            return ""
        (r0, c0), (r1, c1) = sel
        out: list[str] = []
        for y in range(r0, r1 + 1):
            try:
                row = self._row_cells(y)
            except Exception:
                row = None
            if row is None:
                continue
            xs = c0 if y == r0 else 0
            xe = c1 if y == r1 else self._cols - 1
            # `x in row` guard: history lines are plain dicts keyed by column,
            # so direct indexing of an untouched cell would raise (and indexing
            # the live buffer's defaultdict would mutate it).
            line = "".join(
                (row[x].data or " ") if x in row else " "
                for x in range(xs, xe + 1)
            )
            out.append(line.rstrip())
        return "\n".join(out)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            # Record where a drag would start, but DON'T create a selection yet —
            # head stays None until the mouse actually moves. A plain click that
            # set head == anchor used to paint a single highlighted cell that
            # lingered like a stray cursor block (the "phantom").
            self._sel_anchor = self._abs_cell_at(ev.position())
            self._sel_head = None
            self.update()
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.MouseButton.LeftButton and self._sel_anchor is not None:
            self._sel_head = self._abs_cell_at(ev.position())
            self.update()
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        # Press+release with no drag (head never set) means a plain click — drop
        # the anchor so nothing stays highlighted. A real drag leaves head set,
        # so the selection survives for copy.
        if ev.button() == Qt.MouseButton.LeftButton and self._sel_head is None:
            self._sel_anchor = None
            self.update()
        super().mouseReleaseEvent(ev)

    def _clear_selection(self):
        if self._sel_anchor is not None:
            self._sel_anchor = self._sel_head = None
            self.update()

    def _select_all(self):
        """Visually select the whole screen grid (Copy-all uses the full
        scrollback via toPlainText, so this is just the on-screen cue)."""
        top = self._hist_top()
        self._sel_anchor = (top, 0)
        self._sel_head = (top + self._rows - 1, self._cols - 1)
        self.update()

    # ── Right-click menu ─────────────────────────────────────────────
    def contextMenuEvent(self, ev):
        from ui.theme import PALETTE
        p = PALETTE
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background:{p['panel']}; color:{p['text']}; "
            f"border:1px solid {p['border']}; font-family:Consolas; font-size:9pt; }}"
            f"QMenu::item:selected {{ background:{p['accent_muted']}; color:{p['glow_hot']}; }}"
            f"QMenu::item:disabled {{ color:{p['muted_text']}; }}")
        has_sel = self._normalized_selection() is not None
        clip = QApplication.clipboard()
        a_copy = menu.addAction("Copy")
        a_copy.setEnabled(has_sel)
        a_copy_all = menu.addAction("Copy all")          # grabs the whole buffer
        a_sel_all = menu.addAction("Select all")
        menu.addSeparator()
        a_paste = menu.addAction("Paste")
        a_paste.setEnabled(bool(clip.text()))
        menu.addSeparator()
        a_clear = menu.addAction("Clear")
        chosen = menu.exec(ev.globalPos())
        if chosen is None:
            return
        if chosen == a_copy and has_sel:
            clip.setText(self._selected_text())
            self._clear_selection()
        elif chosen == a_copy_all:
            clip.setText(self.toPlainText())
        elif chosen == a_sel_all:
            self._select_all()
        elif chosen == a_paste:
            self._paste_clipboard()
        elif chosen == a_clear:
            try:
                self._screen.reset()
                self._color_cache_dirty = True
                self.update()
            except Exception:
                pass

    # ── Input translation ───────────────────────────────────────────
    def _private_mode(self, mode: int) -> bool:
        """True when a DEC private mode is set. pyte stores private modes
        SHIFTED (``mode << 5`` — see pyte.Screen.set_mode), so the raw number
        never appears in ``screen.mode``; check both forms to be safe across
        pyte versions."""
        try:
            m = self._screen.mode
            return (mode << 5) in m or mode in m
        except Exception:
            return False

    def _app_cursor(self) -> bool:
        # DECCKM (private mode 1): application cursor keys → send ESC O x.
        # (The old check `1 in mode` never matched — pyte stores 1 << 5.)
        return self._private_mode(1)

    def _paste_clipboard(self):
        """Paste clipboard text into the terminal — the ONE paste path.

        Line endings become CR (what terminals send for Enter). When the
        foreground app enabled BRACKETED PASTE (DECSET 2004 — Claude Code and
        most modern TUIs do), the payload is wrapped in ESC[200~ … ESC[201~ so
        the app receives it as a single paste block. Without the wrapper a
        multi-line paste was delivered as line+Enter, line+Enter… which a TUI
        treats as submit-per-line — the "paste does nothing / fires my prompt
        line by line" bug."""
        text = QApplication.clipboard().text()
        if not text:
            return
        payload = text.replace("\r\n", "\r").replace("\n", "\r")
        if self._private_mode(2004):
            payload = "\x1b[200~" + payload + "\x1b[201~"
        self.key_input.emit(payload)

    # ── Drag & drop ──────────────────────────────────────────────────────
    # Dropping a file (image, log, whatever) onto the terminal types its path
    # at the cursor. This is what makes Claude Code's "drag an image in" work:
    # the CLI reads the path the shell hands it. We never *send* file bytes —
    # only the path string, the same as every native terminal.
    def _drop_has_paths(self, md) -> bool:
        return bool(md) and (md.hasUrls() or md.hasText())

    def dragEnterEvent(self, ev: QDragEnterEvent):
        if self._drop_has_paths(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dragMoveEvent(self, ev: QDragMoveEvent):
        if self._drop_has_paths(ev.mimeData()):
            ev.acceptProposedAction()
        else:
            ev.ignore()

    def dropEvent(self, ev: QDropEvent):
        md = ev.mimeData()
        paths: list[str] = []
        if md.hasUrls():
            for url in md.urls():
                local = url.toLocalFile()
                paths.append(local if local else url.toString())
        elif md.hasText():
            paths.append(md.text())
        paths = [p for p in (p.strip() for p in paths) if p]
        if not paths:
            ev.ignore()
            return
        # Quote each path so spaces survive at the shell. A path that already
        # contains a double-quote falls back to single-quoting; otherwise wrap
        # in double quotes (works in cmd, PowerShell, and POSIX shells).
        def _quote(p: str) -> str:
            if " " not in p and "\t" not in p:
                return p
            if '"' not in p:
                return f'"{p}"'
            return f"'{p}'"
        payload = " ".join(_quote(p) for p in paths)
        if len(paths) == 1:
            payload += " "  # trailing space so the next arg/flag is separated
        if self._private_mode(2004):
            payload = "\x1b[200~" + payload + "\x1b[201~"
        self.setFocus()
        self.key_input.emit(payload)
        ev.acceptProposedAction()

    def keyPressEvent(self, ev: QKeyEvent):
        key = ev.key()
        mods = ev.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)

        # Clipboard: Ctrl+C copies a selection (else interrupt); Ctrl+V / Shift+Ins paste.
        if ctrl and key == Qt.Key.Key_C and self._normalized_selection() is not None:
            QApplication.clipboard().setText(self._selected_text())
            self._clear_selection()
            return
        if (ctrl and key == Qt.Key.Key_V) or (shift and key == Qt.Key.Key_Insert):
            self._paste_clipboard()
            return
        if ctrl and shift and key == Qt.Key.Key_C and self._normalized_selection() is not None:
            QApplication.clipboard().setText(self._selected_text())
            self._clear_selection()
            return

        self._clear_selection()

        # On Enter, snapshot the command sitting at the shell prompt so the app
        # can restore it (prefilled) after a restart. Reads what's actually on
        # screen, so history-recall / line editing are captured faithfully.
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._capture_submitted_command()

        seq = self._key_to_seq(key, ctrl, shift, ev.text())
        if seq:
            self.key_input.emit(seq)

    def focusNextPrevChild(self, _next: bool) -> bool:
        # Qt eats Tab / Shift+Tab for focus traversal before keyPressEvent ever
        # sees them. A terminal needs both keys raw (Tab = completion, Shift+Tab
        # = reverse-cycle, e.g. Claude's "shift+tab to cycle"). Returning False
        # tells Qt we handle them ourselves, so they fall through to the PTY.
        return False

    def _capture_submitted_command(self):
        try:
            line = self._screen.display[self._screen.cursor.y]
        except Exception:
            return
        cmd, cwd = self._parse_prompt_line(line)
        if cmd:  # skip bare Enter on an empty prompt and non-prompt (TUI) lines
            self.command_submitted.emit(cmd, cwd or "")

    @staticmethod
    def _parse_prompt_line(line: str) -> tuple[str, str]:
        """Split a shell prompt line into (command, cwd). Returns ('', '') when
        the line isn't a recognizable prompt (e.g. a full-screen TUI), so the
        caller leaves the previously-captured command untouched."""
        line = (line or "").rstrip()
        # cmd.exe default prompt ($P$G): "C:\some\path>command"
        gt = line.find(">")
        if gt > 0:
            head = line[:gt]
            if (":" in head or "\\" in head) and " " not in head[-1:]:
                return line[gt + 1:].strip(), head.strip()
        # POSIX-ish prompts ending in "$ " / "# " before the command.
        for sep in ("$ ", "# "):
            i = line.rfind(sep)
            if i >= 0:
                return line[i + len(sep):].strip(), ""
        return "", ""

    def _key_to_seq(self, key, ctrl, shift, text) -> str:
        app = self._app_cursor()
        ss3 = lambda c: ("\x1bO" + c) if app else ("\x1b[" + c)

        named = {
            Qt.Key.Key_Return:    "\r",
            Qt.Key.Key_Enter:     "\r",
            Qt.Key.Key_Backspace: "\x7f",
            Qt.Key.Key_Tab:       "\t",
            Qt.Key.Key_Backtab:   "\x1b[Z",
            Qt.Key.Key_Escape:    "\x1b",
            Qt.Key.Key_Up:        ss3("A"),
            Qt.Key.Key_Down:      ss3("B"),
            Qt.Key.Key_Right:     ss3("C"),
            Qt.Key.Key_Left:      ss3("D"),
            Qt.Key.Key_Home:      ss3("H"),
            Qt.Key.Key_End:       ss3("F"),
            Qt.Key.Key_PageUp:    "\x1b[5~",
            Qt.Key.Key_PageDown:  "\x1b[6~",
            Qt.Key.Key_Insert:    "\x1b[2~",
            Qt.Key.Key_Delete:    "\x1b[3~",
            Qt.Key.Key_F1:  "\x1bOP", Qt.Key.Key_F2: "\x1bOQ",
            Qt.Key.Key_F3:  "\x1bOR", Qt.Key.Key_F4: "\x1bOS",
            Qt.Key.Key_F5:  "\x1b[15~", Qt.Key.Key_F6: "\x1b[17~",
            Qt.Key.Key_F7:  "\x1b[18~", Qt.Key.Key_F8: "\x1b[19~",
            Qt.Key.Key_F9:  "\x1b[20~", Qt.Key.Key_F10: "\x1b[21~",
            Qt.Key.Key_F11: "\x1b[23~", Qt.Key.Key_F12: "\x1b[24~",
        }
        if key in named:
            return named[key]

        if ctrl:
            # Ctrl+A..Z -> 0x01..0x1a; common Ctrl+symbols.
            if Qt.Key.Key_A <= key <= Qt.Key.Key_Z:
                return chr(key - Qt.Key.Key_A + 1)
            ctrl_syms = {
                Qt.Key.Key_BracketLeft: "\x1b", Qt.Key.Key_Backslash: "\x1c",
                Qt.Key.Key_BracketRight: "\x1d", Qt.Key.Key_Space: "\x00",
            }
            if key in ctrl_syms:
                return ctrl_syms[key]

        if text and (text.isprintable() or text in ("\t",)):
            return text
        return ""

    # ── Scrollback via wheel ────────────────────────────────────────
    def wheelEvent(self, ev):
        try:
            steps = ev.angleDelta().y() // 120
            if steps > 0:
                for _ in range(steps):
                    self._screen.prev_page()
            elif steps < 0:
                for _ in range(-steps):
                    self._screen.next_page()
            # Wheeling mid-drag: the cursor is stationary but the text under it
            # paged, so extend the selection to the cell now under the cursor —
            # drag + wheel selects past one screenful instead of losing the
            # selection (coords are scrollback-absolute, so what was already
            # highlighted stays highlighted on its text).
            if (ev.buttons() & Qt.MouseButton.LeftButton
                    and self._sel_anchor is not None):
                self._sel_head = self._abs_cell_at(ev.position())
            self._color_cache_dirty = True  # scrollback shows different content
            self.update()
        except Exception:
            pass
