"""
Rolling summary QTextEdit helpers: render LLM ``**bold**`` as rich text and
round-trip edits back to markdown-style plain text for storage.
"""

from __future__ import annotations

import html
import re

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QTextEdit

_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)


def markdown_bold_to_html(plain: str, *, fg: str) -> str:
    """HTML for QTextEdit: escape content, ``**segment**`` → <b>, newlines → <br/>."""
    esc = html.escape(plain or "")
    body = _MD_BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", esc)
    body = body.replace("\n", "<br/>")
    return (
        f'<div style="font-family: Consolas, Monaco, monospace; font-size: 8pt; '
        f'color: {fg};">{body}</div>'
    )


def set_rolling_summary_content(te: QTextEdit, plain_markdown: str | None, *, fg: str) -> None:
    """Load summary text with **bold** rendered; empty string clears for placeholder."""
    te.setAcceptRichText(True)
    text = (plain_markdown or "").strip()
    if not text:
        te.clear()
        return
    te.setHtml(markdown_bold_to_html(plain_markdown or "", fg=fg))


def rolling_summary_plain_from_edit(te: QTextEdit) -> str:
    """Serialize QTextEdit content to plain text with ``**bold**`` markers."""
    doc = te.document()
    lines: list[str] = []
    block = doc.firstBlock()
    while block.isValid():
        parts: list[str] = []
        it = block.begin()
        while it != block.end():
            frag = it.fragment()
            if frag.isValid():
                t = frag.text()
                fmt = frag.charFormat()
                if fmt.fontWeight() == QFont.Weight.Bold:
                    parts.append(f"**{t}**")
                else:
                    parts.append(t)
            it += 1
        lines.append("".join(parts))
        block = block.next()
    return "\n".join(lines)
