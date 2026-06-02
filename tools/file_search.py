"""
Fuzzy file search tool — rank files by similarity to a query.

Complements the `glob` tool (exact pattern matching) with a ranked,
typo-tolerant search. Good for: "find something like user_auth",
"where's the config loader", "look for the image helper".

Scoring favors:
  - Matches in the filename over matches in the directory path
  - Contiguous substring matches over scattered character matches
  - Shorter paths when scores are close
  - Case-insensitive by default; case-sensitive if the query has uppercase
"""

import json
import os
from pathlib import Path
from tools.registry import registry

DEFAULT_MAX_RESULTS = 30
IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".nuxt", "coverage", ".pytest_cache", ".cache", ".tox",
    "egg-info", ".mypy_cache", ".idea", ".vscode",
}
IGNORE_EXTS = {
    ".pyc", ".pyo", ".exe", ".dll", ".so", ".dylib", ".o", ".obj",
    ".bin", ".dat", ".db", ".sqlite", ".lock",
}
MAX_CANDIDATES = 20_000


def _fuzzy_score(query: str, candidate: str, case_sensitive: bool) -> tuple[float, int]:
    """Score how well *query* matches *candidate*.

    Returns (score, span_length). Score is 0 when the query isn't even a
    subsequence of the candidate. Higher score = better match.
    """
    q = query if case_sensitive else query.lower()
    c = candidate if case_sensitive else candidate.lower()

    if not q:
        return 0.0, 0

    # Fast bail: must be a subsequence
    ci = 0
    positions = []
    for ch in q:
        idx = c.find(ch, ci)
        if idx < 0:
            return 0.0, 0
        positions.append(idx)
        ci = idx + 1

    # Span of matched characters (tighter = better)
    span = positions[-1] - positions[0] + 1

    # Contiguity bonus: fraction of characters that were consecutive
    contiguous = sum(1 for i in range(1, len(positions)) if positions[i] == positions[i-1] + 1)
    contiguous_ratio = contiguous / max(1, len(positions) - 1)

    # Density: match length / span
    density = len(q) / max(1, span)

    # Boundary bonus — matches that start on word boundaries feel better
    boundary_bonus = 0.0
    for i, pos in enumerate(positions):
        if pos == 0 or c[pos - 1] in "/\\_-. ":
            boundary_bonus += 0.15 / len(positions)

    # Prefix bonus
    prefix_bonus = 0.3 if c.startswith(q) else 0.0
    # Substring bonus (query appears verbatim as a contiguous run)
    substring_bonus = 0.4 if q in c else 0.0

    score = density * 0.4 + contiguous_ratio * 0.3 + boundary_bonus + prefix_bonus + substring_bonus
    return score, span


def _score_path(query: str, rel_path: str, case_sensitive: bool) -> float:
    """Combined filename+path score. Filename matches weighted 2x."""
    fname = os.path.basename(rel_path)
    fname_score, _ = _fuzzy_score(query, fname, case_sensitive)
    path_score, _ = _fuzzy_score(query, rel_path, case_sensitive)
    # Filename is the primary signal
    combined = fname_score * 2.0 + path_score
    # Penalty for very long paths when scores are close
    combined -= min(0.1, len(rel_path) / 5000)
    return combined


def file_search(query: str, path: str = None, max_results: int = None,
                case_sensitive: bool = None) -> str:
    """Fuzzy-rank files matching *query*. Returns top results by score."""
    if not query or not query.strip():
        return json.dumps({"error": "Empty query"})

    search_root = Path(path) if path else Path.cwd()
    if not search_root.is_dir():
        return json.dumps({"error": f"Not a directory: {search_root}"})

    max_results = max_results or DEFAULT_MAX_RESULTS
    # Auto-detect case sensitivity: if query has uppercase, be case-sensitive
    if case_sensitive is None:
        case_sensitive = any(c.isupper() for c in query)

    # Collect candidates
    candidates: list[str] = []
    for dirpath, dirnames, filenames in os.walk(search_root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext in IGNORE_EXTS:
                continue
            full = os.path.join(dirpath, fname)
            try:
                rel = os.path.relpath(full, search_root).replace("\\", "/")
            except ValueError:
                rel = full
            candidates.append(rel)
            if len(candidates) >= MAX_CANDIDATES:
                break
        if len(candidates) >= MAX_CANDIDATES:
            break

    # Score and sort
    scored: list[tuple[float, str]] = []
    for rel in candidates:
        s = _score_path(query, rel, case_sensitive)
        if s > 0.1:  # threshold for a minimal match
            scored.append((s, rel))

    if not scored:
        return json.dumps({
            "results": "No matches found.",
            "count": 0,
            "searched": len(candidates),
            "hint": "Try a shorter or simpler query, or use glob for exact patterns.",
        })

    scored.sort(key=lambda x: (-x[0], len(x[1]), x[1]))
    top = scored[:max_results]

    lines = [f"{rel}  (score {s:.2f})" for s, rel in top]
    output = "\n".join(lines)
    if len(scored) > max_results:
        output += f"\n\n(showing top {max_results} of {len(scored)} matches)"

    return json.dumps({
        "results": output,
        "count": len(top),
        "total_matched": len(scored),
        "searched": len(candidates),
        "root": str(search_root),
    }, ensure_ascii=False)


registry.register(
    name="file_search",
    description=(
        "Fuzzy file search by name. ✓ rough name known, exact path not. ✗ exact glob → use glob."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Free-form name | partial path.",
            },
            "path": {
                "type": "string",
                "description": "Root dir (default workspace).",
            },
            "max_results": {
                "type": "integer",
                "description": "Max results (default 30).",
            },
            "case_sensitive": {
                "type": "boolean",
                "description": "Force case match (default auto: on if query has uppercase).",
            },
        },
        "required": ["query"],
    },
    execute=file_search,
)
