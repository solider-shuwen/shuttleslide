"""
Flow Layout - converts slides to HTML with natural flow layout.
"""

from typing import List, Optional
import base64

from shuttleslide.pptx_to_html.models import ParsedSlide, SlideElement, GroupElement
from shuttleslide.pptx_to_html.layouts.base import BaseLayout


class FlowLayout(BaseLayout):
    """
    Generates HTML with flow layout for slides.
    Elements are arranged in document flow, not absolutely positioned.
    """

    def __init__(self):
        """Initialize the flow layout with converters."""
        super().__init__(use_base64=False)

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
        bg_style = self._get_background_style(slide)
        style_attr = f" style='{bg_style}'" if bg_style else ""
        html_parts = [f"<section class='slide' data-pptx-slide-number='{slide.slide_number}'{style_attr}>"]

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

        elif element.element_type == "group":
            return self._convert_group(element)

        else:
            return ""

    def _convert_group(self, element: GroupElement) -> str:
        """Convert group element in flow layout."""
        if not element.children:
            return ""
        children_html = []
        for child in element.children:
            child_html = self._convert_element(child)
            if child_html:
                children_html.append(child_html)
        inner = "\n".join(children_html)
        return f'<div class="group-wrapper">{inner}</div>'

    def _get_background_style(self, slide: ParsedSlide) -> Optional[str]:
        """
        Generate CSS background string from ParsedSlide.background.

        Args:
            slide: ParsedSlide with optional background data

        Returns:
            CSS background property string or None
        """
        if not slide.background:
            return None
        bg = slide.background
        if bg.bg_type == 'solid' and bg.color:
            return f"background-color: {bg.color}"
        elif bg.bg_type == 'gradient' and bg.gradient_css:
            return f"background: {bg.gradient_css}"
        elif bg.bg_type == 'image' and bg.image_data:
            mime = f"image/{bg.image_data['image_type']}"
            encoded = base64.b64encode(bg.image_data['image_bytes']).decode('utf-8')
            parts = [f'background-image: url("data:{mime};base64,{encoded}")']
            parts.append('background-size: cover')

            if bg.overlay_color and bg.overlay_opacity is not None:
                r = int(bg.overlay_color[1:3], 16)
                g = int(bg.overlay_color[3:5], 16)
                b = int(bg.overlay_color[5:7], 16)
                rgba = f"rgba({r},{g},{b},{bg.overlay_opacity:.2f})"
                parts[0] = (
                    f'background-image: linear-gradient({rgba}, {rgba}), '
                    f'url("data:{mime};base64,{encoded}")'
                )
            return "; ".join(parts)
        return None

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

        /* Multi-level bullet paragraphs (PPT-converted) */
        .bullet-paragraph {
            display: flex;
            align-items: baseline;
        }

        .bullet-paragraph .bullet {
            flex-shrink: 0;
            line-height: 1;
        }

        .bullet-paragraph .bullet img {
            display: inline-block;
            vertical-align: middle;
        }

        .bullet-paragraph .text {
            flex-grow: 1;
        }

        .text-paragraph {
            margin: 0;
            padding: 0;
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
