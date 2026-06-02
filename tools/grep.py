"""
Grep tool - search file contents with regex, powered by ripgrep.

Uses ripgrep (rg) as a subprocess for 10-100x faster search on large
codebases. Results are sorted by file modification time (newest first)
so recently-changed code surfaces at the top.
"""

import json
import os
import subprocess
import shutil
from pathlib import Path
from tools.registry import registry

DEFAULT_MAX_RESULTS = 300

# Known locations for rg.exe on Windows (winget install path)
_RG_SEARCH_PATHS = [
    # WinGet install
    os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\BurntSushi.ripgrep.MSVC_Microsoft.Winget.Source_8wekyb3d8bbwe\ripgrep-15.1.0-x86_64-pc-windows-msvc\rg.exe"
    ),
    # VS Code bundled
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\_\resources\app\node_modules\@vscode\ripgrep\bin\rg.exe"
    ),
    # Cursor bundled
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\cursor\resources\app\node_modules\@vscode\ripgrep\bin\rg.exe"
    ),
]


def _find_rg() -> str | None:
    """Find the ripgrep binary."""
    # Check PATH first
    rg = shutil.which("rg")
    if rg:
        return rg
    # Check known locations
    for path in _RG_SEARCH_PATHS:
        if os.path.isfile(path):
            return path
    return None


_rg_path = _find_rg()


def grep(pattern: str, path: str = None, glob: str = None,
         max_results: int = None) -> str:
    """Search for a regex pattern in files using ripgrep."""
    search_path = path or str(Path.cwd())
    max_results = max_results or DEFAULT_MAX_RESULTS

    if not _rg_path:
        return json.dumps({"error": (
            "ripgrep (rg) not found. Install it: winget install BurntSushi.ripgrep.MSVC "
            "or download from https://github.com/BurntSushi/ripgrep/releases"
        )})

    # Build rg command
    cmd = [
        _rg_path,
        "--json",           # Structured JSON output
        "--max-count", "15", # Max 15 matches per file
        "--max-columns", "500",  # Truncate long lines
        pattern,
        search_path,
    ]

    # File type filtering
    if glob:
        cmd.extend(["--glob", glob])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Search timed out after 30 seconds."})
    except FileNotFoundError:
        return json.dumps({"error": f"ripgrep binary not found at {_rg_path}"})

    # Parse JSON output lines
    matches_by_file: dict[str, list[dict]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        if msg.get("type") != "match":
            continue

        data = msg.get("data", {})
        file_path = data.get("path", {}).get("text", "")
        line_number = data.get("line_number", 0)
        line_text = data.get("lines", {}).get("text", "").rstrip("\n\r")

        if not file_path:
            continue

        if file_path not in matches_by_file:
            matches_by_file[file_path] = []
        matches_by_file[file_path].append({
            "line": line_number,
            "text": line_text,
        })

    if not matches_by_file:
        return json.dumps({"results": "No matches found."})

    # Sort files by modification time (newest first)
    def _mtime(filepath: str) -> float:
        try:
            return os.path.getmtime(filepath)
        except OSError:
            return 0.0

    sorted_files = sorted(matches_by_file.keys(), key=_mtime, reverse=True)

    # Format output
    output_lines = []
    total = 0
    base = Path(search_path) if os.path.isdir(search_path) else Path(search_path).parent

    for fpath in sorted_files:
        if total >= max_results:
            break
        for match in matches_by_file[fpath]:
            if total >= max_results:
                break
            try:
                rel = str(Path(fpath).relative_to(base))
            except ValueError:
                rel = fpath
            output_lines.append(f"{rel}:{match['line']}:{match['text']}")
            total += 1

    output = "\n".join(output_lines)
    if total >= max_results:
        remaining = sum(len(m) for m in matches_by_file.values()) - total
        if remaining > 0:
            output += f"\n\n(showing {total} of {total + remaining}+ matches, capped at {max_results})"

    return json.dumps({"results": output}, ensure_ascii=False)


registry.register(
    name="grep",
    description=(
        "Regex content search via ripgrep. → path:line:text, sorted by mtime (newest first).\n"
        "- glob filters by type (e.g. '*.py').\n"
        "- ✓ auto-skips .git, node_modules, binaries."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern.",
            },
            "path": {
                "type": "string",
                "description": "Dir|file to search (default cwd).",
            },
            "glob": {
                "type": "string",
                "description": "File filter (e.g. '*.py', '*.{ts,tsx}').",
            },
            "max_results": {
                "type": "integer",
                "description": "Max matches (default 100).",
            },
        },
        "required": ["pattern"],
    },
    execute=grep,
)
