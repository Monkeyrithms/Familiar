"""
Clipboard tool — read/write the system clipboard.
"""

import json
from tools.registry import registry


def clipboard(action: str, text: str = "") -> str:
    """Read or write the system clipboard."""
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if not app:
            return json.dumps({"error": "No QApplication instance"})
        cb = app.clipboard()

        if action == "read":
            content = cb.text()
            return json.dumps({"content": content, "length": len(content)})
        elif action == "write":
            if not text:
                return json.dumps({"error": "text required for write"})
            cb.setText(text)
            return json.dumps({
                "status": "done",
                "message": f"Wrote {len(text)} chars to the system clipboard. "
                           f"No further tool calls — confirm to the user in text.",
                "written": True, "length": len(text),
            })
        else:
            return json.dumps({"error": "action must be 'read' or 'write'"})
    except Exception as e:
        return json.dumps({"error": str(e)})


registry.register(
    name="clipboard",
    description="Read|write system clipboard.",
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "write"], "description": "read | write."},
            "text": {"type": "string", "description": "Text for write."},
        },
        "required": ["action"],
    },
    execute=clipboard,
)
