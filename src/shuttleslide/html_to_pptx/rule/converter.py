"""
Playwright-extracted dict → DSL dataclass conversion.

Converts classified elements (raw dicts from JS extraction) into
the typed dataclass instances defined in schema.py.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from shuttleslide.html_to_pptx.schema import (
    BackgroundDef,
    BadgeElement,
    BlurGlowElement,
    BorderDef,
    BulletItem,
    BulletListElement,
    CardElement,
    DividerLineElement,
    GradientDef,
    GradientOverlayElement,
    GradientStop,
    IconTextElement,
    ImageElement,
    NumberedStepElement,
    PositionPercent,
    ShadowDef,
    ShapeElement,
    SlideElementDSL,
    SVGElement,
    TextBoxElement,
    TextBlock,
    TextRun,
    TitleBarElement,
    TableElement,
    TableCell,
)
from shuttleslide.html_to_pptx.rule.classifier import ClassifiedElement, _has_text
from shuttleslide.html_to_pptx.rule.containment import ContainmentTree
from shuttleslide.html_to_pptx.style_mapper import color_opacity, parse_hex_color


# ---------------------------------------------------------------------------
# Helpers — value clamping and parsing for DSL construction
# ---------------------------------------------------------------------------

def _clamp(val, lo, hi, default=None):
    if val is None:
        return default
    try:
        f = float(val)
    except (ValueError, TypeError, OverflowError):
        return default
    if f != f:  # NaN
        return default
    return max(lo, min(hi, f))


def _opacity_from_styles(styles: dict) -> float:
    """Extract element-level opacity (0-1), clamped, defaulting to 1.0.

    Used for TextRun.opacity so semi-transparent text (watermarks, ghosted
    section numbers, hint text) carries through to the renderer, which
    injects <a:alpha> into the run's solidFill.
    """
    try:
        v = float(styles.get("opacity", 1.0))
    except (TypeError, ValueError):
        return 1.0
    if v != v:  # NaN
        return 1.0
    return max(0.0, min(1.0, v))


def _cumulative_opacity(elem: dict) -> float:
    """Read the effective CSS opacity (own × ancestor chain) from Playwright data.

    extract_layout.js walks each element's DOM ancestors and stores the
    multiplied opacity as ``styles.cumulative_opacity``. This matters
    because pure-container wrappers (e.g. ``<div style="opacity:0.25">``
    around an ``<svg>``) are filtered out at extraction time and never
    reach the Python-side containment tree — the only way to recover
    their opacity is to read this pre-computed product.

    Falls back to the element's own opacity when the JS payload predates
    the cumulative_opacity field, so older cached extractions still work.
    """
    styles = elem.get("styles", {}) if elem else {}
    try:
        v = float(styles.get("cumulative_opacity", 1.0))
    except (TypeError, ValueError):
        return _opacity_from_styles(styles)
    if v != v:  # NaN
        return _opacity_from_styles(styles)
    return max(0.0, min(1.0, v))


def _normalize_color_opacity(
    color: Optional[str], opacity: float = 1.0
) -> tuple:
    """Strip alpha from a colour string and merge it into ``opacity``.

    Single source of truth for colour/opacity normalisation at DSL-construction
    time. The renderer's text-run alpha path expects opacity in the schema's
    ``opacity`` field, not embedded in the colour string. Without this, an
    8-hex colour like ``#3b82f614`` (alpha ≈ 0.078) would have its alpha
    silently dropped — ``hex_to_rgbcolor`` only returns RGB, and the existing
    ``_apply_text_run_alpha`` only fires when schema opacity < 1.0. Symptom:
    the ghosted "01" decoration in 3.html rendered at full opacity.

    Accepts anything ``parse_hex_color`` understands
    (#RGB / #RRGGBB / #RRGGBBAA / rgb() / rgba()); unknown formats pass
    through unchanged with opacity untouched.

    Returns ``( colour_without_alpha_or_original_if_unparseable ,
                effective_opacity_in_[0,1] )``.
    """
    if not color:
        return color, opacity
    parsed = parse_hex_color(color)
    if parsed is None:
        return color, opacity
    r, g, b, alpha = parsed
    return f"#{r:02X}{g:02X}{b:02X}", max(0.0, min(1.0, opacity * alpha))


def _pos(rect_pct: Optional[dict]) -> Optional[PositionPercent]:
    if not rect_pct or not isinstance(rect_pct, dict):
        return None
    return PositionPercent(
        x_pct=float(rect_pct.get("x", 0)),
        y_pct=float(rect_pct.get("y", 0)),
        w_pct=float(rect_pct.get("w", 0)),
        h_pct=float(rect_pct.get("h", 0)),
    )


def _gradient(g: Optional[dict]) -> Optional[GradientDef]:
    if not g or not isinstance(g, dict):
        return None
    stops = [
        GradientStop(
            color=s.get("color", "#000000"),
            position=s.get("position", 0),
            opacity=s.get("opacity", 1.0),
        )
        for s in g.get("stops", [])
    ]
    return GradientDef(direction=g.get("direction", "horizontal"), stops=stops)


def _bg(styles: dict) -> Optional[BackgroundDef]:
    """Build BackgroundDef from element styles."""
    grad = _gradient(styles.get("backgroundGradient"))
    color = styles.get("backgroundColor")

    if grad:
        return BackgroundDef(type="gradient", gradient=grad)
    if color and color not in ("transparent", "rgba(0, 0, 0, 0)"):
        return BackgroundDef(type="solid", color=color)
    return None


def _bg_from_data(data: Optional[dict]) -> Optional[BackgroundDef]:
    """Build BackgroundDef from background data dict (analyze_html output)."""
    if not data or not isinstance(data, dict):
        return None
    bg_type = data.get("type", "solid")
    color = data.get("color")
    grad = _gradient(data.get("gradient"))
    image_url = data.get("image_url")

    if bg_type == "gradient" and grad:
        return BackgroundDef(type="gradient", gradient=grad)
    if bg_type == "image" and image_url:
        return BackgroundDef(type="image", image_url=image_url)
    if color and color not in ("transparent", "rgba(0, 0, 0, 0)", None):
        return BackgroundDef(type="solid", color=color)
    return None


def _border(styles: dict) -> Optional[BorderDef]:
    width = styles.get("borderWidth", 0)
    if not width or float(width) <= 0:
        return None
    return BorderDef(
        color=styles.get("borderColor", "#000000") or "#000000",
        width_pt=_clamp(width, 0, 50, 0),
        style=styles.get("borderStyle", "solid") or "solid",
    )


def _shadow(styles: dict) -> Optional[ShadowDef]:
    bs = styles.get("boxShadow")
    if not bs:
        return None
    # Parse "Xpx Ypx Bpx Spx #color" pattern
    m = re.match(
        r"(-?[\d.]+)px\s+(-?[\d.]+)px\s+([\d.]+)px"
        r"(?:\s+([\d.]+)px)?\s+(.+)",
        bs,
    )
    if not m:
        return None
    try:
        return ShadowDef(
            color=m.group(5).strip(),
            blur_pt=float(m.group(3)),
            offset_x_pt=float(m.group(1)),
            offset_y_pt=float(m.group(2)),
        )
    except (ValueError, IndexError):
        return None


# CSS `filter: drop-shadow(Xpx Ypx BlurPx color)` parser — used for the
# ICON+GLOW pattern (Material Icons with a halo). Mirrors `_shadow` but
# targets the `filter` property instead of `box-shadow`. Only the first
# drop-shadow layer is honoured; secondary comma-separated layers are
# dropped (matches the FORBIDDEN CSS guidance for box-shadow).
# Colour group accepts hex / #RRGGBBAA / rgb(...) / rgba(...) / named, so
# the closing `)` of an rgba() inside the colour doesn't truncate the match.
_DROP_SHADOW_RE = re.compile(
    r"drop-shadow\(\s*"
    r"(-?[\d.]+)(?:px)?\s+"
    r"(-?[\d.]+)(?:px)?\s+"
    r"([\d.]+)(?:px)?\s+"
    r"(#[0-9a-fA-F]{3,8}|rgba?\([^)]+\)|[a-zA-Z]+)"
    r"\s*\)"
)


def _drop_shadow_from_styles(styles: dict) -> Optional[ShadowDef]:
    """Build a ShadowDef from the first `filter: drop-shadow(...)` layer.

    Returns None when the element has no filter, no drop-shadow inside the
    filter, or the drop-shadow arguments fail to parse. Colour may be any
    form parse_hex_color understands (#RGB / #RRGGBB / #RRGGBBAA / rgb /
    rgba).
    """
    f = styles.get("filter")
    if not f:
        return None
    m = _DROP_SHADOW_RE.search(f)
    if not m:
        return None
    try:
        return ShadowDef(
            color=m.group(4).strip(),
            blur_pt=float(m.group(3)),
            offset_x_pt=float(m.group(1)),
            offset_y_pt=float(m.group(2)),
        )
    except (ValueError, IndexError):
        return None


def _is_bold(styles: dict) -> bool:
    fw = styles.get("fontWeight", "400")
    try:
        return float(fw) >= 600
    except (ValueError, TypeError):
        return fw == "bold"


def _shape_type_from_styles(styles: dict, rect: dict) -> str:
    """Infer shape type from border radius and aspect ratio."""
    radius = _parse_border_radius_px(styles)
    w = rect.get("w", 0)
    h = rect.get("h", 0)

    if radius <= 0:
        # No radius — rectangle or oval
        if abs(w - h) < 3:
            return "oval"
        return "rectangle"

    # CSS `border-radius: 9999px` means "as round as possible".
    # Square-ish element → perfect circle (OVAL renders identically at any
    # aspect ratio, so use it directly). Long pill → ROUNDED_RECTANGLE with
    # adj=50000 forms a capsule.
    if radius >= 9999:
        if abs(w - h) < 3:
            return "circle"
        return "rounded_rect"  # capsule via _corner_radius_pct = 0.5

    # Has radius
    if abs(w - h) < 3 and radius > min(w, h) * 3:
        return "circle"
    return "rounded_rect"


# Sentinel value returned by CSS for "border-radius: 9999px" / "100%" etc.
# Authors write 9999px to mean "fully rounded" (pill / circle).
_FULLY_ROUNDED_PX = 9999.0


def _parse_border_radius_px(styles: dict) -> float:
    br = styles.get("borderRadius", "0")
    if not br:
        return 0.0
    try:
        v = float(br.replace("px", ""))
    except (ValueError, AttributeError):
        return 0.0
    return v


def _is_fully_rounded(styles: dict) -> bool:
    """True when the author wrote border-radius: 9999px (or similar sentinel)."""
    return _parse_border_radius_px(styles) >= _FULLY_ROUNDED_PX


def _corner_radius_pct(styles: dict, rect: dict) -> float:
    """Estimate corner radius as a fraction of element size."""
    r = _parse_border_radius_px(styles)
    if r <= 0:
        return 0.0
    # Fully-rounded sentinel: skip the 9999/min_dim calculation — it would
    # always clamp to 0.5 anyway, but produces large intermediate values
    # that can lose precision in fixed-point conversion downstream.
    if r >= _FULLY_ROUNDED_PX:
        return 0.5
    # slide is 1280x720, rect is in pct
    w_px = rect.get("w", 0) / 100 * 1280
    h_px = rect.get("h", 0) / 100 * 720
    min_dim = max(min(w_px, h_px), 1)
    return _clamp(r / min_dim, 0, 0.5, 0)


def _is_atomic_overflow(natural_width_pct: float, box_w: float) -> bool:
    """True when single-line text's natural glyph width exceeds its container.

    Browser shows these as visual overflow (CSS default `overflow: visible`
    on inline content); atomic tokens have no whitespace wrap opportunity,
    so the browser couldn't wrap them anyway. Decorative numbers like the
    ghosted "02" watermark (380pt in a 30% container), URLs, and long
    product codes hit this case.

    Used to mark TextBoxElement.no_wrap so the renderer sets
    ``tf.word_wrap = False`` — text overflows the textbox visually instead
    of wrapping. This matches browser behavior precisely without growing
    the textbox past its container (which would risk sibling overlap).
    """
    return natural_width_pct > box_w


def _widen_text_position(
    rect_pct: dict,
    range_line_count: int = 0,
    range_max_line_width_pct: float = 0.0,
    natural_width_pct: float = 0.0,
    padding_pct: float = 1.5,
    max_w_pct: float = 100.0,
    alignment: str = "left",
    extra_width_pct: float = 0.0,
    text: str = "",
    cap_tolerance_pct: float = 4.0,
) -> Optional[PositionPercent]:
    """Build a PositionPercent sized to the text's ACTUAL rendered extent.

    Uses ``Range.getClientRects`` measurements from Stage 1.
    ``range_line_count`` is the authoritative signal:

    - ``range_line_count > 1``: text wraps in the browser. The container width
      IS the constraint that caused the wrap. PPT will wrap too
      (``word_wrap=True`` in renderer), so we preserve ``box_w``. Widening
      would force the text back onto one line and overflow the parent card.
    - ``range_line_count == 1``: single line. Use the actual rendered width
      (``range_max_line_width_pct``) + a small fixed slack. This fixes
      "text box too wide" cases where a single-line text lives in a much
      wider container (e.g. H1 'NVIDIA GPU Support' in a 1216px block with
      only 371px of text → old pipeline kept box=1216, new keeps ~371).

    Atomic-token overflow (single line, wider than container — URLs, product
    codes, watermark digits like the ghosted "01") is handled naturally:
    ``range_max_line_width_pct > box_w``, so the formula uses it as the
    base. No whitespace-based ``has_wrap_opportunity`` special case needed.

    Falls back to legacy ``natural_width_pct`` (canvas.measureText) when
    Range data is unavailable (``range_line_count == 0`` — pure icons,
    empty elements, extraction glitches). The fallback preserves old
    behaviour so unrelated samples don't regress.

    Width growth is anchored by ``alignment`` so the text stays where the
    browser put it:
    - ``left``  (default): left edge stays; grow to the right.
    - ``right``           : right edge stays; grow to the left.
    - ``center``          : center stays; grow equally on both sides.

    Without this, a centred H1 whose original rect was x=28.24, w=43.52
    (perfectly centred on the slide at 50%) grew to w=61.6 at the same x,
    shifting its centre to 59% and making the title look off-centre even
    though the paragraph's alignment was set to "center".

    ``extra_width_pct`` is additional slack for elements whose renderer
    consumes frame width outside the text content area — e.g. ``bullet_list``
    reserves ``marL`` for the DrawingML ``<a:buChar>`` marker. Single source
    of truth for all text-bearing DSL elements; new marker-based elements
    pass their own ``extra_width_pct`` instead of duplicating the math.

    The final ``needed_w`` is soft-capped at ``box_w + cap_tolerance_pct``
    (the HTML container width plus a small overflow allowance). The browser
    already constrained the element inside its container, so siblings start
    at/after ``container.x + container.w`` by construction. ``cap_tolerance_pct``
    is a caller-supplied parameter because safety depends on whether the
    textbox equals the element's visual extent:

    - **Wrapping elements** (``word_wrap=True`` in renderer — bullet lists,
      normal text boxes): textbox is the wrap boundary, text doesn't reach
      the right edge. Default ``4.0`` covers PPT's slightly-wider font
      rendering (GDI/DirectWrite measures 2-5% wider than browser) and
      usually lands inside the CSS gap.
    - **Non-wrapping elements** (``word_wrap=False`` in renderer — badges,
      ``no_wrap=True`` text boxes for atomic-token overflow): textbox IS
      the visual extent, any overflow is real visual overflow. Caller must
      pass ``0.0`` so the textbox stays at ``box_w`` exactly.

    No post-process neighbour scan is needed — and none is done.
    """
    # Single-line slack: covers PPTX font-metric variance (GDI/DirectWrite
    # text shaping measures 2-5% wider than the browser for the same
    # sans-serif font) + textbox internal margins. Fixed value (not scaled
    # with text width) because Range already measured the actual rendered
    # font, so we don't need the old 1.3x FONT_SAFETY factor that was
    # compensating for canvas.measureText theoretical-width error.
    #
    # This slack only matters for wrapping elements (default
    # cap_tolerance_pct=4.0). Non-wrapping callers pass cap_tolerance_pct=0.0
    # — for them textbox == visual extent and slack is intentionally clipped
    # (renderer's word_wrap=False, not slack, prevents wrapping).
    SINGLE_LINE_SLACK_PCT = 4.0

    box_w = rect_pct.get("w", 0)

    if range_line_count > 1:
        # Multi-line in browser → preserve container width so PPTX wraps too.
        needed_w = box_w + extra_width_pct
    elif range_line_count == 1 and range_max_line_width_pct > 0:
        # Single line. Range returns the block's line-box width (= container
        # width for block-level leaf elements), NOT the glyph extent — so for
        # giant decorative text where the glyph fills or overflows the
        # container (e.g. "05" at 315pt with glyph ≈ 40% in a 32.81% box),
        # range_max alone underestimates what PPTX needs to render the run
        # without wrapping/overflowing. canvas.measureText's natural_width
        # captures the true single-line glyph extent because it isn't bound
        # by the container. Take the max so small text stays compact (range
        # and natural agree) and giant text gets enough room.
        base_w = max(range_max_line_width_pct, natural_width_pct)
        needed_w = (
            base_w
            + SINGLE_LINE_SLACK_PCT
            + padding_pct
            + extra_width_pct
        )
    else:
        # Range unavailable (no measurable text, or extraction glitch):
        # fall back to legacy natural_width logic to preserve old behaviour.
        FONT_SAFETY = 1.3
        INTERNAL_MARGIN_PCT = 1.9
        needed_w = max(
            box_w,
            natural_width_pct * FONT_SAFETY
            + INTERNAL_MARGIN_PCT + padding_pct + extra_width_pct,
        )

    # Container soft-cap: textbox can exceed the HTML container by up to
    # ``cap_tolerance_pct``. Caller decides based on whether the textbox
    # equals the element's visual extent:
    #
    #   - Wrapping elements (cap_tolerance_pct=4.0, default): textbox is
    #     the wrap boundary, text doesn't fill the right edge. PPT's
    #     GDI/DirectWrite text shaping measures 2-5% wider than the
    #     browser for the same sans-serif font, so text that fit on one
    #     line in the browser at box_w often wraps in PPT at the same
    #     width. 4% overflow covers the typical font-metric delta while
    #     usually landing inside the CSS gap + card padding between
    #     siblings.
    #   - Non-wrapping elements (cap_tolerance_pct=0.0): badges and
    #     ``no_wrap=True`` text boxes — textbox IS the visual extent
    #     (pill background fills it; word_wrap=False means text reaches
    #     the right edge). Any tolerance is direct visual overflow into
    #     siblings. CSS gaps like Tailwind's ``gap-3`` (0.625%) are far
    #     smaller than the old uniform 4%, so even a 2-badge flex row
    #     would overlap.
    needed_w = min(needed_w, box_w + cap_tolerance_pct)

    # Hard cap: cannot exceed slide width.
    needed_w = min(needed_w, max_w_pct)
    # Hard cap: cannot extend past the right edge of the slide. Without this,
    # a <p> inside a card near the slide's right edge can get its width
    # extrapolated past 100% (the browser measurement picks up the parent
    # card's wider rect when the text is single-line) and the text then
    # refuses to wrap in PPTX.
    x_pct = float(rect_pct.get("x", 0))

    # Anchor width growth by alignment so the text stays visually put.
    extra_w = max(0.0, needed_w - box_w)
    if alignment == "right":
        new_x = x_pct - extra_w
    elif alignment == "center":
        new_x = x_pct - extra_w / 2.0
    else:  # "left" or unknown
        new_x = x_pct

    # Clamp to slide bounds. If we shifted left and ran off the slide, nudge
    # back right; if the right edge still overflows, also shrink width.
    if new_x < 0:
        new_x = 0.0
    if new_x + needed_w > 100.0:
        if alignment == "right":
            # Prefer keeping the right anchor; trim width on the left side.
            needed_w = max(0.0, (x_pct + box_w) - new_x)
        else:
            needed_w = max(0.0, 100.0 - new_x)

    return PositionPercent(
        x_pct=round(new_x, 2),
        y_pct=float(rect_pct.get("y", 0)),
        w_pct=round(needed_w, 2),
        h_pct=float(rect_pct.get("h", 0)),
    )


# ---------------------------------------------------------------------------
# Per-type converters
# ---------------------------------------------------------------------------

def _runs_from_element(elem: dict) -> List[TextRun]:
    """Build TextRuns from an element's inlineRuns, or a single run from its
    own text + computed styles. Shared by text_box and table-cell conversion.
    """
    styles = elem.get("styles", {})
    # Use cumulative opacity (own × ancestor chain) so text inside a
    # <div style="opacity:…"> wrapper inherits the wrapper's opacity —
    # matching the browser's effective rendering.
    opacity = _cumulative_opacity(elem)
    inline_runs = elem.get("inlineRuns")
    if inline_runs:
        runs: List[TextRun] = []
        for line in inline_runs:
            for r in line:
                r_text = r.get("text") or ""
                if r_text == "":
                    continue
                r_color, r_opacity = _normalize_color_opacity(r.get("color"), opacity)
                runs.append(TextRun(
                    text=r_text,
                    font_size_pt=_clamp(r.get("font_size_pt"), 6, 500),
                    color=r_color,
                    bold=bool(r.get("bold")),
                    italic=bool(r.get("italic")),
                    font_name=r.get("font_name"),
                    opacity=r_opacity,
                ))
        if runs:
            return runs
    # Fallback: single run from the element's own text + computed style.
    # directText excludes icon-span text by construction (text nodes only);
    # text_no_icons is JS-side textContent with material-icons spans
    # stripped (extract_layout.js). Both are safe — no heuristic filtering.
    text = (elem.get("directText", "").strip()
            or elem.get("text_no_icons", "").strip()
            or elem.get("text", "").strip())
    fallback_color, fallback_opacity = _normalize_color_opacity(
        styles.get("color"), opacity
    )
    return [TextRun(
        text=text,
        font_size_pt=_clamp(styles.get("fontSize_pt"), 6, 500),
        color=fallback_color,
        bold=_is_bold(styles),
        italic=styles.get("fontStyle") == "italic",
        font_name=styles.get("fontFamily"),
        opacity=fallback_opacity,
    )]


def _to_icon_text(ce: ClassifiedElement,
                  elements: Optional[list] = None,
                  tree: Optional["ContainmentTree"] = None) -> IconTextElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})
    direct = elem.get("directText", "").strip()
    text = direct or elem.get("text", "").strip()

    # Default text styling comes from the parent element. The flex-div-with-
    # span pattern overrides this below (label styling belongs to the span,
    # not the flex container).
    label_styles = styles

    # Extract icon name from classes (material-icons class -> text is the name)
    icon_name = ""
    icon_font: Optional[str] = None
    icon_color: Optional[str] = None
    icon_size_pt = 28.0
    icon_shadow: Optional[ShadowDef] = None

    classes = elem.get("classes", [])
    is_material_icon = ("material-icons" in classes
                        or "material-symbols-outlined" in classes)

    if is_material_icon:
        # Case (a): the element itself is a Material Icons element. Its text
        # IS the icon name, not display text — so the label is empty.
        icon_name = direct or elem.get("text", "").strip()
        text = ""
        icon_font = elem.get("icon_font")
        icon_color = styles.get("color")
        icon_size_pt = _clamp(styles.get("fontSize_pt"), 6, 120, 28.0)
        # ICON+GLOW: filter: drop-shadow(...) on the icon itself.
        icon_shadow = _drop_shadow_from_styles(styles)
    elif elements is not None and tree is not None:
        # Case (b): heading-with-icon OR flex-div-with-icon pattern.
        # Read icon geometry/styling from the child icon element.
        icon_info = _find_icon_child(ce.index, elements, tree)
        if icon_info is not None:
            icon_name = icon_info.get("icon_name", "")
            icon_font = icon_info.get("icon_font")
            icon_color = icon_info.get("icon_color")
            icon_size_pt = icon_info.get("icon_size_pt", 28.0)
            icon_shadow = icon_info.get("icon_shadow")
        else:
            icon_font = elem.get("icon_font")
            icon_color = styles.get("color")
            icon_size_pt = _clamp(styles.get("fontSize_pt"), 6, 120, 28.0)

        # Two sub-patterns:
        #   - Heading: <h3><i>icon</i>Heading</h3> — directText holds the
        #     label, parent styles hold the label styling.
        #   - Flex div: <div class="flex"><i>icon</i><span>label</span></div>
        #     — directText is empty and `text` leaks the icon name into the
        #     label. Read the label TEXT and its STYLING from the longest-
        #     text non-icon child (the span) so the rendered label matches
        #     what the user sees, not the flex container's defaults.
        if not direct:
            label_child = _find_label_child(ce.index, elements, tree)
            if label_child is not None:
                label_styles = label_child.get("styles", {})
                child_text = (label_child.get("directText", "").strip()
                              or label_child.get("text_no_icons", "").strip()
                              or label_child.get("text", "").strip())
                text = child_text
            else:
                # No label child — fall back to elem's own clean text fields.
                # directText is already clean (text nodes only); text_no_icons
                # is JS-side textContent with material-icons spans stripped.
                text = direct or elem.get("text_no_icons", "").strip() or text
        else:
            # Heading case: directText is already clean. No filter needed.
            pass
    else:
        icon_font = elem.get("icon_font")
        icon_color = styles.get("color")
        icon_size_pt = _clamp(styles.get("fontSize_pt"), 6, 120, 28.0)

    return IconTextElement(
        type="icon_text",
        icon_name=icon_name,
        icon_size_pt=icon_size_pt,
        icon_color=icon_color,
        icon_shadow=icon_shadow,
        text=text,
        text_font_size_pt=_clamp(label_styles.get("fontSize_pt"), 6, 120, 22.0),
        text_color=label_styles.get("color"),
        text_bold=_is_bold(label_styles),
        text_font_name=label_styles.get("fontFamily"),
        icon_font=icon_font,
        layout="horizontal",
        position=_pos(rect),
        z_order=ce.index,
    )


def _to_image(ce: ClassifiedElement) -> ImageElement:
    elem = ce.data
    rect = elem.get("rect_pct", {})
    styles = elem.get("styles", {})

    return ImageElement(
        type="image",
        url=elem.get("attrs", {}).get("src", ""),
        alt_text=elem.get("attrs", {}).get("alt", ""),
        border=_border(styles),
        corner_radius_pct=_corner_radius_pct(styles, rect),
        object_fit=styles.get("objectFit", "fill") or "fill",
        position=_pos(rect),
        z_order=ce.index,
    )


def _to_divider_line(ce: ClassifiedElement) -> DividerLineElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})

    return DividerLineElement(
        type="divider_line",
        color=styles.get("backgroundColor", "#00CD82"),
        height_pt=_clamp(rect.get("h", 0) / 100 * 720 * 0.75, 0.5, 20, 2.0),
        position=_pos(rect),
        z_order=ce.index,
    )


def _extract_badge_text(elem: dict) -> str:
    """Extract display text from a badge element, skipping icon names.

    Badge containers often look like
    ``<div class="badge"><span class="material-icons">auto_awesome</span>Label</div>``
    where 'auto_awesome' is a Material Icon ligature name, not display text.

    Field choices:
      - ``directText`` already excludes icon span text by construction (the
        JS extractor only counts TEXT_NODE children, and an ``<span>`` is
        an ELEMENT_NODE — see extract_layout.js:423-429). So directText
        gives us the badge's own text-node content directly.
      - ``text_no_icons`` (JS-side ``textContent`` with material-icons spans
        stripped) is the safe fallback when directText is empty (e.g. the
        label is wrapped in a child element).
      - ``text`` is a last-resort fallback for old cached JSON that
        predates ``text_no_icons``.

    No text-shape heuristic filtering — that approach dropped legitimate
    short lowercase tokens like 'cu129' / 'v1' (see converter.py history).
    """
    direct = elem.get("directText", "").strip()
    if direct:
        return direct
    return (elem.get("text_no_icons", "").strip()
            or elem.get("text", "").strip())


def _find_icon_child(parent_idx: int, elements: list, tree: ContainmentTree) -> Optional[dict]:
    """Find an icon child element among the absorbed children of a badge/card."""
    for child_idx in tree.get_children(parent_idx):
        child = elements[child_idx]
        if child.get("is_icon"):
            child_styles = child.get("styles", {})
            return {
                "icon_name": child.get("directText", "").strip(),
                "icon_font": child.get("icon_font"),
                "icon_color": child_styles.get("color"),
                "icon_size_pt": _clamp(child_styles.get("fontSize_pt"), 6, 120, 18.0),
                "rect_pct": child.get("rect_pct", {}),
                "icon_shadow": _drop_shadow_from_styles(child_styles),
            }
    return None


def _find_label_child(parent_idx: int, elements: list, tree: ContainmentTree) -> Optional[dict]:
    """Find the label-bearing child of an icon_text element.

    Used when the parent (flex div, span container) has no directText of its
    own — the label text lives on an inner ``<span>`` (or similar) element.
    Pattern: ``<div class="flex"><i>icon</i><span>label</span></div>``.

    Returns the child element dict with the longest non-icon text (so caller
    can read both text and styles), or None if no such child exists.
    """
    best: Optional[dict] = None
    best_len = -1
    for child_idx in tree.get_children(parent_idx):
        child = elements[child_idx]
        if child.get("is_icon"):
            continue
        # directText excludes icon-span text by construction (text nodes
        # only); text_no_icons is JS-side textContent with material-icons
        # spans stripped. Skip children with no usable text.
        text = (child.get("directText", "").strip()
                or child.get("text_no_icons", "").strip()
                or child.get("text", "").strip())
        if not text:
            continue
        if len(text) > best_len:
            best_len = len(text)
            best = child
    return best


def _find_text_child_color(parent_idx: int, elements: list, tree: ContainmentTree) -> Optional[str]:
    """Get the text color from the longest-text non-icon child element.

    Badges often have multiple non-icon children (a coloured prefix + body
    text). Picking the first one we see was arbitrary and sometimes returned
    the prefix colour instead of the label colour. The longest-text child
    is most likely the primary label.
    """
    best_color: Optional[str] = None
    best_len = -1
    for child_idx in tree.get_children(parent_idx):
        child = elements[child_idx]
        if child.get("is_icon"):
            continue
        text = child.get("directText", "").strip() or child.get("text", "").strip()
        if len(text) <= best_len:
            continue
        color = child.get("styles", {}).get("color")
        if color:
            best_len = len(text)
            best_color = color
    return best_color


def _find_title_text_child(parent_idx: int, elements: list, tree: ContainmentTree) -> Optional[dict]:
    """Find the text-bearing child of a title_bar (e.g. <h1>) to read its real font styles.

    The title_bar div itself usually inherits body font-size, but the actual title
    styling lives on the inner h1/h2/h3 element.
    """
    for child_idx in tree.get_children(parent_idx):
        child = elements[child_idx]
        if child.get("absorbedByParent"):
            continue
        if not _has_text(child):
            continue
        return child
    return None


def _to_badge(ce: ClassifiedElement, elements: list, tree: ContainmentTree) -> BadgeElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})
    text = _extract_badge_text(elem)

    # Find absorbed icon child
    icon_info = _find_icon_child(ce.index, elements, tree)

    # Get correct text color from child <span> (not badge div's inherited color)
    text_color = _find_text_child_color(ce.index, elements, tree) or styles.get("color", "#FFFFFF")

    # Calculate natural width including icon (legacy signal for fallback path)
    natural_w = float(elem.get("textNaturalWidth_pct", 0) or 0)
    # Range-based actual rendered width (authoritative). Apply same icon +
    # gap adjustment so single-line badges with icons get the right size.
    badge_range_max = float(elem.get("range_max_line_width_pct", 0) or 0)
    range_line_count = int(elem.get("range_line_count", 0) or 0)
    if icon_info:
        icon_w = icon_info.get("rect_pct", {}).get("w", 1.5)
        gap_w = 0.6  # gap-2 ≈ 8px ≈ 0.6%
        natural_w = icon_w + gap_w + max(natural_w, 5.0)
        badge_range_max = icon_w + gap_w + max(badge_range_max, 5.0)

    return BadgeElement(
        type="badge",
        text=text,
        background=_bg(styles),
        border=_border(styles),
        font_size_pt=_clamp(styles.get("fontSize_pt"), 6, 120, 14.0),
        font_color=text_color,
        corner_radius_pct=min(_corner_radius_pct(styles, rect), 0.5),
        shadow=_shadow(styles),
        opacity=_cumulative_opacity(elem),
        icon_name=icon_info.get("icon_name") if icon_info else None,
        icon_font=icon_info.get("icon_font") if icon_info else None,
        icon_color=icon_info.get("icon_color") if icon_info else None,
        icon_size_pt=icon_info.get("icon_size_pt") if icon_info else None,
        position=_widen_text_position(
            rect,
            range_line_count=range_line_count,
            range_max_line_width_pct=badge_range_max,
            natural_width_pct=natural_w,
            padding_pct=3.0,
            text=text,
            # BadgeElement renders with tf.word_wrap=False (pill design),
            # so textbox == visual extent. Any tolerance is direct visual
            # overflow into siblings — pass 0.0 so textbox stays at box_w.
            cap_tolerance_pct=0.0,
        ),
        z_order=ce.index,
    )


def _per_side_border(styles: dict, side: str) -> Optional[BorderDef]:
    """Build a BorderDef from a single side's width/color styles.

    `side` is one of 'Left', 'Right', 'Top', 'Bottom' (capitalised to match
    the JS extractor's `borderXxxWidth` / `borderXxxColor` keys).

    Returns None if either width or color is missing — we no longer fabricate
    a colour from thin air. The previous `default_color="#FF5722"` made every
    side that lacked an explicit colour render as a red stripe, which is not
    what users expect for `border: 3px solid blue` (the browser expands the
    shorthand to all four sides but our JS extractor only carried one colour
    through, so three sides fell back to red).
    """
    width = styles.get(f"border{side}Width", 0)
    if not width or float(width) <= 0:
        return None
    color = styles.get(f"border{side}Color")
    if not color:
        return None
    return BorderDef(
        color=color,
        width_pt=_clamp(width, 0, 50, 0),
        style="solid",
    )


def _to_card(ce: ClassifiedElement, elements: list, tree: ContainmentTree) -> CardElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})

    border = _border(styles)
    if border is not None:
        # A uniform border is set — the renderer draws it via shape.line.
        # Don't also extract per-side stripes: the browser expands
        # `border: 3px solid X` to all four sides, so the extractor reports
        # equal widths on each side. Rendering those as additional stripes
        # would either duplicate the line or, worse, paint the wrong colour
        # when per-side color isn't carried through (see _per_side_border).
        border_left = border_right = border_top = border_bottom = None
    else:
        border_left = _per_side_border(styles, "Left")
        border_right = _per_side_border(styles, "Right")
        border_top = _per_side_border(styles, "Top")
        border_bottom = _per_side_border(styles, "Bottom")

    return CardElement(
        type="card",
        background=_bg(styles),
        border=border,
        border_left=border_left,
        border_right=border_right,
        border_top=border_top,
        border_bottom=border_bottom,
        corner_radius_pct=_corner_radius_pct(styles, rect),
        shadow=_shadow(styles),
        opacity=_cumulative_opacity(elem),
        position=_pos(rect),
        z_order=ce.index,
        children=[],  # children are handled separately
    )


def _to_numbered_step(ce: ClassifiedElement, elements: list, tree: ContainmentTree) -> NumberedStepElement:
    # ce.data is the visible number circle — either a `<div>1</div>` whose
    # directText IS the digit, or a `<div class="rounded-full"><span>1</span>
    # </div>` whose directText is empty (the digit lives in the inner span).
    # After the nested-candidate filter in detect_numbered_sequences, only
    # the outer container survives — so we may need to walk children to find
    # the digit text and its colour.
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})
    direct_text = elem.get("directText", "").strip()

    import re as _re
    num_re = re.compile(r"^([1-9]\d*)[\.\)]?\s*$")
    m = num_re.match(direct_text)
    step_number = int(m.group(1)) if m else 1
    # Text colour: prefer the inner span's colour (where the digit actually
    # renders). The outer circle's `color` is the inherited body colour,
    # which is usually black/transparent — not what's painted on the badge.
    text_color = styles.get("color")
    if not m:
        # directText had no digit — walk descendants for the digit-bearing
        # element (typically a single <span>) and read both number and colour.
        for child_idx in tree.get_children(ce.index):
            child = elements[child_idx]
            child_text = child.get("directText", "").strip()
            cm = num_re.match(child_text)
            if cm:
                step_number = int(cm.group(1))
                child_color = child.get("styles", {}).get("color")
                if child_color:
                    text_color = child_color
                break

    return NumberedStepElement(
        type="numbered_step",
        step_number=step_number,
        number_bg_color=styles.get("backgroundColor") or "#133EFF",
        number_text_color=text_color or "#FFFFFF",
        title="",                # rendered by sibling text_box
        title_color=None,
        description="",          # rendered by sibling text_box
        description_color=None,
        position=_pos(rect),
        z_order=ce.index,
    )


def _to_bullet_list(ce: ClassifiedElement, elements: list, tree: ContainmentTree) -> BulletListElement:
    """Build a BulletListElement from a detected UL container.

    After the classify_elements restructure (mirror of the table pattern),
    ``ce.data`` is the UL element. We re-run ``detect_bullet_groups`` to
    recover this group's LI items, then walk them for text + natural
    widths. Position uses the UL bbox (which includes the ``padding-left``
    area where the browser renders ``::marker``) widened so the longest
    item fits in PPTX — accounting for both the PPTX-vs-browser font
    metric difference (FONT_SAFETY) and the renderer's DrawingML buChar
    hanging indent (BULLET_MARL_PCT).

    Falls back to reading ``ce.data`` directly when no group is found
    (e.g. a stray LI without a detectable UL parent).
    """
    from shuttleslide.html_to_pptx.rule.containment import detect_bullet_groups

    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})

    groups = detect_bullet_groups(elements, tree)
    group = next((g for g in groups if g.parent_idx == ce.index), None)

    items: List[BulletItem] = []
    max_natural_w = 0.0
    max_range_w = 0.0  # authoritative: actual rendered max line width across items
    item_styles = styles  # default to UL's own styles
    if group is not None:
        for item_idx in group.item_elements:
            item_elem = elements[item_idx]
            # LI's textContent includes child <i class="material-icons">name</i>
            # text — use directText (text nodes only) or text_no_icons (JS
            # already stripped material-icons spans) so items don't read
            # like "check_circleLabel". Same class of bug as icon_text/badge.
            raw_text = (item_elem.get("directText", "").strip()
                        or item_elem.get("text_no_icons", "").strip()
                        or item_elem.get("text", "").strip())
            item_text = raw_text
            # Probe for an icon child of this LI (same helper used by
            # _to_badge / _to_icon_text — closing the bullet_list gap so
            # <li><i class="material-icons">check_circle</i>text</li> renders
            # the icon as a vector shape instead of dropping it silently).
            icon_info = _find_icon_child(item_idx, elements, tree)
            items.append(BulletItem(
                text=item_text,
                icon_name=icon_info["icon_name"] if icon_info else None,
                icon_font=icon_info.get("icon_font") if icon_info else None,
                icon_color=icon_info.get("icon_color") if icon_info else None,
                icon_size_pt=icon_info.get("icon_size_pt") if icon_info else None,
            ))
            try:
                nw = float(item_elem.get("textNaturalWidth_pct", 0) or 0)
            except (TypeError, ValueError):
                nw = 0.0
            if nw > max_natural_w:
                max_natural_w = nw
            try:
                rw = float(item_elem.get("range_max_line_width_pct", 0) or 0)
            except (TypeError, ValueError):
                rw = 0.0
            if rw > max_range_w:
                max_range_w = rw
        # LI carries the actual font-size/color; UL inherits body defaults.
        if group.item_elements:
            item_styles = elements[group.item_elements[0]].get("styles", {})
    else:
        # Fallback: ce.data is a single LI without group (legacy path).
        text = (elem.get("directText", "").strip()
                or elem.get("text_no_icons", "").strip()
                or elem.get("text", "").strip())
        items = [BulletItem(text=line.strip())
                 for line in text.split("\n") if line.strip()]
        try:
            max_natural_w = float(elem.get("textNaturalWidth_pct", 0) or 0)
        except (TypeError, ValueError):
            max_natural_w = 0.0
        try:
            max_range_w = float(elem.get("range_max_line_width_pct", 0) or 0)
        except (TypeError, ValueError):
            max_range_w = 0.0

    # Frame widening shares the same math as text_box / badge via
    # ``_widen_text_position``. bullet_list additionally reserves
    # ``BULLET_MARL_PCT`` for the renderer's DrawingML buChar hanging
    # indent (the marker renders inside marL, stealing from text area).
    # Without this, text that fit on one line in the browser wraps in
    # PPTX (e.g. 3.html's "Metal support enabled" — LI bbox 14.2%,
    # text natural_w 14.11, but PPT needs ~15.5% and marL steals
    # another 1.88%).
    BULLET_MARL_PCT = 1.88  # renderer's hanging indent for buChar

    return BulletListElement(
        type="bullet_list",
        items=items,
        bullet_color=item_styles.get("color"),
        font_size_pt=_clamp(item_styles.get("fontSize_pt"), 6, 120, 22.0),
        font_color=item_styles.get("color"),
        spacing_pt=8.0,
        position=_widen_text_position(
            rect,
            # Multiple bullets are inherently multi-line in PPT → force
            # range_line_count >= 2 to take the "preserve container width"
            # branch. Single-item lists with single-line text use the
            # item's actual range measurement.
            range_line_count=max(2, len(items)) if len(items) > 1 else 1,
            range_max_line_width_pct=max_range_w,
            natural_width_pct=max_natural_w,
            extra_width_pct=BULLET_MARL_PCT,
            alignment="left",
            text="\n".join(it.text for it in items),
        ),
        z_order=ce.index,
    )


def _to_title_bar(ce: ClassifiedElement, elements: list, tree: ContainmentTree) -> TitleBarElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})

    # The title_bar div's own computed styles usually inherit from body
    # (small font-size, default color). The real title styling — size, color,
    # weight — lives on the inner <h1> (or similar) child, which has been
    # absorbed by the classifier. Read from that child when available.
    text_child = _find_title_text_child(ce.index, elements, tree)
    if text_child is not None:
        child_styles = text_child.get("styles", {})
        text = (text_child.get("directText", "").strip()
                or text_child.get("text_no_icons", "").strip()
                or text_child.get("text", "").strip())
        font_size_pt = _clamp(child_styles.get("fontSize_pt"), 6, 120, 30.0)
        font_color = child_styles.get("color") or styles.get("color", "#FFFFFF")
        font_bold = _is_bold(child_styles) or _is_bold(styles)
        font_name = child_styles.get("fontFamily") or styles.get("fontFamily")
    else:
        text = (elem.get("directText", "").strip()
                or elem.get("text_no_icons", "").strip()
                or elem.get("text", "").strip())
        font_size_pt = _clamp(styles.get("fontSize_pt"), 6, 120, 30.0)
        font_color = styles.get("color", "#FFFFFF")
        font_bold = _is_bold(styles)
        font_name = styles.get("fontFamily")

    return TitleBarElement(
        type="title_bar",
        text=text,
        font_size_pt=font_size_pt,
        font_color=font_color,
        font_bold=font_bold,
        font_name=font_name,
        height_pct=_clamp(rect.get("h", 0), 1, 30, 11.8),
        background=_bg(styles),
        position=_pos(rect),
        z_order=ce.index,
    )


def _to_text_box(ce: ClassifiedElement) -> TextBoxElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})
    # directText excludes icon-span text by construction (text nodes only);
    # text_no_icons is JS-side textContent with material-icons spans
    # stripped. No heuristic filtering — see extract_layout.js
    # textWithoutIconSpans.
    text = (elem.get("directText", "").strip()
            or elem.get("text_no_icons", "").strip()
            or elem.get("text", "").strip())
    inline_runs = elem.get("inlineRuns")  # [[{text,color,bold,...}, ...], ...]
    # Element-level opacity carries through to every run so semi-transparent
    # text (watermarks, ghosted section numbers, hint text) renders correctly.
    # Use cumulative_opacity so a wrapping <div style="opacity:…"> around
    # the text is honoured (the browser applies it multiplicatively).
    opacity = _cumulative_opacity(elem)

    # Infer text level from tag
    tag = elem.get("tag", "").upper()
    level = "body"
    if tag in ("H1",):
        level = "title"
    elif tag in ("H2",):
        level = "subtitle"
    elif tag in ("H3",):
        level = "h3"
    elif tag in ("H4",):
        level = "h4"
    elif tag in ("SMALL", "CAPTION"):
        level = "caption"

    # Font size can also hint at level
    font_pt = styles.get("fontSize_pt", 0)
    if level == "body" and font_pt >= 36:
        level = "title"
    elif level == "body" and font_pt >= 28:
        level = "subtitle"

    alignment = styles.get("textAlign", "left") or "left"
    if alignment not in ("left", "center", "right"):
        alignment = "left"

    # Build content: prefer inlineRuns (preserves <br> + per-run styling from
    # inline children like <span>). Fall back to a single block otherwise.
    if inline_runs:
        blocks = []
        for line in inline_runs:
            runs = []
            for r in line:
                # Preserve run text verbatim — the JS extractor is responsible
                # for normalising whitespace appropriately per white-space mode
                # (collapse+trim for normal text; preserve for <pre>). Stripping
                # here would destroy indentation and inter-token spaces in
                # code blocks.
                r_text = r.get("text") or ""
                if r_text == "":
                    continue
                r_color, r_opacity = _normalize_color_opacity(r.get("color"), opacity)
                runs.append(TextRun(
                    text=r_text,
                    font_size_pt=_clamp(r.get("font_size_pt"), 6, 500),
                    color=r_color,
                    bold=bool(r.get("bold")),
                    italic=bool(r.get("italic")),
                    font_name=r.get("font_name"),
                    opacity=r_opacity,
                ))
            if not runs:
                continue
            blocks.append(TextBlock(
                text="".join(r.text for r in runs),
                level=level,
                runs=runs,
                alignment=alignment,
            ))
        content = blocks if blocks else None
    else:
        content = None

    if content is None:
        # Fallback: single TextRun/TextBlock from the element's own style.
        fallback_color, fallback_opacity = _normalize_color_opacity(
            styles.get("color"), opacity
        )
        run = TextRun(
            text=text,
            font_size_pt=_clamp(font_pt, 6, 500),
            color=fallback_color,
            bold=_is_bold(styles),
            italic=styles.get("fontStyle") == "italic",
            font_name=styles.get("fontFamily"),
            opacity=fallback_opacity,
        )
        content = [TextBlock(
            text=text,
            level=level,
            runs=[run],
            alignment=alignment,
        )]

    range_line_count = int(ce.data.get("range_line_count", 0) or 0)
    natural_w = float(ce.data.get("textNaturalWidth_pct", 0) or 0)
    box_w = float(rect.get("w", 0) or 0)
    # Atomic-token overflow detection. `range_line_count == 1` gate is
    # critical: multi-line text (range_line_count > 1) can also have
    # natural > box_w (long paragraph whose total length exceeds the
    # container), but that text MUST keep wrap capability — only
    # single-line atomic tokens should disable wrap.
    no_wrap = range_line_count == 1 and _is_atomic_overflow(natural_w, box_w)

    return TextBoxElement(
        type="text_box",
        content=content,
        vertical_align="top",
        no_wrap=no_wrap,
        position=_widen_text_position(
            rect,
            range_line_count=range_line_count,
            range_max_line_width_pct=float(ce.data.get("range_max_line_width_pct", 0) or 0),
            natural_width_pct=natural_w,
            alignment=alignment,
            text=text,
            # Mirror the renderer's word_wrap decision: no_wrap=True means
            # textbox == visual extent, tolerance must be 0 to avoid
            # sibling overlap; otherwise keep default 4% for PPT font
            # metric slack.
            cap_tolerance_pct=0.0 if no_wrap else 4.0,
        ),
        z_order=ce.index,
    )


def _to_gradient_overlay(ce: ClassifiedElement) -> GradientOverlayElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})

    return GradientOverlayElement(
        type="gradient_overlay",
        gradient=_gradient(styles.get("backgroundGradient")),
        opacity=_clamp(styles.get("opacity"), 0, 1, 0.85),
        position=_pos(rect),
        z_order=ce.index,
    )


def _to_shape(ce: ClassifiedElement) -> ShapeElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})

    return ShapeElement(
        type="shape",
        shape_type=_shape_type_from_styles(styles, rect),
        background=_bg(styles),
        border=_border(styles),
        corner_radius_pct=_corner_radius_pct(styles, rect),
        rotation_deg=0.0,
        position=_pos(rect),
        z_order=ce.index,
    )


def _to_blur_glow(ce: ClassifiedElement, elements: list) -> BlurGlowElement:
    elem = ce.data
    styles = elem.get("styles", {})
    rect = elem.get("rect_pct", {})

    # Extract blur radius from CSS filter: "blur(25px)" -> 25 * 0.75 = 18.75pt
    blur_pt = 0.0
    filter_val = styles.get("filter", "")
    if filter_val:
        import re as _re
        m = _re.search(r'blur\(([\d.]+)px\)', filter_val)
        if m:
            blur_pt = float(m.group(1)) * 0.75  # px -> pt

    # Determine opacity: use CSS opacity, but also check for rgba alpha in
    # backgroundColor (the JS extractor outputs #RRGGBBAA when alpha < 1.0).
    # Use the canonical color_opacity helper so classifier and converter
    # agree on what "semi-transparent" means.
    opacity = _clamp(styles.get("opacity", 1.0), 0, 1, 1.0)
    bg = styles.get("backgroundColor") or ""
    bg_alpha = color_opacity(bg)
    if bg_alpha < 1.0:
        opacity = bg_alpha
    # Strip alpha from color for PPTX (PPTX uses separate opacity)
    color = bg[:7] if bg and len(bg) >= 7 else (bg or "#133EFF")

    return BlurGlowElement(
        type="blur_glow",
        color=color,
        opacity=opacity,
        blur_radius_pt=blur_pt,
        position=_pos(rect),
        z_order=ce.index,
    )


def _find_row_bg(elements: list, tree: ContainmentTree,
                 row_container_idx: Optional[int],
                 row_cell_indices: List[int]) -> Optional[BackgroundDef]:
    """Resolve the background colour for a table row.

    Real ``<table>`` markup commonly puts the row tint on the inner
    ``<tr>``, but with ``border-collapse`` rounding that ``<tr>``'s bbox
    may not enclose the cells — so the containment tree assigns the
    cells' parent to ``<thead>``/``<tbody>`` instead, and the ``<tr>``
    becomes a *sibling* of the row container rather than an ancestor of
    the cells. A pure tree walk from the row container would miss it.

    The fix is a spatial lookup: among ALL elements whose Y range overlaps
    with this row's Y range (>=50% of the smaller height) AND whose bbox
    is fully inside the table container, pick the smallest one that has
    a non-transparent bg.
    """
    if not row_cell_indices:
        return None

    # The row's Y range from its first cell.
    first_cell_rect = elements[row_cell_indices[0]].get("rect_pct", {})

    # 1. row_container itself (cheap fast path)
    if row_container_idx is not None:
        bg = _bg(elements[row_container_idx].get("styles", {}))
        if bg is not None:
            return bg

    # 2. Spatial lookup across the whole elements list. Restrict to
    # candidates that are roughly the same height as the row (within 2x)
    # so we don't match the whole table or the slide background.
    row_y1 = first_cell_rect.get("y", 0)
    row_y2 = row_y1 + first_cell_rect.get("h", 0)
    row_h = first_cell_rect.get("h", 0) or 1.0
    # Row X extent = union of all cells' X ranges. Cells share Y but
    # span the table left-to-right; the union is the row's full width.
    row_x1 = min(
        elements[idx].get("rect_pct", {}).get("x", 0)
        for idx in row_cell_indices
    )
    row_x2 = max(
        elements[idx].get("rect_pct", {}).get("x", 0)
        + elements[idx].get("rect_pct", {}).get("w", 0)
        for idx in row_cell_indices
    )
    row_w = (row_x2 - row_x1) or 1.0

    best_bg: Optional[BackgroundDef] = None
    best_area = float("inf")
    for el in elements:
        bg = _bg(el.get("styles", {}))
        if bg is None:
            continue
        r = el.get("rect_pct", {})
        ry1 = r.get("y", 0)
        ry2 = ry1 + r.get("h", 0)
        rh = r.get("h", 0) or 1.0
        # Y-overlap fraction (relative to the shorter of the two).
        oy = max(0.0, min(row_y2, ry2) - max(row_y1, ry1))
        overlap_ratio_y = oy / min(row_h, rh)
        if overlap_ratio_y < 0.5:
            continue
        # X-overlap fraction (relative to the shorter of the two).
        # Rejects elements in other columns of a multi-column layout
        # that share Y range with this row but never overlap the table
        # in X (e.g. a colored card in the right column leaking its bg
        # into a left-column table's body row).
        rx1 = r.get("x", 0)
        rx2 = rx1 + r.get("w", 0)
        rw = r.get("w", 0) or 1.0
        ox = max(0.0, min(row_x2, rx2) - max(row_x1, rx1))
        overlap_ratio_x = ox / min(row_w, rw)
        if overlap_ratio_x < 0.5:
            continue
        # Reject candidates far larger than the row (table or slide bg).
        if rh > 2.5 * row_h:
            continue
        area = r.get("w", 0) * rh
        if area < best_area:
            best_bg, best_area = bg, area

    return best_bg


def _find_row_border_bottom(elements: list, tree: ContainmentTree,
                            row_container_idx: Optional[int],
                            row_cell_indices: List[int]) -> Optional[BorderDef]:
    """Resolve the bottom border (row separator) for a table row.

    Tries row_container first; if that has no border-bottom, samples the
    first cell's border-bottom (real ``<table>`` markup commonly puts the
    separator on ``<th>``/``<td>`` directly, not on ``<thead>``).
    """
    sources = []
    if row_container_idx is not None:
        sources.append(row_container_idx)
    if row_cell_indices:
        sources.append(row_cell_indices[0])

    for idx in sources:
        if idx is None:
            continue
        styles = elements[idx].get("styles", {})
        bb_w = styles.get("borderBottomWidth", 0)
        try:
            bb_w_f = float(bb_w) if bb_w else 0.0
        except (ValueError, TypeError):
            bb_w_f = 0.0
        if bb_w_f > 0:
            return BorderDef(
                color=styles.get("borderBottomColor") or "#CCCCCC",
                width_pt=_clamp(bb_w, 0, 50, 0),
                style="solid",
            )
    return None


def _to_table(ce: ClassifiedElement, elements: list, tree: ContainmentTree) -> TableElement:
    """Build a TableElement from a detected table group.

    Re-runs ``detect_tables`` (pure/deterministic) to recover the grid for
    this container, then assembles styled cells. The row background (e.g. a
    header tint) is read from each row's row-container element, since the
    cell elements themselves are usually transparent.
    """
    from shuttleslide.html_to_pptx.rule.containment import detect_tables

    groups = detect_tables(elements, tree)
    group = next((g for g in groups if g.container_idx == ce.index), None)
    if group is None:
        # Detection mismatch (shouldn't happen) — degrade gracefully.
        return TableElement(type="table", position=_pos(ce.data.get("rect_pct", {})))

    rects = [e.get("rect_pct", {}) for e in elements]
    grid = group.grid
    n_rows = len(grid)
    n_cols = len(grid[0]) if grid else 0

    rows_dsl: List[List[TableCell]] = []
    for r in range(n_rows):
        row_container_idx = (
            group.row_container_idx[r]
            if r < len(group.row_container_idx) else None
        )
        # Row background: try row_container first, then walk down to its
        # descendants and up to its ancestors. Needed because in real
        # <table> markup the bg is often on an inner <tr> whose bbox may
        # not enclose the cells (border-collapse rounding), leaving the
        # containment tree to assign cells' parent to <thead>/<tbody>
        # instead — so row_container (<thead>) has bg=None even though
        # the inner <tr> carries the row tint.
        row_bg = _find_row_bg(elements, tree, row_container_idx, grid[r])
        row_cells: List[TableCell] = []
        for c in range(n_cols):
            cell_elem = elements[grid[r][c]]
            styles = cell_elem.get("styles", {})
            alignment = styles.get("textAlign", "left") or "left"
            if alignment not in ("left", "center", "right"):
                alignment = "left"
            runs = _runs_from_element(cell_elem)
            row_cells.append(TableCell(
                text="".join(run.text for run in runs),
                runs=runs,
                background=row_bg,
                alignment=alignment,
            ))
        rows_dsl.append(row_cells)

    # Column widths / row heights from cell rects.
    col_widths_pct = [
        round(sum(rects[grid[r][c]].get("w", 0) for r in range(n_rows)) / max(n_rows, 1), 2)
        for c in range(n_cols)
    ]
    row_heights_pct = [
        round(rects[grid[r][0]].get("h", 0), 2) for r in range(n_rows)
    ]

    # Table position = bounding box of all cells.
    xs, ys, rights, bottoms = [], [], [], []
    for r in range(n_rows):
        for c in range(n_cols):
            rc = rects[grid[r][c]]
            xs.append(rc.get("x", 0))
            ys.append(rc.get("y", 0))
            rights.append(rc.get("x", 0) + rc.get("w", 0))
            bottoms.append(rc.get("y", 0) + rc.get("h", 0))
    position = PositionPercent(
        x_pct=min(xs) if xs else 0,
        y_pct=min(ys) if ys else 0,
        w_pct=round((max(rights) - min(xs)) if rights else 0, 2),
        h_pct=round((max(bottoms) - min(ys)) if bottoms else 0, 2),
    )

    # Outline border from the container; row separator from row-div border-bottom.
    container_styles = ce.data.get("styles", {})
    border = _border(container_styles)
    # Row separator: border-bottom on row 0's container OR any of row 0's cells.
    # For real <table> markup the border-bottom is on the <th> cells directly
    # (CSS `border-bottom: 3px solid #6366f1` on each <th>), not on <thead>.
    row_separator = _find_row_border_bottom(
        elements, tree,
        group.row_container_idx[0] if group.row_container_idx else None,
        grid[0] if grid else [],
    )

    # Header row: row 0's effective bg is non-transparent, OR its cells are bold.
    header_row = False
    if rows_dsl and rows_dsl[0] and rows_dsl[0][0].background is not None:
        header_row = True
    if not header_row and rows_dsl and rows_dsl[0]:
        header_row = any(
            cell.runs and all(run.bold for run in cell.runs)
            for cell in rows_dsl[0]
        )

    return TableElement(
        type="table",
        rows=rows_dsl,
        col_widths_pct=col_widths_pct,
        row_heights_pct=row_heights_pct,
        header_row=header_row,
        border=border,
        row_separator=row_separator,
        position=position,
        z_order=ce.index,
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

def _to_svg(ce: ClassifiedElement) -> SVGElement:
    """Lift raw SVG markup into SVGElement. No expansion — the renderer
    invokes the vendored svg_to_pptx library to produce native shapes.
    """
    elem = ce.data
    attrs = elem.get("attrs", {}) or {}
    styles = elem.get("styles", {}) or {}
    return SVGElement(
        type="svg",
        svg_markup=attrs.get("svg_markup", "") or "",
        slot_id=attrs.get("data-slot", "") or "",
        viewBox=attrs.get("viewBox"),
        # Cumulative ancestor opacity: when a <div style="opacity:0.25">
        # wraps the <svg> (ambient background pattern in 1.html and
        # similar), the browser renders every shape at 25%. We capture
        # the product here so the renderer can hand it to the vendored
        # library as inherited_styles — multiplying into every fill /
        # stroke alpha.
        opacity=_cumulative_opacity(elem),
        # object-fit captured by extract_layout.js from the SVG's
        # data-object-fit attribute (stamped by inline_svg_placeholders
        # from the originating <img>); mirrors _to_image's reading of
        # styles.objectFit for raster images. The renderer uses this
        # to choose uniform scale + center (cover/contain) vs. stretch
        # (fill). Border / corner_radius are also in styles but are
        # NOT honored by _render_svg yet — they'd need an overlay
        # rect on the grpSp wrapper; deferred until a slide actually
        # uses them on a placeholder <img>.
        object_fit=styles.get("objectFit", "fill") or "fill",
        position=_pos(elem.get("rect_pct")),
        z_order=ce.index,
    )


_CONVERTERS = {
    "icon_text": _to_icon_text,
    "image": lambda ce, el, tree: _to_image(ce),
    "divider_line": lambda ce, el, tree: _to_divider_line(ce),
    "badge": _to_badge,
    "card": _to_card,
    "numbered_step": _to_numbered_step,
    "bullet_list": _to_bullet_list,
    "title_bar": _to_title_bar,
    "text_box": lambda ce, el, tree: _to_text_box(ce),
    "gradient_overlay": lambda ce, el, tree: _to_gradient_overlay(ce),
    "shape": lambda ce, el, tree: _to_shape(ce),
    "blur_glow": lambda ce, el, tree: _to_blur_glow(ce, el),
    "table": _to_table,
    "svg": lambda ce, el, tree: _to_svg(ce),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def convert_to_dsl(
    classified: List[ClassifiedElement],
    elements: List[dict],
    tree: ContainmentTree,
) -> List[SlideElementDSL]:
    """Convert classified elements to DSL dataclass instances.

    Args:
        classified: Output from classifier.classify_elements().
        elements: Original Playwright element list (for parent/child lookups).
        tree: Spatial containment tree.

    Returns:
        List of SlideElementDSL subclass instances.
    """
    result = []
    for ce in classified:
        converter = _CONVERTERS.get(ce.elem_type)
        if converter is None:
            # Fallback to text_box
            dsl_elem = _to_text_box(ce)
        else:
            dsl_elem = converter(ce, elements, tree)

        # Override z_order with browser-computed visual stacking order
        # (from elementsFromPoint in extract_layout.js)
        dsl_elem.z_order = ce.data.get("z_order", ce.index)
        result.append(dsl_elem)

    return result
