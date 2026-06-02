"""
Command safety analyzer — parses shell commands to identify risks before execution.

Inspired by opencode-dev's tree-sitter-based bash.ts analyzer. Since we're in
Python (not Node), we use shlex + regex-based parsing instead of tree-sitter WASM.
Achieves the same safety outcomes:
  - Identifies file-mutating commands and their target paths
  - Detects path escapes (../../ outside workspace)
  - Classifies risk: safe / needs_approval / blocked
  - Extracts structured info for permission requests
"""

import os
import re
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


# ── Command classifications ─────────────────────────────────────────────

# Commands that mutate files — we extract their path arguments
FILE_MUTATING_CMDS = {
    # Unix
    "rm", "rmdir", "mv", "cp", "chmod", "chown", "touch", "mkdir",
    "truncate", "shred", "dd",
    # Windows
    "del", "erase", "rd", "move", "copy", "xcopy", "robocopy",
    "mklink", "attrib", "icacls", "takeown",
}

# Commands that are always dangerous regardless of arguments
ALWAYS_DANGEROUS = {
    # System destruction
    "format", "diskpart", "fdisk", "mkfs",
    # Registry
    "reg", "regedit",
    # Process/service manipulation
    "taskkill", "sc", "net", "schtasks",
    # Package managers (can install malware)
    "pip install", "npm install", "gem install", "cargo install",
}

# Git commands that are destructive
GIT_DESTRUCTIVE = {
    "git reset --hard", "git clean -f", "git clean -fd",
    "git checkout .", "git push --force", "git push -f",
    "git branch -D", "git rebase",
}

# Commands that can exfiltrate data
EXFIL_CMDS = {
    "curl", "wget", "invoke-webrequest", "iwr",
    "scp", "rsync", "ftp", "sftp",
    "nc", "ncat", "netcat",
}

# Safe commands that never need approval
SAFE_CMDS = {
    # Read-only
    "ls", "dir", "cat", "type", "head", "tail", "less", "more",
    "find", "where", "which", "whereis",
    "grep", "rg", "ag", "findstr",
    "wc", "sort", "uniq", "diff", "cmp",
    "file", "stat", "du", "df",
    # Info
    "echo", "printf", "date", "whoami", "hostname", "uname",
    "pwd", "cd", "pushd", "popd",
    # Dev tools (read-only)
    "git status", "git log", "git diff", "git branch", "git show",
    "git remote", "git stash list",
    "python --version", "node --version", "npm --version",
    "pip list", "pip show", "pip freeze",
    # Build/test (generally safe)
    "python", "node", "npm test", "npm run", "pytest", "cargo test",
    "go test", "go build", "make", "cmake",
}


@dataclass
class CommandAnalysis:
    """Result of analyzing a shell command."""
    raw_command: str
    risk_level: str = "safe"  # "safe", "needs_approval", "blocked"
    reasons: list[str] = field(default_factory=list)
    mutated_paths: list[str] = field(default_factory=list)
    escaped_paths: list[str] = field(default_factory=list)  # Paths outside workspace
    commands_found: list[str] = field(default_factory=list)
    is_piped: bool = False
    is_chained: bool = False


# ── Parsing helpers ──────────────────────────────────────────────────────

def _split_pipeline(command: str) -> list[str]:
    """Split a command on pipes, respecting quotes."""
    # Simple split — doesn't handle all edge cases but covers common patterns
    parts = []
    current = []
    in_single = False
    in_double = False
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "'" and not in_double:
            in_single = not in_single
            current.append(ch)
        elif ch == '"' and not in_single:
            in_double = not in_double
            current.append(ch)
        elif ch == '|' and not in_single and not in_double:
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
        i += 1
    if current:
        parts.append("".join(current).strip())
    return [p for p in parts if p]


def _split_chain(command: str) -> list[str]:
    """Split command on && and ; operators, respecting quotes."""
    # Split on && and ; but not inside quotes
    parts = re.split(r'\s*(?:&&|;)\s*', command)
    return [p.strip() for p in parts if p.strip()]


def _tokenize(command: str) -> list[str]:
    """Tokenize a single command into arguments."""
    try:
        if IS_WINDOWS:
            # shlex doesn't handle Windows well, do basic split
            return command.split()
        return shlex.split(command)
    except ValueError:
        # Unbalanced quotes etc — fall back to basic split
        return command.split()


def _resolve_path(path_str: str, cwd: str) -> str:
    """Resolve a path argument to an absolute path."""
    # Expand ~ and environment variables
    expanded = os.path.expanduser(os.path.expandvars(path_str))
    if os.path.isabs(expanded):
        return os.path.normpath(expanded)
    return os.path.normpath(os.path.join(cwd, expanded))


def _is_path_escape(path: str, workspace: str) -> bool:
    """Check if a resolved path escapes outside the workspace."""
    try:
        # Normalize both paths for comparison
        norm_path = os.path.normpath(os.path.abspath(path)).lower()
        norm_ws = os.path.normpath(os.path.abspath(workspace)).lower()
        return not norm_path.startswith(norm_ws)
    except (ValueError, OSError):
        return True  # If we can't resolve it, assume escape


def _extract_path_args(tokens: list[str], cmd: str) -> list[str]:
    """Extract likely path arguments from a command's tokens.
    Skips flags (starting with -) and the command name itself."""
    paths = []
    skip_next = False
    for i, tok in enumerate(tokens):
        if i == 0:
            continue  # Skip the command name
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            # Some flags take a value argument
            if tok in ("-o", "-f", "--output", "--file", "--target", "--dest",
                       "-d", "--directory", "-C"):
                skip_next = True  # Next token is the flag's value
            continue
        # Looks like a path argument
        if tok and not tok.startswith("-"):
            paths.append(tok)
    return paths


# ── Main analysis function ──────────────────────────────────────────────

def analyze_command(command: str, workspace: str = "",
                    allowed_dirs: set[str] | None = None) -> CommandAnalysis:
    """Analyze a shell command for safety risks.

    Args:
        command: The raw command string.
        workspace: The workspace root directory. Paths outside this are flagged.
        allowed_dirs: Additional directories that are explicitly allowed.

    Returns:
        CommandAnalysis with risk level and details.
    """
    result = CommandAnalysis(raw_command=command)

    if not command.strip():
        return result

    # Check for pipes and chains
    result.is_piped = "|" in command
    result.is_chained = "&&" in command or ";" in command

    # Split into individual commands
    if result.is_chained:
        sub_commands = _split_chain(command)
    else:
        sub_commands = [command]

    # Further split each on pipes (we analyze the first command in each pipe)
    all_commands = []
    for sub in sub_commands:
        pipeline = _split_pipeline(sub)
        all_commands.extend(pipeline)

    workspace = workspace or os.getcwd()
    allowed = {os.path.normpath(os.path.abspath(d)).lower()
               for d in (allowed_dirs or set())}
    allowed.add(os.path.normpath(os.path.abspath(workspace)).lower())

    for cmd_str in all_commands:
        tokens = _tokenize(cmd_str)
        if not tokens:
            continue

        cmd_name = tokens[0].lower()
        result.commands_found.append(cmd_name)

        # Check full command prefixes against known categories
        cmd_lower = cmd_str.lower().strip()

        # Safe commands — skip further analysis
        if any(cmd_lower.startswith(safe) for safe in SAFE_CMDS):
            continue

        # Always dangerous
        if any(cmd_lower.startswith(d) for d in ALWAYS_DANGEROUS):
            result.risk_level = "needs_approval"
            result.reasons.append(f"Potentially dangerous command: {cmd_name}")

        # Git destructive
        if any(cmd_lower.startswith(g) for g in GIT_DESTRUCTIVE):
            result.risk_level = "needs_approval"
            result.reasons.append(f"Destructive git operation: {cmd_str[:60]}")

        # Exfiltration risk
        if cmd_name in EXFIL_CMDS:
            result.risk_level = max(result.risk_level, "needs_approval",
                                    key=lambda x: ["safe", "needs_approval", "blocked"].index(x))
            result.reasons.append(f"Network command that could exfiltrate data: {cmd_name}")

        # File-mutating commands — check path safety
        if cmd_name in FILE_MUTATING_CMDS:
            path_args = _extract_path_args(tokens, cmd_name)
            for path_arg in path_args:
                resolved = _resolve_path(path_arg, workspace)
                result.mutated_paths.append(resolved)

                if _is_path_escape(resolved, workspace):
                    # Check if it's in an allowed directory
                    norm_resolved = os.path.normpath(os.path.abspath(resolved)).lower()
                    in_allowed = any(norm_resolved.startswith(a) for a in allowed)
                    if not in_allowed:
                        result.escaped_paths.append(resolved)
                        result.risk_level = "needs_approval"
                        result.reasons.append(
                            f"File operation targets path outside workspace: {resolved}")

        # rm -rf / protection (always block)
        if cmd_name == "rm" and any(t in tokens for t in ["-rf", "-fr"]):
            for path_arg in _extract_path_args(tokens, cmd_name):
                resolved = _resolve_path(path_arg, workspace)
                # Block rm -rf on root-like paths
                if resolved in ("/", "C:\\", os.path.expanduser("~")):
                    result.risk_level = "blocked"
                    result.reasons.append(f"BLOCKED: rm -rf on critical path: {resolved}")

        # taskkill / kill / pkill — block image-name and pattern targeting.
        # `taskkill /IM <name>` kills EVERY process matching that image — if the
        # agent does this with python.exe / pythonw.exe / node.exe / cmd.exe it
        # will euthanize itself. The agent must always target a specific PID
        # (and even then we block our own PID below).
        if cmd_name in ("taskkill", "tskill"):
            tokens_lower = [t.lower() for t in tokens]
            if "/im" in tokens_lower or "-im" in tokens_lower:
                result.risk_level = "blocked"
                result.reasons.append(
                    "BLOCKED: taskkill /IM kills every process matching that "
                    "image name and can shut down the Agent itself. Use the "
                    "terminal tool's bg_action='kill' with the bg_id returned "
                    "by 'start', or taskkill with a specific /PID."
                )
            else:
                # /PID variants — block if it targets our own process
                for i, t in enumerate(tokens_lower):
                    if t in ("/pid", "-pid") and i + 1 < len(tokens):
                        try:
                            target_pid = int(tokens[i + 1])
                            if target_pid == os.getpid():
                                result.risk_level = "blocked"
                                result.reasons.append(
                                    f"BLOCKED: taskkill targets the Agent's own "
                                    f"PID ({target_pid}). This would shut the "
                                    f"Agent down."
                                )
                        except ValueError:
                            pass

        # POSIX pkill / killall — same family, same problem
        if cmd_name in ("pkill", "killall"):
            result.risk_level = "blocked"
            result.reasons.append(
                f"BLOCKED: {cmd_name} kills processes by name pattern and can "
                f"easily target the Agent itself or unrelated processes. Use "
                f"the terminal tool's bg_action='kill' with the bg_id, or kill "
                f"a specific PID with `kill <pid>`."
            )

        # `kill <pid>` — block self-pid
        if cmd_name == "kill":
            for tok in tokens[1:]:
                if tok.startswith("-"):
                    continue
                try:
                    target_pid = int(tok)
                    if target_pid == os.getpid():
                        result.risk_level = "blocked"
                        result.reasons.append(
                            f"BLOCKED: kill targets the Agent's own PID "
                            f"({target_pid})."
                        )
                except ValueError:
                    pass

    return result


def format_analysis(analysis: CommandAnalysis) -> str:
    """Format a CommandAnalysis into a human-readable string for the permission prompt."""
    if analysis.risk_level == "safe":
        return ""

    lines = [f"Command risk: {analysis.risk_level.upper()}"]
    for reason in analysis.reasons:
        lines.append(f"  - {reason}")
    if analysis.mutated_paths:
        lines.append(f"  Paths modified: {', '.join(analysis.mutated_paths[:5])}")
    if analysis.escaped_paths:
        lines.append(f"  Outside workspace: {', '.join(analysis.escaped_paths[:5])}")
    return "\n".join(lines)
