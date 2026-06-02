"""
File write tool - create or overwrite files.
Auto-creates parent directories.
"""

import json
from pathlib import Path
from tools.registry import registry


def file_write(path: str, content: str) -> str:
    """Write content to a file, creating parent dirs if needed."""
    # Checkpoint before mutation
    from core.checkpoints import checkpoint_manager
    checkpoint_manager.ensure_checkpoint(str(Path(path).parent), "before file_write")

    p = Path(path)
    existed = p.exists()

    # Capture original for diff before overwriting
    original = ""
    if existed:
        try:
            original = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            original = ""

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return json.dumps({"error": f'Failed to create directory "{p.parent}": {e}'})

    # Verified write — re-reads after writing to catch silent overwrites
    # (file watcher, IDE autosave, hot-reload races) that would otherwise
    # leave us reporting success on a file that drifted.
    from tools.lint import safe_write_text, validate_file
    write_err = safe_write_text(path, content)
    if write_err:
        return json.dumps({"error": write_err})

    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    byte_count = len(content.encode("utf-8"))
    action = "Updated" if existed else "Created"

    # Publish file change event with original content for diff rendering
    from core.event_bus import bus
    bus.emit("file.changed", path=path, tool="file_write",
             original=original, created=not existed)

    from core.sounds import play_edit_sound
    play_edit_sound(path)

    # Combined lint + LSP. Surface errors as a TOP-LEVEL `error`/`diagnostics`
    # field so the model can't gloss past them buried in a status string.
    validation = validate_file(path)
    result = {
        "status": f'{action} "{path}" ({line_count} lines, {byte_count} bytes).',
    }
    diags = validation.get("diagnostics", [])
    errors = [d for d in diags if d.get("severity") == "error"]
    warnings = [d for d in diags if d.get("severity") == "warning"]
    if errors:
        result["error"] = (
            f"Validation failed: {len(errors)} error(s) in the written file. "
            "Re-read the file, fix the issues below, and write again."
        )
        result["diagnostics"] = errors
    elif warnings:
        result["warnings"] = warnings
    if validation.get("semantic_check_ran") is False and Path(path).suffix.lower() in {".py", ".pyi"}:
        # AST parse passed but neither ruff nor pyflakes is installed — tell the
        # model so it doesn't trust "lint clean" too hard for missing imports.
        result["note"] = (
            "Python semantic check skipped (install `ruff` or `pyflakes` to "
            "catch missing imports / undefined names)."
        )
    return json.dumps(result)


registry.register(
    name="file_write",
    description=(
        "Write whole file. Creates parent dirs. Overwrites. ✗ targeted edits → apply_patch. ✗ 3+ files → plan first."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path incl. filename.",
            },
            "content": {
                "type": "string",
                "description": "COMPLETE content. REQUIRED, non-empty.",
            },
        },
        "required": ["path", "content"],
    },
    execute=file_write,
)
