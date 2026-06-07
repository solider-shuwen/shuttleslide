"""
Shuttleslide - Bidirectional PPTX ↔ HTML conversion library.

This library provides tools for converting PowerPoint (PPTX) files to HTML
and back, with a focus on round-trip format preservation.
"""

__version__ = "0.1.0"

from shuttleslide.pptx_to_html import PPTXToHTMLConverter

__all__ = ["PPTXToHTMLConverter", "__version__"]
