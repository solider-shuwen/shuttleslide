"""
Shape Converter - converts shape elements to HTML.
"""

from typing import Optional
from html import escape
from shuttleslide.pptx_to_html.parser import ShapeElement


class ShapeConverter:
    """
    Converts shape elements from PPTX to HTML.
    """

    # Mapping of PPTX shape types to CSS classes
    SHAPE_TYPE_MAP = {
        "RECTANGLE": "shape-rectangle",
        "ROUNDED_RECTANGLE": "shape-rounded-rectangle",
        "OVAL": "shape-oval",
        "ELLIPSE": "shape-ellipse",
        "CIRCLE": "shape-circle",
        "TRIANGLE": "shape-triangle",
        "DIAMOND": "shape-diamond",
        "STAR": "shape-star",
        "ARROW": "shape-arrow",
        "LINE": "shape-line",
        "ARC": "shape-arc",
    }

    def convert(self, element: ShapeElement) -> str:
        """
        Convert a shape element to HTML.

        Args:
            element: ShapeElement to convert

        Returns:
            HTML string representation
        """
        # Determine shape class
        shape_class = self._get_shape_class(element)

        # Build div attributes
        attrs = []

        # Add CSS class
        attrs.append(f'class="{shape_class}"')

        # Add styling
        styles = self._build_shape_styles(element)
        if styles:
            attrs.append(f'style="{"; ".join(styles)}"')

        # Add data attributes for round-trip
        attrs.extend(self._build_data_attributes(element))

        attr_str = " ".join(attrs)

        # Build HTML
        if element.text:
            escaped_text = escape(element.text)
            return f"<div {attr_str}>{escaped_text}</div>"
        else:
            return f"<div {attr_str}></div>"

    def _get_shape_class(self, element: ShapeElement) -> str:
        """
        Get CSS class for shape type.

        Args:
            element: ShapeElement

        Returns:
            CSS class name
        """
        shape_type = element.shape_type.upper()

        return self.SHAPE_TYPE_MAP.get(shape_type, "shape-generic")

    def _build_shape_styles(self, element: ShapeElement) -> list[str]:
        """
        Build CSS styles for shape element.

        Args:
            element: ShapeElement with styling info

        Returns:
            List of CSS style declarations
        """
        styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"width: {element.width}px",
            f"height: {element.height}px",
            f"z-index: {element.z_order}",
        ]

        # Add fill color
        if element.fill_color:
            styles.append(f"background-color: {element.fill_color}")

        # Add border (line color)
        if element.line_color:
            styles.extend([
                f"border: 1px solid {element.line_color}",
            ])

        # Add shape-specific styles
        shape_type = element.shape_type.upper()

        if shape_type in ["OVAL", "ELLIPSE", "CIRCLE"]:
            styles.append("border-radius: 50%")

        elif shape_type == "ROUNDED_RECTANGLE":
            styles.append("border-radius: 10px")

        elif shape_type == "TRIANGLE":
            # Use clip-path for triangle
            styles.append("clip-path: polygon(50% 0%, 0% 100%, 100% 100%)")

        elif shape_type == "DIAMOND":
            # Use clip-path for diamond
            styles.append("clip-path: polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)")

        # Add text styles if shape has text
        if element.text:
            styles.extend([
                "display: flex",
                "align-items: center",
                "justify-content: center",
                "text-align: center",
            ])

        return styles

    def _build_data_attributes(self, element: ShapeElement) -> list[str]:
        """
        Build data-pptx-* attributes for round-trip conversion.

        Args:
            element: ShapeElement with metadata

        Returns:
            List of data attribute strings
        """
        attrs = []

        # Store position and size
        attrs.append(f'data-pptx-element-type="shape"')
        attrs.append(f'data-pptx-shape-type="{element.shape_type}"')
        attrs.append(f'data-pptx-left="{element.left}"')
        attrs.append(f'data-pptx-top="{element.top}"')
        attrs.append(f'data-pptx-width="{element.width}"')
        attrs.append(f'data-pptx-height="{element.height}"')
        attrs.append(f'data-pptx-z-order="{element.z_order}"')

        # Store colors
        if element.fill_color:
            attrs.append(f'data-pptx-fill-color="{element.fill_color}"')

        if element.line_color:
            attrs.append(f'data-pptx-line-color="{element.line_color}"')

        return attrs

    def convert_with_text_wrapper(self, element: ShapeElement) -> str:
        """
        Convert shape with separate wrapper for text content.

        Args:
            element: ShapeElement to convert

        Returns:
            HTML string with shape and text separated
        """
        if not element.text:
            return self.convert(element)

        # Build outer shape div
        shape_attrs = [
            f'class="{self._get_shape_class(element)}"',
            f'style="{"; ".join(self._build_shape_styles(element))}"',
        ]

        # Remove text-related styles from shape div
        shape_attrs_cleaned = []
        for attr in shape_attrs:
            if "display: flex" not in attr and "align-items" not in attr:
                shape_attrs_cleaned.append(attr)

        shape_html = f"<div {' '.join(shape_attrs_cleaned)}></div>"

        # Build text wrapper
        text_styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"width: {element.width}px",
            f"height: {element.height}px",
            f"z-index: {element.z_order + 1}",
            f"display: flex",
            f"align-items: center",
            f"justify-content: center",
            f"text-align: center",
        ]

        text_attrs = [
            f'style="{"; ".join(text_styles)}"',
            f'data-pptx-shape-text="true"',
        ]

        escaped_text = escape(element.text)
        text_html = f"<div {' '.join(text_attrs)}>{escaped_text}</div>"

        return f"{shape_html}\n{text_html}"
