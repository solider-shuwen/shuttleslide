"""
HTML layout extractor — renders HTML in Playwright and extracts precise layout data.

Produces a structured JSON dict with exact positions (as % of 1280x720),
computed styles (colors, fonts, gradients), and element metadata.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from playwright.async_api import Page

from shuttleslide.html_to_pptx.analyzer.browser import BrowserManager
from shuttleslide.html_to_pptx.html_utils import inline_svg_placeholders

logger = logging.getLogger(__name__)

# Load JS scripts at module level
_JS_DIR = Path(__file__).parent.parent / "js"
_EXTRACT_LAYOUT_JS = (_JS_DIR / "extract_layout.js").read_text(encoding="utf-8")
_EXTRACT_BACKGROUND_JS = (_JS_DIR / "extract_background.js").read_text(encoding="utf-8")
_EXTRACT_THEME_JS = (_JS_DIR / "extract_theme.js").read_text(encoding="utf-8")


async def analyze_html(
    html: str,
    browser: BrowserManager,
    base_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Render HTML in a headless browser and extract precise layout data.

    Args:
        html: The HTML string of a single slide.
        browser: A started BrowserManager instance.
        base_dir: Directory the slide's relative URLs (e.g.
            ``svgs/slide_1_hero.svg``) resolve against. Typically the
            slide HTML file's parent directory. When provided, SVG
            placeholders of the form
            ``<img class="shuttleslide-svg-placeholder" src="svgs/...">``
            are inlined into real ``<svg>`` markup before the page is
            loaded. When None, the HTML is rendered as-is — this
            preserves backward compatibility for callers supplying HTML
            with inline SVG already in place.

    Returns:
        A dict with keys: slide_size, background, elements[]
        - slide_size: {"width": 1280, "height": 720}
        - background: {"color", "gradient", "image_url"}
        - elements: list of element dicts with rect_pct, styles, tag, classes, etc.
    """
    if base_dir is not None:
        html = inline_svg_placeholders(html, base_dir)

    page: Page = await browser.new_page()
    try:
        # Set HTML content and wait for everything to load
        await page.set_content(html, wait_until="networkidle", timeout=15000)

        # Extra wait for fonts/icons to render
        await page.wait_for_timeout(500)

        # Extract background
        bg_data = await page.evaluate(_EXTRACT_BACKGROUND_JS)

        # Extract layout elements
        layout_data = await page.evaluate(_EXTRACT_LAYOUT_JS)

        # Extract theme
        theme_data = await page.evaluate(_EXTRACT_THEME_JS)

        # Combine into result
        result = {
            "slide_size": layout_data.get("slide_size", {"width": 1280, "height": 720}),
            "background": {
                "color": bg_data.get("color"),
                "gradient": bg_data.get("gradient"),
                "image_url": bg_data.get("image_url"),
            },
            "theme": theme_data,
            "slide_rect_px": layout_data.get("slide_rect_px"),
            "element_count": layout_data.get("element_count", 0),
            "elements": layout_data.get("elements", []),
        }

        logger.info(
            "Extracted %d elements from slide (%dx%d)",
            result["element_count"],
            result["slide_size"]["width"],
            result["slide_size"]["height"],
        )

        return result

    except Exception as e:
        logger.error("Failed to analyze HTML: %s", e)
        raise
    finally:
        await page.close()
