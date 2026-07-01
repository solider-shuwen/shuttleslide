"""EMU ↔ CSS px geometry helpers for the agent pipeline.

914400 EMU = 1 inch; 96 CSS px = 1 inch  →  9525 EMU per CSS px.
These helpers translate between the PPTX world (EMU) and the browser
world (CSS px). Used by ``dsl_to_html.py`` and the renderer when the
canvas dimensions are non-default (e.g. portrait, square).
"""
from __future__ import annotations


# 914400 EMU per inch / 96 CSS px per inch = 9525.
# Kept here as the single source of truth so dsl_to_html.py and the
# renderer do not re-derive it.
EMU_PER_CSS_PX = 9525


def emu_to_px(emu: int) -> int:
    """Convert EMU to CSS px (96 DPI)."""
    return emu // EMU_PER_CSS_PX


def px_to_emu(px: int) -> int:
    """Convert CSS px to EMU."""
    return px * EMU_PER_CSS_PX


# Longest side of the canvas is fixed at this baseline; the other side is
# scaled by the aspect ratio. Chosen so 16:9 → 1280x720 (the historical
# default in AgentConfig) and 9:16 → 720x1280 (canonical vertical-video size).
_CANVAS_BASELINE_PX = 1280


def aspect_ratio_to_dimensions(ratio: str) -> tuple[int, int]:
    """Parse ``"W:H"`` and return ``(width_emu, height_emu)``.

    Longest side is fixed at ``_CANVAS_BASELINE_PX`` CSS px. Examples:

      "16:9" → (12192000, 6858000)   # 1280x720 px (landscape, the default)
      "9:16" → (6858000, 12192000)   # 720x1280 px (portrait)
      "1:1"  → (12192000, 12192000)  # 1280x1280 px (square)
      "3:4"  → (9144000, 12192000)   # 960x1280 px (portrait)
      "4:3"  → (12192000, 9144000)   # 1280x960 px (landscape)

    Args:
        ratio: Aspect ratio string in ``"W:H"`` form (e.g. ``"9:16"``).

    Returns:
        Tuple of ``(width_emu, height_emu)``.

    Raises:
        ValueError: if the ratio string is malformed or non-positive.
    """
    parts = ratio.split(":")
    if len(parts) != 2:
        raise ValueError(
            f"aspect ratio must be 'W:H' (e.g. '9:16'), got {ratio!r}"
        )
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise ValueError(
            f"aspect ratio components must be integers, got {ratio!r}"
        ) from exc
    if w <= 0 or h <= 0:
        raise ValueError(
            f"aspect ratio must be positive integers, got {ratio!r}"
        )

    if w >= h:
        width_px = _CANVAS_BASELINE_PX
        height_px = round(_CANVAS_BASELINE_PX * h / w)
    else:
        width_px = round(_CANVAS_BASELINE_PX * w / h)
        height_px = _CANVAS_BASELINE_PX

    return (px_to_emu(width_px), px_to_emu(height_px))
