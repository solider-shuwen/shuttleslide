"""
Unit conversion utilities for PPTX/EMU to CSS conversions.

PPTX uses EMU (English Metric Units) internally:
  1 inch  = 914400 EMU
  1 point = 12700 EMU
  1 pixel (at 96 DPI) = 9525 EMU

Angles in OpenXML are stored as 1/60000 of a degree.
"""


# Constants
EMU_PER_INCH = 914400
EMU_PER_POINT = 12700
PIXELS_PER_INCH = 96
EMU_PER_PIXEL = EMU_PER_INCH // PIXELS_PER_INCH  # 9525
ANGLE_UNITS_PER_DEGREE = 60000


def emu_to_px(emu):
    """Convert EMU to pixels (at 96 DPI)."""
    return emu / EMU_PER_PIXEL


def emu_to_pt(emu):
    """Convert EMU to points."""
    return emu / EMU_PER_POINT


def emu_to_inches(emu):
    """Convert EMU to inches."""
    return emu / EMU_PER_INCH


def px_to_emu(px):
    """Convert pixels (at 96 DPI) to EMU."""
    return px * EMU_PER_PIXEL


def angle_to_degrees(angle_units):
    """Convert OpenXML angle units (1/60000 degree) to degrees."""
    return angle_units / ANGLE_UNITS_PER_DEGREE
