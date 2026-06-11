"""
apply_patch — structured multi-file patch tool.

Accepts THREE input shapes (auto-detected) so it matches whatever a model
naturally emits instead of forcing one rigid grammar:

1. The `*** Begin Patch` envelope (Codex-style):

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

2. A raw unified diff (git / `diff -u` / patch(1) style), no envelope:

    --- a/path/to/x.py
    +++ b/path/to/x.py
    @@ -1,4 +1,4 @@
     context
    -old
    +new

   `/dev/null` on either side is understood as an add / delete.

3. Either of the above wrapped in a ```diff / ```patch markdown fence.

Robustness niceties (all so a near-miss still applies instead of erroring):
- `*** ` prefixes on headers are optional (`Update File:` works too).
- A `@@ -1,4 +1,4 @@` unified hunk header inside the envelope is treated
  as a positional separator, not a literal anchor.
- Leading / trailing *blank context* lines in a hunk are trimmed before
  matching (they're formatting noise, not real context).
- Context matching itself is a 4-tier cascade (exact → rstrip → strip →
  unicode-normalized), so dropped indentation / smart quotes still match.

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

# Headers — `*** ` prefix optional so loose forms still parse.
_HDR_ADD = re.compile(r"^(?:\*\*\*\s+)?Add File:\s+(.+?)\s*$")
_HDR_DEL = re.compile(r"^(?:\*\*\*\s+)?Delete File:\s+(.+?)\s*$")
_HDR_UPD = re.compile(r"^(?:\*\*\*\s+)?Update File:\s+(.+?)\s*$")
_HDR_MOVE = re.compile(r"^(?:\*\*\*\s+)?Move to:\s+(.+?)\s*$")
_BEGIN_RE = re.compile(r"^\s*(?:\*\*\*\s+)?Begin Patch\s*$")
_END_RE = re.compile(r"^\s*(?:\*\*\*\s+)?End Patch\s*$")
_FENCE_RE = re.compile(r"^\s*`{3,}\s*\w*\s*$")

# A unified-diff hunk header like `@@ -1,4 +1,4 @@ optional section`.
_UNIFIED_HUNK = re.compile(
    r"^@@+\s*-?\d+(?:,\d+)?\s+\+?\d+(?:,\d+)?\s*@@+(.*)$")


class PatchError(ValueError):
    pass


def _strip_fences(text: str) -> str:
    """Drop markdown code-fence lines (```diff / ```patch / ```). Code rarely
    contains a line that is *only* a triple-backtick, so this is safe."""
    if "```" not in text:
        return text
    kept = [ln for ln in text.splitlines() if not _FENCE_RE.match(ln)]
    return "\n".join(kept)


def _is_boundary(line: str) -> bool:
    """True if *line* begins a new op or ends the envelope."""
    return bool(
        _HDR_ADD.match(line) or _HDR_DEL.match(line) or _HDR_UPD.match(line)
        or _END_RE.match(line))


def _strip_diff_path(raw: str) -> str:
    """Normalize a unified-diff path token: drop `a/`/`b/` prefix, a trailing
    tab-timestamp, and surrounding quotes."""
    p = raw.strip()
    # `+++ b/x.py\t2020-...` — diff keeps a tab-separated timestamp
    if "\t" in p:
        p = p.split("\t", 1)[0].strip()
    if p.startswith('"') and p.endswith('"') and len(p) >= 2:
        p = p[1:-1]
    if p[:2] in ("a/", "b/"):
        p = p[2:]
    return p


def _looks_like_unified_diff(text: str) -> bool:
    lines = text.splitlines()
    has_marker = any(
        ln.startswith("--- ") or ln.startswith("+++ ")
        or ln.startswith("diff --git") for ln in lines)
    has_hunk = any(_UNIFIED_HUNK.match(ln) for ln in lines)
    return has_marker and has_hunk


def parse_patch(text: str) -> list[Op]:
    """Parse a patch (any supported shape) into a list of operations.

    Raises PatchError on malformed input — the tool returns the error
    text verbatim so the model can retry with a fix.
    """
    if not text or not text.strip():
        raise PatchError("Empty patch.")
    text = _strip_fences(text)

    has_envelope = any(_BEGIN_RE.match(ln) for ln in text.splitlines())
    if has_envelope:
        return _parse_envelope(text)
    if _looks_like_unified_diff(text):
        return _parse_unified_diff(text)
    raise PatchError(
        "Unrecognized patch. Use a '*** Begin Patch' envelope or a unified "
        "diff (lines starting with '--- ', '+++ ', and '@@').")


def _parse_envelope(text: str) -> list[Op]:
    lines = text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines)
                     if _BEGIN_RE.match(ln)) + 1
    except StopIteration:
        raise PatchError(f"Missing '{_BEGIN}'.")
    # End marker is optional — fall back to EOF so a dropped footer still works.
    end = next((i for i, ln in enumerate(lines) if _END_RE.match(ln)),
               len(lines))
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
            while i < len(body) and not _is_boundary(body[i]):
                row = body[i]
                if row.startswith("+"):
                    content.append(row[1:])
                elif row.strip() == "":
                    content.append("")          # tolerate blank body lines
                else:
                    content.append(row)         # tolerate missing '+'
                i += 1
            ops.append(Op(kind="add", path=path, body_add=content))
            continue
        if m_del:
            path = m_del.group(1).strip()
            i += 1
            while i < len(body) and not _is_boundary(body[i]):
                i += 1                            # ignore any del body
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
            while i < len(body) and not _is_boundary(body[i]):
                row = body[i]
                if row.startswith("@@"):
                    uni = _UNIFIED_HUNK.match(row)
                    if uni:
                        # Positional `@@ -n,m +n,m @@` — start a fresh hunk;
                        # any trailing section text becomes an anchor hint.
                        current = Hunk()
                        hunks.append(current)
                        hint = uni.group(1).strip()
                        if hint:
                            current.anchors.append(hint)
                    else:
                        anchor = row[2:].strip().rstrip("@").strip()
                        if current is None or current.lines:
                            current = Hunk()
                            hunks.append(current)
                        if anchor:
                            current.anchors.append(anchor)
                    i += 1
                    continue
                if row == "":
                    if current is None:
                        current = Hunk()
                        hunks.append(current)
                    current.lines.append((" ", ""))
                    i += 1
                    continue
                op = row[0]
                rest = row[1:]
                if op not in (" ", "-", "+"):
                    # A bare line with no prefix → treat as context. Models
                    # often drop the leading space on unchanged lines.
                    op, rest = " ", row
                if current is None:
                    current = Hunk()
                    hunks.append(current)
                current.lines.append((op, rest))
                i += 1
            hunks = [h for h in hunks if h.lines or h.anchors]
            if not hunks:
                raise PatchError(f"Update File '{path}' has no hunks.")
            ops.append(Op(kind="update", path=path,
                          hunks=hunks, move_to=move_to))
            continue
        if ln.strip() == "":
            i += 1
            continue
        raise PatchError(f"Unexpected line (not inside an op): {ln!r}")
    if not ops:
        raise PatchError("Patch contained no operations.")
    return ops


def _parse_unified_diff(text: str) -> list[Op]:
    """Translate a raw unified diff into the same Op model the envelope uses.

    Handles update, add (`--- /dev/null`), and delete (`+++ /dev/null`).
    """
    lines = text.splitlines()
    ops: list[Op] = []
    old_path: str | None = None
    new_path: str | None = None
    git_path: str | None = None
    cur_hunks: list[Hunk] = []
    cur_hunk: Hunk | None = None

    def flush():
        nonlocal old_path, new_path, git_path, cur_hunks, cur_hunk
        path = None
        kind = "update"
        if new_path == "/dev/null":            # file removed
            kind, path = "delete", old_path
        elif old_path == "/dev/null":          # file created
            kind, path = "add", new_path
        else:
            path = new_path or old_path or git_path
        if path and path != "/dev/null":
            clean = [h for h in cur_hunks if h.lines]
            if kind == "add":
                body = [t for h in clean for op, t in h.lines if op == "+"]
                ops.append(Op(kind="add", path=path, body_add=body))
            elif kind == "delete":
                ops.append(Op(kind="delete", path=path))
            elif clean:
                ops.append(Op(kind="update", path=path, hunks=clean))
        old_path = new_path = git_path = None
        cur_hunks = []
        cur_hunk = None

    for ln in lines:
        if ln.startswith("diff --git"):
            flush()
            parts = ln.split()
            if len(parts) >= 4:
                git_path = _strip_diff_path(parts[-1])
            continue
        if ln.startswith("index ") or ln.startswith("similarity ") \
                or ln.startswith("rename ") or ln.startswith("new file ") \
                or ln.startswith("deleted file ") or ln.startswith("Index: ") \
                or ln.startswith("==="):
            continue
        if ln.startswith("--- "):
            old_path = _strip_diff_path(ln[4:])
            continue
        if ln.startswith("+++ "):
            new_path = _strip_diff_path(ln[4:])
            continue
        if _UNIFIED_HUNK.match(ln):
            cur_hunk = Hunk()
            cur_hunks.append(cur_hunk)
            continue
        if cur_hunk is None:
            continue
        if ln.startswith("\\"):                 # "\ No newline at end of file"
            continue
        if ln == "":
            cur_hunk.lines.append((" ", ""))
            continue
        op = ln[0]
        if op in (" ", "-", "+"):
            cur_hunk.lines.append((op, ln[1:]))
        else:
            cur_hunk.lines.append((" ", ln))    # tolerate missing prefix
    flush()
    if not ops:
        raise PatchError("Unified diff contained no applicable hunks.")
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


def _trim_edge_blanks(lines: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop leading/trailing pure-blank *context* lines — they're formatting
    noise that wouldn't match the file. Middle blanks (real) are kept, and
    blank +/- lines are kept (they carry an edit)."""
    out = lines[:]
    while out and out[0] == (" ", ""):
        out.pop(0)
    while out and out[-1] == (" ", ""):
        out.pop()
    return out


def _apply_update(file_lines: list[str], hunk: Hunk) -> list[str]:
    """Apply a single hunk. Returns new lines. Raises PatchError on failure."""
    eff = _trim_edge_blanks(hunk.lines)
    before: list[str] = [text for op, text in eff if op in (" ", "-")]
    window = _find_anchor_window(file_lines, hunk.anchors) if hunk.anchors else 0
    if window < 0:
        # Anchors didn't resolve — they may be soft hints (e.g. the section
        # text trailing a `@@ -n,m +n,m @@` header). Fall back to anchorless
        # context matching across the whole file rather than hard-failing.
        window = 0
        hunk = Hunk(anchors=[], lines=hunk.lines)

    if not before:
        # Pure-insert hunk: anchors are required so we know WHERE to insert.
        if not hunk.anchors:
            raise PatchError("Pure-insert hunk needs at least one '@@' anchor.")
        new_lines = [text for op, text in eff if op == "+"]
        return file_lines[:window] + new_lines + file_lines[window:]

    match_idx = _seek_context(file_lines, before, window)
    if match_idx < 0 and window > 0:
        match_idx = _seek_context(file_lines, before, 0)  # widen past anchors
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

    replacement = [text for op, text in eff if op in (" ", "+")]
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
    # Per-file diagnostics captured BEFORE we touch anything, so the post-edit
    # report can show only the errors THIS patch introduced — not the pile of
    # pre-existing ones already in a big file.
    baseline_diags: dict[str, list[dict]] = {}
    from tools.lint import snapshot_diagnostics

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
            # Snapshot existing diagnostics before the edit (line-independent
            # baseline for the post-edit delta).
            baseline_diags[abs_path] = snapshot_diagnostics(abs_path) or []
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

    # Combined lint + LSP per modified file, then DIFF against the pre-edit
    # baseline so we only surface errors THIS patch introduced — not the pile
    # of pre-existing ones in a big file. Surfaced as a TOP-LEVEL
    # `error`/`diagnostics` field so the model can't gloss past real breakage.
    from tools.lint import validate_file, diff_diagnostics
    file_diagnostics: dict[str, list[dict]] = {}
    suppressed_total = 0
    has_any_error = False
    for change in changes:
        if change["op"] not in ("add", "update", "update+move"):
            continue
        file_path = change.get("to") or change["path"]
        orig_path = change["path"]  # baseline was keyed on the pre-move path
        try:
            v = validate_file(file_path)
        except Exception:
            continue
        after = v.get("diagnostics", []) or []
        baseline = baseline_diags.get(orig_path)
        if baseline is None:
            # New file (Add) — everything is "introduced" by definition.
            introduced_errors = [d for d in after
                                 if d.get("severity") == "error"]
            suppressed = 0
        else:
            delta = diff_diagnostics(baseline, after)
            introduced_errors = delta["introduced_errors"]
            suppressed = delta["preexisting_count"]
        suppressed_total += suppressed
        if introduced_errors:
            has_any_error = True
            file_diagnostics[file_path] = introduced_errors

    result: dict = {
        "status": f"Applied {len(changes)} operation(s).",
        "changes": changes,
    }
    if suppressed_total:
        # Tell the model we filtered noise, so silence isn't mistaken for
        # "the file is pristine".
        result["note"] = (
            f"{suppressed_total} pre-existing diagnostic(s) in the edited "
            "file(s) were left as-is (not introduced by this patch)."
        )
    if has_any_error:
        total = sum(len(v) for v in file_diagnostics.values())
        result["error"] = (
            f"This patch introduced {total} new error(s) across "
            f"{len(file_diagnostics)} file(s). Re-read, fix the issues below, "
            "and patch again. (Pre-existing errors are not shown.)"
        )
        result["diagnostics"] = file_diagnostics
    return json.dumps(result)


# ── Registration ────────────────────────────────────────────────────────

_DESCRIPTION = (
    "Patch tool. Add/edit/delete/rename files atomically. 3+ files: plan first.\n\n"
    "Accepts the '*** Begin Patch' envelope OR a raw unified diff "
    "(--- / +++ / @@), optionally inside a ```diff fence.\n\n"
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
    "@@ scopes to block; stack for repeated names. Ambiguous (no anchor, "
    "multi-match): rejected. Move: '*** Move to: new/path' after header. "
    "Any error rolls all back."
)

registry.register(
    name="apply_patch",
    description=_DESCRIPTION,
    parameters={
        "type": "object",
        "properties": {
            "patch": {
                "type": "string",
                "description": "The full patch — '*** Begin Patch' envelope "
                               "or a unified diff.",
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
