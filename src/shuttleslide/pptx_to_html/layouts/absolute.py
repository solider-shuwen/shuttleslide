"""
Absolute Layout - converts slides to HTML with absolute positioning.
"""

from typing import List, Optional
import base64

from shuttleslide.pptx_to_html.models import ParsedSlide, SlideElement, TextElement, GroupElement
from shuttleslide.pptx_to_html.layouts.base import BaseLayout


class AbsoluteLayout(BaseLayout):
    """
    Generates HTML with absolute positioning for slides.
    Elements are positioned using CSS absolute positioning to preserve exact layout.
    """

    def __init__(self, use_base64: bool = False):
        """
        Initialize the absolute layout with converters.

        Args:
            use_base64: Whether to embed images as base64 (True) or save as separate files (False, default).
        """
        super().__init__(use_base64=use_base64)

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

        # Build inline style for the section
        section_styles = [
            f"width: {slide.width}px",
            f"height: {slide.height}px",
            f"aspect-ratio: {slide.width}/{slide.height}",
        ]

        # Apply slide background
        bg_style = self._get_background_style(slide)
        if bg_style:
            section_styles.append(bg_style)

        section_style_str = "; ".join(section_styles)

        html_parts = [
            f"<section class='slide'",
            f"    data-pptx-slide-number='{slide.slide_number}'",
            f"    data-pptx-layout='{slide.layout_name}'",
            f"    style='{section_style_str}'>",
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

        elif element.element_type == "group":
            return self._convert_group_absolute(element)

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

        # Build wrapper styles with positioning and formatting
        wrapper_styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"width: {element.width}px",
            f"height: {element.height}px",
            f"z-index: {element.z_order}",
            f"overflow: hidden",
        ]

        # Apply vertical alignment using CSS
        # For center/bottom alignment, we adjust the content position within the element
        if element.vertical_align == 'middle':
            wrapper_styles.append("display: flex")
            wrapper_styles.append("align-items: center")
            wrapper_styles.append("justify-content: center")
        elif element.vertical_align == 'bottom':
            wrapper_styles.append("display: flex")
            wrapper_styles.append("align-items: flex-end")
            wrapper_styles.append("justify-content: center")

        # Apply font styling if available
        if element.font_name:
            wrapper_styles.append(f"font-family: '{element.font_name}'")
        if element.font_size:
            wrapper_styles.append(f"font-size: {element.font_size}pt")
        if element.bold:
            wrapper_styles.append("font-weight: bold")
        if element.italic:
            wrapper_styles.append("font-style: italic")
        if element.color:
            wrapper_styles.append(f"color: {element.color}")

        # Apply border/outline if present
        if element.line_color:
            border_width = element.line_width if element.line_width else 1
            wrapper_styles.append(f"border: {border_width}px solid {element.line_color}")

        # Apply rotation and transform styles
        transform_parts = []

        # Handle vertical text (writing-mode is a separate CSS property, not a transform)
        if element.vert:
            # East Asian vertical text
            if element.vert == 'eaVert':
                # For eaVert, use writing-mode: vertical-rl
                wrapper_styles.append("writing-mode: vertical-rl")
            elif element.vert == 'mongolianVert':
                wrapper_styles.append("writing-mode: vertical-rl")
            elif element.vert == 'wordVert':
                wrapper_styles.append("writing-mode: vertical-lr")
        else:
            # For non-vertical text, handle flips
            if element.flip_h:
                transform_parts.append("scaleX(-1)")

        if element.flip_v:
            transform_parts.append("scaleY(-1)")

        if element.rotation:
            transform_parts.append(f"rotate({element.rotation}deg)")

        # Combine all transforms
        if transform_parts:
            wrapper_styles.append(f"transform: {' '.join(transform_parts)}")

        style_str = "; ".join(wrapper_styles)
        return f"<div style=\"{style_str}\">{text_html}</div>"

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

        style_str = "; ".join(wrapper_styles)
        return f"<div style='{style_str}'>{table_html}</div>"

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

    def _convert_group_absolute(self, element: GroupElement) -> str:
        """
        Convert group element to HTML with a positioned container holding children.

        Children have coordinates relative to the group's top-left.

        Args:
            element: GroupElement with children

        Returns:
            HTML string with group container and rendered children
        """
        if not element.children:
            return ""

        wrapper_styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"width: {element.width}px",
            f"height: {element.height}px",
            f"z-index: {element.z_order}",
            f"overflow: visible",
        ]
        style_str = "; ".join(wrapper_styles)

        children_html = []
        for child in element.children:
            child_html = self._convert_group_child_absolute(child, element)
            if child_html:
                children_html.append(child_html)

        inner = "\n".join(children_html)
        return f'<div class="slide-element group-wrapper" style="{style_str}">{inner}</div>'

    def _convert_group_child_absolute(self, child, group: GroupElement) -> str:
        """Convert a child element within a group using group-relative pixel positions."""
        if child.element_type == "text":
            text_html = self.text_converter.convert(child)
            styles = [
                f"position: absolute",
                f"left: {child.left}px",
                f"top: {child.top}px",
                f"width: {child.width}px",
                f"height: {child.height}px",
                f"z-index: {child.z_order}",
                "overflow: hidden",
            ]
            # Add border/outline if present
            if child.line_color:
                border_width = child.line_width if child.line_width else 1
                styles.append(f"border: {border_width}px solid {child.line_color}")
            style_str = "; ".join(styles)
            return f'<div class="slide-element text-element" style="{style_str}">{text_html}</div>'

        elif child.element_type == "image":
            return self.image_converter.convert_with_wrapper(child)

        elif child.element_type == "shape":
            return self.shape_converter.convert(child)

        elif child.element_type == "group":
            if not child.children:
                return ""
            styles = [
                f"position: absolute",
                f"left: {child.left}px",
                f"top: {child.top}px",
                f"width: {child.width}px",
                f"height: {child.height}px",
                f"z-index: {child.z_order}",
                f"overflow: visible",
            ]
            style_str = "; ".join(styles)
            inner_parts = []
            for sub_child in child.children:
                sub_html = self._convert_group_child_absolute(sub_child, child)
                if sub_html:
                    inner_parts.append(sub_html)
            inner = "\n".join(inner_parts)
            return f'<div class="slide-element group-wrapper" style="{style_str}">{inner}</div>'

        elif child.element_type == "table":
            table_html = self.table_converter.convert(child)
            styles = [
                f"position: absolute",
                f"left: {child.left}px",
                f"top: {child.top}px",
                f"z-index: {child.z_order}",
            ]
            style_str = "; ".join(styles)
            return f'<div class="slide-element table-element" style="{style_str}">{table_html}</div>'

        return ""

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

        /* Multi-level bullet paragraphs */
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
