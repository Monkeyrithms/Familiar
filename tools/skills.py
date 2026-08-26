"""
Skills — learned, reusable PROCEDURES (distinct from tools).

A tool is a hand-coded primitive (file_write, web_search). A skill is a recipe:
a procedure the agent worked out once and is worth replaying — "scaffold a
Brikwerx card", "reconcile a backtest against live fills". Memory remembers
FACTS; skills remember HOW TO DO things.

How it works, end to end:
  • save  → store a named procedure with trigger patterns (comma-sep regex,
            same matching as memory keywords) + the steps.
  • recall (automatic) → on each user turn the agent loop calls
            match_skills(message); a skill whose triggers match is injected into
            that turn as "you've done this before, here's the procedure" — reusing
            the memory recall path, zero extra LLM cost.
  • used  → after following a skill, the agent reports the outcome with
            skill(action="used", name=..., success=true/false). This updates the
            success counter. THIS is the self-improving part: a skill that keeps
            leading to failure decays and stops being suggested. Without the
            feedback loop you don't have self-improving — you have a junk drawer.

Storage: one JSON file (skills.json), atomic-written, same pattern as tasks.json.
A skill is small (a procedure, not data), so a flat file is plenty.
"""

import json
import re
import time
from pathlib import Path
from tools.registry import registry

SKILLS_PATH = Path(__file__).parent.parent / "skills.json"

# A skill stops being auto-suggested once it's been used enough to judge AND its
# success rate is poor. We don't demote on a single failure (one bad run isn't a
# bad procedure) — only once there's evidence.
_MIN_USES_TO_JUDGE = 3
_DEMOTE_BELOW = 0.4


# ── Persistence ──────────────────────────────────────────────────────

def load_skills() -> list[dict]:
    if SKILLS_PATH.exists():
        try:
            return json.loads(SKILLS_PATH.read_text(encoding="utf-8")).get("skills", [])
        except (json.JSONDecodeError, KeyError):
            pass
    return []


def save_skills(skills: list[dict]) -> None:
    tmp = SKILLS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps({"skills": skills, "updated_at": time.time()}, indent=2),
                   encoding="utf-8")
    tmp.replace(SKILLS_PATH)


def _find(skills: list[dict], name: str) -> dict | None:
    return next((s for s in skills if s["name"].lower() == name.lower()), None)


def _rate(s: dict) -> float:
    u = s.get("uses", 0)
    return (s.get("success", 0) / u) if u else 1.0


def _is_demoted(s: dict) -> bool:
    return s.get("uses", 0) >= _MIN_USES_TO_JUDGE and _rate(s) < _DEMOTE_BELOW


# ── Automatic recall (called by the agent loop, like scan_keywords) ──

def match_skills(text: str) -> list[dict]:
    """Return skills whose trigger patterns match the message. Demoted skills
    (proven unreliable) are skipped. Pure regex — zero LLM cost."""
    if not text:
        return []
    out = []
    for s in load_skills():
        if _is_demoted(s):
            continue
        for pattern in [p.strip() for p in s.get("triggers", "").split(",") if p.strip()]:
            try:
                hit = re.search(pattern, text, re.IGNORECASE)
            except re.error:
                hit = pattern.lower() in text.lower()
            if hit:
                out.append(s)
                break
    return out


def format_for_injection(skills: list[dict]) -> str:
    """Render matched skills as a recall block for the system context."""
    parts = []
    for s in skills:
        rate = int(round(_rate(s) * 100))
        body = s.get("steps", "").strip()
        parts.append(
            f"[skill: {s['name']} · used {s.get('uses', 0)}x · {rate}% success]\n{body}"
        )
    return "\n\n".join(parts)


# ── The tool ─────────────────────────────────────────────────────────

def skill(action: str, name: str = "", steps: str = "", triggers: str = "",
          success: bool = True) -> str:
    action = (action or "").strip().lower()
    skills = load_skills()

    if action == "save":
        if not name or not steps:
            return json.dumps({"error": "save needs name and steps."})
        existing = _find(skills, name)
        if existing:
            existing["steps"] = steps
            if triggers:
                existing["triggers"] = triggers
            existing["updated_at"] = time.time()
            save_skills(skills)
            return json.dumps({"updated": name})
        skills.append({
            "name": name,
            "steps": steps,
            "triggers": triggers,
            "uses": 0,
            "success": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        save_skills(skills)
        return json.dumps({"saved": name, "triggers": triggers})

    if action == "used":
        s = _find(skills, name)
        if not s:
            return json.dumps({"error": f"no skill named '{name}'."})
        s["uses"] = s.get("uses", 0) + 1
        if success:
            s["success"] = s.get("success", 0) + 1
        s["last_used"] = time.time()
        save_skills(skills)
        return json.dumps({"name": name, "uses": s["uses"],
                           "success_rate": round(_rate(s), 2),
                           "demoted": _is_demoted(s)})

    if action == "list":
        return json.dumps({"skills": [
            {"name": s["name"], "triggers": s.get("triggers", ""),
             "uses": s.get("uses", 0), "success_rate": round(_rate(s), 2),
             "demoted": _is_demoted(s)}
            for s in skills]}, ensure_ascii=False)

    if action == "read":
        s = _find(skills, name)
        return json.dumps({"skill": s} if s else {"error": f"no skill '{name}'."},
                          ensure_ascii=False)

    if action == "delete":
        s = _find(skills, name)
        if not s:
            return json.dumps({"error": f"no skill '{name}'."})
        skills.remove(s)
        save_skills(skills)
        return json.dumps({"deleted": name})

    return json.dumps({"error": f"Unknown action '{action}'. "
                       "Use: save, used, list, read, delete."})


registry.register(
    name="skill",
    description=(
        "Learned, reusable PROCEDURES (recipes of steps), distinct from tools. "
        "save: store a procedure (name, steps, triggers=comma-sep regex that "
        "auto-suggest it on matching messages). used: report outcome after "
        "following a skill (name, success=true/false) — this drives the "
        "self-improving loop; skills that keep failing decay and stop being "
        "suggested. list | read | delete. Save a skill after you work out a "
        "non-obvious multi-step procedure that's worth replaying."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string",
                       "enum": ["save", "used", "list", "read", "delete"],
                       "description": "Skill op."},
            "name": {"type": "string", "description": "Skill name."},
            "steps": {"type": "string",
                      "description": "The procedure (save). Prose or numbered steps."},
            "triggers": {"type": "string",
                         "description": "Comma-sep regex that auto-suggest this skill (save)."},
            "success": {"type": "boolean",
                        "description": "used: did following the skill succeed? Default true."},
        },
        "required": ["action"],
    },
    execute=skill,
)
