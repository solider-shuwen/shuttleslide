"""
Table parsing mixin for PPTXParser.
"""

from typing import Optional

from shuttleslide.pptx_to_html.models import TableElement
from shuttleslide.pptx_to_html.utils.namespaces import NS_A


class TableMixin:
    """Table element parsing methods."""

    def _parse_table(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> TableElement:
        """Parse a table shape."""
        table = shape.table
        rows = len(table.rows)
        cols = len(table.columns)

        # Extract table data
        data = []
        cell_styles = []

        ns = NS_A

        for row in table.rows:
            row_data = []
            row_styles = []
            for cell in row.cells:
                row_data.append(cell.text)
                # Extract cell styling
                cell_style = {
                    "background_color": None,
                    "border_color": None,
                }

                # Try python-pptx fill API first
                bg_color = None
                try:
                    if cell.fill and hasattr(cell.fill, "type") and cell.fill.type is not None:
                        if hasattr(cell.fill, "fore_color") and cell.fill.fore_color is not None:
                            if hasattr(cell.fill.fore_color, "rgb") and cell.fill.fore_color.rgb:
                                bg_color = f"#{cell.fill.fore_color.rgb}"
                except Exception:
                    pass

                # Fallback: parse XML directly for cell background
                if bg_color is None and hasattr(cell, '_tc'):
                    tc_pr = cell._tc.find('a:tcPr', ns)
                    if tc_pr is not None:
                        # Check for solidFill
                        solid_fill = tc_pr.find('a:solidFill', ns)
                        if solid_fill is not None:
                            srgb = solid_fill.find('a:srgbClr', ns)
                            if srgb is not None:
                                bg_color = f"#{srgb.get('val')}"
                            else:
                                scheme_clr = solid_fill.find('a:schemeClr', ns)
                                if scheme_clr is not None:
                                    val = scheme_clr.get('val')
                                    if val and self.theme_color_extractor:
                                        bg_color = self.theme_color_extractor.get_theme_color(val)

                cell_style["background_color"] = bg_color
                row_styles.append(cell_style)
            data.append(row_data)
            cell_styles.append(row_styles)

        return TableElement(
            element_type="table",
            left=left,
            top=top,
            width=width,
            height=height,
            z_order=z_order,
            rows=rows,
            cols=cols,
            data=data,
            cell_styles=cell_styles,
        )
