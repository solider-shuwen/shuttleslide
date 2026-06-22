"""Slide-builder tools (free-form HTML authoring).

The slide-builder authors the inner HTML of each slide directly via
`set_free_form_html`. The system wraps the HTML in a fixed 1280x720
.ppt-slide container; the HTML authoring guide in
`shuttleslide.agent.html_guide` defines the patterns the PPTX converter
recognizes (cards, badges, numbered steps, etc.).

Tools are tagged with group="slide_builder" so the slide-builder node
can expose them to the LLM.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from shuttleslide.agent.html_contract import (
    FORBIDDEN_CSS,
    FORBIDDEN_TAILWIND_CLASSES,
)
from shuttleslide.agent.tools.registry import ToolResult, tool
from shuttleslide.html_to_pptx.schema import SlideDSL


# ---------------------------------------------------------------------------
# free_form HTML sanitization
# ---------------------------------------------------------------------------

# Substrings that must not appear in free_form HTML (case-insensitive).
# Covers the main XSS + html_to_pptx-incompatible vectors.
_FORBIDDEN_HTML_PATTERNS: List[str] = [
    "<script",
    "</script",
    "<iframe",
    "</iframe",
    "<object",
    "</object",
    "<embed",
    "</embed",
    "<link",
    "</link",
    "javascript:",
    "<style",   # delegate styling to Tailwind utilities + inline style="..."
    "</style",
    "<form",
    "</form",
]

# Max length of free_form HTML. Generous enough for a full slide body, tight
# enough to stop the LLM from dumping a giant blob.
_FREE_FORM_HTML_MAX_LEN = 12000


# Regex for finding <i class="material-icons" ...> opening tags. The CSS
# linter uses the spans to whitelist `filter: drop-shadow(...)` only on
# icon elements (the ICON+GLOW pattern). Material Symbols class is also
# recognised.
_ICON_TAG_RE = re.compile(
    r"<i\b[^>]*\bclass\s*=\s*[\"'][^\"']*\b(?:material-icons|material-symbols[\w-]*)\b[^\"']*[\"'][^>]*?>",
    re.IGNORECASE,
)

# Regex for finding every inline style="..." (or style='...') block. The
# second capture group is the declaration list; match.start(2) gives the
# offset of the declarations inside the HTML, which we use to detect
# whether the style block lives inside an <i class="material-icons"> tag.
_STYLE_BLOCK_RE = re.compile(
    r"\bstyle\s*=\s*(\"([^\"]*)\"|'([^']*)')",
    re.IGNORECASE | re.DOTALL,
)

# Regex for the class="..." attribute, used by the Tailwind linter.
_CLASS_ATTR_RE = re.compile(
    r"\bclass\s*=\s*[\"']([^\"']+)[\"']",
    re.IGNORECASE,
)


def _is_position_in_icon_tag(html: str, pos: int, icon_spans: List[Tuple[int, int]]) -> bool:
    """Return True if `pos` lies inside one of the icon opening-tag spans."""
    for start, end in icon_spans:
        if start <= pos <= end:
            return True
    return False


def _lint_css_declarations(style_str: str, in_icon: bool) -> List[str]:
    """Return actionable error strings for any forbidden CSS in `style_str`.

    `in_icon` is True when the style block lives inside a Material Icons
    `<i>` tag — that context unlocks `filter: drop-shadow(...)` (ICON+GLOW
    pattern) and `filter: blur(...)` is allowed on decorative blur_glow
    elements elsewhere, so neither is rejected here on its own.
    """
    errors: List[str] = []
    for raw_decl in style_str.split(";"):
        decl = raw_decl.strip()
        if not decl or ":" not in decl:
            continue
        prop, _, value = decl.partition(":")
        prop = prop.strip().lower()
        value = value.strip().lower()

        # 1) Hard-blacklist properties (no PPTX equivalent at all).
        if prop in {
            "backdrop-filter",
            "background-blend-mode",
            "mix-blend-mode",
            "animation",
            "transition",
        }:
            errors.append(
                f"CSS `{prop}` is forbidden — PPTX has no equivalent. "
                f"See FORBIDDEN CSS PROPERTIES in the HTML AUTHORING GUIDE."
            )
            continue

        # 2) transform: translate/scale/skew forbidden (rotate ok).
        if prop == "transform":
            for bad in ("translate", "scale", "skew"):
                if bad in value:
                    errors.append(
                        f"CSS `transform: {bad}(...)` is forbidden — bbox "
                        f"extraction cannot follow it. Use flex/grid layout or "
                        f"position: absolute with top/left instead."
                    )

        # 3) filter: drop-shadow — only allowed inside <i class=\"material-icons\">.
        elif prop == "filter":
            if "drop-shadow" in value and not in_icon:
                errors.append(
                    f"CSS `filter: drop-shadow(...)` is only allowed on "
                    f"<i class=\"material-icons\"> elements (ICON+GLOW pattern). "
                    f"On other tags, use box-shadow: <Xpx> <Ypx> <BlurPx> <Color>."
                )

        # 4) font-size unit must be px.
        elif prop == "font-size":
            # Allow px and unitless numbers; reject rem/em/%.
            if (
                value.endswith("rem")
                or value.endswith("em")
                or value.endswith("%")
                or "vw" in value
                or "vh" in value
            ):
                errors.append(
                    f"CSS `font-size: {value}` — only px is supported by the "
                    f"px→pt converter. Use explicit px (e.g. font-size: 24px)."
                )

        # 5) box-shadow restrictions: no inset, no multiple layers.
        elif prop == "box-shadow":
            if "inset" in value:
                errors.append(
                    f"CSS `box-shadow: ...inset...` — inset shadows have no "
                    f"PPTX equivalent. Use a border or an inner element with "
                    f"its own background."
                )
            # Multiple shadow layers are separated by top-level commas. Commas
            # inside rgb()/rgba()/hsl() colour args must NOT count, so strip
            # parenthesised groups before checking for a separator.
            value_outside_parens = re.sub(r"\([^()]*\)", "", value)
            if "," in value_outside_parens:
                errors.append(
                    f"CSS `box-shadow` with multiple comma-separated layers — "
                    f"only one outer shadow is rendered. Consolidate to a "
                    f"single `box-shadow: <Xpx> <Ypx> <BlurPx> <Color>`."
                )

    return errors


def _lint_tailwind_classes(html: str) -> List[str]:
    """Return error strings for forbidden Tailwind utility classes."""
    errors: List[str] = []
    for match in _CLASS_ATTR_RE.finditer(html):
        classes = match.group(1).split()
        for cls in classes:
            if cls in FORBIDDEN_TAILWIND_CLASSES:
                errors.append(
                    f"Tailwind class `.{cls}` is forbidden — it produces a CSS "
                    f"effect the PPTX converter cannot represent. See FORBIDDEN "
                    f"TAILWIND CLASSES in the HTML AUTHORING GUIDE."
                )
    return errors


def _validate_free_form_html(html: Any) -> Optional[str]:
    """Return error string if `html` fails free_form sanitization, else None."""
    if not isinstance(html, str):
        return f"html must be a string, got {type(html).__name__}"
    if not html.strip():
        return "html must not be empty"
    if len(html) > _FREE_FORM_HTML_MAX_LEN:
        return (
            f"html is too long: {len(html)} chars "
            f"(max {_FREE_FORM_HTML_MAX_LEN}). Trim the content."
        )
    lowered = html.lower()
    for pat in _FORBIDDEN_HTML_PATTERNS:
        if pat in lowered:
            return (
                f"html contains forbidden pattern {pat!r}. "
                f"Free-form HTML must not include <script>, <iframe>, <object>, "
                f"<embed>, <link>, <style>, <form>, or javascript: URLs."
            )
    # Reject inline event handlers: on<event>="..." or on<event>='...'
    if re.search(r"\son[a-z]+\s*=", lowered):
        return (
            "html contains an inline event handler (on*=). "
            "Event handlers are forbidden in free-form HTML."
        )

    # CSS-level lint. Collect every error so the LLM sees all violations
    # at once and can fix them in a single retry, rather than whack-a-mole.
    icon_spans: List[Tuple[int, int]] = []
    for m in _ICON_TAG_RE.finditer(html):
        icon_spans.append((m.start(), m.end()))

    css_errors: List[str] = []
    for m in _STYLE_BLOCK_RE.finditer(html):
        style_str = m.group(2) if m.group(2) is not None else m.group(3)
        if not style_str:
            continue
        in_icon = _is_position_in_icon_tag(html, m.start(2), icon_spans)
        css_errors.extend(_lint_css_declarations(style_str, in_icon))

    css_errors.extend(_lint_tailwind_classes(html))

    if css_errors:
        # Cap the list to 5 so the error stays readable; the LLM only needs
        # enough to know what to fix.
        head = css_errors[:5]
        suffix = "" if len(css_errors) <= 5 else f"\n... and {len(css_errors) - 5} more"
        return (
            f"html contains {len(css_errors)} CSS/Tailwind contract "
            f"violation(s):\n" + "\n".join(f"  - {e}" for e in head) + suffix
        )
    return None


# ---------------------------------------------------------------------------
# Background + HTML tools
# ---------------------------------------------------------------------------

@tool(
    name="set_slide_background",
    description=(
        "Set the slide's background. strategy is one of: "
        "'image_overlay' (full-bleed image with dark gradient overlay for text readability — "
        "use for hero covers), "
        "'gradient' (linear gradient using theme primary/accent colors), "
        "'solid' (single color, default for content slides), "
        "'geometric' (subtle gradient with low-opacity color washes). "
        "For 'image_overlay', provide image_url. For 'solid' or 'gradient', "
        "color is derived from theme automatically."
    ),
    params={
        "type": "object",
        "properties": {
            "strategy": {
                "type": "string",
                "enum": ["image_overlay", "gradient", "solid", "geometric"],
            },
            "image_url": {"type": "string"},
        },
        "required": ["strategy"],
    },
    groups=["slide_builder"],
)
async def set_slide_background(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    slide = ctx.get("slide")
    if slide is None:
        return ToolResult.failure("no slide in context")
    theme = ctx.get("theme") or {}
    strategy = params.get("strategy")
    if strategy not in ("image_overlay", "gradient", "solid", "geometric"):
        return ToolResult.failure(f"invalid strategy: {strategy!r}")

    # Store as a slot so the free_form template can render accordingly.
    bg_slot: Dict[str, Any] = {"strategy": strategy}
    if strategy == "image_overlay":
        url = params.get("image_url")
        if not isinstance(url, str) or not url.strip():
            return ToolResult.failure("image_overlay strategy requires image_url")
        bg_slot["image_url"] = url.strip()
    elif strategy == "solid":
        bg_slot["color"] = theme.get("bg_color", "#FEFEFE")
    elif strategy == "gradient":
        # Layout templates will read theme colors; we just flag the strategy.
        pass
    slide.slots["background"] = bg_slot
    return ToolResult.success(f"background strategy: {strategy}")


@tool(
    name="set_free_form_html",
    description=(
        "Set the complete inner HTML for this slide. The system wraps it in a "
        "fixed 1280x720 .ppt-slide container. Use Tailwind utility classes for "
        "layout (flex/grid/spacing), inline style=\"...\" with theme colors and "
        "px font sizes, semantic <h1>/<h2>/<h3>/<p> tags, and Material Icons "
        "(<i class=\"material-icons\">name</i>). Follow the HTML AUTHORING GUIDE "
        "in the system prompt — it lists the patterns the PPTX converter "
        "recognizes (cards, badges, title bars, numbered steps, bullet lists, etc.). "
        "FORBIDDEN: <script>, <iframe>, <object>, <embed>, <link>, <style>, "
        "<form>, javascript: URLs, on*= event handlers, rem/em units, "
        "Tailwind text-size classes. Max 12000 chars. "
        "Use <table> for tabular data — do NOT build tables from flex+span."
    ),
    params={
        "type": "object",
        "properties": {
            "html": {
                "type": "string",
                "description": (
                    "Self-contained HTML fragment for the slide body. Will be "
                    "rendered inside a .ppt-slide 1280x720 container."
                ),
            },
        },
        "required": ["html"],
    },
    groups=["slide_builder"],
)
async def set_free_form_html(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    slide = ctx.get("slide")
    if slide is None:
        return ToolResult.failure("no slide in context")
    html = params.get("html")
    err = _validate_free_form_html(html)
    if err is not None:
        return ToolResult.failure(err)
    slide.slots["html"] = html
    return ToolResult.success(f"slide HTML set ({len(html)} chars)")


# ---------------------------------------------------------------------------
# finish_slide — signals slide completion
# ---------------------------------------------------------------------------

@tool(
    name="finish_slide",
    description=(
        "Call when the slide is complete (HTML has been set via "
        "set_free_form_html). No parameters."
    ),
    params={"type": "object", "properties": {}},
    groups=["slide_builder"],
)
async def finish_slide(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    slide = ctx.get("slide")
    if slide is None:
        return ToolResult.failure("no slide in context")
    html = slide.slots.get("html")
    if not html:
        return ToolResult.failure(
            "cannot finish slide: set_free_form_html has not been called yet"
        )

    # Enforce "declared images must be used". Every image payload the image
    # acquirer produced for this slide (passed via ctx["slide_images"]) must
    # appear verbatim in the HTML — UNLESS the slide-builder explicitly
    # declared it omitted because the image was decorative and the HTML
    # budget was tight. See BUDGET RULES in _format_images_block.
    #
    # Payload shape depends on type:
    #   - svg_file  : {type, path, data, description, image_type, mime, meta}
    #   - image_file: {type, path, description, image_type, mime, meta}
    #   - svg (legacy inline)        : {type, data, mime}
    #   - image (legacy base64 data) : {type, data, mime}
    # svg_file / image_file / image render as an <img data-slot="..."> tag
    # (see _format_images_block in prompts.py), so we substring-match that
    # marker. Legacy inline svg embeds the raw <svg ...> markup verbatim,
    # so we match the opening <svg ...> tag (the positioning wrapper may
    # rewrite surrounding whitespace/attributes).
    slide_images = ctx.get("slide_images") or {}
    image_types = ctx.get("image_types") or {}
    last_reasoning = (ctx.get("last_assistant_reasoning") or "").lower()
    if slide_images:
        missing: List[str] = []
        omitted_declared: List[str] = []
        for slot_id, payload in slide_images.items():
            payload_type = (
                payload.get("type", "svg") if isinstance(payload, dict) else "svg"
            )
            # svg_file / image_file / image payloads all render as an
            # <img data-slot="..."> placeholder in the slide HTML (see
            # _format_images_block in prompts.py). Legacy inline "svg"
            # payloads embed the raw <svg ...> markup verbatim, so we
            # match the opening tag substring instead. The old branch
            # only handled "image" and sent svg_file / image_file down
            # the svg-opening-tag path, where they could never match —
            # causing every retry to fail with "missing image snippet"
            # even when the LLM had correctly placed the placeholder.
            if payload_type in ("svg_file", "image_file", "image"):
                marker = f'data-slot="{slot_id}"'
                if marker in html:
                    continue
            else:  # legacy inline "svg" payload
                data = (
                    payload.get("data", "") if isinstance(payload, dict) else ""
                )
                opening_tag = _extract_svg_opening_tag(data)
                if opening_tag is not None and opening_tag in html:
                    continue
            # Image is missing from HTML. Allow it ONLY when the image is
            # decorative AND the LLM explicitly declared the omission in
            # its last reasoning message. This is the escape hatch that
            # prevents the slide-builder from looping forever when an
            # oversized decorative image + rich content can't both fit.
            img_type = image_types.get(slot_id, "illustration")
            is_decorative = img_type in ("illustration", "icon_cluster")
            declared_omit = (
                f"omitting {slot_id}" in last_reasoning
                or f"omit {slot_id}" in last_reasoning
            )
            if is_decorative and declared_omit:
                omitted_declared.append(slot_id)
                continue
            missing.append(slot_id)
        if missing:
            return ToolResult.failure(
                f"slide HTML is missing {len(missing)} required image snippet(s) "
                f"with slot_id(s): {missing}. Copy each PRE-GENERATED IMAGES "
                f"snippet verbatim into your HTML, then call finish_slide again. "
                f"(If an image is decorative — illustration / icon_cluster — and "
                f"the HTML budget is exhausted, you MAY omit it by stating "
                f"`omitting <slot_id>` in your reasoning. Load-bearing images "
                f"hero / flowchart / diagram / chart must always appear.)"
            )

    return ToolResult.success("slide finished")


# Regex to extract the root <svg ...> opening tag from a stored SVG snippet.
# We use this to do a substring check on the slide HTML: if the slide-builder
# embedded the snippet verbatim (as instructed), the opening tag will appear
# character-for-character in the HTML.
_SVG_OPENING_TAG_RE = re.compile(r"<svg\b[^>]*>", re.IGNORECASE)


def _extract_svg_opening_tag(svg_markup: str) -> Optional[str]:
    """Return the root <svg ...> opening tag, or None if not found."""
    match = _SVG_OPENING_TAG_RE.search(svg_markup)
    return match.group(0) if match else None
