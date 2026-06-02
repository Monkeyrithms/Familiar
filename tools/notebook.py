"""
Notebook tools for the root Agent — calendar, notes, todos, and flow
charts stored in Apps/Notebook/. Imports Notebook's agent_bridge SDK
and registers every tool with the root registry at import time.

The Notebook lives at Apps/Notebook/ relative to the Agent repo root,
but both pure-Python (no Qt) agents and embedded-Qt agents can call
through this module — the bridge is registry-agnostic and never
requires Qt as long as you don't trigger UI-refresh paths.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tools.registry import registry

_NOTEBOOK_DIR = (
    Path(__file__).resolve().parent.parent / "Apps" / "Notebook"
).resolve()


def _load_bridge():
    """Put Notebook's dir on sys.path just long enough to import the
    bridge + its data_store sibling. Revert sys.path after import so we
    don't clobber anything else named `theme` / `main`."""
    if not (_NOTEBOOK_DIR / "agent_bridge.py").is_file():
        raise ImportError(f"Notebook not found at {_NOTEBOOK_DIR}")

    path_str = str(_NOTEBOOK_DIR)
    added = path_str not in sys.path
    if added:
        sys.path.insert(0, path_str)

    # Evict any stale imports the dashboard may have left in sys.modules
    # so our lookup resolves Notebook's files, not someone else's `theme`
    # or `main`.
    stale_keys = []
    for key in ("data_store", "agent_bridge"):
        if key in sys.modules:
            mod = sys.modules[key]
            mf = getattr(mod, "__file__", "")
            if not mf or not Path(mf).resolve().is_relative_to(_NOTEBOOK_DIR):
                stale_keys.append(key)
    evicted = {k: sys.modules.pop(k) for k in stale_keys}

    try:
        import agent_bridge  # type: ignore
        return agent_bridge
    finally:
        if added:
            try:
                sys.path.remove(path_str)
            except ValueError:
                pass
        # Put whatever we displaced back on top — future imports in other
        # apps can still reach their own same-named modules.
        for k, mod in evicted.items():
            sys.modules[k] = mod


_bridge = _load_bridge()
_bridge.register_with_root(registry)
