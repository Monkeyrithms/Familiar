"""
JSON-path-aware structural editing — for JSON files AND SQLite JSON columns.

Editing JSON through raw-string file_edit is dangerous: one wrong brace on a
minified/large workflow file corrupts the whole thing, and on a watched inbox
file the app may re-ingest the corruption instantly. This tool edits the PARSE
TREE instead — locate by path, mutate, re-serialize — so brace/quote corruption
is impossible by construction.

Path syntax (a pragmatic JSONPath subset):
    $                      the root
    $.nodes               key 'nodes' on the root object
    $.nodes[3]            4th element of that array
    $.nodes[3].values.model
    $.edges[-1]           last element (negative indexing)
    $.edges[20:40]        slice (get only) — element-range fetch for big arrays
    nodes[0].id           leading '$.' is optional

Actions:
    get     — return the value at path (with array slicing for big arrays)
    set     — set path to `value` (JSON-encoded); creates missing dict keys
    append  — append `value` to the array at path
    delete  — remove the key/element at path

Targets (auto-detected):
    file  — path ends in .json or content sniffs as JSON
    db    — pass db_path + table + column (+ where) to edit a JSON cell in SQLite

Formatting: files round-trip with indent=2 (or compact if the original had no
newlines) and preserve key order. DB cells re-serialize compact by default.
"""

import json
import re
import sqlite3
from pathlib import Path
from tools.registry import registry


_TOKEN_RE = re.compile(r"""
    \.?(?P<key>[A-Za-z_][\w\-]*)      # .key  or  key
  | \[(?P<idx>-?\d+)\]                 # [3] or [-1]
  | \[(?P<slice>-?\d*:-?\d*)\]         # [20:40] [:10] [5:]
""", re.VERBOSE)

_SENTINEL = object()


def _parse_path(path: str):
    """Parse a path string into a list of steps.
    Each step is ('key', name) | ('idx', int) | ('slice', (start, stop))."""
    s = path.strip()
    if s in ("$", "", "."):
        return []
    if s.startswith("$"):
        s = s[1:]
    steps = []
    pos = 0
    while pos < len(s):
        if s[pos] == ".":
            pos += 1
            continue
        m = _TOKEN_RE.match(s, pos)
        if not m or m.start() != pos:
            raise ValueError(f"can't parse path near {s[pos:pos+12]!r}")
        if m.group("key") is not None:
            steps.append(("key", m.group("key")))
        elif m.group("idx") is not None:
            steps.append(("idx", int(m.group("idx"))))
        else:
            raw = m.group("slice")
            a, _, b = raw.partition(":")
            start = int(a) if a else None
            stop = int(b) if b else None
            steps.append(("slice", (start, stop)))
        pos = m.end()
    return steps


def _navigate(root, steps, create=False):
    """Walk to the PARENT of the final step. Returns (parent, last_step).
    With create=True, missing dict keys are created as we descend."""
    if not steps:
        return None, None
    cur = root
    for kind, val in steps[:-1]:
        if kind == "key":
            if not isinstance(cur, dict):
                raise ValueError(f"expected object to index by key '{val}', got {type(cur).__name__}")
            if val not in cur:
                if create:
                    cur[val] = {}
                else:
                    raise KeyError(f"key '{val}' not found")
            cur = cur[val]
        elif kind == "idx":
            if not isinstance(cur, list):
                raise ValueError(f"expected array to index by [{val}], got {type(cur).__name__}")
            cur = cur[val]  # IndexError propagates with a clear message
        else:
            raise ValueError("slices are only valid as the final path step (get only)")
    return cur, steps[-1]


def _get_at(root, steps):
    cur = root
    for kind, val in steps:
        if kind == "key":
            if not isinstance(cur, dict) or val not in cur:
                raise KeyError(f"key '{val}' not found")
            cur = cur[val]
        elif kind == "idx":
            if not isinstance(cur, list):
                raise ValueError(f"expected array for [{val}], got {type(cur).__name__}")
            cur = cur[val]
        else:
            if not isinstance(cur, list):
                raise ValueError(f"slice requires an array, got {type(cur).__name__}")
            start, stop = val
            cur = cur[start:stop]
    return cur


def _apply_mutation(root, steps, action, value):
    """Mutate root in place per action. Returns a short description."""
    if action == "set" and not steps:
        raise ValueError("cannot 'set' the root; pass a path like $.key")

    if action == "append":
        target = _get_at(root, steps) if steps else root
        if not isinstance(target, list):
            raise ValueError(f"append requires an array at path, got {type(target).__name__}")
        target.append(value)
        return f"appended 1 item (array now {len(target)} long)"

    parent, last = _navigate(root, steps, create=(action == "set"))
    kind, val = last

    if action == "set":
        if kind == "key":
            if not isinstance(parent, dict):
                raise ValueError(f"expected object to set key '{val}'")
            existed = val in parent
            parent[val] = value
            return f"{'updated' if existed else 'created'} key '{val}'"
        elif kind == "idx":
            if not isinstance(parent, list):
                raise ValueError(f"expected array to set [{val}]")
            parent[val] = value
            return f"set element [{val}]"
        raise ValueError("cannot 'set' a slice")

    if action == "delete":
        if kind == "key":
            if not isinstance(parent, dict) or val not in parent:
                raise KeyError(f"key '{val}' not found")
            del parent[val]
            return f"deleted key '{val}'"
        elif kind == "idx":
            if not isinstance(parent, list):
                raise ValueError(f"expected array to delete [{val}]")
            del parent[val]
            return f"deleted element [{val}] (array now {len(parent)} long)"
        raise ValueError("cannot 'delete' a slice")

    raise ValueError(f"unknown action '{action}'")


def _load_value(value, value_raw):
    """Resolve the value to set/append. `value` is already-parsed (preferred);
    `value_raw` is a JSON string to parse. Either may be provided."""
    if value is not _SENTINEL:
        return value
    if value_raw is not None:
        try:
            return json.loads(value_raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"value is not valid JSON: {e}. "
                             "To set a literal string, pass it quoted, e.g. value=\"\\\"hello\\\"\".")
    raise ValueError("set/append require a 'value' (parsed) or 'value_raw' (JSON string)")


def json_edit(path: str = "", json_path: str = "$", action: str = "get",
              value=_SENTINEL, value_raw: str = None,
              db_path: str = "", table: str = "", column: str = "",
              where: str = "", backup: bool = False) -> str:
    """Structurally edit JSON in a file or a SQLite JSON column. See module docstring."""
    is_db = bool(db_path)
    try:
        steps = _parse_path(json_path)
    except ValueError as e:
        return json.dumps({"error": f"bad json_path: {e}"})

    # ---- load ----
    if is_db:
        dbp = Path(db_path)
        if not dbp.exists():
            return json.dumps({"error": f"database not found: {db_path}"})
        if not (table and column):
            return json.dumps({"error": "db edit requires both 'table' and 'column'"})
        try:
            conn = sqlite3.connect(str(dbp), timeout=5)
            conn.row_factory = sqlite3.Row
            sql = f"SELECT [{column}] FROM [{table}]"
            if where:
                sql += f" WHERE {where}"
            rows = conn.execute(sql).fetchall()
        except Exception as e:
            return json.dumps({"error": f"db read failed: {e}"})
        if not rows:
            conn.close()
            return json.dumps({"error": "no rows matched (check 'where')"})
        if len(rows) > 1 and action != "get":
            conn.close()
            return json.dumps({"error": f"{len(rows)} rows matched — refusing to mutate more than "
                                        "one cell. Tighten 'where' to target a single row."})
        raw = rows[0][0]
        try:
            doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (json.JSONDecodeError, TypeError) as e:
            conn.close()
            return json.dumps({"error": f"cell is not valid JSON: {e}"})
    else:
        if not path:
            return json.dumps({"error": "pass 'path' (a JSON file) or 'db_path'+'table'+'column'"})
        fp = Path(path)
        if not fp.exists():
            return json.dumps({"error": f"file not found: {path}"})
        try:
            text = fp.read_text(encoding="utf-8")
            doc = json.loads(text)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"file is not valid JSON: {e}"})
        except Exception as e:
            return json.dumps({"error": f"could not read file: {e}"})
        compact = "\n" not in text.strip()

    # ---- get ----
    if action == "get":
        if is_db:
            conn.close()
        try:
            result = _get_at(doc, steps)
        except (KeyError, IndexError, ValueError) as e:
            return json.dumps({"error": f"path not found: {e}"})
        out = {"value": result}
        if isinstance(result, list):
            out["length"] = len(result)
        return json.dumps(out, ensure_ascii=False, default=str)

    # ---- mutate ----
    if action in ("set", "append"):
        try:
            val = _load_value(value, value_raw)
        except ValueError as e:
            if is_db:
                conn.close()
            return json.dumps({"error": str(e)})
    else:
        val = None

    try:
        desc = _apply_mutation(doc, steps, action, val)
    except (KeyError, IndexError, ValueError) as e:
        if is_db:
            conn.close()
        return json.dumps({"error": f"{action} failed at {json_path}: {e}"})

    # ---- persist ----
    if is_db:
        try:
            if backup:
                _backup_db(dbp)
            new_blob = json.dumps(doc, ensure_ascii=False)
            usql = f"UPDATE [{table}] SET [{column}] = ?"
            if where:
                usql += f" WHERE {where}"
            conn.execute(usql, (new_blob,))
            conn.commit()
            conn.close()
        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            return json.dumps({"error": f"db write failed: {e}"})
        return json.dumps({"success": True, "detail": desc,
                           "target": f"{table}.{column}"})
    else:
        from core.checkpoints import checkpoint_manager
        checkpoint_manager.ensure_checkpoint(str(fp.parent), "before json_edit")
        try:
            if compact:
                new_text = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
            else:
                new_text = json.dumps(doc, ensure_ascii=False, indent=2)
                new_text += "\n"
            from tools.lint import safe_write_text
            err = safe_write_text(path, new_text)
            if err:
                return json.dumps({"error": err})
            from core.event_bus import bus
            bus.emit("file.changed", path=path, tool="json_edit", original=text)
        except Exception as e:
            return json.dumps({"error": f"write failed: {e}"})
        return json.dumps({"success": True, "detail": desc, "path": path})


def _backup_db(dbp: Path):
    """Snapshot a SQLite file next to itself before a mutation."""
    import shutil
    import time
    bak = dbp.with_suffix(dbp.suffix + f".bak_{int(time.time())}")
    shutil.copy2(dbp, bak)
    return bak


registry.register(
    name="json_edit",
    description=(
        "Structural JSON editing by path — for JSON FILES and SQLite JSON COLUMNS. "
        "Edits the parse tree (no brace/quote corruption possible). "
        "Path: $.nodes[3].values.model, $.edges[-1], $.edges[20:40] (slice, get only). "
        "Actions: get | set | append | delete. "
        "File: pass `path`. DB cell: pass `db_path`+`table`+`column`(+`where`). "
        "✓ the safe way to add an edge / flip one field in a big workflow JSON."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "JSON file path (file mode)."},
            "json_path": {"type": "string",
                          "description": "Path within the doc, e.g. $.nodes[3].values.model. Default $ (root)."},
            "action": {"type": "string", "enum": ["get", "set", "append", "delete"],
                       "description": "get | set | append | delete. Default get."},
            "value": {"description": "Value for set/append (already-typed JSON: object/array/number/etc.)."},
            "value_raw": {"type": "string",
                          "description": "Value for set/append as a JSON STRING (parsed). Use when value can't be passed typed."},
            "db_path": {"type": "string", "description": "SQLite path (DB mode)."},
            "table": {"type": "string", "description": "Table name (DB mode)."},
            "column": {"type": "string", "description": "JSON column name (DB mode)."},
            "where": {"type": "string",
                      "description": "WHERE clause to target ONE row (DB mode). Mutations refuse >1 match."},
            "backup": {"type": "boolean",
                       "description": "DB mode: snapshot the .db file before writing. Default false."},
        },
        "required": [],
    },
    execute=json_edit,
)
