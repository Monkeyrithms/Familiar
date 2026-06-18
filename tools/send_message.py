"""
send_message — cross-channel messaging (parity with Hermes' send_message).

Sends a message to a connected platform: Telegram, Discord, Slack, Signal,
SMS (Twilio), Matrix, Mattermost, Home Assistant, DingTalk, Feishu, WhatsApp,
or email. Self-contained: pure-stdlib HTTP (urllib), no gateway / no aiohttp /
no platform SDKs, so it hot-reloads cleanly and runs headless on a VPS.

Credentials live in config.json under "messaging". Each platform has a
"default" target (Hermes' "home channel") so `send_message(platform="telegram",
message="hi")` just works with no target. Run action="list" to see what's
configured.

Target forms:
    "telegram"                      -> default chat from config
    "telegram:123456789"            -> explicit chat id
    "telegram:-100123:17"           -> supergroup chat : topic thread
    "discord:#channel-id-or-webhook"
    "slack:#engineering" / "slack:C0123"
    "signal:+15551234567" / "signal:group:GROUP_ID"
    "sms:+15551234567"
"""

import json
import re
import smtplib
import ssl
import urllib.request
import urllib.parse
from email.mime.text import MIMEText
from pathlib import Path

from tools.registry import registry

_CONFIG = Path(__file__).parent.parent / "config.json"

# Per-platform hard message-length limits. Longer text is chunked.
_MAX_LEN = {
    "telegram": 4096,
    "discord": 2000,
    "slack": 3900,
    "mattermost": 16000,
    "matrix": 32000,
    "sms": 1500,
}


# ── config ───────────────────────────────────────────────────────────

def _cfg() -> dict:
    try:
        return json.loads(_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _messaging() -> dict:
    return _cfg().get("messaging", {}) or {}


def _platform_cfg(platform: str) -> dict:
    return _messaging().get(platform, {}) or {}


# ── helpers ──────────────────────────────────────────────────────────

def _http(url: str, *, data: bytes = None, headers: dict = None,
          method: str = None, timeout: float = 30) -> tuple[int, str]:
    """One-shot HTTP. Returns (status, body_text). Never raises for HTTP errors."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=ssl.create_default_context()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def _post_json(url: str, payload: dict, headers: dict = None) -> tuple[int, str]:
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    return _http(url, data=json.dumps(payload).encode(), headers=h, method="POST")


def _chunk(message: str, limit: int) -> list[str]:
    """Split a long message under `limit`, preferring newline boundaries."""
    if len(message) <= limit:
        return [message]
    out, buf = [], ""
    for line in message.splitlines(keepends=True):
        while len(line) > limit:           # a single monster line
            if buf:
                out.append(buf); buf = ""
            out.append(line[:limit]); line = line[limit:]
        if len(buf) + len(line) > limit:
            out.append(buf); buf = line
        else:
            buf += line
    if buf:
        out.append(buf)
    n = len(out)
    return [f"{c}\n({i + 1}/{n})" if n > 1 else c for i, c in enumerate(out)]


def _strip_markdown(text: str) -> str:
    """SMS renders markdown as literal junk — strip it."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*(.+?)\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.+?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_(.+?)_", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"```[a-z]*\n?", "", text)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# ── per-platform senders. each returns a result dict ──────────────────

def _send_telegram(cfg, target, message):
    token = cfg.get("bot_token", "")
    if not token:
        return {"error": "telegram not configured: set messaging.telegram.bot_token in config.json"}
    chat_id, _, thread = (target or "").partition(":")
    chat_id = chat_id or cfg.get("default_chat_id", "")
    thread = thread or cfg.get("default_thread_id", "")
    if not chat_id:
        return {"error": "no telegram chat_id (give 'telegram:CHAT_ID' or set messaging.telegram.default_chat_id)"}

    html = bool(re.search(r"<[a-zA-Z/][^>]*>", message))
    last_id = None
    for chunk in _chunk(message, _MAX_LEN["telegram"]):
        payload = {"chat_id": chat_id, "text": chunk, "disable_web_page_preview": True}
        if html:
            payload["parse_mode"] = "HTML"
        if thread:
            payload["message_thread_id"] = int(thread)
        status, body = _post_json(
            f"https://api.telegram.org/bot{token}/sendMessage", payload)
        data = _try_json(body)
        if status != 200 or not data.get("ok"):
            # retry once as plain text (parse_mode rejected on bad markup)
            payload.pop("parse_mode", None)
            status, body = _post_json(
                f"https://api.telegram.org/bot{token}/sendMessage", payload)
            data = _try_json(body)
            if status != 200 or not data.get("ok"):
                return {"error": f"telegram API ({status}): {data.get('description', body)}"}
        last_id = str((data.get("result") or {}).get("message_id", ""))
    return {"success": True, "platform": "telegram", "chat_id": chat_id, "message_id": last_id}


def _send_discord(cfg, target, message):
    token = cfg.get("bot_token", "")
    webhook = cfg.get("webhook_url", "")
    channel = target or cfg.get("default_channel_id", "")
    # An explicit https target, or a config webhook with no channel -> webhook path
    if (channel and channel.startswith("http")) or (webhook and not channel):
        url = channel if channel.startswith("http") else webhook
        last = None
        for chunk in _chunk(message, _MAX_LEN["discord"]):
            status, body = _post_json(url, {"content": chunk})
            if status not in (200, 204):
                return {"error": f"discord webhook ({status}): {body}"}
            last = status
        return {"success": True, "platform": "discord", "via": "webhook"}
    if not token:
        return {"error": "discord not configured: set messaging.discord.bot_token (or webhook_url) in config.json"}
    if not channel:
        return {"error": "no discord channel (give 'discord:CHANNEL_ID' or set messaging.discord.default_channel_id)"}
    headers = {"Authorization": f"Bot {token}"}
    last_id = None
    for chunk in _chunk(message, _MAX_LEN["discord"]):
        status, body = _post_json(
            f"https://discord.com/api/v10/channels/{channel}/messages",
            {"content": chunk}, headers)
        if status not in (200, 201):
            return {"error": f"discord API ({status}): {body}"}
        last_id = _try_json(body).get("id")
    return {"success": True, "platform": "discord", "chat_id": channel, "message_id": last_id}


def _send_slack(cfg, target, message):
    token = cfg.get("bot_token", "")
    channel = target or cfg.get("default_channel", "")
    if not token:
        return {"error": "slack not configured: set messaging.slack.bot_token in config.json"}
    if not channel:
        return {"error": "no slack channel (give 'slack:#chan' or set messaging.slack.default_channel)"}
    headers = {"Authorization": f"Bearer {token}"}
    last_ts = None
    for chunk in _chunk(message, _MAX_LEN["slack"]):
        status, body = _post_json("https://slack.com/api/chat.postMessage",
                                  {"channel": channel, "text": chunk}, headers)
        data = _try_json(body)
        if not data.get("ok"):
            return {"error": f"slack API: {data.get('error', body)}"}
        last_ts = data.get("ts")
    return {"success": True, "platform": "slack", "chat_id": channel, "message_id": last_ts}


def _send_signal(cfg, target, message):
    http_url = (cfg.get("http_url") or "http://127.0.0.1:8080").rstrip("/")
    account = cfg.get("account", "")
    recipient = target or cfg.get("default_recipient", "")
    if not account:
        return {"error": "signal not configured: set messaging.signal.account (and run signal-cli daemon)"}
    if not recipient:
        return {"error": "no signal recipient (give 'signal:+1555...' or 'signal:group:ID')"}
    params = {"account": account, "message": message}
    if recipient.startswith("group:"):
        params["groupId"] = recipient[6:]
    else:
        params["recipient"] = [recipient]
    status, body = _post_json(f"{http_url}/api/v1/rpc",
                              {"jsonrpc": "2.0", "method": "send",
                               "params": params, "id": "send"})
    data = _try_json(body)
    if status == 0 or "error" in data:
        return {"error": f"signal RPC: {data.get('error', body)}"}
    return {"success": True, "platform": "signal", "chat_id": recipient}


def _send_sms(cfg, target, message):
    sid = cfg.get("twilio_account_sid", "")
    token = cfg.get("twilio_auth_token", "")
    from_number = cfg.get("from_number", "")
    to = target or cfg.get("default_to", "")
    if not all([sid, token, from_number]):
        return {"error": "sms not configured: set messaging.sms.twilio_account_sid / twilio_auth_token / from_number"}
    if not to:
        return {"error": "no sms recipient (give 'sms:+1555...' or set messaging.sms.default_to)"}
    import base64
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {auth}",
               "Content-Type": "application/x-www-form-urlencoded"}
    last_sid = None
    for chunk in _chunk(_strip_markdown(message), _MAX_LEN["sms"]):
        data = urllib.parse.urlencode({"From": from_number, "To": to, "Body": chunk}).encode()
        status, body = _http(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data=data, headers=headers, method="POST")
        jb = _try_json(body)
        if status >= 400:
            return {"error": f"twilio ({status}): {jb.get('message', body)}"}
        last_sid = jb.get("sid")
    return {"success": True, "platform": "sms", "chat_id": to, "message_id": last_sid}


def _send_matrix(cfg, target, message):
    homeserver = (cfg.get("homeserver") or "").rstrip("/")
    token = cfg.get("access_token", "")
    room = target or cfg.get("default_room", "")
    if not homeserver or not token:
        return {"error": "matrix not configured: set messaging.matrix.homeserver / access_token"}
    if not room:
        return {"error": "no matrix room (give 'matrix:!room:server' or set messaging.matrix.default_room)"}
    import time as _t
    txn = f"agent_{int(_t.time() * 1000)}"
    url = f"{homeserver}/_matrix/client/v3/rooms/{urllib.parse.quote(room)}/send/m.room.message/{txn}"
    status, body = _http(url, data=json.dumps({"msgtype": "m.text", "body": message}).encode(),
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json"}, method="PUT")
    if status not in (200, 201):
        return {"error": f"matrix ({status}): {body}"}
    return {"success": True, "platform": "matrix", "chat_id": room,
            "message_id": _try_json(body).get("event_id")}


def _send_mattermost(cfg, target, message):
    base = (cfg.get("url") or "").rstrip("/")
    token = cfg.get("token", "")
    channel = target or cfg.get("default_channel_id", "")
    if not base or not token:
        return {"error": "mattermost not configured: set messaging.mattermost.url / token"}
    if not channel:
        return {"error": "no mattermost channel_id (give 'mattermost:CHANNEL_ID' or set default_channel_id)"}
    status, body = _post_json(f"{base}/api/v4/posts",
                              {"channel_id": channel, "message": message},
                              {"Authorization": f"Bearer {token}"})
    if status not in (200, 201):
        return {"error": f"mattermost ({status}): {body}"}
    return {"success": True, "platform": "mattermost", "chat_id": channel,
            "message_id": _try_json(body).get("id")}


def _send_homeassistant(cfg, target, message):
    base = (cfg.get("url") or "").rstrip("/")
    token = cfg.get("token", "")
    tgt = target or cfg.get("default_target", "")
    if not base or not token:
        return {"error": "homeassistant not configured: set messaging.homeassistant.url / token"}
    payload = {"message": message}
    if tgt:
        payload["target"] = tgt
    status, body = _post_json(f"{base}/api/services/notify/notify", payload,
                              {"Authorization": f"Bearer {token}"})
    if status not in (200, 201):
        return {"error": f"homeassistant ({status}): {body}"}
    return {"success": True, "platform": "homeassistant", "chat_id": tgt}


def _send_dingtalk(cfg, target, message):
    webhook = target if (target or "").startswith("http") else cfg.get("webhook_url", "")
    if not webhook:
        return {"error": "dingtalk not configured: set messaging.dingtalk.webhook_url"}
    status, body = _post_json(webhook, {"msgtype": "text", "text": {"content": message}})
    data = _try_json(body)
    if status != 200 or data.get("errcode", 0) != 0:
        return {"error": f"dingtalk: {data.get('errmsg', body)}"}
    return {"success": True, "platform": "dingtalk"}


def _send_feishu(cfg, target, message):
    webhook = target if (target or "").startswith("http") else cfg.get("webhook_url", "")
    if not webhook:
        return {"error": "feishu not configured: set messaging.feishu.webhook_url (custom-bot webhook)"}
    status, body = _post_json(webhook, {"msg_type": "text", "content": {"text": message}})
    data = _try_json(body)
    if status != 200 or data.get("code", data.get("StatusCode", 0)) not in (0, None):
        return {"error": f"feishu: {body}"}
    return {"success": True, "platform": "feishu"}


def _send_whatsapp(cfg, target, message):
    port = cfg.get("bridge_port", 3000)
    chat_id = target or cfg.get("default_chat_id", "")
    if not chat_id:
        return {"error": "no whatsapp chat (give 'whatsapp:CHAT_ID' or set messaging.whatsapp.default_chat_id)"}
    status, body = _post_json(f"http://localhost:{port}/send",
                              {"chatId": chat_id, "message": message})
    if status != 200:
        return {"error": f"whatsapp bridge ({status}): {body} — is the local bridge running on :{port}?"}
    return {"success": True, "platform": "whatsapp", "chat_id": chat_id,
            "message_id": _try_json(body).get("messageId")}


def _send_email(cfg, target, message):
    """Reuse the app's SMTP block (config.json -> smtp)."""
    smtp = _cfg().get("smtp", {})
    to = target or cfg.get("default_to", "")
    if not smtp.get("host") or not smtp.get("user"):
        return {"error": "email not configured: add smtp {host,port,user,password,from_email} to config.json"}
    if not to:
        return {"error": "no email recipient (give 'email:addr@x.com' or set messaging.email.default_to)"}
    try:
        msg = MIMEText(message, "plain", "utf-8")
        msg["From"] = smtp.get("from_email", smtp["user"])
        msg["To"] = to
        msg["Subject"] = cfg.get("subject", "Familiar Agent")
        with smtplib.SMTP(smtp["host"], smtp.get("port", 587)) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(smtp["user"], smtp["password"])
            s.send_message(msg)
        return {"success": True, "platform": "email", "chat_id": to}
    except Exception as e:
        return {"error": f"email send failed: {e}"}


def _try_json(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _save_telegram(bot_token: str = None, chat_id: str = None) -> bool:
    """Persist telegram creds back into config.json (preserving everything else)."""
    try:
        cfg = json.loads(_CONFIG.read_text(encoding="utf-8"))
        msg = cfg.setdefault("messaging", {})
        tg = msg.setdefault("telegram", {})
        if bot_token is not None:
            tg["bot_token"] = bot_token
        if chat_id is not None:
            tg["default_chat_id"] = chat_id
        _CONFIG.write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n",
                           encoding="utf-8")
        return True
    except Exception:
        return False


def _setup_telegram(token: str = "") -> dict:
    """Validate a bot token and auto-discover the chat_id from recent messages.

    Flow: BotFather gives you a token -> the recipient sends ANY message to the
    bot -> this reads getUpdates, grabs the chat, and writes both back to
    config.json. After this, send_message(platform='telegram', ...) just works.
    """
    cfg = _platform_cfg("telegram")
    token = (token or cfg.get("bot_token", "")).strip()
    if not token:
        return {"error": "No bot token. Get one from @BotFather, then call "
                         "send_message(action='setup_telegram', target='<BOT_TOKEN>') "
                         "or put it in config.json messaging.telegram.bot_token first."}

    # 1. validate the token
    status, body = _http(f"https://api.telegram.org/bot{token}/getMe", timeout=15)
    me = _try_json(body)
    if status != 200 or not me.get("ok"):
        return {"error": f"Invalid bot token (getMe {status}): {me.get('description', body)}"}
    bot_name = (me.get("result") or {}).get("username", "?")

    # 2. discover chat_id from recent updates
    status, body = _http(f"https://api.telegram.org/bot{token}/getUpdates", timeout=15)
    data = _try_json(body)
    if status != 200 or not data.get("ok"):
        return {"error": f"getUpdates failed ({status}): {data.get('description', body)}"}

    chats: dict = {}
    for u in data.get("result", []):
        m = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        chat = m.get("chat") or {}
        if chat.get("id") is not None:
            chats[str(chat["id"])] = (chat.get("username") or chat.get("title")
                                      or chat.get("first_name") or "")

    saved = _save_telegram(bot_token=token)
    if not chats:
        return {"success": False, "bot": f"@{bot_name}", "token_saved": saved,
                "next_step": f"Token is valid and saved. Now send ANY message to @{bot_name} "
                             f"from the recipient's Telegram, then call "
                             f"send_message(action='setup_telegram') again to capture the chat_id."}

    chat_id = list(chats.keys())[-1]   # most recent
    _save_telegram(chat_id=chat_id)
    return {"success": True, "bot": f"@{bot_name}", "chat_id": chat_id,
            "recipient": chats[chat_id], "all_chats": chats,
            "note": f"Saved bot_token + default_chat_id ({chat_id}) to config.json. "
                    f"send_message(platform='telegram', message='hi') now works."}


_SENDERS = {
    "telegram": _send_telegram, "discord": _send_discord, "slack": _send_slack,
    "signal": _send_signal, "sms": _send_sms, "matrix": _send_matrix,
    "mattermost": _send_mattermost, "homeassistant": _send_homeassistant,
    "dingtalk": _send_dingtalk, "feishu": _send_feishu,
    "whatsapp": _send_whatsapp, "email": _send_email,
}

# Which config field holds each platform's default/home target, for action=list.
_DEFAULT_FIELD = {
    "telegram": "default_chat_id", "discord": "default_channel_id",
    "slack": "default_channel", "signal": "default_recipient", "sms": "default_to",
    "matrix": "default_room", "mattermost": "default_channel_id",
    "homeassistant": "default_target", "whatsapp": "default_chat_id",
    "email": "default_to",
}
# Fields that, if present, mean "this platform has credentials".
_CRED_FIELDS = {
    "telegram": ("bot_token",), "discord": ("bot_token", "webhook_url"),
    "slack": ("bot_token",), "signal": ("account",),
    "sms": ("twilio_account_sid",), "matrix": ("homeserver", "access_token"),
    "mattermost": ("url", "token"), "homeassistant": ("url", "token"),
    "dingtalk": ("webhook_url",), "feishu": ("webhook_url",),
    "whatsapp": ("default_chat_id",), "email": (),
}


def _handle_list() -> str:
    msg = _messaging()
    smtp_ok = bool(_cfg().get("smtp", {}).get("host"))
    out = []
    for plat in _SENDERS:
        pc = msg.get(plat, {}) or {}
        if plat == "email":
            configured = smtp_ok
        else:
            configured = any(pc.get(f) for f in _CRED_FIELDS.get(plat, ()))
        entry = {"platform": plat, "configured": configured}
        dflt = pc.get(_DEFAULT_FIELD.get(plat, ""))
        if dflt:
            entry["default_target"] = dflt
        out.append(entry)
    return json.dumps({
        "platforms": out,
        "default_platform": msg.get("default_platform", ""),
        "hint": "Credentials live in config.json -> messaging. "
                "Send with send_message(platform='telegram', message='hi') "
                "or target='telegram:CHAT_ID'.",
    }, indent=2)


def send_message(action: str = "send", platform: str = "", target: str = "",
                 message: str = "") -> str:
    """Send a message across a connected platform, or list configured targets."""
    act = (action or "send").strip().lower()
    if act == "list":
        return _handle_list()
    if act == "setup_telegram":
        return json.dumps(_setup_telegram(token=target))

    # Accept Hermes-style combined target "platform:rest" too.
    if not platform and target and ":" in target and target.split(":", 1)[0] in _SENDERS:
        platform, target = target.split(":", 1)
    platform = (platform or _messaging().get("default_platform", "")).strip().lower()

    if not platform:
        return json.dumps({"error": "no platform. Pass platform='telegram' (or set "
                                    "messaging.default_platform), or action='list'."})
    if platform not in _SENDERS:
        return json.dumps({"error": f"unknown platform '{platform}'. "
                                    f"Available: {', '.join(_SENDERS)}"})
    if not (message or "").strip():
        return json.dumps({"error": "message is required"})

    result = _SENDERS[platform](_platform_cfg(platform), target.strip(), message)
    if result.get("success"):
        result["note"] = ("Delivered. No further tool calls — confirm to the user in text.")
    return json.dumps(result)


registry.register(
    name="send_message",
    description=(
        "Send a message to the user on a real messaging platform — Telegram, "
        "Discord, Slack, Signal, SMS, Matrix, Mattermost, Home Assistant, "
        "DingTalk, Feishu, WhatsApp, or email. Use this to actually text/DM the "
        "user (e.g. alerts, finished work, 'saying hi'). Credentials are in "
        "config.json -> messaging; each platform has a default target so you can "
        "omit 'target'. Call action='list' to see what's configured. "
        "Long messages are auto-chunked to each platform's limit."
    ),
    parameters={
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["send", "list", "setup_telegram"],
                       "description": "'send' (default), 'list' configured platforms, or "
                                      "'setup_telegram' to validate a bot token (pass it as "
                                      "'target') and auto-capture the recipient's chat_id."},
            "platform": {"type": "string",
                         "description": "telegram | discord | slack | signal | sms | "
                                        "matrix | mattermost | homeassistant | dingtalk | "
                                        "feishu | whatsapp | email. Omit to use the default."},
            "target": {"type": "string",
                       "description": "Optional. Recipient/channel id. Omit to use the "
                                      "platform's configured default. e.g. a Telegram "
                                      "chat id, '+15551234567' for sms/signal, '#chan' "
                                      "for slack, 'chat:thread' for a Telegram topic."},
            "message": {"type": "string", "description": "The message text."},
        },
        "required": [],
    },
    execute=send_message,
)
