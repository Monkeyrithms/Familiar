"""
Familiar file-share — keep a /file_share/ folder in sync across peers.

Model (additive union): every node periodically pulls each peer's manifest and
downloads anything it's missing or that the peer has a newer copy of. Dropping a
file into file_share/ propagates it to all connected peers; deleting a file
locally does NOT delete it elsewhere (safest — an accidental delete can't
cascade). Edits resolve by mtime: the newest version wins.

Stdlib-only and GUI-free, so the same code runs on a headless node. The inbound
server (core.network) exposes two authenticated endpoints this drives:
  * /files/manifest → {relpath: {size, mtime, sha256}}
  * /files/get {path} → {data: base64}
"""

from __future__ import annotations

import base64
import hashlib
import os
import threading
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
SHARE_DIR = APP_DIR / "file_share"

_POLL_SECONDS = 12
_MAX_FILE_BYTES = 100 * 1024 * 1024     # skip absurdly large files (tunnel-friendly)

_sync_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()


# ── Local folder helpers ─────────────────────────────────────────────────

def _ensure_dir() -> Path:
    try:
        SHARE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return SHARE_DIR


def _safe_join(rel: str) -> Path | None:
    """Resolve a peer-supplied relative path under SHARE_DIR, rejecting anything
    that escapes the folder (``..``, absolute paths, drive letters)."""
    rel = (rel or "").replace("\\", "/").strip("/")
    if not rel:
        return None
    base = SHARE_DIR.resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None          # path traversal attempt
    return target


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def local_manifest() -> dict:
    """{relpath: {size, mtime, sha256}} for every file under file_share/."""
    _ensure_dir()
    out: dict[str, dict] = {}
    base = SHARE_DIR.resolve()
    for root, _dirs, files in os.walk(base):
        for name in files:
            fp = Path(root) / name
            try:
                st = fp.stat()
                if st.st_size > _MAX_FILE_BYTES:
                    continue
                rel = fp.resolve().relative_to(base).as_posix()
                out[rel] = {"size": st.st_size, "mtime": st.st_mtime,
                            "sha256": _sha256(fp)}
            except Exception:
                continue
    return out


def read_share_file(rel: str) -> bytes | None:
    """Raw bytes of a shared file, or None if missing / out of bounds / oversized."""
    target = _safe_join(rel)
    if target is None or not target.is_file():
        return None
    try:
        if target.stat().st_size > _MAX_FILE_BYTES:
            return None
        return target.read_bytes()
    except Exception:
        return None


def _write_share_file(rel: str, blob: bytes, mtime: float) -> bool:
    target = _safe_join(rel)
    if target is None:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(blob)
        os.replace(tmp, target)
        if mtime:
            os.utime(target, (mtime, mtime))      # preserve mtime → stable conflict winner
        return True
    except Exception:
        return False


# ── Sync loop ────────────────────────────────────────────────────────────

def _need_pull(local: dict, remote_meta: dict, rel: str) -> bool:
    """Pull when we lack the file, or the peer's copy differs AND is newer."""
    cur = local.get(rel)
    if cur is None:
        return True
    if cur.get("sha256") == remote_meta.get("sha256"):
        return False
    return float(remote_meta.get("mtime", 0)) > float(cur.get("mtime", 0))


def _sync_once(manager) -> int:
    """One pass over every peer. Returns the number of files pulled."""
    from core.network import _post, outbound_identity   # local import → no cycle
    _, _, peers = outbound_identity()
    if not peers:
        return 0
    local = local_manifest()
    pulled = 0
    for p in peers:
        url = p.get("url", "")
        ok, resp, _detail = _post(url, "/files/manifest", {}, timeout=10)
        if not ok or not isinstance(resp, dict):
            continue
        remote = resp.get("files") or {}
        for rel, meta in remote.items():
            if _stop.is_set():
                return pulled
            if not _need_pull(local, meta, rel):
                continue
            ok2, blob_resp, _d = _post(url, "/files/get", {"path": rel}, timeout=30)
            if not ok2 or not isinstance(blob_resp, dict) or "data" not in blob_resp:
                continue
            try:
                blob = base64.b64decode(blob_resp["data"])
            except Exception:
                continue
            if _write_share_file(rel, blob, float(meta.get("mtime", 0))):
                pulled += 1
                # Reflect locally so a second peer offering the same file in this
                # same pass doesn't re-pull it.
                local[rel] = {"size": len(blob), "mtime": float(meta.get("mtime", 0)),
                              "sha256": meta.get("sha256", "")}
                manager._log(f"file-share: pulled {rel} from {p.get('name') or url}")
    return pulled


def _loop(manager):
    _ensure_dir()
    while not _stop.is_set():
        try:
            _sync_once(manager)
        except Exception as e:
            try:
                manager._log(f"file-share sync error: {e}")
            except Exception:
                pass
        _stop.wait(_POLL_SECONDS)


def start_sync(manager):
    """Start the background sync loop (idempotent). Called by NetworkManager
    once the link is up."""
    global _sync_thread
    with _lock:
        if _sync_thread is not None and _sync_thread.is_alive():
            return
        _stop.clear()
        _ensure_dir()
        _sync_thread = threading.Thread(target=_loop, args=(manager,),
                                        daemon=True, name="file-share-sync")
        _sync_thread.start()


def stop_sync():
    global _sync_thread
    with _lock:
        _stop.set()
        _sync_thread = None
