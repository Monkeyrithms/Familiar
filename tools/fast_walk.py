"""
fast_walk — fast filesystem traversal.

Prefers ripgrep's `rg --files` for the directory walk: it's multi-threaded,
honors .gitignore, and — crucially for this GUI app — runs in a subprocess, so
it releases the GIL entirely instead of starving the GUI thread the way a long
pure-Python os.walk does. Falls back to os.walk (with cooperative GIL yields)
when rg isn't installed.

Used by file_search. The glob tool keeps its own os.walk because it needs
features rg --files doesn't expose (directory listing, per-pattern depth caps).
"""

import os
import time
import subprocess
from pathlib import Path

from tools._rg import RG_PATH

try:
    from core.proc import NO_WINDOW
except Exception:  # pragma: no cover - defensive
    NO_WINDOW = 0

# Directories always skipped (mirrors file_search / glob ignore sets).
DEFAULT_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "coverage", ".pytest_cache", ".cache", ".tox",
    "egg-info", ".mypy_cache", ".idea", ".vscode",
    "site-packages", "site-packages64",
    "data", "logs", "file_share", "sandbox", "Distributable",
}

_HARD_CAP = 200_000
_YIELD_EVERY = 4000  # GIL breath cadence for the os.walk fallback


def list_files(root, ignore_dirs=None, hidden: bool = False,
               max_files: int | None = None) -> list[str]:
    """Return relative POSIX paths of files under *root*.

    Uses `rg --files` when available, else os.walk. *ignore_dirs* defaults to
    DEFAULT_IGNORE_DIRS. When *hidden* is False, dot-directories/files are
    skipped (matching the previous file_search behavior).
    """
    root = Path(root)
    ignore_dirs = set(ignore_dirs) if ignore_dirs is not None else DEFAULT_IGNORE_DIRS
    cap = min(max_files or _HARD_CAP, _HARD_CAP)

    if RG_PATH:
        files = _list_files_rg(root, ignore_dirs, hidden, cap)
        if files is not None:
            return files
    return _list_files_walk(root, ignore_dirs, hidden, cap)


def _list_files_rg(root: Path, ignore_dirs, hidden: bool, cap: int) -> list[str] | None:
    """rg-backed walk. Returns None on failure so the caller can fall back."""
    cmd = [RG_PATH, "--files"]
    if hidden:
        cmd.append("--hidden")
    for d in ignore_dirs:
        cmd += ["-g", f"!**/{d}/**", "-g", f"!{d}/**"]
    cmd.append(str(root))
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace", creationflags=NO_WINDOW,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if proc.returncode not in (0, 1):  # 1 = "no files", not an error for us
        return None

    out: list[str] = []
    for line in proc.stdout.splitlines():
        if not line:
            continue
        try:
            rel = os.path.relpath(line, root)
        except ValueError:
            rel = line
        out.append(rel.replace("\\", "/"))
        if len(out) >= cap:
            break
    return out


def _list_files_walk(root: Path, ignore_dirs, hidden: bool, cap: int) -> list[str]:
    """Pure-Python fallback with the same ignore semantics + GIL yields."""
    out: list[str] = []
    root_str = str(root)
    for seen, (dirpath, dirnames, filenames) in enumerate(os.walk(root_str)):
        dirnames[:] = [
            d for d in dirnames
            if d not in ignore_dirs and (hidden or not d.startswith("."))
        ]
        if seen % 64 == 0:
            time.sleep(0)  # GIL breath for the GUI thread
        for fname in filenames:
            if not hidden and fname.startswith("."):
                continue
            full = os.path.join(dirpath, fname)
            try:
                rel = os.path.relpath(full, root_str)
            except ValueError:
                rel = full
            out.append(rel.replace("\\", "/"))
            if len(out) >= cap:
                return out
    return out
