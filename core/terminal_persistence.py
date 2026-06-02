"""
Terminal session persistence across app restarts.

The embedded terminals are owned by the GUI process, so closing the app tears
down the ConPTY shells and kills whatever is running inside them (``claude``,
``hermes``, …). We can't keep the processes alive without a separate daemon, but
many CLI tools persist their own session and can be *resumed*. So at shutdown we
record, per conversation, each tab's working directory and — if a known
resumable tool is running in it — the command that resumes it. On restart the
tabs are recreated in the same directory and the resume command is replayed
(e.g. ``claude --continue`` reopens the most recent conversation in that folder).

State lives in ``data/terminal_state.json`` (same convention as
``window_state.json`` / ``viewer_state.json``).
"""

from __future__ import annotations

import json
from pathlib import Path

_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "terminal_state.json"
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"

# tool name (matched as a substring of a descendant process's command line)
# -> command that resumes that tool in the same directory.
_DEFAULT_RESUME_RULES = {
    "claude": "claude --continue",
}


def _resume_rules() -> dict[str, str]:
    """Defaults, overlaid with any ``terminal_resume_rules`` from config.json.

    Lets the user teach the app how to resume their own tools (e.g. Hermes)
    without a code change:  "terminal_resume_rules": {"hermes": "hermes --resume"}
    """
    rules = dict(_DEFAULT_RESUME_RULES)
    try:
        cfg = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        extra = cfg.get("terminal_resume_rules") or {}
        if isinstance(extra, dict):
            rules.update({str(k).lower(): str(v) for k, v in extra.items()})
    except Exception:
        pass
    return rules


def detect_session(shell_pid: int) -> tuple[str | None, str | None]:
    """Inspect a shell process: return ``(cwd, resume_command)``.

    ``cwd`` is the shell's current working directory (where a resumed tool
    should reopen). ``resume_command`` is set only when a known resumable tool
    is found running under the shell; otherwise ``None`` (tab is recreated empty
    in ``cwd``).
    """
    if not shell_pid or shell_pid <= 0:
        return (None, None)
    try:
        import psutil
    except Exception:
        return (None, None)

    try:
        proc = psutil.Process(int(shell_pid))
    except Exception:
        return (None, None)

    cwd: str | None = None
    try:
        cwd = proc.cwd()
    except Exception:
        pass

    rules = _resume_rules()
    resume: str | None = None
    try:
        for child in proc.children(recursive=True):
            try:
                cmdline = " ".join(child.cmdline()).lower()
            except Exception:
                continue
            for tool, command in rules.items():
                if tool in cmdline:
                    resume = command
                    # The tool's own cwd is the most accurate resume location.
                    try:
                        cwd = child.cwd() or cwd
                    except Exception:
                        pass
                    break
            if resume:
                break
    except Exception:
        pass

    return (cwd, resume)


def load_state() -> dict:
    try:
        data = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("conversations"), dict):
            return data
    except Exception:
        pass
    return {"conversations": {}}


def save_state(state: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except Exception:
        pass
