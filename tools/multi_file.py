"""
Multi-file write tool — create/update multiple files in one call.

Hardened against the argument shapes a model actually emits when it's under
load (many files, large content). The clean contract is
``files=[{path, content}, ...]``, but the same payload frequently arrives as:
  • a JSON STRING (the array never got parsed into a list),
  • a single {path, content} dict (model dropped the wrapping list),
  • top-level path=/content= kwargs (model collapsed a 1-file call),
  • files wrapped under another key, or nested one level deeper.
Rather than crash on these near-misses, we coerce them to the canonical shape.
A genuinely empty call returns an ACTIONABLE error so the caller can fall back
to single-file writes instead of hitting a dead end.
"""

import json
from pathlib import Path
from tools.registry import registry


def _coerce_files(files, kwargs) -> tuple[list, str | None]:
    """Best-effort normalize whatever arrived into a list of {path, content}.
    Returns (entries, note). note is a non-fatal explanation of what we fixed."""
    note = None

    # files came as a JSON string — parse it.
    if isinstance(files, str):
        s = files.strip()
        if s:
            try:
                files = json.loads(s)
                note = "files arrived as a JSON string; parsed it"
            except json.JSONDecodeError:
                return [], "files was a string but not valid JSON"

    # files is a dict — either a single {path,content} entry, or a wrapper.
    if isinstance(files, dict):
        if "path" in files and "content" in files:
            return [files], "single file dict wrapped into a list"
        # wrapper like {"files":[...]} or {"0":{...},"1":{...}}
        inner = files.get("files") if "files" in files else None
        if isinstance(inner, (list, str, dict)):
            return _coerce_files(inner, {})
        vals = list(files.values())
        if vals and all(isinstance(v, dict) for v in vals):
            return vals, "files dict-of-entries flattened into a list"

    if isinstance(files, list):
        return files, note

    # No usable `files` — try to salvage a top-level single-file call.
    if not files:
        path = kwargs.get("path")
        content = kwargs.get("content")
        if path and content is not None:
            return [{"path": path, "content": content}], \
                "recovered a single file from top-level path/content"

    return [], note


def multi_file_write(files=None, **kwargs) -> str:
    """Write multiple files at once. Each entry: {path, content}."""
    entries, note = _coerce_files(files, kwargs)

    if not entries:
        return json.dumps({
            "error": "No files to write.",
            "reason": note or "the 'files' array arrived empty or missing — "
                              "large nested arguments are often dropped in transit.",
            "recover": "Retry with a SMALLER batch (2-3 files), or write each "
                       "file with file_write individually. Expected shape: "
                       "files=[{\"path\":\"...\",\"content\":\"...\"}, ...].",
        })

    from core.checkpoints import checkpoint_manager

    results = []
    checkpointed = set()
    for entry in entries:
        if not isinstance(entry, dict):
            results.append({"error": f"entry not an object: {entry!r}"})
            continue
        path = entry.get("path", "")
        content = entry.get("content", "")
        if not path:
            results.append({"path": path, "error": "path required"})
            continue
        if content == "":
            # Allow intentional empty files but flag it — empty content is also
            # a common symptom of a dropped argument, so make it visible.
            results.append({"path": path, "error": "content empty — pass "
                            "non-empty content, or use file_write for an "
                            "intentionally blank file"})
            continue

        try:
            p = Path(path)
            parent = str(p.parent)
            if parent not in checkpointed:
                checkpoint_manager.ensure_checkpoint(parent, "before multi_file_write")
                checkpointed.add(parent)
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
    out = {"written": ok, "total": len(entries), "results": results}
    if note:
        out["note"] = note
    if ok < len(entries):
        out["partial"] = f"{len(entries) - ok} file(s) failed — see results"
    return json.dumps(out)


registry.register(
    name="multi_file_write",
    description=(
        "Write multiple files in 1 call. Each entry: path+content. ✓ project "
        "scaffolding (faster than sequential file_write). For 5+ large files, "
        "prefer 2-3 per call — very large nested arguments can be dropped."
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
