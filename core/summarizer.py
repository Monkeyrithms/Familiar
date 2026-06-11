"""
Rolling summary — compresses older conversation into a narrative summary,
keeping recent messages in full. Maintains last 3 summaries for recovery.

Each memory stream gets its own independent summarizer with custom guidance,
so different streams can emphasize different aspects of the same conversation.
State is persisted in per-stream SQLite databases via core.database.
"""

import json
import time
from pathlib import Path
from typing import Optional
from core.providers import get_client

DEFAULT_SUMMARY_GUIDANCE = (
    "Preserve ALL: decisions, files, code, problems solved, user prefs, unresolved Qs."
)

DEFAULT_MERGE_GUIDANCE = (
    "✗ lose existing details. Add all new developments, decisions, discoveries."
)

# Structured template for coding-focused summaries — ensures actionable
# context survives compression. 9-section format adapted from claw-code's
# compaction strategy. The "All User Messages" + "Current Work & Next Step"
# sections are load-bearing — they prevent agent drift from what was actually
# asked after several rounds of compression.
_STRUCTURED_TEMPLATE = """
## Primary Request
User's original objective, stated plainly. If goal evolved, note the latest form.

## Plan & Instructions
Active plan, constraints, user corrections/prefs given mid-conversation.

## Key Technical Context
Frameworks, libraries, architecture patterns, system constraints in play.

## Files & Code Sections
Every file read/created/modified. Brief note + line numbers for key locs.

## Errors & Fixes
Bugs encountered + the specific fix that worked. ✗ vague "fixed it".

## Discoveries
Surprises, architectural insights, approach changes — anything that altered the plan.

## Progress
Done / in-progress / remaining. Specific file paths.

## All User Messages
Chronological bullet list of every user message in this conversation, abbreviated
to <=120 chars each, preserving INTENT (not just topic). Critical anchor against
drift — do NOT skip any user message, even seemingly minor ones.

## Current Work & Next Step
What was happening immediately before this summary cutoff. End with the user's
MOST RECENT request quoted VERBATIM in quotes. The next action must align with
that exact quote, not a paraphrase or expansion of it.
""".strip()


def _build_summary_prompt(guidance: str = "") -> str:
    g = guidance.strip() if guidance else DEFAULT_SUMMARY_GUIDANCE
    return (
        f"Summarizer for AI coding agent. Compress conversation to structured summary.\n"
        f"{g}\n\n{_STRUCTURED_TEMPLATE}"
    )


def _build_merge_prompt(guidance: str = "") -> str:
    g = guidance.strip() if guidance else DEFAULT_MERGE_GUIDANCE
    return (
        f"Summarizer for AI coding agent. Update existing summary with new events.\n"
        f"Integrate into existing structure. {g}\n"
        f"Rules:\n"
        f"- All User Messages: APPEND new ones; never drop existing entries.\n"
        f"- Current Work & Next Step: REPLACE entirely with the latest state, "
        f"including the latest user request quoted verbatim.\n"
        f"- Other sections: keep unchanged if no updates; add new files/errors as they appear.\n\n"
        f"{_STRUCTURED_TEMPLATE}"
    )


class StreamSummarizer:
    """A single summarizer instance for one memory stream."""

    def __init__(self, conv_id: str, stream_name: str, guidance: str = ""):
        self.conv_id = conv_id
        self.stream_name = stream_name
        self.guidance = guidance
        self.current_summary: Optional[str] = None
        self.summary_end_index: int = 0
        self.chars_since_last_summary: int = 0
        self._last_total_chars: int = 0
        self._history: list[dict] = []

    def reset(self):
        self.current_summary = None
        self.summary_end_index = 0
        self.chars_since_last_summary = 0
        self._last_total_chars = 0

    # ── State persistence (SQLite via core.database) ───────────────

    def save_state(self):
        if not self.conv_id:
            return
        from core.database import save_stream_summary
        save_stream_summary(
            self.stream_name, self.conv_id,
            self.current_summary or "",
            self.summary_end_index,
            self.chars_since_last_summary,
            self._last_total_chars,
        )

    def load_state(self):
        if not self.conv_id:
            return
        from core.database import load_stream_summary
        data = load_stream_summary(self.stream_name, self.conv_id)
        if not data:
            return
        self.current_summary = data["summary"] or None
        self.summary_end_index = data["end_index"]
        self.chars_since_last_summary = data["chars_since"]
        self._last_total_chars = data["last_total_chars"]

    def get_history(self) -> list[dict]:
        if not self.conv_id:
            return []
        from core.database import load_stream_summary_history
        return load_stream_summary_history(self.stream_name, self.conv_id)

    # ── Snapshot / restore for undo ────────────────────────────────

    def snapshot(self) -> dict:
        """Capture this stream's full summary state as a plain dict."""
        return {
            "summary": self.current_summary or "",
            "end_index": int(self.summary_end_index),
            "chars_since": int(self.chars_since_last_summary),
            "last_total_chars": int(self._last_total_chars),
        }

    def restore_from_snapshot(self, snap: dict):
        """Restore in-memory state from a snapshot and persist it."""
        if not isinstance(snap, dict):
            return
        self.current_summary = (snap.get("summary") or "") or None
        self.summary_end_index = int(snap.get("end_index") or 0)
        self.chars_since_last_summary = int(snap.get("chars_since") or 0)
        self._last_total_chars = int(snap.get("last_total_chars") or 0)
        # Persist — write through to DB so next build_context sees the rollback
        self.save_state()

    def clear_persisted(self):
        """Wipe the summary from DB and reset in-memory state."""
        self.reset()
        if not self.conv_id:
            return
        from core.database import clear_stream_summary
        clear_stream_summary(self.stream_name, self.conv_id)

    # ── LLM calls ───────────────────────────────────────────────────

    def _llm_call(self, system: str, user: str, routes: list[dict],
                   temperature: float = 0.3) -> Optional[str]:
        """Try each provider/model route in order; return None if all fail."""
        if not routes:
            return None
        last_err = None
        for i, route in enumerate(routes):
            provider = (route.get("provider") or "openrouter").strip()
            model = (route.get("model") or "").strip()
            if not model:
                continue
            try:
                client = get_client(provider)
                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    max_tokens=2048,
                    temperature=temperature,
                )
                text = (resp.choices[0].message.content or "").strip()
                if text:
                    if i > 0:
                        print(f"[Summarizer:{self.stream_name}] Fallback ok: {provider}/{model}")
                    return text
            except Exception as e:
                last_err = e
                print(f"[Summarizer:{self.stream_name}] LLM error ({provider}/{model}): {e}")
                if i < len(routes) - 1:
                    nxt = routes[i + 1]
                    print(
                        f"[Summarizer:{self.stream_name}] Trying fallback "
                        f"{i + 2}/{len(routes)}: {nxt.get('provider', '')}/{nxt.get('model', '')}"
                    )
        if last_err is not None and len(routes) == 1:
            print(f"[Summarizer:{self.stream_name}] LLM error: {last_err}")
        return None

    def _generate(self, messages: list[dict], routes: list[dict],
                   temperature: float = 0.3) -> Optional[str]:
        text = _format_messages(messages)
        if not text.strip():
            return None
        return self._llm_call(
            _build_summary_prompt(self.guidance),
            f"Summarize this conversation:\n\n{text}",
            routes, temperature)

    def _merge(self, existing: str, new_messages: list[dict],
               routes: list[dict], temperature: float = 0.3) -> Optional[str]:
        """Return updated summary, or None if the LLM call failed (keep index)."""
        text = _format_messages(new_messages)
        if not text.strip():
            return existing
        merged = self._llm_call(
            _build_merge_prompt(self.guidance),
            f"Existing summary:\n\n{existing}\n\n"
            f"New events:\n\n{text}\n\n"
            f"Create an updated comprehensive summary:",
            routes, temperature)
        if merged is None:
            return None
        return merged if merged else existing


# ── Shared helpers ──────────────────────────────────────────────────

def _is_chat_message(msg: dict) -> bool:
    role = msg.get("role", "")
    if role == "user":
        return True
    if role == "assistant" and not msg.get("tool_calls"):
        content = msg.get("content", "")
        return bool(content and (content.strip() if isinstance(content, str) else True))
    return False


def _count_chars(messages: list[dict]) -> int:
    total = 0
    for msg in messages:
        if not _is_chat_message(msg):
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    total += len(item.get("text", ""))
    return total


def _format_messages(messages: list[dict]) -> str:
    from core.fragments import strip_all as _strip_fragments
    parts = []
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system" or role == "tool":
            continue
        if role == "assistant" and msg.get("tool_calls") and not content:
            continue
        if not content:
            continue
        if isinstance(content, list):
            content = next((c["text"] for c in content
                            if isinstance(c, dict) and c.get("type") == "text"), "")
        if not content or not content.strip():
            continue
        # Strip fragment-wrapped scaffolding (recall notes, AGENTS.md, env info,
        # etc.) — these don't belong in the conversation's long-term summary.
        content = _strip_fragments(content)
        if not content or not content.strip():
            continue
        label = "User" if role == "user" else "Agent"
        parts.append(f"[{label}]: {content}")
    return "\n\n".join(parts)


# ── Main orchestrator ───────────────────────────────────────────────

class RollingSummarizer:
    """Manages multiple StreamSummarizers — one per subscribed memory stream."""

    def __init__(self, conv_id: str = ""):
        self.conv_id = conv_id
        self._streams: dict[str, StreamSummarizer] = {}

    def reset(self):
        for s in self._streams.values():
            s.reset()
        self._streams.clear()

    def save_state(self):
        for s in self._streams.values():
            s.save_state()

    def load_state(self):
        pass  # Streams load lazily in _ensure_streams when build_context is called

    def _ensure_streams(self, stream_configs: list[dict]):
        """Create/update StreamSummarizer instances from stream config dicts."""
        active_names = set()
        for sc in stream_configs:
            name = sc.get("name", "General")
            guidance = sc.get("summary_guidance", "")
            active_names.add(name)
            if name not in self._streams:
                ss = StreamSummarizer(self.conv_id, name, guidance)
                ss.load_state()
                self._streams[name] = ss
            else:
                self._streams[name].guidance = guidance
        # Remove streams no longer subscribed
        for name in list(self._streams.keys()):
            if name not in active_names:
                del self._streams[name]

    def get_history(self) -> list[dict]:
        """Combined history across all streams."""
        out = []
        for name, ss in self._streams.items():
            for h in ss.get_history():
                out.append({**h, "stream": name})
        return out

    # ── Snapshot / restore for undo ────────────────────────────────────

    def snapshot_all(self, stream_configs: list[dict] | None = None) -> dict:
        """
        Return a dict mapping stream_name → snapshot dict for all subscribed streams.
        Ensures streams are loaded first so the snapshot reflects persisted state.
        """
        if stream_configs is not None:
            self._ensure_streams(stream_configs)
        return {name: ss.snapshot() for name, ss in self._streams.items()}

    def restore_all(self, snapshot: dict, stream_configs: list[dict] | None = None):
        """
        Restore each stream's summary state from a snapshot dict.
        Writes through to the per-stream DBs. Streams present in the snapshot
        but not currently loaded are loaded first so restore sticks.
        """
        if not isinstance(snapshot, dict) or not snapshot:
            return
        if stream_configs is not None:
            self._ensure_streams(stream_configs)
        for name, snap in snapshot.items():
            ss = self._streams.get(name)
            if ss is None:
                ss = StreamSummarizer(self.conv_id, name, "")
                self._streams[name] = ss
            ss.restore_from_snapshot(snap)

    def clear_all_persisted(self, stream_names: list[str] | None = None):
        """
        Delete rolling summaries (and history) from disk for the given streams
        (or all currently loaded streams if None).
        """
        targets = stream_names if stream_names is not None else list(self._streams.keys())
        for name in targets:
            ss = self._streams.get(name)
            if ss is None:
                ss = StreamSummarizer(self.conv_id, name, "")
                self._streams[name] = ss
            ss.clear_persisted()

    def get_current_summaries(self) -> dict:
        """Return {stream_name: summary_text} for all currently loaded streams."""
        return {
            name: (ss.current_summary or "")
            for name, ss in self._streams.items()
        }

    def build_context(
        self,
        messages: list[dict],
        char_limit: int = 15000,
        refresh_chars: int = 5000,
        provider: str = "openrouter",
        model: str = "",
        summary_routes: list[dict] | None = None,
        stream_configs: list[dict] = None,
        temperature: float = 0.3,
        librarian_active: bool = False,
        fast_path_enabled: bool = True,
        status_callback=None,
    ) -> tuple[list[dict], int]:
        """Build context with per-stream summaries. Returns (context_messages, cutoff_index).

        stream_configs: list of {"name": str, "summary_guidance": str} for this conversation's
        subscribed streams. If None, falls back to a single "General" stream.
        """
        if stream_configs is None:
            stream_configs = [{"name": "General", "summary_guidance": ""}]

        self._ensure_streams(stream_configs)

        # Heal stale summary indices. `summary_end_index` is a positional index
        # into `self.context` captured when the summary was built. On reload,
        # `self.context` is rebuilt without the tool/tool_call messages that
        # were present during the live session, so len(messages) shrinks while
        # the persisted index stays put. Left alone, `messages[effective_cutoff:]`
        # below silently returns an empty list — the LLM sees the summary with
        # no conversation tail. Clamp to keep a recent tail visible and persist.
        healed_any = False
        _min_keep = 6
        for _ss in self._streams.values():
            if _ss.summary_end_index > len(messages):
                _ss.summary_end_index = max(0, len(messages) - _min_keep)
                healed_any = True
        if healed_any:
            print(f"[Summarizer] Healed stale summary_end_index after context "
                  f"length mismatch (len={len(messages)}). Recent tail restored.")
            for _ss in self._streams.values():
                _ss.save_state()

        if not messages:
            return messages.copy(), 0

        total_chars = _count_chars(messages)
        if total_chars < char_limit:
            # Update tracking even when no summary needed
            for ss in self._streams.values():
                ss._last_total_chars = total_chars
            return messages.copy(), 0

        # Find the cutoff point (shared across all streams — same conversation)
        target = int(char_limit * 0.6)
        min_keep = 6
        running = 0
        cutoff = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            c = _count_chars([messages[i]])
            if running + c > target:
                cutoff = i + 1
                break
            running += c
        cutoff = min(cutoff, max(0, len(messages) - min_keep))
        if cutoff <= 2:
            cutoff = 0

        if summary_routes is None:
            summary_routes = []
            if model:
                summary_routes.append({"provider": provider, "model": model})

        # Update each stream's summary independently. status_callback(True) fires
        # once, the moment an actual (potentially slow) LLM summarization begins,
        # so the UI can show "summarizing…" instead of "typing…"; the finally
        # always clears it.
        _summarizing_signaled = False
        try:
            for ss in self._streams.values():
                new_chars = total_chars - ss._last_total_chars
                ss.chars_since_last_summary += max(0, new_chars)
                ss._last_total_chars = total_chars

                needs_update = (ss.current_summary is None or
                                ss.chars_since_last_summary >= refresh_chars)

                # ── Free-compaction fast path ──────────────────────────────
                # Skip the merge LLM call when the librarian is actively capturing
                # recent activity (its workspace_notes / memory_notes are already
                # injected into context, so the gap is covered) AND we haven't
                # fallen too far behind. Each skip saves one summarizer LLM call;
                # the live tail keeps growing until either the librarian goes
                # quiet, the gap exceeds 2× refresh_chars, or hard char_limit
                # forces a re-cut on a future turn.
                if (needs_update
                    and fast_path_enabled
                    and librarian_active
                    and ss.current_summary is not None
                    and ss.summary_end_index > 0
                    and ss.chars_since_last_summary < refresh_chars * 2):
                    # Fast path taken — preserve summary state, skip the merge.
                    # Do NOT reset chars_since_last_summary so the next call
                    # re-evaluates the threshold.
                    needs_update = False

                if needs_update and summary_routes and cutoff > 0:
                    # First real LLM summarization this turn — tell the UI.
                    if status_callback is not None and not _summarizing_signaled:
                        _summarizing_signaled = True
                        try:
                            status_callback(True)
                        except Exception:
                            pass
                    if ss.current_summary and ss.summary_end_index > 0:
                        new_msgs = messages[ss.summary_end_index:cutoff]
                        if new_msgs:
                            updated = ss._merge(
                                ss.current_summary, new_msgs, summary_routes, temperature)
                            if updated is not None:
                                ss.current_summary = updated
                                ss.summary_end_index = cutoff
                                ss.chars_since_last_summary = 0
                                ss.save_state()
                    else:
                        generated = ss._generate(
                            messages[:cutoff], summary_routes, temperature)
                        if generated:
                            ss.current_summary = generated
                            ss.summary_end_index = cutoff
                            ss.chars_since_last_summary = 0
                            ss.save_state()
        finally:
            if _summarizing_signaled and status_callback is not None:
                try:
                    status_callback(False)
                except Exception:
                    pass

        # Build combined context with all stream summaries
        context = []
        effective_cutoff = max((ss.summary_end_index for ss in self._streams.values()), default=0)

        summaries_text = []
        for name, ss in self._streams.items():
            if ss.current_summary and ss.summary_end_index > 0:
                if len(self._streams) > 1:
                    summaries_text.append(f"[Stream: {name}]\n{ss.current_summary}")
                else:
                    summaries_text.append(ss.current_summary)

        if summaries_text:
            combined = "\n\n---\n\n".join(summaries_text)
            heading = "LOW-LEVEL RECENT ACTIVITY (compressed older turns)"
            context.append({
                "role": "system",
                "content": (
                    f"━━━ {heading} ━━━\n"
                    "Per-stream compression of this conversation's older turns. "
                    "Live log follows.\n\n"
                    f"{combined}"
                ),
            })

        context.extend(messages[effective_cutoff:])
        return context, effective_cutoff
