"""
Prompt-injection scanner for external content that lands in the system prompt
or tool results. Scans for hostile patterns before text reaches the model.

Threat model: the user opens a project or reads a file that was authored by
someone else. That file tries to steer the agent into exfiltrating secrets,
running destructive commands, or overriding the system prompt. This module
looks for known-hostile shapes and returns a verdict the caller can act on.

The scanner is a heuristic — false positives are possible, and a determined
attacker can evade regex. It's defense-in-depth, not a security boundary.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable


# ── Pattern sets ────────────────────────────────────────────────────────

# Invisible / control codepoints commonly used to hide instructions or to
# perform homoglyph / bidi attacks inside otherwise-ASCII content.
_INVISIBLE_CODEPOINTS = {
    "\u00AD",  # SOFT HYPHEN
    "\u180E",  # MONGOLIAN VOWEL SEPARATOR
    "\u200B",  # ZERO WIDTH SPACE
    "\u200C",  # ZERO WIDTH NON-JOINER
    "\u200D",  # ZERO WIDTH JOINER
    "\u200E",  # LEFT-TO-RIGHT MARK
    "\u200F",  # RIGHT-TO-LEFT MARK
    "\u202A",  # LEFT-TO-RIGHT EMBEDDING
    "\u202B",  # RIGHT-TO-LEFT EMBEDDING
    "\u202C",  # POP DIRECTIONAL FORMATTING
    "\u202D",  # LEFT-TO-RIGHT OVERRIDE
    "\u202E",  # RIGHT-TO-LEFT OVERRIDE
    "\u2060",  # WORD JOINER
    "\u2061",  # FUNCTION APPLICATION
    "\u2062",  # INVISIBLE TIMES
    "\u2063",  # INVISIBLE SEPARATOR
    "\u2064",  # INVISIBLE PLUS
    "\u2066",  # LEFT-TO-RIGHT ISOLATE
    "\u2067",  # RIGHT-TO-LEFT ISOLATE
    "\u2068",  # FIRST STRONG ISOLATE
    "\u2069",  # POP DIRECTIONAL ISOLATE
    "\uFEFF",  # ZERO WIDTH NO-BREAK SPACE / BOM
}

# Tag characters (U+E0020..U+E007F) are used to encode ASCII invisibly.
def _is_tag_char(cp: int) -> bool:
    return 0xE0020 <= cp <= 0xE007F

# Regex pattern registry. Each entry: (id, compiled_regex, severity, note).
# Severity: "high" (block), "medium" (warn + block), "low" (warn only).
_PATTERNS: list[tuple[str, re.Pattern, str, str]] = [
    # ── Prompt-override phrasing ─────────────────────────────────────────
    ("override_ignore_previous",
     re.compile(r"\b(ignore|disregard|forget|override)\s+(all\s+)?(the\s+)?"
                r"(previous|prior|above|earlier|your)\s+"
                r"(instructions?|system|prompts?|rules?|guidelines?)\b",
                re.I), "high",
     "phrase asking the model to drop prior system instructions"),

    ("override_you_are_now",
     re.compile(r"\byou\s+are\s+now\b|\bfrom\s+now\s+on,?\s+you\s+(are|will|must)\b",
                re.I), "medium",
     "persona-reset phrasing often used in jailbreaks"),

    ("override_new_instructions",
     re.compile(r"^\s*(new\s+instructions?\s*:|###\s*(system|instructions?)\s*:|"
                r"updated\s+system\s+prompt\s*:)",
                re.I | re.M), "high",
     "inline attempt to replace the system prompt"),

    ("jailbreak_known",
     re.compile(r"\b(DAN\s+mode|developer\s+mode|jailbreak|do\s+anything\s+now|"
                r"unrestricted\s+mode|godmode)\b",
                re.I), "high",
     "well-known jailbreak trigger"),

    # ── Fake role markers (try to look like the harness added them) ─────
    ("role_injection_marker",
     re.compile(r"^\s*(system|assistant|user|tool)\s*:\s*\S",
                re.I | re.M), "medium",
     "line begins with a role label — trying to fake a harness turn"),

    ("role_injection_bracket",
     re.compile(r"\[(SYSTEM|ADMIN|DEVELOPER|ROOT|OPENAI|ANTHROPIC)\]",
                re.I), "medium",
     "bracketed role label used to mimic a system tag"),

    ("role_injection_chatml",
     re.compile(r"<\|(im_start|im_end|system|assistant|user|tool)\|>",
                re.I), "high",
     "ChatML-style control token — only the runtime should emit these"),

    ("role_injection_html",
     re.compile(r"</?\s*(system|developer|assistant)\s*>",
                re.I), "medium",
     "HTML-ish pseudo-role tag"),

    # ── HTML comment injection ──────────────────────────────────────────
    ("html_comment_instruction",
     re.compile(r"<!--[^>]*\b(instruction|system|ignore|override|secret|"
                r"password|token|api[_\s]?key)\b[^>]*-->",
                re.I | re.S), "medium",
     "hidden instruction inside an HTML comment"),

    # ── Credential reads ────────────────────────────────────────────────
    ("exfil_read_env",
     re.compile(r"\b(cat|type|more|less|head|tail|Get-Content)\s+[^\n]*"
                r"(\.env\b|\.env\.[\w.-]+|credentials?\b|secrets?\.json\b)",
                re.I), "high",
     "attempt to read local env / credentials"),

    ("exfil_read_ssh",
     re.compile(r"(~?/?\.ssh/|\.aws/credentials|\.aws/config|\.kube/config|"
                r"~/\.netrc|id_[rd]sa|\.pem\b|\.pgp\b|\.gpg\b)",
                re.I), "high",
     "path to a private-key or credentials file"),

    # ── Exfiltration via outbound request ───────────────────────────────
    ("exfil_curl_post",
     re.compile(r"\b(curl|wget|Invoke-WebRequest|Invoke-RestMethod)\b[^\n]*"
                r"(-X\s*POST|--data|--data-binary|--upload-file|-d\s+)",
                re.I), "high",
     "outbound POST — classic exfiltration shape"),

    ("exfil_nc_pipe",
     re.compile(r"\bnc\s+[-\w.]+\s+\d+\b|\bncat\s+[-\w.]+\s+\d+\b",
                re.I), "high",
     "netcat pipe — shell exfil"),

    ("exfil_md_link_query",
     re.compile(r"!\[[^\]]*\]\(https?://[^\s)]+\?[^\s)]{12,}\)"), "medium",
     "markdown image link with a long query string — data-exfil image beacon"),

    ("url_with_credentials",
     re.compile(r"\bhttps?://[^/\s:]+:[^/\s@]+@[^/\s]+"), "medium",
     "URL embedding username:password"),

    # ── Dangerous schemes ───────────────────────────────────────────────
    ("javascript_uri",
     re.compile(r"\bjavascript:\s*[^\s]+", re.I), "medium",
     "javascript: URI — runnable in some previews"),

    ("data_html_uri",
     re.compile(r"\bdata:text/(html|javascript)[;,]", re.I), "medium",
     "data: URI carrying HTML/JS payload"),

    # ── Runtime control mimicry ─────────────────────────────────────────
    ("fake_tool_call_json",
     re.compile(r'\{\s*"(tool_call_id|function)"\s*:\s*\{?\s*"(name|arguments)"',
                re.S), "medium",
     "raw JSON shaped like an OpenAI tool-call — pretends to be an API turn"),

    # ── Destructive command shapes ──────────────────────────────────────
    ("destructive_rm_rf_root",
     re.compile(r"\brm\s+-[rRf]+\s+(/|~|\$HOME)(\s|$)"), "high",
     "rm -rf of a root-like path"),

    ("destructive_format",
     re.compile(r"\b(mkfs\.|dd\s+if=|shred\s+-[a-z]*f|:\(\)\{.*:\|:&\};:)",
                re.I), "high",
     "disk format / fork-bomb / shred shape"),

    # ── Model-identity phishing ─────────────────────────────────────────
    ("identity_reassignment",
     re.compile(r"\byou\s+are\s+(GPT|Claude|Gemini|an?\s+unrestricted|"
                r"an?\s+uncensored)", re.I), "low",
     "attempts to reassign model identity"),
]


# ── Public API ──────────────────────────────────────────────────────────

@dataclass
class Hit:
    pattern_id: str
    severity: str           # "high" | "medium" | "low"
    snippet: str            # the substring that matched (truncated)
    note: str               # human-readable explanation

@dataclass
class ScanResult:
    hits: list[Hit]
    invisible_count: int
    homoglyph_count: int
    source: str             # label for logs ("memory_note:General/x", "AGENTS.md", ...)

    @property
    def is_hostile(self) -> bool:
        """True if the caller should refuse to inject this content."""
        if any(h.severity == "high" for h in self.hits):
            return True
        # Large invisible-char count is suspicious on its own
        if self.invisible_count >= 3:
            return True
        if self.homoglyph_count >= 3:
            return True
        return False

    @property
    def is_suspicious(self) -> bool:
        """True if there's something worth logging, even if not hostile."""
        return bool(self.hits) or self.invisible_count > 0 or self.homoglyph_count > 0


def _count_invisible(text: str) -> int:
    n = 0
    for ch in text:
        if ch in _INVISIBLE_CODEPOINTS:
            n += 1
        elif _is_tag_char(ord(ch)):
            n += 1
    return n


def _count_homoglyph_mixed_words(text: str) -> int:
    """Count words that mix scripts (Latin + Cyrillic/Greek within one word)."""
    mixed = 0
    for token in re.findall(r"\w{3,}", text):
        scripts = set()
        for ch in token:
            name = unicodedata.name(ch, "")
            if name.startswith("LATIN "):
                scripts.add("latin")
            elif name.startswith("CYRILLIC "):
                scripts.add("cyrillic")
            elif name.startswith("GREEK "):
                scripts.add("greek")
        if len(scripts) >= 2:
            mixed += 1
    return mixed


def scan(text: str, source: str = "") -> ScanResult:
    """Scan *text* for prompt-injection shapes. Never raises."""
    if not text:
        return ScanResult([], 0, 0, source)
    hits: list[Hit] = []
    try:
        for pid, rx, sev, note in _PATTERNS:
            m = rx.search(text)
            if m:
                snippet = m.group(0)
                if len(snippet) > 120:
                    snippet = snippet[:117] + "..."
                hits.append(Hit(pid, sev, snippet, note))
    except Exception:
        pass
    inv = _count_invisible(text)
    hg = _count_homoglyph_mixed_words(text)
    return ScanResult(hits, inv, hg, source)


def sanitize(text: str) -> str:
    """Strip invisible control codepoints from *text*. Does not touch regex
    matches — the caller decides what to do with hostile content."""
    if not text:
        return text
    out = []
    for ch in text:
        if ch in _INVISIBLE_CODEPOINTS:
            continue
        if _is_tag_char(ord(ch)):
            continue
        out.append(ch)
    return "".join(out)


def scan_many(items: Iterable[tuple[str, str]]) -> list[ScanResult]:
    """Scan a batch of (source_label, text) pairs. Returns results in order."""
    return [scan(t, s) for s, t in items]


def format_report(result: ScanResult) -> str:
    """One-line human summary of a scan result — for logs."""
    if not result.is_suspicious:
        return f"clean ({result.source})"
    parts = []
    if result.hits:
        top = result.hits[0]
        parts.append(f"{len(result.hits)} hit(s), top={top.pattern_id}({top.severity})")
    if result.invisible_count:
        parts.append(f"invisible={result.invisible_count}")
    if result.homoglyph_count:
        parts.append(f"homoglyph={result.homoglyph_count}")
    verdict = "HOSTILE" if result.is_hostile else "suspicious"
    return f"{verdict} ({result.source}): {', '.join(parts)}"
