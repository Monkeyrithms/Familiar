"""
Tabbed file viewer / code editor for the chat right panel.
"""
import ctypes
import functools
import os
import subprocess
import sys
from PyQt6.QtWidgets import (
    QFrame,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QWidget,
    QPlainTextEdit,
    QFileDialog,
    QSplitter,
    QTextBrowser,
    QSizePolicy,
    QTextEdit,
    QLabel,
    QApplication,
    QTreeView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QFileIconProvider,
    QHeaderView,
    QMenu,
    QInputDialog,
    QMessageBox,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QAbstractItemView,
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer, QUrl, QDir, QSize, QRect, QPoint
from PyQt6.QtGui import (
    QFont,
    QFontMetrics,
    QColor,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextFormat,
    QTextCursor,
    QKeySequence,
    QShortcut,
    QPainter,
    QPen,
    QIcon,
    QDesktopServices,
    QFileSystemModel,
)

from ui.theme import PALETTE
from ui.themed_tab_widget import ThemedClosableTabWidget
from ui.media_viewer import MediaViewer, is_media_ext, MEDIA_EXTS


def _win32_explorer_select_file(path: str, parent: str) -> None:
    """Select *path* in Explorer. Paths with spaces require ``/select,"fullpath"`` as one argv string."""
    safe = path.replace('"', "")
    # ``explorer /select,D:\Code`` + separate argv "Space\..." breaks; quote the full path.
    params = f'/select,"{safe}"'
    try:
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,
            "open",
            "explorer.exe",
            params,
            parent,
            1,  # SW_SHOWNORMAL
        )
        if int(ret) <= 32:
            raise OSError(f"ShellExecuteW returned {ret}")
    except Exception:
        # cmd line: explorer.exe /select,"D:\path with spaces\file.txt"
        inner = safe.replace("%", "%%")
        subprocess.Popen(
            f'explorer.exe /select,"{inner}"',
            shell=True,
            cwd=parent,
        )


def _reveal_file_in_os_file_manager(file_path: str) -> None:
    """Open the system file manager on the folder for *file_path*, selecting the file when the OS allows."""
    raw = (file_path or "").strip()
    if not raw:
        return
    path = os.path.normpath(os.path.abspath(raw))
    parent = os.path.dirname(path) or "."
    try:
        if os.path.isfile(path):
            if sys.platform == "win32":
                _win32_explorer_select_file(path, parent)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(parent))
        elif os.path.isdir(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif os.path.isdir(parent):
            QDesktopServices.openUrl(QUrl.fromLocalFile(parent))
    except Exception:
        try:
            if os.path.isdir(parent):
                QDesktopServices.openUrl(QUrl.fromLocalFile(parent))
        except Exception:
            pass


class _PathHeaderHintLabel(QLabel):
    """Header path hint: left-click copies ``full_path`` and reveals it in the OS file manager."""

    clicked_with_path = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_path_for_click = ""

    def set_path_for_display(self, full_path: str, display_text: str) -> None:
        self._full_path_for_click = (full_path or "").strip()
        self.setText(display_text)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor
            if self._full_path_for_click
            else Qt.CursorShape.ArrowCursor
        )

    def mouseReleaseEvent(self, event):
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._full_path_for_click
        ):
            self.clicked_with_path.emit(self._full_path_for_click)
        super().mouseReleaseEvent(event)


def _mono_selection_qss(p: dict) -> str:
    """Selection highlight for text fields — neutral UI chrome, not accent-tinted."""
    return (
        f"selection-background-color: {p['border']};"
        f"selection-color: {p['text']};"
    )


_WIN_INVALID_NAME = frozenset('<>:"/\\|?*')


def _move_path_to_recycle_bin(path: str) -> tuple[bool, str | None]:
    """Send *path* to the OS trash/recycle bin (folders include contents). Returns (ok, err)."""
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return False, "Path does not exist."

    try:
        import send2trash

        send2trash.send2trash(path)
        return True, None
    except ImportError:
        pass
    except Exception as e:
        return False, str(e)

    if sys.platform == "win32":
        try:
            from ctypes import wintypes

            FO_DELETE = 3
            FOF_ALLOWUNDO = 0x40
            FOF_NOCONFIRMATION = 0x10
            FOF_SILENT = 0x04
            FOF_NOERRORUI = 0x0400

            class SHFILEOPSTRUCTW(ctypes.Structure):
                _fields_ = [
                    ("hwnd", wintypes.HWND),
                    ("wFunc", wintypes.UINT),
                    ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR),
                    ("fFlags", ctypes.c_ushort),
                    ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", wintypes.LPVOID),
                    ("lpszProgressTitle", wintypes.LPCWSTR),
                ]

            buf = path + "\0\0"
            op = SHFILEOPSTRUCTW()
            op.hwnd = None
            op.wFunc = FO_DELETE
            op.pFrom = buf
            op.pTo = None
            op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
            op.fAnyOperationsAborted = False
            op.hNameMappings = None
            op.lpszProgressTitle = None
            rc = int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)))
            if rc != 0:
                return False, f"Recycle Bin operation failed (code {rc})."
            if op.fAnyOperationsAborted:
                return False, "Operation aborted."
            return True, None
        except Exception as e:
            return False, str(e)

    if sys.platform == "darwin":
        try:
            esc = path.replace("\\", "\\\\").replace('"', '\\"')
            r = subprocess.run(
                [
                    "osascript",
                    "-e",
                    f'tell application "Finder" to delete POSIX file "{esc}"',
                ],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if r.returncode != 0:
                msg = (r.stderr or r.stdout or "osascript failed").strip()
                return False, msg or "Could not move to Trash."
            return True, None
        except Exception as e:
            return False, str(e)

    for cmd in (["gio", "trash", path], ["trash-put", path]):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode == 0:
                return True, None
        except FileNotFoundError:
            continue
        except Exception as e:
            return False, str(e)

    return (
        False,
        "Could not move to trash. Install: pip install send2trash",
    )


class _NoFileIconsProvider(QFileIconProvider):
    """Explorer sidebar: no file/folder pixmap icons — text + branch arrows only."""

    def icon(self, *args):
        return QIcon()


class _ExplorerTreeView(QTreeView):
    """QTreeView without native branch expanders — arrows are drawn only in the delegate."""

    def drawBranches(self, painter: QPainter, rect: QRect, index):
        painter.fillRect(rect, QColor(PALETTE["panel"]))


class _ExplorerTreeDelegate(QStyledItemDelegate):
    """Paints folder ▶/▼ arrows, file rows without glyphs, selection + hover fills."""

    def __init__(self, tree: QTreeView, parent=None):
        super().__init__(parent)
        self._tree = tree
        self.apply_palette(PALETTE)

    def apply_palette(self, p: dict) -> None:
        # Bright: files + folder arrows (click opens file / toggles expand).
        self._bright = QColor(p.get("accent_bright", p["text"]))
        # Folder names: main accent — lighter than accent_muted, still softer than leaves.
        self._folder_text = QColor(p.get("accent", self._bright))
        sel = QColor(p["border"])
        self._sel_bg = sel
        self._hover_bg = sel.darker(124)

    def paint(self, painter: QPainter, option, index):
        if index.column() != 0:
            return super().paint(painter, option, index)
        model = index.model()
        if not isinstance(model, QFileSystemModel):
            return super().paint(painter, option, index)

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()

        rect = option.rect
        is_selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        if is_selected:
            painter.fillRect(rect, self._sel_bg)
        elif is_hover:
            painter.fillRect(rect, self._hover_bg)

        fm = opt.fontMetrics
        arrow_reserve = fm.horizontalAdvance("\u25b6  ")

        fi = model.fileInfo(index)
        is_dir = fi.isDir()
        glyph = "\u25bc" if (is_dir and self._tree.isExpanded(index)) else (
            "\u25b6" if is_dir else ""
        )

        painter.setFont(opt.font)
        arrow_rect = QRect(rect.left() + 1, rect.top(), arrow_reserve, rect.height())
        text_left = rect.left() + arrow_reserve + 2
        text_rect = QRect(
            text_left,
            rect.top(),
            max(1, rect.right() - text_left + 1),
            rect.height(),
        )

        if glyph:
            painter.setPen(QPen(self._bright))
            painter.drawText(
                arrow_rect,
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                glyph,
            )

        name_color = self._folder_text if is_dir else self._bright
        painter.setPen(QPen(name_color))
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            model.fileName(index),
        )
        painter.restore()

    def sizeHint(self, option, index):
        sh = super().sizeHint(option, index)
        if index.column() == 0 and isinstance(index.model(), QFileSystemModel):
            fm = option.fontMetrics
            sh.setWidth(sh.width() + fm.horizontalAdvance("\u25b6  "))
        return sh


# ──────────────────────────────────────────────────────────────────────
# File viewer panel (right splitter in chat area)
# ──────────────────────────────────────────────────────────────────────

class PygmentsHighlighter(QSyntaxHighlighter):
    """QSyntaxHighlighter that uses Pygments for tokenization and palette shades for colors."""

    def __init__(self, parent=None, lexer=None):
        super().__init__(parent)
        self._lexer = lexer
        self._formats: dict = {}  # token_type -> QTextCharFormat
        self._rebuild_formats()

    def set_lexer(self, lexer):
        self._lexer = lexer
        self.rehighlight()

    def _rebuild_formats(self):
        """Build QTextCharFormat for each token type from current PALETTE."""
        self._formats.clear()
        p = PALETTE
        try:
            from pygments import token as T
        except ImportError:
            return

        token_map = {
            T.Keyword:              "glow_hot",
            T.Keyword.Constant:     "glow_hot",
            T.Keyword.Namespace:    "glow_hot",
            T.Keyword.Type:         "accent_bright",
            T.Name.Builtin:         "accent_bright",
            T.Name.Function:        "accent_bright",
            T.Name.Class:           "accent_bright",
            T.Name.Decorator:       "accent_bright",
            T.Name.Exception:       "accent_bright",
            T.Operator:             "accent",
            T.Operator.Word:        "glow_hot",
            T.Punctuation:          "accent_muted",
            T.Literal.String:       "muted_text",
            T.Literal.String.Doc:   "muted_text",
            T.Literal.String.Interpol: "accent",
            T.Literal.String.Escape:   "accent",
            T.Literal.Number:       "accent",
            T.Comment:              "accent_soft",
            T.Comment.Single:       "accent_soft",
            T.Comment.Multiline:    "accent_soft",
            T.Comment.Preproc:      "accent_muted",
            T.Name:                 "text",
            T.Text:                 "text",
        }
        for ttype, key in token_map.items():
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(p.get(key, p["text"])))
            self._formats[ttype] = fmt

    def _format_for_token(self, ttype):
        while ttype:
            fmt = self._formats.get(ttype)
            if fmt:
                return fmt
            ttype = ttype.parent
        return QTextCharFormat()

    def highlightBlock(self, text):
        if not self._lexer or not text:
            return
        try:
            from pygments import lex
        except ImportError:
            return
        index = 0
        for ttype, value in lex(text + "\n", self._lexer):
            length = len(value)
            if length == 0:
                continue
            fmt = self._format_for_token(ttype)
            if fmt.foreground().color().isValid():
                self.setFormat(index, length, fmt)
            index += length

    def refresh_palette(self):
        self._rebuild_formats()
        self.rehighlight()


class _LineNumberGutter(QWidget):
    """Left-side gutter that paints line numbers for a CodeEditor.
    Lines flagged in the editor's diff-added list get an accent dot marker."""

    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self):
        from PyQt6.QtCore import QSize
        return QSize(self._editor._line_number_width(), 0)

    def paintEvent(self, event):
        self._editor._paint_line_numbers(event)


class CodeEditor(QPlainTextEdit):
    """Editable code editor with Pygments syntax highlighting, a line-number
    gutter, current-line highlight, and debounced auto-save."""

    file_saved = pyqtSignal(str)  # emits path after save

    def __init__(self, parent=None):
        super().__init__(parent)
        self._file_path: str = ""
        self._highlighter = PygmentsHighlighter(self.document())
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._do_save)
        self._inhibit_reload = False  # prevent reload loop when we save
        self._typing_mute = False       # True during programmatic load/highlight
        self._typing_mute_until_rehl = False
        self.textChanged.connect(self._on_edited)
        self.setFont(QFont("Consolas", 10))
        self.setCursorWidth(3)
        self.setTabStopDistance(QFontMetrics(self.font()).horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        # Diff overlay state (populated by set_diff_highlights)
        self._diff_added_lines: list[int] = []
        self._diff_removed_gutter_lines: list[int] = []

        # Pulse-highlight state (populated by set_pulse_highlight). Each entry
        # is (block_number, start_col_in_block, length). The alpha oscillates
        # between _PULSE_ALPHA_LOW and _PULSE_ALPHA_HIGH via _pulse_timer.
        self._pulse_ranges: list[tuple[int, int, int]] = []
        self._pulse_alpha: int = 55
        self._pulse_dir: int = 1  # +1 ascending, -1 descending
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(60)
        self._pulse_timer.timeout.connect(self._pulse_tick)

        # Line number gutter + current-line highlight
        self._gutter = _LineNumberGutter(self)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._on_update_request)
        self.cursorPositionChanged.connect(self._paint_current_line_highlight)
        self._update_gutter_width()
        self._paint_current_line_highlight()

        # Async chunked rehighlight (used after large pastes / loads).
        # Pygments per-block is the bottleneck; we spread the work across
        # event-loop ticks so paste returns instantly and the UI stays live.
        self._rehl_timer = QTimer(self)
        self._rehl_timer.setInterval(0)
        self._rehl_timer.timeout.connect(self._rehl_tick)
        self._rehl_next_block: int = 0
        self._rehl_end_block: int = 0

    # ── Paste fast-path ────────────────────────────────────────────────
    # Default QPlainTextEdit paste streams chars through the highlighter
    # synchronously, so pygments-lex runs per inserted block on the UI
    # thread. For anything >~50 lines that visibly stalls. We bypass the
    # lexer during the insert and rehighlight in background chunks.

    _PASTE_FAST_PATH_CHARS = 4000
    _PASTE_FAST_PATH_LINES = 50
    _REHL_CHUNK_BLOCKS = 80

    def insertFromMimeData(self, source):
        text = source.text() if source is not None else ""
        if (
            not text
            or (len(text) < self._PASTE_FAST_PATH_CHARS
                and text.count("\n") < self._PASTE_FAST_PATH_LINES)
        ):
            super().insertFromMimeData(source)
            return

        h = self._highlighter
        saved_lexer = getattr(h, "_lexer", None)
        # Suppress per-block pygments during the insert. highlightBlock returns
        # early when _lexer is falsy.
        h._lexer = None

        self.setUpdatesEnabled(False)
        # textChanged → _on_edited fires once per insert; reconnect after so
        # the save-debounce kicks off a single timer rather than churning.
        try:
            self.textChanged.disconnect(self._on_edited)
            _re_on_edited = True
        except TypeError:
            _re_on_edited = False
        try:
            cur = self.textCursor()
            start_block = cur.blockNumber()
            cur.beginEditBlock()
            cur.insertText(text)
            cur.endEditBlock()
            end_block = cur.blockNumber()
        finally:
            if _re_on_edited:
                self.textChanged.connect(self._on_edited)
            self.setUpdatesEnabled(True)
            h._lexer = saved_lexer

        # Fire _on_edited once for the whole paste (autosave debounce, etc.).
        self._on_edited()

        # Schedule background rehighlight over the inserted range. Pad a few
        # blocks before/after to cover multiline-string state spillover.
        pad = 3
        self._schedule_rehighlight_range(
            max(0, start_block - pad),
            end_block + pad,
        )

    def _schedule_rehighlight_range(self, start_block: int, end_block: int) -> None:
        doc = self.document()
        total = doc.blockCount()
        start = max(0, min(start_block, total - 1)) if total > 0 else 0
        end = max(start, min(end_block, total - 1)) if total > 0 else 0
        # Extending pending range: merge so a follow-up paste doesn't restart from scratch.
        if self._rehl_timer.isActive():
            self._rehl_next_block = min(self._rehl_next_block, start)
            self._rehl_end_block = max(self._rehl_end_block, end)
        else:
            self._rehl_next_block = start
            self._rehl_end_block = end
            self._rehl_timer.start()

    def _rehl_tick(self) -> None:
        h = getattr(self, "_highlighter", None)
        doc = self.document()
        if h is None or doc is None or not getattr(h, "_lexer", None):
            self._rehl_timer.stop()
            return
        total = doc.blockCount()
        end = min(self._rehl_next_block + self._REHL_CHUNK_BLOCKS,
                  self._rehl_end_block + 1,
                  total)
        for i in range(self._rehl_next_block, end):
            block = doc.findBlockByNumber(i)
            if block.isValid():
                h.rehighlightBlock(block)
        self._rehl_next_block = end
        if self._rehl_next_block >= min(self._rehl_end_block + 1, total):
            self._rehl_timer.stop()
            if self._typing_mute_until_rehl:
                self._typing_mute_until_rehl = False
                self._typing_mute = False

    def _release_typing_mute_after_load(self):
        """Re-enable typing sounds once load + async rehighlight have settled."""
        if self._rehl_timer.isActive():
            self._typing_mute_until_rehl = True
        else:
            self._typing_mute = False
            self._typing_mute_until_rehl = False

    def has_named_file(self) -> bool:
        return bool(self._file_path)

    def prepare_untitled(self):
        """Blank scratch buffer (no path) — like an empty Notepad tab."""
        self._save_timer.stop()
        self._file_path = ""
        self._typing_mute = True
        self.blockSignals(True)
        self.setPlainText("")
        self.blockSignals(False)
        self._typing_mute = False
        self._highlighter.set_lexer(self._get_lexer(".txt", "scratch.txt"))
        self.setPlaceholderText(
            "Scratch pad — type here. Use Save or Save As to store as a file."
        )

    def save_to_path(self, path: str) -> None:
        """Write current text to *path* and attach this buffer to that file (lexer + autosave)."""
        import os

        abs_path = os.path.abspath(path)
        self._save_timer.stop()
        self._file_path = abs_path
        ext = os.path.splitext(abs_path)[1].lower()
        lexer = self._get_lexer(ext, abs_path)
        self._highlighter.set_lexer(lexer)
        self.setPlaceholderText("")
        self._inhibit_reload = True
        try:
            parent_dir = os.path.dirname(abs_path)
            if parent_dir:
                os.makedirs(parent_dir, exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(self.toPlainText())
            self.file_saved.emit(abs_path)
        except Exception:
            pass
        QTimer.singleShot(500, self._clear_inhibit)

    def save_now(self) -> bool:
        """Persist immediately if a path is set. Returns False when there is nothing to save to."""
        if not self._file_path:
            return False
        self._save_timer.stop()
        self._do_save()
        return True

    def load_file(self, path: str):
        import os
        self._typing_mute = True
        self._typing_mute_until_rehl = False
        self._file_path = path
        self.setPlaceholderText("")
        ext = os.path.splitext(path)[1].lower()
        lexer = self._get_lexer(ext, path)
        self._highlighter.set_lexer(lexer)
        try:
            content = open(path, "r", encoding="utf-8", errors="replace").read()
            self.blockSignals(True)
            large = (
                len(content) >= self._PASTE_FAST_PATH_CHARS
                or content.count("\n") >= self._PASTE_FAST_PATH_LINES
            )
            h = self._highlighter
            saved_lexer = getattr(h, "_lexer", None)
            if large:
                h._lexer = None
                self.setUpdatesEnabled(False)
            self.setPlainText(content)
            if large:
                self.setUpdatesEnabled(True)
                h._lexer = saved_lexer
                end_block = max(0, self.document().blockCount() - 1)
                self._schedule_rehighlight_range(0, end_block)
            self.blockSignals(False)
        except Exception as e:
            self.blockSignals(True)
            self.setPlainText(f"Error reading file: {e}")
            self.blockSignals(False)
        QTimer.singleShot(0, self._release_typing_mute_after_load)

    def reload_from_disk(self):
        if not self._file_path:
            return
        self._typing_mute = True
        self._typing_mute_until_rehl = False
        self._inhibit_reload = True
        cursor = self.textCursor()
        pos = cursor.position()
        scroll_val = self.verticalScrollBar().value()
        try:
            content = open(self._file_path, "r", encoding="utf-8", errors="replace").read()
            self.blockSignals(True)
            # Mirror load_file's large-file fast path: suppress the live
            # pygments lexer during the bulk insert and rehighlight in
            # background chunks. Without this, reloading a big file on a
            # disk change (agent edits, file-watcher reloads) restyles
            # synchronously on the UI thread and stutters.
            large = (
                len(content) >= self._PASTE_FAST_PATH_CHARS
                or content.count("\n") >= self._PASTE_FAST_PATH_LINES
            )
            h = self._highlighter
            saved_lexer = getattr(h, "_lexer", None)
            if large:
                h._lexer = None
                self.setUpdatesEnabled(False)
            self.setPlainText(content)
            if large:
                self.setUpdatesEnabled(True)
                h._lexer = saved_lexer
                end_block = max(0, self.document().blockCount() - 1)
                self._schedule_rehighlight_range(0, end_block)
            self.blockSignals(False)
            cursor.setPosition(min(pos, len(content)))
            self.setTextCursor(cursor)
            self.verticalScrollBar().setValue(scroll_val)
        except Exception:
            pass
        self._inhibit_reload = False
        QTimer.singleShot(0, self._release_typing_mute_after_load)

    def _on_edited(self):
        # User is typing — drop the diff overlay (it's relative to a snapshot
        # that's now stale). Also drop any pulse highlight (positions shift).
        if self._diff_added_lines or self._diff_removed_gutter_lines:
            self.clear_diff_highlights()
        if self._pulse_ranges:
            self.clear_pulse_highlight()
        if self._typing_mute:
            return
        if self._file_path:
            try:
                from tools.workspace_sound_watch import note_viewer_keystroke
                note_viewer_keystroke(self._file_path)
            except Exception:
                pass
            self._save_timer.start(800)  # debounce 800ms

    def _do_save(self):
        if not self._file_path:
            return
        self._inhibit_reload = True
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                f.write(self.toPlainText())
            self.file_saved.emit(self._file_path)
        except Exception:
            pass
        # Brief inhibit window so watchdog doesn't trigger a reload
        QTimer.singleShot(500, self._clear_inhibit)

    def _clear_inhibit(self):
        self._inhibit_reload = False

    @staticmethod
    def _get_lexer(ext: str, path: str):
        try:
            from pygments.lexers import get_lexer_by_name, guess_lexer_for_filename
        except ImportError:
            return None
        ext_map = {
            ".py": "python", ".js": "javascript", ".ts": "typescript",
            ".jsx": "jsx", ".tsx": "tsx",
            ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
            ".java": "java", ".cs": "csharp", ".go": "go",
            ".rs": "rust", ".rb": "ruby", ".php": "php",
            ".sh": "bash", ".bash": "bash", ".zsh": "zsh",
            ".sql": "sql", ".r": "r",
            ".json": "json", ".yaml": "yaml", ".yml": "yaml",
            ".toml": "toml", ".ini": "ini", ".cfg": "ini",
            ".xml": "xml", ".css": "css", ".scss": "scss",
            ".lua": "lua", ".swift": "swift", ".kt": "kotlin",
            ".dart": "dart", ".zig": "zig", ".txt": "text",
            ".ps1": "powershell", ".bat": "batch", ".cmd": "batch",
            ".md": "markdown", ".html": "html", ".htm": "html",
        }
        try:
            if ext in ext_map:
                return get_lexer_by_name(ext_map[ext])
            return guess_lexer_for_filename(path, "")
        except Exception:
            return None

    def refresh_palette(self):
        self._typing_mute = True
        try:
            self._highlighter.refresh_palette()
            # Re-apply diff overlay with current palette colors
            if getattr(self, "_diff_added_lines", None):
                self.set_diff_highlights(
                    self._diff_added_lines,
                    self._diff_removed_gutter_lines or [],
                )
        finally:
            self._typing_mute = False

    # ── Diff visualization ─────────────────────────────────────────────

    def set_diff_highlights(self, added_lines: list[int],
                            removed_gutter_lines: list[int] | None = None) -> None:
        """Highlight *added_lines* (0-based line numbers in the CURRENT buffer)
        with a translucent bright accent overlay. If *removed_gutter_lines* is
        given, those lines get a dimmer translucent overlay — used to mark
        context rows adjacent to an edit so the change is easier to scan.
        Lines are full-width row highlights; theme-neutral (accent, not red/green).
        """
        self._diff_added_lines = list(added_lines or [])
        self._diff_removed_gutter_lines = list(removed_gutter_lines or [])
        self._rebuild_extra_selections()
        # Redraw gutter since diff state changed (affects gutter dots)
        self._gutter.update()

    def clear_diff_highlights(self) -> None:
        """Drop the diff overlay — restores plain syntax-highlighted view."""
        self._diff_added_lines = []
        self._diff_removed_gutter_lines = []
        self._rebuild_extra_selections()
        self._gutter.update()

    def _rebuild_extra_selections(self) -> None:
        """Compose diff overlays + current-line highlight into one selection list.

        Both are QTextEdit.ExtraSelection entries; Qt layers later entries on
        top of earlier ones. We paint the current-line marker LAST so it sits
        above the diff row — but we keep its alpha low so the diff still shows.
        """
        p = PALETTE
        selections = []

        def _row_overlay(line_no: int, fmt: QTextCharFormat) -> None:
            block = self.document().findBlockByNumber(line_no)
            if not block.isValid():
                return
            cursor = QTextCursor(block)
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format = fmt
            selections.append(sel)

        # Dim translucent for removed (ghost) rows — drawn first
        if self._diff_removed_gutter_lines:
            dim_fmt = QTextCharFormat()
            dim_fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
            dim_color = QColor(p.get("accent_soft", p.get("muted_text", "#555555")))
            dim_color.setAlpha(40)
            dim_fmt.setBackground(dim_color)
            for ln in self._diff_removed_gutter_lines:
                _row_overlay(ln, dim_fmt)

        # Bright translucent for added/modified rows
        if self._diff_added_lines:
            add_fmt = QTextCharFormat()
            add_fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
            add_color = QColor(p.get("accent_bright", p["accent"]))
            add_color.setAlpha(110)
            add_fmt.setBackground(add_color)
            for ln in self._diff_added_lines:
                _row_overlay(ln, add_fmt)

        # Pulse-highlight ranges (character-level, not full-width). Drawn
        # above diff overlays so the emphasized text pops even on diff rows.
        if self._pulse_ranges:
            pulse_fmt = QTextCharFormat()
            pulse_color = QColor(p.get("accent_bright", p["accent"]))
            pulse_color.setAlpha(max(0, min(255, int(self._pulse_alpha))))
            pulse_fmt.setBackground(pulse_color)
            doc = self.document()
            for block_no, col, length in self._pulse_ranges:
                block = doc.findBlockByNumber(block_no)
                if not block.isValid():
                    continue
                cursor = QTextCursor(block)
                cursor.setPosition(block.position() + col)
                cursor.setPosition(block.position() + col + length,
                                   QTextCursor.MoveMode.KeepAnchor)
                sel = QTextEdit.ExtraSelection()
                sel.cursor = cursor
                sel.format = pulse_fmt
                selections.append(sel)

        # Current-line marker (sits on top; low alpha so diff still shows)
        if not self.isReadOnly():
            cur_fmt = QTextCharFormat()
            cur_fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
            cur_color = QColor(p.get("accent_soft", p.get("accent_muted", p["accent"])))
            cur_color.setAlpha(22)
            cur_fmt.setBackground(cur_color)
            cur_sel = QTextEdit.ExtraSelection()
            cur_sel.format = cur_fmt
            cur_sel.cursor = self.textCursor()
            cur_sel.cursor.clearSelection()
            selections.append(cur_sel)

        self.setExtraSelections(selections)

    # ── Pulse highlight (for file_show `highlight=` arg) ──────────────

    _PULSE_ALPHA_LOW = 55
    _PULSE_ALPHA_HIGH = 160

    def set_pulse_highlight(self, text: str) -> None:
        """Find every occurrence of *text* in the buffer and pulse-highlight
        them. Case-sensitive literal substring match. Replaces any existing
        pulse ranges. Call with empty string to clear."""
        self.clear_pulse_highlight()
        if not text:
            return
        body = self.toPlainText()
        if not body:
            print(f"[pulse_highlight] buffer is empty, can't highlight", flush=True)
            return
        doc = self.document()
        ranges: list[tuple[int, int, int]] = []
        body_lower = body.lower()
        text_lower = text.lower()
        start = 0
        while True:
            pos = body_lower.find(text_lower, start)
            if pos < 0:
                break
            block = doc.findBlock(pos)
            if not block.isValid():
                break
            col = pos - block.position()
            ranges.append((block.blockNumber(), col, len(text)))
            start = pos + max(1, len(text))
        if not ranges:
            print(f"[pulse_highlight] no matches for {text[:80]!r} "
                  f"(body len={len(body)})", flush=True)
            return
        print(f"[pulse_highlight] {len(ranges)} match(es) for {text[:80]!r}",
              flush=True)
        self._pulse_ranges = ranges
        self._pulse_alpha = self._PULSE_ALPHA_LOW
        self._pulse_dir = 1
        self._pulse_timer.start()
        self._rebuild_extra_selections()
        # Scroll to the first match so the user sees it immediately.
        try:
            self.scroll_to_line(ranges[0][0])
        except Exception:
            pass

    def clear_pulse_highlight(self) -> None:
        self._pulse_timer.stop()
        if self._pulse_ranges:
            self._pulse_ranges = []
            self._rebuild_extra_selections()

    def _pulse_tick(self) -> None:
        step = 8
        self._pulse_alpha += step * self._pulse_dir
        if self._pulse_alpha >= self._PULSE_ALPHA_HIGH:
            self._pulse_alpha = self._PULSE_ALPHA_HIGH
            self._pulse_dir = -1
        elif self._pulse_alpha <= self._PULSE_ALPHA_LOW:
            self._pulse_alpha = self._PULSE_ALPHA_LOW
            self._pulse_dir = 1
        self._rebuild_extra_selections()

    def scroll_to_line(self, line_no: int) -> None:
        """Scroll the viewport so *line_no* (0-based) is roughly centered.

        Deferred via QTimer.singleShot(0, ...) so Qt finishes laying out the
        document after setPlainText() before we ask findBlockByNumber() for
        the block. Without the defer, newly-loaded files silently fail this
        scroll because the document's block count is still stale.
        """
        target = max(0, int(line_no))

        def _do_scroll():
            doc = self.document()
            block = doc.findBlockByNumber(target)
            if not block.isValid():
                # Layout may still be incomplete (very large files). Retry once
                # on a later tick before giving up.
                def _retry():
                    b = self.document().findBlockByNumber(target)
                    if not b.isValid():
                        print(f"[CodeEditor] scroll_to_line: block {target} invalid "
                              f"(doc has {self.document().blockCount()} blocks)")
                        return
                    cursor = QTextCursor(b)
                    self.setTextCursor(cursor)
                    self.centerCursor()
                QTimer.singleShot(50, _retry)
                return
            cursor = QTextCursor(block)
            self.setTextCursor(cursor)
            self.centerCursor()

        QTimer.singleShot(0, _do_scroll)

    # ── Line number gutter + current-line highlight ────────────────────

    def _line_number_width(self) -> int:
        digits = max(2, len(str(max(1, self.blockCount()))))
        fm = self.fontMetrics()
        # Reserve one extra char width for the +/\u2212 diff marker column.
        return 10 + fm.horizontalAdvance("9") * (digits + 1)

    def _update_gutter_width(self, _new_block_count: int = 0) -> None:
        w = self._line_number_width()
        self.setViewportMargins(w, 0, 0, 0)

    def _on_update_request(self, rect, dy: int) -> None:
        if dy != 0:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        from PyQt6.QtCore import QRect
        self._gutter.setGeometry(
            QRect(cr.left(), cr.top(), self._line_number_width(), cr.height())
        )

    def _paint_line_numbers(self, event) -> None:
        p = PALETTE
        painter = QPainter(self._gutter)
        # Background — slightly darker than the editor body
        try:
            from PyQt6.QtGui import QColor as _QC
            bg = _QC(p.get("panel", p.get("panel_alt", "#111111")))
            painter.fillRect(event.rect(), bg)
        except Exception:
            pass

        block = self.firstVisibleBlock()
        block_num = block.blockNumber()
        geom = self.blockBoundingGeometry(block).translated(self.contentOffset())
        top = geom.top()
        bottom = top + self.blockBoundingRect(block).height()
        width = self._gutter.width()
        height = self.fontMetrics().height()

        num_color = QColor(p.get("muted_text", "#888888"))
        active_color = QColor(p.get("accent_bright", p.get("accent", "#61d0ff")))
        removed_color = QColor(p.get("accent_soft", p.get("muted_text", "#888888")))
        added_set = set(self._diff_added_lines or [])
        removed_set = set(self._diff_removed_gutter_lines or [])
        marker_col_w = self.fontMetrics().horizontalAdvance("9")

        painter.setFont(self.font())
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                text = str(block_num + 1)
                is_added = block_num in added_set
                is_removed = block_num in removed_set
                # Line number (right-aligned, leaves room for marker column)
                painter.setPen(
                    active_color if is_added
                    else removed_color if is_removed
                    else num_color)
                painter.drawText(
                    0, int(top), width - marker_col_w - 6, height,
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    text,
                )
                # +/\u2212 marker in its own column after the line number
                if is_added or is_removed:
                    marker = "+" if is_added else "\u2212"
                    painter.setPen(active_color if is_added else removed_color)
                    painter.drawText(
                        width - marker_col_w - 2, int(top), marker_col_w, height,
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                        marker,
                    )
            block = block.next()
            top = bottom
            bottom = top + self.blockBoundingRect(block).height()
            block_num += 1
        painter.end()

    def _paint_current_line_highlight(self) -> None:
        """Redraw extra selections so the current-line marker follows the cursor."""
        self._rebuild_extra_selections()


class _BorderFlashOverlay(QWidget):
    """Transparent overlay that draws a thick colored border over its parent.
    Used by FileViewer.flash_border() to draw attention without restyling
    the viewer's real stylesheet."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._color = QColor(PALETTE.get("accent_bright", PALETTE["accent"]))
        self._thickness = 4
        if parent is not None:
            self.setGeometry(0, 0, parent.width(), parent.height())
            parent.installEventFilter(self)
        self.raise_()

    def set_color(self, color: QColor):
        self._color = color
        self.update()

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self.setGeometry(0, 0, self.parent().width(), self.parent().height())
        return False

    def paintEvent(self, event):
        painter = QPainter(self)
        pen = painter.pen()
        pen.setColor(self._color)
        pen.setWidth(self._thickness)
        painter.setPen(pen)
        # Inset by half the thickness so the border paints fully inside the rect.
        half = self._thickness // 2
        rect = self.rect().adjusted(half, half, -half, -half)
        painter.drawRect(rect)


class _FileViewerTerminalPanel(QFrame):
    """VS Code–style multi-terminal pane: stacked sessions on the left,
    terminal list with +/kill/× controls on the right.
    Reuses ``IntegratedTerminalSession`` from ``ui.terminal_workspace``."""

    def __init__(self, cwd_resolver, collapse_cb, parent=None):
        super().__init__(parent)
        self.setObjectName("FileViewerTerminalPanel")
        self._cwd_resolver = cwd_resolver
        self._collapse_cb = collapse_cb
        from ui.terminal_workspace import IntegratedTerminalSession  # local import — avoids circular
        self._SessionCls = IntegratedTerminalSession
        self._sessions: list = []
        self._tab_seq = 0

        try:
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self._reap_all_shells)
        except Exception:
            pass

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self._stack = QStackedWidget()
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self._stack, stretch=1)

        side = QFrame()
        side.setObjectName("FileViewerTerminalSide")
        side.setFixedWidth(170)
        side_lay = QVBoxLayout(side)
        side_lay.setContentsMargins(0, 0, 0, 0)
        side_lay.setSpacing(0)

        # header row: +, ✂ (kill), stretch, ✕ (hide panel)
        hdr = QHBoxLayout()
        hdr.setContentsMargins(6, 4, 6, 4)
        hdr.setSpacing(4)

        self._add_btn = QPushButton("+")
        self._add_btn.setToolTip("New terminal")
        self._add_btn.setFont(QFont("Consolas", 10))
        self._add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._add_btn.setFixedWidth(24)
        self._add_btn.clicked.connect(self.add_terminal)
        hdr.addWidget(self._add_btn)

        self._kill_btn = QPushButton("✕")
        self._kill_btn.setToolTip("Kill selected terminal")
        self._kill_btn.setFont(QFont("Consolas", 9))
        self._kill_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._kill_btn.setFixedWidth(24)
        self._kill_btn.clicked.connect(self._kill_selected)
        hdr.addWidget(self._kill_btn)

        hdr.addStretch(1)

        self._hide_btn = QPushButton("✕")
        self._hide_btn.setToolTip("Hide terminal panel")
        self._hide_btn.setFont(QFont("Consolas", 10))
        self._hide_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hide_btn.setFixedWidth(22)
        self._hide_btn.clicked.connect(self._request_collapse)
        hdr.addWidget(self._hide_btn)

        self._hdr_w = QWidget()
        self._hdr_w.setLayout(hdr)
        side_lay.addWidget(self._hdr_w)

        self._list = QListWidget()
        self._list.setFont(QFont("Consolas", 9))
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.currentRowChanged.connect(self._on_list_row_changed)
        self._list.itemDoubleClicked.connect(self._rename_item)
        self._list.setToolTip("Double-click a terminal name to rename")
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_list_context_menu)
        side_lay.addWidget(self._list, stretch=1)

        root.addWidget(side)

        self._apply_palette(PALETTE)
        self._title_timer = QTimer(self)
        self._title_timer.setInterval(2000)
        self._title_timer.timeout.connect(self._tick_auto_titles)
        self._title_timer.start()
        self._restore_saved_or_default()

    def _request_collapse(self):
        cb = self._collapse_cb
        if cb:
            cb()

    def _resolve_cwd(self) -> str:
        if self._cwd_resolver:
            try:
                p = (self._cwd_resolver() or "").strip()
                if p and os.path.isdir(p):
                    return os.path.abspath(p)
            except Exception:
                pass
        return os.getcwd()

    def add_terminal(self, label: str | None = None):
        self._tab_seq += 1
        default = label or f"Terminal {self._tab_seq}"
        sess = self._SessionCls(cwd=self._resolve_cwd(), parent=self)
        sess.default_tab_title = default
        sess._on_title_refresh = lambda s=sess: self._sync_title_for_session(s)
        if label:
            sess.set_user_tab_title(label)
        self._sessions.append(sess)
        self._stack.addWidget(sess)
        item = QListWidgetItem(sess.display_title())
        self._list.addItem(item)
        self._list.setCurrentRow(len(self._sessions) - 1)
        QTimer.singleShot(50, sess.focus_terminal)

    # ── Restart persistence ──────────────────────────────────────────
    def persistence_tabs(self, deep: bool = True) -> list[dict]:
        """Snapshot each terminal as {title, cwd, resume, command}."""
        out: list[dict] = []
        for i, sess in enumerate(self._sessions):
            try:
                info = sess.persistence_info(deep=deep)
            except Exception:
                info = {"cwd": "", "resume": None, "command": ""}
            item = self._list.item(i)
            sess = self._sessions[i]
            out.append({
                "title": sess.user_tab_title or (item.text() if item else f"Terminal {i+1}"),
                "user_title": bool(sess.user_tab_title),
                "cwd": info.get("cwd") or "",
                "resume": info.get("resume"),
                "command": info.get("command") or "",
            })
        return out

    def _restore_saved_or_default(self):
        """Recreate last session's terminals (spawned in their cwd, last command
        prefilled); fall back to a single empty terminal."""
        tabs = []
        try:
            from core.terminal_persistence import load_state
            tabs = (load_state().get("file_terminals") or {}).get("tabs") or []
            tabs = [t for t in tabs if isinstance(t, dict)]
        except Exception:
            tabs = []
        if not tabs:
            self.add_terminal()
            return
        for t in tabs:
            self._restore_terminal(
                t.get("cwd", ""), t.get("title", ""),
                t.get("resume"), t.get("command", ""),
                user_title=bool(t.get("user_title")),
            )
        self._list.setCurrentRow(0)

    def _restore_terminal(self, cwd: str, title: str = "",
                          resume: str | None = None, command: str = "",
                          *, user_title: bool = False):
        self._tab_seq += 1
        target = cwd if (cwd and os.path.isdir(cwd)) else self._resolve_cwd()
        label = title or f"Terminal {self._tab_seq}"
        sess = self._SessionCls(cwd=target, parent=self)
        sess.default_tab_title = label
        sess._on_title_refresh = lambda s=sess: self._sync_title_for_session(s)
        if user_title and title:
            sess.set_user_tab_title(title)
        self._sessions.append(sess)
        self._stack.addWidget(sess)
        self._list.addItem(QListWidgetItem(sess.display_title()))
        prefill = resume or command
        if prefill:
            sess.prefill(prefill)
        QTimer.singleShot(50, sess.focus_terminal)

    def _sync_list_title(self, index: int) -> None:
        if not (0 <= index < len(self._sessions)):
            return
        item = self._list.item(index)
        if item is not None:
            item.setText(self._sessions[index].display_title())

    def _sync_title_for_session(self, session) -> None:
        try:
            self._sync_list_title(self._sessions.index(session))
        except ValueError:
            pass

    def _tick_auto_titles(self) -> None:
        for i, sess in enumerate(self._sessions):
            if sess.user_tab_title:
                continue
            item = self._list.item(i)
            new = sess.display_title()
            if item is None or item.text() != new:
                self._sync_list_title(i)

    def _on_list_row_changed(self, row: int):
        if 0 <= row < len(self._sessions):
            self._stack.setCurrentWidget(self._sessions[row])
            QTimer.singleShot(0, self._sessions[row].focus_terminal)

    def _kill_selected(self):
        row = self._list.currentRow()
        if row < 0 or row >= len(self._sessions):
            return
        self._kill_at(row)

    def _kill_at(self, row: int):
        sess = self._sessions.pop(row)
        try: sess.stop()
        except Exception: pass
        self._stack.removeWidget(sess)
        sess.deleteLater()
        self._list.takeItem(row)
        if not self._sessions:
            self.add_terminal()

    def _rename_item(self, item: QListWidgetItem):
        row = self._list.row(item)
        if row < 0 or row >= len(self._sessions):
            return
        sess = self._sessions[row]
        new, ok = QInputDialog.getText(
            self, "Rename terminal", "Name:", text=sess.display_title(),
        )
        if ok and new.strip():
            sess.set_user_tab_title(new.strip())
            item.setText(sess.display_title())

    def _on_list_context_menu(self, pos):
        idx = self._list.indexAt(pos).row()
        if idx < 0:
            return
        p = PALETTE
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {p['panel']};
                color: {p['text']};
                border: 1px solid {p['border']};
                font-family: Consolas; font-size: 9pt;
                padding: 4px 0;
            }}
            QMenu::item {{ padding: 4px 18px; background: transparent; color: {p['text']}; }}
            QMenu::item:selected {{ background: {p['accent_muted']}; color: {p['text']}; }}
            QMenu::separator {{ height: 1px; background: {p['border']}; margin: 4px 8px; }}
        """)
        menu.addAction("Rename", lambda: self._rename_item(self._list.item(idx)))
        menu.addAction("Kill", lambda: self._kill_at(idx))
        menu.exec(self._list.viewport().mapToGlobal(pos))

    def focus_active_input(self):
        row = self._list.currentRow()
        if 0 <= row < len(self._sessions):
            self._sessions[row].focus_terminal()

    def _reap_all_shells(self):
        for s in list(self._sessions):
            try: s.stop()
            except Exception: pass

    def apply_theme(self):
        self._apply_palette(PALETTE)
        for s in self._sessions:
            try: s.apply_theme()
            except Exception: pass

    def _apply_palette(self, p: dict):
        btn_ss = (
            f"QPushButton {{ color:{p['accent']}; background:{p['panel']};"
            f" border:1px solid {p['border']}; border-radius:0; padding:1px 6px; }}"
            f"QPushButton:hover {{ background:{p['accent_muted']}; }}"
        )
        self._add_btn.setStyleSheet(btn_ss)
        self._kill_btn.setStyleSheet(btn_ss)
        self._hide_btn.setStyleSheet(
            f"color:{p['muted_text']}; background:transparent; border:none;"
        )
        self._hdr_w.setStyleSheet(
            f"background:{p['panel']}; border-bottom:1px solid {p['border']};"
        )
        self.setStyleSheet(
            f"QFrame#FileViewerTerminalPanel {{ background:{p['panel_alt']}; border:none; }}"
            f"QFrame#FileViewerTerminalSide {{ background:{p['panel']};"
            f" border-left:1px solid {p['border']}; }}"
        )
        self._list.setStyleSheet(f"""
            QListWidget {{
                background: {p['panel']};
                color: {p['text']};
                border: none;
                outline: none;
                font-family: Consolas;
                font-size: 9pt;
            }}
            QListWidget::item {{ padding: 4px 8px; }}
            QListWidget::item:selected {{ background: {p['accent_muted']}; color: {p['text']}; }}
            QListWidget::item:hover {{ background: {p['accent_muted']}; }}
        """)


class FileViewer(QFrame):
    """Right-panel tabbed file viewer/editor for text, code, PDF, and DOCX files.
    Each open file gets its own tab. Supports live auto-reload via watchdog."""

    # Emitted when the agent edits a file — ChatWindow listens and expands the
    # right splitter if it's collapsed.
    attention_requested = pyqtSignal()

    # Emitted when the explorer tree root changes (user navigation / Open
    # folder). ChatWindow listens and persists viewer state so the navigation
    # spot survives restart — previously a root change only got saved if the
    # periodic auto-save happened to fire afterward, so quitting promptly lost it.
    explorer_root_changed = pyqtSignal(str)

    # Extensions that wrap by default (readable prose)
    _DEFAULT_WRAP_EXTS = {".txt", ".md", ".pdf", ".docx", ".rst", ".log"}
    _SHOW_EDIT_DEBOUNCE_MS = 180
    _LARGE_DIFF_BYTES = 80_000
    _LARGE_DIFF_LINES = 1500
    _WRAP_PREFS_PATH = os.path.join("data", "file_viewer_wrap.json")
    # Extensions with a rich "preview" rendering (toggle in the toolbar).
    # PDF gets the toggle too — "edit" shows the text extract, "preview"
    # shows rendered page images via PyMuPDF.
    _PREVIEWABLE_EXTS = {".md", ".html", ".htm", ".rst", ".pdf"}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FileViewer")
        self._tabs: list[dict] = []  # [{path, text_widget, watcher}, ...]
        self._wrap_prefs: dict[str, bool] = {}
        self._load_wrap_prefs()
        self._wrap_frozen = False  # defer reflow during splitter/window resize
        self._untitled_counter = 0
        self._path_pulse_timer = None  # QTimer while copy-flash runs
        self._path_pulse_step = 0
        # Tabs currently blinking for user attention (idx -> {timer, state})
        self._blinking_tabs: dict[int, dict] = {}
        self._explorer_root: str = ""
        self._explorer_user_pinned: bool = False  # user chose root → block auto-sync
        self._explorer_tree_saved_w = 220
        self._explorer_delegate: _ExplorerTreeDelegate | None = None
        self._no_file_icons = _NoFileIconsProvider()
        self._syncing_explorer_toggle = False
        self._pending_show_edits: dict[str, tuple[str, int]] = {}
        self._show_edit_timer = QTimer(self)
        self._show_edit_timer.setSingleShot(True)
        self._show_edit_timer.setInterval(self._SHOW_EDIT_DEBOUNCE_MS)
        self._show_edit_timer.timeout.connect(self._flush_pending_show_edits)
        self._build_ui()
        # Overlay widget used by flash_border() — sits on top, draws a thick
        # colored border, transparent to mouse events. Hidden by default.
        self._border_flash = _BorderFlashOverlay(self)
        self._border_flash.hide()

    def _load_wrap_prefs(self):
        try:
            import json
            if os.path.isfile(self._WRAP_PREFS_PATH):
                with open(self._WRAP_PREFS_PATH, "r") as f:
                    self._wrap_prefs = json.load(f)
        except Exception:
            self._wrap_prefs = {}

    def _save_wrap_prefs(self):
        try:
            import json
            os.makedirs(os.path.dirname(self._WRAP_PREFS_PATH), exist_ok=True)
            with open(self._WRAP_PREFS_PATH, "w") as f:
                json.dump(self._wrap_prefs, f)
        except Exception:
            pass

    def _get_wrap_for_ext(self, ext: str) -> bool:
        if ext in self._wrap_prefs:
            return self._wrap_prefs[ext]
        return ext.lower() in self._DEFAULT_WRAP_EXTS

    def _apply_wrap_to_widget(self, widget, editable: bool, wrap: bool):
        # Non-text widgets (MediaViewer, etc.) have no line-wrap concept.
        if not hasattr(widget, "setLineWrapMode"):
            return
        if editable:
            mode = QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap
            widget.setLineWrapMode(mode)
        else:
            from PyQt6.QtWidgets import QTextEdit
            mode = QTextEdit.LineWrapMode.WidgetWidth if wrap else QTextEdit.LineWrapMode.NoWrap
            widget.setLineWrapMode(mode)

    def set_wrap_frozen(self, frozen: bool):
        """While True, lock each editor's *viewport* width so WidgetWidth wrap does not reflow.

        Wrap mode is unchanged; line breaks stay tied to the frozen viewport width until release,
        then one reflow runs at the new size (horizontal scroll may appear if the panel shrinks).
        """
        if getattr(self, "_wrap_frozen", False) == frozen:
            return
        self._wrap_frozen = frozen
        if frozen:
            self._sync_frozen_width_locks()
        else:
            for tab in self._tabs:
                w = tab["widget"]
                vp = w.viewport() if hasattr(w, "viewport") else None
                if vp is not None:
                    vp.setMinimumWidth(0)
                    vp.setMaximumWidth(16777215)
                else:
                    w.setMinimumWidth(0)
                    w.setMaximumWidth(16777215)
                    w.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
                tab.pop("_freeze_lock_w", None)
            self._refresh_all_tab_wrap_modes()

    def _sync_frozen_width_locks(self):
        """Lock the editor viewport width so WidgetWidth wrap keeps the same line breaks.

        The outer tab widget can still shrink; horizontal scroll appears inside the editor
        instead of re-wrapping every pixel while the splitter moves.
        """
        if not getattr(self, "_wrap_frozen", False):
            return
        pane = max(80, self._tab_widget.width() - 4)
        for tab in self._tabs:
            w = tab["widget"]
            vp = w.viewport() if hasattr(w, "viewport") else None
            if vp is not None:
                cur = vp.width()
                lock = max(80, min(cur if cur >= 80 else pane, pane))
                vp.setMinimumWidth(lock)
                vp.setMaximumWidth(lock)
            else:
                cur = w.width()
                lock = max(80, min(cur if cur >= 80 else pane, pane))
                w.setMinimumWidth(lock)
                w.setMaximumWidth(lock)
                w.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
            tab["_freeze_lock_w"] = lock

    def _refresh_all_tab_wrap_modes(self):
        """Apply saved wrap prefs per tab."""
        for tab in self._tabs:
            ext = self._ext_for_tab(tab)
            want = self._get_wrap_for_ext(ext)
            self._apply_wrap_to_widget(tab["widget"], tab.get("editable", False), want)

    def _build_ui(self):
        p = PALETTE
        self.setStyleSheet(f"""
            QFrame#FileViewer {{
                background: {p['panel_alt']};
                border: none;
            }}
        """)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header bar: file actions, centered path hint, preview / close
        header = QHBoxLayout()
        header.setContentsMargins(8, 4, 8, 4)
        header.setSpacing(6)

        self._open_btn = QPushButton("Open")
        self._open_btn.setFont(QFont("Consolas", 8))
        self._open_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._open_btn.setToolTip("Open a file in a new tab, or open a folder to set as the Explorer root")
        self._open_btn.setStyleSheet(f"color:{p['accent']};background:transparent;border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")
        self._open_btn.clicked.connect(self._show_open_menu)
        header.addWidget(self._open_btn)

        self._new_btn = QPushButton("New")
        self._new_btn.setFont(QFont("Consolas", 8))
        self._new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._new_btn.setStyleSheet(f"color:{p['accent']};background:transparent;border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")
        self._new_btn.clicked.connect(self._add_untitled_tab)
        header.addWidget(self._new_btn)

        self._save_btn = QPushButton("Save")
        self._save_btn.setFont(QFont("Consolas", 8))
        self._save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_btn.setStyleSheet(f"color:{p['accent']};background:transparent;border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")
        self._save_btn.clicked.connect(self._save_current_tab)
        header.addWidget(self._save_btn)

        self._save_as_btn = QPushButton("Save As…")
        self._save_as_btn.setFont(QFont("Consolas", 8))
        self._save_as_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._save_as_btn.setStyleSheet(f"color:{p['accent']};background:transparent;border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")
        self._save_as_btn.clicked.connect(self._save_as_current_tab)
        header.addWidget(self._save_as_btn)

        self._tree_sidebar_btn = QPushButton("Explorer")
        self._tree_sidebar_btn.setCheckable(True)
        self._tree_sidebar_btn.setChecked(True)
        self._tree_sidebar_btn.setFont(QFont("Consolas", 8))
        self._tree_sidebar_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._tree_sidebar_btn.setToolTip(
            "Show or hide the file tree (or drag the splitter handle)"
        )
        self._apply_explorer_toggle_style(p, True)
        self._tree_sidebar_btn.toggled.connect(self._on_explorer_sidebar_toggled)
        header.addWidget(self._tree_sidebar_btn)

        # Path sits in a stretch slot that spans Save As → Preview, but the
        # label itself is Maximum-width + centered so decorations (e.g.
        # underline) hug the text, not the full toolbar gap.
        self._path_slot = QWidget()
        self._path_slot.setObjectName("FileViewerPathSlot")
        path_row = QHBoxLayout(self._path_slot)
        path_row.setContentsMargins(0, 0, 0, 0)
        path_row.setSpacing(0)
        path_row.addStretch(1)
        self._path_label = _PathHeaderHintLabel()
        self._path_label.setObjectName("FileViewerPathHint")
        self._path_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._path_label.setFont(QFont("Consolas", 7))
        self._apply_path_label_base_style()
        self._path_label.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed
        )
        self._path_label.setWordWrap(False)
        self._path_label.setMinimumWidth(0)
        self._path_label.clicked_with_path.connect(self._on_path_header_hint_clicked)
        path_row.addWidget(self._path_label, 0, Qt.AlignmentFlag.AlignVCenter)
        path_row.addStretch(1)
        self._path_slot.setStyleSheet("QWidget#FileViewerPathSlot { background: transparent; }")
        header.addWidget(self._path_slot, stretch=1)

        # Accept / Cancel for pending agent edits — only visible when the
        # current tab is showing a diff overlay. Navigating away from the
        # diffed tab acts like Accept (the write is already on disk).
        # Both buttons live in the accent family (no red) and pulse their
        # brightness while visible to draw attention.
        self._accept_btn = QPushButton("Accept")
        self._accept_btn.setFont(QFont("Consolas", 8))
        self._accept_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._accept_btn.setToolTip("Commit the agent's edits on this tab (clears the diff highlight).")
        self._accept_btn.clicked.connect(self._accept_current_diff)
        self._accept_btn.hide()
        header.addWidget(self._accept_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setFont(QFont("Consolas", 8))
        self._cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cancel_btn.setToolTip("Revert this file to its pre-edit contents.")
        self._cancel_btn.clicked.connect(self._cancel_current_diff)
        self._cancel_btn.hide()
        header.addWidget(self._cancel_btn)

        # Pulse timer: when the diff buttons are visible, alternate their
        # accent between normal and accent_bright every ~600ms so they
        # visibly ask for attention. Stops while the buttons are hidden.
        self._diff_pulse_state = False
        self._diff_pulse_timer = QTimer(self)
        self._diff_pulse_timer.setInterval(600)
        self._diff_pulse_timer.timeout.connect(self._diff_pulse_tick)
        self._apply_diff_btn_styles()  # seed the initial (non-bright) look

        # Preview toggle — only visible when the current tab's extension is
        # something we can render nicely (markdown, HTML, PDF, etc.).
        self._preview_btn = QPushButton("Preview")
        self._preview_btn.setFont(QFont("Consolas", 8))
        self._preview_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._preview_btn.setCheckable(True)
        self._preview_btn.setChecked(False)
        self._preview_btn.setStyleSheet(self._wrap_btn_style(p, False))
        self._preview_btn.toggled.connect(self._on_preview_toggled)
        self._preview_btn.hide()
        header.addWidget(self._preview_btn)

        # Terminal toggle — opens a VS Code-style bottom terminal panel
        # (lazy-built on first toggle, reuses TerminalWorkspacePanel).
        self._terminal_btn = QPushButton("Terminal")
        self._terminal_btn.setCheckable(True)
        self._terminal_btn.setChecked(False)
        self._terminal_btn.setFont(QFont("Consolas", 8))
        self._terminal_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._terminal_btn.setToolTip(
            "Show or hide a terminal panel at the bottom of the file viewer"
        )
        self._terminal_btn.setStyleSheet(self._wrap_btn_style(p, False))
        self._terminal_btn.toggled.connect(self._on_terminal_toggled)
        header.addWidget(self._terminal_btn)

        self._close_btn = QPushButton("\u2715")
        self._close_btn.setFont(QFont("Consolas", 10))
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.setFixedWidth(20)
        self._close_btn.setStyleSheet(f"color:{p['muted_text']};background:transparent;border:none;")
        self._close_btn.clicked.connect(self._close_viewer)
        header.addWidget(self._close_btn)

        self._header_w = QWidget()
        self._header_w.setLayout(header)
        # No border on the header row itself — a full-width bottom border reads as a
        # giant "underline" across the path stretch between Save As and Preview.
        self._header_w.setStyleSheet(f"background:{p['panel']};")

        # Thin rule between toolbar and tab bar (replaces header border-bottom).
        self._header_sep = QFrame()
        self._header_sep.setFixedHeight(1)
        self._header_sep.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._header_sep.setStyleSheet(
            f"background:{p['border']};border:none;max-height:1px;"
        )

        layout.addWidget(self._header_w)
        layout.addWidget(self._header_sep)

        # Tab bar (styled to match theme) — only the editor stack lives right of the tree.
        self._tab_widget = ThemedClosableTabWidget()
        self._tab_widget.setTabsClosable(True)
        self._tab_widget.tabCloseRequested.connect(self._close_tab)
        self._tab_widget.currentChanged.connect(self._on_tab_changed)
        self._tab_widget.setFont(QFont("Consolas", 8))
        self._apply_tab_styles(p)

        self._editor_panel = QWidget()
        self._editor_panel.setObjectName("FileViewerEditorPanel")
        editor_layout = QVBoxLayout(self._editor_panel)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        editor_layout.addWidget(self._tab_widget, stretch=1)

        ac = QColor(p["accent"])
        ar, ag, ab = ac.red(), ac.green(), ac.blue()
        self._file_tree_split = QSplitter(Qt.Orientation.Horizontal)
        self._file_tree_split.setObjectName("FileViewerMainSplit")
        self._file_tree_split.setChildrenCollapsible(True)
        self._file_tree_split.setHandleWidth(3)
        self._file_tree_split.splitterMoved.connect(self._on_file_tree_split_moved)
        self._file_tree_split.setStyleSheet(f"""
            QSplitter#FileViewerMainSplit::handle:horizontal {{
                background: {p['border']};
            }}
            QSplitter#FileViewerMainSplit::handle:horizontal:hover {{
                background: rgba({ar},{ag},{ab},0.35);
            }}
        """)

        self._tree_frame = QFrame()
        self._tree_frame.setObjectName("FileViewerTreePane")
        self._tree_frame.setMinimumWidth(0)
        tree_lay = QVBoxLayout(self._tree_frame)
        tree_lay.setContentsMargins(0, 0, 0, 0)
        tree_lay.setSpacing(0)

        self._fs_model = QFileSystemModel(self)
        self._fs_model.setReadOnly(True)
        self._fs_model.setIconProvider(self._no_file_icons)
        self._fs_model.setFilter(
            QDir.Filter.AllDirs
            | QDir.Filter.Files
            | QDir.Filter.Hidden
            | QDir.Filter.NoDotAndDotDot
        )
        self._file_tree = _ExplorerTreeView(self._tree_frame)
        self._file_tree.setObjectName("FileViewerExplorer")
        self._file_tree.setModel(self._fs_model)
        self._file_tree.setHeaderHidden(True)
        self._file_tree.setAnimated(False)
        self._file_tree.setRootIsDecorated(False)
        self._file_tree.setIndentation(16)
        self._file_tree.setUniformRowHeights(True)
        self._file_tree.setMouseTracking(True)
        self._file_tree.viewport().setMouseTracking(True)
        self._file_tree.setIconSize(QSize(0, 0))
        self._file_tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        self._file_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        for _col in (1, 2, 3):
            self._file_tree.setColumnHidden(_col, True)
        _hdr = self._file_tree.header()
        _hdr.setStretchLastSection(False)
        _hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self._file_tree.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )
        self._file_tree.setMinimumWidth(0)
        self._file_tree.setFont(QFont("Consolas", 8))
        self._file_tree.clicked.connect(self._on_file_tree_clicked)
        self._file_tree.doubleClicked.connect(self._on_file_tree_double_clicked)
        self._file_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._file_tree.customContextMenuRequested.connect(self._on_file_tree_context_menu)
        self._explorer_delegate = _ExplorerTreeDelegate(self._file_tree, self._file_tree)
        self._file_tree.setItemDelegate(self._explorer_delegate)
        self._file_tree.expanded.connect(lambda *_: self._file_tree.viewport().update())
        self._file_tree.collapsed.connect(lambda *_: self._file_tree.viewport().update())
        tree_lay.addWidget(self._file_tree)
        self._apply_file_tree_theme(p)

        init_root = os.path.abspath(os.getcwd())
        self._explorer_root = init_root
        self._fs_model.setRootPath(init_root)
        self._file_tree.setRootIndex(self._fs_model.index(init_root))

        # Vertical splitter — editor on top, terminal on bottom (lazy). This
        # sits on the right side of the tree split so the file tree spans
        # full height like VS Code's explorer.
        self._terminal_panel = None  # built lazily on first toggle
        self._terminal_saved_h = 220
        self._terminal_split = QSplitter(Qt.Orientation.Vertical)
        self._terminal_split.setObjectName("FileViewerTerminalSplit")
        self._terminal_split.setChildrenCollapsible(True)
        self._terminal_split.setHandleWidth(3)
        self._terminal_split.splitterMoved.connect(self._on_terminal_split_moved)
        self._terminal_split.setStyleSheet(f"""
            QSplitter#FileViewerTerminalSplit::handle:vertical {{
                background: {p['border']};
            }}
            QSplitter#FileViewerTerminalSplit::handle:vertical:hover {{
                background: rgba({ar},{ag},{ab},0.35);
            }}
        """)
        self._terminal_split.addWidget(self._editor_panel)
        self._terminal_split.setCollapsible(0, False)
        self._terminal_split.setStretchFactor(0, 1)

        self._file_tree_split.addWidget(self._tree_frame)
        self._file_tree_split.addWidget(self._terminal_split)
        self._file_tree_split.setCollapsible(0, True)
        self._file_tree_split.setCollapsible(1, False)
        self._file_tree_split.setStretchFactor(0, 0)
        self._file_tree_split.setStretchFactor(1, 1)
        self._file_tree_split.setSizes([220, 640])

        layout.addWidget(self._file_tree_split, stretch=1)

        sc_save = QShortcut(QKeySequence.StandardKey.Save, self)
        sc_save.activated.connect(self._save_current_tab)
        sc_save_as = QShortcut(QKeySequence.StandardKey.SaveAs, self)
        sc_save_as.activated.connect(self._save_as_current_tab)

        self._ensure_scratch_tab()
        QTimer.singleShot(0, self._update_path_header_label)

    def _apply_file_tree_theme(self, p: dict) -> None:
        tree = getattr(self, "_file_tree", None)
        frame = getattr(self, "_tree_frame", None)
        if tree is None:
            return
        dlg = getattr(self, "_explorer_delegate", None)
        if dlg is not None:
            dlg.apply_palette(p)
        # Item chrome (selection, hover) is painted by _ExplorerTreeDelegate — keep QSS transparent.
        tree.setStyleSheet(
            f"""
            QTreeView#FileViewerExplorer {{
                background: {p['panel']};
                color: {p['text']};
                border: none;
                outline: none;
            }}
            QTreeView#FileViewerExplorer::item {{
                background: transparent;
            }}
            QTreeView#FileViewerExplorer::item:selected {{
                background: transparent;
                color: {p['text']};
            }}
            QTreeView#FileViewerExplorer::item:hover {{
                background: transparent;
            }}
            QTreeView#FileViewerExplorer::item:selected:active {{
                background: transparent;
            }}
            """
        )
        if frame is not None:
            frame.setStyleSheet(
                f"""
                QFrame#FileViewerTreePane {{
                    background: {p['panel']};
                    border: none;
                    border-right: 1px solid {p['border']};
                }}
                """
            )

    def set_explorer_root(self, path: str | None, *, pinned: bool = False,
                          auto: bool = False) -> None:
        """Show *path* as the root of the sidebar file tree.

        pinned=True  → the user explicitly chose this (Open-folder, or restoring
                       their last saved spot); it sticks until they change it.
        auto=True    → a programmatic workspace sync; it is SKIPPED once the user
                       has pinned a root, so per-turn syncs no longer yank the
                       tree back and lose the user's navigation.
        """
        # Respect a user-pinned root against automatic workspace syncs.
        if auto and getattr(self, "_explorer_user_pinned", False):
            return
        raw = (path or "").strip()
        if not raw or not os.path.isdir(raw):
            raw = os.getcwd()
        abs_root = os.path.abspath(raw)
        if pinned:
            self._explorer_user_pinned = True
        if abs_root == self._explorer_root:
            return
        self._explorer_root = abs_root
        model = getattr(self, "_fs_model", None)
        tree = getattr(self, "_file_tree", None)
        if model is None or tree is None:
            return
        model.setRootPath(abs_root)
        tree.setRootIndex(model.index(abs_root))
        cur = self._current_path
        if cur:
            self._sync_file_tree_selection(cur)
        # Persist the new spot. A bare auto workspace-sync isn't worth saving for
        # (it's derived), but anything else — user navigation or restore — is.
        if not auto:
            try:
                self.explorer_root_changed.emit(abs_root)
            except Exception:
                pass

    def reset_explorer_pin(self) -> None:
        """Forget any user-pinned root so the next workspace sync applies. Called
        when switching conversations (each conv restores its own saved spot)."""
        self._explorer_user_pinned = False

    # ── Remote workspace (mirror a peer conversation's files) ──────────────
    # The explorer/editor are local-disk based, so we mirror the host's
    # workspace into a local "shadow" folder of placeholder files. The tree is
    # the host's structure; a file's real content is fetched on first open and
    # edits are pushed back to the host on save. Everything else (tabs, editor,
    # watcher, diff overlays) keeps working unchanged.

    def _remote_cache_base(self) -> str:
        base = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "remote_cache")
        os.makedirs(base, exist_ok=True)
        return base

    def enter_remote_workspace(self, peer_url: str, conv_id: str,
                               peer_name: str) -> None:
        """Point the explorer at a PEER conversation's workspace (read+write)."""
        import re as _re
        import shutil
        safe = lambda s: _re.sub(r"[^A-Za-z0-9_.-]", "_", s or "x")[:60]
        root = os.path.join(self._remote_cache_base(), safe(peer_name), safe(conv_id))
        try:
            if os.path.isdir(root):
                shutil.rmtree(root, ignore_errors=True)   # fresh shadow each time
            os.makedirs(root, exist_ok=True)
        except Exception:
            pass
        self._remote_ws = {"peer_url": peer_url, "conv_id": conv_id,
                           "peer_name": peer_name, "root": os.path.abspath(root),
                           "dirs": set(), "files": set()}
        if not getattr(self, "_remote_expand_wired", False):
            self._file_tree.expanded.connect(self._on_remote_expand)
            self._remote_expand_wired = True
        self._materialize_remote_dir("")            # top-level listing
        self.set_explorer_root(root, pinned=True)

    def exit_remote_workspace(self) -> None:
        self._remote_ws = None

    def _remote_rel(self, path: str) -> str | None:
        """Path relative to the active shadow root (posix), or None if `path`
        is not part of the remote workspace."""
        rw = getattr(self, "_remote_ws", None)
        if not rw:
            return None
        try:
            rel = os.path.relpath(os.path.abspath(path), rw["root"])
        except Exception:
            return None
        if rel.startswith("..") or os.path.isabs(rel):
            return None
        return "" if rel == "." else rel.replace("\\", "/")

    def _materialize_remote_dir(self, rel: str) -> None:
        """Fetch a remote directory listing and create local placeholder
        dirs/empty-files so the QFileSystemModel tree shows the host structure."""
        rw = getattr(self, "_remote_ws", None)
        if not rw or rel in rw["dirs"]:
            return
        from core.network import peer_fs_list
        from PyQt6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, res, _ = peer_fs_list(rw["peer_url"], rw["conv_id"], rel)
        finally:
            QApplication.restoreOverrideCursor()
        if not ok or not res:
            return
        rw["dirs"].add(rel)
        base = os.path.join(rw["root"], rel) if rel else rw["root"]
        for e in res.get("entries", []):
            child = os.path.join(base, e["name"])
            try:
                if e.get("is_dir"):
                    os.makedirs(child, exist_ok=True)
                elif not os.path.exists(child):
                    open(child, "a", encoding="utf-8").close()
            except Exception:
                pass

    def _on_remote_expand(self, index) -> None:
        rw = getattr(self, "_remote_ws", None)
        if not rw:
            return
        try:
            path = self._fs_model.filePath(index)
        except Exception:
            return
        rel = self._remote_rel(path)
        if rel is not None:
            self._materialize_remote_dir(rel)

    def _fetch_remote_file(self, path: str) -> None:
        """Pull the host's current content into a shadow file before it's read."""
        rw = getattr(self, "_remote_ws", None)
        rel = self._remote_rel(path)
        if not rw or rel is None or rel in rw["files"]:
            return
        from core.network import peer_fs_read
        from PyQt6.QtWidgets import QApplication
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, text, _ = peer_fs_read(rw["peer_url"], rw["conv_id"], rel)
        finally:
            QApplication.restoreOverrideCursor()
        if ok:
            try:
                # newline="" → write the host's bytes verbatim (no \n→\r\n
                # re-translation), so the shadow file is byte-identical to the
                # host file and the editor renders it exactly as a local one.
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(text)
                rw["files"].add(rel)
            except Exception:
                pass

    def _on_remote_editor_saved(self, path: str) -> None:
        """Push a saved shadow file back to the host (off the UI thread). The
        host commits it in its workspace."""
        rw = getattr(self, "_remote_ws", None)
        rel = self._remote_rel(path)
        if not rw or rel is None:
            return
        try:
            content = open(path, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            return
        import threading
        from core.network import peer_fs_write
        peer_url, conv_id = rw["peer_url"], rw["conv_id"]
        threading.Thread(
            target=lambda: peer_fs_write(peer_url, conv_id, rel, content),
            daemon=True, name="remote-fs-write").start()

    def _remote_fs_call(self, kind: str, *args) -> None:
        """Propagate an explorer mutation (rename/delete/mkdir/new file) to the
        host, off the UI thread. No-op when not in a remote workspace."""
        rw = getattr(self, "_remote_ws", None)
        if not rw:
            return
        import threading
        from core import network as _net
        peer_url, conv_id = rw["peer_url"], rw["conv_id"]

        def _do():
            try:
                if kind == "rename":
                    _net.peer_fs_rename(peer_url, conv_id, args[0], args[1])
                elif kind == "delete":
                    _net.peer_fs_delete(peer_url, conv_id, args[0])
                elif kind == "mkdir":
                    _net.peer_fs_mkdir(peer_url, conv_id, args[0])
                elif kind == "write":
                    _net.peer_fs_write(peer_url, conv_id, args[0], args[1])
            except Exception:
                pass
        threading.Thread(target=_do, daemon=True, name=f"remote-fs-{kind}").start()

    def _remote_push_rename(self, old_path: str, new_path: str) -> None:
        a, b = self._remote_rel(old_path), self._remote_rel(new_path)
        if a is not None and b is not None:
            self._remote_fs_call("rename", a, b)

    def _remote_push_delete(self, path: str) -> None:
        rel = self._remote_rel(path)
        if rel is not None:
            self._remote_fs_call("delete", rel)

    def _remote_push_mkdir(self, path: str) -> None:
        rel = self._remote_rel(path)
        if rel is not None:
            self._remote_fs_call("mkdir", rel)

    def _remote_push_new_file(self, path: str) -> None:
        rel = self._remote_rel(path)
        if rel is None:
            return
        rw = getattr(self, "_remote_ws", None)
        if rw is not None:
            rw["files"].add(rel)   # treat as fetched so open() keeps the new empty file
        self._remote_fs_call("write", rel, "")

    def get_explorer_root(self) -> str:
        """Current sidebar tree root — saved per conversation so the user's
        navigation spot survives restarts."""
        return getattr(self, "_explorer_root", "") or ""

    def get_expanded_dirs(self) -> list[str]:
        """Directories currently expanded in the tree, so we can restore the
        user's drill-down on reload."""
        out: list[str] = []
        tree = getattr(self, "_file_tree", None)
        model = getattr(self, "_fs_model", None)
        if tree is None or model is None:
            return out
        root_idx = tree.rootIndex()

        def _walk(parent):
            for r in range(model.rowCount(parent)):
                idx = model.index(r, 0, parent)
                if tree.isExpanded(idx):
                    p = model.filePath(idx)
                    if p:
                        out.append(p)
                    _walk(idx)
        try:
            _walk(root_idx)
        except Exception:
            pass
        return out[:200]  # bound it

    def restore_expanded_dirs(self, dirs: list[str]) -> None:
        """Re-expand previously open tree folders (best-effort; skips missing)."""
        tree = getattr(self, "_file_tree", None)
        model = getattr(self, "_fs_model", None)
        if tree is None or model is None or not dirs:
            return
        for d in dirs:
            if d and os.path.isdir(d):
                try:
                    tree.setExpanded(model.index(d), True)
                except Exception:
                    pass

    def _on_file_tree_clicked(self, index):
        if not index.isValid():
            return
        path = self._fs_model.filePath(index)
        if os.path.isdir(path):
            self._file_tree.setExpanded(index, not self._file_tree.isExpanded(index))
        elif os.path.isfile(path):
            self.load_file(path)

    def _on_file_tree_double_clicked(self, index):
        """Double-click a folder → make it the explorer root (pinned, persisted),
        so tree navigation actually changes the saved root. Double-click a file
        just opens it (single-click already does, but be tolerant)."""
        if not index.isValid():
            return
        path = self._fs_model.filePath(index)
        if os.path.isdir(path):
            # Pinned: this is an explicit user navigation, it should stick and
            # survive restart (a workspace auto-sync won't override it).
            self.set_explorer_root(path, pinned=True)
        elif os.path.isfile(path):
            self.load_file(path)

    def _sync_file_tree_selection(self, path: str | None) -> None:
        tree = getattr(self, "_file_tree", None)
        model = getattr(self, "_fs_model", None)
        root = getattr(self, "_explorer_root", "") or ""
        if (
            tree is None
            or model is None
            or not path
            or not os.path.isfile(path)
            or not root
        ):
            return
        abs_path = os.path.abspath(path)
        try:
            common = os.path.commonpath(
                [os.path.normcase(abs_path), os.path.normcase(root)]
            )
        except ValueError:
            return
        if os.path.normcase(common) != os.path.normcase(root):
            return
        ix = model.index(abs_path)
        if ix.isValid():
            tree.setCurrentIndex(ix)
            tree.scrollTo(ix)

    def _is_under_explorer_root(self, path: str) -> bool:
        root = (self._explorer_root or "").strip()
        if not root or not path:
            return False
        try:
            r = os.path.normcase(os.path.abspath(root))
            p = os.path.normcase(os.path.abspath(path))
            return os.path.commonpath([p, r]) == r
        except ValueError:
            return False

    def _explorer_validate_basename(self, name: str) -> str | None:
        n = (name or "").strip()
        if not n or n in (".", ".."):
            return "Invalid name."
        if any(ch in n for ch in _WIN_INVALID_NAME):
            return "Name cannot contain: < > : \" / \\ | ? *"
        if sys.platform == "win32" and (n.endswith(" ") or n.endswith(".")):
            return "Name cannot end with a space or dot on Windows."
        return None

    def _on_file_tree_context_menu(self, pos: QPoint) -> None:
        idx = self._file_tree.indexAt(pos)
        root = (self._explorer_root or "").strip()
        if not root or not os.path.isdir(root):
            return

        menu = QMenu(self)
        p = PALETTE
        menu.setStyleSheet(f"""
            QMenu {{
                background: {p['panel']};
                color: {p['text']};
                border: 1px solid {p['border']};
                font-family: Consolas; font-size: 9pt;
                padding: 4px 0;
            }}
            QMenu::item {{
                padding: 4px 18px;
                background: transparent;
                color: {p['text']};
            }}
            QMenu::item:selected {{ background: {p['accent_muted']}; color: {p['text']}; }}
            QMenu::item:disabled {{ color: {p['border']}; }}
            QMenu::separator {{ height: 1px; background: {p['border']}; margin: 4px 8px; }}
        """)
        target_path: str | None = None
        is_dir = False
        parent_for_new = os.path.abspath(root)

        if idx.isValid():
            path = os.path.abspath(self._fs_model.filePath(idx))
            if self._is_under_explorer_root(path):
                target_path = path
                is_dir = os.path.isdir(path)
                parent_for_new = path if is_dir else os.path.dirname(path)

        if target_path is not None:
            act_rename = menu.addAction("Rename…")
            act_rename.triggered.connect(
                functools.partial(self._explorer_rename, target_path)
            )
            act_del = menu.addAction("Move to Recycle Bin…")
            act_del.triggered.connect(
                functools.partial(self._explorer_delete, target_path, is_dir)
            )
            menu.addSeparator()

        act_nf = menu.addAction("New file…")
        act_nf.triggered.connect(
            functools.partial(self._explorer_new_file, parent_for_new)
        )
        act_nd = menu.addAction("New folder…")
        act_nd.triggered.connect(
            functools.partial(self._explorer_new_folder, parent_for_new)
        )

        menu.exec(self._file_tree.viewport().mapToGlobal(pos))

    def _explorer_rename(self, path: str) -> None:
        if not self._is_under_explorer_root(path) or not os.path.exists(path):
            return
        old_name = os.path.basename(path.rstrip("/\\"))
        parent = os.path.dirname(path)
        new_name, ok = QInputDialog.getText(
            self, "Rename", "New name:", text=old_name
        )
        if not ok:
            return
        err = self._explorer_validate_basename(new_name)
        if err:
            QMessageBox.warning(self, "Rename", err)
            return
        new_name = new_name.strip()
        dest = os.path.join(parent, new_name)
        if os.path.normcase(os.path.abspath(dest)) == os.path.normcase(
            os.path.abspath(path)
        ):
            return
        if os.path.exists(dest):
            QMessageBox.warning(
                self, "Rename", "A file or folder with that name already exists."
            )
            return
        is_dir = os.path.isdir(path)
        try:
            os.rename(path, dest)
        except OSError as e:
            QMessageBox.warning(self, "Rename", str(e))
            return
        self._relocate_tabs_after_rename(path, dest, is_dir)
        self._remote_push_rename(path, dest)      # propagate to host if remote
        self._ping_explorer_fs_model()
        self._sync_file_tree_selection(self._current_path)

    def _relocate_tabs_after_rename(
        self, old_path: str, new_path: str, was_dir: bool
    ) -> None:
        o = os.path.abspath(old_path)
        n = os.path.abspath(new_path)
        on = os.path.normcase(o)
        touched: list[dict] = []
        for tab in self._tabs:
            tp = (tab.get("path") or "").strip()
            if not tp:
                continue
            tap = os.path.abspath(tp)
            tnc = os.path.normcase(tap)
            if tnc == on:
                tab["path"] = n
                touched.append(tab)
            elif was_dir:
                try:
                    common = os.path.commonpath([tap, o])
                except ValueError:
                    continue
                if os.path.normcase(common) == on:
                    rel = os.path.relpath(tap, o)
                    tab["path"] = os.path.join(n, rel)
                    touched.append(tab)
        for tab in touched:
            self._stop_watching_tab(tab)
            p = tab.get("path") or ""
            if p and os.path.isfile(p):
                self._start_watching_tab(tab)
            idx = self._tabs.index(tab)
            self._tab_widget.setTabToolTip(idx, p)
            self._tab_widget.setTabText(idx, os.path.basename(p) or "untitled")

    def _close_tabs_under_path(self, path: str, is_dir: bool) -> None:
        """Close editor tabs pointing at *path* or inside *path* when it is a directory."""
        ap = os.path.normcase(os.path.abspath(path))
        to_close: list[int] = []
        for i, tab in enumerate(self._tabs):
            tp = (tab.get("path") or "").strip()
            if not tp:
                continue
            t_abs = os.path.normcase(os.path.abspath(tp))
            if t_abs == ap:
                to_close.append(i)
            elif is_dir:
                pref = ap + os.sep
                if t_abs.startswith(pref):
                    to_close.append(i)
        for i in reversed(sorted(set(to_close))):
            self._close_tab(i)

    def _explorer_delete(self, path: str, is_dir: bool) -> None:
        if not self._is_under_explorer_root(path) or not os.path.exists(path):
            return
        root_abs = os.path.normcase(os.path.abspath(self._explorer_root))
        if os.path.normcase(os.path.abspath(path)) == root_abs:
            QMessageBox.warning(
                self, "Delete", "Cannot delete the workspace root folder."
            )
            return
        name = os.path.basename(path.rstrip("/\\"))
        msg = (
            f"Move “{name}” and all of its contents to the Recycle Bin?"
            if is_dir
            else f"Move “{name}” to the Recycle Bin?"
        )
        if (
            QMessageBox.question(
                self,
                "Move to Recycle Bin",
                msg,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._close_tabs_under_path(path, is_dir=is_dir)
        ok, err = _move_path_to_recycle_bin(path)
        if not ok:
            QMessageBox.warning(
                self,
                "Delete",
                err or "Could not move to the Recycle Bin.",
            )
            return
        self._remote_push_delete(path)            # delete on host too if remote
        self._ping_explorer_fs_model()
        self._sync_file_tree_selection(self._current_path)

    def _explorer_new_file(self, parent_dir: str) -> None:
        pd = os.path.abspath(parent_dir)
        if not self._is_under_explorer_root(pd) or not os.path.isdir(pd):
            return
        name, ok = QInputDialog.getText(self, "New file", "File name (with extension):")
        if not ok:
            return
        err = self._explorer_validate_basename(name)
        if err:
            QMessageBox.warning(self, "New file", err)
            return
        name = name.strip()
        dest = os.path.join(pd, name)
        if os.path.exists(dest):
            QMessageBox.warning(self, "New file", "That path already exists.")
            return
        try:
            dest = os.path.abspath(dest)
            open(dest, "a", encoding="utf-8").close()
        except OSError as e:
            QMessageBox.warning(self, "New file", str(e))
            return
        self._remote_push_new_file(dest)          # create on host too if remote
        self._ping_explorer_fs_model()
        self.load_file(dest)

    def _explorer_new_folder(self, parent_dir: str) -> None:
        pd = os.path.abspath(parent_dir)
        if not self._is_under_explorer_root(pd) or not os.path.isdir(pd):
            return
        name, ok = QInputDialog.getText(self, "New folder", "Folder name:")
        if not ok:
            return
        err = self._explorer_validate_basename(name)
        if err:
            QMessageBox.warning(self, "New folder", err)
            return
        name = name.strip()
        dest = os.path.join(pd, name)
        if os.path.exists(dest):
            QMessageBox.warning(self, "New folder", "That path already exists.")
            return
        try:
            os.makedirs(dest, exist_ok=False)
        except OSError as e:
            QMessageBox.warning(self, "New folder", str(e))
            return
        self._remote_push_mkdir(dest)             # create on host too if remote
        self._ping_explorer_fs_model()
        dest_abs = os.path.abspath(dest)
        nix = self._fs_model.index(dest_abs)
        if nix.isValid():
            pix = self._fs_model.index(os.path.dirname(dest_abs))
            if pix.isValid():
                self._file_tree.expand(pix)
            self._file_tree.setCurrentIndex(nix)
            self._file_tree.scrollTo(nix)

    def _ping_explorer_fs_model(self) -> None:
        """Nudge QFileSystemModel after external FS changes (rename/delete/create)."""
        root = (self._explorer_root or "").strip()
        if not root or self._fs_model is None:
            return
        parent = os.path.dirname(os.path.abspath(root))
        self._fs_model.setRootPath(parent)
        self._fs_model.setRootPath(os.path.abspath(root))
        self._file_tree.setRootIndex(self._fs_model.index(os.path.abspath(root)))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_path_header_label()

    def _apply_path_label_base_style(self) -> None:
        lbl = getattr(self, "_path_label", None)
        if lbl is None:
            return
        p = PALETTE
        hint_color = p.get("accent_soft", p["muted_text"])
        lbl.setStyleSheet(
            f"color:{hint_color};background:transparent;padding:0 6px;border:none;"
        )

    def _apply_path_label_peak_style(self) -> None:
        lbl = getattr(self, "_path_label", None)
        if lbl is None:
            return
        p = PALETTE
        peak = p.get("accent_bright", p["accent"])
        lbl.setStyleSheet(
            f"color:{peak};background:transparent;padding:0 6px;border:none;"
        )

    def _on_path_header_hint_clicked(self, path: str) -> None:
        if not (path and path.strip()):
            return
        QApplication.clipboard().setText(path)
        _reveal_file_in_os_file_manager(path)
        if self._path_pulse_timer is None:
            self._path_pulse_timer = QTimer(self)
            self._path_pulse_timer.timeout.connect(self._path_header_copy_pulse_tick)
        self._path_pulse_timer.stop()
        self._path_pulse_step = 0
        self._apply_path_label_peak_style()
        self._path_pulse_timer.start(90)

    def _path_header_copy_pulse_tick(self) -> None:
        self._path_pulse_step += 1
        n = self._path_pulse_step
        if n == 1:
            self._apply_path_label_base_style()
        elif n == 2:
            self._apply_path_label_peak_style()
        elif n == 3:
            self._apply_path_label_base_style()
            if self._path_pulse_timer is not None:
                self._path_pulse_timer.stop()
        else:
            if self._path_pulse_timer is not None:
                self._path_pulse_timer.stop()
            self._apply_path_label_base_style()

    def _update_path_header_label(self) -> None:
        """Single-line path; uses full stretch width. ElideMiddle only if wider than the label."""
        lbl = getattr(self, "_path_label", None)
        if lbl is None:
            return
        try:
            hl = self._header_w.layout()
            if hl is not None:
                hl.activate()
            ps = getattr(self, "_path_slot", None)
            if ps is not None and ps.layout() is not None:
                ps.layout().activate()
        except Exception:
            pass
        if self._path_pulse_timer is not None and self._path_pulse_timer.isActive():
            self._path_pulse_timer.stop()
            self._apply_path_label_base_style()
        idx = self._tab_widget.currentIndex()
        if not (0 <= idx < len(self._tabs)):
            if isinstance(lbl, _PathHeaderHintLabel):
                lbl.set_path_for_display("", "")
            else:
                lbl.setText("")
            lbl.setToolTip("")
            lbl.setMinimumWidth(0)
            lbl.setMaximumWidth(16777215)
            return
        path = (self._tabs[idx].get("path") or "").strip()
        if not path:
            full_for_click = ""
            text = "Scratch buffer — not saved to disk"
            tip = "No file path yet. Use Save As to write to disk."
        else:
            full_for_click = os.path.normpath(path)
            text = full_for_click
            tip = (
                f"{full_for_click}\n\n"
                "Click: copy path to clipboard and open this location in the file manager."
            )
        # Elide using the path *slot* width (stable); label width is set to
        # text metrics afterward so Qt cannot paint a link-style underline
        # across the whole stretch cell.
        pad = 14
        slot = getattr(self, "_path_slot", None)
        slot_budget = max(0, (slot.width() if slot else 0) - pad)
        if slot_budget > pad + 8 and text:
            fm = lbl.fontMetrics()
            if fm.horizontalAdvance(text) > slot_budget:
                text = fm.elidedText(
                    text, Qt.TextElideMode.ElideMiddle, max(slot_budget, 48)
                )
        lbl.setTextFormat(Qt.TextFormat.PlainText)
        if isinstance(lbl, _PathHeaderHintLabel):
            lbl.set_path_for_display(full_for_click, text)
        else:
            lbl.setText(text)
        lbl.setToolTip(tip)
        self._apply_path_label_base_style()
        # Hug text width: fusion / styles may underline the full QLabel rect
        # when the widget is stretched; force width to rendered text + padding.
        if not (text and text.strip()):
            lbl.setMinimumWidth(0)
            lbl.setMaximumWidth(16777215)
        else:
            lbl.adjustSize()
            shw = max(1, lbl.sizeHint().width())
            cap = shw
            if slot is not None and slot.width() > 16:
                cap = min(shw, max(1, slot.width() - 8))
            lbl.setFixedWidth(int(cap))

    def _apply_tab_styles(self, p: dict):
        self._tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background: {p['panel_alt']};
            }}
            QTabBar::tab {{
                background: {p['panel']};
                color: {p['muted_text']};
                border: 1px solid {p['border']};
                border-bottom: none;
                padding: 3px 10px;
                margin-right: 1px;
                font-family: Consolas;
                font-size: 8pt;
            }}
            QTabBar::tab:selected {{
                background: {p['panel_alt']};
                color: {p['accent']};
                border-bottom: 2px solid {p['accent']};
            }}
            QTabBar::tab:hover {{
                color: {p['accent_bright']};
            }}
        """)
        self._tab_widget.set_close_palette(p)

    @staticmethod
    def _wrap_btn_style(p: dict, checked: bool) -> str:
        ac = p['accent']
        if checked:
            return (f"color:{p['background']};background:{ac};border:1px solid {ac};"
                    f"border-radius:3px;padding:2px 8px;")
        return (f"color:{p['muted_text']};background:transparent;"
                f"border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")

    def _apply_explorer_toggle_style(
        self, p: dict, checked: bool | None = None
    ) -> None:
        btn = self._tree_sidebar_btn
        if checked is None:
            checked = btn.isChecked()
        btn.setStyleSheet(self._wrap_btn_style(p, checked))

    def _on_explorer_sidebar_toggled(self, checked: bool) -> None:
        self._apply_explorer_toggle_style(PALETTE, checked)
        if self._syncing_explorer_toggle:
            return
        self._syncing_explorer_toggle = True
        try:
            sp = self._file_tree_split
            sizes = sp.sizes()
            total = sum(sizes) if sizes else 0
            if total < 50:
                total = max(sp.width(), self.width() - 16, 320)
            if not checked:
                if sizes and sizes[0] > 12:
                    self._explorer_tree_saved_w = max(int(sizes[0]), 120)
                sp.setSizes([0, total])
            else:
                left = min(
                    max(self._explorer_tree_saved_w, 140),
                    max(total * 2 // 3, 140),
                )
                sp.setSizes([left, max(total - left, 100)])
        finally:
            self._syncing_explorer_toggle = False

    def _ensure_terminal_panel(self):
        """Lazily build and attach the bottom terminal panel."""
        if self._terminal_panel is not None:
            return self._terminal_panel
        panel = _FileViewerTerminalPanel(
            cwd_resolver=lambda: (self._explorer_root or os.getcwd()),
            collapse_cb=self._collapse_terminal_from_panel,
            parent=self._terminal_split,
        )
        self._terminal_panel = panel
        self._terminal_split.addWidget(panel)
        self._terminal_split.setStretchFactor(1, 0)
        return panel

    def save_terminal_state(self, deep: bool = True):
        """Persist the file-viewer terminals (global) to terminal_state.json.
        No-op if the panel was never opened — keeps the prior saved state."""
        panel = getattr(self, "_terminal_panel", None)
        if panel is None:
            return
        try:
            from core.terminal_persistence import load_state, save_state
            state = load_state()
            state["file_terminals"] = {"tabs": panel.persistence_tabs(deep=deep)}
            save_state(state)
        except Exception:
            pass

    def _collapse_terminal_from_panel(self):
        """Called when the terminal panel's own × button is clicked."""
        if self._terminal_btn.isChecked():
            self._terminal_btn.setChecked(False)
        else:
            self._on_terminal_toggled(False)

    def _on_terminal_toggled(self, checked: bool) -> None:
        p = PALETTE
        self._terminal_btn.setStyleSheet(self._wrap_btn_style(p, checked))
        sp = self._terminal_split
        if checked:
            self._ensure_terminal_panel()
            sizes = sp.sizes()
            total = sum(sizes) if sizes else 0
            if total < 50:
                total = max(sp.height(), self.height() - 40, 320)
            bot = min(
                max(self._terminal_saved_h, 140),
                max(total * 2 // 3, 140),
            )
            sp.setSizes([max(total - bot, 100), bot])
            if self._terminal_panel is not None:
                QTimer.singleShot(0, self._terminal_panel.focus_active_input)
            return
        # collapse: remember current height, then hide the bottom pane
        sizes = sp.sizes()
        if len(sizes) >= 2 and sizes[1] > 12:
            self._terminal_saved_h = max(int(sizes[1]), 140)
        total = sum(sizes) if sizes else max(sp.height(), 320)
        sp.setSizes([total, 0])

    def _on_terminal_split_moved(self, pos: int, index: int) -> None:
        sizes = self._terminal_split.sizes()
        if len(sizes) < 2:
            return
        bot = sizes[1]
        if bot > 12:
            self._terminal_saved_h = int(bot)
        want_checked = bot > 12
        if self._terminal_btn.isChecked() == want_checked:
            return
        self._terminal_btn.blockSignals(True)
        self._terminal_btn.setChecked(want_checked)
        self._terminal_btn.blockSignals(False)
        self._terminal_btn.setStyleSheet(self._wrap_btn_style(PALETTE, want_checked))

    def _on_file_tree_split_moved(self, pos: int, index: int) -> None:
        if self._syncing_explorer_toggle:
            return
        sizes = self._file_tree_split.sizes()
        if len(sizes) < 2:
            return
        sz0 = sizes[0]
        if sz0 > 12:
            self._explorer_tree_saved_w = int(sz0)
        want_checked = sz0 > 12
        if self._tree_sidebar_btn.isChecked() == want_checked:
            return
        self._tree_sidebar_btn.blockSignals(True)
        self._tree_sidebar_btn.setChecked(want_checked)
        self._tree_sidebar_btn.blockSignals(False)
        self._apply_explorer_toggle_style(PALETTE, want_checked)

    def _ext_for_tab(self, tab: dict) -> str:
        path = tab.get("path") or ""
        if not path:
            return ".txt"
        return os.path.splitext(path)[1].lower()

    def _on_tab_changed(self, idx: int):
        """Update preview toggle and path hint when switching tabs."""
        # Auto-accept any pending diff on the tab we're leaving — the write
        # is already on disk, so navigating away is implicit approval. The
        # highlight/gutter overlay is cleared so it doesn't follow us back
        # the next time this tab is visited.
        prev = getattr(self, "_prev_tab_idx", -1)
        if 0 <= prev < len(self._tabs) and prev != idx:
            if self._tabs[prev].get("diff_pending"):
                self._clear_tab_diff(prev)
        self._prev_tab_idx = idx

        if 0 <= idx < len(self._tabs):
            tab = self._tabs[idx]
            # Preview button visibility + state reflects the active tab
            previewable = tab.get("container") is not None
            self._preview_btn.setVisible(previewable)
            if previewable:
                mode = tab.get("view_mode", "edit")
                self._preview_btn.blockSignals(True)
                self._preview_btn.setChecked(mode == "preview")
                self._preview_btn.setStyleSheet(
                    self._wrap_btn_style(PALETTE, mode == "preview"))
                self._preview_btn.blockSignals(False)
                # Keep preview in sync with any edits that happened while
                # the user was on another tab.
                if mode == "preview":
                    self._refresh_preview(tab)
            # User visited this tab — stop its blink if any
            self._stop_tab_blink(idx)
        if getattr(self, "_wrap_frozen", False):
            QTimer.singleShot(0, self._sync_frozen_width_locks)
        self._refresh_diff_buttons()
        self._update_path_header_label()
        if 0 <= idx < len(self._tabs):
            self._sync_file_tree_selection(self._tabs[idx].get("path") or "")

    # ── Agent-edit visualization ────────────────────────────────────────

    @staticmethod
    def _compute_diff_display(original: str, current: str
                              ) -> tuple[str, list[int], list[int]]:
        """Build a unified-diff-style interleaved display of *original* and
        *current*. Returns (display_text, added_line_indices, removed_line_indices)
        where indices are 0-based line numbers within display_text.

        - `added` lines come from the CURRENT buffer — shown with the bright
          highlight + (+) gutter prefix.
        - `removed` lines come from the ORIGINAL buffer — shown as GHOST rows
          injected into the display, with dim highlight + (\u2212) gutter prefix.

        Ghost lines are NOT part of the real file. The Accept/Cancel flow
        strips them back out; while they're present the widget is held
        read-only so the user can't accidentally save them.
        """
        import difflib
        orig_lines = original.splitlines()
        curr_lines = current.splitlines()
        matcher = difflib.SequenceMatcher(
            a=orig_lines, b=curr_lines, autojunk=False)
        display: list[str] = []
        added: list[int] = []
        removed: list[int] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                display.extend(curr_lines[j1:j2])
            elif tag == "insert":
                for line in curr_lines[j1:j2]:
                    added.append(len(display))
                    display.append(line)
            elif tag == "delete":
                for line in orig_lines[i1:i2]:
                    removed.append(len(display))
                    display.append(line)
            elif tag == "replace":
                # Show removed first (from original), then added (from current)
                for line in orig_lines[i1:i2]:
                    removed.append(len(display))
                    display.append(line)
                for line in curr_lines[j1:j2]:
                    added.append(len(display))
                    display.append(line)
        return "\n".join(display), added, removed

    def show_edit(self, path: str, original: str, surface: bool = True) -> None:
        """Open *path* in a tab and overlay the diff against *original*.

        When ``surface`` is True (a user-initiated reveal) the viewer also
        switches its active tab to this file, surfaces the panel, and blinks the
        tab. When False (the ambient agent-edit path) it loads + applies the diff
        overlay in the BACKGROUND only — no tab switch, no surface — so the user
        isn't yanked away from whatever they're looking at; the diff is simply
        ready for when they click through from the in-chat card.

        Bursts of tool edits coalesce per path. A pending surface=True wins over
        surface=False so an explicit reveal is never downgraded by a later edit.
        """
        if not path:
            print("[FileViewer] show_edit: refused — empty path")
            return

        abs_path = os.path.abspath(path)
        prev = self._pending_show_edits.get(abs_path)
        want_surface = surface or bool(prev and prev[2])
        self._pending_show_edits[abs_path] = (original or "", 4, want_surface)
        self._show_edit_timer.start()

    def _flush_pending_show_edits(self) -> None:
        pending = dict(self._pending_show_edits)
        self._pending_show_edits.clear()
        for abs_path, entry in pending.items():
            original, attempts = entry[0], entry[1]
            surface = entry[2] if len(entry) > 2 else True
            self._show_edit_attempt(abs_path, original, attempts_left=attempts,
                                    surface=surface)

    def _show_edit_attempt(self, abs_path: str, original: str, attempts_left: int,
                           surface: bool = True) -> None:
        if not os.path.isfile(abs_path):
            if attempts_left > 0:
                QTimer.singleShot(50, lambda: self._show_edit_attempt(
                    abs_path, original, attempts_left - 1, surface))
                return
            print(f"[FileViewer] show_edit: gave up — file never appeared on disk: {abs_path!r}")
            return

        idx = self._find_tab_index_for_path(abs_path)
        if idx < 0:
            try:
                self.load_file(abs_path)
            except Exception as e:
                print(f"[FileViewer] show_edit: load_file raised for {abs_path!r}: {e}")
                import traceback; traceback.print_exc()
            idx = self._find_tab_index_for_path(abs_path)
        if idx < 0:
            print(f"[FileViewer] show_edit: load_file failed to create a tab for {abs_path!r}")
            # Still surface the panel so the user knows something happened —
            # even if we can't render the diff, they can manually re-open.
            if surface:
                self.attention_requested.emit()
            return

        tab = self._tabs[idx]
        widget = tab.get("widget")
        # If a prior pending diff is still visible (ghost rows, readonly), clear
        # it and reload the fresh current file content before computing the new
        # diff. Otherwise the stale ghosted buffer would corrupt the compare.
        if tab.get("diff_pending") and isinstance(widget, CodeEditor):
            self._clear_tab_diff(idx)
            try:
                widget.load_file(abs_path)
            except Exception as e:
                print(f"[FileViewer] show_edit: reload after clear_tab_diff failed: {e}")

        # Compute + apply diff overlay (if the file has a prior version)
        if original and isinstance(widget, CodeEditor):
            try:
                current = widget.toPlainText()
                try:
                    file_bytes = os.path.getsize(abs_path)
                except OSError:
                    file_bytes = len(current.encode("utf-8", errors="replace"))
                if (
                    file_bytes > self._LARGE_DIFF_BYTES
                    or current.count("\n") > self._LARGE_DIFF_LINES
                ):
                    # Full interleaved diff + setPlainText stalls the UI on
                    # large files — surface the tab and skip the overlay.
                    pass
                else:
                    display_text, added, removed = self._compute_diff_display(
                        original, current)
                    # Inject ghost (removed) lines by replacing the buffer with the
                    # interleaved display. blockSignals keeps _on_edited from firing
                    # and tripping clear_diff_highlights.
                    if display_text != current:
                        widget.blockSignals(True)
                        widget.setPlainText(display_text)
                        widget.blockSignals(False)
                    widget.set_diff_highlights(added, removed)
                    widget.setReadOnly(True)  # Accept / Cancel only while pending
                    tab["diff_added"] = added
                    tab["diff_removed"] = removed
                    tab["diff_original"] = original
                    tab["diff_has_ghosts"] = bool(removed)
                    tab["diff_pending"] = True
                    if added or removed:
                        # Scroll to the first change so the user sees it immediately.
                        widget.scroll_to_line((added or removed)[0])
            except Exception as e:
                print(f"[FileViewer] show_edit diff error: {e}")
                import traceback; traceback.print_exc()

        # Surface the panel + switch to this tab + start blink — only on an
        # explicit reveal (surface=True). The ambient agent-edit path keeps the
        # diff overlay ready in the background WITHOUT stealing the user's view.
        if not surface:
            return
        self.attention_requested.emit()
        try:
            self._tab_widget.setCurrentIndex(idx)
        except Exception as e:
            print(f"[FileViewer] setCurrentIndex({idx}) failed: {e}")
        try:
            self._start_tab_blink(idx)
        except Exception as e:
            print(f"[FileViewer] start_tab_blink failed: {e}")
        self._refresh_diff_buttons()
        # If the tab is currently in preview mode, re-render so the ribbon
        # and any content changes land in the rendered view.
        if tab.get("view_mode") == "preview":
            try:
                self._refresh_preview(tab)
            except Exception as e:
                print(f"[FileViewer] refresh_preview failed: {e}")

    def _refresh_diff_buttons(self):
        """Show/hide Accept + Cancel based on the current tab's pending-diff state."""
        idx = self._tab_widget.currentIndex()
        pending = (0 <= idx < len(self._tabs)
                   and bool(self._tabs[idx].get("diff_pending")))
        self._accept_btn.setVisible(pending)
        self._cancel_btn.setVisible(pending)
        # Start/stop the attention pulse alongside visibility.
        if pending:
            if not self._diff_pulse_timer.isActive():
                self._diff_pulse_state = False
                self._diff_pulse_timer.start()
                self._apply_diff_btn_styles()
        else:
            if self._diff_pulse_timer.isActive():
                self._diff_pulse_timer.stop()
            self._diff_pulse_state = False
            self._apply_diff_btn_styles()

    @staticmethod
    def _diff_btn_style(p: dict, filled: bool, bright: bool) -> str:
        """Shared styling for Accept/Cancel. `filled` = primary (Accept);
        outlined = secondary (Cancel). Both use the accent color family —
        no red. `bright` swaps accent → accent_bright for the pulse peak."""
        color = p.get("accent_bright" if bright else "accent", "#61d0ff")
        if filled:
            return (
                f"color:{p['background']};background:{color};"
                f"border:1px solid {color};border-radius:3px;"
                f"padding:2px 8px;font-weight:bold;"
            )
        return (
            f"color:{color};background:transparent;"
            f"border:1px solid {color};border-radius:3px;"
            f"padding:2px 8px;font-weight:bold;"
        )

    def _apply_diff_btn_styles(self):
        p = PALETTE
        bright = self._diff_pulse_state
        self._accept_btn.setStyleSheet(self._diff_btn_style(p, filled=True, bright=bright))
        self._cancel_btn.setStyleSheet(self._diff_btn_style(p, filled=False, bright=bright))

    def _diff_pulse_tick(self):
        self._diff_pulse_state = not self._diff_pulse_state
        self._apply_diff_btn_styles()

    def _clear_tab_diff(self, idx: int):
        """Drop the diff overlay + pending state for a tab without touching disk.
        If ghost (removed) lines were injected into the buffer, strip them first
        so the editable buffer matches what's on disk."""
        if not (0 <= idx < len(self._tabs)):
            return
        tab = self._tabs[idx]
        widget = tab.get("widget")
        if isinstance(widget, CodeEditor):
            try:
                if tab.get("diff_has_ghosts"):
                    removed_set = set(tab.get("diff_removed") or [])
                    if removed_set:
                        lines = widget.toPlainText().splitlines()
                        clean = [L for i, L in enumerate(lines)
                                 if i not in removed_set]
                        widget.blockSignals(True)
                        widget.setPlainText("\n".join(clean))
                        widget.blockSignals(False)
                widget.clear_diff_highlights()
                widget.setReadOnly(False)
            except Exception:
                pass
        tab.pop("diff_added", None)
        tab.pop("diff_removed", None)
        tab.pop("diff_original", None)
        tab.pop("diff_has_ghosts", None)
        tab["diff_pending"] = False
        # If the preview is showing, drop the pending-diff ribbon.
        if tab.get("view_mode") == "preview":
            self._refresh_preview(tab)

    def _accept_current_diff(self):
        """Commit the pending edit: just drop the overlay (write is already applied)."""
        idx = self._tab_widget.currentIndex()
        self._clear_tab_diff(idx)
        self._refresh_diff_buttons()

    def _cancel_current_diff(self):
        """Revert the pending edit by writing the pre-edit content back to disk,
        reloading the tab contents, and clearing the overlay."""
        idx = self._tab_widget.currentIndex()
        if not (0 <= idx < len(self._tabs)):
            return
        tab = self._tabs[idx]
        if not tab.get("diff_pending"):
            return
        original = tab.get("diff_original", "")
        path = tab.get("path", "")
        widget = tab.get("widget")
        if path and os.path.isfile(path):
            try:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(original)
            except Exception as e:
                print(f"[FileViewer] cancel diff write failed: {e}")
                return
        if isinstance(widget, CodeEditor):
            try:
                # Block the edited signal while we swap contents so it doesn't
                # get mistaken for a user edit (which would clear the overlay
                # and mark the buffer dirty).
                widget.blockSignals(True)
                widget.setPlainText(original)
                widget.blockSignals(False)
            except Exception:
                pass
        self._clear_tab_diff(idx)
        self._refresh_diff_buttons()

    def _find_tab_index_for_path(self, abs_path: str) -> int:
        for i, tab in enumerate(self._tabs):
            tp = tab.get("path") or ""
            if not tp:
                continue
            try:
                if os.path.normcase(os.path.abspath(tp)) == os.path.normcase(abs_path):
                    return i
            except Exception:
                continue
        return -1

    def blink_tree_path(self, path: str, times: int = 3) -> None:
        """Briefly flash a file's row in the explorer tree to point at a just-
        edited file — WITHOUT changing the active file or the persistent
        selection. Used when the user is already on the File tab so the signal
        is 'this one changed' rather than yanking their view to it."""
        try:
            abs_path = os.path.abspath(path)
            ix = self._fs_model.index(abs_path)
            if not ix.isValid():
                return
            tree = self._file_tree
            try:
                tree.scrollTo(ix)
            except Exception:
                pass
            sm = tree.selectionModel()
            if sm is None:
                return
            from PyQt6.QtCore import QItemSelectionModel, QTimer as _QTimer
            sel = QItemSelectionModel.SelectionFlag
            prev = list(sm.selectedRows()) if hasattr(sm, "selectedRows") else []
            state = {"n": 0}

            def _tick():
                try:
                    if state["n"] % 2 == 0:
                        sm.select(ix, sel.Select | sel.Rows)
                    else:
                        sm.select(ix, sel.Deselect | sel.Rows)
                except Exception:
                    return
                state["n"] += 1
                if state["n"] >= times * 2:
                    try:
                        sm.clearSelection()
                        for pix in prev:
                            sm.select(pix, sel.Select | sel.Rows)
                    except Exception:
                        pass
                    return
                _QTimer.singleShot(180, _tick)

            _tick()
        except Exception:
            pass

    def _start_tab_blink(self, idx: int) -> None:
        """Alternate the tab text color until the user visits the tab."""
        if idx in self._blinking_tabs:
            return  # already blinking
        timer = QTimer(self)
        timer.setInterval(450)
        timer.timeout.connect(lambda: self._tab_blink_tick(idx))
        self._blinking_tabs[idx] = {"state": 0, "timer": timer}
        timer.start()
        # Fire one tick immediately so the blink is visible without waiting
        self._tab_blink_tick(idx)

    def pulse_highlight_current_tab(self, text: str) -> None:
        """Forward a pulse-highlight request to the currently-active tab's
        CodeEditor (if any). No-op if the active tab isn't a code editor
        (e.g., PDF preview, read-only QTextBrowser)."""
        if not text:
            return
        idx = self._tab_widget.currentIndex()
        if not (0 <= idx < len(self._tabs)):
            return
        widget = self._tabs[idx].get("widget")
        if isinstance(widget, CodeEditor):
            try:
                widget.set_pulse_highlight(text)
            except Exception as e:
                print(f"[FileViewer] pulse_highlight error: {e}")

    def flash_border(self, times: int = 5) -> None:
        """Blink the whole viewer's outline a bounded number of times as a
        'hey, look here' cue. Draws a colored border overlay on top of the
        viewer; self-terminating after `times` on/off cycles."""
        if times <= 0 or not hasattr(self, "_border_flash"):
            return
        overlay = self._border_flash
        overlay.set_color(QColor(PALETTE.get("accent_bright", PALETTE["accent"])))
        overlay.setGeometry(0, 0, self.width(), self.height())
        overlay.raise_()
        total_ticks = int(times) * 2
        timer = QTimer(self)
        timer.setInterval(220)
        state = {"count": 0, "on": True}

        def _tick():
            if state["on"]:
                overlay.show()
                overlay.raise_()
            else:
                overlay.hide()
            state["on"] = not state["on"]
            state["count"] += 1
            if state["count"] >= total_ticks:
                timer.stop()
                overlay.hide()

        timer.timeout.connect(_tick)
        timer.start()
        _tick()  # fire first tick immediately

    def flash_tab(self, idx: int, times: int = 3) -> None:
        """Blink the tab a bounded number of times as a 'hey, look here' cue.
        Unlike _start_tab_blink, this stops itself after `times` on/off cycles
        and does NOT get cancelled by the user visiting the tab \u2014 it's short
        enough to play through even when the tab is already current."""
        if not (0 <= idx < len(self._tabs)) or times <= 0:
            return
        # If an unbounded blink is already running for this tab, let it be.
        if idx in self._blinking_tabs:
            return
        total_ticks = int(times) * 2  # on + off per cycle
        timer = QTimer(self)
        timer.setInterval(180)
        state = {"count": 0, "on": True}

        def _tick():
            if not (0 <= idx < len(self._tabs)):
                timer.stop(); return
            p = PALETTE
            color = (QColor(p.get("accent_bright", p["accent"]))
                     if state["on"] else QColor(p.get("muted_text", p["accent"])))
            try:
                self._tab_widget.tabBar().setTabTextColor(idx, color)
            except Exception:
                pass
            state["on"] = not state["on"]
            state["count"] += 1
            if state["count"] >= total_ticks:
                timer.stop()
                try:
                    self._tab_widget.tabBar().setTabTextColor(
                        idx, QColor(PALETTE["muted_text"]))
                except Exception:
                    pass

        timer.timeout.connect(_tick)
        timer.start()
        _tick()  # first tick immediately

    def _tab_blink_tick(self, idx: int) -> None:
        info = self._blinking_tabs.get(idx)
        if info is None or not (0 <= idx < len(self._tabs)):
            return
        p = PALETTE
        state = info["state"]
        color = QColor(p.get("accent_bright", p["accent"])) if state == 0 else QColor(p.get("accent_muted", p["accent"]))
        try:
            self._tab_widget.tabBar().setTabTextColor(idx, color)
        except Exception:
            pass
        info["state"] = 1 - state

    def _stop_tab_blink(self, idx: int) -> None:
        info = self._blinking_tabs.pop(idx, None)
        if info:
            try:
                info["timer"].stop()
            except Exception:
                pass
        # Restore default tab color
        try:
            self._tab_widget.tabBar().setTabTextColor(idx, QColor(PALETTE["muted_text"]))
        except Exception:
            pass

    def _next_untitled_caption(self) -> str:
        self._untitled_counter += 1
        if self._untitled_counter == 1:
            return "Untitled"
        return f"Untitled {self._untitled_counter}"

    def _ensure_scratch_tab(self) -> None:
        """Guarantee at least one editable tab (blank scratch pad)."""
        if self._tabs:
            return
        self._add_untitled_tab()

    def _add_untitled_tab(self) -> None:
        """Open a new empty text tab (no file on disk until Save As)."""
        p = PALETTE
        widget = CodeEditor()
        widget.setStyleSheet(self._code_editor_stylesheet(p))
        widget.prepare_untitled()
        wrap = self._get_wrap_for_ext(".txt")
        self._apply_wrap_to_widget(widget, True, wrap)
        tab_info: dict = {
            "path": "",
            "widget": widget,
            "watcher": None,
            "editable": True,
            "untitled": True,
        }
        self._tabs.append(tab_info)
        cap = self._next_untitled_caption()
        idx = self._tab_widget.addTab(widget, cap)
        self._tab_widget.setTabToolTip(idx, "Not saved — use Save As to create a file")
        self._tab_widget.setCurrentIndex(idx)
        if getattr(self, "_wrap_frozen", False):
            QTimer.singleShot(0, self._sync_frozen_width_locks)

    def _save_current_tab(self) -> None:
        idx = self._tab_widget.currentIndex()
        if not (0 <= idx < len(self._tabs)):
            return
        tab = self._tabs[idx]
        if not tab.get("editable"):
            return
        w = tab["widget"]
        if tab.get("untitled") or not w.has_named_file():
            self._save_as_current_tab()
            return
        w.save_now()

    def _save_as_current_tab(self) -> None:
        idx = self._tab_widget.currentIndex()
        if not (0 <= idx < len(self._tabs)):
            return
        tab = self._tabs[idx]
        if not tab.get("editable"):
            return
        w = tab["widget"]
        start = os.path.join(os.getcwd(), "Untitled.txt")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save As",
            start,
            "Text files (*.txt);;Markdown (*.md);;Python (*.py);;All files (*.*)",
        )
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path = path + ".txt"
        abs_path = os.path.abspath(path)
        w.save_to_path(abs_path)
        tab["path"] = abs_path
        tab["untitled"] = False
        self._tab_widget.setTabText(idx, os.path.basename(abs_path))
        self._tab_widget.setTabToolTip(idx, abs_path)
        ext = os.path.splitext(abs_path)[1].lower()
        wrap = self._get_wrap_for_ext(ext)
        self._apply_wrap_to_widget(w, True, wrap)
        self._start_watching_tab(tab)
        QTimer.singleShot(0, self._update_path_header_label)

    @property
    def _current_path(self) -> str:
        """Path of the currently visible tab (for state persistence compat)."""
        idx = self._tab_widget.currentIndex()
        if 0 <= idx < len(self._tabs):
            return self._tabs[idx]["path"]
        return ""

    @_current_path.setter
    def _current_path(self, value: str):
        """No-op setter for backward compat — tabs manage their own paths."""
        pass

    # Extensions that get a read-only QTextBrowser (binary/complex formats)
    _READONLY_EXTS = {".pdf", ".docx"}
    # Extensions that get a MediaViewer (images / video / audio / SVG)
    _MEDIA_EXTS = MEDIA_EXTS

    def load_file(self, path: str):
        """Open a file in a tab, or switch to it if already open."""
        import os
        if not os.path.isfile(path):
            return
        abs_path = os.path.abspath(path)
        # Remote workspace: pull the host's current content into the shadow file
        # before anything reads it (no-op for local files).
        self._fetch_remote_file(abs_path)
        # Check if already open (skip scratch tabs with no path)
        for i, tab in enumerate(self._tabs):
            tp = tab.get("path") or ""
            if not tp:
                continue
            if os.path.normcase(os.path.abspath(tp)) == os.path.normcase(abs_path):
                self._tab_widget.setCurrentIndex(i)
                if getattr(self, "_wrap_frozen", False):
                    QTimer.singleShot(0, self._sync_frozen_width_locks)
                QTimer.singleShot(0, self._update_path_header_label)
                self._sync_file_tree_selection(abs_path)
                return
        # Remove pristine untitled tabs — if the user never typed in them,
        # they're just default placeholders that shouldn't clutter the tab bar.
        to_remove = []
        for i, tab in enumerate(self._tabs):
            if tab.get("untitled") and tab.get("editable"):
                w = tab["widget"]
                if isinstance(w, CodeEditor) and not w.toPlainText().strip():
                    to_remove.append(i)
        for i in reversed(to_remove):
            self._tabs.pop(i)
            self._tab_widget.removeTab(i)

        ext = os.path.splitext(path)[1].lower()
        p = PALETTE
        if ext in self._MEDIA_EXTS:
            widget = MediaViewer()
            editable = False
        elif ext in self._READONLY_EXTS:
            widget = QTextBrowser()
            widget.setReadOnly(True)
            widget.setOpenExternalLinks(False)
            widget.setFont(QFont("Consolas", 10))
            widget.setStyleSheet(self._text_browser_stylesheet(p))
            editable = False
        else:
            widget = CodeEditor()
            widget.setStyleSheet(self._code_editor_stylesheet(p))
            editable = True
            # Push edits back to the host when this is a remote-workspace file
            # (the handler no-ops for local files).
            widget.file_saved.connect(self._on_remote_editor_saved)

        # Apply saved wrap preference for this extension
        wrap = self._get_wrap_for_ext(ext)
        self._apply_wrap_to_widget(widget, editable, wrap)

        tab_info = {
            "path": abs_path,
            "widget": widget,
            "watcher": None,
            "editable": editable,
            "untitled": False,
        }

        # Page the tab's surface — if this extension supports preview, wrap
        # the editor and a preview pane in a QStackedWidget so we can swap
        # them in place. `tab["widget"]` still points at the editor, so all
        # existing code (set_diff_highlights, watchdog reload, etc.) keeps
        # working without caring about the stack.
        if ext in self._PREVIEWABLE_EXTS:
            from PyQt6.QtWidgets import QStackedWidget
            preview = QTextBrowser()
            # We handle navigation ourselves: in-document #anchors scroll the
            # pane (TOC links), real URLs open in the OS browser. Default
            # QTextBrowser link handling tries to LOAD the href as a new document
            # source, which blanks the pane — so disable it and route through
            # anchorClicked.
            preview.setOpenExternalLinks(False)
            preview.setOpenLinks(False)
            preview.anchorClicked.connect(
                lambda url, pv=preview: self._on_preview_anchor(url, pv))
            preview.setFont(QFont("Consolas", 10))
            preview.setStyleSheet(self._preview_stylesheet(p))
            preview.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
            container = QStackedWidget()
            container.addWidget(widget)   # page 0: editor / text
            container.addWidget(preview)  # page 1: rendered preview
            tab_info["container"] = container
            tab_info["preview"] = preview
            tab_info["view_mode"] = "edit"
            tab_info["preview_timer"] = None  # debounce handle for live refresh
            surface = container
        else:
            surface = widget

        self._tabs.append(tab_info)
        short = os.path.basename(path)
        idx = self._tab_widget.addTab(surface, short)
        self._tab_widget.setTabToolTip(idx, abs_path)
        self._tab_widget.setCurrentIndex(idx)
        self._reload_tab(tab_info)
        self._start_watching_tab(tab_info)
        # Live-refresh the preview as the user edits (debounced). Only
        # matters when preview is active, but the connection is cheap.
        if tab_info.get("preview") is not None and isinstance(widget, CodeEditor):
            widget.textChanged.connect(lambda t=tab_info: self._schedule_preview_refresh(t))
        self.show()
        if getattr(self, "_wrap_frozen", False):
            QTimer.singleShot(0, self._sync_frozen_width_locks)
        QTimer.singleShot(0, self._update_path_header_label)
        self._sync_file_tree_selection(abs_path)

    @staticmethod
    def _code_editor_stylesheet(p: dict) -> str:
        ac = QColor(p['accent'])
        ar, ag, ab = ac.red(), ac.green(), ac.blue()
        return f"""
            QPlainTextEdit {{
                background: {p['panel_alt']};
                color: {p['text']};
                border: none;
                {_mono_selection_qss(p)}
                padding: 8px;
            }}
            QScrollBar:vertical {{
                background: transparent;
                border: 1px solid {p['border']};
                width: 14px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({ar},{ag},{ab},0.15);
                border: 1px solid {p['accent_muted']};
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                border: 1px solid {p['border']};
                height: 14px;
            }}
            QScrollBar::handle:horizontal {{
                background: rgba({ar},{ag},{ab},0.15);
                border: 1px solid {p['accent_muted']};
                min-width: 20px;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """

    def _close_tab(self, index: int):
        if 0 <= index < len(self._tabs):
            # Stop blink timer before indices shift
            self._stop_tab_blink(index)
            tab = self._tabs.pop(index)
            self._stop_watching_tab(tab)
            # Release media-player resources if this was a media tab.
            w = tab.get("widget")
            if isinstance(w, MediaViewer):
                try:
                    w.stop()
                except Exception:
                    pass
            self._tab_widget.removeTab(index)
            # Reindex any remaining blinking tabs that sat after the closed one
            if self._blinking_tabs:
                new_map = {}
                for old_idx, info in self._blinking_tabs.items():
                    if old_idx > index:
                        new_map[old_idx - 1] = info
                    elif old_idx < index:
                        new_map[old_idx] = info
                    # old_idx == index was already stopped above
                self._blinking_tabs = new_map
        if not self._tabs:
            self._ensure_scratch_tab()

    def _reload_tab(self, tab: dict):
        """Reload content for a specific tab."""
        import os
        path = tab["path"]
        widget = tab["widget"]
        if not path or not os.path.isfile(path):
            return
        # An agent-edit diff overlay (ghost rows + highlights) is showing for
        # this tab — a disk reload here would wipe it and discard the pending
        # Accept/Cancel state. The overlay already reflects the new content;
        # skip until the diff is resolved.
        if tab.get("diff_pending"):
            return

        ext = os.path.splitext(path)[1].lower()

        if tab.get("editable"):
            # CodeEditor — skip reload if user just saved (inhibit flag)
            if getattr(widget, '_inhibit_reload', False):
                return
            widget.load_file(path)
        elif isinstance(widget, MediaViewer):
            widget.load(path)
        elif ext == ".pdf":
            self._load_pdf_into(path, widget)
        elif ext == ".docx":
            self._load_docx_into(path, widget)
        else:
            try:
                content = open(path, "r", encoding="utf-8", errors="replace").read()
                widget.setPlainText(content)
            except Exception as e:
                widget.setPlainText(f"Error reading file: {e}")

    @staticmethod
    def _text_browser_stylesheet(p: dict) -> str:
        ac = QColor(p['accent'])
        ar, ag, ab = ac.red(), ac.green(), ac.blue()
        return f"""
            QTextBrowser {{
                background: {p['panel_alt']};
                color: {p['text']};
                border: none;
                {_mono_selection_qss(p)}
                padding: 8px;
            }}
            QScrollBar:vertical {{
                background: transparent;
                border: 1px solid {p['border']};
                width: 14px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({ar},{ag},{ab},0.15);
                border: 1px solid {p['accent_muted']};
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QScrollBar:horizontal {{
                background: transparent;
                border: 1px solid {p['border']};
                height: 14px;
            }}
            QScrollBar::handle:horizontal {{
                background: rgba({ar},{ag},{ab},0.15);
                border: 1px solid {p['accent_muted']};
                min-width: 20px;
            }}
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
                width: 0;
            }}
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
                background: transparent;
            }}
        """

    # ── Rendered preview ──────────────────────────────────────────────

    @staticmethod
    def _preview_stylesheet(p: dict) -> str:
        ac = QColor(p['accent'])
        ar, ag, ab = ac.red(), ac.green(), ac.blue()
        return f"""
            QTextBrowser {{
                background: {p['panel_alt']};
                color: {p['text']};
                border: none;
                padding: 12px 18px;
            }}
            QScrollBar:vertical {{
                background: transparent;
                border: 1px solid {p['border']};
                width: 14px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({ar},{ag},{ab},0.15);
                border: 1px solid {p['accent_muted']};
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """

    def _on_preview_anchor(self, url, preview) -> None:
        """Handle a clicked link in the markdown/HTML preview.
        In-document #fragments scroll the pane; real URLs open externally."""
        frag = url.fragment()
        href = url.toString()
        if not frag and href.startswith("#"):
            frag = href[1:]
        # In-document anchor (no scheme/host, or a leading #) → scroll the pane.
        if frag and not url.scheme() and not url.host():
            self._scroll_preview_to_anchor(preview, frag)
            return
        # Real external link.
        try:
            from PyQt6.QtGui import QDesktopServices
            QDesktopServices.openUrl(url)
        except Exception:
            pass

    def _scroll_preview_to_anchor(self, preview, frag: str) -> None:
        """Scroll to an anchor, tolerating slug-dialect differences. TOC links
        may be GitHub-style (double-dash for '/' and '&', e.g.
        'memory--conversations') while markdown2's header-ids collapse runs to a
        single dash ('memory-conversations'). Try the exact id, then the
        single-dash form, so links work regardless of which convention the
        document author used."""
        candidates = [frag, frag.replace("--", "-")]
        # Remember scroll position; scrollToAnchor is a no-op for a missing id,
        # so we just try each candidate in order.
        seen = set()
        for cand in candidates:
            if cand and cand not in seen:
                seen.add(cand)
                preview.scrollToAnchor(cand)

    def _render_preview_html(self, tab: dict) -> str:
        """Build the HTML shown in the preview pane for *tab*."""
        path = tab.get("path") or ""
        ext = os.path.splitext(path)[1].lower() if path else ""
        body = ""
        widget = tab.get("widget")
        source_text = ""
        if isinstance(widget, CodeEditor):
            source_text = widget.toPlainText()
        elif isinstance(widget, QTextBrowser):
            source_text = widget.toPlainText()

        if ext == ".md":
            try:
                import markdown2
                body = markdown2.markdown(
                    source_text,
                    extras=["fenced-code-blocks", "tables", "code-friendly",
                            "break-on-newline", "cuddled-lists", "header-ids"],
                )
            except Exception as e:
                body = f"<pre>Preview error: {e}</pre>"
        elif ext in (".html", ".htm"):
            body = source_text
        elif ext == ".rst":
            try:
                import docutils.core
                body = docutils.core.publish_parts(
                    source_text, writer_name="html")["html_body"]
            except Exception:
                # Fallback: pre-formatted text so the pane isn't blank
                from html import escape as _esc
                body = f"<pre>{_esc(source_text)}</pre>"
        elif ext == ".pdf":
            # Render actual pages via PyMuPDF — embed page pixmaps as inline
            # data URIs so no temp files are needed.
            body = self._render_pdf_preview_html(path)
        else:
            from html import escape as _esc
            body = f"<pre>{_esc(source_text)}</pre>"

        # Pending-diff ribbon — shows the accept/cancel prompt at the top of
        # the preview so the user knows there's an unconfirmed agent edit.
        ribbon = ""
        if tab.get("diff_pending"):
            added = tab.get("diff_added") or []
            count = len(added)
            p = PALETTE
            ribbon = (
                f"<div style='background:{p['accent']};color:{p['background']};"
                "padding:6px 10px;font-family:Consolas;font-size:9pt;"
                "margin:-12px -18px 12px -18px;border-bottom:2px solid "
                f"{p.get('accent_bright', p['accent'])};'>"
                f"Pending agent edit — {count} line(s) changed. "
                "Use <b>Accept</b> or <b>Cancel</b> above. "
                "Switching tabs accepts it."
                "</div>"
            )

        p = PALETTE
        wrapper = (
            f"<html><head><style>"
            f"body{{color:{p['text']};font-family:Georgia,serif;line-height:1.5;}}"
            f"h1,h2,h3,h4{{color:{p.get('accent_bright', p['accent'])};}}"
            f"code,pre{{background:{p['panel']};color:{p['text']};"
            f"font-family:Consolas,monospace;padding:2px 4px;border-radius:3px;}}"
            f"pre{{padding:8px;overflow-x:auto;border:1px solid {p['border']};}}"
            f"a{{color:{p['accent']};}} "
            f"blockquote{{border-left:3px solid {p['accent_muted']};"
            f"margin:8px 0;padding:4px 12px;color:{p['muted_text']};}}"
            f"table{{border-collapse:collapse;}}"
            f"th,td{{border:1px solid {p['border']};padding:4px 8px;}}"
            f"img{{max-width:100%;}}"
            f"</style></head><body>{ribbon}{body}</body></html>"
        )
        return wrapper

    @staticmethod
    def _render_pdf_preview_html(path: str) -> str:
        try:
            import fitz  # PyMuPDF
            import base64
            doc = fitz.open(path)
            out = []
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=110)
                data = pix.tobytes("png")
                b64 = base64.b64encode(data).decode("ascii")
                out.append(
                    f"<div style='margin:10px 0;text-align:center;'>"
                    f"<img src='data:image/png;base64,{b64}' />"
                    f"<div style='font-family:Consolas;font-size:8pt;opacity:0.6;"
                    f"margin-top:4px;'>Page {i+1} / {len(doc)}</div>"
                    f"</div>"
                )
            doc.close()
            return "".join(out) or "<p><em>(empty PDF)</em></p>"
        except ImportError:
            return "<p>Install PyMuPDF to render PDF previews: <code>pip install PyMuPDF</code></p>"
        except Exception as e:
            return f"<p>PDF preview error: {e}</p>"

    def _on_preview_toggled(self, on: bool):
        idx = self._tab_widget.currentIndex()
        if not (0 <= idx < len(self._tabs)):
            return
        tab = self._tabs[idx]
        container = tab.get("container")
        if container is None:
            # Tab doesn't support preview (extension not previewable) —
            # silently revert the button and bail.
            self._preview_btn.blockSignals(True)
            self._preview_btn.setChecked(False)
            self._preview_btn.blockSignals(False)
            return
        if on:
            self._refresh_preview(tab)
            container.setCurrentIndex(1)
            tab["view_mode"] = "preview"
        else:
            container.setCurrentIndex(0)
            tab["view_mode"] = "edit"
        self._preview_btn.setStyleSheet(self._wrap_btn_style(PALETTE, on))

    def _refresh_preview(self, tab: dict):
        preview = tab.get("preview")
        if preview is None:
            return
        try:
            preview.setHtml(self._render_preview_html(tab))
        except Exception as e:
            preview.setPlainText(f"Preview error: {e}")

    def _schedule_preview_refresh(self, tab: dict):
        """Debounced preview refresh on editor edits — keeps the preview
        in sync without re-rendering on every keystroke."""
        if tab.get("view_mode") != "preview":
            return
        t = tab.get("preview_timer")
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(lambda tb=tab: self._refresh_preview(tb))
            tab["preview_timer"] = t
        t.start(400)

    @staticmethod
    def _load_pdf_into(path: str, text_w):
        try:
            import fitz
            doc = fitz.open(path)
            text_parts = [page.get_text() for page in doc]
            doc.close()
            text_w.setPlainText("\n\n--- Page Break ---\n\n".join(text_parts))
        except ImportError:
            text_w.setPlainText(f"[PDF: {path}]\n\nInstall PyMuPDF to view PDFs:\n  pip install PyMuPDF")
        except Exception as e:
            text_w.setPlainText(f"Error loading PDF: {e}")

    @staticmethod
    def _load_docx_into(path: str, text_w):
        try:
            from docx import Document
            doc = Document(path)
            text_w.setPlainText("\n".join(p.text for p in doc.paragraphs))
        except ImportError:
            text_w.setPlainText(f"[DOCX: {path}]\n\nInstall python-docx:\n  pip install python-docx")
        except Exception as e:
            text_w.setPlainText(f"Error loading DOCX: {e}")

    def refresh_if_showing(self, path: str):
        """Instantly reload any tab displaying this file (skip if editor just saved)."""
        import os
        abs_path = os.path.normcase(os.path.abspath(path))
        for tab in self._tabs:
            if os.path.normcase(os.path.abspath(tab["path"])) == abs_path:
                widget = tab["widget"]
                if tab.get("editable") and getattr(widget, '_inhibit_reload', False):
                    return
                self._reload_tab(tab)

    def _start_watching_tab(self, tab: dict):
        self._stop_watching_tab(tab)
        path = tab["path"]
        if not path:
            return
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
            import os
            watched_dir = os.path.dirname(path)
            norm_path = os.path.normcase(os.path.abspath(path))
            tab_ref = tab
            viewer = self  # capture FileViewer, not the handler

            class _H(FileSystemEventHandler):
                def on_modified(self, event):
                    if not event.is_directory and os.path.normcase(os.path.abspath(event.src_path)) == norm_path:
                        widget = tab_ref["widget"]
                        # Don't wipe a pending agent-edit diff overlay (the write
                        # that triggered this event is the very edit being shown).
                        if tab_ref.get("diff_pending"):
                            return
                        if tab_ref.get("editable") and getattr(widget, '_inhibit_reload', False):
                            return
                        if tab_ref.get("editable"):
                            QTimer.singleShot(0, widget.reload_from_disk)
                        else:
                            QTimer.singleShot(0, lambda: viewer._reload_tab(tab_ref))
                def on_created(self, event):
                    if not event.is_directory and os.path.normcase(os.path.abspath(event.src_path)) == norm_path:
                        if tab_ref.get("diff_pending"):
                            return
                        if tab_ref.get("editable"):
                            QTimer.singleShot(0, tab_ref["widget"].reload_from_disk)
                        else:
                            QTimer.singleShot(0, lambda: viewer._reload_tab(tab_ref))

            obs = Observer()
            obs.schedule(_H(), watched_dir, recursive=False)
            obs.daemon = True
            obs.start()
            tab["watcher"] = obs
        except Exception:
            pass

    @staticmethod
    def _stop_watching_tab(tab: dict):
        obs = tab.get("watcher")
        if obs:
            try:
                obs.stop()
                obs.join(timeout=1)
            except Exception:
                pass
            tab["watcher"] = None

    def _show_open_menu(self):
        """Popup under the Open button — choose between opening a file or a folder."""
        p = PALETTE
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{
                background: {p['panel']};
                color: {p['text']};
                border: 1px solid {p['border']};
                font-family: Consolas; font-size: 9pt;
                padding: 4px 0;
            }}
            QMenu::item {{ padding: 4px 18px; background: transparent; color: {p['text']}; }}
            QMenu::item:selected {{ background: {p['accent_muted']}; color: {p['text']}; }}
            QMenu::separator {{ height: 1px; background: {p['border']}; margin: 4px 8px; }}
        """)
        menu.addAction("File…", self._open_file_dialog)
        menu.addAction("Folder… (sets Explorer root)", self._open_folder_dialog)
        anchor = self._open_btn.mapToGlobal(self._open_btn.rect().bottomLeft())
        menu.exec(anchor)

    def _open_file_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open File", self._explorer_root or "",
            "All Files (*);;Text Files (*.txt);;Markdown (*.md);;PDF (*.pdf)")
        if path:
            self.load_file(path)

    def _open_folder_dialog(self):
        path = QFileDialog.getExistingDirectory(
            self, "Open Folder (Explorer root)",
            self._explorer_root or os.getcwd(),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not path:
            return
        self.set_explorer_root(path, pinned=True)  # explicit user choice — sticks
        # Make sure the Explorer sidebar is visible so the change is obvious.
        if getattr(self, "_tree_sidebar_btn", None) is not None and not self._tree_sidebar_btn.isChecked():
            self._tree_sidebar_btn.setChecked(True)

    def apply_theme(self):
        """Re-apply styles after a color change and reload all tabs."""
        p = PALETTE
        self.setStyleSheet(f"""
            QFrame#FileViewer {{
                background: {p['panel_alt']};
                border: none;
            }}
        """)
        self._open_btn.setStyleSheet(f"color:{p['accent']};background:transparent;border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")
        self._new_btn.setStyleSheet(f"color:{p['accent']};background:transparent;border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")
        self._save_btn.setStyleSheet(f"color:{p['accent']};background:transparent;border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")
        self._save_as_btn.setStyleSheet(f"color:{p['accent']};background:transparent;border:1px solid {p['border']};border-radius:3px;padding:2px 8px;")
        if getattr(self, "_tree_sidebar_btn", None):
            self._apply_explorer_toggle_style(p)
        if self._path_pulse_timer is not None and self._path_pulse_timer.isActive():
            self._path_pulse_timer.stop()
        if getattr(self, "_path_label", None):
            self._apply_path_label_base_style()
        if getattr(self, "_preview_btn", None):
            on = self._preview_btn.isChecked()
            self._preview_btn.setStyleSheet(self._wrap_btn_style(p, on))
        if getattr(self, "_terminal_btn", None):
            on = self._terminal_btn.isChecked()
            self._terminal_btn.setStyleSheet(self._wrap_btn_style(p, on))
        ts = getattr(self, "_terminal_split", None)
        if ts is not None:
            ac = QColor(p["accent"])
            ar, ag, ab = ac.red(), ac.green(), ac.blue()
            ts.setStyleSheet(f"""
                QSplitter#FileViewerTerminalSplit::handle:vertical {{
                    background: {p['border']};
                }}
                QSplitter#FileViewerTerminalSplit::handle:vertical:hover {{
                    background: rgba({ar},{ag},{ab},0.35);
                }}
            """)
        if getattr(self, "_terminal_panel", None) is not None:
            self._terminal_panel.apply_theme()
        self._close_btn.setStyleSheet(f"color:{p['muted_text']};background:transparent;border:none;")
        self._header_w.setStyleSheet(f"background:{p['panel']};")
        if getattr(self, "_header_sep", None):
            self._header_sep.setStyleSheet(
                f"background:{p['border']};border:none;max-height:1px;"
            )
        if getattr(self, "_editor_panel", None):
            self._editor_panel.setStyleSheet(
                "QWidget#FileViewerEditorPanel { background: transparent; }"
            )
        split = getattr(self, "_file_tree_split", None)
        if split is not None:
            ac = QColor(p["accent"])
            ar, ag, ab = ac.red(), ac.green(), ac.blue()
            split.setStyleSheet(f"""
                QSplitter#FileViewerMainSplit::handle:horizontal {{
                    background: {p['border']};
                }}
                QSplitter#FileViewerMainSplit::handle:horizontal:hover {{
                    background: rgba({ar},{ag},{ab},0.35);
                }}
            """)
        self._apply_file_tree_theme(p)
        self._apply_tab_styles(p)
        # Restyle and reload all tabs
        for tab in self._tabs:
            widget = tab["widget"]
            if tab.get("editable"):
                widget.setStyleSheet(self._code_editor_stylesheet(p))
                widget.refresh_palette()
            elif isinstance(widget, MediaViewer):
                # MediaViewer manages its own palette; just re-load so any
                # theme-dependent info bar text refreshes.
                self._reload_tab(tab)
            else:
                widget.setStyleSheet(self._text_browser_stylesheet(p))
                self._reload_tab(tab)
        self._refresh_all_tab_wrap_modes()
        QTimer.singleShot(0, self._update_path_header_label)

    def _close_viewer(self):
        cb = getattr(self, "_collapse_cb", None)
        if cb:
            cb()
            return
        # Collapse via ancestor splitter (tabs stay open)
        w = self.parent()
        while w is not None:
            if isinstance(w, QSplitter) and w.orientation() == Qt.Orientation.Horizontal:
                w.setSizes([1, 0])
                return
            w = w.parent()

    def get_open_paths(self) -> list[str]:
        """Return paths of all open tabs (for state persistence). Untitled buffers are omitted."""
        return [t["path"] for t in self._tabs if t.get("path")]

    def get_active_index(self) -> int:
        return self._tab_widget.currentIndex()

    def close_all_tabs(self):
        """Close all tabs and stop all watchers (caller may add tabs or call _ensure_scratch_tab)."""
        for tab in self._tabs:
            self._stop_watching_tab(tab)
        self._tabs.clear()
        self._tab_widget.clear()
        self._untitled_counter = 0
