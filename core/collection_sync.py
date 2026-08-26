"""Generic collection parity - Plane A, extended beyond workflows.

vault_sync.py gives the `flowcharts` DB table last-writer-wins parity across the
cluster. This module reuses that EXACT algorithm (per-item version
(wall_ms, counter, origin), tombstones, content-poll for local puts, explicit
hooks for local deletes, manifest anti-entropy) but over a pluggable ADAPTER, so
any named collection of items can ride the same machinery:

  * strategies -> loose JSON files in data/strategies/*.json
  * blocks     -> executable Modules/Script/<Name>/block.py folders

The wire protocol is namespaced by a `coll` field on every message
(coll_put / coll_delete / coll_manifest_req / coll_get_req) so two or more engines
share one /sync endpoint without colliding - and without touching the
battle-tested workflow vault_* protocol at all.

An ADAPTER is the only collection-specific code. Contract:

    name              -> short id, e.g. "strategies" (also the version-table tag)
    enumerate()       -> {item_name: content_dict}     # everything live right now
    read(name)        -> content_dict | None
    write(name, body) -> None                          # create or overwrite
    remove(name)      -> None                          # delete
    after_apply(kind) -> None                          # optional: refresh live UI

content_dict must be JSON-serialisable and canonically hashable - identical bytes
on every machine for identical content (the adapter owns that guarantee).

Pure backend: sqlite + filesystem + backend.network. No Qt import here; UI refresh
is delegated to the adapter's after_apply (which may marshal to the GUI thread).
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from typing import Optional

_PUT_SECONDS = 3.0
_MANIFEST_EVERY = 4          # ~12s manifest reconcile, matching vault_sync
_DELETE_SETTLE = 0.4         # an item must stay absent this long before tombstone


def _canon(body: dict) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash(body: dict) -> str:
    return hashlib.sha256(_canon(body).encode("utf-8")).hexdigest()


def _chunks(seq: list, n: int):
    """Yield successive n-sized chunks of seq (for bulk body pulls)."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _ver_gt(a: dict, b: Optional[dict]) -> bool:
    """True iff version a is strictly newer than b (b None = nothing yet)."""
    if b is None:
        return True
    if a["w"] != b["w"]:
        return a["w"] > b["w"]
    if a["c"] != b["c"]:
        return a["c"] > b["c"]
    return str(a["o"]) > str(b["o"])


class CollectionAdapter:
    """Interface a collection must implement. Subclass or duck-type."""
    name: str = "collection"
    # Opt-in: infer deletes by diffing the known-live set against enumerate().
    # Safe ONLY for collections where enumerate() is authoritative and atomic per
    # item (filesystem folders/files) - NOT for a single DB table whose transient
    # empty read would wipe the network. A two-poll debounce guards mid-write races.
    detect_deletes: bool = False

    def enumerate(self) -> dict[str, dict]:
        raise NotImplementedError

    def read(self, item: str) -> Optional[dict]:
        raise NotImplementedError

    def write(self, item: str, body: dict) -> None:
        raise NotImplementedError

    def remove(self, item: str) -> None:
        raise NotImplementedError

    def after_apply(self, kind: str) -> None:
        """Optional hook to refresh live UI after a remote put/delete lands."""
        return None

    def watch_paths(self) -> list[str]:
        """Optional: dirs/files for a filesystem watchdog to monitor. Empty list
        means 'poll only'. The watcher is just a fast trigger; the engine's
        hash-diff still decides what actually changed."""
        return []


class CollectionSync:
    """One parity engine for one collection. Mirror of VaultSync, adapter-driven."""

    def __init__(self, adapter: CollectionAdapter) -> None:
        self.adapter = adapter
        self.coll = adapter.name
        self._table = f"coll_versions_{self.coll}"
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.RLock()
        self._origin = "familiar"
        self._counter = 0
        self._seeded = False
        self._recent_applied: dict[str, str] = {}
        self._pending_apply: set[str] = set()
        # Cheap filesystem fingerprint (path -> mtime/size over watch_paths) from
        # the last poll. Lets the 3s scan skip re-reading + re-hashing every file
        # (e.g. all 120+ block.py) when nothing on disk changed (S3).
        self._last_fp: Optional[tuple] = None
        self._bcast_err_last = 0.0
        # Delete-detection settle: item -> monotonic time first seen absent. An
        # item is tombstoned only once it has stayed absent for _DELETE_SETTLE,
        # which rides out a mid-write moment where enumerate() momentarily can't
        # parse a file. Time-based (not count-based) so a single watchdog wake is
        # enough - the loop re-checks itself after the settle window.
        self._absent_since: dict[str, float] = {}
        self._need_recheck = False
        # Wake event: the filesystem watchdog (or any caller) sets this to make
        # the loop scan NOW instead of waiting out the poll interval.
        self._wake = threading.Event()

    # -- db plumbing (own connection + own per-collection version table) -------
    # One connection is shared between the daemon poll thread and the inbound
    # handler thread (via the coordinator's handle()), so every method that
    # touches self._conn serialises on self._lock (reentrant RLock) — see the
    # matching note in vault_sync (S4).
    def _db(self) -> sqlite3.Connection:
        with self._lock:
            if self._conn is None:
                from core.network import APP_DIR
                db_path = APP_DIR / "data" / "collection_sync.db"
                os.makedirs(db_path.parent, exist_ok=True)
                conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                    conn.execute("PRAGMA synchronous=NORMAL")
                    conn.execute("PRAGMA busy_timeout=5000")
                except sqlite3.Error:
                    pass
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {self._table} (
                        name    TEXT PRIMARY KEY,
                        wall_ms INTEGER NOT NULL,
                        counter INTEGER NOT NULL,
                        origin  TEXT    NOT NULL,
                        chash   TEXT    NOT NULL DEFAULT '',
                        deleted INTEGER NOT NULL DEFAULT 0
                    )
                """)
                conn.commit()
                self._conn = conn
                row = conn.execute(f"SELECT COALESCE(MAX(counter), 0) FROM {self._table}").fetchone()
                self._counter = int(row[0]) if row else 0
            return self._conn

    def _fs_fingerprint(self) -> Optional[tuple]:
        """Cheap (path, mtime_ns, size) signature over the adapter's watch_paths.
        Returns None for poll-only adapters (no watch_paths) so they always scan."""
        try:
            paths = self.adapter.watch_paths()
        except Exception:
            return None
        if not paths:
            return None
        sig = []
        for p in paths:
            try:
                st = os.stat(p)
                sig.append((p, st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((p, 0, -1))
        return tuple(sig)

    def _get_ver(self, name: str) -> Optional[dict]:
        with self._lock:
            row = self._db().execute(
                f"SELECT wall_ms, counter, origin, chash, deleted FROM {self._table} WHERE name = ?",
                (name,)).fetchone()
        if not row:
            return None
        return {"w": int(row[0]), "c": int(row[1]), "o": row[2],
                "chash": row[3], "del": int(row[4])}

    def _put_ver(self, name: str, ver: dict, chash: str, deleted: int) -> None:
        with self._lock:
            self._db().execute(
                f"INSERT INTO {self._table} (name, wall_ms, counter, origin, chash, deleted) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
                "wall_ms=excluded.wall_ms, counter=excluded.counter, origin=excluded.origin, "
                "chash=excluded.chash, deleted=excluded.deleted",
                (name, ver["w"], ver["c"], ver["o"], chash, deleted))
            self._db().commit()

    def _bump(self) -> dict:
        self._counter += 1
        return {"w": int(time.time() * 1000), "c": self._counter, "o": self._origin}

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            try:
                from core.network import outbound_identity
                self._origin = outbound_identity()[0] or "familiar"
            except Exception:
                self._origin = "familiar"
            self.running = True
            self._seeded = False
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            self.running = False
        self._wake.set()

    def request_scan(self) -> None:
        """Thread-safe nudge: wake the loop to scan immediately. Called by the
        filesystem watchdog on any add/remove/rename in a watched directory, so
        local changes propagate in ~milliseconds instead of up to a poll cycle."""
        self._wake.set()

    def _loop(self) -> None:
        tick = 0
        while self.running:
            self._need_recheck = False
            try:
                self._scan_local_puts(seed=not self._seeded)
                self._seeded = True
                if tick % _MANIFEST_EVERY == 0:
                    self._anti_entropy()
            except Exception:
                pass
            tick += 1
            # Wait for the next poll, but wake instantly on request_scan(). When a
            # delete is mid-settle, re-check after the short settle window instead
            # of the full poll interval so the tombstone commits on its own.
            timeout = _DELETE_SETTLE if self._need_recheck else _PUT_SECONDS
            self._wake.wait(timeout)
            self._wake.clear()

    # -- local edit detection (puts) ------------------------------------------
    def _scan_local_puts(self, seed: bool) -> None:
        # Collect broadcasts inside the lock; send them AFTER releasing it so a
        # slow/dead peer can't stall inbound handle()/manifest (S1).
        to_broadcast: list[dict] = []
        with self._lock:
            # S3: skip the re-read+re-hash of every file when nothing under the
            # adapter's watch_paths changed. A delete mid-settle (_absent_since)
            # or a remote apply in flight always forces a scan.
            fp = self._fs_fingerprint()
            settling = bool(self._absent_since)
            if (not seed and fp is not None and fp == self._last_fp
                    and not settling and not self._pending_apply):
                return
            self._last_fp = fp
            try:
                items = self.adapter.enumerate()
            except Exception:
                return
            for name, body in items.items():
                if name in self._pending_apply:
                    continue
                h = _hash(body)
                ra = self._recent_applied.get(name)
                ver = self._get_ver(name)
                if ver is not None and ver["chash"] == h and not ver["del"]:
                    if ra == h:
                        self._recent_applied.pop(name, None)   # redundant now (S5)
                    continue
                if ra == h:
                    continue
                new_ver = self._bump()
                self._put_ver(name, new_ver, h, 0)
                if not seed:
                    to_broadcast.append({"type": "coll_put", "coll": self.coll,
                                         "name": name, "body": body, "version": new_ver})
            if getattr(self.adapter, "detect_deletes", False) and not seed:
                to_broadcast.extend(self._scan_local_deletes(items))
        for payload in to_broadcast:
            self._broadcast(payload)

    def _scan_local_deletes(self, present: dict) -> list[dict]:
        """Tombstone items that were live but have vanished from enumerate().
        Requires the adapter to opt in (filesystem-backed). Time-settled: an item
        must stay absent for _DELETE_SETTLE before it is tombstoned, riding out a
        mid-write moment where a file momentarily can't be read. This needs only a
        single wake (e.g. the watchdog firing on the unlink) - the loop re-checks
        itself after the settle window, so a real delete propagates in well under
        a second without depending on a second unrelated edit. Called under
        self._lock. Returns tombstone broadcast payloads (sent by the caller
        OUTSIDE the lock)."""
        out: list[dict] = []
        live = set()
        for name, w, c, o, dele in self._db().execute(
                f"SELECT name, wall_ms, counter, origin, deleted FROM {self._table}"):
            if not int(dele):
                live.add(name)
        now = time.monotonic()
        for name in live:
            if name in present or name in self._pending_apply:
                self._absent_since.pop(name, None)
                continue
            first = self._absent_since.get(name)
            if first is None:
                self._absent_since[name] = now
                self._need_recheck = True   # come back after the settle window
                continue
            if (now - first) < _DELETE_SETTLE:
                self._need_recheck = True
                continue
            self._absent_since.pop(name, None)
            ver = self._bump()
            self._put_ver(name, ver, "", 1)
            self._recent_applied.pop(name, None)
            out.append({"type": "coll_delete", "coll": self.coll,
                        "name": name, "version": ver})
        return out

    # -- explicit local hooks (called from the GUI/adapter) -------------------
    def note_local_delete(self, name: str) -> None:
        """An item was removed locally -> tombstone + broadcast. Stateless echo
        guard: if already tombstoned at our version, this is the application of a
        peer's tombstone (or a repeat) - record nothing, broadcast nothing."""
        with self._lock:
            cur = self._get_ver(name)
            if cur is not None and cur["del"]:
                return
            ver = self._bump()
            self._put_ver(name, ver, "", 1)
            self._recent_applied.pop(name, None)
            payload = {"type": "coll_delete", "coll": self.coll,
                       "name": name, "version": ver}
        # Called ON THE GUI THREAD (e.g. strategy_page deleting a strategy).
        # broadcast() blocks on per-peer HTTP, so fire-and-forget on a daemon
        # thread — never freeze the UI on a slow/down peer (matches vault_sync).
        threading.Thread(target=self._broadcast, args=(payload,),
                         daemon=True, name=f"coll-{self.coll}-delete-bcast").start()

    def note_local_rename(self, old: str, new: str) -> None:
        self.note_local_delete(old)

    # -- applying remote edits ------------------------------------------------
    def _apply_put(self, name: str, body: dict, ver: dict) -> None:
        with self._lock:
            if not name or not isinstance(body, dict):
                return
            cur = self._get_ver(name)
            if not _ver_gt(ver, cur):
                return
            h = _hash(body)
            self._recent_applied[name] = h
            self._put_ver(name, ver, h, 0)
            self._pending_apply.add(name)
        try:
            self.adapter.write(name, body)
        except Exception as e:
            print(f"[coll_sync:{self.coll}] write failed: {e}")
        finally:
            with self._lock:
                self._pending_apply.discard(name)
        try:
            self.adapter.after_apply("put")
        except Exception:
            pass

    def _apply_delete(self, name: str, ver: dict) -> None:
        with self._lock:
            if not name:
                return
            cur = self._get_ver(name)
            if not _ver_gt(ver, cur):
                return
            self._put_ver(name, ver, "", 1)
            self._recent_applied.pop(name, None)
            self._pending_apply.add(name)
        try:
            self.adapter.remove(name)
        except Exception as e:
            print(f"[coll_sync:{self.coll}] remove failed: {e}")
        finally:
            with self._lock:
                self._pending_apply.discard(name)
        try:
            self.adapter.after_apply("delete")
        except Exception:
            pass

    # -- manifest / anti-entropy ----------------------------------------------
    def manifest(self) -> dict[str, dict]:
        with self._lock:
            out: dict[str, dict] = {}
            try:
                for name, w, c, o, dele in self._db().execute(
                        f"SELECT name, wall_ms, counter, origin, deleted FROM {self._table}"):
                    out[name] = {"w": int(w), "c": int(c), "o": o, "del": int(dele)}
            except sqlite3.Error:
                pass
            return out

    def _body_for(self, name: str) -> Optional[dict]:
        try:
            return self.adapter.read(name)
        except Exception:
            return None

    def _anti_entropy(self) -> None:
        from core.network import outbound_identity, request_peer
        _, _, peers = outbound_identity()
        for p in peers:
            url = p.get("url")
            if not url:
                continue
            resp = request_peer(url, {"type": "coll_manifest_req", "coll": self.coll})
            man = (resp or {}).get("manifest")
            if not isinstance(man, dict):
                continue
            need_pull: list[str] = []
            for name, pver in man.items():
                if not isinstance(pver, dict):
                    continue
                pv = {"w": int(pver.get("w", 0)), "c": int(pver.get("c", 0)),
                      "o": pver.get("o", ""), "del": int(pver.get("del", 0))}
                cur = self._get_ver(name)
                if not _ver_gt(pv, cur):
                    continue
                if pv["del"]:
                    self._apply_delete(name, pv)
                else:
                    need_pull.append(name)
            # S2: bulk-pull newer bodies (one round trip per 50), not one HTTP
            # round trip per differing item.
            for chunk in _chunks(need_pull, 50):
                got = request_peer(url, {"type": "coll_get_multi",
                                         "coll": self.coll, "names": chunk})
                bodies = (got or {}).get("bodies")
                if isinstance(bodies, dict):
                    for name, entry in bodies.items():
                        if not isinstance(entry, dict):
                            continue
                        b = entry.get("body")
                        bv = entry.get("version")
                        if isinstance(b, dict) and isinstance(bv, dict):
                            self._apply_put(name, b, bv)
                else:
                    # Peer predates coll_get_multi — per-name fallback.
                    for name in chunk:
                        g1 = request_peer(url, {"type": "coll_get_req",
                                                "coll": self.coll, "name": name})
                        b = (g1 or {}).get("body")
                        bv = (g1 or {}).get("version")
                        if isinstance(b, dict) and isinstance(bv, dict):
                            self._apply_put(name, b, bv)

    def pull_from_url(self, url: str) -> int:
        """Anti-entropy against one peer URL. Returns number of bodies applied."""
        from core.network import request_peer
        if not url:
            return 0
        resp = request_peer(url, {"type": "coll_manifest_req", "coll": self.coll})
        man = (resp or {}).get("manifest")
        if not isinstance(man, dict):
            return 0
        need_pull: list[str] = []
        applied = 0
        for name, pver in man.items():
            if not isinstance(pver, dict):
                continue
            pv = {"w": int(pver.get("w", 0)), "c": int(pver.get("c", 0)),
                  "o": pver.get("o", ""), "del": int(pver.get("del", 0))}
            cur = self._get_ver(name)
            if not _ver_gt(pv, cur):
                continue
            if pv["del"]:
                self._apply_delete(name, pv)
                applied += 1
            else:
                need_pull.append(name)
        for chunk in _chunks(need_pull, 50):
            got = request_peer(url, {"type": "coll_get_multi",
                                     "coll": self.coll, "names": chunk})
            bodies = (got or {}).get("bodies")
            if isinstance(bodies, dict):
                for name, entry in bodies.items():
                    if not isinstance(entry, dict):
                        continue
                    b = entry.get("body")
                    bv = entry.get("version")
                    if isinstance(b, dict) and isinstance(bv, dict):
                        self._apply_put(name, b, bv)
                        applied += 1
            else:
                for name in chunk:
                    g1 = request_peer(url, {"type": "coll_get_req",
                                            "coll": self.coll, "name": name})
                    b = (g1 or {}).get("body")
                    bv = (g1 or {}).get("version")
                    if isinstance(b, dict) and isinstance(bv, dict):
                        self._apply_put(name, b, bv)
                        applied += 1
        return applied

    def push_to_url(self, url: str) -> int:
        """PUSH our newer items TO one peer. Inverse of pull_from_url.

        Normal anti-entropy is pull-based, so a node only converges by reaching
        out to peers. When we can reach a peer but it can't reach us back (e.g.
        our tunnel address churned), its pull can never close the gap. This
        pushes instead over the working WE→peer channel.
        Returns the number of items sent."""
        from core.network import request_peer, send_to_peer
        if not url:
            return 0
        resp = request_peer(url, {"type": "coll_manifest_req", "coll": self.coll})
        peer_man = (resp or {}).get("manifest")
        if not isinstance(peer_man, dict):
            return 0
        with self._lock:
            local = self.manifest()
        pushed = 0
        for name, lv in local.items():
            ours = {"w": int(lv["w"]), "c": int(lv["c"]), "o": lv["o"]}
            pv = peer_man.get(name)
            tv = ({"w": int(pv.get("w", 0)), "c": int(pv.get("c", 0)),
                   "o": pv.get("o", "")} if isinstance(pv, dict) else None)
            if tv is not None and not _ver_gt(ours, tv):
                continue
            if int(lv.get("del", 0)):
                ok, _ = send_to_peer(url, {"type": "coll_delete", "coll": self.coll,
                                           "name": name, "version": ours})
            else:
                body = self._body_for(name)
                if body is None:
                    continue
                ok, _ = send_to_peer(url, {"type": "coll_put", "coll": self.coll,
                                           "name": name, "body": body, "version": ours})
            if ok:
                pushed += 1
        return pushed

    # -- inbound dispatch (called by the coordinator for this collection) -----
    def handle(self, data: dict) -> Optional[dict]:
        kind = data.get("type")
        if kind == "coll_put":
            self._apply_put(data.get("name", ""), data.get("body"), data.get("version") or {})
            return {"ok": True}
        if kind == "coll_delete":
            self._apply_delete(data.get("name", ""), data.get("version") or {})
            return {"ok": True}
        if kind == "coll_manifest_req":
            return {"ok": True, "manifest": self.manifest()}
        if kind == "coll_get_req":
            name = data.get("name", "")
            b = self._body_for(name)
            v = self._get_ver(name)
            if b is None or v is None:
                return {"ok": False, "error": "not found"}
            return {"ok": True, "body": b,
                    "version": {"w": v["w"], "c": v["c"], "o": v["o"]}}
        if kind == "coll_get_multi":
            names = [n for n in (data.get("names") or []) if isinstance(n, str)]
            out: dict[str, dict] = {}
            for name in names:
                b = self._body_for(name)
                v = self._get_ver(name)
                if b is not None and v is not None:
                    out[name] = {"body": b,
                                 "version": {"w": v["w"], "c": v["c"], "o": v["o"]}}
            return {"ok": True, "bodies": out}
        return None

    def _broadcast(self, payload: dict) -> None:
        try:
            from core.network import broadcast
            results = broadcast(payload)
        except Exception:
            return
        fails = [r for r in (results or []) if not r.get("ok")]
        if fails:
            now = time.monotonic()
            if now - self._bcast_err_last >= 60.0:
                self._bcast_err_last = now
                for r in fails:
                    print(f"[coll_sync:{self.coll}] broadcast to "
                          f"{r.get('name') or r.get('url')} failed: {r.get('detail')} "
                          f"(anti-entropy will heal)", flush=True)
