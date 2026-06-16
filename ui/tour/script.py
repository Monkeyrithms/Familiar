"""The tour script — every line hardcoded, zero LLM calls.

Each TourStep is one beat of the show: stage directions (``actions``,
executed by the director against the real window) plus narration (``say``).

Narration routing: a step with an ``anchor`` speaks through the TourBubble
popup pinned next to that feature — the glow and the words pull the eye to
the same spot. ``anchor=None`` speaks through the agent chat itself (only the
opening beats, while the chat is all that exists).

The tone is professional throughout. The opening keeps the scale of the app
unstated — the user sees only a chat window until the reveal — but it never
winks at the audience about it.

The tour is user-paced: once a step's typing finishes and its async actions
(window growth, panel reveals) complete, a CONTINUE button appears — click it
or press Enter to advance. Steps with explicit ``choices`` show those buttons
instead (the intro gate, the finale's "GET STARTED").

Action verbs are dispatched by ``TourDirector._do_action``:
    window_grow            — glide the window from chat-size to full size
    reveal <feature>       — origami-unfold a named widget / panel
    spotlight <feature>    — edge-glow a widget (veil dims the rest)
    spotlight_items <prov> — glow several widgets at once, each in accent
    spotlight_clear        — drop the spotlight
    ws_tab <key>           — switch the workspace panel to a named tab
    blink <feature>        — quick attention flash on top of the spotlight
    agent_extras           — re-show any chat chrome hidden during genesis
    seed_chat              — sweep intro debris, leave an "ask me" message
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TourStep:
    id: str
    say: str
    actions: list = field(default_factory=list)
    anchor: str | None = None     # feature name → popup; None → agent chat
    choices: list = field(default_factory=list)   # [(label, command), ...]
    type_delay_ms: int = 500      # let the animation land before talking
    auto_advance_ms: int = 0      # >0 → no CONTINUE button; advance on a timer


STEPS: list[TourStep] = [

    # ── opening: a quiet introduction; the scale of the app stays unsaid ──
    TourStep(
        id="intro",
        say=(
            "Hello — I'm **Familiar**, your desktop AI agent.\n\n"
            "Would you like a quick guided tour? It takes a couple of minutes, "
            "and I'll introduce each part of the workspace as it appears. If "
            "you already know your way around, skip ahead and I'll open "
            "everything up."
        ),
        choices=[
            ("▶  START THE TOUR", "begin"),
            ("I KNOW WHAT I'M DOING  +", "skipall"),
        ],
        type_delay_ms=250,
    ),

    # No button on this beat — a short breath, then the app unfolds on its own.
    TourStep(
        id="unfold",
        say="Let me show you around:",
        type_delay_ms=200,
        auto_advance_ms=2000,
    ),

    # ── the reveal: window expands, title bar erupts ───────────────────
    TourStep(
        id="titlebar",
        say=(
            "**This is Familiar.**\n\n"
            "Up here is the **title bar** — your way into everything the app "
            "can do: setup and settings, scheduled tasks, long-term memory, and "
            "this very tour. We'll get to each one."
        ),
        actions=[("window_grow",), ("spotlight", "titlebar")],
        anchor="titlebar",
        type_delay_ms=1400,
    ),

    # ── the tabbed workspace slides open ───────────────────────────────
    TourStep(
        id="workspace",
        say=(
            "And on the side, your **workspace** — a tabbed panel I share with "
            "you. **Notes**, **Calendar**, an embedded **Browser**, a **File** "
            "viewer/editor, and a real **Terminal**. It starts tucked away; "
            "let me open it and walk each tab."
        ),
        actions=[("reveal", "workspace"), ("spotlight", "workspace")],
        anchor="workspace",
        type_delay_ms=900,
    ),

    TourStep(
        id="ws_notes",
        say=(
            "**Notes** — a scratchpad that persists with your work. Jot ideas, "
            "paste snippets, keep a running log. I can read and write here too, "
            "so it doubles as a shared canvas between us."
        ),
        actions=[("ws_tab", "notes"), ("spotlight", "ws_notes")],
        anchor="ws_notes",
    ),

    TourStep(
        id="ws_calendar",
        say=(
            "**Calendar** — a lightweight view of your schedule and anything "
            "time-bound. It pairs naturally with **Tasks** (in the title bar) "
            "when you want me to act on a date or a cadence."
        ),
        actions=[("ws_tab", "calendar"), ("spotlight", "ws_calendar")],
        anchor="ws_calendar",
    ),

    TourStep(
        id="ws_browser",
        say=(
            "**Browser** — a full embedded Chromium with its own persistent "
            "profile, so logins and cookies stick. You can browse here, and I "
            "can read the page you're on to ground what I do for you."
        ),
        actions=[("ws_tab", "browser"), ("spotlight", "ws_browser")],
        anchor="ws_browser",
    ),

    TourStep(
        id="ws_files",
        say=(
            "**File** — a viewer and editor over your active workspace folder. "
            "Browse the tree, open files, watch edits land live as I work. "
            "Every file tool I run is scoped to the workspace you choose."
        ),
        actions=[("ws_tab", "files"), ("spotlight", "ws_files")],
        anchor="ws_files",
    ),

    TourStep(
        id="ws_terminal",
        say=(
            "**Terminal** — a real PTY (ConPTY on Windows), not a fake echo. "
            "Full-screen TUIs work, and its working directory follows your "
            "active workspace. I can run commands here and read what comes back."
        ),
        actions=[("ws_tab", "terminal"), ("spotlight", "ws_terminal")],
        anchor="ws_terminal",
    ),

    # ── title-bar tools, one at a time ─────────────────────────────────
    TourStep(
        id="settings",
        say=(
            "Back to the title bar. **Settings** is mission control: LLM "
            "**providers and API keys** (stored in one place — "
            "`data/keys.json`), your **model** choices, **workspaces**, tools, "
            "and the theme you're looking at. Key changes take effect on my "
            "next reply — no restart."
        ),
        actions=[("spotlight", "settings")],
        anchor="settings",
    ),

    TourStep(
        id="tasks",
        say=(
            "**Tasks** — cron-style scheduled prompts. One-shot reminders or "
            "recurring chores: a morning digest, a nightly summary, a nudge at "
            "3pm. Each task can target a conversation and chime when it runs."
        ),
        actions=[("spotlight", "tasks")],
        anchor="tasks",
    ),

    TourStep(
        id="memory",
        say=(
            "**Memory** — my long-term notes across chats, organized into "
            "**streams** (projects, personas, topics). A background librarian "
            "recalls what's relevant before each turn, so I keep context "
            "without you re-explaining it every time."
        ),
        actions=[("spotlight", "memory")],
        anchor="memory",
    ),

    TourStep(
        id="titlebar_extras",
        say=(
            "Two handy ones on the right: **↑** pins the window **always on "
            "top**, and **▣** grabs a **screenshot to your clipboard** — quick "
            "to paste a view back to me. Drag the bar to move, drag edges to "
            "resize, double-click to maximize."
        ),
        actions=[("spotlight_items", "titlebar_extras")],
        anchor="on_top",
    ),

    # ── the composer + conversation switcher ───────────────────────────
    TourStep(
        id="composer",
        say=(
            "This is where you talk to me — the **composer**. Type a request, "
            "**+** attaches files and images, **←** undoes a turn, and **✕** "
            "stops a response in progress. Press Enter to send."
        ),
        actions=[("spotlight", "composer")],
        anchor="composer",
    ),

    TourStep(
        id="conversations",
        say=(
            "And the **conversation bar** keeps separate threads — switch "
            "between them, and open **Conversation** for per-chat settings: a "
            "different model, workspace, system prompt, or memory streams, "
            "scoped to just this chat."
        ),
        actions=[("spotlight", "conv_bar")],
        anchor="conv_bar",
    ),

    # ── what the agent can actually do ─────────────────────────────────
    TourStep(
        id="agent_powers",
        say=(
            "What does that add up to? With a key configured, ask me for "
            "*\"read this repo and fix the failing test\"*, *\"summarize these "
            "PDFs into notes\"*, or *\"watch this site and ping me when it "
            "changes\"* — and I'll use the files, terminal, browser, web "
            "search, and memory you've just seen to actually do it."
        ),
        actions=[("spotlight", "chat")],
        anchor="chat",
    ),

    # ── replay + finale ────────────────────────────────────────────────
    TourStep(
        id="help",
        say=(
            "You can replay this walkthrough anytime: the **?** button opens "
            "Help, with a **Take Tour** button to run it again from the top. "
            "It's all hardcoded — replaying costs you nothing."
        ),
        actions=[("spotlight", "help")],
        anchor="help",
    ),

    TourStep(
        id="finale",
        say=(
            "That completes the tour — and note where it began: **right "
            "here**, in the chat. This is where you'll find me. Whenever you "
            "need something — a file edited, a task scheduled, a question "
            "answered — just send a message.\n\n"
            "Welcome to **Familiar**."
        ),
        actions=[("agent_extras",), ("spotlight", "chat"),
                 ("blink", "composer"), ("seed_chat",)],
        anchor="chat",
        choices=[("GET STARTED  ▶", "finish")],
    ),
]


# Seeded into the chat at the finale — the intro/tour debris is swept away and
# this clean "here's what to ask" message is left waiting. Rendered as markdown.
CHAT_SEED_SAY = (
    "Here whenever you need me. A few things you can try:\n\n"
    "- *\"Read my project in this workspace and explain how it's structured.\"*\n"
    "- *\"Run the test suite in the terminal and fix whatever fails.\"*\n"
    "- *\"Summarize these notes and add a to-do list at the bottom.\"*\n"
    "- *\"Remind me every weekday at 9am to review open tasks.\"*\n\n"
    "Or just tell me what you're working on — I'll take it from there."
)

# Spoken when the user skips the tour from the intro.
SKIP_SAY = (
    "Understood — opening your workspace now.\n\n"
    "The essentials: type a request below, open the **workspace** panel for "
    "files and a terminal, and find providers and settings up in the title "
    "bar. This tour is always available under **?** → Take Tour. I'm here "
    "whenever you need me."
)

# Spoken after the key gate, before the intro choice.
KEYGATE_SAVED_SAY = "Key saved — I'm fully operational."
KEYGATE_SKIPPED_SAY = (
    "Understood — everything except my chat replies works without a key, and "
    "you can add one anytime under Settings → API Keys."
)
