"""
Base Layout - shared logic for all layout modes.
"""

from typing import Optional

from shuttleslide.pptx_to_html.models import SlideElement
from shuttleslide.pptx_to_html.converters.text import TextConverter
from shuttleslide.pptx_to_html.converters.tables import TableConverter
from shuttleslide.pptx_to_html.converters.images import ImageConverter
from shuttleslide.pptx_to_html.converters.shapes import ShapeConverter


class BaseLayout:
    """
    Common base class for all layout modes.

    Handles converter initialization and provides element dispatch.
    Subclasses override element conversion methods for layout-specific positioning.
    """

    def __init__(self, use_base64: bool = False):
        """
        Initialize the base layout with converters.

        Args:
            use_base64: Whether to embed images as base64 (True) or save as separate files (False, default).
        """
        self.text_converter = TextConverter()
        self.table_converter = TableConverter()
        self.image_converter = ImageConverter(use_base64=use_base64)
        self.shape_converter = ShapeConverter(use_base64=use_base64)

    def _convert_element_basic(self, element: SlideElement) -> Optional[str]:
        """
        Convert an element using the appropriate converter, without layout wrapping.

        Useful for layouts that need the raw converter output before applying
        their own positioning.

        Args:
            element: SlideElement to convert

        Returns:
            HTML string for the element, or None if type is unknown
        """
        if element.element_type == "text":
            return self.text_converter.convert(element)

        elif element.element_type == "table":
            return self.table_converter.convert(element)

        elif element.element_type == "image":
            return self.image_converter.convert(element)

        elif element.element_type == "shape":
            return self.shape_converter.convert(element)

        return None
