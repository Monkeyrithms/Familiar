"""Retrieval feedback log — the on-ramp to a self-improving index.

Every proactive retrieval is a guess: "these chunks are relevant to this
message." Right now that guess vanishes after the turn. This module persists it,
append-only, so that over time the agent accumulates exactly the dataset that
Option B (fine-tuning a local embedding model) needs — (query, chunk, did-it-
actually-help) triples — mined for free from real usage, with no extra user
effort.

Two events:
  - log_retrieval(): records what was retrieved for a message (the guess).
  - mark_used(): later, when a retrieved file is actually read/edited in the
    same turn, flips that row's `used` flag (the outcome).

The pairing of guess + outcome is the training signal. A chunk that was
retrieved AND then used is a positive; retrieved-but-never-touched is a soft
negative. Nothing here changes retrieval behavior — it only observes, so it's
safe to leave on. Storage is a single SQLite file under data/, capped by age so
it can't grow without bound.

Deliberately decoupled: failures here NEVER propagate into the turn. Logging is
best-effort telemetry, not a correctness path.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

_DB_PATH = Path(__file__).parent.parent / "data" / "retrieval_feedback.db"
_LOCK = threading.Lock()
_RETENTION_DAYS = 90


def _conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS retrievals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL NOT NULL,
            turn_id     TEXT,
            workspace   TEXT,
            query       TEXT NOT NULL,
            file        TEXT NOT NULL,
            line_start  INTEGER,
            line_end    INTEGER,
            kind        TEXT,
            name        TEXT,
            vec_score   REAL,
            rerank_score REAL,
            rank        INTEGER,
            source      TEXT,          -- 'preinject' | 'explore' | 'search'
            used        INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rf_turn ON retrievals(turn_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_rf_file ON retrievals(turn_id, file)")
    return conn


def log_retrieval(query: str, hits: list[dict], *, turn_id: str = "",
                  workspace: str = "", source: str = "preinject") -> None:
    """Record the chunks retrieved for a turn. Best-effort; swallows all errors.
    `hits` are the search() result dicts (file/line/kind/name/vec_score/...)."""
    if not query or not hits:
        return
    try:
        rows = []
        now = time.time()
        for rank, h in enumerate(hits):
            rows.append((
                now, turn_id, workspace, query,
                h.get("file", ""), h.get("line_start"), h.get("line_end"),
                h.get("kind"), h.get("name"),
                h.get("vec_score"), h.get("rerank_score"), rank, source,
            ))
        with _LOCK, _conn() as conn:
            conn.executemany(
                "INSERT INTO retrievals (ts, turn_id, workspace, query, file, "
                "line_start, line_end, kind, name, vec_score, rerank_score, "
                "rank, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            _prune(conn)
    except Exception:
        pass


def mark_used(turn_id: str, files: set[str] | list[str]) -> int:
    """Flip `used=1` for retrievals in this turn whose file got touched. Returns
    rows updated. The retrieved-AND-used rows are the positive training pairs."""
    if not turn_id or not files:
        return 0
    try:
        fileset = {_norm(f) for f in files}
        with _LOCK, _conn() as conn:
            rows = conn.execute(
                "SELECT id, file FROM retrievals WHERE turn_id=? AND used=0",
                (turn_id,),
            ).fetchall()
            ids = [rid for rid, f in rows if _norm(f) in fileset]
            if not ids:
                return 0
            conn.executemany(
                "UPDATE retrievals SET used=1 WHERE id=?", [(i,) for i in ids]
            )
            return len(ids)
    except Exception:
        return 0


def _norm(path: str) -> str:
    """Compare by basename + parent, so 'core/agent.py' matches an absolute path
    ending the same way (retrieval stores repo-relative; usage may be absolute)."""
    p = (path or "").replace("\\", "/").rstrip("/")
    return os.path.basename(p).lower()


def stats() -> dict:
    """Aggregate counts for a quick health read — total logged, used, hit-rate.
    Useful both as a sanity check and as the 'is this dataset big enough to
    fine-tune yet' gauge."""
    try:
        with _LOCK, _conn() as conn:
            total = conn.execute("SELECT COUNT(*) FROM retrievals").fetchone()[0]
            used = conn.execute(
                "SELECT COUNT(*) FROM retrievals WHERE used=1"
            ).fetchone()[0]
            turns = conn.execute(
                "SELECT COUNT(DISTINCT turn_id) FROM retrievals WHERE turn_id != ''"
            ).fetchone()[0]
        return {
            "total_retrievals": total, "used": used, "turns": turns,
            "hit_rate": round(used / total, 3) if total else 0.0,
        }
    except Exception:
        return {"total_retrievals": 0, "used": 0, "turns": 0, "hit_rate": 0.0}


def export_pairs(min_used: int = 1) -> list[dict]:
    """Emit (query, file, kind, name, label) rows for downstream fine-tuning.
    label=1 if the chunk was used, 0 if retrieved-but-ignored. This is the
    bridge to Option B — call it when there's enough data to train on."""
    try:
        with _LOCK, _conn() as conn:
            rows = conn.execute(
                "SELECT query, file, kind, name, used FROM retrievals"
            ).fetchall()
        return [
            {"query": q, "file": f, "kind": k, "name": n, "label": int(u)}
            for q, f, k, n, u in rows
        ]
    except Exception:
        return []


def _prune(conn: sqlite3.Connection) -> None:
    cutoff = time.time() - _RETENTION_DAYS * 86400
    try:
        conn.execute("DELETE FROM retrievals WHERE ts < ?", (cutoff,))
    except Exception:
        pass
