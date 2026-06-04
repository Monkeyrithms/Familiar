"""
In-chat diff card — renders a file edit as a compact, viewer-styled diff block
for the chat's rolling timeline. Pure HTML (no Qt widget) so it embeds in the
message body like the other timeline gadgets.

IMPORTANT: this is UI-only. The HTML it produces is stored on the message's
``_stream_timeline`` (UI meta) and is NEVER fed back into the LLM context — the
transcript path only serializes ``type == "text"`` items. So a diff card can be
as detailed as we like without polluting what the model sees.

Colors mirror the file-viewer diff overlay (adds → accent_bright, removes →
accent_soft) so the in-chat preview and the click-through inline diff match.
"""

from __future__ import annotations

import difflib
import html as _html
import os
from urllib.parse import quote

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetricsF, QTextCursor, QTextFormat
from PyQt6.QtWidgets import (
    QFrame, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QPlainTextEdit, QTextEdit, QSizePolicy,
)

from ui.theme import PALETTE

# Lines of changed content shown before the "+N more" expander kicks in.
DEFAULT_CAP = 12
# Context lines kept around each change hunk in the collapsed preview.
_CONTEXT = 2


def _hex_rgb(h: str) -> tuple[int, int, int]:
    h = (h or "#000000").lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except (ValueError, IndexError):
        return 0, 0, 0


def _blend(fg_hex: str, bg_hex: str, alpha: float) -> str:
    """Blend fg over bg at weight `alpha` (0..1). Qt rich text has unreliable
    rgba(), so we pre-mix to a solid hex — same visual result as an alpha wash."""
    fr, fg, fb = _hex_rgb(fg_hex)
    br, bg, bb = _hex_rgb(bg_hex)
    r = round(fr * alpha + br * (1 - alpha))
    g = round(fg * alpha + bg * (1 - alpha))
    b = round(fb * alpha + bb * (1 - alpha))
    return f"#{r:02x}{g:02x}{b:02x}"


def _esc(text: str) -> str:
    """Escape for HTML and preserve leading indentation (Qt rich text collapses
    runs of spaces otherwise). Trailing content still wraps normally."""
    s = _html.escape(text.rstrip("\n"))
    # Preserve only LEADING whitespace as &nbsp; so indentation reads correctly
    # while long lines remain free to wrap.
    i = 0
    while i < len(s) and s[i] in (" ", "\t"):
        i += 1
    if i:
        lead = s[:i].replace("\t", "    ").replace(" ", "&nbsp;")
        s = lead + s[i:]
    return s or "&nbsp;"


def diff_counts(original: str, current: str) -> tuple[int, int]:
    """(added, removed) line counts between two texts."""
    a = (original or "").splitlines()
    b = (current or "").splitlines()
    adds = dels = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "replace":
            dels += i2 - i1
            adds += j2 - j1
        elif tag == "delete":
            dels += i2 - i1
        elif tag == "insert":
            adds += j2 - j1
    return adds, dels


def _rows(original: str, current: str) -> list[dict]:
    """Unified-diff rows with small context: {kind, old, new, text}.
    kind ∈ {add, del, ctx}. Only hunks near changes are emitted."""
    a = (original or "").splitlines()
    b = (current or "").splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    out: list[dict] = []
    opcodes = sm.get_opcodes()
    for k, (tag, i1, i2, j1, j2) in enumerate(opcodes):
        if tag == "equal":
            # Keep only a few context lines adjacent to a real change.
            seg = list(range(i1, i2))
            head = seg[:_CONTEXT] if k > 0 else []
            tail = seg[-_CONTEXT:] if k < len(opcodes) - 1 else []
            keep = head if head == tail else head + tail
            # Avoid double-adding when the equal run is short.
            seen = set()
            for idx in keep:
                if idx in seen:
                    continue
                seen.add(idx)
                out.append({"kind": "ctx", "old": idx + 1, "new": j1 + (idx - i1) + 1,
                            "text": a[idx]})
            if len(seg) > len(seen):
                out.append({"kind": "gap", "old": None, "new": None, "text": ""})
            continue
        if tag in ("replace", "delete"):
            for idx in range(i1, i2):
                out.append({"kind": "del", "old": idx + 1, "new": None, "text": a[idx]})
        if tag in ("replace", "insert"):
            for idx in range(j1, j2):
                out.append({"kind": "add", "old": None, "new": idx + 1, "text": b[idx]})
    return out


def build_diff_card(path: str, original: str, current: str, *,
                    fs: int = 9, expanded: bool = False,
                    card_id: str = "", cap: int = DEFAULT_CAP) -> tuple[str, int, int]:
    """Return (html, adds, dels) for an in-chat diff card.

    - Titlebar = clickable filename (familiar://openfile) + ± counts.
    - Body = viewer-colored unified diff, capped to `cap` rows unless expanded,
      with a familiar://diffmore expander link when truncated.
    """
    p = PALETTE
    bg = p.get("panel_alt", "#101010")
    add_fg = p.get("accent_bright", p.get("accent", "#33ff99"))
    del_fg = p.get("accent_soft", p.get("muted_text", "#888888"))
    gutter = p.get("muted_text", "#888888")
    text_c = p.get("text", "#dddddd")
    border = p.get("border", "#333333")
    title_c = p.get("accent", "#33ff99")
    add_bg = _blend(add_fg, bg, 0.16)
    del_bg = _blend(del_fg, bg, 0.10)

    rows = _rows(original, current)
    adds, dels = diff_counts(original, current)

    changed_rows = [r for r in rows if r["kind"] in ("add", "del")]
    truncated = (not expanded) and len(changed_rows) > cap

    line_fs = max(fs - 1, 7)
    body_parts: list[str] = []
    shown_changed = 0
    for r in rows:
        kind = r["kind"]
        if kind == "gap":
            body_parts.append(
                f'<div style="color:{gutter};font-size:{line_fs}pt;'
                f'font-family:Consolas;padding:0 6px;opacity:0.5;">⋯</div>'
            )
            continue
        if truncated and kind in ("add", "del") and shown_changed >= cap:
            continue
        if kind in ("add", "del"):
            shown_changed += 1
        if kind == "add":
            row_bg, mark, mark_c, num = add_bg, "+", add_fg, r["new"]
        elif kind == "del":
            row_bg, mark, mark_c, num = del_bg, "−", del_fg, r["old"]
        else:
            row_bg, mark, mark_c, num = "transparent", " ", gutter, r["new"]
        num_str = f"{num:>4}" if num else "    "
        num_str = num_str.replace(" ", "&nbsp;")
        body_parts.append(
            f'<div style="background:{row_bg};font-family:Consolas;'
            f'font-size:{line_fs}pt;padding:0 6px;margin:0;">'
            f'<span style="color:{gutter};">{num_str}</span>'
            f'<span style="color:{mark_c};">&nbsp;{mark}&nbsp;</span>'
            f'<span style="color:{text_c};">{_esc(r["text"])}</span>'
            f'</div>'
        )

    # Expander / collapse link
    more_link = ""
    if truncated:
        remaining = len(changed_rows) - cap
        more_link = (
            f'<div style="padding:2px 8px;">'
            f'<a href="familiar://diffmore?id={quote(card_id)}" '
            f'style="color:{title_c};text-decoration:none;font-size:{line_fs}pt;">'
            f'+{remaining} more changed lines</a></div>'
        )
    elif expanded and len(changed_rows) > cap:
        more_link = (
            f'<div style="padding:2px 8px;">'
            f'<a href="familiar://diffless?id={quote(card_id)}" '
            f'style="color:{gutter};text-decoration:none;font-size:{line_fs}pt;">'
            f'show less</a></div>'
        )

    fname = os.path.basename(path) or path
    enc_path = quote(path)
    counts = (
        f'<span style="color:{add_fg};font-size:{line_fs}pt;">+{adds}</span>'
        f'&nbsp;<span style="color:{del_fg};font-size:{line_fs}pt;">−{dels}</span>'
    )
    titlebar = (
        f'<div style="background:{p.get("panel", bg)};border-bottom:1px solid {border};'
        f'padding:3px 8px;">'
        f'<a href="familiar://openfile?path={enc_path}" '
        f'style="color:{title_c};text-decoration:none;font-weight:bold;'
        f'font-size:{line_fs}pt;">▤ {_html.escape(fname)}</a>'
        f'&nbsp;&nbsp;{counts}'
        f'&nbsp;&nbsp;<span style="color:{gutter};font-size:{max(fs-3,6)}pt;">'
        f'(click to open)</span>'
        f'</div>'
    )

    html = (
        f'<div style="margin:8px auto;max-width:94%;border:1px solid {border};'
        f'border-radius:8px;background:{bg};">'
        f'{titlebar}'
        f'<div style="padding:3px 0;">{"".join(body_parts)}{more_link}</div>'
        f'</div>'
    )
    return html, adds, dels


def compute_rows(original: str, current: str) -> tuple[list[dict], int, int]:
    """Return (rows, adds, dels). Rows are compact (changed lines + small
    context), so storing them on a message meta is cheap — no full file copy."""
    rows = _rows(original or "", current or "")
    adds, dels = diff_counts(original or "", current or "")
    return rows, adds, dels


class DiffCardWidget(QFrame):
    """A self-contained, scrollable diff card for the chat flow (Cursor / Claude
    Code style): a header with the filename (click → open the file's inline diff
    in the viewer) over a read-only, horizontally + vertically scrollable diff
    view. Its own layout — long lines scroll instead of wrapping/running together.

    Renders from precomputed `rows` (see compute_rows) so it never holds full
    file contents and never touches the LLM context.
    """

    open_requested = pyqtSignal(str)
    MAX_BODY_HEIGHT = 300

    def __init__(self, path: str, rows: list[dict], adds: int, dels: int, parent=None):
        super().__init__(parent)
        self._path = path
        p = PALETTE
        self.setObjectName("DiffCard")
        self.setStyleSheet(
            f"QFrame#DiffCard {{ background:{p.get('panel_alt', '#101010')};"
            f" border:1px solid {p.get('border', '#333')}; border-radius:8px; }}")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

        lay.addWidget(self._build_header(path, adds, dels))
        lay.addWidget(self._build_body(rows))

    # ── header ──
    def _build_header(self, path: str, adds: int, dels: int) -> QWidget:
        p = PALETTE
        header = QWidget()
        header.setObjectName("DiffHdr")
        header.setStyleSheet(
            f"QWidget#DiffHdr {{ background:{p.get('panel', '#0c0c0c')};"
            f" border-bottom:1px solid {p.get('border', '#333')};"
            f" border-top-left-radius:8px; border-top-right-radius:8px; }}")
        h = QHBoxLayout(header)
        h.setContentsMargins(8, 3, 8, 3)
        h.setSpacing(8)

        name_btn = QPushButton("▤ " + (os.path.basename(path) or path))
        name_btn.setFlat(True)
        name_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        name_btn.setToolTip(f"{path}\nClick to open this file's inline diff in the viewer")
        name_btn.setStyleSheet(
            f"QPushButton {{ color:{p.get('accent', '#33ff99')}; background:transparent;"
            f" border:none; font:bold 8pt Consolas; text-align:left; padding:0; }}"
            f"QPushButton:hover {{ color:{p.get('glow_hot', p.get('accent', '#aef'))}; }}")
        name_btn.clicked.connect(lambda: self.open_requested.emit(self._path))
        h.addWidget(name_btn)
        h.addStretch(1)

        counts = QLabel(f"+{adds}  −{dels}")
        counts.setStyleSheet(
            f"color:{p.get('muted_text', '#888')}; background:transparent; font:8pt Consolas;")
        h.addWidget(counts)
        return header

    # ── scrollable diff body ──
    def _build_body(self, rows: list[dict]) -> QPlainTextEdit:
        p = PALETTE
        ed = QPlainTextEdit()
        ed.setObjectName("DiffBody")
        ed.setReadOnly(True)
        ed.setFont(QFont("Consolas", 9))
        ed.setFrameShape(QFrame.Shape.NoFrame)
        ed.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)  # → horizontal scroll
        ed.document().setDocumentMargin(4)
        ed.setStyleSheet(self._body_stylesheet(p))

        lines = []
        for r in rows:
            kind = r.get("kind")
            num = r.get("new") or r.get("old")
            ns = f"{num:>5}" if num else "     "
            mark = {"add": "+", "del": "−", "ctx": " "}.get(kind, " ")
            if kind == "gap":
                lines.append("    ⋯")
            else:
                lines.append(f"{ns} {mark} {r.get('text', '')}")
        ed.setPlainText("\n".join(lines))

        # Per-line backgrounds (add/del) via full-width extra selections — same
        # visual language as the file-viewer diff overlay.
        add_bg = QColor(p.get("accent_bright", p.get("accent", "#33ff99")))
        add_bg.setAlpha(38)
        del_bg = QColor(p.get("accent_soft", p.get("muted_text", "#888")))
        del_bg.setAlpha(30)
        doc = ed.document()
        sels = []
        for i, r in enumerate(rows):
            if r.get("kind") not in ("add", "del"):
                continue
            sel = QTextEdit.ExtraSelection()
            sel.cursor = QTextCursor(doc.findBlockByNumber(i))
            sel.format.setBackground(add_bg if r["kind"] == "add" else del_bg)
            sel.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
            sels.append(sel)
        ed.setExtraSelections(sels)

        # Fit height to content up to a cap; beyond that the body scrolls.
        line_h = QFontMetricsF(ed.font()).height()
        wanted = int(line_h * max(1, len(lines)) + 12)
        ed.setMaximumHeight(min(max(wanted, 40), self.MAX_BODY_HEIGHT))
        ed.setMinimumHeight(min(wanted, self.MAX_BODY_HEIGHT))
        self._body = ed
        return ed

    @staticmethod
    def _body_stylesheet(p: dict) -> str:
        bg = p.get("panel_alt", "#101010")
        fg = p.get("text", "#ddd")
        thumb = p.get("accent_muted", p.get("border", "#444"))
        track = p.get("panel", "#0c0c0c")
        return (
            f"QPlainTextEdit#DiffBody {{ background:{bg}; color:{fg}; border:none;"
            f" border-bottom-left-radius:8px; border-bottom-right-radius:8px; }}"
            f"QScrollBar:vertical, QScrollBar:horizontal {{ background:{track}; "
            f"width:9px; height:9px; margin:0; }}"
            f"QScrollBar::handle {{ background:{thumb}; border-radius:0; min-height:24px; min-width:24px; }}"
            f"QScrollBar::add-line, QScrollBar::sub-line {{ width:0; height:0; }}"
            f"QScrollBar::add-page, QScrollBar::sub-page {{ background:transparent; }}"
        )
