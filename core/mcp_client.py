"""
MCP (Model Context Protocol) client manager.

Owns a dedicated asyncio loop in a background thread. For each configured MCP
server, spawns a long-lived task that holds the transport + ClientSession open,
listens on an async queue for requests, and fulfills them via the session.

The manager exposes a synchronous API (connect / call_tool / list_tools /
disconnect) because /agent/'s tool system is sync. Async-to-sync bridging
happens via asyncio.run_coroutine_threadsafe + Future.result().

Transports supported:
  - stdio: command + args (optional env / cwd)
  - streamable_http: url (+ optional headers)
  - sse: url (legacy, kept for compatibility)

OAuth is layered on top in core/mcp_oauth.py; this module only consumes a
pre-computed Authorization header (passed via `headers`) for http transports.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_OK = True
    _MCP_ERR = None
except Exception as e:  # pragma: no cover
    _MCP_OK = False
    _MCP_ERR = str(e)

try:
    from mcp.client.streamable_http import streamablehttp_client
except Exception:
    streamablehttp_client = None  # type: ignore

try:
    from mcp.client.sse import sse_client
except Exception:
    sse_client = None  # type: ignore


@dataclass
class ServerState:
    name: str
    config: dict
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    error: str | None = None
    tools: list[dict] = field(default_factory=list)
    resources: list[dict] = field(default_factory=list)
    prompts: list[dict] = field(default_factory=list)
    task: asyncio.Task | None = None


class MCPManager:
    """Manages MCP server connections on a background asyncio loop."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._servers: dict[str, ServerState] = {}
        self._started = threading.Event()
        if _MCP_OK:
            self._start_loop()

    # -------------------- loop lifecycle --------------------

    def _start_loop(self) -> None:
        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            self._started.set()
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=_run, daemon=True, name="mcp-loop")
        self._thread.start()
        self._started.wait(timeout=5)

    def _submit(self, coro, timeout: float | None = 30):
        if not _MCP_OK or self._loop is None:
            raise RuntimeError(f"MCP unavailable: {_MCP_ERR}")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # -------------------- per-server task --------------------

    async def _server_task(self, state: ServerState) -> None:
        """Long-lived task: opens transport+session, serves requests, handles teardown."""
        cfg = state.config
        transport = cfg.get("transport", "stdio")
        try:
            async with AsyncExitStack() as stack:
                if transport == "stdio":
                    params = StdioServerParameters(
                        command=cfg["command"],
                        args=cfg.get("args", []),
                        env=cfg.get("env"),
                        cwd=cfg.get("cwd"),
                    )
                    streams = await stack.enter_async_context(stdio_client(params))
                    read, write = streams[0], streams[1]
                elif transport in ("http", "streamable_http"):
                    if streamablehttp_client is None:
                        raise RuntimeError("streamable_http client unavailable in mcp SDK")
                    streams = await stack.enter_async_context(
                        streamablehttp_client(
                            url=cfg["url"],
                            headers=cfg.get("headers"),
                        )
                    )
                    read, write = streams[0], streams[1]
                elif transport == "sse":
                    if sse_client is None:
                        raise RuntimeError("sse client unavailable in mcp SDK")
                    streams = await stack.enter_async_context(
                        sse_client(url=cfg["url"], headers=cfg.get("headers"))
                    )
                    read, write = streams[0], streams[1]
                else:
                    raise ValueError(f"Unknown transport: {transport}")

                session: ClientSession = await stack.enter_async_context(
                    ClientSession(read, write)
                )
                await session.initialize()

                # Enumerate capabilities.
                try:
                    tools_result = await session.list_tools()
                    state.tools = [self._tool_to_dict(t) for t in tools_result.tools]
                except Exception:
                    state.tools = []
                try:
                    res_result = await session.list_resources()
                    state.resources = [
                        {"uri": str(r.uri), "name": getattr(r, "name", None),
                         "description": getattr(r, "description", None)}
                        for r in res_result.resources
                    ]
                except Exception:
                    state.resources = []
                try:
                    prompts_result = await session.list_prompts()
                    state.prompts = [
                        {"name": p.name, "description": getattr(p, "description", None)}
                        for p in prompts_result.prompts
                    ]
                except Exception:
                    state.prompts = []

                state.ready.set()

                # Request loop.
                while True:
                    req = await state.queue.get()
                    if req is None:
                        break
                    kind, args, fut = req
                    try:
                        if kind == "call_tool":
                            result = await session.call_tool(args["name"], args.get("arguments") or {})
                            fut.set_result(self._call_result_to_dict(result))
                        elif kind == "read_resource":
                            result = await session.read_resource(args["uri"])
                            fut.set_result(self._resource_result_to_dict(result))
                        elif kind == "get_prompt":
                            result = await session.get_prompt(args["name"], args.get("arguments"))
                            fut.set_result(self._prompt_result_to_dict(result))
                        else:
                            fut.set_exception(ValueError(f"Unknown op: {kind}"))
                    except Exception as e:
                        if not fut.done():
                            fut.set_exception(e)
        except Exception as e:
            state.error = f"{type(e).__name__}: {e}"
            state.ready.set()  # unblock waiters — they'll see the error

    @staticmethod
    def _tool_to_dict(t) -> dict:
        return {
            "name": t.name,
            "description": getattr(t, "description", "") or "",
            "inputSchema": getattr(t, "inputSchema", None) or {"type": "object", "properties": {}},
        }

    @staticmethod
    def _call_result_to_dict(result) -> dict:
        content = []
        for item in getattr(result, "content", []) or []:
            t = getattr(item, "type", None)
            if t == "text":
                content.append({"type": "text", "text": getattr(item, "text", "")})
            elif t == "image":
                content.append({"type": "image",
                                "mimeType": getattr(item, "mimeType", None),
                                "data": getattr(item, "data", None)})
            else:
                try:
                    content.append(item.model_dump())
                except Exception:
                    content.append({"type": t or "unknown", "repr": repr(item)})
        return {
            "isError": bool(getattr(result, "isError", False)),
            "content": content,
            "structuredContent": getattr(result, "structuredContent", None),
        }

    @staticmethod
    def _resource_result_to_dict(result) -> dict:
        out = []
        for c in getattr(result, "contents", []) or []:
            try:
                out.append(c.model_dump())
            except Exception:
                out.append({"repr": repr(c)})
        return {"contents": out}

    @staticmethod
    def _prompt_result_to_dict(result) -> dict:
        try:
            return result.model_dump()
        except Exception:
            return {"repr": repr(result)}

    # -------------------- public sync API --------------------

    @property
    def available(self) -> bool:
        return _MCP_OK

    @property
    def unavailable_reason(self) -> str | None:
        return _MCP_ERR

    def connect(self, name: str, config: dict, timeout: float = 30) -> dict:
        """Connect a server. Blocks until initialized (or error). Returns status dict."""
        if not _MCP_OK:
            return {"ok": False, "error": f"MCP SDK unavailable: {_MCP_ERR}"}
        if name in self._servers:
            return {"ok": False, "error": f"Server already connected: {name}"}

        async def _start():
            state = ServerState(name=name, config=config)
            state.task = asyncio.create_task(self._server_task(state))
            self._servers[name] = state
            try:
                await asyncio.wait_for(state.ready.wait(), timeout=timeout)
            except asyncio.TimeoutError:
                state.error = f"timed out after {timeout}s"
            return {
                "ok": state.error is None,
                "error": state.error,
                "tools": state.tools,
                "resources": state.resources,
                "prompts": state.prompts,
            }

        return self._submit(_start(), timeout=timeout + 5)

    def disconnect(self, name: str, timeout: float = 10) -> dict:
        if name not in self._servers:
            return {"ok": False, "error": f"Unknown server: {name}"}

        async def _stop():
            state = self._servers.pop(name, None)
            if state is None:
                return {"ok": False, "error": "gone"}
            try:
                await state.queue.put(None)
            except Exception:
                pass
            if state.task is not None:
                try:
                    await asyncio.wait_for(state.task, timeout=timeout)
                except Exception:
                    state.task.cancel()
            return {"ok": True}

        return self._submit(_stop(), timeout=timeout + 5)

    def list_servers(self) -> list[dict]:
        return [
            {
                "name": s.name,
                "transport": s.config.get("transport", "stdio"),
                "error": s.error,
                "tool_count": len(s.tools),
                "resource_count": len(s.resources),
                "prompt_count": len(s.prompts),
            }
            for s in self._servers.values()
        ]

    def get_tools(self, name: str) -> list[dict]:
        s = self._servers.get(name)
        return list(s.tools) if s else []

    def call_tool(self, server: str, tool: str, arguments: dict | None = None,
                  timeout: float = 120) -> dict:
        state = self._servers.get(server)
        if state is None:
            return {"isError": True, "content": [{"type": "text",
                    "text": f"Unknown MCP server: {server}"}]}
        if state.error:
            return {"isError": True, "content": [{"type": "text",
                    "text": f"Server {server} errored: {state.error}"}]}

        async def _call():
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            await state.queue.put(("call_tool",
                                   {"name": tool, "arguments": arguments or {}}, fut))
            return await asyncio.wait_for(fut, timeout=timeout)

        try:
            return self._submit(_call(), timeout=timeout + 5)
        except Exception as e:
            return {"isError": True, "content": [{"type": "text", "text": str(e)}]}

    def read_resource(self, server: str, uri: str, timeout: float = 60) -> dict:
        state = self._servers.get(server)
        if state is None or state.error:
            return {"error": f"Server not available: {server}"}

        async def _read():
            fut: asyncio.Future = asyncio.get_event_loop().create_future()
            await state.queue.put(("read_resource", {"uri": uri}, fut))
            return await asyncio.wait_for(fut, timeout=timeout)

        try:
            return self._submit(_read(), timeout=timeout + 5)
        except Exception as e:
            return {"error": str(e)}

    def shutdown(self) -> None:
        for name in list(self._servers.keys()):
            try:
                self.disconnect(name, timeout=3)
            except Exception:
                pass
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass


mcp_manager = MCPManager()
