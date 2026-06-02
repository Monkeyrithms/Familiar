"""
Tool-call metadata popup — opened when the user clicks a tool chip in chat.

Shows the on-disk record from core.tool_call_meta: what the tool was called with
and what it returned. This is detail the LLM is NOT crammed with — it lives only
on disk (last N days) for the user's reference. ask_user_question gets a tailored
Q&A view; everything else shows args + result.
"""

import html as _html
import json
import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtWidgets import QLabel, QTextEdit, QScrollArea, QWidget, QVBoxLayout

from ui.glass_dialog import GlassDialog
from ui.theme import PALETTE, selection_css

# Edit tools whose call we render as a green/red-style diff instead of raw args.
EDIT_TOOLS = {"file_edit", "multi_edit", "apply_patch", "file_write"}
_DIFF_MAX_LINES = 600  # cap so a huge edit can't make the dialog enormous


def _blend_hex(fg: str, bg: str, t: float) -> str:
    """Solid hex of fg over bg at fraction t — Qt rich-text <td bgcolor> wants a
    solid color (no rgba), so we pre-blend the translucent overlay."""
    a, b = QColor(fg), QColor(bg)
    if not a.isValid():
        a = QColor("#888888")
    if not b.isValid():
        b = QColor("#111111")
    return QColor(
        int(a.red() * t + b.red() * (1 - t)),
        int(a.green() * t + b.green() * (1 - t)),
        int(a.blue() * t + b.blue() * (1 - t)),
    ).name()


def _diff_lines_from_strings(old: str, new: str) -> list[tuple[str, str]]:
    """(kind, text) rows from old→new via difflib. kind ∈ add|del|ctx."""
    import difflib
    o, n = (old or "").splitlines(), (new or "").splitlines()
    out: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=o, b=n, autojunk=False).get_opcodes():
        if tag == "equal":
            for ln in n[j1:j2]:
                out.append(("ctx", ln))
        elif tag == "insert":
            for ln in n[j1:j2]:
                out.append(("add", ln))
        elif tag == "delete":
            for ln in o[i1:i2]:
                out.append(("del", ln))
        elif tag == "replace":
            for ln in o[i1:i2]:
                out.append(("del", ln))
            for ln in n[j1:j2]:
                out.append(("add", ln))
    return out


def _diff_lines_from_patch(patch: str) -> list[tuple[str, str]]:
    """(kind, text) rows by reading a +/- patch body directly. kind adds hdr."""
    out: list[tuple[str, str]] = []
    for line in (patch or "").splitlines():
        if line.startswith(("+++", "---")):
            out.append(("hdr", line))
        elif line.startswith(("@@", "*** ", "Index:", "===")):
            out.append(("hdr", line))
        elif line.startswith("+"):
            out.append(("add", line[1:]))
        elif line.startswith("-"):
            out.append(("del", line[1:]))
        else:
            out.append(("ctx", line[1:] if line.startswith(" ") else line))
    return out


def _build_diff_blocks(tool_name: str, args: dict) -> list[tuple[str, list]]:
    """Return [(label, [(kind, text), ...]), ...] for an edit tool's call."""
    blocks: list[tuple[str, list]] = []
    if tool_name == "file_edit":
        blocks.append((args.get("path", ""),
                       _diff_lines_from_strings(args.get("old_string", ""),
                                                args.get("new_string", ""))))
    elif tool_name == "multi_edit":
        edits = args.get("edits") or args.get("items") or []
        for i, e in enumerate(edits):
            if not isinstance(e, dict):
                continue
            blocks.append((f"edit {i + 1}",
                           _diff_lines_from_strings(e.get("old_string", ""),
                                                    e.get("new_string", ""))))
    elif tool_name == "apply_patch":
        blocks.append(("patch", _diff_lines_from_patch(args.get("patch", ""))))
    elif tool_name == "file_write":
        content = args.get("content", "")
        blocks.append((args.get("path", "") + "  (new file content)",
                       [("add", ln) for ln in content.splitlines()]))
    return blocks


def show_tool_meta(parent, tool_name: str):
    """Look up the latest stored call for tool_name and show it. If nothing is
    stored, show a short 'no details' note rather than failing silently."""
    from core import tool_call_meta
    rec = tool_call_meta.get_latest_by_name(tool_name)
    dlg = ToolMetaDialog(tool_name, rec, parent=parent)
    dlg.exec()


class ToolMetaDialog(GlassDialog):
    def __init__(self, tool_name: str, rec: dict | None, parent=None):
        super().__init__(title=f"Tool · {tool_name}", parent=parent,
                         width=560, height=520)
        p = PALETTE
        lay = self.content_layout()

        if not rec:
            note = QLabel(
                f"No stored details for “{tool_name}”.\n\n"
                "Metadata is kept for recent calls only; this one may have aged "
                "out or happened before metadata recording was enabled.")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {p['muted_text']};")
            lay.addWidget(note)
            return

        # Header: tool name + when
        ts = rec.get("ts")
        when = ""
        if ts:
            try:
                when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except Exception:
                when = ""
        head = QLabel(f"<b>{tool_name}</b>" + (f"  ·  {when}" if when else ""))
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setStyleSheet(f"color: {p['accent']};")
        lay.addWidget(head)

        # Scrollable body of labeled sections
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        inner_lay = QVBoxLayout(inner)
        inner_lay.setContentsMargins(0, 0, 0, 0)
        inner_lay.setSpacing(10)

        if tool_name == "ask_user_question":
            self._build_qa_view(inner_lay, rec)
        else:
            self._build_generic_view(inner_lay, rec, tool_name)

        inner_lay.addStretch()
        scroll.setWidget(inner)
        lay.addWidget(scroll, stretch=1)

    # ── section helpers ──────────────────────────────────────────────

    def _section(self, layout, title: str, body: str, mono: bool = True):
        p = PALETTE
        lbl = QLabel(title)
        lbl.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {p['accent_muted']};")
        layout.addWidget(lbl)

        box = QTextEdit()
        box.setReadOnly(True)
        box.setPlainText(body if body else "(empty)")
        box.setStyleSheet(
            f"background: {p['panel_alt']}; color: {p['text']}; "
            f"border: 1px solid {p['border']}; padding: 6px; "
            f"font-family: Consolas, monospace; font-size: 10pt; {selection_css()}")
        # Size to content-ish, capped.
        box.setMinimumHeight(40)
        box.setMaximumHeight(220)
        layout.addWidget(box)

    def _build_qa_view(self, layout, rec: dict):
        """Tailored view: each question and the answer the user gave."""
        p = PALETTE
        args = rec.get("args") or {}
        questions = args.get("questions") or []
        answers = {}
        try:
            parsed = json.loads(rec.get("result") or "{}")
            answers = parsed.get("answers") or {}
            status = parsed.get("status", "")
        except Exception:
            status = ""

        if status == "cancelled" or (not answers and not questions):
            note = QLabel("The user dismissed this question without answering.")
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {p['muted_text']};")
            layout.addWidget(note)
            if not questions:
                return

        for q in questions:
            qtext = q.get("question", "")
            opts = [o.get("label", "") if isinstance(o, dict) else str(o)
                    for o in (q.get("options") or [])]
            ans = answers.get(qtext, "—")
            if isinstance(ans, list):
                ans = ", ".join(str(a) for a in ans)
            body = f"Answer:  {ans}"
            if opts:
                body += "\n\nOptions offered:\n" + "\n".join(f"  • {o}" for o in opts)
            self._section(layout, qtext or "(question)", body)

    def _diff_section(self, layout, blocks: list):
        """Render edit-tool diffs as colored rows (added = bright accent, removed
        = dim), mirroring the file viewer. Uses an HTML table so Qt rich text
        fills the full row width."""
        p = PALETTE
        add_bg = _blend_hex(p.get("accent_bright", p["accent"]), p["panel_alt"], 0.32)
        del_bg = _blend_hex(p.get("muted_text", "#888"), p["panel_alt"], 0.20)
        hdr_col = p.get("accent_muted", p["accent"])
        ctx_col = p.get("text", "#ddd")
        gutter_col = p.get("muted_text", "#888")

        total = sum(len(rows) for _, rows in blocks)
        budget = _DIFF_MAX_LINES

        for label, rows in blocks:
            lbl = QLabel(f"DIFF · {label}" if label else "DIFF")
            lbl.setFont(QFont("Consolas", 8, QFont.Weight.Bold))
            lbl.setStyleSheet(f"color: {p['accent_muted']};")
            layout.addWidget(lbl)

            parts = ['<table cellspacing="0" cellpadding="1" width="100%" '
                     'style="font-family:Consolas,monospace;font-size:10pt;">']
            shown = 0
            for kind, text in rows:
                if budget <= 0:
                    break
                budget -= 1
                shown += 1
                safe = _html.escape(text).replace(" ", "&nbsp;") or "&nbsp;"
                if kind == "add":
                    bg, gut, col = add_bg, "+", ctx_col
                elif kind == "del":
                    bg, gut, col = del_bg, "−", ctx_col
                elif kind == "hdr":
                    bg, gut, col = p["panel_alt"], "&nbsp;", hdr_col
                else:
                    bg, gut, col = p["panel_alt"], "&nbsp;", ctx_col
                parts.append(
                    f'<tr><td bgcolor="{bg}" width="18" '
                    f'style="color:{gutter_col};">{gut}</td>'
                    f'<td bgcolor="{bg}" style="color:{col};">{safe}</td></tr>')
            parts.append("</table>")

            box = QTextEdit()
            box.setReadOnly(True)
            box.setHtml("".join(parts))
            box.setStyleSheet(
                f"background: {p['panel_alt']}; border: 1px solid {p['border']}; "
                f"padding: 2px; {selection_css()}")
            box.setMinimumHeight(40)
            box.setMaximumHeight(320)
            layout.addWidget(box)

        if budget <= 0 and total > _DIFF_MAX_LINES:
            more = QLabel(f"… diff truncated ({total - _DIFF_MAX_LINES:,} more lines)")
            more.setStyleSheet(f"color: {p['muted_text']};")
            layout.addWidget(more)

    def _build_generic_view(self, layout, rec: dict, tool_name: str = ""):
        args = rec.get("args") or {}
        tool_name = tool_name or rec.get("name") or rec.get("tool") or ""

        # Edit tools → show a green/red-style diff built from the call args,
        # the same visual format as the file viewer.
        if tool_name in EDIT_TOOLS:
            try:
                blocks = _build_diff_blocks(tool_name, args)
                if any(rows for _, rows in blocks):
                    self._diff_section(layout, blocks)
            except Exception as e:
                print(f"[tool_meta] diff render failed: {e}")

        try:
            args_str = json.dumps(args, indent=2, ensure_ascii=False)
        except Exception:
            args_str = str(args)
        self._section(layout, "ARGUMENTS", args_str)

        result = rec.get("result") or ""
        # Pretty-print JSON results when possible.
        try:
            parsed = json.loads(result)
            result = json.dumps(parsed, indent=2, ensure_ascii=False)
        except Exception:
            pass
        self._section(layout, "RESULT", result)
