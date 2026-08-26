"""
ChatGPT / Codex subscription auth for OpenAI provider.

Uses the private Codex backend (``chatgpt.com/backend-api/codex/responses``),
not ``api.openai.com``. Credentials come from ``~/.codex/auth.json`` (Codex CLI
login) with automatic token refresh — same approach as OpenClaw / OpenCode.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CODEX_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
OPENAI_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OPENAI_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
DEFAULT_INSTRUCTIONS = "Follow the user request."
_EMPTY_INPUT_TEXT = "Continue."
_REQUEST_TIMEOUT_S = 300


def _request_read_timeout_s() -> float:
    try:
        return max(10.0, min(120.0, float(os.environ.get("OPENAI_CODEX_READ_TIMEOUT_S", "120"))))
    except (TypeError, ValueError):
        return 120.0


class _AttrDict:
    """Lightweight OpenAI-SDK-shaped response object."""

    def __init__(self, d: dict):
        self.__dict__.update(d)

    def model_dump(self, exclude_none: bool = False, **kwargs) -> dict:
        out = {}
        for k, v in self.__dict__.items():
            if exclude_none and v is None:
                continue
            if isinstance(v, _AttrDict):
                out[k] = v.model_dump(exclude_none=exclude_none)
            elif isinstance(v, list):
                out[k] = [
                    x.model_dump(exclude_none=exclude_none) if isinstance(x, _AttrDict) else x
                    for x in v
                ]
            else:
                out[k] = v
        return out


@dataclass
class CodexCredentials:
    access_token: str
    refresh_token: str
    account_id: str | None
    expires_at_ms: int | None
    auth_path: Path


def _codex_auth_path() -> Path:
    import os
    home = os.environ.get("CODEX_HOME", "").strip()
    if home:
        return Path(home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def _decode_jwt_payload(token: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        data = json.loads(raw.decode("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _resolve_account_id(access_token: str, auth_data: dict) -> str | None:
    tokens = auth_data.get("tokens") if isinstance(auth_data.get("tokens"), dict) else {}
    for src in (tokens, auth_data):
        if not isinstance(src, dict):
            continue
        for key in ("account_id", "accountId"):
            val = src.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    payload = _decode_jwt_payload(access_token)
    if payload:
        auth = payload.get("https://api.openai.com/auth")
        if isinstance(auth, dict):
            acct = auth.get("chatgpt_account_id")
            if isinstance(acct, str) and acct.strip():
                return acct.strip()
    return None


def _token_expires_at_ms(access_token: str, auth_data: dict) -> int | None:
    tokens = auth_data.get("tokens") if isinstance(auth_data.get("tokens"), dict) else {}
    for src in (auth_data, tokens):
        if not isinstance(src, dict):
            continue
        for key in ("expires_at_ms", "expiresAt", "expires_at"):
            val = src.get(key)
            if isinstance(val, (int, float)) and val > 0:
                ms = int(val)
                return ms if ms > 1_000_000_000_000 else ms * 1000
    payload = _decode_jwt_payload(access_token)
    if payload and isinstance(payload.get("exp"), (int, float)):
        return int(payload["exp"]) * 1000
    return None


def _pick_access_token(obj: dict) -> str | None:
    for key in ("access_token", "ACCESS_TOKEN", "token"):
        val = obj.get(key)
        if isinstance(val, str) and len(val) > 12:
            return val
    inner = obj.get("openai")
    if isinstance(inner, dict):
        for key in ("access_token", "api_key", "token"):
            val = inner.get(key)
            if isinstance(val, str) and len(val) > 12:
                return val
    return None


def _pick_refresh_token(obj: dict) -> str | None:
    for key in ("refresh_token", "refreshToken", "REFRESH_TOKEN"):
        val = obj.get(key)
        if isinstance(val, str) and len(val) > 12:
            return val
    return None


def _load_auth_json(path: Path | None = None) -> tuple[dict, Path] | None:
    path = path or _codex_auth_path()
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data, path


def _save_auth_json(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        pass


def _refresh_codex_token(refresh_token: str) -> dict | None:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OPENAI_OAUTH_CLIENT_ID,
    }).encode()
    req = urllib.request.Request(
        OPENAI_OAUTH_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "agent-app/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except Exception:
        return None
    access = result.get("access_token", "")
    refresh = result.get("refresh_token") or refresh_token
    if not access:
        return None
    expires_in = result.get("expires_in", 3600)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at_ms": int(time.time() * 1000) + int(expires_in) * 1000,
    }


def _apply_refreshed_tokens(auth_data: dict, refreshed: dict) -> str:
    access = refreshed["access_token"]
    refresh = refreshed["refresh_token"]
    expires_at_ms = refreshed.get("expires_at_ms")
    tokens = auth_data.setdefault("tokens", {})
    if not isinstance(tokens, dict):
        tokens = {}
        auth_data["tokens"] = tokens
    tokens["access_token"] = access
    tokens["refresh_token"] = refresh
    if expires_at_ms:
        tokens["expires_at_ms"] = expires_at_ms
    auth_data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return access


def get_codex_credentials() -> CodexCredentials | None:
    """Load Codex OAuth credentials from ``~/.codex/auth.json``, refreshing if needed."""
    loaded = _load_auth_json()
    if not loaded:
        return None
    auth_data, path = loaded

    access = _pick_access_token(auth_data)
    if not access and isinstance(auth_data.get("tokens"), dict):
        access = _pick_access_token(auth_data["tokens"])
    if not access:
        return None

    refresh = _pick_refresh_token(auth_data)
    if not refresh and isinstance(auth_data.get("tokens"), dict):
        refresh = _pick_refresh_token(auth_data["tokens"])

    expires_at_ms = _token_expires_at_ms(access, auth_data)
    now_ms = int(time.time() * 1000)
    if refresh and expires_at_ms and now_ms >= (expires_at_ms - 60_000):
        refreshed = _refresh_codex_token(refresh)
        if refreshed:
            access = _apply_refreshed_tokens(auth_data, refreshed)
            _save_auth_json(path, auth_data)
            expires_at_ms = refreshed.get("expires_at_ms", expires_at_ms)

    account_id = _resolve_account_id(access, auth_data)
    return CodexCredentials(
        access_token=access,
        refresh_token=refresh or "",
        account_id=account_id,
        expires_at_ms=expires_at_ms,
        auth_path=path,
    )


def codex_credentials_ready() -> bool:
    return get_codex_credentials() is not None


def _resolve_codex_model(model: str) -> str:
    """Use the configured model id as-is (strip optional ``provider/`` prefix only)."""
    m = (model or "").strip()
    if "/" in m:
        m = m.rsplit("/", 1)[-1].strip()
    return m


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            parts.append(str(part.get("text") or ""))
        elif part.get("type") == "image_url":
            parts.append("[image]")
    return "\n".join(p for p in parts if p)


def _user_content_blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        text = content.strip()
        return [{"type": "input_text", "text": text or _EMPTY_INPUT_TEXT}]
    if not isinstance(content, list):
        return [{"type": "input_text", "text": _EMPTY_INPUT_TEXT}]
    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = str(part.get("text") or "").strip()
            if text:
                blocks.append({"type": "input_text", "text": text})
        elif ptype == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if isinstance(url, str) and url.startswith("data:"):
                blocks.append({"type": "input_image", "image_url": url, "detail": "auto"})
    return blocks or [{"type": "input_text", "text": _EMPTY_INPUT_TEXT}]


def _messages_to_codex_request(messages: list[dict]) -> tuple[str, list[dict]]:
    """Split OpenAI chat messages into Codex ``instructions`` + ``input`` items.

    Persisted/compacted history may not retain both halves of every tool
    exchange. Codex rejects an orphan ``function_call_output`` and that bad
    history then poisons every later turn. Only emit complete pairs here.
    """
    system_parts: list[str] = []
    input_items: list[dict] = []

    seen_history_calls: set[str] = set()
    result_ids: set[str] = set()
    for history_msg in messages:
        if not isinstance(history_msg, dict):
            continue
        if history_msg.get("role") == "assistant":
            for call in history_msg.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    seen_history_calls.add(str(call["id"]))
        elif history_msg.get("role") == "tool":
            cid = str(history_msg.get("tool_call_id") or "")
            if cid in seen_history_calls:
                result_ids.add(cid)
    emitted_call_ids: set[str] = set()
    emitted_output_ids: set[str] = set()

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "")
        if role == "system":
            text = _text_from_content(msg.get("content", "")).strip()
            if text:
                system_parts.append(text)
            continue

        if role == "user":
            input_items.append({
                "type": "message",
                "role": "user",
                "content": _user_content_blocks(msg.get("content", "")),
            })
            continue

        if role == "assistant":
            text = _text_from_content(msg.get("content", "")).strip()
            if text:
                input_items.append({
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": text}],
                })
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                call_id = str(tc.get("id") or "")
                if (not call_id or call_id not in result_ids
                        or call_id in emitted_call_ids):
                    continue
                args = fn.get("arguments", "")
                if not isinstance(args, str):
                    args = json.dumps(args)
                input_items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": fn.get("name") or "",
                    "arguments": args or "{}",
                })
                emitted_call_ids.add(call_id)
            continue

        if role == "tool":
            call_id = str(msg.get("tool_call_id") or "")
            if (not call_id or call_id not in emitted_call_ids
                    or call_id in emitted_output_ids):
                continue
            output = msg.get("content", "")
            if not isinstance(output, str):
                output = json.dumps(output)
            input_items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": output or "",
            })
            emitted_output_ids.add(call_id)

    instructions = "\n\n".join(system_parts).strip() or DEFAULT_INSTRUCTIONS
    if not input_items:
        input_items.append({
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": _EMPTY_INPUT_TEXT}],
        })
    return instructions, input_items


def _convert_tools(tools: list[dict] | None) -> list[dict]:
    """Convert Chat Completions tool schemas to Codex Responses API shape.

    Chat Completions: ``{type: function, function: {name, description, parameters}}``
    Codex Responses:  ``{type: function, name, description, parameters}``
    """
    if not tools:
        return []
    out: list[dict] = []
    for tool in tools:
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict):
            continue
        name = fn.get("name") or tool.get("name")
        if not name:
            continue
        out.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or tool.get("description") or "",
            "parameters": fn.get("parameters") or tool.get("parameters") or {
                "type": "object",
                "properties": {},
            },
        })
    return out


def _map_reasoning_effort(effort: str | None) -> str | None:
    e = (effort or "").strip().lower()
    if not e or e == "off":
        return None
    if e == "minimal":
        return "low"
    if e in ("low", "medium", "high", "xhigh"):
        return e
    return "medium"


class _OpenAICodexCompletions:
    """Codex OAuth client: streams SSE internally and accepts ``stream_callback``."""

    # ReasoningClientWrapper must not wrap this with OpenAI-SDK stream=True —
    # Codex returns a completed response, not a chunk iterator.
    handles_native_stream_callback = True

    def __init__(self, wrapper: "OpenAICodexClientWrapper"):
        self._wrapper = wrapper

    def create(self, **kwargs):
        stream_callback = kwargs.pop("stream_callback", None)
        kwargs.pop("stream", None)
        kwargs.pop("stream_options", None)
        kwargs.pop("temperature", None)
        # Responses API takes max_output_tokens; carry the cap over instead of
        # dropping it (an uncapped request can run to the model's ceiling —
        # slow turns + surprise token burn).
        max_out = kwargs.pop("max_tokens", None) or kwargs.pop("max_completion_tokens", None)
        kwargs.pop("max_completion_tokens", None)
        kwargs.pop("top_p", None)

        model = _resolve_codex_model(kwargs.get("model", ""))
        messages = kwargs.get("messages") or []
        tools = _convert_tools(kwargs.get("tools"))
        reasoning_effort = _map_reasoning_effort(kwargs.pop("reasoning_effort", None))

        creds = self._wrapper._ensure_fresh_credentials()
        instructions, input_items = _messages_to_codex_request(messages)

        body: dict[str, Any] = {
            "model": model,
            "instructions": instructions,
            "input": input_items,
            "store": False,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        # NOTE: the ChatGPT Codex backend rejects max_output_tokens with
        # HTTP 400 "Unsupported parameter" (observed 2026-07). Do NOT send it;
        # output length is governed by the model/plan instead. max_out is
        # still popped above so it never leaks into the request body.
        _ = max_out

        headers = {
            "Authorization": f"Bearer {creds.access_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "User-Agent": "agent-app/1.0",
        }
        if creds.account_id:
            headers["ChatGPT-Account-ID"] = creds.account_id

        import httpx

        try:
            # read= caps how long a SINGLE socket read may stall — both the
            # wait for response headers and gaps between SSE chunks. The old
            # blanket 300s meant a dead server froze the turn for 5 minutes
            # PER RETRY (×7 retries ≈ half an hour of "is typing…"). 120s is
            # generous for the longest legitimate inter-token gap.
            with httpx.Client(timeout=httpx.Timeout(
                    _REQUEST_TIMEOUT_S, connect=30.0, read=_request_read_timeout_s(),
                    write=30.0, pool=30.0)) as client:
                with client.stream(
                    "POST", CODEX_RESPONSES_URL, headers=headers, json=body
                ) as resp:
                    if resp.status_code >= 400:
                        err_body = resp.read().decode(errors="replace")
                        error = RuntimeError(
                            f"Codex API error {resp.status_code}: {err_body[:800]}"
                        )
                        error.status_code = resp.status_code
                        raise error
                    collected_text: list[str] = []
                    saw_text_deltas = False
                    thinking_parts: list[str] = []
                    tool_calls: dict[str, dict] = {}
                    usage: dict = {}
                    finish_reason = "stop"

                    event_name = "message"
                    data_lines: list[str] = []
                    stream_terminal = False

                    def _emit_text(text: str, *, from_delta: bool = True) -> None:
                        nonlocal saw_text_deltas
                        if not text:
                            return
                        if from_delta:
                            saw_text_deltas = True
                        collected_text.append(text)
                        if stream_callback:
                            try:
                                stream_callback(text)
                            except Exception:
                                pass

                    def _handle_event() -> None:
                        nonlocal event_name, data_lines, finish_reason, stream_terminal
                        if not data_lines:
                            return
                        joined = "\n".join(data_lines).strip()
                        data_lines.clear()
                        if joined == "[DONE]":
                            stream_terminal = True
                            event_name = "message"
                            return
                        try:
                            data = json.loads(joined) if joined else None
                        except json.JSONDecodeError:
                            event_name = "message"
                            return

                        evt = event_name
                        if isinstance(data, dict):
                            evt = str(data.get("type") or event_name)

                        if evt == "response.output_text.delta" and isinstance(data, dict):
                            delta = data.get("delta")
                            if isinstance(delta, str) and delta:
                                _emit_text(delta, from_delta=True)
                        elif evt == "response.output_text.done" and isinstance(data, dict):
                            text = data.get("text")
                            if isinstance(text, str) and text:
                                joined_text = "".join(collected_text)
                                if not joined_text:
                                    _emit_text(text, from_delta=not saw_text_deltas)
                                elif text != joined_text and text.startswith(joined_text):
                                    _emit_text(text[len(joined_text):], from_delta=False)
                        elif evt == "response.reasoning_summary_text.delta" and isinstance(data, dict):
                            delta = data.get("delta")
                            if isinstance(delta, str):
                                thinking_parts.append(delta)
                        elif evt == "response.function_call_arguments.delta" and isinstance(data, dict):
                            idx = str(data.get("output_index", 0))
                            slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                            delta = data.get("delta")
                            if isinstance(delta, str):
                                slot["args"] += delta
                        elif evt == "response.output_item.added" and isinstance(data, dict):
                            item = data.get("item") if isinstance(data.get("item"), dict) else data
                            if isinstance(item, dict) and item.get("type") == "function_call":
                                idx = str(data.get("output_index", len(tool_calls)))
                                tool_calls[idx] = {
                                    "id": item.get("call_id") or item.get("id") or "",
                                    "name": item.get("name") or "",
                                    "args": str(item.get("arguments") or ""),
                                }
                        elif evt == "response.output_item.done" and isinstance(data, dict):
                            item = data.get("item") if isinstance(data.get("item"), dict) else data
                            if isinstance(item, dict):
                                if item.get("type") == "function_call":
                                    idx = str(data.get("output_index", len(tool_calls)))
                                    slot = tool_calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                                    if item.get("call_id"):
                                        slot["id"] = item["call_id"]
                                    if item.get("name"):
                                        slot["name"] = item["name"]
                                    if item.get("arguments"):
                                        slot["args"] = str(item["arguments"])
                                elif item.get("type") == "message" and not saw_text_deltas:
                                    for block in item.get("content") or []:
                                        if isinstance(block, dict) and block.get("type") == "output_text":
                                            text = block.get("text")
                                            if isinstance(text, str) and text:
                                                _emit_text(text, from_delta=False)
                        elif evt in ("response.completed", "response.done") and isinstance(data, dict):
                            resp = data.get("response") if isinstance(data.get("response"), dict) else data
                            u = resp.get("usage") if isinstance(resp, dict) else None
                            if isinstance(u, dict):
                                usage = u
                            stream_terminal = True
                        elif evt in ("response.failed", "response.incomplete", "error"):
                            stream_terminal = True
                            if isinstance(data, dict):
                                err = data.get("error") or data.get("message") or data
                                raise RuntimeError(f"Codex stream ended with {evt}: {err}")

                        event_name = "message"

                    for line in resp.iter_lines():
                        if stream_terminal:
                            break
                        if not line:
                            _handle_event()
                            if stream_terminal:
                                break
                            continue
                        if line.startswith("event:"):
                            event_name = line.split(":", 1)[1].strip() or "message"
                        elif line.startswith("data:"):
                            data_lines.append(line.split(":", 1)[1].lstrip())
                    if not stream_terminal:
                        _handle_event()

        except httpx.HTTPError as e:
            raise RuntimeError(f"Codex request failed: {e}") from e

        tc_list = None
        if tool_calls:
            tc_list = [
                _AttrDict({
                    "id": tool_calls[i]["id"],
                    "type": "function",
                    "function": _AttrDict({
                        "name": tool_calls[i]["name"],
                        "arguments": tool_calls[i]["args"] or "{}",
                    }),
                })
                for i in sorted(tool_calls, key=lambda x: int(x) if str(x).isdigit() else str(x))
            ]
            finish_reason = "tool_calls"

        pt = int(usage.get("input_tokens") or 0)
        ct = int(usage.get("output_tokens") or 0)
        message = _AttrDict({
            "content": "".join(collected_text),
            "tool_calls": tc_list,
            "role": "assistant",
            "refusal": None,
            "_thinking": "".join(thinking_parts) if thinking_parts else None,
        })
        return _AttrDict({
            "choices": [_AttrDict({"message": message, "finish_reason": finish_reason})],
            "model": model,
            "usage": _AttrDict({
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": int(usage.get("total_tokens") or (pt + ct)),
            }),
        })


class _OpenAICodexChat:
    def __init__(self, wrapper: "OpenAICodexClientWrapper"):
        self.completions = _OpenAICodexCompletions(wrapper)


class OpenAICodexClientWrapper:
    """OpenAI-shaped client for ChatGPT / Codex subscription OAuth."""

    def __init__(self, credentials: CodexCredentials | None = None):
        self._creds = credentials or get_codex_credentials()
        if not self._creds:
            raise ValueError("No Codex OAuth credentials available")
        self.chat = _OpenAICodexChat(self)

    def _ensure_fresh_credentials(self) -> CodexCredentials:
        fresh = get_codex_credentials()
        if fresh:
            self._creds = fresh
            return fresh
        if self._creds.refresh_token:
            refreshed = _refresh_codex_token(self._creds.refresh_token)
            if refreshed:
                loaded = _load_auth_json(self._creds.auth_path)
                if loaded:
                    auth_data, path = loaded
                    access = _apply_refreshed_tokens(auth_data, refreshed)
                    _save_auth_json(path, auth_data)
                    self._creds = CodexCredentials(
                        access_token=access,
                        refresh_token=refreshed["refresh_token"],
                        account_id=_resolve_account_id(access, auth_data),
                        expires_at_ms=refreshed.get("expires_at_ms"),
                        auth_path=path,
                    )
                    return self._creds
        return self._creds
