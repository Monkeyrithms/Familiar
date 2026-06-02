"""
Code index — per-workspace semantic index over code files.

Architecture:
  - AST-aware chunking via tree-sitter for Python/JS/TS/Go/Rust.
    Text fallback (paragraphs + hard line limits) for other file types.
  - Per-workspace SQLite+sqlite-vec database (isolated namespaces, so
    indexing two different projects doesn't mix their contents).
  - Incremental updates: every file stores mtime + content hash. Re-index
    of a workspace skips unchanged files; single-file updates (from the
    event bus) re-chunk only that file and swap its chunks atomically.

See tools/vector_search.py for the agent-callable interface.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from core.embeddings import (
    EMBED_DIMS, embed_batch, load_sqlite_vec, vec_to_bytes,
)


# ── Paths ───────────────────────────────────────────────────────────────

_DATA_DIR = Path(__file__).parent.parent / "data" / "vector_indexes"
_REGISTRY_PATH = _DATA_DIR / "_registry.db"


def _ensure_data_dir() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Language map ────────────────────────────────────────────────────────

# Extension -> tree-sitter language name. Extensions NOT listed here fall
# through to the text chunker, which is fine for markdown/plain text.
_LANG_BY_EXT: dict[str, str] = {
    ".py":   "python",
    ".pyi":  "python",
    ".js":   "javascript",
    ".mjs":  "javascript",
    ".cjs":  "javascript",
    ".jsx":  "javascript",
    ".ts":   "typescript",
    ".tsx":  "tsx",
    ".go":   "go",
    ".rs":   "rust",
}

# Tree-sitter node types that represent "a complete, indexable unit" — a
# function, a class, a method, a struct definition. We walk the parse tree
# and emit one chunk per matched node, which keeps the chunk boundary on a
# syntactic boundary (never mid-statement) and matches how humans navigate code.
_CHUNKABLE_NODES: dict[str, frozenset[str]] = {
    "python": frozenset({
        "function_definition", "class_definition",
        "decorated_definition",  # @decorator + def → treat as one unit
    }),
    "javascript": frozenset({
        "function_declaration", "class_declaration",
        "method_definition", "arrow_function",
        "generator_function_declaration",
    }),
    "typescript": frozenset({
        "function_declaration", "class_declaration",
        "method_definition", "method_signature",
        "interface_declaration", "type_alias_declaration",
        "enum_declaration",
    }),
    "tsx": frozenset({
        "function_declaration", "class_declaration",
        "method_definition", "method_signature",
        "interface_declaration", "type_alias_declaration",
        "enum_declaration",
    }),
    "go": frozenset({
        "function_declaration", "method_declaration",
        "type_declaration",
    }),
    "rust": frozenset({
        "function_item", "impl_item", "struct_item",
        "enum_item", "trait_item", "mod_item",
    }),
}

# Hard chunk-size limits. A single function longer than this gets split at
# newline boundaries. ~2000 chars is roughly ~500 tokens, matching Cursor's
# target chunk size.
_MAX_CHUNK_CHARS = 2000
_SOFT_CHUNK_CHARS = 1500    # preferred ceiling; splits above this
_MIN_CHUNK_CHARS = 40       # don't index trivially small things


@dataclass
class Chunk:
    text: str
    line_start: int         # 1-based inclusive
    line_end: int           # 1-based inclusive
    kind: str               # "function", "class", "method", "text", etc.
    name: str = ""


# ── Chunking ────────────────────────────────────────────────────────────

def detect_language(path: str) -> str | None:
    """Return the tree-sitter language name for a file extension, or None."""
    ext = Path(path).suffix.lower()
    return _LANG_BY_EXT.get(ext)


def chunk_file(path: str, text: str) -> list[Chunk]:
    """Split *text* into semantic chunks. Uses tree-sitter when possible,
    falls back to paragraph/line chunking for non-code files."""
    lang = detect_language(path)
    if lang is None:
        return _text_chunker(text)
    try:
        return _ast_chunker(text, lang)
    except Exception:
        # tree-sitter failure (corrupt file, parser bug) — don't lose the
        # file entirely, degrade to text chunker.
        return _text_chunker(text)


def _ast_chunker(src: str, lang_name: str) -> list[Chunk]:
    """Walk the tree-sitter parse tree and emit one chunk per function/class."""
    from tree_sitter_language_pack import get_parser

    parser = get_parser(lang_name)
    tree = parser.parse(src.encode("utf-8", errors="replace"))
    chunkable = _CHUNKABLE_NODES.get(lang_name, frozenset())
    src_bytes = src.encode("utf-8", errors="replace")
    src_lines = src.splitlines()

    chunks: list[Chunk] = []
    covered_ranges: list[tuple[int, int]] = []  # (start_byte, end_byte) already emitted

    def byte_range_to_lines(start_byte: int, end_byte: int) -> tuple[int, int]:
        # tree-sitter gives us byte offsets; convert to 1-based line numbers
        prefix = src_bytes[:start_byte].decode("utf-8", errors="replace")
        line_start = prefix.count("\n") + 1
        segment = src_bytes[start_byte:end_byte].decode("utf-8", errors="replace")
        line_end = line_start + max(segment.count("\n"), 0)
        return line_start, line_end

    def walk(node) -> None:
        if node.type in chunkable:
            start_byte = node.start_byte
            end_byte = node.end_byte
            # Skip if already inside another emitted chunk (avoid double-indexing
            # a method that lives inside an already-emitted class).
            for s, e in covered_ranges:
                if s <= start_byte and end_byte <= e:
                    break
            else:
                text = src_bytes[start_byte:end_byte].decode(
                    "utf-8", errors="replace"
                )
                if len(text) >= _MIN_CHUNK_CHARS:
                    line_start, line_end = byte_range_to_lines(start_byte, end_byte)
                    name = _extract_node_name(node, src_bytes) or ""
                    kind = _normalize_kind(node.type)
                    # Split oversized chunks (e.g. a 400-line god-function)
                    if len(text) > _MAX_CHUNK_CHARS:
                        for sub in _split_long(text, line_start, kind, name):
                            chunks.append(sub)
                    else:
                        chunks.append(Chunk(
                            text=text, line_start=line_start, line_end=line_end,
                            kind=kind, name=name,
                        ))
                    covered_ranges.append((start_byte, end_byte))
                    return  # don't recurse into an already-emitted chunk
        for child in node.children:
            walk(child)

    walk(tree.root_node)

    # Whatever wasn't covered by a function/class chunk — module-level code,
    # imports, globals, top-level statements — gets captured as a single
    # "module" chunk so we don't lose it entirely.
    if not chunks:
        # No chunkable nodes found — the whole file is module-level. Fall
        # back to text chunker so we still get SOMETHING indexed.
        return _text_chunker(src)
    module_text = _extract_uncovered(src_bytes, covered_ranges)
    if module_text.strip() and len(module_text) >= _MIN_CHUNK_CHARS:
        chunks.insert(0, Chunk(
            text=module_text, line_start=1,
            line_end=len(src_lines), kind="module", name="",
        ))

    return chunks


def _extract_node_name(node, src_bytes: bytes) -> str | None:
    """Best-effort: find the `identifier` child of a function/class node and
    return its text. Works for most grammars; returns None on failure."""
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            return src_bytes[child.start_byte:child.end_byte].decode(
                "utf-8", errors="replace"
            )
        # Decorated definitions wrap the real def — recurse one level
        if child.type in ("function_definition", "class_definition",
                          "function_declaration", "class_declaration"):
            name = _extract_node_name(child, src_bytes)
            if name:
                return name
    return None


def _normalize_kind(node_type: str) -> str:
    if "class" in node_type:
        return "class"
    if "method" in node_type:
        return "method"
    if "function" in node_type or "arrow_function" in node_type:
        return "function"
    if "struct" in node_type:
        return "struct"
    if "enum" in node_type:
        return "enum"
    if "interface" in node_type:
        return "interface"
    if "trait" in node_type:
        return "trait"
    if "impl" in node_type:
        return "impl"
    if "type" in node_type:
        return "type"
    if "mod" in node_type:
        return "module"
    return node_type


def _split_long(text: str, start_line: int, kind: str, name: str) -> list[Chunk]:
    """Break an oversized chunk into sub-chunks at blank-line boundaries."""
    lines = text.splitlines()
    subs: list[Chunk] = []
    buf: list[str] = []
    buf_start = start_line
    for i, line in enumerate(lines):
        buf.append(line)
        joined_len = sum(len(l) + 1 for l in buf)
        if joined_len >= _SOFT_CHUNK_CHARS and (not line.strip() or joined_len >= _MAX_CHUNK_CHARS):
            subs.append(Chunk(
                text="\n".join(buf),
                line_start=buf_start,
                line_end=buf_start + len(buf) - 1,
                kind=f"{kind}_part", name=name,
            ))
            buf = []
            buf_start = start_line + i + 1
    if buf:
        subs.append(Chunk(
            text="\n".join(buf),
            line_start=buf_start,
            line_end=buf_start + len(buf) - 1,
            kind=f"{kind}_part", name=name,
        ))
    return subs


def _extract_uncovered(src_bytes: bytes, covered: list[tuple[int, int]]) -> str:
    """Return everything in src_bytes NOT inside one of the covered ranges,
    joined with newlines. Used to capture module-level imports / globals."""
    if not covered:
        return src_bytes.decode("utf-8", errors="replace")
    covered = sorted(covered)
    parts: list[bytes] = []
    cursor = 0
    for s, e in covered:
        if s > cursor:
            parts.append(src_bytes[cursor:s])
        cursor = max(cursor, e)
    if cursor < len(src_bytes):
        parts.append(src_bytes[cursor:])
    return b"".join(parts).decode("utf-8", errors="replace")


def _text_chunker(text: str) -> list[Chunk]:
    """Paragraph-aware text chunker for non-code files. Tries to keep
    paragraphs together; splits at hard limits."""
    if not text.strip():
        return []
    paragraphs = text.split("\n\n")
    chunks: list[Chunk] = []
    buf: list[str] = []
    buf_len = 0
    buf_line_start = 1
    current_line = 1

    def flush(end_line: int) -> None:
        if not buf:
            return
        blob = "\n\n".join(buf).strip()
        if len(blob) >= _MIN_CHUNK_CHARS:
            chunks.append(Chunk(
                text=blob, line_start=buf_line_start,
                line_end=end_line, kind="text", name="",
            ))

    for p in paragraphs:
        p_lines = p.count("\n") + 1
        p_len = len(p)
        if buf and buf_len + p_len > _MAX_CHUNK_CHARS:
            flush(current_line - 1)
            buf = []
            buf_len = 0
            buf_line_start = current_line
        buf.append(p)
        buf_len += p_len + 2
        current_line += p_lines + 1  # +1 for the blank line separator

    flush(current_line - 1)
    return chunks


# ── Workspace registry ──────────────────────────────────────────────────

class WorkspaceRegistry:
    """Tracks which workspaces have been indexed, so the event-bus
    auto-indexer can find the right workspace for a changed file."""

    def __init__(self):
        self._lock = threading.Lock()

    def _conn(self) -> sqlite3.Connection:
        _ensure_data_dir()
        conn = sqlite3.connect(str(_REGISTRY_PATH))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                name         TEXT PRIMARY KEY,
                root_path    TEXT UNIQUE NOT NULL,
                created_at   REAL,
                last_indexed REAL,
                chunk_count  INTEGER DEFAULT 0
            )
        """)
        return conn

    def register(self, name: str, root_path: str) -> None:
        norm = os.path.abspath(root_path)
        with self._lock, self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO workspaces "
                "(name, root_path, created_at) VALUES (?, ?, ?)",
                (name, norm, time.time()),
            )

    def list_all(self) -> list[dict]:
        with self._lock, self._conn() as conn:
            rows = conn.execute(
                "SELECT name, root_path, created_at, last_indexed, chunk_count "
                "FROM workspaces ORDER BY name"
            ).fetchall()
        return [
            {"name": r[0], "root_path": r[1], "created_at": r[2],
             "last_indexed": r[3], "chunk_count": r[4]}
            for r in rows
        ]

    def find_for_file(self, file_path: str) -> dict | None:
        """Find the deepest-matching registered workspace that contains
        *file_path*. Deepest wins so nested workspaces disambiguate correctly."""
        abs_path = os.path.abspath(file_path)
        best = None
        for ws in self.list_all():
            root = ws["root_path"]
            try:
                # Use Path to get proper path-component matching, not string prefix
                # (so /foo/barx doesn't match a workspace at /foo/bar).
                Path(abs_path).relative_to(root)
            except ValueError:
                continue
            if best is None or len(root) > len(best["root_path"]):
                best = ws
        return best

    def delete(self, name: str) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("DELETE FROM workspaces WHERE name=?", (name,))

    def mark_indexed(self, name: str, chunk_count: int) -> None:
        with self._lock, self._conn() as conn:
            conn.execute(
                "UPDATE workspaces SET last_indexed=?, chunk_count=? WHERE name=?",
                (time.time(), chunk_count, name),
            )


registry = WorkspaceRegistry()


# ── FTS5 query tokenizer ────────────────────────────────────────────────

def _tokenize_query(query: str) -> list[str]:
    """Split a user query into FTS5-safe tokens. Keep identifier-like runs
    (letters/digits/underscore), drop punctuation and stopwords so FTS5 doesn't
    try to parse `authenticate_user()` as a subquery."""
    import re as _re
    toks = _re.findall(r"[A-Za-z0-9_]+", query)
    # Very short tokens and common English stopwords hurt BM25 signal on code
    stop = {"a", "an", "the", "of", "for", "to", "in", "on", "is", "are",
            "how", "do", "i", "what", "where", "when", "and", "or"}
    return [t for t in toks if len(t) > 1 and t.lower() not in stop]


# ── The index itself ────────────────────────────────────────────────────

# Default glob patterns — favor code + docs, skip binaries and dep trees.
DEFAULT_PATTERNS = (
    "*.py", "*.pyi",
    "*.js", "*.mjs", "*.cjs", "*.jsx",
    "*.ts", "*.tsx",
    "*.go", "*.rs",
    "*.md", "*.txt", "*.rst",
    "*.json", "*.yaml", "*.yml", "*.toml",
)

_IGNORE_DIRS = frozenset({
    ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "venv", ".env", "env",
    "dist", "build", "target", "out", ".next", ".nuxt",
    "data",  # agent's own embeddings DB — don't index ourselves
})


class CodeIndex:
    """Per-workspace code index. One SQLite file per workspace."""

    def __init__(self, name: str, root: str):
        self.name = name
        self.root = os.path.abspath(root)
        _ensure_data_dir()
        self.db_path = _DATA_DIR / f"{name}.db"

    # ── connection & schema ──

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        vec_ok = load_sqlite_vec(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS files (
                path         TEXT PRIMARY KEY,
                mtime        REAL NOT NULL,
                content_hash TEXT NOT NULL,
                language     TEXT,
                indexed_at   REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path   TEXT NOT NULL,
                text        TEXT NOT NULL,
                line_start  INTEGER,
                line_end    INTEGER,
                kind        TEXT,
                name        TEXT,
                FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # FTS5: BM25 keyword search over chunk text. External-content table so
        # we don't duplicate storage — FTS points back at chunks.text by rowid.
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(text, name, content='chunks', content_rowid='id',
                       tokenize='unicode61')
        """)

        # Dimension check: if the vec table was created with a different size
        # than the current config, refuse to mix. The user must `reindex`.
        if vec_ok:
            stored = conn.execute(
                "SELECT value FROM meta WHERE key='embed_dims'"
            ).fetchone()
            if stored is None:
                try:
                    conn.execute(
                        f"CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec "
                        f"USING vec0(embedding float[{EMBED_DIMS}])"
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO meta (key, value) VALUES "
                        "('embed_dims', ?)", (str(EMBED_DIMS),),
                    )
                except Exception:
                    pass
            else:
                prior = int(stored[0])
                if prior != EMBED_DIMS:
                    raise RuntimeError(
                        f"Index '{self.name}' was built with {prior}-dim "
                        f"embeddings; current config uses {EMBED_DIMS}-dim. "
                        f"Run: vector_search action=reindex name={self.name}"
                    )
        # Store the workspace root so it's recoverable if the registry gets lost
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('root_path', ?)",
            (self.root,),
        )
        conn.commit()
        return conn

    # ── file discovery + hashing ──

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()

    def _iter_files(self, patterns: Iterable[str]) -> Iterable[Path]:
        root = Path(self.root)
        seen: set[str] = set()
        for pat in patterns:
            for fp in root.rglob(pat):
                if not fp.is_file():
                    continue
                if any(part in _IGNORE_DIRS for part in fp.parts):
                    continue
                key = str(fp.resolve())
                if key in seen:
                    continue
                seen.add(key)
                yield fp

    # ── full index (walk workspace, incremental per file) ──

    def reindex(self, patterns: Iterable[str] = DEFAULT_PATTERNS) -> dict:
        """Scan the workspace. For each file: skip if (mtime, hash) unchanged.
        Otherwise replace its chunks with freshly computed ones. Removes
        records for files that no longer exist on disk."""
        stats = {
            "scanned": 0, "unchanged": 0, "updated": 0, "added": 0,
            "removed": 0, "chunks_embedded": 0, "errors": [],
        }
        conn = self._connect()
        try:
            known = {
                row[0]: (row[1], row[2])
                for row in conn.execute(
                    "SELECT path, mtime, content_hash FROM files"
                ).fetchall()
            }
            on_disk: set[str] = set()

            pending: list[tuple[str, str, str, float, list[Chunk]]] = []
            for fp in self._iter_files(patterns):
                stats["scanned"] += 1
                try:
                    rel = str(fp.relative_to(self.root)).replace("\\", "/")
                except ValueError:
                    continue
                on_disk.add(rel)

                try:
                    mtime = fp.stat().st_mtime
                    text = fp.read_text(encoding="utf-8", errors="replace")
                except Exception as e:
                    stats["errors"].append(f"{rel}: {e}")
                    continue

                content_hash = self._hash_text(text)
                previous = known.get(rel)
                if previous and abs(previous[0] - mtime) < 1e-3 and previous[1] == content_hash:
                    stats["unchanged"] += 1
                    continue

                chunks = chunk_file(str(fp), text)
                if not chunks:
                    continue
                lang = detect_language(str(fp)) or "text"
                pending.append((rel, content_hash, lang, mtime, chunks))
                if previous:
                    stats["updated"] += 1
                else:
                    stats["added"] += 1

            # Delete rows for files that disappeared
            to_remove = [p for p in known if p not in on_disk]
            for rel in to_remove:
                self._delete_file_chunks(conn, rel)
                stats["removed"] += 1

            # Apply updates in batches (embed N at a time to keep API calls efficient)
            BATCH = 64
            batch_buf: list[tuple[str, Chunk]] = []
            def flush_batch():
                if not batch_buf:
                    return
                texts = [c.text for _, c in batch_buf]
                embeddings = embed_batch(texts)
                for (rel, chunk), emb in zip(batch_buf, embeddings):
                    cur = conn.execute(
                        "INSERT INTO chunks "
                        "(file_path, text, line_start, line_end, kind, name) "
                        "VALUES (?,?,?,?,?,?)",
                        (rel, chunk.text, chunk.line_start, chunk.line_end,
                         chunk.kind, chunk.name),
                    )
                    # FTS5 external-content sync (keyword index)
                    try:
                        conn.execute(
                            "INSERT INTO chunks_fts(rowid, text, name) VALUES (?,?,?)",
                            (cur.lastrowid, chunk.text, chunk.name or ""),
                        )
                    except Exception:
                        pass
                    if emb:
                        try:
                            conn.execute(
                                "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                                (cur.lastrowid, vec_to_bytes(emb)),
                            )
                            stats["chunks_embedded"] += 1
                        except Exception:
                            pass
                batch_buf.clear()

            for rel, content_hash, lang, mtime, chunks in pending:
                # Wipe old chunks for this file first (replace, not merge)
                self._delete_file_chunks(conn, rel)
                conn.execute(
                    "INSERT OR REPLACE INTO files "
                    "(path, mtime, content_hash, language, indexed_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rel, mtime, content_hash, lang, time.time()),
                )
                for chunk in chunks:
                    batch_buf.append((rel, chunk))
                    if len(batch_buf) >= BATCH:
                        flush_batch()
            flush_batch()
            conn.commit()

            # Update the workspace registry
            total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            registry.mark_indexed(self.name, total)
            stats["total_chunks"] = total
        finally:
            conn.close()
        return stats

    def _delete_file_chunks(self, conn: sqlite3.Connection, rel: str) -> None:
        """Delete a file's row from `files` + all of its chunks + embeddings + FTS."""
        rows = conn.execute(
            "SELECT id, text, name FROM chunks WHERE file_path=?", (rel,)
        ).fetchall()
        for cid, text, name in rows:
            try:
                conn.execute("DELETE FROM chunks_vec WHERE rowid=?", (cid,))
            except Exception:
                pass
            # FTS5 external-content tables need manual sync via delete command
            try:
                conn.execute(
                    "INSERT INTO chunks_fts(chunks_fts, rowid, text, name) "
                    "VALUES('delete', ?, ?, ?)",
                    (cid, text, name or ""),
                )
            except Exception:
                pass
        conn.execute("DELETE FROM chunks WHERE file_path=?", (rel,))
        conn.execute("DELETE FROM files WHERE path=?", (rel,))

    # ── single-file incremental update (used by event-bus subscriber) ──

    def update_file(self, abs_path: str) -> dict:
        """Re-chunk and re-embed a single file. Skip if the file is unchanged
        since the last index. Safe to call from multiple threads — the
        SQLite connection is short-lived and the DELETE/INSERT is atomic."""
        try:
            rel = str(Path(abs_path).resolve().relative_to(self.root)).replace("\\", "/")
        except ValueError:
            return {"skipped": True, "reason": "outside workspace"}

        p = Path(abs_path)
        if not p.is_file():
            # File deleted — remove from index
            with self._connect() as conn:
                self._delete_file_chunks(conn, rel)
                conn.commit()
            return {"removed": rel}

        try:
            mtime = p.stat().st_mtime
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            return {"error": str(e)}

        content_hash = self._hash_text(text)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT mtime, content_hash FROM files WHERE path=?", (rel,)
            ).fetchone()
            if row and abs(row[0] - mtime) < 1e-3 and row[1] == content_hash:
                return {"unchanged": rel}

            chunks = chunk_file(abs_path, text)
            self._delete_file_chunks(conn, rel)
            if not chunks:
                conn.commit()
                return {"removed": rel, "reason": "no chunks"}

            lang = detect_language(abs_path) or "text"
            conn.execute(
                "INSERT OR REPLACE INTO files "
                "(path, mtime, content_hash, language, indexed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (rel, mtime, content_hash, lang, time.time()),
            )

            texts = [c.text for c in chunks]
            embeddings = embed_batch(texts)
            embedded = 0
            for chunk, emb in zip(chunks, embeddings):
                cur = conn.execute(
                    "INSERT INTO chunks "
                    "(file_path, text, line_start, line_end, kind, name) "
                    "VALUES (?,?,?,?,?,?)",
                    (rel, chunk.text, chunk.line_start, chunk.line_end,
                     chunk.kind, chunk.name),
                )
                try:
                    conn.execute(
                        "INSERT INTO chunks_fts(rowid, text, name) VALUES (?,?,?)",
                        (cur.lastrowid, chunk.text, chunk.name or ""),
                    )
                except Exception:
                    pass
                if emb:
                    try:
                        conn.execute(
                            "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                            (cur.lastrowid, vec_to_bytes(emb)),
                        )
                        embedded += 1
                    except Exception:
                        pass
            conn.commit()
            total = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            registry.mark_indexed(self.name, total)
            return {
                "updated": rel, "chunks": len(chunks),
                "embedded": embedded, "total_chunks": total,
            }
        finally:
            conn.close()

    # ── search ──

    def search(self, query: str, limit: int = 10,
               kind_filter: str | None = None,
               mode: str = "hybrid") -> list[dict]:
        """Search indexed chunks. Modes:

            hybrid   — BM25 keyword + vector, fused via Reciprocal Rank Fusion.
                       Best balance: catches exact names (parseJSON) AND semantic
                       matches (authentication flow). Default.
            vector   — pure embedding similarity. Best for fuzzy / conceptual queries.
            keyword  — pure BM25. Best when you know an exact identifier.

        `kind_filter` restricts to functions / classes / etc.
        """
        mode = (mode or "hybrid").lower()
        conn = self._connect()
        try:
            conn.row_factory = sqlite3.Row
            # Pull a wider pool from each ranker before fusing, then trim to limit.
            pool = max(limit * 4, 20)

            vec_rows = (
                self._vector_search(conn, query, pool, kind_filter)
                if mode in ("hybrid", "vector") else []
            )
            fts_rows = (
                self._keyword_search(conn, query, pool, kind_filter)
                if mode in ("hybrid", "keyword") else []
            )
        finally:
            conn.close()

        return self._fuse_results(vec_rows, fts_rows, mode, limit)

    @staticmethod
    def _vector_search(conn: sqlite3.Connection, query: str, pool: int,
                       kind_filter: str | None) -> list[sqlite3.Row]:
        from core.embeddings import embed_text
        query_emb = embed_text(query)
        if not query_emb:
            return []
        sql = """
            SELECT c.id, c.file_path, c.text, c.line_start, c.line_end,
                   c.kind, c.name, v.distance
            FROM chunks_vec v
            JOIN chunks c ON c.id = v.rowid
            WHERE v.embedding MATCH ? AND k = ?
        """
        params: list = [vec_to_bytes(query_emb), pool]
        if kind_filter:
            sql += " AND c.kind = ?"
            params.append(kind_filter)
        sql += " ORDER BY v.distance LIMIT ?"
        params.append(pool)
        try:
            return conn.execute(sql, params).fetchall()
        except Exception:
            return []

    @staticmethod
    def _keyword_search(conn: sqlite3.Connection, query: str, pool: int,
                        kind_filter: str | None) -> list[sqlite3.Row]:
        # FTS5 MATCH is picky about punctuation — escape-wrap each token with
        # double quotes so identifiers like `camelCase` / `snake_case` work.
        tokens = [t for t in _tokenize_query(query) if t]
        if not tokens:
            return []
        fts_q = " OR ".join(f'"{t}"' for t in tokens)
        sql = """
            SELECT c.id, c.file_path, c.text, c.line_start, c.line_end,
                   c.kind, c.name, bm25(chunks_fts) AS bm25_score
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.rowid
            WHERE chunks_fts MATCH ?
        """
        params: list = [fts_q]
        if kind_filter:
            sql += " AND c.kind = ?"
            params.append(kind_filter)
        sql += " ORDER BY bm25_score LIMIT ?"
        params.append(pool)
        try:
            return conn.execute(sql, params).fetchall()
        except Exception:
            return []

    @staticmethod
    def _fuse_results(vec_rows: list, fts_rows: list, mode: str,
                      limit: int) -> list[dict]:
        """Reciprocal Rank Fusion: score = sum(1 / (k + rank_i)) across rankers.
        k=60 is the canonical RRF constant from the original paper. Works well
        without needing to normalize vector cosine vs BM25 scales."""
        K_RRF = 60

        combined: dict[int, dict] = {}

        for rank, row in enumerate(vec_rows):
            cid = row["id"]
            entry = combined.setdefault(cid, {
                "row": row, "rrf": 0.0,
                "vec_score": round(1 - row["distance"], 4),
                "bm25_score": None,
            })
            entry["rrf"] += 1.0 / (K_RRF + rank + 1)

        for rank, row in enumerate(fts_rows):
            cid = row["id"]
            entry = combined.setdefault(cid, {
                "row": row, "rrf": 0.0,
                "vec_score": None,
                "bm25_score": round(-row["bm25_score"], 4),  # bm25() returns negative: smaller = better
            })
            entry["rrf"] += 1.0 / (K_RRF + rank + 1)
            if entry["bm25_score"] is None:
                entry["bm25_score"] = round(-row["bm25_score"], 4)

        ranked = sorted(
            combined.values(), key=lambda e: e["rrf"], reverse=True
        )[:limit]

        out = []
        for entry in ranked:
            row = entry["row"]
            out.append({
                "file": row["file_path"],
                "line_start": row["line_start"],
                "line_end": row["line_end"],
                "kind": row["kind"],
                "name": row["name"],
                "text": row["text"][:600],
                "score": round(entry["rrf"], 4),
                "vec_score": entry["vec_score"],
                "bm25_score": entry["bm25_score"],
                "mode": mode,
            })
        return out

    def status(self) -> dict:
        conn = self._connect()
        try:
            files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
            embedded = 0
            try:
                embedded = conn.execute(
                    "SELECT COUNT(*) FROM chunks_vec"
                ).fetchone()[0]
            except Exception:
                pass
            size_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        finally:
            conn.close()
        return {
            "name": self.name, "root": self.root,
            "files": files, "chunks": chunks, "embedded": embedded,
            "db_size_mb": round(size_bytes / (1024 * 1024), 2),
        }

    def delete(self) -> None:
        """Remove the index database file and the registry entry."""
        registry.delete(self.name)
        try:
            self.db_path.unlink()
        except FileNotFoundError:
            pass


def open_index(name: str) -> CodeIndex | None:
    """Open an existing registered workspace by name."""
    for ws in registry.list_all():
        if ws["name"] == name:
            return CodeIndex(name, ws["root_path"])
    return None


# ── Auto-index on file.changed (event-bus subscriber) ──────────────────

_DEBOUNCE_SECONDS = 2.0
_debounce_lock = threading.Lock()
_debounce_timers: dict[str, threading.Timer] = {}


def _do_autoindex(abs_path: str) -> None:
    """Called by the debounce timer. Find the right workspace and update
    just this file's chunks. Silent on failure — this is a background
    convenience, not a correctness-critical path."""
    try:
        ws = registry.find_for_file(abs_path)
        if not ws:
            return  # file isn't inside any registered workspace
        idx = CodeIndex(ws["name"], ws["root_path"])
        result = idx.update_file(abs_path)
        if "error" in result:
            print(f"[code_index] auto-update failed for {abs_path!r}: {result['error']}")
    except Exception as e:
        print(f"[code_index] auto-update exception: {e}")
    finally:
        with _debounce_lock:
            _debounce_timers.pop(abs_path, None)


def schedule_autoindex(abs_path: str, delay: float = _DEBOUNCE_SECONDS) -> None:
    """Debounced: if called multiple times for the same path in <delay> sec,
    only the last call fires. Prevents thrashing on rapid edits."""
    abs_path = os.path.abspath(abs_path)
    with _debounce_lock:
        existing = _debounce_timers.get(abs_path)
        if existing:
            existing.cancel()
        t = threading.Timer(delay, _do_autoindex, args=(abs_path,))
        t.daemon = True
        _debounce_timers[abs_path] = t
        t.start()


def _on_file_changed(path: str = "", **_) -> None:
    """Event bus handler. Wired at import time in tools/vector_search.py."""
    if not path:
        return
    abs_path = os.path.abspath(path)
    ws = registry.find_for_file(abs_path)
    if not ws:
        return  # outside every registered workspace — don't index arbitrary files
    schedule_autoindex(abs_path)
