"""
Filesystem race guard — detect when a file is changed/deleted/recreated by
ANOTHER process between the agent's reads and writes.

Motivating bug: a workflow inbox silently deleted JSON files seconds after the
agent wrote them (ingest-and-delete vault pattern). Every write "succeeded" and
nothing flagged that something else owned the file — so the agent re-edited the
wrong store for half an hour. A cheap "this path was removed/replaced by another
process N seconds after your last write" signal would have redirected it
immediately.

How it works: we stamp (mtime, size, inode) every time a tool reads or writes a
path. On the NEXT touch of that same path, if the on-disk stamp doesn't match
what we last left there, something external changed it — we surface a one-line
warning. Inode change or vanished-then-reappeared = a strong "another process
owns this file" tell (classic inbox/vault behavior).

This is advisory only — it never blocks a write. It just breaks the silence.
"""

import os
from pathlib import Path

# path -> (mtime_ns, size, inode) as we last left it
_STAMPS: dict[str, tuple] = {}


def _stat(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size, getattr(st, "st_ino", 0))
    except OSError:
        return None


def check_external_change(path: str) -> str:
    """Compare on-disk state to what we last recorded. Return a human warning
    string if another process changed it since, else ''. Does NOT update the
    stamp — call record_write/record_read for that."""
    key = str(Path(path))
    prev = _STAMPS.get(key)
    if prev is None:
        return ""  # never touched it through a tool — nothing to compare
    cur = _stat(key)
    if cur is None:
        return (f"NOTE: '{path}' was DELETED by another process since your last "
                "write — something owns this file (e.g. an ingest-and-delete "
                "inbox). The real data likely lives elsewhere.")
    if cur == prev:
        return ""
    prev_mtime, prev_size, prev_ino = prev
    cur_mtime, cur_size, cur_ino = cur
    if prev_ino and cur_ino and cur_ino != prev_ino:
        return (f"NOTE: '{path}' was REPLACED by another process (new file in "
                "its place) since your last write — strong sign another process "
                "owns this path (delete-and-recreate inbox/vault pattern).")
    return (f"NOTE: '{path}' was modified by another process since your last "
            "write (mtime/size changed) — your edits may be racing another "
            "writer. Re-read before trusting the contents.")


def record_write(path: str) -> None:
    """Stamp the file as we just left it (call after a successful write)."""
    cur = _stat(path)
    if cur is not None:
        _STAMPS[str(Path(path))] = cur


def record_read(path: str) -> None:
    """Stamp on read so a later write can detect drift since this read."""
    cur = _stat(path)
    if cur is not None:
        _STAMPS[str(Path(path))] = cur
