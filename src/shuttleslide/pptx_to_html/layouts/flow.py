"""
Flow Layout - converts slides to HTML with natural flow layout.
"""

from typing import List
from shuttleslide.pptx_to_html.parser import ParsedSlide, SlideElement
from shuttleslide.pptx_to_html.converters.text import TextConverter
from shuttleslide.pptx_to_html.converters.tables import TableConverter
from shuttleslide.pptx_to_html.converters.images import ImageConverter
from shuttleslide.pptx_to_html.converters.shapes import ShapeConverter


class FlowLayout:
    """
    Generates HTML with flow layout for slides.
    Elements are arranged in document flow, not absolutely positioned.
    """

    def __init__(self):
        """Initialize the flow layout with converters."""
        self.text_converter = TextConverter()
        self.table_converter = TableConverter()
        self.image_converter = ImageConverter()
        self.shape_converter = ShapeConverter()

    def convert(self, slides: List[ParsedSlide]) -> str:
        """
        Convert slides to HTML with flow layout.

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
        Convert a single slide to HTML section.

        Args:
            slide: ParsedSlide to convert

        Returns:
            HTML section string
        """
        html_parts = [f"<section class='slide' data-pptx-slide-number='{slide.slide_number}'>"]

        # Add slide metadata
        if slide.metadata:
            html_parts.append(f"    <!-- Slide metadata: {slide.metadata} -->")

        # Sort elements by z-order and convert
        sorted_elements = sorted(slide.elements, key=lambda e: e.z_order)

        for element in sorted_elements:
            element_html = self._convert_element(element)
            if element_html:
                html_parts.append(f"    {element_html}")

        html_parts.append("</section>")

        return "\n".join(html_parts)

    def _convert_element(self, element: SlideElement) -> str:
        """
        Convert a single element to HTML.

        Args:
            element: SlideElement to convert

        Returns:
            HTML string for the element
        """
        if element.element_type == "text":
            return self.text_converter.convert(element)

        elif element.element_type == "table":
            return self.table_converter.convert(element)

        elif element.element_type == "image":
            return self.image_converter.convert(element)

        elif element.element_type == "shape":
            return self.shape_converter.convert(element)

        else:
            return ""

    def _get_styles(self) -> str:
        """
        Get CSS styles for flow layout.

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
            background-color: #f0f0f0;
            padding: 20px;
        }

        .presentation {
            max-width: 1200px;
            margin: 0 auto;
            background-color: white;
            padding: 40px;
            box-shadow: 0 2px 10px rgba(0, 0, 0, 0.1);
        }

        .slide {
            page-break-after: always;
            margin-bottom: 40px;
            min-height: 400px;
        }

        .slide:last-child {
            margin-bottom: 0;
        }

        /* Typography */
        h1 {
            font-size: 36px;
            margin-bottom: 20px;
            color: #333;
        }

        h2 {
            font-size: 28px;
            margin-bottom: 16px;
            color: #444;
        }

        h3 {
            font-size: 24px;
            margin-bottom: 14px;
            color: #555;
        }

        p {
            font-size: 18px;
            line-height: 1.6;
            margin-bottom: 12px;
            color: #333;
        }

        /* Lists */
        ul, ol {
            margin-left: 24px;
            margin-bottom: 16px;
        }

        li {
            font-size: 18px;
            line-height: 1.6;
            margin-bottom: 8px;
        }

        /* Tables */
        table {
            border-collapse: collapse;
            margin: 20px 0;
            width: 100%;
        }

        th {
            background-color: #4CAF50;
            color: white;
            padding: 12px;
            text-align: left;
            font-weight: bold;
        }

        td {
            border: 1px solid #ddd;
            padding: 12px;
        }

        tr:nth-child(even) {
            background-color: #f9f9f9;
        }

        /* Images */
        img {
            max-width: 100%;
            height: auto;
            margin: 20px 0;
            display: block;
        }

        /* Shapes */
        .shape-rectangle,
        .shape-oval,
        .shape-circle,
        .shape-triangle,
        .shape-diamond {
            margin: 20px 0;
        }

        /* Print styles */
        @media print {
            body {
                background-color: white;
                padding: 0;
            }

            .presentation {
                box-shadow: none;
                padding: 0;
                max-width: none;
            }

            .slide {
                page-break-after: always;
                margin-bottom: 0;
            }
        }
    </style>"""

    def convert_slides_to_slides_html(self, slides: List[ParsedSlide]) -> str:
        """
        Convert slides to individual HTML files (one per slide).

        Args:
            slides: List of parsed slides

        Returns:
            List of HTML strings, one per slide
        """
        return [self._convert_slide(slide) for slide in slides]
