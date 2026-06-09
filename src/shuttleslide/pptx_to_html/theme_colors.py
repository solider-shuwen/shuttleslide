"""
Theme color extractor for PPTX files
"""
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, Optional

from shuttleslide.pptx_to_html.utils.namespaces import NS_A

class ThemeColorExtractor:
    """
    Extract theme colors from PPTX theme XML files.
    """
    def __init__(self, pptx_path: str):
        """
        Initialize with PPTX file path.

        Args:
            pptx_path: Path to PPTX file
        """
        self.pptx_path = pptx_path
        self.theme_colors = {}
        self._extract_theme_colors()

    def _extract_theme_colors(self):
        """Extract theme colors from PPTX ZIP file.

        Only reads ppt/theme/theme1.xml (the slide master theme).
        Other theme files (theme2.xml etc.) belong to notes/handout masters
        and typically contain default Office colors, not the presentation's
        custom theme. Reading them would overwrite correct custom colors.
        """
        try:
            with zipfile.ZipFile(self.pptx_path, 'r') as zip_ref:
                # Use only the primary theme (slide master theme)
                primary_theme = 'ppt/theme/theme1.xml'
                all_files = zip_ref.namelist()

                if primary_theme not in all_files:
                    # Fallback: find any theme XML in ppt/theme/
                    theme_files = [f for f in all_files
                                   if f.startswith('ppt/theme/') and f.endswith('.xml')]
                    if not theme_files:
                        return
                    primary_theme = theme_files[0]

                try:
                    theme_content = zip_ref.read(primary_theme)
                    theme_str = theme_content.decode('utf-8', errors='ignore')
                    root = ET.fromstring(theme_str)
                    ns = NS_A
                    clr_schemes = root.findall('.//a:clrScheme', ns)
                    for clr_scheme in clr_schemes:
                        self._parse_color_scheme(clr_scheme, ns)
                except Exception as e:
                    print(f"Warning: Could not parse theme file {primary_theme}: {e}")

        except Exception as e:
            print(f"Warning: Could not extract theme colors: {e}")

    def _parse_color_scheme(self, clr_scheme, ns):
        """Parse color scheme element."""
        # Map theme color names to indices
        color_map = {
            'dk1': 0,    'lt1': 1,    'accent1': 2,  'accent2': 3,  'accent3': 4,
            'accent4': 5, 'accent5': 6,  'accent6': 7,
            'hlink': 8,  'folHlink': 9, 'dk2': 10,   'lt2': 11
        }

        for color_name, color_idx in color_map.items():
            # Find the color element
            color_elem = clr_scheme.find(f'a:{color_name}', ns)
            if color_elem is not None:
                # Look for srgbClr (RGB) or schemeClr (theme reference)
                srgb_elem = color_elem.find('a:srgbClr', ns)
                if srgb_elem is not None:
                    # Direct RGB color
                    rgb_val = srgb_elem.get('val')
                    if rgb_val:
                        self.theme_colors[color_idx] = f"#{rgb_val}"
                        self.theme_colors[color_name] = f"#{rgb_val}"

    def get_theme_color(self, color_ref) -> Optional[str]:
        """
        Get RGB value for theme color reference.

        Args:
            color_ref: Theme color index, MSO_THEME_COLOR enum value,
                       or XML name string (e.g., 'dk1', 'accent1')

        Returns:
            RGB hex string or None
        """
        # MSO_THEME_COLOR enum -> XML name mapping
        mso_to_xml = {
            1: 'dk1', 2: 'lt1', 3: 'dk2', 4: 'lt2',
            5: 'accent1', 6: 'accent2', 7: 'accent3', 8: 'accent4',
            9: 'accent5', 10: 'accent6', 11: 'hlink', 12: 'folHlink',
            13: 'dk1', 14: 'lt1', 15: 'dk2', 16: 'lt2',
            # TEXT_1-2, BACKGROUND_1-2 map to dk1/lt1/dk2/lt2
        }

        if isinstance(color_ref, int):
            # Try enum-to-XML-name mapping first
            xml_name = mso_to_xml.get(int(color_ref))
            if xml_name and xml_name in self.theme_colors:
                return self.theme_colors[xml_name]
            # Fallback to direct index lookup
            return self.theme_colors.get(int(color_ref))

        # Handle string color names
        if isinstance(color_ref, str):
            name = color_ref.lower()
            # Map bg1/bg2 to lt1/lt2 (OpenXML background color aliases)
            bg_map = {'bg1': 'lt1', 'bg2': 'lt2', 'tx1': 'dk1', 'tx2': 'dk2'}
            if name in bg_map:
                return self.theme_colors.get(bg_map[name])
            return self.theme_colors.get(name)

        return None

    def get_all_theme_colors(self) -> Dict[int, str]:
        """Get all available theme colors."""
        return self.theme_colors.copy()


# Test the theme color extractor
if __name__ == "__main__":
    extractor = ThemeColorExtractor("23-CNN.pptx")

    print("=== Theme Colors Found ===")
    for key, value in extractor.get_all_theme_colors().items():
        print(f"{key}: {value}")

    print(f"\nTotal theme colors found: {len(extractor.theme_colors)}")
