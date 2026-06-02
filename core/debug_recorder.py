"""
DebugRecorder — thread-safe per-turn LLM pipeline recorder, scoped per conversation.
Captures the full context and output of each API call round. Persists to SQLite
(`conversations.debug_turns_json`) so debug survives reload and stays isolated
per chat.
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from typing import Any, Dict, List, Optional

from core.database import get_conversation_debug_turns, set_conversation_debug_turns


def estimate_tokens_from_text(text: str | None) -> int:
    """Lightweight token estimate — counts word and symbol spans."""
    if not text:
        return 0
    return len(re.findall(r"\w+|\S", text))


def estimate_tokens_from_messages(messages: List[Dict[str, Any]] | None) -> int:
    """Approximate token count for a list of chat messages."""
    if not messages:
        return 0
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens_from_text(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total += estimate_tokens_from_text(part.get("text", ""))
        total += 4  # role + structure overhead
    return total


def _conv_storage_key(conversation_id: str) -> str:
    return (conversation_id or "").strip() or "__no_conv__"


class DebugRecorder:
    """
    Thread-safe recorder for per-turn LLM pipeline transparency.
    One deque of turns per conversation id (in-memory), mirrored to SQLite.
    """

    def __init__(self, max_turns: int = 25):
        self._lock = threading.RLock()
        self._max_turns = max_turns
        self._by_conv: Dict[str, List[Dict[str, Any]]] = {}

    def load_conversation_from_db(self, conversation_id: str) -> None:
        """Replace in-memory debug turns for *conversation_id* from SQLite."""
        if not (conversation_id or "").strip():
            return
        key = _conv_storage_key(conversation_id)
        loaded = get_conversation_debug_turns(conversation_id)
        with self._lock:
            self._by_conv[key] = copy.deepcopy(loaded[-self._max_turns :]) if loaded else []

    def drop_conversation(self, conversation_id: str) -> None:
        """Drop cached turns for a deleted conversation."""
        if not (conversation_id or "").strip():
            return
        key = _conv_storage_key(conversation_id)
        with self._lock:
            self._by_conv.pop(key, None)

    def _bucket_unlocked(self, conversation_id: str) -> List[Dict[str, Any]]:
        key = _conv_storage_key(conversation_id)
        if key not in self._by_conv:
            if key == "__no_conv__":
                self._by_conv[key] = []
            else:
                loaded = get_conversation_debug_turns(conversation_id)
                self._by_conv[key] = (
                    copy.deepcopy(loaded[-self._max_turns :]) if loaded else []
                )
        return self._by_conv[key]

    def _locate_turn_unlocked(self, turn_id: str) -> tuple[str, Dict[str, Any]] | None:
        for key, turns in self._by_conv.items():
            for t in turns:
                if t.get("id") == turn_id:
                    return key, t
        return None

    def _persist_key(self, storage_key: str, turns_snapshot: List[Dict[str, Any]]) -> None:
        if storage_key == "__no_conv__":
            return
        try:
            set_conversation_debug_turns(storage_key, turns_snapshot)
        except Exception as e:
            print(f"[DebugRecorder] DB save failed: {e}")

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
            "user_message": user_message,
            "base_context": copy.deepcopy(base_context) if base_context else [],
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
            snapshot = copy.deepcopy(bucket)
        self._persist_key(storage_key, snapshot)
        return turn_id

    def record_step(
        self,
        turn_id: str,
        name: str,
        context: List[Dict[str, Any]] | None,
        response: str | None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Add an inference step to an existing turn."""
        with self._lock:
            found = self._locate_turn_unlocked(turn_id)
            if not found:
                return None
            storage_key, target = found

            step_index = len(target.get("steps") or []) + 1
            step: Dict[str, Any] = {
                "index": step_index,
                "name": name,
                "context": copy.deepcopy(context) if context else [],
                "response": response or "",
                "meta": copy.deepcopy(meta) if meta else {},
                "tokens_context": estimate_tokens_from_messages(context),
                "tokens_response": estimate_tokens_from_text(response),
                "timestamp": time.time(),
            }
            target["steps"].append(step)

            tokens_in = sum(s.get("tokens_context", 0) for s in target["steps"])
            tokens_out = sum(s.get("tokens_response", 0) for s in target["steps"])
            target["totals"] = {
                "steps": len(target["steps"]),
                "tokens_context": tokens_in,
                "tokens_response": tokens_out,
                "tokens_all": tokens_in + tokens_out,
            }

            snapshot = copy.deepcopy(self._by_conv.get(storage_key, []))
            result = copy.deepcopy(step)

        self._persist_key(storage_key, snapshot)
        return result

    def finalize_turn(self, turn_id: str, error: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Mark a turn as complete (optionally with an error message)."""
        with self._lock:
            found = self._locate_turn_unlocked(turn_id)
            if not found:
                return None
            storage_key, target = found
            target["error"] = error
            tokens_in = sum(s.get("tokens_context", 0) for s in target["steps"])
            tokens_out = sum(s.get("tokens_response", 0) for s in target["steps"])
            target["totals"] = {
                "steps": len(target["steps"]),
                "tokens_context": tokens_in,
                "tokens_response": tokens_out,
                "tokens_all": tokens_in + tokens_out,
            }
            snapshot = copy.deepcopy(self._by_conv.get(storage_key, []))
            result = copy.deepcopy(target)

        self._persist_key(storage_key, snapshot)
        return result

    def latest_turn(self, conversation_id: str = "") -> Optional[Dict[str, Any]]:
        """Return the most recent turn snapshot for *conversation_id*."""
        with self._lock:
            bucket = self._bucket_unlocked(conversation_id)
            if not bucket:
                return None
            return copy.deepcopy(bucket[-1])

    def get_turn(self, index: int, conversation_id: str = "") -> Optional[Dict[str, Any]]:
        """Return the turn at position *index* (0 = oldest) for *conversation_id*."""
        with self._lock:
            bucket = self._bucket_unlocked(conversation_id)
            if not bucket:
                return None
            try:
                return copy.deepcopy(bucket[index])
            except IndexError:
                return None

    def turn_count(self, conversation_id: str = "") -> int:
        with self._lock:
            bucket = self._bucket_unlocked(conversation_id)
            return len(bucket)


# Module-level singleton — imported by agent.py and debug_panel.py
debug_recorder = DebugRecorder()
