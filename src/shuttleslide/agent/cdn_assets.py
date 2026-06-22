"""On-demand CDN asset cache for offline-resilient HTML rendering.

Downloads and caches the external assets rendered slide HTML depends on:

  - Tailwind CSS JIT runtime (https://cdn.tailwindcss.com) — inlined by default
  - Material Icons CSS + TTF (https://fonts.googleapis.com/icon) — inlined by default
  - Google Fonts CSS + TTF (https://fonts.googleapis.com) — OPT-IN ONLY, see below

At render time the Tailwind and Material Icons assets are inlined into
the HTML output so the document works without network access. This
matters for two reasons:

  1. Browser preview breaks when the CDNs are unreachable (e.g. behind
     the GFW, on air-gapped machines, or when the CDN is having an
     outage). Without Tailwind the entire layout collapses.
  2. `html_to_pptx` renders the HTML via Playwright; the rendered
     element positions are only correct if Tailwind resolves, so PPTX
     conversion is non-deterministic if it depends on a live CDN.

Cache lives at `~/.shuttleslide/cdn/`. If a download fails on first
call, the public getter returns None and the renderer falls back to
the original CDN URL so environments WITH network still work.

The cache is write-once: assets don't auto-refresh. Delete the cache
directory to force a re-download.

Why Google Fonts is NOT inlined by default
------------------------------------------

`get_google_fonts_css` downloads the CSS at the requested URL and inlines
every TTF as base64. Because our urllib User-Agent doesn't trigger
Google's modern browser subsetting, the response contains full-font
TTFs rather than the @unicode-range-split files a Chrome user-agent
would receive. For Noto Sans SC alone that's ~10 MB per weight, and
the cached CSS balloons to ~47 MB. Inlining that into every slide
HTML makes each rendered file ~48 MB (vs 3-8 KB before inlining),
which slows Playwright rendering in `html_to_pptx` and bloats disk.

The default template instead uses a cross-platform system font stack
(SF Pro / Segoe UI / PingFang SC / Microsoft YaHei / ...) which is
always available offline and visually equivalent for slide rendering.

If you specifically need Roboto/Noto Sans SC, you can either:

  - Call `get_google_fonts_css()` directly and inline the result into
    your own template (accepting the ~47 MB size hit).
  - Use `SlideHTMLRenderer(inline_cdn_assets=False)` to emit the
    original CDN <link> tag, which requires network at render time
    but uses Google's @unicode-range optimization.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cache directory
# ---------------------------------------------------------------------------

_CACHE_DIR = Path.home() / ".shuttleslide" / "cdn"


def _cache_dir() -> Path:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _CACHE_DIR


def _fetch(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "shuttleslide/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------------------
# Tailwind JIT script
# ---------------------------------------------------------------------------

DEFAULT_TAILWIND_URL = "https://cdn.tailwindcss.com"
_TAILWIND_CACHE = _CACHE_DIR / "tailwind.js"


def get_tailwind_script() -> Optional[str]:
    """Return the Tailwind JIT script body.

    Downloads on first call, then serves from cache. Returns None if the
    download fails and no cache exists; callers should fall back to the
    CDN <script src> in that case.
    """
    if _TAILWIND_CACHE.exists() and _TAILWIND_CACHE.stat().st_size > 1000:
        return _TAILWIND_CACHE.read_text(encoding="utf-8")
    try:
        logger.info("Downloading Tailwind JIT from %s", DEFAULT_TAILWIND_URL)
        body = _fetch(DEFAULT_TAILWIND_URL).decode("utf-8", errors="replace")
        if len(body) < 1000:
            raise RuntimeError(f"suspiciously small response: {len(body)} bytes")
        _TAILWIND_CACHE.write_text(body, encoding="utf-8")
        logger.info("Cached Tailwind JIT (%d bytes) at %s", len(body), _TAILWIND_CACHE)
        return body
    except Exception as exc:
        logger.warning(
            "Could not download Tailwind JIT from %s: %s. "
            "Renderer will fall back to the CDN <script src>. ",
            DEFAULT_TAILWIND_URL,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Google Fonts CSS with inlined TTFs
# ---------------------------------------------------------------------------

# Match url(...) in CSS. Captures the URL inside parens, allowing quoted
# or unquoted form. Font URLs in Google's CSS are always quoted.
_FONT_URL_RE = re.compile(
    r'url\(\s*["\']?(https?://[^"\')]+?)["\']?\s*\)',
    re.IGNORECASE,
)


def _mime_for_font_url(url: str) -> str:
    ext = url.split("?")[0].rsplit(".", 1)[-1].lower()
    return {
        "ttf": "font/ttf",
        "otf": "font/otf",
        "woff": "font/woff",
        "woff2": "font/woff2",
    }.get(ext, "font/ttf")


def _inline_font_urls(css: str) -> str:
    """Replace each font URL in @font-face src with a base64 data: URI.

    Falls back to keeping the original URL if a font fetch fails (so a
    partial download still produces working CSS for the fonts that did
    resolve).
    """
    def replace(m: re.Match) -> str:
        url = m.group(1)
        try:
            font_bytes = _fetch(url, timeout=15)
            if len(font_bytes) < 1000:
                raise RuntimeError(f"suspiciously small font: {len(font_bytes)} bytes")
            b64 = base64.b64encode(font_bytes).decode("ascii")
            mime = _mime_for_font_url(url)
            return f"url(data:{mime};base64,{b64})"
        except Exception as exc:
            logger.warning("Failed to fetch font %s: %s", url, exc)
            return f"url({url})"

    return _FONT_URL_RE.sub(replace, css)


def _get_font_css_with_data_uris(css_url: str, cache_prefix: str) -> Optional[str]:
    """Common helper: fetch a Google Fonts CSS URL, inline TTFs, cache."""
    cache_key = hashlib.md5(css_url.encode()).hexdigest()[:12]
    css_cache = _cache_dir() / f"{cache_prefix}_{cache_key}.css"
    if css_cache.exists() and css_cache.stat().st_size > 0:
        return css_cache.read_text(encoding="utf-8")
    try:
        logger.info("Downloading font CSS from %s", css_url)
        css = _fetch(css_url, timeout=15).decode("utf-8", errors="replace")
        inlined = _inline_font_urls(css)
        css_cache.write_text(inlined, encoding="utf-8")
        logger.info("Cached font CSS (%d bytes) at %s", len(inlined), css_cache)
        return inlined
    except Exception as exc:
        logger.warning(
            "Could not download font CSS from %s: %s. "
            "Renderer will fall back to the CDN <link>. ",
            css_url,
            exc,
        )
        return None


# Public default CSS URLs. Centralised so the template and the cache
# helper stay in sync.
DEFAULT_GOOGLE_FONTS_URL = (
    "https://fonts.googleapis.com/css2?"
    "family=Noto+Sans+SC:wght@400;500;700;900&"
    "family=Roboto:wght@300;400;500;700;900&display=swap"
)
DEFAULT_MATERIAL_ICONS_URL = "https://fonts.googleapis.com/icon?family=Material+Icons"


def get_google_fonts_css(url: str = DEFAULT_GOOGLE_FONTS_URL) -> Optional[str]:
    """Return Google Fonts CSS with all TTFs inlined as data: URIs.

    OPT-IN ONLY — not wired into the default renderer. See the module
    docstring ("Why Google Fonts is NOT inlined by default") for the
    ~47 MB size problem this avoids.
    """
    return _get_font_css_with_data_uris(url, cache_prefix="gfonts")


def get_material_icons_css(url: str = DEFAULT_MATERIAL_ICONS_URL) -> Optional[str]:
    """Return Material Icons CSS with the TTF inlined as a data: URI."""
    return _get_font_css_with_data_uris(url, cache_prefix="material")
