"""
File watcher tool — monitor directories for changes.
Uses polling (cross-platform) rather than OS-specific watchers.
"""

import json
import time
import threading
from pathlib import Path
from tools.registry import registry

_watchers: dict = {}  # id -> {"path", "thread", "stop", "changes"}
_next_id = 1


def file_watch(action: str, path: str = "", patterns: str = "*.py",
               watch_id: int = 0) -> str:
    """Monitor a directory for file changes."""
    global _next_id

    if action == "start":
        if not path:
            return json.dumps({"error": "path required"})

        stop_event = threading.Event()
        changes = []
        wid = _next_id
        _next_id += 1

        def _poll():
            seen = {}
            p = Path(path)
            for pat in patterns.split(","):
                for f in p.rglob(pat.strip()):
                    if f.is_file():
                        seen[str(f)] = f.stat().st_mtime

            while not stop_event.is_set():
                time.sleep(2)
                current = {}
                for pat in patterns.split(","):
                    for f in p.rglob(pat.strip()):
                        if f.is_file():
                            current[str(f)] = f.stat().st_mtime

                for fp, mtime in current.items():
                    if fp not in seen:
                        changes.append({"type": "created", "path": fp})
                    elif mtime != seen[fp]:
                        changes.append({"type": "modified", "path": fp})
                for fp in seen:
                    if fp not in current:
                        changes.append({"type": "deleted", "path": fp})
                seen = current

        t = threading.Thread(target=_poll, daemon=True)
        t.start()
        _watchers[wid] = {"path": path, "thread": t, "stop": stop_event, "changes": changes}
        return json.dumps({"watch_id": wid, "path": path, "patterns": patterns})

    elif action == "check":
        if watch_id not in _watchers:
            return json.dumps({"error": f"No watcher with id {watch_id}"})
        w = _watchers[watch_id]
        recent = list(w["changes"][-20:])
        w["changes"].clear()
        return json.dumps({"watch_id": watch_id, "changes": recent, "count": len(recent)})

    elif action == "stop":
        if watch_id not in _watchers:
            return json.dumps({"error": f"No watcher with id {watch_id}"})
        w = _watchers.pop(watch_id)
        w["stop"].set()
        return json.dumps({"stopped": watch_id})

    elif action == "list":
        return json.dumps({"watchers": [
            {"id": k, "path": v["path"], "pending": len(v["changes"])}
            for k, v in _watchers.items()
        ]})

    else:
        return json.dumps({"error": "action must be: start, check, stop, list"})


registry.register(
    name="file_watch",
    description=(
        "Monitor dir for file changes.\n"
        "- start: begin. check: recent changes. stop: end. list: all watchers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["start", "check", "stop", "list"]},
            "path": {"type": "string", "description": "Dir to watch (start)."},
            "patterns": {"type": "string", "description": "Globs, comma-sep (default *.py)."},
            "watch_id": {"type": "integer", "description": "Watcher ID (check|stop)."},
        },
        "required": ["action"],
    },
    execute=file_watch,
)
