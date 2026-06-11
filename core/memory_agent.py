"""
Async memory agent — the "librarian" that runs after each conversation turn.

Two jobs:
  1. BEFORE response: pull relevant notes from subscribed streams (injected via system prompt)
  2. AFTER response: review the exchange, decide if anything should be committed to memory
     and update the per-conversation workspace notes pool.

Uses a cheap/fast model to keep costs negligible (~$0.001 per turn).
Runs in a background thread so it never blocks the user.
"""

import json
import queue
import time
import threading
from pathlib import Path
from typing import Callable, Optional
from core.providers import get_client
from core.database import (
    list_note_categories, list_notes_in_category, read_note,
    save_note, search_notes, init_stream_db, vector_search_notes,
)

CONFIG_PATH = Path(__file__).parent.parent / "config.json"
MIN_TURN_CHARS = 80  # Skip trivial exchanges ("ok", "thanks", etc.)
# Cap notes scanned per stream when building the librarian inventory — avoids
# N×read_note() round-trips that hold the GIL and stall the Qt main thread.
_MAX_INVENTORY_NOTES_PER_STREAM = 80

# Single background worker serializes commits. Spawning one thread per turn let
# several LLM+DB-heavy jobs overlap and contend on the GIL / SQLite locks,
# which showed up as multi-second typing freezes when an old commit finished.
_mem_job_queue: queue.Queue = queue.Queue()
_mem_worker_started = False
_mem_worker_lock = threading.Lock()

COMMIT_SYSTEM_PROMPT = """Role: memory librarian. After each turn: (1) update long-term memory notes, (2) update the workspace notes pool.

INPUT:
- exchange: [User] + [Agent] messages
- existing_notes: {stream → {category → [{title, current}]}}  ← "current" is the compiled-truth summary of each note
- workspace_notes: ["...", ...]  (operational pool, always-in-ctx, per-conv)

OUTPUT — JSON object (no markdown, no explanation):
{
  "memory_notes": [...],
  "workspace_notes": [...]
}

MEMORY NOTES rules:
- schema: {stream, category, title, content, keywords}
- ✓ save: user prefs, corrections, personal details, project decisions, learned patterns, key outcomes, user-taught behaviors
- ✗ skip: greetings, acks, task progress, facts re-derivable from code
- update existing (same cat+title) rather than duplicate
- one fact/note
- keywords: comma-sep regex patterns for auto-trigger recall
- [] if nothing durable

NOTE CONTENT STRUCTURE (use for durable facts that will accumulate evidence over time):
    ## Current
    <1–3 sentences: compiled-truth — the current best understanding>

    ## Evidence
    - [YYYY-MM-DD] brief quote or context from this turn supporting the fact

- New note: write both sections. Evidence = one line for this turn.
- Updating existing: rewrite Current if the new evidence meaningfully shifts it; otherwise keep Current as-is. Include ONLY the NEW evidence line(s) for this turn — the system auto-merges with existing Evidence (no need to restate old lines).
- Short facts that won't accrue evidence (e.g. "user's name is X") can skip the structure and use flat content.

WORKSPACE NOTES rules (operational pool):
- 0–12 short entries (≤80 chars each) of ephemeral operational facts for THIS conversation
- ✓ include: active goal, current module/file, counts, dates, intermediate results, process steps
- ✗ shed: completed items, irrelevant details, stale intermediate values
- return FULL updated list each turn (replacement, not diff)
- terse entries: "working on: auth module", "files done: 3/7", "goal: implement workspace notes"
- [] to clear when topic fully shifts"""

RECALL_SYSTEM_PROMPT = """Role: memory librarian. Given user msg + available note inventory, return which notes to load.
Output: JSON array of {stream, category, title}. [] if none relevant.
Rule: selective — only directly relevant notes. Cap: 5.
Output ONLY a JSON array."""

EXPAND_SYSTEM_PROMPT = """Role: query expander for semantic note recall.
Given a user message, output a JSON array of 3 short query variants for vector similarity search.
Variants should cover: synonyms, alt phrasings, related domain terms.
Keep each variant concise (≤12 words).
Output ONLY a JSON array of strings. No explanation."""


def _get_memory_config() -> tuple:
    """Get (provider, model, temperature) for memory operations.

    Resolution order (first non-empty wins):
      1. memory_model / memory_provider — librarian-specific override
      2. summary_model / provider        — same model used for compaction
      3. main model / provider           — last-resort fallback

    Letting memory have its own config means you can run the librarian on a
    cheaper/faster model than your main compactor (which itself is usually
    cheaper than the main agent).
    """
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8")) if CONFIG_PATH.exists() else {}
    except Exception:
        cfg = {}

    summary_temp = cfg.get("summary_temperature", 0.3)

    mem_model = (cfg.get("memory_model") or "").strip()
    mem_provider = (cfg.get("memory_provider") or "").strip()
    if mem_model:
        return (mem_provider or cfg.get("provider", "openrouter")), mem_model, summary_temp

    summary_model = (cfg.get("summary_model") or "").strip()
    main_provider = cfg.get("provider", "openrouter")
    if summary_model:
        return main_provider, summary_model, summary_temp
    return main_provider, cfg.get("model", ""), summary_temp


def _quick_llm(system: str, user: str) -> str | None:
    """Fast LLM call for memory operations. Returns response text or None."""
    provider, model, temperature = _get_memory_config()
    if not model:
        return None
    try:
        client = get_client(provider)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=1024,
            temperature=temperature,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        return None  # Fail silently — memory ops are best-effort


def _parse_json_array(text: str) -> list:
    """Extract a JSON array from LLM output (tolerant of markdown fences)."""
    if not text:
        return []
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        t = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()
    try:
        result = json.loads(t)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        # Try to find array in the text
        start = t.find("[")
        end = t.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except json.JSONDecodeError:
                pass
    return []


def _parse_commit_response(text: str) -> tuple[list, list]:
    """Parse commit LLM response into (memory_notes, workspace_notes).

    Returns ([], []) on parse failure — both lists are empty, not None.
    """
    if not text:
        return [], []
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        t = "\n".join(l for l in lines if not l.strip().startswith("```")).strip()

    # Try object format first: {"memory_notes": [...], "workspace_notes": [...]}
    try:
        obj = json.loads(t)
        if isinstance(obj, dict):
            mem = obj.get("memory_notes", [])
            ws = obj.get("workspace_notes", [])
            if not isinstance(mem, list):
                mem = []
            if not isinstance(ws, list):
                ws = []
            return mem, ws
        if isinstance(obj, list):
            # Old format fallback — pure array of memory notes
            return obj, []
    except json.JSONDecodeError:
        pass

    # Try to find object in text
    start = t.find("{")
    if start >= 0:
        depth = 0
        for i in range(start, len(t)):
            if t[i] == "{":
                depth += 1
            elif t[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[start:i + 1])
                        if isinstance(obj, dict):
                            mem = obj.get("memory_notes", []) or []
                            ws = obj.get("workspace_notes", []) or []
                            return (mem if isinstance(mem, list) else [],
                                    ws if isinstance(ws, list) else [])
                    except json.JSONDecodeError:
                        pass
                    break

    # Fall back to array parse (old format)
    return _parse_json_array(t), []


def _current_section_summary(text: str, limit: int = 300) -> str:
    """Extract the Current section from note text (or SQL preview)."""
    if not text:
        return ""
    lines = text.splitlines()
    evi_idx = next(
        (i for i, ln in enumerate(lines)
         if ln.strip().lower().startswith("## evidence")),
        None,
    )
    current = ("\n".join(lines[:evi_idx]).strip()
               if evi_idx is not None else text.strip())
    if current.lower().startswith("## current"):
        current = current.split("\n", 1)[1].strip() if "\n" in current else ""
    return current[:limit]


def _build_existing_inventory(stream_names: list[str]) -> dict:
    """Compact note inventory for the commit prompt.

    Uses list_notes_in_category previews (one query per category) instead of
    read_note() per title — the old path could open hundreds of SQLite
    connections and parse full note bodies on the GIL, freezing the UI."""
    existing: dict = {}
    for stream in stream_names:
        try:
            cats = list_note_categories(stream)
            stream_info = {"categories": {}}
            note_count = 0
            for cat in cats:
                if note_count >= _MAX_INVENTORY_NOTES_PER_STREAM:
                    break
                titles = list_notes_in_category(stream, cat["category"])
                cat_notes = []
                for t in titles:
                    if note_count >= _MAX_INVENTORY_NOTES_PER_STREAM:
                        break
                    current = _current_section_summary(t.get("preview") or "")
                    cat_notes.append({"title": t["title"], "current": current})
                    note_count += 1
                if cat_notes:
                    stream_info["categories"][cat["category"]] = cat_notes
            existing[stream] = stream_info
        except Exception:
            existing[stream] = {"categories": {}}
    return existing


def _execute_memory_commit(job: dict) -> None:
    user_msg = job["user_msg"]
    agent_reply = job["agent_reply"]
    stream_names = job["stream_names"]
    conv_id = job["conv_id"]
    _ws_notes = job["workspace_notes"]
    on_workspace_update = job.get("on_workspace_update")

    existing = _build_existing_inventory(stream_names)

    prompt = (
        f"existing_notes:\n{json.dumps(existing, indent=2)}\n\n"
        f"workspace_notes:\n{json.dumps(_ws_notes)}\n\n"
        f"exchange:\n"
        f"[User]: {user_msg[:1500]}\n\n"
        f"[Agent]: {agent_reply[:1500]}\n\n"
        f"Output JSON object with memory_notes and workspace_notes."
    )

    raw = _quick_llm(COMMIT_SYSTEM_PROMPT, prompt)
    mem_notes, new_ws_notes = _parse_commit_response(raw)

    saved_titles = []
    for note in mem_notes:
        stream = note.get("stream", "")
        category = note.get("category", "")
        title = note.get("title", "")
        content = note.get("content", "")
        kw = note.get("keywords", "")
        if not all([stream, category, title, content]):
            continue
        if stream not in stream_names:
            continue

        # Check for near-duplicate before saving
        existing_note = read_note(stream, category, title)
        if existing_note and existing_note["content"].strip() == content.strip():
            continue

        # Auto-committed notes are model-extracted from the conversation,
        # so they're 'inferred' (not user-confirmed fact) until verified.
        save_note(stream, category, title, content, keywords=kw,
                  source_conv=conv_id, provenance="inferred")
        saved_titles.append(title)

    if saved_titles:
        titles_str = ", ".join(saved_titles)
        print(f"[MemoryAgent] Committed {len(saved_titles)} note(s): {titles_str}")

    # Sanitize workspace notes: keep only non-empty strings, cap at 15
    clean_ws = [str(n).strip() for n in new_ws_notes if str(n).strip()][:15]
    if clean_ws == _ws_notes:
        return

    if on_workspace_update is not None:
        try:
            on_workspace_update(clean_ws)
            print(f"[MemoryAgent] Workspace notes updated: {len(clean_ws)} entries")
        except Exception as e:
            print(f"[MemoryAgent] workspace update callback error: {e}")

    if conv_id:
        try:
            from core.database import set_workspace_notes
            set_workspace_notes(conv_id, clean_ws)
        except Exception as e:
            print(f"[MemoryAgent] Failed to persist workspace notes: {e}")


def _memory_commit_worker() -> None:
    while True:
        job = _mem_job_queue.get()
        try:
            _execute_memory_commit(job)
        except Exception as e:
            print(f"[MemoryAgent] commit error: {e}")
        finally:
            _mem_job_queue.task_done()


def _ensure_memory_worker() -> None:
    global _mem_worker_started
    with _mem_worker_lock:
        if _mem_worker_started:
            return
        _mem_worker_started = True
        threading.Thread(
            target=_memory_commit_worker, daemon=True, name="memory-agent",
        ).start()


def commit_to_memory(user_msg: str, agent_reply: str, stream_names: list[str],
                     conv_id: str = "",
                     workspace_notes: Optional[list[str]] = None,
                     on_workspace_update: Optional[Callable[[list[str]], None]] = None):
    """Review a turn and commit worthy facts to memory. Runs async.

    workspace_notes: current operational pool (read-only input to LLM).
    on_workspace_update: called with new list[str] when LLM returns updated pool.
    """
    if not user_msg or not agent_reply:
        return
    # Skip trivial exchanges
    if len(user_msg) + len(agent_reply) < MIN_TURN_CHARS:
        return

    _ensure_memory_worker()
    _mem_job_queue.put({
        "user_msg": user_msg,
        "agent_reply": agent_reply,
        "stream_names": list(stream_names),
        "conv_id": conv_id,
        "workspace_notes": list(workspace_notes or []),
        "on_workspace_update": on_workspace_update,
    })


def _expand_queries(user_msg: str) -> list[str]:
    """Expand user message into semantic query variants for vector recall.
    Always includes the original. Returns list of 1-4 strings.
    """
    queries = [user_msg[:300]]
    raw = _quick_llm(EXPAND_SYSTEM_PROMPT, f"user_msg:\n{user_msg[:500]}")
    variants = _parse_json_array(raw)
    for v in variants[:3]:
        if isinstance(v, str):
            v = v.strip()
            if v and v not in queries:
                queries.append(v[:200])
    return queries


def recall_for_query(user_msg: str, stream_names: list[str]) -> list[dict]:
    """Pull relevant notes before the agent responds.

    Pipeline:
      1. Multi-query expansion: rewrite user_msg into 3 semantic variants (1 cheap LLM call)
      2. Vector recall: RRF-fuse across variants using entries_vec per stream
      3. LLM picker fallback: when vector search is unavailable or returns nothing,
         fall back to the original inventory-based picker.
    """
    if not user_msg or len(user_msg) < 20:
        return []

    # Build available notes index (always needed for fallback path)
    available = {}
    for stream in stream_names:
        try:
            cats = list_note_categories(stream)
            if cats:
                stream_info = {}
                for cat in cats:
                    titles = list_notes_in_category(stream, cat["category"])
                    stream_info[cat["category"]] = [t["title"] for t in titles]
                available[stream] = stream_info
        except Exception:
            pass

    if not available:
        return []  # No notes exist yet

    # ── Path A: vector recall with multi-query expansion ──
    try:
        queries = _expand_queries(user_msg)
        vec_hits = vector_search_notes(stream_names, queries, limit_per_stream=8)
    except Exception as e:
        print(f"[MemoryAgent] vector recall error: {e}")
        vec_hits = []

    if vec_hits:
        recalled = []
        for h in vec_hits[:5]:
            _n = read_note(h["stream"], h["category"], h["title"])
            recalled.append({
                "stream": h["stream"],
                "category": h["category"],
                "title": h["title"],
                "content": h["content"],
                "provenance": (_n or {}).get("provenance", "unverified"),
            })
        print(f"[MemoryAgent] Recalled {len(recalled)} note(s) via vector search "
              f"({len(queries)} query variants)")
        return recalled

    # ── Path B: LLM picker fallback (vector infra missing or no matches) ──
    prompt = (
        f"available_notes:\n{json.dumps(available, indent=2)}\n\n"
        f"user_msg:\n{user_msg[:1000]}\n\n"
        f"Which notes to load? Output JSON array."
    )
    raw = _quick_llm(RECALL_SYSTEM_PROMPT, prompt)
    requests = _parse_json_array(raw)

    recalled = []
    for req in requests[:5]:
        stream = req.get("stream", "")
        category = req.get("category", "")
        title = req.get("title", "")
        if not all([stream, category, title]):
            continue
        note = read_note(stream, category, title)
        if note:
            recalled.append({
                "stream": stream,
                "category": category,
                "title": title,
                "content": note["content"],
                "provenance": note.get("provenance", "unverified"),
            })

    if recalled:
        print(f"[MemoryAgent] Recalled {len(recalled)} note(s) via LLM picker (fallback)")

    return recalled
