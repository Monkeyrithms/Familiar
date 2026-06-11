"""Subprocess helpers — suppress the Windows console-window flash.

The app runs as a GUI process (pythonw / packaged .exe) that owns NO console of
its own. On Windows, whenever such a process spawns a *console* child (git,
ripgrep, ssh, cmd, powershell, node CLIs, …), the OS hands that child a brand-new
console window — which flashes on screen for a frame before the child exits. This
is the "terminal flicker" seen during tool calls.

Passing CREATE_NO_WINDOW to every such spawn tells Windows not to allocate that
console. NO_WINDOW is 0 on non-Windows platforms, so it is always safe to OR into
an existing ``creationflags`` value.

Prefer ``proc.run(...)`` / ``proc.popen(...)`` over ``subprocess.run`` /
``subprocess.Popen`` for any non-interactive child the app launches itself, so a
new call site can't silently reintroduce the flicker.
"""

import subprocess
import sys

# CREATE_NO_WINDOW only exists on Windows; 0 elsewhere so it OR-combines cleanly.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run(*args, **kwargs):
    """``subprocess.run`` that never flashes a console window on Windows."""
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | NO_WINDOW
    return subprocess.run(*args, **kwargs)


def popen(*args, **kwargs):
    """``subprocess.Popen`` that never flashes a console window on Windows."""
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | NO_WINDOW
    return subprocess.Popen(*args, **kwargs)
