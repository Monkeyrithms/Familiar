"""
vector_search tool — agent-callable interface for per-workspace semantic
code search. Thin wrapper around core.code_index.

Actions:
  index   — scan a workspace root, embed all changed files. Creates/updates
            the workspace in the registry. Subsequent runs skip unchanged files.
  search  — semantic lookup over a registered workspace's chunks.
  list    — show all registered workspaces + chunk counts.
  status  — detail for one workspace.
  delete  — remove a workspace's index (keeps the files on disk untouched).
  reindex — force-rebuild a workspace (drops all prior embeddings).

After `index`, file.changed events from `file_edit` / `apply_patch` /
`file_write` automatically re-embed the affected file after a 2s debounce,
so the index stays fresh as the agent edits without re-running the tool.
"""

import json
import os
import re
from pathlib import Path

from tools.registry import registry as tool_registry


def _slug(name: str) -> str:
    """Make a safe filename slug from a workspace name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "ws"


def vector_search(action: str, name: str = "", path: str = "",
                  query: str = "", limit: int = 10,
                  kind: str = "", patterns: str = "",
                  mode: str = "hybrid") -> str:
    """Dispatch to the appropriate code_index operation."""
    try:
        return _vector_search_impl(action, name, path, query, limit,
                                   kind, patterns, mode)
    except RuntimeError as e:
        # Dim-mismatch and similar recoverable errors — surface as JSON, not a traceback
        return json.dumps({"error": str(e)})


def _vector_search_impl(action: str, name: str, path: str,
                        query: str, limit: int, kind: str,
                        patterns: str, mode: str) -> str:
    from core.code_index import (
        CodeIndex, DEFAULT_PATTERNS, open_index, registry as ws_registry,
    )

    action = action.lower().strip()

    if action == "index":
        if not path:
            return json.dumps({"error": "`path` required for index action"})
        abs_root = os.path.abspath(path)
        if not Path(abs_root).is_dir():
            return json.dumps({"error": f"Not a directory: {abs_root}"})
        # Derive name from path if not provided
        ws_name = _slug(name) if name else _slug(Path(abs_root).name)
        ws_registry.register(ws_name, abs_root)
        idx = CodeIndex(ws_name, abs_root)
        if patterns:
            pats = tuple(p.strip() for p in patterns.split(",") if p.strip())
        else:
            pats = DEFAULT_PATTERNS
        stats = idx.reindex(pats)
        return json.dumps({
            "workspace": ws_name,
            "root": abs_root,
            "stats": stats,
        }, ensure_ascii=False)

    if action == "search":
        if not query:
            return json.dumps({"error": "`query` required for search action"})
        if not name:
            workspaces = ws_registry.list_all()
            if len(workspaces) == 1:
                name = workspaces[0]["name"]
            elif len(workspaces) == 0:
                return json.dumps({
                    "error": "No workspaces indexed yet. Run action='index' first."
                })
            else:
                return json.dumps({
                    "error": "Multiple workspaces exist; specify `name`.",
                    "available": [w["name"] for w in workspaces],
                })
        idx = open_index(name)
        if not idx:
            return json.dumps({"error": f"Workspace '{name}' not registered."})
        results = idx.search(query, limit=limit, kind_filter=kind or None,
                             mode=mode)
        return json.dumps({
            "workspace": name, "query": query, "mode": mode,
            "count": len(results), "results": results,
        }, ensure_ascii=False)

    if action == "list":
        return json.dumps({"workspaces": ws_registry.list_all()}, ensure_ascii=False)

    if action == "status":
        if not name:
            return json.dumps({"error": "`name` required for status action"})
        idx = open_index(name)
        if not idx:
            return json.dumps({"error": f"Workspace '{name}' not registered."})
        return json.dumps(idx.status(), ensure_ascii=False)

    if action == "delete":
        if not name:
            return json.dumps({"error": "`name` required for delete action"})
        idx = open_index(name)
        if not idx:
            return json.dumps({"error": f"Workspace '{name}' not registered."})
        idx.delete()
        return json.dumps({"deleted": name})

    if action == "reindex":
        if not name:
            return json.dumps({"error": "`name` required for reindex action"})
        idx = open_index(name)
        if not idx:
            return json.dumps({"error": f"Workspace '{name}' not registered."})
        # Drop all prior state so the reindex rebuilds from scratch. Also drop
        # chunks_vec + meta[embed_dims] so the new dim size (if config changed)
        # is accepted cleanly instead of tripping the dim-mismatch guard.
        import sqlite3
        conn = sqlite3.connect(str(idx.db_path))
        try:
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM files")
            try:
                conn.execute("DELETE FROM chunks_fts")
            except Exception:
                pass
            try:
                conn.execute("DROP TABLE IF EXISTS chunks_vec")
            except Exception:
                pass
            conn.execute("DELETE FROM meta WHERE key='embed_dims'")
            conn.commit()
        finally:
            conn.close()
        if patterns:
            pats = tuple(p.strip() for p in patterns.split(",") if p.strip())
        else:
            pats = DEFAULT_PATTERNS
        stats = idx.reindex(pats)
        return json.dumps({"workspace": name, "stats": stats}, ensure_ascii=False)

    return json.dumps({
        "error": f"Unknown action '{action}'. "
                 "Use: index | search | list | status | delete | reindex"
    })


# ── Wire auto-index to file.changed at import time ─────────────────────

def _subscribe():
    """Register the code_index auto-updater on the event bus exactly once."""
    try:
        from core.event_bus import bus
        from core.code_index import _on_file_changed
        bus.on("file.changed", _on_file_changed)
    except Exception as e:
        print(f"[vector_search] failed to subscribe to file.changed: {e}")


_subscribe()


tool_registry.register(
    name="vector_search",
    description=(
        "Semantic code search. "
        "index(path): embed dir, incremental. "
        "search(query): BM25+vector; mode=keyword|vector; kind=function|class|method. "
        "list|status|delete|reindex. Auto-refreshes on file change."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["index", "search", "list", "status", "delete", "reindex"],
            },
            "name": {
                "type": "string",
                "description": "Workspace name. For index, defaults to folder name.",
            },
            "path": {
                "type": "string",
                "description": "Absolute dir to index (index action only).",
            },
            "query": {
                "type": "string",
                "description": "Natural-language query (search action).",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 10).",
            },
            "kind": {
                "type": "string",
                "description": "Filter by chunk kind: function|class|method|module|text|...",
            },
            "patterns": {
                "type": "string",
                "description": "Comma-separated glob patterns. Default: code+docs.",
            },
            "mode": {
                "type": "string",
                "enum": ["hybrid", "vector", "keyword"],
                "description": (
                    "Search mode. hybrid (default): BM25+vector via RRF. "
                    "vector: semantic only. keyword: BM25 only. "
                    "Use keyword when you know an exact identifier."
                ),
            },
        },
        "required": ["action"],
    },
    execute=vector_search,
)
