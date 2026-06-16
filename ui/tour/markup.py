"""Progressive markdown rendering for the tour's typewriters.

Raw markdown syntax (``**bold**``, backticks) must never be visible while a
line is typing. The fix: render the FULL text to HTML once up front, then
reveal it N visible characters at a time — walking the HTML token stream,
keeping every tag, and closing any still-open tags at the cut point so the
partial document is always valid. Bold types as bold from the first letter.

``render_markdown`` is deliberately SELF-CONTAINED — it does the small subset
of markdown the tour uses (bold / italic / inline-code / paragraphs / line
breaks) with plain regex and no third-party dependency. An earlier version
delegated to ``markdown2``; that left bold rendering at the mercy of whatever
markdown2 version happened to be installed, and on some installs the literal
``**asterisks**`` leaked through to the screen. This renderer is identical on
every machine.
"""
from __future__ import annotations

import html as _html
import re

# One visible character is: a tag (zero width), an entity (one char), or a
# single character of text.
_TOKEN = re.compile(r"<[^>]+>|&[a-zA-Z]+;|&#\d+;|.", re.DOTALL)
_TAG_NAME = re.compile(r"</?\s*([a-zA-Z0-9]+)")
_VOID_TAGS = {"br", "hr", "img", "input", "meta", "link"}


def render_markdown(text: str) -> str:
    """Render the tour's markdown subset to HTML — self-contained, no deps.

    Handles ``**bold**``, ``*italic*``, ```code```, blank-line
    paragraphs and single-newline line breaks. Everything else is treated as
    plain (escaped) text, so it can never leak raw syntax to the screen.
    """
    if not text:
        return ""
    s = _html.escape(text)
    # Inline code FIRST, so any * or _ inside backticks isn't treated as emphasis.
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    # Bold before italic (so the ** markers aren't eaten by the single-* rule).
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s, flags=re.DOTALL)
    s = re.sub(r"\*([^*\n]+?)\*", r"<em>\1</em>", s)
    # Blank line → paragraph break; single newline → soft break.
    parts = [p for p in s.split("\n\n") if p.strip()]
    out = "".join("<p>{}</p>".format(p.replace("\n", "<br>")) for p in parts)
    return out or "<p>{}</p>".format(s)


def visible_length(html: str) -> int:
    """How many user-visible characters the HTML contains."""
    return sum(1 for tok in _TOKEN.finditer(html)
               if not tok.group(0).startswith("<"))


def progressive_html(html: str, n_chars: int, caret: str = "") -> str:
    """The first ``n_chars`` visible characters of ``html``, all markup
    preserved, open tags closed, with ``caret`` inserted at the cut."""
    out: list[str] = []
    stack: list[str] = []
    count = 0
    for tok in _TOKEN.finditer(html):
        t = tok.group(0)
        if t.startswith("<"):
            out.append(t)
            m = _TAG_NAME.match(t)
            name = m.group(1).lower() if m else ""
            if t.startswith("</"):
                if stack and stack[-1] == name:
                    stack.pop()
            elif not t.endswith("/>") and name not in _VOID_TAGS:
                stack.append(name)
        else:
            if count >= n_chars:
                break
            out.append(t)
            count += 1
    return "".join(out) + caret + "".join(
        f"</{name}>" for name in reversed(stack))
