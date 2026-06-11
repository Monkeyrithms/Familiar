"""
explore_files — cheap parallel file summarization swarm.

Spawns sub-agents in parallel that pre-read files in Python, then make ONE
cheap LLM call (default Haiku) to produce per-file summaries. Returns a
flat {path: summary} map to the main agent — raw file content never enters
the main agent's context.

Why this exists: when the main expensive model calls file_read 20 times to
"understand the codebase before changing a button color", every file's full
content sits in its context for the rest of the conversation. This tool
keeps that junk out of context — only compact summaries come back.

Async by design: returns a job_id immediately. Main agent should keep
working on independent tasks, then call action='wait' when it actually
needs the summaries.

Token-budget batching: tiny files get bundled into one worker; huge files
get their own worker. No file is ever split across workers.
"""

import fnmatch
import json
import threading
from pathlib import Path

from tools.registry import registry


# Path components that, if seen anywhere in the path, skip the file.
_DEFAULT_DIR_IGNORES = {
    "__pycache__", ".git", ".svn", ".hg",
    "node_modules", ".venv", "venv", "env", ".env",
    "dist", "build", "target", "out",
    ".next", ".nuxt", ".turbo", ".cache",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".idea", ".vscode",
}

# Filename patterns to skip.
_DEFAULT_FILE_GLOBS = {
    "*.pyc", "*.pyo", "*.so", "*.dll", "*.dylib", "*.o", "*.a",
    "*.min.js", "*.min.css", "*.map",
    "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "Cargo.lock",
    "*.exe", "*.bin",
    "*.png", "*.jpg", "*.jpeg", "*.gif", "*.ico", "*.webp", "*.svg",
    "*.mp3", "*.mp4", "*.wav", "*.ogg",
    "*.zip", "*.tar", "*.gz", "*.7z", "*.rar",
    "*.pdf", "*.docx", "*.xlsx", "*.pptx",  # parseable but heavy — skip by default
}


def _is_ignored(path: Path, extra_globs: set) -> bool:
    """Check if a path should be ignored based on directory and filename patterns."""
    for part in path.parts:
        if part in _DEFAULT_DIR_IGNORES:
            return True
    name = path.name
    for pat in _DEFAULT_FILE_GLOBS:
        if fnmatch.fnmatch(name, pat):
            return True
    for pat in extra_globs:
        if fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(str(path), pat):
            return True
    return False


def _expand_patterns(patterns: list, extra_ignores: list) -> list:
    """Expand a mix of file paths, dirs, and glob patterns into a flat
    deduped list of Path objects, with ignores applied."""
    extra_globs = set(extra_ignores or [])
    found: list = []
    seen: set = set()

    def _add(p: Path):
        if not p.is_file():
            return
        if _is_ignored(p, extra_globs):
            return
        s = str(p.resolve())
        if s in seen:
            return
        seen.add(s)
        found.append(p)

    for pat in patterns:
        p = Path(pat)
        if p.is_file():
            _add(p)
            continue
        if p.is_dir():
            for sub in p.rglob("*"):
                _add(sub)
            continue
        # Glob pattern. Use stdlib glob, NOT Path(".").glob: pathlib rejects
        # absolute patterns ("D:\\proj\\*.py"), which silently returned zero
        # matches for any absolute-path glob. stdlib glob handles absolute
        # anchors and recursive ** transparently.
        import glob as _glob
        try:
            for hit in _glob.glob(pat, recursive=("**" in pat)):
                m = Path(hit)
                if m.is_dir():
                    for sub in m.rglob("*"):
                        _add(sub)
                else:
                    _add(m)
        except Exception:
            pass
    return found



def _estimate_tokens(path: Path) -> int:
    """Rough token estimate: bytes / 4. Cheap and good enough for batching."""
    try:
        return max(1, path.stat().st_size // 4)
    except Exception:
        return 100


def _pack_batches(files: list, budget_tokens: int) -> list:
    """Greedy bin-packing into batches under budget_tokens.

    A file larger than budget gets its own solo batch (we never split a
    single file across workers — Haiku's context easily handles single
    large files, and reassembly logic isn't worth the complexity).
    """
    batches: list = []
    current: list = []
    current_tokens = 0

    for f in files:
        ft = _estimate_tokens(f)
        if ft > budget_tokens:
            if current:
                batches.append(current)
                current = []
                current_tokens = 0
            batches.append([str(f)])
            continue
        if current and current_tokens + ft > budget_tokens:
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(str(f))
        current_tokens += ft

    if current:
        batches.append(current)
    return batches


def _parse_delimited(raw: str, expected_paths: list) -> dict:
    """Parse '### <path>\\n<summary>' delimited output into {path: summary}.

    Resilient to slight model formatting drift: matches headers against
    expected paths by exact / suffix match."""
    results: dict = {}
    if not raw:
        return results

    text = raw.strip()
    # Find the first '### ' header — discard any preamble before it
    first = text.find("### ")
    if first < 0:
        # No headers at all — attach whole output to first expected file
        if expected_paths:
            results[expected_paths[0]] = text
        return results
    text = text[first + 4:]

    sections = text.split("\n### ")
    for sec in sections:
        if not sec.strip():
            continue
        parts = sec.split("\n", 1)
        header = parts[0].strip()
        body = (parts[1].strip() if len(parts) > 1 else "")
        # Try to match the header to an expected path
        matched = None
        for ep in expected_paths:
            if header == ep:
                matched = ep
                break
        if matched is None:
            for ep in expected_paths:
                if header.endswith(ep) or ep.endswith(header):
                    matched = ep
                    break
        results[matched or header] = body
    return results


def explore_files(action: str, patterns: list = None, query: str = "",
                   batch_tokens: int = 20000, ignore: list = None,
                   job_id: str = "", ctx=None) -> str:
    """Cheap parallel file exploration. See tool description."""
    from core.subagent import get_orchestrator, get_existing
    from tools.subagent_tool import _bridge

    if action == "start":
        if not patterns:
            return json.dumps({"error": "patterns is required (list of paths/globs)"})
        if not query:
            query = "Summarize the file's purpose and key contents."

        files = _expand_patterns(patterns, ignore or [])
        if not files:
            return json.dumps({
                "error": "No files matched (after default ignores).",
                "patterns": patterns,
                "hint": "Default ignores: __pycache__, .git, node_modules, .venv, *.pyc, lockfiles, binaries, images.",
            })

        batches = _pack_batches(files, budget_tokens=max(1000, batch_tokens))

        orch = get_orchestrator()
        orch._workspace = ctx.cwd if ctx else ""
        conv_id = getattr(ctx, "conv_id", "") if ctx else ""
        orch._conv_id = conv_id

        from core.agent import load_config
        config = load_config()
        orch._provider = (config.get("subagent_explore_provider")
                          or config.get("provider", "anthropic"))
        orch._model = (config.get("subagent_explore_model")
                       or "claude-haiku-4-5")

        tasks = []
        for batch in batches:
            tasks.append(orch.add_explore_task(files=batch, query=query))

        if _bridge:
            _bridge.job_started.emit(orch._job_id,
                                      json.dumps([t.to_dict() for t in tasks]),
                                      conv_id)

        def _run():
            summary = orch.execute()
            if _bridge:
                _bridge.job_completed.emit(orch._job_id, json.dumps(summary), conv_id)

        threading.Thread(target=_run, daemon=True).start()

        return json.dumps({
            "job_id": orch._job_id,
            "status": "dispatched",
            "files_total": len(files),
            "batches": len(batches),
            "model": orch._model,
            "message": (
                f"Exploring {len(files)} files in {len(batches)} parallel workers "
                f"using {orch._model}. Returns immediately — call "
                f"action='wait' job_id='{orch._job_id}' when you need the "
                f"summaries. Continue with other independent work meanwhile."
            ),
        })

    elif action == "status":
        if not job_id:
            return json.dumps({"error": "job_id required"})
        orch = get_existing(job_id)
        if not orch:
            return json.dumps({"error": f"No job '{job_id}'"})
        return json.dumps(orch.get_status())

    elif action == "wait":
        if not job_id:
            return json.dumps({"error": "job_id required"})
        orch = get_existing(job_id)
        if not orch:
            return json.dumps({"error": f"No job '{job_id}'"})

        if not orch.queue.is_complete():
            status = orch.get_status()
            running = sum(1 for t in status["tasks"] if t["status"] == "running")
            pending = sum(1 for t in status["tasks"]
                          if t["status"] in ("pending", "blocked"))
            return json.dumps({
                "job_id": job_id,
                "status": "still_running",
                "running": running,
                "pending": pending,
                "message": (
                    f"Still exploring ({running} running, {pending} pending). "
                    f"Do other independent work and call wait again."
                ),
            })

        # Done — collect and parse delimited output from each batch
        all_tasks = orch.queue.all_tasks()
        flat: dict = {}
        failures: list = []
        for t in all_tasks:
            if t.status.value == "completed":
                expected = t.extra.get("files", [])
                parsed = _parse_delimited(t.result, expected)
                flat.update(parsed)
            else:
                failures.append({"task": t.title, "error": t.error or "(unknown)"})

        out = {
            "job_id": job_id,
            "status": "completed",
            "files_summarized": len(flat),
            "failed_batches": len(failures),
            "results": flat,
        }
        if failures:
            out["failures"] = failures
        return json.dumps(out, ensure_ascii=False)

    return json.dumps({"error": f"Unknown action: {action}. Use: start, wait, status."})


registry.register(
    name="explore_files",
    description=(
        "3+ files? use this not file_read. Parallel cheap-model summarizers; raw content never enters your ctx.\n"
        "start(query, paths/glob) → job_id (immediate). "
        "wait(job_id) → summaries | 'still_running'. "
        "status(job_id) → progress.\n"
        "Pattern: start early, do other work, wait only when blocked."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["start", "wait", "status"],
            },
            "patterns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "start: file paths, dirs, or globs (e.g. ['src/**/*.py', 'main.py']).",
            },
            "query": {
                "type": "string",
                "description": "start: focus question for each summary (e.g. 'how auth flow works').",
            },
            "batch_tokens": {
                "type": "integer",
                "description": "start: max tokens per worker batch. Default 20000. Tiny files get packed; big files go solo.",
            },
            "ignore": {
                "type": "array",
                "items": {"type": "string"},
                "description": "start: extra ignore globs on top of defaults.",
            },
            "job_id": {
                "type": "string",
                "description": "wait|status: job ID returned from start.",
            },
        },
        "required": ["action"],
    },
    execute=explore_files,
)
