"""
apply_patch — structured multi-file patch tool.

Format (inspired by OpenAI Codex's apply_patch):

    *** Begin Patch
    *** Add File: path/to/new.py
    +line one
    +line two
    *** Update File: path/to/existing.py
    @@ class Foo
    @@     def bar(self):
     context line
    -old line
    +new line
     more context
    *** Delete File: path/to/old.py
    *** End Patch

- Add File: body lines must be prefixed with "+" (the "+" is stripped).
- Delete File: body must be empty.
- Update File: one or more hunks. Hunk lines start with " " (context),
  "-" (remove), or "+" (add). Hunks can be anchored with one or more
  "@@ <anchor>" lines — useful when context is ambiguous (e.g. the
  same method shows up in two classes). Anchors must literally exist
  on successive non-consecutive lines in the file.
- Update File can take an optional "*** Move to: new/path" line
  immediately after the header to rename while editing.

Why this instead of file_edit? file_edit is great for a single targeted
replacement per file. apply_patch lets a single tool call mutate several
files (add, edit, delete, rename) atomically-ish, with richer context
so the match is less brittle on large edits.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from tools.registry import registry


# ── Data model ──────────────────────────────────────────────────────────

@dataclass
class Hunk:
    anchors: list[str] = field(default_factory=list)
    # Each line is (op, text) where op in {" ", "-", "+"}.
    lines: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Op:
    kind: str          # "add" | "delete" | "update"
    path: str
    move_to: str | None = None
    body_add: list[str] | None = None   # for kind == "add"
    hunks: list[Hunk] | None = None     # for kind == "update"


# ── Parser ──────────────────────────────────────────────────────────────

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_HDR_ADD = re.compile(r"^\*\*\*\s+Add File:\s+(.+?)\s*$")
_HDR_DEL = re.compile(r"^\*\*\*\s+Delete File:\s+(.+?)\s*$")
_HDR_UPD = re.compile(r"^\*\*\*\s+Update File:\s+(.+?)\s*$")
_HDR_MOVE = re.compile(r"^\*\*\*\s+Move to:\s+(.+?)\s*$")


class PatchError(ValueError):
    pass


def parse_patch(text: str) -> list[Op]:
    """Parse a patch envelope into a list of operations.

    Raises PatchError on malformed input — the tool returns the error
    text verbatim so the model can retry with a fix.
    """
    if not text or _BEGIN not in text:
        raise PatchError(f"Patch must start with '{_BEGIN}'.")
    lines = text.splitlines()
    # Trim to body between Begin/End
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == _BEGIN) + 1
    except StopIteration:
        raise PatchError(f"Missing '{_BEGIN}'.")
    try:
        end = next(i for i, ln in enumerate(lines) if ln.strip() == _END)
    except StopIteration:
        raise PatchError(f"Missing '{_END}'.")
    body = lines[start:end]

    ops: list[Op] = []
    i = 0
    while i < len(body):
        ln = body[i]
        m_add = _HDR_ADD.match(ln)
        m_del = _HDR_DEL.match(ln)
        m_upd = _HDR_UPD.match(ln)
        if m_add:
            path = m_add.group(1).strip()
            i += 1
            content: list[str] = []
            while i < len(body) and not body[i].lstrip().startswith("*** "):
                row = body[i]
                if not row.startswith("+"):
                    raise PatchError(
                        f"Add File body lines must start with '+', got: {row!r}")
                content.append(row[1:])
                i += 1
            ops.append(Op(kind="add", path=path, body_add=content))
            continue
        if m_del:
            path = m_del.group(1).strip()
            i += 1
            # Body must be empty; tolerate blank lines
            while i < len(body) and not body[i].lstrip().startswith("*** "):
                if body[i].strip() != "":
                    raise PatchError(
                        f"Delete File body must be empty, got: {body[i]!r}")
                i += 1
            ops.append(Op(kind="delete", path=path))
            continue
        if m_upd:
            path = m_upd.group(1).strip()
            i += 1
            move_to: str | None = None
            if i < len(body):
                mv = _HDR_MOVE.match(body[i])
                if mv:
                    move_to = mv.group(1).strip()
                    i += 1
            hunks: list[Hunk] = []
            current: Hunk | None = None
            while i < len(body) and not body[i].lstrip().startswith("*** "):
                row = body[i]
                if row.startswith("@@"):
                    anchor = row[2:].strip()
                    if current is None or current.lines:
                        current = Hunk()
                        hunks.append(current)
                    current.anchors.append(anchor)
                    i += 1
                    continue
                if row == "":
                    # Treat blank lines as context (empty) rather than ending
                    if current is None:
                        current = Hunk()
                        hunks.append(current)
                    current.lines.append((" ", ""))
                    i += 1
                    continue
                op = row[0]
                rest = row[1:] if len(row) > 0 else ""
                if op not in (" ", "-", "+"):
                    raise PatchError(
                        f"Update hunk lines must start with ' ', '-', or '+' "
                        f"(or '@@'), got: {row!r}")
                if current is None:
                    current = Hunk()
                    hunks.append(current)
                current.lines.append((op, rest))
                i += 1
            if not hunks:
                raise PatchError(f"Update File '{path}' has no hunks.")
            ops.append(Op(kind="update", path=path,
                          hunks=hunks, move_to=move_to))
            continue
        # Unknown header / stray content between ops — skip blank lines,
        # otherwise it's an error
        if ln.strip() == "":
            i += 1
            continue
        raise PatchError(f"Unexpected line (not inside an op): {ln!r}")
    if not ops:
        raise PatchError("Patch contained no operations.")
    return ops


# ── Applier ─────────────────────────────────────────────────────────────

def _resolve(path: str) -> Path:
    p = Path(path).expanduser()
    return p.resolve() if p.is_absolute() else p.resolve()


def _anchor_match(line: str, anchor: str) -> bool:
    """LLM-friendly anchor matcher: accepts exact, rstrip, full-strip, and
    prefix (stripped) forms. The prefix form lets `class Foo` match
    `class Foo:`, which models drop often."""
    la = line.strip()
    aa = anchor.strip()
    if not aa:
        return False
    if la == aa:
        return True
    if line.rstrip() == anchor.rstrip():
        return True
    if la.startswith(aa):
        # Require the next char to be punctuation/whitespace so `class F`
        # doesn't match `class Foo`.
        tail = la[len(aa):].lstrip()
        return tail == "" or tail[0] in ":;,(){}[]"
    return False


def _find_anchor_window(file_lines: list[str], anchors: list[str]) -> int:
    """Return the line index right after the last anchor — the start of the
    search window for this hunk's context. -1 if any anchor doesn't match."""
    cursor = 0
    for anc in anchors:
        found = -1
        for j in range(cursor, len(file_lines)):
            if _anchor_match(file_lines[j], anc):
                found = j
                break
        if found == -1:
            return -1
        cursor = found + 1
    return cursor


def _seek_context(file_lines: list[str], pattern: list[str], start: int) -> int:
    """Locate *pattern* in *file_lines* at or after *start* using the same
    4-tier cascade Codex's apply_patch uses: exact → rstrip → full-strip →
    Unicode-punctuation-normalized. Returns the match index or -1."""
    if not pattern:
        return start
    n = len(pattern)
    if n > len(file_lines) - start:
        return -1

    # Exact
    for i in range(start, len(file_lines) - n + 1):
        if file_lines[i:i + n] == pattern:
            return i
    # Right-strip
    for i in range(start, len(file_lines) - n + 1):
        if all(file_lines[i + k].rstrip() == pattern[k].rstrip()
               for k in range(n)):
            return i
    # Full-strip
    for i in range(start, len(file_lines) - n + 1):
        if all(file_lines[i + k].strip() == pattern[k].strip()
               for k in range(n)):
            return i
    # Unicode-normalized (smart quotes, fancy dashes, nbsp → ASCII)
    def _norm(s: str) -> str:
        trans = {
            ord("\u2010"): "-", ord("\u2011"): "-", ord("\u2012"): "-",
            ord("\u2013"): "-", ord("\u2014"): "-", ord("\u2015"): "-",
            ord("\u2212"): "-",
            ord("\u2018"): "'", ord("\u2019"): "'",
            ord("\u201A"): "'", ord("\u201B"): "'",
            ord("\u201C"): '"', ord("\u201D"): '"',
            ord("\u201E"): '"', ord("\u201F"): '"',
            ord("\u00A0"): " ", ord("\u2002"): " ", ord("\u2003"): " ",
            ord("\u2004"): " ", ord("\u2005"): " ", ord("\u2006"): " ",
            ord("\u2007"): " ", ord("\u2008"): " ", ord("\u2009"): " ",
            ord("\u200A"): " ", ord("\u202F"): " ", ord("\u205F"): " ",
            ord("\u3000"): " ",
        }
        return s.strip().translate(trans)
    for i in range(start, len(file_lines) - n + 1):
        if all(_norm(file_lines[i + k]) == _norm(pattern[k]) for k in range(n)):
            return i
    return -1


def _apply_update(file_lines: list[str], hunk: Hunk) -> list[str]:
    """Apply a single hunk. Returns new lines. Raises PatchError on failure."""
    before: list[str] = [text for op, text in hunk.lines if op in (" ", "-")]
    window = _find_anchor_window(file_lines, hunk.anchors) if hunk.anchors else 0
    if window < 0:
        raise PatchError(f"Could not locate anchors: {hunk.anchors!r}")

    if not before:
        # Pure-insert hunk: anchors are required so we know WHERE to insert.
        if not hunk.anchors:
            raise PatchError("Pure-insert hunk needs at least one '@@' anchor.")
        new_lines = [text for op, text in hunk.lines if op == "+"]
        return file_lines[:window] + new_lines + file_lines[window:]

    match_idx = _seek_context(file_lines, before, window)
    if match_idx < 0:
        sample = "\n".join(f"  {ln!r}" for ln in before[:3])
        raise PatchError(
            f"Hunk context not found in file. First context lines:\n{sample}")

    # Uniqueness check: if anchors were given, we only search WITHIN the
    # window, so a second match outside doesn't hurt us. If no anchors
    # were given, require uniqueness across the whole file to catch
    # accidents silently patching the wrong block.
    if not hunk.anchors:
        second = _seek_context(file_lines, before, match_idx + 1)
        if second >= 0:
            raise PatchError(
                "Hunk context matches multiple locations — add one or more "
                "'@@ anchor' lines to scope the hunk.")

    replacement = [text for op, text in hunk.lines if op in (" ", "+")]
    n = len(before)
    return file_lines[:match_idx] + replacement + file_lines[match_idx + n:]


def apply_patch(patch_text: str, dry_run: bool = False) -> str:
    """Parse *patch_text* and apply the operations. Returns a JSON result."""
    try:
        ops = parse_patch(patch_text)
    except PatchError as e:
        return json.dumps({"error": f"Parse error: {e}"})

    # Pre-pass: read originals so we can publish diff events + rollback
    originals: dict[str, str] = {}
    changes: list[dict] = []
    warnings: list[str] = []

    # Checkpoint before any mutation so cancel / rollback stays coherent.
    from core.checkpoints import checkpoint_manager
    try:
        workspace = Path(ops[0].path).expanduser()
        workspace = workspace.parent if workspace.parent != workspace else workspace
        checkpoint_manager.ensure_checkpoint(str(workspace), "before apply_patch")
    except Exception:
        pass

    # Validate/compute every op before writing anything — better to fail
    # loudly than leave the repo half-patched.
    computed: list[tuple[Op, list[str] | None, str]] = []
    for op in ops:
        abs_path = str(_resolve(op.path))
        if op.kind == "add":
            if Path(abs_path).exists():
                return json.dumps({
                    "error": f"Add File target already exists: {abs_path}. "
                             "Use Update File instead."})
            body = "\n".join(op.body_add or []) + (
                "\n" if op.body_add else "")
            computed.append((op, None, body))
        elif op.kind == "delete":
            if not Path(abs_path).is_file():
                return json.dumps({
                    "error": f"Delete File target missing: {abs_path}"})
            originals[abs_path] = Path(abs_path).read_text(
                encoding="utf-8", errors="replace")
            computed.append((op, None, ""))
        elif op.kind == "update":
            if not Path(abs_path).is_file():
                return json.dumps({
                    "error": f"Update File target missing: {abs_path}"})
            src = Path(abs_path).read_text(encoding="utf-8", errors="replace")
            originals[abs_path] = src
            # Preserve the file's line-ending style when writing back
            uses_crlf = "\r\n" in src
            file_lines = src.replace("\r\n", "\n").split("\n")
            try:
                for h in op.hunks or []:
                    file_lines = _apply_update(file_lines, h)
            except PatchError as e:
                return json.dumps({
                    "error": f"{op.path}: {e}"})
            new_text = "\n".join(file_lines)
            if uses_crlf:
                new_text = new_text.replace("\n", "\r\n")
            computed.append((op, file_lines, new_text))
        else:
            return json.dumps({"error": f"Unknown op kind: {op.kind}"})

    if dry_run:
        return json.dumps({
            "status": f"dry_run OK — {len(computed)} op(s) would apply.",
            "files": [o.path for o, _, _ in computed],
        })

    # Commit phase — do mutations in order, roll back on any write failure.
    # Use safe_write_text so a silent overwrite (file watcher, IDE autosave)
    # raises an error instead of letting us report success on drifted content.
    from tools.lint import safe_write_text
    written: list[tuple[str, str | None]] = []  # (path, original_or_None)
    try:
        for op, _lines, new_text in computed:
            abs_path = str(_resolve(op.path))
            if op.kind == "add":
                Path(abs_path).parent.mkdir(parents=True, exist_ok=True)
                err = safe_write_text(abs_path, new_text)
                if err:
                    raise RuntimeError(err)
                written.append((abs_path, None))
                changes.append({"op": "add", "path": abs_path})
            elif op.kind == "delete":
                written.append((abs_path, originals.get(abs_path, "")))
                Path(abs_path).unlink()
                changes.append({"op": "delete", "path": abs_path})
            elif op.kind == "update":
                err = safe_write_text(abs_path, new_text)
                if err:
                    raise RuntimeError(err)
                written.append((abs_path, originals.get(abs_path)))
                if op.move_to:
                    new_abs = str(_resolve(op.move_to))
                    Path(new_abs).parent.mkdir(parents=True, exist_ok=True)
                    os.replace(abs_path, new_abs)
                    changes.append({"op": "update+move",
                                    "path": abs_path, "to": new_abs})
                else:
                    changes.append({"op": "update", "path": abs_path})
    except Exception as e:
        # Best-effort rollback
        for path, orig in reversed(written):
            try:
                if orig is None:
                    Path(path).unlink()
                else:
                    Path(path).write_text(orig, encoding="utf-8")
            except Exception:
                warnings.append(f"rollback failed for {path}")
        return json.dumps({
            "error": f"Write failed mid-patch: {e}",
            "rolled_back": [p for p, _ in written],
            "warnings": warnings,
        })

    # Publish file.changed events so the diff viewer picks these up.
    try:
        from core.event_bus import bus
        for op, _lines, _text in computed:
            abs_path = str(_resolve(op.path))
            if op.kind == "update":
                final_path = str(_resolve(op.move_to)) if op.move_to else abs_path
                bus.emit("file.changed", path=final_path,
                         tool="apply_patch",
                         original=originals.get(abs_path, ""))
            elif op.kind == "add":
                bus.emit("file.changed", path=abs_path,
                         tool="apply_patch", original="")
    except Exception as e:
        import traceback
        print(f"[apply_patch] file.changed emission failed: {e}")
        traceback.print_exc()

    from core.sounds import play_edit_sound
    sound_path = ""
    for change in changes:
        if change["op"] in ("add", "update", "update+move"):
            sound_path = change.get("to") or change["path"]
            break
    play_edit_sound(sound_path)

    # Combined lint + LSP per modified file. Surface errors as a TOP-LEVEL
    # `error`/`diagnostics` field per file so the model can't gloss past them
    # buried in a status string.
    from tools.lint import validate_file
    file_diagnostics: dict[str, list[dict]] = {}
    has_any_error = False
    for change in changes:
        if change["op"] not in ("add", "update", "update+move"):
            continue
        file_path = change.get("to") or change["path"]
        try:
            v = validate_file(file_path)
        except Exception:
            continue
        errs = [d for d in v.get("diagnostics", []) if d.get("severity") == "error"]
        if errs:
            has_any_error = True
            file_diagnostics[file_path] = errs

    result: dict = {
        "status": f"Applied {len(changes)} operation(s).",
        "changes": changes,
    }
    if has_any_error:
        total = sum(len(v) for v in file_diagnostics.values())
        result["error"] = (
            f"Validation failed: {total} error(s) across "
            f"{len(file_diagnostics)} file(s). Re-read the affected files, "
            "fix the issues below, and patch again."
        )
        result["diagnostics"] = file_diagnostics
    return json.dumps(result)


# ── Registration ────────────────────────────────────────────────────────

_DESCRIPTION = (
    "Patch tool. Add/edit/delete/rename files atomically. 3+ files: plan first.\n\n"
    "  *** Begin Patch\n"
    "  *** Add File: path/new.py\n"
    "  +line1\n"
    "  *** Update File: path/ex.py\n"
    "  @@ class Foo\n"
    "  @@     def bar(self):\n"
    "   context\n"
    "  -old\n"
    "  +new\n"
    "  *** Delete File: path/gone.py\n"
    "  *** End Patch\n\n"
    "Add: lines '+'. Delete: empty body. Update: ' '=ctx '-'=rm '+'=add. "
    "@@ scopes to block; stack for repeated names. Ambiguous: rejected. "
    "Move: '*** Move to: new/path' after header. Any error rolls all back."
)

registry.register(
    name="apply_patch",
    description=_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "The full patch envelope, from '*** Begin Patch' "
                               "through '*** End Patch'.",
            },
            "dry_run": {
                "type": "boolean",
                "description": "If true, parse and validate the patch but don't "
                               "write any files. Returns the list of ops that "
                               "would be applied.",
            },
        },
        "required": ["patch"],
    },
    execute=lambda patch, dry_run=False: apply_patch(patch, dry_run=dry_run),
)
