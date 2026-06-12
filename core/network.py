"""
Familiar networking engine — peer-to-peer messaging over cloudflared.

Inbound (phase 1):
  * A small HMAC-authenticated HTTP server on the configured port.
  * A cloudflared quick-tunnel pointed at it; the public URL is discovered via
    cloudflared's metrics `/quicktunnel` endpoint (robust — no log scraping).
    Reuses cloudflared.exe shipped next to main.py.
  * `/ping` (liveness) and `/sync` (authenticated event intake → `on_sync`).

Outbound (phase 2):
  * `send_to_peer(url, payload)` / `broadcast(payload)` — sign and POST an
    event envelope to a peer's `/sync`. The envelope automatically carries
    `from` (this node's name), `sent_at`, and `reply_url` (this node's public
    address, when a tunnel is up) so the receiver can answer without manual
    peer configuration.
  * `resolve_peer(name_or_url)` — look up a configured peer by name or URL.

Trust model: the shared secret IS the gate. Every `/sync` body is HMAC-signed
over `timestamp.body` with a 30s replay window; the peers list is an address
book for outbound sends, not an inbound filter (origin IPs are meaningless
through a tunnel).

Everything is best-effort and thread-safe; failures degrade to "not running"
rather than raising into the GUI.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import queue
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent      # Apps/Agent
_CF_METRICS_PORT = 20191                                # local-only metrics port
_MAX_BODY_BYTES = 64 * 1024 * 1024                      # hard cap on any request body


# ── HMAC auth (shared with peers; matches the Settings 'shared secret') ──

def sign(secret: str, body: bytes, ts: str) -> str:
    msg = ts.encode() + b"." + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify(secret: str, body: bytes, ts: str, sig: str, *, window: int = 30) -> bool:
    if not secret or not ts or not sig:
        return False
    try:
        if abs(time.time() - float(ts)) > window:
            return False
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(sign(secret, body, ts), sig)


# ── cloudflared quick-tunnel ─────────────────────────────────────────────

def _cloudflared_exe() -> str | None:
    name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
    cand = APP_DIR / name
    if cand.exists():
        return str(cand)
    import shutil
    return shutil.which("cloudflared")


# ── One-click cloudflared install (Settings → Network) ───────────────────
# We don't bundle the 52MB binary; instead the user can fetch the official
# build straight into APP_DIR (where _cloudflared_exe looks first).
_CF_RELEASE_BASE = "https://github.com/cloudflare/cloudflared/releases/latest/download/"


def cloudflared_target_path() -> Path:
    """Where a downloaded cloudflared lands — next to main.py."""
    return APP_DIR / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")


def cloudflared_present() -> bool:
    """True if cloudflared is available (next to main.py or on PATH)."""
    return _cloudflared_exe() is not None


def _cloudflared_asset_name() -> str | None:
    """The release asset filename for this OS/arch, or None if unsupported."""
    import platform
    m = (platform.machine() or "").lower()
    if sys.platform == "win32":
        return "cloudflared-windows-386.exe" if m in ("x86", "i386", "i686") \
            else "cloudflared-windows-amd64.exe"
    if sys.platform.startswith("linux"):
        if "aarch64" in m or "arm64" in m:
            return "cloudflared-linux-arm64"
        if "arm" in m:
            return "cloudflared-linux-arm"
        return "cloudflared-linux-amd64"
    if sys.platform == "darwin":
        return "cloudflared-darwin-arm64.tgz" if ("arm" in m or "aarch64" in m) \
            else "cloudflared-darwin-amd64.tgz"
    return None


def download_cloudflared(progress=None) -> tuple[bool, str]:
    """Download the official cloudflared into APP_DIR. ``progress`` is an
    optional callback(str) for status text. Returns (ok, message). Safe to call
    off the UI thread."""
    def _say(msg: str):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    if cloudflared_present():
        return True, "cloudflared already installed."
    asset = _cloudflared_asset_name()
    if not asset:
        return False, f"No cloudflared build is published for this platform ({sys.platform})."

    url = _CF_RELEASE_BASE + asset
    target = cloudflared_target_path()
    tmp = APP_DIR / (target.name + ".part")
    try:
        _say("Downloading cloudflared…")
        req = urllib.request.Request(url, headers={"User-Agent": "Familiar"})
        with urllib.request.urlopen(req, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    _say(f"Downloading cloudflared… {done * 100 // total}%  "
                         f"({done // (1024 * 1024)}/{total // (1024 * 1024)} MB)")

        if asset.endswith(".tgz"):
            _say("Extracting…")
            import tarfile
            with tarfile.open(tmp) as t:
                member = next((m for m in t.getmembers()
                               if m.name.rsplit("/", 1)[-1] == "cloudflared"), None)
                if member is None:
                    tmp.unlink(missing_ok=True)
                    return False, "Archive did not contain a cloudflared binary."
                member.name = "cloudflared"
                t.extract(member, APP_DIR)
            tmp.unlink(missing_ok=True)
        else:
            os.replace(tmp, target)

        if sys.platform != "win32":
            try:
                os.chmod(target, 0o755)
            except Exception:
                pass
        return True, f"cloudflared installed ✓  ({target.name})"
    except Exception as e:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False, f"Download failed: {e}"


class _Cloudflared:
    """Spawns a quick-tunnel and reads its public hostname from the metrics
    endpoint. Detached so it survives Familiar, with a PID/URL sidecar so a
    restart reuses the same ephemeral URL instead of churning peer configs."""

    def __init__(self, port: int, log=print):
        self.port = port
        self._log = log
        self.url: str | None = None
        self._pid: int | None = None  # pid we manage (spawned OR adopted via reuse)
        self._proc: subprocess.Popen | None = None
        self._meta_file = APP_DIR / ".familiar_cf.json"
        # Legacy sidecars from the pre-JSON format — cleaned up on sight.
        self._legacy_files = (APP_DIR / ".familiar_cf_pid", APP_DIR / ".familiar_cf_url")

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            if sys.platform == "win32":
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                     capture_output=True, text=True, timeout=5,
                                     encoding="utf-8", errors="replace",
                                     creationflags=subprocess.CREATE_NO_WINDOW).stdout
                return str(pid) in out
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    @staticmethod
    def _kill_pid(pid: int):
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                os.kill(pid, 15)
        except Exception:
            pass

    def _read_meta(self) -> dict:
        try:
            meta = json.loads(self._meta_file.read_text(encoding="utf-8"))
            return meta if isinstance(meta, dict) else {}
        except Exception:
            return {}

    def _clear_sidecars(self):
        for f in (self._meta_file, *self._legacy_files):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass

    def _reuse(self) -> bool:
        meta = self._read_meta()
        pid, url, port = meta.get("pid"), meta.get("url", ""), meta.get("port")
        if not (isinstance(pid, int) and url):
            self._clear_sidecars()  # nothing reusable (or legacy format)
            return False
        alive = self._pid_alive(pid)
        if alive and port == self.port:
            self._pid = pid
            self.url = url
            self._log(f"reusing cloudflared tunnel (pid={pid}) {url}")
            return True
        if alive:
            # Port changed in settings: the old tunnel forwards to the wrong
            # port AND holds the metrics port a fresh spawn needs. Replace it.
            self._log(f"stale tunnel forwards to port {port} (want {self.port}) — replacing")
            self._kill_pid(pid)
        self._clear_sidecars()
        return False

    def start(self) -> str | None:
        if self._reuse():
            return self.url
        exe = _cloudflared_exe()
        if not exe:
            self._log("cloudflared not found next to main.py — inbound tunnel disabled")
            return None
        args = [exe, "tunnel", "--url", f"http://127.0.0.1:{self.port}",
                "--no-autoupdate", "--metrics", f"127.0.0.1:{_CF_METRICS_PORT}"]
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        try:
            self._proc = subprocess.Popen(
                args, creationflags=flags,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            self._log(f"cloudflared failed to start: {e}")
            return None
        # Poll the metrics endpoint for the assigned hostname (~up to ~20s).
        for _ in range(28):
            time.sleep(0.7)
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{_CF_METRICS_PORT}/quicktunnel", timeout=2) as r:
                    host = json.loads(r.read().decode()).get("hostname", "")
                if host:
                    self.url = f"https://{host}"
                    self._pid = self._proc.pid
                    try:
                        self._meta_file.write_text(json.dumps(
                            {"pid": self._proc.pid, "url": self.url, "port": self.port}),
                            encoding="utf-8")
                    except Exception:
                        pass
                    self._log(f"tunnel up: {self.url}")
                    return self.url
            except Exception:
                pass
        self._log("cloudflared started but no URL after ~20s")
        return None

    def stop(self):
        # Kill whichever process we manage — freshly spawned OR adopted via
        # _reuse() (the old code orphaned reused tunnels: it deleted the
        # sidecars but had no Popen handle, so Stop left cloudflared running
        # and a later start collided with it on the metrics port).
        pid = self._proc.pid if (self._proc and self._proc.poll() is None) else self._pid
        if pid:
            self._kill_pid(pid)
        self._pid = None
        self._clear_sidecars()


# ── Conversation snapshots (read straight from the thread-safe, GUI-free
#    conversation store; the server thread can serve these without the UI) ──

def _local_conv_list() -> list[dict]:
    # Private conversations (Conversation dialog → Private) are never exposed.
    from core.conversations import list_conversations, is_conversation_private
    return [{"id": c["id"], "name": c.get("name", ""),
             "modified": c.get("modified", 0),
             "message_count": c.get("message_count", 0)}
            for c in list_conversations()
            if not is_conversation_private(c["id"])]


def _conv_workspace_collapsed(conv_id: str) -> bool:
    """Whether this conversation's tool/workspace pane is collapsed on the host,
    read from the UI's per-conversation viewer_state.json (ratio≈0 = closed).
    Lets a mirror adopt the host conversation's open/closed splitter state.
    Defaults to collapsed when unknown."""
    return _conv_viewer_state(conv_id).get("_collapsed", True)


def _conv_viewer_state(conv_id: str) -> dict:
    """The conversation's persisted viewer layout (per-conv) — which tool tab is
    active and whether the pane is collapsed. Read from viewer_state.json so the
    mirror can match the host's tool/splitter state."""
    try:
        vs = json.loads((APP_DIR / "data" / "viewer_state.json")
                        .read_text(encoding="utf-8"))
        st = vs.get(conv_id) or {}
        return {"_collapsed": float(st.get("ratio", 0) or 0) < 0.02,
                "page": int(st.get("workspace_page", 3) or 3)}
    except Exception:
        return {"_collapsed": True, "page": 3}


def _local_conv_snapshot(conv_id: str) -> dict | None:
    from core.conversations import load_conversation, is_conversation_private
    # Private → invisible to peers. This single gate covers /conv/subscribe and
    # /conv/snapshot (both route through here).
    if not conv_id or is_conversation_private(conv_id):
        return None
    data = load_conversation(conv_id)
    if not data:
        return None
    vstate = _conv_viewer_state(conv_id)
    return {"conv_id": conv_id, "name": data.get("name", ""),
            "messages": data.get("messages", []),
            "workspace_collapsed": vstate["_collapsed"],
            "workspace_page": vstate["page"]}   # which tool tab the host is on


# ── Inbound HTTP server ──────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    manager: "NetworkManager" = None  # set on the server instance's owner

    def log_message(self, *a):
        pass  # silence default stderr logging

    def _reply(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_authed(self):
        """Return (ok, body_bytes, parsed_json|None). Returns (None, …) when the
        request was already rejected (oversized) — caller must stop."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            length = 0
        # Cap the body BEFORE reading it — otherwise an unauthenticated caller
        # who finds the tunnel URL could send a huge Content-Length and make us
        # buffer it all in memory before the signature check fails (pre-auth DoS).
        if length < 0 or length > _MAX_BODY_BYTES:
            self._reply(413, {"error": "request too large"})
            self.close_connection = True
            return None, b"", None
        body = self.rfile.read(length) if length else b""
        ts = self.headers.get("X-Timestamp", "")
        sig = self.headers.get("X-Signature", "")
        mgr = _Handler.manager
        if mgr is None or not verify(mgr.secret, body, ts, sig):
            return False, body, None
        try:
            return True, body, (json.loads(body) if body else {})
        except Exception:
            return True, body, {}

    def do_GET(self):
        # WebSocket upgrade (shares this port): a remote terminal stream.
        if self.path.split("?", 1)[0] == "/term/ws" and \
                "websocket" in (self.headers.get("Upgrade", "").lower()):
            self._handle_ws_terminal()
            return
        if self.path == "/ping":
            mgr = _Handler.manager
            self._reply(200, {"ok": True, "node": mgr.node_name if mgr else "",
                              "app": "Familiar"})
        else:
            self._reply(404, {"error": "not found"})

    def _handle_ws_terminal(self):
        """Authenticate, upgrade this HTTP connection to a WebSocket, and run a
        remote-terminal bridge on it until close. Auth signs the request path."""
        from urllib.parse import urlparse, parse_qs
        from core import wsutil
        mgr = _Handler.manager
        ts = self.headers.get("X-Timestamp", "")
        sig = self.headers.get("X-Signature", "")
        # Sign the path (incl. ?conv=…) the same way bodies are signed elsewhere.
        if mgr is None or not verify(mgr.secret, self.path.encode(), ts, sig):
            self._reply(401, {"error": "unauthorized"})
            return
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key:
            self._reply(400, {"error": "missing Sec-WebSocket-Key"})
            return
        conv_id = (parse_qs(urlparse(self.path).query).get("conv") or [""])[0]
        # A remote shell is opt-in PER CONVERSATION (it grants code execution on
        # this machine). Refuse unless the conversation is shared AND explicitly
        # allows a terminal.
        try:
            from core.conversations import (is_conversation_private,
                                            conversation_allows_terminal)
            if is_conversation_private(conv_id) or not conversation_allows_terminal(conv_id):
                self._reply(403, {"error": "remote terminal not permitted for this conversation"})
                return
        except Exception:
            self._reply(403, {"error": "remote terminal unavailable"})
            return
        # 101 Switching Protocols — write the raw handshake, then hijack the sock.
        resp = ("HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {wsutil.accept_key(key)}\r\n\r\n")
        try:
            self.wfile.write(resp.encode())
            self.wfile.flush()
        except Exception:
            return
        self.close_connection = True   # we own the socket now
        try:
            from core.remote_terminal import run_terminal_bridge
            run_terminal_bridge(self.connection, conv_id, log=mgr._log)
        except Exception as e:
            try:
                mgr._log(f"remote terminal bridge error: {e}")
            except Exception:
                pass

    def do_POST(self):
        ok, _body, data = self._read_authed()
        if ok is None:
            return  # already replied 413 (oversized) — body was not read
        if not ok:
            self._reply(401, {"error": "unauthorized"})
            return
        mgr = _Handler.manager
        if self.path == "/ping":
            self._reply(200, {"ok": True, "node": mgr.node_name})
        elif self.path == "/sync":
            # Inbound chat message → "Network: <node>" conversation.
            if mgr and mgr.on_sync:
                try:
                    mgr.on_sync(data)
                except Exception:
                    pass
            self._reply(200, {"ok": True})

        # ── Remote conversations ──
        elif self.path == "/conv/list":
            try:
                self._reply(200, {"ok": True, "node": mgr.node_name,
                                  "convs": _local_conv_list()})
            except Exception as e:
                self._reply(500, {"error": str(e)})
        elif self.path == "/conv/subscribe":
            conv_id = (data or {}).get("conv_id", "")
            reply_url = (data or {}).get("reply_url", "")
            mgr._subscribe_conv(conv_id, reply_url)
            try:
                snap = _local_conv_snapshot(conv_id)
            except Exception as e:
                self._reply(500, {"error": str(e)}); return
            if snap is None:
                self._reply(404, {"error": "no such conversation"})
            else:
                self._reply(200, {"ok": True, **snap})
        elif self.path == "/conv/unsubscribe":
            mgr._unsubscribe_conv((data or {}).get("conv_id", ""),
                                  (data or {}).get("reply_url", ""))
            self._reply(200, {"ok": True})
        elif self.path == "/conv/input":
            # Ack immediately; the turn runs on the host UI thread (its agent,
            # its tools), streaming events back to our subscription. Private
            # conversations refuse remote input.
            d = data or {}
            from core.conversations import is_conversation_private
            if is_conversation_private(d.get("conv_id", "")):
                self._reply(403, {"error": "conversation is private"})
                return
            self._reply(200, {"ok": True})
            if mgr and mgr.on_remote_input:
                threading.Thread(
                    target=lambda: mgr.on_remote_input(
                        d.get("conv_id", ""), d.get("text", ""),
                        d.get("reply_url", "")),
                    daemon=True).start()
        elif self.path == "/conv/event":
            # A host we're mirroring pushed a live update.
            if mgr and mgr.on_conv_event:
                try:
                    mgr.on_conv_event(data or {})
                except Exception:
                    pass
            self._reply(200, {"ok": True})

        # ── Remote workspace files (scoped to a conversation's workspace) ──
        elif self.path == "/fs/list":
            try:
                from core.remote_fs import fs_list
                res = fs_list((data or {}).get("conv_id", ""),
                              (data or {}).get("subpath", ""))
            except Exception as e:
                self._reply(500, {"error": str(e)}); return
            if res is None:
                self._reply(404, {"error": "no such directory in workspace"})
            else:
                self._reply(200, {"ok": True, **res})
        elif self.path == "/fs/read":
            try:
                from core.remote_fs import fs_read
                text, err = fs_read((data or {}).get("conv_id", ""),
                                    (data or {}).get("path", ""))
            except Exception as e:
                self._reply(500, {"error": str(e)}); return
            if text is None:
                self._reply(404, {"error": err})
            else:
                self._reply(200, {"ok": True, "text": text})
        elif self.path == "/fs/write":
            try:
                from core.remote_fs import fs_write
                ok2, err = fs_write((data or {}).get("conv_id", ""),
                                    (data or {}).get("path", ""),
                                    (data or {}).get("content", ""))
            except Exception as e:
                self._reply(500, {"error": str(e)}); return
            self._reply(200 if ok2 else 400, {"ok": ok2, "error": err})
        elif self.path == "/fs/mkdir":
            try:
                from core.remote_fs import fs_mkdir
                ok2, err = fs_mkdir((data or {}).get("conv_id", ""),
                                    (data or {}).get("path", ""))
            except Exception as e:
                self._reply(500, {"error": str(e)}); return
            self._reply(200 if ok2 else 400, {"ok": ok2, "error": err})
        elif self.path == "/fs/delete":
            try:
                from core.remote_fs import fs_delete
                ok2, err = fs_delete((data or {}).get("conv_id", ""),
                                     (data or {}).get("path", ""))
            except Exception as e:
                self._reply(500, {"error": str(e)}); return
            self._reply(200 if ok2 else 400, {"ok": ok2, "error": err})
        elif self.path == "/fs/rename":
            try:
                from core.remote_fs import fs_rename
                ok2, err = fs_rename((data or {}).get("conv_id", ""),
                                     (data or {}).get("path", ""),
                                     (data or {}).get("new_path", ""))
            except Exception as e:
                self._reply(500, {"error": str(e)}); return
            self._reply(200 if ok2 else 400, {"ok": ok2, "error": err})

        # ── Remote tools: Notes / Calendar / Browser ──
        # Notes & calendar are GLOBAL (not per-conversation), so they're gated by
        # a single machine-wide switch rather than the per-conversation Private
        # flag — otherwise marking conversations private would still leak them.
        elif self.path in ("/notes/list", "/notes/save", "/notes/delete",
                           "/calendar/events") and not _tools_shared():
            self._reply(403, {"error": "notes/calendar sharing is disabled on this machine"})
        elif self.path == "/notes/list":
            try:
                from core.remote_tools import notes_list
                self._reply(200, {"ok": True, "notes": notes_list()})
            except Exception as e:
                self._reply(500, {"error": str(e)})
        elif self.path == "/notes/save":
            try:
                from core.remote_tools import notes_save_one
                note = notes_save_one((data or {}).get("note_id", ""),
                                      (data or {}).get("content", ""))
                self._reply(200, {"ok": True, "note": note})
            except Exception as e:
                self._reply(500, {"error": str(e)})
        elif self.path == "/notes/delete":
            try:
                from core.remote_tools import notes_delete
                ok2 = notes_delete((data or {}).get("note_id", ""))
                self._reply(200, {"ok": ok2})
            except Exception as e:
                self._reply(500, {"error": str(e)})
        elif self.path == "/calendar/events":
            try:
                from core.remote_tools import calendar_events
                ev = calendar_events((data or {}).get("year", 0),
                                     (data or {}).get("month", 0))
                self._reply(200, {"ok": True, "events": ev})
            except Exception as e:
                self._reply(500, {"error": str(e)})
        elif self.path == "/browser/url":
            try:
                from core.remote_tools import browser_url
                self._reply(200, {"ok": True,
                                  "url": browser_url((data or {}).get("conv_id", ""))})
            except Exception as e:
                self._reply(500, {"error": str(e)})

        # ── File share ──
        elif self.path == "/files/manifest":
            try:
                from core.file_share import local_manifest, load_tombstones
                self._reply(200, {"ok": True, "files": local_manifest(),
                                  "tombstones": load_tombstones()})
            except Exception as e:
                self._reply(500, {"error": str(e)})
        elif self.path == "/files/get":
            try:
                from core.file_share import read_share_file
                blob = read_share_file((data or {}).get("path", ""))
            except Exception as e:
                self._reply(500, {"error": str(e)}); return
            if blob is None:
                self._reply(404, {"error": "not found"})
            else:
                import base64
                self._reply(200, {"ok": True,
                                  "data": base64.b64encode(blob).decode()})
        else:
            self._reply(404, {"error": "not found"})


# ── Manager ──────────────────────────────────────────────────────────────

class NetworkManager:
    def __init__(self):
        self.running = False
        self.port = 8787
        self.node_name = ""
        self.secret = ""
        self.peers: list[dict] = []
        self.public_url: str = ""
        # When set, Familiar uses THIS as its public address instead of starting
        # cloudflared — for machines already fronted by any tunnel or reverse
        # proxy that forwards to the inbound port.
        self._public_url_override: str = ""
        self.on_sync = None                 # callback(dict) — inbound chat message
        self.on_remote_input = None         # callback(conv_id, text, reply_url) — a
                                            # peer wants THIS host to run a turn
        self.on_conv_event = None           # callback(dict) — a host we're mirroring
                                            # pushed a live conversation update
        # Set by the app: a callable(req: dict) that — on the GUI thread —
        # creates a TerminalAttachment for req['conv_id'] into req['attachment']
        # then sets req['event']. Lets the remote-terminal bridge attach to the
        # host's LIVE shell instead of spawning a fresh one.
        self.terminal_attach_request = None
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._cf: _Cloudflared | None = None
        self._lock = threading.Lock()
        # conv_id -> {reply_url: last_seen_ts}. Who is mirroring each of our
        # conversations and where to push that conversation's live events.
        self._conv_subs: dict[str, dict[str, float]] = {}
        self._subs_lock = threading.Lock()
        # Outbound conversation events are pushed through a single background
        # pump so neither the GUI thread nor an inference thread ever blocks on
        # a subscriber's network latency, and per-conversation event ORDER is
        # preserved (round_start → text → final).
        self._event_q: "queue.Queue" = queue.Queue()
        self._event_pump_thread: threading.Thread | None = None

    def _log(self, msg: str):
        print(f"[network] {msg}", flush=True)

    def start(self, cfg: dict, on_ready=None):
        """Start (or restart) the inbound server + tunnel from a network config
        dict. Runs the slow tunnel handshake on a worker thread; calls
        on_ready(public_url) when the URL is known (or '' on failure)."""
        net = cfg.get("network", {}) if isinstance(cfg.get("network"), dict) else {}
        self.node_name = net.get("node_name", "") or "familiar"
        self.secret = net.get("secret", "")
        self.port = int(net.get("port", 8787))
        self.peers = [p for p in net.get("peers", []) if isinstance(p, dict) and p.get("url")]
        self._public_url_override = (net.get("public_url_override", "") or "").strip().rstrip("/")
        inbound = bool(net.get("inbound_enabled", True))
        auto_tunnel = bool(net.get("auto_tunnel", True))

        threading.Thread(target=self._start_blocking,
                         args=(inbound, auto_tunnel, on_ready), daemon=True).start()

    def _start_blocking(self, inbound, auto_tunnel, on_ready):
        with self._lock:
            self.stop(_keep_lock=True)
            # (Re)start the conversation-event pump.
            self._event_q = queue.Queue()
            self._event_pump_thread = threading.Thread(
                target=self._event_pump, daemon=True, name="conv-event-pump")
            self._event_pump_thread.start()
            if inbound:
                try:
                    _Handler.manager = self
                    self._server = ThreadingHTTPServer(("127.0.0.1", self.port), _Handler)
                    self._server_thread = threading.Thread(
                        target=self._server.serve_forever, daemon=True)
                    self._server_thread.start()
                    self._log(f"inbound server on 127.0.0.1:{self.port}")
                except Exception as e:
                    self._log(f"inbound server failed: {e}")
            url = ""
            if self._public_url_override:
                # An external tunnel/proxy already fronts the inbound port —
                # use its URL and don't start cloudflared.
                url = self._public_url_override
                self._log(f"using external public address: {url}")
            elif inbound and auto_tunnel:
                self._cf = _Cloudflared(self.port, log=self._log)
                url = self._cf.start() or ""
            self.public_url = url
            self.running = True
        # File-share sync runs alongside the link (no-op if folder/peers empty).
        try:
            from core.file_share import start_sync
            start_sync(self)
        except Exception as e:
            self._log(f"file-share sync not started: {e}")
        if on_ready:
            try:
                on_ready(url)
            except Exception:
                pass

    def stop(self, _keep_lock=False):
        def _do():
            if self._server is not None:
                try:
                    self._server.shutdown()
                    self._server.server_close()
                except Exception:
                    pass
                self._server = None
            if self._cf is not None:
                self._cf.stop()
                self._cf = None
            with self._subs_lock:
                self._conv_subs.clear()
            if self._event_pump_thread is not None:
                try:
                    self._event_q.put_nowait(None)   # sentinel stops the pump
                except Exception:
                    pass
                self._event_pump_thread = None
            try:
                from core.file_share import stop_sync
                stop_sync()
            except Exception:
                pass
            self.public_url = ""
            self.running = False
        if _keep_lock:
            _do()
        else:
            with self._lock:
                _do()

    def peer_reachable_count(self) -> tuple[int, int]:
        """(reachable, total) — pings each peer's /ping. Cheap, short timeouts."""
        total = len(self.peers)
        reachable = 0
        for p in self.peers:
            try:
                with urllib.request.urlopen(p["url"].rstrip("/") + "/ping", timeout=3) as r:
                    if r.status == 200:
                        reachable += 1
            except Exception:
                pass
        return reachable, total

    # ── Remote-conversation subscriptions (host side) ─────────────────────
    def _subscribe_conv(self, conv_id: str, reply_url: str):
        if not conv_id or not reply_url:
            return
        with self._subs_lock:
            self._conv_subs.setdefault(conv_id, {})[reply_url] = time.time()

    def _unsubscribe_conv(self, conv_id: str, reply_url: str):
        with self._subs_lock:
            subs = self._conv_subs.get(conv_id)
            if subs:
                subs.pop(reply_url, None)
                if not subs:
                    self._conv_subs.pop(conv_id, None)

    def conv_has_subscribers(self, conv_id: str) -> bool:
        with self._subs_lock:
            return bool(self._conv_subs.get(conv_id))

    def request_terminal_attach(self, conv_id: str, rows: int, cols: int,
                                timeout: float = 5.0):
        """Ask the GUI thread for an attachment to conv_id's live terminal.
        Blocks the calling (bridge) thread up to `timeout`; returns the
        attachment or None (then the bridge spawns a fresh shell instead)."""
        fn = self.terminal_attach_request
        if fn is None:
            return None
        import threading as _t
        req = {"conv_id": conv_id, "rows": rows, "cols": cols,
               "event": _t.Event(), "attachment": None}
        try:
            fn(req)
        except Exception:
            return None
        req["event"].wait(timeout)
        return req.get("attachment")

    def publish_conv_event(self, conv_id: str, event: dict):
        """Queue a live conversation event for delivery to every subscriber of
        conv_id. Non-blocking and a cheap no-op when nobody is mirroring it, so
        the host's normal turn loop / GUI thread can call it unconditionally.
        Actual sending happens on the event pump, preserving order."""
        with self._subs_lock:
            has = bool(self._conv_subs.get(conv_id))
        if not has:
            return
        try:
            self._event_q.put_nowait((conv_id, dict(event)))
        except Exception:
            pass

    def _event_pump(self):
        while True:
            item = self._event_q.get()
            if item is None:               # sentinel → shut the pump down
                return
            conv_id, event = item
            with self._subs_lock:
                targets = list((self._conv_subs.get(conv_id) or {}).keys())
            if not targets:
                continue
            payload = dict(event)
            payload["type"] = "conv_event"
            payload["conv_id"] = conv_id
            dead = []
            for url in targets:
                ok, _resp, detail = _post(url, "/conv/event", dict(payload), timeout=8)
                if not ok:
                    # A subscriber we can't reach. Loud, because this is the
                    # usual cause of "I sent a message but the mirror shows
                    # nothing": the host can't reach the viewer's public address
                    # (one-way reachability / stale tunnel URL / clock skew).
                    self._log(f"conv event to subscriber {url} failed ({detail}); "
                              f"dropping it. The mirroring machine must be "
                              f"reachable at that address.")
                    dead.append(url)
            if dead:
                with self._subs_lock:
                    subs = self._conv_subs.get(conv_id)
                    if subs:
                        for url in dead:
                            subs.pop(url, None)
                        if not subs:
                            self._conv_subs.pop(conv_id, None)


# Module-level singleton.
network_manager = NetworkManager()


# ── Outbound (phase 2) ───────────────────────────────────────────────────

def _load_net_cfg() -> dict:
    """The 'network' section of config.json, read directly (keeps this module
    stdlib-only / GUI-free so headless nodes can import it)."""
    try:
        cfg = json.loads((APP_DIR / "config.json").read_text(encoding="utf-8"))
        net = cfg.get("network")
        return net if isinstance(net, dict) else {}
    except Exception:
        return {}


def _tools_shared() -> bool:
    """Machine-wide switch for sharing global Notes & Calendar with peers
    (config.json → network.share_notes). Defaults to True (the panels mirror
    by default); set False to keep them local-only."""
    net = _load_net_cfg()
    return bool(net.get("share_notes", True))


def outbound_identity() -> tuple[str, str, list[dict]]:
    """(node_name, secret, peers) — live manager values when networking is
    running, else straight from config.json so outbound works standalone."""
    m = network_manager
    if m.running and m.secret:
        return m.node_name, m.secret, list(m.peers)
    net = _load_net_cfg()
    peers = [p for p in net.get("peers", []) if isinstance(p, dict) and p.get("url")]
    return (net.get("node_name") or "familiar"), net.get("secret", ""), peers


def resolve_peer(name_or_url: str) -> dict | None:
    """Find a configured peer by name (case-insensitive) or URL. Returns the
    peer dict ({'name', 'url'}) or None."""
    want = (name_or_url or "").strip()
    if not want:
        return None
    _, _, peers = outbound_identity()
    low = want.lower().rstrip("/")
    for p in peers:
        if (p.get("name", "").lower() == low
                or p.get("url", "").lower().rstrip("/") == low):
            return p
    return None


def _post(base_url: str, path: str, payload: dict,
          timeout: float = 10) -> tuple[bool, dict | None, str]:
    """Sign `payload` and POST it to base_url+path. Returns (ok, json|None, detail).

    The envelope automatically carries this node's name (`from`), `sent_at`, and
    — when our tunnel is up — `reply_url`, so a receiver can answer without
    having us in its peer list. The single chokepoint for every outbound call
    (chat, conversation mirroring, file pulls)."""
    node, secret, _ = outbound_identity()
    if not secret:
        return False, None, "no shared secret configured (Settings → Network)"
    if not base_url:
        return False, None, "no peer URL"
    envelope = dict(payload or {})
    envelope["from"] = node
    envelope["sent_at"] = time.time()
    if network_manager.public_url:
        envelope.setdefault("reply_url", network_manager.public_url)
    body = json.dumps(envelope, ensure_ascii=False).encode()
    ts = str(time.time())
    req = urllib.request.Request(
        base_url.rstrip("/") + path, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Timestamp": ts,
                 "X-Signature": sign(secret, body, ts)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            try:
                resp = json.loads(r.read().decode() or "{}")
            except Exception:
                resp = {}
            if r.status == 200:
                return True, resp, "ok"
            return False, resp, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        detail = "unauthorized — shared secret mismatch (or clocks >30s apart)" \
            if e.code == 401 else f"HTTP {e.code}"
        return False, None, detail
    except Exception as e:
        return False, None, f"{type(e).__name__}: {e}"


def send_to_peer(url: str, payload: dict, timeout: float = 10) -> tuple[bool, str]:
    """Sign a chat `payload` and POST it to a peer's /sync. Returns (ok, detail)."""
    envelope = dict(payload or {})
    envelope.setdefault("type", "chat")
    ok, _resp, detail = _post(url, "/sync", envelope, timeout=timeout)
    return ok, ("delivered" if ok else detail)


def broadcast(payload: dict, timeout: float = 10) -> list[dict]:
    """send_to_peer() to every configured peer. Returns per-peer results:
    [{'name', 'url', 'ok', 'detail'}, ...]."""
    _, _, peers = outbound_identity()
    results = []
    for p in peers:
        ok, detail = send_to_peer(p["url"], dict(payload or {}), timeout=timeout)
        results.append({"name": p.get("name", ""), "url": p["url"],
                        "ok": ok, "detail": detail})
    return results


# ── Remote conversations (mirror a peer's conversation, live) ─────────────

def peer_conv_list(url: str, timeout: float = 8) -> tuple[bool, list, str]:
    """Fetch a peer's conversation list. Returns (ok, convs, detail) where each
    conv is {'id', 'name', 'modified', 'message_count'}."""
    ok, resp, detail = _post(url, "/conv/list", {}, timeout=timeout)
    return ok, ((resp or {}).get("convs") or []), detail


def peer_conv_subscribe(url: str, conv_id: str,
                        timeout: float = 10) -> tuple[bool, dict | None, str]:
    """Subscribe to a peer conversation's live events (the host pushes to our
    reply_url) and get its current snapshot back in one call. Returns
    (ok, snapshot|None, detail); snapshot is {'conv_id','name','messages'}."""
    ok, resp, detail = _post(url, "/conv/subscribe", {"conv_id": conv_id}, timeout=timeout)
    return ok, (resp if ok else None), detail


def peer_conv_unsubscribe(url: str, conv_id: str, timeout: float = 6) -> bool:
    ok, _resp, _detail = _post(url, "/conv/unsubscribe", {"conv_id": conv_id},
                               timeout=timeout)
    return ok


def peer_conv_input(url: str, conv_id: str, text: str,
                    timeout: float = 10) -> tuple[bool, str]:
    """Drive a peer conversation: deliver a user message for the HOST to run a
    turn on (its inference, its tools, committed on the host). Live output comes
    back asynchronously via /conv/event to our subscription."""
    ok, _resp, detail = _post(url, "/conv/input",
                              {"conv_id": conv_id, "text": text}, timeout=timeout)
    return ok, ("sent" if ok else detail)


# ── Remote workspace files (browse/read/edit a peer conversation's workspace) ──

def peer_fs_list(url: str, conv_id: str, subpath: str = "",
                 timeout: float = 10) -> tuple[bool, dict | None, str]:
    """List a directory in a peer conversation's workspace. Returns
    (ok, {root, path, entries}|None, detail)."""
    ok, resp, detail = _post(url, "/fs/list",
                             {"conv_id": conv_id, "subpath": subpath}, timeout=timeout)
    return ok, (resp if ok else None), detail


def peer_fs_read(url: str, conv_id: str, path: str,
                 timeout: float = 20) -> tuple[bool, str, str]:
    """Read a workspace file from a peer. Returns (ok, text, detail)."""
    ok, resp, detail = _post(url, "/fs/read",
                             {"conv_id": conv_id, "path": path}, timeout=timeout)
    if ok and isinstance(resp, dict):
        return True, resp.get("text", ""), "ok"
    return False, "", detail


def peer_fs_write(url: str, conv_id: str, path: str, content: str,
                  timeout: float = 30) -> tuple[bool, str]:
    """Write a workspace file on a peer (host commits it). Returns (ok, detail)."""
    ok, resp, detail = _post(url, "/fs/write",
                             {"conv_id": conv_id, "path": path, "content": content},
                             timeout=timeout)
    if ok:
        return True, "saved"
    return False, ((resp or {}).get("error") if isinstance(resp, dict) else None) or detail


def _fs_mutate(url: str, path_seg: str, payload: dict, timeout: float) -> tuple[bool, str]:
    ok, resp, detail = _post(url, path_seg, payload, timeout=timeout)
    if ok:
        return True, "ok"
    return False, ((resp or {}).get("error") if isinstance(resp, dict) else None) or detail


def peer_fs_mkdir(url: str, conv_id: str, path: str, timeout: float = 10):
    return _fs_mutate(url, "/fs/mkdir", {"conv_id": conv_id, "path": path}, timeout)


def peer_fs_delete(url: str, conv_id: str, path: str, timeout: float = 15):
    return _fs_mutate(url, "/fs/delete", {"conv_id": conv_id, "path": path}, timeout)


def peer_fs_rename(url: str, conv_id: str, path: str, new_path: str, timeout: float = 15):
    return _fs_mutate(url, "/fs/rename",
                      {"conv_id": conv_id, "path": path, "new_path": new_path}, timeout)


# ── Remote tools: Notes / Calendar / Browser ──────────────────────────────

def peer_notes_list(url: str, timeout: float = 8) -> tuple[bool, list]:
    ok, resp, _ = _post(url, "/notes/list", {}, timeout=timeout)
    return ok, ((resp or {}).get("notes") or []) if ok else []


def peer_notes_save(url: str, note_id: str, content: str,
                    timeout: float = 10) -> tuple[bool, dict]:
    ok, resp, _ = _post(url, "/notes/save",
                        {"note_id": note_id, "content": content}, timeout=timeout)
    return ok, ((resp or {}).get("note") or {}) if ok else {}


def peer_notes_delete(url: str, note_id: str, timeout: float = 8) -> bool:
    ok, _resp, _ = _post(url, "/notes/delete", {"note_id": note_id}, timeout=timeout)
    return ok


def peer_calendar_events(url: str, year: int, month: int,
                         timeout: float = 10) -> tuple[bool, dict]:
    ok, resp, _ = _post(url, "/calendar/events",
                        {"year": year, "month": month}, timeout=timeout)
    return ok, ((resp or {}).get("events") or {}) if ok else {}


def peer_browser_url(url: str, conv_id: str, timeout: float = 8) -> tuple[bool, str]:
    ok, resp, _ = _post(url, "/browser/url", {"conv_id": conv_id}, timeout=timeout)
    return ok, ((resp or {}).get("url") or "") if ok else ""


def open_remote_terminal(url: str, conv_id: str, timeout: float = 15):
    """Open a WebSocket to a peer's remote shell for `conv_id`, scoped to that
    conversation's workspace on the host. Returns a connected socket (use
    core.wsutil.send_frame(mask=True)/recv_frame), or raises ConnectionError."""
    from core import wsutil
    _node, secret, _ = outbound_identity()
    if not secret:
        raise ConnectionError("no shared secret configured")
    if not url:
        raise ConnectionError("no peer URL")
    path = f"/term/ws?conv={conv_id}"
    ts = str(time.time())
    sig = sign(secret, path.encode(), ts)
    return wsutil.connect(url.rstrip("/") + path,
                          headers={"X-Timestamp": ts, "X-Signature": sig},
                          timeout=timeout)
