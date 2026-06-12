"""
Git tool — structured git operations via subprocess.
Follows Hermes' pattern: safe subprocess calls with timeout.
"""

import json
import subprocess
from pathlib import Path
from tools.registry import registry
from core.proc import NO_WINDOW


def _run_git(args: list[str], cwd: str, timeout: int = 30) -> dict:
    """Run a git command and return {stdout, stderr, returncode}."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=NO_WINDOW,  # no console flash on Windows
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except FileNotFoundError:
        return {"stdout": "", "stderr": "git not found in PATH", "returncode": -1}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timed out after {timeout}s", "returncode": -1}


def git(operation: str, path: str = "", args: str = "",
        file: str = "", message: str = "") -> str:
    """Execute a git operation."""
    cwd = path or str(Path.cwd())
    if not Path(cwd).is_dir():
        return json.dumps({"error": f"Directory not found: {cwd}"})

    if operation == "status":
        r = _run_git(["status", "--short"], cwd)
        if r["returncode"] != 0:
            return json.dumps({"error": r["stderr"]})
        return json.dumps({"status": r["stdout"] or "(clean)"})

    elif operation == "diff":
        cmd = ["diff"]
        if file:
            cmd.append(file)
        if args:
            cmd.extend(args.split())
        r = _run_git(cmd, cwd)
        if r["returncode"] != 0 and r["stderr"]:
            return json.dumps({"error": r["stderr"]})
        return json.dumps({"diff": r["stdout"] or "(no changes)"})

    elif operation == "log":
        count = args or "10"
        r = _run_git(["log", f"--oneline", f"-{count}"], cwd)
        if r["returncode"] != 0:
            return json.dumps({"error": r["stderr"]})
        return json.dumps({"log": r["stdout"]})

    elif operation == "blame":
        if not file:
            return json.dumps({"error": "file is required for blame"})
        r = _run_git(["blame", "--line-porcelain", file], cwd, timeout=60)
        if r["returncode"] != 0:
            return json.dumps({"error": r["stderr"]})
        # Summarize blame output (full porcelain is huge)
        lines = []
        current = {}
        for line in r["stdout"].splitlines():
            if line.startswith("\t"):
                current["text"] = line[1:]
                lines.append(current)
                current = {}
            elif line.startswith("author "):
                current["author"] = line[7:]
            elif line.startswith("summary "):
                current["summary"] = line[8:]
        # Compact: "author | summary | text" per line
        compact = []
        for l in lines[:200]:  # cap at 200 lines
            a = l.get("author", "?")
            s = l.get("summary", "")[:40]
            t = l.get("text", "")
            compact.append(f"{a} | {s} | {t}")
        return json.dumps({"blame": "\n".join(compact)}, ensure_ascii=False)

    elif operation == "branch":
        r = _run_git(["branch", "-a"], cwd)
        if r["returncode"] != 0:
            return json.dumps({"error": r["stderr"]})
        return json.dumps({"branches": r["stdout"]})

    elif operation == "show":
        ref = args or "HEAD"
        r = _run_git(["show", "--stat", ref], cwd)
        if r["returncode"] != 0:
            return json.dumps({"error": r["stderr"]})
        return json.dumps({"show": r["stdout"]})

    elif operation == "stash":
        sub = args or "list"
        r = _run_git(["stash", sub], cwd)
        return json.dumps({"result": r["stdout"] or r["stderr"]})

    else:
        return json.dumps({"error": f"Unknown operation: {operation}. "
                           "Use: status, diff, log, blame, branch, show, stash"})


registry.register(
    name="git",
    description=(
        "Git read-only ops: status | diff | log | blame | branch | show | stash.\n"
        "- ✗ commit | push | modify."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["status", "diff", "log", "blame", "branch", "show", "stash"],
                "description": "Which git op to run.",
            },
            "path": {"type": "string",
                     "description": "Repo path (default = workspace)."},
            "file": {"type": "string",
                     "description": "File path. Required for blame; optional for diff."},
            "args": {"type": "string",
                     "description": "Op-specific extra: log=count (default 10), show=ref (default HEAD), stash=subcommand (default list), diff=extra flags."},
            "message": {"type": "string",
                        "description": "Reserved for commit (not currently used)."},
        },
        "required": ["operation"],
    },
    execute=git,
)
