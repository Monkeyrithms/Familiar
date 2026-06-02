"""
Checkpoint Manager — automatic filesystem snapshots via shadow git repos.

Takes snapshots of working directories before file-mutating operations.
Each workspace gets its own independent shadow git repo under
data/checkpoints/. The user's project directories are never touched.

Provides list, diff, and restore (rollback) capabilities.
Inspired by hermes-agent-main's checkpoint_manager.py.
"""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

CHECKPOINT_DIR = Path(__file__).parent.parent / "data" / "checkpoints"
CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_EXCLUDES = [
    "node_modules/", "dist/", "build/",
    ".env", ".env.*", ".env.local",
    "__pycache__/", "*.pyc", "*.pyo",
    ".DS_Store", "*.log",
    ".cache/", ".next/", ".nuxt/",
    "coverage/", ".pytest_cache/",
    ".venv/", "venv/", ".git/",
]

_GIT_TIMEOUT = 30
_MAX_FILES = 50_000


def _shadow_path(working_dir: str) -> Path:
    """Deterministic shadow repo path from working directory."""
    abs_path = str(Path(working_dir).resolve())
    h = hashlib.sha256(abs_path.encode()).hexdigest()[:16]
    return CHECKPOINT_DIR / h


def _git_env(shadow: Path, working_dir: str) -> dict:
    env = os.environ.copy()
    env["GIT_DIR"] = str(shadow)
    env["GIT_WORK_TREE"] = str(Path(working_dir).resolve())
    env.pop("GIT_INDEX_FILE", None)
    return env


def _git(args: list, shadow: Path, working_dir: str,
         timeout: int = _GIT_TIMEOUT, ok_codes: set = None) -> tuple:
    """Run git command. Returns (success, stdout, stderr)."""
    ok_codes = ok_codes or set()
    try:
        r = subprocess.run(
            ["git"] + args,
            capture_output=True, text=True, timeout=timeout,
            env=_git_env(shadow, working_dir),
            cwd=str(Path(working_dir).resolve()),
        )
        return r.returncode == 0, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", f"git timed out after {timeout}s"
    except FileNotFoundError:
        return False, "", "git not found"
    except Exception as e:
        return False, "", str(e)


def _init_shadow(shadow: Path, working_dir: str) -> bool:
    """Initialize shadow repo if needed. Returns True on success."""
    if (shadow / "HEAD").exists():
        return True
    shadow.mkdir(parents=True, exist_ok=True)
    ok, _, _ = _git(["init"], shadow, working_dir)
    if not ok:
        return False
    _git(["config", "user.email", "agent@local"], shadow, working_dir)
    _git(["config", "user.name", "Agent Checkpoint"], shadow, working_dir)
    info = shadow / "info"
    info.mkdir(exist_ok=True)
    (info / "exclude").write_text("\n".join(DEFAULT_EXCLUDES) + "\n", encoding="utf-8")
    (shadow / "WORKDIR").write_text(str(Path(working_dir).resolve()) + "\n", encoding="utf-8")
    return True


def _file_count(path: str) -> int:
    count = 0
    try:
        for _ in Path(path).rglob("*"):
            count += 1
            if count > _MAX_FILES:
                return count
    except (PermissionError, OSError):
        pass
    return count


class CheckpointManager:
    """Manages automatic filesystem checkpoints per workspace.

    Usage:
        cp = CheckpointManager()
        cp.new_turn()  # call at start of each agent turn
        cp.ensure_checkpoint(working_dir, "before file_write")  # before mutations
    """

    def __init__(self, max_snapshots: int = 50):
        self.max_snapshots = max_snapshots
        self._done_this_turn: set = set()
        self._git_ok: bool | None = None
        self._last_hash: str | None = None
        self._last_dir: str | None = None

    def new_turn(self):
        """Reset per-turn dedup. Call at start of each agent iteration."""
        self._done_this_turn.clear()
        self._last_hash = None
        self._last_dir = None

    def ensure_checkpoint(self, working_dir: str, reason: str = "auto") -> str | None:
        """Take a checkpoint if not already done this turn. Returns commit hash or None. Never raises.

        Runs synchronously because the snapshot MUST capture pre-mutation state —
        backgrounding this would race the caller's subsequent write and snapshot
        post-write state, breaking rollback. Per-turn dedup already limits cost
        to one checkpoint per (turn, directory).
        """
        if self._git_ok is None:
            self._git_ok = shutil.which("git") is not None
        if not self._git_ok:
            return None

        abs_dir = str(Path(working_dir).resolve())
        if abs_dir in ("/", str(Path.home())):
            return None
        if abs_dir in self._done_this_turn:
            return self._last_hash  # Return the hash from earlier this turn
        self._done_this_turn.add(abs_dir)

        try:
            h = self._take(abs_dir, reason)
            if h:
                self._last_hash = h
                self._last_dir = abs_dir
            return h
        except Exception:
            return None

    def list_checkpoints(self, working_dir: str) -> list[dict]:
        """List available checkpoints. Most recent first."""
        abs_dir = str(Path(working_dir).resolve())
        shadow = _shadow_path(abs_dir)
        if not (shadow / "HEAD").exists():
            return []

        ok, stdout, _ = _git(
            ["log", "--format=%H|%h|%aI|%s", "-n", str(self.max_snapshots)],
            shadow, abs_dir)
        if not ok or not stdout:
            return []

        results = []
        for line in stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                entry = {
                    "hash": parts[0], "short": parts[1],
                    "time": parts[2], "reason": parts[3],
                    "files": 0, "insertions": 0, "deletions": 0,
                }
                ok2, stat, _ = _git(
                    ["diff", "--shortstat", f"{parts[0]}~1", parts[0]],
                    shadow, abs_dir, ok_codes={128})
                if ok2 and stat:
                    m = re.search(r'(\d+) file', stat)
                    if m: entry["files"] = int(m.group(1))
                    m = re.search(r'(\d+) insertion', stat)
                    if m: entry["insertions"] = int(m.group(1))
                    m = re.search(r'(\d+) deletion', stat)
                    if m: entry["deletions"] = int(m.group(1))
                results.append(entry)
        return results

    def diff(self, working_dir: str, commit_hash: str) -> dict:
        """Diff between a checkpoint and current state."""
        abs_dir = str(Path(working_dir).resolve())
        shadow = _shadow_path(abs_dir)
        if not (shadow / "HEAD").exists():
            return {"error": "No checkpoints"}

        ok, _, _ = _git(["cat-file", "-t", commit_hash], shadow, abs_dir)
        if not ok:
            return {"error": f"Checkpoint '{commit_hash}' not found"}

        _git(["add", "-A"], shadow, abs_dir, timeout=_GIT_TIMEOUT * 2)
        _, stat, _ = _git(["diff", "--stat", commit_hash, "--cached"], shadow, abs_dir)
        _, diff_text, _ = _git(["diff", commit_hash, "--cached", "--no-color"], shadow, abs_dir)
        _git(["reset", "HEAD", "--quiet"], shadow, abs_dir)

        return {"stat": stat, "diff": diff_text}

    def restore(self, working_dir: str, commit_hash: str, file_path: str = None) -> dict:
        """Restore to a checkpoint. Takes a pre-rollback snapshot first."""
        abs_dir = str(Path(working_dir).resolve())
        shadow = _shadow_path(abs_dir)
        if not (shadow / "HEAD").exists():
            return {"error": "No checkpoints"}

        ok, _, _ = _git(["cat-file", "-t", commit_hash], shadow, abs_dir)
        if not ok:
            return {"error": f"Checkpoint '{commit_hash}' not found"}

        # Snapshot current state before restoring (undo the undo)
        self._take(abs_dir, f"pre-rollback (restoring to {commit_hash[:8]})")

        target = file_path if file_path else "."
        ok, _, err = _git(
            ["checkout", commit_hash, "--", target],
            shadow, abs_dir, timeout=_GIT_TIMEOUT * 2)
        if not ok:
            return {"error": f"Restore failed: {err}"}

        _, reason, _ = _git(["log", "--format=%s", "-1", commit_hash], shadow, abs_dir)
        result = {"restored_to": commit_hash[:8], "reason": reason, "directory": abs_dir}
        if file_path:
            result["file"] = file_path
        return result

    def _take(self, working_dir: str, reason: str) -> str | None:
        """Take a snapshot. Returns the commit hash on success, None on failure."""
        shadow = _shadow_path(working_dir)
        if not _init_shadow(shadow, working_dir):
            return None
        if _file_count(working_dir) > _MAX_FILES:
            return None

        ok, _, _ = _git(["add", "-A"], shadow, working_dir, timeout=_GIT_TIMEOUT * 2)
        if not ok:
            return None

        ok_diff, _, _ = _git(["diff", "--cached", "--quiet"], shadow, working_dir, ok_codes={1})
        if ok_diff:
            return None  # Nothing changed

        ok, _, _ = _git(["commit", "-m", reason], shadow, working_dir, timeout=_GIT_TIMEOUT * 2)
        if not ok:
            return None

        # Get the hash of what we just committed
        ok, hash_out, _ = _git(["rev-parse", "HEAD"], shadow, working_dir)
        commit_hash = hash_out.strip() if ok else None

        print(f"[Checkpoint] {working_dir}: {reason} ({commit_hash[:8] if commit_hash else '?'})")
        return commit_hash


# Global instance
checkpoint_manager = CheckpointManager()
