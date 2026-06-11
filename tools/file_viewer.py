"""
File viewer tool — open files in the right-panel viewer for the user to see.
Uses a Qt signal bridge for thread-safe UI interaction.
"""

import json
import os
from PyQt6.QtCore import QObject, pyqtSignal
from tools.registry import registry


class _ViewerBridge(QObject):
    """Thread-safe signal bridge — tool calls emit signals, UI connects slots."""
    # (path, highlight_text_or_empty)
    open_requested = pyqtSignal(str, str)
    refresh_requested = pyqtSignal(str)
    # Fires when the agent edits a file: (path, original_content_or_empty, agent).
    # Empty original means the file was newly created — no diff to show but
    # the viewer still opens/blinks/grabs attention. `agent` is the Agent that
    # made the edit (or None) so the UI can drop the diff card in THAT column's
    # chat — not always the anchor column. Typed `object` to carry the opaque ref.
    edit_notified = pyqtSignal(str, str, object)


bridge = _ViewerBridge()


def notify_file_changed(path: str):
    """Called by agent when file_write/file_edit modifies a file."""
    if path:
        bridge.refresh_requested.emit(os.path.abspath(path))


def notify_edit(path: str, original: str, agent=None):
    """Called by agent after a file mutation tool runs — opens the file in
    the viewer, highlights the diff, and requests attention (blink + expand).
    `agent` identifies the column that made the edit so its diff card lands in
    the right chat pane (the bridge itself is wired once, on the anchor)."""
    if path:
        bridge.edit_notified.emit(os.path.abspath(path), original or "", agent)


def file_show(path: str, highlight: str = "") -> str:
    """Open a file in the right-panel viewer. Optionally pulse-highlight text."""
    if not path:
        return json.dumps({"error": "path is required."})
    if not os.path.isfile(path):
        return json.dumps({"error": f"File not found: {path}"})

    abs_path = os.path.abspath(path)
    hl = (highlight or "").strip()

    # Pre-validate highlight text against file content so the LLM gets
    # honest feedback instead of claiming a highlight that didn't render.
    highlight_found = False
    if hl:
        try:
            content = open(abs_path, "r", encoding="utf-8", errors="replace").read()
            highlight_found = hl.lower() in content.lower()
        except Exception:
            pass

    bridge.open_requested.emit(abs_path, hl if highlight_found else "")

    msg = f"Opened in viewer: {os.path.basename(path)}"
    if hl and highlight_found:
        msg += f" (pulse-highlighting: {hl[:60]!r})"
    elif hl:
        msg += (f" \u2014 WARNING: highlight text not found in file. "
                f"The exact substring {hl[:60]!r} does not appear in "
                f"{os.path.basename(path)}. Read the file first to find "
                f"the correct quote, then retry with the verbatim text.")
    return json.dumps({"status": msg})


registry.register(
    name="file_show",
    description=(
        "Show file in right panel (user sees it). "
        "Don't use file_read for 'show me' - user sees nothing that way. "
        "Supports images|video|audio|PDF|DOCX|code|logs|md|HTML. Auto-reloads on change. "
        "highlight=substring pulses that text in the viewer."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute file path.",
            },
            "highlight": {
                "type": "string",
                "description": (
                    "Optional. Literal substring to pulse-highlight in the "
                    "file. Must appear verbatim in the file content."
                ),
            },
        },
        "required": ["path"],
    },
    execute=file_show,
)
