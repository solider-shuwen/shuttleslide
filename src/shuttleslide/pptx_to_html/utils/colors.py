"""
Color utilities for PPTX to HTML conversion.

Handles color parsing, formatting, and luminance adjustments.
"""

from typing import Optional

from shuttleslide.pptx_to_html.utils.namespaces import NAMESPACES, NS_A


def adjust_color_luminance(hex_color: str, factor: float) -> str:
    """Adjust color luminance by multiplying RGB values.

    Args:
        hex_color: Color as hex string (e.g., '#FF0000')
        factor: Luminance factor. < 1.0 darkens, > 1.0 lightens.

    Returns:
        Adjusted hex color string
    """
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
        if factor <= 1.0:
            r = int(r * factor)
            g = int(g * factor)
            b = int(b * factor)
        else:
            f = factor - 1.0
            r = int(r + (255 - r) * f)
            g = int(g + (255 - g) * f)
            b = int(b + (255 - b) * f)
        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))
        return f"#{r:02x}{g:02x}{b:02x}"
    except (ValueError, IndexError):
        return hex_color


def resolve_xml_color(color_elem, theme_extractor=None) -> Optional[str]:
    """Resolve a DrawingML color element to a hex string.

    Handles <a:srgbClr val="RRGGBB"> and <a:schemeClr val="bg1"> etc.
    Also applies color modifiers like <a:lumMod val="75000"/>.

    Args:
        color_elem: XML element containing color definition
        theme_extractor: ThemeColorExtractor instance for resolving theme colors

    Returns:
        Hex color string (e.g., '#FF0000') or None
    """
    if color_elem is None:
        return None

    ns = NAMESPACES
    color = None
    inner_elem = None  # The actual color element (srgbClr or schemeClr)

    # Check for srgbClr (direct RGB)
    srgb = color_elem.find('a:srgbClr', ns)
    if srgb is not None:
        val = srgb.get('val')
        if val:
            color = f"#{val}"
            inner_elem = srgb

    # Check for schemeClr (theme color reference)
    if color is None:
        scheme = color_elem.find('a:schemeClr', ns)
        if scheme is None:
            scheme = color_elem  # Maybe the element itself is a schemeClr

        if scheme is not None and scheme.tag.endswith('}schemeClr'):
            val = scheme.get('val')
            if val and theme_extractor:
                color = theme_extractor.get_theme_color(val)
                inner_elem = scheme

    # Apply color modifiers (lumMod, etc.)
    if color and inner_elem is not None:
        lum_mod = inner_elem.find('a:lumMod', ns)
        if lum_mod is not None:
            factor = int(lum_mod.get('val', '100000')) / 100000.0
            color = adjust_color_luminance(color, factor)

    return color
