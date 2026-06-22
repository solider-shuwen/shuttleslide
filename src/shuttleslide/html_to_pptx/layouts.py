"""
EMU coordinate helpers for the PPTX renderer.

PPTX default slide:  12,192,000 × 6,858,000 EMU  (10" × 7.5")
1280×720 HTML slides map 1:1 at 96 DPI.

The previous version of this module also defined a LAYOUTS dict of named
regions per layout preset (title_bar, body, left_col, …). That machinery
was unused: every DSL element already carries an explicit `position` in
percentages, and the renderer's `_resolve_region` always read from there.
It has been removed to avoid misleading future contributors; if you need
layout-based fallback positioning, reintroduce it together with the
renderer code that consults it.
"""

from dataclasses import dataclass

# Standard slide dimensions in EMU (10" × 7.5")
SLIDE_WIDTH_EMU = 12192000
SLIDE_HEIGHT_EMU = 6858000

# HTML slide dimensions used in the LLM-generated files
HTML_WIDTH_PX = 1280
HTML_HEIGHT_PX = 720


def px_to_emu(px: float) -> int:
    """Convert CSS pixels (at 96 DPI) to EMU."""
    return int(px * 9525)


def pct_to_emu(pct: float, total_emu: int) -> int:
    """Convert a percentage (0-100) to EMU."""
    return int(total_emu * pct / 100.0)


@dataclass
class Region:
    """A rectangular region in EMU coordinates."""
    left: int
    top: int
    width: int
    height: int


def position_percent_to_region(
    x_pct: float, y_pct: float, w_pct: float, h_pct: float,
    slide_width: int = SLIDE_WIDTH_EMU,
    slide_height: int = SLIDE_HEIGHT_EMU,
) -> Region:
    """Convert percentage-based position to an EMU Region."""
    return Region(
        left=pct_to_emu(x_pct, slide_width),
        top=pct_to_emu(y_pct, slide_height),
        width=pct_to_emu(w_pct, slide_width),
        height=pct_to_emu(h_pct, slide_height),
    )
