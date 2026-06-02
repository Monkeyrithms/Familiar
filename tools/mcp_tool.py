"""
MCP tool bridge.

- Reads MCP server definitions from config.json's "mcp_servers" dict.
- Connects each in a background thread so slow/broken servers can't block
  agent startup.
- Dynamically registers each remote tool as `mcp__<server>__<tool>` in the
  main tool registry so the LLM can call them like any native tool.
- Exposes a meta `mcp` tool for list/connect/disconnect/reconnect/call.

Config schema (config.json):
    "mcp_servers": {
        "filesystem": {
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
            "env": {"FOO": "bar"},
            "disabled": false
        },
        "my_http": {
            "transport": "http",
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "Bearer ..."},
            "oauth": { "client_id": "...", "authorization_endpoint": "...", ... }
        }
    }
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from core.mcp_client import mcp_manager
from tools.registry import registry

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def _safe_json_schema(schema: Any) -> dict:
    """Coerce an MCP inputSchema into an OpenAI-function-compatible schema."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out = dict(schema)
    out.setdefault("type", "object")
    out.setdefault("properties", {})
    return out


def _namespaced(server: str, tool: str) -> str:
    # Match Claude Code convention: mcp__<server>__<tool>
    return f"mcp__{server}__{tool}"


def _render_content(result: dict) -> str:
    """Flatten an MCP CallToolResult dict into a string the LLM can consume."""
    if not isinstance(result, dict):
        return json.dumps({"result": result})
    if result.get("isError"):
        parts = []
        for c in result.get("content") or []:
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
        return json.dumps({"error": "\n".join(parts) or "MCP tool error"})

    texts = []
    extras = []
    for c in result.get("content") or []:
        if c.get("type") == "text":
            texts.append(c.get("text", ""))
        else:
            extras.append(c)

    structured = result.get("structuredContent")
    payload: dict = {}
    if texts:
        payload["text"] = "\n".join(texts)
    if extras:
        payload["content"] = extras
    if structured is not None:
        payload["structured"] = structured
    if not payload:
        payload = {"text": ""}
    return json.dumps(payload, ensure_ascii=False, default=str)


def _make_executor(server: str, tool: str):
    def _exec(**kwargs) -> str:
        try:
            result = mcp_manager.call_tool(server, tool, kwargs)
            return _render_content(result)
        except Exception as e:
            return json.dumps({"error": f"{type(e).__name__}: {e}"})
    _exec.__name__ = f"mcp_{server}_{tool}"
    return _exec


def _register_server_tools(server: str) -> int:
    """Register every tool exposed by `server`. Returns count registered."""
    count = 0
    for t in mcp_manager.get_tools(server):
        tool_name = t["name"]
        full = _namespaced(server, tool_name)
        desc = t.get("description") or f"MCP tool {tool_name} from {server}"
        desc = f"[mcp:{server}] {desc}"
        registry.register(
            name=full,
            description=desc,
            parameters=_safe_json_schema(t.get("inputSchema")),
            execute=_make_executor(server, tool_name),
        )
        count += 1
    return count


def _unregister_server_tools(server: str) -> int:
    """Unregister all tools for a server. Returns count removed."""
    prefix = f"mcp__{server}__"
    names = [n for n in list(registry._tools.keys()) if n.startswith(prefix)]
    for n in names:
        registry.unregister(n)
    return len(names)


def _load_mcp_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("mcp_servers", {}) or {}
    except Exception as e:
        print(f"[mcp] Failed to read config.json: {e}")
        return {}


def _resolve_oauth_headers(name: str, cfg: dict) -> dict:
    """If cfg has an 'oauth' block, mint an Authorization header. Returns updated cfg copy."""
    oauth = cfg.get("oauth")
    if not oauth:
        return cfg
    try:
        from core.mcp_oauth import get_access_token  # late import — optional module
    except Exception as e:
        print(f"[mcp:{name}] OAuth requested but mcp_oauth unavailable: {e}")
        return cfg
    try:
        token = get_access_token(name, oauth)
    except Exception as e:
        print(f"[mcp:{name}] OAuth flow failed: {e}")
        return cfg
    if not token:
        return cfg
    merged = dict(cfg)
    headers = dict(merged.get("headers") or {})
    headers.setdefault("Authorization", f"Bearer {token}")
    merged["headers"] = headers
    return merged


def _connect_and_register(name: str, cfg: dict) -> None:
    resolved = _resolve_oauth_headers(name, cfg)
    result = mcp_manager.connect(name, resolved)
    if not result.get("ok"):
        print(f"[mcp:{name}] connect failed: {result.get('error')}")
        return
    count = _register_server_tools(name)
    print(f"[mcp:{name}] connected ({count} tools registered)")


def _autostart() -> None:
    if not mcp_manager.available:
        reason = mcp_manager.unavailable_reason
        print(f"[mcp] SDK unavailable, skipping MCP servers: {reason}")
        return
    servers = _load_mcp_config()
    if not servers:
        return
    for name, cfg in servers.items():
        if not isinstance(cfg, dict) or cfg.get("disabled"):
            continue
        # Spawn a thread per server so one slow server can't block startup.
        t = threading.Thread(
            target=_connect_and_register, args=(name, cfg),
            daemon=True, name=f"mcp-connect-{name}",
        )
        t.start()


# -------------------- meta tool --------------------

def mcp(operation: str, server: str = "", tool: str = "",
        arguments: str = "") -> str:
    """Meta tool for managing MCP servers."""
    if not mcp_manager.available:
        return json.dumps({"error": f"MCP SDK unavailable: {mcp_manager.unavailable_reason}"})

    if operation == "list":
        servers = mcp_manager.list_servers()
        cfg = _load_mcp_config()
        configured = [{"name": n, "transport": c.get("transport", "stdio"),
                       "disabled": bool(c.get("disabled"))}
                      for n, c in cfg.items() if isinstance(c, dict)]
        return json.dumps({"connected": servers, "configured": configured})

    if operation == "tools":
        if not server:
            return json.dumps({"error": "server required"})
        return json.dumps({"server": server, "tools": mcp_manager.get_tools(server)})

    if operation == "connect":
        if not server:
            return json.dumps({"error": "server required"})
        cfg = _load_mcp_config().get(server)
        if not cfg:
            return json.dumps({"error": f"Server '{server}' not in config.json"})
        # Run synchronously for explicit user-initiated connect.
        resolved = _resolve_oauth_headers(server, cfg)
        result = mcp_manager.connect(server, resolved)
        if result.get("ok"):
            count = _register_server_tools(server)
            return json.dumps({"connected": server, "tools_registered": count,
                               "tool_count": len(result.get("tools", []))})
        return json.dumps({"error": result.get("error")})

    if operation == "disconnect":
        if not server:
            return json.dumps({"error": "server required"})
        _unregister_server_tools(server)
        r = mcp_manager.disconnect(server)
        return json.dumps(r)

    if operation == "reconnect":
        if not server:
            return json.dumps({"error": "server required"})
        _unregister_server_tools(server)
        mcp_manager.disconnect(server)
        cfg = _load_mcp_config().get(server)
        if not cfg:
            return json.dumps({"error": f"Server '{server}' not in config.json"})
        resolved = _resolve_oauth_headers(server, cfg)
        result = mcp_manager.connect(server, resolved)
        if result.get("ok"):
            count = _register_server_tools(server)
            return json.dumps({"reconnected": server, "tools_registered": count})
        return json.dumps({"error": result.get("error")})

    if operation == "call":
        if not server or not tool:
            return json.dumps({"error": "server and tool required"})
        args_dict = {}
        if arguments:
            try:
                args_dict = json.loads(arguments)
            except Exception as e:
                return json.dumps({"error": f"Invalid arguments JSON: {e}"})
        result = mcp_manager.call_tool(server, tool, args_dict)
        return _render_content(result)

    return json.dumps({
        "error": f"Unknown operation: {operation}. "
                 "Use: list | tools | connect | disconnect | reconnect | call"
    })


registry.register(
    name="mcp",
    description=(
        "Manage MCP (Model Context Protocol) servers.\n"
        "- list: show connected + configured servers\n"
        "- tools: list a server's tools (requires server)\n"
        "- connect / disconnect / reconnect: manage a server defined in config.json\n"
        "- call: invoke a tool (server, tool, arguments=JSON string)\n"
        "Remote tools are also auto-registered as mcp__<server>__<tool> and "
        "callable directly."
    ),
    parameters={
        "type": "object",
        "properties": {
            "operation": {"type": "string",
                          "enum": ["list", "tools", "connect", "disconnect", "reconnect", "call"],
                          "description": "Which MCP management op."},
            "server": {"type": "string",
                       "description": "Server name (from config.json mcp_servers)."},
            "tool": {"type": "string",
                     "description": "Tool name on the server. Required for call."},
            "arguments": {"type": "string",
                          "description": "JSON object (as string) of args for call. e.g. '{\"path\":\"/tmp\"}'."},
        },
        "required": ["operation"],
    },
    execute=mcp,
)


# Kick off background auto-connect at import time.
_autostart()
