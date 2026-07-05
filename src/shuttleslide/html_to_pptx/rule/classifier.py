"""
Element classification rule chain for rule-based HTML-to-PPTX conversion.

Implements a priority-ordered set of rules, each checking whether a
Playwright-extracted element matches a specific PPTX element type.
First matching rule wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set

from shuttleslide.html_to_pptx.rule.containment import (
    ContainmentTree,
    NumberedGroup,
    BulletGroup,
    TableGroup,
    detect_numbered_sequences,
    detect_bullet_groups,
    detect_tables,
)
from shuttleslide.html_to_pptx.style_mapper import color_opacity


# Image URL patterns
_IMAGE_EXTS: FrozenSet[str] = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico"}
)


@dataclass
class ClassifiedElement:
    """An element annotated with its classified type."""
    index: int              # original index in elements list
    elem_type: str          # e.g. "text_box", "card", "image"
    data: dict              # original Playwright element dict


def _has_text(elem: dict) -> bool:
    return bool(elem.get("directText", "").strip()) or bool(elem.get("text", "").strip())


def _has_bg_color(styles: dict) -> bool:
    bg = styles.get("backgroundColor")
    return bg is not None and bg not in ("transparent", "rgba(0, 0, 0, 0)")


def _has_gradient(styles: dict) -> bool:
    return styles.get("backgroundGradient") is not None


def _has_border_radius(styles: dict) -> bool:
    br = styles.get("borderRadius", "")
    if not br:
        return False
    try:
        return float(br.replace("px", "")) > 0
    except (ValueError, AttributeError):
        return bool(br)


def _parse_border_radius_px(styles: dict) -> float:
    br = styles.get("borderRadius", "0")
    if not br:
        return 0.0
    try:
        return float(br.replace("px", ""))
    except (ValueError, AttributeError):
        return 0.0


def _is_bold(styles: dict) -> bool:
    fw = styles.get("fontWeight", "400")
    try:
        return float(fw) >= 600
    except (ValueError, TypeError):
        return fw == "bold"


# ---------------------------------------------------------------------------
# Classification rules (priority order)
# ---------------------------------------------------------------------------

def _check_icon_text(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 1: icon + text combinations -> icon_text.

    Three patterns match:
    a) The element itself is an icon (``<i class="material-icons">name</i>``)
       with text — the icon_name is the text.
    b) The element has its own DIRECT display text AND contains a child
       icon (e.g. ``<h3><i>developer_board</i>Heading</h3>`` with
       ``display: flex``). Without this rule the heading would render as
       a full-width text_box and the child icon as a separate icon_text
       at the same x, producing an overlap.
    c) A flex container whose DIRECT non-icon child is a LEAF text element
       (e.g. a ``<span>``) carrying the label —
       ``<div class="flex"><i>icon</i><span>label</span></div>``. The
       span has directText, so its label can be lifted onto the
       icon_text parent.

    Patterns (b)/(c) require the label to come from the element itself or
    a single direct text-bearing child. They explicitly REJECT any element
    with multiple non-icon children carrying visible text — that's a card
    or layout container, not a single icon + label pair.

    INVARIANT: icon_text's schema can only carry a plain label string
    (the ``text`` field). Pattern (c) therefore REQUIRES the lone
    label child to be a LEAF element (``<h3>``, ``<span>label</span>``
    without INLINE_TAGS children). When the label child is itself a
    container — e.g. ``<span><strong>Tip:</strong> ...</span>`` — the
    icon_text schema can't capture the styled runs, and the absorbed-
    indices mechanism (which only catches leaf-text siblings) won't
    swallow the container, so it would render separately as a text_box
    carrying the full inlineRuns. Two overlapping text lines result.
    Falling through lets the container render naturally as a text_box
    (inlineRuns intact) while the icon classifies on its own.
    """
    if elem.get("is_icon") and _has_text(elem):
        return "icon_text"
    if not _has_child_icon(idx, ctx):
        return None
    # Pattern (b): element has direct text of its own.
    if elem.get("directText", "").strip():
        return "icon_text"
    # Pattern (c): count direct non-icon children that carry visible
    # text content. A real icon_text has exactly one such child — the
    # label (e.g. <span>, <h3>). A card with icon + h3 + <ul>/<p> has
    # multiple (the h3 label AND the ul/p with their own text),
    # proving it's a complex container that should render as a card,
    # not collapse to a single icon + label pair.
    #
    # Use ``text`` (descendant-concatenated) for the visibility check
    # so containers like <ul> (whose directText is empty but contains
    # LI text) are correctly counted — otherwise a card with icon +
    # h3 + ul would see only the h3 as a "label" and match icon_text.
    #
    # INVARIANT (see docstring): the lone label child must also be a
    # LEAF. If it's a container (e.g. <span><strong>Tip:</strong>
    # ...</span>), icon_text's plain-string label can't capture the
    # styled runs and the absorbed mechanism won't swallow the
    # container — it would render separately and overlap. Falling
    # through lets the label container render as a text_box with
    # inlineRuns intact, and the icon classify on its own via (a).
    content_children = 0
    label_child_is_container = False
    for child_idx in ctx.tree.get_children(idx):
        child = ctx.elements[child_idx]
        if child.get("is_icon"):
            continue
        text = child.get("directText", "").strip() or child.get("text", "").strip()
        if text:
            content_children += 1
            if ctx.tree.is_container(child_idx):
                label_child_is_container = True
    if content_children == 1 and not label_child_is_container:
        return "icon_text"
    return None


def _has_child_icon(idx: int, ctx: "_ClassificationContext") -> bool:
    """True if any direct child of element ``idx`` is a Material Icons element."""
    for child_idx in ctx.tree.get_children(idx):
        if ctx.elements[child_idx].get("is_icon"):
            return True
    return False


def _check_image(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 2: tag=IMG or attrs.src is image URL -> image."""
    tag = elem.get("tag", "").upper()
    if tag == "IMG":
        return "image"
    src = elem.get("attrs", {}).get("src", "")
    if src:
        src_lower = src.lower()
        if src_lower.startswith("data:image/"):
            return "image"
        for ext in _IMAGE_EXTS:
            if src_lower.split("?")[0].endswith(ext):
                return "image"
    return None


def _check_divider_line(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 3: thin horizontal line -> divider_line."""
    rect = elem.get("rect_pct", {})
    styles = elem.get("styles", {})
    h = rect.get("h", 0)
    w = rect.get("w", 0)
    text = elem.get("directText", "").strip() or elem.get("text", "").strip()

    if h < 1.5 and not text and _has_bg_color(styles) and w > 20:
        return "divider_line"
    return None


def _check_badge(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 4: badge/pill shape -> badge."""
    # Number circles belong to numbered_step sequences — let them fall
    # through to _check_numbered_step instead of being classified as badges.
    for group in ctx.numbered_groups:
        if idx in group.number_elements:
            return None

    classes = [c.lower() for c in elem.get("classes", [])]
    rect = elem.get("rect_pct", {})
    styles = elem.get("styles", {})

    if "badge" in classes:
        return "badge"

    w = rect.get("w", 0)
    h = rect.get("h", 0)
    text = elem.get("directText", "").strip() or elem.get("text", "").strip()

    if (w < 25 and h < 18 and text and _has_bg_color(styles)
            and _has_border_radius(styles)):
        return "badge"
    return None


def _check_card(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 5: card container -> card."""
    classes = [c.lower() for c in elem.get("classes", [])]
    rect = elem.get("rect_pct", {})
    styles = elem.get("styles", {})

    # Decorative elements with blur filter or very low opacity should fall
    # through to _check_blur_glow, not be classified as cards. Without this
    # guard, the 500×500 decorative blur-glow in 1.html (which spatially
    # contains the title and badges) matches the card rule first because
    # the spatial containment tree sees the text-bearing title/badges as
    # its "children" — producing a solid purple rectangle in the PPTX
    # instead of a translucent blurred circle.
    if styles.get("filter") is not None:
        return None
    try:
        opacity = float(styles.get("opacity", 1.0))
    except (TypeError, ValueError):
        opacity = 1.0
    if opacity < 0.3:
        return None

    if "card" in classes:
        return "card"

    w = rect.get("w", 0)
    h = rect.get("h", 0)

    # Container with background + border radius + children with text.
    # h > 5 (not 15) so small one-line callouts (e.g. dashed-border note
    # boxes, faint-bg tinted strips) are still classified as cards rather
    # than being dropped. Badge rule above already guards w < 25, so
    # width-bounded badges stay safe.
    if (w > 25 and h > 5
            and (_has_bg_color(styles) or _has_gradient(styles))
            and _has_border_radius(styles)):
        children = ctx.tree.get_children(idx)
        if children:
            for child_idx in children:
                child = ctx.elements[child_idx]
                if _has_text(child):
                    return "card"
    return None


def _check_numbered_step(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 6: number circle in a numbered sequence -> numbered_step.

    Only the small number circle itself is classified as numbered_step.
    The wrapper container is left unclassified (no rule matches a pure
    flex layout div) so its other children — content card, title,
    description, arrow icon — render naturally without overlapping the
    numbered_step's own rendering.
    """
    for group in ctx.numbered_groups:
        if idx in group.number_elements:
            return "numbered_step"
    return None


def _check_bullet_list(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 7: LI or list/bullet class -> bullet_list."""
    for group in ctx.bullet_groups:
        if idx in group.item_elements:
            return "bullet_list"
    return None


def _check_title_bar(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 8: top full-width bar with background + text -> title_bar."""
    rect = elem.get("rect_pct", {})
    styles = elem.get("styles", {})

    y = rect.get("y", 0)
    w = rect.get("w", 0)
    h = rect.get("h", 0)
    text = elem.get("directText", "").strip() or elem.get("text", "").strip()

    if (y < 15 and w > 90 and h < 20
            and (_has_gradient(styles) or _has_bg_color(styles))
            and text):
        return "title_bar"
    return None


def _check_text_box(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 9: text content -> text_box.

    Uses directText to avoid classifying container elements as text_box
    when their text comes from child elements (e.g. icon names inside badges).
    """
    direct = elem.get("directText", "").strip()
    if direct:
        return "text_box"
    # Fallback to text only if element has no children (leaf element)
    text = elem.get("text", "").strip()
    if text and elem.get("child_count", 0) == 0:
        return "text_box"
    return None


def _check_gradient_overlay(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 10: gradient covering large area with no text -> gradient_overlay."""
    rect = elem.get("rect_pct", {})
    styles = elem.get("styles", {})
    text = elem.get("directText", "").strip() or elem.get("text", "").strip()

    w = rect.get("w", 0)
    h = rect.get("h", 0)

    if _has_gradient(styles) and w > 30 and h > 20 and not text:
        return "gradient_overlay"
    return None


def _check_shape(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 11: background area with no text -> shape."""
    styles = elem.get("styles", {})
    text = elem.get("directText", "").strip() or elem.get("text", "").strip()

    if not text and (_has_bg_color(styles) or _has_gradient(styles)):
        opacity = styles.get("opacity", 1.0)
        # Low opacity -> likely blur_glow, not shape
        if opacity < 0.5:
            return None
        # Has blur filter -> should be blur_glow, not shape
        if styles.get("filter") is not None:
            return None
        return "shape"
    return None


def _check_blur_glow(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule 12: decorative blur/glow circle -> blur_glow."""
    rect = elem.get("rect_pct", {})
    styles = elem.get("styles", {})
    text = elem.get("directText", "").strip() or elem.get("text", "").strip()

    if text:
        return None

    opacity = styles.get("opacity", 1.0)
    w = rect.get("w", 0)
    h = rect.get("h", 0)

    # Semi-transparent, approximately square, large border radius
    is_square = abs(w - h) < 5
    has_big_radius = _parse_border_radius_px(styles) > 20
    has_filter = styles.get("filter") is not None

    # Semi-transparent via CSS opacity OR background-color alpha (#RRGGBBAA).
    # Use the canonical color_opacity helper to keep this consistent with
    # the converter's interpretation of the same hex value.
    has_low_opacity = opacity < 0.5
    bg = styles.get("backgroundColor") or ""
    has_low_alpha_bg = color_opacity(bg) < 0.6

    if (has_low_opacity or has_low_alpha_bg) and (is_square or has_big_radius or has_filter):
        return "blur_glow"
    # Also classify as blur_glow if it has a blur filter (regardless of opacity)
    if has_filter:
        return "blur_glow"
    return None


# Ordered rule chain
# _check_svg must be FIRST: an inline <svg> may contain <text>, <rect>, etc.
# that would otherwise be misclassified by _check_icon_text or _check_shape.
def _check_svg(elem: dict, idx: int, ctx: "_ClassificationContext") -> Optional[str]:
    """Rule: <svg> element → svg. Tag-only — don't require data-slot
    so we never silently drop an SVG.
    """
    if elem.get("tag", "").upper() == "SVG":
        return "svg"
    return None


_RULES = [
    _check_svg,
    # _check_badge MUST run before _check_icon_text: a <span class="badge">
    # with <i>icon</i> + text matches icon_text's pattern (b) (direct text +
    # child icon) and would otherwise be misclassified as icon_text, which
    # has no background field — dropping the pill background entirely.
    # _check_badge is strict (requires class="badge" OR a small pill with
    # bg + radius + text), so it won't steal genuine icon_text like
    # <h3><i>icon</i>Heading</h3> (no bg/radius).
    _check_badge,
    _check_icon_text,
    _check_image,
    _check_divider_line,
    _check_card,
    _check_numbered_step,
    _check_bullet_list,
    _check_title_bar,
    _check_text_box,
    _check_gradient_overlay,
    _check_shape,
    _check_blur_glow,
]


# ---------------------------------------------------------------------------
# Internal classification context
# ---------------------------------------------------------------------------

@dataclass
class _ClassificationContext:
    """Shared context for element classification."""
    elements: List[dict] = field(default_factory=list)
    tree: ContainmentTree = field(default_factory=ContainmentTree)
    numbered_groups: List[NumberedGroup] = field(default_factory=list)
    bullet_groups: List[BulletGroup] = field(default_factory=list)
    table_groups: List[TableGroup] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Element types whose Python converters actually render the merged
# inlineRuns (or carry the text directly via directText/step_number).
# `absorbedByParent` (set by extract_layout.js) is only safe when one
# of these types sits in the ancestor chain — otherwise the absorbed
# element's text is silently dropped. Used by `_has_text_rendering_absorber`.
_TEXT_RENDERING_TYPES: FrozenSet[str] = frozenset({
    "text_box", "badge", "title_bar", "numbered_step", "icon_text",
})


def classify_elements(
    elements: List[dict],
    tree: ContainmentTree,
) -> List[ClassifiedElement]:
    """Classify all elements using the rule chain.

    Args:
        elements: Playwright-extracted element dicts.
        tree: Spatial containment tree.

    Returns:
        List of ClassifiedElement with type annotations.
        Elements that are children of cards are absorbed (not returned).
    """
    # Detect multi-element patterns
    numbered_groups = detect_numbered_sequences(elements, tree)
    bullet_groups = detect_bullet_groups(elements, tree)
    table_groups = detect_tables(elements, tree)

    ctx = _ClassificationContext(
        elements=elements,
        tree=tree,
        numbered_groups=numbered_groups,
        bullet_groups=bullet_groups,
        table_groups=table_groups,
    )

    # Classification is pure (no side effects) but expensive; cache results
    # so the absorbedByParent geometric scan below doesn't re-run the rule
    # chain for the same element on every containment check.
    classification_cache: Dict[int, Optional[str]] = {}

    def _cached_classify(idx: int) -> Optional[str]:
        if idx not in classification_cache:
            classification_cache[idx] = _classify_single(elements[idx], idx, ctx)
        return classification_cache[idx]

    # Tolerance matches build_containment_tree's rect-tie tolerance.
    # Inherited here so the geometric scan stays consistent with the
    # tree's containment semantics.
    _ABS_TOL = 0.5  # pct

    def _has_text_rendering_absorber(child_idx: int) -> bool:
        """Return True if any geometrically-containing ancestor of
        ``child_idx`` has ``inlineRuns`` AND is classified as a
        text-rendering type.

        ``extract_layout.js`` marks an inline descendant as
        ``absorbedByParent`` whenever ANY ancestor has ``inlineRuns``,
        on the assumption that the ancestor will render that text. That
        assumption only holds for ancestors whose Python converter has a
        text-rendering path (text_box / badge / title_bar /
        numbered_step / icon_text). When the only absorbing ancestors
        are non-text-rendering containers — cards render as a frame
        only; unclassified wrapper divs don't render at all — the
        absorbed element's text is silently lost. This helper backs the
        un-absorption decision in the main loop.

        Why a geometric scan instead of ``tree.get_parent`` walks: when
        a wrapper div and the card inside it share the same bbox (very
        common — the wrapper exists only to apply outer margins), the
        containment tree picks one as parent and treats the other as a
        sibling, so walking ancestors can miss the card.
        """
        rect_i = elements[child_idx].get("rect_pct", {})
        if not rect_i:
            return False
        ix1 = rect_i.get("x", 0)
        iy1 = rect_i.get("y", 0)
        ix2 = ix1 + rect_i.get("w", 0)
        iy2 = iy1 + rect_i.get("h", 0)
        for j, other in enumerate(elements):
            if j == child_idx or not other.get("inlineRuns"):
                continue
            rect_j = other.get("rect_pct", {})
            jx1 = rect_j.get("x", 0)
            jy1 = rect_j.get("y", 0)
            jx2 = jx1 + rect_j.get("w", 0)
            jy2 = jy1 + rect_j.get("h", 0)
            if (ix1 >= jx1 - _ABS_TOL and iy1 >= jy1 - _ABS_TOL
                    and ix2 <= jx2 + _ABS_TOL and iy2 <= jy2 + _ABS_TOL):
                if _cached_classify(j) in _TEXT_RENDERING_TYPES:
                    return True
        return False

    # Collect indices absorbed by containers (cards, badges)
    absorbed_indices: Set[int] = set()
    # Map each table container index -> its TableGroup so the main loop can
    # emit a single TableElement and skip the absorbed cells.
    table_container_to_group: Dict[int, TableGroup] = {
        g.container_idx: g for g in table_groups
    }
    table_cell_indices: Set[int] = set()
    for g in table_groups:
        table_cell_indices |= g.consumed

    # Bullet groups: mirror the table pattern. One UL container becomes a
    # single BulletListElement; its LI children are absorbed. The previous
    # per-LI emission forced the bullet_list frame to the LI bbox, which
    # excludes the UL's padding-left (where the browser renders the
    # ::marker). That left no room for both bullet and text in PPTX, so
    # any text whose natural width approached the LI width wrapped.
    # Using the UL bbox gives the frame the marker padding area too.
    bullet_container_to_group: Dict[int, BulletGroup] = {
        g.parent_idx: g for g in bullet_groups if g.parent_idx is not None
    }
    bullet_item_indices: Set[int] = set()
    for g in bullet_groups:
        if g.parent_idx is not None:
            bullet_item_indices |= set(g.item_elements)

    classified: List[ClassifiedElement] = []

    for i, elem in enumerate(elements):
        # Skip children of containers (they will be absorbed into the parent).
        # Badges absorb their children (icon + label). Title bars absorb their
        # children (typically an <h1>) so the title text isn't rendered twice
        # — once by the bar (at the div's inherited font-size) and once by the
        # child text_box. Numbered steps absorb their children because the
        # digit text is rendered by the numbered_step itself via step_number,
        # and the typical structure is `<rounded-full div><span>1</span></div>`
        # — without absorption, the inner span renders as a separate text_box
        # producing a duplicate digit on top of the circle.
        # Icon-text (heading-with-icon pattern) absorbs ONLY its icon child,
        # not other children — but since flex headings like
        # `<h3><i>icon</i>Heading</h3>` only have the one icon child,
        # absorbing it is safe. Other children (rare in this pattern) would
        # still get rendered.
        # Cards render as a frame and their children render as separate
        # elements on top.
        parent = tree.get_parent(i)
        if parent is not None:
            parent_elem = elements[parent]
            parent_type = _cached_classify(parent)
            if parent_type in ("badge", "title_bar", "numbered_step"):
                absorbed_indices.add(i)
            elif parent_type == "icon_text":
                # Absorb the icon child AND any leaf-text sibling (e.g.
                # <span>label</span>). The icon_text parent already carries
                # the label via its `text` field (read from the span child);
                # rendering the span separately would duplicate the text.
                # Skip nested containers (cards/badges) so they still render.
                if elem.get("is_icon"):
                    absorbed_indices.add(i)
                elif (elem.get("directText", "").strip()
                      and not tree.is_container(i)):
                    absorbed_indices.add(i)

        if i in absorbed_indices:
            continue

        # JS marks inline-only descendants (span, strong, etc., merged into
        # parent's inlineRuns) so they don't double-render. The merge is
        # only correct when an ancestor actually renders inlineRuns as
        # text. Cards (and unclassified wrappers around them) absorb
        # their inline descendants but have no text-rendering path —
        # honouring the flag there silently drops the text. Verify a
        # text-rendering absorber exists; otherwise fall through and
        # classify the element (typically as text_box) so it renders on
        # top of the card frame, matching the card-frame contract.
        if elem.get("absorbedByParent") and _has_text_rendering_absorber(i):
            continue

        # Table cells are absorbed into their TableElement — don't emit them.
        if i in table_cell_indices:
            continue

        # The table container becomes a single TableElement.
        if i in table_container_to_group:
            classified.append(ClassifiedElement(
                index=i,
                elem_type="table",
                data=elem,
            ))
            continue

        # LI items are absorbed into their UL's BulletListElement.
        if i in bullet_item_indices:
            continue

        # The UL container becomes a single BulletListElement.
        if i in bullet_container_to_group:
            classified.append(ClassifiedElement(
                index=i,
                elem_type="bullet_list",
                data=elem,
            ))
            continue

        elem_type = _classify_single(elem, i, ctx)
        if elem_type is not None:
            classified.append(ClassifiedElement(
                index=i,
                elem_type=elem_type,
                data=elem,
            ))

    return classified


def _classify_single(elem: dict, idx: int, ctx: _ClassificationContext) -> Optional[str]:
    """Apply the rule chain to a single element. First match wins."""
    for rule in _RULES:
        result = rule(elem, idx, ctx)
        if result is not None:
            return result
    return None
