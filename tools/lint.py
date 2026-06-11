"""
Auto-linter — runs language-specific syntax + semantic checks after file ops.

Returns *structured* diagnostics (not just a pass/fail string) so tool results
can surface a top-level `error`/`diagnostics` field that the model is forced
to address rather than burying it inside a status message.

Python uses a cascade: ast.parse (always available, free) → ruff (fast, catches
unused/undefined/imports) → pyflakes (fallback) → py_compile (last resort).
ast.parse alone catches indentation errors that py_compile can also catch, but
ruff/pyflakes are required to catch missing imports and undefined names —
the exact failures the model was glossing over.
"""

import ast
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Subprocess linters for non-Python languages. {file} is replaced with the path.
# Python is handled separately by _check_python so we can use ast + ruff/pyflakes.
LINTERS = {
    ".js":   ["node", "--check", "{file}"],
    ".ts":   ["npx", "tsc", "--noEmit", "--pretty", "{file}"],
    ".tsx":  ["npx", "tsc", "--noEmit", "--pretty", "--jsx", "react-jsx", "{file}"],
    ".go":   ["go", "vet", "{file}"],
    ".rs":   ["rustfmt", "--check", "{file}"],
    ".rb":   ["ruby", "-c", "{file}"],
    ".sh":   ["bash", "-n", "{file}"],
}

TIMEOUT = 30  # seconds

# Suppress the console window Windows otherwise allocates for console-subsystem
# children (ruff.exe, etc.) when the parent is a GUI process with no console.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# Cache which CLI tools are available (check once per session)
_available: dict[str, bool] = {}


def _is_available(cmd: str) -> bool:
    """Check if a command can be run. Cached.

    Uses the robust resolver for our own pip-installed tools (ruff, pyflakes,
    pylsp) so they're found even when Python's Scripts dir isn't on PATH; falls
    back to a plain PATH lookup for external tooling (node, go, rustfmt, …).
    """
    if cmd not in _available:
        from core.tool_resolver import resolve
        _available[cmd] = (resolve(cmd) is not None) or (shutil.which(cmd) is not None)
    return _available[cmd]


def _tool_argv(cmd: str) -> list[str]:
    """Resolved launch prefix for a console-script, or just [cmd] as a fallback."""
    from core.tool_resolver import resolve_argv
    return resolve_argv(cmd) or [cmd]


# ── Python checker cascade ──────────────────────────────────────────────

# Pyflakes/ruff line-prefixed message regex: "path:line:col: code? message"
_PY_DIAG_RE = re.compile(r"^[^:]+:(\d+):(?:\d+:)?\s*(\w+)?\s*(.+)$")


# Ruff/pyflakes codes that signal a genuine RUNTIME failure (NameError, syntax,
# etc.) — only these are "error". Everything else pyflakes/ruff emits (unused
# import F401, unused var F841, redefinition F811, f-string nits, style) is
# real-but-nonfatal and is classified "warning": it informs without blocking
# an edit or masquerading as breakage. This is what kills the F401-on-a-
# side-effect-import noise — an unused import never crashes a program, so it
# was wrong to surface it as a top-level error.
_PY_ERROR_PREFIXES = ("E9", "F82", "F70", "F831")
_PY_NONFATAL_HINTS = (
    "imported but unused",
    "assigned to but never used",
    "redefinition of unused",
    "imported but unused",
    "may be undefined, or defined from star imports",
)


def _py_severity(code: str | None, message: str) -> str:
    """Classify a Python diagnostic as 'error' (runtime-breaking) or 'warning'
    (real but nonfatal). Used for both ruff (has codes) and pyflakes (often
    code-less, so we fall back to message text)."""
    code = code or ""
    if code.startswith(_PY_ERROR_PREFIXES):
        return "error"
    if code:
        return "warning"
    msg = (message or "").lower()
    if any(hint in msg for hint in _PY_NONFATAL_HINTS):
        return "warning"
    return "error"


def _check_python(path: str) -> dict:
    """Run the Python validation cascade. Returns the structured result dict.

    ast.parse is always run first — it's free and catches every SyntaxError
    (incl. indentation) without spawning a subprocess. Then we layer on a
    semantic check (ruff or pyflakes) to catch the failures py_compile misses:
    missing imports, undefined names, unused variables.
    """
    diags: list[dict] = []

    # 1. AST parse — catches all syntax + indentation errors, instant
    try:
        src = Path(path).read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "diagnostics": [{
            "source": "io", "severity": "error", "line": None,
            "code": None, "message": f"Could not read file: {e}",
        }]}

    try:
        ast.parse(src, filename=path)
    except SyntaxError as e:
        diags.append({
            "source": "ast", "severity": "error", "line": e.lineno,
            "code": "SyntaxError",
            "message": (e.msg or "SyntaxError").strip(),
        })
        # Syntax error means semantic checks will be noisy/useless — return now.
        return {"ok": False, "diagnostics": diags}

    # 2. Import availability check — find packages referenced in `import` /
    #    `from X import Y` statements that aren't findable in the current env.
    #    Ruff can't catch this (it doesn't know what's installed); this is the
    #    one class of runtime failure that slips through otherwise.
    diags.extend(_check_python_imports(src, path))

    # 3. Semantic check — prefer ruff (fast, comprehensive), fall back to pyflakes.
    #    If NEITHER is installed, return a hard error rather than silently passing.
    if _is_available("ruff"):
        semantic_ran = True
        diags.extend(_run_ruff(path))
    elif _is_available("pyflakes"):
        semantic_ran = True
        diags.extend(_run_pyflakes(path))
    else:
        # Neither tool is installed — this is a misconfigured environment, not
        # a code problem. Return a hard error so the caller surfaces it clearly.
        diags.append({
            "source": "lint",
            "severity": "error",
            "line": None,
            "code": "MISSING_LINTER",
            "message": (
                "Neither `ruff` nor `pyflakes` is installed. "
                "Run: pip install ruff pyflakes  "
                "(see requirements.txt). Cannot validate Python semantics."
            ),
        })
        semantic_ran = False

    errors = [d for d in diags if d["severity"] == "error"]
    return {
        "ok": not errors,
        "diagnostics": diags,
        "semantic_check_ran": semantic_ran,
    }


def _check_python_imports(src: str, path: str) -> list[dict]:
    """Check that every top-level package imported by the file is findable in
    the current Python environment. Ruff/pyflakes only check for correctness
    within the source; they cannot know whether `import pandas` will succeed
    at runtime if pandas isn't installed. This fills that gap.

    Takes the already-parsed source string (ast.parse already passed) so we
    don't re-read the file. Returns error diagnostics for any missing package.
    """
    import importlib.util

    # Packages known to be stdlib aliases or always present — skip them to
    # avoid false positives on things like `__future__`, `typing_extensions`.
    _ALWAYS_SKIP = frozenset({
        "__future__", "typing_extensions", "typing", "_typeshed",
    })

    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError:
        return []  # ast.parse already caught this upstream

    missing: list[dict] = []
    seen: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or node.level > 0:
                # relative import — can't resolve without package context
                continue
            names = [node.module.split(".")[0]]
        else:
            continue

        for pkg in names:
            if pkg in seen or pkg in _ALWAYS_SKIP:
                continue
            seen.add(pkg)
            if pkg in sys.stdlib_module_names:
                continue
            try:
                spec = importlib.util.find_spec(pkg)
            except (ModuleNotFoundError, ValueError):
                spec = None
            if spec is None:
                line = getattr(node, "lineno", None)
                missing.append({
                    "source": "import-check",
                    "severity": "error",
                    "line": line,
                    "code": "MISSING_PACKAGE",
                    "message": (
                        f"Package `{pkg}` is not installed in the current "
                        f"Python environment. Run: pip install {pkg}"
                    ),
                })

    return missing


def _run_ruff(path: str) -> list[dict]:
    """Invoke `ruff check --output-format=json` and parse diagnostics."""
    try:
        result = subprocess.run(
            _tool_argv("ruff") + ["check", "--output-format=json", "--exit-zero", str(path)],
            capture_output=True, text=True, timeout=TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    except Exception:
        return []

    out = (result.stdout or "").strip()
    if not out:
        return []
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return []

    diags: list[dict] = []
    for it in items:
        code = it.get("code") or ""
        diags.append({
            "source": "ruff",
            "severity": _py_severity(code, it.get("message", "")),
            "line": (it.get("location") or {}).get("row"),
            "code": code,
            "message": it.get("message", ""),
        })
    return diags


def _run_pyflakes(path: str) -> list[dict]:
    """Invoke pyflakes and parse its `path:line:col: message` output."""
    try:
        result = subprocess.run(
            _tool_argv("pyflakes") + [str(path)],
            capture_output=True, text=True, timeout=TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    except Exception:
        return []

    text = (result.stdout or "") + (result.stderr or "")
    diags: list[dict] = []
    for line in text.splitlines():
        m = _PY_DIAG_RE.match(line)
        if not m:
            continue
        diags.append({
            "source": "pyflakes",
            "severity": _py_severity(m.group(2), m.group(3)),
            "line": int(m.group(1)),
            "code": m.group(2) or None,
            "message": m.group(3).strip(),
        })
    return diags


# ── Generic subprocess linter (non-Python) ──────────────────────────────

def _check_generic(path: str, cmd_template: list[str]) -> dict:
    """Run a non-Python linter and translate its exit code into diagnostics."""
    binary = cmd_template[0]
    if not _is_available(binary):
        return {"ok": True, "diagnostics": [], "skipped": True}

    cmd = [arg.replace("{file}", str(path)) for arg in cmd_template]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=TIMEOUT,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "diagnostics": [{
            "source": binary, "severity": "error", "line": None,
            "code": None, "message": f"Lint timed out after {TIMEOUT}s",
        }]}
    except FileNotFoundError:
        _available[binary] = False
        return {"ok": True, "diagnostics": [], "skipped": True}
    except Exception as e:
        return {"ok": False, "diagnostics": [{
            "source": binary, "severity": "error", "line": None,
            "code": None, "message": f"Lint error: {e}",
        }]}

    if result.returncode == 0:
        return {"ok": True, "diagnostics": []}

    # Couldn't parse line numbers reliably across linters, so emit one
    # "blob" diagnostic with the raw output. Better than nothing.
    raw = (result.stderr or result.stdout or "").strip()
    if not raw:
        raw = f"Lint failed with exit code {result.returncode}"
    return {"ok": False, "diagnostics": [{
        "source": binary, "severity": "error", "line": None,
        "code": None, "message": raw,
    }]}


# ── Public API ──────────────────────────────────────────────────────────

def lint_file(path: str) -> dict | None:
    """Run a syntax/semantic check on *path*. Returns:

        None                                             — no checker available
        {"ok": True,  "diagnostics": []}                  — clean
        {"ok": False, "diagnostics": [ {…}, … ]}          — issues found

    Each diagnostic is {source, severity, line, code, message}.
    `severity` is "error" or "warning". Tools should treat any "error"
    diagnostic as a hard signal worth surfacing as a top-level error.
    """
    ext = Path(path).suffix.lower()
    if ext in {".py", ".pyi"}:
        return _check_python(path)
    cmd_template = LINTERS.get(ext)
    if not cmd_template:
        return None
    return _check_generic(path, cmd_template)


def validate_file(path: str) -> dict:
    """Run lint + LSP on *path* and merge results into one structured dict.

    Returns: {"ok": bool, "diagnostics": [...], "semantic_check_ran": bool|None}

    This is what file_write/file_edit/apply_patch/multi_edit should call —
    one place to wire up post-edit validation, one shape to consume.
    """
    lint = lint_file(path)
    diags: list[dict] = []
    semantic_ran: bool | None = None

    if lint is not None:
        diags.extend(lint.get("diagnostics", []) or [])
        semantic_ran = lint.get("semantic_check_ran")

    # LSP layer — pyright/pylsp/tsserver/gopls/rust-analyzer if available.
    try:
        from core.lsp_client import lsp_manager
        workspace = str(Path(path).parent)
        for parent in Path(path).resolve().parents:
            if any((parent / m).exists() for m in (
                ".git", "pyproject.toml", "package.json",
                "Cargo.toml", "go.mod", "setup.py",
            )):
                workspace = str(parent)
                break
        lsp_diags = lsp_manager.validate_file(path, workspace) or []
        for d in lsp_diags:
            diags.append({
                "source": "lsp",
                "severity": d.get("severity", "info"),
                "line": d.get("line"),
                "code": d.get("code"),
                "message": d.get("message", ""),
            })
    except Exception:
        pass  # LSP optional — never block on it

    errors = [d for d in diags if d.get("severity") == "error"]
    return {
        "ok": not errors,
        "diagnostics": diags,
        "semantic_check_ran": semantic_ran,
    }


def _diag_signature(d: dict) -> tuple:
    """A line-INDEPENDENT identity for a diagnostic. Line numbers shift when
    you insert/delete code, so they can't be part of the identity — otherwise
    every pre-existing error below an inserted line would look 'new'. Identity
    is (source, code, normalized message). Trailing quoted names ('foo') are
    kept because they distinguish 'undefined name x' from 'undefined name y'.
    """
    msg = (d.get("message") or "").strip()
    # Collapse run-of-the-mill numeric noise (column counts, etc.) so the same
    # logical message matches even if a number wiggles.
    msg = re.sub(r"\b\d+\b", "#", msg)
    return (d.get("source"), d.get("code"), msg)


def snapshot_diagnostics(path: str) -> list[dict] | None:
    """Capture a file's current diagnostics BEFORE an edit, so the post-edit
    delta can show only what the edit introduced. Returns None if the file
    doesn't exist yet (a fresh Add) — the caller treats that as 'no baseline'.
    """
    if not Path(path).exists():
        return None
    try:
        return validate_file(path).get("diagnostics", []) or []
    except Exception:
        return None


def diff_diagnostics(before: list[dict] | None,
                     after: list[dict]) -> dict:
    """Compare a before/after diagnostic set and split 'after' into the errors
    the edit INTRODUCED versus ones that were already there.

    Matching is by line-independent signature and is multiset-aware: if a file
    had two identical pre-existing warnings and still has two, neither is 'new';
    if it now has three, exactly one is 'new'. Returns:

        {
          "introduced":  [diags the edit added],
          "introduced_errors": [subset with severity == error],
          "preexisting_count": N,        # how many we suppressed as not-yours
          "ok": bool,                    # True if the edit introduced no errors
        }
    """
    from collections import Counter
    before_counts: Counter = Counter(
        _diag_signature(d) for d in (before or []))
    introduced: list[dict] = []
    for d in after:
        sig = _diag_signature(d)
        if before_counts.get(sig, 0) > 0:
            before_counts[sig] -= 1   # account for one pre-existing instance
        else:
            introduced.append(d)
    introduced_errors = [d for d in introduced
                         if d.get("severity") == "error"]
    preexisting = len(after) - len(introduced)
    return {
        "introduced": introduced,
        "introduced_errors": introduced_errors,
        "preexisting_count": preexisting,
        "ok": not introduced_errors,
    }


def build_validation_result(path: str, status: str,
                            baseline: list[dict] | None = None,
                            error_prefix: str = "") -> dict:
    """Shared post-edit validation shape for every edit tool (file_write,
    file_edit, multi_edit, apply_patch). Runs validate_file, and — when a
    pre-edit *baseline* is supplied — surfaces only the errors THIS edit
    introduced, suppressing the file's pre-existing noise.

    error_prefix may contain "{n}", replaced with the introduced-error count.
    Returns a result dict (caller json.dumps it) with `status`, optional
    `error`+`diagnostics` (introduced errors only), optional `warnings`, and
    an optional `note`.
    """
    result: dict = {"status": status}
    try:
        validation = validate_file(path)
    except Exception:
        return result
    after = validation.get("diagnostics", []) or []

    if baseline is None:
        errors = [d for d in after if d.get("severity") == "error"]
        suppressed = 0
    else:
        delta = diff_diagnostics(baseline, after)
        errors = delta["introduced_errors"]
        suppressed = delta["preexisting_count"]
    warnings = [d for d in after if d.get("severity") == "warning"]

    if errors:
        if error_prefix:
            prefix = error_prefix.replace("{n}", str(len(errors)))
        else:
            prefix = f"This edit introduced {len(errors)} new error(s). "
        result["error"] = (
            prefix + "Re-read, fix the issues below, and edit again."
            + ("" if baseline is None else
               " (Pre-existing errors are not shown.)")
        )
        result["diagnostics"] = errors
    elif warnings:
        result["warnings"] = warnings[:10]

    if suppressed:
        result["note"] = (
            f"{suppressed} pre-existing diagnostic(s) left as-is "
            "(not introduced by this edit)."
        )
    if validation.get("semantic_check_ran") is False and \
            Path(path).suffix.lower() in {".py", ".pyi"}:
        prior = result.get("note", "")
        sem = ("Python semantic check skipped (install `ruff` or `pyflakes` "
               "to catch missing imports / undefined names).")
        result["note"] = (prior + " " + sem).strip() if prior else sem
    return result


def safe_write_text(path: str, content: str, encoding: str = "utf-8") -> str | None:
    """Write *content* to *path* and verify by readback.

    Returns None on success, or an error string if the on-disk content
    differs from what we just wrote. This catches the silent-failure class
    where another process (file watcher, IDE autosave, hot-reload) overwrites
    the file between our write and the model's next turn.
    """
    p = Path(path)
    try:
        p.write_text(content, encoding=encoding)
    except Exception as e:
        return f'Failed to write "{path}": {e}'

    # Readback verify — same encoding, no normalization.
    try:
        actual = p.read_text(encoding=encoding, errors="replace")
    except Exception as e:
        return f'Wrote "{path}" but readback failed: {e}'

    if actual != content:
        # Truncate the diff hint so we don't blow out the tool result.
        return (
            f'Wrote "{path}" but on-disk content differs from what was sent. '
            f"Likely cause: another process (file watcher, IDE, autosave) "
            f"overwrote it. Expected {len(content)} chars, found {len(actual)}."
        )
    return None


def format_lint_result(lint: dict | None, path: str) -> str:
    """Backward-compatible textual summary. Kept for callers that still want
    a one-line status string (e.g. event_bus cache-warmer, legacy log lines).

    New code should prefer `validate_file()` + a structured error field on
    the tool result.
    """
    if lint is None:
        return ""
    if lint.get("ok"):
        return " Lint: ok"
    diags = lint.get("diagnostics", [])
    if not diags:
        return " Lint: failed (no detail)"
    errors = [d for d in diags if d.get("severity") == "error"]
    shown = errors[:3] if errors else diags[:3]
    parts = []
    for d in shown:
        loc = f"L{d['line']}: " if d.get("line") else ""
        code = f"[{d['code']}] " if d.get("code") else ""
        parts.append(f"{loc}{code}{d.get('message', '')}")
    extra = ""
    if len(diags) > len(shown):
        extra = f" (+{len(diags) - len(shown)} more)"
    return " Lint issues: " + "; ".join(parts) + extra


# ── Agent-callable tool ─────────────────────────────────────────────────

def lint(path: str) -> str:
    """
    Run a lint/syntax check on a file and return JSON with structured diagnostics.

    Returns:
        {"ok": true, "diagnostics": []}                      — clean
        {"ok": false, "diagnostics": [...]}                  — issues found
        {"skipped": true, "reason": "..."}                   — no linter for this type
    """
    result = lint_file(path)
    if result is None:
        ext = Path(path).suffix.lower()
        return json.dumps({"skipped": True,
                           "reason": f"No linter configured for '{ext}' files"})
    return json.dumps(result)


try:
    from tools.registry import registry
    registry.register(
        name="lint",
        description=(
            "Syntax + semantic check.\n"
            "- .py: ast.parse → ruff (preferred) | pyflakes (fallback). Catches missing imports, undefined names, indent errors.\n"
            "- .js/.ts/.tsx/.go/.rs/.rb/.sh: language-native check.\n"
            "- → {ok, diagnostics:[{source,severity,line,code,message}]} | {skipped:true}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute file path.",
                },
            },
            "required": ["path"],
        },
        execute=lint,
    )
except Exception:
    pass
