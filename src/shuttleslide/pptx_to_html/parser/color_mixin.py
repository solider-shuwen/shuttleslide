"""
Color handling mixin for PPTXParser.

Provides color extraction and resolution methods used across all parsing mixins.
"""

import re
from typing import Optional

from shuttleslide.pptx_to_html.utils.colors import adjust_color_luminance, resolve_xml_color

# OpenXML scheme color names (e.g., "lt1", "dk1") to python-pptx MSO_THEME_COLOR enum names
_SCHEME_CLR_NAME_MAP = {
    'LT_1': 'LIGHT_1',
    'LT_2': 'LIGHT_2',
    'DK_1': 'DARK_1',
    'DK_2': 'DARK_2',
}


class ColorMixin:
    """Color extraction and resolution methods."""

    def _scheme_clr_to_color(self, val: str) -> Optional[str]:
        """
        Convert an OpenXML schemeClr name (e.g., 'accent1', 'lt1', 'dk2')
        to a hex color string using the theme color extractor.

        Args:
            val: schemeClr val attribute (e.g., 'accent1', 'lt1')

        Returns:
            Hex color string (e.g., '#FFFFFF') or None
        """
        if not val or not self.theme_color_extractor:
            return None
        theme_color_name = val.upper()
        theme_color_name = re.sub(r'([A-Z]+)(\d+)', r'\1_\2', theme_color_name)
        # Map short names (LT1 → LIGHT_1, DK1 → DARK_1, etc.)
        theme_color_name = _SCHEME_CLR_NAME_MAP.get(theme_color_name, theme_color_name)
        try:
            from pptx.enum.dml import MSO_THEME_COLOR
            theme_color_enum = getattr(MSO_THEME_COLOR, theme_color_name)
            theme_rgb = self.theme_color_extractor.get_theme_color(theme_color_enum)
            return theme_rgb
        except (AttributeError, ValueError):
            return None

    def _extract_run_color(self, font, run_xml=None) -> Optional[str]:
        """
        Extract color from a font object, handling RGB and theme colors.

        For theme colors, reads the underlying XML to apply modifiers like
        <a:shade>, <a:tint>, <a:lumMod>, <a:lumOff> that python-pptx ignores.

        Args:
            font: python-pptx Font object from a run
            run_xml: The run's XML element (<a:r>), used to access color modifiers

        Returns:
            Hex color string (e.g., '#FF0000') or None
        """
        if not font.color or font.color.type is None:
            return None

        if font.color.type == 1:  # MSO_COLOR_TYPE.RGB
            return f"#{font.color.rgb}"

        if font.color.type == 2:  # MSO_COLOR_TYPE.SCHEME (theme color)
            if not self.theme_color_extractor:
                return None

            base_color = self.theme_color_extractor.get_theme_color(
                int(font.color.theme_color)
            )
            if base_color is None:
                return None

            # Apply color modifiers from the run's XML element.
            # python-pptx loses these, so we read them directly from <a:rPr>.
            if run_xml is not None:
                try:
                    ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
                    # Find <a:rPr>/<a:solidFill>/<a:schemeClr>
                    rPr = run_xml.find('a:rPr', ns)
                    if rPr is not None:
                        solid_fill = rPr.find('a:solidFill', ns)
                        if solid_fill is not None:
                            scheme_clr = solid_fill.find('a:schemeClr', ns)
                            if scheme_clr is not None:
                                base_color = self._apply_color_modifiers(scheme_clr, base_color)
                except Exception:
                    pass

            return base_color

        return None

    def _apply_color_modifiers(self, color_elem, base_color: str) -> str:
        """
        Apply OpenXML color modifiers (shade, tint, lumMod, lumOff) to a color.

        Args:
            color_elem: XML element (typically <a:schemeClr>)
            base_color: Base hex color string

        Returns:
            Modified hex color string
        """
        ns = {'a': 'http://schemas.openxmlformats.org/drawingml/2006/main'}
        color = base_color

        # <a:shade val="50000"/> — darken toward black (val in 1/1000 percent)
        shade = color_elem.find('a:shade', ns)
        if shade is not None:
            factor = int(shade.get('val', '100000')) / 100000.0
            color = self._shade_color(color, factor)

        # <a:tint val="50000"/> — lighten toward white (val in 1/1000 percent)
        tint = color_elem.find('a:tint', ns)
        if tint is not None:
            factor = int(tint.get('val', '100000')) / 100000.0
            color = self._tint_color(color, factor)

        # <a:lumMod val="50000"/> — luminance modulation
        lum_mod = color_elem.find('a:lumMod', ns)
        if lum_mod is not None:
            factor = int(lum_mod.get('val', '100000')) / 100000.0
            color = self._adjust_color_luminance(color, factor)

        # <a:lumOff val="40000"/> — luminance offset (added after modulation)
        lum_off = color_elem.find('a:lumOff', ns)
        if lum_off is not None:
            offset = int(lum_off.get('val', '0')) / 100000.0
            color = self._lum_offset_color(color, offset)

        return color

    @staticmethod
    def _shade_color(hex_color: str, factor: float) -> str:
        """Shade a color toward black by the given factor (0=black, 1=unchanged)."""
        try:
            r = int(int(hex_color[1:3], 16) * factor)
            g = int(int(hex_color[3:5], 16) * factor)
            b = int(int(hex_color[5:7], 16) * factor)
            return f"#{max(0,min(255,r)):02x}{max(0,min(255,g)):02x}{max(0,min(255,b)):02x}"
        except (ValueError, IndexError):
            return hex_color

    @staticmethod
    def _tint_color(hex_color: str, factor: float) -> str:
        """Tint a color toward white by the given factor (0=white, 1=unchanged)."""
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            r = int(r + (255 - r) * (1 - factor))
            g = int(g + (255 - g) * (1 - factor))
            b = int(b + (255 - b) * (1 - factor))
            return f"#{max(0,min(255,r)):02x}{max(0,min(255,g)):02x}{max(0,min(255,b)):02x}"
        except (ValueError, IndexError):
            return hex_color

    @staticmethod
    def _lum_offset_color(hex_color: str, offset: float) -> str:
        """Add a luminance offset to a color (offset is 0-1 range)."""
        try:
            r = int(hex_color[1:3], 16)
            g = int(hex_color[3:5], 16)
            b = int(hex_color[5:7], 16)
            offset_px = int(offset * 255)
            r = max(0, min(255, r + offset_px))
            g = max(0, min(255, g + offset_px))
            b = max(0, min(255, b + offset_px))
            return f"#{r:02x}{g:02x}{b:02x}"
        except (ValueError, IndexError):
            return hex_color

    def _resolve_xml_color(self, color_elem) -> Optional[str]:
        """
        Resolve a DrawingML color element to a hex string.

        Delegates to utils.colors.resolve_xml_color with the theme extractor.
        """
        return resolve_xml_color(color_elem, self.theme_color_extractor)

    def _adjust_color_luminance(self, hex_color: str, factor: float) -> str:
        """Adjust color luminance by multiplying RGB values. Delegates to utils."""
        return adjust_color_luminance(hex_color, factor)
