"""
Auto titles for integrated terminal tabs.

Priority: user-given name > foreground application > cwd folder name (after the
user has done something) > default ``Terminal N``.
"""

from __future__ import annotations

import os
import re

_SHELL_EXE = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "bash", "sh", "zsh", "fish",
    "nu.exe", "conhost.exe", "windowsterminal.exe", "wt.exe",
})


def is_generic_title(title: str) -> bool:
    t = (title or "").strip()
    if not t:
        return True
    return bool(re.fullmatch(r"Terminal\s+\d+", t, re.IGNORECASE))


def foreground_app_name(shell_pid: int) -> str | None:
    """Basename of the deepest non-shell child process, if any."""
    if not shell_pid or shell_pid <= 0:
        return None
    try:
        import psutil
        proc = psutil.Process(int(shell_pid))
    except Exception:
        return None

    candidates: list[tuple[int, int, str]] = []
    try:
        for child in proc.children(recursive=True):
            try:
                exe = child.name()
            except Exception:
                continue
            base = os.path.splitext(exe)[0].lower()
            if base in _SHELL_EXE or exe.lower() in _SHELL_EXE:
                continue
            try:
                n_children = len(child.children())
            except Exception:
                n_children = 0
            candidates.append((n_children, child.pid, base or exe))
    except Exception:
        return None

    if not candidates:
        return None

    leaves = [c for c in candidates if c[0] == 0]
    _depth, _pid, name = max(leaves or candidates, key=lambda x: x[1])
    name = (name or "").strip()
    return name[:48] if name else None


def live_shell_cwd(shell_pid: int) -> str | None:
    if not shell_pid or shell_pid <= 0:
        return None
    try:
        from core.terminal_persistence import detect_session
        cwd, _resume = detect_session(shell_pid)
        if cwd and os.path.isdir(cwd):
            return cwd
    except Exception:
        pass
    return None


def cwd_folder_title(cwd: str) -> str | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    base = os.path.basename(os.path.normpath(cwd))
    return (base or cwd)[:48]


def auto_tab_title(
    shell_pid: int,
    spawn_cwd: str,
    last_command: str,
    last_prompt_cwd: str = "",
) -> str | None:
    """Return an automatic title, or None to keep the default ``Terminal N``."""
    app = foreground_app_name(shell_pid)
    if app:
        return app

    acted = bool((last_command or "").strip())
    live = live_shell_cwd(shell_pid) or (last_prompt_cwd if last_prompt_cwd else "")
    spawn_norm = os.path.normcase(os.path.abspath(spawn_cwd or ""))
    live_norm = os.path.normcase(os.path.abspath(live)) if live else ""

    if live_norm and live_norm != spawn_norm:
        acted = True

    if not acted:
        return None

    folder = cwd_folder_title(live or spawn_cwd)
    return folder
