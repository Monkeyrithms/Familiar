"""
Vision tool - analyze images from URLs using a vision-capable model.
"""

import json
import base64
import httpx
from core.providers import get_client, load_keys, PROVIDER_INFO
from tools.registry import registry


def _resolve_vision_route() -> tuple[str, str, str]:
    """Return provider, model, source for an actual vision invocation."""
    from core.agent import current_agent, load_config
    cfg = load_config()
    dedicated_model = str(cfg.get("vision_model") or "").strip()
    if cfg.get("vision_enabled") and dedicated_model:
        provider = str(cfg.get("vision_provider") or "openrouter").strip()
        return provider or "openrouter", dedicated_model, "vision_settings"

    agent = current_agent()
    if agent is not None:
        return agent.provider, agent.model, "active_conversation"
    return (str(cfg.get("provider") or "openrouter"),
            str(cfg.get("model") or ""), "global_model")


def _exception_http_status(exc: BaseException) -> int | None:
    """Extract a real HTTP status without guessing from prose."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status = getattr(current, "status_code", None)
        response = getattr(current, "response", None)
        if status is None and response is not None:
            status = getattr(response, "status_code", None)
        try:
            if status is not None:
                return int(status)
        except (TypeError, ValueError):
            pass
        current = (getattr(current, "__cause__", None)
                   or getattr(current, "__context__", None))
    return None


def _vision_error(exc: BaseException, provider: str, model: str,
                  route_source: str) -> dict:
    status = _exception_http_status(exc)
    verified_rate_limit = status == 429
    return {
        "error": f"Vision analysis failed: {type(exc).__name__}: {exc}",
        "error_kind": "rate_limit" if verified_rate_limit else "provider_error",
        "rate_limited": verified_rate_limit, "http_status": status,
        "vision_attempted": True, "provider": provider, "model": model,
        "route_source": route_source,
    }


def vision_analyze(image_url: str, prompt: str = "Describe this image in detail.") -> str:
    """Analyze an image from a URL or local file path using a vision-capable model."""
    import os
    provider, model, route_source = _resolve_vision_route()
    # Use the same provider/model as the main agent — most modern models support vision

    # Fetch image and convert to base64 if it's not already a data URL
    if image_url.startswith("data:"):
        image_content = {"type": "image_url", "image_url": {"url": image_url}}
    elif os.path.isfile(image_url):
        # Local file path — read and base64 encode directly
        try:
            ext = os.path.splitext(image_url)[1].lower()
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "gif": "image/gif", "webp": "image/webp"}.get(ext.lstrip("."), "image/png")
            with open(image_url, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            image_content = {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
        except Exception as e:
            return json.dumps({"error": f"Failed to read local image: {e}",
                               "vision_attempted": False,
                               "rate_limited": False})
    else:
        try:
            resp = httpx.get(image_url, follow_redirects=True, timeout=20,
                             headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/png").split(";")[0]
            b64 = base64.b64encode(resp.content).decode("utf-8")
            image_content = {
                "type": "image_url",
                "image_url": {"url": f"data:{content_type};base64,{b64}"}
            }
        except Exception as e:
            return json.dumps({"error": f"Failed to fetch image: {e}",
                               "vision_attempted": False,
                               "rate_limited": False})

    try:
        client = get_client(provider)
        response = client.chat.completions.create(
            model=model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    image_content,
                ],
            }],
            max_tokens=1024,
        )
        analysis = response.choices[0].message.content or ""
        return json.dumps({
            "analysis": analysis, "vision_attempted": True,
            "provider": provider, "model": model,
            "route_source": route_source, "rate_limited": False,
            "http_status": 200,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps(_vision_error(e, provider, model, route_source),
                          ensure_ascii=False)


registry.register(
    name="vision_analyze",
    description=(
        "Analyze image (URL|path). ✓ user images, browser screenshots, charts, diagrams."
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_url": {"type": "string", "description": "URL or local path."},
            "prompt": {"type": "string", "description": "What to analyze (default: describe)."},
        },
        "required": ["image_url"],
    },
    execute=vision_analyze,
)
