"""
Robust resolution of pip-installed console-scripts (ruff, pyflakes, pylsp, …).

The problem this solves: `pip install ruff` drops `ruff.exe` into the running
interpreter's *Scripts* directory (Windows) or *bin* (POSIX). That directory is
frequently NOT on PATH — especially with the `py` launcher, per-user installs,
or virtualenvs that weren't "activated". So `shutil.which("ruff")` returns None
even though ruff is installed and importable, and the app reports the tool as
missing. The user then "installs it again" and it still doesn't show up — the
exact not-smooth experience we want to kill.

`resolve(name)` therefore looks in three places, in order:
  1. PATH (the normal case),
  2. the script dirs tied to THIS interpreter (so a pip install always counts),
  3. `python -m <module>` for tools that can run as a module (last resort).

Returns an argv list ready to splat into subprocess, or None if truly absent.
Results are cached — resolution touches the filesystem, callers may be hot.
"""

from __future__ import annotations

import os
import shutil
import sys
import sysconfig
from functools import lru_cache
from pathlib import Path

# Tools that can also be launched as `python -m <module>` if no binary is found.
# Maps the console-script name → the importable module that exposes a __main__.
_MODULE_FALLBACK = {
    "pyflakes": "pyflakes",
    "pylsp": "pylsp",
    "ruff": "ruff",  # ruff wheels ship ruff/__main__.py that re-execs the binary
}


def _script_dirs() -> list[Path]:
    """Candidate directories where this interpreter's console-scripts live."""
    dirs: list[Path] = []

    def _add(p) -> None:
        if not p:
            return
        path = Path(p)
        if path not in dirs:
            dirs.append(path)

    # Standard install scheme (e.g. .../PythonXX/Scripts on Windows).
    try:
        _add(sysconfig.get_path("scripts"))
    except Exception:
        pass
    # Per-user install scheme (pip install --user).
    try:
        scheme = "nt_user" if os.name == "nt" else "posix_user"
        _add(sysconfig.get_path("scripts", scheme))
    except Exception:
        pass
    # The interpreter's own dir, and a sibling Scripts/bin — covers venvs and the
    # `py` launcher resolving to a real python whose Scripts isn't on PATH.
    exe_dir = Path(sys.executable).resolve().parent
    _add(exe_dir)
    _add(exe_dir / "Scripts")
    _add(exe_dir / "bin")
    _add(exe_dir.parent / "Scripts")
    _add(exe_dir.parent / "bin")
    return dirs


def _exe_names(name: str) -> list[str]:
    """Filenames a console-script may have on this platform."""
    if os.name == "nt":
        # PATHEXT-style suffixes pip/console-scripts actually produce.
        return [name + ext for ext in (".exe", ".cmd", ".bat", "")]
    return [name]


@lru_cache(maxsize=64)
def resolve(name: str) -> tuple[str, ...] | None:
    """Resolve a console-script to an argv prefix, or None if not found.

    Returns a tuple (hashable, for the cache); callers should `list(...)` it.
    """
    # 1. PATH — the fast, normal path.
    on_path = shutil.which(name)
    if on_path:
        return (on_path,)

    # 2. This interpreter's script directories.
    for d in _script_dirs():
        try:
            if not d.is_dir():
                continue
        except OSError:
            continue
        for fname in _exe_names(name):
            cand = d / fname
            if cand.is_file():
                return (str(cand),)

    # 3. `python -m <module>` for module-runnable tools.
    mod = _MODULE_FALLBACK.get(name)
    if mod:
        try:
            import importlib.util
            if importlib.util.find_spec(mod) is not None:
                return (sys.executable, "-m", mod)
        except Exception:
            pass

    return None


def resolve_argv(name: str) -> list[str] | None:
    """Convenience: resolve(name) as a fresh mutable list (or None)."""
    r = resolve(name)
    return list(r) if r else None


def is_available(name: str) -> bool:
    """True if the console-script can be run one way or another."""
    return resolve(name) is not None
