"""Call-graph expansion — retrieve the neighbors, not just the hit.

When retrieval lands the chunk that answers a question, the code the agent
needs *next* is almost always one hop away in the call graph: the functions this
chunk calls (callees) and the functions that call it (callers). Pure semantic
search throws that structure away — it ranks chunks by similarity, blind to the
fact that code has a dependency graph. This module walks that graph one hop and
folds the neighbors into the result set. It's a big part of why an editor
"already knows" the surrounding code: not better embeddings, graph traversal.

Implementation is index-native (no language server required): the symbol names
already extracted at chunk time ARE a lightweight call graph.
  - callees: symbol names that appear as identifiers inside the seed chunk's
    body and resolve to a known chunk name in the same index.
  - callers: chunks whose body references the seed chunk's symbol name.

This is an approximation of a true call graph (it can't tell a call from a
mention, and it ignores scope/shadowing), but for "show me the related code" it
is cheap, offline, and right far more often than not. A real LSP-backed call
hierarchy can replace the internals later behind this same interface — the LSP
client already exposes goto_definition/find_references.

Never raises into the caller. Bounded fan-out so a hub function (called from 200
places) can't flood the context.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Identifiers too generic to be useful call-graph edges — linking on these would
# pull half the codebase. Not exhaustive; just the worst offenders.
_GENERIC = frozenset({
    "self", "cls", "None", "True", "False", "return", "import", "from",
    "def", "class", "if", "else", "elif", "for", "while", "try", "except",
    "finally", "with", "as", "in", "is", "and", "or", "not", "lambda",
    "int", "str", "float", "bool", "list", "dict", "set", "tuple", "len",
    "print", "range", "type", "super", "get", "set", "id", "value", "name",
    "text", "data", "result", "args", "kwargs", "key", "path", "i", "e",
})


def _identifiers(text: str) -> set[str]:
    return {
        t for t in _IDENT_RE.findall(text or "")
        if len(t) > 2 and t not in _GENERIC
    }


def expand(db_path: str, seeds: list[dict], *,
           max_neighbors: int = 6, per_seed: int = 3) -> list[dict]:
    """Given seed chunks (search() result dicts), return up to `max_neighbors`
    NEW neighbor chunks (callers + callees), deduped against the seeds. Each
    neighbor is tagged with `relation` ('calls' / 'called-by') and `via` (the
    symbol that linked them) so the model knows WHY it's seeing this code.

    Read-only, short-lived connection. Any failure -> empty list (seeds stand
    on their own)."""
    if not seeds:
        return []
    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
    except Exception:
        return []

    seen_keys = {_key(s.get("file"), s.get("line_start")) for s in seeds}
    out: list[dict] = []
    try:
        # Build a name->chunk lookup once (names are cheap; bodies are not).
        name_rows = conn.execute(
            "SELECT id, file_path, text, line_start, line_end, kind, name "
            "FROM chunks WHERE name IS NOT NULL AND name != ''"
        ).fetchall()
        by_name: dict[str, list[sqlite3.Row]] = {}
        for r in name_rows:
            by_name.setdefault(r["name"], []).append(r)

        for seed in seeds:
            added = 0
            seed_name = seed.get("name") or ""
            seed_text = _fetch_text(conn, seed)

            # CALLEES: identifiers in the seed body that name a known chunk.
            if seed_text:
                for ident in _identifiers(seed_text):
                    if ident == seed_name or ident not in by_name:
                        continue
                    for r in by_name[ident]:
                        if added >= per_seed or len(out) >= max_neighbors:
                            break
                        k = _key(r["file_path"], r["line_start"])
                        if k in seen_keys:
                            continue
                        seen_keys.add(k)
                        out.append(_as_hit(r, "calls", ident))
                        added += 1

            # CALLERS: chunks whose body references the seed's symbol name.
            if seed_name and added < per_seed and len(out) < max_neighbors:
                try:
                    caller_rows = conn.execute(
                        "SELECT id, file_path, text, line_start, line_end, "
                        "kind, name FROM chunks "
                        "WHERE text LIKE ? AND name != ? LIMIT 20",
                        (f"%{seed_name}%", seed_name),
                    ).fetchall()
                except Exception:
                    caller_rows = []
                for r in caller_rows:
                    if added >= per_seed or len(out) >= max_neighbors:
                        break
                    if seed_name not in _identifiers(r["text"]):
                        continue  # LIKE substring matched mid-word; require real token
                    k = _key(r["file_path"], r["line_start"])
                    if k in seen_keys:
                        continue
                    seen_keys.add(k)
                    out.append(_as_hit(r, "called-by", seed_name))
                    added += 1

            if len(out) >= max_neighbors:
                break
    except Exception:
        return out
    finally:
        conn.close()
    return out[:max_neighbors]


def _fetch_text(conn: sqlite3.Connection, seed: dict) -> str:
    """Seeds carry truncated text (600 chars). Pull the full body for accurate
    callee extraction when we can identify the row."""
    f, ls = seed.get("file"), seed.get("line_start")
    if f and ls is not None:
        try:
            row = conn.execute(
                "SELECT text FROM chunks WHERE file_path=? AND line_start=? LIMIT 1",
                (f, ls),
            ).fetchone()
            if row:
                return row["text"]
        except Exception:
            pass
    return seed.get("text", "") or ""


def _as_hit(row: sqlite3.Row, relation: str, via: str) -> dict:
    return {
        "file": row["file_path"],
        "line_start": row["line_start"],
        "line_end": row["line_end"],
        "kind": row["kind"],
        "name": row["name"],
        "text": (row["text"] or "")[:600],
        "score": None,
        "vec_score": None,
        "relation": relation,
        "via": via,
    }


def _key(file: str | None, line_start) -> tuple:
    return (file or "", line_start)


def db_path_for(name: str) -> str | None:
    """Resolve the on-disk index DB path for a workspace name."""
    p = Path(__file__).parent.parent / "data" / "vector_indexes" / f"{name}.db"
    return str(p) if p.exists() else None
