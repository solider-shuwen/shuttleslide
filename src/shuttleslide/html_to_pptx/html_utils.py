"""
HTML utilities for the html_to_pptx pipeline.

Shared helpers for splitting and slicing raw HTML documents during slide
extraction. Currently used by the rule-based transformer and the SVG
placeholder inliner.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import List, Optional


logger = logging.getLogger(__name__)


def split_html_slides(html: str) -> List[str]:
    """Split a full HTML document into individual slide HTML strings.

    Recognizes `<div class="ppt-slide">` containers and returns each slide's
    inner HTML. Falls back to the `<body>` content if no slide containers are
    found, and finally to the original HTML if no `<body>` exists.
    """
    slides = []

    pattern = re.compile(
        r'<div[^>]*class=["\'][^"\']*ppt-slide[^"\']*["\'][^>]*>(.+?)</body>',
        re.DOTALL,
    )
    matches = pattern.findall(html)
    if matches:
        for match in matches:
            slides.append(match.strip())
        return slides

    pattern2 = re.compile(
        r'<div[^>]*class=["\'][^"\']*ppt-slide[^"\']*["\'][^>]*>(.+)',
        re.DOTALL,
    )
    matches2 = pattern2.findall(html)
    if matches2:
        slides = [m.strip().rstrip('</div>').strip() for m in matches2]
        return slides

    body_match = re.search(r'<body[^>]*>(.+?)</body>', html, re.DOTALL)
    if body_match:
        slides = [body_match.group(1).strip()]
    else:
        slides = [html]

    return slides


# Matches a complete <img ...> tag (self-closing or not) that carries the
# shuttleslide-svg-placeholder class. We require this explicit class marker
# so the inliner never touches user-authored <img src="*.svg"> references
# (which would otherwise be a valid way to embed SVG as a raster image).
# Attribute order is not guaranteed — the regex scans the full tag body
# for class=, src=, and data-slot= separately.
_SVG_PLACEHOLDER_IMG_RE = re.compile(
    r"<img\b[^>]*\bclass\s*=\s*[\"'][^\"'>]*\bshuttleslide-svg-placeholder\b[^\"'>]*[\"'][^>]*?/?>",
    re.IGNORECASE,
)

_ATTR_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _extract_attr(tag_str: str, attr_name: str) -> Optional[str]:
    """Extract a single attribute value from an HTML tag string.

    Handles double-quoted, single-quoted, and unquoted attribute values.
    Returns None if the attribute is absent.
    """
    if attr_name not in _ATTR_RE_CACHE:
        _ATTR_RE_CACHE[attr_name] = re.compile(
            rf"\b{re.escape(attr_name)}\s*=\s*(?:"
            r'"([^"]*)"'
            r"|"
            r"'([^']*)'"
            r"|"
            r"([^\s>]+)"
            r")",
            re.IGNORECASE,
        )
    m = _ATTR_RE_CACHE[attr_name].search(tag_str)
    if not m:
        return None
    return m.group(1) if m.group(1) is not None else (
        m.group(2) if m.group(2) is not None else m.group(3)
    )


def inline_svg_placeholders(html: str, base_dir: Optional[Path]) -> str:
    """Replace ``<img class="shuttleslide-svg-placeholder" src="...">`` tags
    with the corresponding inline ``<svg>`` markup.

    The agent pipeline writes SVG art to ``{output_dir}/svgs/*.svg`` and
    embeds only a short placeholder reference in the slide HTML (so the
    SVG bytes never flow through the slide-builder LLM context or the
    12000-char free-form HTML cap). The slide HTML on disk is therefore
    compact and self-describing (placeholder carries ``data-description``)
    but cannot be rendered as-is in a browser — the SVG must be inlined
    back before Playwright loads the page. That's what this function does.

    Args:
        html: The slide HTML string potentially containing placeholders.
        base_dir: The directory the SVG ``src`` paths are relative to
            (typically the slide HTML file's parent directory). When
            ``None``, the HTML is returned unchanged — this preserves
            backward compatibility for callers that supply HTML with
            inline SVG already in place.

    Raises:
        FileNotFoundError: A placeholder's ``src`` does not resolve to an
            existing file under ``base_dir``.
        ValueError: A placeholder lacks an ``src`` attribute, or the
            resolved path escapes ``base_dir`` (path traversal guard).

    Returns:
        The HTML with every placeholder replaced by its inline ``<svg>``.
    """
    if base_dir is None:
        # Silent return would let SVG placeholders sail through to
        # _render_image and fail with a confusing "Could not download
        # image" warning. Surface the real cause so the caller knows
        # to pass base_dir.
        if _SVG_PLACEHOLDER_IMG_RE.search(html):
            logger.warning(
                "HTML contains <img class=\"shuttleslide-svg-placeholder\"> "
                "tags but base_dir is None — SVG will NOT be inlined and "
                "the slide will render with broken/missing images. "
                "Pass base_dir= (typically the HTML file's parent dir) "
                "to transform_html()."
            )
        return html

    matches = list(_SVG_PLACEHOLDER_IMG_RE.finditer(html))
    if not matches:
        return html

    # Iterate right-to-left so byte offsets from earlier matches stay
    # valid as we splice replacements in.
    out = html
    for m in reversed(matches):
        tag_str = m.group(0)
        src = _extract_attr(tag_str, "src")
        if not src:
            raise ValueError(
                f"shuttleslide-svg-placeholder <img> is missing src; tag: {tag_str!r}"
            )
        svg_path = (base_dir / src).resolve()
        try:
            svg_path.relative_to(base_dir.resolve())
        except ValueError as exc:
            raise ValueError(
                f"SVG placeholder src {src!r} resolves outside base_dir "
                f"{base_dir!s} (path traversal guard)"
            ) from exc
        if not svg_path.is_file():
            raise FileNotFoundError(
                f"SVG placeholder src {src!r} does not resolve to a file "
                f"(expected {svg_path!s})"
            )
        svg_markup = svg_path.read_text(encoding="utf-8")
        slot_id = _extract_attr(tag_str, "data-slot")
        # extract_layout.js identifies inline SVG via [data-slot] on the
        # <svg> root. The on-disk SVG already carries id + data-slot
        # (set_svg's contract), so in normal operation this is a no-op
        # safety stamp. If the file somehow lost its data-slot, stamp it
        # from the placeholder so the downstream selector still matches.
        if slot_id and 'data-slot="' not in svg_markup and "data-slot='" not in svg_markup:
            svg_markup = re.sub(
                r"<svg\b",
                f'<svg data-slot="{slot_id}"',
                svg_markup,
                count=1,
            )
        out = out[: m.start()] + svg_markup + out[m.end() :]
    return out
