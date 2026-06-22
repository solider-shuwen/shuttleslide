"""
On-demand icon font downloader with local cache.

Extracts icon font URLs from HTML <head> <link> tags (e.g. Google Fonts),
fetches the CSS, parses @font-face src URLs, downloads TTF files,
and caches them under ~/.shuttleslide/fonts/.

Also extracts icon name -> PUA codepoint mappings from the font's GSUB table
so icons can be rendered via direct codepoint in PPTX (not ligatures).
"""

from __future__ import annotations

import hashlib
import html as html_module
import io
import logging
import re
import urllib.request
from pathlib import Path
from typing import Dict, Optional, Tuple

from shuttleslide._vendored.svg_to_pptx.drawingml_utils import (
    EA_FONTS,
    FONT_FALLBACK_WIN,
    GENERIC_FONT_MAP,
    SYSTEM_FONTS,
)

logger = logging.getLogger(__name__)

# Known icon font CSS patterns — map query param → CSS font-family name
_ICON_FONT_PARAMS: dict[str, str] = {
    "Material+Icons": "Material Icons",
    "Material+Icons+Outlined": "Material Icons Outlined",
    "Material+Icons+Round": "Material Icons Round",
    "Material+Icons+Sharp": "Material Icons Sharp",
    "Material+Symbols+Outlined": "Material Symbols Outlined",
    "Material+Symbols+Rounded": "Material Symbols Rounded",
    "Material+Symbols+Sharp": "Material Symbols Sharp",
}

# Reverse map: CSS font-family name -> the <i> CSS class (e.g. 'material-icons').
# Used to resolve icon fonts declared inline via @font-face (data URI) rather
# than via a Google Fonts <link> tag.
_FONT_NAME_TO_ICON_CLASS: dict[str, str] = {
    name: name.lower().replace(" ", "-") for name in _ICON_FONT_PARAMS.values()
}

# icon CSS class → PUA codepoint mapping (extracted from font)
_codepoint_maps: dict[str, dict[str, int]] = {}

# Cache of resolved fonts: icon_class -> (font_name, font_bytes)
_resolved: dict[str, Tuple[str, bytes]] = {}


def _cache_dir() -> Path:
    """Return the font cache directory, creating it if needed."""
    d = Path.home() / ".shuttleslide" / "fonts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _fetch_url(url: str, timeout: int = 30) -> bytes:
    """Fetch a URL and return bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": "shuttleslide/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_font_face_src(css: str) -> Optional[str]:
    """Extract the first .ttf URL from @font-face src in CSS."""
    for m in re.finditer(r'url\(([^)]+\.ttf)\)', css, re.IGNORECASE):
        return m.group(1)
    return None


def _extract_codepoints_from_ttf(font_bytes: bytes) -> dict[str, int]:
    """Extract icon name -> PUA codepoint mapping from a Material Icons TTF.

    Parses the font's GSUB table (rlig feature) to find ligature substitutions
    that map icon name text to PUA glyphs (e.g. "visibility" -> U+E8F4).
    """
    try:
        from fontTools.ttLib import TTFont
    except ImportError:
        logger.warning("fonttools not installed, cannot extract icon codepoints")
        return {}

    font = TTFont(io.BytesIO(font_bytes))

    cmap = font.getBestCmap()
    if not cmap:
        font.close()
        return {}

    # Build glyph_name -> character mapping
    glyph_to_char: dict[str, str] = {}
    for cp, glyph_name in cmap.items():
        glyph_to_char[glyph_name] = chr(cp)

    # Reverse map: glyph_name -> PUA codepoint
    rev_cmap: dict[str, int] = {}
    for cp, glyph_name in cmap.items():
        if 0xE000 <= cp <= 0xF8FF:
            rev_cmap[glyph_name] = cp

    # Parse GSUB ligature table
    gsub = font.get("GSUB")
    if not gsub:
        font.close()
        return {}

    result: dict[str, int] = {}
    for lookup in gsub.table.LookupList.Lookup:
        for st in lookup.SubTable:
            if st.LookupType != 4:
                continue
            for first_glyph, ligs in st.ligatures.items():
                first_char = glyph_to_char.get(first_glyph, first_glyph)
                for lig in ligs:
                    components = list(getattr(lig, 'Component', []))
                    lig_glyph = lig.LigGlyph
                    cp = rev_cmap.get(lig_glyph)
                    if cp is None:
                        continue
                    chars = [first_char]
                    for g in components:
                        chars.append(glyph_to_char.get(g, g))
                    text = "".join(chars)
                    result[text] = cp

    font.close()
    return result


_FONTFACE_RE = re.compile(r"@font-face\s*\{([^}]*)\}", re.DOTALL)


def _font_bytes_from_src(src: str) -> Optional[bytes]:
    """Resolve raw TTF bytes from an @font-face ``src: url(...)`` value.

    Handles base64 data URIs (inline, no network) and http(s) URLs. Returns
    None for relative or unsupported sources.
    """
    if src.startswith("data:"):
        # data:font/ttf;base64,AAAA...
        header, sep, b64 = src.partition(",")
        if not sep or "base64" not in header:
            return None
        import base64
        try:
            return base64.b64decode(b64)
        except Exception:
            return None
    if src.startswith(("http://", "https://")):
        try:
            return _fetch_url(src)
        except Exception as exc:
            logger.debug("Failed to fetch @font-face src %s: %s", src, exc)
            return None
    return None


def _parse_fontface(block: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (font-family, src url) from an @font-face declaration body."""
    ff_match = re.search(
        r"font-family\s*:\s*['\"]?([^;\"']+)['\"]?\s*;",
        block, re.IGNORECASE,
    )
    font_family = ff_match.group(1).strip() if ff_match else None
    src_match = re.search(r"src\s*:\s*url\(([^)]+)\)", block, re.IGNORECASE)
    src = src_match.group(1).strip().strip("'\"") if src_match else None
    return font_family, src


def _resolve_inline_font_faces(html: str, resolved_map: dict[str, str]) -> None:
    """Resolve icon fonts declared inline as @font-face (e.g. base64 data URIs).

    Agent-generated and self-contained HTML often inlines the Material Icons
    TTF inside a ``<style>`` block instead of linking Google Fonts. The
    ``<link>`` pass in :func:`resolve_icon_fonts` misses these, so every icon
    fell back to text. This scans ``<style>`` blocks, decodes the font, and
    populates the same in-memory caches.

    Mutates ``_resolved`` / ``_codepoint_maps`` / ``resolved_map`` in place.
    """
    try:
        from lxml.html import fromstring as html_fromstring
        tree = html_fromstring(html)
    except Exception:
        return

    for style_el in tree.iter("style"):
        style_text = style_el.text or ""
        for ff_match in _FONTFACE_RE.finditer(style_text):
            font_family, src = _parse_fontface(ff_match.group(1))
            if not font_family or not src:
                continue
            icon_class = _FONT_NAME_TO_ICON_CLASS.get(font_family)
            if icon_class is None or icon_class in _resolved:
                continue
            font_bytes = _font_bytes_from_src(src)
            if not font_bytes or len(font_bytes) < 1000:
                continue
            if icon_class not in _codepoint_maps:
                cpmap = _extract_codepoints_from_ttf(font_bytes)
                if cpmap:
                    _codepoint_maps[icon_class] = cpmap
                    logger.info(
                        "Extracted %d icon codepoints from inline '%s'",
                        len(cpmap), font_family,
                    )
            _resolved[icon_class] = (font_family, font_bytes)
            resolved_map[icon_class] = font_family
            logger.info(
                "Resolved inline @font-face icon font '%s' (%d bytes)",
                font_family, len(font_bytes),
            )


def resolve_icon_fonts(html: str) -> Dict[str, str]:
    """Scan HTML for icon font <link> tags and resolve them.

    For each Google Fonts icon link found, downloads the CSS, extracts
    the TTF URL, downloads the font, extracts codepoint mappings,
    and caches the result.

    Args:
        html: Full HTML source (must include <head> with <link> tags).

    Returns:
        Dict mapping icon CSS class name (e.g. 'material-icons') to
        PPTX font name (e.g. 'Material Icons') for fonts successfully resolved.
    """
    from lxml.html import fromstring as html_fromstring

    resolved_map: dict[str, str] = {}

    try:
        tree = html_fromstring(html)
    except Exception:
        return resolved_map

    for link in tree.iter("link"):
        href = link.get("href", "")
        rel = link.get("rel", "")
        if not href or "stylesheet" not in (rel if isinstance(rel, str) else " ".join(rel)):
            continue

        # Check if this is a known icon font URL
        icon_class = None
        font_name = None

        # Match fonts.googleapis.com/icon?family=...
        if "fonts.googleapis.com/icon" in href:
            m = re.search(r'family=([^&]+)', href)
            if m:
                param = m.group(1)
                font_name = _ICON_FONT_PARAMS.get(param)
                if font_name:
                    icon_class = "material-icons"

        # Also check fonts.googleapis.com/css2 with icon font families
        elif "fonts.googleapis.com/css2" in href:
            for param, name in _ICON_FONT_PARAMS.items():
                if f"family={param}" in href:
                    font_name = name
                    icon_class = param.lower().replace("+", "-")
                    break

        if not icon_class or not font_name:
            continue

        # Already resolved?
        if icon_class in _resolved:
            resolved_map[icon_class] = _resolved[icon_class][0]
            continue

        # Try to load font from cache
        cache_key = hashlib.md5(href.encode()).hexdigest()[:12]
        cached_font = _cache_dir() / f"{font_name.replace(' ', '_')}_{cache_key}.ttf"
        cached_cpmap = _cache_dir() / f"{font_name.replace(' ', '_')}_{cache_key}.cpmap"

        font_bytes = None

        if cached_font.exists() and cached_font.stat().st_size > 0:
            font_bytes = cached_font.read_bytes()
            # Load cached codepoint map
            if cached_cpmap.exists():
                _load_cached_codepoints(icon_class, cached_cpmap)
            logger.debug("Loaded cached font: %s", font_name)

        if font_bytes is None:
            # Fetch CSS and extract font URL
            try:
                logger.info("Resolving icon font CSS: %s", href)
                css = _fetch_url(href, timeout=15).decode("utf-8", errors="replace")
                ttf_url = _parse_font_face_src(css)
                if not ttf_url:
                    logger.warning("No TTF URL found in CSS for %s", href)
                    continue

                # Download TTF
                logger.info("Downloading icon font TTF: %s", ttf_url)
                font_bytes = _fetch_url(ttf_url)
                if len(font_bytes) < 1000:
                    logger.warning("Downloaded font too small (%d bytes)", len(font_bytes))
                    continue

                # Cache
                cached_font.write_bytes(font_bytes)
                logger.info("Downloaded icon font '%s' (%d bytes)", font_name, len(font_bytes))

            except Exception as exc:
                logger.warning("Failed to resolve icon font from %s: %s", href, exc)
                continue

        # Extract codepoint mapping from TTF
        if icon_class not in _codepoint_maps:
            cpmap = _extract_codepoints_from_ttf(font_bytes)
            if cpmap:
                _codepoint_maps[icon_class] = cpmap
                # Cache the codepoint map as JSON
                _save_cached_codepoints(cpmap, cached_cpmap)
                logger.info("Extracted %d icon codepoints from '%s'", len(cpmap), font_name)

        _resolved[icon_class] = (font_name, font_bytes)
        resolved_map[icon_class] = font_name

    # Resolve icon fonts inlined as @font-face in <style> (base64 data URIs).
    _resolve_inline_font_faces(html, resolved_map)

    return resolved_map


def _load_cached_codepoints(icon_class: str, path: Path) -> None:
    """Load a cached codepoint map from JSON."""
    if icon_class in _codepoint_maps:
        return
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        _codepoint_maps[icon_class] = {k: int(v, 16) for k, v in data.items()}
        logger.debug("Loaded %d cached codepoints for %s", len(data), icon_class)
    except Exception as exc:
        logger.debug("Failed to load cached codepoints: %s", exc)


def _save_cached_codepoints(cpmap: dict[str, int], path: Path) -> None:
    """Save a codepoint map as JSON."""
    try:
        import json
        data = {k: f"{v:04X}" for k, v in cpmap.items()}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def get_font_bytes(icon_class: str) -> Optional[bytes]:
    """Return cached font bytes for a previously resolved icon class."""
    entry = _resolved.get(icon_class)
    return entry[1] if entry else None


def get_font_name(icon_class: str) -> Optional[str]:
    """Return the PPTX font name for a previously resolved icon class."""
    entry = _resolved.get(icon_class)
    return entry[0] if entry else None


def icon_to_codepoint(icon_class: str, icon_name: str) -> Optional[int]:
    """Look up the PUA codepoint for an icon name.

    Args:
        icon_class: e.g. 'material-icons'
        icon_name: e.g. 'visibility'

    Returns:
        Unicode codepoint (e.g. 0xE8F4), or None if not found.
    """
    cpmap = _codepoint_maps.get(icon_class)
    if cpmap is None:
        return None
    return cpmap.get(icon_name)


# ---------------------------------------------------------------------------
# CSS font-family parsing (for the main text-rendering pipeline)
# ---------------------------------------------------------------------------

def parse_css_font_family(raw: Optional[str]) -> Dict[str, str]:
    """Parse a CSS font-family string into DrawingML script-category fonts.

    Returns a dict with keys ``latin``, ``ea``, ``cs``. An empty string means
    "no font specified for that script" — caller should leave the matching
    ``<a:latin>``/``<a:ea>``/``<a:cs>`` element unset.

    Why this exists: the main text pipeline (extract_layout.js → converter.py
    → renderer.py) used to pass ``getComputedStyle().fontFamily`` verbatim
    into python-pptx's ``run.font.name``, producing malformed DrawingML like
    ``<a:latin typeface="Nunito, sans-serif"/>`` or
    ``<a:latin typeface="Inter, Roboto, -apple-system, &quot;Segoe UI&quot;, ..."/>``.
    PowerPoint cannot parse these and falls back to the default font.

    This parser:
      1. html.unescape() — strip ``&quot;`` and friends that browsers leave
         in computed styles.
      2. Split on commas, strip whitespace and surrounding quotes.
      3. Skip noise tokens (``system-ui``, ``-apple-system``, ``BlinkMacSystemFont``).
      4. Substitute generic families (``sans-serif`` → ``Segoe UI``) and
         macOS/Linux-only fonts (``Roboto`` → ``Segoe UI``) via the same
         table used by the SVG→DrawingML vendor library.
      5. Classify each font as Latin or East-Asian (no CS table today —
         rare for presentation decks). First match per category wins.

    Examples:
        >>> parse_css_font_family('Nunito, sans-serif')
        {'latin': 'Nunito', 'ea': '', 'cs': ''}
        >>> parse_css_font_family('&quot;Nunito Sans&quot;, sans-serif')
        {'latin': 'Nunito Sans', 'ea': '', 'cs': ''}
        >>> parse_css_font_family("Inter, 'Microsoft YaHei', sans-serif")
        {'latin': 'Inter', 'ea': 'Microsoft YaHei', 'cs': ''}
        >>> parse_css_font_family(None)
        {'latin': '', 'ea': '', 'cs': ''}
    """
    result: Dict[str, str] = {"latin": "", "ea": "", "cs": ""}
    if not raw:
        return result

    # Browsers surface HTML-escaped quotes inside getComputedStyle().fontFamily.
    text = html_module.unescape(raw)

    for token in text.split(","):
        name = token.strip().strip("\"'")
        if not name or name in SYSTEM_FONTS:
            continue

        # Map generic families to concrete fonts (sans-serif → Segoe UI, etc.)
        if name in GENERIC_FONT_MAP:
            name = GENERIC_FONT_MAP[name]

        # Map macOS/Linux-only fonts to their Windows equivalents so PPTX
        # opens correctly on Windows. Fonts not in this map stay as-is.
        name = FONT_FALLBACK_WIN.get(name, name)

        # Classify by script category — first match per category wins.
        if name in EA_FONTS:
            if not result["ea"]:
                result["ea"] = name
        else:
            # No CS_FONTS table in the vendored library today — every
            # non-EA font lands in latin. Future work if Arabic/Hindi/
            # Thai decks become common.
            if not result["latin"]:
                result["latin"] = name

    return result


# ---------------------------------------------------------------------------
# Regular (non-icon) text font downloader — for font embedding (Phase 3a)
# ---------------------------------------------------------------------------

_GOOGLE_CSS2_RE = re.compile(
    r"@font-face\s*\{[^}]*font-family\s*:\s*['\"]?([^;\"']+)['\"]?\s*;[^}]*\}",
    re.IGNORECASE | re.DOTALL,
)
_GOOGLE_TTF_RE = re.compile(
    r"src\s*:\s*url\(([^)]+\.ttf)\)", re.IGNORECASE,
)


def fetch_text_font_bytes(font_name: str, timeout: int = 15) -> Optional[bytes]:
    """Download TTF bytes for a regular Google Font (Nunito, Inter, ...).

    Pulls the regular (400) weight from ``fonts.googleapis.com/css2``, parses
    out the first ``@font-face src: url(*.ttf)``, and downloads the TTF.
    Cached at ``~/.shuttleslide/fonts/<name>.ttf``.

    Returns None on any failure (network, CSS parse, unknown font). Callers
    must treat None as "skip embedding this font" — never raise.
    """
    if not font_name:
        return None

    cache_dir = _cache_dir()
    safe_name = re.sub(r"[^A-Za-z0-9_\-]+", "_", font_name)
    cache_path = cache_dir / f"text_{safe_name}.ttf"

    if cache_path.exists() and cache_path.stat().st_size > 1000:
        try:
            return cache_path.read_bytes()
        except OSError as exc:
            logger.debug("Cache read failed for %s: %s", font_name, exc)

    # Google Fonts css2 endpoint: family=Name+With+Spaces
    family_param = font_name.replace(" ", "+")
    css_url = (
        f"https://fonts.googleapis.com/css2?family={family_param}"
        f":wght@400&display=swap"
    )

    try:
        logger.info("Fetching Google Fonts CSS for '%s'", font_name)
        css_bytes = _fetch_url(css_url, timeout=timeout)
        css_text = css_bytes.decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("Failed to fetch CSS for font '%s': %s", font_name, exc)
        return None

    ttf_match = _GOOGLE_TTF_RE.search(css_text)
    if not ttf_match:
        logger.warning("No TTF URL in CSS for font '%s'", font_name)
        return None

    ttf_url = ttf_match.group(1).strip().strip("'\"")
    try:
        logger.info("Downloading TTF for '%s'", font_name)
        font_bytes = _fetch_url(ttf_url, timeout=timeout)
    except Exception as exc:
        logger.warning("Failed to download TTF for font '%s': %s", font_name, exc)
        return None

    if len(font_bytes) < 1000:
        logger.warning("TTF for '%s' suspiciously small (%d bytes)", font_name, len(font_bytes))
        return None

    try:
        cache_path.write_bytes(font_bytes)
    except OSError as exc:
        logger.debug("Cache write failed for %s: %s", font_name, exc)

    logger.info("Downloaded text font '%s' (%d bytes)", font_name, len(font_bytes))
    return font_bytes
