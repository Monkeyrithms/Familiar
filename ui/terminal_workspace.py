"""
Integrated terminal workspace — one surface per tab (block caret, type at the end),
backed by a persistent ``QProcess`` shell. Matches the same stdin stream the agent
uses via ``workspace_terminal`` / ``terminal(..., to_workspace=true)``.
"""

from __future__ import annotations

import os
import sys
import shutil
import signal
import subprocess
import traceback
import ctypes
from collections.abc import Callable

from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QPushButton,
    QFrame,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QLabel,
    QApplication,
    QStackedWidget,
    QMenu,
)
import re

from PyQt6.QtCore import Qt, QProcess, QTimer
from PyQt6.QtGui import (
    QFont, QFontMetrics, QTextCursor, QColor, QKeyEvent, QTextOption,
    QKeySequence, QTextCharFormat,
)

from ui.theme import PALETTE
from ui.themed_tab_widget import ThemedClosableTabWidget
from ui.pty_terminal import PTY_AVAILABLE, PtyBackend, PtyTerminalView


# ──────────────────────────────────────────────────────────────────────
# Terminal output highlighter
# ──────────────────────────────────────────────────────────────────────
#
# The workspace terminal previously inserted raw plain text. This module
# splits each line into (text, role) runs and colors them via QTextCharFormat
# at insertion time — works in QPlainTextEdit, which doesn't accept HTML.
#
# Roles map to PALETTE keys at draw time so colors track the active theme.
# Pattern set is intentionally pragmatic, not full ANSI: we don't see real
# escape codes from cmd.exe most of the time, so we infer structure from
# common shapes in tool/script output.

_TERM_ROLE_TO_PALETTE = {
    "default":   "text",
    "error":     "danger",
    "warn":      "accent_muted",
    "muted":     "muted_text",
    "dim":       "border",
    "number":    "accent_bright",
    "string":    "muted_text",
    "bracket":   "accent_muted",
    "paren":     "border",
    "operator":  "accent",
    "timestamp": "accent",
    "url":       "accent_bright",
    "path":      "glow_hot",
    "good":      "accent_bright",
    "bad":       "danger",
    "caps":      "accent",
    "prompt":    "glow_hot",
}

# One regex, alternation order = priority. First match wins per token.
_TERM_TOKEN_RE = re.compile(
    r"""
      (?P<ts>\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b
            |\b\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b)
    | (?P<url>https?://\S+)
    | (?P<winpath>[A-Za-z]:[\\/][^\s"'<>|]*)
    | (?P<unixpath>(?:\.{1,2}/|/)[\w./\-+]+)
    | (?P<hex>0x[0-9a-fA-F]+|\#[0-9a-fA-F]{3,8}\b)
    | (?P<num>\$?[\d,]+\.?\d*%?)
    | (?P<dqstr>"[^"\n]*")
    | (?P<sqstr>'[^'\n]*')
    | (?P<bool>\b(?:True|False|None|null|true|false)\b)
    | (?P<good>\b(?:OK|PASS|SUCCESS|UP|HEALTHY|RUNNING|DONE|READY)\b)
    | (?P<bad>\b(?:FAIL|FAILED|DOWN|UNHEALTHY|STOPPED|CRITICAL|DENIED|REFUSED)\b)
    | (?P<warn>\b(?:WARN|WARNING|DEPRECATED|RETRY|TIMEOUT)\b)
    | (?P<info>\b(?:INFO|DEBUG|TRACE|VERBOSE|NOTICE)\b)
    | (?P<caps>\b[A-Z][A-Z0-9_]{2,}\b)
    | (?P<bracket>[\[\]\{\}])
    | (?P<paren>[()])
    | (?P<op>->|=>|::|\|\||&&|>>|<<|[|:;=<>])
    """,
    re.VERBOSE,
)

# Whole-line predicates (run before token scan).
_TERM_PROMPT_RE = re.compile(r"^\s*(?:PS\s+[A-Za-z]:\\[^>]*>|\$|>\s*$|[A-Za-z]:\\[^>]*>)")
_TERM_SEP_RE = re.compile(r"^\s*[=\-_*#~]{4,}\s*$")
_TERM_ERROR_HINT = re.compile(r"\b(error|traceback|exception|fatal|panic)\b", re.IGNORECASE)
_TERM_NEG_HINT = re.compile(r"\bno\s+(?:error|errors|exception)|0\s+errors?\b", re.IGNORECASE)


def _term_classify_runs(line: str) -> list[tuple[str, str]]:
    """Split a single line of terminal output into (text, role) runs."""
    if not line:
        return []

    # Whole-line: errors → all error color (unless line says "no errors" etc.)
    if _TERM_ERROR_HINT.search(line) and not _TERM_NEG_HINT.search(line):
        return [(line, "error")]

    # Whole-line: ASCII separator
    if _TERM_SEP_RE.match(line):
        return [(line, "dim")]

    # Whole-line: shell prompt prefix gets dimmed
    prompt_m = _TERM_PROMPT_RE.match(line)
    runs: list[tuple[str, str]] = []
    scan_start = 0
    if prompt_m:
        runs.append((line[: prompt_m.end()], "prompt"))
        scan_start = prompt_m.end()

    last = scan_start
    for m in _TERM_TOKEN_RE.finditer(line, scan_start):
        if m.start() > last:
            runs.append((line[last:m.start()], "default"))
        kind = m.lastgroup
        role = {
            "ts":       "timestamp",
            "url":      "url",
            "winpath":  "path",
            "unixpath": "path",
            "hex":      "number",
            "num":      "number",
            "dqstr":    "string",
            "sqstr":    "string",
            "bool":     "operator",
            "good":     "good",
            "bad":      "bad",
            "warn":     "warn",
            "info":     "muted",
            "caps":     "caps",
            "bracket":  "bracket",
            "paren":    "paren",
            "op":       "operator",
        }.get(kind, "default")
        runs.append((m.group(), role))
        last = m.end()
    if last < len(line):
        runs.append((line[last:], "default"))
    return runs


def _term_format_for_role(role: str, palette: dict) -> QTextCharFormat:
    fmt = QTextCharFormat()
    color_key = _TERM_ROLE_TO_PALETTE.get(role, "text")
    fmt.setForeground(QColor(palette.get(color_key, palette["text"])))
    if role == "url":
        fmt.setFontUnderline(True)
    return fmt


def _terminal_surface_stylesheet(p: dict) -> str:
    # Note: Qt Style Sheets do not support CSS `caret-color` on QPlainTextEdit (Qt logs
    # "Unknown property caret-color"). Caret uses the widget palette / fusion defaults.
    return f"""
        QPlainTextEdit {{
            background: {p['panel_alt']};
            color: {p['text']};
            border: none;
            font-family: Consolas, monospace;
            font-size: 10pt;
            selection-background-color: {p['border']};
            selection-color: {p['text']};
        }}
    """


class TerminalSurface(QPlainTextEdit):
    """Scrollback + editable tail; only the tail after ``_anchor`` is user-editable."""

    _MAX_CMD_HISTORY = 500

    def __init__(self, parent=None):
        super().__init__(parent)
        self._anchor = 0
        self._on_submit: Callable[[str], None] | None = None
        self._on_interrupt: Callable[[], None] | None = None
        self._cmd_history: list[str] = []
        self._hist_pos: int | None = None
        self._hist_stash: str = ""
        self.setReadOnly(False)
        self.setWordWrapMode(QTextOption.WrapMode.NoWrap)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setFont(QFont("Consolas", 10))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setTabChangesFocus(False)
        # Cap buffer at 10k lines — prevents permanent lag from massive output
        self.setMaximumBlockCount(10_000)
        fm = QFontMetrics(self.font())
        self.setCursorWidth(max(8, fm.horizontalAdvance("X")))
        self.textCursor().setVisualNavigation(True)
        # Ensure caret blinking is enabled (cursor flash lives on QApplication, not QGuiApplication).
        try:
            app = QApplication.instance()
            if app is not None and app.cursorFlashTime() <= 0:
                app.setCursorFlashTime(530)
        except Exception:
            pass

    def set_submit_handler(self, fn: Callable[[str], None] | None):
        self._on_submit = fn

    def set_interrupt_handler(self, fn: Callable[[], None] | None):
        self._on_interrupt = fn

    def set_anchor_position(self, pos: int):
        doc = self.document()
        mx = doc.characterCount() - 1
        self._anchor = max(0, min(pos, mx))

    def anchor_position(self) -> int:
        return self._anchor

    def append_process_output(self, text: str):
        if not text:
            return
        self.moveCursor(QTextCursor.MoveOperation.End)
        cursor = self.textCursor()
        palette = PALETTE
        default_fmt = _term_format_for_role("default", palette)

        # Insert line by line so each line gets per-token coloring. Preserve
        # newlines verbatim — `text` may start or end mid-line.
        parts = text.split("\n")
        for i, line in enumerate(parts):
            if line:
                for run_text, role in _term_classify_runs(line):
                    cursor.setCharFormat(_term_format_for_role(role, palette))
                    cursor.insertText(run_text)
            if i < len(parts) - 1:
                cursor.setCharFormat(default_fmt)
                cursor.insertText("\n")

        # Reset to the default format so user-typed text after this isn't
        # inheriting the last run's color.
        cursor.setCharFormat(default_fmt)
        self.setTextCursor(cursor)
        self.moveCursor(QTextCursor.MoveOperation.End)
        # Output becomes immutable scrollback; user input starts at the new end.
        self._anchor = self.textCursor().position()

    def _input_slice(self) -> str:
        doc = self.document()
        c = QTextCursor(doc)
        c.setPosition(self._anchor)
        c.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        t = c.selectedText()
        return t.replace("\u2029", "\n")

    def _clear_input_region(self):
        doc = self.document()
        c = QTextCursor(doc)
        c.setPosition(self._anchor)
        c.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        c.removeSelectedText()
        self._anchor = c.position()

    def _replace_input_region(self, text: str):
        """Replace the editable tail after ``_anchor`` with *text* (no submit)."""
        self._clear_input_region()
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.insertPlainText(text)
        self.moveCursor(QTextCursor.MoveOperation.End)

    def _record_submitted_command(self, raw: str):
        line = (raw or "").rstrip("\r\n")
        if not line.strip():
            return
        if self._cmd_history and self._cmd_history[-1] == line:
            pass
        else:
            self._cmd_history.append(line)
            if len(self._cmd_history) > self._MAX_CMD_HISTORY:
                self._cmd_history = self._cmd_history[-self._MAX_CMD_HISTORY :]
        self._hist_pos = None
        self._hist_stash = ""

    def _navigate_history(self, delta: int) -> bool:
        """Return True if the key was consumed (VS Code–style command recall)."""
        if self.textCursor().hasSelection():
            return False
        if delta < 0:
            if not self._cmd_history:
                return False
            if self._hist_pos is None:
                self._hist_stash = self._input_slice()
                self._hist_pos = len(self._cmd_history) - 1
            elif self._hist_pos > 0:
                self._hist_pos -= 1
            else:
                return True
            self._replace_input_region(self._cmd_history[self._hist_pos])
            return True
        # delta > 0 — down
        if self._hist_pos is None:
            return False
        if self._hist_pos < len(self._cmd_history) - 1:
            self._hist_pos += 1
            self._replace_input_region(self._cmd_history[self._hist_pos])
        else:
            self._hist_pos = None
            self._replace_input_region(self._hist_stash)
        return True

    def _submit_buffer(self):
        raw = self._input_slice()
        if not raw.strip():
            return
        if self._on_submit is None:
            return
        self._record_submitted_command(raw)
        self._clear_input_region()
        self._on_submit(raw)

    def contextMenuEvent(self, ev):
        p = PALETTE
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background:{p['panel']}; color:{p['text']}; "
            f"border:1px solid {p['border']}; font-family:Consolas; font-size:9pt; }}"
            f"QMenu::item:selected {{ background:{p['accent_muted']}; color:{p['glow_hot']}; }}"
            f"QMenu::item:disabled {{ color:{p['muted_text']}; }}")
        has_sel = self.textCursor().hasSelection()
        a_copy = menu.addAction("Copy")
        a_copy.setEnabled(has_sel)
        a_copy_all = menu.addAction("Copy all")
        a_sel_all = menu.addAction("Select all")
        menu.addSeparator()
        a_paste = menu.addAction("Paste")
        a_paste.setEnabled(self.canPaste())
        menu.addSeparator()
        a_clear = menu.addAction("Clear")
        chosen = menu.exec(ev.globalPos())
        if chosen is None:
            return
        if chosen == a_copy and has_sel:
            self.copy()
        elif chosen == a_copy_all:
            QApplication.clipboard().setText(self.toPlainText())
        elif chosen == a_sel_all:
            self.selectAll()
        elif chosen == a_paste:
            self._ensure_cursor_not_before_anchor()
            self.paste()
        elif chosen == a_clear:
            self.clear()
            self._anchor = 0

    def keyPressEvent(self, ev: QKeyEvent):
        # Ctrl+Shift+C: copy the selection, or the whole buffer if nothing is
        # selected — a reliable keyboard copy that never collides with Ctrl+C
        # (interrupt).
        _m = ev.modifiers()
        if (_m & Qt.KeyboardModifier.ControlModifier
                and _m & Qt.KeyboardModifier.ShiftModifier
                and ev.key() == Qt.Key.Key_C):
            if self.textCursor().hasSelection():
                self.copy()
            else:
                QApplication.clipboard().setText(self.toPlainText())
            return
        # Let clipboard / selection shortcuts work in scrollback without anchor enforcement.
        if ev.matches(QKeySequence.StandardKey.Copy):
            # Mirror VS Code terminal behavior: Ctrl+C with a selection copies;
            # Ctrl+C without a selection sends an interrupt to the running command.
            if self.textCursor().hasSelection():
                super().keyPressEvent(ev)
            elif self._on_interrupt is not None:
                self._on_interrupt()
            return
        if ev.matches(QKeySequence.StandardKey.SelectAll):
            super().keyPressEvent(ev)
            return

        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(ev)
                self._ensure_cursor_not_before_anchor()
                return
            self._ensure_cursor_not_before_anchor()
            self._submit_buffer()
            return

        if ev.key() == Qt.Key.Key_Tab:
            self._ensure_cursor_not_before_anchor()
            cur = self.textCursor()
            cur.insertText("\t")
            self.setTextCursor(cur)
            return

        if ev.key() == Qt.Key.Key_Up:
            self._ensure_cursor_not_before_anchor()
            # Match typical terminal behavior: history recall when the current
            # command is a single line; multiline (Shift+Enter) keeps arrow keys
            # for in-buffer movement.
            if "\n" not in self._input_slice() and self._navigate_history(-1):
                return
        elif ev.key() == Qt.Key.Key_Down:
            self._ensure_cursor_not_before_anchor()
            if "\n" not in self._input_slice() and self._navigate_history(1):
                return

        self._ensure_cursor_not_before_anchor()
        cur = self.textCursor()
        if ev.key() == Qt.Key.Key_Backspace:
            if cur.position() <= self._anchor and not cur.hasSelection():
                return
            if cur.hasSelection():
                if cur.selectionStart() < self._anchor or cur.selectionEnd() < self._anchor:
                    cur.clearSelection()
                    cur.setPosition(self.document().characterCount() - 1)
                    self.setTextCursor(cur)
                    return
        super().keyPressEvent(ev)
        self._ensure_cursor_not_before_anchor()

    def insertFromMimeData(self, source):
        self._ensure_cursor_not_before_anchor()
        super().insertFromMimeData(source)
        self._ensure_cursor_not_before_anchor()

    def mousePressEvent(self, ev):
        """Allow click-drag selection in scrollback (before anchor) for copy; do not yank caret to end."""
        super().mousePressEvent(ev)

    def _ensure_cursor_not_before_anchor(self):
        cur = self.textCursor()
        if cur.hasSelection():
            lo = min(cur.selectionStart(), cur.selectionEnd())
            hi = max(cur.selectionStart(), cur.selectionEnd())
            # Selection wholly in scrollback — keep it so the user can copy.
            if hi < self._anchor:
                return
            # Selection wholly in the live input tail — ok.
            if lo >= self._anchor:
                return
            # Selection crosses anchor (rare): still avoid editing scrollback text.
        start = cur.selectionStart()
        end = cur.selectionEnd()
        lo, hi = min(start, end), max(start, end)
        if lo < self._anchor:
            cur.clearSelection()
            cur.movePosition(QTextCursor.MoveOperation.End)
            self.setTextCursor(cur)

    def apply_theme(self):
        self.setStyleSheet(_terminal_surface_stylesheet(PALETTE))


class IntegratedTerminalSession(QWidget):
    """One persistent shell — single typing surface + QProcess."""

    def __init__(self, cwd: str, parent=None):
        super().__init__(parent)
        self._cwd = cwd or os.getcwd()
        self._pty = PTY_AVAILABLE
        self._proc = None          # legacy QProcess (None on the PTY path)
        self._backend = None       # PtyBackend  (None on the legacy path)
        # Restart persistence: last command the user ran + cwd seen at its prompt
        # (psutil gives the live cwd; these are the fallback / command source).
        self._last_command = ""
        self._last_prompt_cwd = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        if self._pty:
            self._init_pty(lay)
        else:
            self._init_legacy(lay)

    # ── PTY backend (ConPTY / real TTY — runs claude, vim, htop, …) ───
    def _init_pty(self, lay):
        self._term = PtyTerminalView(self)
        self._term.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._term, stretch=1)
        self._term.apply_theme()

        self._backend = PtyBackend(self._cwd, rows=self._term._rows, cols=self._term._cols, parent=self)
        self._term.key_input.connect(self._backend.write)
        self._term.resize_requested.connect(self._backend.resize)
        self._term.command_submitted.connect(self._on_command_submitted)
        self._backend.data_received.connect(self._term.feed)
        self._backend.finished.connect(self._on_pty_finished)
        if not self._backend.start():
            self._append_notice("[error] Falling back: PTY shell failed to start.\n")

    # ── Legacy QProcess pipe shell (fallback when pyte/pywinpty absent) ─
    def _init_legacy(self, lay):
        self._proc = QProcess(self)
        self._proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._proc.readyReadStandardOutput.connect(self._read_output)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_proc_error)

        self._term = TerminalSurface(self)
        self._term.set_submit_handler(self._on_user_submit)
        self._term.set_interrupt_handler(self._on_user_interrupt)
        self._term.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._term, stretch=1)

        self._term.apply_theme()
        self._start_shell()

    def _on_pty_finished(self, exit_code: int):
        self._term.append_process_output(f"\r\n[process exited: code={exit_code}]\r\n")

    def is_running(self) -> bool:
        if self._pty:
            return self._backend is not None and self._backend.is_alive()
        return self._proc is not None and self._proc.state() != QProcess.ProcessState.NotRunning

    def _shell_pid(self) -> int:
        if self._pty:
            return self._backend.pid() if self._backend is not None else 0
        return int(self._proc.processId() or 0) if self._proc is not None else 0

    def _on_command_submitted(self, cmd: str, cwd_hint: str):
        """Remember the last command the user ran (for restart prefill) and the
        cwd shown at its prompt (psutil fallback when it's unavailable)."""
        if cmd:
            self._last_command = cmd
        if cwd_hint:
            self._last_prompt_cwd = cwd_hint

    def persistence_info(self, deep: bool = True) -> dict:
        """For restart persistence: the shell's cwd, the last command the user
        ran (restored prefilled), and — if a known resumable tool (e.g. claude)
        is running — the command that reopens it.

        ``deep=True`` queries the live cwd + resumable tool via psutil (accurate;
        used at shutdown). ``deep=False`` skips that process-tree walk and uses
        the cwd seen at the last prompt — cheap enough for the periodic
        crash-safety autosave that runs while the UI is interactive."""
        cwd, resume = self._cwd, None
        if deep:
            try:
                from core.terminal_persistence import detect_session
                live_cwd, resume = detect_session(self._shell_pid())
                if live_cwd:
                    cwd = live_cwd
            except Exception:
                pass
        # Fall back to the cwd seen at the prompt (also the light-save source).
        if (not deep or not cwd or not os.path.isdir(cwd)) and self._last_prompt_cwd:
            cwd = self._last_prompt_cwd
        return {"cwd": cwd, "resume": resume, "command": self._last_command}

    def prefill(self, text: str):
        """Type *text* at the prompt WITHOUT executing it — the user hits Enter.
        On the PTY path the backend queues the write until the prompt appears,
        so this is safe to call right after the shell is spawned."""
        if not text:
            return
        if self._pty:
            if self._backend is not None:
                self._backend.write(text)  # no trailing CR ⇒ not executed
            return
        # Legacy surface: drop it into the editable input tail if supported.
        try:
            self._term.append_process_output(text)
        except Exception:
            pass

    def apply_theme(self):
        self._term.apply_theme()

    def _append_notice(self, text: str):
        self._term.append_process_output(text)

    def _on_user_submit(self, text: str):
        line = text.rstrip("\r\n")
        if line.strip():
            self._last_command = line.strip()
        self.send_raw_line(line)

    def _on_user_interrupt(self):
        self.send_interrupt()

    def _windows_send_ctrl_break(self) -> bool:
        """
        Best-effort Ctrl+Break delivery to the shell process group.
        Returns True when the control event API call succeeds.
        """
        pid = int(self._proc.processId() or 0)
        if pid <= 0:
            return False
        kernel32 = ctypes.windll.kernel32
        CTRL_BREAK_EVENT = 1
        ATTACH_PARENT_PROCESS = -1

        # Detach from any current console first; ignore failures.
        try:
            kernel32.FreeConsole()
        except Exception:
            pass

        attached = bool(kernel32.AttachConsole(pid))
        if not attached:
            if not bool(kernel32.AllocConsole()):
                return False
        if not bool(kernel32.SetConsoleCtrlHandler(None, True)):
            if attached:
                kernel32.FreeConsole()
                kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
            return False
        sent = bool(kernel32.GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid))
        kernel32.SetConsoleCtrlHandler(None, False)
        kernel32.FreeConsole()
        kernel32.AttachConsole(ATTACH_PARENT_PROCESS)
        return sent

    def send_interrupt(self):
        # First priority: kill any agent-spawned subprocess that's currently
        # streaming into a terminal. Those processes are children of the Agent
        # app, NOT of the workspace shell, so the shell-level Ctrl+Break below
        # cannot reach them. Without this branch, Ctrl+C looks broken whenever
        # the agent has run a command.
        try:
            from tools.terminal import interrupt_running_agent_process
            result = interrupt_running_agent_process()
            if result.get("signaled"):
                target = result.get("target", "process")
                bg_id = result.get("bg_id")
                tag = f"^C ({target}" + (f" bg_id={bg_id}" if bg_id else "") + ")\n"
                self._term.append_process_output(tag)
                return
        except Exception as e:
            self._term.append_process_output(f"[interrupt error: {e}]\n")

        # PTY path: ConPTY delivers Ctrl+C to the foreground program correctly —
        # just send ETX. (The AttachConsole/GenerateConsoleCtrlEvent dance below
        # is only needed for the legacy pipe shell.)
        if self._pty:
            if self._backend is not None and self._backend.is_alive():
                self._backend.write("\x03")
                self._term.append_process_output("^C\n")
            return

        if self._proc.state() != QProcess.ProcessState.Running:
            return
        if sys.platform == "win32":
            if self._windows_send_ctrl_break():
                self._term.append_process_output("^C\n")
                return
            # Fallback path for shells that treat ETX from stdin as interrupt-like input.
            try:
                self._proc.write(b"\x03")
                self._term.append_process_output("^C\n")
            except Exception:
                pass
            return
        # POSIX: shell is running on a pipe, so best effort is to signal the shell process.
        pid = int(self._proc.processId() or 0)
        if pid > 0:
            try:
                os.kill(pid, signal.SIGINT)
                return
            except Exception:
                pass
        try:
            self._proc.write(b"\x03")
        except Exception:
            pass

    def send_raw_line(self, line: str):
        """Send text to the shell stdin (user Enter or agent tool). Supports multiple lines."""
        if self._pty:
            if self._backend is None or not self._backend.is_alive():
                self._term.append_process_output(
                    "[error] Shell is not running. Use + Terminal or wait for the shell to start.\n"
                )
                return
            raw = line or ""
            # PTY shells take CR as the line terminator; normalize any newlines.
            payload = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r") + "\r"
            self._backend.write(payload)
            return
        if self._proc.state() != QProcess.ProcessState.Running:
            self._term.append_process_output(
                "[error] Shell is not running. Use + Terminal or wait for the shell to start.\n"
            )
            return
        raw = line or ""
        nl = "\r\n" if sys.platform == "win32" else "\n"
        if "\n" in raw or "\r" in raw:
            parts = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            payload = nl.join(parts) + nl
        else:
            payload = raw + nl
        try:
            self._proc.write(payload.encode("utf-8", errors="replace"))
        except Exception as e:
            self._term.append_process_output(f"[error] write failed: {e}\n")

    def _start_shell(self):
        if not os.path.isdir(self._cwd):
            self._append_notice(f"[error] Working directory does not exist: {self._cwd}\n")
            self._cwd = os.getcwd()

        self._proc.setWorkingDirectory(self._cwd)

        if sys.platform == "win32":
            comspec = os.environ.get("COMSPEC", "cmd.exe")
            self._proc.setProgram(comspec)
            safe = self._cwd.replace('"', r'\"')
            init = f'cd /d "{safe}" && echo [Workspace shell — same session for you and the agent]'
            self._proc.setArguments(["/K", init])
        else:
            bash = shutil.which("bash")
            if bash:
                self._proc.setProgram(bash)
                self._proc.setArguments(["--noprofile", "--norc", "-i"])
            else:
                self._proc.setProgram("/bin/sh")
                self._proc.setArguments(["-i"])

        self._proc.start()
        if not self._proc.waitForStarted(8000):
            self._append_notice(f"[error] Shell did not start: {self._proc.errorString()}\n")
            return
        self._term.moveCursor(QTextCursor.MoveOperation.End)
        self._term.set_anchor_position(self._term.textCursor().position())

    def _on_proc_error(self, _error):
        self._term.append_process_output(f"[error] {self._proc.errorString()}\n")

    def _read_output(self):
        data = bytes(self._proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._term.append_process_output(data)

    def _on_finished(self, exit_code: int, _exit_status):
        self._term.append_process_output(f"\n[process exited: code={exit_code}]\n")

    def append_agent_command(self, cmd: str):
        """Display an agent-issued command in the terminal (no subprocess — output follows separately)."""
        self._term.append_process_output(f"\n$ {cmd}\n")

    def stop(self):
        if self._pty:
            if self._backend is not None:
                self._backend.kill()
            return
        if self._proc.state() != QProcess.ProcessState.NotRunning:
            pid = int(self._proc.processId() or 0)
            if pid > 0 and sys.platform == "win32":
                try:
                    # Match VS Code-like terminal close semantics by killing the
                    # full subprocess tree rooted at the shell.
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=subprocess.CREATE_NO_WINDOW,  # no popup console
                        timeout=10,
                    )
                except Exception:
                    self._proc.kill()
            else:
                self._proc.kill()
            self._proc.waitForFinished(3000)

    def closeEvent(self, event):
        # Called when the widget is closed via Qt's normal teardown — reap the
        # shell so the inner QProcess isn't destroyed while cmd.exe is alive.
        try: self.stop()
        except Exception:
            traceback.print_exc()
        super().closeEvent(event)

    def focus_terminal(self):
        self._term.setFocus(Qt.FocusReason.OtherFocusReason)
        # Legacy TerminalSurface tracks an editable tail; PTY view does not.
        if hasattr(self._term, "moveCursor"):
            self._term.moveCursor(QTextCursor.MoveOperation.End)


def workspace_tab_bar_stylesheet_terminal(p: dict) -> str:
    # Inactive tabs: full thin frame + muted bottom edge so they don’t merge with the pane.
    _mute = p.get("accent_muted", p["border"])
    return f"""
        QTabWidget::pane {{
            border: none;
            background: {p['panel_alt']};
        }}
        QTabBar::tab {{
            background: {p['panel']};
            color: {p['muted_text']};
            border: 1px solid {p['border']};
            border-bottom: 1px solid {_mute};
            padding: 3px 10px;
            margin-right: 1px;
            font-family: Consolas;
            font-size: 8pt;
        }}
        QTabBar::tab:selected {{
            background: {p['panel_alt']};
            color: {p['accent']};
            border: 1px solid {p['border']};
            border-bottom: 2px solid {p['accent']};
        }}
        QTabBar::tab:hover {{
            color: {p['accent_bright']};
        }}
    """


class TerminalWorkspacePanel(QFrame):
    """Multi-tab integrated shell for the right workspace."""

    def __init__(self, parent=None,
                 cwd_resolver: Callable[[], str] | None = None,
                 collapse_cb=None,
                 auto_initial_tab: bool = True,
                 view_change_cb=None):
        super().__init__(parent)
        self.setObjectName("TerminalWorkspacePanel")
        self._sessions: list[IntegratedTerminalSession] = []
        self._titles: list[str] = []  # parallels _sessions; canonical tab titles
        self._conv_sessions: dict[str, IntegratedTerminalSession] = {}  # conv_id -> session
        self._cwd_resolver: Callable[[], str] | None = cwd_resolver
        self._collapse_cb = collapse_cb
        self._tab_seq = 0
        self._auto_initial_tab = auto_initial_tab
        # View mode: "tabbed" (one terminal, tabs on top — the original), "column"
        # (all terminals side-by-side in one row), or "grid" (wrapped N-per-row).
        # _grid_columns == 0 means Auto (derive column count from the panel width).
        self._view_change_cb = view_change_cb
        self._active_index = -1            # canonical "active" terminal in multi views
        self._cells: list[QFrame] = []     # cell frames in the current multi view
        self._last_built_ncols = 0         # so resize only rebuilds when it matters
        try:
            from core.agent import load_config
            _cfg = load_config()
            self._view_mode = str(_cfg.get("terminal_view_mode", "tabbed") or "tabbed").lower()
            self._grid_columns = int(_cfg.get("terminal_grid_columns", 0) or 0)
        except Exception:
            self._view_mode, self._grid_columns = "tabbed", 0
        if self._view_mode not in ("tabbed", "column", "grid"):
            self._view_mode = "tabbed"

        # Kill child cmd.exe/bash shells cleanly on app quit. Without this Qt
        # destroys the QProcess wrappers while their underlying OS processes
        # are still running → the "QProcess: Destroyed while process is still
        # running" warning, and potentially orphaned cmd.exe holding the CWD.
        try:
            from PyQt6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._reap_all_shells)
        except Exception:
            pass

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        p = PALETTE
        nav = QHBoxLayout()
        nav.setContentsMargins(8, 4, 8, 4)
        nav.setSpacing(4)

        self._new_btn = QPushButton("+ Terminal")
        self._new_btn.setFont(QFont("Consolas", 8))
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.setStyleSheet(
            f"color:{p['accent']};background:{p['panel']};"
            f"border:1px solid {p['border']};border-radius:0;padding:2px 10px;"
        )
        self._new_btn.clicked.connect(self.add_terminal_tab)
        nav.addWidget(self._new_btn)

        nav.addStretch(1)

        # View-mode selector: Tabs | Cols | Grid. Compact segmented buttons.
        self._view_btns: dict[str, QPushButton] = {}
        for mode, label, tip in (
            ("tabbed", "Tabs", "One terminal at a time with tabs on top"),
            ("column", "Cols", "All terminals side-by-side in a single row"),
            ("grid",   "Grid", "Terminals tiled in a grid that wraps to new rows"),
        ):
            b = QPushButton(label)
            b.setFont(QFont("Consolas", 8))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setToolTip(tip)
            b.setFixedHeight(20)
            b.clicked.connect(lambda _checked, m=mode: self.set_view_mode(m))
            self._view_btns[mode] = b
            nav.addWidget(b)

        # Grid column count (visible only in Grid mode). 0 = Auto (fit to width).
        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(0, 8)
        self._cols_spin.setSpecialValueText("Auto")
        self._cols_spin.setValue(max(0, min(8, self._grid_columns)))
        self._cols_spin.setFixedHeight(20)
        self._cols_spin.setToolTip("Columns per row before wrapping (Auto fits the width)")
        self._cols_spin.setFont(QFont("Consolas", 8))
        self._cols_spin.valueChanged.connect(self._on_cols_spin_changed)
        self._cols_label = QLabel("cols:")
        self._cols_label.setFont(QFont("Consolas", 8))
        # Style inline at construction — every other toolbar control does, and
        # apply_theme() may not run before this is first shown (Grid mode),
        # which otherwise leaves a native light-themed spinbox.
        self._cols_label.setStyleSheet(f"color:{p['muted_text']};background:transparent;border:none;")
        self._cols_spin.setStyleSheet(
            f"QSpinBox {{ color:{p['text']};background:{p['panel']};"
            f"border:1px solid {p['border']};padding:0 2px; }}")
        nav.addWidget(self._cols_label)
        nav.addWidget(self._cols_spin)

        self._close_btn = QPushButton("\u2715")
        self._close_btn.setFont(QFont("Consolas", 10))
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFixedWidth(22)
        self._close_btn.setStyleSheet(f"color:{p['muted_text']};background:transparent;border:none;")
        self._close_btn.clicked.connect(self._request_collapse)
        nav.addWidget(self._close_btn)

        self._nav_w = QWidget()
        self._nav_w.setLayout(nav)
        self._nav_w.setStyleSheet(f"background:{p['panel']};border-bottom:1px solid {p['border']};")
        root.addWidget(self._nav_w)

        # The display area is a stack: page 0 is the classic tab widget, page 1
        # is the multi-terminal scroll surface (column / grid). Sessions are
        # reparented between them as the view mode changes; only one is shown.
        self._view_stack = QStackedWidget()
        root.addWidget(self._view_stack, stretch=1)

        self._tab_widget = ThemedClosableTabWidget()
        self._tab_widget.setFont(QFont("Consolas", 8))
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.tabCloseRequested.connect(self._close_tab)
        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        self._tab_widget.setStyleSheet(workspace_tab_bar_stylesheet_terminal(p))
        self._tab_widget.set_close_palette(p)
        self._view_stack.addWidget(self._tab_widget)

        # Multi-terminal surface: a scroll area wrapping a grid container.
        self._multi_scroll = QScrollArea()
        self._multi_scroll.setWidgetResizable(True)
        self._multi_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._multi_container = QWidget()
        self._multi_grid = QGridLayout(self._multi_container)
        self._multi_grid.setContentsMargins(6, 6, 6, 6)
        self._multi_grid.setSpacing(6)
        self._multi_scroll.setWidget(self._multi_container)
        self._view_stack.addWidget(self._multi_scroll)

        if self._auto_initial_tab:
            self.add_terminal_tab()

        # Reflect the saved view mode (also positions the stack + nav controls).
        self._apply_view_mode(self._view_mode, persist=False)

    def set_collapse_callback(self, cb):
        self._collapse_cb = cb

    def set_cwd_resolver(self, fn: Callable[[], str] | None):
        self._cwd_resolver = fn

    def _request_collapse(self):
        if self._collapse_cb:
            self._collapse_cb()

    def _reap_all_shells(self):
        """Stop every shell session cleanly on app quit (wired via
        QApplication.aboutToQuit) so QProcess wrappers aren't destroyed while
        their cmd.exe children are still running."""
        for s in list(self._sessions):
            try: s.stop()
            except Exception:
                traceback.print_exc()

    def _resolve_cwd(self) -> str:
        if self._cwd_resolver:
            try:
                path = (self._cwd_resolver() or "").strip()
                if path and os.path.isdir(path):
                    return os.path.abspath(path)
            except Exception:
                pass
        return os.getcwd()

    def _attach_new_session(self, session: "IntegratedTerminalSession", title: str):
        """Register a freshly-built session and place it in the current view
        (a new tab, or a new cell in the column/grid surface). Keeps _sessions /
        _titles / the active index in lockstep so every view mode agrees."""
        self._sessions.append(session)
        self._titles.append(title)
        new_idx = len(self._sessions) - 1
        if self._view_mode == "tabbed":
            self._tab_widget.addTab(session, title)
            self._tab_widget.setCurrentIndex(new_idx)
        else:
            self._active_index = new_idx
            self._rebuild_multi()
        self._active_index = new_idx
        QTimer.singleShot(50, session.focus_terminal)

    def add_terminal_tab(self):
        self._tab_seq += 1
        cwd = self._resolve_cwd()
        session = IntegratedTerminalSession(cwd=cwd, parent=self)
        self._attach_new_session(session, f"Terminal {self._tab_seq}")

    # ── Restart persistence ──────────────────────────────────────────

    def persistence_tabs(self, deep: bool = True) -> list[dict]:
        """Snapshot each tab as {title, cwd, resume, command} for restart restore."""
        out: list[dict] = []
        for i, session in enumerate(self._sessions):
            try:
                info = session.persistence_info(deep=deep)
            except Exception:
                info = {"cwd": "", "resume": None, "command": ""}
            out.append({
                "title": self._titles[i] if i < len(self._titles) else "",
                "cwd": info.get("cwd") or "",
                "resume": info.get("resume"),
                "command": info.get("command") or "",
            })
        return out

    def restore_tab(self, cwd: str, title: str = "", resume: str | None = None,
                    command: str = ""):
        """Recreate a tab in *cwd* (the shell spawns there, so it's already in
        the right place) and PREFILL the last command so the user just hits
        Enter. A known resumable tool (claude → ``claude --continue``) is
        prefilled in preference to the raw last command. The PTY backend queues
        the prefill until the prompt appears."""
        self._tab_seq += 1
        target_cwd = cwd if (cwd and os.path.isdir(cwd)) else self._resolve_cwd()
        session = IntegratedTerminalSession(cwd=target_cwd, parent=self)
        self._attach_new_session(session, title or f"Terminal {self._tab_seq}")
        prefill = resume or command
        if prefill:
            session.prefill(prefill)
        return session

    def create_named_tab(self, title: str, cwd: str = "") -> "IntegratedTerminalSession":
        """Create a new tab with an explicit title (e.g. for an agent bg process)
        and return its session. The shell starts in `cwd` (or the workspace
        default if empty)."""
        target_cwd = cwd if (cwd and os.path.isdir(cwd)) else self._resolve_cwd()
        session = IntegratedTerminalSession(cwd=target_cwd, parent=self)
        self._attach_new_session(session, title or "Agent")
        return session

    def close_session(self, session) -> bool:
        """Close the tab that owns *session*. Returns True if found and closed."""
        try:
            idx = self._sessions.index(session)
        except ValueError:
            return False
        self._close_tab(idx)
        return True

    def _current_index(self) -> int:
        """The active terminal's index, valid in every view mode."""
        if self._view_mode == "tabbed":
            return self._tab_widget.currentIndex()
        if 0 <= self._active_index < len(self._sessions):
            return self._active_index
        return 0 if self._sessions else -1

    def _set_active(self, index: int):
        """Make *index* the active terminal (selects the tab or highlights the
        cell) and focus it."""
        if not (0 <= index < len(self._sessions)):
            return
        self._active_index = index
        if self._view_mode == "tabbed":
            self._tab_widget.setCurrentIndex(index)
        else:
            self._highlight_active_cell()
        QTimer.singleShot(0, self._sessions[index].focus_terminal)

    def _on_tab_changed(self, index: int):
        if index < 0 or index >= len(self._sessions):
            return
        self._active_index = index
        QTimer.singleShot(0, self._sessions[index].focus_terminal)

    def _close_tab(self, index: int):
        if index < 0 or index >= len(self._sessions):
            return
        session = self._sessions.pop(index)
        if index < len(self._titles):
            self._titles.pop(index)
        session.stop()
        session.deleteLater()
        if self._view_mode == "tabbed":
            self._tab_widget.removeTab(index)
        # Remove any conversation mapping pointing at this session
        self._conv_sessions = {k: v for k, v in self._conv_sessions.items() if v is not session}
        # Forget any agent bg-tab registration so subsequent kill/check
        # calls don't reference a dead session.
        try:
            from tools.workspace_terminal import bg_bridge
            bg_bridge.forget_session(session)
        except Exception:
            pass
        # Keep the active index sane, then refresh the multi view if shown.
        if self._active_index >= len(self._sessions):
            self._active_index = len(self._sessions) - 1
        if not self._sessions:
            self.add_terminal_tab()
        elif self._view_mode != "tabbed":
            self._rebuild_multi()

    def get_or_create_for_conv(self, conv_id: str, conv_name: str) -> "IntegratedTerminalSession":
        """Return the terminal session for this conversation, creating a new tab if needed."""
        existing = self._conv_sessions.get(conv_id)
        if existing is not None and existing in self._sessions:
            return existing
        cwd = self._resolve_cwd()
        session = IntegratedTerminalSession(cwd=cwd, parent=self)
        label = (conv_name or conv_id or "Agent")[:28]
        self._attach_new_session(session, label)
        self._conv_sessions[conv_id] = session
        return session

    def close_conv(self, conv_id: str):
        """Close the terminal tab for a conversation (used by sub-agent cleanup)."""
        session = self._conv_sessions.get(conv_id)
        if session is None:
            return
        try:
            idx = self._sessions.index(session)
            self._close_tab(idx)
        except ValueError:
            pass

    def switch_to_conv(self, conv_id: str):
        """Make the terminal for this conversation the active one."""
        session = self._conv_sessions.get(conv_id)
        if session is None:
            return
        try:
            self._set_active(self._sessions.index(session))
        except ValueError:
            pass

    def send_to_active(self, text: str):
        idx = self._current_index()
        if idx < 0 or idx >= len(self._sessions):
            return
        line = (text or "").rstrip("\r\n")
        self._sessions[idx].send_raw_line(line)

    def get_active_text(self) -> str:
        """Return the full scrollback text of the currently active terminal."""
        idx = self._current_index()
        if idx < 0 or idx >= len(self._sessions):
            return ""
        return self._sessions[idx]._term.toPlainText()

    def focus_active_input(self):
        idx = self._current_index()
        if 0 <= idx < len(self._sessions):
            self._sessions[idx].focus_terminal()

    def close_all_sessions(self):
        for s in list(self._sessions):
            s.stop()
            s.deleteLater()
        self._sessions.clear()
        self._titles.clear()
        self._conv_sessions.clear()
        self._tab_widget.clear()
        self._clear_multi_container()
        self._active_index = -1
        self._tab_seq = 0
        self.add_terminal_tab()

    # ── View modes: tabbed / column / grid ───────────────────────────

    def set_view_mode(self, mode: str):
        """User-facing entry: switch view mode, persist it, and tell sibling
        conversation panels to follow so the choice is global."""
        mode = mode if mode in ("tabbed", "column", "grid") else "tabbed"
        if mode == self._view_mode:
            return
        self._apply_view_mode(mode, persist=True)
        if callable(self._view_change_cb):
            try:
                self._view_change_cb(self._view_mode, self._grid_columns, self)
            except Exception:
                pass

    def apply_view_settings(self, mode: str, columns: int):
        """Adopt a view mode + column count pushed from a sibling panel (no
        re-persist, no re-broadcast — avoids feedback loops)."""
        self._grid_columns = max(0, min(8, int(columns or 0)))
        try:
            self._cols_spin.blockSignals(True)
            self._cols_spin.setValue(self._grid_columns)
            self._cols_spin.blockSignals(False)
        except Exception:
            pass
        self._apply_view_mode(mode if mode in ("tabbed", "column", "grid") else "tabbed",
                              persist=False)

    def _on_cols_spin_changed(self, value: int):
        self._grid_columns = max(0, min(8, int(value)))
        self._persist_view_config()
        if self._view_mode == "grid":
            self._rebuild_multi()
        if callable(self._view_change_cb):
            try:
                self._view_change_cb(self._view_mode, self._grid_columns, self)
            except Exception:
                pass

    def _persist_view_config(self):
        try:
            from core.agent import load_config, save_config
            cfg = load_config()
            cfg["terminal_view_mode"] = self._view_mode
            cfg["terminal_grid_columns"] = self._grid_columns
            save_config(cfg)
        except Exception:
            pass

    def _apply_view_mode(self, mode: str, persist: bool):
        """Reparent every session into the target view and show it."""
        mode = mode if mode in ("tabbed", "column", "grid") else "tabbed"
        keep_active = self._current_index()  # preserve selection across the move
        self._detach_all_sessions()
        self._view_mode = mode
        if 0 <= keep_active < len(self._sessions):
            self._active_index = keep_active
        if mode == "tabbed":
            # Block signals: removeTab/addTab fire currentChanged with stale
            # indices mid-reparent, which would otherwise clobber _active_index.
            self._tab_widget.blockSignals(True)
            for i, session in enumerate(self._sessions):
                self._tab_widget.addTab(session, self._titles[i] if i < len(self._titles) else f"Terminal {i+1}")
            if 0 <= self._active_index < len(self._sessions):
                self._tab_widget.setCurrentIndex(self._active_index)
            self._tab_widget.blockSignals(False)
            self._view_stack.setCurrentWidget(self._tab_widget)
        else:
            self._rebuild_multi()
            self._view_stack.setCurrentWidget(self._multi_scroll)
        self._update_view_buttons()
        if persist:
            self._persist_view_config()

    def _detach_all_sessions(self):
        """Pull every session out of whatever currently holds it (tab pages or
        grid cells) so it can be re-homed without being destroyed."""
        self._tab_widget.blockSignals(True)
        while self._tab_widget.count():
            self._tab_widget.removeTab(0)
        self._tab_widget.blockSignals(False)
        for s in self._sessions:
            try:
                s.setParent(None)
            except RuntimeError:
                pass
        self._clear_multi_container()

    def _clear_multi_container(self):
        """Delete leftover cell frames (sessions must already be detached)."""
        if not hasattr(self, "_multi_grid"):
            return
        while self._multi_grid.count():
            item = self._multi_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._cells = []

    def _effective_columns(self) -> int:
        """Column count for the current multi view."""
        n = len(self._sessions)
        if n <= 0:
            return 1
        if self._view_mode == "column":
            return n  # everything in a single row
        # Grid: explicit count, or Auto = fit to the available width.
        if self._grid_columns and self._grid_columns > 0:
            return min(self._grid_columns, n)
        return self._auto_columns(n)

    def _auto_columns(self, n: int) -> int:
        """Pick a column count from the panel width (~360px per terminal)."""
        min_cell = 360
        try:
            avail = self._multi_scroll.viewport().width() or self.width()
        except Exception:
            avail = self.width()
        cols = max(1, int(avail) // min_cell) if avail else 1
        return max(1, min(cols, n))

    def _rebuild_multi(self):
        """(Re)build the column/grid surface from _sessions/_titles."""
        if self._view_mode == "tabbed":
            return
        self._clear_multi_container()
        # Reset any stale row/column stretch factors from a previous layout.
        # QGridLayout keeps them after widgets are removed, so switching e.g.
        # grid (2 rows) -> column (1 row) would leave row-1's stretch holding
        # half the height and the single row wouldn't reach the bottom.
        _span = max(self._multi_grid.rowCount(), self._multi_grid.columnCount(),
                    len(self._sessions)) + 1
        for k in range(_span):
            self._multi_grid.setRowStretch(k, 0)
            self._multi_grid.setColumnStretch(k, 0)
        ncols = max(1, self._effective_columns())
        self._last_built_ncols = ncols
        column_mode = (self._view_mode == "column")
        for i, session in enumerate(self._sessions):
            cell = self._make_cell(i, session)
            self._cells.append(cell)
            r, c = divmod(i, ncols)
            self._multi_grid.addWidget(cell, r, c)
        # Equal stretch so cells share the space evenly.
        for c in range(ncols):
            self._multi_grid.setColumnStretch(c, 1)
        nrows = (len(self._sessions) + ncols - 1) // ncols
        for r in range(max(1, nrows)):
            self._multi_grid.setRowStretch(r, 1)
        # Column mode = one row of side-by-side feeds: let it grow past the
        # viewport and scroll horizontally; otherwise fit width, scroll down.
        self._multi_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded if column_mode
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._multi_scroll.setWidgetResizable(True)
        self._highlight_active_cell()

    def _make_cell(self, index: int, session: "IntegratedTerminalSession") -> QFrame:
        """A framed terminal cell: a thin header (title + close) over the feed.
        Clicking anywhere in the cell makes it the active terminal."""
        cell = QFrame()
        cell.setObjectName("TermCell")
        cell.setMinimumSize(280, 180)
        cell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        v = QVBoxLayout(cell)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)

        header = QWidget()
        header.setObjectName("TermCellHeader")
        h = QHBoxLayout(header)
        h.setContentsMargins(6, 2, 4, 2)
        h.setSpacing(4)
        title = QLabel(self._titles[index] if index < len(self._titles) else f"Terminal {index+1}")
        title.setFont(QFont("Consolas", 8))
        h.addWidget(title)
        h.addStretch(1)
        close = QPushButton("✕")
        close.setFont(QFont("Consolas", 9))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setFixedSize(18, 18)
        close.setFlat(True)
        close.clicked.connect(lambda _checked, s=session: self.close_session(s))
        h.addWidget(close)
        v.addWidget(header)
        v.addWidget(session, stretch=1)
        # _detach_all_sessions() reparents via setParent(None), which HIDES the
        # widget. The tab path re-shows on addTab; this path must show it
        # explicitly or the terminal feed stays blank (only the header paints)
        # until the session is rebuilt.
        session.show()

        # Click-to-activate (header click + a press filter on the cell).
        def _activate(_e=None, s=session):
            try:
                self._set_active(self._sessions.index(s))
            except ValueError:
                pass
        header.mousePressEvent = lambda e, f=_activate: f()  # type: ignore[assignment]

        cell._title_label = title  # for theming/highlight  # type: ignore[attr-defined]
        cell._header = header      # type: ignore[attr-defined]
        cell._session = session    # type: ignore[attr-defined]
        self._style_cell(cell, active=False)
        return cell

    def _style_cell(self, cell: QFrame, active: bool):
        p = PALETTE
        border = p['accent'] if active else p['border']
        cell.setStyleSheet(
            f"QFrame#TermCell {{ background:{p['panel_alt']}; border:1px solid {border}; }}"
            f"QFrame#TermCellHeader {{ background:{p['panel']}; "
            f"border-bottom:1px solid {p['border']}; }}"
        )
        try:
            cell._title_label.setStyleSheet(  # type: ignore[attr-defined]
                f"color:{p['accent'] if active else p['muted_text']};background:transparent;border:none;")
        except Exception:
            pass

    def _highlight_active_cell(self):
        for i, cell in enumerate(self._cells):
            self._style_cell(cell, active=(i == self._current_index()))

    def _update_view_buttons(self):
        """Reflect the active mode on the segmented buttons + show the grid
        column control only in Grid mode."""
        p = PALETTE
        for mode, btn in getattr(self, "_view_btns", {}).items():
            active = (mode == self._view_mode)
            btn.setStyleSheet(
                f"QPushButton {{ color:{p['accent_bright'] if active else p['muted_text']};"
                f" background:{p['panel_alt'] if active else p['panel']};"
                f" border:1px solid {p['accent'] if active else p['border']};"
                f" border-radius:0; padding:1px 8px; }}"
                f"QPushButton:hover {{ border-color:{p['accent_muted']}; }}"
            )
        grid = (self._view_mode == "grid")
        try:
            self._cols_label.setVisible(grid)
            self._cols_spin.setVisible(grid)
        except Exception:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # Auto-grid: re-tile only when the computed column count actually changes,
        # so dragging the splitter doesn't thrash the layout.
        if self._view_mode == "grid" and not (self._grid_columns and self._grid_columns > 0):
            new_cols = self._auto_columns(len(self._sessions))
            if new_cols != self._last_built_ncols:
                self._rebuild_multi()

    def apply_theme(self):
        p = PALETTE
        self.setStyleSheet(
            f"""
            QFrame#TerminalWorkspacePanel {{
                background: {p['panel_alt']};
                border: none;
            }}
            """
        )
        self._nav_w.setStyleSheet(f"background:{p['panel']};border-bottom:1px solid {p['border']};")
        self._new_btn.setStyleSheet(
            f"color:{p['accent']};background:{p['panel']};"
            f"border:1px solid {p['border']};border-radius:0;padding:2px 10px;"
        )
        self._close_btn.setStyleSheet(f"color:{p['muted_text']};background:transparent;border:none;")
        self._tab_widget.setStyleSheet(workspace_tab_bar_stylesheet_terminal(p))
        self._tab_widget.set_close_palette(p)
        try:
            self._cols_label.setStyleSheet(f"color:{p['muted_text']};background:transparent;border:none;")
            self._cols_spin.setStyleSheet(
                f"QSpinBox {{ color:{p['text']};background:{p['panel']};"
                f"border:1px solid {p['border']};padding:0 2px; }}")
        except Exception:
            pass
        self._update_view_buttons()
        for s in self._sessions:
            s.apply_theme()
        # Re-skin the multi-view cells (cheap; no reparenting of the sessions).
        if self._view_mode != "tabbed":
            self._highlight_active_cell()


# ──────────────────────────────────────────────────────────────────────
# Per-conversation terminal: each chat gets its own TerminalWorkspacePanel,
# all alive concurrently. Switching conversations swaps which panel is shown.
# Processes in inactive panels keep running.
# ──────────────────────────────────────────────────────────────────────


class MultiConvTerminalPanel(QFrame):
    """Hosts one ``TerminalWorkspacePanel`` per conversation in a stack so
    each chat has an isolated set of terminal tabs (and processes) without
    interrupting any other chat. Public API forwards to the active panel,
    so existing call sites that did
    ``right_workspace.terminal_panel.send_to_active(...)`` keep working."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("MultiConvTerminalPanel")
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._stack = QStackedWidget()
        self._stack.setMinimumWidth(0)
        self._stack.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        lay.addWidget(self._stack)
        self._panels: dict[str, TerminalWorkspacePanel] = {}
        self._current_conv_id: str = ""
        self._cwd_resolver: Callable[[], str] | None = None
        self._collapse_cb = None
        # Restart persistence: saved tabs from the previous run, consumed once
        # per conversation as its panel is first created this session.
        self._saved_conv_tabs: dict | None = None
        self._restored: set[str] = set()
        # Placeholder shown before any conv is active (very brief — chat_widget
        # calls set_active_conv() during conversation load).
        self._placeholder = QLabel("")
        self._stack.addWidget(self._placeholder)

    # ── Configuration ──────────────────────────────────────────────

    def set_cwd_resolver(self, fn: Callable[[], str] | None):
        self._cwd_resolver = fn
        for p in self._panels.values():
            p.set_cwd_resolver(fn)

    def set_collapse_callback(self, cb):
        self._collapse_cb = cb
        for p in self._panels.values():
            p.set_collapse_callback(cb)

    def _on_panel_view_change(self, mode: str, columns: int, source):
        """One conversation's panel changed view mode/columns — apply the same
        to every other conversation's panel so the choice feels global (and is
        already persisted to config by the source panel)."""
        for p in self._panels.values():
            if p is source:
                continue
            try:
                p.apply_view_settings(mode, columns)
            except Exception:
                pass

    # ── Per-conversation lifecycle ─────────────────────────────────

    def get_or_create_panel(self, conv_id: str) -> TerminalWorkspacePanel:
        if not conv_id:
            conv_id = "_default"
        panel = self._panels.get(conv_id)
        if panel is not None:
            return panel
        saved_tabs = self._take_saved_tabs(conv_id)
        panel = TerminalWorkspacePanel(
            parent=self,
            cwd_resolver=self._cwd_resolver,
            collapse_cb=self._collapse_cb,
            # Skip the default empty tab when we're about to restore real ones.
            auto_initial_tab=not saved_tabs,
            view_change_cb=self._on_panel_view_change,
        )
        panel.setMinimumWidth(0)
        panel.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        self._panels[conv_id] = panel
        self._stack.addWidget(panel)
        for tab in (saved_tabs or []):
            try:
                panel.restore_tab(tab.get("cwd", ""), tab.get("title", ""),
                                  tab.get("resume"), tab.get("command", ""))
            except Exception:
                pass
        return panel

    def _take_saved_tabs(self, conv_id: str) -> list[dict]:
        """Return (once) the saved tabs for *conv_id* from the previous run."""
        if conv_id in self._restored:
            return []
        self._restored.add(conv_id)
        if self._saved_conv_tabs is None:
            try:
                from core.terminal_persistence import load_state
                self._saved_conv_tabs = load_state().get("conversations", {})
            except Exception:
                self._saved_conv_tabs = {}
        entry = (self._saved_conv_tabs or {}).get(conv_id) or {}
        tabs = entry.get("tabs") or []
        return [t for t in tabs if isinstance(t, dict)]

    def save_all(self, deep: bool = True):
        """Persist every live conversation's terminal tabs. Call BEFORE shells
        are killed at shutdown (``deep=True``). The periodic crash-safety save
        passes ``deep=False`` to skip the psutil process-tree walk. Conversations
        without a live panel keep their previously-saved entry (so unvisited
        sessions aren't dropped)."""
        try:
            from core.terminal_persistence import load_state, save_state
        except Exception:
            return
        state = load_state()
        convs = state.setdefault("conversations", {})
        for conv_id, panel in self._panels.items():
            try:
                convs[conv_id] = {"tabs": panel.persistence_tabs(deep=deep)}
            except Exception:
                pass
        save_state(state)

    def set_active_conv(self, conv_id: str):
        """Switch the visible panel to *conv_id* (creating it if needed).
        All other panels remain alive — their processes keep running."""
        panel = self.get_or_create_panel(conv_id)
        self._stack.setCurrentWidget(panel)
        self._current_conv_id = conv_id or "_default"

    def remove_conv(self, conv_id: str):
        """Conversation deleted — kill all of its terminal sessions and drop
        the panel. Each session's stop() does taskkill /T /F so descendant
        processes don't leak."""
        panel = self._panels.pop(conv_id, None)
        if panel is None:
            return
        # Forget any agent bg-tab registrations bound to these sessions
        try:
            from tools.workspace_terminal import bg_bridge
            for s in list(panel._sessions):
                bg_bridge.forget_session(s)
        except Exception:
            pass
        try:
            panel.close_all_sessions()
        except Exception:
            pass
        try:
            self._stack.removeWidget(panel)
            panel.deleteLater()
        except Exception:
            pass

    def active_panel(self) -> TerminalWorkspacePanel | None:
        return self._panels.get(self._current_conv_id)

    # ── Forwarding API (mimics TerminalWorkspacePanel) ─────────────
    # Every call is routed to the active panel so existing chat_widget
    # call sites keep working. For panels that may not yet be active
    # (e.g. agent fires a bg_action while the user is in a different
    # conv), see ``panel_for_conv``.

    def add_terminal_tab(self):
        p = self.active_panel()
        if p is not None:
            p.add_terminal_tab()

    def send_to_active(self, text: str):
        p = self.active_panel()
        if p is not None:
            p.send_to_active(text)

    def get_or_create_for_conv(self, conv_id: str, conv_name: str):
        """Route the agent's primary terminal to *conv_id*'s panel."""
        panel = self.get_or_create_panel(conv_id)
        return panel.get_or_create_for_conv(conv_id, conv_name)

    def close_conv(self, conv_id: str):
        # Sub-agent cleanup: ``conv_id`` here is "sa-<task_id>", which lives
        # inside whichever conversation panel was active when it was created.
        # Search every panel — the user may have switched away in the meantime.
        for panel in list(self._panels.values()):
            try:
                if conv_id in panel._conv_sessions:
                    panel.close_conv(conv_id)
            except Exception:
                pass

    def switch_to_conv(self, conv_id: str):
        p = self.active_panel()
        if p is not None:
            p.switch_to_conv(conv_id)

    def get_active_text(self) -> str:
        p = self.active_panel()
        return p.get_active_text() if p is not None else ""

    def focus_active_input(self):
        p = self.active_panel()
        if p is not None:
            p.focus_active_input()

    def close_all_sessions(self):
        # Reset just the active conversation's panel — used by
        # _new_conversation when the chat is wiped clean.
        p = self.active_panel()
        if p is not None:
            p.close_all_sessions()

    def create_named_tab(self, title: str, cwd: str = ""):
        """Used by bg_bridge to spawn a dedicated tab for a bg process.
        Lands in the active conversation's panel — i.e. whichever conv the
        agent is currently responding to."""
        p = self.active_panel()
        if p is None:
            return None
        return p.create_named_tab(title, cwd)

    def close_session(self, session) -> bool:
        # Find which panel owns the session and close it there.
        for panel in self._panels.values():
            if session in panel._sessions:
                return panel.close_session(session)
        return False

    def apply_theme(self):
        for p in self._panels.values():
            p.apply_theme()
