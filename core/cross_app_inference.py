"""
Shared helpers so embedded apps (vispy_dashboard, Hybrid, Notebook) can resolve
the Agent repo root and call the same LLM client factory as root /Agent/.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def agent_repository_root() -> Path:
    """Directory that contains ``core/`` and ``data/keys.json``."""
    return Path(__file__).resolve().parent.parent


def ensure_agent_repo_on_sys_path() -> Path:
    """Insert the Agent repo root at the front of ``sys.path`` if missing."""
    root = agent_repository_root()
    rs = str(root)
    if rs not in sys.path:
        sys.path.insert(0, rs)
    return root


def merge_provider_credentials(
    provider: str,
    overrides: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build optional ``credentials`` dict for ``get_client`` from host-specific
    overrides (e.g. dashboard ``agent_api_key`` / ``agent_api_url``)."""
    if not overrides:
        return None
    out: dict[str, Any] = {}
    key = (overrides.get("api_key") or "").strip()
    if key:
        out["api_key"] = key
    base = (overrides.get("base_url") or "").strip()
    if base:
        out["base_url"] = base
    auth = (overrides.get("auth_mode") or "").strip()
    if auth:
        out["auth_mode"] = auth
    return out or None
