"""
Multi-edit tool — apply a sequence of edits to one file atomically.

Each edit reuses file_edit's 6-strategy fuzzy-match cascade, but they're
applied in sequence to the IN-MEMORY buffer (not written between steps).
The file is written once at the end if all edits succeed. If any edit
fails, nothing is written and the tool returns which edit broke.

This is the ergonomic win over calling file_edit N times:
  - Single tool call, single diff event, single file_viewer refresh
  - No risk of earlier edits invalidating later ones mid-batch
  - Atomic: either the whole batch lands or none of it does
"""

import json
from pathlib import Path
from tools.registry import registry

# Reuse file_edit's cascade so behavior stays identical for each hunk
from tools.file_edit import _REPLACERS


def _apply_one(content: str, old_string: str, new_string: str,
               replace_all: bool) -> tuple[str | None, str | None, str]:
    """Apply a single edit to *content*. Returns (new_content, strategy_used, error_reason).

    On success: (updated_content, strategy_name, "").
    On failure: (None, None, reason).
    """
    if not old_string:
        return None, None, "old_string is empty"

    not_found = True
    multiple_found = False
    for replacer in _REPLACERS:
        for candidate in replacer(content, old_string):
            idx = content.find(candidate)
            if idx == -1:
                continue
            not_found = False
            if replace_all:
                count = content.count(candidate)
                if count == 0:
                    continue
                updated = content.replace(candidate, new_string)
                return updated, replacer.__name__, ""
            # Single replacement — require uniqueness
            last = content.rfind(candidate)
            if idx != last:
                multiple_found = True
                continue
            updated = content[:idx] + new_string + content[idx + len(candidate):]
            return updated, replacer.__name__, ""

    if not_found:
        return None, None, "old_string not found (tried all 6 fuzzy strategies)"
    if multiple_found:
        return None, None, ("old_string matches multiple locations — make it more "
                            "specific, or set replace_all=true")
    return None, None, "could not resolve a unique match"


def multi_edit(path: str, edits: list) -> str:
    """Apply a list of edits to a single file atomically."""
    if not edits:
        return json.dumps({"error": "edits list required"})

    p = Path(path)
    try:
        original = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return json.dumps({"error": f'Could not read "{path}": {e}'})

    # Checkpoint before mutation
    from core.checkpoints import checkpoint_manager
    checkpoint_manager.ensure_checkpoint(str(p.parent), "before multi_edit")

    # Detect CRLF once
    uses_crlf = "\r\n" in original
    working = original.replace("\r\n", "\n") if uses_crlf else original

    applied = []
    for i, edit in enumerate(edits):
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        replace_all = bool(edit.get("replace_all", False))
        if uses_crlf:
            old = old.replace("\r\n", "\n")
            new = new.replace("\r\n", "\n")

        updated, strategy, err = _apply_one(working, old, new, replace_all)
        if err:
            return json.dumps({
                "error": f"Edit #{i + 1} failed: {err}",
                "edits_applied_before_failure": len(applied),
                "file_unchanged": True,
            })
        working = updated
        applied.append({"index": i + 1, "strategy": strategy})

    if uses_crlf:
        working = working.replace("\n", "\r\n")

    from tools.lint import safe_write_text, validate_file
    write_err = safe_write_text(path, working)
    if write_err:
        return json.dumps({"error": write_err})

    # Emit single file.changed event — file viewer will pick up the diff
    from core.event_bus import bus
    bus.emit("file.changed", path=path, tool="multi_edit", original=original)

    from core.sounds import play_edit_sound
    play_edit_sound(path)

    # Combined lint + LSP. Surface errors as a top-level `error`/`diagnostics`
    # field so the model can't gloss past them buried in a status string.
    validation = validate_file(path)
    result: dict = {
        "status": f'Applied {len(applied)} edit(s) to "{path}".',
        "edits": applied,
    }
    diags = validation.get("diagnostics", [])
    errors = [d for d in diags if d.get("severity") == "error"]
    warnings = [d for d in diags if d.get("severity") == "warning"]
    if errors:
        result["error"] = (
            f"Validation failed: {len(errors)} error(s) in the edited file. "
            "Re-read the file, fix the issues below, and edit again."
        )
        result["diagnostics"] = errors
    elif warnings:
        result["warnings"] = warnings
    if validation.get("semantic_check_ran") is False and p.suffix.lower() in {".py", ".pyi"}:
        result["note"] = (
            "Python semantic check skipped (install `ruff` or `pyflakes` to "
            "catch missing imports / undefined names)."
        )
    return json.dumps(result)


registry.register(
    name="multi_edit",
    description=(
        "Atomic sequence of edits to ONE file. Each edit: {old_string, new_string, replace_all?}. "
        "Same 6-strategy fuzzy cascade as file_edit. Any fail \u2192 nothing written. "
        "Prefer over multiple file_edit calls for same file: atomic, one diff emit, \u2717 prior-edit invalidation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path to the file to edit."},
            "edits": {
                "type": "array",
                "description": "Ordered list of edits to apply.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean", "description": "Default false."},
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        "required": ["path", "edits"],
    },
    execute=multi_edit,
)
