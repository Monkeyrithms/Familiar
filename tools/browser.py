"""
Browser tool — local Chromium automation via agent-browser CLI.

Uses accessibility tree snapshots for text-based page representation.
Elements are referenced by @e1, @e2, etc. from the snapshot output.

Requires: npm install -g agent-browser && agent-browser install
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from tools.registry import registry
from core.proc import NO_WINDOW

# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

_sessions: dict[str, dict] = {}   # task_id -> {"name": str, "last_active": float}
_lock = threading.Lock()
_DEFAULT_TASK = "default"
INACTIVITY_TIMEOUT = 300  # 5 min


_USE_SHELL = (os.name == "nt")  # Windows needs shell=True for npx


def _find_agent_browser() -> str:
    """Locate agent-browser CLI."""
    # Direct binary (Linux/macOS global install)
    path = shutil.which("agent-browser")
    if path:
        return path
    # Windows: agent-browser.cmd in npm global bin
    if os.name == "nt":
        cmd = shutil.which("agent-browser.cmd")
        if cmd:
            return cmd
    # Fallback to npx (requires shell=True on Windows)
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if npx:
        return "npx agent-browser"
    raise FileNotFoundError(
        "agent-browser not found. Install with: npm install -g agent-browser && agent-browser install"
    )


def _get_session(task_id: str = None) -> dict:
    """Get or create a local browser session."""
    task_id = task_id or _DEFAULT_TASK
    with _lock:
        if task_id in _sessions:
            _sessions[task_id]["last_active"] = time.time()
            return _sessions[task_id]
    # Create outside lock
    info = {"name": f"ag_{uuid.uuid4().hex[:10]}", "last_active": time.time()}
    with _lock:
        if task_id in _sessions:
            return _sessions[task_id]
        _sessions[task_id] = info
    return info


def _run(command: str, args: list[str] = None, task_id: str = None,
         timeout: int = 30) -> dict:
    """Run an agent-browser CLI command and return parsed JSON."""
    session = _get_session(task_id)
    browser_cmd = _find_agent_browser()

    cmd_parts = browser_cmd.split() + [
        "--session", session["name"],
        "--json",
        command,
    ] + (args or [])

    # Use temp files for stdout/stderr (agent-browser daemon can hold pipes)
    tmp = tempfile.gettempdir()
    out_path = os.path.join(tmp, f"ab_out_{session['name']}.json")
    err_path = os.path.join(tmp, f"ab_err_{session['name']}.txt")

    try:
        with open(out_path, "w", encoding="utf-8") as out_f, open(err_path, "w", encoding="utf-8") as err_f:
            proc = subprocess.Popen(
                cmd_parts,
                stdout=out_f,
                stderr=err_f,
                stdin=subprocess.DEVNULL,
                shell=_USE_SHELL,
                creationflags=NO_WINDOW,  # no console flash on Windows
            )
            proc.wait(timeout=timeout)

        with open(out_path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if not raw:
            with open(err_path, "r", encoding="utf-8") as f:
                err = f.read().strip()
            return {"success": False, "error": err or "Empty response from agent-browser"}

        return json.loads(raw)
    except subprocess.TimeoutExpired:
        proc.kill()
        return {"success": False, "error": f"Command timed out after {timeout}s"}
    except json.JSONDecodeError:
        return {"success": False, "error": f"Non-JSON output: {raw[:300]}"}
    except FileNotFoundError as e:
        return {"success": False, "error": str(e)}
    finally:
        for p in (out_path, err_path):
            try:
                os.unlink(p)
            except OSError:
                pass


def _snap_text() -> str:
    """Helper: grab snapshot text, capped."""
    snap = _run("snapshot")
    text = snap.get("data", {}).get("snapshot", "") if snap.get("success") else ""
    return text[:8000] if text else "(no snapshot)"


# ---------------------------------------------------------------------------
# Unified browser tool
# ---------------------------------------------------------------------------

def browser(action: str, url: str = "", ref: str = "", text: str = "",
            direction: str = "", key: str = "") -> str:
    """Unified browser control. Routes to the appropriate action."""

    if action == "navigate":
        if not url:
            return json.dumps({"error": "url is required for navigate"})
        result = _run("open", [url])
        if not result.get("success"):
            return json.dumps({"error": result.get("error", "Navigation failed")})
        return json.dumps({"url": url, "snapshot": _snap_text()}, ensure_ascii=False)

    elif action == "snapshot":
        result = _run("snapshot")
        if not result.get("success"):
            return json.dumps({"error": result.get("error", "Snapshot failed")})
        text_out = result.get("data", {}).get("snapshot", "")
        return json.dumps({"snapshot": text_out[:8000] if text_out else "(empty page)"}, ensure_ascii=False)

    elif action == "click":
        if not ref:
            return json.dumps({"error": "ref is required for click (e.g. '@e5')"})
        if not ref.startswith("@"):
            ref = f"@{ref}"
        result = _run("click", [ref])
        if not result.get("success"):
            return json.dumps({"error": result.get("error", f"Click {ref} failed")})
        return json.dumps({"clicked": ref, "snapshot": _snap_text()}, ensure_ascii=False)

    elif action == "type":
        if not ref or not text:
            return json.dumps({"error": "ref and text are required for type"})
        if not ref.startswith("@"):
            ref = f"@{ref}"
        result = _run("type", [ref, text])
        if not result.get("success"):
            return json.dumps({"error": result.get("error", f"Type into {ref} failed")})
        return json.dumps({"typed": text, "into": ref})

    elif action == "scroll":
        direction = (direction or "down").lower().strip()
        if direction not in ("up", "down"):
            return json.dumps({"error": "direction must be 'up' or 'down'"})
        result = _run("scroll", [direction])
        if not result.get("success"):
            return json.dumps({"error": result.get("error", "Scroll failed")})
        return json.dumps({"scrolled": direction, "snapshot": _snap_text()}, ensure_ascii=False)

    elif action == "back":
        result = _run("back")
        if not result.get("success"):
            return json.dumps({"error": result.get("error", "Back failed")})
        return json.dumps({"action": "back", "snapshot": _snap_text()}, ensure_ascii=False)

    elif action == "press":
        if not key:
            return json.dumps({"error": "key is required for press (e.g. 'Enter')"})
        result = _run("press", [key])
        if not result.get("success"):
            return json.dumps({"error": result.get("error", f"Press {key} failed")})
        return json.dumps({"pressed": key})

    elif action == "close":
        task_id = _DEFAULT_TASK
        _run("close", task_id=task_id, timeout=10)
        with _lock:
            _sessions.pop(task_id, None)
        return json.dumps({"closed": True})

    else:
        return json.dumps({"error": f"Unknown action: {action}. "
                           "Use: navigate, snapshot, click, type, scroll, back, press, close"})


# ---------------------------------------------------------------------------
# Registration moved to browser_auto.py (unified with Playwright)
# This file is kept for agent-browser fallback functions.
# ---------------------------------------------------------------------------

# ── Helper functions used by BrowserTV in the UI ────────────────────

def browser_screenshot() -> str:
    """Take a screenshot of the current browser page. Returns JSON with 'path' key."""
    result = _run("screenshot")
    if result.get("success"):
        path = result.get("data", {}).get("path", "")
        if path:
            return json.dumps({"path": path})
    # Fallback: try Playwright if available
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser_inst = pw.chromium.connect_over_cdp("http://localhost:9222")
            page = browser_inst.contexts[0].pages[0] if browser_inst.contexts and browser_inst.contexts[0].pages else None
            if not page:
                return json.dumps({"path": ""})
            tmp = os.path.join(tempfile.gettempdir(), "agent_browser_tv.png")
            page.screenshot(path=tmp)
            return json.dumps({"path": tmp})
    except Exception:
        pass
    return json.dumps({"path": ""})


def browser_back() -> str:
    """Navigate back in the browser."""
    result = _run("back")
    if result.get("success"):
        return json.dumps({"action": "back"})
    return json.dumps({"error": result.get("error", "Back failed")})


_REGISTRATION_DISABLED = True  # noqa
if False:  # pragma: no cover
 registry.register(
    name="browser",
    description=(
        "Headless browser control. Actions: navigate (URL), snapshot (page text), "
        "click (ref), type (ref+text), scroll (up/down), back, press (key), close. "
        "Snapshots mark elements [ref=@e1] for click/type targets."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "snapshot", "click", "type", "scroll", "back", "press", "close"],
                "description": "Browser action.",
            },
            "url": {
                "type": "string",
                "description": "URL (navigate).",
            },
            "ref": {
                "type": "string",
                "description": "Snapshot ref e.g. '@e5' (click/type).",
            },
            "text": {
                "type": "string",
                "description": "Text to type.",
            },
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Scroll dir.",
            },
            "key": {
                "type": "string",
                "description": "Key e.g. 'Enter', 'Tab'.",
            },
        },
        "required": ["action"],
    },
    execute=browser,
)
