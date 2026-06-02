"""
Theme system — entire UI derived from one base color + brightness.
"""

from PyQt6.QtGui import QColor

DEFAULT_ACCENT = "#4ECDC4"
DEFAULT_BRIGHTNESS = 0.25


# Palette keys the user can individually override when Monocolor is OFF, with
# human-readable labels. The Settings "Colors" section is built from this list,
# and these are the keys build_palette() will accept overrides for.
OVERRIDE_KEYS = [
    ("accent",        "Accent"),
    ("accent_bright", "Accent (bright / hover)"),
    ("accent_muted",  "Accent (muted)"),
    ("glow_hot",      "Glow / highlight"),
    ("background",    "Background"),
    ("panel",         "Panel surface"),
    ("border",        "Border"),
    ("text",          "Text"),
    ("muted_text",    "Muted text"),
    ("danger",        "Danger / delete"),
]


def build_palette(base_color: str = DEFAULT_ACCENT, brightness: float = DEFAULT_BRIGHTNESS,
                  overrides: dict | None = None) -> dict:
    b = max(0.0, min(3.0, float(brightness)))
    base_val = int(8 + b * 82)  # 0.0 → 8 (near black), 3.0 → 254 (near white)
    panel_val = max(0, min(255, base_val - 4))
    alt_val = max(0, min(255, panel_val - 4))

    # Detect light mode: if background is bright, invert text direction
    is_light = base_val > 140

    def _grey(v):
        v = max(0, min(255, v))
        return QColor(v, v, v).name()

    def _shade(hex_color, factor):
        c = QColor(hex_color)
        r = min(255, max(0, int(c.red() * factor)))
        g = min(255, max(0, int(c.green() * factor)))
        b_val = min(255, max(0, int(c.blue() * factor)))
        return f"#{r:02x}{g:02x}{b_val:02x}"

    def _neon(hex_color):
        """White-hot glow: keep the hue tint, crush saturation, max brightness."""
        c = QColor(hex_color)
        h, s, v, a = c.getHsv()
        return QColor.fromHsv(h, min(s, 80), 255, a).name()

    if is_light:
        # Light mode: darken text colors, keep accent vibrant
        pal = {
            "background": _grey(base_val),
            "panel": _grey(min(255, base_val + 4)),
            "panel_alt": _grey(min(255, base_val + 8)),
            "border": _shade(base_color, 0.6),
            "accent": base_color,
            "accent_bright": _shade(base_color, 1.3),
            "accent_muted": _shade(base_color, 0.7),
            "accent_soft": _shade(base_color, 0.4),
            "glow_hot": _neon(base_color),
            "text": _shade(base_color, 0.3),
            "muted_text": _shade(base_color, 0.5),
            "danger": "#cc3333",
        }
    else:
        # Dark mode (original)
        pal = {
            "background": _grey(base_val),
            "panel": _grey(panel_val),
            "panel_alt": _grey(alt_val),
            "border": _shade(base_color, 0.4),
            "accent": base_color,
            "accent_bright": _shade(base_color, 1.5),
            "accent_muted": _shade(base_color, 0.5),
            "accent_soft": _shade(base_color, 0.3),
            "glow_hot": _neon(base_color),
            "text": _shade(base_color, 1.8),
            "muted_text": _shade(base_color, 0.9),
            "danger": "#ff5555",
        }

    # Per-key user overrides (Monocolor OFF). Any key the user didn't set still
    # derives from the base color, so partial overrides blend with the theme.
    if overrides:
        for k, v in overrides.items():
            if k in pal and isinstance(v, str) and QColor(v).isValid():
                pal[k] = v

    return pal


def load_theme_config() -> tuple[str, float]:
    """Load base_color and brightness from config.json."""
    import json
    from pathlib import Path
    cfg_path = Path(__file__).parent.parent / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        return (cfg.get("base_color", DEFAULT_ACCENT),
                cfg.get("brightness", DEFAULT_BRIGHTNESS))
    except Exception:
        return DEFAULT_ACCENT, DEFAULT_BRIGHTNESS


def load_color_overrides() -> dict:
    """Per-key color overrides from config.json — only honored when Monocolor is
    OFF. Returns {} when Monocolor is on (the pure single-color look)."""
    import json
    from pathlib import Path
    cfg_path = Path(__file__).parent.parent / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if cfg.get("monocolor", True):
            return {}
        ov = cfg.get("color_overrides") or {}
        return ov if isinstance(ov, dict) else {}
    except Exception:
        return {}


_color, _bright = load_theme_config()
PALETTE = build_palette(_color, _bright, load_color_overrides())


def _blend(fg_hex: str, bg_hex: str, t: float) -> QColor:
    """Mix fg over bg by fraction t (0..1). A solid stand-in for a translucent
    overlay, so it renders identically via the palette and via QSS."""
    a, b = QColor(fg_hex), QColor(bg_hex)
    return QColor(
        int(a.red() * t + b.red() * (1 - t)),
        int(a.green() * t + b.green() * (1 - t)),
        int(a.blue() * t + b.blue() * (1 - t)),
    )


def _selection_bg() -> QColor:
    """Dim, translucent-looking selection: the accent softly blended into the
    background — clearly the theme color, without the harsh full-accent block."""
    return _blend(PALETTE["accent"], PALETTE.get("background", "#0b0d0d"), 0.33)


def _selection_text_color() -> str:
    """Readable text over the dimmed selection (perceived luminance / YIQ)."""
    c = _selection_bg()
    lum = (c.red() * 299 + c.green() * 587 + c.blue() * 114) / 1000  # 0–255
    return "#000000" if lum > 140 else "#ffffff"


def selection_css() -> str:
    """QSS snippet giving a widget the dimmed accent selection. Needed for text
    widgets that set their own stylesheet (which otherwise ignores the palette
    Highlight role and falls back to grey)."""
    return (f"selection-background-color: {_selection_bg().name()};"
            f" selection-color: {_selection_text_color()};")


def apply_selection_palette():
    """Point the Qt Highlight role at the dimmed accent so text/list selection
    across the app reflects the theme instead of Fusion's grey. No-op until the
    QApplication exists; safe to call again after a theme change."""
    try:
        from PyQt6.QtWidgets import QApplication
        from PyQt6.QtGui import QPalette
        app = QApplication.instance()
        if app is None:
            return
        pal = app.palette()
        pal.setColor(QPalette.ColorRole.Highlight, _selection_bg())
        pal.setColor(QPalette.ColorRole.HighlightedText, QColor(_selection_text_color()))
        app.setPalette(pal)
    except Exception:
        pass


def refresh_palette():
    """Reload palette from config. Call after settings change."""
    global PALETTE
    _color, _bright = load_theme_config()
    PALETTE.update(build_palette(_color, _bright, load_color_overrides()))
    apply_selection_palette()


