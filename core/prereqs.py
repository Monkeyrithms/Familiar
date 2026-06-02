"""
Startup prerequisite check - fails fast with a clear message if required
tools are missing rather than letting the agent run and give silent/wrong
results. Call check_prerequisites() early in main() before the UI starts.
"""

import importlib
import shutil
import sys

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
    """Check all required tools/packages. Prints a summary and exits with
    code 1 if any fatal prerequisite is missing."""
    missing_fatal_bins: list[tuple[str, str]] = []
    missing_packages: list[tuple[str, str]] = []
    missing_optional: list[tuple[str, str]] = []

    for cmd, pkg, fatal in _REQUIRED_BINS:
        if shutil.which(cmd) is None and fatal:
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

    if missing_optional:
        print(
            "\n[Agent] Optional language tools not found (non-Python linting "
            "won't work for these):"
        )
        for cmd, pkg in missing_optional:
            print(f"  * {cmd}  ->  install: {pkg}")

    if missing_fatal_bins or missing_packages:
        print(
            "\n[Agent] MISSING REQUIRED DEPENDENCIES - install and restart:\n"
            "  pip install -r requirements.txt\n"
        )
        for cmd, pkg in missing_fatal_bins:
            print(f"  * binary:         {cmd}  (pip install {pkg})")
        for mod, pkg in missing_packages:
            print(f"  * python package: {mod}  (pip install {pkg})")
        print()
        sys.exit(1)
