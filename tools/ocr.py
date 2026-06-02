"""
OCR tool — extract text from images using Tesseract or PIL.
Falls back gracefully if Tesseract isn't installed.
"""

import json
from pathlib import Path
from tools.registry import registry


def ocr_extract(image_path: str, language: str = "eng") -> str:
    """Extract text from an image using OCR."""
    p = Path(image_path)
    if not p.exists():
        return json.dumps({"error": f"Image not found: {image_path}"})

    # Try pytesseract first
    try:
        import pytesseract
        from PIL import Image
        img = Image.open(image_path)
        text = pytesseract.image_to_string(img, lang=language)
        return json.dumps({
            "text": text.strip(),
            "chars": len(text.strip()),
            "engine": "tesseract",
        }, ensure_ascii=False)
    except ImportError:
        pass
    except Exception as e:
        return json.dumps({"error": f"Tesseract error: {e}"})

    # Fallback: use vision_analyze tool for OCR-like extraction
    try:
        from tools.vision import vision_analyze
        result = json.loads(vision_analyze(
            image_path,
            "Extract ALL text from this image exactly as written. "
            "Preserve formatting, line breaks, and structure."
        ))
        return json.dumps({
            "text": result.get("analysis", ""),
            "engine": "vision_model",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"OCR failed: {e}. Install pytesseract for best results."})


registry.register(
    name="ocr",
    description=(
        "Image → text. Tesseract → vision fallback. "
        "✓ exact text extraction; vision_analyze for understanding."
    ),
    parameters={
        "type": "object",
        "properties": {
            "image_path": {"type": "string", "description": "Image file path."},
            "language": {"type": "string", "description": "OCR lang (default eng)."},
        },
        "required": ["image_path"],
    },
    execute=ocr_extract,
)
