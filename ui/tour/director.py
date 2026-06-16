"""TourDirector — runs the hardcoded first-run tour against the live window.

Lifecycle:
    needed()            → should this launch boot into genesis mode?
    prepare_genesis()   → fold the UI down to a bare chat card (before show)
    begin()             → key gate if needed, then the scripted tour

The director owns pacing (typewriter → CONTINUE button → next step; the tour
never advances on its own), executes stage directions, and handles every
input path: popup buttons, inline chat buttons, typed commands, and Enter
(app-wide, = CONTINUE). It is deliberately paranoid — any failing action is
logged and skipped, and exiting the tour always lands the user in a fully
revealed, working app.

Ported from Brikwerx 3, retargeted to Familiar's chat-centric layout: the
"app" that unfolds is the title bar + the tabbed workspace panel, and the
genesis card is the chat itself.
"""
from __future__ import annotations

from PyQt6.QtCore import (
    QEvent, QObject, QRect, QRectF, Qt, QThread, QTimer, pyqtSignal,
)
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import QApplication, QLineEdit, QTextEdit

from core.agent import load_config, save_config

from .colors import T
from .keygate import KeyGateCard, has_llm_key
from .popup import TourBubble
from .reveal import animate_geometry, animate_splitter, grow_in, stagger
from .script import (
    CHAT_SEED_SAY, KEYGATE_SAVED_SAY, KEYGATE_SKIPPED_SAY, SKIP_SAY, STEPS,
)
from .spotlight import SpotlightOverlay
from .typist import Typist

# Set by main.py's --tour flag to force the tour regardless of config.
FORCE_TOUR = False

# Genesis boot: a compact chat card centered on screen (title bar hidden until
# the window grows). Full app size after the tour unfolds.
_GENESIS_W, _GENESIS_H = 460, 480
_FULL_W, _FULL_H = 1320, 820
# The workspace panel's reveal fraction of the chat width.
_WS_FRACTION = 0.42

# Free-text at the intro gate gets a *real* one-shot LLM reply when a key is
# present — the only LLM call in the otherwise-hardcoded tour.
_INTRO_CLASSIFY_PROMPT = (
    "You are the Familiar onboarding assistant. The user is on the very first "
    "screen: a welcome message offering a short guided tour of the app, or to "
    "skip it and open the workspace directly. They just typed a free-form "
    "reply instead of clicking a button.\n\n"
    "Decide what they want and respond with ONE JSON object and nothing else — "
    "no prose, no markdown fences:\n"
    '{"action": "begin" | "skip" | "reply", "say": "<text>"}\n\n'
    '- "begin": they want the guided tour (yes, sure, show me, ok, go on…). '
    'Leave "say" empty or a short one-liner.\n'
    '- "skip": they want to skip it / already know the app / just get to work. '
    'Leave "say" empty.\n'
    '- "reply": they asked a question or said something that is not a clear '
    'yes/no. In "say", answer them warmly and briefly (1-3 sentences), then '
    "invite them to either start the tour or jump straight in.\n\n"
    "Stay concise and friendly. Never output anything outside the JSON object."
)


def _parse_intro_reply(raw: str) -> dict:
    """Pull the {action, say} object out of a model reply. Returns {} on any
    failure so the caller can fall back to keyword routing."""
    import json
    import re
    if not raw:
        return {}
    m = re.search(r"\{.*\}", raw.strip(), re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return {}
    action = str(obj.get("action", "")).strip().lower()
    if action not in ("begin", "skip", "reply"):
        return {}
    return {"action": action, "say": str(obj.get("say", "") or "").strip()}


class _IntroClassifier(QThread):
    """One-shot, off-the-UI-thread classification of the user's intro reply.

    Bypasses the full agent loop (no tools, memory, or system prompt) — a tiny,
    cheap call against the configured provider/model."""
    done = pyqtSignal(dict)

    def __init__(self, provider: str, model: str, text: str) -> None:
        super().__init__()
        self._provider = provider
        self._model = model
        self._text = text

    def run(self) -> None:
        try:
            from core.providers import get_client
            client = get_client(self._provider)
            resp = client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": _INTRO_CLASSIFY_PROMPT},
                    {"role": "user", "content": self._text},
                ],
                max_tokens=320,
                temperature=0.4,
            )
            raw = (resp.choices[0].message.content or "").strip()
            self.done.emit(_parse_intro_reply(raw))
        except Exception as e:
            print(f"[tour] intro classify failed: {e}")
            self.done.emit({})


class TourDirector(QObject):
    def __init__(self, win) -> None:
        super().__init__(win)
        self.win = win
        self.aw = getattr(win, "chat", None)        # the ChatWindow
        self.active = False
        self._begun = False
        self._idx = -1
        self._paused = False
        self._waiting_choice = False
        self._offered: list = []
        self._pending_async = 0
        self._typed_done = False
        self._filter_installed = False
        self._keygate: KeyGateCard | None = None
        self._intro_clf: _IntroClassifier | None = None   # kept alive while running

        self.spot = SpotlightOverlay(win)
        self.bubble: TourBubble | None = None       # lazy — first anchored step
        self.typist = Typist(self.aw)
        self.typist.finished.connect(self._on_typed)

    # ── launch decision ───────────────────────────────────────────────
    @staticmethod
    def needed() -> bool:
        if FORCE_TOUR:
            return True
        ts = load_config().get("tour_state")
        if not isinstance(ts, dict):
            return True
        return not (ts.get("completed") or ts.get("skipped"))

    # ── feature registry ──────────────────────────────────────────────
    def _features(self) -> dict:
        win, tb, cw = self.win, self.win.title_bar, self.aw
        ws = lambda: getattr(cw, "_right_workspace", None)
        f = {
            "titlebar":   lambda: tb,
            "workspace":  ws,
            "ws_notes":    lambda: getattr(ws(), "_btn_notes", None),
            "ws_calendar": lambda: getattr(ws(), "_btn_calendar", None),
            "ws_browser":  lambda: getattr(ws(), "_btn_browser", None),
            "ws_files":    lambda: getattr(ws(), "_btn_files", None),
            "ws_terminal": lambda: getattr(ws(), "_btn_terminal", None),
            "settings":   lambda: getattr(tb, "settings_btn", None),
            "tasks":      lambda: getattr(tb, "tasks_btn", None),
            "memory":     lambda: getattr(tb, "memory_btn", None),
            "help":       lambda: getattr(tb, "help_btn", None),
            "on_top":     lambda: getattr(tb, "always_on_top_btn", None),
            "screenshot": lambda: getattr(tb, "screenshot_btn", None),
            "composer":   lambda: getattr(cw, "input", None),
            "conv_bar":   lambda: getattr(cw, "_conv_bar", None),
            "chat":       lambda: cw,
        }
        return f

    def _widget(self, name: str):
        getter = self._features().get(name)
        try:
            return getter() if getter else None
        except Exception:
            return None

    # ── genesis: fold the app away ────────────────────────────────────
    def prepare_genesis(self) -> None:
        """Hide everything but the bare agent chat. Safe to call on a fully
        visible window too (tour replay)."""
        win, tb = self.win, self.win.title_bar
        if self.aw is None:
            return
        self.active = True
        self._begun = False
        self._idx = -1
        if win.isMaximized():
            win.showNormal()

        # Keep the title bar VISIBLE so the genesis card stays draggable and
        # closable — this is a frameless window, so the bar is the only move
        # handle and the only way to the window controls. Just tuck away the
        # tool buttons; they grow back in during window_grow.
        tb.show()
        for b in self._titlebar_tool_buttons():
            try:
                b.hide()
            except Exception:
                pass
        try:
            self.aw._collapse_workspace()           # workspace tucked away
        except Exception:
            pass
        self.aw.enter_tour_mode(self)

        # Remember where the window should grow back to, then shrink to a card.
        geo = win.geometry()
        full = geo if geo.width() >= 900 else QRect(geo.x(), geo.y(), _FULL_W, _FULL_H)
        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1280, 800)
        fw = min(full.width(), avail.width())
        fh = min(full.height(), avail.height())
        self._full_rect = QRect(
            avail.x() + (avail.width() - fw) // 2,
            avail.y() + (avail.height() - fh) // 2, fw, fh)
        # The window enforces an 800x600 minimum — relax it so the genesis card
        # can be a small chat square. Restored when the tour ends.
        self._saved_min = (win.minimumWidth(), win.minimumHeight())
        win.setMinimumSize(_GENESIS_W, _GENESIS_H)
        win.setGeometry(QRect(
            avail.x() + (avail.width() - _GENESIS_W) // 2,
            avail.y() + (avail.height() - _GENESIS_H) // 2,
            _GENESIS_W, _GENESIS_H,
        ))

    # ── begin ─────────────────────────────────────────────────────────
    def begin(self) -> None:
        if self._begun or not self.active or self.aw is None:
            return
        self._begun = True
        app = QApplication.instance()
        if app is not None and not self._filter_installed:
            app.installEventFilter(self)
            self._filter_installed = True
        if not has_llm_key():
            self._show_keygate()
        else:
            QTimer.singleShot(350, lambda: self._run_step(0))

    def _show_keygate(self) -> None:
        self._keygate = KeyGateCard(self.aw)
        self._keygate.completed.connect(self._on_keygate_done)
        try:
            self.aw.tour_mount_widget(self._keygate)
        except Exception as e:
            print(f"[tour] mount keygate failed: {e}")
            QTimer.singleShot(0, lambda: self._on_keygate_done(False))

    def _on_keygate_done(self, ai_enabled: bool) -> None:
        if self._keygate is not None:
            try:
                self.aw.tour_unmount_widget(self._keygate)
            except Exception:
                pass
            self._keygate.deleteLater()
            self._keygate = None
        ack = KEYGATE_SAVED_SAY if ai_enabled else KEYGATE_SKIPPED_SAY
        self._say_aside(ack, then=lambda: self._run_step(0))

    def _say_aside(self, text: str, then=None) -> None:
        """Type a one-off line outside the step machinery."""
        try:
            self.typist.finished.disconnect(self._on_typed)
        except TypeError:
            pass

        def done():
            try:
                self.typist.finished.disconnect(done)
            except TypeError:
                pass
            self.typist.finished.connect(self._on_typed)
            if then:
                QTimer.singleShot(900, then)
        self.typist.finished.connect(done)
        self.typist.say(text)

    # ── narration channels: chat (pre-unfold) vs anchored popup ──────
    def _get_bubble(self) -> TourBubble:
        if self.bubble is None:
            self.bubble = TourBubble(self.win)
            self.bubble.command.connect(self.handle_command)
            self.bubble.typing_finished.connect(self._on_typed)
        return self.bubble

    # ── Enter = Continue, app-wide while the tour runs ────────────────
    def eventFilter(self, _obj, ev) -> bool:
        if (ev.type() != QEvent.Type.KeyPress
                or ev.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                or not self.active or self._keygate is not None
                or self._idx < 0):
            return False
        fw = QApplication.focusWidget()
        if isinstance(fw, QTextEdit) and fw.toPlainText().strip():
            return False
        if isinstance(fw, QLineEdit) and fw.text().strip():
            return False
        self._next()
        return True

    # ── step machinery ────────────────────────────────────────────────
    def _run_step(self, idx: int) -> None:
        if not self.active:
            return
        if idx >= len(STEPS):
            self.finish()
            return
        self._idx = idx
        step = STEPS[idx]
        self._waiting_choice = False
        self._offered = []
        self._typed_done = False
        self._pending_async = 0
        try:
            self.aw.tour_clear_choices()        # drop any prior inline buttons
        except Exception:
            pass
        if step.anchor is None and self.bubble is not None:
            self.bubble.hide_bubble()

        for action in step.actions:
            try:
                self._do_action(action)
            except Exception as e:
                print(f"[tour] action {action} failed: {e}")

        QTimer.singleShot(step.type_delay_ms,
                          lambda i=idx: self._start_typing(i))

    def _start_typing(self, idx: int) -> None:
        if not self.active or self._idx != idx:
            return
        step = STEPS[idx]
        say = step.say
        if not say:
            self._on_typed()
        elif step.anchor is not None:
            self._get_bubble().present(
                self._widget(step.anchor), say,
                step_no=idx + 1, total=len(STEPS))
        else:
            self.typist.say(say)

    def _on_typed(self) -> None:
        if not self.active or self._idx < 0:
            return
        self._typed_done = True
        self._maybe_offer_continue()

    def _maybe_offer_continue(self) -> None:
        """Buttons appear only once narration AND async stage work are done."""
        if (not self.active or self._waiting_choice
                or not self._typed_done or self._pending_async > 0
                or self._idx < 0):
            return
        step = STEPS[self._idx]
        if getattr(step, "auto_advance_ms", 0) > 0:
            self._waiting_choice = True
            self._offered = []
            QTimer.singleShot(
                step.auto_advance_ms,
                lambda i=self._idx: self._auto_advance(i))
            return
        self._offered = step.choices or [("CONTINUE  →", "next")]
        self._waiting_choice = True
        if step.anchor is not None:
            self._get_bubble().show_choices(self._offered)
        else:
            try:
                self.aw.tour_choices(self._offered)
            except Exception as e:
                print(f"[tour] tour_choices failed: {e}")

    def _advance(self) -> None:
        self.spot.clear()
        self._run_step(self._idx + 1)

    def _auto_advance(self, idx: int) -> None:
        if self.active and self._idx == idx and self._waiting_choice:
            self._waiting_choice = False
            self._advance()

    def _async_started(self) -> None:
        self._pending_async += 1

    def _async_done(self) -> None:
        self._pending_async = max(0, self._pending_async - 1)
        self._maybe_offer_continue()

    # ── stage directions ──────────────────────────────────────────────
    def _do_action(self, action: tuple) -> None:
        verb, args = action[0], action[1:]
        win, tb = self.win, self.win.title_bar

        if verb == "window_grow":
            tb.show()
            tools = self._titlebar_tool_buttons()
            for b in tools:
                b.hide()
            self._async_started()

            def grown():
                stagger([b for b in tools if not b.isVisible()], gap_ms=80)
                self._async_done()
            animate_geometry(win, self._full_rect, duration=800, on_done=grown)

        elif verb == "reveal":
            self._reveal(args[0])

        elif verb == "spotlight":
            w = self._widget(args[0])
            QTimer.singleShot(650, lambda: self.spot.focus(w)
                              if self.active else None)

        elif verb == "spotlight_items":
            QTimer.singleShot(650, lambda: self.spot.focus_items(
                self._multi_provider(args[0])) if self.active else None)

        elif verb == "spotlight_clear":
            self.spot.clear()

        elif verb == "ws_tab":
            self._switch_ws_tab(args[0])

        elif verb == "agent_extras":
            try:
                self.aw.tour_reveal_extras()
            except Exception:
                pass

        elif verb == "blink":
            QTimer.singleShot(700, lambda: self._blink(self._widget(args[0]))
                              if self.active else None)

        elif verb == "seed_chat":
            self._seed_chat()

        else:
            print(f"[tour] unknown action verb: {verb}")

    def _titlebar_tool_buttons(self) -> list:
        tb = self.win.title_bar
        names = ("help_btn", "settings_btn", "tasks_btn", "memory_btn",
                 "always_on_top_btn", "screenshot_btn")
        return [getattr(tb, n) for n in names if getattr(tb, n, None) is not None]

    def _switch_ws_tab(self, key: str) -> None:
        ws = getattr(self.aw, "_right_workspace", None)
        if ws is None:
            return
        btn = getattr(ws, f"_btn_{key}", None)
        if btn is not None:
            try:
                btn.click()
            except Exception as e:
                print(f"[tour] ws_tab {key} failed: {e}")

    def _multi_provider(self, group: str):
        """A callable → [(QRectF in window coords, QColor), …] for a named set
        of widgets, each glowing in the accent. Re-evaluated every frame."""
        if group == "titlebar_extras":
            widgets = [self._widget("on_top"), self._widget("screenshot")]
        else:
            widgets = []
        widgets = [w for w in widgets if w is not None]

        def provider():
            out = []
            for w in widgets:
                try:
                    if not w.isVisible():
                        continue
                    tl = w.mapTo(self.win, w.rect().topLeft())
                    out.append((
                        QRectF(float(tl.x()), float(tl.y()),
                               float(w.width()), float(w.height())),
                        QColor(T["brand"]),
                    ))
                except RuntimeError:
                    continue
            return out
        return provider

    def _blink(self, widget, flashes: int = 2, half_ms: int = 150) -> None:
        if widget is None:
            return
        from PyQt6.QtWidgets import QGraphicsOpacityEffect
        try:
            eff = QGraphicsOpacityEffect(widget)
            eff.setOpacity(1.0)
            widget.setGraphicsEffect(eff)
        except Exception:
            return
        seq = [0.35, 1.0] * max(1, flashes)

        def step(i: int) -> None:
            if i >= len(seq) or not self.active:
                try:
                    widget.setGraphicsEffect(None)
                except RuntimeError:
                    pass
                return
            try:
                eff.setOpacity(seq[i])
            except RuntimeError:
                return
            QTimer.singleShot(half_ms, lambda: step(i + 1))
        step(0)

    def _seed_chat(self) -> None:
        if self.aw is None:
            return
        try:
            self.aw.tour_seed_suggestions(CHAT_SEED_SAY)
        except Exception as e:
            print(f"[tour] seed chat failed: {e}")

    def _reveal(self, name: str) -> None:
        cw = self.aw
        if name == "workspace":
            ws = getattr(cw, "_right_workspace", None)
            sp = getattr(cw, "_chat_hsplitter", None)
            if ws is None or sp is None:
                return
            ws.show()
            total = max(sp.width(), 200)
            ws_px = int(total * _WS_FRACTION)
            ci = getattr(cw, "_chat_index", 0)
            wi = getattr(cw, "_ws_index", 1)
            target = [0, 0]
            target[ci] = total - ws_px
            target[wi] = ws_px
            self._async_started()
            animate_splitter(sp, target, duration=720, on_done=self._async_done)
        else:
            w = self._widget(name)
            if w is not None:
                grow_in(w, axis="h", duration=420)

    # ── user interaction ──────────────────────────────────────────────
    def handle_command(self, cmd: str) -> None:
        cmd = (cmd or "").strip().lower()
        if not self.active or self._keygate is not None:
            return
        if cmd == "begin":
            if self._waiting_choice:
                self._waiting_choice = False
                self._advance()
        elif cmd == "skipall":
            self.skip_all()
        elif cmd == "finish":
            self.finish()
        elif cmd == "next":
            self._next()
        elif cmd == "back":
            self._back()
        elif cmd == "exit":
            self.exit_early()

    # ── intro gate: natural language → LLM-decided path ──────────────
    def _classify_intro(self, text: str) -> None:
        agent = getattr(self.aw, "agent", None)
        provider = getattr(agent, "provider", None) if agent else None
        model = getattr(agent, "model", None) if agent else None
        if not provider or not model:
            self._intro_keyword_route((text or "").strip().lower())
            return
        if self._intro_clf is not None and self._intro_clf.isRunning():
            return
        self._intro_clf = _IntroClassifier(provider, model, text)
        self._intro_clf.done.connect(self._on_intro_classified)
        self._intro_clf.start()

    def _on_intro_classified(self, result: dict) -> None:
        if (not self.active or self._idx < 0
                or STEPS[self._idx].id != "intro"):
            return
        action = (result or {}).get("action")
        say = (result or {}).get("say") or ""
        if action == "begin":
            if say:
                self._say_aside(say, then=lambda: self.handle_command("begin"))
            else:
                self.handle_command("begin")
        elif action == "skip":
            self.skip_all()
        elif action == "reply" and say:
            self._say_aside(say)
        else:
            self._say_aside(
                "Happy to help either way — say **tour** and I'll show you "
                "around, or **skip** and I'll open everything up.")

    def _intro_keyword_route(self, t: str) -> None:
        def has(*words):
            return any(w in t for w in words)
        if has("tour", "yes", "sure", "show", "ok", "go", "start", "y"):
            self.handle_command("begin")
        elif has("no", "skip", "know what", "pass"):
            self.handle_command("skipall")
        else:
            self._say_aside(
                "Just say **tour** (or hit the button) and I'll show you "
                "around — or **skip** and I'll open everything up.")

    def handle_user_text(self, text: str) -> None:
        """Typed input during the tour — keyword routing, no LLM (except the
        intro gate, which gets a real one-shot reply when a key is present)."""
        t = (text or "").strip().lower()
        if not t or self._keygate is not None:
            return
        at_gate = self._waiting_choice and self._idx >= 0

        def has(*words):
            return any(w in t for w in words)

        if at_gate and STEPS[self._idx].id == "intro":
            if has_llm_key():
                self._classify_intro(text)
            else:
                self._intro_keyword_route(t)
            return

        if has("exit", "quit", "stop the tour", "end the tour", "enough"):
            self.exit_early()
        elif has("skip the tour", "skip all", "skip everything"):
            self.skip_all()
        elif has("back", "previous", "again", "repeat"):
            self._back()
        elif has("resume", "play", "continue", "next", "go on", "skip"):
            self._next()
        else:
            self._say_aside(
                "Good question to hold onto — during the tour I'm running on "
                "rails (scripted, costs you zero tokens), so I can't freestyle "
                "just yet. The moment the tour ends I'm fully interactive — ask "
                "me again then.\n\nFor now: **Enter** or **CONTINUE** keeps "
                "things moving; **back** and **exit** work too.")

    def _next(self) -> None:
        if self.typist.active:
            self.typist.finish_now()
        elif self.bubble is not None and self.bubble.typing:
            self.bubble.finish_typing()
        elif self._waiting_choice:
            cmd = self._offered[0][1] if self._offered else "next"
            self._waiting_choice = False
            if cmd == "skipall":
                self.skip_all()
            elif cmd == "finish":
                self.finish()
            else:
                self._advance()
        else:
            self._advance()

    def _back(self) -> None:
        self.typist.abort()
        if self.bubble is not None:
            self.bubble.hide_bubble()
        self.spot.clear()
        self._run_step(max(0, self._idx - 1))

    # ── endings ───────────────────────────────────────────────────────
    def skip_all(self) -> None:
        """Intro 'I know what I'm doing' — quick deploy, then done."""
        self.typist.abort()
        if self.bubble is not None:
            self.bubble.hide_bubble()
        self._waiting_choice = False
        self.spot.clear()
        self._reveal_everything()
        self._say_aside(SKIP_SAY, then=lambda: self.finish(skipped=True))

    def exit_early(self) -> None:
        """Mid-tour exit — open everything that's still folded, end cleanly."""
        self.typist.abort()
        if self.bubble is not None:
            self.bubble.hide_bubble()
        self._waiting_choice = False
        self.spot.clear()
        self._reveal_everything()
        self._say_aside(
            "No problem — tour's over, everything's open. Replay it anytime "
            "from the **?** button → Take Tour. I'm here when you need me.",
            then=self.finish)

    def _reveal_everything(self) -> None:
        win, tb = self.win, self.win.title_bar
        # Restore the window's real minimum size (relaxed for the genesis card).
        sm = getattr(self, "_saved_min", None)
        if sm is not None:
            try:
                win.setMinimumSize(sm[0], sm[1])
            except Exception:
                pass
        tb.show()
        for b in self._titlebar_tool_buttons():
            b.show()
        if win.geometry().width() < self._full_rect.width() - 40:
            animate_geometry(win, self._full_rect, duration=550)
        try:
            cw = self.aw
            sp = getattr(cw, "_chat_hsplitter", None)
            if sp is not None and getattr(cw, "_ws_size", lambda: 0)() <= 10:
                cw._right_workspace.show()
                total = max(sp.width(), 200)
                ws_px = int(total * _WS_FRACTION)
                ci = getattr(cw, "_chat_index", 0)
                wi = getattr(cw, "_ws_index", 1)
                target = [0, 0]
                target[ci] = total - ws_px
                target[wi] = ws_px
                animate_splitter(sp, target, duration=550, delay=300)
        except Exception as e:
            print(f"[tour] reveal workspace (finish) failed: {e}")
        try:
            self.aw.tour_reveal_extras()
        except Exception:
            pass

    def finish(self, skipped: bool = False) -> None:
        if not self.active:
            return
        self.typist.abort()
        if self.bubble is not None:
            self.bubble.hide_bubble()
        self.spot.shutdown()
        if self._filter_installed:
            app = QApplication.instance()
            if app is not None:
                app.removeEventFilter(self)
            self._filter_installed = False
        self._reveal_everything()
        self.active = False
        try:
            cfg = dict(load_config())
            cfg["tour_state"] = {
                "completed": not skipped, "skipped": skipped, "version": 1,
            }
            save_config(cfg)
        except Exception as e:
            print(f"[tour] could not persist tour_state: {e}")
        try:
            self.aw.exit_tour_mode()
        except Exception:
            pass
