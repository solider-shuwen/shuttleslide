"""
PPT Layout - converts slides to HTML with a PPT-style editor interface.
Left sidebar with thumbnails, main panel with current slide, and fullscreen play mode.
"""

from typing import List, Optional

from shuttleslide.pptx_to_html.models import ParsedSlide, SlideElement, TextElement, GroupElement
from shuttleslide.pptx_to_html.layouts.base import BaseLayout
from shuttleslide.pptx_to_html.utils.units import scene3d_to_css


class PPTLayout(BaseLayout):
    """
    Generates HTML with a PPT-style editor interface.
    Features: left thumbnail sidebar, main slide panel, fullscreen play mode.
    """

    def __init__(self, use_base64: bool = False, output_dir: str = None):
        """
        Initialize the PPT layout with converters and templates.

        Args:
            use_base64: Whether to embed images as base64 (True) or save as separate files (False, default).
            output_dir: Directory for saving image assets relative to the output HTML.
        """
        super().__init__(use_base64=use_base64, output_dir=output_dir)

    def convert(self, slides: List[ParsedSlide]) -> str:
        """
        Convert slides to HTML with PPT-style editor interface using templates.

        Args:
            slides: List of parsed slides

        Returns:
            Complete HTML document string
        """
        if not slides:
            return self._empty_html()

        # Get presentation dimensions from first slide
        first_slide = slides[0]
        slide_width = int(first_slide.width)
        slide_height = int(first_slide.height)

        # Prepare slides with HTML content
        slides_context = []
        for slide in slides:
            # Sort elements by z-order and convert each
            sorted_elements = sorted(slide.elements, key=lambda e: e.z_order)
            elements_html = []
            for element in sorted_elements:
                element_html = self._convert_element_absolute(element)
                if element_html:
                    elements_html.append({"html": element_html})

            slide_context = {
                "slide_number": slide.slide_number,
                "elements": elements_html,
            }

            # Add background style if present
            bg_style = self._get_background_style(slide, slide.slide_number)
            if bg_style:
                slide_context["background_style"] = bg_style

            slides_context.append(slide_context)

        # Render templates
        css_template = self.env.get_template("pptview.css")
        styles = css_template.render(
            slide_width=slide_width,
            slide_height=slide_height,
        )

        js_template = self.env.get_template("pptview.js")
        script = js_template.render(
            slide_width=slide_width,
            slide_height=slide_height,
            slides=slides_context,
        )

        html_template = self.env.get_template("pptview.html")
        return html_template.render(
            title="Presentation",
            slides=slides_context,
            slide_width=slide_width,
            slide_height=slide_height,
            styles=styles,
            script=script,
        )

    def _render_slide_elements(self, slide: ParsedSlide) -> str:
        """
        Render all elements of a slide as HTML string (without section wrapper).
        Used by FlowLayout which needs the raw element HTML.

        Args:
            slide: ParsedSlide to render

        Returns:
            HTML string of all sorted elements
        """
        sorted_elements = sorted(slide.elements, key=lambda e: e.z_order)
        parts = []
        for element in sorted_elements:
            element_html = self._convert_element_absolute(element)
            if element_html:
                parts.append(element_html)
        return "\n".join(parts)

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

        # Build position styles (px-based)
        position_styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"width: {element.width}px",
            f"height: {element.height}px",
            f"z-index: {element.z_order}",
        ]

        # Add decoration styles from shared helper
        decoration_styles = self._build_text_decoration_styles(element)

        all_styles = position_styles + decoration_styles
        return self._build_text_wrapper_html(text_html, all_styles)

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

        wrapper_styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"width: {element.width}px",
            f"height: {element.height}px",
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
        For groups with scene3d, applies the 3D transform to the wrapper.

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

        # Apply scene3d transform if present
        if element.metadata and element.metadata.get('scene3d_camera'):
            css_3d = scene3d_to_css(element.metadata['scene3d_camera'])
            if css_3d:
                wrapper_styles.append(f"transform: {css_3d}")
            wrapper_styles.append("transform-style: preserve-3d")
            # Remove scene3d from all descendants so they don't get individual transforms
            self._remove_scene3d_recursive(element.children)

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

            # Position styles
            position_styles = [
                f"position: absolute",
                f"left: {child.left}px",
                f"top: {child.top}px",
                f"width: {child.width}px",
                f"height: {child.height}px",
                f"z-index: {child.z_order}",
            ]

            # Decoration styles from shared helper
            decoration_styles = self._build_text_decoration_styles(child)

            all_styles = position_styles + decoration_styles
            return self._build_text_wrapper_html(text_html, all_styles, "slide-element text-element")

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

            # Apply scene3d transform if present
            if hasattr(child, 'metadata') and child.metadata and child.metadata.get('scene3d_camera'):
                css_3d = scene3d_to_css(child.metadata['scene3d_camera'])
                if css_3d:
                    styles.append(f"transform: {css_3d}")
                styles.append("transform-style: preserve-3d")
                self._remove_scene3d_recursive(child.children)

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

    def convert_slides_to_slides_html(self, slides: List[ParsedSlide]) -> List[str]:
        """
        Convert slides to individual HTML strings (one per slide).

        Args:
            slides: List of parsed slides

        Returns:
            List of HTML strings, one per slide
        """
        results = []
        for slide in slides:
            sorted_elements = sorted(slide.elements, key=lambda e: e.z_order)
            parts = []
            for element in sorted_elements:
                element_html = self._convert_element_absolute(element)
                if element_html:
                    parts.append(element_html)
            results.append("\n".join(parts))
        return results
