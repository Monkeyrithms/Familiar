"""
Minimal RFC 6455 WebSocket helpers (stdlib only).

Just enough to run a single bidirectional binary/text stream over the SAME port
as the HTTP server — the tunnel (cloudflared/ngrok) only forwards one port, so
the WebSocket upgrade is handled by hijacking an HTTP connection rather than
running a second server. No fragmentation/extension support (terminal frames are
small and self-contained), which keeps this tiny and dependency-free.

Opcodes used: 0x1 text (control JSON, e.g. resize), 0x2 binary (raw bytes),
0x8 close, 0x9 ping, 0xA pong.
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def accept_key(sec_websocket_key: str) -> str:
    """The Sec-WebSocket-Accept value for a client's Sec-WebSocket-Key."""
    digest = hashlib.sha1((sec_websocket_key + _WS_GUID).encode()).digest()
    return base64.b64encode(digest).decode()


def _recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf += chunk
    return bytes(buf)


def send_frame(sock, data: bytes, opcode: int = OP_BINARY, *, mask: bool) -> None:
    """Send one WebSocket frame. Clients MUST mask (mask=True); servers MUST
    NOT (mask=False)."""
    n = len(data)
    out = bytearray([0x80 | opcode])           # FIN + opcode
    mbit = 0x80 if mask else 0
    if n < 126:
        out.append(mbit | n)
    elif n < 65536:
        out.append(mbit | 126)
        out += struct.pack(">H", n)
    else:
        out.append(mbit | 127)
        out += struct.pack(">Q", n)
    if mask:
        mkey = os.urandom(4)
        out += mkey
        out += bytes(b ^ mkey[i % 4] for i, b in enumerate(data))
    else:
        out += data
    sock.sendall(bytes(out))


def recv_frame(sock) -> tuple[int, bytes]:
    """Read one frame → (opcode, payload). Unmasks client→server payloads."""
    b0, b1 = _recv_exact(sock, 2)
    opcode = b0 & 0x0F
    masked = bool(b1 & 0x80)
    ln = b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    mkey = _recv_exact(sock, 4) if masked else b""
    payload = _recv_exact(sock, ln) if ln else b""
    if masked and payload:
        payload = bytes(b ^ mkey[i % 4] for i, b in enumerate(payload))
    return opcode, payload


def connect(url: str, headers: dict | None = None, timeout: float = 15):
    """Client handshake. Accepts ws://, wss://, http(s):// (https→wss). Returns a
    connected socket ready for send_frame(mask=True)/recv_frame, or raises."""
    u = urlparse(url)
    secure = u.scheme in ("wss", "https")
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if secure else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    key = base64.b64encode(os.urandom(16)).decode()
    lines = [f"GET {path} HTTP/1.1", f"Host: {host}", "Upgrade: websocket",
             "Connection: Upgrade", f"Sec-WebSocket-Key: {key}",
             "Sec-WebSocket-Version: 13"]
    for k, v in (headers or {}).items():
        lines.append(f"{k}: {v}")
    req = ("\r\n".join(lines) + "\r\n\r\n").encode()

    sock = socket.create_connection((host, port), timeout=timeout)
    if secure:
        ctx = ssl.create_default_context()
        sock = ctx.wrap_socket(sock, server_hostname=host)
    sock.sendall(req)

    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("closed during WebSocket handshake")
        resp += chunk
        if len(resp) > 65536:
            raise ConnectionError("handshake response too large")
    status_line = resp.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    if "101" not in status_line:
        raise ConnectionError(f"WebSocket handshake rejected: {status_line}")
    sock.settimeout(None)
    return sock
