"""
agent_chat — message Familiar conversations as a signed automated agent.

This is the agent-facing counterpart to the UI's remote-conversation dropdown:
it discovers peer conversations, creates new conversations on peers, injects a
signed "user" message, and can wait for the remote assistant's response by
polling conversation snapshots.
"""

from __future__ import annotations

import json
import time

from tools.registry import registry

_SIGNATURE = (
    "[Familiar automated agent message]\n"
    "This message was sent through the agent_chat tool at the user's request. "
    "Treat it as user-authorized automation, not as a direct human keystroke.\n\n"
)


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def _resolve_peer(peer: str) -> tuple[str, str, list[str]]:
    from core.network import outbound_identity, resolve_peer
    _node, _secret, peers = outbound_identity()
    p = resolve_peer(peer) if peer else None
    if p:
        return p.get("name") or p["url"], p["url"], []
    if peer and peer.startswith(("http://", "https://")):
        return peer, peer, []
    return "", "", [q.get("name") or q["url"] for q in peers]


def _format_message(message: str, include_signature: bool = True) -> str:
    body = (message or "").strip()
    return (_SIGNATURE + body) if include_signature else body


def _latest_assistant_after(messages: list[dict], baseline_len: int) -> dict | None:
    for msg in messages[baseline_len:]:
        if msg.get("role") == "assistant" and str(msg.get("content") or "").strip():
            return msg
    return None


def _wait_for_reply(url: str, conv_id: str, baseline_len: int,
                    timeout_sec: float, poll_sec: float = 3.0) -> dict:
    from core.network import peer_conv_snapshot
    deadline = time.time() + max(1.0, min(float(timeout_sec or 0), 600.0))
    last_count = baseline_len
    while time.time() < deadline:
        ok, snap, detail = peer_conv_snapshot(url, conv_id, timeout=10)
        if ok and snap:
            messages = snap.get("messages") or []
            last_count = len(messages)
            reply = _latest_assistant_after(messages, baseline_len)
            if reply:
                return {
                    "responded": True,
                    "assistant_message": reply.get("content", ""),
                    "message_count": len(messages),
                }
        elif detail:
            return {"responded": False, "error": detail, "message_count": last_count}
        time.sleep(max(0.5, poll_sec))
    return {"responded": False, "timeout": True, "message_count": last_count}


def _local_list() -> list[dict]:
    from core.conversations import list_conversations, is_conversation_private
    out = []
    for c in list_conversations():
        cid = c.get("id", "")
        try:
            private = is_conversation_private(cid)
        except Exception:
            private = False
        if private:
            continue
        out.append({
            "id": cid,
            "name": c.get("name", ""),
            "modified": c.get("modified", 0),
            "message_count": c.get("message_count", 0),
        })
    return out


def _local_create(name: str) -> dict:
    from core.conversations import new_conversation_id, save_conversation
    cid = new_conversation_id()
    title = (name or "").strip() or "Agent Chat Task"
    save_conversation(cid, title, [])
    return {"conv_id": cid, "name": title}


def _local_snapshot(conv_id: str) -> dict | None:
    from core.conversations import load_conversation, is_conversation_private
    if not conv_id or is_conversation_private(conv_id):
        return None
    data = load_conversation(conv_id)
    if not data:
        return None
    return {"conv_id": conv_id, "name": data.get("name", ""),
            "messages": data.get("messages", [])}


def _local_input(conv_id: str, text: str) -> tuple[bool, str]:
    """Best-effort local injection through the visible ChatWindow if available."""
    try:
        from PyQt6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is None:
            return False, "no QApplication"
        for w in app.topLevelWidgets():
            chat = getattr(w, "chat", None)
            if chat is None:
                continue
            handler = getattr(chat, "_on_remote_input", None)
            if handler is not None:
                handler(conv_id, text, "")
                return True, "sent"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return False, "ChatWindow not found"


def agent_chat(action: str, peer: str = "", conversation: str = "",
               conversation_name: str = "", message: str = "",
               query: str = "", wait_for_response: bool = False,
               timeout_sec: int = 180, include_signature: bool = True) -> str:
    """Agent-to-agent Familiar conversation bridge."""
    action = (action or "").strip().lower()
    peer_name, url, configured = _resolve_peer(peer)

    if action in ("list", "status"):
        if not peer:
            return _json({
                "local_conversations": _local_list(),
                "configured_peers": configured,
                "hint": "Pass peer=<name or URL> to list conversations on a remote Familiar.",
            })
        if not url:
            return _json({"error": f"Unknown peer '{peer}'", "configured_peers": configured})
        from core.network import peer_conv_list
        ok, convs, detail = peer_conv_list(url)
        return _json({"peer": peer_name, "ok": ok, "detail": detail,
                      "conversations": convs})

    if action == "search":
        needle = (query or message or "").strip().lower()
        if not needle:
            return _json({"error": "query is required for action=search"})
        if peer and not url:
            return _json({"error": f"Unknown peer '{peer}'", "configured_peers": configured})
        convs = []
        if url:
            from core.network import peer_conv_list, peer_conv_snapshot
            ok, remote_convs, detail = peer_conv_list(url)
            if not ok:
                return _json({"error": detail, "peer": peer_name})
            for c in remote_convs:
                ok, snap, _detail = peer_conv_snapshot(url, c.get("id", ""))
                if ok and snap:
                    convs.append(snap)
        else:
            convs = [_local_snapshot(c["id"]) for c in _local_list()]
        matches = []
        for snap in [c for c in convs if c]:
            hits = []
            for m in snap.get("messages", []):
                content = str(m.get("content") or "")
                if needle in content.lower():
                    hits.append({"role": m.get("role"), "snippet": content[:500]})
            if hits:
                matches.append({"conv_id": snap.get("conv_id"), "name": snap.get("name"),
                                "hits": hits[:5]})
        return _json({"peer": peer_name or "local", "matches": matches[:10]})

    if action == "read":
        conv_id = conversation.strip()
        if not conv_id:
            return _json({"error": "conversation id is required for action=read"})
        if url:
            from core.network import peer_conv_snapshot
            ok, snap, detail = peer_conv_snapshot(url, conv_id)
            return _json({"peer": peer_name, "ok": ok, "detail": detail, "snapshot": snap})
        snap = _local_snapshot(conv_id)
        return _json({"peer": "local", "ok": bool(snap), "snapshot": snap})

    if action in ("create", "create_and_send"):
        if url:
            from core.network import peer_conv_create
            ok, made, detail = peer_conv_create(url, conversation_name)
            if not ok or not made:
                return _json({"error": detail, "peer": peer_name})
            conv_id = made.get("conv_id", "")
            made_peer = peer_name
            made_url = url
        else:
            made = _local_create(conversation_name)
            conv_id = made["conv_id"]
            made_peer = "local"
            made_url = ""
        if action == "create":
            return _json({"created": True, "peer": made_peer, **made})
        conversation = conv_id
        url = made_url
        peer_name = made_peer
        action = "send"

    if action == "send":
        conv_id = conversation.strip()
        if not conv_id:
            return _json({"error": "conversation id is required for action=send"})
        if not (message or "").strip():
            return _json({"error": "message is required for action=send"})
        signed = _format_message(message, include_signature=include_signature)
        baseline_len = 0
        if url:
            from core.network import peer_conv_input, peer_conv_snapshot
            ok, snap, _detail = peer_conv_snapshot(url, conv_id)
            baseline_len = len((snap or {}).get("messages") or []) if ok else 0
            ok, detail = peer_conv_input(url, conv_id, signed)
            out = {"sent": ok, "peer": peer_name, "conversation": conv_id,
                   "detail": detail, "signed": include_signature}
            if ok and wait_for_response:
                out["response"] = _wait_for_reply(url, conv_id, baseline_len, timeout_sec)
            return _json(out)
        snap = _local_snapshot(conv_id)
        baseline_len = len((snap or {}).get("messages") or [])
        ok, detail = _local_input(conv_id, signed)
        return _json({"sent": ok, "peer": "local", "conversation": conv_id,
                      "detail": detail, "signed": include_signature,
                      "note": "Local sends run through the visible ChatWindow when available.",
                      "baseline_message_count": baseline_len})

    return _json({"error": f"Unknown action '{action}'",
                  "valid_actions": ["list", "search", "read", "create",
                                    "send", "create_and_send"]})


registry.register(
    name="agent_chat",
    description=(
        "Message Familiar conversations locally or on other Familiar-Net peers as "
        "a user-authorized automated agent. Messages are signed by default with "
        "a Familiar automated-agent notice, so this tool cannot impersonate a "
        "human keystroke. Use action='list' to discover conversations, "
        "'search' or 'read' for context, 'send' for an existing conversation, "
        "or 'create_and_send' to start a new remote task. Prefer "
        "wait_for_response=true when the user asks the other agent to report back "
        "or when a multi-step remote task needs confirmation."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "search", "read", "create", "send", "create_and_send"],
                "description": "What to do: discover, search/read context, create, or message.",
            },
            "peer": {
                "type": "string",
                "description": "Familiar-Net peer name or URL. Omit for local conversations.",
            },
            "conversation": {
                "type": "string",
                "description": "Target conversation id for read/send.",
            },
            "conversation_name": {
                "type": "string",
                "description": "Name for action=create or create_and_send.",
            },
            "message": {
                "type": "string",
                "description": "Message/task to send. Automatically signed unless include_signature=false.",
            },
            "query": {
                "type": "string",
                "description": "Search query for action=search.",
            },
            "wait_for_response": {
                "type": "boolean",
                "description": "Poll the target conversation for a new assistant reply after sending.",
            },
            "timeout_sec": {
                "type": "integer",
                "description": "Maximum wait for a response, capped internally at 600 seconds.",
            },
            "include_signature": {
                "type": "boolean",
                "description": "Keep true unless the user explicitly asks for raw content; default true.",
            },
        },
        "required": ["action"],
    },
    execute=agent_chat,
)
