"""svg_to_pptx — SVG to PPTX conversion package (vendored from ppt-master).

Original project: ppt-master by Hugo He (MIT, 2025-2026).
https://github.com/.../ppt-master

Public API (subset relevant to shuttleslide integration):
    - convert_element(): Convert a single SVG element to a ShapeResult
      (DrawingML XML + EMU bounds).
    - convert_svg_to_slide_shapes(): Convert a whole-slide SVG file to a
      complete slide XML (used by ppt-master's full pipeline; not needed
      for shuttleslide's embed-as-element use case but kept available).
    - ConvertContext, ShapeResult: state types passed through the
      conversion pipeline.

The original package also exposes ``main`` (CLI) and
``create_pptx_with_native_svg`` (full PPTX assembly); those pull in
optional dependencies (animation config, narration, media cache) that
shuttleslide does not use, so they are intentionally not re-exported
here. Import them directly from ``._vendored.svg_to_pptx.pptx_cli`` or
``.pptx_builder`` if needed.
"""

from .drawingml_context import ConvertContext, ShapeResult
from .drawingml_converter import (
    convert_element,
    convert_svg_to_slide_shapes,
    collect_defs,
)

__all__ = [
    "ConvertContext",
    "ShapeResult",
    "collect_defs",
    "convert_element",
    "convert_svg_to_slide_shapes",
]
