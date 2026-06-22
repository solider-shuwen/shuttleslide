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
