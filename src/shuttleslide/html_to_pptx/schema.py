"""
PPT-DSL Schema — structured JSON format for HTML-to-PPTX conversion.

This module defines the intermediate data model that bridges LLM-extracted
HTML content and the PPTX renderer.  The LLM transformer outputs JSON
conforming to these dataclasses; the renderer consumes them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional, Union


# ---------------------------------------------------------------------------
# Position helpers
# ---------------------------------------------------------------------------

@dataclass
class PositionPercent:
    """Element position as percentages of the slide dimensions."""
    x_pct: float = 0.0
    y_pct: float = 0.0
    w_pct: float = 0.0
    h_pct: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass
class GradientStop:
    """A single colour stop in a gradient."""
    color: str          # '#RRGGBB' or '#RRGGBBAA'
    position: float = 0.0   # 0.0 – 1.0
    opacity: float = 1.0    # 0.0 – 1.0


@dataclass
class GradientDef:
    """Gradient fill definition."""
    direction: str = "horizontal"  # 'horizontal', 'vertical', 'diagonal_135'
    stops: List[GradientStop] = field(default_factory=list)


@dataclass
class BackgroundDef:
    """Slide or element background."""
    type: str = "solid"  # 'solid', 'gradient', 'image', 'none'
    color: Optional[str] = None
    gradient: Optional[GradientDef] = None
    image_url: Optional[str] = None


@dataclass
class BorderDef:
    """Border / outline definition."""
    color: str = "#000000"
    width_pt: float = 0.0
    style: str = "solid"   # 'solid', 'dashed', 'dotted'


@dataclass
class ShadowDef:
    """Drop-shadow definition."""
    color: str = "#00000044"
    blur_pt: float = 4.0
    offset_x_pt: float = 2.0
    offset_y_pt: float = 2.0


# ---------------------------------------------------------------------------
# Text content
# ---------------------------------------------------------------------------

@dataclass
class TextRun:
    """A single styled run of text."""
    text: str = ""
    font_size_pt: Optional[float] = None
    color: Optional[str] = None
    bold: bool = False
    italic: bool = False
    font_name: Optional[str] = None
    opacity: float = 1.0           # 0.0-1.0; <1.0 means semi-transparent text


@dataclass
class TextBlock:
    """A paragraph-level block of text (may contain multiple styled runs)."""
    text: str = ""
    level: str = "body"       # 'title', 'subtitle', 'h3', 'h4', 'body', 'caption'
    runs: List[TextRun] = field(default_factory=list)
    alignment: str = "left"   # 'left', 'center', 'right'
    line_spacing_pt: Optional[float] = None
    spacing_before_pt: float = 0.0
    spacing_after_pt: float = 0.0


# ---------------------------------------------------------------------------
# Slide elements — the atomic building blocks
# ---------------------------------------------------------------------------

@dataclass
class SlideElementDSL:
    """Base for all elements in a slide."""
    type: str = "text_box"  # discriminator
    position: Optional[PositionPercent] = None
    z_order: int = 0


@dataclass
class TitleBarElement(SlideElementDSL):
    """Full-width gradient/solid title bar at top of slide."""
    type: str = "title_bar"
    text: str = ""
    font_size_pt: float = 30.0
    font_color: str = "#FFFFFF"
    font_bold: bool = True
    font_name: Optional[str] = None
    height_pct: float = 11.8  # ~85px / 720px
    background: Optional[BackgroundDef] = None


@dataclass
class TextBoxElement(SlideElementDSL):
    """A free-form text box with one or more paragraphs."""
    type: str = "text_box"
    content: List[TextBlock] = field(default_factory=list)
    vertical_align: str = "top"  # 'top', 'middle', 'bottom'
    no_wrap: bool = False  # Atomic-token overflow (URLs, decorative
                           # numbers, product codes): text glyph wider
                           # than container in browser. Browser shows
                           # visual overflow; PPT matches via
                           # tf.word_wrap=False so text overflows the
                           # textbox instead of wrapping.


@dataclass
class ImageElement(SlideElementDSL):
    """An image (from URL or embedded bytes)."""
    type: str = "image"
    url: Optional[str] = None
    alt_text: str = ""
    border: Optional[BorderDef] = None
    corner_radius_pct: float = 0.0  # 0.0 – 0.5
    shadow: Optional[ShadowDef] = None
    object_fit: str = "fill"  # 'fill', 'cover', 'contain'


@dataclass
class ShapeElement(SlideElementDSL):
    """A geometric shape (rect, rounded_rect, oval, etc.)."""
    type: str = "shape"
    shape_type: str = "rectangle"  # 'rectangle', 'rounded_rect', 'oval', 'circle'
    background: Optional[BackgroundDef] = None
    border: Optional[BorderDef] = None
    corner_radius_pct: float = 0.0
    shadow: Optional[ShadowDef] = None
    rotation_deg: float = 0.0


@dataclass
class GradientOverlayElement(SlideElementDSL):
    """Semi-transparent gradient overlay (e.g. over a background image)."""
    type: str = "gradient_overlay"
    gradient: Optional[GradientDef] = None
    opacity: float = 0.85


@dataclass
class BlurGlowElement(SlideElementDSL):
    """Decorative blur/glow circle (simulated via oval + soft edge)."""
    type: str = "blur_glow"
    color: str = "#133EFF"
    opacity: float = 0.3
    blur_radius_pt: float = 0.0  # CSS blur radius converted to pt


@dataclass
class IconTextElement(SlideElementDSL):
    """An icon + text combination (from Material Icons, etc.)."""
    type: str = "icon_text"
    icon_name: str = ""       # e.g. "visibility"
    icon_symbol: Optional[str] = None  # Unicode fallback, e.g. "👁"
    icon_size_pt: float = 28.0
    icon_color: Optional[str] = None
    icon_shadow: Optional[ShadowDef] = None  # from filter: drop-shadow on the icon
    text: str = ""
    text_font_size_pt: float = 22.0
    text_color: Optional[str] = None
    text_bold: bool = False
    text_font_name: Optional[str] = None  # from HTML computed style
    icon_font: Optional[str] = None  # icon font class key, e.g. 'material-icons'
    layout: str = "horizontal"  # 'horizontal' | 'vertical'


@dataclass
class CardElement(SlideElementDSL):
    """A card (rounded rectangle with optional per-side accent borders).

    `border` is the uniform border (CSS `border: 1px solid …`). The
    `border_left/right/top/bottom` fields model single-side borders that
    CSS authors commonly write as e.g. `border-left: 4px solid #f00`.
    Rendered as thin rectangles on the relevant edge.
    """
    type: str = "card"
    background: Optional[BackgroundDef] = None
    border: Optional[BorderDef] = None
    border_left: Optional[BorderDef] = None
    border_right: Optional[BorderDef] = None
    border_top: Optional[BorderDef] = None
    border_bottom: Optional[BorderDef] = None
    corner_radius_pct: float = 0.02
    padding_pct: float = 2.0
    shadow: Optional[ShadowDef] = None
    opacity: float = 1.0
    children: List[Any] = field(default_factory=list)  # nested elements


@dataclass
class NumberedStepElement(SlideElementDSL):
    """A numbered step in a process flow."""
    type: str = "numbered_step"
    step_number: int = 1
    number_bg_color: str = "#133EFF"
    number_text_color: str = "#FFFFFF"
    title: str = ""
    title_color: Optional[str] = None
    description: str = ""
    description_color: Optional[str] = None
    icon_name: Optional[str] = None
    show_arrow: bool = False
    arrow_color: Optional[str] = None


@dataclass
class DividerLineElement(SlideElementDSL):
    """A thin horizontal divider line."""
    type: str = "divider_line"
    color: str = "#00CD82"
    height_pt: float = 2.0


@dataclass
class BadgeElement(SlideElementDSL):
    """A pill/badge shape with text (e.g. floating label under an image)."""
    type: str = "badge"
    text: str = ""
    background: Optional[BackgroundDef] = None
    border: Optional[BorderDef] = None
    font_size_pt: float = 14.0
    font_color: str = "#FFFFFF"
    corner_radius_pct: float = 0.5   # fully rounded pill
    shadow: Optional[ShadowDef] = None
    opacity: float = 1.0
    # Optional icon embedded in the badge
    icon_name: Optional[str] = None
    icon_font: Optional[str] = None
    icon_color: Optional[str] = None
    icon_size_pt: Optional[float] = None


@dataclass
class BulletItem:
    """One item of a BulletListElement.

    Carries the item text plus an optional inline Material Icon
    (rendered in the bullet margin). Mirrors the icon fields on
    IconTextElement so the renderer can reuse _render_vector_icon_primitives.
    """
    text: str = ""
    icon_name: Optional[str] = None
    icon_font: Optional[str] = None     # e.g. 'material-icons'
    icon_color: Optional[str] = None
    icon_size_pt: Optional[float] = None


@dataclass
class BulletListElement(SlideElementDSL):
    """A list of bullet points."""
    type: str = "bullet_list"
    items: List[BulletItem] = field(default_factory=list)
    bullet_color: Optional[str] = None
    font_size_pt: float = 22.0
    font_color: Optional[str] = None
    spacing_pt: float = 8.0


@dataclass
class TableCell:
    """A single cell in a table. Text is carried as styled runs."""
    text: str = ""
    runs: List[TextRun] = field(default_factory=list)
    background: Optional[BackgroundDef] = None   # row tint (e.g. header bg)
    alignment: str = "left"                       # 'left', 'center', 'right'


@dataclass
class TableElement(SlideElementDSL):
    """A native PPTX table — a grid of styled cells.

    Built by spatial grid detection from either real ``<table>`` markup or
    div+flex+span "div-table" layouts. Renders via python-pptx ``add_table``.
    """
    type: str = "table"
    rows: List[List[TableCell]] = field(default_factory=list)  # rows[row][col]
    col_widths_pct: List[float] = field(default_factory=list)  # per-column widths (% of slide)
    row_heights_pct: List[float] = field(default_factory=list) # per-row heights (% of slide)
    header_row: bool = False                     # row 0 is a header
    border: Optional[BorderDef] = None           # table outline (from container)
    row_separator: Optional[BorderDef] = None    # horizontal lines between rows


@dataclass
class SVGElement(SlideElementDSL):
    """An inline ``<svg data-slot="...">`` element — converted to native
    editable DrawingML shapes via the vendored svg_to_pptx library.

    ``svg_markup`` is the authoritative payload (contains ``<defs>``,
    ``<use>``, gradients, etc.). ``viewBox`` and ``slot_id`` are cached
    for fast access during rendering without re-parsing the markup.

    ``opacity`` is the *cumulative* CSS opacity of every DOM ancestor
    of the ``<svg>`` (e.g. a wrapping ``<div style="opacity:0.25">``),
    multiplied with the SVG's own opacity attribute. The renderer hands
    this to the vendored library as ``inherited_styles={'opacity': …}``
    so every shape inside the SVG multiplies its fill/stroke alpha by
    this factor — matching the browser's effective opacity.
    """
    type: str = "svg"
    svg_markup: str = ""          # raw <svg>...</svg> outerHTML
    slot_id: str = ""             # data-slot attribute (debugging)
    viewBox: Optional[str] = None # "0 0 W H" cached
    opacity: float = 1.0          # cumulative ancestor opacity (1.0 = opaque)
    # object-fit semantics carried over from the originating placeholder
    # <img> by inline_svg_placeholders → extract_layout.js → _to_svg.
    # Mirrors ImageElement.object_fit. Renderer picks uniform scale +
    # center (cover/contain) vs. independent-axis stretch (fill).
    object_fit: str = "fill"  # 'fill', 'cover', 'contain'


# Union of all element types for deserialization
ELEMENT_TYPES = {
    "title_bar": TitleBarElement,
    "text_box": TextBoxElement,
    "image": ImageElement,
    "shape": ShapeElement,
    "gradient_overlay": GradientOverlayElement,
    "blur_glow": BlurGlowElement,
    "icon_text": IconTextElement,
    "card": CardElement,
    "numbered_step": NumberedStepElement,
    "divider_line": DividerLineElement,
    "badge": BadgeElement,
    "bullet_list": BulletListElement,
    "table": TableElement,
    "svg": SVGElement,
}


# ---------------------------------------------------------------------------
# Theme & Slide
# ---------------------------------------------------------------------------

@dataclass
class ThemeDef:
    """Global theme extracted from the HTML.

    Fields align with the ``define_theme`` tool and ``THEME_DESIGNER_PROMPT``
    so nothing the LLM emits is silently dropped when ``state.theme`` (a
    plain dict) is coerced into this dataclass for rendering. The slide
    HTML references these via ``{{theme.<field>}}`` placeholders (see
    ``agent/theme_tokens.py``); keeping the field list in sync is what
    makes the placeholders resolve at render time.
    """
    primary_color: str = "#133EFF"
    accent_color: str = "#00CD82"
    warn_color: str = "#FF5722"
    bg_color: str = "#FEFEFE"
    text_color: str = "#1F2937"
    title_color: str = "#133EFF"
    font_title: str = "Roboto"
    font_body: str = "Roboto"


@dataclass
class SlideDSL:
    """A single slide definition."""
    layout: str = "free_form"  # layout preset ID (always free_form in the agent flow)
    background: Optional[BackgroundDef] = None
    elements: List[Any] = field(default_factory=list)  # instances of *Element
    # Slot-based content for the agent pipeline. The free-form pipeline
    # stores the authored HTML under slots["html"]; the renderer wraps it
    # in the .ppt-slide container. The `elements` list above is the legacy
    # model used by html_to_pptx's own pipeline (which builds its own
    # SlideDSL from HTML); the agent flow only uses `slots`.
    slots: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PresentationDSL:
    """Top-level PPT-DSL document."""
    theme: ThemeDef = field(default_factory=ThemeDef)
    slides: List[SlideDSL] = field(default_factory=list)
    slide_width_emu: int = 12192000   # 1280px at 96dpi ≈ 13.33in
    slide_height_emu: int = 6858000   # 720px at 96dpi ≈ 7.5in


# ---------------------------------------------------------------------------
# Serialization / Deserialization
# ---------------------------------------------------------------------------

def _dict_to_position(d) -> Optional[PositionPercent]:
    if d is None or not isinstance(d, dict):
        return None
    return PositionPercent(
        x_pct=float(d.get("x_pct", 0)),
        y_pct=float(d.get("y_pct", 0)),
        w_pct=float(d.get("w_pct", 0)),
        h_pct=float(d.get("h_pct", 0)),
    )


def _dict_to_gradient(d) -> Optional[GradientDef]:
    if d is None or not isinstance(d, dict):
        return None
    return GradientDef(
        direction=d.get("direction", "horizontal"),
        stops=[GradientStop(**gs) for gs in d.get("stops", [])],
    )


def _dict_to_background(d) -> Optional[BackgroundDef]:
    if d is None or not isinstance(d, dict):
        return None
    return BackgroundDef(
        type=d.get("type", "solid"),
        color=d.get("color"),
        gradient=_dict_to_gradient(d.get("gradient")),
        image_url=d.get("image_url"),
    )


def _dict_to_border(d) -> Optional[BorderDef]:
    if d is None or not isinstance(d, dict):
        return None
    return BorderDef(
        color=d.get("color", "#000000"),
        width_pt=float(d.get("width_pt", 0)),
        style=d.get("style", "solid"),
    )


def _dict_to_shadow(d) -> Optional[ShadowDef]:
    if d is None or not isinstance(d, dict):
        return None
    return ShadowDef(
        color=d.get("color", "#00000044"),
        blur_pt=float(d.get("blur_pt", 4.0)),
        offset_x_pt=float(d.get("offset_x_pt", 2.0)),
        offset_y_pt=float(d.get("offset_y_pt", 2.0)),
    )


def _dict_to_runs(runs_data) -> List[TextRun]:
    if not isinstance(runs_data, list):
        return []
    return [
        TextRun(
            text=r.get("text", "") if isinstance(r, dict) else str(r),
            font_size_pt=r.get("font_size_pt") if isinstance(r, dict) else None,
            color=r.get("color") if isinstance(r, dict) else None,
            bold=r.get("bold", False) if isinstance(r, dict) else False,
            italic=r.get("italic", False) if isinstance(r, dict) else False,
            font_name=r.get("font_name") if isinstance(r, dict) else None,
        )
        for r in runs_data
    ]


def _dict_to_blocks(blocks_data) -> List[TextBlock]:
    if not isinstance(blocks_data, list):
        return []
    result = []
    for b in blocks_data:
        if not isinstance(b, dict):
            continue
        result.append(TextBlock(
            text=b.get("text", ""),
            level=b.get("level", "body"),
            runs=_dict_to_runs(b.get("runs", [])),
            alignment=b.get("alignment", "left"),
            line_spacing_pt=b.get("line_spacing_pt"),
            spacing_before_pt=float(b.get("spacing_before_pt", 0)),
            spacing_after_pt=float(b.get("spacing_after_pt", 0)),
        ))
    return result


def _dict_to_element(e: dict):
    """Convert a raw dict to the appropriate element dataclass with nested conversion."""
    elem_type = e.get("type", "text_box")
    cls = ELEMENT_TYPES.get(elem_type, TextBoxElement)
    # Start with raw filtered kwargs
    kwargs = {k: v for k, v in e.items() if k in cls.__dataclass_fields__}
    # Convert known nested structures
    if "position" in kwargs and isinstance(kwargs["position"], dict):
        kwargs["position"] = _dict_to_position(kwargs["position"])
    if "background" in kwargs and isinstance(kwargs["background"], dict):
        kwargs["background"] = _dict_to_background(kwargs["background"])
    if "border" in kwargs and isinstance(kwargs["border"], dict):
        kwargs["border"] = _dict_to_border(kwargs["border"])
    if "border_left" in kwargs and isinstance(kwargs["border_left"], dict):
        kwargs["border_left"] = _dict_to_border(kwargs["border_left"])
    if "border_right" in kwargs and isinstance(kwargs["border_right"], dict):
        kwargs["border_right"] = _dict_to_border(kwargs["border_right"])
    if "border_top" in kwargs and isinstance(kwargs["border_top"], dict):
        kwargs["border_top"] = _dict_to_border(kwargs["border_top"])
    if "border_bottom" in kwargs and isinstance(kwargs["border_bottom"], dict):
        kwargs["border_bottom"] = _dict_to_border(kwargs["border_bottom"])
    if "gradient" in kwargs and isinstance(kwargs["gradient"], dict):
        kwargs["gradient"] = _dict_to_gradient(kwargs["gradient"])
    if "shadow" in kwargs and isinstance(kwargs["shadow"], dict):
        kwargs["shadow"] = _dict_to_shadow(kwargs["shadow"])
    if "content" in kwargs and isinstance(kwargs["content"], list):
        kwargs["content"] = _dict_to_blocks(kwargs["content"])
    if "children" in kwargs and isinstance(kwargs["children"], list):
        kwargs["children"] = [_dict_to_element(c) for c in kwargs["children"] if isinstance(c, dict)]
    # Note: we deliberately do NOT fill missing fields with default values here.
    # The dataclass __init__ already applies `default` and `default_factory()`
    # for any field absent from kwargs — doing it manually (as the previous
    # code did) overrode factory fields with None, breaking iteration.
    return cls(**kwargs)


def load_presentation(data: Union[str, Dict]) -> PresentationDSL:
    """Load a PresentationDSL from a clean JSON string or dict.

    Expects well-structured data (e.g. from a hand-written JSON file or
    a previously dumped PresentationDSL).
    """
    if isinstance(data, str):
        data = json.loads(data)

    theme_data = data.get("theme", {})
    theme = ThemeDef(**{k: v for k, v in theme_data.items() if k in ThemeDef.__dataclass_fields__})

    slides = []
    for s in data.get("slides", []):
        # Simple background parse (clean data only)
        bg_data = s.get("background")
        bg = None
        if bg_data and isinstance(bg_data, dict):
            grad = None
            if bg_data.get("gradient") and isinstance(bg_data["gradient"], dict):
                gd = bg_data["gradient"]
                grad = GradientDef(
                    direction=gd.get("direction", "horizontal"),
                    stops=[GradientStop(**gs) for gs in gd.get("stops", [])],
                )
            bg = BackgroundDef(
                type=bg_data.get("type", "solid"),
                color=bg_data.get("color"),
                gradient=grad,
                image_url=bg_data.get("image_url"),
            )

        # Element parse with recursive nested conversion
        elements = []
        for e in s.get("elements", []):
            if not isinstance(e, dict):
                continue
            elements.append(_dict_to_element(e))

        slides.append(SlideDSL(
            layout=s.get("layout", "title_bar_two_col"),
            background=bg,
            elements=elements,
        ))

    return PresentationDSL(
        theme=theme,
        slides=slides,
        slide_width_emu=data.get("slide_width_emu", 12192000),
        slide_height_emu=data.get("slide_height_emu", 6858000),
    )


def dump_presentation(pres: PresentationDSL) -> Dict:
    """Serialize a PresentationDSL to a JSON-friendly dict."""

    def _to_dict(obj):
        if isinstance(obj, (str, int, float, bool)) or obj is None:
            return obj
        if isinstance(obj, list):
            return [_to_dict(v) for v in obj]
        if hasattr(obj, "__dataclass_fields__"):
            result = {}
            for k, v in asdict(obj).items():
                if v is not None:
                    result[k] = _to_dict(v)
            return result
        return obj

    return _to_dict(pres)
