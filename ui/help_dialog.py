"""
Help dialog — setup-focused guide for new and returning users.
"""

from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QPushButton, QTextBrowser, QLabel,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from ui.theme import PALETTE
from ui.glass_dialog import GlassDialog


def _help_html() -> str:
    p = PALETTE
    accent = p.get("accent", "#00fff7")
    muted = p.get("muted_text", "#888")
    hot = p.get("accent_bright", accent)
    border = p.get("border", "#333")
    return f"""
<style>
body {{ font-family: Consolas, monospace; font-size: 10pt; line-height: 1.5; color: {p['text']}; }}
h1 {{ color: {hot}; font-size: 15pt; margin: 0 0 10px 0; }}
h2 {{ color: {accent}; font-size: 11pt; margin: 20px 0 8px 0; padding-top: 4px;
      border-top: 1px solid {border}; }}
h2:first-of-type {{ border-top: none; margin-top: 8px; }}
h3 {{ color: {hot}; font-size: 10pt; margin: 14px 0 4px 0; }}
p {{ margin: 6px 0; }}
ul, ol {{ margin: 6px 0 8px 22px; padding: 0; }}
li {{ margin: 4px 0; }}
.tip {{ color: {muted}; font-style: italic; margin-top: 8px; }}
.warn {{ color: {hot}; }}
a {{ color: {accent}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
code {{ color: {hot}; }}
.toc {{ background: rgba(0,0,0,0.15); border: 1px solid {border};
        padding: 10px 14px; margin: 0 0 14px 0; }}
.toc li {{ margin: 2px 0; }}
</style>

<h1>Familiar — Setup &amp; Help</h1>
<p>Familiar is a desktop AI agent: <strong>chat</strong> on one side, a tabbed
<strong>workspace</strong> (Notes, Calendar, Browser, File, Terminal) on the other.
Use this guide to configure providers, workspaces, and tools. Changes in
<strong>Settings</strong> are written to disk only when you click <strong>Save</strong>.</p>

<div class="toc">
<strong>On this page</strong>
<ol>
<li><a href="#checklist">Quick-start checklist</a></li>
<li><a href="#settings-map">Settings tabs (in order)</a></li>
<li><a href="#api-keys">API Keys</a></li>
<li><a href="#model">Model</a></li>
<li><a href="#workspaces">Workspaces</a></li>
<li><a href="#prompt">System Prompt</a></li>
<li><a href="#audio">Audio</a></li>
<li><a href="#tools">Tools</a></li>
<li><a href="#network">Network</a></li>
<li><a href="#memory">Memory</a></li>
<li><a href="#tasks">Tasks</a></li>
<li><a href="#conversation">Per-conversation settings</a></li>
<li><a href="#window">Using the window</a></li>
<li><a href="#mcp">MCP (advanced)</a></li>
<li><a href="#troubleshooting">Troubleshooting</a></li>
<li><a href="#files">Where files live</a></li>
</ol>
</div>

<h2 id="checklist">Quick-start checklist</h2>
<ol>
<li><strong>API Keys</strong> — at least one provider (API key or OAuth where offered).</li>
<li><strong>Model</strong> — provider + model ID; optional fallbacks and Explore model.</li>
<li><strong>Workspaces</strong> — register project folder(s) the agent may touch.</li>
<li><strong>Conversation → Model</strong> — pick workspace (and model) for this chat.</li>
<li><strong>System Prompt</strong> — optional; defaults work for most people.</li>
<li>Send a short test message; open <strong>File</strong> or <strong>Terminal</strong> in the workspace panel if you code locally.</li>
</ol>
<p class="tip">Settings and Help are non-modal — you can keep them open beside the main window.</p>

<h2 id="settings-map">Settings tabs (recommended order)</h2>
<p>Title bar <strong>Settings</strong> opens these tabs:</p>
<ol>
<li><strong>UI</strong> — theme, chat display, workspace panel side, CRT overlay.</li>
<li><strong>API Keys</strong> — provider credentials (+ Tavily for web search).</li>
<li><strong>Model</strong> — main model, summary, vision, explore, embeddings, fallbacks, thinking.</li>
<li><strong>Workspaces</strong> — project roots and optional Python venv paths.</li>
<li><strong>System Prompt</strong> — global personality / rules for every turn.</li>
<li><strong>Audio</strong> — UI sounds, TTS voice engine, workspace edit/typing sounds.</li>
<li><strong>Tools</strong> — per-tool models (subagent, memory), stats, audit, truncation caps.</li>
<li><strong>Network</strong> — optional LAN / Cloudflare tunnel (power users).</li>
</ol>

<h2 id="api-keys">API Keys</h2>
<p>Keys are stored in <code>data/keys.json</code> (local only; not sent except to the provider you call).</p>
<h3>Chat providers (Settings → API Keys)</h3>
<ul>
<li><strong>Local API</strong> — Ollama, LM Studio, or any OpenAI-compatible server. No key required.
  Default base URL is <code>http://127.0.0.1:1234/v1</code> if you leave URL blank (override in keys entry if needed).</li>
<li><strong>OpenRouter</strong> — one key, many hosted models.</li>
<li><strong>OpenAI</strong> — API key or OAuth (Codex credentials).</li>
<li><strong>Anthropic</strong> — API key or OAuth (Claude Code credentials).</li>
<li><strong>DeepSeek, Google GenAI, Kimi/Moonshot, Z.AI/GLM, MiniMax, Alibaba Cloud, Hugging Face</strong> — add only the keys you use.</li>
<li><strong>Tavily</strong> — powers web search tools; separate from chat providers.</li>
<li><strong>ElevenLabs</strong> — optional; also configurable under Settings → Audio when that engine is selected.</li>
</ul>
<p>After saving keys, choose the same provider on <strong>Settings → Model</strong> (or per conversation — see below).</p>

<h2 id="model">Model</h2>
<h3>Main chat</h3>
<ul>
<li><strong>Provider + Model</strong> — default brain for new work. Model field autocomplete remembers recent IDs per provider.</li>
<li><strong>Temp</strong> — sampling temperature for replies (shown beside the model field).</li>
<li><strong>Summary Model</strong> — optional separate model for rolling summaries; blank = use main model.</li>
<li><strong>Max Tokens</strong> — upper bound on assistant output length.</li>
</ul>
<h3>Specialized models (same tab)</h3>
<ul>
<li><strong>Vision Model</strong> — enable + provider + model for image attachments and the vision tool.</li>
<li><strong>Explore Model</strong> — cheap/fast model for <code>explore_files</code> (parallel file summaries). Use Haiku or a small OpenRouter model, not your flagship chat model.</li>
<li><strong>Embedding Model</strong> — vector search for codebase + session recall (provider auto-routed).</li>
</ul>
<h3>Reasoning &amp; fallbacks</h3>
<ul>
<li><strong>Thinking</strong> — extended reasoning where the provider supports it; effort level + Anthropic token budget.</li>
<li><strong>Fallback Models 1–3</strong> — tried in order when the primary model <em>refuses</em> (not for every network error).</li>
</ul>
<p class="tip">Subagent and Memory chat models are edited on <strong>Settings → Tools</strong> (table rows), not duplicated on the Model tab.</p>

<h2 id="workspaces">Workspaces</h2>
<p>A workspace is a named project root: file tools, terminals, and semantic search stay scoped to it.</p>
<ul>
<li><strong>Add Folder</strong> / <strong>Create New Folder</strong> / <strong>Create with venv</strong> — quick setup paths.</li>
<li><strong>Name, Path, venv</strong> — venv is optional; when set, Python tools can prefer that interpreter.</li>
<li>Removing a workspace from the list does not delete files on disk.</li>
</ul>
<p><strong>Per conversation:</strong> click <strong>Conversation</strong> (beside the bottom conversation bar) → <strong>Model</strong> tab → <strong>Workspace</strong> dropdown. The hint text beside the bar shows the active workspace name.</p>

<h2 id="prompt">System Prompt</h2>
<p>Global instructions prepended every turn (tone, coding style, safety). The built-in default matches Familiar’s personality; replace or extend for stricter coding rules or project norms.</p>
<p class="tip">Per-conversation overrides live in <strong>Conversation → Prompt</strong> and layer on top — they do not replace this global prompt.</p>

<h2 id="audio">Audio</h2>
<ul>
<li><strong>UI Sounds</strong> — chimes from the app <code>sounds/</code> folder (startup, lightbulb, etc.).</li>
<li><strong>Workspace sounds</strong> — edit sounds when the agent writes files; typing sounds while you type in the built-in file viewer (toggle separately).</li>
<li><strong>Mute patterns</strong> — path globs that suppress edit sounds for noisy paths.</li>
<li><strong>Use Voice / Voice Engine</strong> — TTS for spoken replies: Edge (free), ElevenLabs (API), or local Chatterbox.</li>
</ul>

<h2 id="tools">Tools</h2>
<p>Familiar ships many tools (~50). By default a <strong>router</strong> sends only a relevant subset each turn (~85–90% fewer schema tokens). On the Tools tab:</p>
<ul>
<li><strong>Stats table</strong> — calls, tokens, errors; <code>explore_files</code> row is read-only (edit Explore on Model tab).</li>
<li><strong>Subagent / Memory rows</strong> — set cheaper models for delegated work and the memory librarian.</li>
<li><strong>Send full tools list each turn</strong> — disable routing (more tokens, all tools visible).</li>
<li><strong>Tool Self-Audit</strong> — after repeated tool failures, route findings to a chosen conversation.</li>
<li><strong>Tool Result Truncation</strong> — cap huge outputs; the agent sees head/tail + hints to read narrower slices.</li>
</ul>

<h2 id="network">Network (optional)</h2>
<p>Expose Familiar to other machines: shared secret, inbound port, optional Cloudflare tunnel. Only enable if you need remote access; treat the secret like a password. Per-stream network sync is configured in the Memory dialog when relevant.</p>

<h2 id="memory">Memory (title bar → Memory)</h2>
<p>Long-term notes across chats, organized into <strong>streams</strong> (projects, personas, etc.).</p>
<ul>
<li>Create/rename streams, set focus text, <strong>Auto-subscribe</strong> for new conversations.</li>
<li>Notes use categories; a background librarian recalls relevant notes before each turn.</li>
<li><strong>Rolling summary</strong> — per-stream guidance for compressing long threads.</li>
<li><strong>Subscribe per chat:</strong> right-click the conversation dropdown → <strong>Streams</strong>, or use <strong>Conversation → Streams</strong>.</li>
</ul>

<h2 id="tasks">Tasks (title bar → Tasks)</h2>
<p>Cron-style scheduled prompts: one-shot or recurring. Use for reminders, daily digests, or automated agent chores. Each task can target a conversation and optional delivery (sound, notification, etc.).</p>

<h2 id="conversation">Per-conversation settings</h2>
<p><strong>Conversation</strong> button (bottom bar, right of the chat picker) opens:</p>
<ul>
<li><strong>Model</strong> — override provider, model, and workspace for this chat only.</li>
<li><strong>Prompt</strong> — extra system instructions + rolling summary text for this chat.</li>
<li><strong>Streams</strong> — which memory streams to read/write, with permissions.</li>
<li><strong>Debug</strong> — inspect stored LLM/tool round-trips for this conversation.</li>
</ul>
<p>Right-click the conversation dropdown to <strong>Rename</strong> or toggle <strong>Streams</strong> quickly. <strong>−</strong> deletes (with confirm); <strong>+</strong> creates a new chat.</p>

<h2 id="window">Using the window</h2>
<ul>
<li><strong>Title bar (left)</strong> — <strong>?</strong> Help, <strong>Settings</strong>, <strong>Tasks</strong>, <strong>Memory</strong>.</li>
<li><strong>Title bar (right)</strong> — Always on top (↑), screenshot to clipboard (▣), window controls. Drag the title bar to move; drag edges to resize; double-click title bar to maximize.</li>
<li><strong>Workspace panel</strong> — toolbar: Notes | Calendar | Browser | File | Terminal. Starts collapsed; agent or you can open it. Side (left/right) is in Settings → UI → Workspace Side.</li>
<li><strong>Browser</strong> — persistent Chromium profile (cookies/logins). Requires <code>PyQt6-WebEngine</code>.</li>
<li><strong>Terminal</strong> — real PTY (ConPTY on Windows); full-screen TUIs work. Cwd follows the active workspace.</li>
<li><strong>Single instance</strong> — launching Familiar again focuses the existing window.</li>
</ul>

<h2 id="mcp">MCP (advanced)</h2>
<p>Model Context Protocol servers add external tools (databases, SaaS APIs, etc.). Define servers in <code>config.json</code> under <code>mcp_servers</code>:</p>
<ul>
<li><code>stdio</code> — command + args (e.g. <code>npx -y @modelcontextprotocol/server-filesystem /path</code>).</li>
<li><code>http</code> — URL + headers; OAuth block supported for protected endpoints.</li>
</ul>
<p>On startup, enabled servers connect in the background; tools appear as <code>mcp__&lt;server&gt;__&lt;tool&gt;</code>. The agent also has a meta <code>mcp</code> tool to list/connect/disconnect. See the project README for full schema examples.</p>

<h2 id="troubleshooting">Troubleshooting</h2>
<h3>Startup / prerequisites</h3>
<p>On launch, Familiar checks required Python packages and CLI tools (<code>ruff</code>, <code>pyflakes</code>, <code>pylsp</code>, <code>tree-sitter</code>, <code>sqlite-vec</code>, <code>pyte</code>, etc.) and may auto-install via pip. Windows also needs <code>pywinpty</code> for the integrated terminal. Read the splash/status line for the exact miss.</p>
<h3>Agent can’t see my files</h3>
<p>Confirm <strong>Conversation → Model → Workspace</strong> points at your project. Tools are scoped to that root; arbitrary paths outside it may be rejected for safety.</p>
<h3>Empty, refused, or error replies</h3>
<ul>
<li>Provider, model ID, and API key must match (OpenRouter IDs often look like <code>vendor/model</code>).</li>
<li>Try a <strong>Fallback</strong> model on the Model tab.</li>
<li>Check provider dashboard for billing, rate limits, or outages.</li>
</ul>
<h3>Browser tab missing or blank</h3>
<p>Install <code>PyQt6-WebEngine</code>. Chat and other tabs still work without it.</p>
<h3>Costs adding up</h3>
<p>Use a smaller <strong>Explore Model</strong>, cheaper <strong>Memory</strong> / <strong>Subagent</strong> models on the Tools tab, lower <strong>Max Tokens</strong>, and keep tool routing enabled (leave “full tools list” off unless debugging).</p>

<h2 id="files">Where files live</h2>
<ul>
<li><code>config.json</code> — models, UI flags, workspaces, MCP servers, tool caps (in the Agent app folder).</li>
<li><code>data/keys.json</code> — API keys (keep private; gitignored with <code>data/</code>).</li>
<li><code>data/</code> — SQLite conversations, window geometry, tool stats, viewer state.</li>
<li><code>sounds/</code> — bundled UI audio; agent can also play sounds via tools.</li>
</ul>

<p class="tip">Still stuck? Ask in chat — “walk me through setting up OpenRouter and a workspace for X” — and Familiar can guide you live.</p>
"""


class HelpDialog(GlassDialog):
    """Non-modal setup guide; stays readable while Settings is open."""

    def __init__(self, parent=None, on_open_settings=None):
        super().__init__(
            title="Help", parent=parent, width=700, height=760,
            # No geometry_key: saved positions can restore the dialog onto
            # another monitor / a stale spot. Help must ALWAYS open centered
            # over Familiar's window (see showEvent).
        )
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._on_open_settings = on_open_settings
        self._build_ui()

    def _build_ui(self) -> None:
        layout = self.content_layout()
        p = PALETTE

        intro = QLabel(
            "Setup guide — follow the checklist, then Save in Settings. "
            "Links below jump within this page."
        )
        intro.setFont(QFont("Consolas", 9))
        intro.setWordWrap(True)
        intro.setStyleSheet(
            f"color: {p['muted_text']}; background: transparent; border: none;"
        )
        layout.addWidget(intro)

        self._body = QTextBrowser()
        self._body.setOpenExternalLinks(True)
        self._body.setFont(QFont("Consolas", 10))
        self._body.setHtml(_help_html())
        self._body.setStyleSheet(f"""
            QTextBrowser {{
                background: transparent;
                border: 1px solid {p['border']};
                padding: 10px;
            }}
        """)
        layout.addWidget(self._body, stretch=1)

        btn_row = QHBoxLayout()
        top_btn = QPushButton("Top")
        top_btn.setToolTip("Scroll to the beginning")
        top_btn.clicked.connect(lambda: self._body.scrollToAnchor("checklist"))
        btn_row.addWidget(top_btn)
        btn_row.addStretch()
        if self._on_open_settings is not None:
            settings_btn = QPushButton("Open Settings…")
            settings_btn.setToolTip("Open Settings (non-modal)")
            settings_btn.clicked.connect(self._on_open_settings)
            btn_row.addWidget(settings_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        # Re-center over the main window every time it opens — never trust a
        # stale position (parent may have moved/resized since last show).
        if self.parent() is not None:
            geo = self.parent().window().geometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )
        try:
            self._body.scrollToAnchor("checklist")
        except Exception:
            pass
