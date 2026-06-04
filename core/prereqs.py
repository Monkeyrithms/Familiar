"""
Startup prerequisite check - fails fast with a clear message if required
tools are missing rather than letting the agent run and give silent/wrong
results. Call check_prerequisites() early in main() before the UI starts.
"""

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

_REQ = Path(__file__).resolve().parent.parent / "requirements.txt"


def _scan(resolve_tool):
    """Return (missing_fatal_bins, missing_packages, missing_optional)."""
    missing_fatal_bins: list[tuple[str, str]] = []
    missing_packages: list[tuple[str, str]] = []
    missing_optional: list[tuple[str, str]] = []

    for cmd, pkg, fatal in _REQUIRED_BINS:
        # resolve() also finds tools in this interpreter's Scripts dir, so a
        # pip-installed ruff/pyflakes/pylsp counts even when it's not on PATH —
        # which is what used to make this gate fail right after a fresh install.
        if fatal and resolve_tool(cmd) is None and shutil.which(cmd) is None:
            missing_fatal_bins.append((cmd, pkg))

    for mod, pkg in _REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing_packages.append((mod, pkg))

    # Platform-specific pseudo-terminal backend — without it, the integrated
    # terminal drops to pipe mode and interactive CLIs (claude/cursor/codex)
    # refuse to run (no TTY).
    pty_mod, pty_pkg = (("winpty", "pywinpty") if sys.platform == "win32"
                        else ("ptyprocess", "ptyprocess"))
    try:
        importlib.import_module(pty_mod)
    except ImportError:
        missing_packages.append((pty_mod, pty_pkg))

    for cmd, pkg in _OPTIONAL_BINS:
        if shutil.which(cmd) is None:
            missing_optional.append((cmd, pkg))

    return missing_fatal_bins, missing_packages, missing_optional


def _auto_install(pkgs: list[str]) -> None:
    """Best-effort: install the missing requirements into THIS interpreter.

    Prefer the pinned requirements.txt so versions stay coherent; fall back to
    the specific pip names if it isn't present. Routed through sys.executable so
    it always lands in the Python the app is actually running under (the usual
    cause of a "I installed it but it still says missing" mismatch)."""
    print("[Agent] Installing missing dependencies — one moment...\n")
    try:
        if _REQ.exists():
            subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(_REQ)])
        elif pkgs:
            subprocess.call([sys.executable, "-m", "pip", "install", *pkgs])
    except Exception as e:
        print(f"[Agent] Auto-install failed: {e}")

# CLI tools that must be on PATH. (cmd, pip_install_hint, fatal)
_REQUIRED_BINS: list[tuple[str, str, bool]] = [
    ("ruff",     "ruff",               True),
    ("pyflakes", "pyflakes",           True),
    ("pylsp",    "python-lsp-server",  True),
]

# Python packages that must be importable for full agent functionality.
# (module_name, pip_install_hint)
_REQUIRED_PACKAGES: list[tuple[str, str]] = [
    ("tree_sitter",                "tree-sitter"),
    ("tree_sitter_language_pack",  "tree-sitter-language-pack"),
    ("sqlite_vec",                 "sqlite-vec"),
    ("pyte",                       "pyte"),  # VT screen — interactive terminals
]

# Non-Python language tools: warn but don't block (user may not work with them)
_OPTIONAL_BINS: list[tuple[str, str]] = [
    ("node",                        "nodejs"),
    ("typescript-language-server",  "typescript-language-server (npm)"),
    ("gopls",                       "gopls (go install golang.org/x/tools/gopls@latest)"),
    ("rust-analyzer",               "rust-analyzer"),
]


def check_prerequisites() -> None:
    """Ensure required tools/packages are present, installing what's missing.

    This is the safety net for launch paths that DON'T go through START.bat's
    bootstrap (restart.bat, the IDE, a bare `py main.py`): rather than just
    printing "install it yourself and restart", it installs the missing pieces
    into the running interpreter and re-checks. Genuine import dependencies that
    still can't load are fatal (the app can't run without them — relaunch so the
    fresh install is imported). The CLI tools (ruff/pyflakes/pylsp) only degrade
    code-quality checks, so a stubborn miss warns instead of blocking launch."""
    from core.tool_resolver import resolve as _resolve_tool

    missing_fatal_bins, missing_packages, missing_optional = _scan(_resolve_tool)

    # Self-heal anything required that's missing, then re-scan.
    if missing_fatal_bins or missing_packages:
        pkgs = sorted({pkg for _, pkg in missing_fatal_bins}
                      | {pkg for _, pkg in missing_packages})
        _auto_install(pkgs)
        try:
            _resolve_tool.cache_clear()  # forget pre-install "not found" results
        except Exception:
            pass
        missing_fatal_bins, missing_packages, missing_optional = _scan(_resolve_tool)

    if missing_optional:
        print(
            "\n[Agent] Optional language tools not found (non-Python linting "
            "won't work for these):"
        )
        for cmd, pkg in missing_optional:
            print(f"  * {cmd}  ->  install: {pkg}")

    # CLI tools that only reduce functionality: warn, don't block.
    if missing_fatal_bins:
        print(
            "\n[Agent] These code-quality tools couldn't be installed/resolved "
            "(Python linting will be reduced — the app still runs):"
        )
        for cmd, pkg in missing_fatal_bins:
            print(f"  * {cmd}  (pip install {pkg})")

    # Genuine import dependencies the app can't run without. They were just
    # installed but can't be imported into THIS already-started process —
    # relaunch picks them up. Only these gate the launch.
    if missing_packages:
        print(
            "\n[Agent] Required packages were installed but need a restart to "
            "load. Please relaunch Familiar:\n"
        )
        for mod, pkg in missing_packages:
            print(f"  * {mod}  (pip install {pkg})")
        print()
        sys.exit(1)
