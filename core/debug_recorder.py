"""
DebugRecorder — thread-safe per-turn LLM pipeline recorder, scoped per conversation.
Captures the context and output of each API call round. Persists to SQLite
(`conversations.debug_turns_json`) so debug survives reload and stays isolated
per chat.

Design constraints (learned the hard way — see logs/native_crash.log):
- NEVER deep-copy the live context. Messages can be huge (100s of KB each) and
  a 12-turn bucket deep-copied per step caused GB-scale allocation churn, GC
  pauses, and correlated with access-violation crashes during long runs.
- Store SANITIZED plain data only: role/content strings clipped to a cap,
  tool calls summarized. Nothing exotic (SDK objects, bytes, Qt refs) survives
  into the recorder, so later json.dumps/GC can't trip over it.
- Per step, store only the DELTA of messages appended since the previous step.
  The full per-step context view is reconstructed on demand (debug panel /
  audit tool), not materialized in the hot inference loop.
- Persist from ONE background writer thread, debounced — the agent loop never
  blocks on a SQLite write again.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional

from core.database import (
    fit_debug_turns,
    get_conversation_debug_turns,
    set_conversation_debug_turns_json,
)

# Per-message content kept in a snapshot (chars). Head+tail clip beyond this.
_MSG_CAP = 3000
# Max messages stored per snapshot/delta (middle dropped beyond this).
_MSG_LIMIT = 300
# Debounce window: burst of record_step calls coalesces into one DB write.
_FLUSH_DEBOUNCE_S = 1.5


def estimate_tokens_from_text(text: str | None) -> int:
    """Cheap token estimate (~chars/4). Deliberately O(1) on string length —
    the old regex-span counter walked megabytes of context per step."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_tokens_from_messages(messages: List[Dict[str, Any]] | None) -> int:
    """Approximate token count for a list of chat messages (len-based, fast)."""
    if not messages:
        return 0
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            total += estimate_tokens_from_text(str(msg))
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens_from_text(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens_from_text(part.get("text", "")) or 400
        for tc in (msg.get("tool_calls") or []):
            try:
                total += estimate_tokens_from_text(
                    (tc.get("function") or {}).get("arguments", "") if isinstance(tc, dict) else ""
                )
            except Exception:
                pass
        total += 4  # role + structure overhead
    return total


def _clip(text: str, cap: int = _MSG_CAP) -> str:
    """Head+tail clip with an explicit marker so the panel shows what happened."""
    if len(text) <= cap:
        return text
    head = (cap * 2) // 3
    tail = cap - head
    return (f"{text[:head]}\n… [debug snapshot: {len(text) - cap:,} chars "
            f"clipped] …\n{text[-tail:]}")


def _sanitize_content(content: Any) -> tuple[str, int]:
    """Flatten any message content into a clipped plain string.
    Returns (text, original_char_count)."""
    if content is None:
        return "", 0
    if isinstance(content, str):
        return _clip(content), len(content)
    if isinstance(content, list):
        parts: List[str] = []
        orig = 0
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type", "")
                if ptype in ("image_url", "image", "input_image"):
                    parts.append("[image omitted from debug snapshot]")
                    orig += 4 * 1500  # flat image token estimate, in chars
                else:
                    txt = part.get("text") or ""
                    if not isinstance(txt, str):
                        txt = str(txt)
                    orig += len(txt)
                    parts.append(_clip(txt))
            else:
                s = str(part)
                orig += len(s)
                parts.append(_clip(s))
        return "\n".join(parts), orig
    s = str(content)
    return _clip(s), len(s)


def _sanitize_message(msg: Any) -> Dict[str, Any]:
    """Reduce one message to plain, bounded, JSON-safe data."""
    if not isinstance(msg, dict):
        text, orig = _sanitize_content(msg)
        return {"role": "?", "content": text, "_orig_chars": orig}
    text, orig = _sanitize_content(msg.get("content"))
    out: Dict[str, Any] = {"role": str(msg.get("role", "")), "content": text}
    if orig > len(text):
        out["_orig_chars"] = orig
    if msg.get("tool_call_id"):
        out["tool_call_id"] = str(msg["tool_call_id"])
    if msg.get("name"):
        out["name"] = str(msg["name"])
    tcs = msg.get("tool_calls")
    if tcs:
        slim = []
        for tc in tcs:
            try:
                if isinstance(tc, dict):
                    fn = tc.get("function") or {}
                    slim.append({
                        "id": str(tc.get("id", "")),
                        "name": str(fn.get("name", "")),
                        "args": _clip(str(fn.get("arguments", "")), 500),
                    })
                else:  # SDK object
                    slim.append({
                        "id": str(getattr(tc, "id", "")),
                        "name": str(getattr(getattr(tc, "function", None), "name", "")),
                        "args": _clip(str(getattr(getattr(tc, "function", None),
                                                  "arguments", "")), 500),
                    })
            except Exception:
                slim.append({"id": "", "name": "?", "args": ""})
        out["tool_calls"] = slim
    return out


def _sanitize_messages(messages: List[Any] | None) -> List[Dict[str, Any]]:
    msgs = messages or []
    if len(msgs) > _MSG_LIMIT:
        head, tail = 20, _MSG_LIMIT - 21
        kept = list(msgs[:head]) + [
            {"role": "system",
             "content": f"[debug snapshot: {len(msgs) - head - tail} middle "
                        f"messages omitted]"}
        ] + list(msgs[-tail:])
        return [m if isinstance(m, dict) and "_orig_chars" in m else _sanitize_message(m)
                for m in kept]
    return [_sanitize_message(m) for m in msgs]


def _conv_storage_key(conversation_id: str) -> str:
    return (conversation_id or "").strip() or "__no_conv__"


class DebugRecorder:
    """
    Thread-safe recorder for per-turn LLM pipeline transparency.
    One list of turns per conversation id (in-memory, bounded), mirrored to
    SQLite by a debounced background writer.
    """

    def __init__(self, max_turns: int = 12):
        self._lock = threading.RLock()
        self._max_turns = max_turns
        self._by_conv: Dict[str, List[Dict[str, Any]]] = {}
        self._dirty: set[str] = set()
        self._wake = threading.Event()
        self._writer_started = False

    # ── background persistence ────────────────────────────────────────

    def _mark_dirty(self, storage_key: str) -> None:
        if storage_key == "__no_conv__":
            return
        with self._lock:
            self._dirty.add(storage_key)
            if not self._writer_started:
                self._writer_started = True
                t = threading.Thread(target=self._writer_loop, daemon=True,
                                     name="debug-recorder-writer")
                t.start()
        self._wake.set()

    def _writer_loop(self) -> None:
        while True:
            self._wake.wait()
            time.sleep(_FLUSH_DEBOUNCE_S)  # coalesce bursts of steps
            self._wake.clear()
            with self._lock:
                dirty = list(self._dirty)
                self._dirty.clear()
                # Serialize under the lock: buckets are only mutated while the
                # lock is held, so dumps never races an append.
                payloads = []
                for key in dirty:
                    bucket = self._by_conv.get(key)
                    if bucket is None:
                        continue
                    try:
                        payloads.append((key, fit_debug_turns(bucket)))
                    except Exception:
                        pass
            for key, payload in payloads:
                try:
                    set_conversation_debug_turns_json(key, payload)
                except Exception as e:
                    print(f"[DebugRecorder] DB save failed: {e}")

    def flush(self) -> None:
        """Force-persist all dirty conversations now (call on shutdown)."""
        with self._lock:
            dirty = list(self._dirty)
            self._dirty.clear()
            payloads = []
            for key in dirty:
                bucket = self._by_conv.get(key)
                if bucket is None:
                    continue
                try:
                    payloads.append((key, fit_debug_turns(bucket)))
                except Exception:
                    pass
        for key, payload in payloads:
            try:
                set_conversation_debug_turns_json(key, payload)
            except Exception:
                pass

    # ── bucket management ────────────────────────────────────────────

    def load_conversation_from_db(self, conversation_id: str) -> None:
        """Replace in-memory debug turns for *conversation_id* from SQLite."""
        if not (conversation_id or "").strip():
            return
        key = _conv_storage_key(conversation_id)
        loaded = get_conversation_debug_turns(conversation_id)
        with self._lock:
            self._by_conv[key] = list(loaded[-self._max_turns:]) if loaded else []

    def drop_conversation(self, conversation_id: str) -> None:
        """Drop cached turns for a deleted conversation."""
        if not (conversation_id or "").strip():
            return
        key = _conv_storage_key(conversation_id)
        with self._lock:
            self._by_conv.pop(key, None)
            self._dirty.discard(key)

    def _bucket_unlocked(self, conversation_id: str) -> List[Dict[str, Any]]:
        key = _conv_storage_key(conversation_id)
        if key not in self._by_conv:
            if key == "__no_conv__":
                self._by_conv[key] = []
            else:
                loaded = get_conversation_debug_turns(conversation_id)
                self._by_conv[key] = list(loaded[-self._max_turns:]) if loaded else []
        return self._by_conv[key]

    def _locate_turn_unlocked(self, turn_id: str) -> tuple[str, Dict[str, Any]] | None:
        for key, turns in self._by_conv.items():
            for t in turns:
                if t.get("id") == turn_id:
                    return key, t
        return None

    # ── recording ────────────────────────────────────────────────────

    def start_turn(
        self,
        base_context: List[Dict[str, Any]] | None,
        user_message: str,
        model_name: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        conversation_id: str = "",
    ) -> str:
        """Begin a new turn and return its id."""
        turn_id = f"{int(time.time() * 1000)}"
        turn: Dict[str, Any] = {
            "id": turn_id,
            "conversation_id": (conversation_id or "").strip(),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model_name": model_name or "",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "user_message": _clip(user_message or "", 8000),
            "base_context": _sanitize_messages(base_context),
            # raw message count at last snapshot — used to slice per-step deltas
            "_ctx_len": len(base_context or []),
            "steps": [],
            "error": None,
            "totals": {"steps": 0, "tokens_context": 0, "tokens_response": 0, "tokens_all": 0},
        }
        with self._lock:
            bucket = self._bucket_unlocked(conversation_id)
            bucket.append(turn)
            while len(bucket) > self._max_turns:
                bucket.pop(0)
            storage_key = _conv_storage_key(conversation_id)
        self._mark_dirty(storage_key)
        return turn_id

    def record_step(
        self,
        turn_id: str,
        name: str,
        context: List[Dict[str, Any]] | None,
        response: str | None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Add an inference step to an existing turn. Stores only the messages
        appended since the previous step (delta) — bounded and sanitized."""
        raw = context or []
        # Sanitize OUTSIDE the lock — the caller's thread owns this list and
        # isn't mutating it during the call.
        tokens_ctx = estimate_tokens_from_messages(raw)
        with self._lock:
            found = self._locate_turn_unlocked(turn_id)
            if not found:
                return None
            storage_key, target = found
            prev_len = int(target.get("_ctx_len", 0))
            if len(raw) >= prev_len:
                delta = _sanitize_messages(raw[prev_len:])
                reset = False
            else:
                # Context shrank (emergency compact / rebuild) — snapshot fresh.
                delta = _sanitize_messages(raw)
                reset = True
            target["_ctx_len"] = len(raw)

            step: Dict[str, Any] = {
                "index": len(target.get("steps") or []) + 1,
                "name": name,
                "context_delta": delta,
                "context_len": len(raw),
                "response": _clip(response or "", 16000),
                "meta": self._sanitize_meta(meta),
                "tokens_context": tokens_ctx,
                "tokens_response": estimate_tokens_from_text(response),
                "timestamp": time.time(),
            }
            if reset:
                step["context_reset"] = True
            target["steps"].append(step)
            self._retotal_unlocked(target)
            result = dict(step)
        self._mark_dirty(storage_key)
        return result

    @staticmethod
    def _sanitize_meta(meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        if not meta:
            return {}
        try:
            # Round-trip through JSON with clipping — guarantees plain data.
            return json.loads(_clip(json.dumps(meta, ensure_ascii=False,
                                               default=str), 20000))
        except Exception:
            return {"_meta_error": "unserializable meta dropped"}

    @staticmethod
    def _retotal_unlocked(target: Dict[str, Any]) -> None:
        steps = target.get("steps") or []
        tokens_in = sum(s.get("tokens_context", 0) for s in steps)
        tokens_out = sum(s.get("tokens_response", 0) for s in steps)
        target["totals"] = {
            "steps": len(steps),
            "tokens_context": tokens_in,
            "tokens_response": tokens_out,
            "tokens_all": tokens_in + tokens_out,
        }

    def finalize_turn(self, turn_id: str, error: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Mark a turn as complete (optionally with an error message)."""
        with self._lock:
            found = self._locate_turn_unlocked(turn_id)
            if not found:
                return None
            storage_key, target = found
            target["error"] = error
            self._retotal_unlocked(target)
            result = self._view_turn_unlocked(target)
        self._mark_dirty(storage_key)
        return result

    # ── read side (debug panel / audit tool) ─────────────────────────

    def _view_turn_unlocked(self, turn: Dict[str, Any]) -> Dict[str, Any]:
        """Reconstruct the legacy shape: each step carries its full 'context'
        view (base_context + accumulated deltas). Shallow — treat as read-only."""
        ctx: List[Any] = list(turn.get("base_context") or [])
        steps_view = []
        for step in turn.get("steps") or []:
            if "context" in step:  # legacy persisted format (full snapshots)
                ctx = list(step.get("context") or [])
                steps_view.append(dict(step))
                continue
            if step.get("context_reset"):
                ctx = list(step.get("context_delta") or [])
            else:
                ctx = ctx + list(step.get("context_delta") or [])
            sv = {k: v for k, v in step.items() if k != "context_delta"}
            sv["context"] = ctx
            steps_view.append(sv)
        tv = {k: v for k, v in turn.items() if k not in ("_ctx_len", "steps")}
        tv["steps"] = steps_view
        return tv

    def latest_turn(self, conversation_id: str = "") -> Optional[Dict[str, Any]]:
        """Return the most recent turn view for *conversation_id* (read-only)."""
        with self._lock:
            bucket = self._bucket_unlocked(conversation_id)
            if not bucket:
                return None
            return self._view_turn_unlocked(bucket[-1])

    def get_turn(self, index: int, conversation_id: str = "") -> Optional[Dict[str, Any]]:
        """Return the turn view at position *index* (0 = oldest), read-only."""
        with self._lock:
            bucket = self._bucket_unlocked(conversation_id)
            if not bucket:
                return None
            try:
                return self._view_turn_unlocked(bucket[index])
            except IndexError:
                return None

    def turn_count(self, conversation_id: str = "") -> int:
        with self._lock:
            bucket = self._bucket_unlocked(conversation_id)
            return len(bucket)


# Module-level singleton — imported by agent.py and debug_panel.py
debug_recorder = DebugRecorder()
