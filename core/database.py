"""
Database layer — SQLite storage for conversations and memory streams.

Two database types:
  1. conversations.db  — all conversations, messages, FTS5 + vector search
  2. streams/{name}.db — per-stream rolling summaries and memory entries + vectors

Uses sqlite-vec for vector similarity search (cosine distance) alongside
FTS5 keyword search. Falls back to FTS5-only if sqlite-vec unavailable.
"""

import json
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONVERSATIONS_DB = DATA_DIR / "conversations.db"
STREAMS_DIR = DATA_DIR / "streams"
STREAMS_DIR.mkdir(parents=True, exist_ok=True)

# Embedding dimensions (text-embedding-3-small)
from core.embeddings import EMBED_DIMS, load_sqlite_vec, vec_to_bytes, embed_text
from core.workspace_paths import sanitize_agent_paths

# Weight blend for hybrid search: total = (w_fts * fts_score) + (w_vec * vec_score)
W_FTS = 0.4
W_VEC = 0.6

_vec_available = None  # Cached check


def _has_vec() -> bool:
    """Check if sqlite-vec extension is loadable."""
    global _vec_available
    if _vec_available is None:
        try:
            conn = sqlite3.connect(":memory:")
            _vec_available = load_sqlite_vec(conn)
            conn.close()
        except Exception:
            _vec_available = False
    return _vec_available


# ── Conversations DB ────────────────────────────────────────────────

class _PooledConnection(sqlite3.Connection):
    """Reused per-thread connection. ``close()`` is a no-op for the handle but
    rolls back any uncommitted transaction — matching the old per-call behavior
    where closing a connection released locks and discarded uncommitted writes —
    so the ~30 call sites that do ``conn.close()`` keep working unchanged."""

    def close(self):  # noqa: A003 — intentionally shadow
        try:
            if self.in_transaction:
                self.rollback()
        except Exception:
            pass


_conn_local = threading.local()
# Serialize writes — multiple background threads (conv saver, draft saver,
# debug recorder, embed worker) otherwise hit SQLITE_BUSY and spam the console.
_conv_write_lock = threading.Lock()


def _conv_conn() -> sqlite3.Connection:
    """Thread-local pooled connection. Opening a fresh connection per call —
    plus reloading the sqlite-vec C extension each time — stutters the UI when
    many small reads/writes run per turn, so we keep one live handle per thread.
    Each thread gets its own connection (no cross-thread sharing)."""
    conn = getattr(_conn_local, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")  # liveness probe — recreate if stale
            return conn
        except sqlite3.Error:
            try:
                sqlite3.Connection.close(conn)
            except Exception:
                pass
            conn = None
    conn = sqlite3.connect(str(CONVERSATIONS_DB), timeout=5,
                           factory=_PooledConnection)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    if _has_vec():
        load_sqlite_vec(conn)
    _conn_local.conn = conn
    return conn


def init_conversations_db():
    """Create conversation tables if they don't exist."""
    conn = _conv_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS conversations (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            workspace   TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            provider    TEXT NOT NULL DEFAULT '',
            system_prompt TEXT NOT NULL DEFAULT '',
            streams_json TEXT NOT NULL DEFAULT '[]',
            created_at  REAL NOT NULL,
            modified_at REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            position        INTEGER NOT NULL,
            role            TEXT NOT NULL,
            content         TEXT NOT NULL DEFAULT '',
            tool_calls_json TEXT,
            tool_call_id    TEXT,
            tool_names_json TEXT,
            command         TEXT,
            UNIQUE(conversation_id, position)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_conv
            ON messages(conversation_id, position);

        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            content,
            content=messages,
            content_rowid=id
        );

        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES('delete', old.id, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, content)
                VALUES('delete', old.id, old.content);
            INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
        END;
    """)
    # Migrate: add columns if missing (for existing DBs)
    for col, typ in [("tool_names_json", "TEXT"), ("command", "TEXT"), ("metadata_json", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {col} {typ}")
        except Exception:
            pass
    # Conversation-level settings migration
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN include_timestamps INTEGER NOT NULL DEFAULT 1")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE conversations ADD COLUMN conversation_cwd TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    try:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN debug_turns_json TEXT NOT NULL DEFAULT '[]'"
        )
    except Exception:
        pass
    try:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN composer_draft TEXT NOT NULL DEFAULT ''"
        )
    except Exception:
        pass
    # prompt_replace: when 1, the conversation prompt REPLACES the base system
    # prompt instead of layering on top of it (per-conversation total control).
    try:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN prompt_replace INTEGER NOT NULL DEFAULT 0"
        )
    except Exception:
        pass
    # context_note: per-conversation "author's note" injected as the LAST system
    # message every turn (after the conversation) for heavy recency weight.
    try:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN context_note TEXT NOT NULL DEFAULT ''"
        )
    except Exception:
        pass
    # reflect_json: persisted conversation-scoped self-review rule
    # ({when, scope, criteria}); empty when no standing rule.
    try:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN reflect_json TEXT NOT NULL DEFAULT ''"
        )
    except Exception:
        pass
    # stream_live: per-conversation live token streaming (1=stream, 0=only-final).
    # Default 1 = current behavior.
    try:
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN stream_live INTEGER NOT NULL DEFAULT 1"
        )
    except Exception:
        pass

    # Chat images — persistent BLOB storage for images attached to messages
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_images (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            position        INTEGER NOT NULL,
            image_data      BLOB NOT NULL,
            mime_type       TEXT NOT NULL DEFAULT 'image/png',
            source          TEXT NOT NULL DEFAULT '',
            UNIQUE(conversation_id, position)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_chat_images_conv
            ON chat_images(conversation_id, position)
    """)

    # Vector table (requires sqlite-vec)
    if _has_vec():
        try:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS messages_vec
                USING vec0(embedding float[{EMBED_DIMS}])
            """)
        except Exception as e:
            print(f"[DB] Could not create messages_vec: {e}")
    conn.close()


# ── Conversation CRUD ───────────────────────────────────────────────

_conv_list_cache: list[dict] | None = None


def invalidate_conversation_list_cache() -> None:
    """Drop cached list_conversations() result after any conversation mutation."""
    global _conv_list_cache
    _conv_list_cache = None


def list_conversations() -> list[dict]:
    """List all conversations, sorted by most recent."""
    global _conv_list_cache
    if _conv_list_cache is not None:
        return _conv_list_cache
    conn = _conv_conn()
    rows = conn.execute("""
        SELECT c.id, c.name, c.modified_at, c.workspace, c.streams_json,
               COUNT(m.id) as message_count
        FROM conversations c
        LEFT JOIN messages m ON m.conversation_id = c.id
        GROUP BY c.id
        ORDER BY c.modified_at DESC
    """).fetchall()
    conn.close()
    _conv_list_cache = [{
        "id": r["id"],
        "name": r["name"],
        "modified": r["modified_at"],
        "workspace": r["workspace"],
        "streams": json.loads(r["streams_json"]),
        "message_count": r["message_count"],
    } for r in rows]
    return _conv_list_cache


def get_conversation_streams(conv_id: str) -> list[str]:
    """Lightweight streams fetch — avoids loading every message for a menu."""
    if not conv_id:
        return []
    conn = _conv_conn()
    row = conn.execute(
        "SELECT streams_json FROM conversations WHERE id=?", (conv_id,)
    ).fetchone()
    conn.close()
    if not row:
        return []
    try:
        return json.loads(row["streams_json"])
    except Exception:
        return []


def _chat_image_thumbnail_bytes(img_path: str) -> bytes | None:
    """Resize an attached image for chat_images storage (outside write lock)."""
    try:
        from PIL import Image as _Img
        import io as _io
        _Img.MAX_IMAGE_PIXELS = None
        _im = _Img.open(img_path)
        _long = max(_im.size)
        if _long > 800:
            _s = 800 / _long
            _im = _im.resize((int(_im.size[0] * _s), int(_im.size[1] * _s)), _Img.LANCZOS)
        if _im.mode not in ("RGB",):
            _im = _im.convert("RGB")
        _buf = _io.BytesIO()
        _im.save(_buf, format="JPEG", quality=85)
        return _buf.getvalue()
    except Exception:
        return None


def save_conversation(conv_id: str, name: str, messages: list[dict],
                      workspace: str = "", model: str = "",
                      system_prompt: str = "", streams: list[str] = None,
                      include_timestamps: bool = None, provider: str | None = None,
                      prompt_replace: bool = None, context_note: str | None = None):
    """Save or update a conversation and all its messages."""
    image_paths: list[tuple[int, str]] = []
    embed_queue: list[tuple] = []

    with _conv_write_lock:
        conn = _conv_conn()
        now = time.time()

        existing = conn.execute(
            "SELECT id, streams_json FROM conversations WHERE id=?", (conv_id,)
        ).fetchone()

        if streams is None:
            if existing:
                streams = json.loads(existing["streams_json"])
            else:
                streams = _default_streams()

        streams_json = json.dumps(streams)

        if existing:
            # Preserve fields not passed
            if not workspace:
                workspace = conn.execute(
                    "SELECT workspace FROM conversations WHERE id=?", (conv_id,)
                ).fetchone()["workspace"]
            if not model:
                model = conn.execute(
                    "SELECT model FROM conversations WHERE id=?", (conv_id,)
                ).fetchone()["model"]
            if provider is None:
                provider = conn.execute(
                    "SELECT provider FROM conversations WHERE id=?", (conv_id,)
                ).fetchone()["provider"]

            # Preserve include_timestamps if not explicitly passed
            if include_timestamps is None:
                row_ts = conn.execute(
                    "SELECT include_timestamps FROM conversations WHERE id=?", (conv_id,)
                ).fetchone()
                include_timestamps = bool(row_ts["include_timestamps"]) if row_ts else True
            # Preserve prompt_replace if not explicitly passed
            if prompt_replace is None:
                row_pr = conn.execute(
                    "SELECT prompt_replace FROM conversations WHERE id=?", (conv_id,)
                ).fetchone()
                prompt_replace = (bool(row_pr["prompt_replace"])
                                  if (row_pr and "prompt_replace" in row_pr.keys()) else False)
            # Preserve context_note if not explicitly passed
            if context_note is None:
                row_cn = conn.execute(
                    "SELECT context_note FROM conversations WHERE id=?", (conv_id,)
                ).fetchone()
                context_note = (row_cn["context_note"]
                                if (row_cn and "context_note" in row_cn.keys()) else "") or ""

            conn.execute("""
                UPDATE conversations SET name=?, workspace=?, model=?, provider=?,
                       system_prompt=?, streams_json=?, include_timestamps=?,
                       prompt_replace=?, context_note=?, modified_at=?
                WHERE id=?
            """, (name, workspace, model, provider or "", system_prompt, streams_json,
                  1 if include_timestamps else 0, 1 if prompt_replace else 0,
                  context_note, now, conv_id))
        else:
            if include_timestamps is None:
                include_timestamps = True
            if prompt_replace is None:
                prompt_replace = False
            if context_note is None:
                context_note = ""
            conn.execute("""
                INSERT INTO conversations (id, name, workspace, model, provider, system_prompt,
                                           streams_json, include_timestamps, prompt_replace,
                                           context_note, created_at, modified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (conv_id, name, workspace, model, provider or "", system_prompt, streams_json,
                  1 if include_timestamps else 0, 1 if prompt_replace else 0,
                  context_note, now, now))

        # Count existing messages to only embed NEW ones
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conversation_id=?", (conv_id,)
        ).fetchone()[0]

        # Replace all messages — batch clear old vectors
        if _has_vec():
            try:
                conn.execute("""
                    DELETE FROM messages_vec WHERE rowid IN (
                        SELECT id FROM messages WHERE conversation_id=?
                    )
                """, (conv_id,))
            except Exception:
                pass
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))

        # Build stream-aware embedding prefix from stream descriptions
        embed_prefix = _build_embed_prefix(streams)

        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            content = msg.get("content", "")
            # Serialize plan data into content for persistence
            if role == "plan_card" and msg.get("_plan_data"):
                content = json.dumps(msg["_plan_data"], ensure_ascii=False)
            if isinstance(content, list):
                content = json.dumps(content, ensure_ascii=False)
            tool_calls = json.dumps(msg["tool_calls"], ensure_ascii=False) if msg.get("tool_calls") else None
            tool_call_id = msg.get("tool_call_id")
            tool_names = json.dumps(msg.get("tool_names", [])) if msg.get("tool_names") else None
            command = msg.get("command")
            # Pack extra metadata (timestamp, usage, etc.)
            extra = {}
            if msg.get("_timestamp"):
                extra["timestamp"] = msg["_timestamp"]
            if msg.get("_usage"):
                extra["usage"] = msg["_usage"]
            if msg.get("_checkpoint_hash"):
                extra["checkpoint_hash"] = msg["_checkpoint_hash"]
                extra["checkpoint_dir"] = msg.get("_checkpoint_dir", "")
            if msg.get("_thumb"):
                extra["thumb"] = msg["_thumb"]
            if msg.get("_summary_snapshot"):
                extra["summary_snapshot"] = msg["_summary_snapshot"]
            tl = msg.get("_stream_timeline")
            if isinstance(tl, list) and tl:
                extra["stream_timeline"] = tl
            # Diff cards: persist the precomputed diff so they reload with content
            # instead of empty +0/-0 shells.
            if role == "diff_card":
                extra["diff_path"] = msg.get("_diff_path", "")
                extra["diff_rows"] = msg.get("_diff_rows", [])
                extra["diff_adds"] = msg.get("_diff_adds", 0)
                extra["diff_dels"] = msg.get("_diff_dels", 0)
            metadata_json = json.dumps(extra) if extra else None
            cur = conn.execute("""
                INSERT INTO messages (conversation_id, position, role, content,
                                      tool_calls_json, tool_call_id, tool_names_json, command, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (conv_id, i, role, content, tool_calls, tool_call_id, tool_names, command, metadata_json))
            # Only embed messages that are NEW (beyond what was previously saved)
            is_new = i >= existing_count
            if is_new and role in ("user", "assistant") and content and len(content) > 20 and not tool_calls:
                prefixed = f"{embed_prefix}{content[:2000]}" if embed_prefix else content[:2000]
                embed_queue.append((cur.lastrowid, prefixed))

            # Thumbnail work happens after commit so we don't hold the write lock.
            if not msg.get("_thumb"):
                img_path = msg.get("image_path", "")
                if img_path and Path(img_path).is_file():
                    image_paths.append((i, img_path))

        conn.commit()
        conn.close()

    image_jobs: list[tuple[int, bytes]] = []
    for position, img_path in image_paths:
        img_bytes = _chat_image_thumbnail_bytes(img_path)
        if img_bytes:
            image_jobs.append((position, img_bytes))

    if image_jobs:
        with _conv_write_lock:
            conn = _conv_conn()
            for position, img_bytes in image_jobs:
                conn.execute("""
                    INSERT OR REPLACE INTO chat_images
                        (conversation_id, position, image_data, mime_type, source)
                    VALUES (?, ?, ?, ?, ?)
                """, (conv_id, position, img_bytes, "image/jpeg", "attached"))
            conn.commit()
            conn.close()

    # Generate embeddings in background (non-blocking)
    if embed_queue and _has_vec():
        import threading
        threading.Thread(target=_embed_messages, args=(embed_queue,), daemon=True).start()
    invalidate_conversation_list_cache()


def append_message_to_conversation(conv_id: str, role: str, content: str,
                                    meta: dict | None = None) -> bool:
    """Append a single message to an existing conversation without rewriting all messages.

    Used by the tool-audit system to inject audit prompts into a target conversation.
    Updates modified_at so the conversation sorts to the top of the list.
    Returns True on success, False on error.
    """
    try:
        with _conv_write_lock:
            conn = _conv_conn()
            row = conn.execute("SELECT id FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if not row:
                conn.close()
                print(f"[DB] append_message: conv {conv_id} not found")
                return False
            max_pos = conn.execute(
                "SELECT COALESCE(MAX(position), -1) FROM messages WHERE conversation_id=?",
                (conv_id,)
            ).fetchone()[0]
            new_pos = max_pos + 1

            metadata_json = None
            if meta:
                extra = dict(meta)
                extra["timestamp"] = time.time()
                metadata_json = json.dumps(extra, default=str)

            conn.execute("""
                INSERT INTO messages (conversation_id, position, role, content, metadata_json)
                VALUES (?, ?, ?, ?, ?)
            """, (conv_id, new_pos, role, content, metadata_json))
            conn.execute(
                "UPDATE conversations SET modified_at=? WHERE id=?",
                (time.time(), conv_id)
            )
            conn.commit()
            conn.close()
        invalidate_conversation_list_cache()
        return True
    except Exception as e:
        print(f"[DB] append_message error: {e}")
        return False


def load_conversation(conv_id: str) -> dict | None:
    """Load a conversation with all messages."""
    conn = _conv_conn()
    row = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
    if not row:
        conn.close()
        return None

    msg_rows = conn.execute("""
        SELECT position, role, content, tool_calls_json, tool_call_id,
               tool_names_json, command, metadata_json
        FROM messages WHERE conversation_id=? ORDER BY position
    """, (conv_id,)).fetchall()

    # Fetch which positions have images stored
    image_positions = set()
    try:
        img_rows = conn.execute(
            "SELECT position FROM chat_images WHERE conversation_id=?", (conv_id,)
        ).fetchall()
        image_positions = {r["position"] for r in img_rows}
    except Exception:
        pass
    conn.close()

    messages = []
    for mr in msg_rows:
        msg = {"role": mr["role"]}
        content = mr["content"]
        if content.startswith("["):
            try:
                msg["content"] = json.loads(content)
            except (json.JSONDecodeError, ValueError):
                msg["content"] = content
        else:
            msg["content"] = content
        if mr["tool_calls_json"]:
            msg["tool_calls"] = json.loads(mr["tool_calls_json"])
        if mr["tool_call_id"]:
            msg["tool_call_id"] = mr["tool_call_id"]
        if mr["tool_names_json"]:
            msg["tool_names"] = json.loads(mr["tool_names_json"])
        if mr["command"]:
            msg["command"] = mr["command"]
        # Restore plan data from serialized content
        if msg["role"] == "plan_card" and isinstance(msg["content"], str) and msg["content"].startswith("{"):
            try:
                msg["_plan_data"] = json.loads(msg["content"])
            except (json.JSONDecodeError, ValueError):
                msg["_plan_data"] = {}
        # Restore metadata (timestamp, usage, checkpoint)
        meta_raw = mr["metadata_json"] if "metadata_json" in mr.keys() else None
        if meta_raw:
            try:
                extra = json.loads(meta_raw)
                if "timestamp" in extra:
                    msg["_timestamp"] = extra["timestamp"]
                if "usage" in extra:
                    msg["_usage"] = extra["usage"]
                if "checkpoint_hash" in extra:
                    msg["_checkpoint_hash"] = extra["checkpoint_hash"]
                    msg["_checkpoint_dir"] = extra.get("checkpoint_dir", "")
                if "thumb" in extra:
                    msg["_thumb"] = extra["thumb"]
                if "summary_snapshot" in extra:
                    msg["_summary_snapshot"] = extra["summary_snapshot"]
                if "stream_timeline" in extra:
                    msg["_stream_timeline"] = extra["stream_timeline"]
                if "diff_rows" in extra:
                    msg["_diff_rows"] = extra["diff_rows"]
                    msg["_diff_path"] = extra.get("diff_path", "")
                    msg["_diff_adds"] = extra.get("diff_adds", 0)
                    msg["_diff_dels"] = extra.get("diff_dels", 0)
            except (json.JSONDecodeError, ValueError):
                pass
        # Restore image path from DB cache
        pos = mr["position"]
        if pos in image_positions:
            cached = get_chat_image_path(conv_id, pos)
            if cached:
                msg["image_path"] = cached
        messages.append(msg)

    try:
        inc_ts = bool(row["include_timestamps"])
    except (KeyError, IndexError):
        inc_ts = True
    try:
        conv_cwd = row["conversation_cwd"] or ""
    except (KeyError, IndexError):
        conv_cwd = ""
    try:
        composer_draft = row["composer_draft"] or ""
    except (KeyError, IndexError):
        composer_draft = ""
    try:
        reflect_raw = row["reflect_json"] if "reflect_json" in row.keys() else ""
        reflect = json.loads(reflect_raw) if reflect_raw else {}
    except (KeyError, IndexError, json.JSONDecodeError, ValueError):
        reflect = {}
    try:
        stream_live = (bool(row["stream_live"]) if "stream_live" in row.keys() else True)
    except (KeyError, IndexError):
        stream_live = True

    return {
        "name": row["name"],
        "messages": messages,
        "workspace": row["workspace"],
        "model": row["model"],
        "provider": row["provider"],
        "system_prompt": row["system_prompt"],
        "streams": json.loads(row["streams_json"]),
        "include_timestamps": inc_ts,
        "prompt_replace": (bool(row["prompt_replace"])
                           if "prompt_replace" in row.keys() else False),
        "context_note": (row["context_note"]
                         if "context_note" in row.keys() else "") or "",
        "conversation_cwd": conv_cwd,
        "composer_draft": composer_draft,
        "reflect": reflect,
        "stream_live": stream_live,
    }


def get_conversation_meta(conv_id: str) -> dict | None:
    """Lightweight metadata fetch: the conversations row only, no messages.

    Use when you need name/model/provider without paying to load and JSON-parse
    every message (e.g. the auto-save name lookup that runs on the UI thread
    every 10s and after each response)."""
    conn = _conv_conn()
    row = conn.execute(
        "SELECT name, workspace, model, provider, system_prompt "
        "FROM conversations WHERE id=?", (conv_id,)).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "name": row["name"],
        "workspace": row["workspace"],
        "model": row["model"],
        "provider": row["provider"],
        "system_prompt": row["system_prompt"],
    }


def delete_conversation(conv_id: str):
    """Delete a conversation and its messages (CASCADE)."""
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        conn.commit()
        conn.close()
    invalidate_conversation_list_cache()


def get_conversation_debug_turns(conv_id: str) -> list:
    """Load persisted LLM debug turns (full-context snapshots) for a conversation."""
    if not conv_id:
        return []
    conn = _conv_conn()
    row = conn.execute(
        "SELECT debug_turns_json FROM conversations WHERE id=?", (conv_id,)
    ).fetchone()
    conn.close()
    if not row:
        return []
    try:
        raw = row["debug_turns_json"]
    except (KeyError, IndexError):
        raw = None
    if raw is None or raw == "":
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, TypeError, ValueError):
        return []


def set_conversation_debug_turns(conv_id: str, turns: list) -> None:
    """Persist debug turns JSON for *conv_id* (replaces prior value)."""
    if not conv_id:
        return
    payload = json.dumps(turns or [], ensure_ascii=False, default=str)
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute(
            "UPDATE conversations SET debug_turns_json=? WHERE id=?",
            (payload, conv_id),
        )
        conn.commit()
        conn.close()


def get_conversation_composer_draft(conv_id: str) -> str:
    """Return the saved chat input draft for *conv_id*, or empty string."""
    if not conv_id:
        return ""
    conn = _conv_conn()
    row = conn.execute(
        "SELECT composer_draft FROM conversations WHERE id=?", (conv_id,)
    ).fetchone()
    conn.close()
    if not row:
        return ""
    try:
        return row["composer_draft"] or ""
    except (KeyError, IndexError):
        return ""


def set_conversation_composer_draft(conv_id: str, text: str) -> None:
    """Persist partial chat input for *conv_id* (per-conversation composer).

    Synchronous: opens a connection, writes, commits, closes. Safe to call
    from anywhere but BLOCKS the caller until the disk write completes —
    which on a hot DB with WAL contention can stall ~10-50ms. For UI paths
    that fire on every typing pause, prefer `enqueue_composer_draft_save`."""
    if not conv_id:
        return
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute(
            "UPDATE conversations SET composer_draft=? WHERE id=?",
            (text if text else "", conv_id),
        )
        conn.commit()
        conn.close()


# ── Background composer-draft writer ────────────────────────────────────
#
# Why this exists: the chat composer fires a textChanged-debounced save on
# every typing pause. Doing that write synchronously on the Qt main thread
# stutters typing (10-50ms per write under WAL contention). Instead, the UI
# enqueues the latest draft text and a single daemon thread drains the queue.
# Per-conversation latest-wins coalescing means rapid updates don't pile up
# writes — only the last text in a burst actually touches disk.

import threading as _threading

_draft_latest: dict[str, str] = {}
_draft_lock = _threading.Lock()
_draft_wake = _threading.Event()
_draft_worker_started = False


def _draft_save_worker() -> None:
    """Drain the per-conv latest-draft snapshot whenever woken."""
    while True:
        _draft_wake.wait()
        _draft_wake.clear()
        with _draft_lock:
            snapshot = dict(_draft_latest)
            _draft_latest.clear()
        for conv_id, text in snapshot.items():
            try:
                set_conversation_composer_draft(conv_id, text)
            except Exception as e:
                print(f"[draft worker] {conv_id}: {e}")


def _ensure_draft_worker() -> None:
    global _draft_worker_started
    if _draft_worker_started:
        return
    _draft_worker_started = True
    t = _threading.Thread(target=_draft_save_worker, daemon=True,
                          name="composer-draft-saver")
    t.start()


def enqueue_composer_draft_save(conv_id: str, text: str) -> None:
    """Non-blocking variant of set_conversation_composer_draft. Returns
    immediately. The actual SQLite write happens on a background daemon
    thread within a few ms. Multiple rapid calls per conv_id coalesce into
    one write of the latest text."""
    if not conv_id:
        return
    _ensure_draft_worker()
    with _draft_lock:
        _draft_latest[conv_id] = text
    _draft_wake.set()


# ── Background conversation saver ─────────────────────────────────────
#
# save_conversation() deletes and re-inserts every message row — fine on a
# background thread, brutal on the Qt main thread during long chats (10s+
# stalls with hundreds of messages). The UI enqueues a snapshot; a daemon
# thread coalesces per-conv bursts into one disk write of the latest state.

_conv_save_latest: dict[str, dict] = {}
_conv_save_lock = _threading.Lock()
_conv_save_wake = _threading.Event()
_conv_save_worker_started = False

# Ephemeral UI-only fields — large cached HTML can make a shallow snapshot of a
# long chat cost seconds on the Qt main thread (GIL) while the user is typing.
_PERSIST_SKIP_MSG_KEYS = frozenset({"_html", "_streaming"})


def _messages_for_persist(messages: list[dict]) -> list[dict]:
    """Lightweight snapshot for background save (HTML is rebuilt on load)."""
    out: list[dict] = []
    for m in messages:
        if not _PERSIST_SKIP_MSG_KEYS.intersection(m.keys()):
            out.append(dict(m))
            continue
        out.append({k: v for k, v in m.items() if k not in _PERSIST_SKIP_MSG_KEYS})
    return out


def _resolve_conversation_save_name(
    conv_id: str, messages: list[dict], name_hint: str = ""
) -> str:
    """Derive display name on the background saver thread (not the UI thread)."""
    existing = get_conversation_meta(conv_id)
    name = (name_hint or "").strip() or (existing or {}).get("name", "New Chat")
    if name.startswith("New Chat") and messages:
        first_user = next(
            (m.get("content", "") for m in messages if m.get("role") == "user"),
            "",
        )
        if isinstance(first_user, str) and first_user.strip():
            name = first_user[:40].strip()
            if len(first_user) > 40:
                name += "..."
    return name


def _conv_save_worker() -> None:
    while True:
        _conv_save_wake.wait()
        _conv_save_wake.clear()
        with _conv_save_lock:
            snapshot = dict(_conv_save_latest)
            _conv_save_latest.clear()
        for payload in snapshot.values():
            cid = payload.get("conv_id", "?")
            try:
                save_payload = dict(payload)
                if save_payload.pop("resolve_name", False):
                    save_payload["name"] = _resolve_conversation_save_name(
                        cid,
                        save_payload.get("messages") or [],
                        save_payload.get("name", ""),
                    )
                save_conversation(**save_payload)
            except Exception as e:
                print(f"[conv save worker] {cid}: {e}")
                with _conv_save_lock:
                    _conv_save_latest.setdefault(cid, payload)
                _conv_save_wake.set()


def _ensure_conv_save_worker() -> None:
    global _conv_save_worker_started
    if _conv_save_worker_started:
        return
    _conv_save_worker_started = True
    t = _threading.Thread(target=_conv_save_worker, daemon=True,
                          name="conversation-saver")
    t.start()


def enqueue_conversation_save(
    conv_id: str,
    name: str = "",
    messages: list[dict] | None = None,
    *,
    workspace: str = "",
    model: str = "",
    system_prompt: str = "",
    streams: list[str] | None = None,
    include_timestamps: bool | None = None,
    provider: str | None = None,
    prompt_replace: bool | None = None,
    context_note: str | None = None,
    resolve_name: bool = False,
) -> None:
    """Non-blocking save_conversation — latest-wins per conv_id.

    When *resolve_name* is True, *name* is only a hint; the worker loads
    conversation metadata and derives the title (same rules as the UI
    auto-save) so SQLite reads stay off the Qt main thread.
    """
    if not conv_id:
        return
    _ensure_conv_save_worker()
    msg_snapshot = _messages_for_persist(messages or [])
    with _conv_save_lock:
        _conv_save_latest[conv_id] = {
            "conv_id": conv_id,
            "name": name,
            "messages": msg_snapshot,
            "workspace": workspace,
            "model": model,
            "system_prompt": system_prompt,
            "streams": streams,
            "include_timestamps": include_timestamps,
            "provider": provider,
            "prompt_replace": prompt_replace,
            "context_note": context_note,
            "resolve_name": resolve_name,
        }
    _conv_save_wake.set()


def flush_pending_conversation_saves(timeout_s: float = 5.0) -> None:
    """Drain the background conversation-save queue (call on app exit)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with _conv_save_lock:
            pending = bool(_conv_save_latest)
        if not pending:
            return
        _conv_save_wake.set()
        time.sleep(0.05)
    print("[conv save worker] flush timed out — some conversations may not be saved")


# ── Chat image persistence ──────────────────────────────────────────

IMAGE_CACHE_DIR = DATA_DIR / "image_cache"
IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def save_chat_image(conv_id: str, position: int, image_bytes: bytes,
                    mime_type: str = "image/png", source: str = ""):
    """Store an image BLOB for a message. Overwrites existing at same position."""
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("""
            INSERT OR REPLACE INTO chat_images (conversation_id, position, image_data, mime_type, source)
            VALUES (?, ?, ?, ?, ?)
        """, (conv_id, position, image_bytes, mime_type, source))
        conn.commit()
        conn.close()


def load_chat_image(conv_id: str, position: int) -> bytes | None:
    """Load raw image bytes for a message position."""
    conn = _conv_conn()
    row = conn.execute(
        "SELECT image_data FROM chat_images WHERE conversation_id=? AND position=?",
        (conv_id, position)
    ).fetchone()
    conn.close()
    return bytes(row["image_data"]) if row else None


def get_chat_image_path(conv_id: str, position: int) -> str | None:
    """Get a cached file path for a chat image. Extracts from DB on first access."""
    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif",
               "image/webp": ".webp", "image/bmp": ".bmp"}
    cache_path = IMAGE_CACHE_DIR / f"{conv_id}_{position}.png"
    if cache_path.exists():
        return str(cache_path)
    conn = _conv_conn()
    row = conn.execute(
        "SELECT image_data, mime_type FROM chat_images WHERE conversation_id=? AND position=?",
        (conv_id, position)
    ).fetchone()
    conn.close()
    if not row:
        return None
    ext = ext_map.get(row["mime_type"], ".png")
    cache_path = IMAGE_CACHE_DIR / f"{conv_id}_{position}{ext}"
    cache_path.write_bytes(bytes(row["image_data"]))
    return str(cache_path)


def rename_conversation(conv_id: str, new_name: str):
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("UPDATE conversations SET name=?, modified_at=? WHERE id=?",
                     (new_name, time.time(), conv_id))
        conn.commit()
        conn.close()
    invalidate_conversation_list_cache()


def set_conversation_workspace(conv_id: str, workspace: str):
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("UPDATE conversations SET workspace=?, modified_at=? WHERE id=?",
                     (workspace, time.time(), conv_id))
        conn.commit()
        conn.close()


def set_conversation_cwd(conv_id: str, cwd: str):
    """Pinned working path for this conversation — survives reloads so the
    agent keeps operating in the right directory after the transcript is
    summarized or context is rebuilt."""
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("UPDATE conversations SET conversation_cwd=?, modified_at=? WHERE id=?",
                     (cwd, time.time(), conv_id))
        conn.commit()
        conn.close()


def set_conversation_reflect(conv_id: str, data: dict):
    """Persist (or clear) this conversation's standing self-review rule.
    `data` is {when, scope, criteria}; pass {} to clear."""
    payload = json.dumps(data) if data else ""
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("UPDATE conversations SET reflect_json=?, modified_at=? WHERE id=?",
                     (payload, time.time(), conv_id))
        conn.commit()
        conn.close()


def set_conversation_stream_live(conv_id: str, stream_live: bool):
    """Per-conversation live token streaming (1=stream, 0=only-final)."""
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("UPDATE conversations SET stream_live=?, modified_at=? WHERE id=?",
                     (1 if stream_live else 0, time.time(), conv_id))
        conn.commit()
        conn.close()


def set_conversation_model(conv_id: str, model: str):
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("UPDATE conversations SET model=?, modified_at=? WHERE id=?",
                     (model, time.time(), conv_id))
        conn.commit()
        conn.close()


def set_conversation_provider(conv_id: str, provider: str):
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("UPDATE conversations SET provider=?, modified_at=? WHERE id=?",
                     (provider, time.time(), conv_id))
        conn.commit()
        conn.close()


def set_conversation_streams(conv_id: str, streams: list[str]):
    with _conv_write_lock:
        conn = _conv_conn()
        conn.execute("UPDATE conversations SET streams_json=?, modified_at=? WHERE id=?",
                     (json.dumps(streams), time.time(), conv_id))
        conn.commit()
        conn.close()
    invalidate_conversation_list_cache()


def _build_embed_prefix(streams: list[str]) -> str:
    """Build a prefix from stream descriptions to steer embedding space."""
    if not streams:
        return ""
    try:
        cfg_path = Path(__file__).parent.parent / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            all_streams = {s["name"]: s for s in cfg.get("memory_streams", [])}
            descs = []
            for name in streams:
                s = all_streams.get(name, {})
                desc = s.get("description", "").strip()
                if desc:
                    descs.append(desc)
            if descs:
                return f"[Context: {'; '.join(descs)}] "
    except Exception:
        pass
    return ""


def _embed_messages(queue: list[tuple]):
    """Background: generate embeddings and store in messages_vec."""
    from core.embeddings import embed_batch
    texts = [text for _, text in queue]
    embeddings = embed_batch(texts, purpose="conversation")

    with _conv_write_lock:
        conn = _conv_conn()
        for (row_id, _), emb in zip(queue, embeddings):
            if emb:
                conn.execute(
                    "INSERT OR REPLACE INTO messages_vec(rowid, embedding) VALUES (?, ?)",
                    (row_id, vec_to_bytes(emb))
                )
        conn.commit()
        conn.close()


def search_conversations(query: str, streams: list[str] = None,
                         limit: int = 5, exclude_conv: str = "") -> list[dict]:
    """Hybrid search: FTS5 keyword + vector similarity, scoped to streams.

    Returns list of {conversation_id, name, snippet, score} grouped by conversation.
    """
    conn = _conv_conn()
    fts_results = {}  # conv_id -> {name, snippet, score}
    vec_results = {}

    # Build stream filter clause
    stream_filter = ""
    stream_params = []
    if streams:
        placeholders = ",".join("?" for _ in streams)
        stream_filter = f"""
            AND EXISTS (
                SELECT 1 FROM json_each(c.streams_json) j
                WHERE j.value IN ({placeholders})
            )
        """
        stream_params = list(streams)

    exclude_clause = "AND c.id != ?" if exclude_conv else ""
    exclude_params = [exclude_conv] if exclude_conv else []

    # ── FTS5 keyword search ──
    try:
        fts_rows = conn.execute(f"""
            SELECT m.conversation_id, c.name, m.content, f.rank
            FROM messages_fts f
            JOIN messages m ON m.id = f.rowid
            JOIN conversations c ON c.id = m.conversation_id
            WHERE messages_fts MATCH ?
            {stream_filter}
            {exclude_clause}
            ORDER BY f.rank
            LIMIT ?
        """, [query] + stream_params + exclude_params + [limit * 10]).fetchall()

        for r in fts_rows:
            cid = r["conversation_id"]
            if cid not in fts_results:
                # FTS5 rank is negative (closer to 0 = better), normalize to 0-1
                score = 1.0 / (1.0 + abs(r["rank"]))
                fts_results[cid] = {
                    "name": r["name"],
                    "snippet": r["content"][:200],
                    "score": score,
                }
    except Exception as e:
        print(f"[DB] FTS5 search error: {e}")

    # ── Vector similarity search ──
    if _has_vec():
        # Prefix query with stream context to match how messages were embedded
        query_for_vec = f"{_build_embed_prefix(streams or [])}{query}"
        query_emb = embed_text(query_for_vec, purpose="conversation")
        if query_emb:
            try:
                vec_rows = conn.execute(f"""
                    SELECT m.conversation_id, c.name, m.content, v.distance
                    FROM messages_vec v
                    JOIN messages m ON m.id = v.rowid
                    JOIN conversations c ON c.id = m.conversation_id
                    WHERE v.embedding MATCH ?
                    AND k = ?
                    {stream_filter}
                    {exclude_clause}
                    ORDER BY v.distance
                """, [vec_to_bytes(query_emb), limit * 10] + stream_params + exclude_params).fetchall()

                for r in vec_rows:
                    cid = r["conversation_id"]
                    if cid not in vec_results:
                        # Cosine distance: 0 = identical, 2 = opposite. Normalize to 0-1 score.
                        score = max(0, 1.0 - r["distance"])
                        vec_results[cid] = {
                            "name": r["name"],
                            "snippet": r["content"][:200],
                            "score": score,
                        }
            except Exception as e:
                print(f"[DB] Vector search error: {e}")

    conn.close()

    # ── Blend scores with diversity bonus ──
    all_convs = set(fts_results.keys()) | set(vec_results.keys())
    blended = []
    for cid in all_convs:
        fts = fts_results.get(cid, {})
        vec = vec_results.get(cid, {})
        fts_score = fts.get("score", 0)
        vec_score = vec.get("score", 0)

        # Source diversity bonus: reward results found by BOTH methods
        both_sources = 1 if (fts_score > 0 and vec_score > 0) else 0
        diversity_bonus = 0.05 * both_sources

        total = (W_FTS * fts_score) + (W_VEC * vec_score) + diversity_bonus
        blended.append({
            "conversation_id": cid,
            "name": fts.get("name") or vec.get("name", ""),
            "snippet": fts.get("snippet") or vec.get("snippet", ""),
            "score": round(min(1.0, total), 4),
            "fts_score": round(fts_score, 4),
            "vec_score": round(vec_score, 4),
        })

    blended.sort(key=lambda x: x["score"], reverse=True)
    return blended[:limit]


def _default_streams() -> list[str]:
    """Get stream names that have auto_subscribe enabled."""
    try:
        cfg_path = Path(__file__).parent.parent / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            streams = cfg.get("memory_streams", [])
            return [s["name"] for s in streams if s.get("auto_subscribe")]
    except Exception:
        pass
    return ["General"]


# ── JSON migration ──────────────────────────────────────────────────

def migrate_json_conversations():
    """One-time migration: import JSON conversation files into SQLite."""
    json_dir = DATA_DIR / "conversations"
    if not json_dir.exists():
        return 0

    count = 0
    for f in json_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            conv_id = f.stem
            # Check if already migrated
            conn = _conv_conn()
            exists = conn.execute(
                "SELECT id FROM conversations WHERE id=?", (conv_id,)
            ).fetchone()
            conn.close()
            if exists:
                # Already in DB — remove stale JSON so it can't resurrect after deletion
                try:
                    f.unlink()
                except Exception:
                    pass
                continue

            save_conversation(
                conv_id=conv_id,
                name=data.get("name", conv_id),
                messages=data.get("messages", []),
                workspace=data.get("workspace", ""),
                model=data.get("model", ""),
                system_prompt=data.get("system_prompt", ""),
                streams=data.get("streams"),
            )
            # Remove JSON file so it doesn't get re-imported if user deletes from DB
            try:
                f.unlink()
            except Exception:
                pass
            count += 1
        except Exception as e:
            print(f"[DB] Migration error for {f.name}: {e}")
    return count


# ── Stream DBs ──────────────────────────────────────────────────────

def _stream_db_path(stream_name: str) -> Path:
    safe = stream_name.replace(" ", "_").replace("/", "_")
    return STREAMS_DIR / f"{safe}.db"


# Stream DBs that have already had journal_mode=WAL applied this process.
# Switching to WAL takes a momentary EXCLUSIVE lock, and SQLite does NOT honor
# the busy-timeout for a journal_mode pragma — so re-running it on every open
# threw "database is locked" whenever another thread (e.g. the MemoryAgent) was
# mid-write. WAL is persistent on the file, so it only needs setting once.
_stream_wal_done: set[str] = set()
_stream_wal_lock = threading.Lock()

# Serialize stream WRITES across threads (MemoryAgent, summarizer auto-save,
# memory dialog edits). Mirrors _conv_write_lock for the conversations DB.
# Reads don't take it — WAL allows concurrent readers with one writer.
_stream_write_lock = threading.RLock()


def _stream_conn(stream_name: str) -> sqlite3.Connection:
    path = _stream_db_path(stream_name)
    # timeout= sets the busy handler; busy_timeout pragma makes it explicit so
    # concurrent writers QUEUE (up to 30s) instead of failing instantly.
    conn = sqlite3.connect(str(path), timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    key = str(path)
    if key not in _stream_wal_done:
        with _stream_wal_lock:
            if key not in _stream_wal_done:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    # Already WAL but momentarily locked — fine; mode persists.
                    pass
                _stream_wal_done.add(key)
    conn.row_factory = sqlite3.Row
    if _has_vec():
        load_sqlite_vec(conn)
    return conn


_stream_init_done: set[str] = set()


def init_stream_db(stream_name: str):
    """Create tables for a memory stream database. Idempotent DDL; we still skip
    re-running it once per stream per process to cut redundant write-locks."""
    if stream_name in _stream_init_done:
        return
    _stream_write_lock.acquire()
    try:
        if stream_name in _stream_init_done:
            return
        _init_stream_db_locked(stream_name)
        _stream_init_done.add(stream_name)
    finally:
        _stream_write_lock.release()


def _init_stream_db_locked(stream_name: str):
    """DDL body for init_stream_db. Caller holds _stream_write_lock."""
    conn = _stream_conn(stream_name)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS summaries (
            conversation_id TEXT PRIMARY KEY,
            summary         TEXT,
            end_index       INTEGER NOT NULL DEFAULT 0,
            chars_since     INTEGER NOT NULL DEFAULT 0,
            last_total_chars INTEGER NOT NULL DEFAULT 0,
            updated_at      REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS summary_history (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            summary         TEXT NOT NULL,
            end_index       INTEGER NOT NULL,
            created_at      REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_sumhist_conv
            ON summary_history(conversation_id);

        CREATE TABLE IF NOT EXISTS stream_overview (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            overview    TEXT NOT NULL DEFAULT '',
            updated_at  REAL NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            source_conv TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_entries_key ON entries(key);

        CREATE TABLE IF NOT EXISTS categories (
            path        TEXT PRIMARY KEY,
            created_at  REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS notes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            category    TEXT NOT NULL,
            title       TEXT NOT NULL,
            content     TEXT NOT NULL,
            keywords    TEXT NOT NULL DEFAULT '',
            provenance  TEXT NOT NULL DEFAULT 'unverified',
            source_conv TEXT,
            created_at  REAL NOT NULL,
            updated_at  REAL NOT NULL,
            UNIQUE(category, title)
        );

        CREATE INDEX IF NOT EXISTS idx_notes_category ON notes(category);

        CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
            title, content,
            content=notes,
            content_rowid=id
        );

        CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
            INSERT INTO notes_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END;

        CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
            INSERT INTO notes_fts(notes_fts, rowid, title, content)
            VALUES('delete', old.id, old.title, old.content);
        END;

        CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
            INSERT INTO notes_fts(notes_fts, rowid, title, content)
            VALUES('delete', old.id, old.title, old.content);
            INSERT INTO notes_fts(rowid, title, content)
            VALUES (new.id, new.title, new.content);
        END;
    """)
    # Migrate: add keywords column if missing (for existing DBs)
    try:
        conn.execute("ALTER TABLE notes ADD COLUMN keywords TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass  # Column already exists
    # Migrate: add provenance column (origin/trust label) for existing DBs.
    try:
        conn.execute("ALTER TABLE notes ADD COLUMN provenance TEXT NOT NULL DEFAULT 'unverified'")
    except Exception:
        pass  # Column already exists

    # Vector table for entries
    if _has_vec():
        try:
            conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS entries_vec
                USING vec0(embedding float[{EMBED_DIMS}])
            """)
        except Exception:
            pass
    conn.close()


def save_stream_summary(stream_name: str, conv_id: str,
                        summary: str, end_index: int,
                        chars_since: int, last_total_chars: int):
    """Save/update a rolling summary for a conversation in a stream."""
    summary = sanitize_agent_paths(summary)
    init_stream_db(stream_name)
    now = time.time()
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        conn.execute("""
            INSERT INTO summaries (conversation_id, summary, end_index, chars_since,
                                   last_total_chars, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id) DO UPDATE SET
                summary=excluded.summary, end_index=excluded.end_index,
                chars_since=excluded.chars_since, last_total_chars=excluded.last_total_chars,
                updated_at=excluded.updated_at
        """, (conv_id, summary, end_index, chars_since, last_total_chars, now))
        # Append to history
        if summary:
            conn.execute("""
                INSERT INTO summary_history (conversation_id, summary, end_index, created_at)
                VALUES (?, ?, ?, ?)
            """, (conv_id, summary, end_index, now))
            # Keep only last 3 per conversation
            conn.execute("""
                DELETE FROM summary_history WHERE id NOT IN (
                    SELECT id FROM summary_history
                    WHERE conversation_id=? ORDER BY created_at DESC LIMIT 3
                ) AND conversation_id=?
            """, (conv_id, conv_id))
        conn.commit()
        conn.close()


def load_stream_summary(stream_name: str, conv_id: str) -> dict | None:
    """Load a rolling summary for a conversation from a stream."""
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    row = conn.execute(
        "SELECT * FROM summaries WHERE conversation_id=?", (conv_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "summary": row["summary"],
        "end_index": row["end_index"],
        "chars_since": row["chars_since"],
        "last_total_chars": row["last_total_chars"],
    }


def clear_stream_summary(stream_name: str, conv_id: str):
    """Delete the rolling summary (and history) for a conversation in a stream."""
    init_stream_db(stream_name)
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        conn.execute("DELETE FROM summaries WHERE conversation_id=?", (conv_id,))
        conn.execute("DELETE FROM summary_history WHERE conversation_id=?", (conv_id,))
        conn.commit()
        conn.close()


def load_stream_summary_history(stream_name: str, conv_id: str) -> list[dict]:
    """Load summary history for a conversation from a stream."""
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    rows = conn.execute("""
        SELECT summary, end_index, created_at FROM summary_history
        WHERE conversation_id=? ORDER BY created_at DESC LIMIT 3
    """, (conv_id,)).fetchall()
    conn.close()
    return [{"summary": r["summary"], "end_index": r["end_index"],
             "timestamp": r["created_at"]} for r in rows]


def list_stream_summaries(stream_name: str) -> list[dict]:
    """List all conversations that have a summary in a stream, newest first."""
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    rows = conn.execute("""
        SELECT conversation_id, summary, updated_at
        FROM summaries ORDER BY updated_at DESC
    """).fetchall()
    conn.close()
    return [{"conversation_id": r["conversation_id"],
             "summary": r["summary"],
             "updated_at": r["updated_at"]} for r in rows]


def update_stream_summary_text(stream_name: str, conv_id: str, summary: str):
    """Update only the summary text for a conversation in a stream (preserves other fields)."""
    init_stream_db(stream_name)
    import time as _time
    summary = sanitize_agent_paths(summary)
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        conn.execute(
            "UPDATE summaries SET summary=?, updated_at=? WHERE conversation_id=?",
            (summary, _time.time(), conv_id)
        )
        conn.commit()
        conn.close()


# ── Stream overview (high-level, cross-session) ─────────────────────
# One row per stream DB. User-editable; captures the stream's enduring
# priorities/observations. Falls back to latest prior-conv summary when empty.

def load_stream_overview(stream_name: str) -> str:
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    row = conn.execute(
        "SELECT overview FROM stream_overview WHERE id = 1").fetchone()
    conn.close()
    return (row["overview"] if row else "") or ""


def save_stream_overview(stream_name: str, overview: str):
    overview = sanitize_agent_paths(overview)
    init_stream_db(stream_name)
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        conn.execute(
            "INSERT INTO stream_overview (id, overview, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET overview=excluded.overview, updated_at=excluded.updated_at",
            (overview, time.time()))
        conn.commit()
        conn.close()




# ── Stream Notes CRUD ───────────────────────────────────────────────

MAX_NOTE_CHARS = 2000


def create_category(stream_name: str, path: str):
    """Ensure a category path exists (including intermediate paths)."""
    init_stream_db(stream_name)
    now = time.time()
    parts = path.split("/")
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        for i in range(len(parts)):
            sub = "/".join(parts[:i + 1])
            conn.execute(
                "INSERT OR IGNORE INTO categories (path, created_at) VALUES (?, ?)",
                (sub, now))
        conn.commit()
        conn.close()


def delete_category_entry(stream_name: str, path: str):
    """Delete a category entry (and sub-paths) from the categories table."""
    init_stream_db(stream_name)
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        conn.execute("DELETE FROM categories WHERE path=? OR path LIKE ?",
                     (path, path + "/%"))
        conn.commit()
        conn.close()


def list_note_categories(stream_name: str) -> list[dict]:
    """List all note categories in a stream with counts.
    Includes empty categories from the categories table."""
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    # Get categories with notes
    note_cats = conn.execute("""
        SELECT category, COUNT(*) as count,
               MAX(updated_at) as last_updated
        FROM notes GROUP BY category
    """).fetchall()
    note_map = {r["category"]: {"count": r["count"], "last_updated": r["last_updated"]}
                for r in note_cats}
    # Get all registered categories (including empty)
    all_cats = conn.execute("SELECT path, created_at FROM categories").fetchall()
    conn.close()

    # Merge: registered categories + categories that have notes
    all_paths = set(note_map.keys())
    for r in all_cats:
        all_paths.add(r["path"])

    result = []
    for path in sorted(all_paths):
        info = note_map.get(path, {})
        result.append({
            "category": path,
            "count": info.get("count", 0),
            "last_updated": info.get("last_updated", 0),
        })
    return result


def list_notes_in_category(stream_name: str, category: str) -> list[dict]:
    """List note titles in a category (no content — just the index)."""
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    rows = conn.execute("""
        SELECT id, title, LENGTH(content) as size, updated_at
        FROM notes WHERE category=? ORDER BY updated_at DESC
    """, (category,)).fetchall()
    conn.close()
    return [{"id": r["id"], "title": r["title"], "size": r["size"],
             "updated_at": r["updated_at"]} for r in rows]


def read_note(stream_name: str, category: str, title: str) -> dict | None:
    """Read a specific note by category + title."""
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    row = conn.execute(
        "SELECT * FROM notes WHERE category=? AND title=?", (category, title)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row["id"], "category": row["category"], "title": row["title"],
            "content": row["content"], "keywords": row["keywords"] if "keywords" in row.keys() else "",
            "provenance": row["provenance"] if "provenance" in row.keys() else "unverified",
            "source_conv": row["source_conv"],
            "created_at": row["created_at"], "updated_at": row["updated_at"]}


def _split_note_content(content: str) -> tuple[str, str]:
    """Split note into (current_section, evidence_section) at "## Evidence" header.
    Returns (full_content, "") if no header present."""
    lines = content.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("## evidence"):
            current = "\n".join(lines[:i]).strip()
            evidence = "\n".join(lines[i + 1:]).strip()
            return current, evidence
    return content.strip(), ""


def _merge_note_content(old_content: str, new_content: str) -> str:
    """Merge new note content into existing, preserving append-only Evidence.

    New Current section wins (LLM may have revised). Evidence lines accumulate —
    dedupe by exact-line match so re-stating old evidence doesn't inflate the log.
    """
    old_curr, old_evi = _split_note_content(old_content)
    new_curr, new_evi = _split_note_content(new_content)
    final_curr = new_curr or old_curr
    old_lines = [ln for ln in old_evi.splitlines() if ln.strip()]
    new_lines = [ln for ln in new_evi.splitlines() if ln.strip() and ln not in old_lines]
    merged_evi_lines = old_lines + new_lines
    if merged_evi_lines:
        evi_block = "\n".join(merged_evi_lines)
        return f"{final_curr}\n\n## Evidence\n{evi_block}"
    return final_curr


def _embed_note_row(stream_name: str, note_id: int, title: str, content: str):
    """Best-effort: embed note and upsert into entries_vec. Silent on failure."""
    if not _has_vec():
        return
    try:
        prefix = _build_embed_prefix([stream_name])
        curr, _ = _split_note_content(content)
        text = f"{prefix}{title}: {curr[:1500]}"
        emb = embed_text(text, purpose="conversation")
        if not emb:
            return
        with _stream_write_lock:
            conn = _stream_conn(stream_name)
            conn.execute(
                "INSERT OR REPLACE INTO entries_vec(rowid, embedding) VALUES (?, ?)",
                (note_id, vec_to_bytes(emb))
            )
            conn.commit()
            conn.close()
    except Exception as e:
        print(f"[DB] note embed error ({stream_name}/{title}): {e}")


def save_note(stream_name: str, category: str, title: str, content: str,
              keywords: str = "", source_conv: str = "", provenance: str = "") -> dict:
    """Save or update a note. Content capped at MAX_NOTE_CHARS.
    On update: merges new Evidence lines into existing (append-only timeline).
    Embeds note into entries_vec for vector recall (best-effort).
    """
    content = sanitize_agent_paths(content)
    keywords = sanitize_agent_paths(keywords) if keywords else keywords
    existing = read_note(stream_name, category, title)
    if existing:
        content = _merge_note_content(existing["content"], content)
    if len(content) > MAX_NOTE_CHARS:
        content = content[:MAX_NOTE_CHARS]
    # Provenance (origin/trust): an explicit value wins; otherwise keep the
    # existing note's label; brand-new notes default to 'unverified'.
    prov = (provenance or (existing.get("provenance") if existing else "")
            or "unverified")
    create_category(stream_name, category)
    init_stream_db(stream_name)
    now = time.time()
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        conn.execute("""
            INSERT INTO notes (category, title, content, keywords, provenance, source_conv, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(category, title) DO UPDATE SET
                content=excluded.content, keywords=excluded.keywords,
                provenance=excluded.provenance,
                source_conv=excluded.source_conv, updated_at=excluded.updated_at
        """, (category, title, content, keywords, prov, source_conv, now, now))
        note_id_row = conn.execute(
            "SELECT id FROM notes WHERE category=? AND title=?", (category, title)
        ).fetchone()
        conn.commit()
        conn.close()
    if note_id_row:
        _embed_note_row(stream_name, note_id_row["id"], title, content)
    return {"saved": True, "category": category, "title": title,
            "chars": len(content), "keywords": keywords, "provenance": prov}


def delete_note(stream_name: str, category: str, title: str) -> bool:
    """Delete a note by category + title. Also removes vector index entry."""
    init_stream_db(stream_name)
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        note_id_row = conn.execute(
            "SELECT id FROM notes WHERE category=? AND title=?", (category, title)
        ).fetchone()
        cur = conn.execute(
            "DELETE FROM notes WHERE category=? AND title=?", (category, title))
        if note_id_row and _has_vec():
            try:
                conn.execute("DELETE FROM entries_vec WHERE rowid=?", (note_id_row["id"],))
            except Exception:
                pass
        conn.commit()
        conn.close()
    return cur.rowcount > 0


def vector_search_notes(stream_names: list[str], queries: list[str],
                         limit_per_stream: int = 8) -> list[dict]:
    """Vector search notes across streams using one or more query variants.
    Scores fused via Reciprocal Rank Fusion (RRF) across all query variants.
    Returns deduped list [{stream, category, title, content, score}] sorted by score desc.
    """
    if not _has_vec() or not queries or not stream_names:
        return []
    from core.embeddings import embed_batch
    prefix = _build_embed_prefix(stream_names)
    texts = [f"{prefix}{q}" for q in queries if q and q.strip()]
    if not texts:
        return []
    embeddings = embed_batch(texts, purpose="conversation")
    embeddings = [e for e in embeddings if e]
    if not embeddings:
        return []

    RRF_K = 60  # standard RRF constant
    fused: dict[tuple, dict] = {}  # (stream, category, title) -> {record, rrf_score}

    for stream in stream_names:
        try:
            init_stream_db(stream)
            conn = _stream_conn(stream)
            for emb in embeddings:
                try:
                    rows = conn.execute("""
                        SELECT n.category, n.title, n.content, v.distance
                        FROM entries_vec v
                        JOIN notes n ON n.id = v.rowid
                        WHERE v.embedding MATCH ? AND k = ?
                        ORDER BY v.distance
                    """, (vec_to_bytes(emb), limit_per_stream)).fetchall()
                except Exception:
                    rows = []
                for rank, r in enumerate(rows):
                    key = (stream, r["category"], r["title"])
                    rrf = 1.0 / (RRF_K + rank + 1)
                    if key in fused:
                        fused[key]["rrf_score"] += rrf
                    else:
                        fused[key] = {
                            "record": {
                                "stream": stream,
                                "category": r["category"],
                                "title": r["title"],
                                "content": r["content"],
                            },
                            "rrf_score": rrf,
                        }
            conn.close()
        except Exception as e:
            print(f"[DB] vector_search_notes error ({stream}): {e}")

    ranked = sorted(fused.values(), key=lambda x: -x["rrf_score"])
    return [{**e["record"], "score": e["rrf_score"]} for e in ranked]


def search_notes(stream_name: str, query: str, limit: int = 10) -> list[dict]:
    """FTS5 search across all notes in a stream."""
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    try:
        rows = conn.execute("""
            SELECT n.id, n.category, n.title,
                   SUBSTR(n.content, 1, 300) as snippet, f.rank
            FROM notes_fts f
            JOIN notes n ON n.id = f.rowid
            WHERE notes_fts MATCH ?
            ORDER BY f.rank
            LIMIT ?
        """, (query, limit)).fetchall()
    except Exception:
        rows = []
    conn.close()
    return [{"id": r["id"], "category": r["category"], "title": r["title"],
             "snippet": r["snippet"]} for r in rows]


# ── Keyword scan ────────────────────────────────────────────────────

def scan_keywords(stream_names: list[str], text: str) -> list[dict]:
    """Scan text against all keyword patterns in the given streams.
    Returns notes whose keywords match. Zero LLM cost — pure regex.

    Keywords are stored as comma-separated patterns per note.
    Each pattern is compiled as a case-insensitive regex.
    """
    import re as _re
    if not text or not stream_names:
        return []

    text_lower = text.lower()
    matched = []

    for stream in stream_names:
        try:
            init_stream_db(stream)
            conn = _stream_conn(stream)
            rows = conn.execute(
                "SELECT category, title, content, keywords, provenance FROM notes WHERE keywords != ''"
            ).fetchall()
            conn.close()

            for r in rows:
                prov = r["provenance"] if "provenance" in r.keys() else "unverified"
                kw_str = r["keywords"]
                patterns = [p.strip() for p in kw_str.split(",") if p.strip()]
                for pattern in patterns:
                    try:
                        if _re.search(pattern, text_lower, _re.IGNORECASE):
                            matched.append({
                                "stream": stream,
                                "category": r["category"],
                                "title": r["title"],
                                "content": r["content"],
                                "provenance": prov,
                                "matched_keyword": pattern,
                            })
                            break  # One match per note is enough
                    except _re.error:
                        # Bad regex — try as literal
                        if pattern.lower() in text_lower:
                            matched.append({
                                "stream": stream,
                                "category": r["category"],
                                "title": r["title"],
                                "provenance": prov,
                                "content": r["content"],
                                "matched_keyword": pattern,
                            })
                            break
        except Exception:
            continue

    return matched


# ── Stream Notes: tree management ───────────────────────────────────

def rename_category(stream_name: str, old_path: str, new_path: str) -> int:
    """Rename a category and all its sub-categories. Returns count of notes moved."""
    init_stream_db(stream_name)
    conn = _stream_conn(stream_name)
    # Exact match
    cur1 = conn.execute(
        "UPDATE notes SET category=?, updated_at=? WHERE category=?",
        (new_path, time.time(), old_path))
    count = cur1.rowcount
    # Sub-categories: trading/renko → new_path/renko
    rows = conn.execute(
        "SELECT id, category FROM notes WHERE category LIKE ?",
        (old_path + "/%",)).fetchall()
    for r in rows:
        new_cat = new_path + r["category"][len(old_path):]
        conn.execute("UPDATE notes SET category=?, updated_at=? WHERE id=?",
                     (new_cat, time.time(), r["id"]))
        count += 1
    conn.commit()
    conn.close()
    return count


def move_category(stream_name: str, source_path: str, dest_parent: str) -> int:
    """Move a category (and all sub-categories) under a new parent.

    move_category("stream", "source", "dest_parent")
      source/child → dest_parent/source/child
      source       → dest_parent/source
    """
    leaf = source_path.rsplit("/", 1)[-1]
    new_path = f"{dest_parent}/{leaf}" if dest_parent else leaf
    return rename_category(stream_name, source_path, new_path)


def rename_note(stream_name: str, category: str, old_title: str, new_title: str) -> bool:
    """Rename a note within the same category."""
    init_stream_db(stream_name)
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        cur = conn.execute(
            "UPDATE notes SET title=?, updated_at=? WHERE category=? AND title=?",
            (new_title, time.time(), category, old_title))
        conn.commit()
        conn.close()
    return cur.rowcount > 0


def move_note(stream_name: str, old_category: str, title: str, new_category: str) -> bool:
    """Move a note to a different category."""
    init_stream_db(stream_name)
    with _stream_write_lock:
        conn = _stream_conn(stream_name)
        cur = conn.execute(
            "UPDATE notes SET category=?, updated_at=? WHERE category=? AND title=?",
            (new_category, time.time(), old_category, title))
        conn.commit()
        conn.close()
    return cur.rowcount > 0


# ── Maintenance ─────────────────────────────────────────────────────

# Only VACUUM when dead space actually warrants it. VACUUM rewrites the ENTIRE
# database file — on a 75MB conversations.db that's several seconds — so doing it
# every launch is pure waste when the free-page ratio is near zero (the common
# case). Reclaim only once bloat crosses a real threshold.
VACUUM_FREE_RATIO = 0.20   # ≥20% of pages free → worth reclaiming
VACUUM_MIN_FREE_PAGES = 2000  # …and at least this many (skip tiny DBs)


def _needs_vacuum(conn: sqlite3.Connection) -> bool:
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if not page_count:
            return False
        return (freelist >= VACUUM_MIN_FREE_PAGES
                and freelist / page_count >= VACUUM_FREE_RATIO)
    except Exception:
        return False


def vacuum_db(conn: sqlite3.Connection, *, force: bool = False):
    """Checkpoint WAL (cheap, every time) and reclaim dead space (only when the
    free-page ratio warrants it, or force=True)."""
    try:
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    try:
        if force or _needs_vacuum(conn):
            conn.execute("VACUUM")
    except Exception:
        pass  # VACUUM can fail if another connection is open — not critical


def maintain_all():
    """Run periodic maintenance on all databases. Call on app startup or shutdown."""
    # Conversations DB
    try:
        conn = _conv_conn()
        vacuum_db(conn)
        conn.close()
    except Exception:
        pass

    # Stream DBs
    for db_file in STREAMS_DIR.glob("*.db"):
        try:
            conn = sqlite3.connect(str(db_file), timeout=5)
            conn.execute("PRAGMA journal_mode=WAL")
            vacuum_db(conn)
            conn.close()
        except Exception:
            pass


# ── Init on import ──────────────────────────────────────────────────

init_conversations_db()
_migrated = migrate_json_conversations()
if _migrated:
    print(f"[DB] Migrated {_migrated} conversations from JSON to SQLite")

# Run maintenance OFF the import critical path AND off the busy startup window.
#
# VACUUM must briefly acquire the database lock. Firing it at import time — while
# the first conversation is still loading and several pooled connections are
# live — means it loses the lock race and fails silently (caught in vacuum_db).
# It only ever ran once per launch, at the worst possible moment, so a bloated
# file was NEVER reclaimed and grew without bound: an 88MB conversations.db that
# was 89% dead pages, turning the first (cold-disk) conversation load into a
# ~1.5s stall. Fix: wait for startup to settle, then run maintenance, then
# re-check periodically. Once the file is compact the VACUUM threshold isn't met,
# so each later pass is just a cheap WAL checkpoint.
_MAINT_STARTUP_DELAY_S = 30
_MAINT_INTERVAL_S = 600  # re-check ~every 10 min; no-op once the file is compact


def _startup_maintenance():
    import time as _t
    _t.sleep(_MAINT_STARTUP_DELAY_S)
    while True:
        try:
            maintain_all()
        except Exception:
            pass
        _t.sleep(_MAINT_INTERVAL_S)


_threading.Thread(
    target=_startup_maintenance, name="db-maintenance", daemon=True
).start()
