"""
Remote terminal (viewer side) — a live shell running on a PEER's machine,
rendered with the same pyte-backed PtyTerminalView as the local terminal but
driven over a WebSocket instead of a local PTY.

Output bytes from the host are decoded (incrementally, so multi-byte UTF-8 split
across frames survives) and fed to the view; the view's keystrokes and resize
events are framed back to the host. All socket I/O is off the GUI thread; bytes
cross back via Qt signals.
"""

from __future__ import annotations

import codecs
import json
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QLabel

from core import wsutil
from ui.pty_terminal import PtyTerminalView
from ui.theme import PALETTE


class RemoteTerminalWidget(QWidget):
    """Drop-in terminal page that talks to a host shell over a WebSocket."""

    _data_in = pyqtSignal(str)      # host→view (marshalled to GUI thread)
    _status = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._banner = QLabel("")
        self._banner.setObjectName("RemoteTermBanner")
        lay.addWidget(self._banner)
        self._view = PtyTerminalView(self)
        lay.addWidget(self._view, 1)

        self._sock = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

        self._data_in.connect(self._view.feed)
        self._status.connect(self._banner.setText)
        self._view.key_input.connect(self._on_key)
        self._view.resize_requested.connect(self._on_resize)
        self._apply_banner_style()

    def _apply_banner_style(self):
        p = PALETTE
        self._banner.setStyleSheet(
            f"QLabel#RemoteTermBanner {{ background:{p.get('panel','#0c0c0c')};"
            f" color:{p.get('muted_text','#888')};"
            f" border-bottom:1px solid {p.get('border','#333')};"
            f" font:8pt Consolas; padding:3px 8px; }}")

    # ── Lifecycle ─────────────────────────────────────────────────────
    def connect_to(self, peer_url: str, conv_id: str, peer_name: str = "") -> None:
        self.disconnect_now()
        self._stop = threading.Event()
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._banner.setText(f"🌐 remote shell on [{peer_name}] — connecting…")
        threading.Thread(target=self._run, args=(peer_url, conv_id),
                         daemon=True, name="remote-term-client").start()

    def disconnect_now(self) -> None:
        self._stop.set()
        s, self._sock = self._sock, None
        if s is not None:
            try:
                wsutil.send_frame(s, b"", wsutil.OP_CLOSE, mask=True)
            except Exception:
                pass
            try:
                s.close()
            except Exception:
                pass

    # ── WS read loop (worker thread) ──────────────────────────────────
    def _run(self, peer_url: str, conv_id: str) -> None:
        from core.network import open_remote_terminal
        try:
            sock = open_remote_terminal(peer_url, conv_id, timeout=15)
        except Exception as e:
            self._status.emit(f"⚠ remote shell unavailable: {e}")
            return
        self._sock = sock
        self._status.emit("🌐 remote shell — connected · commands run on the host")
        # Tell the host our current size, then stream.
        self._send_resize(self._view._rows, self._view._cols)
        while not self._stop.is_set():
            try:
                op, data = wsutil.recv_frame(sock)
            except Exception:
                break
            if op == wsutil.OP_CLOSE:
                break
            if op == wsutil.OP_BINARY and data:
                self._data_in.emit(self._decoder.decode(data))
            elif op == wsutil.OP_PING:
                self._send_raw(data, wsutil.OP_PONG)
        if not self._stop.is_set():
            self._status.emit("🌐 remote shell — disconnected")

    # ── view → host ───────────────────────────────────────────────────
    def _on_key(self, seq: str) -> None:
        self._send_raw(seq.encode("utf-8"), wsutil.OP_BINARY)

    def _on_resize(self, rows: int, cols: int) -> None:
        self._send_resize(rows, cols)

    def _send_resize(self, rows: int, cols: int) -> None:
        self._send_raw(json.dumps({"resize": [int(rows), int(cols)]}).encode(),
                       wsutil.OP_TEXT)

    def _send_raw(self, data: bytes, opcode: int) -> None:
        sock = self._sock
        if sock is None:
            return
        with self._send_lock:
            try:
                wsutil.send_frame(sock, data, opcode, mask=True)
            except Exception:
                pass

    def focus_active_input(self) -> None:
        self._view.setFocus(Qt.FocusReason.OtherFocusReason)

    def apply_theme(self) -> None:
        self._apply_banner_style()
        self._view.apply_theme()
