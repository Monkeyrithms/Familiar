"""Theme shim — exposes Familiar's ``PALETTE`` under the key names the ported
tour engine (popup, spotlight, …) expects.

Brikwerx 3's tour modules read a palette dict ``T`` keyed ``brand / panel /
panel_2 / border / text / sub``. Familiar's palette (``ui.theme.PALETTE``) uses
``accent / panel / panel_alt / border / text / muted_text``. Rather than touch
every ported file, this one place maps between them.

``T`` is a *live* view: ``ui.theme.refresh_palette()`` mutates ``PALETTE`` in
place (it never rebinds the name), so every lookup here reflects the current
theme — the tour re-tints automatically when the user changes their base color.
"""
from __future__ import annotations

from ui.theme import PALETTE

# B3 tour key  →  Familiar PALETTE key
_MAP = {
    "brand":   "accent",
    "panel":   "panel",
    "panel_2": "panel_alt",
    "border":  "border",
    "text":    "text",
    "sub":     "muted_text",
    "glow":    "glow_hot",
    "danger":  "danger",
}

# Sensible fallbacks if a palette is ever missing a key.
_FALLBACK = {
    "brand": "#4ECDC4", "panel": "#141414", "panel_2": "#101010",
    "border": "#2a2a2a", "text": "#e6e6e6", "sub": "#888888",
    "glow": "#aeffff", "danger": "#ff5555",
}


class _ThemeView:
    """Dict-like, read-only, always resolves against the *current* PALETTE."""

    def __getitem__(self, key: str) -> str:
        pal_key = _MAP.get(key, key)
        val = PALETTE.get(pal_key)
        if not val:
            val = PALETTE.get(key) or _FALLBACK.get(key, "#4ECDC4")
        return val

    def get(self, key: str, default=None):
        try:
            return self[key]
        except Exception:
            return default


T = _ThemeView()
