"""
Prerequisite bootstrap — run by START.bat BEFORE main.py.

Why a separate pre-launch step (not main()'s check_prerequisites): Python binds
imports at process start, so a dep installed *during* a run (e.g. pywinpty)
won't take effect until the next launch — the terminal would still be in
pipe-mode this session. Running here, in its own process before main.py is
imported, means a fresh machine self-heals on the very first launch.

What it guarantees (this is the part that used to leak):
  * Every package in requirements.txt is installed — not just a hand-picked
    "critical" subset. The old version fast-pathed whenever 8 import checks
    passed, so anything else in requirements.txt (ruff, pyflakes, pylsp, numpy,
    vispy, WebEngine, …) was silently skipped on a machine that happened to have
    those 8. That's exactly how `ruff` went missing despite being REQUIRED.

How it stays fast: a stamp file records the hash of requirements.txt that was
last installed. If the hash matches AND the launch-critical imports resolve, we
return in a blink. We only run pip when the requirements file changed (a dep was
added/bumped) or a critical import is missing (fresh / broken environment).

Exit code 1 (gate the launch) only if a launch-CRITICAL import is STILL missing
after install. Tools that merely degrade the app (ruff/pyflakes/pylsp) are
installed and verified, but a stubborn miss warns rather than blocks.

Lives in core/ (app code, not UI). requirements.txt stays at the repo root, so
paths resolve one level up from here.
"""

import hashlib
import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent   # repo root (core/ is one down)
REQ = ROOT / "requirements.txt"
STAMP = ROOT / "data" / ".deps_ok"               # records the installed req hash

# Make `import core.tool_resolver` work even though START.bat runs this file by
# path (so sys.path[0] is core/, not the repo root).
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# (import_name, pip_name) — packages whose absence is fatal to launch
# (e.g. no pywinpty ⇒ pipe-mode terminals ⇒ claude/cursor/codex won't run).
_CRITICAL: list[tuple[str, str]] = [
    ("PyQt6.QtWidgets", "PyQt6"),
    ("markdown2", "markdown2"),   # imported at startup in ui/chat_widget — fatal
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

# Console-scripts from requirements.txt that the agent shells out to. Not fatal
# to launch (lint degrades to ast/py_compile, LSP just goes dark), but we want
# them present and resolvable so the user never has to install them by hand.
_REQUIRED_BINS = ["ruff", "pyflakes", "pylsp"]


def _missing_imports() -> list[str]:
    out = []
    for mod, pip_name in _CRITICAL:
        try:
            importlib.import_module(mod)
        except Exception:
            out.append(pip_name)
    return out


def _missing_bins() -> list[str]:
    """Required console-scripts that can't be resolved (PATH or Scripts dir)."""
    try:
        from core.tool_resolver import resolve
        resolve.cache_clear()  # don't trust a pre-install cached miss
    except Exception:
        return []  # resolver unavailable → skip bin verification, never block
    return [b for b in _REQUIRED_BINS if resolve(b) is None]


def _req_hash() -> str:
    """Identity of the dependency state we'd be installing: requirements.txt
    content + the interpreter it lands in. Keying on the interpreter means
    upgrading/switching Python (whose site-packages differ) forces a fresh
    verify instead of wrongly trusting another interpreter's stamp."""
    try:
        h = hashlib.sha256(REQ.read_bytes())
        h.update(sys.executable.encode("utf-8", "replace"))
        h.update(sys.version.encode("utf-8", "replace"))
        return h.hexdigest()
    except Exception:
        return ""


def _read_stamp() -> str:
    try:
        return STAMP.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def _write_stamp(value: str) -> None:
    try:
        STAMP.parent.mkdir(parents=True, exist_ok=True)
        STAMP.write_text(value, encoding="utf-8")
    except Exception:
        pass


def _pip(*args) -> int:
    return subprocess.call([sys.executable, "-m", "pip", "install", *args])


def main() -> int:
    miss_imp = _missing_imports()
    cur_hash = _req_hash()
    stamp = _read_stamp()

    # Fast path: requirements unchanged since the last successful install AND
    # every launch-critical import resolves. Bins are NOT part of this gate (a
    # bin pip genuinely can't provide must not force a reinstall every launch).
    if not miss_imp and cur_hash and stamp == cur_hash:
        return 0

    # Decide whether to run pip. We install when the environment looks fresh or
    # changed: a missing critical import, or a requirements file we haven't
    # installed yet (new machine, or a dep was added/bumped).
    need_install = bool(miss_imp) or (stamp != cur_hash)
    if need_install:
        why = []
        if miss_imp:
            why.append(f"missing: {', '.join(sorted(set(miss_imp)))}")
        if stamp != cur_hash:
            why.append("requirements.txt changed" if stamp else "first run")
        print(f"[Familiar] Installing dependencies ({'; '.join(why)}) — one moment...\n")

        if REQ.exists():
            _pip("-r", str(REQ))
        else:
            # No requirements file next to us — install the missing critical ones.
            _pip(*sorted(set(miss_imp)) or ["openai"])

    # Verify the launch-critical imports actually resolve now.
    still_imp = _missing_imports()
    if still_imp:
        print(f"\n[Familiar] STILL missing after install: {', '.join(sorted(set(still_imp)))}")
        print("Check the pip errors above (network? wrong Python?), then relaunch.")
        return 1

    # Tools that only degrade functionality: install/verify, but don't block.
    still_bins = _missing_bins()
    if still_bins:
        print(
            f"[Familiar] Note: these tools didn't resolve after install: "
            f"{', '.join(still_bins)}. The app will run; code-quality checks for "
            f"Python may be reduced until they're available."
        )

    # Record success so the next launch fast-paths. Stamp the requirements hash
    # even if a non-critical bin lagged — re-running pip wouldn't conjure a
    # package pip already couldn't provide, and we don't want a reinstall loop.
    if cur_hash:
        _write_stamp(cur_hash)
    if need_install:
        print("\n[Familiar] Prerequisites OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
