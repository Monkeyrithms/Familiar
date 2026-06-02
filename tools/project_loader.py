"""
Project context loader — survey a project's structure and cache the map.
Respects .gitignore patterns. Builds a summary the agent can reference
without re-scanning every time.
"""

import json
import os
from pathlib import Path
from tools.registry import registry

IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "coverage", ".pytest_cache", ".cache", ".tox", "egg-info",
}
IGNORE_EXTENSIONS = {".pyc", ".pyo", ".exe", ".dll", ".so", ".dylib", ".o", ".obj"}
MAX_FILES = 5000
# Cap per-file bytes scanned for line counting. Files larger than this get an
# estimated line count from a sampled chunk rather than a full read — a full
# decode of every file is what stalls the UI thread (GIL held the whole time).
LINE_COUNT_BYTE_CAP = 1_000_000


def _count_lines(fp: Path) -> int:
    """Cheap newline count via chunked BINARY reads (no utf-8 decode, GIL
    released on each read()). For files over the cap, estimate from a sample
    so a single huge file can't dominate a survey."""
    try:
        size = fp.stat().st_size
    except Exception:
        return 0
    if size == 0:
        return 0
    try:
        with fp.open("rb") as f:
            if size <= LINE_COUNT_BYTE_CAP:
                newlines = 0
                while True:
                    block = f.read(65536)
                    if not block:
                        break
                    newlines += block.count(b"\n")
                return newlines
            # Large file: sample the first cap bytes, scale up.
            sample = f.read(LINE_COUNT_BYTE_CAP)
            sample_nl = sample.count(b"\n")
            if sample_nl == 0:
                return 0
            return int(sample_nl * (size / LINE_COUNT_BYTE_CAP))
    except Exception:
        return 0


def _load_gitignore(root: Path) -> list[str]:
    gi = root / ".gitignore"
    if gi.exists():
        return [l.strip() for l in gi.read_text(errors="replace").splitlines()
                if l.strip() and not l.startswith("#")]
    return []


def _should_ignore(path: Path, root: Path, gitignore: list[str]) -> bool:
    rel = str(path.relative_to(root)).replace("\\", "/")
    for pattern in gitignore:
        if pattern.endswith("/") and pattern.rstrip("/") in rel.split("/"):
            return True
        if path.name == pattern or rel.endswith(pattern):
            return True
    return False


def project_context(action: str, path: str = "", depth: int = 3) -> str:
    """Load and summarize a project's structure."""

    if action == "survey":
        if not path:
            # Default to current workspace
            try:
                from core.agent import load_config
                cfg = load_config()
                ws_name = cfg.get("default_workspace", "")
                workspaces = cfg.get("workspaces", {})
                if ws_name and ws_name in workspaces:
                    ws = workspaces[ws_name]
                    raw = ws.get("path", ws) if isinstance(ws, dict) else ws
                    from core.workspace_paths import resolve_workspace_entry_path
                    path = str(resolve_workspace_entry_path(str(raw) if raw else ""))
            except Exception:
                pass
        if not path:
            return json.dumps({"error": "path required — pass a path or set a workspace"})
        root = Path(path)
        if not root.is_dir():
            return json.dumps({"error": f"Not a directory: {path}"})

        gitignore = _load_gitignore(root)
        tree = {}
        file_count = 0
        total_lines = 0
        extensions = {}

        for dirpath, dirnames, filenames in os.walk(root):
            # Filter ignored dirs
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            dp = Path(dirpath)
            rel_dir = str(dp.relative_to(root)).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""

            if _should_ignore(dp, root, gitignore):
                dirnames.clear()
                continue

            # Honor depth: stop descending past `depth` levels below root.
            # (depth<=0 means root only.) Pruning dirnames stops os.walk from
            # walking — and full-reading — an entire deep tree the caller
            # explicitly bounded.
            cur_depth = 0 if rel_dir == "" else rel_dir.count("/") + 1
            if cur_depth >= depth:
                dirnames.clear()

            for fname in filenames:
                fp = dp / fname
                if fp.suffix in IGNORE_EXTENSIONS:
                    continue
                if _should_ignore(fp, root, gitignore):
                    continue

                file_count += 1
                if file_count > MAX_FILES:
                    return json.dumps({
                        "error": f"Project too large (>{MAX_FILES} files). Use a more specific path."
                    })

                ext = fp.suffix or "(no ext)"
                extensions[ext] = extensions.get(ext, 0) + 1

                lines = _count_lines(fp)
                total_lines += lines

                key = rel_dir or "."
                if key not in tree:
                    tree[key] = []
                tree[key].append({"name": fname, "lines": lines, "ext": ext})

        # Build summary
        top_dirs = sorted(tree.keys())[:50]
        summary_tree = {}
        for d in top_dirs:
            files = tree[d]
            if len(files) > 50:
                summary_tree[d] = f"{len(files)} files"
            else:
                summary_tree[d] = [f["name"] for f in files]

        return json.dumps({
            "root": path,
            "files": file_count,
            "lines": total_lines,
            "extensions": dict(sorted(extensions.items(), key=lambda x: -x[1])[:15]),
            "tree": summary_tree,
        }, ensure_ascii=False)

    elif action == "files":
        if not path:
            return json.dumps({"error": "path required"})
        root = Path(path)
        gitignore = _load_gitignore(root)
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            dp = Path(dirpath)
            if _should_ignore(dp, root, gitignore):
                dirnames.clear()
                continue
            for fname in filenames:
                fp = dp / fname
                if fp.suffix in IGNORE_EXTENSIONS or _should_ignore(fp, root, gitignore):
                    continue
                rel = str(fp.relative_to(root)).replace("\\", "/")
                try:
                    size = fp.stat().st_size
                except Exception:
                    size = 0
                files.append({"path": rel, "size": size, "ext": fp.suffix})
                if len(files) >= MAX_FILES:
                    break
        return json.dumps({"root": path, "files": files[:2000], "total": len(files)})

    else:
        return json.dumps({"error": "action must be: survey, files"})


registry.register(
    name="project",
    description=(
        "Project structure survey. Respects .gitignore.\n"
        "- survey: tree summary + file|line counts + ext breakdown.\n"
        "- files: full file listing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["survey", "files"]},
            "path": {"type": "string", "description": "Project root."},
            "depth": {"type": "integer", "description": "Max dir depth (default 3)."},
        },
        "required": ["action", "path"],
    },
    execute=project_context,
)
