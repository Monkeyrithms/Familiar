"""
peer_network — let the agent talk to other Familiar nodes over the
cloudflared mesh (Settings → Network).

Actions:
  * status — networking state: running, this node's public URL, configured
    peers and whether each is currently reachable.
  * send   — deliver a message to one peer (by name or URL) or, with no peer,
             broadcast to every configured peer. Messages arrive in the
             receiver's "Network: <node>" conversation (or its headless
             console) as HMAC-authenticated /sync events.
"""

import json
import urllib.request

from tools.registry import registry


def _ping(url: str, timeout: float = 4) -> dict:
    """Unauthenticated liveness probe of a peer's /ping."""
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/ping", timeout=timeout) as r:
            info = json.loads(r.read().decode() or "{}")
            return {"reachable": r.status == 200, "node": info.get("node", "")}
    except Exception as e:
        return {"reachable": False, "error": f"{type(e).__name__}: {e}"}


def peer_network(action: str, peer: str = "", message: str = "") -> str:
    from core.network import (network_manager, outbound_identity,
                              resolve_peer, send_to_peer, broadcast)
    action = (action or "").strip().lower()
    node, secret, peers = outbound_identity()

    if action == "status":
        out = {
            "node_name": node,
            "secret_configured": bool(secret),
            "inbound_running": network_manager.running,
            "public_url": network_manager.public_url or "(no tunnel)",
            "peers": [{"name": p.get("name", ""), "url": p["url"], **_ping(p["url"])}
                      for p in peers],
        }
        if not peers:
            out["note"] = "No peers configured (Settings → Network → Peers)."
        return json.dumps(out, ensure_ascii=False)

    if action == "send":
        message = (message or "").strip()
        if not message:
            return json.dumps({"error": "message is required for action=send"})
        if not secret:
            return json.dumps({"error": "No shared secret configured — "
                                        "set one in Settings → Network first."})
        if peer:
            p = resolve_peer(peer)
            url = p["url"] if p else (peer if peer.startswith(("http://", "https://")) else "")
            if not url:
                return json.dumps({
                    "error": f"Unknown peer '{peer}'. Use a configured peer name, "
                             f"or a full https:// URL.",
                    "configured_peers": [q.get("name") or q["url"] for q in peers]})
            ok, detail = send_to_peer(url, {"type": "chat", "message": message})
            return json.dumps({"sent": ok, "peer": (p or {}).get("name") or url,
                               "detail": detail}, ensure_ascii=False)
        results = broadcast({"type": "chat", "message": message})
        if not results:
            return json.dumps({"error": "No peers configured to broadcast to."})
        return json.dumps({"sent": all(r["ok"] for r in results),
                           "results": results}, ensure_ascii=False)

    return json.dumps({"error": f"Unknown action '{action}'. Use 'status' or 'send'."})


registry.register(
    name="peer_network",
    description=(
        "Communicate with other Familiar instances over the peer network "
        "(cloudflared mesh, Settings → Network). action='status' lists this "
        "node's public URL and each configured peer's reachability. "
        "action='send' delivers a message to one peer (peer=name or URL) or "
        "broadcasts to all peers when peer is omitted. Use it when the user "
        "asks to message, notify, or check on their other machines."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["status", "send"],
                "description": "'status' = network/peer overview; 'send' = deliver a message.",
            },
            "peer": {
                "type": "string",
                "description": "Peer name (from Settings → Network) or full https:// URL. "
                               "Omit to broadcast to every configured peer.",
            },
            "message": {
                "type": "string",
                "description": "The message text to deliver (required for action='send').",
            },
        },
        "required": ["action"],
    },
    execute=peer_network,
)
