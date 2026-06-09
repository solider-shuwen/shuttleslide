"""
Converters for PPTX element types to HTML.
"""

from shuttleslide.pptx_to_html.converters.text import TextConverter
from shuttleslide.pptx_to_html.converters.tables import TableConverter
from shuttleslide.pptx_to_html.converters.images import ImageConverter
from shuttleslide.pptx_to_html.converters.shapes import ShapeConverter
from shuttleslide.pptx_to_html.converters.svg_generator import SVGShapeGenerator

__all__ = [
    'TextConverter',
    'TableConverter',
    'ImageConverter',
    'ShapeConverter',
    'SVGShapeGenerator',
]
