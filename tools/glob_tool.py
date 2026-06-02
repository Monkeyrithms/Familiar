"""
Glob tool — list files matching a pattern.

Fills a critical gap: the agent needs a way to discover files by name
without relying on grep (content search) or terminal (ls/find).
Supports recursive glob patterns like '**/*.md'.
"""

import json
import os
from pathlib import Path
from tools.registry import registry

DEFAULT_MAX_FILES = 500
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "coverage", ".pytest_cache", ".cache", ".tox",
    "egg-info", ".mypy_cache",
}


def glob_files(pattern: str, path: str = None, max_results: int = None) -> str:
    """List files matching a glob pattern, sorted by modification time (newest first)."""
    search_root = Path(path) if path else Path.cwd()
    max_results = max_results or DEFAULT_MAX_FILES

    if not search_root.is_dir():
        return json.dumps({"error": f"Not a directory: {search_root}"})

    matches = []
    try:
        for p in search_root.glob(pattern):
            if not p.is_file():
                continue
            # Skip ignored directories
            parts = p.relative_to(search_root).parts
            if any(part in IGNORE_DIRS for part in parts):
                continue
            try:
                mtime = p.stat().st_mtime
                size = p.stat().st_size
            except OSError:
                mtime = 0
                size = 0
            matches.append({
                "path": str(p.relative_to(search_root)).replace("\\", "/"),
                "abs_path": str(p),
                "size": size,
                "mtime": mtime,
            })
            if len(matches) >= max_results * 2:  # gather extra for sorting
                break
    except Exception as e:
        return json.dumps({"error": f"Glob failed: {e}"})

    # Sort by modification time, newest first
    matches.sort(key=lambda m: m["mtime"], reverse=True)
    matches = matches[:max_results]

    # Format concise output
    lines = []
    for m in matches:
        size_str = _human_size(m["size"])
        lines.append(f"{m['path']}  ({size_str})")

    total = len(matches)
    output = "\n".join(lines) if lines else "No files matched."
    if total >= max_results:
        output += f"\n\n(capped at {max_results} results)"

    return json.dumps({
        "results": output,
        "count": total,
        "root": str(search_root),
    }, ensure_ascii=False)


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}TB"


registry.register(
    name="glob",
    description=(
        "List files by glob (e.g. '**/*.md', '*.py', 'docs/**'). Sorted by mtime (newest first).\n"
        "- ✓ discover by name|ext before reading.\n"
        "- ✓ auto-skips .git, node_modules, __pycache__."
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob (e.g. '**/*.py', 'src/**/*.ts').",
            },
            "path": {
                "type": "string",
                "description": "Root dir (default workspace).",
            },
            "max_results": {
                "type": "integer",
                "description": "Max files (default 500).",
            },
        },
        "required": ["pattern"],
    },
    execute=glob_files,
)
