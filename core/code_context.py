"""Proactive code-context pre-injection (Cursor-style retrieval).

Before the model's first token, run a vector query over the active workspace's
code index and, IF the top hit is semantically relevant enough, paste the most
relevant chunks into context as a transient system fragment. The model starts
already knowing the relevant code — no decide -> call vector_search -> wait ->
read tool round-trip.

The whole design is the RELEVANCE GATE. Searching every turn is cheap (one
query embedding + a vector lookup), but INJECTING every turn would tax every
off-topic message ("thanks", "what's the weather") with 2-3k tokens of
irrelevant code. So:

  - We gate on absolute cosine similarity (`vec_score` = 1 - cosine distance),
    NOT the fused RRF `score`. RRF is a relative rank — its top value is ~constant
    regardless of match quality, so it can't tell "great match" from "best of a
    bad lot". Cosine similarity is absolute: a weather question against a Python
    repo scores near zero on every chunk -> nothing injected -> zero waste.
  - Only chunks at/above the threshold are injected, capped at `limit`.
  - Every turn's top score is logged (when logging is on) so the threshold can
    be tuned against real traffic before anyone trusts a fixed number.

OFF BY DEFAULT (`code_preinject_enabled`). This commits retrieved guesses into
context before the model has reasoned about the problem, which is the right
trade in an editor (you're almost always asking about nearby code) but not
universally. Opt in, watch the logged scores, then decide.
"""

from __future__ import annotations

# Absolute cosine similarity gate. Calibrated against this codebase: on-topic
# questions score ~0.44-0.63, off-topic-but-code-shaped ones ~0.20-0.36. 0.42
# sits in the gap — catches real questions, rejects unrelated ones. Per-repo
# corpora differ, so it's configurable and every turn's top score is logged.
DEFAULT_THRESHOLD = 0.42   # absolute cosine similarity; tune against logs
DEFAULT_LIMIT = 5          # max chunks injected on a relevant turn
DEFAULT_SEARCH_POOL = 12   # how many hits to pull before gating/trimming


def _looks_like_code_question(msg: str) -> bool:
    """Cheap heuristic fast-path: skip the vector query entirely on messages
    that obviously aren't about code. Conservative — when unsure, returns True
    and lets the cosine gate make the real call. This only saves the query
    embedding on clear non-code turns; it is NOT the relevance decision."""
    if not msg or len(msg.strip()) < 3:
        return False
    low = msg.lower()
    code_signals = (
        "function", "class", "method", "import", "module", "file", "error",
        "bug", "trace", "stack", "exception", "def ", "async", "return",
        "where", "how does", "how do", "why does", "implement", "refactor",
        "test", "api", "endpoint", "handler", "config", "schema", "query",
        "fix", "add", "remove", "rename", "search", "index", "call", "wire",
        "(", ")", "_", ".py", ".ts", ".js", "->", "=>", "::",
    )
    return any(sig in low for sig in code_signals)


def gather(user_message: str, workspace_path: str, *,
           enabled: bool, threshold: float = DEFAULT_THRESHOLD,
           limit: int = DEFAULT_LIMIT, vector_enabled: bool = True,
           expand_calls: bool = False, hyde: bool = False,
           hyde_client=None, hyde_model: str = "",
           turn_id: str = "", log=None) -> str | None:
    """Return injectable code-context text for this turn, or None.

    None means "inject nothing" — no index, gate not cleared, feature off, or
    an error. Caller wraps the returned text in a fragment and inserts it as a
    transient system message (never persisted, never summarized).

    Policy is passed in explicitly by the caller — the active memory stream is
    the authority over `enabled` / `threshold` / `vector_enabled`, resolved in
    agent._resolve_retrieval_policy(). This function no longer reads config.

    Optional stages, each gated + fail-safe:
      - hyde: rewrite the query into a hypothetical answer before searching
        (needs hyde_client + hyde_model). Closes the question/answer gap.
      - expand_calls: after gating, pull one hop of call-graph neighbors
        (callers/callees) so the model gets the surrounding code too.
      - turn_id: when set, every retrieval is logged to retrieval_feedback for
        later outcome attribution (which chunks actually got used).
    """
    if not enabled or not vector_enabled:
        return None
    if not user_message or not workspace_path:
        return None

    threshold = float(threshold)
    limit = int(limit)

    # Cheap heuristic fast-path: clear non-code turns skip the query entirely.
    if not _looks_like_code_question(user_message):
        if log:
            log("[code-preinject] skipped (non-code heuristic)")
        return None

    try:
        from core.code_index import registry, open_index
    except Exception:
        return None

    ws = registry.find_for_file(workspace_path)
    if not ws or not ws.get("last_indexed"):
        return None
    idx = open_index(ws["name"])
    if not idx:
        return None

    # HyDE: move the query vector into answer-space before searching. Fail-safe —
    # returns the original query on any error (incl. no client / rate limit).
    search_query = user_message
    if hyde and hyde_client and hyde_model:
        try:
            from core.hyde import hypothetical
            search_query = hypothetical(
                user_message, client=hyde_client, model=hyde_model, log=log,
            )
        except Exception:
            search_query = user_message

    try:
        # Hybrid pull (catches exact identifiers too), but gate on vec_score.
        hits = idx.search(search_query, limit=DEFAULT_SEARCH_POOL)
    except Exception as e:
        if log:
            log(f"[code-preinject] search error: {e}")
        return None

    if not hits:
        return None

    # Gate on absolute cosine similarity. Keyword-only hits have vec_score=None
    # and are NOT eligible to clear the gate on their own (no semantic signal),
    # though they can still ride along once the gate is open.
    scored = [h for h in hits if h.get("vec_score") is not None]
    top = max((h["vec_score"] for h in scored), default=None)

    # Always log the top score so the threshold can be tuned against real msgs.
    if log:
        shown = f"{top:.4f}" if top is not None else "none"
        decision = "INJECT" if (top is not None and top >= threshold) else "skip"
        log(f"[code-preinject] top_vec_score={shown} thr={threshold} -> {decision}")

    if top is None or top < threshold:
        return None

    # Keep only chunks at/above threshold, best first, capped.
    keep = sorted(
        [h for h in scored if h["vec_score"] >= threshold],
        key=lambda h: h["vec_score"], reverse=True,
    )[:limit]
    if not keep:
        return None

    # Call-graph expansion: one hop of callers/callees around the kept hits, so
    # the model sees the surrounding code, not just the single best chunk. The
    # neighbors are clearly labeled as graph-adjacent (not similarity matches).
    neighbors: list[dict] = []
    if expand_calls:
        try:
            from core.call_graph import expand as _expand, db_path_for
            dbp = db_path_for(ws["name"])
            if dbp:
                neighbors = _expand(dbp, keep, max_neighbors=min(limit, 6))
        except Exception as e:
            if log:
                log(f"[code-preinject] call-graph expand error: {e}")

    # Feedback log: record what we surfaced this turn so a later mark_used() can
    # attribute which chunks actually helped. Best-effort, never blocks.
    if turn_id:
        try:
            from core.retrieval_feedback import log_retrieval
            log_retrieval(user_message, keep + neighbors, turn_id=turn_id,
                          workspace=ws["name"], source="preinject")
        except Exception:
            pass

    lines = []
    for h in keep:
        loc = f"{h['file']}:{h['line_start']}-{h['line_end']}"
        label = f"{h.get('kind', 'chunk')} {h.get('name') or ''}".strip()
        lines.append(f"--- {loc}  ({label})  sim={h['vec_score']:.2f}\n{h['text']}")

    if neighbors:
        for n in neighbors:
            loc = f"{n['file']}:{n['line_start']}-{n['line_end']}"
            label = f"{n.get('kind', 'chunk')} {n.get('name') or ''}".strip()
            rel = n.get("relation", "related")
            via = n.get("via", "")
            lines.append(
                f"--- {loc}  ({label})  [{rel} via {via}]\n{n['text']}"
            )

    body = (
        "POSSIBLY-RELEVANT CODE from the active workspace index, retrieved by "
        "semantic similarity to this message. These are RETRIEVAL GUESSES, not "
        "verified context \u2014 confirm by reading the real files before relying "
        "on them, and ignore entirely if off-target.\n\n"
        + "\n\n".join(lines)
    )
    return body
