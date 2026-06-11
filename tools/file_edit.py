"""
File edit tool - targeted string replacement with fuzzy matching.
Uses a 6-strategy cascade (inspired by opencode-dev) to tolerate
minor whitespace, indentation, and escape differences from the LLM.
Falls back gracefully: exact match first, then increasingly lenient.
"""

import json
import re
from pathlib import Path
from tools.registry import registry


# ── Levenshtein distance (standard DP matrix) ──────────────────────────

def _levenshtein(a: str, b: str) -> int:
    """Compute the Levenshtein edit distance between two strings."""
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    # Use two-row optimization to save memory
    prev = list(range(lb + 1))
    curr = [0] * (lb + 1)
    for i in range(1, la + 1):
        curr[0] = i
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            curr[j] = min(
                prev[j] + 1,       # deletion
                curr[j - 1] + 1,   # insertion
                prev[j - 1] + cost  # substitution
            )
        prev, curr = curr, prev
    return prev[lb]


# ── Replacement strategies (generators yielding candidate match strings) ─

def _simple_replacer(content: str, old_string: str):
    """Strategy 1: Exact match — just yield the search string as-is."""
    yield old_string


def _line_trimmed_replacer(content: str, old_string: str):
    """Strategy 2: Match lines after trimming each line's whitespace.
    Yields the ORIGINAL (untrimmed) substring from content."""
    content_lines = content.split("\n")
    search_lines = old_string.split("\n")
    if not search_lines:
        return

    for i in range(len(content_lines) - len(search_lines) + 1):
        matched = True
        for j in range(len(search_lines)):
            if content_lines[i + j].strip() != search_lines[j].strip():
                matched = False
                break
        if matched:
            # Reconstruct the exact substring from original content
            start = sum(len(content_lines[k]) + 1 for k in range(i))
            end = start + sum(len(content_lines[i + k]) + 1
                              for k in range(len(search_lines))) - 1
            yield content[start:end]


# Similarity thresholds for block anchor matching
_SINGLE_CANDIDATE_THRESHOLD = 0.0   # Ultra-permissive for single match
_MULTI_CANDIDATE_THRESHOLD = 0.3    # Require 30% similarity when ambiguous


def _block_anchor_replacer(content: str, old_string: str):
    """Strategy 3: For 3+ line blocks, anchor on first and last line,
    then fuzzy-match the middle using Levenshtein distance."""
    search_lines = old_string.split("\n")
    if len(search_lines) < 3:
        return

    content_lines = content.split("\n")
    first_trimmed = search_lines[0].strip()
    last_trimmed = search_lines[-1].strip()

    # Find all positions where first AND last anchors match
    candidates = []
    for i in range(len(content_lines) - len(search_lines) + 1):
        if (content_lines[i].strip() == first_trimmed and
                content_lines[i + len(search_lines) - 1].strip() == last_trimmed):
            candidates.append(i)

    if not candidates:
        return

    middle_search = search_lines[1:-1]

    if len(candidates) == 1:
        # Single candidate — ultra-permissive threshold
        i = candidates[0]
        middle_content = content_lines[i + 1: i + len(search_lines) - 1]
        if middle_search:
            similarity = 0.0
            for s_line, c_line in zip(middle_search, middle_content):
                max_len = max(len(s_line), len(c_line), 1)
                dist = _levenshtein(s_line, c_line)
                similarity += 1.0 - dist / max_len
            similarity /= len(middle_search)
        else:
            similarity = 1.0

        if similarity >= _SINGLE_CANDIDATE_THRESHOLD:
            start = sum(len(content_lines[k]) + 1 for k in range(i))
            end = start + sum(len(content_lines[i + k]) + 1
                              for k in range(len(search_lines))) - 1
            yield content[start:end]
    else:
        # Multiple candidates — pick the best one above threshold
        best_sim = -1.0
        best_idx = -1
        for i in candidates:
            middle_content = content_lines[i + 1: i + len(search_lines) - 1]
            if middle_search:
                similarity = 0.0
                for s_line, c_line in zip(middle_search, middle_content):
                    max_len = max(len(s_line), len(c_line), 1)
                    dist = _levenshtein(s_line, c_line)
                    similarity += 1.0 - dist / max_len
                similarity /= len(middle_search)
            else:
                similarity = 1.0
            if similarity > best_sim:
                best_sim = similarity
                best_idx = i

        if best_sim >= _MULTI_CANDIDATE_THRESHOLD and best_idx >= 0:
            start = sum(len(content_lines[k]) + 1 for k in range(best_idx))
            end = start + sum(len(content_lines[best_idx + k]) + 1
                              for k in range(len(search_lines))) - 1
            yield content[start:end]


def _whitespace_normalized_replacer(content: str, old_string: str):
    """Strategy 4: Collapse all whitespace runs to single spaces, then match."""
    def normalize(text: str) -> str:
        return re.sub(r'\s+', ' ', text).strip()

    norm_search = normalize(old_string)
    if not norm_search:
        return

    # Try single-line substring match
    norm_content = normalize(content)
    if norm_search in norm_content:
        # Build a regex from the search words with flexible whitespace
        words = norm_search.split(' ')
        pattern = r'\s+'.join(re.escape(w) for w in words)
        for m in re.finditer(pattern, content):
            yield m.group(0)
        return

    # Try multi-line block match
    content_lines = content.split("\n")
    search_lines = old_string.split("\n")
    if len(search_lines) < 2:
        return
    for i in range(len(content_lines) - len(search_lines) + 1):
        matched = True
        for j in range(len(search_lines)):
            if normalize(content_lines[i + j]) != normalize(search_lines[j]):
                matched = False
                break
        if matched:
            start = sum(len(content_lines[k]) + 1 for k in range(i))
            end = start + sum(len(content_lines[i + k]) + 1
                              for k in range(len(search_lines))) - 1
            yield content[start:end]


def _indentation_flexible_replacer(content: str, old_string: str):
    """Strategy 5: Strip minimum indentation from both, then compare."""
    def dedent(text: str) -> str:
        lines = text.split("\n")
        non_empty = [l for l in lines if l.strip()]
        if not non_empty:
            return text
        min_indent = min(len(l) - len(l.lstrip()) for l in non_empty)
        return "\n".join(l[min_indent:] if len(l) >= min_indent else l for l in lines)

    dedented_search = dedent(old_string)
    search_lines = dedented_search.split("\n")
    content_lines = content.split("\n")

    for i in range(len(content_lines) - len(search_lines) + 1):
        block = content_lines[i: i + len(search_lines)]
        dedented_block = dedent("\n".join(block))
        if dedented_block.split("\n") == search_lines:
            start = sum(len(content_lines[k]) + 1 for k in range(i))
            end = start + sum(len(content_lines[i + k]) + 1
                              for k in range(len(search_lines))) - 1
            yield content[start:end]


def _escape_normalized_replacer(content: str, old_string: str):
    """Strategy 6: Normalize escape sequences, then match."""
    _ESCAPE_MAP = {
        '\\n': '\n', '\\t': '\t', '\\r': '\r',
        "\\'": "'", '\\"': '"', '\\\\': '\\',
    }

    def unescape(text: str) -> str:
        result = text
        for esc, char in _ESCAPE_MAP.items():
            result = result.replace(esc, char)
        return result

    unescaped_search = unescape(old_string)

    # Direct substring match with unescaped version
    if unescaped_search != old_string and unescaped_search in content:
        yield unescaped_search
        return

    # Block match with unescape applied per-line
    content_lines = content.split("\n")
    search_lines = old_string.split("\n")
    if len(search_lines) < 2:
        return
    for i in range(len(content_lines) - len(search_lines) + 1):
        matched = True
        for j in range(len(search_lines)):
            if unescape(content_lines[i + j]) != unescape(search_lines[j]):
                # Also try: content line matches unescaped search line
                if content_lines[i + j] != unescape(search_lines[j]):
                    matched = False
                    break
        if matched:
            start = sum(len(content_lines[k]) + 1 for k in range(i))
            end = start + sum(len(content_lines[i + k]) + 1
                              for k in range(len(search_lines))) - 1
            yield content[start:end]


# Smart-punctuation → ASCII equivalents. LLMs often type straight quotes
# while source files contain curly quotes (or vice-versa) — same visual intent,
# different codepoints. Without this, file_edit keeps failing until the model
# guesses the exact codepoint.
_PUNCT_MAP = {
    "\u2018": "'", "\u2019": "'",                   # ‘ ’ → '
    "\u201C": '"', "\u201D": '"',                   # “ ” → "
    "\u2013": "-", "\u2014": "-",                   # – — → -
    "\u2026": "...",                                # … → ...
    "\u00A0": " ",                                  # NBSP → space
    "\u200B": "", "\u200C": "", "\u200D": "",       # zero-width chars → strip
    "\uFEFF": "",                                   # BOM → strip
}


def _normalize_punct(text: str) -> str:
    for src, dst in _PUNCT_MAP.items():
        if src in text:
            text = text.replace(src, dst)
    return text


def _punct_normalized_replacer(content: str, old_string: str):
    """Strategy 7: Normalize smart quotes / dashes / NBSP / zero-widths on BOTH
    sides, then locate. Yields the ORIGINAL substring from content so the
    replacement preserves the file's existing codepoints elsewhere."""
    norm_old = _normalize_punct(old_string)
    if norm_old == old_string:
        return  # No smart-punct in search — other strategies handle this
    norm_content = _normalize_punct(content)
    idx = norm_content.find(norm_old)
    if idx < 0:
        return
    # Map normalized index back to the original content. Since _PUNCT_MAP only
    # shrinks ‖ substitutes length (e.g. NBSP→space is 1:1, zero-width→'' is 1:0,
    # ellipsis→... is 1:3), we scan original char-by-char counting normalized
    # chars until we hit idx.
    orig_start = _map_norm_index_to_orig(content, idx)
    orig_end = _map_norm_index_to_orig(content, idx + len(norm_old))
    if orig_start is None or orig_end is None:
        return
    yield content[orig_start:orig_end]


def _map_norm_index_to_orig(original: str, norm_idx: int):
    """Walk original; advance a normalized-char counter; return original index
    where the counter equals norm_idx."""
    n = 0
    for i, ch in enumerate(original):
        if n == norm_idx:
            return i
        replacement = _PUNCT_MAP.get(ch, ch)
        n += len(replacement)
    if n == norm_idx:
        return len(original)
    return None


# ── Cascade: ordered list of strategies ─────────────────────────────────

_REPLACERS = [
    _simple_replacer,
    _line_trimmed_replacer,
    _block_anchor_replacer,
    _whitespace_normalized_replacer,
    _indentation_flexible_replacer,
    _escape_normalized_replacer,
    _punct_normalized_replacer,
]


# ── Main edit function ──────────────────────────────────────────────────

def file_edit(path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> str:
    """Replace old_string with new_string in a file using fuzzy matching cascade."""
    # Checkpoint before mutation
    from core.checkpoints import checkpoint_manager
    checkpoint_manager.ensure_checkpoint(str(Path(path).parent), "before file_edit")

    p = Path(path)

    try:
        original = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return json.dumps({"error": f'Could not read "{path}": {e}'})

    # Snapshot diagnostics before the edit so _build_edit_result can show only
    # the errors this edit introduced, not the file's pre-existing pile.
    from tools.lint import snapshot_diagnostics
    _baseline = snapshot_diagnostics(path)

    # Detect line ending style to preserve it
    uses_crlf = "\r\n" in original
    if uses_crlf:
        # Normalize to \n for matching, restore \r\n after
        original_normalized = original.replace("\r\n", "\n")
        old_normalized = old_string.replace("\r\n", "\n")
        new_normalized = new_string.replace("\r\n", "\n")
    else:
        original_normalized = original
        old_normalized = old_string
        new_normalized = new_string

    # Run the cascade: try each strategy in order
    matched_text = None
    strategy_used = None
    not_found = True
    multiple_found = False

    for replacer in _REPLACERS:
        for candidate in replacer(original_normalized, old_normalized):
            idx = original_normalized.find(candidate)
            if idx == -1:
                continue  # Candidate wasn't actually in content

            not_found = False

            if replace_all:
                # For replace_all, use the first strategy that finds anything
                count = original_normalized.count(candidate)
                updated = original_normalized.replace(candidate, new_normalized)
                if uses_crlf:
                    updated = updated.replace("\n", "\r\n")
                from tools.lint import safe_write_text
                write_err = safe_write_text(path, updated)
                if write_err:
                    return json.dumps({"error": write_err})
                from core.event_bus import bus
                bus.emit("file.changed", path=path, tool="file_edit", original=original)
                return _build_edit_result(
                    path,
                    f'Replaced {count} occurrence{"s" if count != 1 else ""} in "{path}".',
                    baseline=_baseline,
                )

            # For single replacement, verify uniqueness
            last_idx = original_normalized.rfind(candidate)
            if idx != last_idx:
                multiple_found = True
                continue  # Multiple matches — try next strategy

            # Unique match found!
            matched_text = candidate
            strategy_used = replacer.__name__
            break

        if matched_text is not None:
            break

    if not_found:
        return json.dumps({
            "error": (
                f'The string to replace was not found in "{path}". '
                "Make sure old_string matches the file contents (whitespace, "
                "indentation, line endings). The fuzzy matcher tried 6 strategies "
                "including trimmed, indentation-flexible, and whitespace-normalized "
                "matching — none could locate your string."
            )
        })

    if matched_text is None and multiple_found:
        return json.dumps({
            "error": (
                f'old_string matches multiple locations in "{path}". '
                "Provide a more specific string to uniquely identify the section, "
                "or set replace_all to true."
            )
        })

    if matched_text is None:
        return json.dumps({"error": f'Could not resolve a unique match in "{path}".'})

    # Perform the replacement
    idx = original_normalized.find(matched_text)
    updated = (original_normalized[:idx] + new_normalized +
               original_normalized[idx + len(matched_text):])

    if uses_crlf:
        updated = updated.replace("\n", "\r\n")

    from tools.lint import safe_write_text
    write_err = safe_write_text(path, updated)
    if write_err:
        return json.dumps({"error": write_err})

    # Note which strategy was used (helps with debugging)
    strategy_note = ""
    if strategy_used and strategy_used != "_simple_replacer":
        friendly = strategy_used.replace("_replacer", "").replace("_", " ").strip()
        strategy_note = f" (matched via {friendly})"

    # Publish file change event — includes original content so the file viewer
    # can compute and display a diff.
    from core.event_bus import bus
    bus.emit("file.changed", path=path, tool="file_edit", original=original)

    return _build_edit_result(
        path,
        f'Replaced 1 occurrence in "{path}"{strategy_note}.',
        baseline=_baseline,
    )


def _build_edit_result(path: str, status: str,
                       baseline: list[dict] | None = None) -> str:
    """Return a JSON tool result with structured error/diagnostics fields when
    the post-edit check finds problems. Delegates to the shared
    build_validation_result so every edit tool reports identically, and — given
    a pre-edit baseline — surfaces only the errors this edit introduced. Shared
    by both the replace_all and single-replace exit paths."""
    from core.sounds import play_edit_sound
    from tools.lint import build_validation_result
    play_edit_sound(path)
    result = build_validation_result(path, status, baseline=baseline)
    return json.dumps(result)


registry.register(
    name="file_edit",
    description=(
        "Replace old_string with new_string in a file. Fuzzy cascade: exact \u2192 line-trim \u2192 "
        "block-anchor (Levenshtein) \u2192 whitespace-norm \u2192 indent-flex \u2192 escape-norm. "
        "Errors on ambiguous match unless replace_all=true. "
        "Use file_write for new files / full rewrites."
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute file path."},
            "old_string": {
                "type": "string",
                "description": "String to find. Minor whitespace/indent drift tolerated.",
            },
            "new_string": {
                "type": "string",
                "description": "Replacement. Must differ from old_string.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence (default false \u2192 errors on dup).",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
    execute=file_edit,
)
