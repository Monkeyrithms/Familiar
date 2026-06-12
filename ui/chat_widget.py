"""
Chat UI widgets - individual message widgets with hover, click-to-copy,
tool call bubbles, thinking animation, and browser TV overlay.
"""

import bisect
import html as html_module
import json
import math
import markdown2
import os
import re
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QPushButton, QApplication,
    QTextBrowser, QTextEdit, QPlainTextEdit, QLabel, QComboBox, QFrame, QLineEdit,
    QScrollArea, QSizePolicy, QSpacerItem, QFileDialog, QSplitter,
    QSplitterHandle, QGraphicsOpacityEffect,
)
from PyQt6.QtCore import (
    Qt, QObject, QThread, pyqtSignal, QTimer, QRegularExpression,
    QPropertyAnimation, QEasingCurve, QPoint,
)
from PyQt6.QtGui import (
    QFont, QFontMetrics, QTextCursor, QColor, QPixmap, QImage, QPainter, QPainterPath,
    QSyntaxHighlighter, QTextCharFormat, QTextDocument,
)
from PyQt6.QtWidgets import QProxyStyle, QStyle
from ui.theme import PALETTE


class _NoFocusRectStyle(QProxyStyle):
    """Proxy style that suppresses the dotted focus rectangle Qt's rich-text6
    engine paints around a focused/clicked <a> anchor in a QLabel. Neither
    setFocusPolicy(NoFocus) nor a QSS `outline:none` reaches that primitive —
    only intercepting PE_FrameFocusRect at the style layer kills it. Applied to
    the chat message body labels so clicking a tool chip leaves no 90s-hyperlink
    dotted box behind."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element == QStyle.PrimitiveElement.PE_FrameFocusRect:
            return  # swallow — draw nothing
        super().drawPrimitive(element, option, painter, widget)


# One shared instance; QProxyStyle is stateless here. Parented to the app later
# via setStyle() on each body label (the label takes ownership reference).
_NO_FOCUS_RECT_STYLE: "_NoFocusRectStyle | None" = None


def _no_focus_rect_style() -> "_NoFocusRectStyle":
    global _NO_FOCUS_RECT_STYLE
    if _NO_FOCUS_RECT_STYLE is None:
        _NO_FOCUS_RECT_STYLE = _NoFocusRectStyle()
    return _NO_FOCUS_RECT_STYLE


# Idle threshold for suppressing OS attention flashes. Past this with no
# keyboard/mouse input, the user is considered away from the machine.
_AWAY_IDLE_MS = 120_000


def _user_is_away() -> bool:
    """True when nobody is at the machine — screensaver running or no
    keyboard/mouse input for a while. Used to suppress OS attention requests
    (QApplication.alert / FlashWindowEx): on Windows a taskbar flash counts
    as activity and yanks down a running screensaver, so flashing on every
    agent file edit overnight keeps killing it. Flashing for an absent user
    serves nobody — skip it."""
    if sys.platform != "win32":
        return False
    import ctypes
    try:
        user32 = ctypes.windll.user32
        # Screensaver actively running?
        SPI_GETSCREENSAVERRUNNING = 0x0072
        running = ctypes.c_int(0)
        if user32.SystemParametersInfoW(SPI_GETSCREENSAVERRUNNING, 0,
                                        ctypes.byref(running), 0) and running.value:
            return True

        # No input for a while → away (also covers display-off, which the
        # screensaver check can't see).
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if user32.GetLastInputInfo(ctypes.byref(lii)):
            idle_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            if idle_ms > _AWAY_IDLE_MS:
                return True
    except Exception:
        pass
    return False
from ui.conversation_bar import ConversationBar
from ui.file_viewer import FileViewer
from core.agent import Agent, load_config
from core.debug_recorder import debug_recorder
from core.conversations import (
    list_conversations, save_conversation, load_conversation,
    get_conversation_meta,
    rename_conversation, delete_conversation, new_conversation_id,
    set_conversation_workspace, set_conversation_model,
)
from core.database import (
    get_conversation_composer_draft,
    set_conversation_composer_draft,
    enqueue_composer_draft_save,
    enqueue_conversation_save,
)


THUMB_DIR = Path(__file__).parent.parent / "data" / "image_cache"
THUMB_DIR.mkdir(parents=True, exist_ok=True)

# Persisted copies of attachments (esp. clipboard pastes, whose temp files are
# cleaned up on send). Keeping a stable copy here lets a sent image survive in
# the chat and across conversation reloads.
ATTACH_DIR = Path(__file__).parent.parent / "data" / "attachments"

# PIL decompression ceiling for thumbnails / large paths (blocks pathological images).
_THUMB_MAX_DECODE_PIXELS = 32_000_000


def _tool_labels(names: list[str]) -> list[str]:
    from collections import Counter
    counts = Counter(n for n in names if n)
    return [f"{name} x{cnt}" if cnt > 1 else name for name, cnt in counts.items()]


def _tool_labels_with_names(names: list[str]) -> list[tuple[str, str]]:
    """Like _tool_labels but pairs each display label with its raw tool name,
    so chips can link to the metadata store."""
    from collections import Counter
    counts = Counter(n for n in names if n)
    return [(f"{name} x{cnt}" if cnt > 1 else name, name) for name, cnt in counts.items()]


# Process-global monotonic counter for chip hover-uniqueness. Each rendered
# chip INSTANCE gets a distinct uid so its href (toolmeta:<name>#<uid>) is unique
# across the WHOLE document — not just within one tool row. Without this, two
# chips of the same tool (e.g. a turn that interleaves text with repeated
# file_read calls) share an href, and the hover recolor's global str.replace()
# lights every one of them. itertools.count().__next__ is atomic under the GIL,
# so it's safe even when assistant HTML is pre-rendered off the UI thread.
import itertools as _itertools
_CHIP_UID_COUNTER = _itertools.count(1)


def _next_chip_uid() -> str:
    return str(next(_CHIP_UID_COUNTER))


# Tool labels carry a trailing original-name marker so the chip can link back
# to the metadata store even after _tool_labels() prettifies the display text.
# tool_calls_display_html passes the raw name via this mechanism.
def _chip_anchor(label: str, raw_name: str, color: str, uid: str = "") -> str:
    """Wrap a chip's text in an anchor so clicking it opens the metadata popup.
    Uses a custom toolmeta: scheme handled by the message body's linkActivated.

    ``uid`` makes the href unique per chip INSTANCE (``toolmeta:<name>#<uid>``)
    so hover highlighting targets only the chip under the cursor — without it,
    every chip of the same tool shares one href and all of them light up."""
    safe = html_module.escape(label)
    if not raw_name:
        return safe
    href = "toolmeta:" + html_module.escape(raw_name)
    if uid:
        href += "#" + html_module.escape(uid)
    return f'<a href="{href}" style="color:{color};text-decoration:none;">{safe}</a>'


def _tool_call_chip_cell(name: str, fs: int, raw_name: str = "", uid: str = "") -> str:
    """One tool chip as a table cell — Qt rich text only renders borders on tables."""
    p = PALETTE
    bfs = max(fs - 2, 7)
    ac = p["accent"]
    inner = _chip_anchor(name, raw_name or name, p["accent_muted"], uid)
    return (
        f'<td style="border-width:1px;border-style:solid;border-color:{ac};'
        f'padding:5px 12px;color:{p["accent_muted"]};font-size:{bfs}pt;">'
        f'{inner}</td>'
    )


def _tool_call_bubble_cell(name: str, fs: int, raw_name: str = "", uid: str = "") -> str:
    """Rounded tool pill. Qt rich text ignores border-radius on a <td>, so the
    pill is PAINTED to a PNG (real rounded corners) and embedded as an inline
    <img>, wrapped in the toolmeta anchor so it stays clickable. Falls back to
    the square chip cell if painting fails."""
    pill = _tool_pill_png(name, fs)
    if pill is None:
        return _tool_call_chip_cell(name, fs, raw_name, uid)
    path, w, h = pill
    href = "toolmeta:" + html_module.escape(raw_name or name)
    if uid:
        href += "#" + html_module.escape(uid)
    img = (f'<img src="file:///{path}" width="{w}" height="{h}" '
           f'style="vertical-align:middle;">')
    return (
        f'<td style="border:none;padding:0;">'
        f'<a href="{href}" style="text-decoration:none;">{img}</a>'
        f'</td>'
    )


def tool_calls_display_html(
    names: list[str],
    fs: int,
    *,
    mode: str = "chips",
    show_hint: bool = False,
    margin: str = "8px 0 6px 0",
    align_center: bool = True,
) -> str:
    """Render tool names as chips, rounded bubbles, or a comma-separated list."""
    label_pairs = _tool_labels_with_names(names)
    if not label_pairs:
        return ""
    p = PALETTE
    bfs = max(fs - 2, 7)
    align = "center" if align_center else "left"

    if mode == "comma":
        inner = ", ".join(
            _chip_anchor(l, raw, p["accent_muted"], _next_chip_uid())
            for (l, raw) in label_pairs)
        hint = "Tools: " if show_hint else ""
        return (
            f'<div align="{align}" style="margin:{margin};color:{p["accent_muted"]};'
            f'font-size:{bfs}pt;">{hint}{inner}</div>'
        )

    cell_fn = _tool_call_bubble_cell if mode == "bubbles" else _tool_call_chip_cell
    cells = "".join(cell_fn(l, fs, raw, _next_chip_uid())
                    for (l, raw) in label_pairs)
    hint_td = ""
    if show_hint:
        hint_td = (
            f'<td style="border:none;padding:5px 8px 5px 0;color:{p["accent_muted"]};'
            f'font-size:{bfs}pt;">Tools:</td>'
        )
    return (
        f'<div align="{align}" style="margin:{margin};">'
        f'<table cellspacing="4" cellpadding="0"><tr>{hint_td}{cells}</tr></table>'
        f'</div>'
    )


def _tool_call_chips_row_html(names: list[str], fs: int,
                              margin: str = "8px 0 6px 0") -> str:
    """Centered horizontal row of hollow bordered tool chips (legacy default)."""
    return tool_calls_display_html(names, fs, mode="chips", margin=margin)


def _mono_selection_qss(p: dict) -> str:
    """Selection highlight for text fields — neutral UI chrome, not accent-tinted."""
    return (
        f"selection-background-color: {p['border']};"
        f"selection-color: {p['text']};"
    )


# ── Typographic emphasis: brighten conventional text to break monocolor
# monotony. Borrowed from the Hybrid text-adventure engine's quote highlighting,
# extended with a couple of tasteful extras. Operates on already-rendered HTML;
# carefully skips anything inside <code>/<pre> and existing tags.
_QUOTE_OPEN = "\"“”„‟«»‹›❝❞〝〞＂"
_QUOTE_CLOSE = "\"“”„‟«»‹›❝❞〝〞＂"
# A quoted run: open-quote, inner (no tags/quotes, but allow simple inline
# emphasis tags), close-quote. Non-greedy so adjacent quotes don't merge.
_QUOTE_RE = re.compile(
    rf'([{_QUOTE_OPEN}])'
    rf'((?:[^<{_QUOTE_OPEN}{_QUOTE_CLOSE}]'
    rf'|<(?:em|strong|i|b)\b[^>]*>.*?</(?:em|strong|i|b)>)*?'
    rf'[^<{_QUOTE_OPEN}{_QUOTE_CLOSE}]*?)'
    rf'([{_QUOTE_CLOSE}])',
    re.DOTALL,
)
# A leading "Label:" at the very start of a line/block — e.g. "Note:", "Warning:",
# "Step 1:". Brightened to read as a soft header. Up to 4 words before the colon.
_LABEL_RE = re.compile(
    r'(^|<br\s*/?>|<p[^>]*>|<li[^>]*>)'
    r'(\s*)'
    r'((?:[A-Z][\w&/\- ]{0,28}?)?[A-Za-z0-9])(:)(\s|&nbsp;|<)',
)
# Spans of HTML we must NOT touch (code/pre keep their own coloring; style holds
# CSS that must never be brightened/escaped).
_PROTECT_RE = re.compile(r'(<(code|pre|style)\b[^>]*>.*?</\2>)', re.DOTALL | re.IGNORECASE)
# Tags to split on when brightening quotes — everything EXCEPT the inline
# formatting tags a quoted phrase may legitimately span (em/strong/i/b). Splitting
# here guarantees _QUOTE_RE only ever sees visible text, never the quoted VALUES
# inside tag attributes (style="...", align="...", title="..."). Wrapping those in
# a <span> shreds the tag and spills raw markup into the chat — the regression
# this guards against.
_SPLIT_TAG_RE = re.compile(r'(</?(?!(?:em|strong|i|b)\b)[a-zA-Z][^>]*>)', re.IGNORECASE)

# ── Extra brightness-hierarchy passes (engagement without a rainbow) ──────────
# Short parenthetical asides → DIMMED so they recede from the main line.
_PAREN_RE = re.compile(r'\([^()\n]{1,120}\)')
# Standalone numbers / counts / %s / money → BRIGHT, so data/figures pop.
_NUM_RE = re.compile(r'(?<![\w$#.])(\$?\d[\d,]*(?:\.\d+)*%?)(?![\w])')
# ALL-CAPS words (acronyms + shouted emphasis: NONE, OFF, DB, API) → BRIGHT.
_CAPS_RE = re.compile(r'\b[A-Z]{2,}\b')


def _sub_text_runs(html: str, pattern: "re.Pattern", repl) -> str:
    """Apply pattern→repl to VISIBLE TEXT ONLY — never inside a tag's attributes.
    Splits on tags (keeping inline em/strong/i/b within the text), so spans added
    by an earlier decoration pass are protected from later ones and no tag is ever
    corrupted (the raw-markup-spill bug guard)."""
    chunks = _SPLIT_TAG_RE.split(html)
    for k in range(0, len(chunks), 2):  # even = text, odd = a split-out tag
        if chunks[k]:
            chunks[k] = pattern.sub(repl, chunks[k])
    return "".join(chunks)

# Animated-ellipsis dot spans: data-gd="<group>-<dot>" style="<style>". The
# animator rewrites the style value in ONE pass with this instead of three
# str.find() scans per group per tick.
_ELLIPSIS_DOT_RE = re.compile(r'(data-gd="(\d+)-([123])" style=")([^"]*)(")')


# Tools that must ALWAYS appear in the chat timeline even when the user has
# tool-call bubbles turned off — because they demand the user's attention or
# action. ask_user_question blocks the agent on a human answer; hiding it would
# leave the user with no in-chat trace that they were asked anything.
_ALWAYS_SHOW_TOOLS = {"ask_user_question"}


def _md_emphasis_style() -> str:
    """Shared <style> block that recolors LLM markdown emphasis to glow_hot:
    bold/standalone-bold lines (strong/b), headings (h1-h6), and list items
    (li — colors the bullet/number marker too). Every markdown render path
    must include this or that path renders in flat body color."""
    p = PALETTE
    hot = p.get("glow_hot", p.get("accent_bright", "#aeffff"))
    return (
        f'<style>'
        f'p {{ margin-top: 0; margin-bottom: 0; }} '
        f'strong, b {{ color: {hot}; }} '
        f'h1, h2, h3, h4, h5, h6 {{ color: {hot}; margin-top: 6px; margin-bottom: 2px; }} '
        f'ul, ol {{ margin-top: 2px; margin-bottom: 2px; }} '
        f'li {{ color: {hot}; }}'
        f'</style>'
    )


def _emphasize_html(html: str) -> str:
    """Layer a readable brightness HIERARCHY onto rendered markdown so prose isn't
    one flat block of color:

        DIM  (muted)        → parenthetical asides — they recede
        BODY (text)         → normal prose
        BRIGHT (accent_brt) → numbers/figures, ALL-CAPS, 'Label:' prefixes — pop
        HOT  (glow_hot)     → quoted text (bold/headings get HOT via the <style>)

    Every pass touches VISIBLE TEXT ONLY (tag-aware) and code/pre/style is left
    untouched, so tags are never corrupted."""
    if not html:
        return html
    p = PALETTE
    hot = p.get("glow_hot", p.get("accent_bright", "#aeffff"))
    bright = p.get("accent_bright", hot)
    dim = p.get("muted_text", p.get("accent_muted", "#888888"))
    label_c = bright

    # Split out protected (code) regions so we never touch their contents.
    parts = _PROTECT_RE.split(html)
    # re.split with a capturing group yields: [text, code, lang, text, code, lang, ...]
    out = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        # A protected code block is the next captured group (parts[i+1]); the
        # split interleaves text, full-match, inner-group. Detect & pass through.
        if i + 1 < len(parts) and parts[i + 1] is not None and parts[i + 1].startswith("<") \
                and (parts[i + 1].lower().startswith("<code")
                     or parts[i + 1].lower().startswith("<pre")
                     or parts[i + 1].lower().startswith("<style")):
            out.append(seg)                 # plain text before the protected block
            out.append(parts[i + 1])        # the protected block verbatim
            i += 3                          # skip text + full-match + tag-name group
            continue
        # Decorate visible text only. Each pass is tag-aware (re-splits on tags),
        # so a span added by an earlier pass is never re-matched/corrupted. Order
        # is outer→inner so nesting reads correctly (e.g. a number inside a
        # dimmed aside still pops bright).
        seg = _sub_text_runs(
            seg, _PAREN_RE,
            lambda m: f'<span style="color:{dim};">{m.group(0)}</span>')
        seg = _sub_text_runs(
            seg, _NUM_RE,
            lambda m: f'<span style="color:{bright};">{m.group(0)}</span>')
        seg = _sub_text_runs(
            seg, _CAPS_RE,
            lambda m: f'<span style="color:{bright};">{m.group(0)}</span>')
        seg = _sub_text_runs(
            seg, _QUOTE_RE,
            lambda m: f'<span style="color:{hot};">{m.group(0)}</span>')
        # _LABEL_RE keeps its leading tag (group 1) and only wraps the label
        # text, so it never corrupts a tag — safe on the whole segment.
        seg = _LABEL_RE.sub(
            lambda m: (f'{m.group(1)}{m.group(2)}'
                       f'<span style="color:{label_c};">{m.group(3)}{m.group(4)}</span>'
                       f'{m.group(5)}'),
            seg)
        out.append(seg)
        i += 1
    return "".join(out)


def _ensure_thumb(meta: dict):
    """Generate a resized thumbnail for a message's image_path and store as _thumb.
    Mutates meta in place. Idempotent — skips if _thumb already exists and is valid."""
    if meta.get("_thumb") and os.path.isfile(meta["_thumb"]):
        return
    src = meta.get("image_path", "")
    if not src or not os.path.isfile(src):
        return
    try:
        import hashlib
        st = os.stat(src)
        key = f"{src}|{st.st_mtime}|800"
        h = hashlib.md5(key.encode()).hexdigest()
        cached = THUMB_DIR / f"{h}.jpg"

        if cached.exists():
            meta["_thumb"] = str(cached)
            return

        from PIL import Image
        import io

        Image.MAX_IMAGE_PIXELS = _THUMB_MAX_DECODE_PIXELS
        try:
            img = Image.open(src)
        except Image.DecompressionBombError:
            return
        long_edge = max(img.size)
        if long_edge > 800:
            scale = 800 / long_edge
            img = img.resize(
                (int(img.size[0] * scale), int(img.size[1] * scale)),
                Image.LANCZOS)
        if img.mode in ("RGBA", "LA", "PA"):
            img.save(str(cached).replace(".jpg", ".png"), format="PNG")
            meta["_thumb"] = str(cached).replace(".jpg", ".png")
        else:
            img = img.convert("RGB")
            img.save(str(cached), format="JPEG", quality=85)
            meta["_thumb"] = str(cached)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────
# Inference thread
# ──────────────────────────────────────────────────────────────────────

class InferenceThread(QThread):
    # reply, tool_call_log, pre-rendered assistant HTML (built off the UI thread)
    finished = pyqtSignal(str, list, str)
    errored = pyqtSignal(str)
    stopped = pyqtSignal()  # user hit STOP
    tool_called = pyqtSignal(str, dict)  # tool_name, args — fires mid-inference
    chunk = pyqtSignal(str)        # streamed answer-text delta (live rendering)
    round_started = pyqtSignal()   # a new model round began — reset live view

    def __init__(self, agent: Agent, message: str, image_path: str = None):
        super().__init__()
        self.agent = agent
        self.message = message
        self.image_path = image_path

    def run(self):
        try:
            # Tool UI hooks are installed on the agent by ChatWindow before start().
            # Live token streaming is PER-CONVERSATION (agent._stream_live, set
            # from the Conversation dialog). When off, leave the callback unset so
            # the agent buffers the whole reply and only the FINAL response is
            # posted (cleanest with the reflect self-review loop).
            stream_live = bool(getattr(self.agent, "_stream_live", True))
            self.agent._stream_callback = self.chunk.emit if stream_live else None
            self.agent._on_round_start = self.round_started.emit if stream_live else None
            reply = self.agent.chat(self.message, image_path=self.image_path)
            self.agent._tool_callback = None
            self.agent._tool_batch_callback = None
            self.agent._stream_callback = None
            self.agent._on_round_start = None
            reply_html = ""
            try:
                extras = ["fenced-code-blocks", "tables", "code-friendly"]
                reply_html = markdown2.markdown(reply, extras=extras)
            except Exception:
                reply_html = ""
            self.finished.emit(reply, list(self.agent.tool_call_log), reply_html)
        except InterruptedError:
            self._clear_agent_hooks()
            self.stopped.emit()
        except BaseException as e:
            # BaseException, not Exception: SystemExit / KeyboardInterrupt /
            # GeneratorExit raised inside a provider lib or tool would otherwise
            # unwind this thread WITHOUT any signal — UI shows nothing, turn
            # looks finished, agent "just stopped". Every exit path must emit.
            self._clear_agent_hooks()
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            sys.stderr.flush()
            try:
                log_path = Path(__file__).resolve().parent.parent / "logs" / "errors.log"
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as f:
                    f.write(f"\n=== {datetime.now().isoformat()} — inference error ===\n{tb}\n")
            except Exception:
                pass
            etype = type(e).__name__
            msg = f"{etype}: {e}" if str(e) else etype
            self.errored.emit(f"{msg}\n\n{tb}")

    def _clear_agent_hooks(self):
        self.agent._tool_callback = None
        self.agent._tool_batch_callback = None
        self.agent._stream_callback = None
        self.agent._on_round_start = None

    def _on_tool(self, name: str, args: dict):
        self.tool_called.emit(name, args)


class ConversationLoadThread(QThread):
    """Load conversation JSON from SQLite off the UI thread."""

    loaded = pyqtSignal(str, object)  # conv_id, data dict | None

    def __init__(self, conv_id: str):
        super().__init__()
        self._conv_id = conv_id

    def run(self):
        data = None
        try:
            data = load_conversation(self._conv_id)
        except Exception as e:
            print(f"[ChatWidget] conversation load failed ({self._conv_id}): {e}")
        self.loaded.emit(self._conv_id, data)


# ──────────────────────────────────────────────────────────────────────
# Splitter with hover sound on handle enter
# ──────────────────────────────────────────────────────────────────────

class _HoverSoundHandle(QSplitterHandle):
    def enterEvent(self, event):
        try:
            from core.sounds import play_ui
            # Subtle message/tool-bubble click sound, not the louder hover chime.
            play_ui("message.mp3")
        except Exception:
            pass
        super().enterEvent(event)


class HoverSoundSplitter(QSplitter):
    def createHandle(self):
        return _HoverSoundHandle(self.orientation(), self)


# ──────────────────────────────────────────────────────────────────────
# Full-size image overlay — click anywhere to dismiss
# ──────────────────────────────────────────────────────────────────────

class ImageOverlay(QWidget):
    """Semi-transparent overlay that shows an image centered on the parent window."""

    _active = None  # only one overlay at a time

    @staticmethod
    def show_image(pixmap: QPixmap, parent_window):
        if ImageOverlay._active is not None:
            ImageOverlay._active.close()
        overlay = ImageOverlay(pixmap, parent_window)
        overlay.show()
        overlay.raise_()

    def __init__(self, pixmap: QPixmap, parent_window):
        super().__init__(parent_window)
        ImageOverlay._active = self
        self._pixmap = pixmap
        self.setGeometry(parent_window.rect())
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        # Scale image to fit window with padding
        pad = 40
        max_w = self.width() - pad * 2
        max_h = self.height() - pad * 2
        scaled = pixmap.scaled(
            max_w, max_h,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation)

        # Round corners
        rounded = QPixmap(scaled.size())
        rounded.fill(QColor("transparent"))
        rp = QPainter(rounded)
        rp.setRenderHint(QPainter.RenderHint.Antialiasing)
        clip = QPainterPath()
        clip.addRoundedRect(0, 0, scaled.width(), scaled.height(), 8, 8)
        rp.setClipPath(clip)
        rp.drawPixmap(0, 0, scaled)
        rp.end()
        self._display = rounded

    def paintEvent(self, event):
        painter = QPainter(self)
        # Dark scrim
        painter.fillRect(self.rect(), QColor(0, 0, 0, 180))
        # Centered image
        x = (self.width() - self._display.width()) // 2
        y = (self.height() - self._display.height()) // 2
        painter.drawPixmap(x, y, self._display)

    def mousePressEvent(self, event):
        self.close()

    def keyPressEvent(self, event):
        self.close()

    def close(self):
        ImageOverlay._active = None
        super().close()
        self.deleteLater()


# ──────────────────────────────────────────────────────────────────────
# Individual message widget
# ──────────────────────────────────────────────────────────────────────

# Display name for the assistant's messages in the chat. The real source of
# truth for "is this an assistant message" is meta["role"]; this is purely the
# label shown in the bubble header.
AGENT_LABEL = "Familiar"


def _meta_display_sender(meta: dict) -> str | None:
    """Bubble header label for a user/assistant meta row, or None for cards."""
    role = meta.get("role", "")
    if role == "user":
        return "You"
    if role == "assistant":
        return AGENT_LABEL
    return None


def _show_sender_nametag(message_meta: list, idx: int) -> bool:
    """True when this row should show the sender header (start of a speaker run)."""
    if idx < 0 or idx >= len(message_meta):
        return True
    cur = _meta_display_sender(message_meta[idx])
    if cur is None:
        return True
    for j in range(idx - 1, -1, -1):
        prev = _meta_display_sender(message_meta[j])
        if prev is not None:
            return prev != cur
    return True


_SPARKLE_CACHE = {"key": None, "html": ""}

# Cache of painted tool-pill PNGs, keyed by (label, fontsize, accent, color).
# Qt's rich-text engine ignores CSS border-radius on <td>, so true rounded
# "bubble" pills are painted to a pixmap and embedded as an inline <img> (the
# same proven trick the sparkle icon uses). Anchored so they stay clickable.
_PILL_CACHE: dict = {}
_PILL_DIR = THUMB_DIR.parent / "pill_cache"


def _tool_pill_png(label: str, fs: int) -> tuple[str, int, int] | None:
    """Paint a rounded-rect pill for *label* to a cached PNG. Returns
    (file_path, css_width, css_height) or None on failure. High-DPI crisp:
    rendered at 2x and tagged with width/height so it displays at logical size."""
    import hashlib
    from PyQt6.QtGui import QPixmap, QPainter, QPen
    from PyQt6.QtCore import QRectF
    p = PALETTE
    accent = p["accent"]
    text_color = p["accent_muted"]
    bfs = max(fs - 2, 7)
    key = (label, bfs, accent, text_color)
    cached = _PILL_CACHE.get(key)
    if cached:
        return cached
    try:
        scale = 2  # render at 2x for crispness, display at 1x
        font = QFont("Consolas", bfs)
        fm = QFontMetrics(font)
        pad_x, pad_y = 12, 5
        tw = fm.horizontalAdvance(label)
        th = fm.height()
        w = tw + pad_x * 2
        h = th + pad_y * 2
        pm = QPixmap(w * scale, h * scale)
        pm.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pm)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(scale, scale)
        # Rounded border (the whole point — real rounded corners).
        pen = QPen(QColor(accent))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        radius = h / 2.0  # full pill
        painter.drawRoundedRect(QRectF(0.75, 0.75, w - 1.5, h - 1.5), radius, radius)
        # Label.
        painter.setFont(font)
        painter.setPen(QPen(QColor(text_color)))
        painter.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, label)
        painter.end()

        _PILL_DIR.mkdir(parents=True, exist_ok=True)
        h8 = hashlib.md5(f"{label}|{bfs}|{accent}|{text_color}".encode()).hexdigest()[:16]
        out = _PILL_DIR / f"{h8}.png"
        if not pm.save(str(out), "PNG"):
            return None
        result = (str(out).replace("\\", "/"), w, h)
        _PILL_CACHE[key] = result
        return result
    except Exception:
        return None


def _sparkle_img_html() -> str:
    """The app's sparkle icon as an inline <img> for QLabel rich text, cached
    per accent color so it tracks the theme. Returns '' on any failure so the
    header simply omits it instead of showing a broken-image box."""
    try:
        from pathlib import Path
        from ui.theme import PALETTE
        from ui.app_icon import build_app_icon
        key = (PALETTE.get("accent"), PALETTE.get("glow_hot"))
        if _SPARKLE_CACHE["key"] == key and _SPARKLE_CACHE["html"]:
            return _SPARKLE_CACHE["html"]
        # Render glow-hot: pass the hot color for BOTH body and core so the icon
        # beside the nametag burns at the same temperature as the name itself —
        # exactly the titlebar treatment (build_app_icon(col, col)).
        hot = PALETTE.get("glow_hot") or PALETTE.get("accent_bright")
        icon = build_app_icon(hot, hot)
        out = Path(__file__).resolve().parent.parent / "data" / "sparkle_inline.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        if not icon.pixmap(32, 32).save(str(out), "PNG"):
            return ""
        html = f' <img src="file:///{str(out).replace(chr(92), "/")}" width="13" height="13">'
        _SPARKLE_CACHE.update(key=key, html=html)
        return html
    except Exception:
        return ""


class ChatMessageWidget(QFrame):
    """A single chat message with hover highlight, click-to-copy, and tool bubbles."""

    _selected_widget = None  # class-level: only one selected at a time
    _font_size = 10           # class-level: updated from config by ChatWindow

    # Shared ellipsis animation — one timer drives all visible widgets
    _ellipsis_timer = None
    _ellipsis_widgets: list = []  # all widgets with ellipsis
    _ellipsis_tick = 0
    _ellipsis_enabled = True  # toggled from settings (the feature on/off switch)
    # Only animate while a response is actively generating. When idle (e.g. the
    # user is composing), animating every visible "..." message means a full
    # rich-text setText re-parse on each one every 500ms — which stalls the UI
    # thread and makes typing feel laggy. Gated on by _set_inferring().
    _ellipsis_active = False
    _tool_display_mode = "chips"
    _show_tools_hint = False

    def __init__(self, sender: str, content: str, tool_names: list[str] = None,
                 image_path: str = None, cached_html: str = None,
                 timestamp: float = None, usage: dict = None,
                 show_timestamps: bool = True, show_usage: bool = False,
                 show_tool_chips: bool = True, chat_mode: str = "fancy",
                 tool_call_only: bool = False, continuation: bool = False,
                 inline_timeline: bool = False, pastes: list = None, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatMsg")
        self.sender = sender
        self.content = content
        # Large pasted blocks shown as collapsed PasteCardWidgets under the bubble
        # (render-only; the full text lives in the message content for the LLM).
        self.pastes = pastes or []
        self.tool_names = tool_names or []
        self.image_path = image_path
        self._cached_html = cached_html
        self._timestamp = timestamp
        self._usage = usage
        self._show_timestamps = show_timestamps
        self._show_usage = show_usage
        self._show_tool_chips = show_tool_chips
        self._chat_mode = chat_mode
        self._tool_call_only = tool_call_only
        self._continuation = continuation
        self._inline_timeline = inline_timeline
        self._selected = False
        self._context_outside_window = False
        self._ctx_dim_overlay = None
        # Set in __init__ (not just fancy-mode _build) so the shared ellipsis
        # animator can safely drive plain-mode widgets too.
        self._visible_in_viewport = True
        self._current_html = ""
        # Wrap width last applied by the parent chat view (0 = not yet applied)
        # and the body label's stylesheet padding as (horizontal, vertical)
        # totals — both feed _sync_body_min_height().
        self._wrap_width = 0
        self._body_pads = (0, 0)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self._build()
        self._apply_base_style()

    def _build(self):

        # If plain mode, render as simple text
        if self._chat_mode == "plain":
            self._build_plain()
            return


        p = PALETTE
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)

        # Determine colors
        if self.sender == "You":
            header_color = p["glow_hot"]
            text_color = p["muted_text"]
        elif self.sender == "Error":
            header_color = p["danger"]
            text_color = p["danger"]
        else:
            header_color = p["glow_hot"]
            text_color = p["text"]

        # Attached image card (above the text block) — centered, click to expand
        if self.image_path and os.path.isfile(self.image_path):
            pixmap = self._load_image_pixmap(self.image_path)
            if pixmap and not pixmap.isNull():
                self._full_pixmap = pixmap  # keep for expand

                max_preview = 500
                if pixmap.width() > max_preview:
                    scaled = pixmap.scaledToWidth(
                        max_preview, Qt.TransformationMode.SmoothTransformation)
                else:
                    scaled = pixmap

                # Round the corners
                rounded = QPixmap(scaled.size())
                rounded.fill(QColor("transparent"))
                rp = QPainter(rounded)
                rp.setRenderHint(QPainter.RenderHint.Antialiasing)
                clip_path = QPainterPath()
                clip_path.addRoundedRect(0, 0, scaled.width(), scaled.height(), 6, 6)
                rp.setClipPath(clip_path)
                rp.drawPixmap(0, 0, scaled)
                rp.end()

                img_card = QFrame()
                img_card.setStyleSheet(
                    f"background: {p['panel_alt']}; border: 1px solid {p['border']};"
                    f"border-radius: 6px; padding: 4px;")
                img_card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
                img_card.setCursor(Qt.CursorShape.PointingHandCursor)
                card_layout = QVBoxLayout(img_card)
                card_layout.setContentsMargins(4, 4, 4, 4)
                card_layout.setSpacing(2)

                img_label = QLabel()
                img_label.setPixmap(rounded)
                img_label.setStyleSheet("background:transparent; border:none;")
                card_layout.addWidget(img_label)

                fname_label = QLabel(os.path.basename(self.image_path))
                fname_label.setFont(QFont("Consolas", 7))
                fname_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                fname_label.setStyleSheet(f"color: {p['muted_text']}; background:transparent; border:none;")
                card_layout.addWidget(fname_label)

                # Click to show full-size overlay
                img_card.mousePressEvent = self._toggle_image_expand

                # Center the card
                center_row = QHBoxLayout()
                center_row.setContentsMargins(0, 0, 0, 0)
                center_row.addStretch()
                center_row.addWidget(img_card)
                center_row.addStretch()
                layout.addLayout(center_row)

        # Build bubbles HTML if tool calls present
        fs = self._font_size
        combined_html = self._make_combined_html(fs)
        combined_html = self._apply_ellipsis_markup(combined_html)

        self._base_html = combined_html
        self._current_html = combined_html
        self._visible_in_viewport = True

        body = QLabel()
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        self._wire_body_links(body)
        body.setStyleSheet("background: transparent; padding: 5px 5px 6px 5px;")
        body.setMinimumWidth(0)
        body.setText(combined_html)
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)

        self._body = body
        self._body_pads = (10, 11)  # stylesheet padding: 5+5 horiz, 5+6 vert
        layout.addWidget(body)

        # Pasted-text cards (collapsed/expandable) under the typed text.
        self._add_paste_cards(layout)

        # Register for ellipsis animation if needed
        if self._has_ellipsis and self._ellipsis_groups:
            self._register_ellipsis_widget()

    def _add_paste_cards(self, layout):
        if not self.pastes:
            return
        from ui.paste_card import PasteCardWidget
        for p in self.pastes:
            try:
                card = PasteCardWidget(
                    p.get("text", ""),
                    lines=p.get("lines", 0),
                    chars=p.get("chars", 0),
                    fs=max(self._font_size - 1, 8),
                )
                layout.addWidget(card)
            except Exception:
                pass

    def _apply_ellipsis_markup(self, combined_html: str) -> str:
        """Turn literal ... in HTML into animated dot spans (build + live updates)."""
        self._has_ellipsis = bool(re.search(r'\.\.\.', combined_html))
        self._ellipsis_groups = []
        self._ellipsis_states = []
        if not self._has_ellipsis:
            try:
                ChatMessageWidget._ellipsis_widgets.remove(self)
            except ValueError:
                pass
            return combined_html
        group_idx = [0]

        def _make_dot_span(match):
            gid = group_idx[0]
            group_idx[0] += 1
            self._ellipsis_groups.append(gid)
            self._ellipsis_states.append(gid % 4)
            return (
                f'<span data-eg="{gid}">'
                f'<span data-gd="{gid}-1" style="">.</span>'
                f'<span data-gd="{gid}-2" style="">.</span>'
                f'<span data-gd="{gid}-3" style="">.</span>'
                f'</span>'
            )

        html = re.sub(r'\.\.\.', _make_dot_span, combined_html)
        self._register_ellipsis_widget()
        return html

    def _register_ellipsis_widget(self):
        if self not in ChatMessageWidget._ellipsis_widgets:
            ChatMessageWidget._ellipsis_widgets.append(self)
        if ChatMessageWidget._ellipsis_timer is None:
            ChatMessageWidget._ellipsis_timer = QTimer()
            ChatMessageWidget._ellipsis_timer.timeout.connect(
                ChatMessageWidget._animate_ellipsis_all)
            ChatMessageWidget._ellipsis_timer.start(500)

    def _header_and_text_colors(self) -> tuple[str, str]:
        p = PALETTE
        if self.sender == "You":
            return p["glow_hot"], p["muted_text"]
        if self.sender == "Error":
            return p["danger"], p["danger"]
        return p["glow_hot"], p["text"]

    def _wire_body_links(self, body) -> None:
        """Make a body QLabel's links clickable (tool chips use toolmeta:),
        while keeping real http(s) links opening externally."""
        body.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)
        body.setOpenExternalLinks(False)
        # No keyboard focus → no dotted focus rectangle on a just-clicked chip.
        body.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        # Belt-and-suspenders: also strip the anchor focus-rect at the style layer
        # (focus policy alone doesn't suppress the rich-text anchor box).
        body.setStyle(_no_focus_rect_style())
        body.linkActivated.connect(self._on_body_link)
        body.linkHovered.connect(lambda href, b=body: self._on_body_link_hover(href, b))

    def _on_body_link_hover(self, href: str, body) -> None:
        """Brighten ONLY the tool chip under the cursor (text → glow color) and
        KEEP it lit while the cursor rests on it. Each chip instance carries a
        globally-unique href (toolmeta:<name>#<uid>), so the recolor targets
        exactly one chip — never every chip of that tool.

        Qt fires a spurious linkHovered("") whenever the cursor crosses the
        chip's padding or a kerning gap, which used to revert the glow instantly
        (the "doesn't stay lit" flicker). We debounce the revert: a short timer
        un-highlights only if no chip hover arrives first, so the glow holds."""
        p = PALETTE
        if href and href.startswith("toolmeta:"):
            # (Still) over a chip — cancel any pending un-highlight.
            t = getattr(self, "_chip_revert_timer", None)
            if t is not None:
                t.stop()
            if getattr(self, "_chip_hover_current", "") == href:
                return  # already lit — don't re-render and fight our own hover
            # Moving straight from one lit chip to another: restore the previous
            # chip's body first so highlights never stack across bodies.
            prev_body = getattr(self, "_chip_hover_body", None)
            prev_base = getattr(self, "_chip_hover_base", None)
            if prev_base is not None and prev_body is not None and prev_body is not body:
                prev_body.setText(prev_base)
                self._chip_hover_base = None
            if getattr(self, "_chip_hover_base", None) is None:
                self._chip_hover_base = body.text()
            self._chip_hover_body = body
            needle = f'href="{html_module.escape(href)}" style="color:{p["accent_muted"]};'
            repl = f'href="{html_module.escape(href)}" style="color:{p["glow_hot"]};'
            # Rebuild from the resting base; the unique href means exactly one
            # anchor matches, so only the hovered chip changes color.
            body.setText(self._chip_hover_base.replace(needle, repl))
            self._chip_hover_current = href
        elif getattr(self, "_chip_hover_current", ""):
            # Left a chip (or a transient "" event) — revert after a short delay.
            self._schedule_chip_revert()

    def _schedule_chip_revert(self) -> None:
        """Debounce the un-highlight so transient empty-href events don't make
        the chip glow flicker out while the cursor is still effectively on it."""
        t = getattr(self, "_chip_revert_timer", None)
        if t is None:
            t = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._revert_chip_hover)
            self._chip_revert_timer = t
        t.start(70)

    def _revert_chip_hover(self) -> None:
        base = getattr(self, "_chip_hover_base", None)
        bdy = getattr(self, "_chip_hover_body", None)
        if base is not None and bdy is not None:
            bdy.setText(base)
        self._chip_hover_base = None
        self._chip_hover_body = None
        self._chip_hover_current = ""

    def _on_body_link(self, href: str) -> None:
        """Handle a clicked link in the message body. toolmeta:<name> opens the
        tool-call metadata popup; everything else opens in the browser."""
        if href.startswith("familiar://"):
            self._handle_familiar_link(href)
            return
        if href.startswith("toolmeta:"):
            # Strip the per-instance uid suffix (toolmeta:<name>#<uid>).
            tool_name = href[len("toolmeta:"):].split("#", 1)[0]
            try:
                from core.sounds import play_ui
                play_ui("message.mp3")
            except Exception:
                pass
            try:
                from ui.tool_meta_dialog import show_tool_meta
                show_tool_meta(self, tool_name)
            except Exception:
                pass
            return
        # Real external link.
        try:
            from PyQt6.QtGui import QDesktopServices
            from PyQt6.QtCore import QUrl
            QDesktopServices.openUrl(QUrl(href))
        except Exception:
            pass

    def _make_combined_html(self, fs: int | None = None) -> str:
        p = PALETTE
        fs = fs or self._font_size
        header_color, text_color = self._header_and_text_colors()
        sparkle = _sparkle_img_html() if self.sender == AGENT_LABEL else ""

        tool_chips_html = ""
        if self.tool_names and self._show_tool_chips and not getattr(self, "_inline_timeline", False):
            tool_chips_html = tool_calls_display_html(
                self.tool_names, fs,
                mode=ChatMessageWidget._tool_display_mode,
                show_hint=ChatMessageWidget._show_tools_hint,
                margin="0 0 2px 0",
            )

        if getattr(self, "_tool_call_only", False):
            return (
                f'<div style="font-family:Consolas; word-wrap:break-word;">'
                f'{tool_chips_html}'
                f'</div>'
            )

        if getattr(self, "_continuation", False):
            html_body = self._cached_html or markdown2.markdown(
                self.content, extras=["fenced-code-blocks", "tables", "code-friendly"])
            _hr = f'<p style="color:{p["accent_soft"]};margin:4px 0;opacity:0.4;">{"─" * 60}</p>'
            html_body = html_body.replace("<hr>", _hr).replace("<hr />", _hr)
            html_body = _emphasize_html(html_body)
            return (
                f'<style>p {{ margin-top: 0; margin-bottom: 0; }} strong, b {{ color: {p["glow_hot"]}; }} '
                f'h1, h2, h3, h4, h5, h6 {{ color: {p["glow_hot"]}; margin-top: 6px; margin-bottom: 2px; }} '
                f'li {{ color: {p["glow_hot"]}; }}</style>'
                f'<div style="font-family:Consolas; word-wrap:break-word;">'
                f'<span style="color:{text_color}; font-size:{fs}pt;">'
                f'{html_body}</span>'
                f'</div>'
            )

        if getattr(self, "_inline_timeline", False):
            html_body = self._cached_html or ""
            header = ""
            if not getattr(self, "_continuation", False):
                header = (
                    f'<p style="margin-bottom:2px;">'
                    f'<span style="color:{header_color};font-weight:bold;font-size:{max(fs - 1, 7)}pt;">{self.sender}</span>{sparkle}'
                    f'{self._format_timestamp()}</p>'
                )
            return (
                f'<style>p {{ margin-top: 0; margin-bottom: 0; }} strong, b {{ color: {p["glow_hot"]}; }} '
                f'h1, h2, h3, h4, h5, h6 {{ color: {p["glow_hot"]}; margin-top: 6px; margin-bottom: 2px; }} '
                f'li {{ color: {p["glow_hot"]}; }}</style>'
                f'<div style="font-family:Consolas; word-wrap:break-word;">'
                f'{header}'
                f'<div style="color:{text_color}; font-size:{fs}pt;">'
                f'{html_body}</div>'
                f'{self._format_usage()}'
                f'</div>'
            )

        html_body = self._cached_html or markdown2.markdown(
            self.content, extras=["fenced-code-blocks", "tables", "code-friendly"])
        _hr = f'<p style="color:{p["accent_soft"]};margin:4px 0;opacity:0.4;">{"─" * 60}</p>'
        html_body = html_body.replace("<hr>", _hr).replace("<hr />", _hr)
        html_body = _emphasize_html(html_body)

        header = ""
        if not getattr(self, "_continuation", False):
            header = (
                f'<p style="margin-bottom:2px;">'
                f'<span style="color:{header_color};font-weight:bold;font-size:{max(fs - 1, 7)}pt;">{self.sender}</span>{sparkle}'
                f'{self._format_timestamp()}</p>'
            )
        return (
            f'<style>p {{ margin-top: 0; margin-bottom: 0; }} strong, b {{ color: {p["glow_hot"]}; }} '
            f'h1, h2, h3, h4, h5, h6 {{ color: {p["glow_hot"]}; margin-top: 6px; margin-bottom: 2px; }} '
            f'li {{ color: {p["glow_hot"]}; }}</style>'
            f'<div style="font-family:Consolas; word-wrap:break-word;">'
            f'{tool_chips_html}'
            f'{header}'
            f'<span style="color:{text_color}; font-size:{fs}pt;">'
            f'{html_body}</span>'
            f'{self._format_usage()}'
            f'</div>'
        )

    def update_content(self, content: str, cached_html: str | None = None,
                       tool_names: list[str] | None = None, usage: dict | None = None,
                       inline_timeline: bool | None = None,
                       measure_height: bool = True):
        """Live-update an assistant bubble while tokens stream in."""
        self.content = content
        if cached_html is not None:
            self._cached_html = cached_html
        if tool_names is not None:
            self.tool_names = tool_names
        if usage is not None:
            self._usage = usage
        if inline_timeline is not None:
            self._inline_timeline = inline_timeline
        if self._chat_mode == "plain":
            if hasattr(self, "_body"):
                plain_html = self._apply_ellipsis_markup(self._make_plain_html())
                self._base_html = plain_html
                self._current_html = plain_html
                self._body.setText(plain_html)
                if measure_height:
                    self._sync_body_min_height()
            return
        combined_html = self._make_combined_html()
        combined_html = self._apply_ellipsis_markup(combined_html)
        self._base_html = combined_html
        self._current_html = combined_html
        if hasattr(self, "_body"):
            self._body.setText(combined_html)
            if measure_height:
                self._sync_body_min_height()

    def reconfigure(self, *, sender: str, content: str, tool_names: list[str] | None = None,
                    image_path: str | None = None, cached_html: str | None = None,
                    timestamp: float | None = None, usage: dict | None = None,
                    show_timestamps: bool = True, show_usage: bool = False,
                    show_tool_chips: bool = True, chat_mode: str = "fancy",
                    continuation: bool = False, inline_timeline: bool = False):
        """Reuse a pooled bubble — avoids QWidget churn when virtual-scrolling."""
        if image_path and image_path != self.image_path:
            return False
        self.sender = sender
        self.content = content
        self.tool_names = tool_names or []
        self._timestamp = timestamp
        self._usage = usage
        self._show_timestamps = show_timestamps
        self._show_usage = show_usage
        self._show_tool_chips = show_tool_chips
        self._chat_mode = chat_mode
        self._continuation = continuation
        self._inline_timeline = inline_timeline
        if cached_html is not None:
            self._cached_html = cached_html
        self.update_content(
            content, cached_html, tool_names, usage, inline_timeline)
        self._apply_base_style()
        # NOTE: do NOT show() here. A pooled bubble is parentless at this point
        # (released widgets are setParent(None)+hide()), and show()-ing a
        # parentless widget makes it a momentary top-level WINDOW — the "stray
        # windows flashing on screen" bug. _insert_msg_widget show()s it AFTER it
        # is reparented into the layout.
        return True

    @staticmethod
    def set_ellipsis_active(active: bool):
        """Gate the ellipsis animation to the active-inference window. Flipping
        it off leaves every "..." at rest as a normal, fully-visible ellipsis so
        no dots are stranded mid-blink (transparent) while idle."""
        if ChatMessageWidget._ellipsis_active == active:
            return
        ChatMessageWidget._ellipsis_active = active
        if not active:
            ChatMessageWidget._freeze_ellipsis_visible()

    @staticmethod
    def _freeze_ellipsis_visible():
        """One-shot pass: force every animated dot back to visible and reset
        group state, so paused ellipses read as a plain '...'."""
        for w in ChatMessageWidget._ellipsis_widgets:
            try:
                groups = getattr(w, "_ellipsis_groups", None)
                if not groups:
                    continue
                html = w._current_html
                # Single O(n) pass: blank every dot's style (all visible).
                new_html, n = _ELLIPSIS_DOT_RE.subn(
                    lambda m: m.group(1) + m.group(5), html)
                w._ellipsis_states = [3 for _ in groups]
                if n and new_html != html:
                    w._current_html = new_html
                    w._body.setText(new_html)
            except (RuntimeError, AttributeError):
                pass

    @staticmethod
    def _animate_ellipsis_all():
        """Shared timer — each ellipsis group advances its own state independently."""
        if not (ChatMessageWidget._ellipsis_enabled
                and ChatMessageWidget._ellipsis_active):
            return
        hid = "color:transparent;"
        vis = ""

        alive = []
        for w in ChatMessageWidget._ellipsis_widgets:
            try:
                if not w._visible_in_viewport:
                    alive.append(w)
                    continue

                html = w._current_html
                # gid -> current state (0=all hidden, 1=dot1, 2=dot1+2, 3=all)
                states = dict(zip(w._ellipsis_groups, w._ellipsis_states))
                changed = [False]

                def _dot(m, _states=states, _changed=changed):
                    state = _states.get(int(m.group(2)))
                    if state is None:
                        return m.group(0)
                    new_style = vis if state >= int(m.group(3)) else hid
                    if new_style != m.group(4):
                        _changed[0] = True
                    return m.group(1) + new_style + m.group(5)

                # One O(n) regex pass over the HTML instead of 3 scans per group.
                html = _ELLIPSIS_DOT_RE.sub(_dot, html)
                w._ellipsis_states = [
                    (s + 1) % 4 for s in w._ellipsis_states]

                if changed[0]:
                    w._current_html = html
                    w._body.setText(html)
                alive.append(w)
            except RuntimeError:
                pass
        ChatMessageWidget._ellipsis_widgets = alive

        if not alive and ChatMessageWidget._ellipsis_timer:
            ChatMessageWidget._ellipsis_timer.stop()
            ChatMessageWidget._ellipsis_timer = None


    def _build_plain(self):
        """Render as one flat, flowing text block - no frame, no per-message
        panel, no height cap. Mirrors the vispy_dashboard transcript model:
        text flows into the shared scroll surface and grows freely."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)
        layout.setSpacing(0)
        body = QLabel()
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        # Selectable AND link-clickable (tool chips are toolmeta: anchors).
        body.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.LinksAccessibleByMouse)
        body.setOpenExternalLinks(False)
        body.linkActivated.connect(self._on_body_link)
        body.linkHovered.connect(lambda href, b=body: self._on_body_link_hover(href, b))
        # Kill the dotted anchor focus-rect at the style layer. This body keeps
        # focus (it's text-selectable), so NoFocus isn't an option here — the
        # proxy style is what actually removes the 90s-hyperlink dotted box.
        body.setStyle(_no_focus_rect_style())
        # Text label shows an I-beam (it's selectable) instead of inheriting the
        # row's click-to-copy pointing-hand cursor.
        body.setCursor(Qt.CursorShape.IBeamCursor)
        from ui.theme import selection_css
        body.setStyleSheet(
            f"QLabel {{ background: transparent; border: none; "
            f"padding: 0; {selection_css()} }}")
        body.setMinimumWidth(0)
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        # Run through the ellipsis markup so plain mode ALSO gets the animated
        # ellipsis (when enabled) — the animator reads _current_html and writes
        # back to _body, identical to fancy mode.
        plain_html = self._apply_ellipsis_markup(self._make_plain_html())
        self._base_html = plain_html
        self._current_html = plain_html
        body.setText(plain_html)
        self._body = body
        self._body_pads = (0, 0)  # plain-mode stylesheet has padding: 0
        layout.addWidget(body)
        # Pasted-text cards (collapsed/expandable) under the typed text.
        self._add_paste_cards(layout)
        # Stay frameless so rows read as one continuous transcript.
        self.setFrameShape(QFrame.Shape.NoFrame)

    def _make_plain_html(self) -> str:
        """Plain-mode content: bold sender header + body. Tool chips use the same
        bordered rows as fancy mode; inline timelines reuse cached timeline HTML."""
        import html as _html
        p = PALETTE
        fs = self._font_size
        small = max(fs - 3, 6)
        if self.sender == "You":
            header_color, text_color = p["glow_hot"], p["muted_text"]
        elif self.sender == "Error":
            header_color, text_color = p["danger"], p["danger"]
        else:
            header_color, text_color = p["glow_hot"], p["text"]
        sp = _sparkle_img_html() if self.sender == AGENT_LABEL else ""
        parts: list[str] = []
        if not getattr(self, "_continuation", False):
            ts_html = ""
            if self._show_timestamps and self._timestamp:
                import time as _t
                ts = _t.strftime("%H:%M:%S", _t.localtime(self._timestamp))
                ts_html = (f' <span style="color:{p["accent_muted"]};font-size:{small}pt;">'
                           f'{ts}</span>')
            parts.append(
                f'<span style="color:{header_color};font-weight:bold;font-size:{fs}pt;">'
                f'{_html.escape(self.sender)}</span>{sp}{ts_html}<br>',
            )
        if getattr(self, "_inline_timeline", False) and self._cached_html:
            parts.append(
                f'<div style="color:{text_color};font-size:{fs}pt;">'
                f'{self._cached_html}</div>'
            )
        else:
            if self.tool_names and self._show_tool_chips:
                chip_row = tool_calls_display_html(
                    self.tool_names, fs,
                    mode=ChatMessageWidget._tool_display_mode,
                    show_hint=ChatMessageWidget._show_tools_hint,
                    margin="4px 0 6px 0",
                )
                if chip_row:
                    parts.append(chip_row)
            # quote=False keeps literal " in the text (it's body content, not an
            # attribute) so the emphasis pass can match quoted runs. < & > are
            # still escaped.
            body_text = _html.escape(self.content or "", quote=False)
            body_text = body_text.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
            body_text = _emphasize_html(body_text)  # brighten quotes / labels
            parts.append(
                f'<span style="color:{text_color};font-size:{fs}pt;">{body_text}</span>'
            )
        return "".join(parts)

    def set_visible_in_viewport(self, visible: bool):
        """Called by parent scroll area to pause/resume animation."""
        self._visible_in_viewport = visible

    def apply_wrap_width(self, max_width: int):
        """Constrain label width so Qt doesn't recalc word-wrap on every resize."""
        if max_width > 0 and hasattr(self, '_body'):
            self._wrap_width = max_width
            self._body.setMaximumWidth(max_width)
            self._sync_body_min_height()

    def _sync_body_min_height(self):
        """Pin the body label's minimum height to an explicitly measured value.

        QLabel's heightForWidth for word-wrapped rich text goes stale — its
        internal size cache can survive width/content changes that land between
        layout passes — so the scroll container ends up 1-2 lines shorter than
        the real content: the "last lines clipped below the pane even at max
        scroll" bug. Measuring the document ourselves and setting an explicit
        minimum makes the layout minimum authoritative; the scroll area can then
        never under-size the container, so the scrollbar range always covers the
        full transcript regardless of what QLabel's cache claims."""
        body = getattr(self, "_body", None)
        if body is None or self._wrap_width <= 0:
            return
        pad_h, pad_v = self._body_pads
        text_w = self._wrap_width - pad_h
        if text_w <= 0:
            return
        # _wrap_width is the label's maximum width — a CAP, not a guarantee.
        # When the layout gives the label LESS than the cap, text wraps onto
        # more lines than a cap-width measurement predicts, so the pinned
        # minimum comes out 1-2 lines short and the tail clips below the pane
        # (until a resize re-measures at the true width — the "changing window
        # geometry fixes it" symptom). Measure at the real laid-out width.
        if body.width() > 0:
            actual_w = body.width() - pad_h
            if 0 < actual_w < text_w:
                text_w = actual_w
        try:
            doc = QTextDocument()
            doc.setDefaultFont(body.font())
            doc.setDocumentMargin(0)
            doc.setHtml(self._current_html or body.text())
            doc.setTextWidth(text_w)
            body.setMinimumHeight(math.ceil(doc.size().height()) + pad_v)
        except Exception:
            pass

    def _format_timestamp(self) -> str:
        if not self._show_timestamps or not self._timestamp:
            return ""
        import time as _t
        p = PALETTE
        fs = max(self._font_size - 3, 6)
        local = _t.localtime(self._timestamp)
        time_str = _t.strftime("%I:%M %p", local).lstrip("0")
        # Add date for messages before today
        if local[:3] != _t.localtime()[:3]:
            date_str = _t.strftime("%B %d, %Y", local).replace(" 0", " ")
            time_str = f"{time_str} ({date_str})"
        return f' <span style="color:{p["accent_muted"]};font-size:{fs}pt;">{time_str}</span>'

    def _format_usage(self) -> str:
        if not self._show_usage or not self._usage:
            return ""
        p = PALETTE
        fs = max(self._font_size - 3, 6)
        u = self._usage
        parts = [f"{u.get('prompt_tokens', 0):,} in / {u.get('completion_tokens', 0):,} out"]
        cr = u.get("cache_read", 0)
        cw = u.get("cache_write", 0)
        if cr or cw:
            parts.append(f"cache: {cr:,} read, {cw:,} write")
        # Total tokens processed this turn — includes EVERY round of tool calls
        # and any thinking. Sum across rounds = prompt + cache_read + cache_write.
        ctx_tokens = u.get("ctx_input_tokens", 0)
        if ctx_tokens:
            parts.append(f"ctx: {ctx_tokens:,} tokens")
        return (f'<div style="color:{p["border"]};font-size:{fs}pt;margin-top:1px;">'
                f'{" | ".join(parts)}</div>')

    def _toggle_image_expand(self, event=None):
        """Show full-size image as a centered overlay on the main window."""
        if not hasattr(self, '_full_pixmap'):
            return
        ImageOverlay.show_image(self._full_pixmap, self.window())

    @staticmethod
    def _load_image_pixmap(path: str, max_size: int = 800) -> QPixmap | None:
        """Load an image. For small/cached files uses Qt directly.
        For large originals, falls back to PIL with resize."""
        try:
            # Fast path: try Qt directly (works for cached thumbs and small images)
            file_size = os.path.getsize(path)
            if file_size < 5_000_000:  # under 5MB — Qt can handle it
                qimg = QImage(str(path))
                if not qimg.isNull():
                    pm = QPixmap.fromImage(qimg)
                    if pm.width() > max_size or pm.height() > max_size:
                        pm = pm.scaled(max_size, max_size,
                            Qt.AspectRatioMode.KeepAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)
                    return ChatMessageWidget._maybe_mono(pm)

            # Slow path: PIL for huge files
            from PIL import Image
            import io

            Image.MAX_IMAGE_PIXELS = _THUMB_MAX_DECODE_PIXELS
            try:
                img = Image.open(path)
            except Image.DecompressionBombError:
                return None
            long_edge = max(img.size)
            if long_edge > max_size:
                scale = max_size / long_edge
                img = img.resize(
                    (int(img.size[0] * scale), int(img.size[1] * scale)),
                    Image.LANCZOS)
            if img.mode in ("RGBA", "LA", "PA"):
                fmt = "PNG"
            else:
                img = img.convert("RGB")
                fmt = "JPEG"
            buf = io.BytesIO()
            img.save(buf, format=fmt, quality=85 if fmt == "JPEG" else None)
            qimg = QImage()
            qimg.loadFromData(buf.getvalue())
            if qimg.isNull():
                return None
            return ChatMessageWidget._maybe_mono(QPixmap.fromImage(qimg))
        except Exception:
            return None

    @staticmethod
    def _maybe_mono(pm: "QPixmap | None") -> "QPixmap | None":
        """Apply the monochrome accent tint to a chat image when the
        Monocolor → Images option is on."""
        if pm is None or not getattr(ChatMessageWidget, "_monocolor_images", False):
            return pm
        return ChatMessageWidget._apply_monocolor(pm)

    @staticmethod
    def _apply_monocolor(pm: QPixmap) -> QPixmap:
        """Desaturate then accent-tint a pixmap (luminance × accent), preserving
        the original alpha. Fast Qt compositing — no per-pixel Python loop."""
        try:
            accent = QColor(PALETTE.get("accent", "#33ff99"))
            img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
            gray = (img.convertToFormat(QImage.Format.Format_Grayscale8)
                       .convertToFormat(QImage.Format.Format_ARGB32))
            out = QImage(img.size(), QImage.Format.Format_ARGB32)
            out.fill(Qt.GlobalColor.transparent)
            painter = QPainter(out)
            painter.drawImage(0, 0, gray)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
            painter.fillRect(out.rect(), accent)
            # Restore the original alpha channel (transparency / rounded corners).
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            painter.drawImage(0, 0, img)
            painter.end()
            return QPixmap.fromImage(out)
        except Exception:
            return pm

    def recolor_in_place(self) -> None:
        """Re-render this bubble's HTML with the CURRENT palette, without tearing
        the widget down — used to recolor the transcript live during a streaming
        turn (a full teardown/rebuild would collide with the stream-flush timer).
        Re-reads PALETTE via _make_*_html, so a prior refresh_palette() applies."""
        if self._chat_mode == "plain":
            html = self._apply_ellipsis_markup(self._make_plain_html())
        else:
            html = self._apply_ellipsis_markup(self._make_combined_html())
        self._base_html = html
        self._current_html = html
        if hasattr(self, "_body"):
            self._body.setText(html)
            self._sync_body_min_height()
        if self._selected:
            self._apply_selected_style()
        else:
            self._apply_base_style()

    def _apply_base_style(self):
        self.setStyleSheet(
            "QFrame#ChatMsg { background: transparent; border: none; margin: 0; padding: 0; }"
        )

    def _apply_hover_style(self):
        if self._chat_mode == "plain":
            return
        p = PALETTE
        c = QColor(p["accent"])
        self.setStyleSheet(
            f"QFrame#ChatMsg {{ background: rgba({c.red()},{c.green()},{c.blue()},0.07); "
            f"border: none; margin: 0; padding: 0; }}"
        )

    def _apply_selected_style(self):
        if self._chat_mode == "plain":
            return
        p = PALETTE
        c = QColor(p["accent"])
        self.setStyleSheet(
            f"QFrame#ChatMsg {{ background: rgba({c.red()},{c.green()},{c.blue()},0.18); "
            f"border: none; margin: 0; padding: 0; }}"
        )

    def enterEvent(self, event):
        if self._selected:
            self._apply_selected_style()
        else:
            self._apply_hover_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._selected:
            self._apply_selected_style()
        else:
            self._apply_base_style()
        super().leaveEvent(event)

    def set_context_outside_window(self, outside: bool) -> None:
        """Dim rows outside the summarized context window without QGraphicsOpacityEffect."""
        outside = bool(outside)
        if self._context_outside_window == outside:
            return
        self._context_outside_window = outside
        if outside:
            if self._ctx_dim_overlay is None:
                overlay = QFrame(self)
                overlay.setObjectName("CtxDimOverlay")
                overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                overlay.setFrameShape(QFrame.Shape.NoFrame)
                overlay.setStyleSheet("background-color: rgba(0, 0, 0, 110); border: none;")
                self._ctx_dim_overlay = overlay
            self._ctx_dim_overlay.setGeometry(self.rect())
            self._ctx_dim_overlay.show()
            self._ctx_dim_overlay.raise_()
        else:
            if self._ctx_dim_overlay is not None:
                self._ctx_dim_overlay.hide()
        if isinstance(self.graphicsEffect(), QGraphicsOpacityEffect):
            self.setGraphicsEffect(None)

    def resizeEvent(self, event):
        o = self._ctx_dim_overlay
        if o is not None and o.isVisible():
            o.setGeometry(self.rect())
        super().resizeEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            if self._selected:
                self.deselect()
            else:
                self.select()
                QApplication.clipboard().setText(self.content)
        else:
            super().mousePressEvent(event)

    def select(self):
        try:
            from core.sounds import play_ui
            play_ui("select_message.mp3")
        except Exception:
            pass
        prev = ChatMessageWidget._selected_widget
        if prev is not None and prev is not self:
            try:
                prev.deselect()
            except RuntimeError:
                ChatMessageWidget._selected_widget = None
        self._selected = True
        self._apply_selected_style()
        ChatMessageWidget._selected_widget = self

    def deselect(self):
        self._selected = False
        self._apply_base_style()
        if ChatMessageWidget._selected_widget is self:
            ChatMessageWidget._selected_widget = None



# ──────────────────────────────────────────────────────────────────────
# Terminal syntax highlighting — accent-derived colors
# ──────────────────────────────────────────────────────────────────────

def _terminal_highlight(text: str) -> str:
    """Colorize terminal output using theme-derived shades.
    Returns HTML with inline color spans."""
    import re as _re
    import html as _html
    p = PALETTE
    accent = QColor(p["accent"])

    # Derive shades from accent
    c_bright = p["accent_bright"]   # numbers, important values
    c_accent = p["accent"]          # keywords, commands
    c_muted = p["accent_muted"]     # punctuation, operators
    c_string = p["muted_text"]      # strings
    c_error = p["accent_muted"]     # errors, tracebacks — dimmed, not red
    c_dim = f"rgb({max(accent.red()//3,40)},{max(accent.green()//3,40)},{max(accent.blue()//3,40)})"  # separators

    lines = text.split("\n")
    result = []

    for line in lines:
        safe = _html.escape(line)

        # Error lines — full red
        if any(kw in line.lower() for kw in ("error", "traceback", "exception", "failed")):
            result.append(f'<span style="color:{c_error}">{safe}</span>')
            continue

        # Separator lines (===, ---, etc.)
        stripped = line.strip()
        if stripped and all(c in "=-_*#" for c in stripped) and len(stripped) > 3:
            result.append(f'<span style="color:{c_dim}">{safe}</span>')
            continue

        # Token-level highlighting
        def _colorize(m):
            tok = m.group(0)
            safe_tok = _html.escape(tok)
            # Numbers (including decimals, percentages, currency)
            if _re.match(r'^[\$]?[\d,]+\.?\d*%?$', tok):
                return f'<span style="color:{c_bright}">{safe_tok}</span>'
            # Quoted strings
            if (tok.startswith('"') and tok.endswith('"')) or (tok.startswith("'") and tok.endswith("'")):
                return f'<span style="color:{c_string}">{safe_tok}</span>'
            # UP/DOWN/OK/PASS/FAIL indicators
            if tok.upper() in ("UP", "OK", "PASS", "SUCCESS", "TRUE", "YES"):
                return f'<span style="color:{c_bright}">{safe_tok}</span>'
            if tok.upper() in ("DOWN", "FAIL", "FALSE", "NO", "ERROR"):
                return f'<span style="color:{c_error}">{safe_tok}</span>'
            # Brackets and punctuation
            if tok in ("(", ")", "[", "]", "{", "}", "|", ":", ";", "=", "->", "=>"):
                return f'<span style="color:{c_muted}">{safe_tok}</span>'
            return safe_tok

        # Match tokens: numbers, quoted strings, brackets, words
        highlighted = _re.sub(
            r'\"[^\"]*\"|\'[^\']*\'|[\$]?[\d,]+\.?\d*%?|[\(\)\[\]\{\}\|:;=]|->|=>|\b[A-Z]{2,}\b',
            _colorize, safe
        )
        result.append(highlighted)

    return "<br>".join(result)


# ──────────────────────────────────────────────────────────────────────
# Plan widget — live in-flight work progress
# ──────────────────────────────────────────────────────────────────────

class PlanWidget(QFrame):
    """Live-updating plan card showing task progress during inference."""

    _STATUS_ICONS = {
        "pending": "\u2022",      # •
        "in_progress": "\u25B6",  # ▶
        "done": "\u2713",         # ✓
        "skipped": "\u2014",      # —
        "blocked": "\u2716",      # ✖
    }

    COLLAPSED_HEIGHT = 120
    EXPANDED_HEIGHT = 440
    _RADIUS = "8px"
    _SIDE_PAD = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("PlanCard")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._expanded = True       # plans default open — you want to read them
        self._title = ""
        self._steps: list[dict] = []
        self._last_updated = -1
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._refresh)
        self._build_ui()

    def _build_ui(self):
        p = PALETTE
        self.setStyleSheet(
            f"QFrame#PlanCard {{ background:{p.get('panel_alt', '#101010')};"
            f" border:1px solid {p.get('border', '#333')};"
            f" border-radius:{self._RADIUS};"
            f" margin-left:{self._SIDE_PAD}px; margin-right:{self._SIDE_PAD}px; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

        self._header = QPushButton(self._header_text())
        self._header.setObjectName("PlanHdr")
        self._header.setFlat(True)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        self._header.setStyleSheet(
            f"QPushButton#PlanHdr {{ color:{p.get('accent', '#33ff99')};"
            f" background:{p.get('panel', '#0c0c0c')};"
            f" border:none; border-bottom:1px solid {p.get('border', '#333')};"
            f" border-top-left-radius:{self._RADIUS}; border-top-right-radius:{self._RADIUS};"
            f" font:bold 9pt Consolas; text-align:left; padding:4px 8px; }}"
            f"QPushButton#PlanHdr:hover {{ color:{p.get('glow_hot', p.get('accent', '#aef'))}; }}")
        self._header.clicked.connect(self._toggle_expand)
        lay.addWidget(self._header)

        ed = QPlainTextEdit()
        ed.setObjectName("PlanBody")
        ed.setReadOnly(True)
        ed.setFont(QFont("Consolas", max(ChatMessageWidget._font_size - 1, 8)))
        ed.setFrameShape(QFrame.Shape.NoFrame)
        ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        ed.document().setDocumentMargin(6)
        ed.setStyleSheet(self._body_stylesheet(p))
        self._body = ed
        lay.addWidget(ed)
        self._apply_height()

    def _header_text(self) -> str:
        n = len(self._steps)
        title = self._title or "Plan"
        done = sum(1 for s in self._steps if s.get("status") == "done")
        prog = f" · {done}/{n} done" if n else ""
        tip = "click to collapse" if self._expanded else "click to expand"
        return f"{title} · {n} steps{prog}  —  {tip}"

    def _plan_text(self) -> str:
        lines = []
        for s in self._steps:
            icon = self._STATUS_ICONS.get(s.get("status"), self._STATUS_ICONS["pending"])
            lines.append(f"{icon}  {s.get('label', '')}")
        return "\n".join(lines)

    def _set_plan(self, title: str, steps: list):
        self._title = title or self._title
        self._steps = steps or []
        self._header.setText(self._header_text())
        sb = self._body.verticalScrollBar()
        pos = sb.value()
        self._body.setPlainText(self._plan_text())
        sb.setValue(pos)
        self._apply_height()

    def _toggle_expand(self):
        self._expanded = not self._expanded
        self._header.setText(self._header_text())
        self._apply_height()

    def _apply_height(self):
        from PyQt6.QtGui import QFontMetricsF
        cap = self.EXPANDED_HEIGHT if self._expanded else self.COLLAPSED_HEIGHT
        line_h = QFontMetricsF(self._body.font()).height()
        wanted = int(line_h * max(1, len(self._steps)) + 16)
        h = min(max(wanted, 36), cap)
        self._body.setMinimumHeight(h)
        self._body.setMaximumHeight(h)

    @staticmethod
    def _body_stylesheet(p: dict) -> str:
        bg = p.get("panel_alt", "#101010")
        fg = p.get("text", "#ddd")
        thumb = p.get("accent_muted", p.get("border", "#444"))
        track = p.get("panel", "#0c0c0c")
        border = p.get("border", "#333")
        return (
            f"QPlainTextEdit#PlanBody {{ background:{bg}; color:{fg}; border:none;"
            f" border-bottom-left-radius:{PlanWidget._RADIUS};"
            f" border-bottom-right-radius:{PlanWidget._RADIUS}; }}"
            f"QScrollBar:vertical {{ background:{track}; width:9px; margin:0;"
            f" border:1px solid {border}; }}"
            f"QScrollBar::handle:vertical {{ background:{thumb}; border-radius:0;"
            f" min-height:24px; }}"
            f"QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}"
            f"QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}"
        )

    def start_polling(self):
        self._poll_timer.start(400)

    def stop_polling(self):
        self._poll_timer.stop()

    def _refresh(self):
        from tools.plan import get_current_plan
        plan = get_current_plan()
        if not plan:
            if self._steps:
                self.stop_polling()  # plan finished — keep the final text shown
            return
        if plan.get("updated_at", 0) == self._last_updated:
            return
        self._last_updated = plan.get("updated_at", 0)
        self._set_plan(plan.get("title", ""), plan.get("steps", []))

    def set_final_state(self, plan_data: dict):
        """Static final state (persisted after the plan finishes)."""
        self.stop_polling()
        if not plan_data:
            return
        self._set_plan(plan_data.get("title", ""), plan_data.get("steps", []))


# ──────────────────────────────────────────────────────────────────────
# Terminal TV — live command output in the top panel (beside Browser TV)
# ──────────────────────────────────────────────────────────────────────

class TerminalTV(QWidget):
    """Live terminal output viewer, styled to match BrowserTV."""

    closed = pyqtSignal()
    output_ready = pyqtSignal(str, str)  # (command, captured_output)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        p = PALETTE

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header bar
        header = QHBoxLayout()
        header.setContentsMargins(6, 3, 6, 3)
        header.setSpacing(4)

        self._cmd_label = QLabel("Terminal")
        self._cmd_label.setFont(QFont("Consolas", max(ChatMessageWidget._font_size - 2, 7)))
        self._cmd_label.setStyleSheet(f"color:{p['accent_muted']};border:none;")
        header.addWidget(self._cmd_label, stretch=1)

        self._status = QLabel("")
        self._status.setFont(QFont("Consolas", max(ChatMessageWidget._font_size - 2, 7), QFont.Weight.Bold))
        self._status.setStyleSheet(f"color:{p['accent']};border:none;")
        header.addWidget(self._status)

        header_w = QWidget()
        header_w.setLayout(header)
        header_w.setStyleSheet(f"background:{p['panel']};border-bottom:1px solid {p['border']};")
        layout.addWidget(header_w)

        # Output area
        self._output = QTextEdit()
        self._output.setReadOnly(True)
        self._output.setFont(QFont("Consolas", 9))
        self._output.setStyleSheet(f"""
            QTextEdit {{
                background: {p['panel_alt']};
                color: {p['text']};
                border: none;
                {_mono_selection_qss(p)}
            }}
        """)
        layout.addWidget(self._output, stretch=1)

        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)
        self._done = False
        self._auto_close_timer = None
        self._exit_code = None
        self._owned_queue = None   # snapshot set on start() — each viewer owns its queue
        self._idle_ticks = 0       # ticks with no output, for timeout

    def start(self, command: str):
        """Begin showing output for a new command."""
        # Cancel any pending timers from previous run
        self._poll_timer.stop()
        if self._auto_close_timer:
            self._auto_close_timer.stop()
            self._auto_close_timer = None
        self._done = False
        self._emitted = False
        self._exit_code = None
        self._idle_ticks = 0
        self._output.clear()
        cmd_short = command[:80] + ("..." if len(command) > 80 else "")
        self._cmd_label.setText(f"$ {cmd_short}")
        self._status.setText("running...")

        # Snapshot the current output queue so this viewer owns it exclusively.
        # Without this, multiple viewers race on the same global queue and steal
        # each other's sentinels, causing timers to run forever.
        from tools.terminal import get_output_queue
        self._owned_queue = get_output_queue()

        self._poll_timer.start(100)  # 100ms — was 50ms; HTML rendering is expensive

    def _poll(self):
        try:
            q = self._owned_queue
            if not q:
                return
            count = 0
            got_sentinel = False
            while count < 50:  # was 100; cap burst to reduce per-tick HTML work
                try:
                    line = q.get_nowait()
                except Exception:
                    break
                if line is None:
                    got_sentinel = True
                    break
                # Intercept exit code marker from terminal tool
                if isinstance(line, str) and line.startswith("__EXIT_CODE__:"):
                    try:
                        self._exit_code = int(line.split(":", 1)[1])
                    except (ValueError, IndexError):
                        pass
                    continue
                # Syntax-highlighted HTML
                cursor = self._output.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                cursor.insertHtml(_terminal_highlight(line.rstrip()) + "<br>")
                count += 1
            if count > 0:
                sb = self._output.verticalScrollBar()
                sb.setValue(sb.maximum())
                self._idle_ticks = 0
            else:
                self._idle_ticks += 1
                # 60s timeout: stop polling if no output arrives (guards against lost sentinels)
                if self._idle_ticks > 600:
                    self._finish()
                    return
            if got_sentinel:
                self._finish()
        except Exception:
            pass

    def _finish(self):
        self._done = True
        self._status.setText("done")
        self._poll_timer.stop()
        # Emit captured output for the chat log card (once per run)
        # Skip card for failed commands — the agent's response already explains the error
        if not self._emitted:
            self._emitted = True
            if self._exit_code is None or self._exit_code == 0:
                output_text = self._output.toPlainText().strip()
                cmd_text = self._cmd_label.text().lstrip("$ ").strip()
                if output_text:
                    self.output_ready.emit(cmd_text, output_text)
        # Auto-close the TV panel
        if self._auto_close_timer:
            self._auto_close_timer.stop()
        self._auto_close_timer = QTimer(self)
        self._auto_close_timer.setSingleShot(True)
        self._auto_close_timer.timeout.connect(self.closed.emit)
        self._auto_close_timer.start(2000)

    def stop(self):
        self._poll_timer.stop()
        if self._auto_close_timer:
            self._auto_close_timer.stop()
        self._done = True


class LiveTerminalCard(QFrame):
    """Inline terminal card that shows live output, then becomes a static card."""

    finished = pyqtSignal(int)  # exit_code

    MAX_STUB_LINES = 4

    def __init__(self, command: str, parent=None):
        super().__init__(parent)
        self._command = command
        self._lines: list[str] = []
        self._exit_code: int | None = None
        self._done = False
        self.setObjectName("TerminalCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._build_ui()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)

    def start_polling(self):
        self._poll_timer.start(100)

    def stop_polling(self):
        self._poll_timer.stop()

    def _build_ui(self):
        p = PALETTE
        fs = ChatMessageWidget._font_size
        fs_body = max(fs - 2, 7)
        fs_tag = max(fs - 3, 6)
        muted = p["accent_muted"]
        card_bg = p["panel_alt"]
        border_c = p["border"]

        self.setStyleSheet(f"""
            QFrame#TerminalCard {{
                background: {card_bg};
                border: 1px solid {border_c};
                border-top: 2px solid {muted};
                border-radius: 6px;
                margin: 4px 10px;
            }}
        """)
        self._card_layout = QVBoxLayout(self)
        self._card_layout.setContentsMargins(0, 0, 0, 0)
        self._card_layout.setSpacing(0)

        # IN section
        in_section = QWidget()
        in_lay = QHBoxLayout(in_section)
        in_lay.setContentsMargins(8, 4, 8, 4)
        in_lay.setSpacing(6)
        in_tag = QLabel("IN")
        in_tag.setFont(QFont("Consolas", fs_tag, QFont.Weight.Bold))
        in_tag.setFixedWidth(22)
        in_tag.setStyleSheet(f"color:{muted};border:none;")
        in_lay.addWidget(in_tag)
        cmd_short = self._command if len(self._command) <= 80 else self._command[:77] + "..."
        in_text = QLabel(f"$ {cmd_short}")
        in_text.setFont(QFont("Consolas", fs_body))
        in_text.setStyleSheet(f"color:{p['text']};border:none;")
        in_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        in_lay.addWidget(in_text, stretch=1)

        self._status_label = QLabel("running...")
        self._status_label.setFont(QFont("Consolas", fs_tag, QFont.Weight.Bold))
        self._status_label.setStyleSheet(f"color:{p['accent']};border:none;")
        in_lay.addWidget(self._status_label)
        self._card_layout.addWidget(in_section)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background:{border_c};max-height:1px;border:none;")
        self._card_layout.addWidget(sep)

        # OUT section — live output
        out_section = QWidget()
        out_lay = QHBoxLayout(out_section)
        out_lay.setContentsMargins(8, 4, 8, 4)
        out_lay.setSpacing(6)
        out_tag = QLabel("OUT")
        out_tag.setFont(QFont("Consolas", fs_tag, QFont.Weight.Bold))
        out_tag.setFixedWidth(22)
        out_tag.setStyleSheet(f"color:{muted};border:none;")
        out_tag.setAlignment(Qt.AlignmentFlag.AlignTop)
        out_lay.addWidget(out_tag)
        self._out_text = QLabel("")
        self._out_text.setFont(QFont("Consolas", fs_body))
        self._out_text.setStyleSheet(f"color:{p['text']};border:none;")
        self._out_text.setWordWrap(True)
        self._out_text.setTextFormat(Qt.TextFormat.RichText)
        self._out_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        out_lay.addWidget(self._out_text, stretch=1)
        self._out_section = out_section
        self._card_layout.addWidget(out_section)

    def _poll(self):
        from tools.terminal import get_output_queue
        q = get_output_queue()
        if not q:
            return
        changed = False
        for _ in range(200):
            try:
                line = q.get_nowait()
            except Exception:
                break
            if line is None:
                self._finalize()
                return
            if isinstance(line, str) and line.startswith("__EXIT_CODE__:"):
                try:
                    self._exit_code = int(line.split(":", 1)[1])
                except (ValueError, IndexError):
                    pass
                continue
            if isinstance(line, str):
                self._lines.append(line.rstrip())
                # Cap stored lines — only the tail matters for display
                if len(self._lines) > 500:
                    self._lines = self._lines[-200:]
                changed = True
        if changed:
            self._update_display()

    def _update_display(self):
        # Show last N lines as stub
        tail = self._lines[-self.MAX_STUB_LINES:]
        self._out_text.setText(_terminal_highlight("\n".join(tail)))

    def _finalize(self):
        if self._done:
            return
        self._done = True
        self.stop_polling()
        p = PALETTE
        ec = self._exit_code or 0
        if ec == 0:
            self._status_label.setText("done")
            self._status_label.setStyleSheet(f"color:{p['accent']};border:none;")
        else:
            self._status_label.setText(f"exit {ec}")
            self._status_label.setStyleSheet(f"color:{p['danger']};border:none;")
        self._update_display()
        self.finished.emit(ec)

    def set_final_output(self, output: str, exit_code: int = 0):
        """Restore from persisted state (not live)."""
        self._lines = output.split("\n") if output else []
        self._exit_code = exit_code
        self._done = True
        p = PALETTE
        if exit_code == 0:
            self._status_label.setText("done")
            self._status_label.setStyleSheet(f"color:{p['accent']};border:none;")
        else:
            self._status_label.setText(f"exit {exit_code}")
            self._status_label.setStyleSheet(f"color:{p['danger']};border:none;")
        self._update_display()

    def get_output(self) -> str:
        return "\n".join(self._lines)


# ──────────────────────────────────────────────────────────────────────
# Sub-agent job card — live-updating card for parallel sub-agent tasks
# ──────────────────────────────────────────────────────────────────────

class SubAgentCard(QFrame):
    """Inline card showing sub-agent job status with per-task progress.

    Live state: polls orchestrator, shows spinning indicator per task.
    Completed state: compact summary with results.
    """

    finished = pyqtSignal(dict)  # summary dict

    def __init__(self, job_id: str, tasks: list[dict], parent=None):
        super().__init__(parent)
        self._job_id = job_id
        self._tasks = {t["task_id"]: t for t in tasks}
        self._done = False
        self.setObjectName("SubAgentCard")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._build_ui()
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll)

    def start_polling(self):
        self._poll_timer.start(400)

    def stop_polling(self):
        self._poll_timer.stop()

    def _build_ui(self):
        p = PALETTE
        fs = ChatMessageWidget._font_size
        # Compact card (~3 lines for header + one task row)
        fs_body = max(fs - 4, 6)
        fs_tag = max(fs - 5, 6)

        self.setStyleSheet(f"""
            QFrame#SubAgentCard {{
                background: {p['panel_alt']};
                border: 1px solid {p['border']};
                border-left: 2px solid {p['accent']};
                border-radius: 3px;
                margin: 0;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 1, 4, 1)
        lay.setSpacing(0)

        # Header
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        title = QLabel(f"Sub-agents ({len(self._tasks)} tasks)")
        title.setFont(QFont("Consolas", fs_body, QFont.Weight.Bold))
        title.setStyleSheet(f"color:{p['text']};border:none;padding:0;margin:0;")
        header.addWidget(title, stretch=1)
        self._status_label = QLabel("dispatching...")
        self._status_label.setFont(QFont("Consolas", fs_tag, QFont.Weight.Bold))
        self._status_label.setStyleSheet(f"color:{p['accent']};border:none;padding:0;margin:0;")
        header.addWidget(self._status_label)
        lay.addLayout(header)

        # Task rows
        self._task_rows: dict[str, QLabel] = {}
        for tid, task in self._tasks.items():
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(3)
            status_dot = QLabel("-")
            status_dot.setFont(QFont("Consolas", fs_body))
            status_dot.setFixedWidth(9)
            status_dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
            status_dot.setStyleSheet(f"color:{p['muted_text']};border:none;padding:0;margin:0;")
            row.addWidget(status_dot)
            t_title = (task.get("title", tid) or "")[:40]
            desc = QLabel(f"{t_title} [{task.get('mode', 'general')}]")
            desc.setFont(QFont("Consolas", fs_tag))
            desc.setWordWrap(False)
            desc.setStyleSheet(f"color:{p['text']};border:none;padding:0;margin:0;")
            row.addWidget(desc, stretch=1)
            task_status = QLabel("pending")
            task_status.setFont(QFont("Consolas", fs_tag))
            task_status.setStyleSheet(f"color:{p['muted_text']};border:none;padding:0;margin:0;")
            row.addWidget(task_status)
            lay.addLayout(row)
            self._task_rows[tid] = (status_dot, desc, task_status)

    def update_task(self, task_id: str, status: str, data: dict = None):
        """Update a single task's display."""
        p = PALETTE
        if task_id not in self._task_rows:
            return
        dot, desc, label = self._task_rows[task_id]

        if status == "running":
            dot.setText(">")
            dot.setStyleSheet(f"color:{p['accent']};border:none;")
            d = data or {}
            round_num = d.get("round")
            max_rounds = d.get("max_rounds", 15)
            tool = d.get("current_tool", "")
            if tool:
                args_s = d.get("current_args", "")
                display = f"r{round_num}/{max_rounds} {tool}"
                if args_s:
                    display += f"({args_s[:28]})"
            elif round_num:
                display = f"round {round_num}/{max_rounds}…"
            else:
                display = "working…"
            label.setText(display)
            label.setStyleSheet(f"color:{p['accent']};border:none;")
            activity = d.get("activity", [])
            if activity:
                label.setToolTip("\n".join(activity))
        elif status == "completed":
            dot.setText("+")
            dot.setStyleSheet(f"color:{p['accent']};border:none;")
            label.setText("done")
            label.setStyleSheet(f"color:{p['accent']};border:none;")
            preview = (data or {}).get("result_preview", "")
            if preview:
                label.setToolTip(preview[:500])
        elif status == "failed":
            dot.setText("x")
            dot.setStyleSheet(f"color:{p['danger']};border:none;")
            full_err = (data or {}).get("full_error") or (data or {}).get("error", "failed")
            first_line = full_err.split("\n")[0][:60]
            label.setText(first_line)
            label.setStyleSheet(f"color:{p['danger']};border:none;")
            label.setToolTip(full_err[:2000])

        # Update task data
        if task_id in self._tasks:
            self._tasks[task_id]["status"] = status

    def _poll(self):
        """Check if all tasks are complete."""
        from core.subagent import get_existing
        orch = get_existing(self._job_id)
        if not orch:
            return
        status = orch.get_status()
        if status.get("complete"):
            self._finalize(status)

    def _finalize(self, status: dict = None):
        self._poll_timer.stop()
        self._done = True
        p = PALETTE
        completed = sum(1 for t in self._tasks.values() if t.get("status") == "completed")
        total = len(self._tasks)
        if completed == total:
            self._status_label.setText("all done")
            self._status_label.setStyleSheet(f"color:{p['accent']};border:none;")
        else:
            self._status_label.setText(f"{completed}/{total} done")
            self._status_label.setStyleSheet(f"color:{p['accent_muted']};border:none;")
        self.finished.emit(status or {})

    def set_final_state(self, data: dict):
        """Restore from persisted data (session reload)."""
        self._done = True
        p = PALETTE
        for task_data in data.get("tasks", []):
            tid = task_data.get("task_id", "")
            self.update_task(tid, task_data.get("status", "pending"), task_data)
        completed = sum(1 for t in data.get("tasks", []) if t.get("status") == "completed")
        total = len(data.get("tasks", []))
        self._status_label.setText(f"{completed}/{total} done")
        self._status_label.setStyleSheet(f"color:{p['accent']};border:none;")


class SubAgentCardSlot(QWidget):
    """Centers a sub-agent card with side margins so it reads as a narrow floating card."""

    _SIDE_PAD = 28
    _WIDTH_RATIO = 0.54
    _WIDTH_MIN = 200
    _WIDTH_MAX = 360

    def __init__(self, job_id: str, tasks: list[dict], parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(self._SIDE_PAD, 2, self._SIDE_PAD, 2)
        outer.setSpacing(0)
        outer.addStretch(1)
        self.card = SubAgentCard(job_id, tasks)
        self.card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum)
        outer.addWidget(self.card, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(1)
        self._apply_card_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_card_width()

    def _apply_card_width(self):
        w = self.width()
        if w < 80:
            return
        inner = max(0, w - 2 * self._SIDE_PAD)
        mw = int(max(self._WIDTH_MIN, min(self._WIDTH_MAX, inner * self._WIDTH_RATIO)))
        self.card.setFixedWidth(mw)


def _subagent_card_resolve(widget) -> SubAgentCard | None:
    """Map message row widget (slot or legacy card) to SubAgentCard."""
    if isinstance(widget, SubAgentCardSlot):
        return widget.card
    if isinstance(widget, SubAgentCard):
        return widget
    return None


# ──────────────────────────────────────────────────────────────────────
# Thinking indicator widget
# ──────────────────────────────────────────────────────────────────────

class ThinkingWidget(QFrame):
    """Slim 'Agent is typing...' indicator, centered, appears after 3s delay."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ThinkingMsg")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(24)

        p = PALETTE
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._label = QLabel(f"{AGENT_LABEL} is typing")
        self._label.setFont(QFont("Consolas", 9))
        self._label.setStyleSheet(f"color: {p['accent_muted']}; background: transparent; border: none;")
        layout.addWidget(self._label)

        self._dots = [QLabel(".") for _ in range(3)]
        for dot in self._dots:
            dot.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
            dot.setFixedWidth(8)
            dot.setStyleSheet(f"color: transparent; background: transparent; border: none;")
            layout.addWidget(dot)

        # Invisible initially (space reserved) — text appears after 2s delay
        self._label.setStyleSheet(f"color: transparent; background: transparent; border: none;")
        for dot in self._dots:
            dot.setStyleSheet(f"color: transparent; background: transparent; border: none;")
        self._revealed = False
        self._delay_timer = QTimer(self)
        self._delay_timer.setSingleShot(True)
        self._delay_timer.timeout.connect(self._reveal)
        self._delay_timer.start(2000)

        self._state = 0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._animate)
        self._anim_timer.start(400)

        self.setStyleSheet(
            "QFrame#ThinkingMsg { background: transparent; border: none; }"
        )

        # Tool call tracking (kept for snapshot/restore, no UI)
        self._tool_counts: dict[str, int] = {}

    def add_tool_bubble(self, name: str):
        """Track tool calls (no UI bubbles, just counting for snapshot/restore)."""
        self._tool_counts[name] = self._tool_counts.get(name, 0) + 1

    def _reveal(self):
        """Show the typing indicator text after delay."""
        p = PALETTE
        self._revealed = True
        self._label.setStyleSheet(f"color: {p['accent_muted']}; background: transparent; border: none;")

    def _animate(self):
        if not self._revealed:
            return
        p = PALETTE
        for i, dot in enumerate(self._dots):
            if self._state == 3:
                dot.setStyleSheet(f"color: transparent; background: transparent; border: none;")
            elif i <= self._state:
                dot.setStyleSheet(f"color: {p['accent_muted']}; background: transparent; border: none;")
            else:
                dot.setStyleSheet(f"color: transparent; background: transparent; border: none;")
        self._state = (self._state + 1) % 4

    def stop(self):
        self._anim_timer.stop()
        self._delay_timer.stop()


# ──────────────────────────────────────────────────────────────────────
# Chat input
# ──────────────────────────────────────────────────────────────────────

class _TaskThread(QThread):
    """Runs a scheduled task prompt in an isolated agent, saves result to conversation."""
    task_completed = pyqtSignal(str, str, str)  # conv_id, task_name, reply

    def __init__(self, agent, task: dict, conv_id: str):
        super().__init__()
        self._agent = agent
        self._task = task
        self._conv_id = conv_id

    def run(self):
        import time as _time
        from tools.tasks import mark_task_result
        from core.conversations import load_conversation, save_conversation
        try:
            # Load conversation context so the agent knows what's been said
            data = load_conversation(self._conv_id)
            if data:
                for msg in data.get("messages", []):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role in ("user", "assistant") and content:
                        self._agent.context.append({"role": role, "content": content})
                # Restore conversation settings
                if data.get("system_prompt"):
                    self._agent.set_system_prompt_override(data["system_prompt"])
                self._agent._system_prompt_replace = bool(data.get("prompt_replace", False))
                self._agent._context_note = data.get("context_note", "") or ""
                conv_streams = data.get("streams", [])
                self._agent.set_conversation_streams(conv_streams)
                # Restore pinned working path for this conversation
                self._agent.set_conv_id(self._conv_id)
                self._agent.set_conversation_cwd(data.get("conversation_cwd", ""), persist=False)

            reply = self._agent.chat(self._task["prompt"])
            tool_names = [t["tool"] for t in self._agent.tool_call_log
                          if t.get("success") is not False] if self._agent.tool_call_log else []
            usage = getattr(self._agent, '_turn_usage', None)
            # Append only the assistant response — identical to a real chat message
            data = load_conversation(self._conv_id)
            messages = data.get("messages", []) if data else []
            msg = {
                "role": "assistant",
                "content": reply,
                "tool_names": tool_names,
                "_timestamp": _time.time(),
            }
            if usage and usage.get("prompt_tokens", 0) > 0:
                msg["_usage"] = dict(usage)
            messages.append(msg)
            name = data.get("name", f"Task: {self._task['name']}") if data else f"Task: {self._task['name']}"
            save_conversation(self._conv_id, name, messages)
            mark_task_result(self._task["id"], True)
            self.task_completed.emit(self._conv_id, self._task["name"], reply)
        except Exception as e:
            mark_task_result(self._task["id"], False, str(e))


class _NetworkReplyThread(QThread):
    """Auto-respond to an inbound peer message: run the agent on it in the
    peer's conversation, save the reply, and send it back over the network.
    The triggering peer message is already saved as the conversation's last
    user message, so it becomes the chat prompt rather than duplicated context."""
    completed = pyqtSignal(str, str, str)  # conv_id, node, reply

    def __init__(self, agent, conv_id: str, node: str, reply_url: str):
        super().__init__()
        self._agent = agent
        self._conv_id = conv_id
        self._node = node
        self._reply_url = reply_url

    def run(self):
        import time as _time
        from core.conversations import load_conversation, save_conversation
        try:
            data = load_conversation(self._conv_id) or {}
            messages = data.get("messages", [])
            if not messages or messages[-1].get("role") != "user":
                return
            prompt = messages[-1].get("content", "")
            for msg in messages[:-1]:
                if msg.get("role") in ("user", "assistant") and msg.get("content"):
                    self._agent.context.append(
                        {"role": msg["role"], "content": msg["content"]})
            if data.get("system_prompt"):
                self._agent.set_system_prompt_override(data["system_prompt"])
            self._agent.set_conv_id(self._conv_id)

            reply = self._agent.chat(prompt)

            tool_names = [t["tool"] for t in self._agent.tool_call_log
                          if t.get("success") is not False] if self._agent.tool_call_log else []
            data = load_conversation(self._conv_id) or {}
            messages = data.get("messages", [])
            messages.append({"role": "assistant", "content": reply,
                             "tool_names": tool_names, "_timestamp": _time.time()})
            save_conversation(self._conv_id, data.get("name", f"Network: {self._node}"),
                              messages)

            # Answer the peer: configured address first, then the reply_url the
            # (authenticated) sender embedded in its envelope.
            try:
                from core.network import resolve_peer, send_to_peer
                p = resolve_peer(self._node)
                url = (p or {}).get("url") or self._reply_url
                if url and reply.strip():
                    send_to_peer(url, {"type": "chat", "message": reply})
            except Exception:
                pass
            self.completed.emit(self._conv_id, self._node, reply)
        except Exception as e:
            print(f"[network] auto-respond failed: {e}", flush=True)


class _RemoteHostTurnThread(QThread):
    """Runs a turn for a conversation a PEER is mirroring. The peer sent a user
    message; THIS machine (the host) runs the inference and tools — committing
    locally — and streams live events (user echo, round starts, throttled text
    snapshots, tool chips, final reply) back to every subscriber via the network
    event pump. Same isolated-Agent pattern as task/network reply threads."""
    refreshed = pyqtSignal(str)    # conv_id — host UI refreshes after the reply
    user_saved = pyqtSignal(str)   # conv_id — incoming message saved; refresh now

    def __init__(self, agent, conv_id: str, text: str):
        super().__init__()
        self._agent = agent
        self._conv_id = conv_id
        self._text = text

    def run(self):
        import time as _time
        from core.network import network_manager
        from core.conversations import load_conversation, save_conversation
        cid = self._conv_id

        def emit(kind, **kw):
            network_manager.publish_conv_event(cid, {"kind": kind, **kw})

        try:
            data = load_conversation(cid) or {}
            name = data.get("name", "Conversation")
            messages = data.get("messages", [])
            umsg = {"role": "user", "content": self._text, "_timestamp": _time.time()}
            messages.append(umsg)
            save_conversation(cid, name, messages)
            emit("user", message=umsg)
            self.user_saved.emit(cid)   # host shows the incoming message immediately

            # Hydrate context from prior turns (everything before the new user msg).
            for m in messages[:-1]:
                if m.get("role") in ("user", "assistant") and m.get("content"):
                    self._agent.context.append({"role": m["role"], "content": m["content"]})
            if data.get("system_prompt"):
                self._agent.set_system_prompt_override(data["system_prompt"])
            self._agent._system_prompt_replace = bool(data.get("prompt_replace", False))
            self._agent._context_note = data.get("context_note", "") or ""
            self._agent.set_conv_id(cid)
            self._agent.set_conversation_cwd(data.get("conversation_cwd", ""), persist=False)

            # Live streaming hooks → throttled snapshots over the network.
            acc = {"text": ""}
            last = {"ts": 0.0}

            def on_round():
                acc["text"] = ""
                emit("round_start")

            def on_delta(d):
                acc["text"] += d
                now = _time.monotonic()
                if now - last["ts"] >= 0.2:
                    last["ts"] = now
                    emit("text", text=acc["text"])

            self._agent._on_round_start = on_round
            self._agent._stream_callback = on_delta
            self._agent._tool_callback = lambda n, a: emit("tool", name=n)

            reply = self._agent.chat(self._text)

            tool_names = [t["tool"] for t in self._agent.tool_call_log
                          if t.get("success") is not False] if self._agent.tool_call_log else []
            data = load_conversation(cid) or {}
            messages = data.get("messages", [])
            amsg = {"role": "assistant", "content": reply, "tool_names": tool_names,
                    "_timestamp": _time.time()}
            messages.append(amsg)
            save_conversation(cid, data.get("name", name), messages)
            emit("final", message=amsg)
            self.refreshed.emit(cid)
        except Exception as e:
            print(f"[network] remote-host turn failed: {e}", flush=True)
            try:
                network_manager.publish_conv_event(
                    cid, {"kind": "final",
                          "message": {"role": "assistant",
                                      "content": f"[remote turn error: {e}]"}})
            except Exception:
                pass
            self.refreshed.emit(cid)   # host still refreshes to show what was saved


class ChatInput(QTextEdit):
    def __init__(self, chat_window, parent=None):
        super().__init__(parent)
        self.chat_window = chat_window
        self.setAcceptRichText(False)
        self.setPlaceholderText("")
        self.setCursorWidth(3)
        self.setMinimumHeight(80)
        self.setMaximumHeight(80)
        self._apply_styles()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
            else:
                self.chat_window.send_message()
        elif event.key() == Qt.Key.Key_V and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            clipboard = QApplication.clipboard()
            mime = clipboard.mimeData()
            if mime and mime.hasImage():
                self._paste_image_from_clipboard(mime)
            else:
                super().keyPressEvent(event)
        elif event.key() == Qt.Key.Key_Escape:
            if self.chat_window._pending_image:
                self.chat_window._clear_pending_image()
            else:
                super().keyPressEvent(event)
        elif event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            if (not self.toPlainText()
                    and self.chat_window._pending_image
                    and event.key() == Qt.Key.Key_Backspace):
                self.chat_window._clear_pending_image()
            else:
                super().keyPressEvent(event)
        else:
            super().keyPressEvent(event)

    def _paste_image_from_clipboard(self, mime):
        """Save clipboard image to a temp file and pin it as a pending attachment."""
        import tempfile
        import uuid
        image = mime.imageData()
        if image is None or image.isNull():
            return
        tmp = os.path.join(
            tempfile.gettempdir(), f"agent_paste_{uuid.uuid4().hex}.png")
        image.save(tmp, "PNG")
        self.chat_window._show_pending_image(tmp, "Clipboard image")

    def insertFromMimeData(self, source):
        """Intercept large text pastes: instead of dumping a wall of text into
        the composer (heavy to render, hard to scroll past), capture it as a
        collapsed paste card. The full text still goes to the model on send."""
        try:
            if source is not None and source.hasText():
                txt = source.text()
                if txt and self.chat_window._should_capture_paste(txt):
                    self.chat_window._capture_pasted_text(txt)
                    return
        except Exception:
            pass
        super().insertFromMimeData(source)

    def set_focus_highlight(self, on: bool):
        """Light the composer border (accent) when the chat input is the user's
        single active target, dim it otherwise — mirrors the terminal grid's
        selected-cell highlight so only one of {chat, terminal} reads as focused."""
        on = bool(on)
        if getattr(self, "_focus_highlight", False) == on:
            return
        self._focus_highlight = on
        self._apply_styles()

    def _apply_styles(self):
        p = PALETTE
        fs = self.chat_window.agent.config.get("chat_font_size", 10)
        self.setFont(QFont("Consolas", fs))
        border_c = p['accent_bright'] if getattr(self, "_focus_highlight", False) else p['accent_muted']
        # stylesheet 'color' controls the cursor; QTextCharFormat controls typed text
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {p['panel']};
                color: {p['text']};
                border: 1px solid {border_c};
                padding: 6px;
                font-family: Consolas, monospace;
                font-size: {fs}pt;
                font-style: normal;
                {_mono_selection_qss(p)}
            }}
        """)
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(p['text']))
        fmt.setFont(QFont("Consolas", fs))
        self.setCurrentCharFormat(fmt)

# ──────────────────────────────────────────────────────────────────────
# Main chat window
# ──────────────────────────────────────────────────────────────────────

class _AuditBridge(QObject):
    """Thread-safe bridge: event_bus → Qt signal for tool-audit events."""
    triggered = pyqtSignal(str, str)  # (tool_name, target_conv_id)

_audit_bridge = _AuditBridge()


class ChatWindow(QWidget):
    """Main chat surface. tool_activity / tool_batch are emitted from the agent
    worker thread and handled on the UI thread for live tool chips + sounds."""
    tool_activity = pyqtSignal(str, dict)
    tool_batch = pyqtSignal(list)
    # Raised from the inference worker thread when ask_user_question fires.
    # Qt delivers it (queued) to the main thread's event loop, which actually
    # builds the board. Carries (questions, result_dict, done_event).
    _question_requested = pyqtSignal(list, dict, object)
    # Fires once when the initial conversation finishes hydrating at startup —
    # the cue for main() to fade out the splash screen.
    initial_load_finished = pyqtSignal()
    # Inbound peer event (already HMAC-verified) — raised from the network
    # server's worker threads; Qt queues it onto the GUI thread.
    network_event = pyqtSignal(object)
    # A peer wants THIS host to run a turn in one of our conversations
    # (conv_id, text, reply_url). Marshalled onto the GUI thread.
    remote_input_received = pyqtSignal(str, str, str)
    # A host we're mirroring pushed a live conversation event (dict).
    conv_event_received = pyqtSignal(object)
    # The network terminal bridge asks (from a server thread) for an attachment
    # to a conversation's live shell; built on the GUI thread.
    terminal_attach_requested = pyqtSignal(object)

    _PARALLEL_TOOL_UI_MS = 140

    def __init__(self, agent: Agent, parent=None):
        super().__init__(parent)
        self.agent = agent
        self._parallel_tool_pending: list[str] = []
        self.tool_activity.connect(self._on_tool_activity)
        self.tool_batch.connect(self._on_tool_batch)
        self._question_requested.connect(self._show_question_board)
        self._thread = None
        self._queued_message = None  # message submitted mid-job, auto-sent on finish
        self._stream_did_split = False  # set when a diff card splits the live bubble this turn
        # True while the agent is BLOCKED on an ask_user_question board — it's
        # waiting on the user, not working, so the "still working" animated
        # ellipsis cues are suppressed.
        self._awaiting_user_answer = False
        self._thinking = None
        self._conv_threads: dict[str, dict] = {}  # conv_id -> {"thread", "meta", "agent_state"}
        self._current_conv_id = ""
        self._pending_image: str | None = None
        self._message_meta: list[dict] = []  # full history
        self._cutoff_meta_cache_key: tuple | None = None
        self._cutoff_meta_cache_value: int = 0
        self._idx_to_widget: dict[int, ChatMessageWidget] = {}  # rendered subset
        self._visible_start = 0
        self._visible_end = 0
        self._baseline_end = 0  # "most recent" end before scrolling back
        self._char_limit = self.agent.config.get("display_char_limit", 15000)
        self._loading_more = False
        self._sliding = False
        self._load_check_scheduled = False
        self._sync_debounce_timer = QTimer(self)
        self._sync_debounce_timer.setSingleShot(True)
        self._sync_debounce_timer.setInterval(48)
        self._sync_debounce_timer.timeout.connect(self._recalc_and_sync_now)
        self._last_divider_sig: tuple = ()
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.timeout.connect(self._auto_save)
        self._auto_save_timer.start(10000)
        self._composer_draft_timer = QTimer(self)
        self._composer_draft_timer.setSingleShot(True)
        self._composer_draft_timer.timeout.connect(self._persist_current_composer_draft)
        self._viewer_state_save_timer = QTimer(self)
        self._viewer_state_save_timer.setSingleShot(True)
        self._viewer_state_save_timer.setInterval(4000)
        self._viewer_state_save_timer.timeout.connect(self._flush_viewer_state_save)
        # Live-streaming state: tokens accumulate in a buffer and flush into a
        # real assistant bubble on a timer (~16fps).
        self._stream_buffer: list[str] = []
        self._stream_committed_text: str = ""
        self._stream_dirty = False
        self._stream_active = False
        self._stream_live_meta_idx: int | None = None
        self._inferring = False
        self._composer_draft_cache: dict[str, str] = {}
        self._conv_load_generation = 0
        self._conv_load_thread: ConversationLoadThread | None = None
        self._message_widget_pool: list[ChatMessageWidget] = []
        self._MSG_WIDGET_POOL_MAX = 48
        self._theme_rebuild_idx = 0
        self._theme_rebuild_end = 0
        self._theme_rebuild_scroll: tuple = (True, 0, 0)
        self._live_plan_timeline_ref: tuple[int, int] | None = None
        self._stream_flush_timer = QTimer(self)
        self._stream_flush_timer.setInterval(100)
        self._stream_flush_timer.timeout.connect(self._flush_stream)
        ChatMessageWidget._font_size = self.agent.config.get("chat_font_size", 10)
        ChatMessageWidget._ellipsis_enabled = self.agent.config.get("animate_ellipsis", True)
        _mode = self.agent.config.get("tool_display_mode", "chips")
        ChatMessageWidget._tool_display_mode = (
            _mode if _mode in ("chips", "bubbles", "comma") else "chips"
        )
        ChatMessageWidget._show_tools_hint = bool(
            self.agent.config.get("show_tools_hint", False)
        )
        ChatMessageWidget._monocolor_images = bool(
            self.agent.config.get("monocolor", True)
            and self.agent.config.get("monocolor_images", False)
        )

        # Set up dangerous command approval callback
        from tools.terminal import set_approval_callback
        self._approval_result = None
        set_approval_callback(self._request_command_approval)

        # Set up ask_user_question callback — the agent raises an in-place
        # answer board where the composer sits and blocks for the user's reply.
        from tools.ask_user import set_question_callback
        self._question_board = None
        set_question_callback(self._request_user_question)

        self._build_ui()
        self._apply_styles()
        self._sync_file_explorer_root()

        # Wire file viewer tool via Qt signal bridge (thread-safe)
        from tools.file_viewer import bridge as viewer_bridge
        print(f"[chat_widget] connecting to viewer bridge_id={id(viewer_bridge)}", flush=True)
        viewer_bridge.open_requested.connect(self.open_file_in_viewer)
        viewer_bridge.refresh_requested.connect(self._file_viewer.refresh_if_showing)
        viewer_bridge.edit_notified.connect(self._on_agent_edit_file)
        # File viewer requests attention (agent just edited a file) —
        # expand the splitter if it's collapsed, and blink if it's not.
        self._file_viewer.attention_requested.connect(self._surface_file_viewer)
        # Persist the explorer navigation spot whenever the user re-roots the
        # tree (double-click a folder / Open folder), so it survives restart.
        self._file_viewer.explorer_root_changed.connect(
            lambda _root: self._save_viewer_state())

        # Wire tool-audit bridge (blink + sound when an audit fires)
        _audit_bridge.triggered.connect(self._on_audit_triggered)
        try:
            from core.event_bus import bus
            bus.on("audit.triggered", lambda tool="", conv_id="", **_:
                   _audit_bridge.triggered.emit(tool, conv_id))
        except Exception as e:
            print(f"[chat_widget] audit bridge wiring failed: {e}")

        from tools.workspace_terminal import (
            bridge as wt_bridge, set_read_handler, set_panel_resolver,
        )
        wt_bridge.show_requested.connect(self._show_workspace_terminal)
        wt_bridge.new_tab_requested.connect(self._right_workspace.terminal_panel.add_terminal_tab)
        wt_bridge.send_requested.connect(self._right_workspace.terminal_panel.send_to_active)
        set_read_handler(self._right_workspace.terminal_panel.get_active_text)
        # Agent bg-tab bridge: lets tools/terminal.py spawn dedicated tabs
        # for `background=True` commands. Closing a tab kills the process tree.
        set_panel_resolver(lambda: self._right_workspace.terminal_panel)
        from tools.workspace_browser import set_context_provider, set_grab_handler
        set_context_provider(self._right_workspace.browser_panel.get_current_page_context)
        set_grab_handler(self._right_workspace.browser_panel.grab_current_view)

        # Sub-agent bridge — live cards + per-agent terminals
        from tools.subagent_tool import get_bridge as _get_sa_bridge
        sa_bridge = _get_sa_bridge()
        if sa_bridge:
            sa_bridge.job_started.connect(self._on_subagent_job_started)
            sa_bridge.task_updated.connect(self._on_subagent_task_updated)
            sa_bridge.terminal_requested.connect(self._on_subagent_terminal)
            sa_bridge.job_completed.connect(self._on_subagent_job_completed)

        # ── Voice queue (TTS autoplay) ─────────────────────────────────
        import queue as _queue_mod
        self._voice_queue: _queue_mod.Queue = _queue_mod.Queue()
        self._voice_worker_running = False  # True while the drain thread is alive
        self._voice_interrupt = False       # set True when user sends a new message
        self._voice_current_proc = None     # subprocess.Popen handle of the playing clip

        # Chart bridge — inline chart cards
        try:
            from tools.chart import get_chart_bridge
            get_chart_bridge().chart_ready.connect(self._on_chart_ready)
        except Exception:
            pass

        # Defer conversation load so all widgets are parented first
        QTimer.singleShot(0, self._load_initial_conversation)
        # Task scheduler ticker — checks every 60s for due tasks
        self._task_timer = QTimer(self)
        self._task_timer.timeout.connect(self._task_tick)
        self._task_timer.start(60000)
        # Startup tasks: fire once, shortly after the UI settles.
        QTimer.singleShot(2500, self._run_startup_tasks)
        # Networking: route inbound /sync events onto the GUI thread, then
        # auto-start the inbound server + tunnel if enabled in config. on_sync
        # is installed unconditionally so a later Settings → Start works too.
        self._remote_mirror = None      # active remote-conversation mirror (viewer side)
        self._remote_convs_by_peer = {}  # peer_name -> [conv dicts] for the dropdown
        try:
            from core.network import network_manager
            self.network_event.connect(self._on_network_event)
            self.remote_input_received.connect(self._on_remote_input)
            self.conv_event_received.connect(self._on_conv_event)
            network_manager.on_sync = self.network_event.emit
            network_manager.on_remote_input = (
                lambda cid, text, reply_url: self.remote_input_received.emit(
                    cid or "", text or "", reply_url or ""))
            network_manager.on_conv_event = self.conv_event_received.emit
            self.terminal_attach_requested.connect(self._on_terminal_attach_request)
            network_manager.terminal_attach_request = self.terminal_attach_requested.emit
            # Poll peers for their conversation lists. The timer ALWAYS runs and
            # self-gates on network_manager.running, so enabling networking later
            # via Settings (e.g. on a fresh install) starts the dropdown updating
            # without an app restart.
            self._remote_conv_timer = QTimer(self)
            self._remote_conv_timer.timeout.connect(self._refresh_remote_convs)
            self._remote_conv_timer.start(15000)
            if (self.agent.config.get("network") or {}).get("enabled"):
                network_manager.start({"network": self.agent.config.get("network", {})})
                QTimer.singleShot(4000, self._refresh_remote_convs)
        except Exception:
            pass

    # ── Unified one-target focus (chat composer ↔ terminal grid) ───────────

    def set_input_focus_highlight(self, on: bool):
        """Light/dim the composer border. Named to match the coordinator's
        per-column API so this works in single-pane AND multi-column."""
        try:
            self.input.set_focus_highlight(on)
        except Exception:
            pass

    @staticmethod
    def _widget_within(widget, ancestor) -> bool:
        """True if `widget` is `ancestor` or nested inside it (walks parents)."""
        if ancestor is None or widget is None:
            return False
        w = widget
        while w is not None:
            if w is ancestor:
                return True
            try:
                w = w.parentWidget()
            except Exception:
                return False
        return False

    def _on_app_focus_changed(self, _old, now):
        """Route focus to a single selected target: composer or terminal."""
        if now is None:
            return
        try:
            if self._widget_within(now, self.input):
                self.set_input_focus_highlight(True)
                try:
                    self._right_workspace.terminal_panel.clear_active_highlight()
                except Exception:
                    pass
            elif self._widget_within(
                    now, getattr(self._right_workspace, "terminal_panel", None)):
                # Terminal took focus — it lights its own cell; just dim the composer.
                self.set_input_focus_highlight(False)
        except RuntimeError:
            pass  # widget torn down mid-callback

    def _workspace_preserves_focus(self, widget) -> bool:
        """True when *widget* is an interactive workspace tool that should keep
        keyboard focus after the agent finishes (Terminal, File viewer)."""
        if widget is None:
            return False
        ws = getattr(self, "_right_workspace", None)
        if ws is None:
            return False
        for panel in (
            getattr(ws, "terminal_panel", None),
            getattr(ws, "remote_terminal", None),
            getattr(ws, "file_viewer", None),
        ):
            if panel is not None and self._widget_within(widget, panel):
                return True
        return False

    def _maybe_focus_input(self):
        """Return focus to the composer after a turn unless the user is typing
        in the workspace Terminal or File viewer."""
        from PyQt6.QtWidgets import QApplication
        fw = QApplication.focusWidget()
        if self._workspace_preserves_focus(fw):
            return
        try:
            self.input.setFocus()
        except Exception:
            pass

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Conversation bar + Conversation button on same row
        conv_row = QHBoxLayout()
        conv_row.setContentsMargins(0, 0, 4, 0)
        conv_row.setSpacing(4)

        self._conv_bar = ConversationBar()
        self._conv_bar.conversation_selected.connect(self._switch_conversation)
        self._conv_bar.new_requested.connect(self._new_conversation)
        self._conv_bar.rename_requested.connect(self._rename_conversation)
        self._conv_bar.delete_requested.connect(self._delete_conversation)
        # This bar's −/+ buttons are workspace-oriented (they emit ws_* signals).
        # Single-pane has no workspaces, so map them to conversation new/delete —
        # otherwise the − button is dead and conversations can't be deleted here.
        self._conv_bar.ws_new_requested.connect(self._new_conversation)
        self._conv_bar.ws_delete_requested.connect(self._delete_current_conversation)
        conv_row.addWidget(self._conv_bar, stretch=1)

        conv_btn = QPushButton("Conversation")
        conv_btn.setObjectName("promptBtn")
        conv_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        conv_btn.clicked.connect(self._open_conversation_dialog)
        conv_row.addWidget(conv_btn)

        layout.addLayout(conv_row)

        # Stream chips — created here, parented into the left chat column later
        self._stream_chips_container = QWidget()
        self._stream_chips_layout = QHBoxLayout(self._stream_chips_container)
        self._stream_chips_layout.setContentsMargins(0, 0, 0, 0)
        self._stream_chips_layout.setSpacing(4)
        self._refresh_stream_chips()

        p = PALETTE

        # Chat panel — scroll + input wrapped with thin border
        self._chat_panel = QFrame()
        self._chat_panel.setObjectName("ChatPanel")
        self._chat_panel.setStyleSheet(f"QFrame#ChatPanel {{ border: 1px solid {p['border']}; }}")
        chat_layout = QVBoxLayout(self._chat_panel)
        chat_layout.setContentsMargins(0, 0, 0, 0)
        chat_layout.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._messages_container = QWidget()
        self._messages_layout = QVBoxLayout(self._messages_container)
        self._messages_layout.setContentsMargins(0, 0, 0, 0)
        self._messages_layout.setSpacing(0)

        self._scroll.setWidget(self._messages_container)
        self._scroll.verticalScrollBar().valueChanged.connect(self._on_scroll)
        self._scroll.verticalScrollBar().rangeChanged.connect(self._on_scrollbar_range_changed)
        self._pinned_to_bottom = True
        self._live_plan_widget = None
        self._live_plan_meta_idx = None
        self._messages_container.installEventFilter(self)

        # Pristine-state intro hint — dimmed, centered onboarding text drawn over
        # the empty chat on a fresh start. Fades in with the startup "lights on"
        # (see show_intro_hint_if_pristine) and vanishes the instant any content
        # appears (see _recalc_and_sync_now). Mouse-transparent so it never
        # blocks the empty area.
        self._intro_hint = QLabel(self._messages_container)
        self._intro_hint.setObjectName("introHint")
        self._intro_hint.setTextFormat(Qt.TextFormat.RichText)
        self._intro_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._intro_hint.setWordWrap(True)
        self._intro_hint.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._intro_hint.setStyleSheet("background: transparent; border: none;")
        self._intro_hint_effect = QGraphicsOpacityEffect(self._intro_hint)
        self._intro_hint.setGraphicsEffect(self._intro_hint_effect)
        self._intro_hint_anim = None
        self._intro_hint.hide()

        # Left column: stream chips + scroll area (tracks together when file viewer opens)
        self._chat_left = QWidget()
        self._chat_left.setMinimumWidth(0)
        chat_left_layout = QVBoxLayout(self._chat_left)
        chat_left_layout.setContentsMargins(0, 0, 0, 0)
        chat_left_layout.setSpacing(0)
        chat_left_layout.addWidget(self._stream_chips_container)
        chat_left_layout.addWidget(self._scroll, stretch=1)

        # Live streaming preview — used when stream_display == "preview".
        # Parented to the messages container (appended during streaming) so it
        # stays inside the scroll area without colliding with virtual-scroll widgets.
        self._stream_preview = QPlainTextEdit(self._messages_container)
        self._stream_preview.setReadOnly(True)
        self._stream_preview.setFrameShape(QFrame.Shape.NoFrame)
        self._stream_preview.setObjectName("streamPreview")
        self._stream_preview.setMaximumHeight(112)
        self._stream_preview.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._stream_preview.setVisible(False)
        self._stream_preview.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._stream_fade_anim = None
        self._apply_stream_preview_style()

        # Horizontal splitter: chat | tool workspace. Which SIDE the workspace
        # sits on is configurable (Settings → "Workspace panel side"); default
        # right. _ws_index/_chat_index let every size op stay side-agnostic.
        ws_side = str(self.agent.config.get("workspace_side", "right") or "right").lower()
        self._ws_left = (ws_side == "left")
        self._ws_index = 0 if self._ws_left else 1
        self._chat_index = 1 - self._ws_index

        self._chat_hsplitter = HoverSoundSplitter(Qt.Orientation.Horizontal)
        self._chat_hsplitter.setHandleWidth(10)
        self._chat_hsplitter.setContentsMargins(0, 0, 8, 0)  # clear the window resize grip (bottom-right)
        self._chat_hsplitter.setStyleSheet(self._splitter_idle_ss(p))
        from ui.right_workspace import RightWorkspacePanel

        self._right_workspace = RightWorkspacePanel()
        self._right_workspace.set_collapse_splitter_callback(self._collapse_workspace)
        self._right_workspace.terminal_panel.set_cwd_resolver(self._workspace_folder_path)
        self._file_viewer = self._right_workspace.file_viewer
        # Add the two panes in the configured order so the workspace lands on
        # the chosen side; index 0 is always the leftmost widget.
        if self._ws_left:
            self._chat_hsplitter.addWidget(self._right_workspace)
            self._chat_hsplitter.addWidget(self._chat_left)
        else:
            self._chat_hsplitter.addWidget(self._chat_left)
            self._chat_hsplitter.addWidget(self._right_workspace)
        self._chat_hsplitter.setCollapsible(0, True)
        self._chat_hsplitter.setCollapsible(1, True)
        # Start fully collapsed — workspace panel width = 0
        self._collapse_workspace()
        chat_layout.addWidget(self._chat_hsplitter, stretch=1)

        # Defer file-viewer wrap + chat message label widths until splitter / window resize settles.
        self._chat_wrap_layout_frozen = False
        self._file_viewer_layout_timer = QTimer(self)
        self._file_viewer_layout_timer.setSingleShot(True)
        self._file_viewer_layout_timer.timeout.connect(self._release_layout_resize_freeze)
        self._chat_hsplitter.splitterMoved.connect(self._start_layout_settle_cycle)

        # Image attach preview bar (Discord-style thumbnail + overlay X), centered
        self._image_preview = QFrame()
        self._image_preview.setObjectName("imagePreview")
        self._image_preview.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self._image_preview.hide()
        preview_layout = QHBoxLayout(self._image_preview)
        preview_layout.setContentsMargins(6, 4, 6, 4)
        preview_layout.setSpacing(6)
        self._image_thumb_host = QFrame()
        self._image_thumb_host.setFixedSize(52, 52)
        self._image_thumb_host.setStyleSheet("background: transparent; border: none;")
        thumb_host_layout = QGridLayout(self._image_thumb_host)
        thumb_host_layout.setContentsMargins(0, 0, 0, 0)
        thumb_host_layout.setSpacing(0)
        self._image_thumb = QLabel()
        self._image_thumb.setFixedSize(48, 48)
        self._image_thumb.setStyleSheet("background:transparent; border:none;")
        thumb_host_layout.addWidget(self._image_thumb, 0, 0)
        self._image_remove_btn = QPushButton("\u2715")
        self._image_remove_btn.setObjectName("imageRemoveBtn")
        self._image_remove_btn.setFixedSize(18, 18)
        self._image_remove_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._image_remove_btn.setFont(QFont("Consolas", 9))
        self._image_remove_btn.setToolTip("Remove attachment (Esc)")
        self._image_remove_btn.clicked.connect(self._clear_pending_image)
        thumb_host_layout.addWidget(
            self._image_remove_btn, 0, 0,
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight)
        preview_layout.addWidget(self._image_thumb_host)
        self._image_name_label = QLabel()
        self._image_name_label.setFont(QFont("Consolas", 8))
        self._image_name_label.setMaximumWidth(180)
        self._image_name_label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._image_name_label.setStyleSheet(
            f"color:{p['muted_text']}; background:transparent; border:none;")
        preview_layout.addWidget(self._image_name_label)
        # Center the preview bar
        self._image_preview_row = QHBoxLayout()
        self._image_preview_row.setContentsMargins(0, 0, 0, 0)
        self._image_preview_row.addStretch()
        self._image_preview_row.addWidget(self._image_preview)
        self._image_preview_row.addStretch()
        chat_left_layout.addLayout(self._image_preview_row)
        # Keep legacy reference for compatibility
        self._image_label = self._image_preview

        # Pasted-text pills: compact removable chips for large pastes so the
        # composer stays clean text instead of a wall of pasted content.
        self._pending_pastes: list[dict] = []
        self._paste_bar = QWidget()
        self._paste_bar.setObjectName("pasteBar")
        self._paste_bar_layout = QHBoxLayout(self._paste_bar)
        self._paste_bar_layout.setContentsMargins(8, 0, 8, 2)
        self._paste_bar_layout.setSpacing(4)
        self._paste_bar_layout.addStretch()  # pills pack left, before the stretch
        self._paste_bar.hide()
        chat_left_layout.addWidget(self._paste_bar)

        # Input field
        self.input = ChatInput(self)
        self.input.textChanged.connect(self._on_composer_draft_text_changed)
        chat_left_layout.addWidget(self.input)

        # Unified one-target focus: when the composer (or anything inside it) is
        # focused, light its border and clear the terminal grid's highlight; when
        # a terminal takes focus, drop the composer highlight (the terminal lights
        # its own cell). Only one of {chat, terminal} reads as "the user's focus".
        from PyQt6.QtWidgets import QApplication as _QApp
        _app = _QApp.instance()
        if _app is not None:
            _app.focusChanged.connect(self._on_app_focus_changed)

        # Bottom button row: CLEAR ... ← ✕ | ▣ →
        bottom = QHBoxLayout()
        bottom.setSpacing(2)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("clearBtn")
        self.clear_btn.setFixedWidth(50)
        self.clear_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clear_btn.clicked.connect(self.clear_chat)
        bottom.addWidget(self.clear_btn)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setObjectName("copyBtn")
        self.copy_btn.setFixedWidth(50)
        self.copy_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_btn.setToolTip(
            "Copy plain-text transcript for messages in the LLM context window "
            "(same range as non-dimmed chat). Older dimmed history is omitted; "
            "a leading … is added when something was skipped.")
        self.copy_btn.clicked.connect(self._copy_in_context_transcript)
        bottom.addWidget(self.copy_btn)

        # Typing indicator lives in the bottom bar, centered in the gap
        self._typing_label = QLabel("")
        _typing_font = QFont("Consolas", 9)
        _typing_font.setItalic(True)  # "Familiar is …" reads as a soft status cue
        self._typing_label.setFont(_typing_font)
        p_t = PALETTE
        self._typing_label.setStyleSheet(f"color: transparent; background: transparent; border: none;")
        self._typing_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bottom.addWidget(self._typing_label, stretch=1)

        self.undo_btn = QPushButton("\u2190")  # ←
        self.undo_btn.setObjectName("sendBtn")  # same size as send
        self.undo_btn.setToolTip("Undo last turn")
        self.undo_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_btn.clicked.connect(self._undo_last_turn)
        bottom.addWidget(self.undo_btn)

        self.stop_btn = QPushButton("\u2715")  # ✕
        self.stop_btn.setObjectName("attachBtn")  # same size as attach
        self.stop_btn.setStyleSheet("QPushButton#attachBtn { font-size: 13px; }")  # keep ✕ at old size
        self.stop_btn.setToolTip("Stop inference")
        self.stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.stop_btn.clicked.connect(self._stop_inference)
        bottom.addWidget(self.stop_btn)

        self.attach_btn = QPushButton("+")  # attach
        self.attach_btn.setObjectName("attachBtn")
        self.attach_btn.setToolTip("Attach File")
        self.attach_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.attach_btn.clicked.connect(self._attach_file)
        bottom.addWidget(self.attach_btn)

        self.send_btn = QPushButton("\u2192")  # →
        self.send_btn.setObjectName("sendBtn")
        self.send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_btn.clicked.connect(self.send_message)
        bottom.addWidget(self.send_btn)

        chat_left_layout.addLayout(bottom)

        layout.addWidget(self._chat_panel, stretch=1)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_file_viewer_layout_timer"):
            self._start_layout_settle_cycle()

    def _start_layout_settle_cycle(self):
        """Skip expensive chat rewrap + file-viewer wrap reflow until motion stops (~100ms idle)."""
        self._chat_wrap_layout_frozen = True
        if hasattr(self, "_resize_timer") and self._resize_timer.isActive():
            self._resize_timer.stop()
        if hasattr(self, "_file_viewer") and self._file_viewer.get_open_paths():
            self._file_viewer.set_wrap_frozen(True)
        if hasattr(self, "_file_viewer_layout_timer"):
            self._file_viewer_layout_timer.start(100)

    def _release_layout_resize_freeze(self):
        self._chat_wrap_layout_frozen = False
        if hasattr(self, "_file_viewer"):
            self._file_viewer.set_wrap_frozen(False)
        self._on_debounced_resize()

    def _request_command_approval(self, command: str) -> bool:
        """Show approval dialog for dangerous commands. Thread-safe — blocks until user responds."""
        import threading
        if threading.current_thread() is threading.main_thread():
            return self._show_approval_dialog(command)
        # Called from background thread — marshal to main thread
        result = [None]
        event = threading.Event()

        def _ask():
            result[0] = self._show_approval_dialog(command)
            event.set()

        QTimer.singleShot(0, _ask)
        event.wait(timeout=60)  # 60s max wait
        return result[0] if result[0] is not None else False

    def _show_approval_dialog(self, command: str) -> bool:
        from ui.glass_dialog import GlassDialog
        return GlassDialog.confirm(
            self, "Dangerous Command",
            f"The agent wants to run a potentially destructive command:\n\n"
            f"$ {command[:200]}\n\n"
            f"Allow this?")

    # ── ask_user_question: in-place answer board ─────────────────────────

    def _request_user_question(self, questions: list) -> dict | None:
        """Tool entry point (called from the inference worker thread). Marshals
        to the main thread to raise the board, blocks until the user answers,
        cancels, or aborts. Returns {question: answer} or None.

        Mirrors _request_command_approval's thread bridge."""
        import threading
        if threading.current_thread() is threading.main_thread():
            # Shouldn't happen (tools run on the worker), but stay correct.
            return self._show_question_board_blocking(questions)

        result = {"answers": None}
        done = threading.Event()

        # Hand the work to the main thread via a queued signal. QTimer.singleShot
        # CANNOT be used here: it posts to the calling thread's event loop, and
        # this runs on a worker thread that has none — the board would never
        # build (the original hang). A pyqtSignal is delivered to the receiver's
        # (main) thread event loop, which is exactly what we need.
        self._question_requested.emit(questions, result, done)

        # Wake periodically so a mid-question Stop (which sets the global abort)
        # can tear the board down. NO time ceiling — the user gets as long as
        # they want to answer; only an explicit Stop ends the wait early.
        from core.tool_context import get_global_abort
        abort = get_global_abort()
        while not done.wait(timeout=0.25):
            if abort.is_set():
                self._request_board_teardown()
                return None
        return result["answers"]

    def _request_board_teardown(self):
        """Tear the board down from any thread (queued onto the main thread)."""
        import threading
        if threading.current_thread() is threading.main_thread():
            self._teardown_question_board()
        else:
            # Reuse the queued signal path: emit with empty questions as a
            # teardown sentinel handled in _show_question_board.
            self._question_requested.emit([], {"_teardown": True}, None)

    def _show_question_board(self, questions: list, result: dict, done):
        """Main-thread slot (queued from the worker via _question_requested).
        Builds the board, hides the composer, wires callbacks."""
        # Teardown sentinel (abort/timeout from the worker thread).
        if isinstance(result, dict) and result.get("_teardown"):
            self._teardown_question_board()
            return
        from ui.question_board import QuestionBoard
        try:
            board = QuestionBoard(questions, parent=self._chat_left)
        except Exception:
            result["answers"] = None
            if done is not None:
                done.set()
            return

        def _on_submit(answers: dict):
            result["answers"] = answers
            self._teardown_question_board()
            # Back to actually working — restore the animated "working" cues.
            ChatMessageWidget.set_ellipsis_active(True)
            # Now that the question is answered, drop the deferred chip and update
            # the status hint until the next tool/stream overwrites it.
            self._note_live_stream_tool("ask_user_question")
            self._set_typing_prefix(f"{AGENT_LABEL} is reviewing the response")
            done.set()

        def _on_cancel():
            result["answers"] = None
            self._teardown_question_board()
            ChatMessageWidget.set_ellipsis_active(True)
            self._note_live_stream_tool("ask_user_question")
            self._set_typing_prefix(f"{AGENT_LABEL} is reviewing the response")
            done.set()

        board.submitted.connect(_on_submit)
        board.cancelled.connect(_on_cancel)

        self._question_board = board
        # Persist the open board so a shutdown/crash while it's up doesn't lose
        # the question — it's restored on next launch (see _restore_pending_question).
        try:
            from core.pending_question import save_pending_question
            save_pending_question(self._current_conv_id, questions)
        except Exception:
            pass
        # Hide the composer + button bar and slot the board where the input was.
        self.input.setVisible(False)
        self._set_bottom_row_visible(False)
        # Insert directly above the (now-hidden) bottom button row.
        idx = self._chat_left.layout().indexOf(self.input)
        if idx < 0:
            self._chat_left.layout().addWidget(board)
        else:
            self._chat_left.layout().insertWidget(idx, board)
        board.setVisible(True)
        board.setFocus()

        # The agent is now BLOCKED waiting on the user — suppress every "still
        # working" animated-ellipsis cue until they answer: the bottom typing
        # dots and the trailing "…" on the live stream bubble.
        self._awaiting_user_answer = True
        self._typing_label.setText("")
        ChatMessageWidget.set_ellipsis_active(False)
        if self._stream_in_chat():
            try:
                self._refresh_live_stream_display(show_ellipsis=False)
            except Exception:
                pass

    def _teardown_question_board(self):
        """Main-thread: remove the board and restore the composer."""
        # No longer waiting on the user (answered, cancelled, or aborted).
        self._awaiting_user_answer = False
        # The board has resolved — drop the durable record so it isn't re-raised
        # on the next launch. EXCEPT during shutdown: a question still open when
        # the app closes must survive to be restored next launch, so leave the
        # record intact when we're tearing down because we're quitting.
        if not getattr(self, "_shutting_down", False):
            try:
                from core.pending_question import clear_pending_question
                clear_pending_question()
            except Exception:
                pass
        board = self._question_board
        if board is not None:
            try:
                board.setParent(None)
                board.deleteLater()
            except Exception:
                pass
            self._question_board = None
        self.input.setVisible(True)
        self._set_bottom_row_visible(True)
        try:
            self.input.setFocus()
        except Exception:
            pass

    def _set_bottom_row_visible(self, visible: bool):
        """Show/hide the composer's bottom button row widgets as a group."""
        for btn in (getattr(self, "clear_btn", None), getattr(self, "copy_btn", None),
                    getattr(self, "undo_btn", None), getattr(self, "stop_btn", None),
                    getattr(self, "attach_btn", None), getattr(self, "send_btn", None)):
            if btn is not None:
                btn.setVisible(visible)

    def _show_question_board_blocking(self, questions: list):
        """Fallback for the (unexpected) main-thread call path: spin a local
        event loop until the board resolves."""
        from PyQt6.QtCore import QEventLoop
        import threading
        result = {"answers": None}
        done = threading.Event()
        loop = QEventLoop()

        def _finish():
            loop.quit()

        orig = done.set
        def _set_and_quit():
            orig()
            QTimer.singleShot(0, _finish)
        done.set = _set_and_quit  # type: ignore

        self._show_question_board(questions, result, done)
        loop.exec()
        return result["answers"]

    def _restore_pending_question(self, questions: list):
        """Re-raise a question board that was open when the app last shut down.

        The original agent turn died with the process, so there's no blocked tool
        call to hand the answer back to. Instead, answering resumes the work by
        sending the agent a fresh turn that quotes the question and the user's
        choice — the conversation context (incl. the request that prompted the
        question) is already loaded, so the agent simply continues.
        """
        if not questions or self._thread is not None:
            return
        from ui.question_board import QuestionBoard
        try:
            board = QuestionBoard(questions, parent=self._chat_left)
        except Exception:
            from core.pending_question import clear_pending_question
            clear_pending_question()
            return

        def _resume(answers: dict):
            self._teardown_question_board()  # also clears the durable record
            ChatMessageWidget.set_ellipsis_active(True)
            parts = []
            for q, a in (answers or {}).items():
                ans = ", ".join(a) if isinstance(a, list) else str(a)
                parts.append(f"• {q} → {ans}")
            body = "\n".join(parts)
            resume_text = (
                "[Answering the question you asked before we were interrupted]\n"
                f"{body}\n\nPlease continue from here."
            )
            try:
                self.input.setPlainText(resume_text)
                self.send_message()
            except Exception:
                pass

        def _dismiss():
            self._teardown_question_board()  # also clears the durable record
            ChatMessageWidget.set_ellipsis_active(True)

        board.submitted.connect(_resume)
        board.cancelled.connect(_dismiss)

        self._question_board = board
        self.input.setVisible(False)
        self._set_bottom_row_visible(False)
        idx = self._chat_left.layout().indexOf(self.input)
        if idx < 0:
            self._chat_left.layout().addWidget(board)
        else:
            self._chat_left.layout().insertWidget(idx, board)
        board.setVisible(True)
        board.setFocus()
        self._awaiting_user_answer = True
        # Re-persist: the question stays recorded until it's actually answered or
        # dismissed, so even a second shutdown-during-restore can't lose it.
        try:
            from core.pending_question import save_pending_question
            save_pending_question(self._current_conv_id, questions)
        except Exception:
            pass

    def _maybe_restore_pending_question(self):
        """On startup, if a question board was open at last shutdown and it
        belongs to the conversation now showing, raise it again."""
        if getattr(self, "_pending_question_restored", False):
            return
        try:
            from core.pending_question import load_pending_question
            pending = load_pending_question()
        except Exception:
            return
        if not pending:
            return
        # Only restore into its own conversation so the question lands in the
        # right context. _load_initial_conversation already prefers that conv, so
        # a mismatch here means the conversation is gone — drop the stale record.
        if pending.get("conv_id") and pending["conv_id"] != self._current_conv_id:
            try:
                from core.pending_question import clear_pending_question
                clear_pending_question()
            except Exception:
                pass
            return
        self._pending_question_restored = True
        # Leave the record in place — _restore_pending_question keeps it until the
        # board is answered or dismissed (it tears down via _teardown, which
        # clears it). If the board fails to build, it clears the record itself.
        self._restore_pending_question(pending.get("questions") or [])

    def _update_conv_summary_label(self):
        """Refresh stream chips in the top bar."""
        self._refresh_stream_chips()

    def _refresh_stream_chips(self):
        """Rebuild the stream chip bubbles in the controls bar."""
        if not hasattr(self, '_stream_chips_layout'):
            return
        # Clear old chips
        while self._stream_chips_layout.count():
            item = self._stream_chips_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        p = PALETTE
        accent = QColor(p["accent"])
        from ui.conversation_dialog import _normalize_streams

        raw = getattr(self.agent, "_conversation_streams", None) or []
        streams = _normalize_streams(raw)
        if not streams:
            cfg = load_config()
            auto = [s["name"] for s in cfg.get("memory_streams", []) if s.get("auto_subscribe")]
            streams = [{"name": n, "read": True, "write": True} for n in auto]

        self._stream_chips_layout.addStretch()
        for s in streams:
            chip = QPushButton(s["name"])
            chip.setFont(QFont("Consolas", max(ChatMessageWidget._font_size - 3, 6)))
            chip.setCursor(Qt.CursorShape.PointingHandCursor)
            chip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            stream_name = s["name"]
            chip.clicked.connect(lambda checked, n=stream_name: self._open_memory_for_stream(n))
            chip.setStyleSheet(f"""
                QPushButton {{
                    background: rgba({accent.red()},{accent.green()},{accent.blue()},0.15);
                    color: {p['accent']};
                    border: 1px solid {p['accent_muted']};
                    border-radius: 10px;
                    padding: 2px 10px;
                }}
                QPushButton:hover {{
                    background: rgba({accent.red()},{accent.green()},{accent.blue()},0.3);
                    border-color: {p['accent']};
                }}
            """)
            self._stream_chips_layout.addWidget(chip)
        self._stream_chips_layout.addStretch()

    def _open_memory_for_stream(self, stream_name: str):
        from ui.memory_dialog import MemoryDialog
        dlg = MemoryDialog(parent=self.window(), initial_stream=stream_name)
        if dlg.exec():
            from ui.theme import refresh_palette
            refresh_palette()
            self.window()._apply_styles()
            self.window().title_bar.apply_theme()
            self.apply_theme()
            self.window().update()

    def _open_conversation_dialog(self):
        from ui.conversation_dialog import ConversationDialog
        dlg = ConversationDialog(self.agent, conv_id=self._current_conv_id, parent=self)
        if dlg.exec():
            self._update_conv_summary_label()
            self._auto_save()
        # Always refresh in case streams changed
        self._refresh_conv_bar()

    def _edit_system_prompt(self):
        """Legacy — redirects to ConversationDialog."""
        self._open_conversation_dialog()

    def _refresh_ws_combo(self):
        self._update_conv_summary_label()

    def _set_provider_combo_to(self, pid: str):
        self._update_conv_summary_label()

    def _set_ws_combo_to(self, name: str):
        self._update_conv_summary_label()

    def _on_ws_changed(self, idx):
        pass  # Now handled by ConversationDialog

    # ── Conversation management ──────────────────────────────────────

    def _on_composer_draft_text_changed(self):
        if not self._current_conv_id:
            return
        self._composer_draft_cache[self._current_conv_id] = self.input.toPlainText()
        self._composer_draft_timer.stop()
        self._composer_draft_timer.start(400)

    def _persist_current_composer_draft(self):
        cid = self._current_conv_id
        if not cid or self._is_remote_id(cid):
            return
        text = self._composer_draft_cache.get(cid, self.input.toPlainText())
        self._composer_draft_cache[cid] = text
        enqueue_composer_draft_save(cid, text)

    def _apply_composer_draft_for_conv(self, conv_id: str, text: str | None = None):
        if not conv_id:
            return
        if text is None:
            text = self._composer_draft_cache.get(conv_id)
            if text is None:
                text = get_conversation_composer_draft(conv_id)
                self._composer_draft_cache[conv_id] = text
        self.input.blockSignals(True)
        self.input.setPlainText(text)
        self.input.blockSignals(False)
        self.input.moveCursor(QTextCursor.MoveOperation.End)

    def _load_initial_conversation(self):
        """Load the last active conversation, or the most recent, or create new."""
        convos = list_conversations()
        if convos:
            # Try to restore the conversation the user last had open
            last_id = self.agent.config.get("last_conversation_id", "")
            conv_ids = {c["id"] for c in convos}
            # A question board left open at last shutdown takes priority: open its
            # conversation so the restored board lands in the right context.
            pending_conv = ""
            try:
                from core.pending_question import load_pending_question
                _pq = load_pending_question()
                if _pq:
                    pending_conv = _pq.get("conv_id", "") or ""
            except Exception:
                pending_conv = ""
            if pending_conv and pending_conv in conv_ids:
                self._current_conv_id = pending_conv
            elif last_id and last_id in conv_ids:
                self._current_conv_id = last_id
            else:
                self._current_conv_id = convos[0]["id"]
            # Activate this conv's terminal panel before _load_conv runs so the
            # restored viewer state lands in the right per-conv panel.
            self._right_workspace.terminal_panel.set_active_conv(self._current_conv_id)
            self._load_conv(self._current_conv_id)
        else:
            self._new_conversation()
        self._refresh_conv_bar()
        self._persist_active_conv()
        # Ensure we start at the bottom of the chat
        QTimer.singleShot(100, lambda: self._scroll_to_bottom(force=True))

    def _refresh_conv_bar(self):
        convos = list_conversations()
        self._conv_bar.set_conversations(convos, self._current_conv_id)
        self._update_conv_hint()

    def _update_conv_hint(self):
        """Subdued context line beside the conversation dropdown — the active
        conversation's workspace, or the mirror banner when remoting into a
        peer's conversation."""
        if self._remote_mirror is not None:
            return  # keep the "🌐 mirroring" banner set by _enter_remote_mirror
        try:
            ws = getattr(self.agent, "workspace_path", "") or ""
            self._conv_bar.set_hint(f"⌂ {ws}" if ws else "")
        except Exception:
            pass

    def _new_conversation(self):
        self._composer_draft_timer.stop()
        self._persist_current_composer_draft()
        self._snapshot_current()
        # Save current before switching
        self._auto_save()
        cid = new_conversation_id()
        # Default to first available workspace
        workspaces = self.agent.config.get("workspaces", {})
        default_ws = next(iter(workspaces), "")
        # Generate unique "New Chat N" name
        existing_names = {c["name"] for c in list_conversations()}
        chat_name = "New Chat"
        n = 1
        while chat_name in existing_names:
            n += 1
            chat_name = f"New Chat {n}"
        save_conversation(cid, chat_name, [], workspace=default_ws,
                          model=self.agent.model)
        self._current_conv_id = cid
        self.agent.summarizer.save_state()
        self.agent.clear_context()
        self.agent.summarizer = __import__('core.summarizer', fromlist=['RollingSummarizer']).RollingSummarizer(cid)
        self.agent.set_conv_id(cid)
        self.agent.set_conversation_cwd("", persist=False)
        self.agent.set_workspace(default_ws)
        self.agent._provider_override = ""
        self.agent._model_override = ""
        self.agent.set_system_prompt_override("")
        self.agent._system_prompt_replace = False
        self.agent._context_note = ""
        self._set_ws_combo_to(default_ws)
        self._set_provider_combo_to(self.agent.provider)
        self._update_conv_summary_label()
        self._clear_message_widgets()
        self._message_meta = []
        self._refresh_conv_bar()
        self._persist_active_conv()
        # Reset file viewer + browser for fresh conversation. The terminal
        # panel doesn't need wiping — set_active_conv on a fresh conv_id
        # creates a brand new per-conv panel; previous conv's tabs and
        # processes stay alive in their own panel, hidden but still running.
        self._file_viewer.close_all_tabs()
        self._file_viewer._ensure_scratch_tab()
        self._right_workspace.browser_panel.restore_state(None)
        self._right_workspace.terminal_panel.set_active_conv(self._current_conv_id)
        self._right_workspace.set_workspace_page(3)
        self._sync_file_explorer_root()
        self._collapse_workspace()
        self._apply_composer_draft_for_conv(self._current_conv_id)

    def _persist_active_conv(self):
        """Write the active conversation ID to config so it survives restart."""
        if self._is_remote_id(self._current_conv_id):
            return  # don't restore into a remote mirror on next launch
        from core.agent import save_config
        cfg = load_config()
        if cfg.get("last_conversation_id") != self._current_conv_id:
            cfg["last_conversation_id"] = self._current_conv_id
            save_config(cfg)

    def _switch_conversation(self, conv_id: str):
        if conv_id == self._current_conv_id:
            return
        # Remote conversation chosen → open a live mirror, not a local load.
        if self._is_remote_id(conv_id):
            self._composer_draft_timer.stop()
            self._persist_current_composer_draft()
            self._clear_pending_pastes()
            self._snapshot_current()
            self._enter_remote_mirror(conv_id)
            return
        # Leaving a remote mirror for a local conversation.
        if self._remote_mirror is not None:
            self._exit_remote_mirror()
            self._conv_bar.set_hint("")
        self._composer_draft_timer.stop()
        self._persist_current_composer_draft()
        self._clear_pending_pastes()  # pending pills belong to this compose session
        self._snapshot_current()
        self._auto_save()
        self._current_conv_id = conv_id
        self._persist_active_conv()
        # Swap which per-conv terminal panel is visible. The previous
        # conversation's tabs and processes keep running, just hidden.
        self._right_workspace.terminal_panel.set_active_conv(conv_id)
        self._restore_snapshot(conv_id)
        self._conv_bar.highlight(conv_id)
        self._conv_bar.stop_blink(conv_id)
        self._update_conv_hint()
        # Fire any pending input blink for this conversation
        if hasattr(self, '_pending_input_blinks') and conv_id in self._pending_input_blinks:
            self._pending_input_blinks.discard(conv_id)
            self._start_input_blink()

    def _capture_stream_state(self) -> dict:
        """Snapshot the live-streaming pipeline (it's per-window shared state) so
        a backgrounded turn keeps its own buffer instead of bleeding into the
        conversation shown next."""
        return {
            "buffer": self._stream_buffer,
            "active": self._stream_active,
            "dirty": self._stream_dirty,
            "live_meta_idx": self._stream_live_meta_idx,
            "committed": self._stream_committed_text,
            "did_split": self._stream_did_split,
            "tool_verb": getattr(self, "_active_tool_verb", ""),
        }

    def _reset_stream_state(self):
        self._stream_buffer = []
        self._stream_active = False
        self._stream_dirty = False
        self._stream_live_meta_idx = None
        self._stream_committed_text = ""
        self._stream_did_split = False
        self._active_tool_verb = ""

    def _restore_stream_state(self, s: dict):
        self._stream_buffer = s.get("buffer", []) or []
        self._stream_active = bool(s.get("active", False))
        self._stream_dirty = bool(s.get("dirty", False))
        self._stream_live_meta_idx = s.get("live_meta_idx")
        self._stream_committed_text = s.get("committed", "")
        self._stream_did_split = bool(s.get("did_split", False))
        self._active_tool_verb = s.get("tool_verb", "")

    def _snapshot_current(self):
        """Save current conversation's live state (thread, context, meta) so we can switch away."""
        cid = self._current_conv_id
        if not cid or self._is_remote_id(cid):
            return  # remote mirrors keep no local snapshot
        if self._thread is not None:
            # Give the running thread its OWN agent so it can't corrupt
            # the shared one when we load a different conversation.
            bg_agent = Agent()
            bg_agent.context = list(self.agent.context)
            bg_agent.tool_call_log = list(self.agent.tool_call_log)
            bg_agent.summarizer = self.agent.summarizer
            bg_agent._provider_override = self.agent._provider_override
            bg_agent._model_override = self.agent._model_override
            bg_agent._workspace_name = self.agent._workspace_name
            bg_agent._system_prompt_override = self.agent._system_prompt_override
            bg_agent._system_prompt_replace = self.agent._system_prompt_replace
            bg_agent._context_note = getattr(self.agent, "_context_note", "")
            bg_agent.set_conv_id(cid)
            # Swap the thread's agent reference to the isolated copy
            self._thread.agent = bg_agent

            # Save thinking state so it survives the switch
            tool_counts = {}

            self._conv_threads[cid] = {
                "thread": self._thread,
                "agent": bg_agent,
                "meta": list(self._message_meta),  # COPY, not reference
                "tool_counts": tool_counts,
            }
            # Disconnect signals from UI (thread keeps running)
            try:
                self._thread.finished.disconnect(self._on_response)
                self._thread.errored.disconnect(self._on_error)
                self._thread.stopped.disconnect(self._on_stopped)
            except (TypeError, RuntimeError):
                pass
            # round_started is per-WINDOW too: left connected, the backgrounded
            # thread's next model round fires _on_stream_round_start against the
            # conversation shown NEXT — sealing/resetting its live buffer and
            # injecting this turn's narration into the wrong transcript. Detach it
            # like chunk/finished; the bg buffer below keeps accumulating raw text.
            try:
                self._thread.round_started.disconnect(self._on_stream_round_start)
            except (TypeError, RuntimeError):
                pass
            self.agent._tool_callback = None
            self.agent._tool_batch_callback = None
            # Wire up a background finisher
            thread = self._thread
            meta_ref = self._conv_threads[cid]["meta"]
            conv_id_ref = cid

            def _bg_finish(reply, tool_log, reply_html):
                tool_names = [t["tool"] for t in tool_log
                              if t.get("success") is not False] if tool_log else []
                html = reply_html or markdown2.markdown(
                    reply, extras=["fenced-code-blocks", "tables", "code-friendly"])
                meta_ref.append({
                    "role": "assistant", "content": reply,
                    "tool_names": tool_names, "image_path": "", "_html": html,
                })
                data = load_conversation(conv_id_ref)
                name = data.get("name", "Chat") if data else "Chat"
                save_conversation(conv_id_ref, name, meta_ref)
                self._conv_threads.pop(conv_id_ref, None)

            def _bg_error(err):
                self._conv_threads.pop(conv_id_ref, None)

            def _bg_stopped():
                self._conv_threads.pop(conv_id_ref, None)

            thread.finished.connect(_bg_finish)
            thread.errored.connect(_bg_error)
            thread.stopped.connect(_bg_stopped)

            # ── Isolate the live STREAM + tool UI ──────────────────────────
            # The streaming pipeline (chunk → _on_stream_chunk → shared buffer)
            # and the tool-call callbacks are per-WINDOW. Left connected, this
            # backgrounded turn renders its tokens + fires tool chips/sounds into
            # whatever conversation is shown next — the cross-talk bug. Snapshot
            # the stream state, detach chunk from the UI, and keep accumulating
            # this turn's tokens into ITS OWN buffer so switching back shows the
            # full text-so-far.
            self._conv_threads[cid]["stream"] = self._capture_stream_state()
            try:
                thread.chunk.disconnect(self._on_stream_chunk)
            except (TypeError, RuntimeError):
                pass
            _bg_buf = self._conv_threads[cid]["stream"]["buffer"]

            def _bg_chunk(delta, _buf=_bg_buf):
                if delta:
                    _buf.append(delta)
            self._conv_threads[cid]["_bg_chunk"] = _bg_chunk
            try:
                thread.chunk.connect(_bg_chunk)
            except Exception:
                pass
            # The backgrounded agent must NOT drive the shared tool chips/sounds.
            bg_agent._tool_callback = None
            bg_agent._tool_batch_callback = None
            self._reset_stream_state()

            self._thread = None
        self._hide_thinking()

    def _restore_snapshot(self, conv_id: str):
        """Restore a conversation — from snapshot if it has a running thread, else from disk."""
        snap = self._conv_threads.get(conv_id)
        if snap and snap.get("thread") and snap["thread"].isRunning():
            # Conversation has a running thread — adopt its agent back
            bg_agent = snap["agent"]
            self._thread = snap["thread"]
            self._thread.agent = self.agent  # point thread back to shared agent
            self._message_meta = snap["meta"]

            # Copy bg_agent state back into the shared agent
            self.agent.context = bg_agent.context
            self.agent.tool_call_log = bg_agent.tool_call_log
            self.agent.summarizer = bg_agent.summarizer
            self.agent._provider_override = bg_agent._provider_override
            self.agent._model_override = bg_agent._model_override
            self.agent._workspace_name = bg_agent._workspace_name
            self.agent._system_prompt_override = bg_agent._system_prompt_override
            self.agent._system_prompt_replace = getattr(bg_agent, "_system_prompt_replace", False)
            self.agent._context_note = getattr(bg_agent, "_context_note", "")

            # Reconnect signals
            try:
                self._thread.finished.disconnect()
                self._thread.errored.disconnect()
                self._thread.stopped.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._thread.finished.connect(self._on_response)
            self._thread.errored.connect(self._on_error)
            self._thread.stopped.connect(self._on_stopped)
            # Re-arm round_started (detached in _snapshot_current) so resumed
            # rounds paint into THIS conversation again.
            try:
                self._thread.round_started.disconnect()
            except (TypeError, RuntimeError):
                pass
            self._thread.round_started.connect(self._on_stream_round_start)
            self.agent._tool_callback = lambda n, a: self.tool_activity.emit(n, a)
            self.agent._tool_batch_callback = lambda ns: self.tool_batch.emit(ns)

            # Re-wire the live stream back to the UI and restore its state (incl.
            # the tokens accumulated by the bg buffer while this conv was hidden).
            try:
                self._thread.chunk.disconnect()  # drop the background accumulator
            except (TypeError, RuntimeError):
                pass
            self._restore_stream_state(snap.get("stream", {}))
            try:
                self._thread.chunk.connect(self._on_stream_chunk)
            except Exception:
                pass

            self._clear_message_widgets()
            self._recalc_and_sync(immediate=True)
            # Paint the stream-so-far (including tokens streamed while away).
            if self._stream_live_meta_idx is not None:
                self._stream_dirty = True
                try:
                    self._flush_stream()
                except Exception:
                    pass
            self._show_thinking()


            # Restore UI controls
            self._set_ws_combo_to(self.agent._workspace_name)
            self._set_provider_combo_to(self.agent.provider)
            self._update_conv_summary_label()
            self._set_inferring(True)
            self._conv_threads.pop(conv_id, None)
            self._apply_composer_draft_for_conv(conv_id)
            QTimer.singleShot(100, self._restore_viewer_state)
        else:
            # No active thread — load from disk normally
            self._conv_threads.pop(conv_id, None)
            self._thread = None
            self._reset_stream_state()  # don't carry a live stream across the switch
            self._load_conv(conv_id)

    def _message_html_theme_key(self) -> tuple:
        """Palette + typography signature baked into cached message HTML."""
        p = PALETTE
        return (
            p.get("accent"), p.get("text"), p.get("glow_hot"), p.get("muted_text"),
            p.get("accent_muted"), p.get("accent_soft"), p.get("panel"),
            p.get("panel_alt"), p.get("border"), p.get("danger"),
            p.get("accent_bright"),  # used by the emphasis hierarchy (numbers/CAPS)
            ChatMessageWidget._font_size,
            ChatMessageWidget._tool_display_mode,
            bool(ChatMessageWidget._show_tools_hint),
            self.agent.config.get("chat_mode", "fancy"),
            bool(self.agent.config.get("show_timestamps", True)),
            bool(self.agent.config.get("show_usage", False)),
            bool(self.agent.config.get("show_tools_called", True)),
        )

    def _invalidate_message_html_cache(self) -> None:
        for meta in self._message_meta:
            meta.pop("_html", None)
            meta.pop("_html_theme_key", None)

    def _ensure_meta_html(self, meta: dict) -> str:
        """Return themed HTML for a meta row; regenerate when palette/font changes."""
        theme_key = self._message_html_theme_key()
        cached = meta.get("_html")
        if cached and meta.get("_html_theme_key") == theme_key:
            return cached
        if self._has_inline_timeline(meta):
            meta.pop("_streaming", None)
            html = self._render_stream_timeline_body_html(meta, False)
        else:
            extras = ["fenced-code-blocks", "tables", "code-friendly"]
            if meta.get("role") == "user":
                extras.append("break-on-newline")
            # Paste messages render only the TYPED portion in the bubble; the big
            # pasted blocks are shown as separate collapsed cards (their full text
            # still lives in content for the LLM/transcript).
            src = meta.get("_typed", "") if meta.get("_pastes") else meta.get("content", "")
            html = markdown2.markdown(src, extras=extras)
        meta["_html"] = html
        meta["_html_theme_key"] = theme_key
        return html

    def _release_message_widget(self, w, *, to_pool: bool = True) -> None:
        try:
            self._messages_layout.removeWidget(w)
        except Exception:
            pass
        if (
            to_pool
            and isinstance(w, ChatMessageWidget)
            and not w.image_path
            and not getattr(w, "pastes", None)   # paste-card bubbles can't be reused
            and w._chat_mode != "plain"
            and len(self._message_widget_pool) < self._MSG_WIDGET_POOL_MAX
        ):
            try:
                w.setParent(None)
                w.hide()
                self._message_widget_pool.append(w)
                return
            except RuntimeError:
                pass
        try:
            w.deleteLater()
        except RuntimeError:
            pass

    def _obtain_message_widget(self, **kwargs) -> ChatMessageWidget:
        chat_mode = kwargs.get("chat_mode", "fancy")
        image_path = kwargs.get("image_path")
        pastes = kwargs.get("pastes")
        # Image AND paste messages must be built fresh: pooled reconfigure() only
        # rebuilds the text body, not the image card or paste cards.
        if not image_path and not pastes:
            reuse_kwargs = {k: v for k, v in kwargs.items() if k != "pastes"}
            for idx, pooled in enumerate(self._message_widget_pool):
                if pooled._chat_mode == chat_mode:
                    self._message_widget_pool.pop(idx)
                    if pooled.reconfigure(**reuse_kwargs):
                        return pooled
                    try:
                        pooled.deleteLater()
                    except RuntimeError:
                        pass
                    break
        return ChatMessageWidget(parent=self._scroll, **kwargs)

    def _set_conv_loading(self, loading: bool) -> None:
        if loading:
            self._conv_bar.set_hint("Loading conversation…")
        else:
            self._update_conv_hint()

    def _load_conv(self, conv_id: str):
        """Load a conversation from disk without blocking the UI thread."""
        self._conv_load_generation += 1
        gen = self._conv_load_generation
        self._set_conv_loading(True)
        if self._conv_load_thread and self._conv_load_thread.isRunning():
            try:
                self._conv_load_thread.loaded.disconnect()
            except (TypeError, RuntimeError):
                pass
        thread = ConversationLoadThread(conv_id)
        thread.loaded.connect(
            lambda cid, data, g=gen: self._on_conv_loaded(g, cid, data))
        self._conv_load_thread = thread
        thread.start()

    def _on_conv_loaded(self, generation: int, conv_id: str, data: dict | None):
        if generation != self._conv_load_generation or conv_id != self._current_conv_id:
            return
        self._set_conv_loading(False)
        if not data:
            return
        self._apply_loaded_conv_data(conv_id, data)

    def _apply_loaded_conv_data(self, conv_id: str, data: dict):
        """Main-thread hydration after background SQLite load."""
        self._apply_composer_draft_for_conv(conv_id, text=data.get("composer_draft", ""))
        threading.Thread(
            target=self.agent.summarizer.save_state,
            daemon=True,
            name="summarizer-save",
        ).start()
        self.agent.clear_context()
        self._clear_message_widgets()
        self._message_meta = []
        self.agent._provider_override = ""
        self.agent._model_override = ""
        self.agent._system_prompt_override = ""
        self.agent._system_prompt_replace = False
        self.agent._context_note = ""
        self.agent.set_conv_id(conv_id)
        try:
            debug_recorder.load_conversation_from_db(conv_id)
        except Exception:
            pass
        self.agent.set_conversation_cwd(data.get("conversation_cwd", ""), persist=False)
        # Restore a standing self-review rule (scope=conversation) if one was set.
        _refl = data.get("reflect") or {}
        if _refl.get("when"):
            self.agent.set_reflection(_refl.get("when", "after"),
                                      _refl.get("scope", "conversation"),
                                      _refl.get("criteria", ""), persist=False)
        else:
            self.agent.clear_reflection(persist=False)
        # Per-conversation live-streaming preference (default on).
        self.agent._stream_live = bool(data.get("stream_live", True))
        self.agent.summarizer = __import__(
            'core.summarizer', fromlist=['RollingSummarizer']).RollingSummarizer(conv_id)
        conv_streams = data.get("streams", [])
        self.agent.set_conversation_streams(conv_streams)
        try:
            stream_configs = self.agent._get_stream_configs()
            self.agent.summarizer._ensure_streams(stream_configs)
        except Exception:
            pass

        ws_name = data.get("workspace", "")
        workspaces = self.agent.config.get("workspaces", {})
        if ws_name and ws_name not in workspaces:
            ws_name = next(iter(workspaces), "")
            set_conversation_workspace(conv_id, ws_name)
        self.agent.set_workspace(ws_name)
        self._set_ws_combo_to(ws_name)

        provider = data.get("provider", "")
        if provider:
            self.agent.set_provider(provider)
        else:
            self.agent._provider_override = ""
        self._set_provider_combo_to(self.agent.provider)

        model = data.get("model", "")
        if model:
            self.agent.set_model(model)
        else:
            self.agent._model_override = ""
        self._update_conv_summary_label()

        self.agent.set_system_prompt_override(data.get("system_prompt", ""))
        self.agent._system_prompt_replace = bool(data.get("prompt_replace", False))
        self.agent._context_note = data.get("context_note", "") or ""
        self.agent._include_context_timestamps = data.get("include_timestamps", True)

        # Build the full message meta + LLM context in ONE synchronous pass.
        # This is just dict appends — microseconds even for hundreds of messages
        # — so the transcript paints its visible (newest) window immediately,
        # instead of waiting ~21 event-loop ticks of 24-per-batch hydration only
        # to render at the very end. (The DB load already runs off-thread.)
        self._load_hydrate_thumbs_dirty = False
        self._load_hydrate_pending_thumbs: list[dict] = []
        for msg in (data.get("messages") or []):
            role = msg.get("role", "")
            if role in ("user", "assistant"):
                self.agent.context.append({"role": role, "content": msg.get("content", "")})
                if msg.get("image_path") and not msg.get("_thumb"):
                    self._load_hydrate_pending_thumbs.append(msg)
                self._message_meta.append(msg)
            elif role in ("terminal_card", "plan_card", "subagent_card", "chart_card", "diff_card"):
                self._message_meta.append(msg)
        self._finish_conv_hydrate()

    def _finish_conv_hydrate(self):
        # Paint the visible (newest) window NOW — the virtual scroll only renders
        # what's on screen, so first paint is bounded regardless of conversation
        # length. Thumbnail generation for OLD image messages happens afterward,
        # in the background, so it never blocks the first paint.
        self._recalc_and_sync(immediate=True)
        QTimer.singleShot(50, lambda: self._scroll_to_bottom(force=True))
        QTimer.singleShot(200, lambda: self._scroll_to_bottom(force=True))
        QTimer.singleShot(100, self._restore_viewer_state)
        # One-shot: tell main() the startup conversation has rendered so it can
        # fade the splash. Emitted only on the first hydrate.
        if not getattr(self, "_initial_load_emitted", False):
            self._initial_load_emitted = True
            try:
                self.initial_load_finished.emit()
            except Exception:
                pass
            # A question board open at last shutdown is restored now that the
            # transcript (and its context) has rendered.
            try:
                self._maybe_restore_pending_question()
            except Exception:
                pass

        # Generate image thumbnails in the BACKGROUND; refresh once at the end so
        # any visible image message picks up its thumbnail without blocking paint.
        pending_thumbs = self._load_hydrate_pending_thumbs

        def _process_pending_thumbs():
            if not pending_thumbs:
                if self._load_hydrate_thumbs_dirty:
                    self._auto_save()
                    self._recalc_and_sync(immediate=True)
                return
            m = pending_thumbs.pop(0)
            _ensure_thumb(m)
            if m.get("_thumb"):
                self._load_hydrate_thumbs_dirty = True
            QTimer.singleShot(0, _process_pending_thumbs)

        if pending_thumbs:
            QTimer.singleShot(0, _process_pending_thumbs)

    def _load_conv_sync(self, conv_id: str):
        """Synchronous load — reserved for rare paths that already block."""
        data = load_conversation(conv_id)
        if not data:
            return
        self._apply_loaded_conv_data(conv_id, data)

    def _rename_conversation(self, conv_id: str, new_name: str):
        if self._is_remote_id(conv_id):
            return  # remote conversations are renamed on their host, not here
        rename_conversation(conv_id, new_name)
        self._refresh_conv_bar()

    def _delete_current_conversation(self):
        """− button: delete the conversation currently selected in the bar,
        after a confirm. Single-pane mapping of the workspace − button."""
        conv_id = self._current_conv_id
        if not conv_id:
            return
        from ui.glass_dialog import GlassDialog
        name = "this conversation"
        try:
            for c in list_conversations():
                if c["id"] == conv_id:
                    name = c.get("name") or name
                    break
        except Exception:
            pass
        if not GlassDialog.confirm(
                self, "Delete conversation",
                f'Delete "{name}" and all its messages? This cannot be undone.'):
            return
        self._delete_conversation(conv_id)

    def _delete_conversation(self, conv_id: str):
        delete_conversation(conv_id)
        # A deleted thread can't be navigated to — clear any pending alert blink.
        try:
            self._conv_bar.stop_blink(conv_id)
        except Exception:
            pass
        try:
            debug_recorder.drop_conversation(conv_id)
        except Exception:
            pass
        # Clean up viewer state for deleted conversation
        try:
            data = json.loads(open(self._VIEWER_STATE_PATH, "r", encoding="utf-8").read())
            data.pop(conv_id, None)
            with open(self._VIEWER_STATE_PATH, "w", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False))
        except Exception:
            pass
        # Tear down this conversation's terminal panel — kills every shell
        # tree it owned (taskkill /T /F per session) so background processes
        # tied to the deleted chat don't linger.
        try:
            self._right_workspace.terminal_panel.remove_conv(conv_id)
        except Exception:
            pass
        if conv_id == self._current_conv_id:
            convos = list_conversations()
            if convos:
                self._current_conv_id = convos[0]["id"]
                self._right_workspace.terminal_panel.set_active_conv(self._current_conv_id)
                self._load_conv(self._current_conv_id)
            else:
                self._new_conversation()
                return
        self._refresh_conv_bar()

    def _auto_save(self, *, immediate: bool = False):
        """Save the current conversation to disk (background thread by default)."""
        if not self._current_conv_id or self._is_remote_id(self._current_conv_id):
            return  # a remote mirror has no local row to write

        model = self.agent._model_override or self.agent.model
        provider = self.agent._provider_override
        if immediate:
            threading.Thread(
                target=self.agent.summarizer.save_state,
                daemon=True,
                name="summarizer-save",
            ).start()
            existing = get_conversation_meta(self._current_conv_id)
            name = (existing or {}).get("name", "New Chat")
            if name.startswith("New Chat") and self._message_meta:
                first_user = next(
                    (m["content"] for m in self._message_meta if m["role"] == "user"),
                    "",
                )
                if first_user:
                    name = first_user[:40].strip()
                    if len(first_user) > 40:
                        name += "..."
            save_conversation(
                self._current_conv_id, name, self._message_meta,
                workspace=self.agent._workspace_name,
                model=model,
                provider=provider,
                system_prompt=self.agent._system_prompt_override,
                prompt_replace=getattr(self.agent, "_system_prompt_replace", False),
                context_note=getattr(self.agent, "_context_note", ""),
                include_timestamps=getattr(self.agent, "_include_context_timestamps", True),
            )
            self._save_viewer_state()
        else:
            enqueue_conversation_save(
                self._current_conv_id,
                messages=self._message_meta,
                workspace=self.agent._workspace_name,
                model=model,
                provider=provider,
                system_prompt=self.agent._system_prompt_override,
                prompt_replace=getattr(self.agent, "_system_prompt_replace", False),
                context_note=getattr(self.agent, "_context_note", ""),
                include_timestamps=getattr(self.agent, "_include_context_timestamps", True),
                resolve_name=True,
            )
            self._viewer_state_save_timer.start()

    def _clear_message_widgets(self, *, destroy_pool: bool = False):
        self._detach_stream_preview()
        self._last_divider_sig = ()
        self._date_dividers = {}
        self._messages_container.setUpdatesEnabled(False)
        for w in list(self._idx_to_widget.values()):
            self._release_message_widget(w, to_pool=not destroy_pool)
        self._idx_to_widget.clear()
        self._visible_start = 0
        self._visible_end = 0
        while self._messages_layout.count() > 0:
            item = self._messages_layout.takeAt(0)
            w = item.widget()
            if w and w not in self._message_widget_pool:
                try:
                    w.deleteLater()
                except RuntimeError:
                    pass
        if destroy_pool:
            for w in self._message_widget_pool:
                try:
                    w.deleteLater()
                except RuntimeError:
                    pass
            self._message_widget_pool.clear()
        self._messages_container.setUpdatesEnabled(True)

    # ── Virtual scroll: message display ────────────────────────────

    def _add_message(self, sender: str, content: str, tool_names: list[str] = None,
                     image_path: str = None, track: bool = True,
                     precomputed_assistant_html: str = "",
                     typed: str = "", pastes: list = None):
        if track:
            role = "user" if sender == "You" else "assistant" if sender == AGENT_LABEL else None
            if role:
                # Pre-render markdown once and cache it. For paste messages the
                # bubble shows only the TYPED text — the pasted blocks render as
                # separate cards — while `content` keeps the full text for the LLM.
                extras = ["fenced-code-blocks", "tables", "code-friendly"]
                if role == "user":
                    extras.append("break-on-newline")
                if role == "assistant" and precomputed_assistant_html:
                    html = precomputed_assistant_html
                else:
                    html = markdown2.markdown(typed if pastes else content, extras=extras)
                import time as _time
                theme_key = self._message_html_theme_key()
                meta = {
                    "role": role, "content": content,
                    "tool_names": tool_names or [],
                    "image_path": image_path or "",
                    "_html": html,
                    "_html_theme_key": theme_key,
                    "_timestamp": _time.time(),
                }
                if pastes:
                    meta["_pastes"] = pastes
                    meta["_typed"] = typed
                _ensure_thumb(meta)
                self._message_meta.append(meta)
        # Recalculate visible range and sync widgets
        self._recalc_and_sync()
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _recalc_and_sync(self, *, immediate: bool = False):
        """Recalculate the visible window and create/destroy widgets.

        Debounced by default so bursts of tool-call UI updates (plan cards,
        subagent cards, etc.) coalesce into one layout pass per frame batch.
        """
        if immediate:
            self._sync_debounce_timer.stop()
            self._recalc_and_sync_now()
            return
        if not self._sync_debounce_timer.isActive():
            self._sync_debounce_timer.start()

    def _recalc_and_sync_now(self):
        """Immediate sync — used by debounce timer and forced paths."""
        # Any content at all → the pristine intro hint must go.
        if self._message_meta:
            self._hide_intro_hint()
        self._visible_start, self._visible_end = self._calc_range()
        self._baseline_end = self._visible_end
        self._sync_widgets()

    # ── Pristine-state intro hint ──────────────────────────────────────
    def _intro_hint_html(self) -> str:
        p = PALETTE
        muted = p.get("muted_text", "#888888")
        accent = p.get("accent_muted", p.get("accent", "#4ECDC4"))
        return (
            f'<div style="color:{muted}; line-height:155%;">'
            f'<div style="font-size:17pt; color:{accent};">Familiar</div>'
            f'<div style="font-size:10pt;">your local AI agent — try something like:</div>'
            f'<div style="font-size:10pt;">&nbsp;</div>'
            f'<div style="font-size:11pt;">“work in C:\\Users\\you\\projects\\app”'
            f'&nbsp;&nbsp;<span style="color:{accent};">— point me at a folder to work in</span></div>'
            f'<div style="font-size:11pt;">“what’s in this codebase?”</div>'
            f'<div style="font-size:11pt;">“find every TODO and list them”</div>'
            f'<div style="font-size:11pt;">“add a /health endpoint and a test for it”</div>'
            f'<div style="font-size:11pt;">“summarize the last git commit”</div>'
            f'<div style="font-size:10pt;">&nbsp;</div>'
            f'<div style="font-size:9pt; color:{accent};">type below to begin '
            f'· drop a file in · right-click the conversation name to rename</div>'
            f'</div>'
        )

    def _position_intro_hint(self):
        h = getattr(self, "_intro_hint", None)
        if h is None:
            return
        try:
            h.setGeometry(self._messages_container.rect())
            h.raise_()
        except RuntimeError:
            pass

    def show_intro_hint_if_pristine(self):
        """Show the onboarding hint only on a truly fresh start: a single, empty
        conversation. Called from the startup 'lights on' moment so it fades in
        as the wordmark ignites."""
        h = getattr(self, "_intro_hint", None)
        if h is None or self._message_meta:
            return
        try:
            if len(list_conversations()) > 1:
                return
        except Exception:
            pass
        h.setText(self._intro_hint_html())
        self._position_intro_hint()
        h.show()
        h.raise_()
        try:
            self._intro_hint_effect.setOpacity(0.0)
            anim = QPropertyAnimation(self._intro_hint_effect, b"opacity")
            anim.setDuration(700)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.start()
            self._intro_hint_anim = anim
        except Exception:
            self._intro_hint_effect.setOpacity(1.0)

    def _hide_intro_hint(self):
        h = getattr(self, "_intro_hint", None)
        if h is not None and h.isVisible():
            if self._intro_hint_anim is not None:
                self._intro_hint_anim.stop()
                self._intro_hint_anim = None
            h.hide()

    # Terminal/plan cards render collapsed — cap their cost so they don't
    # blow the char budget and cause the visible window to thrash.
    _CARD_CHAR_CAP = 200

    def _meta_char_cost(self, meta: dict) -> int:
        """Return the display-char cost of a meta entry for range budgeting."""
        role = meta.get("role", "")
        c = len(meta.get("content", ""))
        if role in ("terminal_card", "plan_card", "subagent_card", "chart_card", "diff_card"):
            return min(c, self._CARD_CHAR_CAP)
        # Cap a SINGLE message's budget cost. A message larger than _char_limit
        # used to blow the whole window budget, so _calc_range and the slide trim
        # loops collapsed the window down to that one message — evicting the
        # user's scroll position and flinging the view to the top (you could
        # never reach the bottom). A big message is still ONE widget; capping
        # only its BUDGET keeps neighbours loaded so scroll anchoring stays sane.
        return min(c, max(1, self._char_limit // 2))

    def _calc_range(self, anchor_end: int = None) -> tuple[int, int]:
        """Calculate (start, end) index range fitting within char budget.
        Works backwards from anchor_end (default: len of meta)."""
        n = len(self._message_meta)
        if n == 0:
            return 0, 0
        end = anchor_end if anchor_end is not None else n
        end = min(end, n)
        total_chars = 0
        start = end
        for i in range(end - 1, -1, -1):
            c = self._meta_char_cost(self._message_meta[i])
            if total_chars + c > self._char_limit and start < end:
                break
            total_chars += c
            start = i
        return start, end

    def _insert_msg_widget(self, meta_idx: int, widget, ordered_indices: list):
        """Insert a message widget at the correct LAYOUT position for its meta
        index. Computes the position from the real layout (so date-divider rows,
        which also occupy layout slots, are accounted for) — a plain bisect over
        message indices is divider-blind and dropped new messages above a midnight
        divider / the prior reply. ``ordered_indices`` is kept in sync for the
        next insert."""
        layout = self._messages_layout
        # Find the next already-rendered message with a HIGHER meta index; insert
        # right before its widget. If none, append at the end.
        pos_in_order = bisect.bisect_left(ordered_indices, meta_idx)
        layout_pos = None
        for higher in ordered_indices[pos_in_order:]:
            w_after = self._idx_to_widget.get(higher)
            if w_after is not None:
                li = layout.indexOf(w_after)
                if li >= 0:
                    layout_pos = li
                    break
        if layout_pos is None:
            # Append — but stay BEFORE a trailing live stream-preview if present,
            # so a new message never lands below the preview panel.
            layout_pos = layout.count()
            sp = getattr(self, "_stream_preview", None)
            if sp is not None:
                spi = layout.indexOf(sp)
                if spi >= 0:
                    layout_pos = spi
        layout.insertWidget(layout_pos, widget)
        # Show only AFTER reparenting into the layout — a widget shown while it
        # has no parent flashes as a stray top-level window. Pooled bubbles are
        # released hidden, so this is what actually reveals them.
        widget.show()
        bisect.insort(ordered_indices, meta_idx)

    def _sync_widgets(self):
        """Create widgets for indices in range, remove those outside."""
        if self._loading_more:
            return
        self._loading_more = True

        # Suspend layout updates during batch add/remove
        self._messages_container.setUpdatesEnabled(False)

        # Remove out-of-range widgets
        to_remove = [i for i in self._idx_to_widget
                     if i < self._visible_start or i >= self._visible_end]
        for i in to_remove:
            w = self._idx_to_widget.pop(i)
            self._release_message_widget(w, to_pool=True)
        if not hasattr(self, "_date_dividers"):
            self._date_dividers = {}

        # Sorted indices of widgets already in the layout (only message rows here;
        # date dividers are inserted after this loop). Keeps insert positions O(log n)
        # per widget instead of scanning all keys each time.
        ordered_indices = sorted(self._idx_to_widget.keys())

        vp_w = self._scroll.viewport().width()

        # Create missing widgets in order
        for i in range(self._visible_start, self._visible_end):
            if i in self._idx_to_widget:
                continue
            meta = self._message_meta[i]

            # Terminal card — live or persisted
            if meta.get("role") == "terminal_card":
                tc = LiveTerminalCard(meta.get("command", ""))
                if meta.get("_terminal_live"):
                    tc.start_polling()
                    meta_idx = i
                    def _on_finished(ec, idx=meta_idx, card=tc):
                        if idx < len(self._message_meta):
                            self._message_meta[idx]["content"] = card.get_output()
                            self._message_meta[idx]["_terminal_live"] = False
                            self._message_meta[idx]["_exit_code"] = ec
                        self._auto_save()
                    tc.finished.connect(_on_finished)
                else:
                    tc.set_final_output(
                        meta.get("content", ""),
                        meta.get("_exit_code", 0))
                self._idx_to_widget[i] = tc
                self._insert_msg_widget(i, tc, ordered_indices)
                continue

            # Sub-agent card — live or persisted (centered slot, not full width)
            if meta.get("role") == "subagent_card":
                slot = SubAgentCardSlot(
                    meta.get("_job_id", ""),
                    meta.get("_tasks", []),
                )
                if meta.get("_subagent_live"):
                    slot.card.start_polling()
                    meta_idx = i
                    def _on_sa_finished(summary, idx=meta_idx, c=slot.card):
                        if idx < len(self._message_meta):
                            self._message_meta[idx]["_subagent_live"] = False
                            self._message_meta[idx]["_subagent_summary"] = summary
                        self._auto_save()
                    slot.card.finished.connect(_on_sa_finished)
                else:
                    slot.card.set_final_state(meta.get("_subagent_summary", {}))
                self._idx_to_widget[i] = slot
                self._insert_msg_widget(i, slot, ordered_indices)
                continue

            # Plan card — live (polling) or persisted snapshot
            if meta.get("role") == "plan_card":
                pw = PlanWidget()
                if meta.get("_plan_live"):
                    pw.start_polling()
                    self._live_plan_widget = pw
                else:
                    pw.set_final_state(meta.get("_plan_data", {}))
                self._idx_to_widget[i] = pw
                self._insert_msg_widget(i, pw, ordered_indices)
                continue

            # Chart card — bezeled inline chart image
            if meta.get("role") == "chart_card":
                from ui.chart_card import ChartCardSlot
                slot = ChartCardSlot(
                    meta.get("_chart_path", ""),
                    meta.get("_chart_title", ""),
                    meta.get("_chart_type", ""),
                )
                self._idx_to_widget[i] = slot
                self._insert_msg_widget(i, slot, ordered_indices)
                continue

            # Diff card — scrollable, viewer-styled file-edit diff (its own layout)
            if meta.get("role") == "diff_card":
                from ui.diff_card import DiffCardWidget
                dc = DiffCardWidget(
                    meta.get("_diff_path", ""),
                    meta.get("_diff_rows", []),
                    meta.get("_diff_adds", 0),
                    meta.get("_diff_dels", 0),
                )
                dc.open_requested.connect(self._open_edited_file_in_viewer)
                self._idx_to_widget[i] = dc
                self._insert_msg_widget(i, dc, ordered_indices)
                continue

            sender = "You" if meta["role"] == "user" else AGENT_LABEL
            html = self._ensure_meta_html(meta)
            _ensure_thumb(meta)
            w = self._obtain_message_widget(
                sender=sender,
                # Paste bubbles display the typed text only; the full content
                # (with pastes inlined) stays in meta for the LLM/transcript.
                content=(meta.get("_typed", "") if meta.get("_pastes") else meta["content"]),
                tool_names=meta.get("tool_names"),
                image_path=meta.get("_thumb") or meta.get("image_path") or None,
                cached_html=html,
                timestamp=meta.get("_timestamp"),
                usage=meta.get("_usage") if sender == AGENT_LABEL else None,
                show_timestamps=self.agent.config.get("show_timestamps", True),
                show_usage=self.agent.config.get("show_usage", False),
                show_tool_chips=self.agent.config.get("show_tools_called", True),
                chat_mode=self.agent.config.get("chat_mode", "fancy"),
                continuation=not _show_sender_nametag(self._message_meta, i),
                inline_timeline=self._has_inline_timeline(meta),
                pastes=meta.get("_pastes"),
            )
            if vp_w > 0:
                w.apply_wrap_width(max(50, vp_w - 12))

            self._idx_to_widget[i] = w
            self._insert_msg_widget(i, w, ordered_indices)

        # Insert date dividers between messages that cross midnight
        import time as _time
        p = PALETTE

        def _get_ts(meta_idx):
            """Get timestamp from a meta entry, checking neighbors if missing."""
            ts = self._message_meta[meta_idx].get("_timestamp", 0)
            if ts:
                return ts
            # For cards/entries without timestamps, scan nearby entries
            for offset in range(1, 5):
                if meta_idx + offset < len(self._message_meta):
                    t = self._message_meta[meta_idx + offset].get("_timestamp", 0)
                    if t:
                        return t
                if meta_idx - offset >= 0:
                    t = self._message_meta[meta_idx - offset].get("_timestamp", 0)
                    if t:
                        return t
            return 0

        def _format_date(ts):
            date_str = _time.strftime("%b %d, %Y", _time.localtime(ts)).replace(" 0", " ")
            return date_str

        rendered = sorted(self._idx_to_widget.keys())

        def _divider_signature() -> tuple:
            sig: list = []
            if rendered:
                fts = _get_ts(rendered[0])
                if fts:
                    sig.append(("first", _time.localtime(fts)[:3]))
            for j in range(1, len(rendered)):
                prev_ts = _get_ts(rendered[j - 1])
                curr_ts = _get_ts(rendered[j])
                if prev_ts and curr_ts:
                    pd = _time.localtime(prev_ts)[:3]
                    cd = _time.localtime(curr_ts)[:3]
                    if pd != cd:
                        sig.append((rendered[j], cd))
            return tuple(sig)

        div_sig = _divider_signature()
        if div_sig != self._last_divider_sig:
            for dw in list(self._date_dividers.values()):
                try:
                    self._messages_layout.removeWidget(dw)
                    dw.deleteLater()
                except RuntimeError:
                    pass
            self._date_dividers = {}
            self._last_divider_sig = div_sig
            _rebuild_dividers = True
        else:
            _rebuild_dividers = False

        def _make_divider(date_str):
            div = QFrame()
            div.setFrameShape(QFrame.Shape.NoFrame)
            div.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            div.setFixedHeight(24)
            dl = QHBoxLayout(div)
            dl.setContentsMargins(20, 4, 20, 4)
            dl.setSpacing(8)
            l1 = QFrame()
            l1.setFrameShape(QFrame.Shape.HLine)
            l1.setStyleSheet(f"background:{p['glow_hot']};max-height:1px;border:none;")
            dl.addWidget(l1, stretch=1)
            lbl = QLabel(date_str)
            lbl.setFont(QFont("Consolas", max(ChatMessageWidget._font_size - 3, 6)))
            lbl.setStyleSheet(f"color:{p['glow_hot']};border:none;")
            dl.addWidget(lbl)
            l2 = QFrame()
            l2.setFrameShape(QFrame.Shape.HLine)
            l2.setStyleSheet(f"background:{p['glow_hot']};max-height:1px;border:none;")
            dl.addWidget(l2, stretch=1)
            return div

        if _rebuild_dividers:
            # First message divider — show date if it's not today
            if rendered:
                first_ts = _get_ts(rendered[0])
                if first_ts:
                    first_date = _time.localtime(first_ts)[:3]
                    today = _time.localtime()[:3]
                    if first_date != today:
                        divider = _make_divider(_format_date(first_ts))
                        layout_pos = self._messages_layout.indexOf(
                            self._idx_to_widget[rendered[0]])
                        if layout_pos >= 0:
                            self._messages_layout.insertWidget(layout_pos, divider)
                        self._date_dividers[-1] = divider

            # Date crossover dividers between messages
            for j in range(1, len(rendered)):
                prev_ts = _get_ts(rendered[j - 1])
                curr_ts = _get_ts(rendered[j])
                if not prev_ts or not curr_ts:
                    continue
                prev_date = _time.localtime(prev_ts)[:3]
                curr_date = _time.localtime(curr_ts)[:3]
                if prev_date != curr_date:
                    curr_i = rendered[j]
                    divider = _make_divider(_format_date(curr_ts))
                    layout_pos = self._messages_layout.indexOf(
                        self._idx_to_widget[curr_i])
                    if layout_pos >= 0:
                        self._messages_layout.insertWidget(layout_pos, divider)
                    self._date_dividers[curr_i] = divider

        # Dim messages outside the LLM context window (before summary cutoff)
        cutoff_meta = self._get_context_cutoff_meta_index()
        for idx, w in self._idx_to_widget.items():
            dim = cutoff_meta > 0 and idx < cutoff_meta
            setter = getattr(w, "set_context_outside_window", None)
            if callable(setter):
                setter(dim)
            else:
                if isinstance(w.graphicsEffect(), QGraphicsOpacityEffect):
                    w.setGraphicsEffect(None)

        # Resume layout — single reflow
        self._messages_container.setUpdatesEnabled(True)
        self._loading_more = False
        self._place_stream_preview()

    def _stream_in_chat(self) -> bool:
        """True: live tokens render in assistant bubbles; False: preview panel only."""
        return self.agent.config.get("stream_display", "chat") != "preview"

    def _find_live_stream_idx(self) -> int | None:
        for i, meta in enumerate(self._message_meta):
            if meta.get("role") == "assistant" and meta.get("_streaming"):
                return i
        return None

    def _find_live_stream_idx(self) -> int | None:
        for i, meta in enumerate(self._message_meta):
            if meta.get("role") == "assistant" and meta.get("_streaming"):
                return i
        return None

    def _live_stream_meta(self) -> dict | None:
        idx = self._stream_live_meta_idx
        if idx is None:
            idx = self._find_live_stream_idx()
            self._stream_live_meta_idx = idx
        if idx is None or idx >= len(self._message_meta):
            return None
        return self._message_meta[idx]

    def _seal_stream_text_to_timeline(self, meta: dict):
        """Move in-flight narration into the inline timeline (text segment)."""
        text = self._compose_live_stream_text(show_ellipsis=False).strip()
        self._stream_committed_text = ""
        self._stream_buffer = []
        if not text:
            return
        tl = meta.setdefault("_stream_timeline", [])
        tl.append({"type": "text", "content": text})

    def _schedule_plan_resurface(self):
        """Debounce: collapse a burst of step updates into one re-surface."""
        t = getattr(self, "_plan_resurface_timer", None)
        if t is None:
            t = self._plan_resurface_timer = QTimer(self)
            t.setSingleShot(True)
            t.timeout.connect(self._resurface_live_plan)
        t.start(350)

    def _resurface_live_plan(self):
        """Re-show the live checklist beside the latest work.

        If tool rows / narration have landed in the timeline *below* the live
        plan card since it was last rendered, freeze that card on the state the
        user saw and append a fresh live one at the bottom. The agent then
        watches the same plan tick off step-by-step as work proceeds, instead
        of a single card stranded near the top of the turn. No new work beneath
        it → nothing to do (avoids a wall of duplicate cards on rapid edits).
        """
        ref = getattr(self, "_live_plan_timeline_ref", None)
        if not ref or not self._stream_in_chat():
            return
        meta_idx, tl_idx = ref
        if meta_idx >= len(self._message_meta):
            return
        meta = self._message_meta[meta_idx]
        tl = meta.get("_stream_timeline")
        if not isinstance(tl, list) or tl_idx >= len(tl):
            return
        # Has anything other than the plan card itself been appended after it?
        tail = tl[tl_idx + 1:]
        has_new_work = any(
            it.get("type") in ("text", "tools", "tool", "subagent", "diff",
                               "screenshot", "chart")
            and (it.get("content", "").strip() if it.get("type") == "text" else True)
            for it in tail
        )
        if not has_new_work:
            return
        # Fold any in-flight narration in first, so the frozen card sits above it.
        self._seal_stream_text_to_timeline(meta)
        # Freeze the old card on the last-shown state (render path stored it).
        try:
            from tools.plan import get_current_plan
            import copy
            cur = get_current_plan()
            if cur:
                tl[tl_idx]["plan_data"] = copy.deepcopy(cur)
        except Exception:
            pass
        tl[tl_idx]["live"] = False
        # Append a fresh live card at the bottom and re-point the live ref to it.
        tl.append({"type": "plan", "live": True, "plan_data": {}})
        self._live_plan_timeline_ref = (meta_idx, len(tl) - 1)
        self._refresh_live_stream_display(show_ellipsis=self._stream_active)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _timeline_tool_names(self, meta: dict) -> list[str]:
        names: list[str] = []
        for item in meta.get("_stream_timeline", []):
            if item.get("type") == "tools":
                names.extend(item.get("names") or [])
            elif item.get("type") == "tool":
                names.append(item.get("name", ""))
            elif item.get("type") == "subagent":
                names.append("subagent")
            elif item.get("type") == "plan":
                names.append("plan")
            elif item.get("type") == "screenshot":
                names.append("screenshot")
        return [n for n in names if n]

    def _append_timeline_tool(self, meta: dict, name: str):
        """Record a tool call; consecutive tools share one centered row (L→R order)."""
        self._seal_stream_text_to_timeline(meta)
        tl = meta.setdefault("_stream_timeline", [])
        if tl and tl[-1].get("type") == "tools":
            tl[-1]["names"].append(name)
        elif tl and tl[-1].get("type") == "tool":
            tl[-1] = {"type": "tools", "names": [tl[-1].get("name", "tool"), name]}
        else:
            tl.append({"type": "tools", "names": [name]})

    def _timeline_plain_text(self, meta: dict) -> str:
        parts: list[str] = []
        for item in meta.get("_stream_timeline", []):
            if item.get("type") == "text":
                parts.append(item.get("content", ""))
        return "\n\n".join(p for p in parts if p.strip()).strip()

    def _timeline_tools_row_html(self, names: list[str], fs: int,
                                 force: bool = False) -> str:
        if not force and not self.agent.config.get("show_tools_called", True):
            return ""
        mode = self.agent.config.get("tool_display_mode", "chips")
        if mode not in ("chips", "bubbles", "comma"):
            mode = "chips"
        show_hint = bool(self.agent.config.get("show_tools_hint", False))
        return tool_calls_display_html(
            names, fs, mode=mode, show_hint=show_hint, margin="6px 0 8px 0",
        )

    @staticmethod
    def _should_trail_ellipsis(text: str) -> bool:
        """Trailing busy dots only after a completed sentence (ends with '.')."""
        text = text.strip()
        if not text:
            return True
        last = text[-1]
        if last in (":", "?", "!", "…"):
            return False
        return last == "."

    @staticmethod
    def _apply_trailing_ellipsis(text: str) -> str:
        """Turn running commentary into an in-progress sentence with animated dots."""
        import re
        text = text.strip()
        if not text:
            return "..."
        if not ChatWindow._should_trail_ellipsis(text):
            return text
        return re.sub(r"[.!?…]+$", "", text).rstrip() + "..."

    def _timeline_item_body_html(self, item: dict, fs: int) -> str:
        p = PALETTE
        t = item.get("type")
        if t in ("tool", "tools"):
            names = item.get("names") if t == "tools" else [item.get("name", "tool")]
            if not self.agent.config.get("show_tools_called", True):
                # Bubbles off — keep only the always-show tools (the user
                # question), drop the rest.
                names = [n for n in names if n in _ALWAYS_SHOW_TOOLS]
                if not names:
                    return ""
                return self._timeline_tools_row_html(names, fs, force=True)
            return self._timeline_tools_row_html(names, fs)
        if t == "subagent":
            try:
                from ui.gadget_cards import build_subagent_card
                return build_subagent_card(
                    item.get("tasks") or [],
                    summary=item.get("summary") or {},
                    live=bool(item.get("live")),
                    fs=fs,
                )
            except Exception:
                # Never let a card-render error break the whole bubble.
                return self._timeline_tools_row_html(["subagent"], fs)
        if t == "plan":
            pd = item.get("plan_data") or {}
            if item.get("live"):
                # Live plans persist plan_data only on finish — read the
                # in-flight state directly so the card shows the actual plan.
                try:
                    from tools.plan import get_current_plan
                    live_pd = get_current_plan()
                    if live_pd:
                        pd = live_pd
                        # Remember the state we actually showed. When this card
                        # is later frozen (a fresh checklist surfaces below it),
                        # it keeps the snapshot the user last saw instead of
                        # reverting to an older state or duplicating the new one.
                        import copy
                        item["plan_data"] = copy.deepcopy(live_pd)
                except Exception:
                    pass
            try:
                from ui.gadget_cards import build_plan_card
                return build_plan_card(pd, fs=fs, live=bool(item.get("live")))
            except Exception:
                return self._timeline_tools_row_html(["plan"], fs)
        if t == "screenshot":
            path = item.get("image_path") or ""
            if path and os.path.isfile(path):
                return (
                    f'<div style="text-align:center;margin:8px 0;">'
                    f'<span style="color:{p["muted_text"]};font-size:{max(fs - 1, 7)}pt;">'
                    f'Screenshot captured</span></div>'
                )
            return self._timeline_tools_row_html(["screenshot"], fs)
        if t == "chart":
            title = item.get("title") or item.get("chart_type") or "Chart"
            return self._timeline_tools_row_html([f"chart: {title}"], fs)
        if t == "diff":
            try:
                from ui.diff_card import build_diff_card
                html, _a, _d = build_diff_card(
                    item.get("path", ""), item.get("original", ""),
                    item.get("current", ""), fs=fs,
                    expanded=bool(item.get("expanded", False)),
                    card_id=str(item.get("id", "")),
                )
                return html
            except Exception:
                # Never let a diff-render error break the whole bubble.
                return self._timeline_tools_row_html(
                    [f"edited {os.path.basename(item.get('path', '') or 'file')}"], fs)
        return ""

    @staticmethod
    def _has_inline_timeline(meta: dict) -> bool:
        tl = meta.get("_stream_timeline")
        return isinstance(tl, list) and len(tl) > 0

    def _timeline_md_cached(self, text: str) -> str:
        """Markdown for a SEALED timeline segment, cached per content+theme.

        The 100ms stream flush re-renders the whole timeline body; sealed
        text segments are immutable, so re-running markdown2 on each of them
        every tick is pure waste (it grows with turn length — long multi-tool
        turns were the stutter). Keyed by the exact text + the theme signature
        so palette/font changes naturally invalidate."""
        cache = getattr(self, "_tl_md_cache", None)
        if cache is None:
            cache = self._tl_md_cache = {}
        key = self._message_html_theme_key()
        hit = cache.get(text)
        if hit is not None and hit[0] == key:
            return hit[1]
        html = self._markdown_html(text)
        if len(cache) > 256:
            cache.clear()
        cache[text] = (key, html)
        return html

    def _render_stream_timeline_body_html(self, meta: dict,
                                          show_ellipsis: bool = False) -> str:
        """Body HTML: chronologically interleaved narration + inline tool/gadget rows."""
        fs = ChatMessageWidget._font_size
        parts: list[str] = []
        prev_type: str | None = None
        trailing_cfg = bool(self.agent.config.get("trailing_ellipsis", False))
        timeline = meta.get("_stream_timeline", [])
        last_text_idx = None
        for i, item in enumerate(timeline):
            if item.get("type") == "text":
                last_text_idx = i

        cur = self._compose_live_stream_text(show_ellipsis=False).strip()

        last_text = cur
        if not last_text and last_text_idx is not None:
            last_text = (timeline[last_text_idx].get("content") or "").strip()

        if trailing_cfg and show_ellipsis:
            use_trailing = self._should_trail_ellipsis(last_text)
            show_busy_fallback = False
        elif show_ellipsis:
            use_trailing = False
            show_busy_fallback = True
        else:
            use_trailing = False
            show_busy_fallback = False

        for i, item in enumerate(timeline):
            if item.get("type") == "text":
                text = (item.get("content") or "").strip()
                if text:
                    if use_trailing and not cur and i == last_text_idx:
                        text = self._apply_trailing_ellipsis(text)
                    if prev_type == "text":
                        parts.append(
                            f'<div style="height:10px;"></div>'
                        )
                    parts.append(self._timeline_md_cached(text))
                    prev_type = "text"
            else:
                block = self._timeline_item_body_html(item, fs)
                if block:
                    parts.append(block)
                    prev_type = item.get("type")

        if cur:
            if use_trailing:
                cur = self._apply_trailing_ellipsis(cur)
            # Render in-flight tokens through markdown LIVE so formatting snaps in
            # as soon as a span closes (e.g. the moment the closing * arrives),
            # like ChatGPT/Cursor. markdown2 leaves a trailing UNCLOSED marker
            # ('*half') as literal text, so partial spans never mis-format — and
            # at ~0.4ms/call on a 100ms flush it's free. (_markdown_html also runs
            # the quote/label brightening pass.)
            parts.append(self._markdown_html(cur))

        if show_busy_fallback:
            parts.append(self._busy_ellipsis_html())
        elif use_trailing and not parts:
            parts.append(self._markdown_html("..."))

        if not parts:
            return ""
        # Prepend the glow_hot emphasis style so headings / bold / lists render
        # hot in EVERY consumer of this body html — including plain mode, which
        # embeds this cached html directly and otherwise has no style block.
        return (
            _md_emphasis_style()
            + f'<div style="font-family:Consolas;word-wrap:break-word;">'
            + '<div style="height:4px;"></div>'.join(parts)
            + '</div>'
        )

    @staticmethod
    def _busy_ellipsis_html() -> str:
        """Centered animated busy indicator (literal ... replaced at paint time)."""
        p = PALETTE
        fs = ChatMessageWidget._font_size
        return (
            f'<div align="center" style="width:100%;margin:10px 0 8px 0;'
            f'color:{p["muted_text"]};font-size:{max(fs - 1, 7)}pt;">'
            f'...</div>'
        )

    def _render_stream_timeline_combined_html(self, meta: dict,
                                              show_ellipsis: bool = False,
                                              usage: dict | None = None) -> str:
        """Full assistant bubble HTML with one header and inline timeline body."""
        fs = ChatMessageWidget._font_size
        p = PALETTE
        body = self._render_stream_timeline_body_html(meta, show_ellipsis)
        usage_html = ""
        u = usage if usage is not None else meta.get("_usage")
        if u and isinstance(u, dict):
            pt = u.get("prompt_tokens", 0)
            ct = u.get("completion_tokens", 0)
            if pt or ct:
                usage_html = (
                    f'<p style="color:{p["muted_text"]};font-size:{max(fs - 2, 7)}pt;'
                    f'margin-top:6px;opacity:0.75;">tokens in:{pt} out:{ct}</p>'
                )
        ts = meta.get("_timestamp")
        ts_html = ""
        if ts and self.agent.config.get("show_timestamps", True):
            import time as _time
            ts_html = (
                f' <span style="color:{p["muted_text"]};font-size:{max(fs - 2, 7)}pt;'
                f'font-weight:normal;">'
                f'{_time.strftime("%H:%M:%S", _time.localtime(ts))}</span>'
            )
        return (
            f'<style>p {{ margin-top: 0; margin-bottom: 0; }} strong, b {{ color: {p["glow_hot"]}; }} '
            f'h1, h2, h3, h4, h5, h6 {{ color: {p["glow_hot"]}; margin-top: 6px; margin-bottom: 2px; }} '
            f'li {{ color: {p["glow_hot"]}; }}</style>'
            f'<div style="font-family:Consolas; word-wrap:break-word;">'
            f'<p style="margin-bottom:2px;">'
            f'<span style="color:{p["glow_hot"]};font-weight:bold;font-size:{max(fs - 1, 7)}pt;">'
            f'Agent</span>{ts_html}</p>'
            f'<span style="color:{p["text"]}; font-size:{fs}pt;">{body}</span>'
            f'{usage_html}'
            f'</div>'
        )

    def _detach_stream_preview(self):
        """Keep the live preview widget out of the way during layout rebuilds."""
        if not hasattr(self, "_stream_preview"):
            return
        try:
            self._messages_layout.removeWidget(self._stream_preview)
            # removeWidget alone leaves the (still-parented) widget painting at
            # its last geometry — a stale ghost rectangle until the next resize
            # forces a repaint. Hide it AND drop its opacity graphics effect: a
            # QGraphicsOpacityEffect caches the widget into an offscreen pixmap
            # that can keep painting as a ghost box after detach (it only clears
            # on the resize that invalidates the cache — exactly the reported
            # symptom). Clearing the effect removes that cached layer.
            if isinstance(self._stream_preview.graphicsEffect(), QGraphicsOpacityEffect):
                self._stream_preview.setGraphicsEffect(None)
            self._stream_preview.setVisible(False)
        except Exception:
            pass

    def _place_stream_preview(self):
        """Append the live preview after the visible transcript while streaming."""
        if not hasattr(self, "_stream_preview") or self._stream_in_chat():
            return
        self._detach_stream_preview()
        if not self._stream_active:
            self._stream_preview.setVisible(False)
            return
        self._messages_layout.addWidget(self._stream_preview)
        self._stream_preview.setVisible(True)
        if self._pinned_to_bottom and not self._sliding:
            QTimer.singleShot(0, lambda: self._scroll_to_bottom(force=True))

    def _get_context_cutoff_meta_index(self) -> int:
        """Compute which messages would be outside the LLM context window
        given the current char_limit setting. Mirrors the summarizer's
        cutoff logic so dimming updates live when the user changes settings.

        Returns a meta index: messages before this are outside the window.
        """
        cfg = self.agent.config
        cache_key = (
            id(self._message_meta),
            len(self._message_meta),
            bool(cfg.get("enable_summarization", True)),
            int(cfg.get("summary_char_limit", 15000)),
        )
        if cache_key == self._cutoff_meta_cache_key:
            return self._cutoff_meta_cache_value

        if not cfg.get("enable_summarization", True):
            self._cutoff_meta_cache_key = cache_key
            self._cutoff_meta_cache_value = 0
            return 0
        char_limit = cfg.get("summary_char_limit", 15000)
        target = int(char_limit * 0.6)  # same as summarizer

        # Walk context backwards (user/assistant only) accumulating chars
        # to find the cutoff, then map that to a meta index.
        chat_entries = []  # (meta_idx, char_count) for user/assistant entries
        for meta_idx, meta in enumerate(self._message_meta):
            if meta.get("role") in ("user", "assistant"):
                content = meta.get("content", "")
                chat_entries.append((meta_idx, len(content) if isinstance(content, str) else 0))

        total_chars = sum(c for _, c in chat_entries)
        if total_chars < char_limit:
            self._cutoff_meta_cache_key = cache_key
            self._cutoff_meta_cache_value = 0
            return 0  # everything fits, no dimming

        running = 0
        cutoff_chat_idx = len(chat_entries)
        for i in range(len(chat_entries) - 1, -1, -1):
            if running + chat_entries[i][1] > target:
                cutoff_chat_idx = i + 1
                break
            running += chat_entries[i][1]

        min_keep = 6
        cutoff_chat_idx = min(cutoff_chat_idx, max(0, len(chat_entries) - min_keep))
        if cutoff_chat_idx <= 2:
            self._cutoff_meta_cache_key = cache_key
            self._cutoff_meta_cache_value = 0
            return 0

        # Convert chat_entries index to meta index
        val = (
            chat_entries[cutoff_chat_idx][0]
            if cutoff_chat_idx < len(chat_entries)
            else 0
        )
        self._cutoff_meta_cache_key = cache_key
        self._cutoff_meta_cache_value = val
        return val

    def _format_meta_plain_transcript(self, meta: dict) -> str:
        """Plain-text one block for a single _message_meta entry (for clipboard export)."""
        role = meta.get("role", "")
        content = meta.get("content", "")
        if not isinstance(content, str):
            content = str(content) if content is not None else ""

        if role == "user":
            lines = ["You:", content.strip()]
            ip = meta.get("image_path") or meta.get("_thumb")
            if ip:
                lines.append(f"[attached image: {ip}]")
            return "\n".join(lines).strip()
        if role == "assistant":
            tools = meta.get("tool_names") or []
            if tools:
                header = f"Agent [tools: {', '.join(str(t) for t in tools)}]:"
            else:
                header = "Agent:"
            return f"{header}\n{content.strip()}".strip()
        if role == "terminal_card":
            cmd = meta.get("command", "")
            ec = meta.get("_exit_code")
            head = f"[Terminal: {cmd}]"
            if ec is not None:
                head += f" (exit {ec})"
            body = content.strip()
            return f"{head}\n{body}".strip() if body else head
        if role == "plan_card":
            pd = meta.get("_plan_data") or {}
            if not pd:
                return "[Plan]"
            try:
                body = json.dumps(pd, ensure_ascii=False, indent=2)
            except (TypeError, ValueError):
                body = str(pd)
            return f"[Plan]\n{body}"
        if role == "subagent_card":
            lines = ["[Subagent]"]
            for t in meta.get("_tasks") or []:
                tid = t.get("task_id", "")
                st = t.get("status", "")
                nm = t.get("name", "")
                label = nm or tid or "task"
                lines.append(f"  - {label}: {st}")
            summ = meta.get("_subagent_summary") or {}
            if summ:
                try:
                    lines.append(json.dumps(summ, ensure_ascii=False, indent=2))
                except (TypeError, ValueError):
                    lines.append(str(summ))
            return "\n".join(lines).strip()
        if role == "chart_card":
            c = (meta.get("content") or "[chart]").strip()
            path = meta.get("_chart_path", "")
            if path:
                return f"{c}\n[chart file: {path}]"
            return c
        return ""

    def _copy_in_context_transcript(self):
        """Copy messages from the summary/context cutoff through the end (non-dimmed range)."""
        cutoff = self._get_context_cutoff_meta_index()
        blocks: list[str] = []
        for meta in self._message_meta[cutoff:]:
            block = self._format_meta_plain_transcript(meta)
            if block.strip():
                blocks.append(block.strip())
        text = "\n\n".join(blocks)
        if cutoff > 0:
            text = f"...\n\n{text}" if text else "..."
        QApplication.clipboard().setText(text)

    def _is_at_bottom(self, threshold: int = 30) -> bool:
        vbar = self._scroll.verticalScrollBar()
        return (vbar.maximum() - vbar.value()) <= threshold

    def _scroll_to_bottom(self, force: bool = True):
        """Scroll to bottom and KEEP it pinned across the next few layout passes.

        A large message's rich-text QLabel finalizes its height over several
        frames, so a single setValue(maximum()) on the next tick reads a STALE,
        too-small maximum and parks the view well above the true bottom — the
        "it scrolled itself up" bug. We re-assert the bottom for a short window
        so we catch the final, larger height, but bail the instant the user
        scrolls away (range stopped growing yet the value dropped) so a reader
        mid-conversation is never yanked."""
        if not force and not self._is_at_bottom():
            return
        # Refill the re-assert budget (~0.5s of 40ms ticks). Active streaming
        # keeps refilling this; once updates stop, the chain counts down and ends.
        self._bottom_stick_left = 12
        if not getattr(self, "_bottom_stick_running", False):
            self._bottom_stick_running = True
            self._bs_prev_max = -1
            QTimer.singleShot(0, self._assert_bottom_once)

    def _assert_bottom_once(self):
        if getattr(self, "_sliding", False):
            self._bottom_stick_running = False
            return
        vbar = self._scroll.verticalScrollBar()
        cur_max = vbar.maximum()
        cur_val = vbar.value()
        prev_max = self._bs_prev_max
        # Range stopped growing AND the value sits well below bottom → the user
        # deliberately scrolled away. Stop re-asserting and let them read.
        if prev_max >= 0 and cur_val < cur_max - 8 and cur_max <= prev_max:
            self._bottom_stick_running = False
            return
        vbar.setValue(cur_max)
        self._bs_prev_max = cur_max
        self._bottom_stick_left -= 1
        if self._bottom_stick_left > 0:
            QTimer.singleShot(40, self._assert_bottom_once)
        else:
            self._bottom_stick_running = False

    def eventFilter(self, source, event):
        """Rewrap labels on container resize. Debounced."""
        from PyQt6.QtCore import QEvent
        if source is self._messages_container and event.type() == QEvent.Type.Resize:
            # Keep the pristine intro hint centered as the chat area resizes.
            if getattr(self, "_intro_hint", None) is not None and self._intro_hint.isVisible():
                self._position_intro_hint()
            if getattr(self, "_chat_wrap_layout_frozen", False):
                return super().eventFilter(source, event)
            if not hasattr(self, '_resize_timer'):
                self._resize_timer = QTimer(self)
                self._resize_timer.setSingleShot(True)
                self._resize_timer.timeout.connect(self._on_debounced_resize)
            self._resize_timer.start(80)
        return super().eventFilter(source, event)

    def _on_debounced_resize(self):
        """Batch-update label widths after resize settles."""
        if getattr(self, "_chat_wrap_layout_frozen", False):
            return
        vp_w = self._scroll.viewport().width()
        if vp_w > 0:
            wrap_w = max(50, vp_w - 12)
            vp = self._scroll.viewport()
            vp_rect = vp.rect() if vp else None
            from PyQt6.QtCore import QRect
            for w in self._idx_to_widget.values():
                if vp_rect is not None:
                    try:
                        top = w.mapTo(vp, w.rect().topLeft())
                        bottom = w.mapTo(vp, w.rect().bottomRight())
                        if not vp_rect.intersects(QRect(top, bottom)):
                            continue
                    except RuntimeError:
                        continue
                if hasattr(w, 'apply_wrap_width'):
                    w.apply_wrap_width(wrap_w)
        self._update_viewport_visibility()

    def _update_viewport_visibility(self):
        """Tell message widgets whether they're visible so ellipsis animation
        only runs for on-screen widgets."""
        try:
            vp = self._scroll.viewport()
            if not vp:
                return
            from PyQt6.QtCore import QRect
            vp_rect = vp.rect()
            wrap_w = max(50, vp.width() - 12)
            for w in self._idx_to_widget.values():
                try:
                    top = w.mapTo(vp, w.rect().topLeft())
                    bottom = w.mapTo(vp, w.rect().bottomRight())
                    visible = vp_rect.intersects(QRect(top, bottom))
                    w.set_visible_in_viewport(visible)
                    # Offscreen widgets are skipped by the debounced-resize
                    # rewrap for speed; refresh their wrap width (and pinned
                    # body height) as they come into view so a width change
                    # while they were offscreen can't leave them mis-sized.
                    if (visible and getattr(w, "_wrap_width", wrap_w) != wrap_w
                            and hasattr(w, "apply_wrap_width")):
                        w.apply_wrap_width(wrap_w)
                except RuntimeError:
                    pass
        except Exception:
            pass

    # ── Virtual scroll: load on scroll ───────────────────────────────

    def _on_scroll(self):
        if self._sliding:
            return
        # Track whether user is pinned to bottom
        self._pinned_to_bottom = self._is_at_bottom(threshold=30)
        if not self._load_check_scheduled:
            self._load_check_scheduled = True
            QTimer.singleShot(100, self._do_load_check)
        if not hasattr(self, '_vis_timer'):
            self._vis_timer = QTimer(self)
            self._vis_timer.setSingleShot(True)
            self._vis_timer.timeout.connect(self._update_viewport_visibility)
        self._vis_timer.start(150)

    def _on_scrollbar_range_changed(self, min_val, max_val):
        """Auto-pin to bottom when content/viewport changes, if user was at bottom."""
        if self._pinned_to_bottom and not self._sliding:
            self._scroll.verticalScrollBar().setValue(max_val)

    def _do_load_check(self):
        self._load_check_scheduled = False
        if self._loading_more or self._sliding:
            return
        vbar = self._scroll.verticalScrollBar()
        near_top = vbar.value() <= 200
        near_bottom = vbar.value() >= vbar.maximum() - 200

        if near_top and self._visible_start > 0:
            self._slide_window_up()
        elif near_bottom and self._visible_end < len(self._message_meta):
            self._slide_window_down()

    def _find_anchor_widget(self) -> tuple:
        """Find the first visible widget and its y-position for scroll anchoring."""
        for idx in sorted(self._idx_to_widget.keys()):
            w = self._idx_to_widget[idx]
            try:
                return w, w.pos().y()
            except RuntimeError:
                continue
        return None, 0

    def _capture_anchor(self, min_idx: int) -> tuple:
        """Anchor on the lowest-index rendered widget at or after ``min_idx`` — a
        widget that SURVIVES an upcoming top-trim. Anchoring on the old topmost
        (which slide-down then deletes) is what flung the view: the restore read
        a dead widget, the delta went wild, and you landed at the top."""
        for idx in sorted(self._idx_to_widget.keys()):
            if idx < min_idx:
                continue
            w = self._idx_to_widget[idx]
            try:
                return w, w.pos().y()
            except RuntimeError:
                continue
        return None, 0

    def _restore_scroll_anchor(self, anchor_widget, old_y):
        """Restore scroll position by anchoring to a widget's position delta."""
        if anchor_widget is None:
            return
        try:
            new_y = anchor_widget.pos().y()
            delta = new_y - old_y
            vbar = self._scroll.verticalScrollBar()
            vbar.setValue(vbar.value() + delta)
        except RuntimeError:
            pass

    def _total_window_chars(self, start: int, end: int) -> int:
        """Sum of char costs for the given range."""
        return sum(self._meta_char_cost(self._message_meta[i]) for i in range(start, end))

    def _slide_window_up(self):
        """Scroll near top — load older messages, trim bottom to stay within budget."""
        if self._loading_more or self._sliding or self._visible_start == 0:
            return
        self._sliding = True

        # Load up to half the char budget of older messages
        budget = self._char_limit // 2
        total = 0
        new_start = self._visible_start
        for i in range(self._visible_start - 1, -1, -1):
            c = self._meta_char_cost(self._message_meta[i])
            total += c
            new_start = i
            if total >= budget:
                break
        if new_start == self._visible_start:
            self._sliding = False
            return

        # Trim bottom to keep total within char_limit
        new_end = self._visible_end
        running = self._total_window_chars(new_start, new_end)
        while running > self._char_limit and new_end > new_start + 1:
            new_end -= 1
            running -= self._meta_char_cost(self._message_meta[new_end])

        # Anchor to the current top widget
        anchor, anchor_y = self._find_anchor_widget()

        self._visible_start = new_start
        self._visible_end = new_end
        self._sync_widgets()

        # Force layout, then restore position via widget anchor
        self._messages_container.adjustSize()
        self._messages_container.updateGeometry()
        QTimer.singleShot(0, lambda: self._finish_slide(anchor, anchor_y))

    def _slide_window_down(self):
        """Scroll near bottom — load newer messages, trim top to stay within budget."""
        if self._loading_more or self._sliding or self._visible_end >= len(self._message_meta):
            return
        self._sliding = True

        # If at bottom and we've expanded upward past baseline, snap back
        if self._is_at_bottom(threshold=100) and self._visible_end >= self._baseline_end:
            baseline_start, _ = self._calc_range(anchor_end=self._baseline_end)
            if self._visible_start < baseline_start:
                self._visible_start = baseline_start
                self._visible_end = self._baseline_end
                self._sync_widgets()
                # End the slide BEFORE pinning to bottom: _assert_bottom_once
                # (and _on_scrollbar_range_changed) skip work while _sliding, so
                # snapping back to bottom must clear the flag first.
                self._sliding = False
                self._scroll_to_bottom(force=True)
                return

        # Load up to half the char budget of newer messages
        budget = self._char_limit // 2
        total = 0
        new_end = self._visible_end
        for i in range(self._visible_end, len(self._message_meta)):
            c = self._meta_char_cost(self._message_meta[i])
            total += c
            new_end = i + 1
            if total >= budget:
                break
        if new_end == self._visible_end:
            self._sliding = False
            return

        # Trim top to keep total within char_limit
        new_start = self._visible_start
        running = self._total_window_chars(new_start, new_end)
        while running > self._char_limit and new_start < new_end - 1:
            running -= self._meta_char_cost(self._message_meta[new_start])
            new_start += 1

        # Anchor on a widget that survives the top-trim (index >= new_start),
        # NOT the old topmost — which we're about to delete.
        anchor, anchor_y = self._capture_anchor(new_start)

        self._visible_start = new_start
        self._visible_end = new_end
        if new_end > self._baseline_end:
            self._baseline_end = new_end
        self._sync_widgets()

        self._messages_container.adjustSize()
        self._messages_container.updateGeometry()
        QTimer.singleShot(0, lambda: self._finish_slide(anchor, anchor_y))

    def _finish_slide(self, anchor_widget, anchor_y):
        self._restore_scroll_anchor(anchor_widget, anchor_y)
        self._sliding = False

    def _finish_slide_no_anchor(self):
        self._sliding = False

    # Map tool names to verb phrases for the "Agent is ..." indicator when no
    # target filename is available (see _verb_for_tool — file_* use basename when possible).
    # Anything not in this map falls back to "Agent is using {tool_name}".
    _TOOL_VERBS = {
        "file_read": "reading a file",
        "file_write": "writing a file",
        "file_edit": "editing a file",
        "file_show": "opening a file viewer",
        "file_search": "searching files",
        "file_watcher": "watching files",
        "multi_edit": "editing multiple files",
        "multi_file": "working on multiple files",
        "multi_file_write": "writing files",
        "grep": "searching code",
        "glob": "finding files",
        "terminal": "running a terminal command",
        "ssh": "running a remote command",
        "web_fetch": "fetching a web page",
        "web_search": "searching the web",
        "browser": "browsing the web",
        "browser_auto": "automating the browser",
        "read_browser": "reading the browser",
        "screenshot": "taking a screenshot",
        "ocr": "reading text from an image",
        "vision": "analyzing an image",
        "transcribe": "transcribing audio",
        "tts": "speaking aloud",
        "play_sound": "playing a sound",
        "clipboard": "using the clipboard",
        "chart": "drawing a chart",
        "pdf_gen": "generating a PDF",
        "doc_parser": "parsing a document",
        "data_extract": "extracting data",
        "diff_tool": "diffing files",
        "archive": "archiving files",
        "git": "working with git",
        "lint": "linting code",
        "lsp": "looking up a symbol",
        "hot_reload": "hot-reloading",
        "db_query": "querying a database",
        "vector_search": "doing a vector search",
        "session_search": "searching past sessions",
        "memory": "checking memory",
        "plan": "updating the plan",
        "thinking": "thinking",
        "project_loader": "loading the project",
        "workspace": "switching workspaces",
        "workspace_browser": "browsing the workspace",
        "workspace_terminal": "working in the terminal",
        "checkpoint": "saving a checkpoint",
        "subagent": "delegating to a sub-agent",
        "http_client": "making an HTTP request",
        "tasks": "managing tasks",
        "notify": "sending a notification",
        "ask_user_question": "waiting for a response",
    }

    # Tools that represent the agent's internal grind (reading, searching,
    # parsing, fetching, querying, executing) — play the "terminal" sound set.
    # Excludes anything that produces a user-facing artifact (browser, viewer,
    # charts, PDFs, screenshots, notifications, TTS) and the edit tools above
    # (which have their own sound).
    _TERMINAL_SOUND_TOOLS = frozenset({
        # Shell / execution
        "terminal", "ssh",
        # File reads & scans
        "file_read", "file_search", "file_watcher", "multi_file",
        # Search
        "grep", "glob",
        # Code inspection
        "lint", "lsp", "diff_tool",
        # Data parsing (output goes into the agent's context, not to the user)
        "doc_parser", "data_extract", "ocr", "transcribe", "vision",
        # Network fetches
        "http_client", "web_fetch", "web_search",
        # Data stores
        "db_query", "vector_search", "session_search", "memory",
        # SCM / archive / project state
        "git", "archive", "project_loader", "hot_reload", "checkpoint",
        # User-facing file surface (viewer flip)
        "file_show",
    })

    @staticmethod
    def _tool_target_filename(tool_name: str, args: dict | None) -> str:
        """Basename or short summary for typing-indicator text, or "" if unknown."""
        if not args or not isinstance(args, dict):
            return ""
        for key in ("path", "file_path", "target_path", "filepath"):
            v = args.get(key)
            if v and isinstance(v, str) and v.strip():
                base = os.path.basename(v.strip().rstrip("/\\"))
                if base and base not in (".", ".."):
                    return base
        if tool_name == "multi_file_write":
            files = args.get("files")
            if isinstance(files, list) and files:
                if len(files) == 1 and isinstance(files[0], dict):
                    p = (files[0].get("path") or "").strip()
                    if p:
                        b = os.path.basename(p.rstrip("/\\"))
                        return b if b else ""
                return f"{len(files)} files"
        return ""

    def _verb_for_tool(self, tool_name: str, args: dict | None = None) -> str:
        a = args if isinstance(args, dict) else {}
        fname = self._tool_target_filename(tool_name, a)

        if tool_name == "file_read":
            return f"reading {fname}" if fname else "reading a file"
        if tool_name == "file_write":
            return f"writing {fname}" if fname else "writing a file"
        if tool_name in ("file_edit", "multi_edit"):
            return f"editing {fname}" if fname else "editing a file"
        if tool_name == "multi_file_write":
            return f"writing {fname}" if fname else "writing files"
        if tool_name == "file_show":
            return f"opening {fname}" if fname else "opening the file viewer"

        verb = self._TOOL_VERBS.get(tool_name)
        if verb:
            return verb
        pretty = (tool_name or "a tool").replace("_", " ")
        return f"using {pretty}"

    def _show_thinking(self):
        """Show 'Agent is typing...' in the bottom bar after 2s delay."""
        p = PALETTE
        self._thinking = True
        self._thinking_dots_state = 0
        self._typing_prefix = f"{AGENT_LABEL} is typing"
        self._thinking_timer = QTimer(self)
        self._thinking_timer.timeout.connect(self._animate_typing)
        # Delay before showing
        self._thinking_delay = QTimer(self)
        self._thinking_delay.setSingleShot(True)
        self._thinking_delay.timeout.connect(self._reveal_typing)
        self._thinking_delay.start(2000)

    def _reveal_typing(self):
        """Start the dot animation after delay."""
        if not self._thinking:
            return
        self._thinking_timer.start(400)
        self._animate_typing()  # show immediately

    def _animate_typing(self):
        # Waiting on the user (ask_user_question board open) is NOT working —
        # show no "is typing…" dots until they answer.
        if getattr(self, "_awaiting_user_answer", False):
            self._typing_label.setText("")
            return
        # Blank ONLY while text is actively streaming (the bubble's trailing
        # ellipsis is the cue then). While a TOOL is running mid-turn — reading a
        # file, querying a db, etc. — keep showing the status so the bottom bar
        # never goes silent while the agent is clearly busy.
        tool_busy = bool(getattr(self, "_active_tool_verb", ""))
        if getattr(self, "_stream_active", False) and not tool_busy:
            self._typing_label.setText("")
            return
        p = PALETTE
        dots = "." * (self._thinking_dots_state % 3 + 1)
        pad = " " * (3 - len(dots))  # reserve space
        prefix = getattr(self, "_typing_prefix", f"{AGENT_LABEL} is typing")
        self._typing_label.setText(f"{prefix}{dots}{pad}")
        self._typing_label.setStyleSheet(
            f"color: {p['accent_muted']}; background: transparent; border: none;"
            f" font-style: italic;")
        self._thinking_dots_state += 1

    def _set_typing_prefix(self, prefix: str):
        """Update the 'Agent is ...' phrase and repaint immediately (if revealed)."""
        self._typing_prefix = prefix
        if self._thinking and self._thinking_timer.isActive():
            self._animate_typing()

    def _hide_thinking(self):
        self._active_tool_verb = ""
        if self._thinking:
            self._thinking = False
            try:
                self._thinking_timer.stop()
                self._thinking_delay.stop()
            except (RuntimeError, AttributeError):
                pass
            self._typing_label.setText("")
            p = PALETTE
            self._typing_label.setStyleSheet(
                f"color: transparent; background: transparent; border: none;")
        self._typing_prefix = f"{AGENT_LABEL} is typing"

    # ── Live token streaming ──────────────────────────────────────────

    @staticmethod
    def _markdown_html(text: str) -> str:
        try:
            html = markdown2.markdown(
                text, extras=["fenced-code-blocks", "tables", "code-friendly"])
            return _emphasize_html(html)
        except Exception:
            return _emphasize_html(f"<pre>{html_module.escape(text)}</pre>")

    @staticmethod
    def _streaming_plain_html(text: str) -> str:
        """Unused as of live-markdown streaming (in-flight text now goes through
        _markdown_html). Kept as a plain-text fallback if a perf regression ever
        makes per-tick markdown undesirable."""
        escaped = html_module.escape(text, quote=False)
        body = escaped.replace("\n", "<br>").replace("  ", "&nbsp;&nbsp;")
        body = _emphasize_html(body)
        return (
            f'<div style="white-space:pre-wrap;word-wrap:break-word;margin:0;">'
            f'{body}</div>'
        )

    def _apply_stream_preview_style(self):
        if not hasattr(self, "_stream_preview"):
            return
        p = PALETTE
        fs = max(int(self.agent.config.get("chat_font_size", 11)) - 1, 8)
        self._stream_preview.setStyleSheet(
            f"#streamPreview {{"
            f" background: {p['panel_alt']}; color: {p['muted_text']};"
            f" border: 1px solid {p['border']}; border-radius: 4px;"
            f" margin: 8px 10px 4px 10px; padding: 6px 10px;"
            f" font-family: Consolas; font-size: {fs}pt; }}"
        )

    def _stream_preview_opacity_effect(self) -> QGraphicsOpacityEffect:
        eff = self._stream_preview.graphicsEffect()
        if not isinstance(eff, QGraphicsOpacityEffect):
            eff = QGraphicsOpacityEffect(self._stream_preview)
            self._stream_preview.setGraphicsEffect(eff)
        return eff

    def _fade_stream_preview(self, show: bool):
        if not hasattr(self, "_stream_preview"):
            return
        if self._stream_fade_anim is not None:
            try:
                self._stream_fade_anim.stop()
            except RuntimeError:
                pass
            self._stream_fade_anim = None

        eff = self._stream_preview_opacity_effect()
        anim = QPropertyAnimation(eff, b"opacity", self)
        anim.setDuration(260)
        anim.setEasingCurve(QEasingCurve.Type.InOutQuad)
        if show:
            self._place_stream_preview()
            eff.setOpacity(0.0)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
        else:
            anim.setStartValue(eff.opacity())
            anim.setEndValue(0.0)
            anim.finished.connect(self._finish_hide_stream_preview)
        self._stream_fade_anim = anim
        anim.start()

    def _finish_hide_stream_preview(self):
        if hasattr(self, "_stream_preview"):
            self._detach_stream_preview()
            self._stream_preview.clear()
            self._stream_preview.setVisible(False)
        self._stream_fade_anim = None

    def _ensure_live_stream_message(self):
        if self._stream_live_meta_idx is not None:
            return
        import time as _time
        self._stream_live_meta_idx = len(self._message_meta)
        self._message_meta.append({
            "role": "assistant",
            "content": "",
            "tool_names": [],
            "image_path": "",
            "_html": "",
            "_timestamp": _time.time(),
            "_streaming": True,
            "_stream_timeline": [],
        })
        self._recalc_and_sync(immediate=True)

    def _close_live_stream_bubble(self):
        """Finalize the current streaming bubble in place so the NEXT streamed
        text starts a fresh bubble below it. Used to interleave diff cards in
        true chronological order (text → card → text) instead of dumping all
        cards after the whole turn's narration."""
        idx = self._stream_live_meta_idx
        if idx is None:
            idx = self._find_live_stream_idx()
        self._stream_live_meta_idx = None
        if idx is None or idx >= len(self._message_meta):
            return
        meta = self._message_meta[idx]
        self._seal_stream_text_to_timeline(meta)
        self._stream_buffer = []
        self._stream_committed_text = ""
        if not meta.get("_stream_timeline"):
            # Nothing streamed before the edit — drop the empty shell. Caller
            # re-syncs immediately (like _finalize_stream_response) so the
            # index shift is reconciled cleanly.
            self._message_meta.pop(idx)
            return
        meta.pop("_streaming", None)
        meta["content"] = self._timeline_plain_text(meta)
        meta["tool_names"] = self._timeline_tool_names(meta)
        meta["_html"] = self._render_stream_timeline_body_html(meta, False)
        meta["_html_theme_key"] = self._message_html_theme_key()

    def _compose_live_stream_text(self, show_ellipsis: bool = False) -> str:
        """Merge committed rounds, in-flight tokens, and optional thinking ellipsis."""
        parts: list[str] = []
        if self._stream_committed_text.strip():
            parts.append(self._stream_committed_text.rstrip())
        cur = "".join(self._stream_buffer).strip()
        if cur:
            parts.append(cur)
        text = "\n\n".join(parts)
        if show_ellipsis:
            text = f"{text}\n\n..." if text else "..."
        return text

    def _refresh_live_stream_display(self, show_ellipsis: bool = False):
        """Paint the single growing assistant bubble during chat-mode streaming."""
        meta = self._live_stream_meta()
        if meta is None:
            return
        idx = self._stream_live_meta_idx
        body_html = self._render_stream_timeline_body_html(meta, show_ellipsis)
        meta["content"] = self._timeline_plain_text(meta)
        cur = self._compose_live_stream_text(show_ellipsis=False).strip()
        if cur:
            meta["content"] = (
                (meta["content"] + "\n\n" if meta["content"] else "") + cur
            )
        meta["_html"] = body_html
        meta.pop("_html_theme_key", None)
        widget = self._idx_to_widget.get(idx)
        if isinstance(widget, ChatMessageWidget):
            # Text must update every tick, but the height re-measure
            # (a second full QTextDocument parse of the whole bubble) is the
            # per-tick O(turn-length) cost. During live streaming we pin to the
            # bottom anyway and finalize measures exactly, so throttle the
            # measure to ~4/sec — text stays current, the double-parse stops
            # dominating long multi-tool turns.
            import time as _t
            now = _t.monotonic()
            measure = (now - getattr(self, "_stream_measure_last", 0.0)) >= 0.25
            if measure:
                self._stream_measure_last = now
            widget.update_content(
                meta["content"], body_html,
                tool_names=[], inline_timeline=True,
                measure_height=measure,
            )
        else:
            self._recalc_and_sync(immediate=True)
        if self._pinned_to_bottom and not self._sliding:
            QTimer.singleShot(0, lambda: self._scroll_to_bottom(force=True))

    def _note_live_stream_tool(self, name: str):
        """Append an inline tool chip to the live bubble timeline (chat mode)."""
        if not self._stream_in_chat() or not name:
            return
        if name in ("plan", "subagent", "screenshot"):
            return
        self._ensure_live_stream_message()
        meta = self._live_stream_meta()
        if meta is None:
            return
        self._seal_stream_text_to_timeline(meta)
        if (name in _ALWAYS_SHOW_TOOLS
                or self.agent.config.get("show_tools_called", True)):
            self._append_timeline_tool(meta, name)
        self._refresh_live_stream_display(show_ellipsis=True)

    # ── In-chat diff cards (replaces the disruptive "yank to viewer") ────
    def _note_live_stream_diff(self, path: str, original: str):
        """Drop a scrollable diff card into the chat flow as its own message row
        (role='diff_card'), Cursor/Claude-Code style. UI-ONLY — it stores compact
        precomputed diff rows (no full file) and never reaches LLM context; the
        agent transcript only serializes user/assistant content."""
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                current = f.read()
        except OSError:
            return
        try:
            from ui.diff_card import compute_rows
            rows, adds, dels = compute_rows(original or "", current)
        except Exception:
            return
        if adds == 0 and dels == 0:
            return  # no-op edit — nothing to show

        # Close the current text bubble so the card lands right after the
        # narration produced so far AND any further narration starts a NEW
        # bubble below the card — true chronological interleaving. The split
        # flag tells finalize not to re-append the full reply (which would
        # duplicate the pre-edit text into the trailing bubble).
        if self._stream_in_chat():
            self._close_live_stream_bubble()
            self._stream_did_split = True

        import time as _t
        self._message_meta.append({
            "role": "diff_card",
            "content": f"[diff: {os.path.basename(path)} +{adds} -{dels}]",
            "_diff_path": path,
            "_diff_rows": rows,
            "_diff_adds": adds,
            "_diff_dels": dels,
            "_timestamp": _t.time(),
        })
        # immediate=True reconciles the widget map after the possible empty-bubble
        # pop in _close_live_stream_bubble (mirrors _finalize_stream_response).
        self._recalc_and_sync(immediate=True)
        QTimer.singleShot(50, self._scroll_to_bottom)
        self._auto_save()

    def _toggle_diff_expand(self, card_id: str, expand: bool):
        """Expand/collapse a diff card in place and re-render only its bubble."""
        if not card_id:
            return
        for idx, meta in enumerate(self._message_meta):
            tl = meta.get("_stream_timeline")
            if not tl:
                continue
            for item in tl:
                if item.get("type") == "diff" and str(item.get("id")) == card_id:
                    item["expanded"] = expand
                    body = self._render_stream_timeline_body_html(meta, False)
                    meta["_html"] = body
                    widget = self._idx_to_widget.get(idx)
                    if isinstance(widget, ChatMessageWidget):
                        widget.update_content(
                            meta.get("content", ""), body,
                            tool_names=meta.get("tool_names", []),
                            inline_timeline=True)
                    else:
                        self._recalc_and_sync(immediate=True)
                    return

    def _open_edited_file_in_viewer(self, path: str):
        """User-initiated jump from a diff card to the file's inline diff in the
        viewer (the old auto-behavior, now opt-in)."""
        if not path:
            return
        try:
            self._right_workspace.set_workspace_page(3)
        except Exception:
            pass
        original = getattr(self, "_edit_originals", {}).get(path)
        try:
            self._file_viewer.show_edit(path, original if original is not None else "",
                                        surface=True)
        except Exception:
            try:
                self._file_viewer.load_file(path)
            except Exception:
                pass

    def _handle_familiar_link(self, href: str) -> bool:
        """Handle familiar:// chat links. Returns True if consumed."""
        if not href.startswith("familiar://"):
            return False
        from urllib.parse import urlparse, parse_qs, unquote
        try:
            u = urlparse(href)
            qs = parse_qs(u.query)
        except Exception:
            return True
        action = u.netloc or u.path.lstrip("/")
        if action == "openfile":
            self._open_edited_file_in_viewer(unquote((qs.get("path") or [""])[0]))
        elif action == "diffmore":
            self._toggle_diff_expand((qs.get("id") or [""])[0], True)
        elif action == "diffless":
            self._toggle_diff_expand((qs.get("id") or [""])[0], False)
        return True

    # ── Focus-aware edit signal (blink, never steal focus) ──────────────
    def _signal_edit_blink(self, path: str):
        """Ambient signal that the agent edited a file. What blinks depends on
        where the user's attention is:
          - dock collapsed     → splitter handle + pending-edit count badge
          - dock open, off-tab → flash the File tab button
          - dock open, on File → blink the edited file's tree leaf
        """
        sizes = self._chat_hsplitter.sizes()
        collapsed = len(sizes) >= 2 and sizes[1] <= 10
        if collapsed:
            try:
                self._blink_splitter_handle_fast()
            except Exception:
                pass
            self._bump_pending_edit_badge()
            return
        try:
            page = self._right_workspace.current_workspace_page()
        except Exception:
            page = -1
        if page != 3:
            try:
                self._right_workspace.flash_file_tab()
            except Exception:
                pass
        else:
            try:
                self._file_viewer.blink_tree_path(path)
            except Exception:
                pass

    def _ensure_edit_badge(self) -> "QLabel":
        b = getattr(self, "_edit_badge", None)
        if b is None:
            p = PALETTE
            b = QLabel(self)
            b.setObjectName("editBadge")
            b.setAlignment(Qt.AlignmentFlag.AlignCenter)
            b.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            b.setStyleSheet(
                f"QLabel#editBadge {{ color:{p['background']};"
                f"background:{p['accent_bright']};border-radius:7px;"
                f"padding:0 4px; }}")
            b.hide()
            self._edit_badge = b
            try:
                self._chat_hsplitter.splitterMoved.connect(self._maybe_clear_edit_badge)
            except Exception:
                pass
        return b

    def _position_edit_badge(self):
        b = getattr(self, "_edit_badge", None)
        if b is None:
            return
        try:
            handle = self._chat_hsplitter.handle(1)
            top_left = handle.mapTo(self, QPoint(0, 0))
            x = top_left.x() + handle.width() // 2 - b.width() // 2
            y = top_left.y() + 6
            b.move(max(0, x), max(0, y))
        except Exception:
            pass

    def _bump_pending_edit_badge(self):
        self._pending_edits = getattr(self, "_pending_edits", 0) + 1
        b = self._ensure_edit_badge()
        b.setText(f"{self._pending_edits}")
        b.adjustSize()
        self._position_edit_badge()
        b.show()
        b.raise_()

    def _maybe_clear_edit_badge(self, *_args):
        """When the dock is opened, the pending edits have been 'seen' — reset."""
        sizes = self._chat_hsplitter.sizes()
        if len(sizes) >= 2 and sizes[1] > 10:
            self._pending_edits = 0
            b = getattr(self, "_edit_badge", None)
            if b is not None:
                b.hide()

    def _append_stream_round(self):
        """End of a model round — fold narration into the inline timeline."""
        text = "".join(self._stream_buffer).strip()
        self._stream_buffer = []
        self._stream_dirty = False
        if text:
            if self._stream_committed_text.strip():
                self._stream_committed_text = (
                    self._stream_committed_text.rstrip() + "\n\n" + text)
            else:
                self._stream_committed_text = text
        if not self._stream_in_chat():
            return
        meta = self._live_stream_meta()
        if meta is not None and self._compose_live_stream_text(show_ellipsis=False).strip():
            self._seal_stream_text_to_timeline(meta)
            self._refresh_live_stream_display(show_ellipsis=True)

    def _on_stream_round_start(self):
        """New model round — append prior narration or reset the preview panel."""
        self._publish_host_turn_event("round_start")
        self._mirror_text_last = 0.0
        if self._stream_in_chat():
            self._append_stream_round()
            return
        self._stream_buffer = []
        self._stream_dirty = False
        if hasattr(self, "_stream_preview"):
            self._stream_preview.clear()

    def _on_stream_chunk(self, delta: str):
        """Buffer a streamed token. The flush timer paints it (throttled)."""
        if not delta:
            return
        self._stream_buffer.append(delta)
        self._stream_dirty = True
        # Text is flowing again — the bubble's trailing ellipsis takes over the
        # "busy" cue, so drop the tool status.
        self._active_tool_verb = ""
        first_chunk = not self._stream_active
        self._stream_active = True
        if first_chunk:
            self._typing_label.setText("")
            if not self._stream_in_chat():
                self._fade_stream_preview(show=True)
        if not self._stream_flush_timer.isActive():
            self._stream_flush_timer.start()

    def _flush_stream(self):
        if not self._stream_dirty:
            return
        self._stream_dirty = False
        text = "".join(self._stream_buffer)
        if not text and not self._stream_committed_text:
            return
        # Mirror this conversation's live text to any peer watching it, throttled.
        import time as _t
        now = _t.monotonic()
        if now - getattr(self, "_mirror_text_last", 0.0) >= 0.2:
            self._mirror_text_last = now
            self._publish_host_turn_event("text", text=text)
        if self._stream_in_chat():
            self._ensure_live_stream_message()
            self._refresh_live_stream_display(show_ellipsis=True)
        else:
            self._stream_preview.setPlainText(text)
            sb = self._stream_preview.verticalScrollBar()
            sb.setValue(sb.maximum())
            if self._pinned_to_bottom and not self._sliding:
                QTimer.singleShot(0, lambda: self._scroll_to_bottom(force=True))

    def _finalize_stream_response(self, reply: str, tool_names: list,
                                  reply_html: str, extra_meta: dict) -> tuple[bool, int | None]:
        """Merge the final reply into the live bubble, if one exists (chat mode).

        Returns (handled, meta_index_of_final_assistant_message).
        """
        try:
            self._stream_flush_timer.stop()
        except (RuntimeError, AttributeError):
            pass
        self._stream_active = False
        self._stream_dirty = False

        if not self._stream_in_chat():
            self._stream_buffer = []
            self._stream_committed_text = ""
            return False, None

        display = (reply or "").strip()
        idx = self._stream_live_meta_idx
        if idx is None:
            idx = self._find_live_stream_idx()
        # A diff-card split may have closed the last bubble. If trailing narration
        # is still buffered (committed/in-flight) with no live bubble to hold it,
        # open a fresh one so it isn't dropped.
        if idx is None and self._stream_in_chat() and \
                self._compose_live_stream_text(show_ellipsis=False).strip():
            self._ensure_live_stream_message()
            idx = self._stream_live_meta_idx
        self._stream_live_meta_idx = None
        if idx is None:
            return False, None

        meta = self._message_meta[idx]

        if not display and not meta.get("_stream_timeline"):
            if 0 <= idx < len(self._message_meta):
                self._message_meta.pop(idx)
                self._recalc_and_sync(immediate=True)
            return True, None

        self._seal_stream_text_to_timeline(meta)
        self._stream_buffer = []
        self._stream_committed_text = ""

        # When this turn split bubbles around diff cards, the full reply spans
        # multiple bubbles — re-appending it whole here would duplicate the
        # earlier text into the trailing bubble. Trust the streamed segments.
        if display and not getattr(self, "_stream_did_split", False):
            tl = meta.setdefault("_stream_timeline", [])
            if not (tl and tl[-1].get("type") == "text"
                    and tl[-1].get("content", "").strip() == display):
                tl.append({"type": "text", "content": display, "final": True})

        meta.pop("_streaming", None)
        meta["content"] = self._timeline_plain_text(meta) or display
        meta["tool_names"] = self._timeline_tool_names(meta)
        body_html = self._render_stream_timeline_body_html(meta, False)
        meta["_html"] = body_html
        meta["_html_theme_key"] = self._message_html_theme_key()
        if extra_meta:
            meta.update(extra_meta)

        widget = self._idx_to_widget.get(idx)
        if isinstance(widget, ChatMessageWidget):
            widget.update_content(
                meta["content"],
                self._render_stream_timeline_body_html(meta, False),
                tool_names=meta["tool_names"],
                usage=extra_meta.get("_usage") if extra_meta else None,
                inline_timeline=True,
            )
        else:
            self._recalc_and_sync(immediate=True)
        if self._pinned_to_bottom and not self._sliding:
            QTimer.singleShot(0, lambda: self._scroll_to_bottom(force=True))
        return True, idx

    def _abort_live_stream(self):
        """Drop any in-progress streaming UI (stop/error paths)."""
        try:
            self._stream_flush_timer.stop()
        except (RuntimeError, AttributeError):
            pass
        self._stream_active = False
        self._stream_buffer = []
        self._stream_committed_text = ""
        self._stream_dirty = False
        if self._stream_in_chat():
            if self._stream_live_meta_idx is not None:
                idx = self._stream_live_meta_idx
                self._stream_live_meta_idx = None
                if (0 <= idx < len(self._message_meta)
                        and self._message_meta[idx].get("_streaming")):
                    self._message_meta.pop(idx)
                    self._recalc_and_sync(immediate=True)
            else:
                idx = self._find_live_stream_idx()
                if idx is not None:
                    self._message_meta.pop(idx)
                    self._recalc_and_sync(immediate=True)
        elif hasattr(self, "_stream_preview") and self._stream_preview.isVisible():
            self._fade_stream_preview(show=False)
        else:
            self._finish_hide_stream_preview()

    def _end_stream(self):
        """Clear streaming timers/state; hide preview panel when applicable."""
        try:
            self._stream_flush_timer.stop()
        except (RuntimeError, AttributeError):
            pass
        self._stream_active = False
        self._stream_buffer = []
        self._stream_dirty = False
        if not self._stream_in_chat():
            if hasattr(self, "_stream_preview") and self._stream_preview.isVisible():
                self._fade_stream_preview(show=False)
            else:
                self._finish_hide_stream_preview()

    # ── Agent browser routing ─────────────────────────────────────────

    def _route_browser_to_workspace(self, url: str):
        """Load URL in the conversation's browser tab in the right workspace."""
        bp = self._right_workspace.browser_panel
        if not bp.has_embedded_browser():
            return
        conv_id = self._current_conv_id or "default"
        conv_name = self._get_current_conv_name()
        bp.get_or_create_for_conv(conv_id, conv_name, url)
        self._right_workspace.set_workspace_page(2)
        bp.switch_to_conv(conv_id)
        if self._ws_size() <= 10:
            self._expand_workspace(0.45)

    # ── Agent terminal routing ─────────────────────────────────────────

    def _route_terminal_to_workspace(self, cmd: str):
        """Route command/output to the conversation terminal without forcing UI reveal."""
        is_collapsed = self._ws_size() < 20

        panel = self._right_workspace.terminal_panel
        conv_id = self._current_conv_id or "default"
        conv_name = self._get_current_conv_name()
        session = panel.get_or_create_for_conv(conv_id, conv_name)
        session.append_agent_command(cmd)
        if is_collapsed:
            # Keep the splitter closed; just signal subtle attention.
            self._blink_splitter_handle_fast()
        else:
            # If the workspace is already visible, keep tab/page in sync.
            self._right_workspace.set_workspace_page(4)
            panel.switch_to_conv(conv_id)
        # Stream subprocess output into the terminal surface
        self._pipe_terminal_output_to_session(session)

    def _get_current_conv_name(self) -> str:
        """Return the display name of the current conversation."""
        try:
            for c in list_conversations():
                if c["id"] == self._current_conv_id:
                    return c.get("name", "Agent") or "Agent"
        except Exception:
            pass
        return "Agent"

    _active_pipe_timer: QTimer | None = None  # Track the active pipe timer

    def _pipe_terminal_output_to_session(self, session):
        """Poll the terminal output queue and stream each line into the terminal surface.

        Each call captures the *current* output queue and runs its own timer
        until the process emits a sentinel (None). Multiple pipes can run
        concurrently — important for background processes that stay alive
        while the agent runs additional foreground commands. A pipe ends
        only when its captured queue closes; nothing kills it externally.
        """
        from tools.terminal import get_output_queue

        timer = QTimer(self)
        self._active_pipe_timer = timer  # most-recent reference, used by stop button
        if not hasattr(self, "_pipe_timers"):
            self._pipe_timers = []
        self._pipe_timers.append(timer)
        captured_q = [None]
        wait_ticks = [0]

        def _cleanup():
            timer.stop()
            try:
                self._pipe_timers.remove(timer)
            except ValueError:
                pass
            if self._active_pipe_timer is timer:
                self._active_pipe_timer = None

        def _poll():
            from tools.terminal import get_output_queue as _gq
            if captured_q[0] is None:
                q = _gq()
                if q is None:
                    wait_ticks[0] += 1
                    if wait_ticks[0] > 40:  # 2s timeout — process never started
                        _cleanup()
                    return
                captured_q[0] = q

            q_ref = captured_q[0]
            batch = []
            done = False
            for _ in range(200):
                try:
                    line = q_ref.get_nowait()
                except Exception:
                    break
                if line is None:
                    done = True
                    break
                if isinstance(line, str) and line.startswith("__EXIT_CODE__:"):
                    try:
                        ec = int(line.split(":", 1)[1])
                        if ec != 0:
                            batch.append(f"\n[exit {ec}]\n")
                    except Exception:
                        pass
                    continue
                if isinstance(line, str):
                    batch.append(line)
            if batch:
                try:
                    session._term.append_process_output("".join(batch))
                except RuntimeError:
                    _cleanup()
                    return
            if done:
                _cleanup()

        timer.timeout.connect(_poll)
        timer.start(80)

    # ── File attach ───────────────────────────────────────────────────

    # ── File viewer state persistence ─────────────────────────────────

    _VIEWER_STATE_PATH = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "viewer_state.json")

    def _collect_viewer_state(self) -> dict:
        """Snapshot viewer layout from the UI thread (cheap)."""
        sizes = self._chat_hsplitter.sizes()
        total = sum(sizes) or 1
        return {
            "paths": self._file_viewer.get_open_paths(),
            "active": self._file_viewer.get_active_index(),
            "ratio": self._ws_size() / total if total else 0,
            "workspace_page": self._right_workspace.current_workspace_page(),
            "workspace_page_rev": 2,
            "browser": self._right_workspace.browser_panel.get_state(),
            # Explorer navigation spot — root the tree is showing + which folders
            # are drilled open — so the user doesn't lose their place on restart.
            "explorer_root": self._file_viewer.get_explorer_root(),
            "explorer_expanded": self._file_viewer.get_expanded_dirs(),
        }

    @staticmethod
    def _write_viewer_state_file(conv_id: str, state: dict, path: str) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[conv_id] = state
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _flush_viewer_state_save(self):
        """Debounced viewer-state write — disk I/O off the typing hot path."""
        cid = self._current_conv_id
        if not cid:
            return
        state = self._collect_viewer_state()
        path = self._VIEWER_STATE_PATH
        threading.Thread(
            target=self._write_viewer_state_file,
            args=(cid, state, path),
            daemon=True,
            name="viewer-state-save",
        ).start()

    def _save_viewer_state(self):
        """Persist current file viewer state (sync — used on close)."""
        cid = self._current_conv_id
        if not cid:
            return
        self._write_viewer_state_file(cid, self._collect_viewer_state(), self._VIEWER_STATE_PATH)

    def _restore_viewer_state(self):
        """Restore file viewer state for the current conversation."""
        cid = self._current_conv_id
        if not cid:
            return
        try:
            data = json.loads(open(self._VIEWER_STATE_PATH, "r", encoding="utf-8").read())
        except Exception:
            data = {}
        state = data.get(cid)
        self._file_viewer.close_all_tabs()
        # New conversation context → forget the previous conv's pinned root so
        # this conv's saved spot (or the workspace default) applies cleanly.
        try:
            self._file_viewer.reset_explorer_pin()
        except Exception:
            pass
        if not state:
            self._file_viewer._ensure_scratch_tab()
            self._right_workspace.browser_panel.restore_state(None)
            self._right_workspace.set_workspace_page(3)
            self._sync_file_explorer_root()
            self._collapse_workspace()
            try:
                from tools.workspace_sound_watch import mark_viewer_ready
                mark_viewer_ready()
            except Exception:
                pass
            return
        # Restore splitter ratio. ratio == 0 only means the pane was COLLAPSED
        # at the last save — it is NOT a "no saved state" sentinel. The explorer
        # root, open tabs, and expanded folders are still valid and must be
        # restored either way (treating ratio-0 as no-state was the bug where a
        # restart forgot the user's navigation and re-rooted to the workspace).
        try:
            ratio = float(state.get("ratio") or 0)
        except (TypeError, ValueError):
            ratio = 0.0
        if ratio > 0:
            total = self._chat_hsplitter.width() or 800
            ws = int(total * ratio)
            self._set_split(total - ws, ws)
        # Restore tabs
        for path in state.get("paths", []):
            if os.path.isfile(path):
                self._file_viewer.load_file(path)
        if not self._file_viewer._tabs:
            self._file_viewer._ensure_scratch_tab()
        try:
            active = int(state.get("active", 0) or 0)
        except (TypeError, ValueError):
            active = 0
        if 0 <= active < len(self._file_viewer._tabs):
            self._file_viewer._tab_widget.setCurrentIndex(active)
        try:
            page = int(state.get("workspace_page", 0) or 0)
        except (TypeError, ValueError):
            page = 3
        try:
            page_rev = int(state.get("workspace_page_rev", 1) or 1)
        except (TypeError, ValueError):
            page_rev = 1
        if page_rev < 2:
            # Migrate indices from old order: Files, Browser, Terminal, Notes, Calendar
            _legacy = {0: 3, 1: 2, 2: 4, 3: 0, 4: 1}
            page = _legacy.get(page, page)
        self._right_workspace.set_workspace_page(
            page if page in (0, 1, 2, 3, 4) else 3)
        self._right_workspace.browser_panel.restore_state(state.get("browser"))
        # Restore the explorer's navigation spot for this conversation: the saved
        # root (if it still exists) wins; otherwise fall back to the workspace
        # folder. Then re-open whichever tree folders were drilled in.
        saved_root = (state.get("explorer_root") or "").strip()
        if saved_root and os.path.isdir(saved_root):
            # Restoring the user's last spot for this conversation → pin it so a
            # post-turn workspace sync won't override it.
            self._file_viewer.set_explorer_root(saved_root, pinned=True)
        else:
            self._sync_file_explorer_root()
        expanded = state.get("explorer_expanded") or []
        if expanded:
            # Defer: the QFileSystemModel populates rows asynchronously, so the
            # child indexes aren't available the instant we set the root.
            QTimer.singleShot(
                150, lambda d=list(expanded): self._file_viewer.restore_expanded_dirs(d))
        if ratio <= 0:
            # The pane was collapsed at save time: restore the same resting
            # look — but everything above (root, tabs, expansion) is loaded,
            # so expanding the pane shows the user's spot, not the workspace.
            self._collapse_workspace()
        try:
            from tools.workspace_sound_watch import mark_viewer_ready
            mark_viewer_ready()
        except Exception:
            pass

    # ── File viewer ────────────────────────────────────────────────────

    # ── Workspace splitter helpers (side-agnostic) ─────────────────────
    # The tool workspace can sit on the left OR right of the chat (config
    # "workspace_side"); _ws_index is its splitter index. These keep every
    # collapse/expand/visibility check working regardless of side.

    def _ws_size(self) -> int:
        """Current pixel width of the workspace pane (0 when collapsed)."""
        sizes = self._chat_hsplitter.sizes()
        i = getattr(self, "_ws_index", 1)
        return sizes[i] if len(sizes) > i else 0

    def _set_split(self, chat_px: int, ws_px: int):
        """Apply sizes in the splitter's actual index order for the chosen side."""
        sizes = [0, 0]
        sizes[getattr(self, "_chat_index", 0)] = max(0, int(chat_px))
        sizes[getattr(self, "_ws_index", 1)] = max(0, int(ws_px))
        self._chat_hsplitter.setSizes(sizes)

    def _collapse_workspace(self):
        """Hide the workspace pane (width 0), giving all space to the chat."""
        self._set_split(1, 0)

    def _expand_workspace(self, frac: float = 0.45):
        """Reveal the workspace pane at ~frac of the total width."""
        total = self._chat_hsplitter.width() or 800
        ws = int(total * frac)
        self._set_split(total - ws, ws)

    def _toggle_file_viewer(self):
        """Toggle the workspace pane open/closed via splitter sizes."""
        if self._ws_size() > 10:
            self._collapse_workspace()
        else:
            self._expand_workspace(0.45)

    def _sync_file_explorer_root(self):
        """Point the File viewer sidebar at the current workspace folder — but
        only if the user hasn't pinned their own root (auto=True). This is what
        stops per-turn syncs from yanking the tree back to the workspace and
        losing wherever the user navigated."""
        try:
            self._file_viewer.set_explorer_root(
                self._workspace_folder_path(), auto=True)
        except Exception:
            pass

    def _workspace_folder_path(self) -> str:
        """Absolute path of the active workspace folder (for the file-tree root
        and integrated terminal cwd).

        Falls back to a real workspace destination — the configured
        default_workspace, else the first defined workspace — rather than
        os.getcwd(), which is just wherever the process happened to launch and
        produced the stale-root bug on restart."""
        from core.workspace_paths import resolve_workspace_entry_path

        workspaces = self.agent.config.get("workspaces", {}) or {}

        def _resolve(name: str) -> str | None:
            ws = workspaces.get(name, {}) or {}
            path = (ws.get("path") or "").strip()
            if path:
                rp = resolve_workspace_entry_path(path)
                if rp.is_dir():
                    return str(rp)
            return None

        # 1) active conversation's workspace
        hit = _resolve(self.agent._workspace_name)
        if hit:
            return hit
        # 2) configured default, then 3) first defined workspace
        hit = _resolve(self.agent.config.get("default_workspace", ""))
        if hit:
            return hit
        for name in workspaces:
            hit = _resolve(name)
            if hit:
                return hit
        # 4) last resort
        return os.getcwd()

    def _show_workspace_terminal(self):
        """Focus terminal page only when the right workspace is already visible."""
        if self._ws_size() <= 10:
            self._blink_splitter_handle_fast()
            return
        self._right_workspace.set_workspace_page(4)
        self._right_workspace.terminal_panel.focus_active_input()

    def open_file_in_viewer(self, path: str, highlight: str = ""):
        """Programmatic entry point — open file in viewer, blink if collapsed or unfocused."""
        _hl = repr(highlight[:80]) if highlight else "(none)"
        print(f"[viewer] open_file_in_viewer path={path!r} highlight={_hl}", flush=True)
        sizes = self._chat_hsplitter.sizes()
        if sizes[1] > 10:
            self._right_workspace.set_workspace_page(3)
        self._file_viewer.load_file(path)
        # Optional pulse-highlight of a literal quote
        if highlight:
            try:
                self._file_viewer.pulse_highlight_current_tab(highlight)
            except Exception as e:
                print(f"[viewer] pulse_highlight failed: {e}")
        # Border flash — "hey, we are here now"
        try:
            self._file_viewer.flash_border(times=5)
        except Exception as e:
            print(f"[viewer] flash_border failed: {e}")
        sizes = self._chat_hsplitter.sizes()
        if sizes[1] <= 10:
            # Viewer is collapsed — blink the handle to get attention
            self._blink_splitter_handle()
        # If app isn't focused, blink the taskbar icon — unless the user is
        # away (the flash would dismiss a running screensaver for no one).
        if not QApplication.activeWindow() and not _user_is_away():
            try:
                QApplication.alert(self.window(), 3000)
            except Exception:
                pass

    def _on_agent_edit_file(self, path: str, original: str):
        """Agent edited a file. NON-DISRUPTIVE: update the viewer silently in the
        background (so a later click-through is instant), drop an in-chat diff
        card, and blink an ambient signal — but never switch the user's tab."""
        # Remember the original so a diff-card click can re-show the real diff
        # even after several files were edited.
        self._edit_originals = getattr(self, "_edit_originals", {})
        if original is not None:
            self._edit_originals[path] = original

        # Silent viewer state update — NO set_workspace_page(), no tab switch.
        try:
            self._file_viewer.show_edit(path, original, surface=False)
        except TypeError:
            # Older signature without the surface kwarg.
            try:
                self._file_viewer.show_edit(path, original)
            except Exception:
                import traceback; traceback.print_exc()
        except Exception:
            import traceback; traceback.print_exc()

        # Chronological in-chat diff card (UI-only; never enters LLM context).
        try:
            self._note_live_stream_diff(path, original or "")
        except Exception:
            import traceback; traceback.print_exc()

        # Ambient, focus-aware blink.
        try:
            self._signal_edit_blink(path)
        except Exception:
            pass

        # Taskbar blink only when someone is actually there to see it — an
        # OS attention flash dismisses a running screensaver on Windows.
        if not QApplication.activeWindow() and not _user_is_away():
            try:
                QApplication.alert(self.window(), 3000)
            except Exception:
                pass

    def _surface_file_viewer(self):
        """Handle viewer attention without forcing the splitter open."""
        sizes = self._chat_hsplitter.sizes()
        if sizes[1] <= 10:
            self._blink_splitter_handle_fast()
        else:
            # Already open — quick single-flash of the handle to draw attention
            self._flash_splitter_handle_once()

    @staticmethod
    def _splitter_idle_ss(p: dict) -> str:
        """Idle (hollow) splitter handle style. Two 1px vertical accent_muted
        edges bracket the handle so it reads as an outline rather than a fill —
        visually distinct from a scrollbar. Hover state fills with the gradient
        so the user gets the affordance only when intent matters."""
        accent = QColor(p["accent"])
        ar, ag, ab = accent.red(), accent.green(), accent.blue()
        return f"""
            QSplitter::handle:horizontal {{
                background: transparent;
                border-left: 1px solid {p['accent_muted']};
                border-right: 1px solid {p['accent_muted']};
            }}
            QSplitter::handle:horizontal:hover {{
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba({ar},{ag},{ab},150),
                    stop:0.5 rgba({ar},{ag},{ab},255),
                    stop:1 rgba({ar},{ag},{ab},150));
                border: none;
            }}
        """

    @staticmethod
    def _splitter_attention_ss(p: dict) -> str:
        """Solid-bright fill — used by attention-seeking blink animations."""
        accent = QColor(p["accent"])
        ar, ag, ab = accent.red(), accent.green(), accent.blue()
        return f"""
            QSplitter::handle:horizontal {{
                background: rgba({ar},{ag},{ab},255);
                border: none;
            }}
        """

    def _flash_splitter_handle_once(self):
        """Single bright->normal flash on an already-open splitter handle."""
        p = PALETTE
        splitter = self._chat_hsplitter
        original_ss = splitter.styleSheet()
        splitter.setStyleSheet(self._splitter_attention_ss(p))
        QTimer.singleShot(220, lambda: splitter.setStyleSheet(original_ss))

    def _blink_splitter_handle(self):
        """Blink the horizontal splitter handle to attract attention, then auto-expand."""
        p = PALETTE
        bright_ss = self._splitter_attention_ss(p)
        normal_ss = self._splitter_idle_ss(p)
        splitter = self._chat_hsplitter
        count = [0]

        def _tick():
            count[0] += 1
            if count[0] >= 6:
                splitter.setStyleSheet(normal_ss)
                # Auto-expand after blink
                total = splitter.width()
                splitter.setSizes([int(total * 0.55), int(total * 0.45)])
                return
            if count[0] % 2 == 1:
                splitter.setStyleSheet(bright_ss)
            else:
                splitter.setStyleSheet(normal_ss)
            QTimer.singleShot(250, _tick)

        _tick()

    # ── File attach ───────────────────────────────────────────────────

    def _attach_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Attach File", "",
            "All supported (*.png *.jpg *.jpeg *.gif *.webp *.pdf *.docx *.xlsx *.xls *.pptx *.txt *.csv *.json *.md);;"
            "Images (*.png *.jpg *.jpeg *.gif *.webp);;"
            "Documents (*.pdf *.docx *.xlsx *.xls *.pptx);;"
            "Text (*.txt *.csv *.json *.md);;"
            "All files (*)")
        if path:
            self._show_pending_image(path)

    def _show_pending_image(self, path: str, label: str = None):
        """Populate the preview bar with a thumbnail + filename."""
        self._pending_image = path
        p = PALETTE
        # Try to load a thumbnail for image files
        pixmap = ChatMessageWidget._load_image_pixmap(path, max_size=100)
        if pixmap and not pixmap.isNull():
            thumb = pixmap.scaled(44, 44,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            # Round corners on thumbnail
            rounded = QPixmap(thumb.size())
            rounded.fill(QColor("transparent"))
            painter = QPainter(rounded)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            clip = QPainterPath()
            clip.addRoundedRect(0, 0, thumb.width(), thumb.height(), 4, 4)
            painter.setClipPath(clip)
            painter.drawPixmap(0, 0, thumb)
            painter.end()
            self._image_thumb.setPixmap(rounded)
        else:
            self._image_thumb.clear()
            self._image_thumb.setText("\U0001f4ce")  # 📎
            self._image_thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        display = label or os.path.basename(path)
        elided = self._image_name_label.fontMetrics().elidedText(
            display, Qt.TextElideMode.ElideMiddle, self._image_name_label.maximumWidth())
        self._image_name_label.setText(elided)
        self._image_name_label.setToolTip(display)
        self._image_preview.setToolTip("Remove attachment with ✕ or Esc")
        self._image_preview.setStyleSheet(
            f"QFrame#imagePreview {{ background: {p['panel_alt']};"
            f"border: 1px solid {p['border']}; border-radius: 6px; }}")
        self._image_preview.show()

    def _clear_pending_image(self):
        import tempfile
        path = self._pending_image
        self._pending_image = None
        self._image_thumb.clear()
        self._image_name_label.clear()
        self._image_name_label.setToolTip("")
        self._image_preview.setToolTip("")
        self._image_preview.hide()
        if path:
            base = os.path.basename(path)
            if (path.startswith(tempfile.gettempdir())
                    and base.startswith("agent_paste")):
                try:
                    os.remove(path)
                except OSError:
                    pass
        try:
            self.input.setFocus()
        except Exception:
            pass

    # ── Send / receive ───────────────────────────────────────────────

    def _persist_attachment(self, path: str) -> str:
        """Copy a pending attachment into a stable dir so it survives the temp
        cleanup in _clear_pending_image() (which deletes clipboard pastes) and
        conversation reloads. User-chosen files are already stable → returned
        as-is."""
        if not path:
            return path
        try:
            import tempfile
            import uuid
            import shutil
            if not os.path.isfile(path):
                return path
            if not path.startswith(tempfile.gettempdir()):
                return path  # a real file on disk — persists on its own
            ATTACH_DIR.mkdir(parents=True, exist_ok=True)
            ext = os.path.splitext(os.path.basename(path))[1] or ".png"
            dst = ATTACH_DIR / f"attach_{uuid.uuid4().hex}{ext}"
            shutil.copy2(path, dst)
            return str(dst)
        except Exception:
            return path

    # ── Large-paste capture (collapsed paste cards) ───────────────────────
    _PASTE_MIN_CHARS = 1200      # below this it's small enough to show inline
    _PASTE_MIN_LINES = 18

    def _should_capture_paste(self, text: str) -> bool:
        if not text:
            return False
        return (len(text) >= self._PASTE_MIN_CHARS
                or text.count("\n") + 1 >= self._PASTE_MIN_LINES)

    def _capture_pasted_text(self, text: str) -> None:
        """Stash a big paste as a removable pill above the composer (keeping the
        composer clean). On send the full text is appended to the message for the
        model, while the bubble shows a collapsed card."""
        lines = text.count("\n") + 1
        entry = {"text": text, "lines": lines, "chars": len(text), "pill": None}
        pill = self._make_paste_pill(entry)
        entry["pill"] = pill
        # Insert before the trailing stretch so pills stay left-packed.
        self._paste_bar_layout.insertWidget(self._paste_bar_layout.count() - 1, pill)
        self._pending_pastes.append(entry)
        self._paste_bar.show()

    def _make_paste_pill(self, entry: dict) -> QFrame:
        p = PALETTE
        accent = QColor(p["accent"])
        pill = QFrame()
        pill.setObjectName("pastePill")
        pill.setStyleSheet(
            f"QFrame#pastePill {{ background: rgba({accent.red()},{accent.green()},{accent.blue()},0.12);"
            f" border: 1px solid {p['border']}; border-radius: 11px; }}")
        lay = QHBoxLayout(pill)
        lay.setContentsMargins(10, 2, 5, 2)
        lay.setSpacing(5)
        label = QLabel(f"Pasted text · {entry['lines']:,} lines")
        label.setFont(QFont("Consolas", 8))
        label.setStyleSheet(f"color: {p['accent']}; background: transparent; border: none;")
        label.setToolTip(f"{entry['lines']:,} lines · {entry['chars']:,} chars")
        lay.addWidget(label)
        x = QPushButton("✕")
        x.setFixedSize(16, 16)
        x.setCursor(Qt.CursorShape.PointingHandCursor)
        x.setFont(QFont("Consolas", 8))
        x.setToolTip("Remove pasted text")
        x.setStyleSheet(
            f"QPushButton {{ color: {p['muted_text']}; background: transparent; border: none; }}"
            f"QPushButton:hover {{ color: {p.get('accent_bright', p['accent'])}; }}")
        x.clicked.connect(lambda _=False, e=entry: self._remove_paste(e))
        lay.addWidget(x)
        return pill

    def _remove_paste(self, entry: dict) -> None:
        pill = entry.get("pill")
        if pill is not None:
            try:
                self._paste_bar_layout.removeWidget(pill)
                pill.deleteLater()
            except RuntimeError:
                pass
        if entry in self._pending_pastes:
            self._pending_pastes.remove(entry)
        if not self._pending_pastes:
            self._paste_bar.hide()

    def _clear_pending_pastes(self) -> None:
        for e in list(getattr(self, "_pending_pastes", [])):
            pill = e.get("pill")
            if pill is not None:
                try:
                    self._paste_bar_layout.removeWidget(pill)
                    pill.deleteLater()
                except RuntimeError:
                    pass
        self._pending_pastes = []
        if hasattr(self, "_paste_bar"):
            self._paste_bar.hide()

    def _extract_pastes(self, raw: str):
        """Build (full_for_model, typed_for_display, pastes) from the composer
        text + the pending paste pills. Pastes are appended after the typed text
        for the model; the bubble shows the typed text + collapsed cards."""
        pending = list(getattr(self, "_pending_pastes", None) or [])
        typed = raw.strip()
        pastes = [{"text": e["text"], "lines": e["lines"], "chars": e["chars"]}
                  for e in pending]
        if pastes:
            blocks = "\n\n".join(p["text"] for p in pastes)
            full = (typed + "\n\n" + blocks) if typed else blocks
        else:
            full = typed
        # NOTE: does not clear the pending pastes — the caller clears them only
        # once the message is actually committed (so a cancelled mid-job send
        # doesn't silently drop them).
        return full.strip(), typed, pastes

    def send_message(self):
        # Expand any captured paste placeholders: the model sees the full text
        # (`content`), the bubble shows the typed text + collapsed paste cards.
        raw_input = self.input.toPlainText()
        full_text, typed_text, sent_pastes = self._extract_pastes(raw_input)
        self._sending_pastes = sent_pastes
        self._sending_typed = typed_text
        text = full_text.strip()
        if not text and not self._pending_image:
            return
        # Mirroring a peer's conversation → the HOST runs the turn (its agent,
        # its tools). We just relay the message and render what streams back.
        if self._remote_mirror is not None:
            self._send_remote(text)
            return
        # Persist the attachment NOW (before any _clear_pending_image deletes the
        # clipboard temp) so the sent image stays visible in the chat.
        pending_img = self._persist_attachment(self._pending_image) if self._pending_image else None
        if self._thread is not None:
            # Mid-job: ask whether to INTERRUPT the agent now or QUEUE the
            # message to auto-send the moment the current turn finishes.
            # Either way the queue drain in _finish_inference delivers it —
            # interrupt just stops the turn first (Stop keeps whatever was
            # produced, then the message runs against that context).
            from ui.interrupt_queue_dialog import InterruptQueueDialog
            dlg = InterruptQueueDialog(text, parent=self)
            # Flag for _drain_queued_message: exec() spins an event loop, so
            # the running turn can finish (and try to drain) underneath us.
            self._midjob_dialog_open = True
            try:
                dlg.exec()
            finally:
                self._midjob_dialog_open = False
            choice = dlg.result_action()
            if choice == "cancel":
                return  # message stays in the composer untouched
            if self._thread is not None:
                # Merge with anything already queued so an earlier mid-job
                # submit isn't silently clobbered.
                prev = getattr(self, "_queued_message", None) or {}
                if prev.get("text") and text:
                    text = prev["text"] + "\n\n" + text
                self._queued_message = {
                    "text": text or prev.get("text", ""),
                    "image": pending_img or prev.get("image"),
                }
                self._clear_pending_image()
                self._clear_pending_pastes()  # pastes are folded into queued text
                self.input.clear()
                try:
                    self.input.setPlaceholderText(
                        "Interrupting — your message sends next…"
                        if choice == "interrupt" else
                        "Queued — sends when the current reply finishes…")
                except Exception:
                    pass
                if choice == "interrupt":
                    self._stop_inference()
                return
            # else: the turn finished while the dialog was open — fall
            # through and send normally.

        # Interrupt any ongoing TTS playback — user is speaking now
        self._interrupt_voice()

        # Model is now set via ConversationDialog — no sync needed here

        # Determine what to send and display
        send_text = text or ("What's in this image?" if pending_img else "")
        if not send_text and not pending_img:
            return  # double guard

        display_text = text or "What's in this image?"

        # Capture the summarizer state BEFORE this turn runs so undo can
        # later revert it. Stamp the snapshot on the user meta entry (which
        # is what gets persisted), so it survives save/load.
        try:
            pre_turn_summary_snapshot = self.agent.get_current_summary_snapshot()
        except Exception as e:
            print(f"[send_message] summary snapshot failed: {e}")
            pre_turn_summary_snapshot = {}

        # If last message is already this exact user text (e.g. after undo), don't duplicate —
        # just resubmit to the LLM
        last_meta = self._message_meta[-1] if self._message_meta else {}
        if last_meta.get("role") == "user" and last_meta.get("content", "").strip() == display_text.strip():
            if pre_turn_summary_snapshot:
                last_meta["_summary_snapshot"] = pre_turn_summary_snapshot
        else:
            self._add_message("You", display_text, image_path=pending_img,
                              typed=getattr(self, "_sending_typed", ""),
                              pastes=getattr(self, "_sending_pastes", None))
            # Bake a persistent thumbnail now so the bubble keeps showing the
            # image even after the source temp is cleaned up.
            if self._message_meta and pending_img:
                try:
                    _ensure_thumb(self._message_meta[-1])
                except Exception:
                    pass
            if self._message_meta and pre_turn_summary_snapshot:
                self._message_meta[-1]["_summary_snapshot"] = pre_turn_summary_snapshot

        # Mirror this user turn to any peer watching this conversation.
        self._publish_host_turn_event(
            "user", message={"role": "user", "content": display_text})

        try:
            from core.sounds import play_ui
            play_ui("message.mp3")
        except Exception:
            pass

        self.input.clear()
        self._clear_pending_pastes()  # message committed — drop the pending pills
        self._set_inferring(True)
        self._show_thinking()
        self._stream_committed_text = ""
        self._stream_did_split = False  # reset per turn; set when a diff card splits the bubble

        self._thread = InferenceThread(
            self.agent, send_text,
            image_path=pending_img)
        self._clear_pending_image()
        self.agent._tool_callback = lambda n, a: self.tool_activity.emit(n, a)
        self.agent._tool_batch_callback = lambda ns: self.tool_batch.emit(ns)
        self._thread.finished.connect(self._on_response)
        self._thread.errored.connect(self._on_error)
        self._thread.stopped.connect(self._on_stopped)
        self._thread.chunk.connect(self._on_stream_chunk)
        self._thread.round_started.connect(self._on_stream_round_start)
        self._thread.start()
        self._arm_inference_watchdog()

    # ── Inference watchdog ────────────────────────────────────────────
    # Catches the "agent just stopped" failure: the QThread ended but NO
    # signal (finished/errored/stopped) ever reached the UI — e.g. a C-level
    # crash inside a provider lib, or a signal lost during conv switching.
    # Without this the UI stays in limbo: no typing indicator, no error, no
    # interrupt dialog — and the user has to prompt "continue" blind.

    def _arm_inference_watchdog(self):
        if getattr(self, "_inference_watchdog", None) is None:
            t = QTimer(self)
            t.setInterval(3000)
            t.timeout.connect(self._check_inference_alive)
            self._inference_watchdog = t
        self._watchdog_dead_ticks = 0
        self._inference_watchdog.start()

    def _check_inference_alive(self):
        thread = getattr(self, "_thread", None)
        if thread is None or not self._inferring:
            self._inference_watchdog.stop()
            return
        if thread.isRunning():
            self._watchdog_dead_ticks = 0
            return
        # Thread ended. Queued signals normally land within one event-loop
        # pass; give it 2 ticks (6s) of grace before declaring it dead.
        self._watchdog_dead_ticks += 1
        if self._watchdog_dead_ticks < 2:
            return
        self._inference_watchdog.stop()
        self._hide_thinking()
        self._add_message(
            AGENT_LABEL,
            "⚠ The worker thread ended without reporting a result (likely a "
            "crash inside a provider/tool library). The turn was recovered — "
            "context is intact; say `continue` to resume.")
        self._finish_inference()

    def _on_tool_batch(self, names: list):
        """Parallel read-only batch — reveal chips on one row, staggered in order."""
        skip = {"plan", "subagent", "screenshot"}
        self._parallel_tool_pending = [n for n in names if n and n not in skip]
        if not self._parallel_tool_pending:
            return
        self._parallel_tool_step()

    def _parallel_tool_step(self):
        if not self._parallel_tool_pending:
            return
        name = self._parallel_tool_pending.pop(0)
        self._apply_tool_chip_and_sound(name, {})
        if self._parallel_tool_pending:
            QTimer.singleShot(self._PARALLEL_TOOL_UI_MS, self._parallel_tool_step)

    def _apply_tool_chip_and_sound(self, name: str, args: dict):
        """One chip on the shared tool row + optional terminal sound."""
        verb = self._verb_for_tool(name, args)
        # Mark a tool as in-flight so the status stays visible even mid-stream;
        # cleared the moment text resumes (_on_stream_chunk) or the turn ends.
        self._active_tool_verb = verb
        self._set_typing_prefix(f"{AGENT_LABEL} is {verb}")
        # Mirror tool activity to any peer watching this conversation.
        self._publish_host_turn_event("tool", name=name)
        # ask_user_question: don't drop a chip while the question board is still
        # open — it's redundant next to the live widget. The chip is added later,
        # once answered, by _on_question_answered.
        if name != "ask_user_question":
            self._note_live_stream_tool(name)
        try:
            from core.sounds import play_ui_random
            if name in self._TERMINAL_SOUND_TOOLS:
                play_ui_random([f"terminal{i}.mp3" for i in range(1, 7)])
        except Exception:
            pass

    def _on_tool_activity(self, name: str, args: dict):
        """Sequential tool exec — chip + sound as each tool starts."""
        self._apply_tool_called_ui(name, args)

    def _apply_tool_called_ui(self, name: str, args: dict):
        """Full per-tool UI (chips, sounds, plan/subagent gadgets)."""
        if name in ("plan", "subagent", "screenshot"):
            pass  # handled below; don't double-add generic chip
        else:
            self._apply_tool_chip_and_sound(name, args)
        if name == "plan":
            action = args.get("action", "")
            if action == "create":
                if self._live_plan_widget:
                    self._live_plan_widget.stop_polling()
                    self._live_plan_widget = None
                if self._stream_in_chat():
                    self._ensure_live_stream_message()
                    meta = self._live_stream_meta()
                    if meta is not None:
                        self._seal_stream_text_to_timeline(meta)
                        tl = meta.setdefault("_stream_timeline", [])
                        tl.append({"type": "plan", "live": True, "plan_data": {}})
                        self._live_plan_timeline_ref = (
                            self._stream_live_meta_idx, len(tl) - 1)
                        self._refresh_live_stream_display(show_ellipsis=True)
                    QTimer.singleShot(50, self._scroll_to_bottom)
                    return
                self._live_plan_meta_idx = len(self._message_meta)
                self._message_meta.append({
                    "role": "plan_card",
                    "content": "",
                    "tool_names": [],
                    "image_path": "",
                    "_html": "",
                    "_plan_data": {},
                    "_plan_live": True,
                })
                self._clear_message_widgets()
                self._recalc_and_sync(immediate=True)
                pw = self._idx_to_widget.get(self._live_plan_meta_idx)
                if pw and isinstance(pw, PlanWidget):
                    pw.start_polling()
                    self._live_plan_widget = pw
                QTimer.singleShot(50, self._scroll_to_bottom)
            elif action in ("update", "add_step", "remove_step"):
                # A step changed. Two jobs: (1) flip the existing card's status
                # live, and (2) if real work (tool rows / narration) has piled up
                # beneath the checklist since it was last shown, re-surface a
                # fresh copy next to that new work — so the user watches it tick
                # off over time (the Claude/Cursor behaviour) instead of it
                # scrolling out of view and freezing near the top.
                if (getattr(self, "_live_plan_timeline_ref", None)
                        and self._stream_in_chat()):
                    QTimer.singleShot(200, lambda: self._refresh_live_stream_display(
                        show_ellipsis=self._stream_active))
                    # Coalesce a burst of rapid step flips into one re-surface.
                    self._schedule_plan_resurface()
            elif action == "finish":
                from tools.plan import get_current_plan, get_last_finished_plan
                # Cancel any pending re-surface so it can't fire after close.
                _rt = getattr(self, "_plan_resurface_timer", None)
                if _rt is not None:
                    _rt.stop()
                # The tool may have already executed (queued cross-thread
                # signal) and cleared the live plan — fall back to the
                # finished snapshot so the persisted card keeps its steps.
                plan_data = get_current_plan() or get_last_finished_plan()
                plan_ref = getattr(self, "_live_plan_timeline_ref", None)
                if plan_ref and self._stream_in_chat():
                    meta_idx, tl_idx = plan_ref
                    if meta_idx < len(self._message_meta):
                        tl = self._message_meta[meta_idx].get("_stream_timeline", [])
                        if tl_idx < len(tl):
                            tl[tl_idx]["plan_data"] = plan_data or {}
                            tl[tl_idx]["live"] = False
                    self._live_plan_timeline_ref = None
                    self._refresh_live_stream_display(show_ellipsis=self._stream_active)
                    self._auto_save()
                    return
                if self._live_plan_widget:
                    self._live_plan_widget.stop_polling()
                    if plan_data:
                        self._live_plan_widget.set_final_state(plan_data)
                idx = getattr(self, '_live_plan_meta_idx', None)
                if idx is not None and idx < len(self._message_meta):
                    self._message_meta[idx]["_plan_data"] = plan_data or {}
                    self._message_meta[idx]["_plan_live"] = False
                self._live_plan_widget = None
                self._auto_save()

        # Terminal — foreground commands run silently and return to the agent.
        # Background commands get their own dedicated tab via the bg-tab bridge
        # (tools/terminal.py → workspace_terminal.bg_bridge), so they're handled
        # entirely on the bridge side; no UI mirroring happens here.
        # No-op for terminal calls — kept here as a placeholder so the typing
        # indicator still updates ("Agent is running a terminal command") via
        # the verb-mapping above.
        pass

        # Screenshot preview card
        if name == "screenshot":
            import tempfile
            tmp = os.path.join(tempfile.gettempdir(), "agent_screenshot.jpg")

            def _show_screenshot_card():
                if os.path.isfile(tmp):
                    import time as _time
                    # Mirror the screenshot to any peer watching this
                    # conversation, so a remote operator sees this machine's screen.
                    try:
                        from core.network import network_manager
                        cid = self._current_conv_id
                        if (not self._is_remote_id(cid)
                                and network_manager.conv_has_subscribers(cid)):
                            import base64 as _b64
                            raw = open(tmp, "rb").read()
                            if 0 < len(raw) <= 8 * 1024 * 1024:
                                network_manager.publish_conv_event(cid, {
                                    "kind": "image", "fmt": "jpeg",
                                    "data": _b64.b64encode(raw).decode()})
                    except Exception:
                        pass
                    shot_meta = {
                        "type": "screenshot",
                        "image_path": tmp,
                    }
                    if self._stream_in_chat():
                        self._ensure_live_stream_message()
                        meta = self._live_stream_meta()
                        if meta is not None:
                            self._seal_stream_text_to_timeline(meta)
                            meta.setdefault("_stream_timeline", []).append(shot_meta)
                            self._refresh_live_stream_display(show_ellipsis=True)
                    else:
                        import time as _time
                        self._message_meta.append({
                            "role": "assistant", "content": "Screenshot captured.",
                            "tool_names": ["screenshot"],
                            "image_path": tmp,
                            "_html": "<em>Screenshot captured.</em>",
                            "_timestamp": _time.time(),
                        })
                        self._recalc_and_sync()
                    QTimer.singleShot(50, self._scroll_to_bottom)

            # Delay slightly — the file is saved on the inference thread
            QTimer.singleShot(500, _show_screenshot_card)

        # Browser — mirror navigate actions in the workspace browser panel
        if name == "browser" or name.startswith("browser_"):
            action = args.get("action", name.replace("browser_", ""))
            if action == "navigate":
                url = args.get("url", "")
                if url:
                    self._route_browser_to_workspace(url)

    # ── Sub-agent UI handlers ──────────────────────────────────────────

    def _timeline_has_subagent(self, job_id: str) -> bool:
        idx = self._find_live_stream_idx()
        if idx is not None:
            for item in self._message_meta[idx].get("_stream_timeline", []):
                if item.get("type") == "subagent" and item.get("job_id") == job_id:
                    return True
        return any(m.get("_job_id") == job_id for m in self._message_meta)

    def _on_subagent_job_started(self, job_id: str, tasks_json: str):
        """Insert a live sub-agent card into the chat."""
        import time as _time
        if self._timeline_has_subagent(job_id):
            return
        try:
            tasks = json.loads(tasks_json)
        except (json.JSONDecodeError, TypeError):
            tasks = []
        if self._stream_in_chat():
            self._ensure_live_stream_message()
            meta = self._live_stream_meta()
            if meta is not None:
                self._seal_stream_text_to_timeline(meta)
                meta.setdefault("_stream_timeline", []).append({
                    "type": "subagent",
                    "job_id": job_id,
                    "tasks": tasks,
                    "live": True,
                    "summary": {},
                })
                self._refresh_live_stream_display(show_ellipsis=True)
            QTimer.singleShot(50, self._scroll_to_bottom)
            return
        self._message_meta.append({
            "role": "subagent_card",
            "content": "",
            "tool_names": ["subagent"],
            "image_path": "",
            "_html": "",
            "_job_id": job_id,
            "_tasks": tasks,
            "_subagent_live": True,
            "_subagent_summary": {},
            "_timestamp": _time.time(),
        })
        self._clear_message_widgets()
        self._recalc_and_sync(immediate=True)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _on_subagent_task_updated(self, task_id: str, status: str, data_json: str):
        """Update the live sub-agent card for a specific task."""
        try:
            data = json.loads(data_json)
        except (json.JSONDecodeError, TypeError):
            data = {}
        stream_idx = self._find_live_stream_idx()
        if stream_idx is not None:
            meta = self._message_meta[stream_idx]
            for item in meta.get("_stream_timeline", []):
                if item.get("type") != "subagent":
                    continue
                for t in item.get("tasks", []):
                    if t.get("task_id") == task_id:
                        t["status"] = status
                        if data:
                            t.update({k: v for k, v in data.items()
                                      if k not in ("task_id", "status")})
                        self._refresh_live_stream_display(show_ellipsis=self._stream_active)
                        return
        for idx, meta in enumerate(self._message_meta):
            if meta.get("role") != "subagent_card" or not meta.get("_subagent_live"):
                continue
            tasks = meta.get("_tasks", [])
            for t in tasks:
                if t.get("task_id") == task_id:
                    t["status"] = status
                    # Update the widget if visible
                    widget = _subagent_card_resolve(self._idx_to_widget.get(idx))
                    if widget:
                        widget.update_task(task_id, status, data)
                    return

    def _on_subagent_terminal(self, task_id: str, command: str, cwd: str):
        """A sub-agent needs a terminal — route output without forcing splitter open."""
        sizes = self._chat_hsplitter.sizes()
        was_collapsed = len(sizes) >= 2 and sizes[1] < 20

        if was_collapsed:
            self._blink_splitter_handle_fast()

        # Sub-agent terminals belong to the conversation that spawned them —
        # land them as a tab inside the current conv's panel, not a brand new
        # per-conv panel.
        multi = self._right_workspace.terminal_panel
        sub_panel = multi.active_panel() or multi.get_or_create_panel(
            self._current_conv_id or "_default")
        sa_conv_id = f"sa-{task_id}"
        short_name = f"[SA] {task_id[-8:]}"
        session = sub_panel.get_or_create_for_conv(sa_conv_id, short_name)
        session.append_agent_command(command)

        if not was_collapsed:
            self._right_workspace.set_workspace_page(4)
            sub_panel.switch_to_conv(sa_conv_id)
            self._blink_terminal_tab(sub_panel, sa_conv_id)

    def _on_subagent_job_completed(self, job_id: str, summary_json: str):
        """Job finished — finalize the card, clean up terminal tabs."""
        try:
            summary = json.loads(summary_json)
        except (json.JSONDecodeError, TypeError):
            summary = {}
        stream_idx = self._find_live_stream_idx()
        if stream_idx is not None:
            meta = self._message_meta[stream_idx]
            for item in meta.get("_stream_timeline", []):
                if item.get("type") == "subagent" and item.get("job_id") == job_id:
                    item["live"] = False
                    item["summary"] = summary
                    for td in summary.get("tasks", []):
                        for t in item.get("tasks", []):
                            if t.get("task_id") == td.get("task_id"):
                                t["status"] = td.get("status", "completed")
                    self._refresh_live_stream_display(show_ellipsis=self._stream_active)
                    self._auto_save()
                    QTimer.singleShot(3000, lambda s=summary: self._cleanup_subagent_tabs(s))
                    return
        for meta in self._message_meta:
            if meta.get("_job_id") == job_id:
                meta["_subagent_live"] = False
                meta["_subagent_summary"] = summary
                # Also update individual task statuses in the card widget
                for idx, m in enumerate(self._message_meta):
                    if m.get("_job_id") == job_id:
                        widget = _subagent_card_resolve(self._idx_to_widget.get(idx))
                        if widget:
                            for td in summary.get("tasks", []):
                                widget.update_task(
                                    td.get("task_id", ""),
                                    td.get("status", "completed"),
                                    td,
                                )
                            widget._finalize(summary)
                        break
                break
        self._auto_save()

        QTimer.singleShot(3000, lambda s=summary: self._cleanup_subagent_tabs(s))

    def _cleanup_subagent_tabs(self, summary: dict):
        panel = self._right_workspace.terminal_panel
        for task_data in summary.get("tasks", []):
            sa_conv_id = f"sa-{task_data.get('task_id', '')}"
            try:
                panel.close_conv(sa_conv_id)
            except Exception:
                pass

    def _on_chart_ready(self, path: str, title: str, chart_type: str):
        """Chart tool finished — inject inline or as a chart card."""
        import time as _time
        if self._stream_in_chat():
            self._ensure_live_stream_message()
            meta = self._live_stream_meta()
            if meta is not None:
                self._seal_stream_text_to_timeline(meta)
                meta.setdefault("_stream_timeline", []).append({
                    "type": "chart",
                    "title": title,
                    "chart_type": chart_type,
                    "path": path,
                })
                self._refresh_live_stream_display(show_ellipsis=self._stream_active)
                QTimer.singleShot(50, self._scroll_to_bottom)
                self._auto_save()
                return
        self._message_meta.append({
            "role":         "chart_card",
            "content":      f"[chart: {title or chart_type}]",
            "_chart_path":  path,
            "_chart_title": title,
            "_chart_type":  chart_type,
            "_timestamp":   _time.time(),
        })
        self._recalc_and_sync()
        QTimer.singleShot(50, self._scroll_to_bottom)
        self._auto_save()

    def _blink_terminal_tab(self, panel, conv_id: str):
        """Blink a terminal tab's text color to draw attention."""
        p = PALETTE
        session = panel._conv_sessions.get(conv_id)
        if not session:
            return
        try:
            idx = panel._sessions.index(session)
        except (ValueError, AttributeError):
            return
        tab_widget = panel._tab_widget
        original_color = tab_widget.tabBar().tabTextColor(idx)
        accent = QColor(p["accent"])
        count = [0]

        def _tick():
            count[0] += 1
            if count[0] >= 6:
                tab_widget.tabBar().setTabTextColor(idx, original_color)
                return
            if count[0] % 2 == 1:
                tab_widget.tabBar().setTabTextColor(idx, accent)
            else:
                tab_widget.tabBar().setTabTextColor(idx, original_color)
            QTimer.singleShot(200, _tick)

        _tick()

    def _blink_splitter_handle_fast(self):
        """Rapid 3-blink attention flash for the splitter handle."""
        p = PALETTE
        bright_ss = self._splitter_attention_ss(p)
        normal_ss = self._splitter_idle_ss(p)
        splitter = self._chat_hsplitter
        count = [0]

        def _tick():
            count[0] += 1
            if count[0] >= 6:  # 3 blinks (on-off-on-off-on-off)
                splitter.setStyleSheet(normal_ss)
                return
            if count[0] % 2 == 1:
                splitter.setStyleSheet(bright_ss)
            else:
                splitter.setStyleSheet(normal_ss)
            QTimer.singleShot(150, _tick)  # Fast: 150ms per phase

        _tick()

    def _on_response(self, reply: str, tool_log: list, reply_html: str = ""):
        self._hide_thinking()

        if not reply and not tool_log:
            self._abort_live_stream()
            self._end_stream()
            self._finish_inference()
            return
        tool_names = [t["tool"] for t in tool_log
                      if t.get("success") is not False] if tool_log else []
        display = reply or "(Agent returned empty response)"

        # Mirror the finished reply to any peer watching this conversation.
        self._publish_host_turn_event(
            "final", message={"role": "assistant", "content": display,
                              "tool_names": tool_names})

        extra_meta = {}
        thinking = getattr(self.agent, '_turn_thinking', None)
        if thinking:
            extra_meta["_thinking"] = thinking[:2000]
        usage = getattr(self.agent, '_turn_usage', None)
        if usage and usage.get("prompt_tokens", 0) > 0:
            extra_meta["_usage"] = dict(usage)

        handled, finalized_idx = self._finalize_stream_response(
            display, tool_names, reply_html, extra_meta)

        if not handled:
            meta_len_before = len(self._message_meta)
            try:
                self._add_message(
                    AGENT_LABEL, display, tool_names=tool_names,
                    precomputed_assistant_html=reply_html,
                )
            except Exception as e:
                print(f"[ChatWidget] _add_message failed ({type(e).__name__}): {e}. "
                      f"Falling back to plain append so the reply is not lost.")
                import time as _time
                self._message_meta.append({
                    "role": "assistant", "content": display,
                    "tool_names": tool_names or [], "image_path": "",
                    "_html": f"<pre>{display}</pre>",
                    "_timestamp": _time.time(),
                })
                self._recalc_and_sync()
            if len(self._message_meta) == meta_len_before and display:
                print(f"[ChatWidget] WARNING: reply of {len(display)} chars was not "
                      f"appended to _message_meta. tool_log={len(tool_log)} entries.")
            if self._message_meta and extra_meta:
                self._message_meta[-1].update(extra_meta)
                self._clear_message_widgets()
                self._recalc_and_sync(immediate=True)
        elif extra_meta.get("_usage") or extra_meta.get("_thinking"):
            # Ensure usage/thinking footers render after finalize's in-place update.
            if finalized_idx is not None and finalized_idx < len(self._message_meta):
                widget = self._idx_to_widget.get(finalized_idx)
                if isinstance(widget, ChatMessageWidget):
                    meta = self._message_meta[finalized_idx]
                    widget.update_content(
                        meta.get("content", display),
                        meta.get("_html", reply_html),
                        tool_names=meta.get("tool_names", []),
                        usage=meta.get("_usage"),
                    )

        self._end_stream()

        # Attach checkpoint info to this turn's meta (if any file mutations happened)
        from core.checkpoints import checkpoint_manager
        cp_idx = finalized_idx if finalized_idx is not None else (
            len(self._message_meta) - 1 if self._message_meta else None)
        if checkpoint_manager._last_hash and cp_idx is not None and cp_idx < len(self._message_meta):
            self._message_meta[cp_idx]["_checkpoint_hash"] = checkpoint_manager._last_hash
            self._message_meta[cp_idx]["_checkpoint_dir"] = checkpoint_manager._last_dir

        # Sync controls in case agent switched workspace via tool
        self._set_ws_combo_to(self.agent._workspace_name)
        self._refresh_ws_combo()
        self._sync_file_explorer_root()
        self._update_conv_summary_label()
        # Check if name changed (first-message auto-naming). Metadata-only reads
        # — loading + parsing every message twice here stutters every turn.
        old_data = get_conversation_meta(self._current_conv_id)
        old_name = old_data["name"] if old_data else ""
        self._auto_save()
        new_data = get_conversation_meta(self._current_conv_id)
        new_name = new_data["name"] if new_data else ""
        if old_name != new_name:
            self._refresh_conv_bar()  # name changed, rebuild bricks

        # Play response sound + any deferred sounds queued by play_sound tool
        try:
            from core.sounds import play_ui
            play_ui("message.mp3")
        except Exception:
            pass
        from core.sounds import drain_deferred
        drain_deferred()

        # Auto-TTS: speak the response if enabled in settings
        if reply and reply.strip():
            cfg = self.agent.config
            if cfg.get("tts_autoplay", False):
                self._speak_response(reply)

        self._finish_inference()

    def _on_error(self, error: str):
        self._hide_thinking()
        self._abort_live_stream()
        self._end_stream()

        # Audible cue for the kick-back so the user notices even if looking elsewhere.
        try:
            from core.sounds import play_ui
            play_ui("error.mp3")
        except Exception:
            pass

        # Roll back user message from display
        last_user_text = ""
        if self._message_meta and self._message_meta[-1].get("role") == "user":
            last_user_text = self._message_meta[-1].get("content", "")
            self._message_meta.pop()

        # Roll back from agent context
        if self.agent.context and self.agent.context[-1]["role"] == "user":
            self.agent.context.pop()

        # Restore message to input box
        if last_user_text:
            self.input.setPlainText(last_user_text)

        # Show error, rebuild display
        self._add_message("Error", error)
        self._clear_message_widgets()
        self._recalc_and_sync(immediate=True)
        self._auto_save()
        self._finish_inference()

    @staticmethod
    def _wait_qthread(thread: QThread | None, wait_ms: int = 2500) -> None:
        """Block until a worker QThread exits so Qt can tear down thread-local storage.

        Destroying a QThread (or letting it GC) while ``run()`` is still active
        prints the ``QThreadStorage: entry N destroyed before end of thread`` spam
        on stderr at process exit.
        """
        if thread is None:
            return
        try:
            if thread.isRunning():
                if not thread.wait(max(0, int(wait_ms))):
                    thread.terminate()
                    thread.wait(500)
            else:
                thread.wait(100)
        except RuntimeError:
            pass

    def _shutdown_workers(self, wait_ms: int = 2500) -> None:
        """Join every chat-owned QThread before the GUI is torn down."""
        self._shutting_down = True
        try:
            self.agent._stop_requested = True
        except Exception:
            pass
        try:
            from core.tool_context import trigger_abort
            trigger_abort()
        except Exception:
            pass
        for name in (
            "_auto_save_timer", "_sync_debounce_timer", "_stream_flush_timer",
            "_composer_draft_timer", "_viewer_state_save_timer",
            "_inference_watchdog", "_input_blink_timer",
        ):
            timer = getattr(self, name, None)
            if timer is not None:
                try:
                    timer.stop()
                except RuntimeError:
                    pass
        try:
            self._interrupt_voice()
        except Exception:
            pass

        threads: list[QThread] = []
        if self._thread is not None:
            threads.append(self._thread)
        if self._conv_load_thread is not None:
            threads.append(self._conv_load_thread)
        for snap in list(getattr(self, "_conv_threads", {}).values()):
            bg = snap.get("thread")
            if bg is not None:
                threads.append(bg)
        threads.extend(list(getattr(self, "_task_threads", [])))

        seen: set[int] = set()
        for th in threads:
            if th is None or id(th) in seen:
                continue
            seen.add(id(th))
            try:
                th.finished.disconnect()
            except Exception:
                pass
            self._wait_qthread(th, wait_ms)

        self._thread = None
        self._conv_load_thread = None
        self._conv_threads.clear()
        if hasattr(self, "_task_threads"):
            self._task_threads.clear()

    def _stop_inference(self):
        """User hit STOP — signal agent to abort. Force-kill if it doesn't exit."""
        if self._thread is None:
            return
        self.agent._stop_requested = True
        # Also signal tool-level abort so long-running tools exit immediately
        try:
            from core.tool_context import trigger_abort
            trigger_abort()
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
            _orchestrators.clear()
        except Exception:
            pass
        # Stop the active terminal pipe timer
        if self._active_pipe_timer is not None:
            try:
                self._active_pipe_timer.stop()
            except RuntimeError:
                pass
            self._active_pipe_timer = None
        # Give it 2s to exit gracefully, then force-terminate
        QTimer.singleShot(2000, self._force_stop)

    def _force_stop(self):
        """Force-kill the thread if it's still stuck (e.g. blocked on API call)."""
        if self._thread is None:
            return
        if self._thread.isRunning():
            self._thread.terminate()
            self._thread.wait(1000)
            self._on_stopped()

    def _on_stopped(self):
        """User hit STOP — HALT the turn but KEEP everything produced so far so
        they can add new information. Rolling the turn back is the UNDO button's
        job, NOT Stop's.

        Two technicalities are handled so the kept turn stays coherent:
          * the partial streamed reply is SEALED into the conversation (instead
            of dropped), so the work stays visible; and
          * a stop can land right after the model requested tools but before
            their results were recorded — those dangling tool_calls are filled
            with a synthetic 'interrupted' result so the NEXT turn's API call is
            still valid.
        """
        self._hide_thinking()
        # Seal (don't drop) the in-progress streamed reply, then clear timers.
        self._seal_interrupted_stream()
        self._end_stream()
        # Keep agent.context well-formed for the next turn.
        self._repair_interrupted_context()

        # KEEP the user message and all produced content — no pop, and no
        # restoring text to the input box (that was the rollback behaviour).
        self._clear_message_widgets()
        self._recalc_and_sync(immediate=True)
        self._auto_save()
        self._finish_inference()

    def _seal_interrupted_stream(self):
        """Finalize the in-progress streaming bubble in place (rather than
        dropping it like _abort_live_stream) so a stopped turn keeps its work."""
        partial = ""
        try:
            partial = self._compose_live_stream_text(show_ellipsis=False).strip()
        except Exception:
            partial = ""
        handled = False
        if self._stream_in_chat():
            idx = self._stream_live_meta_idx
            if idx is None:
                idx = self._find_live_stream_idx()
            if idx is not None:
                try:
                    handled = bool(self._finalize_stream_response(partial, [], "", {})[0])
                except Exception:
                    handled = False
        if not handled:
            # Side-preview mode (or nothing streaming in chat): just clear the
            # buffers; the preview already showed any partial text.
            self._stream_buffer = []
            self._stream_committed_text = ""

    def _repair_interrupted_context(self):
        """Keep agent.context valid after a stop. If the turn was interrupted
        right after the model asked for tools, the last assistant message has
        tool_calls whose results were never recorded — an invalid state for the
        next API call. Append a synthetic result for each unanswered tool_call."""
        ctx = self.agent.context
        if not ctx:
            return
        # Find the last assistant message bearing tool_calls within this turn.
        last_asst = None
        for i in range(len(ctx) - 1, -1, -1):
            role = ctx[i].get("role")
            if role == "assistant" and ctx[i].get("tool_calls"):
                last_asst = i
                break
            if role == "user":
                return  # turn boundary, no open tool round
        if last_asst is None:
            return
        answered = {
            m.get("tool_call_id")
            for m in ctx[last_asst + 1:]
            if m.get("role") == "tool"
        }
        import json as _json
        for tc in (ctx[last_asst].get("tool_calls") or []):
            tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tcid and tcid not in answered:
                ctx.append({
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": _json.dumps(
                        {"stopped": "Interrupted by the user before this tool ran."}),
                })

    def _send_attach_dim_stylesheet(self) -> str:
        """Dimmed send/attach while inference is running (inline — parent QSS can't override)."""
        p = PALETTE
        return (
            f"color: {p['border']}; background: {p['panel']}; "
            f"border: 1px solid {p['border']};"
        )

    def _polish_send_attach_buttons(self):
        """Drop stale inline styles so parent #sendBtn / #attachBtn rules apply again."""
        for btn in (self.send_btn, self.attach_btn):
            btn.setStyleSheet("")
            style = btn.style()
            style.unpolish(btn)
            style.polish(btn)
            btn.update()

    def _apply_send_attach_button_styles(self):
        """Sync send/attach colors with the current palette (theme change + inferring)."""
        if self._inferring:
            dim = self._send_attach_dim_stylesheet()
            self.send_btn.setStyleSheet(dim)
            self.attach_btn.setStyleSheet(dim)
        else:
            self._polish_send_attach_buttons()

    def _set_inferring(self, active: bool):
        """Visually dim send/attach during inference. Input stays usable but muted."""
        self._inferring = active
        # Only animate "..." ellipses while generating — keeps the UI thread idle
        # (and typing smooth) the rest of the time.
        ChatMessageWidget.set_ellipsis_active(active)
        p = PALETTE
        if active:
            # Keep the composer usable during inference so the user can queue a
            # follow-up (it auto-sends when this turn ends). Only Attach is
            # disabled — no mid-job attachments.
            dim = self._send_attach_dim_stylesheet()
            self.attach_btn.setStyleSheet(dim)
            self.attach_btn.setEnabled(False)
            self.send_btn.setEnabled(True)
            self.input._apply_styles()
        else:
            self.send_btn.setEnabled(True)
            self.attach_btn.setEnabled(True)
            self._polish_send_attach_buttons()
            self.input._apply_styles()

    def _finish_inference(self):
        if self._thread is not None:
            self._wait_qthread(self._thread, 100)
        self._thread = None
        # Clear any leftover interrupt state so a force-terminated turn can't
        # make the NEXT turn (incl. a queued message) stop the instant it starts.
        try:
            self.agent._stop_requested = False
            from core.tool_context import reset_global_abort
            reset_global_abort()
        except Exception:
            pass
        self._parallel_tool_pending.clear()
        self._end_stream()  # belt-and-suspenders: clear any leftover stream state
        self._set_inferring(False)
        threading.Thread(
            target=self.agent.summarizer.save_state,
            daemon=True,
            name="summarizer-save",
        ).start()
        self._maybe_focus_input()
        # Send anything the user queued while this turn was running.
        self._drain_queued_message()
        # A theme change during the turn deferred its transcript rebuild (it would
        # have collided with the live stream). Run it now — but only if draining
        # didn't immediately kick off another turn; if it did, the flag stays set
        # and the next _finish_inference handles it.
        if (getattr(self, "_theme_rebuild_deferred", False)
                and not getattr(self, "_theme_applying", False)
                and self._thread is None):
            try:
                self._rebuild_transcript_for_theme()
            except Exception:
                self._theme_rebuild_deferred = False

    def _drain_queued_message(self):
        """Auto-send a message the user submitted while a turn was in progress."""
        if getattr(self, "_midjob_dialog_open", False):
            # The interrupt/queue dialog is open and its exec() loop is
            # processing this turn's finish — let it resolve first, then
            # re-check (its outcome may queue a message or start a new turn).
            QTimer.singleShot(100, self._drain_queued_message)
            return
        q = getattr(self, "_queued_message", None)
        if not q:
            return
        if self._thread is not None:
            # A new turn already started (e.g. the dialog closed into a
            # normal send) — keep the message queued for the next finish.
            return
        self._queued_message = None
        try:
            self.input.setPlaceholderText("")
        except Exception:
            pass
        # If the user typed something new after queuing, that newer text wins —
        # don't clobber it. Otherwise restore the queued message + image.
        if not self.input.toPlainText().strip():
            if q.get("image"):
                try:
                    self._show_pending_image(q["image"], "Queued image")
                except Exception:
                    pass
            self.input.setPlainText(q.get("text", "") or "")
        # Defer so the just-finished turn fully tears down before the next starts.
        QTimer.singleShot(0, self.send_message)

    # ── Cron ticker ──────────────────────────────────────────────────

    # ── Task scheduler ────────────────────────────────────────────────

    def _resolve_task_conv(self, task: dict) -> str:
        """Resolve which conversation a task should deliver to.

        For conversation-targeted tasks: use the stored conversation_id.
        For stream-targeted tasks: find a conversation subscribed to that stream,
        preferring the currently active conversation.
        Falls back to creating a new conversation if nothing matches.
        """
        from core.conversations import (
            list_conversations, load_conversation,
            new_conversation_id, save_conversation,
        )
        deliver_type = task.get("deliver_to_type", "conversation")

        if deliver_type == "stream":
            stream_name = task.get("deliver_to_stream", "")
            if stream_name:
                # Prefer current conversation if it has this stream
                if self._current_conv_id:
                    data = load_conversation(self._current_conv_id)
                    if data:
                        conv_streams = data.get("streams", [])
                        names = [s if isinstance(s, str) else s.get("name", "")
                                 for s in conv_streams]
                        if stream_name in names:
                            return self._current_conv_id

                # Search all conversations for one with this stream
                for conv in list_conversations():
                    data = load_conversation(conv["id"])
                    if not data:
                        continue
                    conv_streams = data.get("streams", [])
                    names = [s if isinstance(s, str) else s.get("name", "")
                             for s in conv_streams]
                    if stream_name in names:
                        return conv["id"]

            # No matching conversation — create one with the stream
            conv_id = new_conversation_id()
            save_conversation(conv_id, f"Task: {task.get('name', 'Task')}", [],
                              streams=[stream_name] if stream_name else [])
            self._refresh_conv_bar()
            return conv_id

        # Conversation-targeted (default)
        conv_id = task.get("conversation_id", "")
        if not conv_id:
            conv_id = new_conversation_id()
            save_conversation(conv_id, f"Task: {task.get('name', 'Task')}", [])
            self._refresh_conv_bar()
        return conv_id

    def _task_tick(self):
        """Run any due (time-scheduled) tasks."""
        from tools.tasks import tick_due_tasks, load_tasks
        due = tick_due_tasks()
        if not due:
            return
        task_map = {t["id"]: t for t in load_tasks()}
        for task in due:
            self._run_task_now(task, task_map.get(task["id"], {}))

    def _run_startup_tasks(self):
        """Fire every enabled task carrying an 'on startup' condition — once per
        launch. Scheduling is untouched (startup tasks have no time trigger)."""
        from tools.tasks import load_tasks, mark_task_result
        for t in load_tasks():
            if not t.get("enabled", True):
                continue
            if any((c or {}).get("kind") == "startup" for c in t.get("conditions", [])):
                summary = {"id": t["id"], "name": t.get("name", "Task"),
                           "prompt": t.get("prompt", ""),
                           "conversation_id": t.get("conversation_id", "")}
                try:
                    self._run_task_now(summary, t)
                except Exception as e:
                    print(f"[tasks] startup task '{t.get('name')}' failed: {e}")
                    mark_task_result(t["id"], False, str(e))

    def _run_task_now(self, task: dict, full_task: dict):
        """Execute one task's actions immediately. Shared by the time ticker and
        the startup runner; does not touch scheduling."""
        from tools.tasks import mark_task_result

        task_name = task.get("name", "Task")
        prompt = task.get("prompt", "")

        actions = full_task.get("actions", [])
        if not actions:
            atype = full_task.get("action_type", "prompt")
            actions = [{"type": atype, "content": prompt}]

        has_prompt = any(a.get("type") == "prompt" for a in actions)
        non_prompt_actions = [a for a in actions if a.get("type") != "prompt"]
        conv_id = self._resolve_task_conv(full_task)

        def _fire_non_prompt(acts, cid, tname, prm):
            """Execute all non-prompt actions."""
            for action in acts:
                atype = action.get("type", "")
                content = action.get("content", "") or prm

                if atype == "visual":
                    self.visual_alert(source_conv_id=cid,
                                      request_response=action.get("request_response", False))
                elif atype == "audio":
                    try:
                        from tools.tts import text_to_speech
                        text_to_speech(content or tname, play=True)
                    except Exception:
                        pass
                elif atype == "sound":
                    try:
                        from core.sounds import play
                        play(content or "alert.mp3")
                    except Exception:
                        pass
                elif atype == "execute":
                    self._run_execute_action(action.get("content", ""))

        if has_prompt:
            prompt_content = next(
                (a["content"] for a in actions if a["type"] == "prompt" and a.get("content")),
                prompt or task_name)

            agent = Agent()
            agent.config = self.agent.config
            agent._provider_override = self.agent._provider_override or ""
            agent._model_override = self.agent._model_override or ""
            task_copy = dict(task)
            task_copy["prompt"] = prompt_content
            t = _TaskThread(agent, task_copy, conv_id)
            if not hasattr(self, '_task_threads'):
                self._task_threads = []
            self._task_threads.append(t)

            # Fire alerts AFTER prompt completes so everything hits at once
            def _on_this_task_done(cid_=conv_id, tname_=task_name, prompt_=prompt,
                                   acts_=non_prompt_actions):
                _fire_non_prompt(acts_, cid_, tname_, prompt_)

            t.task_completed.connect(self._on_task_completed)
            t.task_completed.connect(lambda *_, f=_on_this_task_done: f())
            t.finished.connect(lambda th=t: QTimer.singleShot(100, lambda: self._task_thread_done(th)))
            t.start()
            mark_task_result(task["id"], True)
        else:
            # No prompt — fire alerts immediately
            _fire_non_prompt(non_prompt_actions, conv_id, task_name, prompt)
            mark_task_result(task["id"], True)

    def _run_execute_action(self, content: str):
        """Launch a .py / .exe / script in its own console window, detached from
        Familiar. `content` is a path optionally followed by args (quote paths
        containing spaces). The working dir is the target's own folder — so you
        no longer have to cd into each script's directory by hand."""
        import os, sys, shlex, subprocess
        content = (content or "").strip()
        if not content:
            return
        try:
            parts = shlex.split(content, posix=False)
        except ValueError:
            parts = content.split()
        if not parts:
            return
        target = parts[0].strip('"')
        args = [p.strip('"') for p in parts[1:]]
        cwd = os.path.dirname(target) or None
        ext = os.path.splitext(target)[1].lower()
        if ext == ".py":
            cmd = [("py" if sys.platform == "win32" else sys.executable), target, *args]
        elif ext == ".pyw":
            cmd = [sys.executable, target, *args]
        else:
            cmd = [target, *args]  # .exe / .bat / .cmd / etc.
        flags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
        try:
            subprocess.Popen(cmd, cwd=cwd, creationflags=flags)
        except Exception as e:
            print(f"[tasks] execute action failed for {content!r}: {e}")

    def _on_task_completed(self, conv_id: str, task_name: str, reply: str):
        """Task finished — refresh chat if it delivered to the current conversation, play sound."""
        if conv_id == self._current_conv_id:
            # Reload the conversation to pick up the new messages
            from core.conversations import load_conversation
            data = load_conversation(conv_id)
            if data:
                self._message_meta.clear()
                self.agent.context.clear()
                for msg in data.get("messages", []):
                    role = msg.get("role", "")
                    content = msg.get("content", "")
                    if role in ("user", "assistant"):
                        self.agent.context.append({"role": role, "content": content})
                        self._message_meta.append(msg)
                    elif role in ("terminal_card", "plan_card", "subagent_card", "chart_card", "diff_card"):
                        self._message_meta.append(msg)
                self._clear_message_widgets()
                self._recalc_and_sync(immediate=True)
                QTimer.singleShot(50, self._scroll_to_bottom)

        # Play sound after reload so it coincides with the message appearing
        try:
            from core.sounds import play_ui
            play_ui("message.mp3")
        except Exception:
            pass

        # Auto-TTS: speak the response if enabled in settings (parity with _on_response)
        if reply and reply.strip():
            try:
                if self.agent.config.get("tts_autoplay", False):
                    self._speak_response(reply)
            except Exception:
                pass

    def _task_thread_done(self, thread):
        """Clean up finished task thread references."""
        if hasattr(self, '_task_threads'):
            try:
                self._task_threads.remove(thread)
            except ValueError:
                pass
        thread.deleteLater()

    # ── Peer network intake ───────────────────────────────────────────

    def _on_network_event(self, data):
        """Authenticated /sync event from a peer (GUI thread, via network_event).

        'chat' events land in a per-peer "Network: <node>" conversation as a
        user message. With network.auto_respond enabled, the agent then answers
        in that conversation and the reply is sent back to the peer — full
        agent-to-agent messaging. Other event types are logged and ignored."""
        if not isinstance(data, dict):
            return
        if data.get("type") != "chat":
            print(f"[network] ignoring event type {data.get('type')!r}", flush=True)
            return
        node = str(data.get("from") or "peer").strip() or "peer"
        text = str(data.get("message") or "").strip()
        if not text:
            return

        import time as _time
        from core.conversations import (list_conversations, load_conversation,
                                        new_conversation_id, save_conversation)
        conv_name = f"Network: {node}"
        conv_id = next((c["id"] for c in list_conversations()
                        if c.get("name") == conv_name), "")
        if not conv_id:
            conv_id = new_conversation_id()
            save_conversation(conv_id, conv_name, [])
            self._refresh_conv_bar()
        conv = load_conversation(conv_id) or {}
        messages = conv.get("messages", [])
        messages.append({"role": "user", "content": f"[from {node} via network] {text}",
                         "_timestamp": _time.time()})
        save_conversation(conv_id, conv_name, messages)

        if bool((self.agent.config.get("network") or {}).get("auto_respond")):
            agent = Agent()
            agent.config = self.agent.config
            agent._provider_override = self.agent._provider_override or ""
            agent._model_override = self.agent._model_override or ""
            t = _NetworkReplyThread(agent, conv_id, node,
                                    str(data.get("reply_url") or ""))
            if not hasattr(self, '_task_threads'):
                self._task_threads = []
            self._task_threads.append(t)
            t.completed.connect(self._on_task_completed)
            t.finished.connect(lambda th=t: QTimer.singleShot(
                100, lambda: self._task_thread_done(th)))
            t.start()
        else:
            # No inference — just surface the message (refresh + sound).
            self._on_task_completed(conv_id, conv_name, "")

    # ── Remote conversations: HOST side ──────────────────────────────
    # A peer is mirroring one of OUR conversations and sent input. We run the
    # turn here (our agent, our tools, committed here) and stream it back.

    def _on_remote_input(self, conv_id: str, text: str, reply_url: str):
        from core.network import network_manager
        if not conv_id or not text:
            return
        # Ensure the sender receives this conversation's events even if it drove
        # us without an explicit /conv/subscribe first.
        if reply_url:
            network_manager._subscribe_conv(conv_id, reply_url)

        # If the host has THIS conversation open and is idle (composer empty, no
        # turn running, not itself mirroring), run the message through the normal
        # local turn path. That gives the host the exact same streaming + typing
        # indicator a local message would, and the host-turn publish hooks
        # (_publish_host_turn_event) tee the same stream out to the viewer — so
        # both ends mirror each other. Otherwise run it headless in the
        # background and just refresh the view at the boundaries.
        if (conv_id == self._current_conv_id and self._thread is None
                and self._remote_mirror is None
                and not self.input.toPlainText().strip()):
            self.input.setPlainText(text)
            self.send_message()
            return

        agent = Agent()
        agent.config = self.agent.config
        agent._provider_override = self.agent._provider_override or ""
        agent._model_override = self.agent._model_override or ""
        t = _RemoteHostTurnThread(agent, conv_id, text)
        if not hasattr(self, '_task_threads'):
            self._task_threads = []
        self._task_threads.append(t)
        t.user_saved.connect(self._on_remote_host_turn_done)   # live: show incoming msg
        t.refreshed.connect(self._on_remote_host_turn_done)    # show the reply
        t.finished.connect(lambda th=t: QTimer.singleShot(
            100, lambda: self._task_thread_done(th)))
        t.start()

    def _on_remote_host_turn_done(self, conv_id: str):
        """A peer-driven turn finished on this host. Refresh the local view if
        that conversation happens to be open, and play the message cue."""
        self._on_task_completed(conv_id, "", "")

    def _on_terminal_attach_request(self, req):
        """GUI thread: build a TerminalAttachment to the conversation's live
        shell so a remote viewer mirrors the real session. Sets req['attachment']
        (or leaves it None → the bridge spawns a fresh shell) and signals done."""
        try:
            from ui.terminal_workspace import TerminalAttachment
            backend = self._right_workspace.terminal_panel.active_backend_for(
                req.get("conv_id", ""))
            if backend is not None and backend.is_alive():
                req["attachment"] = TerminalAttachment(backend)
        except Exception as e:
            print(f"[network] terminal attach failed: {e}", flush=True)
        finally:
            try:
                req["event"].set()
            except Exception:
                pass

    def _publish_host_turn_event(self, kind: str, **kw):
        """Best-effort: mirror the LOCALLY-driven turn of the current conversation
        out to any peer watching it. No-op when nobody is subscribed (a cheap
        lock check), and never for a conversation we ourselves are mirroring."""
        cid = self._current_conv_id
        if not cid or self._remote_mirror is not None or self._is_remote_id(cid):
            return
        try:
            from core.network import network_manager
            if network_manager.conv_has_subscribers(cid):
                network_manager.publish_conv_event(cid, {"kind": kind, **kw})
        except Exception:
            pass

    # ── Remote conversations: VIEWER side ────────────────────────────

    def _is_remote_id(self, conv_id: str) -> bool:
        return isinstance(conv_id, str) and conv_id.startswith("remote::")

    def _refresh_remote_convs(self):
        """Pull each peer's conversation list (off-thread) and merge into the
        dropdown. Cheap, best-effort; failures just leave a peer's list stale."""
        from core.network import network_manager, outbound_identity
        if not network_manager.running:
            return
        _, _, peers = outbound_identity()
        if not peers:
            return
        for p in peers:
            threading.Thread(target=self._fetch_peer_convs, args=(dict(p),),
                             daemon=True).start()

    def _fetch_peer_convs(self, peer: dict):
        from core.network import peer_conv_list
        ok, convs, _detail = peer_conv_list(peer.get("url", ""))
        if not ok:
            return
        name = peer.get("name") or peer.get("url", "")
        # Marshal back to the GUI thread through the conv-event sink.
        self.conv_event_received.emit(
            {"kind": "_peer_conv_list", "peer": name, "url": peer.get("url", ""),
             "convs": convs})

    def _apply_peer_conv_list(self, peer: str, url: str, convs: list):
        self._remote_convs_by_peer[peer] = [
            {"id": f"remote::{peer}::{c.get('id')}", "name": c.get("name", ""),
             "peer": peer, "peer_url": url, "remote_id": c.get("id")}
            for c in convs if c.get("id")]
        try:
            self._conv_bar.set_remote_conversations(self._remote_convs_by_peer)
        except Exception:
            pass

    def _enter_remote_mirror(self, combo_id: str):
        """Open a live, read/write mirror of a peer's conversation. We render its
        snapshot and stream its turns; messages we send run on the HOST."""
        try:
            _, peer, remote_id = combo_id.split("::", 2)
        except ValueError:
            return
        entry = next((c for lst in self._remote_convs_by_peer.values() for c in lst
                      if c["id"] == combo_id), None)
        peer_url = entry["peer_url"] if entry else ""
        if not peer_url:
            return
        self._exit_remote_mirror()      # leave any prior mirror cleanly
        self._auto_save()               # persist the local conversation we're leaving
        try:
            self._save_viewer_state()   # remember the local conv's splitter/tools layout
        except Exception:
            pass
        self._current_conv_id = combo_id
        self._remote_mirror = {"combo_id": combo_id, "peer": peer,
                               "peer_url": peer_url, "remote_id": remote_id,
                               "live_idx": None}
        self.agent.clear_context()
        self._clear_message_widgets()
        self._message_meta = []
        self._conv_bar.highlight(combo_id)
        self._conv_bar.set_hint(f"🌐 [{peer}] mirroring · host runs the work")
        # Point the File viewer at the host's workspace (read+write over the
        # net). Best-effort — chat mirroring still works if files don't.
        try:
            self._file_viewer.enter_remote_workspace(peer_url, remote_id, peer)
        except Exception as e:
            print(f"[network] remote workspace unavailable: {e}", flush=True)
        # Mirror the host's Notes (edit), Calendar (read-only), and Browser
        # (current URL) for this conversation. All best-effort.
        try:
            self._right_workspace.notes_panel.enter_remote(peer_url, remote_id)
            self._right_workspace.calendar_panel.enter_remote(peer_url)
            # Terminal → a live shell ON THE HOST (WebSocket), scoped to the
            # conversation's workspace.
            self._right_workspace.enter_remote_terminal(peer_url, remote_id, peer)
        except Exception as e:
            print(f"[network] remote tool mirror partial: {e}", flush=True)
        threading.Thread(target=self._open_remote_browser_url,
                         args=(peer_url, remote_id, combo_id), daemon=True).start()
        threading.Thread(target=self._subscribe_remote,
                         args=(peer_url, remote_id, combo_id), daemon=True).start()

    def _subscribe_remote(self, peer_url: str, remote_id: str, combo_id: str):
        from core.network import peer_conv_subscribe
        ok, snap, _detail = peer_conv_subscribe(peer_url, remote_id)
        self.conv_event_received.emit(
            {"kind": "_remote_snapshot", "combo_id": combo_id, "ok": ok,
             "snapshot": snap or {}})

    def _open_remote_browser_url(self, peer_url: str, remote_id: str, combo_id: str):
        """Fetch the host conversation's current browser URL (off-thread) and
        open it in our browser panel for the mirror, matching what the host
        sees."""
        from core.network import peer_browser_url
        ok, url = peer_browser_url(peer_url, remote_id)
        if ok and url:
            self.conv_event_received.emit(
                {"kind": "_remote_browser", "combo_id": combo_id, "url": url})

    def _exit_remote_mirror(self):
        m = self._remote_mirror
        if not m:
            return
        self._disarm_remote_watchdog()
        self._hide_thinking()
        self._remote_mirror = None
        try:
            self._file_viewer.exit_remote_workspace()
        except Exception:
            pass
        try:
            self._right_workspace.notes_panel.exit_remote()
            self._right_workspace.calendar_panel.exit_remote()
            self._right_workspace.exit_remote_terminal()
        except Exception:
            pass
        try:
            from core.network import peer_conv_unsubscribe
            threading.Thread(
                target=peer_conv_unsubscribe, args=(m["peer_url"], m["remote_id"]),
                daemon=True).start()
        except Exception:
            pass

    def _send_remote(self, text: str):
        """Deliver a composer message to the host that owns the mirrored
        conversation. Render it locally NOW (a chat where your own message
        doesn't appear reads as broken); the host's 'user' echo is deduped
        against this optimistic copy, so the host stays the source of truth."""
        m = self._remote_mirror
        if not m or not text.strip():
            return
        self.input.clear()
        self._add_message("You", text)
        m["pending_echo"] = text
        self._set_typing_prefix(f"{AGENT_LABEL} (remote) is thinking")
        self._show_thinking()
        self._arm_remote_watchdog()
        combo = m["combo_id"]
        peer_url, remote_id = m["peer_url"], m["remote_id"]

        def _send():
            from core.network import peer_conv_input
            ok, detail = peer_conv_input(peer_url, remote_id, text)
            # Report only a DELIVERY failure here; success is confirmed by the
            # host echoing a 'user' event back. Marshalled to the GUI thread.
            if not ok:
                self.conv_event_received.emit(
                    {"kind": "_send_result", "combo_id": combo, "detail": detail})
        threading.Thread(target=_send, daemon=True).start()

    def _remote_notice(self, text: str):
        """Render a plain agent-bubble notice in the mirror (the 'Error' sender
        doesn't map to a real role, so it would silently render nothing)."""
        self._add_message(AGENT_LABEL, f"⚠ {text}")

    def _arm_remote_watchdog(self, seconds: int = 90):
        """Restart the 'host went silent' timer. Re-armed on every inbound event
        so a long, actively-streaming turn never trips it — only true silence
        (host offline, can't reach us back, or crashed) does."""
        if not hasattr(self, "_remote_wait_timer"):
            self._remote_wait_timer = QTimer(self)
            self._remote_wait_timer.setSingleShot(True)
            self._remote_wait_timer.timeout.connect(self._on_remote_timeout)
        self._remote_wait_timer.start(seconds * 1000)

    def _disarm_remote_watchdog(self):
        t = getattr(self, "_remote_wait_timer", None)
        if t is not None:
            t.stop()

    def _on_remote_timeout(self):
        if self._remote_mirror is None:
            return
        self._hide_thinking()
        self._remote_notice(
            "No response from the host — it may be offline, busy, or unable to "
            "reach this machine. Your message was delivered; the reply (if any) "
            "will appear when it arrives. Tip: the host must be able to reach "
            "THIS machine's public address too (check Settings → Network → "
            "Check peers on the host).")

    def _on_conv_event(self, data):
        """GUI-thread sink for every conv_event_received emission: peer-list
        results, mirror snapshots, send failures, and live events from a host."""
        if not isinstance(data, dict):
            return
        kind = data.get("kind")

        # Internal (locally-emitted) control messages.
        if kind == "_peer_conv_list":
            self._apply_peer_conv_list(data.get("peer", ""), data.get("url", ""),
                                       data.get("convs", []))
            return
        if kind == "_remote_snapshot":
            self._apply_remote_snapshot(data)
            return
        if kind == "_remote_browser":
            m = self._remote_mirror
            if m and data.get("combo_id") == m["combo_id"] and data.get("url"):
                try:
                    self._right_workspace.browser_panel.get_or_create_for_conv(
                        m["combo_id"], m["peer"], data["url"])
                except Exception:
                    pass
            return
        if kind == "_send_result":
            m = self._remote_mirror
            if m and data.get("combo_id") == m["combo_id"]:
                self._disarm_remote_watchdog()
                self._hide_thinking()
                m.pop("pending_echo", None)
                self._remote_notice(f"Couldn't deliver your message to the host: "
                                    f"{data.get('detail', 'unknown error')}")
            return

        # Live event from a host we're mirroring — ignore unless it matches the
        # open mirror. Any matching event = the host is alive → re-arm watchdog.
        m = self._remote_mirror
        if not m or data.get("conv_id") != m["remote_id"]:
            return
        self._arm_remote_watchdog()
        if kind == "user":
            content = data.get("message", {}).get("content", "")
            # Skip the host's echo of OUR optimistic message (already rendered
            # at send time); still render other viewers'/the host's own input.
            if m.pop("pending_echo", None) != content:
                self._add_message("You", content)
        elif kind == "round_start":
            m["live_idx"] = None
        elif kind == "text":
            self._render_remote_live_text(data.get("text", ""))
        elif kind == "tool":
            self._set_typing_prefix(f"{AGENT_LABEL} (remote) · {data.get('name', 'tool')}")
        elif kind == "image":
            self._render_remote_image(data)
        elif kind == "final":
            self._disarm_remote_watchdog()
            self._hide_thinking()
            msg = data.get("message", {})
            self._finalize_remote_text(msg.get("content", ""),
                                       msg.get("tool_names", []))
            m["live_idx"] = None

    def _render_remote_image(self, data):
        """Show a screenshot (or other image) the host shared into the mirrored
        conversation — e.g. a screenshot of the host's screen."""
        import base64 as _b64
        import os as _os
        import tempfile as _tf
        import time as _t
        try:
            raw = _b64.b64decode(data.get("data", "") or "")
        except Exception:
            return
        if not raw:
            return
        path = _os.path.join(_tf.gettempdir(), f"remote_shot_{int(_t.time() * 1000)}.jpg")
        try:
            with open(path, "wb") as f:
                f.write(raw)
        except Exception:
            return
        self._message_meta.append({
            "role": "assistant", "content": "[remote screenshot]",
            "tool_names": ["screenshot"], "image_path": path,
            "_html": "<em>🌐 remote screenshot</em>", "_timestamp": _t.time(),
        })
        self._clear_message_widgets()
        self._recalc_and_sync(immediate=True)
        QTimer.singleShot(50, self._scroll_to_bottom)

    def _apply_remote_snapshot(self, data):
        m = self._remote_mirror
        if not m or data.get("combo_id") != m["combo_id"]:
            return
        if not data.get("ok"):
            self._remote_notice("Could not load this conversation from the host — "
                                "it may be offline.")
            return
        snap = data.get("snapshot", {})
        self._message_meta = []
        for msg in snap.get("messages", []):
            role = msg.get("role")
            if role in ("user", "assistant") and msg.get("content"):
                self._message_meta.append({
                    "role": role, "content": msg["content"],
                    "tool_names": msg.get("tool_names", []),
                    "_timestamp": msg.get("_timestamp", 0),
                })
        self._clear_message_widgets()
        self._recalc_and_sync(immediate=True)
        QTimer.singleShot(50, self._scroll_to_bottom)
        # Adopt the host conversation's tool/workspace pane state so the mirror
        # matches what it looks like on the host: same open/closed splitter AND
        # the same tool tab (Files/Terminal/Browser/…).
        try:
            if snap.get("workspace_collapsed", True):
                self._collapse_workspace()
            else:
                self._expand_workspace()
            page = snap.get("workspace_page")
            if page is not None:
                self._right_workspace.set_workspace_page(int(page))
        except Exception:
            pass

    def _render_remote_live_text(self, text: str):
        """Update (or create) the streaming assistant bubble for a remote turn."""
        m = self._remote_mirror
        if not m:
            return
        idx = m.get("live_idx")
        if idx is None or idx >= len(self._message_meta):
            self._add_message(AGENT_LABEL, text)
            m["live_idx"] = len(self._message_meta) - 1
        else:
            meta = self._message_meta[idx]
            meta["content"] = text
            meta["_html"] = markdown2.markdown(
                text, extras=["fenced-code-blocks", "tables", "code-friendly"])
            meta.pop("_html_theme_key", None)
            w = self._idx_to_widget.get(idx)
            if isinstance(w, ChatMessageWidget):
                w.update_content(text, meta["_html"])
            if self._pinned_to_bottom:
                QTimer.singleShot(0, lambda: self._scroll_to_bottom(force=True))

    def _finalize_remote_text(self, text: str, tool_names: list):
        m = self._remote_mirror
        if not m:
            return
        idx = m.get("live_idx")
        html = markdown2.markdown(
            text, extras=["fenced-code-blocks", "tables", "code-friendly"])
        if idx is not None and idx < len(self._message_meta):
            meta = self._message_meta[idx]
            meta["content"] = text
            meta["tool_names"] = tool_names or []
            meta["_html"] = html
            meta.pop("_html_theme_key", None)
            self._clear_message_widgets()
            self._recalc_and_sync(immediate=True)
        else:
            self._add_message(AGENT_LABEL, text, tool_names=tool_names,
                              precomputed_assistant_html=html)
        try:
            from core.sounds import play_ui
            play_ui("message.mp3")
        except Exception:
            pass
        QTimer.singleShot(50, self._scroll_to_bottom)

    # ── Tool-audit alert ──────────────────────────────────────────────

    def _on_audit_triggered(self, tool: str, conv_id: str):
        """An audit prompt was injected into a target conversation —
        blink the conversation brick and play an alert sound."""
        print(f"[audit] Triggered for '{tool}' → conv {conv_id[:8]}...")
        try:
            from core.sounds import play_ui
            play_ui("alert.mp3")
        except Exception:
            pass
        if conv_id:
            self.visual_alert(source_conv_id=conv_id)

    # ── Visual alert / blink system ─────────────────────────────────

    def _conversation_count(self) -> int:
        """How many conversations exist right now."""
        try:
            from core.database import list_conversations
            return len(list_conversations())
        except Exception:
            return 0

    def _conv_exists(self, conv_id: str) -> bool:
        """Whether a conversation id still exists (not deleted)."""
        if not conv_id:
            return False
        try:
            from core.database import list_conversations
            return any(c.get("id") == conv_id for c in list_conversations())
        except Exception:
            return False

    def visual_alert(self, source_conv_id: str = "", request_response: bool = False):
        """Fire a visual alert — blink OS taskbar, conversation brick, and optionally input box.
        Smart: won't blink things the user is already looking at."""
        import ctypes

        win = self.window()
        app_focused = win.isActiveWindow() if win else False
        viewing_source = (source_conv_id == self._current_conv_id) if source_conv_id else True

        # 1) OS taskbar flash — only if app is NOT focused and the user is
        # actually present (flashing dismisses a running screensaver).
        if not app_focused and hasattr(win, 'winId') and not _user_is_away():
            try:
                hwnd = int(win.winId())

                class FLASHWINFO(ctypes.Structure):
                    _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                                ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                                ("dwTimeout", ctypes.c_uint)]

                fw = FLASHWINFO()
                fw.cbSize = ctypes.sizeof(FLASHWINFO)
                fw.hwnd = hwnd
                fw.dwFlags = 0x03  # FLASHW_ALL
                fw.uCount = 5
                fw.dwTimeout = 0
                ctypes.windll.user32.FlashWindowEx(ctypes.byref(fw))
            except Exception:
                pass

        # 2) Conversation brick blink — only persist a visual alert when the
        # user actually has to SWITCH conversations to reach the source. Skip it
        # when:
        #   - we're already viewing that conversation (nothing to navigate to),
        #   - the source thread no longer exists, or
        #   - there's only one conversation (you're necessarily already there;
        #     being in the window is enough to see it).
        if source_conv_id and not viewing_source and self._conv_exists(source_conv_id) \
                and self._conversation_count() > 1:
            self._conv_bar.start_blink(source_conv_id)

        # 3) Input box blink — if viewing, start now; if not, defer until user switches
        if request_response:
            if viewing_source:
                self._start_input_blink()
            else:
                # Queue it — will fire when user switches to this conversation
                if not hasattr(self, '_pending_input_blinks'):
                    self._pending_input_blinks = set()
                self._pending_input_blinks.add(source_conv_id)

    def _start_input_blink(self):
        """Blink the input box border to request user attention. Stops when user types."""
        if hasattr(self, '_input_blink_timer') and self._input_blink_timer.isActive():
            return
        p = PALETTE
        self._input_blink_phase = True
        self._input_blink_timer = QTimer(self)
        self._input_blink_timer.timeout.connect(self._input_blink_tick)
        self._input_blink_timer.start(500)
        # Connect to input to stop on typing
        self.input.textChanged.connect(self._stop_input_blink)

    def _input_blink_tick(self):
        p = PALETTE
        self._input_blink_phase = not self._input_blink_phase
        if self._input_blink_phase:
            self.input.setStyleSheet(f"""
                QTextEdit {{
                    background: {p['panel']};
                    color: {p['text']};
                    border: 2px solid {p['glow_hot']};
                    padding: 5px;
                    font-family: Consolas, monospace;
                    font-size: 10pt;
                    {_mono_selection_qss(p)}
                }}""")
        else:
            self.input.setStyleSheet(f"""
                QTextEdit {{
                    background: {p['panel']};
                    color: {p['text']};
                    border: 1px solid {p['accent_muted']};
                    padding: 6px;
                    font-family: Consolas, monospace;
                    font-size: 10pt;
                    {_mono_selection_qss(p)}
                }}""")

    def _stop_input_blink(self):
        if hasattr(self, '_input_blink_timer'):
            self._input_blink_timer.stop()
        try:
            self.input.textChanged.disconnect(self._stop_input_blink)
        except TypeError:
            pass
        self.input._apply_styles()

    # ── Auto-TTS ──────────────────────────────────────────────────────

    def _speak_response(self, text: str):
        """Queue TTS for the agent's response. Clips play sequentially; user
        input interrupts the queue (see ``_interrupt_voice``)."""
        import threading

        # Enqueue synthesize-then-play job
        self._voice_interrupt = False  # reset interrupt flag for new batch
        self._voice_queue.put(text)

        # Ensure the drain worker is running
        if not self._voice_worker_running:
            self._voice_worker_running = True
            t = threading.Thread(target=self._voice_drain_worker, daemon=True)
            t.start()

    def _voice_drain_worker(self):
        """Background thread that drains the voice queue one clip at a time."""
        try:
            while not self._voice_interrupt:
                try:
                    text = self._voice_queue.get(timeout=0.3)
                except Exception:
                    # queue.Empty — check if more items are coming
                    if self._voice_queue.empty():
                        break
                    continue
                if self._voice_interrupt:
                    break
                try:
                    from tools.tts import synthesize_audio, _play_audio
                    path = synthesize_audio(text)
                    if path and not self._voice_interrupt:
                        proc = _play_audio(path, blocking=False)
                        self._voice_current_proc = proc
                        if proc is not None:
                            # Wait for playback to finish (or interrupt)
                            while proc.poll() is None:
                                if self._voice_interrupt:
                                    proc.kill()
                                    break
                                import time; time.sleep(0.1)
                        self._voice_current_proc = None
                except Exception as e:
                    print(f"[TTS] Error: {e}")
        finally:
            # Drain any leftover items if interrupted
            while not self._voice_queue.empty():
                try:
                    self._voice_queue.get_nowait()
                except Exception:
                    break
            self._voice_worker_running = False
            self._voice_current_proc = None

    def _interrupt_voice(self):
        """Kill current playback and flush the voice queue (user sent a new message)."""
        self._voice_interrupt = True
        proc = self._voice_current_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        # Flush the queue
        while not self._voice_queue.empty():
            try:
                self._voice_queue.get_nowait()
            except Exception:
                break

    def clear_chat(self):
        """
        Confirm before wiping the conversation. The dialog previews each
        subscribed memory stream's rolling summary and lets the user decide
        whether to also clear those summaries (default: keep).
        """
        if self._thread is not None:
            return

        from ui.clear_conversation_dialog import ClearConversationDialog
        stream_summaries = {}
        try:
            stream_summaries = self.agent.get_subscribed_stream_summaries()
        except Exception as e:
            print(f"[clear_chat] summary fetch failed: {e}")

        dlg = ClearConversationDialog(stream_summaries, parent=self)
        if not dlg.exec():
            return
        if dlg.result_action() != "clear":
            return

        # Wipe messages
        self._clear_message_widgets()
        self._message_meta = []
        self.agent.clear_context()

        # Optionally wipe summaries per user's per-stream selection
        streams_to_clear = dlg.streams_to_clear()
        if streams_to_clear:
            try:
                self.agent.clear_stream_summaries(streams_to_clear)
            except Exception as e:
                print(f"[clear_chat] clear summaries failed: {e}")

        self._auto_save()

    def _undo_last_turn(self):
        """Smart undo:
        - If last visible is assistant/terminal_card: remove everything back to (and including)
          the preceding user message + any cards between them. Restore user text to input.
        - If last visible is user: remove just that user message, restore text to input.
        """
        if not self._message_meta or self._thread is not None:
            return

        last_role = self._message_meta[-1].get("role", "")

        # Snapshot captured on the user message being removed — used to roll
        # back rolling summaries so they don't drift ahead of the context.
        summary_snapshot_to_restore: dict | None = None

        if last_role == "user":
            # Edge case: last message is user (LLM didn't fire yet)
            # Just remove the user message and put text back in input
            removed = self._message_meta.pop()
            user_text = removed.get("content", "")
            # Pull snapshot from meta OR from agent context
            summary_snapshot_to_restore = removed.get("_summary_snapshot")
            # Also remove from agent context
            if self.agent.context and self.agent.context[-1].get("role") == "user":
                ctx_user = self.agent.context.pop()
                if not summary_snapshot_to_restore:
                    summary_snapshot_to_restore = ctx_user.get("_summary_snapshot")
            if user_text:
                self.input.setPlainText(user_text)
        else:
            # Normal case: roll back the whole turn
            # Collect checkpoint info from entries we're about to remove
            checkpoint_hash = None
            checkpoint_dir = None
            user_text = ""
            found_user = False
            while self._message_meta:
                entry = self._message_meta[-1]
                role = entry.get("role", "")
                # Grab checkpoint from assistant entry if present
                if entry.get("_checkpoint_hash") and not checkpoint_hash:
                    checkpoint_hash = entry["_checkpoint_hash"]
                    checkpoint_dir = entry.get("_checkpoint_dir", "")
                if role == "user":
                    user_text = entry.get("content", "")
                    summary_snapshot_to_restore = entry.get("_summary_snapshot")
                    self._message_meta.pop()
                    found_user = True
                    break
                self._message_meta.pop()
            if not found_user:
                return

            # Remove from agent context: pop back to (and including) last user
            ctx = self.agent.context
            while ctx:
                role = ctx[-1].get("role", "")
                popped = ctx.pop()
                if role == "user":
                    # Prefer snapshot from agent context if meta didn't have one
                    if not summary_snapshot_to_restore:
                        summary_snapshot_to_restore = popped.get("_summary_snapshot")
                    break

            # Restore filesystem checkpoint if this turn had file mutations
            if checkpoint_hash and checkpoint_dir:
                try:
                    from core.checkpoints import checkpoint_manager
                    result = checkpoint_manager.restore(checkpoint_dir, checkpoint_hash)
                    if result.get("error"):
                        print(f"[Undo] Checkpoint restore failed: {result['error']}")
                    else:
                        print(f"[Undo] Restored files to checkpoint {checkpoint_hash[:8]}")
                except Exception as e:
                    print(f"[Undo] Checkpoint restore error: {e}")

            if user_text:
                self.input.setPlainText(user_text)

        # Roll back rolling summaries so they match the rewound context.
        # The snapshot was captured when the user message was originally appended,
        # so restoring it reverts to the exact state before this turn started.
        if summary_snapshot_to_restore:
            try:
                self.agent.restore_summary_snapshot(summary_snapshot_to_restore)
                print("[Undo] Rolling summary state restored.")
            except Exception as e:
                print(f"[Undo] Summary restore failed: {e}")

        # Clear cached HTML so visible + off-screen messages re-theme on next paint
        self._invalidate_message_html_cache()

        # Refresh display
        self._clear_message_widgets()
        self._recalc_and_sync(immediate=True)
        self._auto_save()
        self._set_inferring(False)
        QTimer.singleShot(0, self.input.setFocus)

    def _open_settings(self, on_accept=None):
        """Open Settings NON-MODALLY so the rest of the window (title-bar
        always-on-top / screenshot, chat, etc.) stays interactive while it's up.

        Because it no longer blocks, the post-accept refresh runs from the
        dialog's `accepted` signal rather than a return value. `on_accept`
        (passed by main.py) lets the host window refresh theme too.
        """
        from ui.settings_dialog import SettingsDialog
        # Reuse an already-open instance instead of stacking duplicates.
        existing = getattr(self, "_settings_dlg", None)
        if existing is not None:
            try:
                existing.raise_()
                existing.activateWindow()
                return
            except RuntimeError:
                self._settings_dlg = None

        dlg = SettingsDialog(self.agent, parent=self)
        self._settings_dlg = dlg

        def _on_accepted():
            self._refresh_ws_combo()
            self._update_conv_summary_label()
            mode = self.agent.config.get("conversation_bar_overflow", "wrap")
            self._conv_bar.set_mode(mode)
            self._refresh_conv_bar()
            if callable(on_accept):
                on_accept()

        def _on_finished(_result):
            try:
                from ui.dialog_geometry import save_geometry
                save_geometry("settings", dlg)
            except Exception:
                pass
            self._settings_dlg = None

        dlg.accepted.connect(_on_accepted)
        dlg.finished.connect(_on_finished)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # ── Styles ───────────────────────────────────────────────────────

    def apply_theme(self):
        """Re-apply styles after a settings change. Rebuilds visible messages in batches."""
        # Re-entrancy guard. apply_theme tears down and rebuilds the whole
        # transcript; if a second call lands while the first is mid-flight (e.g.
        # the user clicks through colors quickly), the two rebuilds fight over
        # _idx_to_widget / _message_meta. Coalesce into a single trailing rebuild.
        if getattr(self, "_theme_applying", False):
            self._theme_rebuild_deferred = True
            return
        self._theme_applying = True
        try:
            self.agent.config = load_config()
            ChatMessageWidget._font_size = self.agent.config.get("chat_font_size", 10)
            ChatMessageWidget._ellipsis_enabled = self.agent.config.get("animate_ellipsis", True)
            mode = self.agent.config.get("tool_display_mode", "chips")
            ChatMessageWidget._tool_display_mode = (
                mode if mode in ("chips", "bubbles", "comma") else "chips"
            )
            ChatMessageWidget._show_tools_hint = bool(
                self.agent.config.get("show_tools_hint", False)
            )
            ChatMessageWidget._monocolor_images = bool(
                self.agent.config.get("monocolor", True)
                and self.agent.config.get("monocolor_images", False)
            )
            self._char_limit = self.agent.config.get("display_char_limit", 15000)
            self._apply_styles()
            self._apply_send_attach_button_styles()
            self._apply_stream_preview_style()
            self.input._apply_styles()
            self._conv_bar._apply_styles()
            self._refresh_stream_chips()
            p = PALETTE
            self._chat_panel.setStyleSheet(f"QFrame#ChatPanel {{ border: 1px solid {p['border']}; }}")
            self._chat_hsplitter.setStyleSheet(self._splitter_idle_ss(p))
            self._apply_workspace_side()
            self._right_workspace.apply_theme()

            # The heavy part — tear down every message widget and rebuild it —
            # cannot run safely while a turn is streaming: the live-stream flush
            # timer is concurrently creating/mutating the same widgets, and the
            # two collide hard enough to wedge the UI (the "change colors mid-job
            # and it locks up forever" bug). Defer it to _finish_inference; the
            # new chrome is already visible, and the transcript recolors the
            # instant the turn ends.
            if self._thread is not None:
                # Can't tear down/rebuild mid-stream (collides with the flush
                # timer), but we CAN recolor the already-rendered widgets in
                # place so the visible transcript repaints in the new palette
                # immediately instead of staying the old color until the turn
                # ends. A full rebuild still runs in _finish_inference.
                self._theme_rebuild_deferred = True
                self._recolor_transcript_in_place()
                return
            self._rebuild_transcript_for_theme()
        finally:
            self._theme_applying = False

    def _recolor_transcript_in_place(self) -> None:
        """Repaint every already-rendered transcript widget with the current
        palette WITHOUT removing/recreating it — safe during a streaming turn
        (never touches _idx_to_widget membership or the layout, so it can't
        collide with the live-stream flush timer). The actively-streaming bubble
        is skipped; it repaints itself on its next flush."""
        self._invalidate_message_html_cache()
        live_idx = getattr(self, "_stream_live_meta_idx", None)
        n = len(self._message_meta)
        for idx, w in list(self._idx_to_widget.items()):
            if idx == live_idx:
                continue
            try:
                if isinstance(w, ChatMessageWidget):
                    # Inline-timeline bodies bake palette colors into cached HTML,
                    # so refresh that body from meta before re-wrapping. Plain
                    # markdown bodies carry no color (the wrapper supplies it).
                    if 0 <= idx < n and self._has_inline_timeline(self._message_meta[idx]):
                        w._cached_html = self._ensure_meta_html(self._message_meta[idx])
                    w.recolor_in_place()
                    continue
                recolor = getattr(w, "recolor_in_place", None)
                if callable(recolor):
                    recolor()
            except RuntimeError:
                pass  # C++ object already deleted

    def _rebuild_transcript_for_theme(self):
        """Repaint the visible transcript with the current palette, in batches.
        Split out of apply_theme so it can be deferred until a running turn ends."""
        self._theme_rebuild_deferred = False
        # Invalidate all cached HTML — off-screen messages pick up the new palette
        # when scrolled into view; visible ones rebuild below.
        self._invalidate_message_html_cache()

        vbar = self._scroll.verticalScrollBar()
        was_at_bottom = self._is_at_bottom()
        self._theme_rebuild_scroll = (was_at_bottom, vbar.value(), vbar.maximum())
        self._clear_message_widgets()
        self._visible_start, self._visible_end = self._calc_range()
        self._baseline_end = self._visible_end
        self._theme_rebuild_idx = self._visible_start
        self._theme_rebuild_end = self._visible_end
        QTimer.singleShot(0, self._theme_rebuild_batch)

    def _apply_workspace_side(self):
        """Re-dock the workspace panel on the configured side without a restart.

        QSplitter.insertWidget() MOVES an existing child rather than adding a
        copy, so re-slotting the workspace pane is all it takes; the index
        bookkeeping the size math relies on is refreshed to match.
        """
        ws_side = str(self.agent.config.get("workspace_side", "right") or "right").lower()
        want_left = (ws_side == "left")
        if want_left == bool(getattr(self, "_ws_left", False)):
            return  # already docked on the requested side
        sizes = self._chat_hsplitter.sizes()
        self._ws_left = want_left
        self._ws_index = 0 if want_left else 1
        self._chat_index = 1 - self._ws_index
        self._chat_hsplitter.insertWidget(self._ws_index, self._right_workspace)
        self._chat_hsplitter.setCollapsible(0, True)
        self._chat_hsplitter.setCollapsible(1, True)
        # Panes swapped position — swap their widths so each keeps its size.
        if len(sizes) == 2:
            self._chat_hsplitter.setSizes(list(reversed(sizes)))

    def _theme_rebuild_batch(self):
        """Create visible message widgets in small batches to avoid UI stalls."""
        if self._theme_rebuild_idx >= self._theme_rebuild_end:
            self._sync_widgets()
            was_at_bottom, old_val, old_max = self._theme_rebuild_scroll
            def _restore_scroll():
                vbar = self._scroll.verticalScrollBar()
                if was_at_bottom:
                    self._scroll_to_bottom(force=True)
                elif old_max > 0:
                    new_max = vbar.maximum()
                    if new_max > 0:
                        vbar.setValue(int(old_val * new_max / old_max))
                    else:
                        vbar.setValue(old_val)
            QTimer.singleShot(50, _restore_scroll)
            QTimer.singleShot(200, _restore_scroll)
            return

        self._messages_container.setUpdatesEnabled(False)
        vp_w = self._scroll.viewport().width()
        ordered_indices = sorted(self._idx_to_widget.keys())
        batch_end = min(self._theme_rebuild_idx + 8, self._theme_rebuild_end)
        for i in range(self._theme_rebuild_idx, batch_end):
            if i in self._idx_to_widget or i >= len(self._message_meta):
                continue
            meta = self._message_meta[i]
            role = meta.get("role", "")
            if role in ("terminal_card", "plan_card", "subagent_card", "chart_card", "diff_card"):
                continue
            sender = "You" if role == "user" else AGENT_LABEL
            html = self._ensure_meta_html(meta)
            w = self._obtain_message_widget(
                sender=sender,
                content=(meta.get("_typed", "") if meta.get("_pastes") else meta.get("content", "")),
                tool_names=meta.get("tool_names"),
                image_path=meta.get("_thumb") or meta.get("image_path") or None,
                cached_html=html,
                timestamp=meta.get("_timestamp"),
                usage=meta.get("_usage") if sender == AGENT_LABEL else None,
                show_timestamps=self.agent.config.get("show_timestamps", True),
                show_usage=self.agent.config.get("show_usage", False),
                show_tool_chips=self.agent.config.get("show_tools_called", True),
                chat_mode=self.agent.config.get("chat_mode", "fancy"),
                continuation=not _show_sender_nametag(self._message_meta, i),
                inline_timeline=self._has_inline_timeline(meta),
                pastes=meta.get("_pastes"),
            )
            if vp_w > 0:
                w.apply_wrap_width(max(50, vp_w - 12))
            self._idx_to_widget[i] = w
            self._insert_msg_widget(i, w, ordered_indices)
        self._messages_container.setUpdatesEnabled(True)
        self._theme_rebuild_idx = batch_end
        QTimer.singleShot(0, self._theme_rebuild_batch)

    def _rebuild_messages(self):
        """Re-render visible messages from _message_meta with current theme."""
        self._invalidate_message_html_cache()
        self._clear_message_widgets()
        self._recalc_and_sync(immediate=True)

    def _apply_styles(self):
        p = PALETTE
        self.setStyleSheet(f"""
            QWidget {{
                background: {p['background']};
            }}
            QLabel {{
                color: {p['accent']};
                font-family: Consolas, monospace;
                font-size: 10pt;
            }}
            QLabel#imageLabel {{
                color: {p['accent_muted']};
                background: transparent;
                border: none;
                font-size: 8pt;
                padding: 0 10px;
            }}
            QPushButton {{
                background: {p['panel']};
                color: {p['text']};
                border: 1px solid {p['border']};
                padding: 5px 10px;
                font-family: Consolas, monospace;
            }}
            QPushButton:hover {{
                background: {p['panel_alt']};
            }}
            QPushButton:disabled {{
                background: {p['panel']};
                color: {p['border']};
            }}
            QPushButton#settingsBtn {{
                border: 1px solid {p['accent_muted']};
            }}
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
            QPushButton#clearBtn, QPushButton#copyBtn {{
                color: {p['accent']};
                background: {p['panel']};
                border: 1px solid {p['accent']};
                padding: 2px 6px;
                font-size: 9pt;
            }}
            QPushButton#clearBtn:hover, QPushButton#copyBtn:hover {{
                background: {p['accent_muted']};
                color: {p['background']};
                border-color: {p['accent']};
            }}
            QPushButton#clearBtn:pressed, QPushButton#copyBtn:pressed {{
                background: {p['accent']};
                color: {p['background']};
            }}
            QPushButton#attachBtn {{
                color: {p['accent']};
                background: {p['panel']};
                border: 1px solid {p['accent']};
                font-size: 18px;
                min-width: 20px; max-width: 20px;
                min-height: 20px; max-height: 20px;
                padding: 0;
            }}
            QPushButton#attachBtn:hover {{
                background: {p['accent_muted']};
                color: {p['background']};
                border-color: {p['accent']};
            }}
            QPushButton#attachBtn:pressed {{
                background: {p['accent']};
                color: {p['background']};
            }}
            QPushButton#sendBtn {{
                color: {p['accent']};
                background: {p['panel']};
                border: 1px solid {p['accent']};
                font-weight: bold;
                font-size: 16px;
                min-width: 25px; max-width: 25px;
                min-height: 25px; max-height: 25px;
                padding: 0;
            }}
            QPushButton#sendBtn:hover {{
                background: {p['accent_muted']};
                color: {p['background']};
                border-color: {p['accent']};
            }}
            QPushButton#sendBtn:pressed {{
                background: {p['accent']};
                color: {p['background']};
            }}
            QPushButton#imageRemoveBtn {{
                color: {p['background']};
                background: {p['accent_muted']};
                border: 1px solid {p['accent']};
                border-radius: 9px;
                font-size: 9pt;
                font-weight: bold;
                min-width: 18px; max-width: 18px;
                min-height: 18px; max-height: 18px;
                padding: 0;
            }}
            QPushButton#imageRemoveBtn:hover {{
                background: {p['accent']};
                color: {p['background']};
            }}
            QPushButton#imageRemoveBtn:pressed {{
                background: {p['accent_bright']};
                color: {p['background']};
            }}
            QScrollArea {{
                background: {p['background']};
                border: none;
            }}
            QScrollBar:vertical {{
                background: transparent;
                border: 1px solid {p['border']};
                width: 14px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({QColor(p['accent']).red()},{QColor(p['accent']).green()},{QColor(p['accent']).blue()},0.15);
                border: 1px solid {p['accent_muted']};
                min-height: 20px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
        """)
