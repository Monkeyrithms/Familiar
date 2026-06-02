"""
Tool self-audit — tracks per-tool failures and triggers an analysis prompt
into a user-chosen conversation when a tool's error count crosses a threshold.

The audit prompt tells the agent to read the tool source, compare it with
the failure log, and propose schema/description improvements. The user
reviews findings in the target conversation and decides whether to apply.

Lifecycle:
  1. Agent catches a tool failure → log_failure()
  2. log_failure counts unaudited failures for that tool
  3. If count ≥ threshold → build prompt, inject into target conv, emit event
  4. User opens target conv, sees audit prompt, hits Enter to run
  5. Agent reads tool source, proposes changes, asks user to confirm
  6. On "yes": file_edit + hot_reload
"""

import json
import sqlite3
import time
import traceback as _tb
from pathlib import Path
from typing import Optional

AUDIT_DB_PATH = Path(__file__).parent.parent / "data" / "tool_audit.db"

# Error keywords that indicate an LLM-origin failure (bad args, missing
# params, JSON parse errors, type mismatches) vs infrastructure failures
# (network, filesystem, timeout) that shouldn't trigger an audit.
_LLM_ORIGIN_KEYWORDS = (
    "required", "missing", "invalid", "json", "type", "unexpected",
    "argument", "parameter", "key", "schema", "parse", "decode",
    "not a valid", "expected", "unrecognized", "unknown",
)
_INFRA_KEYWORDS = (
    "timeout", "timed out", "connection", "refused", "no such file",
    "filenotfound", "permissionerror", "enoent", "eacces",
    "502", "503", "504", "429", "rate limit", "disk full",
    "remotedisconnected", "bad gateway",
)


def _conn() -> sqlite3.Connection:
    AUDIT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(AUDIT_DB_PATH), timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_audit_db():
    conn = _conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_failures (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tool        TEXT NOT NULL,
            args_json   TEXT NOT NULL DEFAULT '{}',
            error_msg   TEXT NOT NULL DEFAULT '',
            traceback   TEXT NOT NULL DEFAULT '',
            model       TEXT NOT NULL DEFAULT '',
            conv_id     TEXT NOT NULL DEFAULT '',
            error_class TEXT NOT NULL DEFAULT 'other',
            audited     INTEGER NOT NULL DEFAULT 0,
            created_at  REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_tf_tool_unaudited
            ON tool_failures(tool, audited, created_at);

        CREATE TABLE IF NOT EXISTS audit_runs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tool        TEXT NOT NULL,
            failure_ids TEXT NOT NULL DEFAULT '[]',
            target_conv TEXT NOT NULL DEFAULT '',
            created_at  REAL NOT NULL
        );
    """)
    conn.close()


def classify_error(error_msg: str) -> str:
    """Classify an error as LLM-origin or infrastructure.
    Returns 'llm' or 'infra'."""
    low = (error_msg or "").lower()
    for kw in _INFRA_KEYWORDS:
        if kw in low:
            return "infra"
    for kw in _LLM_ORIGIN_KEYWORDS:
        if kw in low:
            return "llm"
    return "llm"  # default to LLM-origin (better to over-audit than miss)


def log_failure(
    tool: str,
    args: dict,
    error_msg: str,
    tb: str = "",
    model: str = "",
    conv_id: str = "",
) -> Optional[int]:
    """Record a tool failure. Returns the row ID, or None on error."""
    try:
        init_audit_db()
        error_class = classify_error(error_msg)
        conn = _conn()
        cur = conn.execute("""
            INSERT INTO tool_failures (tool, args_json, error_msg, traceback,
                                       model, conv_id, error_class, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            tool,
            json.dumps(args, ensure_ascii=False, default=str)[:4000],
            (error_msg or "")[:2000],
            (tb or "")[:4000],
            model or "",
            conv_id or "",
            error_class,
            time.time(),
        ))
        conn.commit()
        row_id = cur.lastrowid
        conn.close()
        return row_id
    except Exception as e:
        print(f"[tool_audit] log_failure error: {e}")
        return None


def count_unaudited(tool: str) -> int:
    """Count unaudited LLM-origin failures for a tool."""
    try:
        init_audit_db()
        conn = _conn()
        row = conn.execute("""
            SELECT COUNT(*) as cnt FROM tool_failures
            WHERE tool = ? AND audited = 0 AND error_class = 'llm'
        """, (tool,)).fetchone()
        conn.close()
        return row["cnt"] if row else 0
    except Exception:
        return 0


def get_unaudited_failures(tool: str, limit: int = 10) -> list[dict]:
    """Fetch recent unaudited LLM-origin failures for a tool."""
    try:
        init_audit_db()
        conn = _conn()
        rows = conn.execute("""
            SELECT id, tool, args_json, error_msg, traceback, model, conv_id, created_at
            FROM tool_failures
            WHERE tool = ? AND audited = 0 AND error_class = 'llm'
            ORDER BY created_at DESC LIMIT ?
        """, (tool, limit)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def mark_audited(failure_ids: list[int]):
    """Mark failures as included in an audit run."""
    if not failure_ids:
        return
    try:
        conn = _conn()
        placeholders = ",".join("?" for _ in failure_ids)
        conn.execute(
            f"UPDATE tool_failures SET audited = 1 WHERE id IN ({placeholders})",
            failure_ids,
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[tool_audit] mark_audited error: {e}")


def _build_audit_prompt(tool: str, failures: list[dict]) -> str:
    """Build the audit prompt to inject into the target conversation."""
    # Find the tool source file
    tools_dir = Path(__file__).parent.parent / "tools"
    source_file = tools_dir / f"{tool}.py"
    # Some tools have non-matching filenames
    if not source_file.exists():
        candidates = list(tools_dir.glob("*.py"))
        for c in candidates:
            if tool.replace("_", "") in c.stem.replace("_", ""):
                source_file = c
                break

    source_rel = str(source_file.relative_to(Path(__file__).parent.parent)) if source_file.exists() else f"tools/{tool}.py (NOT FOUND)"

    failure_entries = []
    for f in failures[:5]:
        entry = {
            "args": f.get("args_json", "{}"),
            "error": f.get("error_msg", ""),
            "traceback": f.get("traceback", "")[:500],
            "model": f.get("model", ""),
        }
        failure_entries.append(entry)

    failures_json = json.dumps(failure_entries, indent=2, ensure_ascii=False)

    return (
        f"[TOOL SELF-AUDIT] `{tool}` has {len(failures)} unaudited LLM-origin failures.\n\n"
        f"**Source file:** `{source_rel}`\n"
        f"**Registry block:** look for `registry.register(` in that file.\n\n"
        f"**Recent failures:**\n```json\n{failures_json}\n```\n\n"
        f"**Task:**\n"
        f"1. Read `{source_rel}` — focus on the `registry.register()` block (description + parameter descriptions)\n"
        f"2. Compare the tool's schema/instructions against the failure patterns above\n"
        f"3. Identify: vague instructions? missing examples? misleading param descriptions? "
        f"type mismatches? required-vs-optional confusion?\n"
        f"4. Propose specific text changes to the description/schema that would reduce these failures\n"
        f"5. Ask me before applying any fix\n\n"
        f"After I approve, use `file_edit` to update the tool and `hot_reload` to activate the changes."
    )


def maybe_trigger_audit(tool: str, config: dict) -> bool:
    """Check if a tool has crossed the audit threshold and fire if so.

    Returns True if an audit was triggered.
    """
    if not config.get("tool_audit_enabled", False):
        return False
    threshold = config.get("tool_audit_threshold", 3)
    target_conv = config.get("tool_audit_target_conv", "").strip()
    if not target_conv:
        return False

    count = count_unaudited(tool)
    if count < threshold:
        return False

    failures = get_unaudited_failures(tool, limit=threshold + 5)
    if not failures:
        return False

    prompt = _build_audit_prompt(tool, failures)

    # Inject the audit prompt into the target conversation
    try:
        from core.database import append_message_to_conversation
        success = append_message_to_conversation(
            target_conv, "user", prompt,
            meta={"_audit": True, "_audit_tool": tool},
        )
        if not success:
            print(f"[tool_audit] Failed to inject audit into conv {target_conv}")
            return False
    except Exception as e:
        print(f"[tool_audit] Injection error: {e}")
        return False

    # Mark these failures as audited
    failure_ids = [f["id"] for f in failures]
    mark_audited(failure_ids)

    # Record the audit run
    try:
        conn = _conn()
        conn.execute("""
            INSERT INTO audit_runs (tool, failure_ids, target_conv, created_at)
            VALUES (?, ?, ?, ?)
        """, (tool, json.dumps(failure_ids), target_conv, time.time()))
        conn.commit()
        conn.close()
    except Exception:
        pass

    # Emit event so UI can blink + alert
    try:
        from core.event_bus import bus
        bus.emit("audit.triggered", tool=tool, conv_id=target_conv)
    except Exception as e:
        print(f"[tool_audit] event emission error: {e}")

    print(f"[tool_audit] Audit triggered for '{tool}' → conv {target_conv[:8]}... "
          f"({len(failures)} failures)")
    return True
