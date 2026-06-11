"""
Vision tool - analyze images from URLs using a vision-capable model.
"""

import json
import base64
import httpx
from core.providers import get_client, load_keys, PROVIDER_INFO
from tools.registry import registry


def vision_analyze(image_url: str, prompt: str = "Describe this image in detail.") -> str:
    """Analyze an image from a URL or local file path using a vision-capable model."""
    import os
    from core.agent import load_config  # lazy: avoid circular import at tool-registration time
    cfg = load_config()
    provider = cfg.get("provider", "openrouter")
    # Use the same provider/model as the main agent — most modern models support vision
    model = cfg.get("model", "deepseek/deepseek-chat-v3-0324")

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
            return json.dumps({"error": f"Failed to read local image: {e}"})
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
            return json.dumps({"error": f"Failed to fetch image: {e}"})

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
        return json.dumps({"analysis": analysis}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"Vision analysis failed: {e}"})


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
