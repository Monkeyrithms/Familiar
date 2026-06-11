"""
Memory tool — lets the agent manually read, write, search, and manage
notes in its subscribed memory streams.

The async memory agent handles automatic background commits, but this
tool gives the agent direct control when it needs to explicitly save
something, look up a fact, or organize its notes.
"""

import json
import time
from tools.registry import registry
from core.database import (
    list_note_categories, list_notes_in_category, read_note,
    save_note, delete_note, search_notes,
    rename_category, move_category, rename_note, move_note,
)


def _get_streams() -> list[str]:
    """Get current conversation's readable streams from the active agent."""
    try:
        from core.agent import current_agent
        agent = current_agent()
        if agent is not None:
            return agent.get_readable_streams()
    except Exception:
        pass
    return ["General"]


WRITE_ACTIONS = {"save", "delete", "rename_category", "move_category", "rename_note", "move_note"}


def _get_writable_streams() -> list[str]:
    """Get streams this conversation can write to from the active agent."""
    try:
        from core.agent import current_agent
        agent = current_agent()
        if agent is not None:
            return agent.get_writable_streams()
    except Exception:
        pass
    return _get_streams()


def memory(action: str, stream: str = "", category: str = "",
           title: str = "", content: str = "", keywords: str = "",
           query: str = "", provenance: str = "") -> str:
    """Manage memory notes across subscribed streams."""

    streams = _get_streams()

    # Default to first subscribed stream
    if not stream and streams:
        stream = streams[0]

    if stream not in streams:
        return json.dumps({
            "error": f"Stream '{stream}' not subscribed. Available: {streams}"
        })

    if action == "browse":
        # List categories, or notes within a category
        if category:
            notes = list_notes_in_category(stream, category)
            return json.dumps({
                "stream": stream, "category": category,
                "notes": notes, "count": len(notes),
            }, ensure_ascii=False)
        else:
            cats = list_note_categories(stream)
            return json.dumps({
                "stream": stream, "categories": cats,
                "count": len(cats),
            }, ensure_ascii=False)

    elif action == "read":
        if not category or not title:
            return json.dumps({"error": "category and title required for read"})
        note = read_note(stream, category, title)
        if not note:
            return json.dumps({"error": f"Note not found: {category}/{title}"})
        return json.dumps({
            "stream": stream, "category": note["category"],
            "title": note["title"], "content": note["content"],
            "provenance": note.get("provenance", "unverified"),
        }, ensure_ascii=False)

    elif action == "save":
        if not category or not title or not content:
            return json.dumps({"error": "category, title, and content required for save"})
        prov = (provenance or "").strip().lower()
        if prov and prov not in ("observed", "confirmed", "inferred", "imported", "unverified"):
            return json.dumps({"error": f"provenance must be one of: observed, confirmed, "
                               f"inferred, imported, unverified (got '{provenance}')"})
        result = save_note(stream, category, title, content,
                           keywords=keywords, provenance=prov)
        return json.dumps(result, ensure_ascii=False)

    elif action == "delete":
        if not category or not title:
            return json.dumps({"error": "category and title required for delete"})
        ok = delete_note(stream, category, title)
        return json.dumps({"deleted": ok, "category": category, "title": title})

    elif action == "search":
        if not query:
            return json.dumps({"error": "query required for search"})
        results = search_notes(stream, query)
        return json.dumps({
            "stream": stream, "query": query,
            "results": results, "count": len(results),
        }, ensure_ascii=False)

    elif action == "rename_category":
        if not category or not query:
            return json.dumps({"error": "category (old path) and query (new path) required"})
        count = rename_category(stream, category, query)
        return json.dumps({
            "renamed": True, "from": category, "to": query,
            "notes_affected": count,
        })

    elif action == "move_category":
        if not category or query is None:
            return json.dumps({"error": "category (source) and query (dest parent, or '' for root) required"})
        count = move_category(stream, category, query)
        return json.dumps({
            "moved": True, "source": category, "dest_parent": query or "(root)",
            "notes_affected": count,
        })

    elif action == "rename_note":
        if not category or not title or not query:
            return json.dumps({"error": "category, title (old), and query (new title) required"})
        ok = rename_note(stream, category, title, query)
        return json.dumps({"renamed": ok, "category": category,
                           "old_title": title, "new_title": query})

    elif action == "move_note":
        if not category or not title or not query:
            return json.dumps({"error": "category (old), title, and query (new category) required"})
        ok = move_note(stream, category, title, query)
        return json.dumps({"moved": ok, "title": title,
                           "from": category, "to": query})

    elif action == "compress":
        """Compress memory: agent-driven trimming of notes to a target size."""
        from core.database import list_note_categories
        if not category:
            # Compress entire stream
            cats = list_note_categories(stream)
            total_notes = sum(len(list_notes_in_category(stream, cat["category"])) for cat in cats)
            target_reduction = max(10, int(total_notes * 0.3))  # Remove ~30% or min 10 notes
            return json.dumps({
                "error": f"Compress entire stream not yet supported. "
                         f"Specify a category to compress ({total_notes} total notes, "
                         f"recommend removing ~{target_reduction})"
            })
        else:
            # Compress single category
            notes = list_notes_in_category(stream, category)
            if len(notes) < 2:
                return json.dumps({
                    "compressed": False, "reason": "category too small",
                    "count": len(notes)
                })
            
            # Read all notes and show which are candidates for removal
            note_summaries = []
            for note_info in notes:
                note = read_note(stream, category, note_info["title"])
                if note:
                    # Extract current section (before ## Evidence header)
                    content = note["content"]
                    lines = content.splitlines()
                    evi_idx = next(
                        (i for i, ln in enumerate(lines)
                         if ln.strip().lower().startswith("## evidence")),
                        None
                    )
                    curr = "\n".join(lines[:evi_idx]).strip() if evi_idx else content.strip()
                    note_summaries.append({
                        "title": note["title"],
                        "confidence": note.get("confidence", 0.8),
                        "importance": note.get("importance", "medium"),
                        "age_days": (time.time() - note.get("updated_at", time.time())) / 86400,
                        "chars": len(curr),
                    })
            
            return json.dumps({
                "stream": stream, "category": category,
                "total_notes": len(notes),
                "candidates_for_removal": note_summaries,
                "guidance": "Review candidates and call memory action=delete for notes to remove. "
                            "Prioritize low confidence/importance, or oldest notes."
            }, ensure_ascii=False)

    else:
        return json.dumps({
            "error": f"Unknown action: {action}. "
                     "Use: browse, read, save, delete, search, compress, "
                     "rename_category, move_category, rename_note, move_note"
        })


registry.register(
    name="memory",
    description=(
        "Persistent memory notes. "
        "browse|read|save|delete|search|rename_category|move_category|rename_note|move_note. "
        "save: keywords (comma-sep regex) auto-inject note on msg match. "
        "save: provenance = origin/trust of the fact — 'confirmed' (user stated it), "
        "'observed' (read from a source/file), 'imported' (from a transcript), "
        "'inferred' (you deduced it). Recalled notes show this tag so trust is visible."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["browse", "read", "save", "delete", "search",
                         "compress", "rename_category", "move_category", "rename_note", "move_note"],
                "description": "Memory op.",
            },
            "stream": {"type": "string", "description": "Stream name (default: first subscribed)."},
            "category": {"type": "string", "description": "Category path."},
            "title": {"type": "string", "description": "Note title."},
            "content": {"type": "string", "description": "Note content (save; max 2000 chars)."},
            "keywords": {
                "type": "string",
                "description": "Comma-sep regex patterns for auto-trigger recall (save).",
            },
            "query": {"type": "string", "description": "Search query (search)."},
            "provenance": {
                "type": "string",
                "enum": ["confirmed", "observed", "imported", "inferred", "unverified"],
                "description": "Origin/trust of the fact (save). confirmed=user said it; "
                "observed=read from a source; imported=from a transcript; inferred=you deduced it.",
            },
        },
        "required": ["action"],
    },
    execute=memory,
)
