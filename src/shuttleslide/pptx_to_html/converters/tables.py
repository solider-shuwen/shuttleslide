"""
Table Converter - converts table elements to HTML.
"""

from typing import List
from html import escape
from shuttleslide.pptx_to_html.parser import TableElement


class TableConverter:
    """
    Converts table elements from PPTX to HTML.
    """

    def convert(self, element: TableElement) -> str:
        """
        Convert a table element to HTML.

        Args:
            element: TableElement to convert

        Returns:
            HTML string representation
        """
        if element.rows == 0 or element.cols == 0:
            return ""

        # Build table HTML
        html_parts = ["<table"]

        # Add styling and data attributes
        table_attrs = self._build_table_attributes(element)
        html_parts.append(table_attrs)
        html_parts.append(">")

        # Build rows
        for row_idx in range(element.rows):
            html_parts.append("<tr>")
            for col_idx in range(element.cols):
                cell_html = self._convert_cell(element, row_idx, col_idx)
                html_parts.append(cell_html)
            html_parts.append("</tr>")

        html_parts.append("</table>")

        return "".join(html_parts)

    def _convert_cell(self, element: TableElement, row_idx: int, col_idx: int) -> str:
        """
        Convert a single table cell to HTML.

        Args:
            element: TableElement containing the cell
            row_idx: Row index
            col_idx: Column index

        Returns:
            HTML string for the cell
        """
        # Get cell content
        text = ""
        if row_idx < len(element.data) and col_idx < len(element.data[row_idx]):
            text = element.data[row_idx][col_idx]

        # Get cell style
        cell_style = ""
        bg_color = None
        if row_idx < len(element.cell_styles) and col_idx < len(element.cell_styles[row_idx]):
            style_info = element.cell_styles[row_idx][col_idx]
            bg_color = style_info.get("background_color")

        # Build cell attributes
        attrs = []

        # Add styling
        styles = []
        if bg_color:
            styles.append(f"background-color: {bg_color}")

        if styles:
            attrs.append(f'style="{"; ".join(styles)}"')

        # Add data attributes for round-trip
        attrs.append(f'data-pptx-row="{row_idx}"')
        attrs.append(f'data-pptx-col="{col_idx}"')

        attr_str = " ".join(attrs)
        escaped_text = escape(text)

        return f"<td {attr_str}>{escaped_text}</td>"

    def _build_table_attributes(self, element: TableElement) -> str:
        """
        Build table attributes including data-pptx-* for round-trip.

        Args:
            element: TableElement

        Returns:
            Attribute string
        """
        attrs = []

        # Add styling
        styles = [
            f"width: {element.width}px",
            f"height: {element.height}px",
        ]
        attrs.append(f'style="{"; ".join(styles)}"')

        # Add data attributes for round-trip
        attrs.append(f'data-pptx-element-type="table"')
        attrs.append(f'data-pptx-left="{element.left}"')
        attrs.append(f'data-pptx-top="{element.top}"')
        attrs.append(f'data-pptx-width="{element.width}"')
        attrs.append(f'data-pptx-height="{element.height}"')
        attrs.append(f'data-pptx-z-order="{element.z_order}"')
        attrs.append(f'data-pptx-rows="{element.rows}"')
        attrs.append(f'data-pptx-cols="{element.cols}"')

        return " ".join(attrs)

    @staticmethod
    def has_header_row(element: TableElement) -> bool:
        """
        Determine if the first row is a header row.

        Args:
            element: TableElement to check

        Returns:
            True if first row appears to be a header
        """
        if element.rows == 0:
            return False

        # Simple heuristic: if all cells in first row have bold text
        # This is a simplified check - in real implementation you'd check formatting
        # For now, just check if first row has content
        if len(element.data) > 0:
            first_row = element.data[0]
            return all(cell.strip() for cell in first_row)

        return False

    def convert_with_header(self, element: TableElement) -> str:
        """
        Convert table to HTML with proper header row (thead/tbody).

        Args:
            element: TableElement to convert

        Returns:
            HTML string with thead/tbody structure
        """
        if element.rows == 0 or element.cols == 0:
            return ""

        html_parts = ["<table"]

        # Add styling and data attributes
        table_attrs = self._build_table_attributes(element)
        html_parts.append(table_attrs)
        html_parts.append(">")

        # Check for header row
        use_header = self.has_header_row(element)

        if use_header and element.rows > 0:
            # Header row
            html_parts.append("<thead>")
            html_parts.append("<tr>")
            for col_idx in range(element.cols):
                text = element.data[0][col_idx] if len(element.data) > 0 else ""
                cell_html = f"<th>{escape(text)}</th>"
                html_parts.append(cell_html)
            html_parts.append("</tr>")
            html_parts.append("</thead>")

            # Body rows
            html_parts.append("<tbody>")
            for row_idx in range(1, element.rows):
                html_parts.append("<tr>")
                for col_idx in range(element.cols):
                    cell_html = self._convert_cell(element, row_idx, col_idx)
                    html_parts.append(cell_html)
                html_parts.append("</tr>")
            html_parts.append("</tbody>")
        else:
            # No header, just body
            html_parts.append("<tbody>")
            for row_idx in range(element.rows):
                html_parts.append("<tr>")
                for col_idx in range(element.cols):
                    cell_html = self._convert_cell(element, row_idx, col_idx)
                    html_parts.append(cell_html)
                html_parts.append("</tr>")
            html_parts.append("</tbody>")

        html_parts.append("</table>")

        return "".join(html_parts)
