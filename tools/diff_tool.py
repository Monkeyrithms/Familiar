"""
Diff/patch tool — generate and apply unified diffs.

`apply` is hunk-aware: it parses each `@@ -a,b +c,d @@` header, locates the
hunk by matching its context/removed lines against the file (searching outward
from the header's line hint, so it tolerates drift), and refuses with an error
rather than silently corrupting the file when a hunk's context can't be found.
"""

import json
import re
import difflib
from pathlib import Path
from tools.registry import registry
from tools._json import dumps as _fast_dumps

_HUNK_HDR = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class _Hunk:
    __slots__ = ("old_start", "lines")

    def __init__(self, old_start: int):
        self.old_start = old_start          # 1-based start in the original
        self.lines: list[tuple[str, str]] = []  # (tag, text); tag in ' -+'


def _parse_hunks(patch: str) -> list[_Hunk]:
    """Parse a unified diff into a list of hunks."""
    hunks: list[_Hunk] = []
    cur: _Hunk | None = None
    for line in patch.splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        m = _HUNK_HDR.match(line)
        if m:
            cur = _Hunk(int(m.group(1)))
            hunks.append(cur)
            continue
        if cur is None:
            continue  # preamble before the first hunk
        if line == "":
            cur.lines.append((" ", ""))      # blank context line
            continue
        tag = line[0]
        if tag in (" ", "+", "-"):
            cur.lines.append((tag, line[1:]))
        # '\' ("No newline at end of file") and anything else: ignore.
    return hunks


def _locate_hunk(orig: list[str], hunk: _Hunk, min_idx: int) -> int:
    """Find the 0-based index in *orig* where this hunk's old block starts.

    Searches outward from the header's line hint so a stale `@@` offset still
    applies cleanly. Raises ValueError when the context genuinely isn't there.
    """
    old_block = [text for tag, text in hunk.lines if tag in (" ", "-")]
    hint = hunk.old_start - 1
    if not old_block:
        # Pure insertion — clamp the header position into the valid range.
        return min(max(hint, min_idx), len(orig))

    radius = max(len(orig), 1)
    for offset in range(0, radius + 1):
        for c in ({hint + offset, hint - offset} if offset else {hint}):
            if c < min_idx or c < 0 or c + len(old_block) > len(orig):
                continue
            if all(orig[c + k] == old_block[k] for k in range(len(old_block))):
                return c
    raise ValueError(
        f"hunk context not found near line {hunk.old_start} "
        f"(the file no longer matches the patch)"
    )


def _apply_unified_diff(original: str, patch: str) -> str:
    """Apply *patch* (a unified diff) to *original*, returning the new text."""
    hunks = _parse_hunks(patch)
    if not hunks:
        raise ValueError("no hunks (@@ headers) found in patch")

    orig = original.splitlines()
    result: list[str] = []
    src = 0  # 0-based cursor into orig

    for hunk in hunks:
        start = _locate_hunk(orig, hunk, src)
        result.extend(orig[src:start])  # untouched lines before the hunk
        src = start
        for tag, text in hunk.lines:
            if tag == " ":
                if src >= len(orig) or orig[src] != text:
                    raise ValueError(
                        f"context mismatch at line {src + 1} (expected {text!r})")
                result.append(orig[src])
                src += 1
            elif tag == "-":
                if src >= len(orig) or orig[src] != text:
                    raise ValueError(
                        f"removed line not found at line {src + 1} (expected {text!r})")
                src += 1  # drop it
            else:  # '+'
                result.append(text)

    result.extend(orig[src:])  # trailing unchanged lines

    new_text = "\n".join(result)
    if original.endswith("\n") and (new_text or result):
        new_text += "\n"
    return new_text


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
            return _fast_dumps({"diff": "".join(diff)})
        except Exception as e:
            return json.dumps({"error": str(e)})

    elif action == "diff_strings":
        a = (content1 or "").splitlines(keepends=True)
        b = (content2 or "").splitlines(keepends=True)
        diff = difflib.unified_diff(a, b, fromfile="before", tofile="after")
        return _fast_dumps({"diff": "".join(diff)})

    elif action == "apply":
        if not path or not patch:
            return json.dumps({"error": "path and patch required"})
        try:
            original = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return json.dumps({"error": f"Could not read {path}: {e}"})
        try:
            updated = _apply_unified_diff(original, patch)
        except ValueError as e:
            # Refuse rather than write a corrupted file.
            return json.dumps({"error": f"Patch did not apply: {e}"})
        except Exception as e:
            return json.dumps({"error": f"Patch failed: {e}"})
        try:
            Path(path).write_text(updated, encoding="utf-8")
        except Exception as e:
            return json.dumps({"error": f"Could not write {path}: {e}"})
        return json.dumps({
            "applied": True,
            "path": path,
            "lines": len(updated.splitlines()),
        })

    else:
        return json.dumps({"error": "action must be: diff_files, diff_strings, apply"})


registry.register(
    name="diff",
    description=(
        "Generate/apply unified diffs. "
        "diff_files: 2 files | diff_strings: 2 strings | apply: patch → file.\n"
        "- apply is hunk-aware (honors @@ offsets); refuses on context mismatch."
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
