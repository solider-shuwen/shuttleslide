"""Single source of truth for the agent ↔ html_to_pptx contract.

This module declares exactly which HTML/CSS patterns the agent may emit
and which patterns `html_to_pptx` is guaranteed to render 1:1 to PPTX.
Both sides reference these constants:

- `agent/html_guide.py` builds its FORBIDDEN / ALLOWED markdown sections
  from these constants, so the prompt the LLM sees is always in sync.
- `agent/tools/slide_tools.py::_validate_free_form_html` imports
  `FORBIDDEN_CSS` and `FORBIDDEN_TAILWIND_CLASSES` to lint the HTML
  before it reaches the renderer, returning actionable error messages
  so the LLM can retry with corrected HTML.
- `html_to_pptx/rule/classifier.py` and `rule/converter.py` are the
  downstream consumers; when their behavior changes, update
  `RECOGNIZED_PATTERNS[*].converter_ref` to match.

The goal is to make "the agent emits X" ⇔ "html_to_pptx renders X
perfectly" an enforceable invariant instead of an implicit hope.
"""

from __future__ import annotations

from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Allowed CSS — properties and value shapes that html_to_pptx renders 1:1.
# ---------------------------------------------------------------------------
# Each entry maps a CSS property name to a short description of the value
# shapes the converter actually understands. Lint does NOT enforce value
# shapes here (too brittle); this dict is documentation + future test
# generator input.
ALLOWED_CSS: Dict[str, str] = {
    "color": "hex (#RGB / #RRGGBB / #RRGGBBAA), rgb(), rgba()",
    "background-color": "hex, rgb(), rgba()",
    "background": "solid color or linear-gradient(<angle>deg, <color> <pct>%, ...) with any number of stops",
    "border": "<width>px <style> <color>",
    "border-radius": "<int>px or 9999px (see FORBIDDEN notes on shape constraints)",
    "border-color": "hex / rgb()",
    "border-width": "<int>px",
    "font-family": "font stack with comma-separated names",
    "font-size": "<int>px (REQUIRED unit — no rem/em/%)",
    "font-weight": "normal | bold | 100..900",
    "font-style": "normal | italic",
    "text-align": "left | center | right | justify",
    "text-decoration": "underline | line-through | none",
    "line-height": "<number> (unitless multiplier) or <int>px",
    "letter-spacing": "<number>px",
    "opacity": "<float 0..1>",
    "padding": "<int>px (1-4 values)",
    "margin": "<int>px (1-4 values)",
    "width": "<int>px | <int>% (of parent)",
    "height": "<int>px | <int>%",
    "box-shadow": "single layer only: <Xpx> <Ypx> <BlurPx> <Color>",
    "display": "flex | grid | block | inline | none",
    "flex-direction": "row | column",
    "justify-content": "flex-start | center | flex-end | space-between | space-around",
    "align-items": "flex-start | center | flex-end | stretch",
    "gap": "<int>px",
    "grid-template-columns": "repeat(<n>, 1fr) | <int>fr <int>fr ...",
    "position": "absolute (top-level only — nested absolute is forbidden) | relative | static",
    "top": "<int>px",
    "left": "<int>px",
    "right": "<int>px",
    "bottom": "<int>px",
    "transform": "rotate(<int>deg) only — see FORBIDDEN for translate/scale/skew",
    "filter": "blur(<int>px) on decorative blur_glow only; drop-shadow(<Xpx> <Ypx> <Bpx> <color>) on <i class=\"material-icons\"> only",
    "z-index": "<int>",
}


# ---------------------------------------------------------------------------
# Forbidden CSS — properties/patterns lint will reject.
# ---------------------------------------------------------------------------
# Each entry: (css_property_or_pattern, reason, replacement)
# `reason` explains WHY (so the LLM understands the constraint), and
# `replacement` is the actionable alternative the LLM should use.
FORBIDDEN_CSS: List[Tuple[str, str, str]] = [
    (
        "backdrop-filter",
        "PPTX has no equivalent for glassmorphism / backdrop blur.",
        "Use background-color: rgba(R,G,B,0.7) + a 1px border with rgba() color for a similar translucent card look.",
    ),
    (
        "background-blend-mode",
        "PPTX does not support CSS compositing modes.",
        "Use a single background-color or a single linear-gradient.",
    ),
    (
        "mix-blend-mode",
        "PPTX does not support element-level blend modes.",
        "Layer elements with explicit opacity / rgba colors instead.",
    ),
    (
        "animation",
        "PPTX is static — animations are ignored by the screenshot extractor.",
        "Remove. If motion is essential, accept it will not appear in PPTX.",
    ),
    (
        "transition",
        "PPTX is static — transitions are ignored.",
        "Remove.",
    ),
    (
        "@keyframes",
        "PPTX is static — keyframes are ignored.",
        "Remove.",
    ),
    (
        "transform: translate",
        "Translator offsets confuse the bbox extractor (element appears at its pre-transform rect).",
        "Use flex/grid layout, or set position: absolute with top/left to place elements.",
    ),
    (
        "transform: scale",
        "Scale transforms distort bbox extraction.",
        "Set the actual width/height in px.",
    ),
    (
        "transform: skew",
        "Skew has no PPTX equivalent.",
        "Remove or use a simple rotation.",
    ),
    (
        "filter: drop-shadow",
        "Only allowed on <i class=\"material-icons\"> elements (see ICON+GLOW pattern). Other tags lose the effect.",
        "Use box-shadow: <Xpx> <Ypx> <BlurPx> <Color> on the element directly.",
    ),
    (
        "filter: blur",
        "Only allowed on decorative blur_glow elements (see BLUR GLOW pattern in the guide).",
        "If you need a soft glow, use the BLUR GLOW pattern: a positioned div with filter: blur(Npx) and low opacity.",
    ),
    (
        "position: absolute (nested)",
        "Nested absolute positioning causes z-order and bbox overlaps the extractor cannot disentangle.",
        "Flatten: only top-level absolute (direct child of the .ppt-slide root). Use relative+flex inside cards.",
    ),
    (
        "font-size with rem/em/%",
        "The px→pt converter only reads inline px values. rem/em/% silently become wrong sizes.",
        "Use explicit px: font-size: 24px (not 1.5rem, not 150%).",
    ),
    (
        "box-shadow with multiple layers",
        "Only one outerShdw is rendered. Comma-separated shadows after the first are dropped.",
        "Use a single box-shadow: <Xpx> <Ypx> <BlurPx> <Color>.",
    ),
    (
        "box-shadow with inset",
        "Inset shadows have no PPTX equivalent.",
        "Use a border or an inner element with its own background.",
    ),
    (
        "box-shadow with spread (4th length)",
        "Spread is not read by the shadow parser.",
        "Drop the spread value: box-shadow: <X> <Y> <Blur> <Color>.",
    ),
]


# ---------------------------------------------------------------------------
# Forbidden Tailwind utility classes — same idea, for class-based effects.
# ---------------------------------------------------------------------------
# Listed as raw class names; the linter checks for exact match or
# prefix match (for variant classes like backdrop-blur-md).
FORBIDDEN_TAILWIND_CLASSES: List[str] = [
    "backdrop-blur",
    "backdrop-blur-sm",
    "backdrop-blur-md",
    "backdrop-blur-lg",
    "backdrop-blur-xl",
    "backdrop-blur-2xl",
    "backdrop-blur-3xl",
    "backdrop-saturate",
    "backdrop-grayscale",
    "drop-shadow",        # forbidden except on <i class="material-icons"> — checked separately
    "drop-shadow-sm",
    "drop-shadow-md",
    "drop-shadow-lg",
    "drop-shadow-xl",
    "drop-shadow-2xl",
    "mix-blend-multiply",
    "mix-blend-screen",
    "mix-blend-overlay",
    "animate-pulse",
    "animate-spin",
    "animate-bounce",
    "animate-ping",
    "animate-ping-sm",
]

# Tailwind classes that are restricted — allowed only on specific element
# types. The linter checks context before rejecting.
RESTRICTED_TAILWIND_CLASSES: Dict[str, str] = {
    # class_name -> description of when it IS allowed
    "absolute": "Allowed on top-level decorative elements (blur_glow, gradient_overlay) directly inside .ppt-slide. FORBIDDEN inside another position:absolute ancestor.",
    "fixed": "Generally forbidden — use absolute at most.",
    "translate-x-0": "Forbidden — use left/right positioning instead.",
    "translate-y-0": "Forbidden — use top/bottom positioning instead.",
}


# ---------------------------------------------------------------------------
# Recognized element patterns — what the classifier in
# html_to_pptx/rule/classifier.py knows how to identify.
# ---------------------------------------------------------------------------
# Each entry documents the pattern's signature and points at the
# classifier + converter that handle it. When behavior changes there,
# update the line refs here. (Refs are advisory — they will go stale as
# the code evolves; treat them as breadcrumbs, not ground truth.)
RECOGNIZED_PATTERNS: List[Dict[str, str]] = [
    {
        "name": "CARD",
        "signature": "Bordered container, width > 25% of slide, height > 5%, has bg-color/gradient, has border-radius > 0, ≥1 child with text.",
        "classifier_ref": "rule/classifier.py::_check_card",
        "converter_ref": "rule/converter.py::_to_card",
        "renderer_ref": "renderer.py::_render_card",
    },
    {
        "name": "BADGE",
        "signature": "Small pill/tag, width < 25%, height < 18%, has text, has bg-color, has border-radius.",
        "classifier_ref": "rule/classifier.py::_check_badge",
        "converter_ref": "rule/converter.py::_to_badge",
        "renderer_ref": "renderer.py::_render_badge",
    },
    {
        "name": "TITLE_BAR",
        "signature": "Top 15% of slide, width > 90%, height < 20%, has bg or gradient, has text.",
        "classifier_ref": "rule/classifier.py::_check_title_bar",
        "converter_ref": "rule/converter.py::_to_title_bar",
        "renderer_ref": "renderer.py::_render_title_bar",
    },
    {
        "name": "DIVIDER_LINE",
        "signature": "Height < 1.5% (~10px), no text, has bg-color, width > 20%.",
        "classifier_ref": "rule/classifier.py::_check_divider_line",
        "converter_ref": "rule/converter.py::_to_divider_line",
        "renderer_ref": "renderer.py::_render_divider_line",
    },
    {
        "name": "NUMBERED_STEP",
        "signature": "Square-ish number circle < 10% width, < 12% height, text matches /^\\d+[.)]?$/, sequence starts at 1.",
        "classifier_ref": "rule/classifier.py::_check_numbered_step",
        "converter_ref": "rule/converter.py::_to_numbered_step",
        "renderer_ref": "renderer.py::_render_numbered_step",
    },
    {
        "name": "BULLET_LIST",
        "signature": "<ul><li> with ≥2 siblings, or element with class containing 'list'/'bullet'/'check'.",
        "classifier_ref": "rule/classifier.py::_check_bullet_list",
        "converter_ref": "rule/converter.py::_to_bullet_list",
        "renderer_ref": "renderer.py::_render_bullet_list",
    },
    {
        "name": "TABLE",
        "signature": "Real <table> with aligned rows of same schema. Cells render as separate text_boxes.",
        "classifier_ref": "rule/classifier.py (table detector)",
        "converter_ref": "rule/converter.py::_to_table",
        "renderer_ref": "renderer.py::_render_table",
    },
    {
        "name": "ICON_TEXT",
        "signature": "<i class=\"material-icons\"> (possibly with adjacent text span).",
        "classifier_ref": "rule/classifier.py::_check_icon_text",
        "converter_ref": "rule/converter.py::_to_icon_text",
        "renderer_ref": "renderer.py::_render_icon_text",
    },
    {
        "name": "ICON_TEXT_WITH_GLOW",
        "signature": "<i class=\"material-icons\"> with style=\"filter: drop-shadow(Xpx Ypx Bpx color);\" → renders as outerShdw.",
        "classifier_ref": "rule/classifier.py::_check_icon_text",
        "converter_ref": "rule/converter.py::_to_icon_text (icon_shadow field)",
        "renderer_ref": "renderer.py::_try_render_vector_icon → _apply_shape_shadow",
    },
    {
        "name": "IMAGE",
        "signature": "<img> with src=data: or http(s):. Supports object-fit cover/contain/fill.",
        "classifier_ref": "rule/classifier.py::_check_image",
        "converter_ref": "rule/converter.py::_to_image",
        "renderer_ref": "renderer.py::_render_image",
    },
    {
        "name": "SHAPE",
        "signature": "Rectangle / rounded rectangle / circle / oval from a styled <div>.",
        "classifier_ref": "rule/classifier.py::_check_shape",
        "converter_ref": "rule/converter.py::_to_shape",
        "renderer_ref": "renderer.py::_render_shape",
    },
    {
        "name": "GRADIENT_OVERLAY",
        "signature": "Gradient bg, width > 30%, height > 20%, no text.",
        "classifier_ref": "rule/classifier.py::_check_gradient_overlay",
        "converter_ref": "rule/converter.py::_to_gradient_overlay",
        "renderer_ref": "renderer.py::_render_gradient_overlay",
    },
    {
        "name": "BLUR_GLOW",
        "signature": "No text, low opacity (<0.5) OR low-alpha bg, AND (square-ish OR big radius OR has filter: blur).",
        "classifier_ref": "rule/classifier.py::_check_blur_glow",
        "converter_ref": "rule/converter.py::_to_blur_glow",
        "renderer_ref": "renderer.py::_render_blur_glow",
    },
    {
        "name": "SVG_NATIVE",
        "signature": (
            "Inline <svg data-slot=\"...\"> element produced by the SVG "
            "Generator stage. The html_to_pptx converter extracts this "
            "fragment and converts it to multiple editable native PPT "
            "shapes via the vendored svg_to_pptx package (rect/circle/"
            "line/path/text/etc.)."
        ),
        "classifier_ref": "rule/classifier.py::_check_svg_native (Phase 2)",
        "converter_ref": "rule/converter.py::_to_svg_native via svg_handler.py (Phase 2)",
        "renderer_ref": "renderer.py (Phase 2)",
    },
]


# ---------------------------------------------------------------------------
# Markdown renderers — used by html_guide.py to inject contract text
# into the LLM prompt. Keeping generation here means the prompt never
# drifts from the constants above.
# ---------------------------------------------------------------------------

def render_forbidden_css_markdown() -> str:
    """Render FORBIDDEN_CSS as a markdown list for LLM consumption."""
    lines = ["== FORBIDDEN CSS PROPERTIES (the linter will reject these) ==", ""]
    for prop, reason, replacement in FORBIDDEN_CSS:
        lines.append(f"- `{prop}` — {reason}")
        lines.append(f"  REPLACE WITH: {replacement}")
    return "\n".join(lines)


def render_forbidden_tailwind_markdown() -> str:
    """Render forbidden Tailwind classes as a markdown list."""
    lines = ["== FORBIDDEN TAILWIND CLASSES (the linter will reject these) ==", ""]
    lines.append("- " + ", ".join(f".{c}" for c in FORBIDDEN_TAILWIND_CLASSES))
    lines.append("")
    lines.append("Restricted (context-dependent):")
    for cls, when in RESTRICTED_TAILWIND_CLASSES.items():
        lines.append(f"- `.{cls}` — {when}")
    return "\n".join(lines)


def render_allowed_css_summary() -> str:
    """Compact one-line-per-property summary of ALLOWED_CSS for the prompt."""
    lines = ["== SUPPORTED CSS (use these freely) ==", ""]
    for prop, shape in ALLOWED_CSS.items():
        lines.append(f"- `{prop}`: {shape}")
    return "\n".join(lines)
