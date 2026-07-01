"""Theme Designer tool — single atomic tool that defines the global theme.

Theme is stored on the AgentState as a plain dict (the LLM picks the shape),
then coerced to ThemeDef at render time.
"""

from __future__ import annotations

from typing import Any, Dict

from shuttleslide.agent.tools.registry import ToolResult, tool


# ---------------------------------------------------------------------------
# WCAG 2.1 contrast math — used to reject themes where text/title would be
# unreadable on the chosen background. See ECMA-376 / W3C WCAG 2.1 §1.4.3.
# Returns ratio in [1.0, 21.0]. AA thresholds: 4.5:1 body, 3.0:1 large text.
# ---------------------------------------------------------------------------


def _relative_luminance(hex_str: str) -> float:
    """sRGB hex → relative luminance (0.0 = black, 1.0 = white)."""
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))

    def _channel(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def _contrast_ratio(fg: str, bg: str) -> float:
    """WCAG contrast ratio between two hex colors. Range 1.0–21.0."""
    l_fg = _relative_luminance(fg)
    l_bg = _relative_luminance(bg)
    lighter, darker = max(l_fg, l_bg), min(l_fg, l_bg)
    return (lighter + 0.05) / (darker + 0.05)


# WCAG AA thresholds. title is treated as large text (≥ 3:1); body as normal (≥ 4.5:1).
_TITLE_MIN_CONTRAST = 3.0
_TEXT_MIN_CONTRAST = 4.5


@tool(
    name="define_theme",
    description=(
        "Define the global theme for the slide deck. Call this ONCE with all fields. "
        "The theme will be locked and applied to every slide."
    ),
    params={
        "type": "object",
        "properties": {
            "primary_color": {
                "type": "string",
                "description": "Main brand color, 6-digit hex (#RRGGBB). Used for titles, bars, primary accents. Pick a color that fits the style hint; do NOT reuse any example value verbatim.",
            },
            "accent_color": {
                "type": "string",
                "description": "Secondary highlight color (#RRGGBB). Icons, dividers, key data.",
            },
            "warn_color": {
                "type": "string",
                "description": "Alert color (#RRGGBB). Used sparingly.",
            },
            "bg_color": {
                "type": "string",
                "description": "Default slide background. Light decks: near-white; dark decks: near-black.",
            },
            "text_color": {
                "type": "string",
                "description": "Default body text color. Must contrast with bg_color at WCAG AA (>= 4.5:1) or the tool rejects it.",
            },
            "title_color": {
                "type": "string",
                "description": "Title text color. Must contrast with bg_color at WCAG AA (>= 3:1 for large text). White/near-white for dark decks, primary_color or dark for light decks.",
            },
            "font_title": {
                "type": "string",
                "description": "Font for titles. Roboto is safe; consider Inter, Playfair Display, Noto Sans SC.",
            },
            "font_body": {
                "type": "string",
                "description": "Font for body text.",
            },
            "decoration_style": {
                "type": "string",
                "enum": ["minimal", "glassmorphism", "neon", "editorial", "playful"],
                "description": "Controls decorative elements (blur_glow, gradients, badges).",
            },
            "cover_bg_strategy": {
                "type": "string",
                "enum": ["dark_gradient", "image_overlay", "solid_color", "geometric"],
                "description": "How the title slide (slide 1) background should look.",
            },
            "layout_conventions": {
                "type": "string",
                "description": "1-3 sentence description of how content slides are laid out.",
            },
        },
        "required": [
            "primary_color",
            "accent_color",
            "bg_color",
            "text_color",
            "title_color",
            "font_title",
            "font_body",
            "decoration_style",
            "cover_bg_strategy",
            "layout_conventions",
        ],
    },
    groups=["theme_builder"],
)
async def define_theme(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    state = ctx.get("state")
    if state is None:
        return ToolResult.failure("no state in context")

    required = [
        "primary_color", "accent_color", "bg_color", "text_color", "title_color",
        "font_title", "font_body", "decoration_style", "cover_bg_strategy",
        "layout_conventions",
    ]
    missing = [r for r in required if not params.get(r)]
    if missing:
        return ToolResult.failure(f"missing required fields: {', '.join(missing)}")

    # Normalize hex colors: LLMs frequently drop the leading "#".
    # Be lenient — strip whitespace and prepend "#" if missing, instead of
    # forcing a retry round-trip. Reject only if the value isn't a plausible
    # hex string (3 or 6 hex digits after any leading "#").
    color_fields = ("primary_color", "accent_color", "bg_color", "text_color", "title_color")
    for color_field in color_fields:
        val = params.get(color_field)
        if not isinstance(val, str):
            return ToolResult.failure(
                f"{color_field} must be a hex color string, got {type(val).__name__}"
            )
        cleaned = val.strip().lstrip("#")
        if len(cleaned) not in (3, 6) or any(c not in "0123456789abcdefABCDEF" for c in cleaned):
            return ToolResult.failure(
                f"{color_field} must be a hex color (e.g. #00D4FF), got {val!r}"
            )
        params[color_field] = f"#{cleaned}"

    # WCAG AA contrast gate — reject themes whose title/body text would be
    # illegible on bg_color. Forces the LLM to retry instead of silently
    # shipping dark-on-dark or light-on-light slides. Note: this only
    # checks text against bg_color; cover slides that render text on a
    # primary_color gradient may still need per-slide contrast review.
    title_ratio = _contrast_ratio(params["title_color"], params["bg_color"])
    if title_ratio < _TITLE_MIN_CONTRAST:
        return ToolResult.failure(
            f"title_color {params['title_color']} on bg_color "
            f"{params['bg_color']} has contrast {title_ratio:.2f}:1 "
            f"(need >= {_TITLE_MIN_CONTRAST}:1, WCAG AA for large text). "
            f"Use white or near-white for dark decks; a dark color for light decks."
        )
    text_ratio = _contrast_ratio(params["text_color"], params["bg_color"])
    if text_ratio < _TEXT_MIN_CONTRAST:
        return ToolResult.failure(
            f"text_color {params['text_color']} on bg_color "
            f"{params['bg_color']} has contrast {text_ratio:.2f}:1 "
            f"(need >= {_TEXT_MIN_CONTRAST}:1, WCAG AA for body text). "
            f"Pick a color with higher contrast against the background."
        )

    # Store the full dict on the state.
    state.theme = dict(params)
    return ToolResult.success(
        f"theme defined: primary={params['primary_color']} "
        f"accent={params['accent_color']} style={params['decoration_style']}"
    )
