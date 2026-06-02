"""
Tool registry - modular tool/skill system.

Tools are registered here and made available to the agent.
Each tool has a name, description, parameter schema, and execute function.

Context-aware: tools that accept a `ctx` keyword argument automatically
receive a ToolContext with abort signals, metadata streaming, and session info.
"""

import inspect


class ToolRegistry:
    def __init__(self):
        self._tools: dict = {}

    def register(self, name: str, description: str, parameters: dict, execute):
        # Detect whether the execute function accepts a `ctx` parameter
        accepts_ctx = False
        try:
            sig = inspect.signature(execute)
            accepts_ctx = "ctx" in sig.parameters
        except (ValueError, TypeError):
            pass

        self._tools[name] = {
            "name": name,
            "description": description,
            "parameters": parameters,
            "execute": execute,
            "accepts_ctx": accepts_ctx,
        }

    def unregister(self, name: str):
        self._tools.pop(name, None)

    def get(self, name: str) -> dict | None:
        return self._tools.get(name)

    def accepts_context(self, name: str) -> bool:
        """Check if a tool accepts a ToolContext parameter."""
        tool = self._tools.get(name)
        return tool.get("accepts_ctx", False) if tool else False

    def list_tools(self) -> list[dict]:
        return [
            {"name": t["name"], "description": t["description"]}
            for t in self._tools.values()
        ]

    def get_schemas(self) -> list[dict]:
        """Return OpenAI function-calling schemas for all registered tools."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["parameters"],
                },
            }
            for t in self._tools.values()
        ]

    def execute(self, name: str, arguments: dict, ctx=None) -> str:
        """Execute a tool by name.

        If the tool accepts a `ctx` parameter and one is provided,
        it will be injected automatically. The ctx is never passed
        as part of the LLM arguments dict.
        """
        tool = self._tools.get(name)
        if not tool:
            return f"Unknown tool: {name}"

        if tool.get("accepts_ctx") and ctx is not None:
            return tool["execute"](**arguments, ctx=ctx)
        return tool["execute"](**arguments)


registry = ToolRegistry()
