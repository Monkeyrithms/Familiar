"""
HTTP client tool — full REST client for API interaction.
Supports GET/POST/PUT/DELETE/PATCH with headers, auth, and JSON body.
"""

import json
import re
import httpx
from tools.registry import registry

try:
    from selectolax.parser import HTMLParser as _HTMLParser
    _HAVE_SELECTOLAX = True
except ImportError:  # pragma: no cover - depends on environment
    _HAVE_SELECTOLAX = False


def _html_to_text(html: str) -> str:
    """Strip HTML to readable text.

    Prefers selectolax (a fast C/Lexbor parser that walks the real DOM and
    drops script/style/nav cleanly); falls back to regex stripping when it
    isn't installed.
    """
    if _HAVE_SELECTOLAX:
        try:
            tree = _HTMLParser(html)
            for tag in tree.css("script, style, noscript, template, svg"):
                tag.decompose()
            body = tree.body or tree.root
            text = body.text(separator=" ", strip=True) if body else ""
            return re.sub(r"\s+", " ", text).strip()
        except Exception:
            pass  # fall through to regex
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def http_request(method: str, url: str, headers: dict = None,
                 body: str = "", json_body: dict = None,
                 auth_bearer: str = "", timeout: int = 30,
                 extract_text: bool = False) -> str:
    """Make an HTTP request and return the response."""
    method = method.upper()
    if method not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
        return json.dumps({"error": f"Unsupported method: {method}"})

    req_headers = dict(headers or {})
    if auth_bearer:
        req_headers["Authorization"] = f"Bearer {auth_bearer}"
    if not req_headers.get("User-Agent"):
        req_headers["User-Agent"] = "Mozilla/5.0 Agent/1.0"

    from core.redact import redact

    kwargs = {
        "method": method,
        "url": url,
        "headers": req_headers,
        "timeout": timeout,
        "follow_redirects": True,
    }

    if json_body:
        kwargs["json"] = json_body
    elif body:
        kwargs["content"] = body

    try:
        resp = httpx.request(**kwargs)

        # Extract clean text from HTML (replaces old web_fetch behavior)
        if extract_text:
            text = _html_to_text(resp.text)
            return redact(json.dumps({
                "status": resp.status_code,
                "text": text[:15000],
                "url": str(resp.url),
            }, ensure_ascii=False))

        try:
            resp_body = resp.json()
        except Exception:
            resp_body = resp.text[:10000]

        result = {
            "status": resp.status_code,
            "headers": dict(resp.headers),
            "body": resp_body,
            "url": str(resp.url),
        }
        return redact(json.dumps(result, ensure_ascii=False, default=str))
    except httpx.TimeoutException:
        return json.dumps({"error": f"Request timed out after {timeout}s"})
    except Exception as e:
        return json.dumps({"error": str(e)})


registry.register(
    name="http",
    description=(
        "HTTP request. GET | POST | PUT | DELETE | PATCH.\n"
        "- ✓ APIs, endpoints, webhooks, web pages.\n"
        "- extract_text=true → strip HTML → clean text (web reader mode)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"],
                "description": "HTTP method.",
            },
            "url": {
                "type": "string",
                "description": "Full URL.",
            },
            "headers": {
                "type": "object",
                "description": "Headers k-v map.",
            },
            "body": {
                "type": "string",
                "description": "Raw body (POST|PUT|PATCH).",
            },
            "json_body": {
                "type": "object",
                "description": "JSON body (auto Content-Type).",
            },
            "auth_bearer": {
                "type": "string",
                "description": "Bearer token → Authorization.",
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds (default 30).",
            },
            "extract_text": {
                "type": "boolean",
                "description": "Strip HTML → clean text.",
            },
        },
        "required": ["method", "url"],
    },
    execute=http_request,
)
