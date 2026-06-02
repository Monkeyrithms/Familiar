"""
LSP tool — exposes Language Server Protocol capabilities to the agent.

Actions:
  - diagnostics: Get errors/warnings for a file
  - definition:  Go to the definition of a symbol
  - references:  Find all references to a symbol
  - hover:       Get type info / documentation for a symbol
  - symbols:     List all symbols in a file
"""

import json
from pathlib import Path
from tools.registry import registry


def _get_workspace(path: str) -> str:
    """Resolve workspace root from a file path."""
    # Walk up to find common project markers
    p = Path(path).resolve()
    for parent in [p.parent] + list(p.parents):
        if any((parent / marker).exists()
               for marker in [".git", "pyproject.toml", "package.json",
                               "Cargo.toml", "go.mod", "setup.py"]):
            return str(parent)
    return str(p.parent)


def lsp_tool(action: str, path: str, line: int = 0, col: int = 0) -> str:
    """Execute an LSP action on a file."""
    from core.lsp_client import lsp_manager

    workspace = _get_workspace(path)

    if action == "diagnostics":
        server = lsp_manager.get_server(path, workspace)
        if not server:
            return json.dumps({
                "note": "No LSP server available for this file type. "
                "Install pyright (Python), typescript-language-server (JS/TS), "
                "gopls (Go), or rust-analyzer (Rust).",
                "diagnostics": [],
            })
        server.notify_change(path)
        diagnostics = server.get_diagnostics(path)
        return json.dumps({
            "file": path,
            "diagnostics": [d.to_dict() for d in diagnostics],
            "count": len(diagnostics),
        })

    elif action == "definition":
        server = lsp_manager.get_server(path, workspace)
        if not server:
            return json.dumps({"error": "No LSP server available for this file type."})
        # Convert to 0-based
        locations = server.goto_definition(path, line - 1, col - 1)
        return json.dumps({
            "definitions": [loc.to_dict() for loc in locations],
            "count": len(locations),
        })

    elif action == "references":
        server = lsp_manager.get_server(path, workspace)
        if not server:
            return json.dumps({"error": "No LSP server available for this file type."})
        locations = server.find_references(path, line - 1, col - 1)
        return json.dumps({
            "references": [loc.to_dict() for loc in locations],
            "count": len(locations),
        })

    elif action == "hover":
        server = lsp_manager.get_server(path, workspace)
        if not server:
            return json.dumps({"error": "No LSP server available for this file type."})
        info = server.hover(path, line - 1, col - 1)
        return json.dumps({"hover": info or "(no info available)"})

    elif action == "symbols":
        server = lsp_manager.get_server(path, workspace)
        if not server:
            return json.dumps({"error": "No LSP server available for this file type."})
        symbols = server.document_symbols(path)
        return json.dumps({
            "file": path,
            "symbols": [s.to_dict() for s in symbols],
            "count": len(symbols),
        })

    else:
        return json.dumps({
            "error": f"Unknown action: {action}. "
            "Use: diagnostics, definition, references, hover, symbols"
        })


registry.register(
    name="lsp",
    description=(
        "LSP: diagnostics|definition|references|hover|symbols. "
        "Needs pyright|typescript-language-server|gopls|rust-analyzer. "
        "definition|references|hover need line+col (1-based)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["diagnostics", "definition", "references", "hover", "symbols"],
                "description": "LSP op.",
            },
            "path": {
                "type": "string",
                "description": "Absolute file path.",
            },
            "line": {
                "type": "integer",
                "description": "Line (1-based). Req for definition|references|hover.",
            },
            "col": {
                "type": "integer",
                "description": "Col (1-based). Req for definition|references|hover.",
            },
        },
        "required": ["action", "path"],
    },
    execute=lsp_tool,
)
