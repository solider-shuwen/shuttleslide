"""
Base Layout - shared logic for all layout modes.
"""

import os
import base64
from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader

from shuttleslide.pptx_to_html.models import SlideElement, ParsedSlide
from shuttleslide.pptx_to_html.converters.text import TextConverter
from shuttleslide.pptx_to_html.converters.tables import TableConverter
from shuttleslide.pptx_to_html.converters.images import ImageConverter
from shuttleslide.pptx_to_html.converters.shapes import ShapeConverter
from shuttleslide.pptx_to_html.utils.units import scene3d_to_css


class BaseLayout:
    """
    Common base class for all layout modes.

    Handles converter initialization and provides element dispatch.
    Subclasses override element conversion methods for layout-specific positioning.
    """

    def __init__(self, use_base64: bool = False, output_dir: Optional[str] = None):
        """
        Initialize the base layout with converters.

        Args:
            use_base64: Whether to embed images as base64 (True) or save as separate files (False, default).
            output_dir: Directory for saving image assets. If None, uses default.
        """
        self.use_base64 = use_base64
        self.output_dir = output_dir
        self.text_converter = TextConverter(use_base64=use_base64, output_dir=output_dir)
        self.table_converter = TableConverter()
        self.image_converter = ImageConverter(use_base64=use_base64, output_dir=output_dir)
        self.shape_converter = ShapeConverter(use_base64=use_base64, output_dir=output_dir)

        # Initialize Jinja2 template environment
        template_dir = Path(__file__).parent.parent / "templates"
        self.env = Environment(loader=FileSystemLoader(template_dir))

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

    def _empty_html(self) -> str:
        """Return HTML for empty presentation."""
        return """<!DOCTYPE html>
<html>
<head>
    <meta charset='UTF-8'>
    <title>Empty Presentation</title>
</head>
<body style='display:flex;justify-content:center;align-items:center;height:100vh;margin:0;font-family:Arial,sans-serif;'>
    <div style='text-align:center'>
        <h1>Empty Presentation</h1>
        <p>No slides found in this presentation.</p>
    </div>
</body>
</html>"""

    @staticmethod
    def _remove_scene3d_recursive(children: List):
        """Remove scene3d_camera from all descendants recursively.

        When a group has scene3d, the transform is applied to the group wrapper.
        Children and nested descendants must not have individual scene3d transforms,
        otherwise they get double-transformed.
        """
        for child in children:
            if hasattr(child, 'metadata') and child.metadata and 'scene3d_camera' in child.metadata:
                del child.metadata['scene3d_camera']
            if hasattr(child, 'children') and child.children:
                BaseLayout._remove_scene3d_recursive(child.children)

    def _get_background_style(self, slide: ParsedSlide, slide_number: int = 0) -> Optional[str]:
        """
        Generate CSS background string from ParsedSlide.background.

        Args:
            slide: ParsedSlide with optional background data
            slide_number: Slide number used for unique background image filenames

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
            image_url = self._build_background_image_url(bg.image_data, slide_number)
            parts = [f'background-image: url("{image_url}")']
            parts.append('background-size: cover')

            if bg.overlay_color and bg.overlay_opacity is not None:
                r = int(bg.overlay_color[1:3], 16)
                g = int(bg.overlay_color[3:5], 16)
                b = int(bg.overlay_color[5:7], 16)
                rgba = f"rgba({r},{g},{b},{bg.overlay_opacity:.2f})"
                parts[0] = (
                    f'background-image: linear-gradient({rgba}, {rgba}), '
                    f'url("{image_url}")'
                )
            return "; ".join(parts)
        return None

    def _build_background_image_url(self, image_data: dict, slide_number: int) -> str:
        """
        Build image URL for slide background, respecting use_base64 setting.

        Args:
            image_data: Dict with 'image_type' and 'image_bytes'
            slide_number: Slide number for unique filename

        Returns:
            URL string (data URI or relative file path)
        """
        if self.use_base64:
            mime = f"image/{image_data['image_type']}"
            encoded = base64.b64encode(image_data['image_bytes']).decode('utf-8')
            return f"data:{mime};base64,{encoded}"

        # Save as file
        ext = f".{image_data['image_type'].lower()}"
        filename = f"bg-{slide_number}{ext}"

        if self.output_dir is None:
            assets_dir = os.path.join("output_assets", "images")
        else:
            assets_dir = os.path.join(self.output_dir, "images")

        os.makedirs(assets_dir, exist_ok=True)

        filepath = os.path.join(assets_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(image_data['image_bytes'])

        return os.path.join("output_assets", "images", filename).replace(os.sep, '/')

    @staticmethod
    def _build_text_decoration_styles(element) -> List[str]:
        """Build CSS style declarations for text decoration (everything except positioning).

        Handles: font, bold, italic, color, border, vertical alignment,
        overflow, white-space, writing-mode, scene3D, rotation.

        Uses getattr() to safely access attributes that may not exist on
        all element types (e.g. group children).

        Args:
            element: An element with text-related attributes (TextElement or group child)

        Returns:
            List of CSS declaration strings
        """
        styles = []

        # White-space preservation for text
        styles.append("white-space: pre-wrap")

        # Conditional overflow for multi-paragraph text
        paragraphs = getattr(element, 'paragraphs', None)
        if not paragraphs or len(paragraphs) <= 1:
            styles.append("overflow: hidden")

        # Vertical alignment using CSS flexbox (column direction for stacked paragraphs)
        vertical_align = getattr(element, 'vertical_align', None)
        if vertical_align == 'middle':
            styles.append("display: flex")
            styles.append("flex-direction: column")
            styles.append("justify-content: center")
        elif vertical_align == 'bottom':
            styles.append("display: flex")
            styles.append("flex-direction: column")
            styles.append("justify-content: flex-end")

        # Font styling
        font_name = getattr(element, 'font_name', None)
        if font_name:
            styles.append(f"font-family: '{font_name}', Arial, sans-serif")
        font_size = getattr(element, 'font_size', None)
        if font_size:
            styles.append(f"font-size: {font_size}pt")
        if getattr(element, 'bold', None):
            styles.append("font-weight: bold")
        if getattr(element, 'italic', None):
            styles.append("font-style: italic")
        color = getattr(element, 'color', None)
        if color:
            styles.append(f"color: {color}")

        # Border/outline
        line_color = getattr(element, 'line_color', None)
        if line_color:
            border_width = getattr(element, 'line_width', None) or 1
            styles.append(f"border: {border_width}px solid {line_color}")

        # Rotation and transform styles
        transform_parts = []

        # Handle vertical text (writing-mode)
        vert = getattr(element, 'vert', None)
        if vert:
            if vert == 'eaVert':
                styles.append("writing-mode: vertical-rl")
            elif vert == 'mongolianVert':
                styles.append("writing-mode: vertical-rl")
            elif vert == 'wordVert':
                styles.append("writing-mode: vertical-lr")

        # Apply scene3D CSS transform
        metadata = getattr(element, 'metadata', None)
        if metadata and metadata.get('scene3d_camera'):
            css_3d = scene3d_to_css(metadata['scene3d_camera'])
            if css_3d:
                transform_parts.append(css_3d)

        rotation = getattr(element, 'rotation', None)
        if rotation:
            transform_parts.append(f"rotate({rotation}deg)")

        # Combine all transforms
        if transform_parts:
            styles.append(f"transform: {' '.join(transform_parts)}")

        return styles

    @staticmethod
    def _build_text_wrapper_html(text_html: str, styles: List[str], css_class: str = "") -> str:
        """Combine styles and text content into a <div> wrapper.

        Args:
            text_html: Inner HTML content (output from text_converter)
            styles: List of CSS declaration strings
            css_class: Optional CSS class name(s)

        Returns:
            HTML div element string
        """
        style_str = "; ".join(styles)
        class_attr = f' class="{css_class}"' if css_class else ""
        return f'<div{class_attr} style="{style_str}">{text_html}</div>'
