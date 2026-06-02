"""
Terminal tool - execute shell commands with live output streaming.
Uses cmd on Windows, bash on Linux/macOS.

Now context-aware: accepts ToolContext for abort signals, live metadata
streaming, and permission checks for dangerous commands.
"""

import json
import signal
import subprocess
import sys
import os
import shlex
import threading
import queue
from tools.registry import registry

IS_WINDOWS = sys.platform == "win32"
DEFAULT_TIMEOUT = 120   # Raised from 30 to match opencode-dev (2 minutes)

# On Windows we spawn agent subprocesses in their own process group so we can
# deliver CTRL_BREAK_EVENT (the only signal the Win32 console API will route
# to a foreign-console process). Without this flag, send_signal does nothing.
_POPEN_GROUP_FLAGS = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if IS_WINDOWS else {}
)

# Shared output queue — UI polls this for live terminal lines
_output_queue: queue.Queue | None = None
_active_command: str = ""

# Currently-running foreground agent subprocess. Cleared when the wait loop
# returns. Used by interrupt_running_agent_process / shutdown_all_processes
# to reach the live foreground command.
_active_foreground_proc: subprocess.Popen | None = None

# Background processes are now real workspace terminal tabs — their registry
# lives in tools.workspace_terminal.bg_bridge, not here.


def _signal_proc(proc: subprocess.Popen) -> bool:
    """Send an interrupt to an agent-spawned subprocess. Returns True if a
    live process was signaled. Uses CTRL_BREAK_EVENT on Windows (requires the
    process to have been started with CREATE_NEW_PROCESS_GROUP) and SIGINT on
    POSIX. Falls back to kill() if the polite signal fails."""
    if proc is None or proc.poll() is not None:
        return False
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        return True
    except Exception:
        try:
            proc.kill()
            return True
        except Exception:
            return False


def _kill_proc_tree(proc: subprocess.Popen) -> None:
    """Kill a subprocess and ALL of its descendants. Critical on Windows because
    we spawn agent commands via `cmd /c <cmd>` — proc.kill() only terminates
    cmd.exe and orphans whatever it launched (python.exe etc). Uses
    `taskkill /T /F` to nuke the tree."""
    if proc is None:
        return
    if proc.poll() is not None:
        return
    pid = proc.pid
    try:
        if IS_WINDOWS and pid:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,  # no popup console
                timeout=5,
            )
        else:
            proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def shutdown_all_processes() -> dict:
    """Kill every agent-spawned subprocess. Called at app shutdown.

    Foreground: Popen children that still have an active wait loop get a tree
    kill. Background: tabs get closed (each tab's stop() runs taskkill /T /F
    on its shell, reaping descendants)."""
    fg_killed = 0
    fg = _active_foreground_proc
    if fg is not None and fg.poll() is None:
        _kill_proc_tree(fg)
        fg_killed = 1

    bg_killed = 0
    try:
        from tools.workspace_terminal import bg_bridge
        bg_killed = bg_bridge.shutdown_all()
    except Exception:
        pass

    return {"foreground_killed": fg_killed, "background_killed": bg_killed}


def interrupt_running_agent_process() -> dict:
    """Ctrl+C handler for the workspace terminal.

    With background processes now living in their own tabs, Ctrl+C *in* a
    bg tab is handled by the tab itself (it sends Ctrl+Break to its own
    shell, which reaches the bg command). This function only handles the
    foreground agent subprocess case — agent-spawned Popen that's still in
    its wait loop. Returns whether anything was signaled."""
    fg = _active_foreground_proc
    if fg is not None and fg.poll() is None:
        _kill_proc_tree(fg)
        return {"signaled": True, "target": "foreground", "bg_id": None}
    return {"signaled": False, "target": "none", "bg_id": None}


def get_output_queue() -> queue.Queue | None:
    return _output_queue


def get_active_command() -> str:
    return _active_command


def prepare_output_queue(command: str):
    """Pre-create the output queue before the tool runs.
    Called from the main thread so the UI widget can start polling immediately."""
    global _output_queue, _active_command
    _output_queue = queue.Queue()
    _active_command = command


def _start_bg_process(command: str, cwd: str = None) -> dict:
    """Start a background process by spawning a dedicated workspace terminal
    tab and writing the command into its shell. Returns the bg_id (= tab id).
    The user can see it, type into it, and close it like a real terminal —
    closing the tab kills the process tree.

    Replaces the old Popen-based path so 'background' no longer means
    'invisible' or 'parallel universe'."""
    from tools.workspace_terminal import bg_bridge
    result = bg_bridge.start_bg(command, cwd or "")
    if result.get("error"):
        return result
    return {
        "bg_id": result["bg_id"],
        "command": result["command"],
        "tab": result.get("tab", ""),
        "note": (
            "Background process is running in its own workspace terminal tab. "
            "The user can see it. Use bg_action='kill' with this bg_id to stop "
            "it (closes the tab + kills the process tree). bg_action='check' "
            "returns recent tab output."
        ),
    }


def _check_bg_process(bg_id: int) -> dict:
    """Check status of a background process tab."""
    from tools.workspace_terminal import bg_bridge
    return bg_bridge.check_bg(bg_id)


def _kill_bg_process(bg_id: int) -> dict:
    """Stop a background process by closing its workspace terminal tab.
    The shell's `taskkill /T /F` (in IntegratedTerminalSession.stop) reaps
    the whole subprocess tree."""
    from tools.workspace_terminal import bg_bridge
    return bg_bridge.kill_bg(bg_id)


def _list_bg_processes() -> dict:
    """List all background process tabs."""
    from tools.workspace_terminal import bg_bridge
    return bg_bridge.list_bg()


# ── Dangerous command detection ─────────────────────────────────────────

_DESTRUCTIVE_PATTERNS = {
    "rm -rf", "rm -r", "del /s", "del /q /s", "rmdir /s", "rd /s",
    "git reset --hard", "git clean -f", "git checkout .",
    "format ", "diskpart", "reg delete",
}

# Approval callback — set by the UI to prompt user for dangerous commands
_approval_callback = None


def set_approval_callback(fn):
    """Set a callback for dangerous command approval. Called from UI thread."""
    global _approval_callback
    _approval_callback = fn


# ── Main execution ──────────────────────────────────────────────────────

_MAX_OUTPUT_PREVIEW = 30_000  # Metadata preview cap (30KB like opencode-dev)


# Commands that should be transparently converted to dedicated tool calls
_SEARCH_COMMANDS = {"rg", "grep", "findstr", "ag", "ack"}
_READ_COMMANDS = {"cat", "type", "head", "tail", "less", "more"}

# Unix-only commands that should be converted to Windows equivalents
# Maps unix_cmd → (windows_cmd_template, description)
# The template receives the original args as a string; use {args} placeholder.
_UNIX_TO_WIN = {
    "ls":   ("dir {args}",   "dir"),
    "pwd":  ("cd",           "cd"),
    "cp":   ("copy {args}",  "copy"),
    "mv":   ("move {args}",  "move"),
    "rm":   ("del {args}",   "del"),
    "mkdir":("mkdir {args}", "mkdir"),
    "touch":('type nul > {args}', "type nul >"),
}

# rg/grep flags that consume the next argument as a value
_RG_VALUE_FLAGS_LONG = {
    "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not",
    "--type-add", "--max-count", "--max-columns", "--after-context",
    "--before-context", "--context", "--sort", "--sortr", "--threads",
    "--encoding", "--replace", "--max-depth", "--pre", "--pre-glob",
    "--field-match-separator", "--max-filesize", "--colors", "--color",
}
_RG_VALUE_FLAGS_SHORT = set("egtTmMABCEjd")  # short flags that take a value


def _cmd_name(raw: str) -> str:
    """Normalize a command name: strip path, lower-case, drop .exe."""
    name = raw.replace("\\", "/").split("/")[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _strip_quotes(s: str) -> str:
    """Strip a single layer of surrounding quotes (added by shlex posix=False)."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


# Shell operators — stop treating positional args once we hit one of these
_SHELL_OPS = {"|", "||", "&", "&&", ">", ">>", "<", "<<"}


def _parse_rg_args(args: list[str]) -> dict:
    """
    Parse rg / grep / ag / ack arguments into a normalized dict.
    Returns: {pattern, paths, glob} — all may be None/empty.
    """
    pattern = None
    explicit_pattern = None
    paths: list[str] = []
    glob = None

    i = 0
    while i < len(args):
        arg = args[i]

        # End-of-flags marker
        if arg == "--":
            i += 1
            for rem in args[i:]:
                if explicit_pattern is None and pattern is None:
                    pattern = rem
                else:
                    paths.append(rem)
            break

        if arg.startswith("--"):
            # Long flag with embedded value: --glob=*.py
            if "=" in arg:
                key, val = arg.split("=", 1)
                val = _strip_quotes(val)
                if key in ("--regexp", "-e"):
                    explicit_pattern = val
                elif key in ("--glob", "--iglob"):
                    glob = val
                # all other --key=val: ignore the value, nothing to skip
            else:
                if arg in ("--regexp", "-e"):
                    i += 1
                    if i < len(args):
                        explicit_pattern = _strip_quotes(args[i])
                elif arg in ("--glob", "--iglob"):
                    i += 1
                    if i < len(args):
                        glob = _strip_quotes(args[i])
                elif arg in _RG_VALUE_FLAGS_LONG:
                    i += 1  # skip value arg

        elif arg.startswith("-") and len(arg) > 1 and arg != "-":
            # Short flags, possibly combined: -rni, -e pattern, -g glob
            j = 1
            while j < len(arg):
                ch = arg[j]
                rest = arg[j + 1:]

                if ch == "e":
                    if rest:
                        explicit_pattern = _strip_quotes(rest)
                    elif i + 1 < len(args):
                        i += 1
                        explicit_pattern = _strip_quotes(args[i])
                    j = len(arg)  # consumed rest

                elif ch == "g":
                    if rest:
                        glob = _strip_quotes(rest)
                    elif i + 1 < len(args):
                        i += 1
                        glob = _strip_quotes(args[i])
                    j = len(arg)

                elif ch in _RG_VALUE_FLAGS_SHORT:
                    # Consumes rest of cluster or next arg as value
                    if rest:
                        pass  # rest is the value, skip it
                    elif i + 1 < len(args):
                        i += 1
                    j = len(arg)

                else:
                    j += 1  # boolean flag, move to next char

        else:
            # Stop at shell operators — everything after belongs to a piped command
            if arg in _SHELL_OPS:
                break
            # Positional argument — strip surrounding quotes added by shlex posix=False
            val = _strip_quotes(arg)
            if explicit_pattern is None and pattern is None:
                pattern = val
            else:
                paths.append(val)

        i += 1

    return {
        "pattern": _strip_quotes(explicit_pattern) if explicit_pattern else pattern,
        "paths": paths,
        "glob": _strip_quotes(glob) if glob else glob,
    }


def _resolve_path(raw: str, cwd: str | None) -> str:
    """Resolve a path argument relative to cwd, normalising '.' → cwd.
    On Windows, os.path.abspath() binds Unix-style /foo paths to the current drive."""
    if raw in (".", "./", ".\\"):
        return cwd or os.getcwd()
    if os.path.isabs(raw):
        # abspath resolves /foo → C:\foo on Windows (binds to current drive root)
        return os.path.abspath(raw)
    return os.path.join(cwd or os.getcwd(), raw)


def _try_convert_command(command: str, cwd: str | None) -> str | None:
    """
    Try to transparently convert a search/read shell command into a dedicated
    tool call.  Returns the tool's result JSON on success, or None if the
    command is not in a recognised category (let it fall through to normal
    execution).  Returns an error JSON if the command is recognised but
    unparseable (prevents unsafe execution while explaining what went wrong).
    """
    try:
        parts = shlex.split(command, posix=(sys.platform != "win32"))
    except ValueError:
        parts = command.split()

    if not parts:
        return None

    cmd = _cmd_name(parts[0])
    args = parts[1:]

    # ── Search commands → grep tool ──────────────────────────────────────
    if cmd in _SEARCH_COMMANDS:
        parsed = _parse_rg_args(args)
        pattern = parsed["pattern"]

        if not pattern:
            return json.dumps({
                "error": (
                    f"Intercepted '{cmd}' but could not extract a search pattern. "
                    "Use the grep tool directly: grep(pattern=..., path=..., glob=...)"
                ),
                "exit_code": -1,
            })

        kwargs: dict = {"pattern": pattern}

        if parsed["paths"]:
            kwargs["path"] = _resolve_path(parsed["paths"][0], cwd)
        elif cwd:
            kwargs["path"] = cwd

        if parsed["glob"]:
            kwargs["glob"] = parsed["glob"]

        try:
            result = registry.execute("grep", kwargs)
            # Annotate so the agent knows what happened
            data = json.loads(result)
            data["_note"] = (
                f"[auto-converted '{cmd}' → grep tool]"
            )
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc), "exit_code": -1})

    # ── Read commands → file_read tool ──────────────────────────────────
    if cmd in _READ_COMMANDS:
        # Collect positional file args; handle -n / --lines for head/tail
        files: list[str] = []
        limit: int | None = None
        i = 0
        while i < len(args):
            a = args[i]
            if a == "--":
                files.extend(args[i + 1:])
                break
            if cmd in ("head", "tail") and a in ("-n", "--lines"):
                i += 1
                if i < len(args):
                    try:
                        limit = abs(int(args[i]))
                    except ValueError:
                        pass
            elif cmd in ("head", "tail") and a.startswith("-n"):
                try:
                    limit = abs(int(a[2:]))
                except ValueError:
                    pass
            elif not a.startswith("-"):
                files.append(a)
            i += 1

        if not files:
            return json.dumps({
                "error": (
                    f"Intercepted '{cmd}' but no file path found. "
                    "Use the file_read tool directly: file_read(path=...)"
                ),
                "exit_code": -1,
            })

        file_path = _resolve_path(files[0], cwd)
        kwargs = {"path": file_path}
        if limit and cmd == "head":
            kwargs["limit"] = limit
        # tail: file_read doesn't support tail natively; omit limit and return whole file
        # (the agent will see all lines and can work from there)

        try:
            result = registry.execute("file_read", kwargs)
            data = json.loads(result)
            data["_note"] = f"[auto-converted '{cmd}' → file_read tool]"
            return json.dumps(data, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc), "exit_code": -1})

    # ── Unix-only commands → Windows equivalents ────────────────────────
    if cmd in _UNIX_TO_WIN:
        win_template, win_name = _UNIX_TO_WIN[cmd]
        # Resolve any Unix-style path args to Windows paths
        resolved_args = []
        for a in args:
            if a.startswith("-"):
                # Skip Unix flags (e.g. ls -la → dir, just drop flags)
                continue
            if a in _SHELL_OPS:
                break
            resolved_args.append(_resolve_path(_strip_quotes(a), cwd) if a not in (".", "..") else a)
        args_str = " ".join(f'"{a}"' if " " in a else a for a in resolved_args)
        win_cmd = win_template.format(args=args_str).strip()
        # Run the Windows equivalent via normal shell execution — fall through
        # by returning a special marker that signals "re-run with win_cmd"
        # Simpler: just execute it here directly
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        try:
            result = subprocess.run(
                ["cmd", "/c", win_cmd],
                capture_output=True, text=True,
                cwd=cwd or None, env=env,
                timeout=30, encoding="utf-8", errors="replace",
            )
            out = (result.stdout + result.stderr).strip()
            note = f"[auto-converted '{cmd}' → '{win_name}']"
            return json.dumps({
                "output": out or "(no output)",
                "exit_code": result.returncode,
                "_note": note,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc), "exit_code": 1})

    # ── Unix find → dir /s /b ────────────────────────────────────────────
    if cmd == "find":
        # find /path -name "*.txt" → dir /s /b "*.txt" in /path
        search_path = cwd or os.getcwd()
        name_pattern = "*"
        i = 0
        while i < len(args):
            a = args[i]
            if a in _SHELL_OPS:
                break
            if a in ("-name", "-iname"):
                i += 1
                if i < len(args):
                    name_pattern = _strip_quotes(args[i])
            elif a in ("-type", "-maxdepth", "-mindepth", "-not", "!", "-o", "-and"):
                i += 1  # skip value
            elif not a.startswith("-"):
                search_path = _resolve_path(_strip_quotes(a), cwd)
            i += 1
        win_cmd = f'dir /s /b "{name_pattern}"'
        env = os.environ.copy()
        try:
            result = subprocess.run(
                ["cmd", "/c", win_cmd],
                capture_output=True, text=True,
                cwd=search_path, env=env,
                timeout=30, encoding="utf-8", errors="replace",
            )
            out = (result.stdout + result.stderr).strip()
            return json.dumps({
                "output": out or "No files found.",
                "exit_code": result.returncode,
                "_note": f"[auto-converted 'find' → 'dir /s /b' in {search_path}]",
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"error": str(exc), "exit_code": 1})

    return None  # Not a command we handle — let terminal proceed normally


def terminal(command: str, timeout: int = None, cwd: str = None,
             background: bool = False, bg_action: str = "", bg_id: int = 0,
             to_workspace: bool = False, ctx=None) -> str:
    """Execute a shell command, or manage background processes.

    Args:
        ctx: Optional ToolContext with abort signal and metadata streaming.
    """
    global _output_queue, _active_command, _active_foreground_proc

    # Background process management
    if bg_action == "start" or background:
        result = _start_bg_process(command, cwd)
        return json.dumps(result)
    if bg_action == "check":
        return json.dumps(_check_bg_process(bg_id))
    if bg_action == "kill":
        return json.dumps(_kill_bg_process(bg_id))
    if bg_action == "list":
        return json.dumps(_list_bg_processes())

    timeout = timeout or DEFAULT_TIMEOUT

    # ── Transparently convert search/read commands → dedicated tools ──
    effective_cwd = cwd or (ctx.cwd if ctx else None)
    converted = _try_convert_command(command, effective_cwd)
    if converted is not None:
        return converted

    # ── Safety analysis ──
    from core.command_safety import analyze_command, format_analysis
    workspace = cwd or (ctx.cwd if ctx else os.getcwd())
    analysis = analyze_command(command, workspace=workspace)

    if analysis.risk_level == "blocked":
        return json.dumps({
            "output": f"Command blocked by safety analyzer:\n{format_analysis(analysis)}",
            "exit_code": -1,
        })

    if analysis.risk_level == "needs_approval":
        # Checkpoint before risky commands
        try:
            from core.checkpoints import checkpoint_manager
            checkpoint_manager.ensure_checkpoint(cwd or ".", f"before terminal: {command[:60]}")
        except Exception:
            pass

        # Ask for approval — prefer ctx.ask() if available, fallback to callback
        approved = True
        description = format_analysis(analysis)
        if ctx:
            approved = ctx.ask(description)
        elif _approval_callback:
            try:
                approved = _approval_callback(command)
            except Exception:
                pass
        if not approved:
            return json.dumps({"output": "Command blocked by user.", "exit_code": -1})

    if to_workspace:
        if background or bg_action:
            return json.dumps(
                {"error": "to_workspace cannot be used with background or bg_action."},
                ensure_ascii=False,
            )
        try:
            from tools.workspace_terminal import bridge as wt
            wt.send_requested.emit(command.strip())
            return json.dumps(
                {
                    "output": (
                        "Command was sent to the integrated workspace terminal (right panel, Terminal tab). "
                        "Watch that surface for output. Exit code is not tracked here — use the normal "
                        "terminal tool without to_workspace when you need a captured result."
                    ),
                    "exit_code": 0,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"output": str(e), "exit_code": 1}, ensure_ascii=False)

    if IS_WINDOWS:
        shell_cmd = ["cmd", "/c", command]
    else:
        shell_cmd = ["bash", "-c", command]

    _output_queue = queue.Queue()
    _active_command = command

    # Push initial metadata
    if ctx:
        ctx.metadata({"description": command[:80], "status": "running"})

    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        proc = subprocess.Popen(
            shell_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd or None,
            env=env,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            **_POPEN_GROUP_FLAGS,
        )

        # Register so the workspace terminal's Ctrl+C can find this process.
        _active_foreground_proc = proc

        output_lines = []
        output_chars = [0]    # Track total chars to enforce hard cap
        aborted = False
        expired = False

        MAX_OUTPUT_CHARS = 2_000_000  # 2MB hard cap — kills the process if exceeded
        OUTPUT_WARN = 500_000         # 500KB — start dropping lines from output_lines

        def _read_output():
            for line in iter(proc.stdout.readline, ""):
                output_chars[0] += len(line)

                # Always feed the UI queue (it has its own 10k line cap now)
                if _output_queue:
                    _output_queue.put(line)

                # Only keep lines in memory up to the warn threshold
                # (head+tail strategy: keep first lines, then only last lines)
                if output_chars[0] <= OUTPUT_WARN:
                    output_lines.append(line)
                elif len(output_lines) > 0 and not hasattr(_read_output, '_warned'):
                    _read_output._warned = True
                    output_lines.append(
                        f"\n... (output exceeded {OUTPUT_WARN // 1000}KB, "
                        f"truncating — full output in terminal tab) ...\n"
                    )

                # Hard kill if output is truly insane
                if output_chars[0] > MAX_OUTPUT_CHARS:
                    output_lines.append(
                        f"\n[KILLED: output exceeded {MAX_OUTPUT_CHARS // 1_000_000}MB — "
                        f"process terminated to protect system memory]\n"
                    )
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break

            proc.stdout.close()

        reader = threading.Thread(target=_read_output, daemon=True)
        reader.start()

        # ── Wait loop: race between exit / abort / timeout ──
        # Instead of a single blocking wait, poll periodically so we
        # can check the abort signal from ToolContext.
        poll_interval = 0.1  # 100ms
        elapsed = 0.0
        while True:
            retcode = proc.poll()
            if retcode is not None:
                break  # Process exited

            # Check abort signal from ToolContext
            if ctx and ctx.aborted:
                aborted = True
                proc.kill()
                proc.wait(timeout=3)
                break

            # Check timeout
            if elapsed >= timeout:
                expired = True
                proc.kill()
                proc.wait(timeout=3)
                break

            # Stream metadata updates periodically (every ~1s)
            if ctx and int(elapsed * 10) % 10 == 0 and elapsed > 0:
                preview = "".join(output_lines)
                if len(preview) > _MAX_OUTPUT_PREVIEW:
                    preview = preview[-_MAX_OUTPUT_PREVIEW:]
                ctx.metadata({
                    "output": preview,
                    "description": command[:80],
                    "status": "running",
                })

            threading.Event().wait(poll_interval)
            elapsed += poll_interval

        reader.join(timeout=2)
        output = "".join(output_lines).strip()
        exit_code = proc.returncode

        # ── Build metadata footer for abort/timeout ──
        meta_lines = []
        if expired:
            meta_lines.append(f"Command timed out after {timeout}s")
        if aborted:
            meta_lines.append("User aborted the command")
        if meta_lines:
            output += "\n\n<bash_metadata>\n" + "\n".join(meta_lines) + "\n</bash_metadata>"
            exit_code = exit_code if exit_code is not None else 124

        if not output:
            if exit_code == 0:
                output = "(command completed with no output)"
            else:
                output = f"(command exited with code {exit_code}, no output)"

        if exit_code and exit_code != 0 and not expired and not aborted:
            output += f"\n(exit code: {exit_code})"

        # Signal completion to UI queue
        if _output_queue:
            _output_queue.put(f"__EXIT_CODE__:{exit_code or 0}")
            _output_queue.put(None)
        _active_command = ""
        _active_foreground_proc = None

        # Final metadata push
        if ctx:
            ctx.metadata({
                "description": command[:80],
                "status": "done",
                "exit_code": exit_code,
            })

        return json.dumps({"output": output, "exit_code": exit_code or 0},
                          ensure_ascii=False)

    except Exception as e:
        if _output_queue:
            _output_queue.put("__EXIT_CODE__:1")
            _output_queue.put(None)
        _active_foreground_proc = None
        _active_command = ""
        if ctx:
            ctx.metadata({"status": "error", "description": str(e)[:80]})
        return json.dumps({"output": str(e), "exit_code": 1})


registry.register(
    name="terminal",
    description=(
        "Shell exec. Two modes:\n"
        " - FOREGROUND (default): you wait, you get {output, exit_code} back. "
        "   Runs silently — no UI mirroring, no tab spam. Use for short reads "
        "   (git status, dir, python --version, etc.).\n"
        " - BACKGROUND (background=true): the command opens its own dedicated "
        "   tab in the user's workspace terminal panel and runs there. The user "
        "   sees it live, can type into it, can close the tab to kill it. The "
        "   agent gets {bg_id} back immediately. Use for long-running scripts, "
        "   servers, watchers — anything the user expects to keep running.\n"
        "\n"
        "BACKGROUND CONTROL — read carefully:\n"
        " - bg_action='list' → all registered bg tabs (bg_id, command, running).\n"
        " - bg_action='check' bg_id=N → status + recent output of that tab.\n"
        " - bg_action='kill'  bg_id=N → close the tab (kills the process tree).\n"
        " - bg_id is the small integer (1, 2, 3, ...) returned by the start "
        "   call. NOT an OS pid. Closing the tab in the UI also kills the "
        "   process — so the user can stop things directly.\n"
        " - NEVER run raw `taskkill`, `kill`, `pkill`, or `killall`. They are "
        "   blocked when targeting by image name (e.g. taskkill /IM python.exe) "
        "   because that would also kill the Agent itself.\n"
        " - Before claiming a process has stopped, verify with bg_action='list' "
        "   or 'check'. Do not assume.\n"
        "\n"
        "OTHER: cwd=param always; cd-chains break it. Windows cmds only. "
        "timeout=120s (foreground)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute.",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds (foreground only). Default 120.",
            },
            "cwd": {
                "type": "string",
                "description": "Working directory.",
            },
            "background": {
                "type": "boolean",
                "description": "Start as background process (returns immediately with bg_id) so the agent isn't blocked. "
                "Output still streams live into the user's workspace terminal — background does not mean hidden. "
                "Use only for processes the user wants kept running (servers, watchers).",
            },
            "bg_action": {
                "type": "string",
                "enum": ["start", "check", "kill", "list"],
                "description": "Background process action. 'check' and 'kill' need bg_id. 'list' shows all. "
                "ALWAYS prefer bg_action='kill' over running taskkill/kill yourself — those can hit unrelated "
                "processes (including the Agent itself) and are blocked by the safety analyzer when targeting "
                "by image name.",
            },
            "bg_id": {
                "type": "integer",
                "description": "Background process ID — the SMALL integer from the 'bg_id' field of the start "
                "result (typically 1, 2, 3, ...). NOT the OS pid. The start result returns "
                "{\"bg_id\": 1, \"pid\": 9428, ...} — use 1, not 9428.",
            },
            "to_workspace": {
                "type": "boolean",
                "description": "If true, send the command to the integrated workspace terminal (shared with the user). "
                "Do not use with background/bg_action. Output appears in the UI, not in this tool's return value.",
            },
        },
        "required": ["command"],
    },
    execute=terminal,
)
