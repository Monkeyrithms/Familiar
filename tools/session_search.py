"""
Session search tool — recall past conversations via hybrid FTS5 + vector search.

Search is scoped to the current conversation's subscribed memory streams.
Uses iterative retrieval with coverage estimation (inspired by Hybrid's
quasi-RAG orchestrator) and multi-stage context assembly.

Pipeline:
  1. Search (FTS5 + vector, stream-scoped)
  2. Estimate coverage confidence
  3. If low confidence → expand query, search again (max 2 rounds)
  4. Assemble context: broad summaries → deep detail → metadata
"""

import json
import re
from tools.registry import registry

# Coverage threshold — below this, expand query and retry
CONFIDENCE_THRESHOLD = 0.55
MAX_SEARCH_ROUNDS = 2


def _get_current_streams() -> list[str]:
    """Get the current conversation's subscribed streams from config."""
    try:
        from core.agent import load_config
        cfg = load_config()
        streams = cfg.get("memory_streams", [])
        return [s["name"] for s in streams if s.get("auto_subscribe")]
    except Exception:
        return ["General"]


def _estimate_coverage(results: list[dict], query: str) -> dict:
    """Estimate how well our results cover the query.

    Returns {confidence: 0-1, query_terms_covered: float, source_diversity: float,
             missing_terms: [str]}
    """
    if not results:
        return {"confidence": 0.0, "query_terms_covered": 0.0,
                "source_diversity": 0.0, "missing_terms": query.split()}

    # Extract query terms (strip OR/AND operators)
    raw_terms = re.findall(r'[A-Za-z0-9_]+', query.lower())
    query_terms = [t for t in raw_terms if t not in ("or", "and", "not")]

    if not query_terms:
        return {"confidence": 0.5, "query_terms_covered": 0.5,
                "source_diversity": 0.5, "missing_terms": []}

    # Check which query terms appear in result snippets
    all_text = " ".join(r.get("snippet", "").lower() for r in results)
    found = [t for t in query_terms if t in all_text]
    missing = [t for t in query_terms if t not in all_text]
    term_coverage = len(found) / len(query_terms) if query_terms else 0

    # Source diversity: do results come from both FTS5 and vector?
    has_fts = any(r.get("fts_score", 0) > 0 for r in results)
    has_vec = any(r.get("vec_score", 0) > 0 for r in results)
    source_diversity = (0.5 * has_fts + 0.5 * has_vec)

    # Score strength: average of top result scores
    top_scores = [r.get("score", 0) for r in results[:3]]
    avg_score = sum(top_scores) / len(top_scores) if top_scores else 0

    confidence = (0.40 * term_coverage) + (0.25 * source_diversity) + (0.35 * min(1.0, avg_score * 3))

    return {
        "confidence": round(confidence, 3),
        "query_terms_covered": round(term_coverage, 3),
        "source_diversity": round(source_diversity, 3),
        "missing_terms": missing,
    }


def _expand_query(original_query: str, missing_terms: list[str],
                  round_num: int) -> str:
    """Generate an expanded query targeting gaps in coverage."""
    # Broaden: use OR between all terms (FTS5 defaults to AND which is too strict)
    terms = re.findall(r'[A-Za-z0-9_]+', original_query)
    terms = [t for t in terms if t.upper() not in ("OR", "AND", "NOT")]

    if missing_terms:
        # Focus on the missing terms
        expanded = " OR ".join(missing_terms + terms[:2])
    else:
        # Just broaden the original
        expanded = " OR ".join(terms)

    return expanded


def _assemble_context(results: list[dict], query: str,
                      coverage: dict, streams: list[str]) -> dict:
    """Multi-stage context assembly (Hybrid-inspired).

    Stage 1: Broad — short snippets from all results (breadth)
    Stage 2: Deep — full rolling summaries from top hits (depth)
    Stage 3: Metadata — confidence and coverage info
    """
    from core.database import load_conversation, load_stream_summary

    # ── Stage 1: Broad coverage (snippets from all results) ──
    broad = []
    for r in results:
        broad.append({
            "conversation": r.get("name", ""),
            "relevance": r.get("score", 0),
            "snippet": r.get("snippet", "")[:300],
        })

    # ── Stage 2: Deep dive (rolling summaries from top 3) ──
    deep = []
    for r in results[:3]:
        cid = r.get("conversation_id", "")
        if not cid:
            continue
        conv_data = load_conversation(cid)
        if not conv_data:
            continue

        conv_streams = conv_data.get("streams", [])
        summaries = {}
        for stream_name in conv_streams:
            if stream_name in streams:
                s = load_stream_summary(stream_name, cid)
                if s and s.get("summary"):
                    summaries[stream_name] = s["summary"]

        if summaries:
            deep.append({
                "conversation": r.get("name", ""),
                "stream_summaries": summaries,
            })

    # ── Stage 3: Metadata ──
    metadata = {
        "confidence": coverage["confidence"],
        "query_terms_covered": coverage["query_terms_covered"],
        "source_diversity": coverage["source_diversity"],
        "results_found": len(results),
    }
    if coverage.get("missing_terms"):
        metadata["terms_not_found"] = coverage["missing_terms"]

    return {
        "query": query,
        "streams": streams,
        "broad_results": broad,
        "detailed_results": deep,
        "search_metadata": metadata,
    }


def session_search(query: str = "", limit: int = 5) -> str:
    """Search past conversations using hybrid keyword + semantic search.

    Uses iterative retrieval: if first pass has low confidence,
    expands the query and searches again (max 2 rounds).
    Returns multi-stage context: broad snippets + deep summaries + metadata.
    """
    from core.database import search_conversations

    if not query or not query.strip():
        # No query = list recent conversations
        from core.database import list_conversations
        recent = list_conversations()[:limit]
        return json.dumps({
            "mode": "recent",
            "conversations": [{
                "id": c["id"],
                "name": c["name"],
                "messages": c["message_count"],
                "streams": c["streams"],
            } for c in recent],
        }, ensure_ascii=False)

    streams = _get_current_streams()
    best_results = []
    best_coverage = {"confidence": 0.0}
    final_query = query

    for round_num in range(MAX_SEARCH_ROUNDS):
        search_query = query if round_num == 0 else _expand_query(
            query, best_coverage.get("missing_terms", []), round_num)

        results = search_conversations(search_query, streams=streams, limit=limit)
        coverage = _estimate_coverage(results, query)

        # Keep best results across rounds
        if coverage["confidence"] > best_coverage["confidence"]:
            best_results = results
            best_coverage = coverage
            final_query = search_query

        if coverage["confidence"] >= CONFIDENCE_THRESHOLD:
            break  # Good enough

    if not best_results:
        return json.dumps({
            "query": query,
            "streams": streams,
            "broad_results": [],
            "detailed_results": [],
            "search_metadata": {
                "confidence": 0.0,
                "results_found": 0,
                "message": "No matching conversations found in subscribed streams.",
            },
        })

    return json.dumps(
        _assemble_context(best_results, final_query, best_coverage, streams),
        ensure_ascii=False,
    )


registry.register(
    name="session_search",
    description=(
        "Past conversation search. Hybrid keyword+semantic. "
        "✓ 'remember when'|'we discussed'|prior context. Empty query → recent convs."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Query. OR for broad recall: 'renko OR brick OR ATR'. Empty → recent convs."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 5).",
            },
        },
    },
    execute=session_search,
)
