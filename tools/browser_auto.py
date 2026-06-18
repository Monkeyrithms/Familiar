"""
Browser tool — ONE visible, stealthed, persistent browser the agent drives
and the user can watch / take over (e.g. to solve a CAPTCHA).

THREADING: the agent executes tool calls on a ThreadPoolExecutor (see
core/agent.py). Playwright's *sync* objects are bound to the thread that
created them — using them from another pool thread orphans the event loop and
the session "dies between calls", relaunching a fresh window every cycle. So
ALL Playwright work is marshalled onto a single dedicated worker thread via a
queue; the agent's call threads only enqueue + wait.

Backends (priority, all via Playwright):
  1. Browserbase — cloud, stealth + server-side CAPTCHA solving (api key set). [Hermes parity]
  2. Camoufox    — self-hosted anti-detect Firefox over CDP (camoufox_cdp_url). [Hermes parity]
  3. Local       — headed Chromium with anti-detection hardening.
"""

import json
import os
import queue
import ssl
import tempfile
import threading
import urllib.request
from pathlib import Path

from tools.registry import registry

_CONFIG = Path(__file__).parent.parent / "config.json"

_STEALTH_JS = r"""
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
const _q = window.navigator.permissions && window.navigator.permissions.query;
if (_q) {
  window.navigator.permissions.query = (p) => (
    p && p.name === 'notifications'
      ? Promise.resolve({state: Notification.permission})
      : _q(p)
  );
}
"""

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_ELEMENTS_JS = r"""
() => {
  const pick = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const tag = el.tagName.toLowerCase();
    if (el.name) return tag + '[name="' + el.name + '"]';
    const ph = el.getAttribute && el.getAttribute('placeholder');
    if (ph) return tag + '[placeholder="' + ph.replace(/"/g, '\\"') + '"]';
    const al = el.getAttribute && el.getAttribute('aria-label');
    if (al) return tag + '[aria-label="' + al.replace(/"/g, '\\"') + '"]';
    const t = el.getAttribute && el.getAttribute('type');
    if (t) return tag + '[type="' + t + '"]';
    return tag;
  };
  const out = [];
  const els = document.querySelectorAll(
    'input,textarea,select,button,a[href],[role="button"],[contenteditable="true"]');
  els.forEach((el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    out.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      label: el.getAttribute('aria-label') || '',
      text: (el.innerText || el.value || '').trim().slice(0, 60),
      selector: pick(el),
    });
  });
  return out.slice(0, 60);
}
"""


# ── config ───────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8")).get("browser", {}) or {}
    except Exception:
        return {}


def _check_playwright() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except ImportError:
        return False


def _browserbase_connect_url() -> str | None:
    cfg = _cfg()
    key = cfg.get("browserbase_api_key") or os.environ.get("BROWSERBASE_API_KEY", "")
    proj = cfg.get("browserbase_project_id") or os.environ.get("BROWSERBASE_PROJECT_ID", "")
    if not key or not proj:
        return None
    payload = {"projectId": proj,
               "browserSettings": {"solveCaptchas": True,
                                   "advancedStealth": bool(cfg.get("browserbase_advanced_stealth"))}}
    req = urllib.request.Request(
        "https://api.browserbase.com/v1/sessions",
        data=json.dumps(payload).encode(),
        headers={"x-bb-api-key": key, "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        return json.loads(r.read().decode()).get("connectUrl")


# ── page helpers (run ON the worker thread) ───────────────────────────

def _page_text(page, max_chars: int = 6000) -> str:
    try:
        return page.inner_text("body")[:max_chars]
    except Exception:
        return ""


def _frames_for(page):
    return [page.main_frame] + [f for f in page.frames if f != page.main_frame]


def _elements(page) -> list:
    found = []
    for fr in _frames_for(page):
        try:
            for e in (fr.evaluate(_ELEMENTS_JS) or []):
                if fr != page.main_frame:
                    e["frame"] = fr.url[:80]
                found.append(e)
        except Exception:
            continue
    return found[:60]


def _value(frame, selector: str):
    try:
        return frame.input_value(selector, timeout=1500)
    except Exception:
        try:
            return frame.eval_on_selector(selector, "el => el.value || el.innerText || ''")
        except Exception:
            return None


def _robust_fill(page, selector: str, text: str, timeout: int, backend: str) -> dict:
    target = None
    for fr in _frames_for(page):
        try:
            fr.wait_for_selector(selector, timeout=1500, state="visible")
            target = fr
            break
        except Exception:
            continue
    if target is None:
        return {"error": f"selector not found in any frame: {selector}"}
    try:
        target.click(selector, timeout=timeout)
    except Exception:
        pass
    try:
        target.fill(selector, "")
        target.fill(selector, text)
    except Exception:
        pass
    if _value(target, selector) == text:
        return {"typed": text, "into": selector, "verified": True, "backend": backend}
    try:
        loc = target.locator(selector).first
        loc.click(timeout=timeout)
        try:
            loc.fill("")
        except Exception:
            pass
        loc.press_sequentially(text, delay=25)
    except Exception as e:
        return {"typed": text, "into": selector, "verified": False,
                "value": _value(target, selector), "warning": f"keystroke fallback: {e}",
                "backend": backend}
    val = _value(target, selector)
    return {"typed": text, "into": selector, "verified": val == text,
            "value": val, "backend": backend}


# ── dedicated single-thread Playwright worker ─────────────────────────

class _BrowserWorker:
    """Owns all Playwright objects on ONE thread; actions are marshalled in."""

    def __init__(self):
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        # owned exclusively by the worker thread:
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self.backend = None

    # -- public: callable from any thread --
    def submit(self, op, timeout: float = 180) -> dict:
        self._ensure_thread()
        box = {"done": threading.Event(), "result": None, "error": None}
        self._q.put((op, box))
        if not box["done"].wait(timeout):
            return {"error": f"browser worker timeout after {timeout}s"}
        if box["error"] is not None:
            e = box["error"]
            return {"error": f"{type(e).__name__}: {e}", "backend": self.backend}
        return box["result"]

    def _ensure_thread(self):
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._loop, daemon=True,
                                            name="familiar-browser")
            self._thread.start()

    def _loop(self):
        while True:
            op, box = self._q.get()
            try:
                box["result"] = op()
            except Exception as e:  # never let the worker thread die
                box["error"] = e
            finally:
                box["done"].set()

    # -- everything below runs ONLY on the worker thread --

    def _get_page(self):
        from playwright.sync_api import sync_playwright
        if self.page is not None and not self.page.is_closed():
            return self.page
        if self.pw is None:
            self.pw = sync_playwright().start()

        if self.browser is None or not self.browser.is_connected():
            self.browser = None
            # 1) Browserbase
            try:
                bb = _browserbase_connect_url()
            except Exception as e:
                bb = None
                print(f"[browser] Browserbase failed, falling back: {e}")
            if bb:
                self.browser = self.pw.chromium.connect_over_cdp(bb)
                self.backend = "browserbase"
            # 2) Camoufox
            if self.browser is None:
                cam = _cfg().get("camoufox_cdp_url", "")
                if cam:
                    try:
                        self.browser = self.pw.chromium.connect_over_cdp(cam)
                        self.backend = "camoufox"
                    except Exception as e:
                        print(f"[browser] Camoufox connect failed: {e}")
            # 3) Local headed Chromium
            if self.browser is None:
                self.browser = self.pw.chromium.launch(
                    headless=bool(_cfg().get("headless", False)),
                    args=["--disable-blink-features=AutomationControlled",
                          "--no-default-browser-check"])
                self.backend = "local"
            self.ctx = None  # force a fresh context for the new browser

        if self.ctx is None:
            if self.backend in ("browserbase", "camoufox") and self.browser.contexts:
                self.ctx = self.browser.contexts[0]
            else:
                self.ctx = self.browser.new_context(
                    user_agent=_UA, viewport={"width": 1280, "height": 800},
                    locale="en-US", timezone_id="America/New_York")
                if _cfg().get("stealth", True):
                    self.ctx.add_init_script(_STEALTH_JS)

        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self.page

    def _teardown(self):
        for obj, meth in ((self.page, "close"), (self.ctx, "close"),
                          (self.browser, "close"), (self.pw, "stop")):
            try:
                if obj:
                    getattr(obj, meth)()
            except Exception:
                pass
        self.pw = self.browser = self.ctx = self.page = None
        self.backend = None

    def act(self, action, url, selector, text, script,
            screenshot_path, wait, direction, key) -> dict:
        tmo = wait if wait else 5000

        if action == "close":
            self._teardown()
            return {"closed": True}
        if action == "status":
            return {"backend": self.backend or "(not started)",
                    "headless": bool(_cfg().get("headless", False)),
                    "stealth": bool(_cfg().get("stealth", True))}

        page = self._get_page()

        if action in ("navigate", "snapshot", "screenshot", "scrape") and url:
            page.goto(url, wait_until="domcontentloaded", timeout=tmo * 3)

        if action in ("navigate", "snapshot"):
            try:
                page.wait_for_load_state("networkidle", timeout=tmo)
            except Exception:
                pass
            return {"url": page.url, "title": page.title(), "backend": self.backend,
                    "elements": _elements(page), "text": _page_text(page)}

        if action == "screenshot":
            path = screenshot_path or os.path.join(tempfile.gettempdir(),
                                                   "agent_browser_screenshot.png")
            page.screenshot(path=path, full_page=True)
            return {"screenshot": path, "url": page.url}

        if action == "click":
            if not selector:
                return {"error": "selector required for click"}
            target = None
            for fr in _frames_for(page):
                try:
                    fr.wait_for_selector(selector, timeout=1500, state="visible")
                    target = fr
                    break
                except Exception:
                    continue
            (target or page).click(selector, timeout=tmo)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=tmo)
            except Exception:
                pass
            return {"clicked": selector, "url": page.url, "backend": self.backend,
                    "elements": _elements(page), "text": _page_text(page)}

        if action in ("type", "fill"):
            if not selector:
                return {"error": "selector required for type"}
            return _robust_fill(page, selector, text, tmo, self.backend)

        if action == "press":
            k = key or text or "Enter"
            page.keyboard.press(k)
            try:
                page.wait_for_load_state("domcontentloaded", timeout=tmo)
            except Exception:
                pass
            return {"pressed": k, "url": page.url, "backend": self.backend,
                    "elements": _elements(page), "text": _page_text(page)}

        if action == "scrape":
            if selector:
                els = page.query_selector_all(selector)
                return {"selector": selector,
                        "results": [el.text_content().strip() for el in els[:50]],
                        "count": min(len(els), 50)}
            return {"text": _page_text(page, max_chars=15000)}

        if action == "scroll":
            d = (direction or text or "down").lower()
            page.evaluate("(d) => window.scrollBy(0, d==='up' ? -window.innerHeight : window.innerHeight)", d)
            page.wait_for_timeout(400)
            return {"scrolled": d, "text": _page_text(page)}

        if action == "back":
            page.go_back(timeout=tmo)
            return {"url": page.url, "elements": _elements(page), "text": _page_text(page)}

        if action == "run_script":
            return {"result": page.evaluate(script)}

        return {"error": f"Unknown action: '{action}'. Valid: navigate, snapshot, screenshot, "
                         "click, type, press, scrape, scroll, back, run_script, status, close"}


_worker = _BrowserWorker()


# ── unified browser tool (runs on agent's pool thread, marshals to worker) ──

def browser_auto(action: str, url: str = "", selector: str = "",
                 text: str = "", script: str = "",
                 screenshot_path: str = "", wait: int = 5000,
                 direction: str = "", key: str = "") -> str:
    no_url_needed = {"close", "snapshot", "back", "scroll", "press", "click",
                     "type", "fill", "screenshot", "run_script", "scrape", "status"}
    if not url and action not in no_url_needed:
        return json.dumps({"error": f"url required for action '{action}'"})

    # Prefer the visible in-app "Agent" tab (user's session) when the GUI bridge
    # is wired — unless the user explicitly configured a cloud backend.
    cfg = _cfg()
    if cfg.get("prefer_inapp", True) and not (cfg.get("browserbase_api_key")
                                              or cfg.get("camoufox_cdp_url")):
        try:
            from tools.inapp_browser import inapp_available, inapp_call
            if inapp_available():
                timeout = max(25.0, (wait or 5000) / 1000.0 * 5)
                req = {"action": action, "url": url, "selector": selector, "text": text,
                       "script": script, "screenshot_path": screenshot_path,
                       "wait": wait, "direction": direction, "key": key}
                return json.dumps(inapp_call(req, timeout=timeout),
                                  ensure_ascii=False, default=str)
        except Exception:
            pass  # bridge unavailable (headless/no GUI) → fall through to Playwright

    if not _check_playwright():
        try:
            from tools.browser import browser as _ab
            return _ab(action=action, url=url, ref=selector, text=text,
                       direction=direction, key=key)
        except Exception:
            return json.dumps({"error": "No browser backend. Install Playwright: "
                                        "pip install playwright && python -m playwright install chromium"})

    result = _worker.submit(
        lambda: _worker.act(action, url, selector, text, script,
                            screenshot_path, wait, direction, key))
    return json.dumps(result, ensure_ascii=False, default=str)


registry.register(
    name="browser",
    description=(
        "Visible, stealthed, persistent browser the user can watch and take over "
        "(e.g. to solve a CAPTCHA). Backends auto-select: Browserbase (cloud, solves "
        "CAPTCHAs) > Camoufox > local headed Chromium with anti-detection. Runs on a "
        "dedicated thread so the session survives across calls.\n"
        "Actions: navigate(url) | snapshot | screenshot→PNG | click(selector) | "
        "type(selector,text) | press(key) | scrape(selector) | scroll(direction) | "
        "back | run_script(js) | status | close.\n"
        "navigate/snapshot return an 'elements' list with real CSS selectors — TYPE/CLICK "
        "those selectors (e.g. input[name=\"email\"], #password), not guesses. 'type' "
        "verifies the value stuck (handles React inputs + cross-origin iframes); check "
        "'verified'. If a CAPTCHA/human-check appears, STOP and ask the user to solve it "
        "in the visible window, then continue."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["navigate", "snapshot", "screenshot", "click", "type",
                                "press", "scrape", "scroll", "back", "run_script",
                                "status", "close"],
                       "description": "Browser action to perform."},
            "url": {"type": "string", "description": "URL to navigate to."},
            "selector": {"type": "string", "description": "CSS selector (click/type/scrape). "
                                                          "Use selectors from the 'elements' list."},
            "text": {"type": "string", "description": "Text to type (type), or scroll direction."},
            "key": {"type": "string", "description": "Key to press, e.g. 'Enter', 'Tab'."},
            "script": {"type": "string", "description": "JavaScript to run (run_script)."},
            "screenshot_path": {"type": "string", "description": "Path to save screenshot."},
            "wait": {"type": "integer", "description": "Timeout in ms (default 5000)."},
        },
        "required": ["action"],
    },
    execute=browser_auto,
)
