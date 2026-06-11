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


# Scene3D isometric camera preset → CSS transform mapping.
# PPT uses orthographic (parallel) projection for isometric cameras.
# We use perspective(99999px) to approximate orthographic projection,
# preventing the parent container's perspective from distorting elements.
# Isometric tilt angle: arctan(1/sqrt(2)) ≈ 35.264° (standard).
_ISOMETRIC_CAMERA_MAP = {
    'isometricRightUp':   "perspective(99999px) rotateX(35.264deg) rotateY(-45deg)",
    'isometricLeftUp':    "perspective(99999px) rotateX(35.264deg) rotateY(45deg)",
    'isometricTopUp':     "perspective(99999px) rotateX(54.736deg) rotateZ(45deg)",
    'isometricBottomUp':  "perspective(99999px) rotateX(-54.736deg) rotateZ(45deg)",
    'isometricRightDown': "perspective(99999px) rotateX(-35.264deg) rotateY(-45deg)",
    'isometricLeftDown':  "perspective(99999px) rotateX(-35.264deg) rotateY(45deg)",
}


def scene3d_to_css(camera_preset: str):
    """Convert a scene3D camera preset name to a CSS transform string.

    Returns None if the camera preset is not recognized.
    """
    return _ISOMETRIC_CAMERA_MAP.get(camera_preset)
