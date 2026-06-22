"""
RuleSlideTransformer — converts HTML slides to PresentationDSL deterministically.

Pipeline: HTML -> Playwright (extract layout) -> Python Rules (classify) -> DSL

The full HTML document (with <head> CSS) is passed to Playwright for rendering,
ensuring layout positions are accurate. Classification uses deterministic
Python rules — no LLM, no network calls, fully offline.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional

from shuttleslide.html_to_pptx.analyzer import BrowserManager, analyze_html
from shuttleslide.html_to_pptx.html_utils import split_html_slides
from shuttleslide.html_to_pptx.schema import (
    PresentationDSL,
    SlideDSL,
    ThemeDef,
    BackgroundDef,
)
from shuttleslide.html_to_pptx.rule.containment import build_containment_tree
from shuttleslide.html_to_pptx.rule.classifier import classify_elements
from shuttleslide.html_to_pptx.rule.converter import convert_to_dsl
from shuttleslide.html_to_pptx.rule.layout import build_background
from shuttleslide.html_to_pptx.fonts import resolve_icon_fonts

logger = logging.getLogger(__name__)


# Default theme
_DEFAULT_THEME = {
    "primary_color": "#133EFF",
    "accent_color": "#00CD82",
    "warn_color": "#FF5722",
    "bg_color": "#FEFEFE",
    "text_color": "#1F2937",
    "font_title": "Roboto",
    "font_body": "Roboto",
}


def _extract_head(html: str) -> str:
    """Extract the <head>...</head> section from a full HTML document."""
    m = re.search(r"<head[^>]*>(.+?)</head>", html, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(0)
    return ""


def _count_ppt_slides(html: str) -> int:
    """Count the number of .ppt-slide elements in HTML."""
    return len(re.findall(r'class=["\'][^"\']*ppt-slide', html))


def _split_preserve_head(html: str) -> List[str]:
    """Split HTML into per-slide pages, each preserving the full <head>.

    For single-slide documents: return the original HTML as-is.
    For multi-slide: wrap each slide's content with the shared <head>.
    """
    n_slides = _count_ppt_slides(html)
    if n_slides <= 1:
        return [html]

    head_section = _extract_head(html)
    slides_content = split_html_slides(html)
    result = []
    for content in slides_content:
        page = f"<!DOCTYPE html><html><head>{head_section}</head><body>{content}</body></html>"
        result.append(page)
    return result


class RuleSlideTransformer:
    """Converts HTML slides to PresentationDSL via Playwright + rule-based classification.

    Usage::

        transformer = RuleSlideTransformer()
        # Pass base_dir so <img class="shuttleslide-svg-placeholder">
        # and relative <img src="images/..."> resolve against the HTML
        # file's parent directory.
        dsl = await transformer.transform_html(
            html_string, base_dir=html_path.parent,
        )
        renderer = PPTXRenderer(base_dir=html_path.parent)
        renderer.render(dsl, "output.pptx")
    """

    def __init__(self):
        self.browser_mgr = BrowserManager()

    async def transform_html(
        self,
        html: str,
        verbose: bool = False,
        base_dir: Optional[Path] = None,
    ) -> PresentationDSL:
        """Transform a complete HTML document into a PresentationDSL.

        Args:
            html: HTML string containing one or more slides.
            verbose: If True, log detailed classification info.
            base_dir: Directory the slide's relative URLs (svgs/...,
                images/...) resolve against. Typically the HTML file's
                parent directory. When provided, SVG placeholders of
                the form ``<img class="shuttleslide-svg-placeholder">``
                are inlined into real ``<svg>`` markup before each
                slide is rendered by Playwright. When None, the HTML
                is processed as-is (backward compatible with HTML that
                already has inline SVG).

        Returns:
            PresentationDSL ready for PPTXRenderer.
        """
        await self.browser_mgr.start()
        try:
            return await self._do_transform(html, verbose, base_dir)
        finally:
            await self.browser_mgr.stop()

    async def _do_transform(
        self,
        html: str,
        verbose: bool,
        base_dir: Optional[Path] = None,
    ) -> PresentationDSL:
        # Resolve icon fonts early so font data is available for vector rendering
        resolve_icon_fonts(html)

        # Split into per-slide HTML pages with preserved <head> CSS
        slides_html = _split_preserve_head(html)

        if verbose:
            logger.info("Processing %d slide(s)", len(slides_html))

        theme_def = None
        all_slides: List[SlideDSL] = []

        for i, slide_html in enumerate(slides_html):
            if verbose:
                logger.info("Slide %d/%d: extracting layout...", i + 1, len(slides_html))

            # 1. Playwright extraction (full HTML with CSS)
            layout_data = await analyze_html(slide_html, self.browser_mgr, base_dir=base_dir)

            # Extract theme from first slide
            if theme_def is None:
                theme_dict = {**_DEFAULT_THEME, **layout_data.get("theme", {})}
                theme_def = ThemeDef(**{
                    k: v for k, v in theme_dict.items()
                    if k in ThemeDef.__dataclass_fields__
                })

            # 2. Build containment tree
            elements = layout_data.get("elements", [])
            tree = build_containment_tree(elements)

            if verbose:
                logger.info(
                    "  %d elements, %d containers detected",
                    len(elements),
                    sum(1 for idx in range(len(elements)) if tree.is_container(idx)),
                )

            # 3. Classify elements
            classified = classify_elements(elements, tree)

            if verbose:
                type_counts = {}
                for ce in classified:
                    type_counts[ce.elem_type] = type_counts.get(ce.elem_type, 0) + 1
                logger.info("  Classified: %s", type_counts)

            # 4. Convert to DSL dataclasses
            dsl_elements = convert_to_dsl(classified, elements, tree)

            # 5. Build background
            background = build_background(layout_data.get("background"))

            # 6. Assemble slide
            slide_dsl = SlideDSL(
                background=background,
                elements=dsl_elements,
            )
            all_slides.append(slide_dsl)

        if theme_def is None:
            theme_def = ThemeDef()

        return PresentationDSL(theme=theme_def, slides=all_slides)
