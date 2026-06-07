"""
PPTX Parser - parses PowerPoint files and extracts slide information.
"""

from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


@dataclass
class SlideElement:
    """Base class for slide elements."""
    element_type: str
    left: float
    top: float
    width: float
    height: float
    z_order: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TextElement(SlideElement):
    """Text element from a slide."""
    text: str
    font_name: Optional[str] = None
    font_size: Optional[float] = None
    bold: bool = False
    italic: bool = False
    color: Optional[str] = None
    is_title: bool = False
    level: int = 0


@dataclass
class TableElement(SlideElement):
    """Table element from a slide."""
    rows: int
    cols: int
    data: List[List[str]]
    cell_styles: List[List[Dict[str, Any]]] = field(default_factory=list)


@dataclass
class ImageElement(SlideElement):
    """Image element from a slide."""
    image_bytes: bytes
    image_type: str
    alt_text: str = ""


@dataclass
class ShapeElement(SlideElement):
    """Shape element from a slide."""
    shape_type: str
    fill_color: Optional[str] = None
    line_color: Optional[str] = None
    text: Optional[str] = None


@dataclass
class ParsedSlide:
    """Represents a parsed slide with all its elements."""
    slide_number: int
    layout_name: str
    width: float
    height: float
    elements: List[SlideElement] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class PPTXParser:
    """
    Parser for PPTX files that extracts slide structure and elements.
    """

    def __init__(self, pptx_path: str | Path):
        """
        Initialize the parser with a PPTX file path.

        Args:
            pptx_path: Path to the PowerPoint file
        """
        self.pptx_path = Path(pptx_path)
        self.presentation: Optional[Presentation] = None
        self.slides: List[ParsedSlide] = []

    def parse(self) -> List[ParsedSlide]:
        """
        Parse the PPTX file and return list of parsed slides.

        Returns:
            List of parsed slides with their elements
        """
        self.presentation = Presentation(str(self.pptx_path))
        self.slides = []

        for slide_idx, slide in enumerate(self.presentation.slides, start=1):
            parsed_slide = self._parse_slide(slide, slide_idx)
            self.slides.append(parsed_slide)

        return self.slides

    def _parse_slide(self, slide, slide_number: int) -> ParsedSlide:
        """
        Parse a single slide and extract all elements.

        Args:
            slide: python-pptx slide object
            slide_number: Slide number (1-indexed)

        Returns:
            ParsedSlide object with all elements
        """
        # Get slide dimensions
        slide_width = slide.slide_width
        slide_height = slide.slide_height

        # Get layout name
        layout_name = slide.slide_layout.name if slide.slide_layout else "Blank"

        parsed_slide = ParsedSlide(
            slide_number=slide_number,
            layout_name=layout_name,
            width=slide_width,
            height=slide_height,
        )

        # Extract all shapes from the slide
        for z_order, shape in enumerate(slide.shapes):
            element = self._parse_shape(shape, z_order)
            if element:
                parsed_slide.elements.append(element)

        return parsed_slide

    def _parse_shape(self, shape, z_order: int) -> Optional[SlideElement]:
        """
        Parse a single shape and return the appropriate element.

        Args:
            shape: python-pptx shape object
            z_order: Z-order of the shape

        Returns:
            SlideElement or None if shape type is not supported
        """
        left = shape.left
        top = shape.top
        width = shape.width
        height = shape.height

        # Placeholder shape
        if shape.is_placeholder:
            return self._parse_placeholder(shape, left, top, width, height, z_order)

        # Text box
        if hasattr(shape, "text_frame") and shape.text_frame:
            return self._parse_text_box(shape, left, top, width, height, z_order)

        # Table
        if shape.shape_type == MSO_SHAPE_TYPE.TABLE:
            return self._parse_table(shape, left, top, width, height, z_order)

        # Picture/Image
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            return self._parse_image(shape, left, top, width, height, z_order)

        # Group shape
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            return self._parse_group(shape, z_order)

        # Generic shape
        return self._parse_generic_shape(shape, left, top, width, height, z_order)

    def _parse_placeholder(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> TextElement:
        """Parse a placeholder shape."""
        text = shape.text_frame.text if hasattr(shape, "text_frame") else ""

        # Determine if this is a title
        is_title = shape.placeholder_format.type in (
            0,  # Title
            14,  # Centered Title
        ) if hasattr(shape, "placeholder_format") else False

        return TextElement(
            element_type="text",
            left=left,
            top=top,
            width=width,
            height=height,
            z_order=z_order,
            text=text,
            is_title=is_title,
            metadata={"placeholder_type": shape.placeholder_format.type if hasattr(shape, "placeholder_format") else None}
        )

    def _parse_text_box(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> TextElement:
        """Parse a text box shape."""
        text_frame = shape.text_frame
        text = text_frame.text

        # Extract formatting from first paragraph
        font_name = None
        font_size = None
        bold = False
        italic = False
        color = None

        if text_frame.paragraphs:
            first_para = text_frame.paragraphs[0]
            if first_para.runs:
                first_run = first_para.runs[0]
                font = first_run.font
                font_name = font.name
                font_size = font.size.pt if font.size else None
                bold = font.bold
                italic = font.italic
                if font.color and font.color.type == 1:  # RGB color
                    color = f"#{font.color.rgb:06X}"

        return TextElement(
            element_type="text",
            left=left,
            top=top,
            width=width,
            height=height,
            z_order=z_order,
            text=text,
            font_name=font_name,
            font_size=font_size,
            bold=bold,
            italic=italic,
            color=color,
        )

    def _parse_table(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> TableElement:
        """Parse a table shape."""
        table = shape.table
        rows = len(table.rows)
        cols = len(table.columns)

        # Extract table data
        data = []
        cell_styles = []

        for row in table.rows:
            row_data = []
            row_styles = []
            for cell in row:
                row_data.append(cell.text)
                # Extract cell styling
                cell_style = {
                    "background_color": None,
                    "border_color": None,
                }
                if cell.fill and cell.fill.fore_color:
                    cell_style["background_color"] = f"#{cell.fill.fore_color.rgb:06X}" if cell.fill.fore_color.rgb else None
                row_styles.append(cell_style)
            data.append(row_data)
            cell_styles.append(row_styles)

        return TableElement(
            element_type="table",
            left=left,
            top=top,
            width=width,
            height=height,
            z_order=z_order,
            rows=rows,
            cols=cols,
            data=data,
            cell_styles=cell_styles,
        )

    def _parse_image(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> ImageElement:
        """Parse an image shape."""
        # Get image bytes
        image = shape.image
        image_bytes = image.blob
        image_type = image.ext

        # Get alt text
        alt_text = "" if not hasattr(shape, "alt_text") else shape.alt_text

        return ImageElement(
            element_type="image",
            left=left,
            top=top,
            width=width,
            height=height,
            z_order=z_order,
            image_bytes=image_bytes,
            image_type=image_type,
            alt_text=alt_text,
        )

    def _parse_group(self, shape, z_order: int) -> Optional[ShapeElement]:
        """Parse a group shape (simplified - returns None for MVP)."""
        # For MVP, we skip group shapes
        # In future, we could recursively parse grouped shapes
        return None

    def _parse_generic_shape(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> Optional[ShapeElement]:
        """Parse a generic shape (rectangles, circles, lines, etc.)."""
        # Get shape type name
        shape_type = shape.shape_type.name if hasattr(shape.shape_type, "name") else str(shape.shape_type)

        # Extract fill and line colors
        fill_color = None
        line_color = None

        if shape.fill and shape.fill.type == 1:  # Solid fill
            if hasattr(shape.fill, "fore_color") and shape.fill.fore_color.rgb:
                fill_color = f"#{shape.fill.fore_color.rgb:06X}"

        if shape.line and shape.line.color and shape.line.color.rgb:
            line_color = f"#{shape.line.color.rgb:06X}"

        # Check if shape has text
        text = None
        if hasattr(shape, "text_frame") and shape.text_frame:
            text = shape.text_frame.text

        return ShapeElement(
            element_type="shape",
            left=left,
            top=top,
            width=width,
            height=height,
            z_order=z_order,
            shape_type=shape_type,
            fill_color=fill_color,
            line_color=line_color,
            text=text,
        )

    def get_presentation_metadata(self) -> Dict[str, Any]:
        """
        Get metadata about the presentation.

        Returns:
            Dictionary with presentation metadata
        """
        if not self.presentation:
            return {}

        return {
            "title": self.presentation.core_properties.title or "",
            "author": self.presentation.core_properties.author or "",
            "subject": self.presentation.core_properties.subject or "",
            "created": str(self.presentation.core_properties.created) if self.presentation.core_properties.created else "",
            "modified": str(self.presentation.coreProperties.modified) if hasattr(self.presentation.coreProperties, "modified") and self.presentation.coreProperties.modified else "",
            "slide_count": len(self.presentation.slides),
            "slide_width": self.presentation.slide_width,
            "slide_height": self.presentation.slide_height,
        }
