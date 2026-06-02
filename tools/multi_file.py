"""
Multi-file write tool — create/update multiple files in one call.
"""

import json
from pathlib import Path
from tools.registry import registry


def multi_file_write(files: list) -> str:
    """Write multiple files at once. Each entry: {path, content}."""
    if not files:
        return json.dumps({"error": "files list required"})

    from core.checkpoints import checkpoint_manager

    results = []
    for entry in files:
        path = entry.get("path", "")
        content = entry.get("content", "")
        if not path or not content:
            results.append({"path": path, "error": "path and content required"})
            continue

        # Checkpoint before first write
        checkpoint_manager.ensure_checkpoint(str(Path(path).parent), "before multi_file_write")

        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            existed = p.exists()
            p.write_text(content, encoding="utf-8")
            results.append({
                "path": path,
                "status": "updated" if existed else "created",
                "lines": content.count("\n") + 1,
            })
        except Exception as e:
            results.append({"path": path, "error": str(e)})

    ok = sum(1 for r in results if "error" not in r)
    return json.dumps({"written": ok, "total": len(files), "results": results})


registry.register(
    name="multi_file_write",
    description=(
        "Write multiple files in 1 call. Each entry: path+content. ✓ project scaffolding (faster than sequential file_write)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
                "description": "{path,content} list.",
            },
        },
        "required": ["files"],
    },
    execute=multi_file_write,
)
