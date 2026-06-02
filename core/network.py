"""
Familiar networking engine — phase 1: bring the link UP.

Responsibilities (this phase):
  * Start a small HMAC-authenticated inbound HTTP server on the configured port.
  * Bring up a cloudflared quick-tunnel pointed at it and discover the public
    URL via cloudflared's metrics `/quicktunnel` endpoint (robust — no log
    scraping). Reuses cloudflared.exe shipped next to main.py.
  * Expose the public URL + a peer-reachability check so the UI can populate the
    "Your public address" field and the connection-count indicator.

Chat-message mirroring + memory-stream sync land on top of this in later phases;
the inbound server already exposes /ping and a stub /sync so peers can be
reached and authenticated now.

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
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent      # Apps/Agent
_CF_METRICS_PORT = 20191                                # local-only metrics port


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
        self._proc: subprocess.Popen | None = None
        self._pid_file = APP_DIR / ".familiar_cf_pid"
        self._url_file = APP_DIR / ".familiar_cf_url"

    def _reuse(self) -> bool:
        try:
            pid = int(self._pid_file.read_text().strip())
            url = self._url_file.read_text().strip()
        except Exception:
            return False
        if not url:
            return False
        # PID still alive?
        try:
            if sys.platform == "win32":
                out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                     capture_output=True, text=True, timeout=5).stdout
                alive = str(pid) in out
            else:
                os.kill(pid, 0)
                alive = True
        except Exception:
            alive = False
        if alive:
            self.url = url
            self._log(f"reusing cloudflared tunnel (pid={pid}) {url}")
            return True
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
                    try:
                        self._pid_file.write_text(str(self._proc.pid))
                        self._url_file.write_text(self.url)
                    except Exception:
                        pass
                    self._log(f"tunnel up: {self.url}")
                    return self.url
            except Exception:
                pass
        self._log("cloudflared started but no URL after ~20s")
        return None

    def stop(self):
        if self._proc and self._proc.poll() is None:
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(self._proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    self._proc.terminate()
            except Exception:
                pass
        for f in (self._pid_file, self._url_file):
            try:
                f.unlink(missing_ok=True)
            except Exception:
                pass


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
        """Return (ok, body_bytes, parsed_json|None)."""
        length = int(self.headers.get("Content-Length", 0) or 0)
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
        if self.path == "/ping":
            mgr = _Handler.manager
            self._reply(200, {"ok": True, "node": mgr.node_name if mgr else "",
                              "app": "Familiar"})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        ok, _body, data = self._read_authed()
        if not ok:
            self._reply(401, {"error": "unauthorized"})
            return
        if self.path == "/ping":
            self._reply(200, {"ok": True, "node": _Handler.manager.node_name})
        elif self.path == "/sync":
            # Chat-event intake — wired to conversation mirroring in phase 2.
            mgr = _Handler.manager
            if mgr and mgr.on_sync:
                try:
                    mgr.on_sync(data)
                except Exception:
                    pass
            self._reply(200, {"ok": True})
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
        self.on_sync = None                 # callback(dict) — set by the app (phase 2)
        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._cf: _Cloudflared | None = None
        self._lock = threading.Lock()

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
        inbound = bool(net.get("inbound_enabled", True))
        auto_tunnel = bool(net.get("auto_tunnel", True))

        threading.Thread(target=self._start_blocking,
                         args=(inbound, auto_tunnel, on_ready), daemon=True).start()

    def _start_blocking(self, inbound, auto_tunnel, on_ready):
        with self._lock:
            self.stop(_keep_lock=True)
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
            if inbound and auto_tunnel:
                self._cf = _Cloudflared(self.port, log=self._log)
                url = self._cf.start() or ""
            self.public_url = url
            self.running = True
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


# Module-level singleton.
network_manager = NetworkManager()
