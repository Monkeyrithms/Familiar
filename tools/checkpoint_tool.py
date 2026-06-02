"""
Checkpoint tool — list, diff, and restore filesystem snapshots.

Snapshots are taken automatically before file mutations.
This tool lets the agent (or user) inspect and roll back changes.
"""

import json
from tools.registry import registry
from core.checkpoints import checkpoint_manager


def checkpoint(action: str, directory: str = "", commit: str = "",
               file_path: str = "") -> str:
    """Manage filesystem checkpoints."""

    if not directory:
        return json.dumps({"error": "directory is required"})

    if action == "list":
        checks = checkpoint_manager.list_checkpoints(directory)
        return json.dumps({
            "directory": directory,
            "checkpoints": checks,
            "count": len(checks),
        }, ensure_ascii=False)

    elif action == "diff":
        if not commit:
            return json.dumps({"error": "commit hash required for diff"})
        result = checkpoint_manager.diff(directory, commit)
        if "error" in result:
            return json.dumps(result)
        return json.dumps({
            "directory": directory,
            "commit": commit,
            "stat": result.get("stat", ""),
            "diff": result.get("diff", "")[:5000],  # Cap diff output
        }, ensure_ascii=False)

    elif action == "restore":
        if not commit:
            return json.dumps({"error": "commit hash required for restore"})
        result = checkpoint_manager.restore(directory, commit, file_path=file_path or None)
        if "error" in result:
            return json.dumps(result)
        return json.dumps(result, ensure_ascii=False)

    else:
        return json.dumps({
            "error": f"Unknown action: {action}. Use: list, diff, restore"
        })


registry.register(
    name="checkpoint",
    description=(
        "Filesystem checkpoints (auto-snapshot before file changes).\n"
        "- list: available snapshots. diff: snapshot vs current. restore: roll back.\n"
        "- Pre-rollback snapshot auto-taken → restores reversible."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "diff", "restore"],
                "description": "Op.",
            },
            "directory": {
                "type": "string",
                "description": "Working dir.",
            },
            "commit": {
                "type": "string",
                "description": "Checkpoint hash (from list) for diff|restore.",
            },
            "file_path": {
                "type": "string",
                "description": "Restore single file (optional).",
            },
        },
        "required": ["action", "directory"],
    },
    execute=checkpoint,
)
