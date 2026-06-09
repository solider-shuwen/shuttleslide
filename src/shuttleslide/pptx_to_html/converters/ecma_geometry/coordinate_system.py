"""
DrawingML Coordinate System

Handles coordinate conversion from DrawingML's normalized system
to pixel coordinates for SVG rendering.
"""

import math
from typing import Dict, Optional


class CoordinateSystem:
    """
    DrawingML coordinate system converter.

    DrawingML uses a coordinate system based on:
    - Boundary box coordinates: l (left), t (top), r (right), b (bottom)
    - Dimensions: w (width), h (height)
    - Centers: hc (horizontal center), vc (vertical center)
    - Half dimensions: wd2, hd2
    - Edge lengths: ss (shorter side), ls (longer side)

    Coordinates are typically in EMU (English Metric Units) or
    normalized to a coordinate space (often 0-21600 range).
    """

    def __init__(self):
        """Initialize the coordinate system."""
        self.base_unit = 21600  # DrawingML base coordinate unit
        self.coord_space = 43200  # DrawingML path coordinate space

    def setup_shape_context(
        self,
        width: float,
        height: float,
        adjustments: Optional[Dict[str, int]] = None
    ) -> Dict[str, float]:
        """
        Setup the calculation context for a shape.

        DrawingML formulas need context variables. These can be provided in either:
        - 43200 coordinate space (standard for preset shape definitions)
        - Pixel space (actual shape dimensions)

        We use pixel space for formula evaluation to match the original behavior,
        but path coordinates are normalized from 43200 space.

        Args:
            width: Shape width in pixels
            height: Shape height in pixels
            adjustments: Optional adjustment values from user

        Returns:
            Dictionary with all DrawingML built-in variables
        """
        # Use pixel space for formula evaluation
        context = {
            'l': 0,
            't': 0,
            'r': width,
            'b': height,
            'w': width,
            'h': height,
            'wd2': width / 2,
            'hd2': height / 2,
            'hc': width / 2,
            'vc': height / 2,
            'ss': min(width, height),
            'ls': max(width, height),
        }

        # Add adjustment values
        if adjustments:
            context.update(adjustments)

        return context

    def resolve_coordinate(self, coord: str, context: Dict[str, float]) -> float:
        """
        Resolve a coordinate value to a float.

        Args:
            coord: Coordinate reference (variable name or numeric string)
            context: Variable context

        Returns:
            Resolved coordinate value
        """
        if not coord:
            return 0.0

        coord = coord.strip()

        # Check if it's a variable in context
        if coord in context:
            return float(context[coord])

        # Try to parse as number
        try:
            return float(coord)
        except ValueError:
            return 0.0

    def normalize_to_pixels(
        self,
        value: float,
        reference_size: float,
        coord_unit: Optional[str] = None
    ) -> float:
        """
        Convert a coordinate value to pixels.

        DrawingML coordinates can be:
        - Direct pixel values
        - Normalized to base_unit (21600)
        - EMU units (914400 EMU = 1 inch)

        Args:
            value: Coordinate value
            reference_size: Reference dimension (width or height)
            coord_unit: Optional unit hint

        Returns:
            Value in pixels
        """
        # If the value is already in a reasonable pixel range, use as-is
        if 0 <= value <= reference_size * 2:
            return value

        # If value looks like it's in the base_unit system (0-21600 range)
        if value > 100 and value < 100000:
            # Normalize to reference size
            return (value / self.base_unit) * reference_size

        # Otherwise, assume it's already in pixels
        return value

    def calculate_angle_radians(self, angle_value: float) -> float:
        """
        Convert DrawingML angle to radians.

        DrawingML angles are in 1/60000 degrees.

        Args:
            angle_value: Angle value in DrawingML units

        Returns:
            Angle in radians
        """
        return angle_value * math.pi / 1800000

    def resolve_point(self, x: str, y: str, context: Dict[str, float]) -> tuple:
        """
        Resolve a point coordinate.

        Args:
            x: X coordinate (variable name or numeric)
            y: Y coordinate (variable name or numeric)
            context: Variable context

        Returns:
            Tuple of (x, y) coordinates
        """
        x_val = self.resolve_coordinate(x, context)
        y_val = self.resolve_coordinate(y, context)
        return (x_val, y_val)
