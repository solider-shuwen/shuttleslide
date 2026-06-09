"""
Color handling mixin for PPTXParser.

Provides color extraction and resolution methods used across all parsing mixins.
"""

from typing import Optional

from shuttleslide.pptx_to_html.utils.colors import adjust_color_luminance, resolve_xml_color


class ColorMixin:
    """Color extraction and resolution methods."""

    def _extract_run_color(self, font) -> Optional[str]:
        """
        Extract color from a font object, handling RGB and theme colors.

        Args:
            font: python-pptx Font object from a run

        Returns:
            Hex color string (e.g., '#FF0000') or None
        """
        if not font.color or font.color.type is None:
            return None

        if font.color.type == 1:  # MSO_COLOR_TYPE.RGB
            return f"#{font.color.rgb}"

        if font.color.type == 2:  # MSO_COLOR_TYPE.SCHEME (theme color)
            if self.theme_color_extractor:
                return self.theme_color_extractor.get_theme_color(
                    int(font.color.theme_color)
                )

        return None

    def _resolve_xml_color(self, color_elem) -> Optional[str]:
        """
        Resolve a DrawingML color element to a hex string.

        Delegates to utils.colors.resolve_xml_color with the theme extractor.
        """
        return resolve_xml_color(color_elem, self.theme_color_extractor)

    def _adjust_color_luminance(self, hex_color: str, factor: float) -> str:
        """Adjust color luminance by multiplying RGB values. Delegates to utils."""
        return adjust_color_luminance(hex_color, factor)
