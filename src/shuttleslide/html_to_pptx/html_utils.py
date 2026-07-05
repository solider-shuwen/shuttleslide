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


# Match a single CSS declaration inside a style="..." attribute body.
# `prop: value;` — captures the property name and value, ignoring the
# trailing semicolon or end-of-string. Negative lookbehind on `[\w-]`
# prevents "min-width" from matching a bare "width" search.
_CSS_DECL_RE = re.compile(
    r"(?<![\w-])(?P<prop>[\w-]+)\s*:\s*(?P<val>[^;]+?)\s*(?:;|$)",
)


def _parse_img_box_semantics(style_str: Optional[str]) -> dict:
    """Extract width / height / object-fit from a ``<img>`` style attribute.

    Returns a dict possibly containing keys ``width``, ``height``,
    ``object_fit``. Values are raw CSS strings (e.g. ``"100%"``, ``"50px"``,
    ``"cover"``). ``object_fit`` is lower-cased; missing or unrecognized
    values are dropped (caller defaults to ``"fill"``).
    """
    if not style_str:
        return {}
    out: dict = {}
    for m in _CSS_DECL_RE.finditer(style_str):
        prop = m.group("prop").lower()
        val = m.group("val").strip()
        if prop == "width":
            out["width"] = val
        elif prop == "height":
            out["height"] = val
        elif prop == "object-fit":
            v = val.lower()
            if v in {"cover", "contain", "fill", "none", "scale-down"}:
                out["object_fit"] = v
    return out


# Match the opening <svg ...> tag and capture (head, body, tail) where
# head includes "<svg", body is the attribute payload, tail is ">".
_SVG_OPEN_TAG_RE = re.compile(r"(\s*<svg\b)([^>]*)(>)", re.IGNORECASE)

# Matches an existing width=/height= attribute on the SVG root (to strip
# before re-injecting the <img>-derived value).
_SVG_DIM_ATTR_RE = re.compile(
    r"\s+(?:width|height)\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s/>]+)",
    re.IGNORECASE,
)


def _stamp_svg_root_attrs(
    svg_markup: str,
    *,
    width: Optional[str],
    height: Optional[str],
    object_fit: str,
) -> str:
    """Stamp box-model attributes onto an inlined ``<svg>`` root.

    Called after :func:`inline_svg_placeholders` reads the raw SVG file to
    carry the placeholder ``<img>``'s CSS box semantics (width / height /
    object-fit) onto the ``<svg>`` element. Without this, the raw SVG
    (which typically has only a ``viewBox`` and no explicit width/height)
    renders at its intrinsic viewBox aspect ratio inside the container,
    losing the container-fill behavior the slide HTML relied on.

    Behavior:

    * No-op when the ``<img>`` had no relevant CSS (no width, no height,
      and object-fit at its default ``"fill"``). Keeps the SVG verbatim
      for older fixtures and consumers that don't style the placeholder.
    * Otherwise, override any existing ``width`` / ``height`` /
      ``data-object-fit`` on the root — the ``<img>``'s CSS wins by
      design, and the data attribute is what extract_layout.js reads
      (``style.objectFit`` is unreliable on ``<svg>``).
    * Inject ``preserveAspectRatio`` only when the SVG root does not
      already declare one (respect author intent for the visual crop).
      ``cover`` → ``xMidYMid slice``, ``contain`` → ``xMidYMid meet``.
    """
    if not width and not height and object_fit == "fill":
        return svg_markup

    m = _SVG_OPEN_TAG_RE.match(svg_markup)
    if not m:
        return svg_markup
    head, body, tail = m.group(1), m.group(2), m.group(3)

    body = _SVG_DIM_ATTR_RE.sub("", body)
    body = re.sub(
        r"\s+data-object-fit\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s/>]+)",
        "",
        body,
        flags=re.IGNORECASE,
    )

    inject: list[str] = []
    if width:
        inject.append(f'width="{width}"')
    if height:
        inject.append(f'height="{height}"')
    inject.append(f'data-object-fit="{object_fit}"')

    has_par = re.search(r"\bpreserveAspectRatio\s*=", body, re.IGNORECASE)
    if not has_par:
        if object_fit == "cover":
            inject.append('preserveAspectRatio="xMidYMid slice"')
        elif object_fit == "contain":
            inject.append('preserveAspectRatio="xMidYMid meet"')

    return head + body + " " + " ".join(inject) + tail + svg_markup[m.end():]


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
        # Carry the <img>'s CSS box semantics (width / height / object-fit)
        # onto the inlined <svg> root. Without this, the raw SVG (typically
        # viewBox-only) renders at its intrinsic aspect ratio and ignores
        # the container-fill behavior the slide HTML was relying on — the
        # resulting PPTX group ends up sized to the viewBox aspect (e.g.
        # 5in × 2.81in for a 1280×720 viewBox) instead of the container
        # (e.g. 5in × 13.33in). See _stamp_svg_root_attrs for details.
        img_style = _extract_attr(tag_str, "style")
        box = _parse_img_box_semantics(img_style)
        svg_markup = _stamp_svg_root_attrs(
            svg_markup,
            width=box.get("width"),
            height=box.get("height"),
            object_fit=box.get("object_fit", "fill"),
        )
        out = out[: m.start()] + svg_markup + out[m.end() :]
    return out
