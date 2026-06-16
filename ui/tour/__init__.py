"""Familiar first-run tour — the "digital origami" onboarding.

On a fresh install the window boots as nothing but a small agent chat card,
centered on screen. A fully hardcoded (zero-LLM, zero-cost) guided tour then
unfolds the rest of the app piece by piece — the window grows, the title bar
erupts, the tabbed workspace slides open, and each tool blinks/glows in turn,
narrated through the chat with a typewriter effect so it reads like Familiar
itself is giving the walkthrough.

Ported from Brikwerx 3's tour engine; the animation/popup/spotlight/markup
layer is near-verbatim, while the director + script are rewritten for
Familiar's chat-centric layout.

Entry point: ``TourDirector`` (director.py). ``TourDirector.needed()`` decides
whether this launch should start in genesis mode.
"""
from .director import TourDirector

__all__ = ["TourDirector"]
