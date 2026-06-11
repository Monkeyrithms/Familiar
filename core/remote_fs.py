"""
Remote workspace filesystem — let a peer browse and edit the files of a
conversation's workspace on the HOST, over the authenticated network channel.

Scope & safety:
  * Every operation is confined to ONE conversation's workspace root (the
    conversation's pinned cwd, else its workspace folder, else the Agent root).
  * A peer-supplied relative path is resolved under that root and rejected if it
    escapes it (``..`` / absolute / drive-letter) — same guard as file_share.
  * Read AND write are allowed (the host owns the files; edits commit on the
    host). Oversized files are skipped to stay tunnel-friendly.

Stdlib-only and GUI-free, so the network server thread can serve these without
the UI. The /fs/* endpoints in core.network drive these functions.
"""

from __future__ import annotations

import os
from pathlib import Path

from core.workspace_paths import AGENT_ROOT, resolve_workspace_entry_path

_MAX_FILE_BYTES = 25 * 1024 * 1024      # 25 MB cap for read/write over the tunnel
# Directories never worth shipping over the wire (huge, noisy, or sensitive).
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
              ".mypy_cache", ".ruff_cache", ".pytest_cache", "data"}


def workspace_root(conv_id: str) -> Path | None:
    """Resolve a conversation's workspace directory on this host. Mirrors
    Agent.workspace_path: pinned cwd wins, else the named workspace folder,
    else the Agent root."""
    if not conv_id:
        return None
    try:
        from core.conversations import load_conversation, is_conversation_private
        from core.agent import load_config
    except Exception:
        return None
    if is_conversation_private(conv_id):
        return None      # private conversation → no remote file access at all
    data = load_conversation(conv_id)
    if not data:
        return None
    cwd = (data.get("conversation_cwd") or "").strip()
    if cwd:
        p = resolve_workspace_entry_path(cwd)
        if p.is_dir():
            return p.resolve()
    ws_name = data.get("workspace") or ""
    if ws_name:
        ws = (load_config().get("workspaces", {}) or {}).get(ws_name, {})
        path = ws.get("path", "")
        if path:
            p = resolve_workspace_entry_path(path)
            if p.is_dir():
                return p.resolve()
    return AGENT_ROOT.resolve()


def _safe_target(conv_id: str, rel: str) -> tuple[Path | None, Path | None]:
    """(root, absolute_target) for a peer-supplied relative path, or (root, None)
    if it escapes the workspace root."""
    root = workspace_root(conv_id)
    if root is None:
        return None, None
    rel = (rel or "").replace("\\", "/").strip("/")
    target = (root / rel).resolve() if rel else root
    try:
        target.relative_to(root)
    except ValueError:
        return root, None       # traversal attempt
    return root, target


def fs_list(conv_id: str, subpath: str = "") -> dict | None:
    """List a directory inside the conversation's workspace. Returns
    {root, path, entries:[{name, is_dir, size}]} or None if out of bounds."""
    root, target = _safe_target(conv_id, subpath)
    if root is None or target is None or not target.is_dir():
        return None
    entries = []
    try:
        for child in sorted(target.iterdir(),
                            key=lambda c: (not c.is_dir(), c.name.lower())):
            if child.is_dir() and child.name in _SKIP_DIRS:
                continue
            try:
                is_dir = child.is_dir()
                size = 0 if is_dir else child.stat().st_size
            except OSError:
                continue
            entries.append({"name": child.name, "is_dir": is_dir, "size": size})
    except OSError:
        return None
    return {"root": str(root),
            "path": target.relative_to(root).as_posix(),
            "entries": entries}


def fs_read(conv_id: str, rel: str) -> tuple[str | None, str]:
    """Read a workspace file. Returns (text, error). text is None on error;
    binary/oversized files are refused with an explanatory error."""
    root, target = _safe_target(conv_id, rel)
    if root is None or target is None:
        return None, "path is outside the conversation workspace"
    if not target.is_file():
        return None, "not a file"
    try:
        if target.stat().st_size > _MAX_FILE_BYTES:
            return None, "file too large to open remotely"
        raw = target.read_bytes()
    except OSError as e:
        return None, str(e)
    try:
        return raw.decode("utf-8"), ""
    except UnicodeDecodeError:
        return None, "binary file (not editable as text)"


def fs_write(conv_id: str, rel: str, content: str) -> tuple[bool, str]:
    """Write text to a workspace file (creating parent dirs). Returns (ok, error)."""
    root, target = _safe_target(conv_id, rel)
    if root is None or target is None:
        return False, "path is outside the conversation workspace"
    if target.is_dir():
        return False, "path is a directory"
    data = (content or "").encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:
        return False, "content too large"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return True, ""
    except OSError as e:
        return False, str(e)


def fs_mkdir(conv_id: str, rel: str) -> tuple[bool, str]:
    """Create a directory in the workspace. Returns (ok, error)."""
    root, target = _safe_target(conv_id, rel)
    if root is None or target is None:
        return False, "path is outside the conversation workspace"
    try:
        target.mkdir(parents=True, exist_ok=True)
        return True, ""
    except OSError as e:
        return False, str(e)


def fs_delete(conv_id: str, rel: str) -> tuple[bool, str]:
    """Delete a file or directory (recursively) in the workspace. The root
    itself can't be deleted. Returns (ok, error)."""
    import shutil
    root, target = _safe_target(conv_id, rel)
    if root is None or target is None:
        return False, "path is outside the conversation workspace"
    if target == root:
        return False, "refusing to delete the workspace root"
    if not target.exists():
        return False, "no such path"
    try:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return True, ""
    except OSError as e:
        return False, str(e)


def fs_rename(conv_id: str, rel: str, new_rel: str) -> tuple[bool, str]:
    """Rename/move a file or directory WITHIN the workspace. Both source and
    destination must resolve inside the root. Returns (ok, error)."""
    root, src = _safe_target(conv_id, rel)
    _root2, dst = _safe_target(conv_id, new_rel)
    if root is None or src is None or dst is None:
        return False, "path is outside the conversation workspace"
    if src == root:
        return False, "refusing to rename the workspace root"
    if not src.exists():
        return False, "no such path"
    if dst.exists():
        return False, "destination already exists"
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return True, ""
    except OSError as e:
        return False, str(e)
