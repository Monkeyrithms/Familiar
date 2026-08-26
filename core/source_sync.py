"""Source-code propagation for Familiar — Plane D.

Carries the app's own Python source across the Familiar-Net cluster on the
same LWW/tombstone/anti-entropy engine as Brikwerx 3's source_sync. Inbound
source is staged under `.update_staging/` and applied only after the user (or
the idle auto-update path) restarts — never hot-applied mid-run.

Scope is a strict allowlist: main.py, core/, ui/, tools/ — not config, data,
logs, file_share, or user secrets.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Callable, Optional

from core.collection_sync import CollectionAdapter, CollectionSync

# Familiar root (core/source_sync.py -> core -> Apps/Agent).
ROOT = Path(__file__).resolve().parent.parent
STAGING = ROOT / ".update_staging"

_ALLOW_FILES = {"main.py"}
_SRC_ROOTS = ("core", "ui", "tools")
_DENY_ROOTS: tuple[str, ...] = ()
_EXCLUDE_PARTS = {"__pycache__", ".update_staging", ".git", "venv", ".venv",
                  "data", "logs", "file_share", "Distributable", "sandbox",
                  ".pytest_cache", ".mypy_cache", ".ruff_cache", "assets", "sounds"}


def _norm(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _under(rel: str, roots) -> bool:
    return any(rel == r or rel.startswith(r + "/") for r in roots)


def _is_test_or_scratch(parts: list[str]) -> bool:
    if any(seg == "tests" for seg in parts):
        return True
    name = parts[-1]
    if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
        return True
    if name.startswith("diag_"):
        return True
    return name.startswith("_") and not name.startswith("__")


def _is_scoped(rel: str) -> bool:
    rel = rel.replace("\\", "/")
    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return False
    if ":" in rel:
        return False
    parts = rel.split("/")
    if any(p in _EXCLUDE_PARTS for p in parts):
        return False
    if any(".tmp." in p for p in parts):
        return False
    if not rel.endswith(".py"):
        return False
    if rel in _ALLOW_FILES:
        return True
    if _DENY_ROOTS and _under(rel, _DENY_ROOTS):
        return False
    if not _under(rel, _SRC_ROOTS):
        return False
    return not _is_test_or_scratch(parts)


def _read(path: Path) -> Optional[str]:
    try:
        return _norm(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _hash_text(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".swap")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _url_reachable(url: str, timeout: float = 8.0) -> bool:
    """True iff url/ping answers as a Familiar node."""
    import json
    import urllib.request
    if not url:
        return False
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/ping", timeout=timeout) as r:
            if r.status != 200:
                return False
            d = json.loads(r.read().decode() or "{}")
            return d.get("app") == "Familiar"
    except Exception:
        return False


def peer_url_for(name: str) -> str:
    """Resolve a configured peer name to its Familiar-Net URL."""
    from core.network import outbound_identity
    key = (name or "").strip().lower()
    if not key:
        return ""
    _, _, peers = outbound_identity()
    for p in peers or []:
        if not isinstance(p, dict):
            continue
        nm = (p.get("name") or "").strip().lower()
        if nm == key:
            return (p.get("url") or "").strip()
    return ""


class SourceAdapter(CollectionAdapter):
    name = "source"
    detect_deletes = False

    def __init__(self) -> None:
        self._window = None
        self._notify: Optional[Callable[[], None]] = None
        self._boot_hashes: dict[str, str] = {}

    def attach_window(self, window) -> None:
        self._window = window

    def set_notify(self, cb: Callable[[], None]) -> None:
        self._notify = cb

    def snapshot_boot_hashes(self) -> None:
        hashes: dict[str, str] = {}
        for rel in self._live_relpaths():
            text = self._live_text(rel)
            if text is not None:
                hashes[rel] = _hash_text(text)
        self._boot_hashes = hashes

    def _staged_pending(self) -> list[str]:
        out: list[str] = []
        for rel in sorted(self._staged_relpaths()):
            staged = _read(STAGING / rel)
            if staged is None:
                continue
            if staged != self._live_text(rel):
                out.append(rel)
        return out

    def _local_pending(self) -> list[str]:
        if not self._boot_hashes:
            return []
        out: list[str] = []
        for rel in sorted(self._live_relpaths()):
            text = self._live_text(rel)
            if text is None:
                continue
            if self._boot_hashes.get(rel) != _hash_text(text):
                out.append(rel)
        return out

    def pending_detail(self) -> tuple[list[str], list[str]]:
        return self._staged_pending(), self._local_pending()

    def note_local_change(self) -> None:
        if self._notify is not None:
            try:
                self._notify()
            except Exception:
                pass

    def _live_relpaths(self) -> set[str]:
        out: set[str] = set()
        for f in _ALLOW_FILES:
            if (ROOT / f).is_file():
                out.add(f)
        for r in _SRC_ROOTS:
            base = ROOT / r
            if not base.is_dir():
                continue
            for p in base.rglob("*.py"):
                rel = p.relative_to(ROOT).as_posix()
                if _is_scoped(rel):
                    out.add(rel)
        return out

    def _staged_relpaths(self) -> set[str]:
        out: set[str] = set()
        if not STAGING.is_dir():
            return out
        for p in STAGING.rglob("*"):
            if p.is_file():
                rel = p.relative_to(STAGING).as_posix()
                if _is_scoped(rel):
                    out.add(rel)
        return out

    def _desired_text(self, rel: str) -> Optional[str]:
        st = STAGING / rel
        if st.is_file():
            return _read(st)
        return _read(ROOT / rel)

    def _live_text(self, rel: str) -> Optional[str]:
        return _read(ROOT / rel)

    def enumerate(self) -> dict[str, dict]:
        rels = self._live_relpaths() | self._staged_relpaths()
        out: dict[str, dict] = {}
        for rel in rels:
            text = self._desired_text(rel)
            if text is not None:
                out[rel] = {"source": text}
        return out

    def read(self, item: str) -> Optional[dict]:
        text = self._desired_text(item)
        return {"source": text} if text is not None else None

    def write(self, item: str, body: dict) -> None:
        if not _is_scoped(item) or not isinstance(body, dict):
            return
        src = body.get("source")
        if not isinstance(src, str):
            return
        src = _norm(src)
        live = self._live_text(item)
        st = STAGING / item
        if live is not None and src == live:
            try:
                st.unlink(missing_ok=True)
            except OSError:
                pass
            return
        _atomic_write(st, src)

    def remove(self, item: str) -> None:
        return None

    def after_apply(self, kind: str) -> None:
        if self._notify is not None:
            try:
                self._notify()
            except Exception:
                pass

    def watch_paths(self) -> list[str]:
        return [str(ROOT / r) for r in sorted(self._live_relpaths())]

    def watch_dirs(self) -> list[str]:
        """Directories to watch so NEW source files are noticed too.

        File-level watches only fire for paths that already existed; a freshly
        created core/ module would otherwise never raise the restart banner.
        """
        out = {str(ROOT)}
        for r in _SRC_ROOTS:
            base = ROOT / r
            if not base.is_dir():
                continue
            out.add(str(base))
            for p in base.rglob("*"):
                if p.is_dir() and not any(seg in _EXCLUDE_PARTS for seg in p.parts):
                    out.add(str(p))
        return sorted(out)

    def pending(self) -> list[str]:
        staged, local = self.pending_detail()
        return sorted(set(staged) | set(local))

    def apply_staged(self) -> list[str]:
        applied: list[str] = []
        for rel in self.pending():
            staged = _read(STAGING / rel)
            if staged is None:
                continue
            _atomic_write(ROOT / rel, staged)
            applied.append(rel)
        try:
            if STAGING.is_dir():
                shutil.rmtree(STAGING, ignore_errors=True)
        except Exception:
            pass
        return applied


class SourceSync:
    def __init__(self) -> None:
        self.adapter = SourceAdapter()
        self.engine = CollectionSync(self.adapter)
        self.coll = self.adapter.name
        self._fs_watch = None
        self._started = False

    def attach_window(self, window) -> None:
        self.adapter.attach_window(window)

    def set_notify(self, cb: Callable[[], None]) -> None:
        self.adapter.set_notify(cb)

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.adapter.snapshot_boot_hashes()
        self.engine.start()
        self._install_fs_watch()

    def stop(self) -> None:
        self._started = False
        self.engine.stop()

    def _install_fs_watch(self) -> None:
        if self._fs_watch is not None:
            return
        try:
            from PyQt6.QtCore import QFileSystemWatcher
        except Exception:
            return
        paths = self.adapter.watch_paths()
        if not paths:
            return
        parent = self.adapter._window
        w = QFileSystemWatcher(parent)
        existing = [p for p in paths if os.path.exists(p)]
        if existing:
            w.addPaths(existing)
        # Directory watches catch NEW files (and deletes); file watches alone
        # only ever fire for paths that existed when the watcher was installed.
        try:
            dirs = [d for d in self.adapter.watch_dirs() if os.path.isdir(d)]
            if dirs:
                w.addPaths(dirs)
        except Exception:
            pass
        w.fileChanged.connect(self._on_file_changed)
        w.directoryChanged.connect(self._on_dir_changed)
        self._fs_watch = w

    def _on_dir_changed(self, _path: str) -> None:
        """A new source file may have appeared — start watching it."""
        try:
            w = self._fs_watch
            if w is not None:
                known = set(w.files())
                fresh = [p for p in self.adapter.watch_paths()
                         if p not in known and os.path.exists(p)]
                if fresh:
                    w.addPaths(fresh)
        except Exception:
            pass
        self.adapter.note_local_change()

    def _on_file_changed(self, path: str) -> None:
        """Re-arm the watch, then notify.

        Editors and our own _atomic_write() save via os.replace(), which swaps
        the inode. QFileSystemWatcher drops the watch on the old inode, so
        WITHOUT re-adding the path only the FIRST edit of each file is ever
        seen — every later edit to that file goes silently unnoticed.
        """
        try:
            w = self._fs_watch
            if w is not None and os.path.exists(path) and path not in w.files():
                w.addPath(path)
        except Exception:
            pass
        self.adapter.note_local_change()

    def pending(self) -> list[str]:
        return self.adapter.pending()

    def pending_detail(self) -> tuple[list[str], list[str]]:
        return self.adapter.pending_detail()

    def apply_staged(self) -> list[str]:
        return self.adapter.apply_staged()

    def handle(self, data: dict) -> Optional[dict]:
        if not isinstance(data, dict):
            return None
        kind = data.get("type")
        if kind == "source_update_req":
            return self._handle_source_update_req(data)
        if not isinstance(kind, str) or not kind.startswith("coll_"):
            return None
        if data.get("coll") != self.coll:
            return None
        return self.engine.handle(data)

    def _handle_source_update_req(self, data: dict) -> dict:
        """Inbound: a peer pushed its source to us and wants us to apply + restart.

        apply_only=True (the push-first flow): skip the pull, just apply whatever
        the peer already staged via coll_put and relaunch.
        apply_only=False (legacy pull flow): pull from the sender first, then restart."""
        apply_only = bool(data.get("apply_only"))
        pulled = 0
        if not apply_only:
            sender_url = (data.get("reply_url") or data.get("url") or "").strip()
            if not sender_url:
                return {"ok": False,
                        "error": "requester sent no address — cannot pull updates"}
            if not _url_reachable(sender_url):
                return {"ok": False,
                        "error": "can't reach the requesting machine (its tunnel may be down)"}
            try:
                pulled = self.engine.pull_from_url(sender_url)
            except Exception as e:
                return {"ok": False, "error": f"pull failed: {e}"}
        try:
            staged, local = self.pending_detail()
        except Exception as e:
            return {"ok": False, "error": f"pending check failed: {e}"}
        if not staged and not local:
            return {"ok": True, "action": "noop", "pulled": pulled,
                    "detail": "already up to date"}
        win = getattr(self.adapter, "_window", None)
        if win is None:
            return {"ok": False, "error": "no window attached"}
        if getattr(win, "_update_apply_busy", False):
            return {"ok": False, "error": "update already in progress"}
        agent_active = getattr(win, "_agent_work_active", None)
        if callable(agent_active) and agent_active():
            return {"ok": False, "error": "agent is running — restart deferred"}
        apply_fn = getattr(win, "_apply_update_and_restart", None)
        if not callable(apply_fn):
            return {"ok": False, "error": "update handler unavailable"}
        try:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, apply_fn)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "action": "restarting", "pulled": pulled,
                "detail": "applying update and restarting"}


def request_remote_update(peer_name: str) -> dict:
    """PUSH our source to a peer over the WE→peer channel, then tell it to apply
    and restart.

    This works even when the peer's tunnel address has churned and it can no
    longer reach us back — we push (not pull) so no reverse reachability needed."""
    from core.network import request_peer
    url = peer_url_for(peer_name)
    if not url:
        return {"ok": False, "error": "peer URL not configured"}
    try:
        pushed = source_sync.engine.push_to_url(url)
    except Exception as e:
        return {"ok": False, "error": f"couldn't reach peer to push update: {e}"}
    resp = request_peer(url, {"type": "source_update_req", "apply_only": True},
                        timeout=45)
    if resp is None:
        return {"ok": False,
                "error": f"pushed {pushed} file(s) but couldn't trigger restart on peer"}
    if resp.get("ok") is False:
        return {"ok": False, "error": str(resp.get("error") or "peer rejected update")}
    if isinstance(resp, dict):
        resp = dict(resp)
        resp["pushed"] = pushed
    return resp


def request_local_update(window) -> dict:
    """Apply staged source on this machine and relaunch (GUI-thread-safe)."""
    if window is None:
        return {"ok": False, "error": "no window"}
    if getattr(window, "_update_apply_busy", False):
        return {"ok": False, "error": "update already in progress"}
    apply_fn = getattr(window, "_apply_update_and_restart", None)
    if not callable(apply_fn):
        return {"ok": False, "error": "update handler unavailable"}
    try:
        from PyQt6.QtCore import QTimer, QThread
        if window.thread() == QThread.currentThread():
            apply_fn()
        else:
            QTimer.singleShot(0, apply_fn)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "action": "restarting", "detail": "applying update locally"}


source_sync = SourceSync()
