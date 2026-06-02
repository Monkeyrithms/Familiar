"""
Workspace path helpers — paths under the Agent install root are stored relative
(``Apps/...``) so moving the tree between drives or folders
does not break ``config.json``.

On Windows, anything that is *not* a drive or UNC absolute path is treated as
relative to the Agent root (so ``/Apps/Foo`` and ``Apps/Foo`` both work).

On POSIX, only non-absolute paths are joined to the Agent root; a leading
``/`` is a normal filesystem absolute path.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# Agent repository root (parent of ``core/``)
AGENT_ROOT = Path(__file__).resolve().parent.parent


def _is_windows_drive_or_unc(path: str) -> bool:
    s = (path or "").strip()
    if not s:
        return False
    if s.startswith("\\\\"):
        return True
    if len(s) >= 2 and s[1] == ":":
        return True
    return False


def resolve_workspace_entry_path(raw: str | None) -> Path:
    """Resolve a workspace ``path`` or ``venv`` string from config to an absolute Path."""
    if raw is None or not str(raw).strip():
        return AGENT_ROOT
    raw = str(raw).strip()
    if os.name == "nt":
        if _is_windows_drive_or_unc(raw):
            return Path(raw).resolve()
        rel = raw.replace("\\", "/").lstrip("/")
        if rel in (".", ""):
            return AGENT_ROOT.resolve()
        return (AGENT_ROOT / rel).resolve()
    p = Path(raw)
    if p.is_absolute():
        return p.resolve()
    if raw in (".", ""):
        return AGENT_ROOT.resolve()
    return (AGENT_ROOT / raw).resolve()


def to_config_workspace_path(abs_path: str | Path) -> str:
    """Prefer a path under AGENT_ROOT as ``Apps/...``; otherwise store absolute."""
    try:
        ap = Path(abs_path).resolve()
        root = AGENT_ROOT.resolve()
        rel = ap.relative_to(root)
        s = rel.as_posix()
        if s in (".", ""):
            return "."
        return s
    except (ValueError, OSError):
        return str(Path(abs_path).resolve())


def _build_root_pattern() -> re.Pattern:
    """Regex that matches the Agent root prefix in either slash style.

    Matches ``<ROOT>`` followed by a separator (``\\`` or ``/``) so we only
    rewrite paths that actually descend into the tree, not bare root mentions.
    Case-insensitive on Windows where the filesystem is.
    """
    root = str(AGENT_ROOT)
    # Build alternatives for both separator flavours
    fwd = root.replace("\\", "/")
    bwd = root.replace("/", "\\")
    alts = sorted({re.escape(fwd), re.escape(bwd)}, key=len, reverse=True)
    pat = r"(?:" + "|".join(alts) + r")[\\/]"
    flags = re.IGNORECASE if os.name == "nt" else 0
    return re.compile(pat, flags)


_ROOT_RE = _build_root_pattern()


def sanitize_agent_paths(text: str) -> str:
    """Strip the Agent root prefix from any paths embedded in ``text``.

    Anything inside the install root becomes a local path (``Apps/Foo/bar.py``)
    so persisted context survives the tree being moved or renamed. Paths
    outside the root are left alone.
    """
    if not text or not isinstance(text, str):
        return text
    return _ROOT_RE.sub("", text)
