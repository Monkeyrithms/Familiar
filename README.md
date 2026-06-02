<h1 align="center">
  <span style="font-family: Gabriola, 'Segoe Script', 'Palatino Linotype', cursive;">Familiar</span>
  <img src="assets/agent.png" alt="Familiar icon" height="28" align="absmiddle">
</h1>

**An all-in-one AI agent written in Python that lives in a normal desktop window — not a terminal.**

&emsp;&emsp;Most capable open source agents assume you're comfortable in a command line environment, spinning up local servers, or wiring together CLI tools. Familiar is for everyone else: open a window, type, and get an open-source agent that can use your computer, browse the web, write and run code, and navigate your files — without you ever touching a command line. It's an agent-in-a-box that does the things, in a little UI that makes it easy for the non-technically-inclined to still get a lot done.

&emsp;&emsp;With ~55 built-in tools, integrated terminals, an embedded browser with agentic control, a file and media viewer, semantic code search, persistent memory, sub-agents, and a scheduler for cron jobs, this little productivity fairy does it all, and takes up to a dozen LLM providers, keeps everything in local SQLite, and stays light enough to just leave open. Power users get the depth; everyone else gets to ignore it.

&emsp;&emsp;If you're still using closed-source harnesses and want the power of open-source, still using VS Code for quick or agentic work, have a preference for windowed UIs or Python, dont wanna wire up Telegram or Discord to use your agent, or if you're just trying to figure out "how to openclaw" in the first place — this app is for you.

---

<h2 align="center">Table of Contents</h2>

<p align="center">
<a href="#highlights">Highlights</a> &nbsp;·&nbsp;
<a href="#layout">Layout</a> &nbsp;·&nbsp;
<a href="#requirements">Requirements</a> &nbsp;·&nbsp;
<a href="#installation">Installation</a> &nbsp;·&nbsp;
<a href="#configuration">Configuration</a> &nbsp;·&nbsp;
<a href="#running">Running</a> &nbsp;·&nbsp;
<a href="#the-workspace">The Workspace</a> &nbsp;·&nbsp;
<a href="#tools">Tools</a> &nbsp;·&nbsp;
<a href="#core-architecture">Core Architecture</a> &nbsp;·&nbsp;
<a href="#memory--conversations">Memory &amp; Conversations</a> &nbsp;·&nbsp;
<a href="#sub-agents">Sub-agents</a> &nbsp;·&nbsp;
<a href="#scheduled-tasks">Scheduled Tasks</a> &nbsp;·&nbsp;
<a href="#mcp-integration">MCP Integration</a> &nbsp;·&nbsp;
<a href="#safety--defense-in-depth">Safety &amp; Defense-in-Depth</a> &nbsp;·&nbsp;
<a href="#theming">Theming</a> &nbsp;·&nbsp;
<a href="#data--file-layout">Data &amp; File Layout</a> &nbsp;·&nbsp;
<a href="#troubleshooting">Troubleshooting</a> &nbsp;·&nbsp;
<a href="#development">Development</a>
</p>

---

## Highlights

- **12+ LLM providers, one window** — Use your own AI. Anthropic, OpenAI, OpenRouter, DeepSeek, Google Gemini, Kimi/Moonshot, Z.AI/GLM, MiniMax, Alibaba Qwen, Hugging Face, and any local OpenAI-compatible endpoint (Ollama / LM Studio). Switch provider/model per conversation. OAuth login supported for Anthropic (Claude Code creds) and OpenAI (Codex creds).
- **~55 built-in tools** — files, shell, git, web, browser automation, vision, OCR, TTS, transcription, charts, PDF generation, databases, and more — each hot-reloadable without restarting the app.
- **Two-pass tool routing** — tools are grouped into ~8 categories; a cheap router picks the category first, so only the relevant schemas are sent. ~85–90% fewer tool-schema tokens per turn.
- **Integrated PTY terminal** — a real pseudo-terminal (ConPTY on Windows, POSIX PTY elsewhere) that runs full-screen TUIs: `vim`, `htop`, even `claude` / `codex` themselves. The agent can drive it too.
- **Embedded browser** — a persistent-profile Chromium tab (cookies, logins, OAuth) the agent can navigate and read.
- **Semantic code search** — AST-aware chunking via tree-sitter, embeddings stored per-workspace in SQLite + sqlite-vec.
- **Persistent memory** — an async "librarian" agent recalls relevant notes before each turn and commits durable facts after, across named memory streams. Hybrid full-text + vector recall.
- **Sub-agent orchestration** — decompose a goal into a dependency-aware task graph and run sub-agents in parallel, each with its own context and filtered toolset.
- **Scheduled tasks** — recurring or one-shot prompts on a cron/interval schedule, with sound/visual/delivery actions.
- **MCP client** — connect Model Context Protocol servers over stdio / streamable-HTTP / SSE, with OAuth 2.0 (PKCE) for HTTP servers.
- **Safety built in** — prompt-injection scanning (zero-token local regex), command risk analysis, secret redaction, and untrusted-data fencing of all tool output.
- **Themable UI** — one base color + brightness drives the entire palette (dark or light), with an optional retro CRT scanline overlay.

---

## Layout

The window splits into a **chat column** on the left — conversation messages, tool-call chips, inline charts/images/code, and the composer input — and a tabbed **right workspace** (Notes · Calendar · Browser · File viewer · Terminal). A draggable **conversation bar** runs along the bottom; the **title bar** carries Settings / Tasks / Memory on the left and always-on-top, screenshot-to-clipboard, and window controls on the right.

It's a frameless window: drag the title bar to move, drag any edge to resize. **Always-on-top** toggle and **screenshot-to-clipboard** (with a camera-flash animation) live in the title bar, and Familiar is **single-instance** — launching it again just surfaces the existing window.

---

## Requirements

- **Python 3.10+** (uses modern type-union syntax)
- **PyQt6** + **PyQt6-WebEngine** (the embedded browser tab; the app degrades gracefully without WebEngine)
- A few CLI tools and Python packages are **required** and checked at startup (`core/prereqs.py`) — the app exits with a clear message if they're missing:

| Required (fatal if missing) | Install |
|---|---|
| `ruff` | `pip install ruff` |
| `pyflakes` | `pip install pyflakes` |
| `pylsp` (python-lsp-server) | `pip install python-lsp-server` |
| `tree-sitter`, `tree-sitter-language-pack` | `pip install tree-sitter tree-sitter-language-pack` |
| `sqlite-vec` | `pip install sqlite-vec` |

| Optional (warn-only — needed only for non-Python work) | Install |
|---|---|
| `node` | nodejs |
| `typescript-language-server` | `npm i -g typescript-language-server` |
| `gopls` | `go install golang.org/x/tools/gopls@latest` |
| `rust-analyzer` | rustup component |

The integrated PTY terminal needs `pyte` plus `pywinpty` (Windows) or `ptyprocess` (POSIX).

---

## Installation

```bash
cd "Apps/Agent"
pip install -r requirements.txt

# PyQt6 UI stack (install separately — has its own pinned versions)
pip install PyQt6 PyQt6-WebEngine
```

Then add your API keys (see [Configuration](#configuration)) and launch.

> **Note:** Familiar runs from this directory in place — there is no build/install step. It reads and writes `config.json`, `keys.json`, `tasks.json`, and the `data/` tree right here.

---

## Configuration

### API keys — `keys.json`

Keys live in `keys.json` (one entry per provider). Recognized keys:

```jsonc
{
  "anthropic":   "sk-ant-...",
  "openai":      "sk-...",
  "openrouter":  "sk-or-...",
  "deepseek":    "...",
  "google":      "AIza...",
  "kimi":        "...",        // Moonshot
  "zai":         "...",        // Z.AI / Zhipu GLM
  "minimax":     "...",
  "alibaba":     "...",        // Qwen
  "huggingface": "hf_...",
  "local":       "",           // Ollama / LM Studio (usually no key)
  "tavily":      "tvly-...",   // web_search
  "elevenlabs":  "..."         // optional TTS backend
}
```

You can edit `keys.json` directly or use **Settings → API Keys**. Anthropic and OpenAI can also authenticate via existing **OAuth** credentials (Claude Code / Codex) rather than a raw key.

> ⚠️ **Do not commit `keys.json` (or `config.json`, which may hold a `google_api_key`) to source control.** Keep them local.

### Behavior — `config.json`

`config.json` holds everything else, editable via the **Settings** dialog. Notable keys:

| Key | Purpose |
|---|---|
| `provider`, `model`, `provider_models` | Active provider + per-provider model selection |
| `max_tokens`, `temperature` | Generation params |
| `system_prompt` | Global system message |
| `fallback_model_1..3` (+ `_provider`) | Models to retry with on refusal/error |
| `reasoning_effort` | `off` / `low` / `medium` / `high` — normalized per provider |
| `thinking_enabled`, `thinking_budget` | Extended thinking (Anthropic) |
| `enable_summarization`, `summary_char_limit`, `summary_model` | Rolling summarizer |
| `embedding_model` | Semantic search / memory embeddings (default `openai/text-embedding-3-small`) |
| `memory_streams` | Named memory streams + per-stream summary guidance |
| `workspaces` | Named workspace folders (path + optional venv) |
| `base_color`, `brightness`, `crt_enabled`, `crt_speed` | Theme + CRT overlay |
| `tts_backend`, `tts_voice`, `vision_model` | Audio / vision config |
| `tool_display_mode`, `show_usage`, `show_timestamps`, `ui_sounds` | UI preferences |

---

## Running

```bash
python main.py
```

Or, on Windows, double-click **`START.bat`** or **`Agent.lnk`**. Both ship in the folder; the shortcut icon and target are fixed automatically to this copy of the app on launch (no setup script).

On launch Familiar checks prerequisites, restores its last window geometry and conversation, preloads UI sounds, and starts the tools hot-reload watcher.

---

## The Workspace

The window is split into the **chat** (left) and a tabbed **right workspace**.

### Chat
- Message bubbles with click-to-copy, code syntax highlighting, and markdown rendering
- Tool-call chips (configurable: chip / bubble / comma modes, with de-duplication like "grep ×3")
- Inline image thumbnails, chart cards, and a live "thinking" indicator
- A **conversation bar** along the bottom: draggable-to-reorder bricks, right-click to rename/delete, `+` for new

### Right workspace tabs
- **Notes** — local, category-organized scratch notes (`data/workspace_notes.json`)
- **Calendar** — month grid overlaid with scheduled-task indicators
- **Browser** — embedded Chromium (QWebEngine) with a persistent profile (cookies/logins survive restarts), tabs, back/forward/reload, and OAuth popup handling. The agent can read the page you're looking at.
- **File** — a tabbed file viewer: file tree + code editor (syntax-highlighted, live-reload on external change) + an integrated **media viewer** (images, animated GIF/APNG, SVG, video, audio with transport controls)
- **Terminal** — the integrated PTY terminal (see Highlights), one tab per conversation, with intelligent output coloring (status keywords, paths, URLs, timestamps, full 16-color ANSI)

### Dialogs
- **Settings** — UI/theme, API keys, model selection, workspaces, system prompt, audio, and a tools table (enable/disable, descriptions, invocation counts)
- **Tasks** — the scheduler (conditions + actions pipeline with validity highlighting and countdowns)
- **Memory** — manage memory streams, browse/edit notes, configure rolling-summary guidance
- **Per-conversation settings** — override model, system prompt, and memory-stream read/write access, plus a **Debug panel** showing the exact full context sent to the model each turn

---

## Tools

Tools self-register on import (`tools/registry.py`, `tools/__init__.py`). Each declares a name, description, JSON schema, and execute function; tools that accept a `ctx` parameter get a **ToolContext** (abort signal for the Stop button, live progress callback, cwd, message history). A `watchdog`-based **hot-reload** watcher reloads edited tool modules live — you can develop tools while the app runs.

<details>
<summary><strong>File operations</strong></summary>

| Tool | Description |
|---|---|
| `file_read` | Read files with line numbers; auto-parses PDF/DOCX/XLSX/PPTX; offset/limit for large files |
| `file_write` | Write/overwrite files; creates parent dirs; emits change events |
| `file_edit` | Targeted string replacement with whitespace/indent-tolerant fuzzy matching |
| `apply_patch` | Atomic multi-file add/edit/delete/rename with anchor-based matching |
| `multi_file` | Create/update multiple files in one call |
| `file_search` | Fuzzy filename search with typo tolerance |
| `glob` | List files by glob pattern, sorted by mtime, auto-skipping ignore dirs |
| `grep` | ripgrep-backed content search |
| `file_watcher` | Watch directories for changes |
| `file_viewer` | Open a file in the right-panel viewer (with highlight / diff render) |
| `notebook` | Read/edit Jupyter notebooks |
| `diff_tool` | Generate/apply unified diffs |

</details>

<details>
<summary><strong>Code execution & language tooling</strong></summary>

| Tool | Description |
|---|---|
| `terminal` | Run shell commands with live streaming output + abort + permission checks |
| `workspace_terminal` | Send commands to the active workspace terminal tab |
| `lsp` | LSP actions (diagnostics, definition, references, hover, symbols) for Python/JS/Go/Rust |
| `lint` | Auto-lint after edits (ruff → pyflakes → py_compile; subprocess linters for other langs) |
| `hot_reload` | Reload tool modules |

</details>

<details>
<summary><strong>Search & discovery</strong></summary>

| Tool | Description |
|---|---|
| `vector_search` | Semantic code search over per-workspace embeddings (index/search/reindex) |
| `session_search` | Recall past conversations via hybrid FTS5 + vector search |
| `explore_files` | Spawn a cheap parallel sub-agent swarm to summarize many files without flooding context |

</details>

<details>
<summary><strong>Git & version control</strong></summary>

| Tool | Description |
|---|---|
| `git_tool` | status / diff / log / blame / branch / show / stash |
| `worktree` | Create/list/remove isolated git worktrees for parallel branch work |
| `checkpoint_tool` | List / diff / restore filesystem snapshots (shadow-git, never touches your `.git`) |
| `project_loader` | Load + summarize a project tree (respects `.gitignore`) |
| `workspace` / `workspace_browser` | Manage workspaces; read the embedded browser |

</details>

<details>
<summary><strong>Web & HTTP</strong></summary>

| Tool | Description |
|---|---|
| `web_search` | Web search via Tavily |
| `web_fetch` | Fetch a URL, optionally HTML→markdown |
| `http_client` | Full REST client (GET/POST/PUT/DELETE/PATCH, headers, auth, JSON) |
| `browser` / `browser_auto` | Headless Playwright browser automation (navigate, click, type, snapshot, a11y tree) |

</details>

<details>
<summary><strong>AI, media & data</strong></summary>

| Tool | Description |
|---|---|
| `vision` | Analyze images (URL or local) with a vision model |
| `ocr` | Extract text from images (Tesseract or vision fallback) |
| `transcribe` | Audio → text (local Whisper or OpenAI Whisper) |
| `tts` | Text → speech (Edge Neural / ElevenLabs / local Chatterbox) |
| `chart` | vispy line/bar/scatter/candlestick/heatmap → PNG card |
| `pdf_gen` | Generate PDFs from text/markdown/HTML |
| `db_query` | SQL against SQLite (read-only by default) |
| `data_extract` | Messy text/HTML/CSV → clean JSON |
| `archive` | Create/extract zip/tar |
| `doc_parser` | Parse documents |

</details>

<details>
<summary><strong>Agents, planning & system</strong></summary>

| Tool | Description |
|---|---|
| `subagent` | Spawn parallel sub-agents (delegate / spawn / status / wait) |
| `plan` | In-flight work plan with live progress steps |
| `reflect` | Self-review loop — critique the draft reply against your criteria and silently rewrite before you see it (pre/post/both; this turn or a standing rule) |
| `ask_user_question` | Pause and ask you a structured multiple-choice question instead of guessing |
| `thinking` | Adjust extended-thinking level at runtime |
| `memory` | Read/write/search memory-stream notes explicitly |
| `tasks` | Manage scheduled tasks |
| `mcp_tool` | Bridge to MCP servers (tools register as `mcp__<server>__<tool>`) |
| `ssh_tool` | Run commands on remote hosts (paramiko / system ssh) |
| `clipboard` | Read/write the system clipboard |
| `notify` | Send email (SMTP) or webhook notifications |
| `screenshot` | Capture Familiar's own window |
| `play_sound` | Queue a sound for playback |
| `audit` | Token/cost auditor (per-turn counts, hotspots, per-tool stats) |

</details>

---

## Core Architecture

The agent loop (`core/agent.py`) does much more than call an API:

1. **Two-pass tool routing** — a router LLM picks one of ~8 tool categories; only that category's schemas are sent for the real call.
2. **Streaming** with per-delta callbacks for live UI updates.
3. **Malformed tool-call recovery** — auto-repairs broken JSON and fuzzy-corrects misspelled tool names.
4. **Refusal detection** — if a response opens with "I'm sorry / I cannot…", it retries on a fallback model.
5. **Retry with backoff** — up to 4 attempts with exponential backoff.
6. **Learned model quirks** (`core/model_quirks.py`) — when a model rejects an unsupported kwarg, Familiar learns it and strips it on the next call (persisted to `data/`).
7. **Per-model behavior nudges** (`core/model_behavior.py`) — short, targeted corrections for known failure modes per model family.
8. **Output truncation** (`core/truncate.py`) — large tool results are head+tail trimmed (default 12K chars) with a model-friendly hint on how to re-fetch the rest.
9. **Context compression** (`core/context_compressor.py`) — as context fills, old tool outputs are pruned (recent tokens protected) and orphaned tool-call/result pairs repaired.

**Providers** (`core/providers.py`): all use the OpenAI Python SDK with provider-specific `base_url`s, except Anthropic, which uses its native SDK behind an OpenAI-shaped adapter. Reasoning effort is normalized (`off`/`low`/`medium`/`high`) and translated per provider (Anthropic `thinking` blocks vs OpenAI `reasoning_effort`).

---

## Memory & Conversations

- **Storage** — conversations and memory streams live in local **SQLite** with **FTS5** full-text and **sqlite-vec** vector search (`core/database.py`, `core/conversations.py`).
- **Rolling summarizer** (`core/summarizer.py`) — compresses older history into a structured 9-section narrative (request, plan, files, errors/fixes, progress, all user messages, current work) while keeping recent turns verbatim; per-stream guidance controls what's kept vs. dropped.
- **Memory agent** (`core/memory_agent.py`) — an async "librarian" that runs on a cheap model in a background thread (~$0.001/turn): **before** a turn it injects relevant recalled notes; **after**, it commits durable facts. Recall blends ~60% vector + ~40% full-text.
- **Memory streams** — named scopes (e.g. *General*, *RPG*) with auto-subscribe and per-conversation read/write permissions.

---

## Sub-agents

The `subagent` tool (`core/subagent.py`) decomposes a goal into a dependency graph, then dispatches sub-agents in parallel rounds as dependencies resolve. Each sub-agent has its own conversation and a filtered toolset; a thread-safe **SharedMemory** store feeds upstream results into downstream agents. Status flows to the UI as live cards and terminal tabs. A simple single-agent "spawn" mode is also available.

---

## Scheduled Tasks

The scheduler (Tasks dialog + `tools/tasks.py`, stored in `tasks.json`) runs prompts on a cron/interval schedule or one-shot datetime. Each task carries **conditions** and **actions** (run an LLM prompt, fire a visual window alert, play a sound) and can deliver results to a target conversation or stream. Example included: an *Hourly Market News Monitor* that checks tickers and alerts on significant news.

---

## MCP Integration

`core/mcp_client.py` runs an asyncio loop on a background thread and supports three transports: **stdio** (command + args), **streamable-HTTP** (URL), and **SSE** (legacy). HTTP servers can use **OAuth 2.0 (PKCE)** — on first connect Familiar opens a browser, catches the redirect on a loopback server, exchanges the code, stores tokens in `data/mcp_tokens.json`, and auto-refreshes them. Remote tools register dynamically as `mcp__<server>__<tool>`.

---

## Safety & Defense-in-Depth

- **Prompt-injection scanner** (`core/prompt_injection.py`) — zero-token local regex over external content (files, web pages, tool output): invisible/bidi codepoints, fake role markers, jailbreak phrasing, ChatML/HTML control tokens. Hostile content is blocked; suspicious content warns the model.
- **Untrusted-data fencing** — tool output is wrapped in `⟦tool-output:NAME⟧ … ⟦/tool-output⟧` and the system prompt teaches the model to treat it as data, not instructions.
- **Command safety** (`core/command_safety.py`) — parses shell commands, flags file-mutating ops, path escapes outside the workspace, and exfil patterns; classifies as safe / needs-approval / blocked.
- **Secret redaction** (`core/redact.py`) — masks API keys, bearer tokens, connection strings, and private keys before they reach the model.
- **Clarification mode** (`core/clarification.py`) — for non-trivial build requests, injects a short "interview" nudge (skippable with "just do it").
- **Reflection / self-review** (`core/reflection.py`, `tools/reflect.py`) — a steerable self-review loop. Tell Familiar to *"review your work"* or *"don't write my character"* (or it invokes the `reflect` tool itself) and it critiques its draft against your criteria and **silently rewrites** until it passes — *before* you ever see the reply. Works as a pre-answer reasoning pass, a post-answer rewrite, or both; scoped to a single turn or kept as a standing rule for the conversation. Best paired with streaming off for the cleanest "only see the final answer" experience.

---

## Theming

The entire palette is derived from a single **base color** + **brightness** value (Settings → UI). Dark and light modes are chosen automatically by brightness, with luminance-aware text and accent-tinted selection. An optional **CRT overlay** (`ui/crt_overlay.py`) adds animated scanlines and an edge glow. UI sound effects (file edits, messages, alerts, snapshots) live in `sounds/`.

---

## Data & File Layout

```
Apps/Agent/
├── main.py                 # entry point: window, single-instance guard, lifecycle
├── config.json             # behavior + UI config (KEEP LOCAL — may hold a key)
├── keys.json               # API keys (KEEP LOCAL — never commit)
├── tasks.json              # scheduled tasks
├── requirements.txt        # Python deps (PyQt6 installed separately)
├── START.bat / Agent.lnk   # Windows launchers (no console)
├── assets/                 # app icons
├── sounds/                 # UI sound effects
├── core/                   # agent loop, providers, memory, safety, MCP, LSP, …
├── tools/                  # ~55 self-registering, hot-reloadable tools
├── ui/                     # PyQt6 widgets, dialogs, theme, terminal, browser
└── data/                   # runtime state (created as needed):
    ├── conversations / *.db        # SQLite conversation + memory stores
    ├── vector_indexes/             # per-workspace semantic index
    ├── checkpoints/                # shadow-git filesystem snapshots
    ├── worktrees/                  # git worktrees
    ├── webengine_workspace/        # embedded browser profile
    ├── image_cache / audio_cache / streams / summaries / voice_refs
    ├── window_state.json           # window geometry
    ├── mcp_tokens.json             # MCP OAuth tokens
    └── error_log.txt               # tool failure log
```

Workspace paths are stored **relative** to the Agent root where possible (`core/workspace_paths.py`), so the whole tree can be moved between drives without breaking config.

---

## Troubleshooting

- **"MISSING REQUIRED DEPENDENCIES" on startup** — run `pip install -r requirements.txt`; the message lists exactly what's missing.
- **No browser tab** — install `PyQt6-WebEngine`. The app still runs without it; the Browser tab shows an install hint.
- **Terminal isn't interactive (TUIs don't render)** — install `pyte` + `pywinpty` (Windows) or `ptyprocess` (POSIX). Without them, the terminal falls back to a non-interactive line-buffered mode.
- **App appears frozen on Windows when launched via `py main.py`** — that's the console's Quick Edit mode (clicking selects text and pauses the process). Familiar disables it automatically; if it still happens, press Enter in the console, or launch via `START.bat` (no console).
- **Code changes don't take effect** — Familiar is single-instance. Fully quit it before relaunching so the new code loads.
- **Semantic search / memory returns nothing** — embeddings need a working `embedding_model` and a key for its provider; otherwise recall degrades to full-text only.

---

## Development

- **Add a tool**: drop a module in `tools/`, call `registry.register(...)` at import time, and add the import to `tools/__init__.py`. The hot-reload watcher picks up edits live. Accept a `ctx` parameter to receive the abort signal and progress callback.
- **Tools are grouped** into routing categories in `core/agent.py` (`_TOOL_CATEGORIES`) — add new tools to the right category so the router can find them.
- **Linting**: `ruff` runs automatically after edits; the project ships a `.ruff_cache`.
- **Debug**: the per-conversation **Debug panel** shows the exact, untruncated context sent to the model each turn — the fastest way to see what the agent actually saw.

---

*Familiar — your terminal-dwelling, tool-wielding, memory-keeping desktop familiar.*
