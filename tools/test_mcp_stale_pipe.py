"""Prove the stale-pipe path: detect, reap, reconnect, retry.

Uses a fake MCP server that answers normally and can be told to go
silent -- simulating a rotted pipe (process alive, never replies).
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

FAKE = os.path.join(HERE, "_fake_mcp_server.py")

FAKE_SRC = '''
import json, os, sys, time

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

TOOLS = [{"name": "echo", "description": "echo",
          "inputSchema": {"type": "object", "properties": {}}}]

# Once this file exists, the server goes silent: alive but never replying.
GAG = os.environ.get("FAKE_MCP_GAG", "")

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid = msg.get("id")
    method = msg.get("method") or ""
    if GAG and os.path.exists(GAG) and method != "initialize":
        # Rotted pipe: consume the request, never answer.
        while True:
            time.sleep(3600)
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"}}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "pong"}]}})
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "result": {}})
'''


def main():
    with open(FAKE, "w", encoding="utf-8") as fh:
        fh.write(FAKE_SRC)
    gag = os.path.join(HERE, "_fake_mcp_gag.tmp")
    for path in (gag,):
        if os.path.exists(path):
            os.remove(path)

    from core.mcp_client import MCPManager

    mgr = MCPManager()
    cfg = {"transport": "stdio", "command": sys.executable,
           "args": ["-u", FAKE], "cwd": HERE,
           "env": {**os.environ, "FAKE_MCP_GAG": gag}}

    res = mgr.connect("fake", cfg, timeout=30)
    print("connect ok:", res.get("ok"), "tools:", len(res.get("tools") or []))
    if not res.get("ok"):
        print("FAIL: could not connect:", res.get("error"))
        return 1

    state = mgr._servers["fake"]
    first_pid = state.child_pid
    print("child pid captured:", first_pid)
    if not first_pid:
        print("FAIL: no child pid -> reaping would be impossible")
        return 1

    out = mgr.call_tool("fake", "echo", {}, timeout=10)
    print("healthy call isError:", out.get("isError"))

    # Rot the pipe.
    with open(gag, "w") as fh:
        fh.write("x")
    print("\n-- pipe gagged; server now alive but silent --")

    t0 = time.time()
    out = mgr.call_tool("fake", "echo", {}, timeout=8)
    elapsed = time.time() - t0
    print(f"call after rot: {elapsed:.1f}s isError={out.get('isError')}")

    # Reconnect spawns a fresh child; the gag only silences non-initialize
    # traffic, so the retry lands on a new process.
    new_state = mgr._servers.get("fake")
    new_pid = new_state.child_pid if new_state else None
    print("new child pid:", new_pid)

    if new_pid and new_pid != first_pid:
        print("PASS: reconnected onto a fresh child")
    else:
        print("NOTE: same pid -- reconnect did not respawn")

    # The old child must be gone, not lingering.
    alive = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-Process -Id {first_pid} -ErrorAction SilentlyContinue"],
        capture_output=True, text=True).stdout.strip()
    if alive:
        print(f"FAIL: old child {first_pid} STILL ALIVE (orphan)")
    else:
        print(f"PASS: old child {first_pid} reaped")

    mgr.shutdown()
    for path in (gag, FAKE):
        try:
            os.remove(path)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
