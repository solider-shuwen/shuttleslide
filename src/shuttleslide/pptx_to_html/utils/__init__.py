"""
Utility functions for PPTX to HTML conversion.
"""

from shuttleslide.pptx_to_html.utils.text_sanitizer import (
    sanitize_pptx_text,
    has_special_chars,
    get_special_char_count,
    PPT_SPECIAL_CHARS,
)
from shuttleslide.pptx_to_html.utils.namespaces import NAMESPACES, NS_A, NS_A_CLARK, NS_P_CLARK, NS_R_CLARK
from shuttleslide.pptx_to_html.utils.units import (
    emu_to_px,
    emu_to_pt,
    emu_to_inches,
    px_to_emu,
    angle_to_degrees,
    EMU_PER_INCH,
    EMU_PER_POINT,
    EMU_PER_PIXEL,
    ANGLE_UNITS_PER_DEGREE,
)
from shuttleslide.pptx_to_html.utils.colors import adjust_color_luminance, resolve_xml_color

__all__ = [
    'sanitize_pptx_text',
    'has_special_chars',
    'get_special_char_count',
    'PPT_SPECIAL_CHARS',
    'NAMESPACES',
    'NS_A',
    'NS_A_CLARK',
    'NS_P_CLARK',
    'NS_R_CLARK',
    'emu_to_px',
    'emu_to_pt',
    'emu_to_inches',
    'px_to_emu',
    'angle_to_degrees',
    'EMU_PER_INCH',
    'EMU_PER_POINT',
    'EMU_PER_PIXEL',
    'ANGLE_UNITS_PER_DEGREE',
    'adjust_color_luminance',
    'resolve_xml_color',
]
