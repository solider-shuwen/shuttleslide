"""
Image Converter - converts image elements to HTML.
"""

import base64
from typing import Optional
from shuttleslide.pptx_to_html.parser import ImageElement


class ImageConverter:
    """
    Converts image elements from PPTX to HTML.
    """

    def __init__(self, use_base64: bool = True):
        """
        Initialize the image converter.

        Args:
            use_base64: If True, embed images as base64. If False, save as separate files.
        """
        self.use_base64 = use_base64
        self.image_counter = 0

    def convert(self, element: ImageElement) -> str:
        """
        Convert an image element to HTML.

        Args:
            element: ImageElement to convert

        Returns:
            HTML string representation
        """
        if not element.image_bytes:
            return ""

        # Build image attributes
        attrs = []

        # Add styling
        styles = [
            f"width: {element.width}px",
            f"height: {element.height}px",
        ]
        attrs.append(f'style="{"; ".join(styles)}"')

        # Add alt text
        if element.alt_text:
            attrs.append(f'alt="{element.alt_text}"')

        # Add data attributes for round-trip
        attrs.append(f'data-pptx-element-type="image"')
        attrs.append(f'data-pptx-left="{element.left}"')
        attrs.append(f'data-pptx-top="{element.top}"')
        attrs.append(f'data-pptx-width="{element.width}"')
        attrs.append(f'data-pptx-height="{element.height}"')
        attrs.append(f'data-pptx-z-order="{element.z_order}"')
        attrs.append(f'data-pptx-image-type="{element.image_type}"')

        # Build src attribute
        if self.use_base64:
            src = self._build_base64_src(element)
        else:
            src = self._build_file_src(element)

        attrs.append(f'src="{src}"')

        attr_str = " ".join(attrs)

        return f"<img {attr_str} />"

    def _build_base64_src(self, element: ImageElement) -> str:
        """
        Build base64 data URI for the image.

        Args:
            element: ImageElement with image bytes

        Returns:
            Data URI string
        """
        # Determine MIME type
        mime_type = self._get_mime_type(element.image_type)

        # Encode to base64
        encoded = base64.b64encode(element.image_bytes).decode("utf-8")

        return f"data:{mime_type};base64,{encoded}"

    def _build_file_src(self, element: ImageElement) -> str:
        """
        Build file path for the image (not implemented in MVP).

        Args:
            element: ImageElement

        Returns:
            File path string
        """
        # For MVP, we just use base64
        # In future, this would save to a file and return the path
        return self._build_base64_src(element)

    def _get_mime_type(self, image_type: str) -> str:
        """
        Get MIME type for image format.

        Args:
            image_type: Image file extension (e.g., "png", "jpeg")

        Returns:
            MIME type string
        """
        mime_types = {
            "png": "image/png",
            "jpeg": "image/jpeg",
            "jpg": "image/jpeg",
            "gif": "image/gif",
            "bmp": "image/bmp",
            "tiff": "image/tiff",
            "webp": "image/webp",
            "emf": "image/x-emf",  # Enhanced Metafile
            "wmf": "image/x-wmf",  # Windows Metafile
        }

        return mime_types.get(image_type.lower(), "image/png")

    def convert_with_wrapper(self, element: ImageElement) -> str:
        """
        Convert image with a wrapper div for better positioning control.

        Args:
            element: ImageElement to convert

        Returns:
            HTML string with wrapper div
        """
        img_html = self.convert(element)

        wrapper_styles = [
            f"position: absolute",
            f"left: {element.left}px",
            f"top: {element.top}px",
            f"width: {element.width}px",
            f"height: {element.height}px",
            f"z-index: {element.z_order}",
        ]

        wrapper_attrs = [
            f'style="{"; ".join(wrapper_styles)}"',
            f'data-pptx-wrapper="image"',
        ]

        wrapper_attr_str = " ".join(wrapper_attrs)

        return f"<div {wrapper_attr_str}>{img_html}</div>"

    @staticmethod
    def get_image_dimensions(element: ImageElement) -> tuple[int, int]:
        """
        Get the display dimensions of an image.

        Args:
            element: ImageElement

        Returns:
            Tuple of (width, height) in pixels
        """
        return int(element.width), int(element.height)

    @staticmethod
    def calculate_aspect_ratio(width: float, height: float) -> float:
        """
        Calculate the aspect ratio of an image.

        Args:
            width: Image width
            height: Image height

        Returns:
            Aspect ratio (width / height)
        """
        if height == 0:
            return 1.0
        return width / height
