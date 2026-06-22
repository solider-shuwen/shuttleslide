"""
Style Mapper — CSS / HTML style values → PPTX property conversions.

Handles color parsing, font-size conversion, gradient mapping, etc.

Unit convention
---------------
Numeric fields and helper names throughout the html_to_pptx module follow
a strict unit-suffix convention so values can never silently leak across
conversions:

* ``_pct`` — percentage of the 1280×720 slide canvas (from JS extractor)
* ``_px``  — CSS pixels at 96 DPI (rare on the Python side; mostly used by
             the layouts helpers)
* ``_pt``  — typographic points (1 pt = 1/72 inch). All font-size, border
             width, paragraph spacing, and similar CSS-derived values are
             stored in points after the extractor multiplies px by 0.75.
* ``_emu`` — English Metric Units, the PPTX internal coordinate (9525 EMU
             per pixel, 12700 EMU per point).

Use ``px_to_emu`` for ``_px`` inputs and ``pt_to_emu`` for ``_pt`` inputs.
Mixing them produces ~25 % size errors (the px→pt factor is 0.75).
"""

import re
from typing import Optional, Tuple, List

from pptx.util import Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN


# ---------------------------------------------------------------------------
# Color parsing
# ---------------------------------------------------------------------------

def parse_hex_color(hex_str: str) -> Optional[Tuple[int, int, int, float]]:
    """
    Parse a CSS hex color string into (R, G, B, alpha).

    Supports: '#RGB', '#RRGGBB', '#RRGGBBAA', 'rgb(r,g,b)', 'rgba(r,g,b,a)'
    Returns None if unparseable.
    """
    if not hex_str:
        return None

    hex_str = hex_str.strip()

    # Try rgba(r, g, b, a) / rgb(r, g, b)
    m = re.match(r'rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)', hex_str)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        a = float(m.group(4)) if m.group(4) else 1.0
        return (r, g, b, a)

    # Remove leading #
    if hex_str.startswith('#'):
        hex_str = hex_str[1:]

    # #RGB → #RRGGBB
    if len(hex_str) == 3:
        hex_str = hex_str[0]*2 + hex_str[1]*2 + hex_str[2]*2

    if len(hex_str) == 6:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        return (r, g, b, 1.0)

    if len(hex_str) == 8:
        r = int(hex_str[0:2], 16)
        g = int(hex_str[2:4], 16)
        b = int(hex_str[4:6], 16)
        a = int(hex_str[6:8], 16) / 255.0
        return (r, g, b, a)

    return None


def hex_to_rgbcolor(hex_str: str) -> Optional[RGBColor]:
    """Convert a hex color string to a python-pptx RGBColor."""
    parsed = parse_hex_color(hex_str)
    if parsed is None:
        return None
    r, g, b, _ = parsed
    return RGBColor(r, g, b)


def color_opacity(hex_str: str) -> float:
    """Extract the alpha channel from a color string. Returns 1.0 if opaque."""
    parsed = parse_hex_color(hex_str)
    if parsed is None:
        return 1.0
    return parsed[3]


# ---------------------------------------------------------------------------
# Font size conversion
# ---------------------------------------------------------------------------

def px_to_pt(px: float) -> float:
    """Convert CSS pixels to typographic points (at 96 DPI).

    1 pt = 1/72 inch, 1 px = 1/96 inch
    px → pt: multiply by 72/96 = 0.75
    """
    return px * 0.75


def pt_to_emu(pt: float) -> int:
    """Convert points to EMU (1 pt = 12700 EMU)."""
    return int(pt * 12700)


# ---------------------------------------------------------------------------
# Alignment mapping
# ---------------------------------------------------------------------------

ALIGNMENT_MAP = {
    "left": PP_ALIGN.LEFT,
    "center": PP_ALIGN.CENTER,
    "right": PP_ALIGN.RIGHT,
    "justify": PP_ALIGN.JUSTIFY,
}


def map_alignment(align_str: str) -> PP_ALIGN:
    """Map a PPT-DSL alignment string to PP_ALIGN enum."""
    return ALIGNMENT_MAP.get(align_str, PP_ALIGN.LEFT)


# ---------------------------------------------------------------------------
# Gradient direction mapping
# ---------------------------------------------------------------------------

def gradient_angle_deg(direction: str) -> float:
    """Map a PPT-DSL gradient direction to a DrawingML angle in degrees.

    DrawingML `<a:lin ang="...">`: ang is in 1/60000 degree units, 0° means
    the first stop is on the left and the last on the right (i.e. colour
    flows left→right). Angles increase clockwise: 90° = top→bottom,
    180° = right→left, 270° = bottom→top.

    CSS `linear-gradient(Xdeg, ...)`: X is the direction the gradient
    flows toward, 0° = upward (bottom→top), 90° = rightward (left→right),
    180° = downward (top→bottom). Increases clockwise.

    Conversion: drawingml_deg = (css_deg - 90) mod 360.

    Supports two direction encodings:
    - ``css_<deg>`` (new): emitted by ``extract_layout.js::parseGradient``
      for any CSS angle, e.g. ``css_45``, ``css_180``. Default when no
      angle is present is ``css_90`` (= horizontal).
    - Legacy named directions (``horizontal``/``vertical``/``diagonal_45``/
      ``diagonal_135``): preserved for backward compatibility with older
      extracted JSON. The previous implementation had vertical and
      diagonal_135 swapped; this is fixed here.
    """
    if direction and direction.startswith("css_"):
        try:
            css_deg = float(direction[4:])
        except (TypeError, ValueError):
            return 0.0
        return (css_deg - 90.0) % 360.0
    # Legacy named-direction mapping (fixed).
    legacy = {
        "horizontal": 0.0,        # CSS 90°  → DrawingML 0°
        "vertical": 90.0,         # CSS 180° → DrawingML 90°  (was 270, bug)
        "diagonal_45": 315.0,     # CSS 45°  → DrawingML 315°
        "diagonal_135": 45.0,     # CSS 135° → DrawingML 45°  (was 225, bug)
    }
    return legacy.get(direction, 0.0)


# ---------------------------------------------------------------------------
# Transparency helpers
# ---------------------------------------------------------------------------

def opacity_to_transparency(opacity: float) -> int:
    """Convert 0.0-1.0 opacity to PPTX transparency percentage (0-100).

    PPTX transparency is the inverse: 0 = fully opaque, 100 = invisible.
    """
    return int((1.0 - max(0.0, min(1.0, opacity))) * 100000)


# ---------------------------------------------------------------------------
# Material Icon → Unicode mapping (common subset)
# ---------------------------------------------------------------------------

ICON_TO_UNICODE: dict = {
    "visibility": "👁",
    "visibility_off": "🚫",
    "school": "🎓",
    "cancel": "✕",
    "bug_report": "🐛",
    "check_circle": "✓",
    "error_outline": "⚠",
    "help_outline": "?",
    "medical_services": "🏥",
    "camera_alt": "📷",
    "warning": "⚠",
    "fact_check": "✓",
    "code": "</>",
    "cloud": "☁",
    "psychology": "🧠",
    "arrow_downward": "↓",
    "sentiment_dissatisfied": "😞",
    "auto_awesome": "✨",
}


def icon_to_unicode(icon_name: str) -> str:
    """Get a Unicode fallback for a Material Icon name."""
    return ICON_TO_UNICODE.get(icon_name, f"[{icon_name}]")
