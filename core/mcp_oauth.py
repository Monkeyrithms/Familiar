"""
OAuth 2.0 Authorization Code flow (with PKCE) for MCP HTTP servers.

On first connect, opens the user's browser to the authorization endpoint,
catches the redirect on a loopback HTTP server, exchanges the code for
tokens, and stores them in data/mcp_tokens.json. On subsequent connects,
refreshes the access token if expired.

Per-server config (inside config.json's mcp_servers.<name>.oauth):
    {
        "client_id": "...",                     required
        "client_secret": "...",                 optional (public clients omit)
        "authorization_endpoint": "https://...", required
        "token_endpoint": "https://...",         required
        "scope": "read write",                   optional
        "redirect_port": 7860,                   optional (auto if omitted)
        "audience": "...",                       optional (some providers)
        "extra_auth_params": {"prompt": "consent"}  optional
    }

This module is intentionally dependency-light: stdlib only + `requests` (which
/agent/ already uses elsewhere).
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

_TOKEN_PATH = Path(__file__).parent.parent / "data" / "mcp_tokens.json"
_TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)

_STORE_LOCK = threading.Lock()
_REFRESH_SKEW = 60  # seconds before expiry to trigger refresh


# -------------------- token storage --------------------

def _load_store() -> dict:
    if not _TOKEN_PATH.exists():
        return {}
    try:
        with open(_TOKEN_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_store(data: dict) -> None:
    tmp = _TOKEN_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    tmp.replace(_TOKEN_PATH)


def _store_tokens(server: str, tokens: dict) -> None:
    with _STORE_LOCK:
        data = _load_store()
        data[server] = tokens
        _save_store(data)


def _load_tokens(server: str) -> dict | None:
    with _STORE_LOCK:
        return _load_store().get(server)


def clear_tokens(server: str) -> bool:
    with _STORE_LOCK:
        data = _load_store()
        if server in data:
            del data[server]
            _save_store(data)
            return True
        return False


# -------------------- PKCE helpers --------------------

def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


# -------------------- loopback callback server --------------------

class _CallbackHandler(BaseHTTPRequestHandler):
    # Set by factory
    result: dict = {}
    expected_state: str = ""

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", "/callback"):
            self.send_response(404); self.end_headers(); return
        qs = urllib.parse.parse_qs(parsed.query)
        state = (qs.get("state") or [""])[0]
        code = (qs.get("code") or [""])[0]
        err = (qs.get("error") or [""])[0]

        body: bytes
        if err:
            self.result["error"] = err
            self.result["error_description"] = (qs.get("error_description") or [""])[0]
            body = b"<h2>Authorization failed</h2><p>You can close this tab.</p>"
        elif state != self.expected_state:
            self.result["error"] = "state_mismatch"
            body = b"<h2>State mismatch</h2><p>You can close this tab.</p>"
        elif not code:
            self.result["error"] = "missing_code"
            body = b"<h2>No code returned</h2><p>You can close this tab.</p>"
        else:
            self.result["code"] = code
            body = b"<h2>Authorization complete</h2><p>You can close this tab.</p>"

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a, **kw):  # silence
        return


def _pick_port(preferred: int | None) -> int:
    if preferred:
        return int(preferred)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_auth_flow(server_name: str, cfg: dict, timeout: float = 300) -> dict:
    """Interactive flow. Returns token dict or raises."""
    client_id = cfg.get("client_id")
    auth_ep = cfg.get("authorization_endpoint")
    token_ep = cfg.get("token_endpoint")
    if not (client_id and auth_ep and token_ep):
        raise ValueError("oauth config needs client_id, authorization_endpoint, token_endpoint")

    port = _pick_port(cfg.get("redirect_port"))
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    state = _b64url(secrets.token_bytes(16))
    verifier, challenge = _pkce_pair()

    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if cfg.get("scope"):
        params["scope"] = cfg["scope"]
    if cfg.get("audience"):
        params["audience"] = cfg["audience"]
    extra = cfg.get("extra_auth_params") or {}
    if isinstance(extra, dict):
        params.update(extra)

    auth_url = f"{auth_ep}?{urllib.parse.urlencode(params)}"

    # Per-flow handler class to isolate state.
    class Handler(_CallbackHandler):
        result: dict = {}
        expected_state = state

    Handler.result = {}

    httpd = HTTPServer(("127.0.0.1", port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="mcp-oauth-cb")
    t.start()
    try:
        print(f"[mcp:{server_name}] Opening browser for OAuth: {auth_url}")
        try:
            webbrowser.open(auth_url)
        except Exception as e:
            print(f"[mcp:{server_name}] Couldn't open browser ({e}). Visit URL manually.")

        deadline = time.time() + timeout
        while time.time() < deadline and not Handler.result:
            time.sleep(0.2)
        if not Handler.result:
            raise TimeoutError("OAuth flow timed out waiting for callback")
        if "error" in Handler.result:
            raise RuntimeError(
                f"OAuth error: {Handler.result['error']} "
                f"{Handler.result.get('error_description','')}"
            )
        code = Handler.result["code"]
    finally:
        try:
            httpd.shutdown()
            httpd.server_close()
        except Exception:
            pass

    # Exchange code for tokens.
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]
    r = requests.post(token_ep, data=data, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Token exchange failed: {r.status_code} {r.text}")
    tokens = r.json()
    tokens["_obtained_at"] = int(time.time())
    tokens["_client_id"] = client_id
    return tokens


def _refresh(cfg: dict, tokens: dict) -> dict | None:
    """Refresh tokens. Returns new dict or None on failure."""
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        return None
    token_ep = cfg.get("token_endpoint")
    client_id = cfg.get("client_id") or tokens.get("_client_id")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if cfg.get("client_secret"):
        data["client_secret"] = cfg["client_secret"]
    if cfg.get("scope"):
        data["scope"] = cfg["scope"]
    try:
        r = requests.post(token_ep, data=data, timeout=30)
        if not r.ok:
            return None
        new = r.json()
        # Some IdPs omit refresh_token on refresh — preserve prior one.
        new.setdefault("refresh_token", refresh_token)
        new["_obtained_at"] = int(time.time())
        new["_client_id"] = client_id
        return new
    except Exception:
        return None


def _is_expired(tokens: dict) -> bool:
    exp_in = tokens.get("expires_in")
    got = tokens.get("_obtained_at", 0)
    if not exp_in or not got:
        return False  # if we don't know, trust it until a 401 forces re-auth
    return time.time() >= (got + int(exp_in) - _REFRESH_SKEW)


# -------------------- public API --------------------

def get_access_token(server: str, cfg: dict) -> str | None:
    """Return a valid access_token for `server`, running the OAuth flow if needed."""
    tokens = _load_tokens(server)
    if tokens:
        if _is_expired(tokens):
            refreshed = _refresh(cfg, tokens)
            if refreshed:
                _store_tokens(server, refreshed)
                tokens = refreshed
            else:
                # Refresh failed — fall through to full flow.
                tokens = None
        if tokens and tokens.get("access_token"):
            return tokens["access_token"]

    # No usable tokens — run full auth flow.
    tokens = _run_auth_flow(server, cfg)
    if not tokens.get("access_token"):
        raise RuntimeError(f"OAuth flow returned no access_token: {tokens}")
    _store_tokens(server, tokens)
    return tokens["access_token"]
