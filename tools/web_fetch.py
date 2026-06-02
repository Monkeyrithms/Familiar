"""
Web fetch tool - retrieve and extract content from URLs.
Returns clean text/markdown, not raw HTML.
"""

import json
import re
import httpx
from tools.registry import registry


def _html_to_text(html: str) -> str:
    """Strip HTML tags and decode entities for a rough plaintext extraction."""
    # Remove script/style blocks
    text = re.sub(r'<(script|style)[^>]*>.*?</\1>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common entities
    for entity, char in [('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'),
                         ('&quot;', '"'), ('&#39;', "'"), ('&nbsp;', ' ')]:
        text = text.replace(entity, char)
    # Collapse whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def web_fetch(url: str, max_chars: int = 20000) -> str:
    """Fetch a URL and return its text content."""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    try:
        resp = httpx.get(url, headers=headers, follow_redirects=True, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        return json.dumps({"error": f"Failed to fetch {url}: {e}"})

    content_type = resp.headers.get("content-type", "")

    # JSON response — return directly
    if "json" in content_type:
        text = resp.text[:max_chars]
        return json.dumps({"url": url, "content": text, "type": "json"}, ensure_ascii=False)

    # Plain text
    if "text/plain" in content_type:
        text = resp.text[:max_chars]
        return json.dumps({"url": url, "content": text, "type": "text"}, ensure_ascii=False)

    # HTML — extract text
    text = _html_to_text(resp.text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n(content truncated)"

    return json.dumps({"url": url, "content": text, "type": "html"}, ensure_ascii=False)


registry.register(
    name="web_fetch",
    description=(
        "Fetch URL → text. HTML → extracted text; JSON/text → raw."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "URL to fetch.",
            },
            "max_chars": {
                "type": "integer",
                "description": "Max chars (default 20000).",
            },
        },
        "required": ["url"],
    },
    execute=web_fetch,
)
