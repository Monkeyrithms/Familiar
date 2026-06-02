"""
Git worktree orchestration tool.

Creates/lists/removes git worktrees under data/worktrees/<repo_hash>/<name>/,
letting the agent work on multiple branches of the same repo simultaneously
without disturbing the user's primary checkout. Worktrees are isolated, so
parallel subagents or speculative edits can't stomp on each other.
"""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from tools.registry import registry

WORKTREE_ROOT = Path(__file__).parent.parent / "data" / "worktrees"
WORKTREE_ROOT.mkdir(parents=True, exist_ok=True)

_TIMEOUT = 60


def _run_git(args: list[str], cwd: str, timeout: int = _TIMEOUT) -> dict:
    try:
        r = subprocess.run(
            ["git"] + args, cwd=cwd,
            capture_output=True, text=True, timeout=timeout,
        )
        return {"stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode}
    except FileNotFoundError:
        return {"stdout": "", "stderr": "git not found in PATH", "returncode": -1}
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": f"Timed out after {timeout}s", "returncode": -1}


def _repo_slot(repo_path: str) -> Path:
    abs_path = str(Path(repo_path).resolve())
    h = hashlib.sha256(abs_path.encode()).hexdigest()[:16]
    return WORKTREE_ROOT / h


def _resolve_repo(path: str) -> tuple[str | None, str | None]:
    """Find the top-level of the git repo containing path. Returns (top, err)."""
    p = Path(path).resolve() if path else Path.cwd()
    if not p.exists():
        return None, f"Path not found: {p}"
    r = _run_git(["rev-parse", "--show-toplevel"], str(p if p.is_dir() else p.parent))
    if r["returncode"] != 0:
        return None, r["stderr"].strip() or "Not a git repository"
    return r["stdout"].strip(), None


def _branch_exists(repo: str, branch: str) -> bool:
    r = _run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], repo)
    return r["returncode"] == 0


def worktree(operation: str, repo_path: str = "", branch: str = "",
             base: str = "HEAD", name: str = "", force: bool = False) -> str:
    """Manage git worktrees."""
    repo, err = _resolve_repo(repo_path)
    if err:
        return json.dumps({"error": err})

    if operation == "list":
        r = _run_git(["worktree", "list", "--porcelain"], repo)
        if r["returncode"] != 0:
            return json.dumps({"error": r["stderr"]})
        entries = []
        cur: dict = {}
        for line in r["stdout"].splitlines():
            if not line.strip():
                if cur:
                    entries.append(cur); cur = {}
                continue
            if line.startswith("worktree "):
                cur["path"] = line[9:]
            elif line.startswith("HEAD "):
                cur["head"] = line[5:]
            elif line.startswith("branch "):
                cur["branch"] = line[7:].replace("refs/heads/", "")
            elif line == "detached":
                cur["detached"] = True
        if cur:
            entries.append(cur)
        return json.dumps({"repo": repo, "worktrees": entries})

    if operation == "create":
        if not branch:
            return json.dumps({"error": "branch is required for create"})
        slot = _repo_slot(repo)
        slot.mkdir(parents=True, exist_ok=True)
        wt_name = name or branch.replace("/", "_")
        target = slot / wt_name
        if target.exists():
            return json.dumps({"error": f"Worktree path already exists: {target}"})

        args = ["worktree", "add"]
        if _branch_exists(repo, branch):
            args.extend([str(target), branch])
        else:
            args.extend(["-b", branch, str(target), base])
        r = _run_git(args, repo, timeout=_TIMEOUT * 2)
        if r["returncode"] != 0:
            return json.dumps({"error": r["stderr"].strip()})
        return json.dumps({
            "created": str(target), "branch": branch, "base": base, "repo": repo,
        })

    if operation == "remove":
        if not name and not repo_path:
            return json.dumps({"error": "name or repo_path is required for remove"})
        # Accept either a worktree name (under our slot) or an absolute path.
        target = Path(name)
        if not target.is_absolute():
            target = _repo_slot(repo) / name
        if not target.exists():
            return json.dumps({"error": f"Worktree not found: {target}"})
        args = ["worktree", "remove", str(target)]
        if force:
            args.insert(2, "--force")
        r = _run_git(args, repo, timeout=_TIMEOUT * 2)
        if r["returncode"] != 0:
            # Best-effort fallback: prune and rm the directory.
            if force:
                _run_git(["worktree", "prune"], repo)
                try:
                    shutil.rmtree(target, ignore_errors=True)
                    return json.dumps({"removed": str(target), "method": "force-rmtree"})
                except Exception as e:
                    return json.dumps({"error": f"Remove failed: {e}"})
            return json.dumps({"error": r["stderr"].strip()})
        return json.dumps({"removed": str(target)})

    if operation == "prune":
        r = _run_git(["worktree", "prune", "-v"], repo)
        return json.dumps({"pruned": r["stdout"] or "(nothing to prune)"})

    return json.dumps({
        "error": f"Unknown operation: {operation}. Use: list | create | remove | prune"
    })


registry.register(
    name="worktree",
    description=(
        "Git worktrees. list|create|remove|prune. "
        "Isolated branch checkout under data/worktrees/. ✗ touches main checkout."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string",
                          "enum": ["list", "create", "remove", "prune"],
                          "description": "Which worktree op."},
            "repo_path": {"type": "string",
                          "description": "Any path inside the target repo (default cwd)."},
            "branch": {"type": "string",
                       "description": "Branch name. Required for create."},
            "base": {"type": "string",
                     "description": "Base ref for a newly-created branch (default HEAD)."},
            "name": {"type": "string",
                     "description": "Worktree dir name. Required for remove (name under data/worktrees or absolute path). Optional for create (default=branch)."},
            "force": {"type": "boolean",
                      "description": "remove: force even with uncommitted changes."},
        },
        "required": ["operation"],
    },
    execute=worktree,
)
