"""
Grep tool - search file contents with regex, powered by ripgrep.

Uses ripgrep (rg) as a subprocess for 10-100x faster search on large
codebases. Results are sorted by file modification time (newest first)
so recently-changed code surfaces at the top.

Exposes the ripgrep features actually worth reaching for: case-insensitive
search, surrounding context lines, literal (fixed-string) search, word-boundary
matching, multiline patterns, and hidden-file search. Per-file truncation is
SURFACED, never silent.
"""

import json
import os
import subprocess
import shutil
import time
from pathlib import Path
from tools.registry import registry
from core.proc import NO_WINDOW

DEFAULT_MAX_RESULTS = 300

# Dependency / build / cache dirs to skip by default. ripgrep already honors
# .gitignore, but a virtualenv's site-packages is usually NOT gitignored, and
# searching it floods stdout with tens of thousands of matches — the pure-Python
# JSON parse below then holds the GIL long enough to stutter the GUI. Excluding
# these keeps grep fast and consistent with the glob tool's IGNORE_DIRS.
_DEFAULT_EXCLUDES = (
    "node_modules", "site-packages", "site-packages64", "__pycache__",
    "dist", "build", ".next", ".nuxt", "coverage", ".pytest_cache",
    ".cache", ".tox", ".mypy_cache",
    ".git", ".hg", ".svn", ".ruff_cache", ".idea", ".vscode",
    "data", "logs", "file_share", "sandbox", "Distributable",
)
# Default cap on MATCHES reported per file — keeps one busy file from eating the
# whole result budget so matches stay spread across files. Unlike the old hard
# 15, this is adjustable AND surfaced when hit (see the "+N more" notes).
DEFAULT_PER_FILE_CAP = 40

# Known locations for rg.exe on Windows (winget install path)
_RG_SEARCH_PATHS = [
    # WinGet install
    os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\BurntSushi.ripgrep.MSVC_Microsoft.Winget.Source_8wekyb3d8bbwe\ripgrep-15.1.0-x86_64-pc-windows-msvc\rg.exe"
    ),
    # VS Code bundled
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\_\resources\app\node_modules\@vscode\ripgrep\bin\rg.exe"
    ),
    # Cursor bundled
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\cursor\resources\app\node_modules\@vscode\ripgrep\bin\rg.exe"
    ),
]


def _find_rg() -> str | None:
    """Find the ripgrep binary."""
    # Check PATH first
    rg = shutil.which("rg")
    if rg:
        return rg
    # Check known locations
    for path in _RG_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


_rg_path = _find_rg()


def grep(pattern: str, path: str = None, glob: str = None,
         max_results: int = None, case_insensitive: bool = False,
         context: int = 0, fixed_string: bool = False,
         word: bool = False, multiline: bool = False,
         hidden: bool = False, per_file_cap: int = None) -> str:
    """Search for a regex pattern in files using ripgrep."""
    search_path = path or str(Path.cwd())
    max_results = max_results or DEFAULT_MAX_RESULTS
    per_file_cap = per_file_cap or DEFAULT_PER_FILE_CAP
    context = max(0, int(context or 0))

    if not _rg_path:
        return json.dumps({"error": (
            "ripgrep (rg) not found. Install it: winget install BurntSushi.ripgrep.MSVC "
            "or download from https://github.com/BurntSushi/ripgrep/releases"
        )})

    # Build rg command. --max-count is per_file_cap+1 so we can DETECT (and
    # surface) when a file had more matches than we show, instead of the old
    # silent hard cut at 15.
    cmd = [
        _rg_path,
        "--json",
        "--no-messages",
        "--max-count", str(per_file_cap + 1),
        "--max-columns", "500",
    ]
    if case_insensitive:
        cmd.append("-i")
    if fixed_string:
        cmd.append("-F")
    if word:
        cmd.append("-w")
    if hidden:
        cmd.append("--hidden")
    if multiline:
        # -U lets a pattern span lines; dotall makes '.' cross newlines too,
        # which is what you almost always want when reaching for multiline.
        cmd.extend(["-U", "--multiline-dotall"])
    if context > 0:
        cmd.extend(["-C", str(context)])
    # Default excludes (exclusive globs — they prune without forcing
    # "include-only" mode, so a user-supplied `glob` still works on top).
    for _ex in _DEFAULT_EXCLUDES:
        cmd.extend(["--glob", f"!**/{_ex}/**"])
    if glob:
        cmd.extend(["--glob", glob])
    cmd.extend([pattern, search_path])

    # STREAM ripgrep's output and stop the moment we have enough.
    #
    # The old code ran rg to completion with capture_output=True, buffering the
    # ENTIRE result, then parsed every JSON line in pure Python. On a large tree
    # (e.g. a default-path search over ~250k files) a common pattern emits tens
    # of thousands of lines / tens of MB of JSON — and we'd parse all of it even
    # though only `max_results` matches are ever surfaced. That pure-Python parse
    # holds the GIL for seconds, freezing the Qt GUI thread.
    #
    # Instead we read rg's stdout lazily and KILL the process once we've gathered
    # `max_results` matches. Each readline is I/O that releases the GIL, and the
    # parse is now bounded to O(max_results) regardless of repo size — so the UI
    # stays responsive. Small searches behave exactly as before.
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=NO_WINDOW,  # no console flash on Windows
        )
    except FileNotFoundError:
        return json.dumps({"error": f"ripgrep binary not found at {_rg_path}"})

    # Parse JSON output lines. Collect both 'match' and 'context' entries so
    # context lines can be interleaved with their matches per file.
    matches_by_file: dict[str, list[dict]] = {}
    match_count_by_file: dict[str, int] = {}
    kept_matches = 0          # matches within per_file_cap (≈ output match lines)
    hit_global_cap = False    # stopped because we reached max_results
    timed_out = False
    deadline = time.monotonic() + 30  # wall-clock guard (replaces run timeout)

    try:
        for _i, line in enumerate(proc.stdout):
            # Re-check the clock cheaply every so often so a pathological search
            # can't run past the deadline.
            if _i % 1000 == 0 and time.monotonic() > deadline:
                timed_out = True
                break
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            mtype = msg.get("type")
            if mtype not in ("match", "context"):
                continue

            data = msg.get("data", {})
            file_path = data.get("path", {}).get("text", "")
            line_number = data.get("line_number", 0)
            line_text = data.get("lines", {}).get("text", "").rstrip("\n\r")
            if not file_path:
                continue

            is_match = mtype == "match"
            # Honor per-file cap on MATCHES only (context lines ride along free).
            if is_match:
                seen = match_count_by_file.get(file_path, 0)
                if seen >= per_file_cap:
                    match_count_by_file[file_path] = seen + 1  # keep counting for note
                    continue
                match_count_by_file[file_path] = seen + 1

            matches_by_file.setdefault(file_path, []).append({
                "line": line_number,
                "text": line_text,
                "is_match": is_match,
            })

            # Stop once we've kept enough matches to fill the output budget.
            # We give a little headroom so the mtime sort below still has a few
            # files to choose "newest first" from.
            if is_match:
                kept_matches += 1
                if kept_matches >= max_results + per_file_cap:
                    hit_global_cap = True
                    break
    finally:
        # If we stopped early, terminate rg and reap it so no process leaks.
        # Wait before draining stderr; reading a pipe while rg is still exiting
        # can block and leave the UI stuck on the in-flight search indicator.
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
        try:
            stderr_text = (proc.stderr.read() or "") if proc.stderr else ""
        except Exception:
            stderr_text = ""

    if timed_out and not matches_by_file:
        return json.dumps({"error": "Search timed out after 30 seconds."})

    # rg exits 2 on a real error (e.g. a bad regex). Surface it instead of
    # reporting "no matches" — a malformed pattern shouldn't look like an empty
    # result. (Only meaningful when we let rg finish; an early kill won't be 2.)
    if (not hit_global_cap and not timed_out
            and proc.returncode == 2 and stderr_text.strip()):
        return json.dumps({"error": f"ripgrep: {stderr_text.strip()[:300]}"})

    if not matches_by_file:
        return json.dumps({"results": "No matches found."})

    # Sort files by modification time (newest first)
    def _mtime(filepath: str) -> float:
        try:
            return os.path.getmtime(filepath)
        except OSError:
            return 0.0

    sorted_files = sorted(matches_by_file.keys(), key=_mtime, reverse=True)

    # Format output. Matches use 'path:line:text'; context uses 'path-line-text'
    # (same convention as grep -C), so the two are visually distinguishable.
    output_lines = []
    total = 0
    capped_note_added = False
    base = Path(search_path) if os.path.isdir(search_path) else Path(search_path).parent

    for fpath in sorted_files:
        if total >= max_results:
            break
        entries = sorted(matches_by_file[fpath], key=lambda e: e["line"])
        try:
            rel = str(Path(fpath).relative_to(base))
        except ValueError:
            rel = fpath
        for entry in entries:
            if total >= max_results:
                break
            sep = ":" if entry["is_match"] else "-"
            output_lines.append(f"{rel}{sep}{entry['line']}{sep}{entry['text']}")
            total += 1
        # Surface per-file truncation (the old behavior hid this silently).
        extra = match_count_by_file.get(fpath, 0) - per_file_cap
        if extra > 0 and total < max_results:
            output_lines.append(
                f"  … {extra} more match(es) in {rel} (raise per_file_cap to see)")
            capped_note_added = True

    output = "\n".join(output_lines)
    # `hit_global_cap` means we stopped reading rg early (there were more matches
    # we never parsed), so we can only report a lower bound. `total >= max_results`
    # means we filled the output budget from what we did parse. Either way the
    # honest signal is "N+ matches, narrow the pattern or path".
    if total >= max_results or hit_global_cap:
        output += (f"\n\n(showing {total} matches; more exist - "
                   f"narrow the pattern or pass a `path` to scope the search "
                   f"(max_results={max_results}))")
    elif timed_out:
        output += ("\n\n(search hit the 30s limit — results are partial; "
                   "pass a narrower `path` to scope it)")
    elif capped_note_added:
        output += ("\n\n(some files had more matches than per_file_cap="
                   f"{per_file_cap}; raise it or narrow the pattern)")

    return json.dumps({"results": output}, ensure_ascii=False)


registry.register(
    name="grep",
    description=(
        "Regex content search via ripgrep. → path:line:text, sorted by mtime (newest first).\n"
        "- glob filters by type (e.g. '*.py').\n"
        "- case_insensitive, word (whole-word), fixed_string (literal, no regex).\n"
        "- context=N shows N lines around each match (like grep -C).\n"
        "- multiline lets a pattern span lines. hidden searches dotfiles.\n"
        "- ✓ auto-skips .git, node_modules, binaries. Per-file truncation is surfaced."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern (or literal text if fixed_string=true).",
            },
            "path": {
                "type": "string",
                "description": "Dir|file to search (default cwd).",
            },
            "glob": {
                "type": "string",
                "description": "File filter (e.g. '*.py', '*.{ts,tsx}').",
            },
            "max_results": {
                "type": "integer",
                "description": "Max total matches (default 300).",
            },
            "case_insensitive": {
                "type": "boolean",
                "description": "Case-insensitive search (rg -i). Default false.",
            },
            "context": {
                "type": "integer",
                "description": "Show N lines of context around each match (rg -C). Default 0.",
            },
            "fixed_string": {
                "type": "boolean",
                "description": "Treat pattern as a literal string, not regex (rg -F). Default false.",
            },
            "word": {
                "type": "boolean",
                "description": "Match whole words only (rg -w). Default false.",
            },
            "multiline": {
                "type": "boolean",
                "description": "Allow patterns to span lines (rg -U, dotall). Default false.",
            },
            "hidden": {
                "type": "boolean",
                "description": "Also search hidden/dotfiles (rg --hidden). Default false.",
            },
            "per_file_cap": {
                "type": "integer",
                "description": "Max matches reported per file before truncation is noted (default 40).",
            },
        },
        "required": ["pattern"],
    },
    execute=grep,
)
