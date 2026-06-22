"""
Extract vector glyph outlines from icon fonts and convert to OpenXML custom geometry.

Downloads icon font TTFs (via fonts.py infrastructure), uses fontTools to extract
bezier-curve outlines, transforms coordinates, and produces <a:custGeom> XML for
embedding as vector shapes in PPTX.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from lxml import etree

logger = logging.getLogger(__name__)

# DrawingML namespace
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

# Cache: (font_bytes_id, codepoint) -> GlyphOutline | None
_outline_cache: dict[tuple[int, int], Optional[GlyphOutline]] = {}

# Default path coordinate space resolution
_PATH_SIZE = 10000


@dataclass
class GlyphOutline:
    """A fully resolved glyph outline ready for OpenXML serialization."""

    commands: List[Tuple[str, list]]
    # Each command is (type, points) where:
    #   type: "moveTo", "lineTo", "quadBezTo", "cubicBezTo", "close"
    #   points: list of (x, y) tuples (already in OpenXML path coordinates)
    path_w: int = _PATH_SIZE
    path_h: int = _PATH_SIZE


def extract_glyph_outline(
    font_bytes: bytes,
    codepoint: int,
    path_size: int = _PATH_SIZE,
) -> Optional[GlyphOutline]:
    """Extract a glyph outline from TTF bytes for a given Unicode codepoint.

    Args:
        font_bytes: Raw TTF font file bytes.
        codepoint: Unicode codepoint (e.g. 0xE8F4 for Material Icons PUA).
        path_size: Target coordinate space resolution (default 10000).

    Returns:
        GlyphOutline with transformed coordinates, or None if glyph not found.
    """
    # Check cache
    cache_key = (id(font_bytes), codepoint)
    if cache_key in _outline_cache:
        return _outline_cache[cache_key]

    try:
        from fontTools.pens.recordingPen import RecordingPen
        from fontTools.pens.boundsPen import BoundsPen
        from fontTools.ttLib import TTFont
    except ImportError:
        logger.warning("fonttools not installed, cannot extract glyph outlines")
        _outline_cache[cache_key] = None
        return None

    try:
        import io as _io

        font = TTFont(_io.BytesIO(font_bytes))
        glyph_set = font.getGlyphSet()
        cmap = font.getBestCmap()

        if cmap is None:
            _outline_cache[cache_key] = None
            return None

        glyph_name = cmap.get(codepoint)
        if glyph_name is None:
            logger.debug("Codepoint U+%04X not found in font cmap", codepoint)
            _outline_cache[cache_key] = None
            return None

        # Get bounding box
        bounds_pen = BoundsPen(glyph_set)
        glyph_set[glyph_name].draw(bounds_pen)
        if bounds_pen.bounds is None:
            logger.debug("Glyph '%s' has no outline (empty)", glyph_name)
            _outline_cache[cache_key] = None
            return None

        x_min, y_min, x_max, y_max = bounds_pen.bounds
        glyph_w = x_max - x_min
        glyph_h = y_max - y_min
        if glyph_w == 0 or glyph_h == 0:
            _outline_cache[cache_key] = None
            return None

        # Record outline
        rec_pen = RecordingPen()
        glyph_set[glyph_name].draw(rec_pen)

        font.close()

        # Calculate aspect ratio from bounding box
        path_h = path_size
        path_w = round(path_size * glyph_w / glyph_h) if glyph_h > 0 else path_size

        # Transform coordinates with correct aspect ratio
        commands = _transform_commands(rec_pen.value, x_min, y_min, x_max, y_max, path_w, path_h)

        outline = GlyphOutline(commands=commands, path_w=path_w, path_h=path_h)
        _outline_cache[cache_key] = outline
        return outline

    except Exception as exc:
        logger.warning("Failed to extract glyph outline for U+%04X: %s", codepoint, exc)
        _outline_cache[cache_key] = None
        return None


def _decompose_qcurve(
    points: tuple,
) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    """Decompose a multi-point quadratic spline into individual segments.

    TrueType fonts use quadratic bezier splines. A qCurveTo with N>2 points
    represents a chain where consecutive off-curve points have implied
    on-curve midpoints.

    Returns list of (control_point, end_point) pairs.
    """
    if len(points) <= 1:
        return []
    if len(points) == 2:
        return [(points[0], points[1])]

    segments = []
    for i in range(len(points) - 1):
        if i < len(points) - 2:
            # Implied on-curve midpoint between consecutive off-curve points
            implied = (
                (points[i][0] + points[i + 1][0]) / 2,
                (points[i][1] + points[i + 1][1]) / 2,
            )
            segments.append((points[i], implied))
        else:
            # Last segment: off-curve to final on-curve
            segments.append((points[i], points[i + 1]))
    return segments


def _transform_commands(
    raw_commands: list,
    x_min: float,
    y_min: float,
    x_max: float,
    y_max: float,
    path_w: int,
    path_h: int,
) -> list[tuple[str, list]]:
    """Transform font coordinates to OpenXML path coordinates.

    Applies: translate to origin, flip Y, scale to path_size.
    """
    glyph_w = x_max - x_min
    glyph_h = y_max - y_min
    if glyph_w == 0 or glyph_h == 0:
        return []

    def tx(fx: float) -> int:
        return round((fx - x_min) / glyph_w * path_w)

    def ty(fy: float) -> int:
        return round((y_max - fy) / glyph_h * path_h)

    result = []
    start_pt = None  # Track moveTo point for qCurveTo None closure
    cur_x, cur_y = 0, 0  # Track current position for quad→cubic conversion
    for op, args in raw_commands:
        if op == "moveTo":
            pt = args[0]
            start_pt = pt
            cur_x, cur_y = tx(pt[0]), ty(pt[1])
            result.append(("moveTo", [(cur_x, cur_y)]))

        elif op == "lineTo":
            pt = args[0]
            cur_x, cur_y = tx(pt[0]), ty(pt[1])
            result.append(("lineTo", [(cur_x, cur_y)]))

        elif op == "qCurveTo":
            # RecordingPen records qCurveTo as ("qCurveTo", ((x1,y1), (x2,y2), ...))
            # Last point may be None — means "close back to moveTo point"
            points = list(args)
            if points and points[-1] is None:
                # Closed quadratic spline — need a starting point
                if start_pt is None:
                    # No preceding moveTo: compute start as midpoint of
                    # first and last off-curve points (TrueType convention)
                    first = points[0]
                    last = points[-2]  # -2 because -1 is None
                    start_pt = (
                        (first[0] + last[0]) / 2,
                        (first[1] + last[1]) / 2,
                    )
                    cur_x, cur_y = tx(start_pt[0]), ty(start_pt[1])
                    result.append(("moveTo", [(cur_x, cur_y)]))
                points[-1] = start_pt
            segments = _decompose_qcurve(tuple(points))
            for control, end in segments:
                cx, cy = tx(control[0]), ty(control[1])
                ex, ey = tx(end[0]), ty(end[1])
                # Convert quadratic bezier to cubic bezier for PowerPoint compatibility
                # C1 = (cur + 2*control) / 3, C2 = (2*control + end) / 3
                c1x = round((cur_x + 2 * cx) / 3)
                c1y = round((cur_y + 2 * cy) / 3)
                c2x = round((2 * cx + ex) / 3)
                c2y = round((2 * cy + ey) / 3)
                result.append(("cubicBezTo", [(c1x, c1y), (c2x, c2y), (ex, ey)]))
                cur_x, cur_y = ex, ey

        elif op == "curveTo":
            # CFF fonts use cubic beziers — map directly
            p1x, p1y = tx(args[0][0]), ty(args[0][1])
            p2x, p2y = tx(args[1][0]), ty(args[1][1])
            p3x, p3y = tx(args[2][0]), ty(args[2][1])
            result.append(("cubicBezTo", [(p1x, p1y), (p2x, p2y), (p3x, p3y)]))
            cur_x, cur_y = p3x, p3y

        elif op == "closePath":
            result.append(("close", []))
            start_pt = None  # Reset for next contour

    return result


def glyph_outline_to_custgeom_xml(outline: GlyphOutline) -> etree._Element:
    """Convert a GlyphOutline to an lxml <a:custGeom> element."""
    NS = f"{{{_A_NS}}}"

    cust_geom = etree.Element(f"{NS}custGeom", nsmap={"a": _A_NS})

    # Required empty children
    etree.SubElement(cust_geom, f"{NS}avLst")
    etree.SubElement(cust_geom, f"{NS}gdLst")
    etree.SubElement(cust_geom, f"{NS}ahLst")
    etree.SubElement(cust_geom, f"{NS}cxnLst")

    # Text inset rect (no inset for icons)
    rect = etree.SubElement(cust_geom, f"{NS}rect")
    rect.set("l", "0")
    rect.set("t", "0")
    rect.set("r", "0")
    rect.set("b", "0")

    # Path list
    path_lst = etree.SubElement(cust_geom, f"{NS}pathLst")
    path_elem = etree.SubElement(path_lst, f"{NS}path")
    path_elem.set("w", str(outline.path_w))
    path_elem.set("h", str(outline.path_h))
    path_elem.set("fill", "norm")
    path_elem.set("extrusionOk", "false")

    for cmd_type, points in outline.commands:
        if cmd_type == "moveTo":
            move_to = etree.SubElement(path_elem, f"{NS}moveTo")
            pt = etree.SubElement(move_to, f"{NS}pt")
            pt.set("x", str(points[0][0]))
            pt.set("y", str(points[0][1]))

        elif cmd_type == "lineTo":
            ln_to = etree.SubElement(path_elem, f"{NS}lnTo")
            pt = etree.SubElement(ln_to, f"{NS}pt")
            pt.set("x", str(points[0][0]))
            pt.set("y", str(points[0][1]))

        elif cmd_type == "quadBezTo":
            q_bez = etree.SubElement(path_elem, f"{NS}quadBezTo")
            for px, py in points:
                pt = etree.SubElement(q_bez, f"{NS}pt")
                pt.set("x", str(px))
                pt.set("y", str(py))

        elif cmd_type == "cubicBezTo":
            c_bez = etree.SubElement(path_elem, f"{NS}cubicBezTo")
            for px, py in points:
                pt = etree.SubElement(c_bez, f"{NS}pt")
                pt.set("x", str(px))
                pt.set("y", str(py))

        elif cmd_type == "close":
            etree.SubElement(path_elem, f"{NS}close")

    return cust_geom
