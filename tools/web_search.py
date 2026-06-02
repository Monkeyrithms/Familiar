"""
Web search tool using Tavily API.
"""

import json
from core.providers import load_keys
from tools.registry import registry

TAVILY_BASE_URL = "https://api.tavily.com"


def web_search(query: str, num_results: int = 5) -> str:
    """Search the web and return results."""
    import httpx  # lazy: ~190ms import kept off startup (only needed on search)
    keys = load_keys()
    api_key = keys.get("tavily", {}).get("api_key", "")
    if not api_key:
        return json.dumps({"error": "No Tavily API key configured. Add it in Settings."})

    response = httpx.post(
        f"{TAVILY_BASE_URL}/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": min(num_results, 20),
            "include_raw_content": False,
            "include_images": False,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    results = []
    for r in data.get("results", []):
        results.append({
            "title": r.get("title", ""),
            "url": r.get("url", ""),
            "content": r.get("content", ""),
        })

    return json.dumps({"results": results}, ensure_ascii=False)


# --- Register with tool registry ---

registry.register(
    name="web_search",
    description=(
        "Web search. ✓ recent events, news, prices, unknown facts, anything "
        "needing up-to-date internet data."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query.",
            },
            "num_results": {
                "type": "integer",
                "description": "# results (default 5, max 20).",
            },
        },
        "required": ["query"],
    },
    execute=web_search,
)
