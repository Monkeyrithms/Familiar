"""
Database query tool — execute SQL queries against SQLite databases.
Read-only by default, write mode opt-in.
"""

import json
import sqlite3
from pathlib import Path
from tools.registry import registry


def db_query(path: str, query: str = "", write: bool = False, limit: int = 100,
             action: str = "query") -> str:
    """
    Execute SQL queries or inspect the schema of a SQLite database.

    action="query"   — run a SQL statement (default)
    action="inspect" — return full schema: tables, columns, types, foreign keys,
                       indexes, and row counts. No query argument needed.
    """
    p = Path(path)
    if not p.exists():
        return json.dumps({"error": f"Database not found: {path}"})
    if p.suffix not in (".db", ".sqlite", ".sqlite3"):
        return json.dumps({"error": "Only .db/.sqlite/.sqlite3 files supported"})

    try:
        conn = sqlite3.connect(str(p), timeout=5)
        conn.row_factory = sqlite3.Row

        if action == "inspect":
            schema = _inspect_schema(conn)
            conn.close()
            return json.dumps(schema, ensure_ascii=False, default=str)

        # --- regular query ---
        if not query:
            conn.close()
            return json.dumps({"error": "query is required for action='query'"})

        if write:
            conn.execute(query)
            conn.commit()
            changes = conn.total_changes
            conn.close()
            return json.dumps({"success": True, "changes": changes})
        else:
            cur = conn.execute(query)
            rows = cur.fetchmany(limit)
            columns = [d[0] for d in cur.description] if cur.description else []
            data = [dict(r) for r in rows]
            conn.close()
            return json.dumps({
                "columns": columns,
                "rows": data,
                "count": len(data),
                "truncated": len(data) >= limit,
            }, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _inspect_schema(conn: sqlite3.Connection) -> dict:
    """Return a comprehensive schema description of the connected database."""
    conn.row_factory = sqlite3.Row
    tables_raw = conn.execute(
        "SELECT name, type FROM sqlite_master "
        "WHERE type IN ('table','view') ORDER BY type, name"
    ).fetchall()

    schema: dict = {"tables": {}, "views": {}}

    for row in tables_raw:
        name = row["name"]
        obj_type = row["type"]  # 'table' or 'view'

        # Column info
        cols = conn.execute(f"PRAGMA table_info({name})").fetchall()
        columns = [
            {
                "cid":         c["cid"],
                "name":        c["name"],
                "type":        c["type"],
                "not_null":    bool(c["notnull"]),
                "default":     c["dflt_value"],
                "primary_key": bool(c["pk"]),
            }
            for c in cols
        ]

        # Foreign keys (tables only)
        fks = []
        if obj_type == "table":
            fk_rows = conn.execute(f"PRAGMA foreign_key_list({name})").fetchall()
            for fk in fk_rows:
                fks.append({
                    "from_col":  fk["from"],
                    "to_table":  fk["table"],
                    "to_col":    fk["to"],
                    "on_update": fk["on_update"],
                    "on_delete": fk["on_delete"],
                })

        # Indexes (tables only)
        indexes = []
        if obj_type == "table":
            idx_rows = conn.execute(f"PRAGMA index_list({name})").fetchall()
            for idx in idx_rows:
                idx_info = conn.execute(f"PRAGMA index_info({idx['name']})").fetchall()
                indexes.append({
                    "name":    idx["name"],
                    "unique":  bool(idx["unique"]),
                    "columns": [ii["name"] for ii in idx_info],
                })

        # Row count (skip for views to avoid performance issues)
        row_count = None
        if obj_type == "table":
            try:
                row_count = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
            except Exception:
                pass

        entry = {
            "columns":      columns,
            "foreign_keys": fks,
            "indexes":      indexes,
        }
        if row_count is not None:
            entry["row_count"] = row_count

        bucket = "tables" if obj_type == "table" else "views"
        schema[bucket][name] = entry

    # Triggers (names only)
    triggers = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' ORDER BY name"
    ).fetchall()]
    if triggers:
        schema["triggers"] = triggers

    return schema


registry.register(
    name="db_query",
    description=(
        "SQLite query | inspect.\n"
        "- action='query' (default): exec SQL. Read-only unless write=true. Capped at `limit` (default 100).\n"
        "- action='inspect': full schema (tables, views, cols, types, PK, FK, indexes, row counts). No query needed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path":   {"type": "string",  "description": ".db | .sqlite | .sqlite3 path."},
            "action": {"type": "string",  "enum": ["query", "inspect"],
                       "description": "'query' (SQL) | 'inspect' (schema). Default 'query'."},
            "query":  {"type": "string",  "description": "SQL (required for query)."},
            "write":  {"type": "boolean", "description": "Allow writes (default false)."},
            "limit":  {"type": "integer", "description": "Max rows (default 100)."},
        },
        "required": ["path"],
    },
    execute=db_query,
)
