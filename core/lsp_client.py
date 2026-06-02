"""
LSP client — communicates with Language Server Protocol servers to provide
semantic code intelligence: diagnostics, go-to-definition, references,
hover info, and document symbols.

Inspired by opencode-dev's src/lsp/ module. Supports multiple language
servers running concurrently (one per language). Servers are lazily spawned
on first use and automatically cleaned up.

Supported servers (auto-detected):
  - Python:     pyright / pylsp
  - TypeScript: typescript-language-server
  - Go:         gopls
  - Rust:       rust-analyzer

The agent uses this for:
  1. Post-edit validation: "Did my edit introduce type errors?"
  2. Symbol navigation: "Where is this function defined?"
  3. Code understanding: "What are all the references to this symbol?"
"""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any


# ── LSP JSON-RPC transport ──────────────────────────────────────────────

class LspTransport:
    """Handles JSON-RPC communication with an LSP server over stdio."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending: dict[int, threading.Event] = {}
        self._results: dict[int, Any] = {}
        # Push diagnostics: most LSP servers send publishDiagnostics notifications
        # rather than responding to pull requests. We store them here keyed by URI
        # so get_diagnostics can wait for them instead of returning nothing.
        self._push_diags: dict[str, list] = {}
        self._push_diag_lock = threading.Lock()
        self._push_diag_event = threading.Event()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()

    def _read_loop(self):
        """Read LSP responses from stdout."""
        try:
            while self._proc.poll() is None:
                # Read Content-Length header
                header = b""
                while True:
                    byte = self._proc.stdout.read(1)
                    if not byte:
                        return
                    header += byte
                    if header.endswith(b"\r\n\r\n"):
                        break

                # Parse Content-Length
                content_length = 0
                for line in header.decode("utf-8").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":")[1].strip())
                        break

                if content_length == 0:
                    continue

                # Read body
                body = self._proc.stdout.read(content_length)
                if not body:
                    return

                msg = json.loads(body.decode("utf-8"))

                if "id" in msg and "method" not in msg:
                    # Response to one of our requests
                    req_id = msg["id"]
                    with self._lock:
                        if req_id in self._pending:
                            self._results[req_id] = msg.get("result")
                            self._pending[req_id].set()
                elif "method" in msg and "id" not in msg:
                    # Server-pushed notification — handle publishDiagnostics
                    method = msg.get("method", "")
                    params = msg.get("params", {})
                    if method == "textDocument/publishDiagnostics":
                        uri = params.get("uri", "")
                        items = params.get("diagnostics", [])
                        with self._push_diag_lock:
                            self._push_diags[uri] = items
                        self._push_diag_event.set()
        except Exception:
            pass

    def _next_id(self) -> int:
        with self._lock:
            self._request_id += 1
            return self._request_id

    def request(self, method: str, params: dict, timeout: float = 10.0) -> Any:
        """Send a request and wait for the response."""
        req_id = self._next_id()
        event = threading.Event()

        with self._lock:
            self._pending[req_id] = event

        msg = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }
        self._send(msg)

        if not event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            return None  # Timeout

        with self._lock:
            self._pending.pop(req_id, None)
            return self._results.pop(req_id, None)

    def notify(self, method: str, params: dict):
        """Send a notification (no response expected)."""
        msg = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send(msg)

    def _send(self, msg: dict):
        """Encode and send a JSON-RPC message."""
        body = json.dumps(msg)
        header = f"Content-Length: {len(body)}\r\n\r\n"
        try:
            self._proc.stdin.write(header.encode("utf-8"))
            self._proc.stdin.write(body.encode("utf-8"))
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def clear_push_diagnostics(self, uri: str) -> None:
        """Clear stored push diagnostics for a URI before notifying a change,
        so we don't accidentally return stale results from the previous analysis."""
        with self._push_diag_lock:
            self._push_diags.pop(uri, None)
        self._push_diag_event.clear()

    def wait_for_push_diagnostics(self, uri: str, timeout: float) -> list | None:
        """Wait up to *timeout* seconds for publishDiagnostics to arrive for
        *uri*. Returns the diagnostic list, or None if it never arrived."""
        deadline = time.monotonic() + timeout
        while True:
            with self._push_diag_lock:
                if uri in self._push_diags:
                    return self._push_diags[uri]
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Wait for any notification to arrive, then re-check
            self._push_diag_event.wait(timeout=min(remaining, 0.1))
            self._push_diag_event.clear()
        # One final check after timeout
        with self._push_diag_lock:
            return self._push_diags.get(uri)

    def shutdown(self):
        """Gracefully shut down the transport."""
        try:
            self.request("shutdown", {}, timeout=3)
            self.notify("exit", {})
        except Exception:
            pass
        try:
            self._proc.kill()
            self._proc.wait(timeout=3)
        except Exception:
            pass


# ── LSP Server Manager ──────────────────────────────────────────────────

@dataclass
class Diagnostic:
    """A single diagnostic (error/warning) from the LSP server."""
    file: str
    line: int       # 0-based
    col: int        # 0-based
    end_line: int
    end_col: int
    severity: str   # "error", "warning", "info", "hint"
    message: str
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line + 1,  # Convert to 1-based for display
            "col": self.col + 1,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
        }


@dataclass
class Location:
    """A source code location."""
    file: str
    line: int   # 0-based
    col: int    # 0-based

    def to_dict(self) -> dict:
        return {"file": self.file, "line": self.line + 1, "col": self.col + 1}


@dataclass
class Symbol:
    """A document symbol (function, class, variable, etc.)."""
    name: str
    kind: str
    file: str
    line: int
    col: int
    children: list = field(default_factory=list)

    def to_dict(self) -> dict:
        result = {
            "name": self.name,
            "kind": self.kind,
            "line": self.line + 1,
        }
        if self.children:
            result["children"] = [c.to_dict() for c in self.children]
        return result


# Symbol kind mapping (LSP spec)
_SYMBOL_KINDS = {
    1: "file", 2: "module", 3: "namespace", 4: "package",
    5: "class", 6: "method", 7: "property", 8: "field",
    9: "constructor", 10: "enum", 11: "interface", 12: "function",
    13: "variable", 14: "constant", 15: "string", 16: "number",
    17: "boolean", 18: "array", 19: "object", 20: "key",
    21: "null", 22: "enum_member", 23: "struct", 24: "event",
    25: "operator", 26: "type_parameter",
}

_SEVERITY_MAP = {1: "error", 2: "warning", 3: "info", 4: "hint"}


# ── Language server configurations ──────────────────────────────────────

_SERVER_CONFIGS = {
    "python": {
        "commands": [
            ["pyright-langserver", "--stdio"],
            ["pylsp"],
        ],
        "extensions": {".py", ".pyi"},
        "language_id": "python",
    },
    "typescript": {
        "commands": [
            ["typescript-language-server", "--stdio"],
        ],
        "extensions": {".ts", ".tsx", ".js", ".jsx"},
        "language_id": "typescript",
    },
    "go": {
        "commands": [
            ["gopls", "serve"],
        ],
        "extensions": {".go"},
        "language_id": "go",
    },
    "rust": {
        "commands": [
            ["rust-analyzer"],
        ],
        "extensions": {".rs"},
        "language_id": "rust",
    },
}


class LspServer:
    """Manages a single LSP server instance for a language."""

    def __init__(self, language: str, workspace_root: str):
        self.language = language
        self.workspace_root = workspace_root
        self._transport: LspTransport | None = None
        self._initialized = False
        self._open_files: set[str] = set()
        self._file_versions: dict[str, int] = {}

    def _find_command(self) -> list[str] | None:
        """Find an available server command."""
        config = _SERVER_CONFIGS.get(self.language)
        if not config:
            return None
        for cmd in config["commands"]:
            if shutil.which(cmd[0]):
                return cmd
        return None

    def start(self) -> bool:
        """Start the LSP server. Returns True if successful."""
        cmd = self._find_command()
        if not cmd:
            return False

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.workspace_root,
            )
            self._transport = LspTransport(proc)
        except Exception:
            return False

        # Send initialize
        result = self._transport.request("initialize", {
            "processId": os.getpid(),
            "rootUri": Path(self.workspace_root).as_uri(),
            "capabilities": {
                "textDocument": {
                    "synchronization": {"didOpen": True, "didChange": True, "didClose": True},
                    "publishDiagnostics": {"relatedInformation": True},
                    "definition": {"linkSupport": True},
                    "references": {},
                    "hover": {"contentFormat": ["plaintext", "markdown"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                },
            },
            "workspaceFolders": [
                {"uri": Path(self.workspace_root).as_uri(), "name": Path(self.workspace_root).name}
            ],
        }, timeout=15)

        if result is None:
            self.stop()
            return False

        self._transport.notify("initialized", {})
        self._initialized = True
        return True

    def stop(self):
        """Stop the LSP server."""
        if self._transport:
            self._transport.shutdown()
            self._transport = None
        self._initialized = False
        self._open_files.clear()

    @property
    def ready(self) -> bool:
        return self._initialized and self._transport is not None

    def _ensure_file_open(self, file_path: str):
        """Ensure a file is opened in the LSP server."""
        if not self.ready:
            return
        uri = Path(file_path).as_uri()
        if uri in self._open_files:
            return

        try:
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        config = _SERVER_CONFIGS.get(self.language, {})
        lang_id = config.get("language_id", self.language)

        self._file_versions[uri] = 1
        self._transport.notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": lang_id,
                "version": 1,
                "text": text,
            }
        })
        self._open_files.add(uri)

    def notify_change(self, file_path: str):
        """Notify the server that a file has changed."""
        if not self.ready:
            return
        uri = Path(file_path).as_uri()

        try:
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        # Clear stale push diagnostics so get_diagnostics waits for fresh ones
        self._transport.clear_push_diagnostics(uri)

        version = self._file_versions.get(uri, 0) + 1
        self._file_versions[uri] = version

        if uri not in self._open_files:
            self._ensure_file_open(file_path)
            return

        self._transport.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": text}],
        })

    def get_diagnostics(self, file_path: str, wait_ms: int = 2000) -> list[Diagnostic]:
        """Get diagnostics for a file.

        Tries LSP 3.17 pull first (textDocument/diagnostic). Falls back to
        waiting for a textDocument/publishDiagnostics push notification, which
        is what most servers (pylsp, typescript-language-server, etc.) actually use.
        """
        if not self.ready:
            return []

        self._ensure_file_open(file_path)
        uri = Path(file_path).as_uri()

        # Try pull diagnostics (LSP 3.17 — supported by pyright, newer servers)
        result = self._transport.request("textDocument/diagnostic", {
            "textDocument": {"uri": uri},
        }, timeout=5.0)

        if result and "items" in result:
            return self._parse_diagnostics(file_path, result["items"])

        # Fall back to waiting for push diagnostics (publishDiagnostics).
        # Most servers send these asynchronously after didOpen / didChange.
        items = self._transport.wait_for_push_diagnostics(
            uri, timeout=wait_ms / 1000.0
        )
        if items is not None:
            return self._parse_diagnostics(file_path, items)

        return []

    def goto_definition(self, file_path: str, line: int, col: int) -> list[Location]:
        """Get definition location(s) for a symbol at the given position."""
        if not self.ready:
            return []

        self._ensure_file_open(file_path)
        uri = Path(file_path).as_uri()

        result = self._transport.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        }, timeout=5)

        if not result:
            return []

        return self._parse_locations(result)

    def find_references(self, file_path: str, line: int, col: int,
                        include_declaration: bool = True) -> list[Location]:
        """Find all references to a symbol at the given position."""
        if not self.ready:
            return []

        self._ensure_file_open(file_path)
        uri = Path(file_path).as_uri()

        result = self._transport.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
            "context": {"includeDeclaration": include_declaration},
        }, timeout=10)

        if not result:
            return []

        return self._parse_locations(result)

    def hover(self, file_path: str, line: int, col: int) -> str:
        """Get hover information (type info, docs) for a symbol."""
        if not self.ready:
            return ""

        self._ensure_file_open(file_path)
        uri = Path(file_path).as_uri()

        result = self._transport.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": col},
        }, timeout=5)

        if not result or "contents" not in result:
            return ""

        contents = result["contents"]
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return contents.get("value", str(contents))
        if isinstance(contents, list):
            return "\n".join(
                c.get("value", str(c)) if isinstance(c, dict) else str(c)
                for c in contents
            )
        return str(contents)

    def document_symbols(self, file_path: str) -> list[Symbol]:
        """Get all symbols in a document (functions, classes, variables)."""
        if not self.ready:
            return []

        self._ensure_file_open(file_path)
        uri = Path(file_path).as_uri()

        result = self._transport.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri},
        }, timeout=5)

        if not result:
            return []

        return self._parse_symbols(file_path, result)

    # ── Parsing helpers ──

    def _parse_diagnostics(self, file_path: str, items: list) -> list[Diagnostic]:
        diagnostics = []
        for item in items:
            rng = item.get("range", {})
            start = rng.get("start", {})
            end = rng.get("end", {})
            diagnostics.append(Diagnostic(
                file=file_path,
                line=start.get("line", 0),
                col=start.get("character", 0),
                end_line=end.get("line", 0),
                end_col=end.get("character", 0),
                severity=_SEVERITY_MAP.get(item.get("severity", 4), "hint"),
                message=item.get("message", ""),
                source=item.get("source", ""),
            ))
        return diagnostics

    def _parse_locations(self, result) -> list[Location]:
        locations = []
        if isinstance(result, dict):
            result = [result]
        for loc in result:
            uri = loc.get("uri", loc.get("targetUri", ""))
            rng = loc.get("range", loc.get("targetRange", {}))
            start = rng.get("start", {})
            file_path = uri
            if uri.startswith("file:///"):
                file_path = uri[8:] if sys.platform == "win32" else uri[7:]
                # Fix Windows paths: file:///C:/... -> C:/...
                if sys.platform == "win32" and len(file_path) > 1 and file_path[0] == '/':
                    file_path = file_path[1:]
            locations.append(Location(
                file=file_path,
                line=start.get("line", 0),
                col=start.get("character", 0),
            ))
        return locations

    def _parse_symbols(self, file_path: str, result: list) -> list[Symbol]:
        symbols = []
        for item in result:
            rng = item.get("range", item.get("location", {}).get("range", {}))
            start = rng.get("start", {})
            kind_num = item.get("kind", 0)
            children = []
            if "children" in item:
                children = self._parse_symbols(file_path, item["children"])
            symbols.append(Symbol(
                name=item.get("name", ""),
                kind=_SYMBOL_KINDS.get(kind_num, f"kind_{kind_num}"),
                file=file_path,
                line=start.get("line", 0),
                col=start.get("character", 0),
                children=children,
            ))
        return symbols


# ── Global LSP Manager (singleton) ──────────────────────────────────────

class LspManager:
    """Manages multiple LSP servers — one per language per workspace."""

    def __init__(self):
        self._servers: dict[str, LspServer] = {}  # key: "language:workspace"
        self._lock = threading.Lock()

    def _key(self, language: str, workspace: str) -> str:
        return f"{language}:{os.path.normpath(workspace)}"

    def _detect_language(self, file_path: str) -> str | None:
        """Detect the language from file extension."""
        ext = Path(file_path).suffix.lower()
        for lang, config in _SERVER_CONFIGS.items():
            if ext in config["extensions"]:
                return lang
        return None

    def get_server(self, file_path: str, workspace: str) -> LspServer | None:
        """Get or create an LSP server for the given file's language."""
        language = self._detect_language(file_path)
        if not language:
            return None

        key = self._key(language, workspace)

        with self._lock:
            if key in self._servers:
                server = self._servers[key]
                if server.ready:
                    return server
                # Server died — remove and try again
                server.stop()
                del self._servers[key]

            # Start a new server
            server = LspServer(language, workspace)
            if server.start():
                self._servers[key] = server
                return server
            return None

    def validate_file(self, file_path: str, workspace: str) -> list[dict]:
        """Quick validation: get diagnostics for a file after an edit.

        Returns a list of diagnostic dicts (errors/warnings) or empty list
        if no LSP server is available or no issues found.
        """
        server = self.get_server(file_path, workspace)
        if not server:
            return []

        server.notify_change(file_path)
        diagnostics = server.get_diagnostics(file_path)

        # Only return errors and warnings (skip hints/info)
        return [
            d.to_dict() for d in diagnostics
            if d.severity in ("error", "warning")
        ]

    def shutdown_all(self):
        """Stop all LSP servers."""
        with self._lock:
            for server in self._servers.values():
                try:
                    server.stop()
                except Exception:
                    pass
            self._servers.clear()


# Singleton instance
lsp_manager = LspManager()
