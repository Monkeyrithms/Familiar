"""
Remote terminal — host side. A peer connects over a WebSocket and gets a real
shell on THIS machine, scoped to the mirrored conversation's workspace
directory (like an SSH session, but over the existing authenticated tunnel).

Design: a FRESH PTY per connection, owned entirely by this bridge on plain
threads (no Qt), so it's decoupled from the host's own terminal UI and its
ConPTY threading rules are respected — the spawning/handler thread does all
writes, a separate reader thread does all reads. Output → binary WS frames;
binary WS frames → PTY input; text WS frames → control (JSON, e.g. resize).
"""

from __future__ import annotations

import json
import os
import sys
import threading

from core import wsutil


def _spawn_pty(cwd: str, rows: int = 24, cols: int = 80):
    """Spawn a shell PTY. Returns (proc, is_str) — is_str True when the backend
    speaks str (pywinpty) vs bytes (ptyprocess)."""
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    if sys.platform == "win32":
        from winpty import PtyProcess
        argv = env.get("COMSPEC", "cmd.exe")
        proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
        return proc, True
    from ptyprocess import PtyProcess
    argv = ["/bin/bash", "-i"] if os.path.exists("/bin/bash") else ["/bin/sh", "-i"]
    proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
    return proc, False


def _run_attached(sock, attachment, log=print) -> None:
    """Mirror an existing host shell over the WebSocket: replay its scrollback,
    stream live output, and forward the viewer's keystrokes/resize to it."""
    stop = threading.Event()

    # Tell the viewer the host's dimensions so its renderer lines up, then
    # replay the recent scrollback (with ANSI) so it sees the live state.
    try:
        rows, cols = attachment.dims()
        wsutil.send_frame(sock, json.dumps({"dims": [rows, cols]}).encode(),
                          wsutil.OP_TEXT, mask=False)
        hist = attachment.history()
        if hist:
            wsutil.send_frame(sock, hist.encode("utf-8", "replace"),
                              wsutil.OP_BINARY, mask=False)
    except Exception:
        pass

    def sender():
        while not stop.is_set():
            data = attachment.next_output(timeout=0.2)
            if data is None:
                continue
            raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
            try:
                wsutil.send_frame(sock, raw, wsutil.OP_BINARY, mask=False)
            except Exception:
                break
        stop.set()

    st = threading.Thread(target=sender, daemon=True, name="remote-term-attach-send")
    st.start()
    try:
        while not stop.is_set():
            opcode, payload = wsutil.recv_frame(sock)
            if opcode == wsutil.OP_CLOSE:
                break
            if opcode == wsutil.OP_PING:
                wsutil.send_frame(sock, payload, wsutil.OP_PONG, mask=False)
            elif opcode == wsutil.OP_BINARY:
                attachment.write(payload.decode("utf-8", "replace"))
            elif opcode == wsutil.OP_TEXT:
                try:
                    msg = json.loads(payload.decode("utf-8", "replace"))
                    rc = msg.get("resize")
                    if isinstance(rc, (list, tuple)) and len(rc) == 2:
                        attachment.resize(int(rc[0]), int(rc[1]))
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        stop.set()
        try:
            attachment.detach()
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass


def run_terminal_bridge(sock, conv_id: str, log=print) -> None:
    """Drive a remote terminal over an already-upgraded WebSocket `sock` until
    it closes. Blocks the calling (per-connection) thread.

    Preference: ATTACH to the conversation's live shell (so the viewer sees the
    host's actual session — e.g. an agent already running — with its scrollback,
    and their keystrokes drive it). Falls back to spawning a fresh shell in the
    workspace when there's no live session."""
    from core.network import network_manager
    attachment = None
    try:
        attachment = network_manager.request_terminal_attach(conv_id, 24, 80)
    except Exception:
        attachment = None
    if attachment is not None:
        _run_attached(sock, attachment, log)
        return

    from core.remote_fs import workspace_root
    root = workspace_root(conv_id)          # None if private / missing
    if root is None:
        try:
            wsutil.send_frame(sock, b"[remote terminal unavailable for this conversation]\r\n",
                              wsutil.OP_BINARY, mask=False)
            wsutil.send_frame(sock, b"", wsutil.OP_CLOSE, mask=False)
        except Exception:
            pass
        return

    try:
        proc, is_str = _spawn_pty(str(root))
    except Exception as e:
        log(f"[remote-term] spawn failed: {e}")
        try:
            wsutil.send_frame(sock, f"[shell spawn failed: {e}]\r\n".encode(),
                              wsutil.OP_BINARY, mask=False)
            wsutil.send_frame(sock, b"", wsutil.OP_CLOSE, mask=False)
        except Exception:
            pass
        return

    stop = threading.Event()

    def reader():
        while not stop.is_set():
            try:
                data = proc.read(65536)
            except EOFError:
                break
            except Exception:
                break
            if not data:
                break
            raw = data.encode("utf-8", "replace") if isinstance(data, str) else data
            try:
                wsutil.send_frame(sock, raw, wsutil.OP_BINARY, mask=False)
            except Exception:
                break
        stop.set()
        try:
            wsutil.send_frame(sock, b"", wsutil.OP_CLOSE, mask=False)
        except Exception:
            pass

    rt = threading.Thread(target=reader, daemon=True, name="remote-term-reader")
    rt.start()

    try:
        while not stop.is_set():
            opcode, payload = wsutil.recv_frame(sock)
            if opcode == wsutil.OP_CLOSE:
                break
            if opcode == wsutil.OP_PING:
                wsutil.send_frame(sock, payload, wsutil.OP_PONG, mask=False)
                continue
            if opcode == wsutil.OP_BINARY:
                # Raw keystrokes → PTY input (writes happen on THIS thread).
                try:
                    proc.write(payload.decode("utf-8", "replace") if is_str else payload)
                except Exception:
                    break
            elif opcode == wsutil.OP_TEXT:
                # Control JSON, e.g. {"resize":[rows,cols]}.
                try:
                    msg = json.loads(payload.decode("utf-8", "replace"))
                    rc = msg.get("resize")
                    if isinstance(rc, (list, tuple)) and len(rc) == 2:
                        proc.setwinsize(int(rc[0]), int(rc[1]))
                except Exception:
                    pass
    except Exception:
        pass
    finally:
        stop.set()
        try:
            proc.terminate(force=True)
        except Exception:
            try:
                proc.kill(9)
            except Exception:
                pass
        try:
            sock.close()
        except Exception:
            pass
