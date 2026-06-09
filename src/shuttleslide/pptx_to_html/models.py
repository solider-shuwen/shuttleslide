"""
Data models for PPTX to HTML conversion.

Contains all dataclass definitions used to represent parsed slide elements.
These models decouple converters and layouts from the parser implementation.
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field


def calculate_position_percentages(element: 'SlideElement', slide_width: float, slide_height: float) -> Dict[str, float]:
    """
    Calculate element position as percentages of slide dimensions.

    Args:
        element: SlideElement with position and size data
        slide_width: Width of the slide in pixels
        slide_height: Height of the slide in pixels

    Returns:
        Dictionary with percentage values for left, top, width, height
    """
    if slide_width > 0 and slide_height > 0:
        return {
            "left_pct": (element.left / slide_width * 100),
            "top_pct": (element.top / slide_height * 100),
            "width_pct": (element.width / slide_width * 100),
            "height_pct": (element.height / slide_height * 100),
        }
    return {
        "left_pct": 0.0,
        "top_pct": 0.0,
        "width_pct": 0.0,
        "height_pct": 0.0,
    }


@dataclass
class SlideElement:
    """Base class for slide elements."""
    element_type: str
    left: float
    top: float
    width: float
    height: float
    z_order: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RunElement:
    """Represents a single text run with its own formatting."""
    text: str = ""
    bold: Optional[bool] = None       # None = inherited from paragraph
    italic: Optional[bool] = None     # None = inherited from paragraph
    font_name: Optional[str] = None   # None = inherited
    font_size: Optional[float] = None # None = inherited
    color: Optional[str] = None       # '#RRGGBB' or None (inherited)


@dataclass
class BulletProperties:
    """Bullet properties for a paragraph parsed from OpenXML."""
    type: str = 'none'                    # 'none' | 'char' | 'autonum' | 'blip' | 'inherited'
    char: Optional[str] = None            # buChar character (e.g., '\u2022')
    autonum_type: Optional[str] = None    # e.g., 'arabicPeriod', 'alphaLcParenR'
    autonum_start: Optional[int] = None   # Start number for auto-numbered bullets
    font_typeface: Optional[str] = None   # Bullet font from <a:buFont>
    font_size_pct: Optional[int] = None   # Bullet size as percentage (100000 = 100%)
    color: Optional[str] = None           # Bullet color as '#RRGGBB'
    blip_image_bytes: Optional[bytes] = None   # Raw image bytes for buBlip bullets
    blip_image_type: Optional[str] = None      # Image type: 'png', 'jpg', etc.


@dataclass
class ParagraphElement:
    """Paragraph element for multi-level text structure."""
    text: str = ""
    level: int = 0  # PowerPoint paragraph level (0-8)
    alignment: Optional[str] = None
    font_name: Optional[str] = None
    font_size: Optional[float] = None
    bold: bool = False
    italic: bool = False
    color: Optional[str] = None
    # Spacing properties
    line_spacing: Optional[float] = None        # Multiplier (1.5 = 150%)
    line_spacing_pts: Optional[float] = None    # Fixed points
    spacing_before: Optional[float] = None      # Points
    spacing_after: Optional[float] = None       # Points
    # Indent properties
    margin_left: Optional[float] = None         # Left margin in pt (from marL, EMU)
    indent: Optional[float] = None              # First line indent in pt (negative = hanging)
    # Bullet properties
    bullet: Optional[BulletProperties] = None
    # Run-level content
    runs: List[RunElement] = field(default_factory=list)

    @property
    def has_bullet(self) -> bool:
        """Whether this paragraph should render as a bullet point."""
        return self.bullet is not None and self.bullet.type not in ('none',)


@dataclass
class TextElement(SlideElement):
    """Text element from a slide."""
    text: str = ""  # Keep for backward compatibility
    paragraphs: List[ParagraphElement] = field(default_factory=list)  # New paragraph structure
    font_name: Optional[str] = None
    font_size: Optional[float] = None
    bold: bool = False
    italic: bool = False
    color: Optional[str] = None
    is_title: bool = False
    level: int = 0
    # Rotation and transform properties
    rotation: Optional[float] = None  # Rotation angle in degrees
    vert: Optional[str] = None      # Vertical text (eaVert, mongolianVert, etc.)
    flip_h: bool = False           # Horizontal flip
    flip_v: bool = False           # Vertical flip
    vertical_align: Optional[str] = None  # Vertical alignment: 'top', 'middle', 'bottom'
    # Border/outline properties
    line_color: Optional[str] = None  # Outline color
    line_width: Optional[float] = None  # Outline width in pixels


@dataclass
class TableElement(SlideElement):
    """Table element from a slide."""
    rows: int = 0
    cols: int = 0
    data: List[List[str]] = field(default_factory=list)
    cell_styles: List[List[Dict[str, Any]]] = field(default_factory=list)


@dataclass
class ImageElement(SlideElement):
    """Image element from a slide."""
    image_bytes: bytes = b""
    image_type: str = ""
    alt_text: str = ""
    # Crop rectangle (OpenXML percentage values in 1/100000ths): {'l', 't', 'r', 'b'}
    src_rect: Optional[Dict[str, int]] = None
    # Color change effect: {'from': '#RRGGBB', 'to': 'transparent'}
    clr_change: Optional[Dict[str, str]] = None
    # Image fill mode from <a:blipFill>: "stretch" (default), "tile", or "none"
    fill_mode: str = "stretch"
    # PPT image scale (shape_EMU / cropped_img_EMU) — only meaningful with scene3d
    scale_w: Optional[float] = None
    scale_h: Optional[float] = None


@dataclass
class ShapeElement(SlideElement):
    """Shape element from a slide."""
    shape_type: str = ""
    fill_color: Optional[str] = None
    line_color: Optional[str] = None
    dash_style: Optional[str] = None  # PPTX preset dash: solid, dash, dashDot, lgDash, lgDashDot, etc.
    text: Optional[str] = None
    blip_fill: Optional[Dict[str, Any]] = None  # Image fill data: {image_bytes, image_type}
    flip_h: bool = False  # Horizontal flip
    flip_v: bool = False  # Vertical flip
    rotation: Optional[float] = None  # Rotation angle in degrees


@dataclass
class GroupElement(SlideElement):
    """Group shape containing child elements with coordinate transformation."""
    children: List[Any] = field(default_factory=list)  # List of SlideElement subclasses


@dataclass
class SlideBackground:
    """Resolved background for a slide (after inheritance chain)."""
    bg_type: str  # 'solid', 'gradient', 'image', 'none'
    color: Optional[str] = None  # '#RRGGBB' for solid fill
    gradient_css: Optional[str] = None  # CSS linear-gradient(...) string
    image_data: Optional[Dict[str, Any]] = None  # {image_bytes, image_type} for image bg
    overlay_color: Optional[str] = None  # '#RRGGBB' for semi-transparent overlay
    overlay_opacity: Optional[float] = None  # 0.0-1.0 opacity of the overlay


@dataclass
class MasterTextStyle:
    """Default text style from slide master for a given paragraph level."""
    font_name: Optional[str] = None
    font_size: Optional[float] = None  # In points
    color: Optional[str] = None  # '#RRGGBB'
    bold: Optional[bool] = None
    italic: Optional[bool] = None
    # Bullet defaults from master level style
    bullet_type: Optional[str] = None       # 'char' | 'autonum' | None
    bullet_char: Optional[str] = None       # Character for buChar bullets
    bullet_autonum_type: Optional[str] = None  # Auto-numbering scheme
    bullet_font: Optional[str] = None       # typeface from <a:buFont>


@dataclass
class ParsedSlide:
    """Represents a parsed slide with all its elements."""
    slide_number: int
    layout_name: str
    width: float
    height: float
    elements: List[SlideElement] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    hidden: bool = False
    has_animations: bool = False
    background: Optional[SlideBackground] = None
