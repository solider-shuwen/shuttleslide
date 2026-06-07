"""
Absolute Layout - converts slides to HTML with absolute positioning.
"""

from typing import List
from shuttleslide.pptx_to_html.parser import ParsedSlide, SlideElement, TextElement
from shuttleslide.pptx_to_html.converters.text import TextConverter
from shuttleslide.pptx_to_html.converters.tables import TableConverter
from shuttleslide.pptx_to_html.converters.images import ImageConverter
from shuttleslide.pptx_to_html.converters.shapes import ShapeConverter


class AbsoluteLayout:
    """
    Generates HTML with absolute positioning for slides.
    Elements are positioned using CSS absolute positioning to preserve exact layout.
    """

    def __init__(self):
        """Initialize the absolute layout with converters."""
        self.text_converter = TextConverter()
        self.table_converter = TableConverter()
        self.image_converter = ImageConverter()
        self.shape_converter = ShapeConverter()

    def convert(self, slides: List[ParsedSlide]) -> str:
        """
        Convert slides to HTML with absolute positioning.

        Args:
            slides: List of parsed slides

        Returns:
            Complete HTML document string
        """
        html_parts = [
            "<!DOCTYPE html>",
            "<html>",
            "<head>",
            "    <meta charset='UTF-8'>",
            "    <meta name='viewport' content='width=device-width, initial-scale=1.0'>",
            "    <title>Presentation</title>",
            self._get_styles(),
            "</head>",
            "<body>",
            "    <div class='presentation'>",
        ]

        # Convert each slide
        for slide in slides:
            slide_html = self._convert_slide(slide)
            html_parts.append(f"        {slide_html}")

        html_parts.extend([
            "    </div>",
            "</body>",
            "</html>",
        ])

        return "\n".join(html_parts)

    def _convert_slide(self, slide: ParsedSlide) -> str:
        """
        Convert a single slide to HTML section with absolute positioning.

        Args:
            slide: ParsedSlide to convert

        Returns:
            HTML section string
        """
        # Calculate slide aspect ratio for responsive scaling
        slide_ratio = slide.height / slide.width if slide.width > 0 else 0.75

        html_parts = [
            f"<section class='slide'",
            f"    data-pptx-slide-number='{slide.slide_number}'",
            f"    data-pptx-layout='{slide.layout_name}'",
            f"    style='width: {slide.width}px; height: {slide.height}px; aspect-ratio: {slide.width}/{slide.height};'>",
        ]

        # Add slide metadata
        if slide.metadata:
            html_parts.append(f"    <!-- Slide metadata: {slide.metadata} -->")

        # Sort elements by z-order and convert with absolute positioning
        sorted_elements = sorted(slide.elements, key=lambda e: e.z_order)

        for element in sorted_elements:
            element_html = self._convert_element_absolute(element)
            if element_html:
                html_parts.append(f"    {element_html}")

        html_parts.append("</section>")

        return "\n".join(html_parts)

    def _convert_element_absolute(self, element: SlideElement) -> str:
        """
        Convert a single element to HTML with absolute positioning.

        Args:
            element: SlideElement to convert

        Returns:
            HTML string for the element with absolute positioning
        """
        if element.element_type == "text":
            return self._convert_text_absolute(element)

        elif element.element_type == "table":
            return self._convert_table_absolute(element)

        elif element.element_type == "image":
            return self._convert_image_absolute(element)

        elif element.element_type == "shape":
            return self._convert_shape_absolute(element)

        else:
            return ""

    def _convert_text_absolute(self, element: TextElement) -> str:
        """
        Convert text element to HTML with absolute positioning.

        Args:
            element: TextElement to convert

        Returns:
            HTML string with absolute positioning
        """
        # Get basic text HTML
        text_html = self.text_converter.convert(element)

        # Wrap in positioned div
        wrapper_styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"width: {element.width}px",
            f"height: {element.height}px",
            f"z-index: {element.z_order}",
            f"overflow: hidden",
        ]

        return f"<div style='{"; ".join(wrapper_styles)}'>{text_html}</div>"

    def _convert_table_absolute(self, element: SlideElement) -> str:
        """
        Convert table element to HTML with absolute positioning.

        Args:
            element: TableElement to convert

        Returns:
            HTML string with absolute positioning
        """
        # Get basic table HTML
        table_html = self.table_converter.convert(element)

        # The table already has width/height styling, just add positioning
        wrapper_styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"z-index: {element.z_order}",
        ]

        return f"<div style='{"; ".join(wrapper_styles)}'>{table_html}</div>"

    def _convert_image_absolute(self, element: SlideElement) -> str:
        """
        Convert image element to HTML with absolute positioning.

        Args:
            element: ImageElement to convert

        Returns:
            HTML string with absolute positioning
        """
        # Get image HTML with wrapper
        return self.image_converter.convert_with_wrapper(element)

    def _convert_shape_absolute(self, element: SlideElement) -> str:
        """
        Convert shape element to HTML with absolute positioning.

        Args:
            element: ShapeElement to convert

        Returns:
            HTML string with absolute positioning
        """
        # Get basic shape HTML
        shape_html = self.shape_converter.convert(element)

        # Shape already has absolute positioning in converter
        return shape_html

    def _get_styles(self) -> str:
        """
        Get CSS styles for absolute layout.

        Returns:
            CSS style block
        """
        return """    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }

        body {
            font-family: 'Arial', sans-serif;
            background-color: #1a1a1a;
            padding: 20px;
            display: flex;
            justify-content: center;
            align-items: flex-start;
            min-height: 100vh;
        }

        .presentation {
            width: 100%;
            max-width: 100%;
        }

        .slide {
            position: relative;
            background-color: white;
            margin: 20px auto;
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
        }

        /* Typography base styles */
        h1, h2, h3, h4, h5, h6, p {
            margin: 0;
            padding: 0;
        }

        /* Responsive scaling */
        @media (max-width: 1200px) {
            .slide {
                transform: scale(0.8);
                transform-origin: top center;
            }
        }

        @media (max-width: 800px) {
            .slide {
                transform: scale(0.6);
                transform-origin: top center;
            }
        }

        @media (max-width: 600px) {
            .slide {
                transform: scale(0.4);
                transform-origin: top center;
            }
        }

        /* Print styles */
        @media print {
            body {
                background-color: white;
                padding: 0;
            }

            .presentation {
                max-width: 100%;
            }

            .slide {
                margin: 0;
                page-break-after: always;
                box-shadow: none;
                transform: none !important;
            }
        }
    </style>"""

    def convert_slides_to_slides_html(self, slides: List[ParsedSlide]) -> List[str]:
        """
        Convert slides to individual HTML files (one per slide).

        Args:
            slides: List of parsed slides

        Returns:
            List of HTML strings, one per slide
        """
        return [self._convert_slide(slide) for slide in slides]
