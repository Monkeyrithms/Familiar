"""
familiar_node — headless Familiar network node (no GUI, stdlib only).

Runs the same inbound server + cloudflared tunnel the desktop app uses, prints
every authenticated inbound message, and lets you send from stdin. Built for
testing the peer mesh from a VPS / server where the Qt app can't run.

Setup (Linux):
    mkdir -p ~/familiar/core
    # copy core/network.py and core/familiar_node.py into ~/familiar/core/
    cd ~/familiar
    curl -L -o cloudflared \
      https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
    chmod +x cloudflared

Run (from the folder CONTAINING core/ — cloudflared is looked up next to it):
    python3 core/familiar_node.py --name vps --secret 'SAME_SECRET_AS_PC' \
        [--port 8787] [--peer https://xxx.trycloudflare.com] [--echo]

  --echo    auto-acknowledge every inbound chat back to its sender
  stdin     type a line and press Enter to broadcast it to --peer targets;
            'NAME: text' sends to the configured peer named NAME.
"""

import argparse
import os
import sys

# This file lives in core/; put its PARENT on sys.path so `core.network`
# resolves whether it's run as `python core/familiar_node.py` or
# `python -m core.familiar_node`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.network import network_manager, resolve_peer, send_to_peer, broadcast  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Headless Familiar network node")
    ap.add_argument("--name", default="node", help="this node's name (shown to peers)")
    ap.add_argument("--secret", default=os.environ.get("FAMILIAR_SECRET", ""),
                    help="shared secret (or set FAMILIAR_SECRET)")
    ap.add_argument("--port", type=int, default=8787, help="local inbound port")
    ap.add_argument("--peer", action="append", default=[], metavar="URL",
                    help="peer base URL (repeatable)")
    ap.add_argument("--no-tunnel", action="store_true",
                    help="skip cloudflared (local/LAN testing only)")
    ap.add_argument("--echo", action="store_true",
                    help="auto-reply to every inbound chat (round-trip test)")
    args = ap.parse_args()

    if not args.secret:
        ap.error("--secret (or FAMILIAR_SECRET) is required")

    def on_sync(data: dict):
        node = data.get("from", "?")
        if data.get("type") == "chat":
            print(f"<< [{node}] {data.get('message', '')}", flush=True)
            if args.echo:
                url = (resolve_peer(node) or {}).get("url") or data.get("reply_url", "")
                if url:
                    ok, detail = send_to_peer(
                        url, {"type": "chat",
                              "message": f"echo from {args.name}: {data.get('message', '')}"})
                    print(f">> echo to {node}: {'ok' if ok else detail}", flush=True)
                else:
                    print(f"!! no route back to {node} (not in --peer list, "
                          f"no reply_url in envelope)", flush=True)
        else:
            print(f"<< [{node}] {data}", flush=True)

    network_manager.on_sync = on_sync
    network_manager.start(
        {"network": {
            "node_name": args.name, "secret": args.secret, "port": args.port,
            "inbound_enabled": True, "auto_tunnel": not args.no_tunnel,
            "peers": [{"name": f"peer{i + 1}", "url": u}
                      for i, u in enumerate(args.peer)],
        }},
        on_ready=lambda url: print(
            f"PUBLIC URL: {url}" if url else
            "PUBLIC URL: (none — tunnel off or cloudflared failed; "
            "inbound still listens on 127.0.0.1)", flush=True))

    print(f"node '{args.name}' starting on 127.0.0.1:{args.port} — "
          f"Ctrl+C to stop. Type to broadcast; 'peerN: text' to target one peer.",
          flush=True)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            target, _, rest = line.partition(":")
            peer = resolve_peer(target.strip()) if rest else None
            if peer:
                ok, detail = send_to_peer(peer["url"], {"type": "chat",
                                                        "message": rest.strip()})
                print(f">> {peer.get('name')}: {'ok' if ok else detail}", flush=True)
            else:
                results = broadcast({"type": "chat", "message": line})
                if not results:
                    print(">> no peers configured (--peer URL)", flush=True)
                for r in results:
                    print(f">> {r['name'] or r['url']}: "
                          f"{'ok' if r['ok'] else r['detail']}", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        network_manager.stop()
        print("stopped.", flush=True)


if __name__ == "__main__":
    main()
