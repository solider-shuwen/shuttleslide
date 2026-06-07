"""
Text Converter - converts text elements to HTML.
"""

from typing import Optional
from html import escape
from shuttleslide.pptx_to_html.parser import TextElement


class TextConverter:
    """
    Converts text elements from PPTX to HTML.
    """

    def convert(self, element: TextElement) -> str:
        """
        Convert a text element to HTML.

        Args:
            element: TextElement to convert

        Returns:
            HTML string representation
        """
        text = element.text.strip()

        if not text:
            return ""

        # Determine HTML tag based on element properties
        if element.is_title:
            tag = "h1"
        elif element.level > 0:
            # Map level to heading tag (1 -> h2, 2 -> h3, etc.)
            tag = f"h{min(element.level + 1, 6)}"
        else:
            # Check for list-like content
            if text.startswith(("- ", "* ", "• ", "· ")):
                return self._convert_list_item(text, element)
            elif text[0:1].isdigit() and text[1:2] in [". ", ") "]:
                return self._convert_list_item(text, element, ordered=True)
            else:
                tag = "p"

        # Build HTML with styling
        styles = self._build_text_styles(element)

        # Build attributes
        attrs = []
        if styles:
            attrs.append(f'style="{styles}"')

        # Add data attributes for round-trip
        attrs.extend(self._build_data_attributes(element))

        # Build HTML
        attr_str = " ".join(attrs)
        escaped_text = escape(text)

        return f"<{tag} {attr_str}>{escaped_text}</{tag}>"

    def _convert_list_item(self, text: str, element: TextElement, ordered: bool = False) -> str:
        """
        Convert a list item to HTML.

        Args:
            text: Text content
            element: TextElement with styling info
            ordered: Whether this is an ordered list item

        Returns:
            HTML string for list item
        """
        # Remove bullet/number
        if ordered:
            # Remove "1. " or "1) " prefix
            clean_text = text.split(". ", 1)[-1].split(") ", 1)[-1]
            tag = "li"
        else:
            # Remove bullet prefix
            clean_text = text[2:] if text[:2] in ["- ", "* "] else text[3:]
            tag = "li"

        # Build HTML with styling
        styles = self._build_text_styles(element)

        # Build attributes
        attrs = []
        if styles:
            attrs.append(f'style="{styles}"')

        attrs.extend(self._build_data_attributes(element))

        attr_str = " ".join(attrs)
        escaped_text = escape(clean_text.strip())

        return f"<{tag} {attr_str}>{escaped_text}</{tag}>"

    def _build_text_styles(self, element: TextElement) -> str:
        """
        Build CSS style string from text element properties.

        Args:
            element: TextElement with styling info

        Returns:
            CSS style string
        """
        styles = []

        if element.font_name:
            styles.append(f"font-family: '{element.font_name}'")

        if element.font_size:
            styles.append(f"font-size: {element.font_size}pt")

        if element.bold:
            styles.append("font-weight: bold")

        if element.italic:
            styles.append("font-style: italic")

        if element.color:
            styles.append(f"color: {element.color}")

        return "; ".join(styles)

    def _build_data_attributes(self, element: TextElement) -> list[str]:
        """
        Build data-pptx-* attributes for round-trip conversion.

        Args:
            element: TextElement with metadata

        Returns:
            List of data attribute strings
        """
        attrs = []

        # Store original position and size
        attrs.append(f'data-pptx-left="{element.left}"')
        attrs.append(f'data-pptx-top="{element.top}"')
        attrs.append(f'data-pptx-width="{element.width}"')
        attrs.append(f'data-pptx-height="{element.height}"')
        attrs.append(f'data-pptx-z-order="{element.z_order}"')

        # Store font information
        if element.font_name:
            attrs.append(f'data-pptx-font-name="{element.font_name}"')

        if element.font_size:
            attrs.append(f'data-pptx-font-size="{element.font_size}"')

        if element.bold:
            attrs.append('data-pptx-bold="true"')

        if element.italic:
            attrs.append('data-pptx-italic="true"')

        if element.color:
            attrs.append(f'data-pptx-color="{element.color}"')

        if element.is_title:
            attrs.append('data-pptx-is-title="true"')

        return attrs

    @staticmethod
    def detect_heading_level(text: str) -> int:
        """
        Detect heading level from text content.

        Args:
            text: Text to analyze

        Returns:
            Heading level (0 = body text, 1 = h1, 2 = h2, etc.)
        """
        # Simple heuristic: if text is short and on its own line, it might be a heading
        if len(text) < 50 and text == text.upper():
            return 1  # Likely h1
        elif len(text) < 80:
            return 2  # Likely h2
        else:
            return 0  # Body text

    @staticmethod
    def is_list_text(text: str) -> tuple[bool, bool]:
        """
        Check if text is a list item.

        Args:
            text: Text to check

        Returns:
            Tuple of (is_list, is_ordered)
        """
        # Check for unordered list bullets
        if text.startswith(("- ", "* ", "• ", "· ")):
            return True, False

        # Check for ordered list numbers
        if text[0:1].isdigit() and text[1:2] in [". ", ") "]:
            return True, True

        return False, False
