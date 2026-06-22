"""Theme Designer tool — single atomic tool that defines the global theme.

Theme is stored on the AgentState as a plain dict (the LLM picks the shape),
then coerced to ThemeDef at render time.
"""

from __future__ import annotations

from typing import Any, Dict

from shuttleslide.agent.tools.registry import ToolResult, tool


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
                "description": "Main brand color, hex like #133EFF. Used for titles, bars, primary accents.",
            },
            "accent_color": {
                "type": "string",
                "description": "Secondary highlight color (e.g. #00CD82). Icons, dividers, key data.",
            },
            "warn_color": {
                "type": "string",
                "description": "Alert color (e.g. #FF5722). Used sparingly.",
            },
            "bg_color": {
                "type": "string",
                "description": "Default slide background. Light decks: #FEFEFE; dark decks: #0a0e27.",
            },
            "text_color": {
                "type": "string",
                "description": "Default body text color. Must contrast with bg_color.",
            },
            "title_color": {
                "type": "string",
                "description": "Title text color. Often white for dark decks, primary_color for light decks.",
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

    # Store the full dict on the state.
    state.theme = dict(params)
    return ToolResult.success(
        f"theme defined: primary={params['primary_color']} "
        f"accent={params['accent_color']} style={params['decoration_style']}"
    )
