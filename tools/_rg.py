"""
Shared ripgrep locator.

The grep tool already finds and uses ripgrep; this exposes the same lookup so
other tools (file_search's traversal, etc.) can reuse rg's fast, parallel,
gitignore-aware walk instead of re-implementing the search path logic.
"""

import os
import shutil

# Known locations for rg.exe on Windows when it isn't on PATH (mirrors grep.py).
_RG_SEARCH_PATHS = [
    os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\BurntSushi.ripgrep.MSVC_Microsoft.Winget.Source_8wekyb3d8bbwe\ripgrep-15.1.0-x86_64-pc-windows-msvc\rg.exe"
    ),
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\_\resources\app\node_modules\@vscode\ripgrep\bin\rg.exe"
    ),
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\cursor\resources\app\node_modules\@vscode\ripgrep\bin\rg.exe"
    ),
]


def find_rg() -> str | None:
    """Locate the ripgrep binary, or None if unavailable."""
    rg = shutil.which("rg")
    if rg:
        return rg
    for path in _RG_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


RG_PATH = find_rg()
