"""
In-chat gadget cards — Plan and Sub-agent/Explore timeline blocks rendered as
pure inline HTML, matching the diff-card look: an accent titlebar over a
bordered, contained body docked to the transcript.

These render inside the message bubble's QLabel (RichText). Qt's QLabel
rich-text engine honors borders on TABLE cells but silently drops border /
border-radius on <div> — so the card frame is built from a 1-column table
(header row + body row). Div backgrounds and text colors render fine, so the
per-step rows stay as divs inside the body cell.

Like diff cards, these are UI-only: the HTML lives on the message's
``_stream_timeline`` and never enters LLM context (the transcript path only
serializes ``type == "text"`` items).
"""

from __future__ import annotations

import html as _html

from ui.diff_card import _blend
from ui.theme import PALETTE

_SIDE_PAD = 8

# Steps/tasks shown before the "+N more" line kicks in.
_ROW_CAP = 24

# status → (glyph, palette key for the glyph)
_PLAN_STATUS = {
    "pending":     ("•", "muted_text"),    # •
    "in_progress": ("▶", "accent"),        # ▶
    "done":        ("✓", "accent_bright"), # ✓
    "skipped":     ("—", "muted_text"),    # —
    "blocked":     ("✖", "danger"),        # ✖
}

_TASK_STATUS = {
    "pending":   ("·", "muted_text"),      # ·
    "blocked":   ("·", "muted_text"),
    "running":   ("▶", "accent"),          # ▶
    "completed": ("✓", "accent_bright"),   # ✓
    "failed":    ("✖", "danger"),          # ✖
}


def _card(titlebar: str, body: str, *, width_pct: int = 96) -> str:
    """Bordered card: header cell over a body cell, in a centered 1-col table.

    Tables are used (not div+border) because the chat bubble is a QLabel whose
    rich-text engine only paints borders on table cells. cellspacing=0 +
    border-top:none on the body cell collapses the shared edge to a single
    divider line between the titlebar and the body.
    """
    p = PALETTE
    bg = p.get("panel_alt", "#101010")
    panel = p.get("panel", "#0c0c0c")
    border = p.get("border", "#333333")
    return (
        f'<table align="center" width="{width_pct}%" cellspacing="0" '
        f'cellpadding="0" style="margin:8px 0;">'
        f'<tr><td style="background:{panel};border:1px solid {border};'
        f'padding:4px 8px;">{titlebar}</td></tr>'
        f'<tr><td style="background:{bg};border:1px solid {border};'
        f'border-top:none;padding:4px 0;">{body}</td></tr>'
        f'</table>'
    )


def _titlebar(icon: str, title: str, sub: str, fs: int) -> str:
    """Centered accent title with an optional muted sub-label."""
    p = PALETTE
    line_fs = max(fs - 1, 7)
    sub_html = ""
    if sub:
        sub_html = (
            f'&nbsp;&nbsp;<span style="color:{p.get("muted_text", "#888")};'
            f'font-size:{max(fs - 3, 6)}pt;">{sub}</span>'
        )
    return (
        f'<div align="center" style="font-family:Consolas;">'
        f'<span style="color:{p.get("accent", "#33ff99")};font-weight:bold;'
        f'font-size:{line_fs}pt;">{icon} {_html.escape(title)}</span>'
        f'{sub_html}'
        f'</div>'
    )


def _more_row(remaining: int, noun: str, fs: int, *, center: bool = False) -> str:
    p = PALETTE
    align = ' align="center"' if center else ""
    return (
        f'<div{align} style="color:{p.get("muted_text", "#888")};'
        f'font-family:Consolas;font-size:{max(fs - 2, 6)}pt;padding:0 8px;'
        f'opacity:0.7;">+{remaining} more {noun}</div>'
    )


def build_plan_card(plan_data: dict, *, fs: int = 9, live: bool = False) -> str:
    """Plan timeline card: titlebar (title + progress) over status-iconed steps.

    Steps are left-aligned so the plan reads as a list you can actually scan.
    """
    p = PALETTE
    bg = p.get("panel_alt", "#101010")
    text_c = p.get("text", "#dddddd")
    muted = p.get("muted_text", "#888888")
    line_fs = max(fs - 1, 7)

    plan_data = plan_data or {}
    title = plan_data.get("title") or plan_data.get("goal") or "Plan"
    steps = plan_data.get("steps") or []

    rows: list[str] = []
    done = 0
    for step in steps[:_ROW_CAP]:
        if isinstance(step, str):
            label, status = step, "pending"
        else:
            label = step.get("label") or step.get("description") or ""
            status = step.get("status", "pending")
        glyph, glyph_key = _PLAN_STATUS.get(status, _PLAN_STATUS["pending"])
        glyph_c = p.get(glyph_key, muted)
        row_bg = "transparent"
        label_html = _html.escape(label)
        if status == "done":
            done += 1
            label_c = muted
        elif status == "in_progress":
            label_c = text_c
            row_bg = _blend(p.get("accent", "#33ff99"), bg, 0.10)
            label_html = f"<b>{label_html}</b>"
        elif status == "skipped":
            label_c = muted
            label_html = f"<s>{label_html}</s>"
        elif status == "blocked":
            label_c = p.get("danger", "#ff5555")
        else:
            label_c = text_c
        rows.append(
            f'<div style="background:{row_bg};font-family:Consolas;'
            f'font-size:{line_fs}pt;padding:1px 10px;">'
            f'<span style="color:{glyph_c};">{glyph}</span>'
            f'<span style="color:{label_c};">&nbsp;&nbsp;{label_html}</span>'
            f'</div>'
        )
    if len(steps) > _ROW_CAP:
        rows.append(_more_row(len(steps) - _ROW_CAP, "steps", fs))
    if not rows:
        rows.append(
            f'<div style="color:{muted};font-family:Consolas;'
            f'font-size:{line_fs}pt;padding:1px 10px;">Drafting plan…</div>'
        )

    n = len(steps)
    sub = f"{done}/{n} done" if n else ""
    if live and n and done < n:
        sub += " · live"
    return _card(_titlebar("◆", title, sub, fs), "".join(rows))


def build_subagent_card(tasks: list, *, summary: dict | None = None,
                        live: bool = False, fs: int = 9) -> str:
    """Sub-agent / Explore timeline card: titled like the job (Explore jobs get
    their own name), one status-glyphed row per task, summary footer when done.

    Rows are centered to match the tool-widget standard.
    """
    p = PALETTE
    muted = p.get("muted_text", "#888888")
    text_c = p.get("text", "#dddddd")
    line_fs = max(fs - 1, 7)

    tasks = tasks or []
    summary = summary or {}
    explore = bool(tasks) and all(t.get("mode") == "explore" for t in tasks)
    title = "Explore" if explore else "Sub-agents"
    noun = "workers" if explore else "tasks"

    rows: list[str] = []
    completed = failed = 0
    for t in tasks[:_ROW_CAP]:
        status = t.get("status") or "pending"
        glyph, glyph_key = _TASK_STATUS.get(status, _TASK_STATUS["pending"])
        glyph_c = p.get(glyph_key, muted)
        label = (t.get("title") or t.get("name")
                 or (t.get("task_id") or "task")[-8:])
        label = _html.escape(label[:48])

        if status == "running":
            rnd = t.get("round")
            tool = t.get("current_tool", "")
            if tool:
                detail = f"r{rnd}/{t.get('max_rounds', 15)} {tool}"
            elif rnd:
                detail = f"round {rnd}/{t.get('max_rounds', 15)}…"
            else:
                detail = "working…"
            detail_c = p.get("accent", "#33ff99")
        elif status == "completed":
            completed += 1
            detail, detail_c = "done", p.get("accent", "#33ff99")
        elif status == "failed":
            failed += 1
            err = (t.get("error") or t.get("full_error") or "failed")
            detail = err.split("\n")[0][:48]
            detail_c = p.get("danger", "#ff5555")
        else:
            detail, detail_c = status, muted
        rows.append(
            f'<div align="center" style="font-family:Consolas;'
            f'font-size:{line_fs}pt;padding:1px 8px;">'
            f'<span style="color:{glyph_c};">{glyph}</span>'
            f'<span style="color:{text_c};">&nbsp;&nbsp;{label}</span>'
            f'<span style="color:{detail_c};">&nbsp;—&nbsp;'
            f'{_html.escape(detail)}</span>'
            f'</div>'
        )
    if len(tasks) > _ROW_CAP:
        rows.append(_more_row(len(tasks) - _ROW_CAP, noun, fs, center=True))
    if not rows:
        rows.append(
            f'<div align="center" style="color:{muted};font-family:Consolas;'
            f'font-size:{line_fs}pt;padding:1px 8px;">Dispatching…</div>'
        )

    if summary:
        parts = [f'{summary.get("completed", completed)} completed']
        n_failed = summary.get("failed", failed)
        if n_failed:
            parts.append(
                f'<span style="color:{p.get("danger", "#ff5555")};">'
                f'{n_failed} failed</span>')
        rounds = summary.get("rounds")
        if rounds and not explore:
            parts.append(f"{rounds} rounds")
        rows.append(
            f'<div align="center" style="color:{muted};font-family:Consolas;'
            f'font-size:{max(fs - 2, 6)}pt;padding:3px 8px 0 8px;">'
            f'{" · ".join(parts)}</div>'
        )

    n = len(tasks)
    if live:
        sub = f"{n} {noun} · {completed}/{n} done" if n else "dispatching…"
    else:
        sub = (f"{n} {noun} · all done" if n and completed == n
               else f"{n} {noun} · {completed}/{n} done")
    return _card(_titlebar("◈", title, sub, fs), "".join(rows), width_pct=72)
