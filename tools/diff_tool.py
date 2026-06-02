"""
Diff/patch tool — generate and apply unified diffs.
"""

import json
import difflib
from pathlib import Path
from tools.registry import registry


def diff_patch(action: str, path: str = "", path2: str = "",
               patch: str = "", content1: str = "", content2: str = "") -> str:
    """Generate or apply unified diffs."""

    if action == "diff_files":
        if not path or not path2:
            return json.dumps({"error": "path and path2 required"})
        try:
            a = Path(path).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            b = Path(path2).read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            diff = difflib.unified_diff(a, b, fromfile=path, tofile=path2)
            return json.dumps({"diff": "".join(diff)}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "diff_strings":
        a = (content1 or "").splitlines(keepends=True)
        b = (content2 or "").splitlines(keepends=True)
        diff = difflib.unified_diff(a, b, fromfile="before", tofile="after")
        return json.dumps({"diff": "".join(diff)}, ensure_ascii=False)

    elif action == "apply":
        if not path or not patch:
            return json.dumps({"error": "path and patch required"})
        try:
            original = Path(path).read_text(encoding="utf-8", errors="replace")
            # Simple patch application — parse unified diff
            lines = original.splitlines(keepends=True)
            patch_lines = patch.splitlines(keepends=True)
            result_lines = []
            orig_idx = 0

            for pl in patch_lines:
                if pl.startswith("---") or pl.startswith("+++") or pl.startswith("@@"):
                    continue
                if pl.startswith("-"):
                    orig_idx += 1  # skip removed line
                elif pl.startswith("+"):
                    result_lines.append(pl[1:])  # add new line
                else:
                    if orig_idx < len(lines):
                        result_lines.append(lines[orig_idx])
                    orig_idx += 1

            # Append remaining original lines
            while orig_idx < len(lines):
                result_lines.append(lines[orig_idx])
                orig_idx += 1

            Path(path).write_text("".join(result_lines), encoding="utf-8")
            return json.dumps({"applied": True, "path": path, "lines": len(result_lines)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    else:
        return json.dumps({"error": "action must be: diff_files, diff_strings, apply"})


registry.register(
    name="diff",
    description=(
        "Generate/apply unified diffs. "
        "diff_files: 2 files | diff_strings: 2 strings | apply: patch → file."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["diff_files", "diff_strings", "apply"]},
            "path": {"type": "string", "description": "File path (diff_files|apply)."},
            "path2": {"type": "string", "description": "2nd file (diff_files)."},
            "patch": {"type": "string", "description": "Unified diff (apply)."},
            "content1": {"type": "string", "description": "1st string (diff_strings)."},
            "content2": {"type": "string", "description": "2nd string (diff_strings)."},
        },
        "required": ["action"],
    },
    execute=diff_patch,
)
