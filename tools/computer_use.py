"""
Computer-use tool — let the agent drive the REAL desktop: move/click the mouse,
type, press keys, scroll, and screenshot.

Design note: Familiar's agent loop already IS the computer-use loop. The model
screenshots, reasons about what it sees (vision), then calls an action — and the
harness loops. So this tool deliberately does NOT contain its own plan/act loop;
it exposes clean primitives and lets the agent orchestrate.

SAFETY — this drives the user's actual machine, not a sandbox/VM:
  • Actuation (mouse/keyboard) is GATED. It stays OFF until the user sets
    "computer_use_enabled": true in config.json. While off, every motion action
    is refused with an explanatory message. Screenshots are always allowed
    (read-only).
  • pyautogui FAILSAFE stays ON: slam the mouse into a screen corner to abort.
  • Optional region clamp: "computer_use_region": [x, y, w, h] in config.json
    refuses any click/move outside that rectangle.

DPI / scaling — the #1 Windows footgun:
  On a scaled display (125%/150%) the screenshot is in physical pixels while
  pyautogui clicks in logical pixels, so naive coords miss. The model reports
  coordinates in the SCREENSHOT it saw (coord_space="image", the default) and
  this tool scales image→logical automatically using the last capture's size.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

from tools.registry import registry

# Cached size (width, height) of the most recent screenshot, in physical pixels.
# Used to translate image-space coordinates the model reports into the logical
# coordinate space pyautogui actuates in. Guarded because tools run off-thread.
_last_shot_size: tuple[int, int] | None = None
_lock = threading.Lock()


def _cfg() -> dict:
    """Read computer-use settings from config.json. Missing file → all defaults."""
    try:
        from core.agent import load_config
        return load_config() or {}
    except Exception:
        try:
            p = Path(__file__).parent.parent / "config.json"
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        except Exception:
            return {}


def _enabled() -> bool:
    cfg = _cfg()
    if str(os.environ.get("FAMILIAR_COMPUTER_USE", "")).lower() in ("1", "true", "yes"):
        return True
    return bool(cfg.get("computer_use_enabled", False))


def _region() -> tuple[int, int, int, int] | None:
    r = _cfg().get("computer_use_region")
    if isinstance(r, (list, tuple)) and len(r) == 4:
        try:
            return tuple(int(v) for v in r)  # type: ignore[return-value]
        except Exception:
            return None
    return None


def _pg():
    """Import pyautogui lazily with the failsafe armed."""
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.02
    return pyautogui


def _to_logical(x: float, y: float, coord_space: str):
    """Translate a reported coordinate into pyautogui's logical pixel space.

    coord_space="image": x,y are in the last screenshot's physical pixels →
    scale by logical/physical. coord_space="logical": pass through unchanged.
    """
    pg = _pg()
    log_w, log_h = pg.size()
    if coord_space == "logical" or _last_shot_size is None:
        return int(round(x)), int(round(y))
    phys_w, phys_h = _last_shot_size
    if not phys_w or not phys_h:
        return int(round(x)), int(round(y))
    sx = log_w / phys_w
    sy = log_h / phys_h
    return int(round(x * sx)), int(round(y * sy))


def _check_bounds(x: int, y: int) -> str | None:
    pg = _pg()
    w, h = pg.size()
    if not (0 <= x <= w and 0 <= y <= h):
        return f"Coordinate ({x},{y}) is outside the screen ({w}x{h})."
    reg = _region()
    if reg:
        rx, ry, rw, rh = reg
        if not (rx <= x <= rx + rw and ry <= y <= ry + rh):
            return (f"Coordinate ({x},{y}) is outside the allowed region "
                    f"{reg} (computer_use_region in config.json).")
    return None


def _capture(analyze: bool, prompt: str) -> dict:
    """Grab the whole screen with pyautogui (physical pixels), save, cache size,
    and optionally run vision analysis."""
    global _last_shot_size
    pg = _pg()
    img = pg.screenshot()
    with _lock:
        _last_shot_size = (img.width, img.height)

    tmp_path = Path(tempfile.gettempdir()) / "agent_computer_use.png"
    img.save(tmp_path)

    from tools.screenshot import _capture_diagnostics
    capture = _capture_diagnostics(tmp_path.read_bytes())
    if not capture.get("valid"):
        return {"error": "Desktop capture returned a black or invalid frame.",
                "capture": capture, "vision_attempted": False,
                "rate_limited": False}

    log_w, log_h = pg.size()
    result = {
        "captured": True,
        "image_path": str(tmp_path),
        "screenshot_size": [img.width, img.height],
        "logical_size": [log_w, log_h],
        "note": "Report click/move coords in screenshot-pixel space "
                "(coord_space='image', the default); scaling is automatic.",
        "capture": capture,
        "vision_attempted": bool(analyze),
        "rate_limited": False,
    }
    if analyze:
        try:
            from tools.vision import vision_analyze
            ap = prompt or ("Describe the desktop. List clickable UI elements "
                            "(buttons, fields, icons) with their approximate "
                            "pixel coordinates.")
            analysis = json.loads(vision_analyze(str(tmp_path), ap))
            result["analysis"] = analysis.get("analysis", "")
            if "error" in analysis:
                result["analysis_error"] = analysis["error"]
            result["vision_attempted"] = bool(analysis.get("vision_attempted"))
            result["vision_provider"] = analysis.get("provider")
            result["vision_model"] = analysis.get("model")
            result["rate_limited"] = bool(analysis.get("rate_limited", False))
        except Exception as e:
            result["analysis_error"] = str(e)
    return result


_MOTION = {"click", "double_click", "right_click", "move", "drag",
           "type", "press", "hotkey", "scroll"}


def computer_use(action: str, x: float = None, y: float = None,
                 x2: float = None, y2: float = None,
                 text: str = "", keys: str = "", amount: int = 0,
                 coord_space: str = "image",
                 analyze: bool = True, prompt: str = "") -> str:
    """Drive the real desktop. See registry description for actions."""
    action = (action or "").strip().lower()

    try:
        _pg()
    except Exception as e:
        return json.dumps({"error": f"pyautogui unavailable: {e}. "
                           "pip install pyautogui pillow"})

    # Read-only actions are always allowed.
    if action == "screenshot":
        return json.dumps(_capture(analyze, prompt), ensure_ascii=False)
    if action == "position":
        pg = _pg()
        px, py = pg.position()
        return json.dumps({"position": [px, py]})
    if action == "size":
        pg = _pg()
        w, h = pg.size()
        return json.dumps({"logical_size": [w, h]})

    # Everything below moves the mouse/keyboard → gated.
    if action in _MOTION and not _enabled():
        return json.dumps({
            "error": "Computer-use actuation is DISABLED.",
            "how_to_enable": 'Set "computer_use_enabled": true in config.json '
                             '(or env FAMILIAR_COMPUTER_USE=1), then retry.',
            "why": "This tool controls the real mouse/keyboard on this machine. "
                   "It is off by default so a stray or injected instruction "
                   "can't click things without the user opting in.",
        })

    pg = _pg()
    try:
        if action in ("click", "double_click", "right_click", "move"):
            if x is None or y is None:
                return json.dumps({"error": f"'{action}' needs x and y."})
            lx, ly = _to_logical(x, y, coord_space)
            err = _check_bounds(lx, ly)
            if err:
                return json.dumps({"error": err})
            if action == "move":
                pg.moveTo(lx, ly, duration=0.15)
            elif action == "click":
                pg.click(lx, ly)
            elif action == "double_click":
                pg.doubleClick(lx, ly)
            elif action == "right_click":
                pg.rightClick(lx, ly)
            return json.dumps({"status": "done", "action": action,
                               "at_logical": [lx, ly]})

        if action == "drag":
            if None in (x, y, x2, y2):
                return json.dumps({"error": "'drag' needs x,y (from) and x2,y2 (to)."})
            sx, sy = _to_logical(x, y, coord_space)
            dx, dy = _to_logical(x2, y2, coord_space)
            for cx, cy in ((sx, sy), (dx, dy)):
                err = _check_bounds(cx, cy)
                if err:
                    return json.dumps({"error": err})
            pg.moveTo(sx, sy, duration=0.1)
            pg.dragTo(dx, dy, duration=0.4, button="left")
            return json.dumps({"status": "done", "action": "drag",
                               "from": [sx, sy], "to": [dx, dy]})

        if action == "type":
            if not text:
                return json.dumps({"error": "'type' needs text."})
            pg.typewrite(text, interval=0.01)
            return json.dumps({"status": "done", "action": "type",
                               "chars": len(text)})

        if action == "press":
            if not keys:
                return json.dumps({"error": "'press' needs a key name, e.g. 'enter'."})
            pg.press(keys.strip())
            return json.dumps({"status": "done", "action": "press", "key": keys})

        if action == "hotkey":
            combo = [k.strip() for k in (keys or "").replace("+", ",").split(",") if k.strip()]
            if not combo:
                return json.dumps({"error": "'hotkey' needs keys, e.g. 'ctrl,c'."})
            pg.hotkey(*combo)
            return json.dumps({"status": "done", "action": "hotkey", "combo": combo})

        if action == "scroll":
            clicks = int(amount or 0)
            if clicks == 0:
                return json.dumps({"error": "'scroll' needs amount (+up / -down)."})
            if x is not None and y is not None:
                lx, ly = _to_logical(x, y, coord_space)
                pg.moveTo(lx, ly, duration=0.1)
            pg.scroll(clicks)
            return json.dumps({"status": "done", "action": "scroll", "amount": clicks})

        return json.dumps({"error": f"Unknown action '{action}'. Valid: screenshot, "
                           "click, double_click, right_click, move, drag, type, "
                           "press, hotkey, scroll, position, size."})
    except Exception as e:
        return json.dumps({"error": f"{action} failed: {e}"})


registry.register(
    name="computer_use",
    description=(
        "Drive the REAL desktop (mouse + keyboard). The agent loop is the "
        "computer-use loop: call action='screenshot' to SEE, reason about the "
        "image, then act. Actions: screenshot (read-only, optional vision) | "
        "click | double_click | right_click | move | drag (x,y→x2,y2) | "
        "type(text) | press(keys='enter') | hotkey(keys='ctrl,c') | "
        "scroll(amount,+up/-down) | position | size. Report x,y in the "
        "screenshot's pixel space (coord_space='image', default — DPI scaling "
        "is automatic). SAFETY: mouse/keyboard are OFF until "
        "'computer_use_enabled':true in config.json; screenshots always work. "
        "pyautogui failsafe = slam mouse to a corner to abort."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["screenshot", "click", "double_click", "right_click",
                         "move", "drag", "type", "press", "hotkey", "scroll",
                         "position", "size"],
                "description": "What to do.",
            },
            "x": {"type": "number", "description": "X coordinate (screenshot pixels)."},
            "y": {"type": "number", "description": "Y coordinate (screenshot pixels)."},
            "x2": {"type": "number", "description": "Drag destination X."},
            "y2": {"type": "number", "description": "Drag destination Y."},
            "text": {"type": "string", "description": "Text to type."},
            "keys": {"type": "string", "description": "Key for press ('enter') or "
                     "combo for hotkey ('ctrl,c')."},
            "amount": {"type": "integer", "description": "Scroll amount (+up/-down)."},
            "coord_space": {"type": "string", "enum": ["image", "logical"],
                            "description": "Coordinate space of x,y. Default 'image'."},
            "analyze": {"type": "boolean", "description": "screenshot: run vision (default true)."},
            "prompt": {"type": "string", "description": "screenshot: what to look for."},
        },
        "required": ["action"],
    },
    execute=computer_use,
)
