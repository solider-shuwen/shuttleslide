"""
Image Converter - converts image elements to HTML.
"""

import base64
import os
from typing import Optional, Dict, Any
from shuttleslide.pptx_to_html.models import ImageElement


class ImageConverter:
    """
    Converts image elements from PPTX to HTML.
    """

    def __init__(self, use_base64: bool = False, output_dir: Optional[str] = None):
        """
        Initialize the image converter.

        Args:
            use_base64: If True, embed images as base64. If False, save as separate files (default).
            output_dir: Directory path for saving image files (relative to HTML file).
                       If None and use_base64=False, creates "{html_filename}_assets/images/" directory.
        """
        self.use_base64 = use_base64
        self.output_dir = output_dir
        self.image_counter = 0
        self._created_dirs = set()  # Track created directories to avoid redundant calls

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

        # Apply CSS based on PPT fill mode read from <a:blipFill>.
        # stretch: image fills shape completely (may distort).
        # tile: image repeats at natural size.
        # none: image centered at natural size, contained within shape.
        if element.fill_mode == "stretch":
            styles = [
                "width: 100%",
                "height: 100%",
            ]
        elif element.fill_mode == "tile":
            styles = [
                "width: 100%",
                "height: 100%",
                "object-fit: none",
            ]
        else:
            styles = [
                "max-width: 100%",
                "max-height: 100%",
                "object-fit: contain",
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
        Build file path for the image and save the image file.

        Args:
            element: ImageElement with image bytes

        Returns:
            Relative file path string for HTML src attribute
        """
        if not element.image_bytes:
            return self._build_base64_src(element)

        # Determine file extension
        ext = self._get_file_extension(element.image_type)

        # Generate unique filename
        filename = f"image-{self.image_counter}{ext}"
        self.image_counter += 1

        # Create assets directory if needed
        if self.output_dir is None:
            # Use default assets directory
            assets_dir = os.path.join("output_assets", "images")
        else:
            assets_dir = os.path.join(self.output_dir, "images")

        # Create directory if it doesn't exist
        if assets_dir not in self._created_dirs:
            os.makedirs(assets_dir, exist_ok=True)
            self._created_dirs.add(assets_dir)

        # Save image file
        filepath = os.path.join(assets_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(element.image_bytes)

        # Return relative path for HTML (use forward slashes for web compatibility)
        return f"{assets_dir.replace(os.sep, '/')}/{filename}"

    def _get_file_extension(self, image_type: str) -> str:
        """
        Get file extension for image format.

        Args:
            image_type: Image file extension (e.g., "png", "jpeg")

        Returns:
            File extension with dot (e.g., ".png")
        """
        # Handle common formats
        if image_type.lower() in ['png', 'jpeg', 'jpg', 'gif', 'bmp', 'webp']:
            return f".{image_type.lower()}"
        elif image_type.lower() in ['emf', 'wmf']:
            # For metafiles, convert to PNG
            return ".png"
        else:
            # Default to PNG
            return ".png"

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

    def convert_with_wrapper(self, element: ImageElement, pct: Dict[str, float] = None) -> str:
        """
        Convert image with a wrapper div for better positioning control.

        Args:
            element: ImageElement to convert

        Returns:
            HTML string with wrapper div
        """
        img_html = self.convert(element)

        # Use percentage positioning if provided, otherwise use pixels
        if pct is not None:
            wrapper_styles = [
                f"position: absolute",
                f"left: {pct['left_pct']:.3f}%",
                f"top: {pct['top_pct']:.3f}%",
                f"width: {pct['width_pct']:.3f}%",
                f"height: {pct['height_pct']:.3f}%",
                f"z-index: {element.z_order}",
            ]
        else:
            wrapper_styles = [
                f"position: absolute",
                f"left: {element.left}px",
                f"top: {element.top}px",
                f"width: {element.width}px",
                f"height: {element.height}px",
                f"z-index: {element.z_order}",
            ]

        wrapper_attrs = [
            f'class="image-wrapper"',
        ]

        # Apply scene3D CSS transform (approximate isometric effects)
        # PPT uses orthographic (parallel) projection for isometric cameras —
        # no perspective/vanishing point. We use perspective(99999px) to
        # approximate orthographic projection.
        #
        # Isometric tilt angle: arctan(1/sqrt(2)) ≈ 35.264° (standard)
        #
        # Note: PPT "scale" (scale_w/scale_h) is NOT applied here because the
        # image is already displayed at the shape's dimensions via the wrapper
        # div sizing and img { width/height: 100% }. Adding CSS scaleX/Y would
        # double-count the stretching.
        if element.metadata and element.metadata.get('scene3d_camera'):
            camera = element.metadata['scene3d_camera']
            if camera == 'isometricRightUp':
                wrapper_styles.append("transform: perspective(99999px) rotateX(35.264deg) rotateY(-45deg)")
            elif camera == 'isometricLeftUp':
                wrapper_styles.append("transform: perspective(99999px) rotateX(35.264deg) rotateY(45deg)")
            elif camera == 'isometricTopUp':
                wrapper_styles.append("transform: perspective(99999px) rotateX(54.736deg) rotateZ(45deg)")
            elif camera == 'isometricRightDown':
                wrapper_styles.append("transform: perspective(99999px) rotateX(-35.264deg) rotateY(-45deg)")
            elif camera == 'isometricLeftDown':
                wrapper_styles.append("transform: perspective(99999px) rotateX(-35.264deg) rotateY(45deg)")
            elif camera == 'isometricBottomUp':
                wrapper_styles.append("transform: perspective(99999px) rotateX(-54.736deg) rotateZ(45deg)")

        wrapper_attrs.append(f'style="{"; ".join(wrapper_styles)}"')
        wrapper_attrs.append(f'data-pptx-wrapper="image"')

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
