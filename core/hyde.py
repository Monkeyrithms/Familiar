"""HyDE — Hypothetical Document Embeddings for code retrieval.

The query/answer mismatch: you embed a QUESTION ("how do I cancel a running
turn?") and compare it against CODE ("def stop_inference(self):
self._cancel_event.set()"). A question and the code that answers it don't look
alike in vector space — they live in different neighborhoods, because one is
English interrogative and the other is an imperative implementation. Bi-encoder
similarity quietly suffers for it.

HyDE closes the gap: ask a cheap LLM to write a SHORT hypothetical answer —
roughly the code/explanation that *would* answer the question — then embed THAT
and search with it. The fake answer lives in the same neighborhood as the real
passage, so retrieval lands closer. The hypothetical doesn't need to be correct;
it only needs to look like the target, which is a much lower bar.

This is the strong sibling of plain query expansion: expansion adds keywords,
HyDE moves the whole query vector into answer-space.

Cost: one small completion per retrieved turn. So it is:
  - OFF by default, opt-in via policy (it adds latency to every gated turn).
  - Fail-safe: ANY error (no client, rate limit, 402, timeout) returns the
    original query unchanged. Retrieval never breaks because HyDE was enabled.
  - Length-capped so the hypothetical can't blow up the embedding input.
"""

from __future__ import annotations

_PROMPT = (
    "You are helping a code search engine. Given a developer's question about a "
    "codebase, write a SHORT, plausible code snippet or technical description "
    "that would answer it — as if it were an excerpt from the actual source "
    "file. Do not hedge, do not say 'I don't know', do not ask questions. Output "
    "only the snippet/description, no preamble, max 120 words. If the question "
    "names specific identifiers, use them verbatim."
)


def hypothetical(query: str, *, client, model: str,
                 max_words: int = 120, log=None) -> str:
    """Return a hypothetical-answer string for *query*, or the original query on
    any failure. `client` is an OpenAI-compatible client (caller supplies the
    already-configured one); `model` should be a cheap/fast model.

    The returned text is meant to be EMBEDDED, not shown to the user — it's a
    retrieval probe, possibly wrong, and that's fine."""
    q = (query or "").strip()
    if not q or client is None or not model:
        return query
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _PROMPT},
                {"role": "user", "content": q},
            ],
            temperature=0.0,
            max_tokens=200,
        )
        text = (resp.choices[0].message.content or "").strip()
        if not text:
            return query
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words])
        # Concatenate the real query with the hypothetical: keeps exact
        # identifiers from the question (high-precision signal) while pulling the
        # vector toward answer-space. Best of both.
        combined = f"{q}\n{text}"
        if log:
            log(f"[hyde] expanded query +{len(words)}w")
        return combined
    except Exception as e:
        if log:
            log(f"[hyde] fallback (original query): {e}")
        return query
