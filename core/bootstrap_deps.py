"""
Prerequisite bootstrap — run by START.bat BEFORE main.py.

Why a separate pre-launch step (not main()'s check_prerequisites): Python binds
imports at process start, so a dep installed *during* a run (e.g. pywinpty)
won't take effect until the next launch — the terminal would still be in
pipe-mode this session. Running here, in its own process before main.py is
imported, means a fresh machine self-heals on the very first launch.

Fast path: if every critical package already imports, this returns in a blink
and START.bat proceeds. Only on a miss does it pip-install requirements.txt.
Exit code 1 (gate the launch) only if deps are STILL missing after install.

Lives in core/ (app code, not UI). requirements.txt stays at the repo root, so
paths resolve one level up from here.
"""

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root (core/ is one down)
REQ = ROOT / "requirements.txt"

# (import_name, pip_name) — packages whose absence silently breaks the app
# (e.g. no pywinpty ⇒ pipe-mode terminals ⇒ claude/cursor/codex won't run).
_CRITICAL: list[tuple[str, str]] = [
    ("PyQt6.QtWidgets", "PyQt6"),
    ("pyte", "pyte"),
    ("tree_sitter", "tree-sitter"),
    ("tree_sitter_language_pack", "tree-sitter-language-pack"),
    ("sqlite_vec", "sqlite-vec"),
    ("watchdog", "watchdog"),
    ("openai", "openai"),
]
if sys.platform == "win32":
    _CRITICAL.append(("winpty", "pywinpty"))      # ConPTY → real TTY terminals
else:
    _CRITICAL.append(("ptyprocess", "ptyprocess"))


def _missing() -> list[str]:
    out = []
    for mod, pip_name in _CRITICAL:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pip_name)
    return out


def _pip(*args) -> int:
    return subprocess.call([sys.executable, "-m", "pip", "install", *args])


def main() -> int:
    miss = _missing()
    if not miss:
        return 0  # fast path — everything's here

    print(f"[Familiar] Missing prerequisites: {', '.join(sorted(set(miss)))}")
    print("[Familiar] Installing — one moment...\n")

    if REQ.exists():
        _pip("-r", str(REQ))
    else:
        # No requirements file next to us — install the missing ones directly.
        _pip(*sorted(set(miss)))

    still = _missing()
    if still:
        print(f"\n[Familiar] STILL missing after install: {', '.join(sorted(set(still)))}")
        print("Check the pip errors above (network? wrong Python?), then relaunch.")
        return 1
    print("\n[Familiar] Prerequisites OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
