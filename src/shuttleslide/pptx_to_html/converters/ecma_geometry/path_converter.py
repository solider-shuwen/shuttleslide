"""
DrawingML to SVG Path Converter

Converts DrawingML path commands to SVG path data strings.
"""

import math
from typing import Dict, List, Optional

from shuttleslide.pptx_to_html.converters.ecma_geometry.coordinate_system import CoordinateSystem
from shuttleslide.pptx_to_html.utils.units import angle_to_degrees


class PathConverter:
    """
    Converts DrawingML path commands to SVG path syntax.

    Mapping:
    - moveTo → M x,y
    - lnTo → L x,y
    - cubicBezTo → C x1,y1 x2,y2 x,y
    - quadBezTo → Q x1,y1 x,y
    - arcTo → A rx ry rot large-arc sweep x,y
    - close → Z
    """

    def __init__(self, coord_system: CoordinateSystem):
        """
        Initialize the path converter.

        Args:
            coord_system: Coordinate system for value resolution
        """
        self.coord = coord_system

    def convert_path_to_svg(
        self,
        path: Dict,
        context: Dict[str, float],
        width: float,
        height: float
    ) -> str:
        """
        Convert a DrawingML path to SVG path data.

        Args:
            path: DrawingML path definition
            context: Variable context with resolved values
            width: Shape width in pixels
            height: Shape height in pixels

        Returns:
            SVG path data string (the "d" attribute)
        """
        # Get path dimensions for normalization
        # DrawingML paths define their own coordinate space (often 21600x21600)
        path_w = path.get('width')
        path_h = path.get('height')

        # Parse path dimensions - they're usually strings like '21600'
        # These define the coordinate space for DIRECT coordinates in this path
        # Variable references from formulas are already in pixel space
        if path_w is None or path_w == '':
            path_w = 21600.0  # Default DrawingML path coordinate space
        else:
            try:
                path_w = float(path_w)
            except (ValueError, TypeError):
                path_w = 21600.0

        if path_h is None or path_h == '':
            path_h = 21600.0
        else:
            try:
                path_h = float(path_h)
            except (ValueError, TypeError):
                path_h = 21600.0

        svg_commands = []
        current_point = (0.0, 0.0)  # Track current point for arcTo

        for cmd in path.get('commands', []):
            svg_cmd, current_point = self._convert_command(
                cmd, context, width, height, current_point, path_w, path_h
            )
            if svg_cmd:
                svg_commands.append(svg_cmd)

        return ' '.join(svg_commands)

    def _convert_command(
        self,
        cmd: Dict,
        context: Dict[str, float],
        width: float,
        height: float,
        current_point: tuple,
        path_w: float,
        path_h: float
    ) -> tuple:
        """
        Convert a single path command.

        Args:
            cmd: DrawingML command dictionary
            context: Variable context
            width: Shape width
            height: Shape height
            current_point: Current (x, y) position
            path_w: Path width in DrawingML coordinate space (usually 43200)
            path_h: Path height in DrawingML coordinate space (usually 43200)

        Returns:
            Tuple of (SVG command string, new current point)
        """
        cmd_type = cmd.get('type')

        if cmd_type == 'moveTo':
            return self._convert_move_to(cmd, context, width, height, path_w, path_h)
        elif cmd_type == 'lnTo':
            return self._convert_line_to(cmd, context, width, height, path_w, path_h)
        elif cmd_type == 'cubicBezTo':
            return self._convert_cubic_bezier(cmd, context, width, height, path_w, path_h)
        elif cmd_type == 'quadBezTo':
            return self._convert_quadratic_bezier(cmd, context, width, height, path_w, path_h)
        elif cmd_type == 'arcTo':
            return self._convert_arc_to(cmd, context, width, height, current_point, path_w, path_h)
        elif cmd_type == 'close':
            return ('Z', current_point)

        return (None, current_point)

    def _convert_move_to(
        self,
        cmd: Dict,
        context: Dict[str, float],
        width: float,
        height: float,
        path_w: float,
        path_h: float
    ) -> tuple:
        """Convert moveTo command: M x,y

        Important: Coordinates can be either:
        - Direct numbers (x=3900): in 43200 space, need normalization
        - Variable references (x=x23): formula result in pixel space, use directly
        """
        x_str = cmd['x']
        y_str = cmd['y']

        x = self.coord.resolve_coordinate(x_str, context)
        y = self.coord.resolve_coordinate(y_str, context)

        # Check if this is a variable reference or direct coordinate
        is_var_x = self._is_variable_reference(x_str)
        is_var_y = self._is_variable_reference(y_str)

        if is_var_x:
            # Variable reference from formula - already in pixel space
            x_px = x
        else:
            # Direct coordinate in 43200 space - normalize to pixels
            x_px = (x / path_w) * width

        if is_var_y:
            # Variable reference from formula - already in pixel space
            y_px = y
        else:
            # Direct coordinate in 43200 space - normalize to pixels
            y_px = (y / path_h) * height

        return (f"M {x_px:.2f} {y_px:.2f}", (x_px, y_px))

    def _is_variable_reference(self, value: str) -> bool:
        """Check if a coordinate value is a variable reference."""
        if not value:
            return False
        # Variable references start with a letter (like "x23", "yPos", "g26")
        return value[0].isalpha() if value else False

    def _convert_line_to(
        self,
        cmd: Dict,
        context: Dict[str, float],
        width: float,
        height: float,
        path_w: float,
        path_h: float
    ) -> tuple:
        """Convert lnTo command: L x,y"""
        x_str = cmd['x']
        y_str = cmd['y']

        x = self.coord.resolve_coordinate(x_str, context)
        y = self.coord.resolve_coordinate(y_str, context)

        is_var_x = self._is_variable_reference(x_str)
        is_var_y = self._is_variable_reference(y_str)

        if is_var_x:
            x_px = x
        else:
            x_px = (x / path_w) * width

        if is_var_y:
            y_px = y
        else:
            y_px = (y / path_h) * height

        return (f"L {x_px:.2f} {y_px:.2f}", (x_px, y_px))

    def _convert_cubic_bezier(
        self,
        cmd: Dict,
        context: Dict[str, float],
        width: float,
        height: float,
        path_w: float,
        path_h: float
    ) -> tuple:
        """Convert cubicBezTo command: C x1,y1 x2,y2 x,y"""
        cp1 = cmd.get('control1', {})
        cp2 = cmd.get('control2', {})
        end = cmd.get('end', {})

        x1_str = cp1.get('x', '0')
        y1_str = cp1.get('y', '0')
        x2_str = cp2.get('x', '0')
        y2_str = cp2.get('y', '0')
        x_str = end.get('x', '0')
        y_str = end.get('y', '0')

        x1 = self.coord.resolve_coordinate(x1_str, context)
        y1 = self.coord.resolve_coordinate(y1_str, context)
        x2 = self.coord.resolve_coordinate(x2_str, context)
        y2 = self.coord.resolve_coordinate(y2_str, context)
        x = self.coord.resolve_coordinate(x_str, context)
        y = self.coord.resolve_coordinate(y_str, context)

        # Check each coordinate
        is_var_x1 = self._is_variable_reference(x1_str)
        is_var_y1 = self._is_variable_reference(y1_str)
        is_var_x2 = self._is_variable_reference(x2_str)
        is_var_y2 = self._is_variable_reference(y2_str)
        is_var_x = self._is_variable_reference(x_str)
        is_var_y = self._is_variable_reference(y_str)

        x1_px = x1 if is_var_x1 else (x1 / path_w) * width
        y1_px = y1 if is_var_y1 else (y1 / path_h) * height
        x2_px = x2 if is_var_x2 else (x2 / path_w) * width
        y2_px = y2 if is_var_y2 else (y2 / path_h) * height
        x_px = x if is_var_x else (x / path_w) * width
        y_px = y if is_var_y else (y / path_h) * height

        return (f"C {x1_px:.2f} {y1_px:.2f} {x2_px:.2f} {y2_px:.2f} {x_px:.2f} {y_px:.2f}", (x_px, y_px))

    def _convert_quadratic_bezier(
        self,
        cmd: Dict,
        context: Dict[str, float],
        width: float,
        height: float,
        path_w: float,
        path_h: float
    ) -> tuple:
        """Convert quadBezTo command: Q x1,y1 x,y"""
        cp = cmd.get('control', {})
        end = cmd.get('end', {})

        x1_str = cp.get('x', '0')
        y1_str = cp.get('y', '0')
        x_str = end.get('x', '0')
        y_str = end.get('y', '0')

        x1 = self.coord.resolve_coordinate(x1_str, context)
        y1 = self.coord.resolve_coordinate(y1_str, context)
        x = self.coord.resolve_coordinate(x_str, context)
        y = self.coord.resolve_coordinate(y_str, context)

        is_var_x1 = self._is_variable_reference(x1_str)
        is_var_y1 = self._is_variable_reference(y1_str)
        is_var_x = self._is_variable_reference(x_str)
        is_var_y = self._is_variable_reference(y_str)

        x1_px = x1 if is_var_x1 else (x1 / path_w) * width
        y1_px = y1 if is_var_y1 else (y1 / path_h) * height
        x_px = x if is_var_x else (x / path_w) * width
        y_px = y if is_var_y else (y / path_h) * height

        return (f"Q {x1_px:.2f} {y1_px:.2f} {x_px:.2f} {y_px:.2f}", (x_px, y_px))

    def _convert_arc_to(
        self,
        cmd: Dict,
        context: Dict[str, float],
        width: float,
        height: float,
        current_point: tuple,
        path_w: float,
        path_h: float
    ) -> tuple:
        """
        Convert arcTo command: A rx ry rot large-arc sweep x,y

        DrawingML arcTo parameters:
        - wR: x-radius (in path coordinate space)
        - hR: y-radius (in path coordinate space)
        - stAng: start angle (in 60,000ths of a degree)
        - swAng: sweep angle (in 60,000ths of a degree)

        DrawingML arcTo specification:
        The ellipse is defined by its radii (wR, hR). The arc starts at the current
        point on the ellipse perimeter at angle stAng and sweeps by swAng degrees.

        CRITICAL: The ellipse is positioned by working backwards from the current point.
        Given the current point and start angle, we calculate where the ellipse center
        must be to have the current point on its perimeter at that angle.

        Reference: ECMA-376 20.1.10.55 and StackOverflow discussions on DrawingML arcTo
        """
        wR_str = cmd.get('wR', '0')
        hR_str = cmd.get('hR', '0')
        stAng = self.coord.resolve_coordinate(cmd.get('stAng', '0'), context)
        swAng = self.coord.resolve_coordinate(cmd.get('swAng', '0'), context)

        wR = self.coord.resolve_coordinate(wR_str, context)
        hR = self.coord.resolve_coordinate(hR_str, context)

        # Check if radii are variable references or direct coordinates
        is_var_wR = self._is_variable_reference(wR_str)
        is_var_hR = self._is_variable_reference(hR_str)

        # Convert radii to pixel space
        if is_var_wR:
            wR_px = abs(wR)
        else:
            wR_px = (abs(wR) / path_w) * width

        if is_var_hR:
            hR_px = abs(hR)
        else:
            hR_px = (abs(hR) / path_h) * height

        # Current point is where the arc STARTS on the ellipse perimeter
        cur_x, cur_y = current_point

        # Convert angles to radians (DrawingML uses 60,000ths of a degree)
        start_angle = math.radians(angle_to_degrees(stAng))
        sweep_angle = math.radians(angle_to_degrees(swAng))

        # Calculate ellipse center position
        # Given: current point is on ellipse perimeter at start_angle
        # We need to find the ellipse center (cx, cy) such that:
        #   cur_x = cx + wR_px * cos(start_angle)
        #   cur_y = cy + hR_px * sin(start_angle)
        # Therefore:
        #   cx = cur_x - wR_px * cos(start_angle)
        #   cy = cur_y - hR_px * sin(start_angle)

        cx = cur_x - wR_px * math.cos(start_angle)
        cy = cur_y - hR_px * math.sin(start_angle)

        # Calculate end point
        # The end point is on the same ellipse at angle (start_angle + sweep_angle)
        end_angle = start_angle + sweep_angle
        end_x = cx + wR_px * math.cos(end_angle)
        end_y = cy + hR_px * math.sin(end_angle)

        rx = wR_px
        ry = hR_px
        x_axis_rot = 0  # DrawingML arcs are axis-aligned

        # SVG cannot render arcs where start point == end point (spec says
        # the arc is omitted).  Detect full-circle / full-ellipse cases and
        # split into two half-arcs.
        dist_sq = (end_x - cur_x) ** 2 + (end_y - cur_y) ** 2
        if dist_sq < 0.01 and abs(sweep_angle) > 0.01:
            # Full-circle workaround: draw two half-arcs through the
            # point diametrically opposite the start point.
            mid_angle = start_angle + sweep_angle / 2.0
            mid_x = cx + rx * math.cos(mid_angle)
            mid_y = cy + ry * math.sin(mid_angle)
            half_large = 1 if abs(sweep_angle) > math.pi else 0
            half_sweep = 1 if sweep_angle > 0 else 0

            svg_cmd = (
                f"A {rx:.2f} {ry:.2f} {x_axis_rot} {half_large} {half_sweep} {mid_x:.2f} {mid_y:.2f} "
                f"A {rx:.2f} {ry:.2f} {x_axis_rot} {half_large} {half_sweep} {end_x:.2f} {end_y:.2f}"
            )
        else:
            # Normal arc
            # swAng is in 60,000ths of a degree, so 180° = 180 * 60000 = 10,800,000
            large_arc = 1 if abs(swAng) > 10800000 else 0
            sweep = 1 if swAng > 0 else 0
            svg_cmd = f"A {rx:.2f} {ry:.2f} {x_axis_rot} {large_arc} {sweep} {end_x:.2f} {end_y:.2f}"

        # Update current point for next command
        new_current_point = (end_x, end_y)

        return (svg_cmd, new_current_point)
