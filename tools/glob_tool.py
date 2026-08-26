"""
Glob tool — list files matching a pattern.

Fills a critical gap: the agent needs a way to discover files by name
without relying on grep (content search) or terminal (ls/find).
Supports recursive glob patterns like '**/*.md'.
"""

import json
import os
import re
import stat as _stat
import time
from pathlib import Path
from tools.registry import registry

DEFAULT_MAX_FILES = 500
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "coverage", ".pytest_cache", ".cache", ".tox",
    "egg-info", ".mypy_cache",
    # A virtualenv's site-packages dwarfs the project itself (tens of thousands
    # of files). Walking it stalls the search and — because the traversal is
    # pure-Python and holds the GIL — starves the app's GUI thread into stutter.
    "site-packages", "site-packages64",
    "data", "logs", "file_share", "sandbox", "Distributable",
}

# Cooperatively release the GIL every N directories so a large tree never
# monopolises the interpreter and freezes the GUI thread that shares it.
_YIELD_EVERY = 64


def _compile_glob(pattern: str) -> re.Pattern:
    """Translate a glob (``**``, ``*``, ``?``, ``[set]``) into a regex matched
    against a forward-slash relative path. ``**/`` spans any number of
    directories (including zero); ``*`` and ``?`` stop at a path separator, so
    ``*.py`` stays top-level only — same semantics as ``Path.glob``."""
    pat = pattern.replace("\\", "/")
    if pat.startswith("./"):
        pat = pat[2:]
    i, n, out = 0, len(pat), []
    while i < n:
        if pat[i:i + 3] == "**/":
            out.append("(?:[^/]+/)*"); i += 3
        elif pat[i:i + 2] == "**":
            out.append(".*"); i += 2
        elif pat[i] == "*":
            out.append("[^/]*"); i += 1
        elif pat[i] == "?":
            out.append("[^/]"); i += 1
        elif pat[i] == "[":
            j = i + 1
            if j < n and pat[j] in "!^":
                j += 1
            if j < n and pat[j] == "]":
                j += 1
            while j < n and pat[j] != "]":
                j += 1
            if j >= n:
                out.append(r"\["); i += 1
            else:
                inner = pat[i + 1:j]
                if inner.startswith("!"):
                    inner = "^" + inner[1:]
                out.append("[" + inner + "]"); i = j + 1
        else:
            out.append(re.escape(pat[i])); i += 1
    return re.compile("^" + "".join(out) + r"\Z")


def glob_files(pattern: str, path: str = None, max_results: int = None,
               include_dirs: bool = False) -> str:
    """List paths matching a glob pattern, sorted by mtime (newest first).

    By default only files are returned. Set include_dirs=True to also surface
    directories — needed when discovering a project folder by name.
    """
    search_root = Path(path) if path else Path.cwd()
    max_results = max_results or DEFAULT_MAX_FILES

    if not search_root.is_dir():
        return json.dumps({"error": f"Not a directory: {search_root}"})

    rx = _compile_glob(pattern)
    # Non-recursive patterns (no ``**``) can only match down to a fixed depth —
    # the number of '/' in the pattern. Stop descending past it so ``*.py`` and
    # ``src/*.ts`` don't needlessly walk the entire subtree.
    recursive = "**" in pattern
    max_depth = None if recursive else pattern.replace("\\", "/").rstrip("/").count("/")

    root_str = str(search_root)
    gather_cap = max_results * 2  # gather extra so the mtime sort has headroom
    matches = []
    try:
        for seen, (dirpath, dirnames, filenames) in enumerate(
                os.walk(root_str)):  # followlinks=False → no symlink-loop hangs
            # Prune ignored dirs IN PLACE so os.walk never descends into them.
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]

            rel_dir = os.path.relpath(dirpath, root_str)
            depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1

            # Candidate names at THIS level — capture dirs BEFORE depth-pruning
            # empties dirnames, or include_dirs would miss top-level folders.
            names = list(filenames)
            if include_dirs:
                names += list(dirnames)

            # Stop descending once we're as deep as this pattern can match.
            if max_depth is not None and depth >= max_depth:
                dirnames[:] = []

            if seen % _YIELD_EVERY == 0:
                time.sleep(0)  # GIL breath for the GUI thread

            for name in names:
                rel = name if rel_dir == "." else f"{rel_dir}{os.sep}{name}"
                rel = rel.replace("\\", "/")
                if not rx.match(rel):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                matches.append({
                    "path": rel,
                    "abs_path": full,
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                    "is_dir": _stat.S_ISDIR(st.st_mode),
                })
            if len(matches) >= gather_cap:
                break
    except Exception as e:
        return json.dumps({"error": f"Glob failed: {e}"})

    # Sort by modification time, newest first
    matches.sort(key=lambda m: m["mtime"], reverse=True)
    matches = matches[:max_results]

    # Format concise output
    lines = []
    for m in matches:
        if m.get("is_dir"):
            lines.append(f"{m['path']}/  (dir)")
        else:
            size_str = _human_size(m["size"])
            lines.append(f"{m['path']}  ({size_str})")

    total = len(matches)
    output = "\n".join(lines) if lines else "No files matched."
    if total >= max_results:
        output += f"\n\n(capped at {max_results} results)"

    return json.dumps({
        "results": output,
        "count": total,
        "root": str(search_root),
    }, ensure_ascii=False)


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


registry.register(
    name="glob",
    description=(
        "List files by glob (e.g. '**/*.md', '*.py', 'docs/**'). Sorted by mtime (newest first).\n"
        "- ✓ discover by name|ext before reading.\n"
        "- ✓ auto-skips .git, node_modules, __pycache__."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob (e.g. '**/*.py', 'src/**/*.ts').",
            },
            "path": {
                "type": "string",
                "description": "Root dir (default workspace).",
            },
            "max_results": {
                "type": "integer",
                "description": "Max files (default 500).",
            },
            "include_dirs": {
                "type": "boolean",
                "description": "Also list matching directories (default false, files only).",
            },
        },
        "required": ["pattern"],
    },
    execute=glob_files,
)
