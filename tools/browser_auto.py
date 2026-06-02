"""
Browser tool — headless browser automation with a persistent session.

Uses Playwright with a long-lived browser + page so that multi-step flows
(navigate → click → type → submit → snapshot) all operate on the same session.
Falls back to agent-browser CLI if Playwright is not installed.
"""

import json
import os
import tempfile
import threading
from pathlib import Path
from tools.registry import registry

# ── Persistent Playwright session ───────────────────────────────────────────

_lock = threading.Lock()
_pw = None          # playwright instance (sync_playwright().start())
_pw_browser = None  # Browser
_pw_page = None     # active Page

_HAS_PLAYWRIGHT = None


def _check_playwright() -> bool:
    global _HAS_PLAYWRIGHT
    if _HAS_PLAYWRIGHT is None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
            _HAS_PLAYWRIGHT = True
        except ImportError:
            _HAS_PLAYWRIGHT = False
    return _HAS_PLAYWRIGHT


def _get_page():
    """Return (or lazily create) the persistent Playwright page."""
    global _pw, _pw_browser, _pw_page
    with _lock:
        if _pw is None:
            from playwright.sync_api import sync_playwright
            _pw = sync_playwright().start()
        if _pw_browser is None or not _pw_browser.is_connected():
            _pw_browser = _pw.chromium.launch(headless=True)
        if _pw_page is None or _pw_page.is_closed():
            _pw_page = _pw_browser.new_page()
        return _pw_page


def _close_session():
    """Tear down the persistent session."""
    global _pw, _pw_browser, _pw_page
    with _lock:
        try:
            if _pw_page and not _pw_page.is_closed():
                _pw_page.close()
        except Exception:
            pass
        try:
            if _pw_browser and _pw_browser.is_connected():
                _pw_browser.close()
        except Exception:
            pass
        try:
            if _pw:
                _pw.stop()
        except Exception:
            pass
        _pw = None
        _pw_browser = None
        _pw_page = None


def _page_text(page, max_chars: int = 8000) -> str:
    """Extract visible text from the current page."""
    try:
        return page.inner_text("body")[:max_chars]
    except Exception:
        return ""


# ── agent-browser CLI fallback ───────────────────────────────────────────────

def _agentbrowser_fallback(action, url, selector, text, direction, key) -> dict:
    try:
        from tools.browser import browser as old_browser
        return json.loads(old_browser(
            action=action, url=url, ref=selector, text=text,
            direction=direction, key=key))
    except Exception as e:
        return {"error": f"agent-browser fallback failed: {e}"}


# ── Unified browser tool ─────────────────────────────────────────────────────

def browser_auto(action: str, url: str = "", selector: str = "",
                 text: str = "", script: str = "",
                 screenshot_path: str = "", wait: int = 3000,
                 direction: str = "", key: str = "") -> str:
    """Unified persistent-session browser automation."""

    # Actions that don't need a URL
    no_url_needed = {"close", "snapshot", "back", "scroll", "press"}
    if not url and action not in no_url_needed:
        return json.dumps({"error": f"url required for action '{action}'"})

    # ── agent-browser CLI path (preferred when installed) ────────────────
    # Only use for simple stateless actions where session continuity isn't needed
    if action in ("navigate", "click", "type", "back", "press", "scroll", "snapshot"):
        result = _agentbrowser_fallback(action, url, selector or "", text or "", direction, key)
        if not result.get("error"):
            return json.dumps(result, ensure_ascii=False, default=str)

    # ── Playwright path ──────────────────────────────────────────────────
    if not _check_playwright():
        return json.dumps({
            "error": (
                "No browser backend available. "
                "Install Playwright: pip install playwright && python -m playwright install chromium"
            )
        })

    try:
        timeout_ms = wait if wait else 3000

        if action == "navigate":
            page = _get_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 3)
            return json.dumps({
                "url": page.url,
                "title": page.title(),
                "content": _page_text(page),
            }, ensure_ascii=False)

        elif action == "snapshot":
            # Read the current page without navigating
            page = _get_page()
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 3)
            return json.dumps({
                "url": page.url,
                "title": page.title(),
                "content": _page_text(page),
            }, ensure_ascii=False)

        elif action == "screenshot":
            page = _get_page()
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 3)
            path = screenshot_path or os.path.join(
                tempfile.gettempdir(), "agent_browser_screenshot.png")
            page.screenshot(path=path, full_page=True)
            return json.dumps({"screenshot": path, "url": page.url}, ensure_ascii=False)

        elif action == "click":
            page = _get_page()
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 3)
            page.click(selector, timeout=timeout_ms)
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return json.dumps({
                "clicked": selector,
                "url": page.url,
                "content": _page_text(page),
            }, ensure_ascii=False)

        elif action == "type":
            page = _get_page()
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 3)
            page.fill(selector, text)
            return json.dumps({"typed": text, "into": selector}, ensure_ascii=False)

        elif action == "press":
            page = _get_page()
            k = key or text or "Enter"
            page.keyboard.press(k)
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            return json.dumps({
                "pressed": k,
                "url": page.url,
                "content": _page_text(page),
            }, ensure_ascii=False)

        elif action == "scrape":
            page = _get_page()
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 3)
            if selector:
                elements = page.query_selector_all(selector)
                texts = [el.text_content().strip() for el in elements[:50]]
                return json.dumps({
                    "selector": selector,
                    "results": texts,
                    "count": len(texts),
                }, ensure_ascii=False)
            else:
                return json.dumps({"text": _page_text(page, max_chars=15000)}, ensure_ascii=False)

        elif action == "scroll":
            page = _get_page()
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 3)
            d = (direction or text or "down").lower()
            if d == "down":
                page.evaluate("window.scrollBy(0, window.innerHeight)")
            else:
                page.evaluate("window.scrollBy(0, -window.innerHeight)")
            page.wait_for_timeout(500)
            return json.dumps({
                "scrolled": d,
                "content": _page_text(page),
            }, ensure_ascii=False)

        elif action == "back":
            page = _get_page()
            page.go_back(timeout=timeout_ms)
            return json.dumps({
                "url": page.url,
                "content": _page_text(page),
            }, ensure_ascii=False)

        elif action == "run_script":
            page = _get_page()
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms * 3)
            result = page.evaluate(script)
            return json.dumps({"result": result}, ensure_ascii=False, default=str)

        elif action == "close":
            _close_session()
            return json.dumps({"closed": True})

        else:
            return json.dumps({
                "error": f"Unknown action: '{action}'. "
                         "Valid: navigate, snapshot, screenshot, click, type, press, "
                         "scrape, scroll, back, run_script, close"
            })

    except Exception as e:
        return json.dumps({"error": str(e)})


registry.register(
    name="browser",
    description=(
        "Headless browser, persistent session. ✗ user cookies → use read_browser.\n"
        "Actions: navigate(url)\u2192text | snapshot\u2192current text | screenshot\u2192PNG path | "
        "click(selector) | type(selector,text) | press(key) | scrape(selector)\u2192text | "
        "scroll(direction) | back | run_script(js)\u2192result | close.\n"
        "Flow: navigate \u2192 click/type \u2192 press Enter \u2192 snapshot \u2192 screenshot"
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "snapshot", "screenshot", "click", "type", "press",
                         "scrape", "scroll", "back", "run_script", "close"],
                "description": "Browser action to perform.",
            },
            "url": {
                "type": "string",
                "description": "URL to navigate to. Optional for snapshot/scroll/back/press/close.",
            },
            "selector": {
                "type": "string",
                "description": "CSS selector for click/type/scrape actions.",
            },
            "text": {
                "type": "string",
                "description": "Text to type (type action), scroll direction (scroll action).",
            },
            "key": {
                "type": "string",
                "description": "Key to press, e.g. 'Enter', 'Tab', 'Escape' (press action).",
            },
            "script": {
                "type": "string",
                "description": "JavaScript to execute (run_script action).",
            },
            "screenshot_path": {
                "type": "string",
                "description": "File path to save screenshot (screenshot action). Defaults to temp file.",
            },
            "wait": {
                "type": "integer",
                "description": "Timeout in ms for navigation/click (default 3000).",
            },
        },
        "required": ["action"],
    },
    execute=browser_auto,
)
