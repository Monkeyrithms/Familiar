"""
Secret redaction — scrubs API keys, tokens, passwords from text before
it reaches the chat, memory, or logs.

Patterns sourced from hermes-agent-main's redact.py.
"""

import re

_PATTERNS = [
    # API keys by prefix
    (r'sk-ant-[a-zA-Z0-9_-]{20,}', "anthropic_key"),
    (r'sk-[a-zA-Z0-9]{20,}', "openai_key"),
    (r'ghp_[a-zA-Z0-9]{30,}', "github_pat"),
    (r'gho_[a-zA-Z0-9]{30,}', "github_oauth"),
    (r'xox[bpsar]-[a-zA-Z0-9\-]{20,}', "slack_token"),
    (r'AIza[a-zA-Z0-9_-]{30,}', "google_key"),
    (r'AKIA[A-Z0-9]{16}', "aws_access_key"),
    (r'pplx-[a-zA-Z0-9]{40,}', "perplexity_key"),
    (r'fal_[a-zA-Z0-9_-]{20,}', "fal_key"),
    (r'fc-[a-zA-Z0-9]{30,}', "fireworks_key"),
    (r'sk_live_[a-zA-Z0-9]{20,}', "stripe_key"),
    (r'SG\.[a-zA-Z0-9_-]{20,}', "sendgrid_key"),
    (r'hf_[a-zA-Z0-9]{20,}', "huggingface_key"),
    (r'npm_[a-zA-Z0-9]{20,}', "npm_token"),
    (r'pypi-[a-zA-Z0-9]{20,}', "pypi_token"),
    (r'tvly-[a-zA-Z0-9]{20,}', "tavily_key"),

    # Bearer tokens in headers
    (r'[Bb]earer\s+[a-zA-Z0-9_\-\.]{20,}', "bearer_token"),

    # Generic patterns
    (r'(?i)(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|auth[_-]?token|password)\s*[=:]\s*["\']?[a-zA-Z0-9_\-\.]{16,}["\']?', "key_value"),

    # Connection strings with passwords
    (r'(?i)(postgres|mysql|mongodb|redis)://[^:]+:[^@]+@', "connection_string"),

    # Private keys
    (r'-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----', "private_key"),
]

_COMPILED = [(re.compile(p), name) for p, name in _PATTERNS]


def redact(text: str) -> str:
    """Redact secrets from text. Returns cleaned text."""
    if not text:
        return text
    result = text
    for pattern, name in _COMPILED:
        def _mask(m):
            val = m.group(0)
            if len(val) < 18:
                return "***"
            return val[:6] + "..." + val[-4:]
        result = pattern.sub(_mask, result)
    return result


def contains_secrets(text: str) -> bool:
    """Check if text contains any secret patterns."""
    if not text:
        return False
    for pattern, _ in _COMPILED:
        if pattern.search(text):
            return True
    return False
