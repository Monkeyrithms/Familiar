"""
Familiar file-share — keep a /file_share/ folder in sync across peers.

Model (additive union + tombstones): every node periodically pulls each peer's
manifest and downloads anything it's missing or that the peer has a newer copy
of. Dropping a file into file_share/ propagates it to all connected peers.

Deletes: a bare filesystem delete does NOT cascade (the file re-seeds from
peers — safest against accidents). To delete a file *everywhere*, call
``delete_shared_file()`` (Settings → Network → Shared files): it removes the
local copy AND records a tombstone {relpath: deleted_at}. Tombstones ride
along in the manifest exchange; peers adopt newer tombstones, delete their
copy, and refuse to re-pull the file. Dropping a NEW copy with mtime newer
than the tombstone resurrects the file (intentional re-share wins).
Tombstones expire after _TOMBSTONE_TTL so the ledger can't grow forever.

Stdlib-only and GUI-free, so the same code runs on a headless node. The inbound
server (core.network) exposes two authenticated endpoints this drives:
  * /files/manifest → {files: {relpath: {size, mtime, sha256}},
                       tombstones: {relpath: deleted_at}}
  * /files/get {path} → {data: base64}
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import time
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
SHARE_DIR = APP_DIR / "file_share"

_POLL_SECONDS = 12
_MAX_FILE_BYTES = 100 * 1024 * 1024     # skip absurdly large files (tunnel-friendly)
_TOMBSTONE_FILE = APP_DIR / "data" / "file_share_tombstones.json"
_TOMBSTONE_TTL = 30 * 24 * 3600         # forget deletions after 30 days

_sync_thread: threading.Thread | None = None
_stop = threading.Event()
_lock = threading.Lock()
_tomb_lock = threading.Lock()


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


# ── Tombstones (cascading deletes) ───────────────────────────────────────

def load_tombstones() -> dict[str, float]:
    """{relpath: deleted_at} — expired entries are filtered on read."""
    try:
        raw = json.loads(_TOMBSTONE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    out: dict[str, float] = {}
    for rel, ts in raw.items():
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            continue
        if now - ts < _TOMBSTONE_TTL:
            out[str(rel)] = ts
    return out


def _save_tombstones(tombs: dict[str, float]) -> None:
    try:
        _TOMBSTONE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _TOMBSTONE_FILE.with_suffix(".json.part")
        tmp.write_text(json.dumps(tombs, indent=2, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, _TOMBSTONE_FILE)
    except Exception:
        pass


def _update_tombstones(add: dict[str, float] | None = None,
                       remove: set[str] | None = None) -> dict[str, float]:
    """Atomically merge changes into the tombstone file (max-ts wins on add)."""
    with _tomb_lock:
        tombs = load_tombstones()
        for rel, ts in (add or {}).items():
            tombs[rel] = max(float(ts), tombs.get(rel, 0.0))
        for rel in (remove or set()):
            tombs.pop(rel, None)
        _save_tombstones(tombs)
        return tombs


def delete_shared_file(rel: str) -> bool:
    """Delete a shared file locally AND tombstone it so every peer deletes it
    too instead of re-seeding it back. The tombstone timestamp is forced past
    the file's mtime so the deletion beats the existing copies everywhere."""
    target = _safe_join(rel)
    if target is None:
        return False
    ts = time.time()
    try:
        if target.is_file():
            ts = max(ts, target.stat().st_mtime + 1.0)
            target.unlink()
    except Exception:
        return False
    _update_tombstones(add={rel.replace("\\", "/").strip("/"): ts})
    return True


def list_share_files() -> list[dict]:
    """Lightweight folder listing for UIs: [{rel, size, mtime}], sorted.
    No hashing (local_manifest sha256s every file — too slow for a dialog)."""
    _ensure_dir()
    out: list[dict] = []
    base = SHARE_DIR.resolve()
    for root, _dirs, files in os.walk(base):
        for name in files:
            fp = Path(root) / name
            try:
                st = fp.stat()
                out.append({"rel": fp.resolve().relative_to(base).as_posix(),
                            "size": st.st_size, "mtime": st.st_mtime})
            except Exception:
                continue
    return sorted(out, key=lambda d: d["rel"].lower())


def local_manifest() -> dict:
    """{relpath: {size, mtime, sha256}} for every file under file_share/.
    Tombstoned files are excluded so we never re-offer a deleted file to
    peers (a copy can linger on disk briefly between tombstone adoption
    and the sync pass that enacts it)."""
    _ensure_dir()
    tombs = load_tombstones()
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
                if rel in tombs and st.st_mtime <= tombs[rel]:
                    continue
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
    tombs = load_tombstones()
    tomb_add: dict[str, float] = {}
    tomb_remove: set[str] = set()
    pulled = 0
    for p in peers:
        url = p.get("url", "")
        ok, resp, _detail = _post(url, "/files/manifest", {}, timeout=10)
        if not ok or not isinstance(resp, dict):
            continue
        remote = resp.get("files") or {}

        # Adopt the peer's tombstones first: a newer "deleted at T" beats any
        # copy with mtime <= T — delete ours and stop offering/pulling it. A
        # local copy NEWER than T means the file was re-shared after the
        # delete; keep it and don't adopt (the peer will resurrect from us).
        for rel, ts in (resp.get("tombstones") or {}).items():
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if ts <= tombs.get(rel, 0.0):
                continue                      # already known (or older news)
            target = _safe_join(rel)
            if target is not None and target.is_file():
                try:
                    if target.stat().st_mtime > ts:
                        continue              # our copy is newer → resurrect
                    target.unlink()
                    local.pop(rel, None)
                    manager._log(f"file-share: deleted {rel} "
                                 f"(tombstone from {p.get('name') or url})")
                except Exception:
                    continue                  # couldn't enact — retry next pass
            tombs[rel] = ts
            tomb_add[rel] = ts
            tomb_remove.discard(rel)

        for rel, meta in remote.items():
            if _stop.is_set():
                break
            # Tombstoned and not newer than the delete → don't respawn it.
            t = tombs.get(rel, 0.0)
            if t and float(meta.get("mtime", 0)) <= t:
                continue
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
                if t:
                    # Newer copy past the tombstone → the file is back on
                    # purpose. Clear the tombstone so it propagates again.
                    tombs.pop(rel, None)
                    tomb_remove.add(rel)
                    tomb_add.pop(rel, None)
                # Reflect locally so a second peer offering the same file in this
                # same pass doesn't re-pull it.
                local[rel] = {"size": len(blob), "mtime": float(meta.get("mtime", 0)),
                              "sha256": meta.get("sha256", "")}
                manager._log(f"file-share: pulled {rel} from {p.get('name') or url}")
        if _stop.is_set():
            break
    if tomb_add or tomb_remove:
        _update_tombstones(add=tomb_add, remove=tomb_remove)
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
