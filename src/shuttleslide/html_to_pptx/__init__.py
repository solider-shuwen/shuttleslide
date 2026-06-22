"""
HTML to PPTX conversion module.

Converts HTML presentations to PowerPoint files via a three-stage pipeline:
  1. HTML → Playwright rendering → precise layout data extraction
  2. Layout data → rule-based classifier → PPT-DSL JSON
  3. PPT-DSL JSON → PPTX (via python-pptx)

Known limitations
-----------------
* Tables are detected via **spatial grid alignment**, so both real
  ``<table>`` markup and div+flex+span "div-table" layouts render as a
  native PPTX table. Cell merging (colspan / rowspan), per-cell custom
  borders beyond row separators, and images / vertical text inside cells
  are not supported in this version — cells fall back to ``text_box``
  if the grid cannot be detected (e.g. misaligned columns).
* ``object-fit: scale-down`` is not implemented (falls back to ``fill``).
  ``cover`` and ``contain`` are supported.
* Per-side borders are honoured on cards only. Generic shapes still use
  the uniform ``border`` field.
"""

from shuttleslide.html_to_pptx.schema import (
    PresentationDSL,
    SlideDSL,
    ThemeDef,
    load_presentation,
    dump_presentation,
)
from shuttleslide.html_to_pptx.renderer import PPTXRenderer
from shuttleslide.html_to_pptx.rule import RuleSlideTransformer
from shuttleslide.html_to_pptx.image_utils import ImageCache
from shuttleslide.html_to_pptx.analyzer import analyze_html, BrowserManager
from shuttleslide.html_to_pptx.fonts import (
    parse_css_font_family,
    fetch_text_font_bytes,
)
from shuttleslide.html_to_pptx.font_embedder import embed_fonts

__all__ = [
    "PresentationDSL",
    "SlideDSL",
    "ThemeDef",
    "load_presentation",
    "dump_presentation",
    "PPTXRenderer",
    "RuleSlideTransformer",
    "ImageCache",
    "analyze_html",
    "BrowserManager",
    "parse_css_font_family",
    "fetch_text_font_bytes",
    "embed_fonts",
]
