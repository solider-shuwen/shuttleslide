"""
Layout inference and background building for rule-based pipeline.

Determines the slide layout preset from classified element positions
and constructs BackgroundDef from Playwright-extracted background data.
"""

from __future__ import annotations

from typing import Optional

from shuttleslide.html_to_pptx.schema import BackgroundDef
from shuttleslide.html_to_pptx.rule.converter import _gradient, _bg_from_data


# ---------------------------------------------------------------------------
# Background builder
# ---------------------------------------------------------------------------

def build_background(bg_data: dict) -> Optional[BackgroundDef]:
    """Build BackgroundDef from analyze_html's background data.

    Args:
        bg_data: Dict with keys: color, gradient, image_url

    Returns:
        BackgroundDef or None if no meaningful background.
    """
    if not bg_data:
        return None

    return _bg_from_data(bg_data)
