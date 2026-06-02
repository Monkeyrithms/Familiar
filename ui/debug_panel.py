"""
DebugPanel — shows the complete LLM context for each agent turn in one conversation.
No truncations. The full sandwich: system message(s), conversation history, tool
calls, responses — exactly what the model saw and said. Data is scoped by
conversation id and persisted in SQLite (see ``core.debug_recorder``).
"""

from __future__ import annotations

import html
import json
from typing import Any, Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.debug_recorder import debug_recorder
from ui.theme import PALETTE


def _step_display_name(name: str) -> str:
    if not name:
        return "Step"
    if name == "forced_final":
        return "Forced Final Answer (hard-stop)"
    if name.startswith("round_"):
        try:
            return f"Round {int(name.split('_', 1)[1])}"
        except ValueError:
            pass
    return name.replace("_", " ").title()


def _extract_text(content: Any) -> tuple[str, bool]:
    """Return (text, has_image) from a message content value."""
    if isinstance(content, list):
        parts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
        has_img = any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
        return "\n".join(parts), has_img
    return str(content or ""), False


class DebugPanel(QWidget):
    """Full-context debug panel for one conversation (LLM context + tool rounds)."""

    def __init__(self, conversation_id: str = "", parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._conversation_id = (conversation_id or "").strip()
        self._current_turn: Optional[Dict[str, Any]] = None
        self._turn_index: int = -1
        self._step_labels: Dict[int, QLabel] = {}
        self._step_views: Dict[int, QTextEdit] = {}
        self._main_layout: Optional[QVBoxLayout] = None
        self._init_ui()
        self._apply_styles()
        self._refresh()

    # ── UI construction ───────────────────────────────────────────────────────

    def _init_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        content = QWidget()
        self._main_layout = QVBoxLayout(content)
        self._main_layout.setContentsMargins(12, 12, 12, 12)
        self._main_layout.setSpacing(10)

        # Header
        self._header = QLabel(
            "DEBUG — Full LLM Context (this conversation)"
            if self._conversation_id
            else "DEBUG — Full LLM Context (open a conversation to scope debug)"
        )
        self._header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._header.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        self._main_layout.addWidget(self._header)

        # Controls
        ctrl = QHBoxLayout()
        ctrl.setSpacing(6)
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        self._refresh_btn.clicked.connect(self._refresh)
        ctrl.addWidget(self._refresh_btn)
        self._copy_btn = QPushButton("Copy to Clipboard")
        self._copy_btn.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        self._copy_btn.clicked.connect(self._copy)
        ctrl.addWidget(self._copy_btn)
        ctrl.addStretch()
        self._prev_btn = QPushButton("← Prev")
        self._prev_btn.setFont(QFont("Consolas", 9))
        self._prev_btn.clicked.connect(self._go_prev)
        ctrl.addWidget(self._prev_btn)
        self._turn_label = QLabel("—")
        self._turn_label.setFont(QFont("Consolas", 9))
        self._turn_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._turn_label.setMinimumWidth(60)
        ctrl.addWidget(self._turn_label)
        self._next_btn = QPushButton("Next →")
        self._next_btn.setFont(QFont("Consolas", 9))
        self._next_btn.clicked.connect(self._go_next)
        ctrl.addWidget(self._next_btn)
        self._main_layout.addLayout(ctrl)

        # Turn metadata (compact one-liner)
        self._meta_lbl = QLabel("Turn Overview")
        self._meta_lbl.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        self._main_layout.addWidget(self._meta_lbl)
        self._meta_view = self._make_view(min_h=55, max_h=70)
        self._main_layout.addWidget(self._meta_view)

        # THE SANDWICH — full context exactly as sent to model
        self._ctx_lbl = QLabel("Full Context  (the complete sandwich — system + conversation, no truncations)")
        self._ctx_lbl.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        self._main_layout.addWidget(self._ctx_lbl)
        self._ctx_view = self._make_view(min_h=300, max_h=99999)
        self._main_layout.addWidget(self._ctx_view)

        # Per-round panels inserted dynamically below
        scroll.setWidget(content)
        outer.addWidget(scroll)

    def _make_view(self, min_h: int = 120, max_h: int = 400) -> QTextEdit:
        v = QTextEdit()
        v.setReadOnly(True)
        v.setFont(QFont("Consolas", 9))
        v.setMinimumHeight(min_h)
        if max_h < 99999:
            v.setMaximumHeight(max_h)
        v.setMinimumWidth(0)
        return v

    # ── Styles ────────────────────────────────────────────────────────────────

    def _apply_styles(self) -> None:
        p = PALETTE
        accent = p.get("accent", "#4ECDC4")
        muted = p.get("muted_text", accent)
        text = p.get("text", accent)
        border = p.get("border", accent)

        view_css = (
            f"QTextEdit {{"
            f"  background-color: #0d0d0d;"
            f"  color: {text};"
            f"  border: 1px solid {border};"
            f"  selection-background-color: {accent};"
            f"}}"
        )
        btn_css = (
            f"QPushButton {{"
            f"  background-color: #1a1a1a; color: {accent};"
            f"  border: 1px solid {accent}; border-radius: 3px; padding: 3px 8px;"
            f"}}"
            f"QPushButton:hover {{ background-color: #222; }}"
            f"QPushButton:pressed {{ background-color: {accent}; color: #111; }}"
        )
        lbl_css = f"color: {accent}; font-weight: bold;"
        muted_css = f"color: {muted};"

        self._header.setStyleSheet(lbl_css)
        self._meta_lbl.setStyleSheet(lbl_css)
        self._ctx_lbl.setStyleSheet(lbl_css)
        self._turn_label.setStyleSheet(muted_css)
        for v in (self._meta_view, self._ctx_view):
            v.setStyleSheet(view_css)
        for btn in (self._refresh_btn, self._copy_btn, self._prev_btn, self._next_btn):
            btn.setStyleSheet(btn_css)
        for lbl in self._step_labels.values():
            lbl.setStyleSheet(lbl_css)
        for v in self._step_views.values():
            v.setStyleSheet(view_css)

    # ── Navigation ────────────────────────────────────────────────────────────

    def _refresh(self) -> None:
        self._turn_index = -1
        self._render(debug_recorder.latest_turn(self._conversation_id))

    def _go_prev(self) -> None:
        total = debug_recorder.turn_count(self._conversation_id)
        if not total:
            return
        if self._turn_index == -1:
            self._turn_index = total - 1
        self._turn_index = max(0, self._turn_index - 1)
        self._render(debug_recorder.get_turn(self._turn_index, self._conversation_id))

    def _go_next(self) -> None:
        total = debug_recorder.turn_count(self._conversation_id)
        if not total or self._turn_index == -1:
            return
        self._turn_index += 1
        if self._turn_index >= total:
            self._turn_index = -1
            self._render(debug_recorder.latest_turn(self._conversation_id))
        else:
            self._render(debug_recorder.get_turn(self._turn_index, self._conversation_id))

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _render(self, turn: Optional[Dict[str, Any]]) -> None:
        self._current_turn = turn

        total = debug_recorder.turn_count(self._conversation_id)
        if not total or turn is None:
            self._turn_label.setText("0/0")
        else:
            pos = total if self._turn_index == -1 else self._turn_index + 1
            self._turn_label.setText(f"{pos}/{total}")

        p = PALETTE
        accent = p.get("accent", "#4ECDC4")
        muted = p.get("muted_text", accent)
        text_col = p.get("text", accent)

        if turn is None:
            self._meta_view.setPlainText("No debug data yet — run a message and click Refresh.")
            self._ctx_view.setPlainText("")
            self._clear_steps()
            return

        # ── Metadata ──────────────────────────────────────────────────────────
        totals = turn.get("totals") or {}
        err = f"  ERROR: {turn['error']}" if turn.get("error") else ""
        self._meta_view.setPlainText(
            f"{turn.get('started_at')}  |  {turn.get('model_name')}  |  "
            f"max_tokens={turn.get('max_tokens')}  temp={turn.get('temperature')}  |  "
            f"steps={totals.get('steps',0)}  "
            f"tokens ctx/out/all={totals.get('tokens_context',0)}/"
            f"{totals.get('tokens_response',0)}/{totals.get('tokens_all',0)}"
            f"{err}"
        )

        # ── Full Context sandwich ──────────────────────────────────────────────
        # Reconstruct in order: system message(s) first, then base_context messages
        self._ctx_view.setHtml(
            self._render_full_sandwich(turn, accent, muted, text_col)
        )

        # ── Per-round step panels ──────────────────────────────────────────────
        self._clear_steps()
        for step in turn.get("steps") or []:
            self._add_step_panel(step, accent, muted, text_col)

    def _render_full_sandwich(
        self,
        turn: Dict[str, Any],
        accent: str,
        muted: str,
        text_col: str,
    ) -> str:
        """
        Render the exact messages array that was sent to the API for round 1,
        verbatim — system messages and all. Every message is its own block.
        No truncations, no grouping, no cleverness.
        """
        parts: List[str] = []
        base_ctx = turn.get("base_context") or []

        if not base_ctx:
            return f"<p style='color:#ff5555;'><b>Context is empty.</b> The model received nothing.</p>"

        for msg in base_ctx:
            role = msg.get("role", "?")
            raw_content = msg.get("content") or ""

            # Handle tool call messages (assistant with tool_calls)
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                tc_text = json.dumps(tool_calls, indent=2, ensure_ascii=False)
                body = f"[tool_calls]\n{tc_text}"
                if raw_content:
                    text, _ = _extract_text(raw_content)
                    if text.strip():
                        body = text + "\n\n" + body
                parts.append(self._msg_block(role, body, accent, muted, text_col))
                continue

            text, has_image = _extract_text(raw_content)
            if has_image:
                text += "\n[+ image attachment]"
            parts.append(self._msg_block(role, text, accent, muted, text_col))

        return "".join(parts) or f"<p style='color:{muted};'>(empty)</p>"

    def _msg_block(
        self,
        role: str,
        content: str,
        accent: str,
        muted: str,
        text_col: str,
    ) -> str:
        """Render one message as a labeled block, full content, no truncation."""
        role_color = {
            "system": "#888888",
            "user": accent,
            "assistant": text_col,
            "tool": muted,
        }.get(role, accent)

        return (
            f"<div style='margin-bottom:12px;'>"
            f"<div style='color:{role_color}; font-weight:bold; "
            f"border-bottom:1px solid {muted}; margin-bottom:4px; padding-bottom:2px;'>"
            f"[{html.escape(role.upper())}]</div>"
            f"<pre style='white-space:pre-wrap; margin:0; color:{text_col}; "
            f"background:#0c0c0c; padding:6px;'>{html.escape(content)}</pre>"
            f"</div>"
        )

    # ── Per-round step panels ─────────────────────────────────────────────────

    def _clear_steps(self) -> None:
        if not self._main_layout:
            return
        to_remove = []
        for i in range(self._main_layout.count()):
            item = self._main_layout.itemAt(i)
            if item:
                w = item.widget()
                if w and (w in self._step_labels.values() or w in self._step_views.values()):
                    to_remove.append(i)
        for i in reversed(to_remove):
            item = self._main_layout.takeAt(i)
            if item and item.widget():
                item.widget().setParent(None)
                item.widget().deleteLater()
        self._step_labels.clear()
        self._step_views.clear()

    def _add_step_panel(
        self,
        step: Dict[str, Any],
        accent: str,
        muted: str,
        text_col: str,
    ) -> None:
        if not self._main_layout:
            return
        p = PALETTE
        idx = step.get("index", len(self._step_views) + 1)
        title = _step_display_name(str(step.get("name") or ""))
        tok_in = step.get("tokens_context", 0)
        tok_out = step.get("tokens_response", 0)

        lbl = QLabel(f"{title}  (~{tok_in} ctx / ~{tok_out} out tokens)")
        lbl.setFont(QFont("Consolas", 9, QFont.Weight.Bold))
        lbl.setStyleSheet(f"color: {accent}; font-weight: bold;")
        self._main_layout.addWidget(lbl)
        self._step_labels[idx] = lbl

        view = self._make_view(min_h=80, max_h=99999)
        view.setStyleSheet(
            f"QTextEdit {{ background-color:#0d0d0d; color:{p.get('text',accent)};"
            f" border:1px solid {p.get('border',accent)}; }}"
        )
        view.setHtml(self._format_step(step, accent, muted, text_col))
        self._main_layout.addWidget(view)
        self._step_views[idx] = view

    def _format_step(
        self,
        step: Dict[str, Any],
        accent: str,
        muted: str,
        text_col: str,
    ) -> str:
        parts: List[str] = []
        meta = step.get("meta") or {}

        # Tool calls the model requested
        tool_calls = meta.get("tool_calls") or []
        if tool_calls:
            try:
                tc_str = json.dumps(tool_calls, indent=2, ensure_ascii=False)
            except Exception:
                tc_str = str(tool_calls)
            parts.append(f"<p style='color:{accent}; margin:0 0 4px 0;'><b>Tool Calls</b></p>")
            parts.append(
                f"<pre style='white-space:pre-wrap; background:#0c0c0c; padding:6px;"
                f" border:1px solid {muted}; color:{muted}; margin:0 0 8px 0;'>"
                f"{html.escape(tc_str)}</pre>"
            )

        # Extended thinking (no truncation)
        thinking = meta.get("thinking") or ""
        if thinking:
            parts.append(f"<p style='color:{accent}; margin:4px 0 4px 0;'><b>Extended Thinking</b></p>")
            parts.append(
                f"<pre style='white-space:pre-wrap; background:#0c0c0c; padding:6px;"
                f" border:1px solid {muted}; color:{muted}; margin:0 0 8px 0;'>"
                f"{html.escape(thinking)}</pre>"
            )

        if meta.get("forced"):
            parts.append(f"<p style='color:#ffaa00;'>⚠ Forced final answer (hard-stop)</p>")

        # Context delta (what changed from previous round — tool results etc.)
        context = step.get("context") or []
        if context:
            parts.append(
                f"<p style='color:{accent}; margin:4px 0 4px 0;'><b>Context sent this round</b> "
                f"<span style='color:{muted}; font-weight:normal;'>({len(context)} messages)</span></p>"
            )
            for i, msg in enumerate(context):
                role = msg.get("role", "?")
                raw = msg.get("content") or ""
                tool_calls_msg = msg.get("tool_calls")
                if tool_calls_msg:
                    try:
                        body = json.dumps(tool_calls_msg, indent=2, ensure_ascii=False)
                    except Exception:
                        body = str(tool_calls_msg)
                    text = f"[tool_calls]\n{body}"
                    if raw:
                        t, _ = _extract_text(raw)
                        if t.strip():
                            text = t + "\n\n" + text
                else:
                    text, has_img = _extract_text(raw)
                    if has_img:
                        text += "\n[+ image]"
                role_color = {"system": "#888", "user": accent, "assistant": text_col, "tool": muted}.get(role, accent)
                parts.append(
                    f"<div style='margin-bottom:6px;'>"
                    f"<span style='color:{role_color}; font-weight:bold;'>[{html.escape(role.upper())}]</span>"
                    f"<pre style='white-space:pre-wrap; margin:2px 0 0 0; background:#0c0c0c;"
                    f" padding:4px; color:{text_col};'>{html.escape(text)}</pre></div>"
                )

        # Response (full, no truncation)
        response = step.get("response") or ""
        if response:
            parts.append(f"<p style='color:{accent}; margin:4px 0 4px 0;'><b>Response</b></p>")
            parts.append(
                f"<pre style='white-space:pre-wrap; background:#0c0c0c; padding:6px;"
                f" border:1px solid {muted}; color:{text_col}; margin:0;'>"
                f"{html.escape(response)}</pre>"
            )

        return "".join(parts) if parts else f"<p style='color:{muted};'>(no data)</p>"

    # ── Clipboard export ──────────────────────────────────────────────────────

    def _build_clipboard_text(self) -> str:
        turn = self._current_turn
        if not turn:
            return "No debug data. Run a message then click Refresh."

        lines: List[str] = []
        totals = turn.get("totals") or {}
        lines.append("=== DEBUG TURN ===")
        lines.append(f"ID      : {turn.get('id')}")
        lines.append(f"Started : {turn.get('started_at')}")
        lines.append(f"Model   : {turn.get('model_name')}")
        lines.append(f"Params  : max_tokens={turn.get('max_tokens')}  temp={turn.get('temperature')}")
        lines.append(
            f"Totals  : steps={totals.get('steps',0)}  "
            f"tokens ctx/out/all={totals.get('tokens_context',0)}/"
            f"{totals.get('tokens_response',0)}/{totals.get('tokens_all',0)}"
        )
        if turn.get("error"):
            lines.append(f"Error   : {turn['error']}")
        lines.append("")

        # Full sandwich — every message in base_context, no truncations
        for msg in turn.get("base_context") or []:
            role = msg.get("role", "?").upper()
            raw = msg.get("content") or ""
            tc = msg.get("tool_calls")
            if tc:
                try:
                    body = json.dumps(tc, indent=2, ensure_ascii=False)
                except Exception:
                    body = str(tc)
                text = f"[tool_calls]\n{body}"
                if raw:
                    t, _ = _extract_text(raw)
                    if t.strip():
                        text = t + "\n\n" + text
            else:
                text, has_img = _extract_text(raw)
                if has_img:
                    text += "\n[+ image attachment]"
            lines.append(f"━━━ [{role}] ━━━")
            lines.append(text)
            lines.append("")

        # Per-round steps
        for step in turn.get("steps") or []:
            title = _step_display_name(str(step.get("name") or ""))
            lines.append(f"══════════════════════════════")
            lines.append(f"  {title.upper()}")
            lines.append(f"  tokens ctx={step.get('tokens_context',0)}  out={step.get('tokens_response',0)}")
            lines.append(f"══════════════════════════════")
            meta = step.get("meta") or {}
            if meta.get("tool_calls"):
                lines.append("Tool calls:")
                try:
                    lines.append(json.dumps(meta["tool_calls"], indent=2, ensure_ascii=False))
                except Exception:
                    lines.append(str(meta["tool_calls"]))
                lines.append("")
            if meta.get("thinking"):
                lines.append("Extended thinking:")
                lines.append(str(meta["thinking"]))
                lines.append("")
            for msg in step.get("context") or []:
                role = msg.get("role", "?").upper()
                raw = msg.get("content") or ""
                tc = msg.get("tool_calls")
                if tc:
                    try:
                        text = json.dumps(tc, indent=2, ensure_ascii=False)
                    except Exception:
                        text = str(tc)
                else:
                    text, _ = _extract_text(raw)
                lines.append(f"  [{role}]: {text}")
            if step.get("response"):
                lines.append("Response:")
                lines.append(step["response"])
            lines.append("")

        lines.append("=== END ===")
        return "\n".join(lines)

    def _copy(self) -> None:
        cb = QApplication.clipboard()
        if cb:
            cb.setText(self._build_clipboard_text())
