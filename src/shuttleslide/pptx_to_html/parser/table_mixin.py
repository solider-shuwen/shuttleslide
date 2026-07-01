"""
Table parsing mixin for PPTXParser.

Extracts per-cell text styling (color, font-size, bold, italic, alignment,
vertical anchor) in addition to background/border colors.  Without this,
header rows in dark-themed tables render as black-on-dark text and the
alignment information from `<a:pPr algn="ctr">` is lost.
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
                row_styles.append(self._parse_cell_style(cell, ns))

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

    def _parse_cell_style(self, cell, ns) -> dict:
        """
        Extract styling for a single table cell.

        Captures both the cell-level properties (background color, borders,
        vertical anchor, margins) and the dominant run-level text properties
        (color, font-size, bold, italic, font-name) plus paragraph-level
        alignment.  These are emitted as inline styles on the <td> so the
        rendered table matches the PPT.

        The "dominant" run is the first run in the first paragraph — PPT
        tables typically use uniform formatting per cell, and when they
        don't, the first run is the visually correct anchor for cell-level
        style application.
        """
        cell_style = {
            "background_color": None,
            "border_color": None,
            "text_color": None,
            "font_size": None,        # in points (float)
            "bold": None,             # tri-state: True / False / None
            "italic": None,
            "font_name": None,
            "alignment": None,        # 'left' | 'center' | 'right' | 'justify'
            "vertical_anchor": None,  # 'top' | 'middle' | 'bottom'
        }

        # --- Cell-level background ---
        bg_color = None
        try:
            if cell.fill and hasattr(cell.fill, "type") and cell.fill.type is not None:
                if hasattr(cell.fill, "fore_color") and cell.fill.fore_color is not None:
                    if hasattr(cell.fill.fore_color, "rgb") and cell.fill.fore_color.rgb:
                        bg_color = f"#{cell.fill.fore_color.rgb}"
        except Exception:
            pass

        # Fallback: parse XML directly for cell background + anchor + margins
        if hasattr(cell, '_tc'):
            tc_pr = cell._tc.find('a:tcPr', ns)
            if tc_pr is not None:
                if bg_color is None:
                    bg_color = self._extract_tc_background(tc_pr, ns)
                # Vertical anchor: tcPr anchor="ctr" | "t" | "b"
                anchor = tc_pr.get('anchor')
                if anchor:
                    cell_style["vertical_anchor"] = self._anchor_to_css(anchor)

        cell_style["background_color"] = bg_color

        # --- Text-level properties from first paragraph / first run ---
        self._extract_cell_text_style(cell, cell_style, ns)

        return cell_style

    def _extract_tc_background(self, tc_pr, ns) -> Optional[str]:
        """Extract background color from a <a:tcPr> element."""
        solid_fill = tc_pr.find('a:solidFill', ns)
        if solid_fill is None:
            return None
        srgb = solid_fill.find('a:srgbClr', ns)
        if srgb is not None:
            val = srgb.get('val')
            return f"#{val}" if val else None
        scheme_clr = solid_fill.find('a:schemeClr', ns)
        if scheme_clr is not None:
            val = scheme_clr.get('val')
            if val and self.theme_color_extractor:
                return self.theme_color_extractor.get_theme_color(val)
        return None

    @staticmethod
    def _anchor_to_css(anchor: str) -> str:
        """Map PPTX <a:tcPr anchor="..."> values to CSS vertical-align keywords."""
        return {
            't': 'top',
            'ctr': 'middle',
            'b': 'bottom',
        }.get(anchor, 'middle')

    def _extract_cell_text_style(self, cell, cell_style: dict, ns):
        """
        Walk the cell's paragraphs and extract the dominant text style.

        Reads paragraph-level alignment from <a:pPr algn="..."> and the
        first run's rPr for color/size/bold/italic/font-name.  Updates
        cell_style in place.
        """
        if not hasattr(cell, '_tc'):
            return

        txBody = cell._tc.find('a:txBody', ns)
        if txBody is None:
            return

        # Use the first paragraph for alignment (PPT cells typically use a
        # single paragraph; multi-paragraph cells inherit from the first).
        first_p = txBody.find('a:p', ns)
        if first_p is None:
            return

        # Paragraph alignment
        pPr = first_p.find('a:pPr', ns)
        if pPr is not None:
            algn = pPr.get('algn')
            if algn:
                cell_style["alignment"] = self._algn_to_css(algn)

        # First run properties
        first_r = first_p.find('a:r', ns)
        if first_r is None:
            # No runs — try endParaRPr for paragraph-level defaults
            endParaRPr = first_p.find('a:endParaRPr', ns)
            if endParaRPr is not None:
                self._apply_rpr(endParaRPr, cell_style, ns)
            return

        rPr = first_r.find('a:rPr', ns)
        if rPr is None:
            return
        self._apply_rpr(rPr, cell_style, ns)

    @staticmethod
    def _algn_to_css(algn: str) -> Optional[str]:
        """Map PPTX <a:pPr algn="..."> values to CSS text-align keywords."""
        return {
            'l': 'left',
            'ctr': 'center',
            'r': 'right',
            'just': 'justify',
            'dist': 'justify',
            'thaiDist': 'justify',
        }.get(algn)

    def _apply_rpr(self, rPr, cell_style: dict, ns):
        """Read color/size/bold/italic/font-name from an <a:rPr> element."""
        sz = rPr.get('sz')
        if sz:
            try:
                cell_style["font_size"] = int(sz) / 100.0
            except ValueError:
                pass

        b = rPr.get('b')
        if b == '1':
            cell_style["bold"] = True
        elif b == '0':
            cell_style["bold"] = False

        i = rPr.get('i')
        if i == '1':
            cell_style["italic"] = True
        elif i == '0':
            cell_style["italic"] = False

        # Font name from <a:latin typeface="...">
        latin = rPr.find('a:latin', ns)
        if latin is not None:
            typeface = latin.get('typeface')
            if typeface and not typeface.startswith('+'):
                # Skip theme references like "+mn-lt"
                cell_style["font_name"] = typeface

        # Text color from <a:solidFill>
        solid_fill = rPr.find('a:solidFill', ns)
        if solid_fill is not None:
            srgb = solid_fill.find('a:srgbClr', ns)
            if srgb is not None:
                val = srgb.get('val')
                if val:
                    cell_style["text_color"] = f"#{val}"
            else:
                scheme_clr = solid_fill.find('a:schemeClr', ns)
                if scheme_clr is not None:
                    val = scheme_clr.get('val')
                    if val and self.theme_color_extractor:
                        resolved = self.theme_color_extractor.get_theme_color(val)
                        if resolved:
                            cell_style["text_color"] = resolved
