"""
SVG Shape Generator - converts PowerPoint shapes to SVG format.
Supports complex shapes including polygons, curves, and custom paths.
Also supports blipFill (image fills) with clip paths.

Uses official ECMA-376 preset shape definitions.
"""

import base64
import os
import re as _re
from typing import Dict, List, Optional, Any
from xml.etree import ElementTree as ET
from shuttleslide.pptx_to_html.models import ShapeElement
from shuttleslide.pptx_to_html.utils.namespaces import NS_A


class SVGShapeGenerator:
    """
    Generates SVG representations of PowerPoint shapes.
    Supports both preset and custom geometries with complex styling.
    """

    # Arrowhead SVG path definitions: type -> (path_d, refX, refY)
    _ARROWHEAD_PATHS = {
        'triangle': ('M 0 0 L 10 5 L 0 10 Z', 10, 5),
        'arrow':    ('M 0 0 L 10 5 L 0 10', 10, 5),
        'stealth':  ('M 0 5 L 10 0 L 8 5 L 10 10 Z', 10, 5),
        'diamond':  ('M 5 0 L 10 5 L 5 10 L 0 5 Z', 10, 5),
        'oval':     ('M 5 0 A 5 5 0 1 1 5 10 A 5 5 0 1 1 5 0 Z', 10, 5),
        'open':     ('M 0 0 L 10 5 L 0 10', 10, 5),
    }

    # Size multipliers for arrowhead width/length relative to line width
    _WIDTH_MULT = {'sm': 1.5, 'med': 2.5, 'lg': 3.5}
    _LENGTH_MULT = {'sm': 2.0, 'med': 3.0, 'lg': 4.0}

    def __init__(self, use_base64: bool = False, output_dir: Optional[str] = None):
        """
        Initialize the SVG shape generator.

        Args:
            use_base64: If True, embed images as base64. If False, save as separate files (default).
            output_dir: Directory path for saving image files (relative to HTML file).
        """
        # PowerPoint XML namespace
        self.ns = NS_A

        # Store settings
        self.use_base64 = use_base64
        self.output_dir = output_dir
        self.svg_image_counter = 0
        self._created_svg_dirs = set()

        # Initialize official preset shape cache
        self.preset_cache = None
        try:
            from shuttleslide.pptx_to_html.converters.ecma_geometry.preset_cache import PresetShapeCache

            # Path to presetShapeDefinitions.xml
            # File is at: src/shuttleslide/pptx_to_html/data/presetShapeDefinitions.xml
            # This file is at: src/shuttleslide/pptx_to_html/converters/svg_generator.py
            current_dir = os.path.dirname(os.path.abspath(__file__))
            xml_path = os.path.join(current_dir, '..', 'data', 'presetShapeDefinitions.xml')

            if os.path.exists(xml_path):
                self.preset_cache = PresetShapeCache(xml_path)
        except Exception as e:
            print(f"Warning: Could not initialize preset shape cache: {e}")

    def generate_svg(self, element: ShapeElement, geometry: Dict = None) -> str:
        """
        Generate SVG representation of a shape element.

        Args:
            element: ShapeElement with geometry data
            geometry: Pre-extracted geometry data dictionary

        Returns:
            SVG string representation
        """
        if geometry:
            # Use provided geometry data
            if geometry.get('type') == 'custom' and geometry.get('paths'):
                svg = self._generate_custom_svg(element, geometry)
            elif geometry.get('type') == 'preset':
                svg = self._generate_preset_svg(element, geometry)
            else:
                svg = self._generate_simple_svg(element)
        else:
            # Fallback: generate simple SVG for basic shapes
            svg = self._generate_simple_svg(element)

        # Apply flipH/flipV as SVG transform (not CSS) for correct visual rendering.
        # PPT flipH/flipV mirrors the shape content within its bounding box.
        if hasattr(element, 'flip_h') and (element.flip_h or element.flip_v):
            svg = self._apply_svg_flip(svg, element)

        return svg

    def _apply_svg_flip(self, svg: str, element: ShapeElement) -> str:
        """Apply flipH/flipV as SVG transform by wrapping inner content in a <g>.

        Uses SVG transform on the path/shape content inside the <svg> element,
        which correctly mirrors the geometry within the SVG coordinate system
        without affecting CSS layout or positioning.
        """
        import re as _re2
        w = element.width
        h = element.height

        # Build SVG transform string
        parts = []
        if element.flip_h:
            parts.append(f"translate({w:.2f}, 0) scale(-1, 1)")
        if element.flip_v:
            parts.append(f"translate(0, {h:.2f}) scale(1, -1)")
        if not parts:
            return svg

        transform_str = " ".join(parts)

        # Insert a <g transform="..."> right after the opening <svg ...> tag,
        # wrapping all inner content, then close </g> before </svg>
        # Match: <svg ...>content</svg>
        match = _re2.match(r'(<svg[^>]*>)(.*)(</svg>)', svg, _re2.DOTALL)
        if match:
            svg_open = match.group(1)
            inner = match.group(2)
            svg_close = match.group(3)
            return f'{svg_open}<g transform="{transform_str}">{inner}</g>{svg_close}'

        return svg

    def _extract_geometry_data(self, shape) -> Dict[str, Any]:
        """
        Extract geometry data from PowerPoint shape object.

        Args:
            shape: python-pptx shape object

        Returns:
            Dictionary with geometry information
        """
        if not hasattr(shape, '_element'):
            return None

        elem = shape._element
        geometry_data = {}

        # Check for preset geometry
        prst_geom = elem.find('.//a:prstGeom', self.ns)
        if prst_geom is not None:
            geometry_data['type'] = 'preset'
            geometry_data['prst'] = prst_geom.get('prst')

            # Extract adjustment values
            av_lst = prst_geom.find('a:avLst', self.ns)
            if av_lst is not None:
                adjustments = []
                for gd in av_lst.findall('a:gd', self.ns):
                    adjustments.append({
                        'name': gd.get('name'),
                        'formula': gd.get('fmla')
                    })
                geometry_data['adjustments'] = adjustments

        # Check for custom geometry
        cust_geom = elem.find('.//a:custGeom', self.ns)
        if cust_geom is not None:
            geometry_data['type'] = 'custom'

            # Extract path data
            path_lst = cust_geom.find('a:pathLst', self.ns)
            if path_lst is not None:
                paths = []
                for path in path_lst.findall('a:path', self.ns):
                    path_data = self._parse_path(path)
                    paths.append(path_data)
                geometry_data['paths'] = paths

            # Extract guide list (formula definitions)
            gd_lst = cust_geom.find('a:gdLst', self.ns)
            if gd_lst is not None:
                guides = []
                for gd in gd_lst.findall('a:gd', self.ns):
                    guides.append({
                        'name': gd.get('name'),
                        'formula': gd.get('fmla')
                    })
                geometry_data['guides'] = guides

        return geometry_data if geometry_data else None

    def _parse_path(self, path_elem) -> Dict[str, Any]:
        """
        Parse a PowerPoint path element.

        Args:
            path_elem: XML path element

        Returns:
            Dictionary with path data
        """
        path_data = {
            'width': path_elem.get('w'),
            'height': path_elem.get('h'),
            'commands': []
        }

        # Parse path commands
        for child in path_elem:
            tag_name = child.tag.split('}')[-1]  # Remove namespace

            if tag_name == 'moveTo':
                pt = child.find('a:pt', self.ns)
                if pt is not None:
                    path_data['commands'].append({
                        'type': 'M',
                        'x': pt.get('x'),
                        'y': pt.get('y')
                    })

            elif tag_name == 'lnTo':
                pt = child.find('a:pt', self.ns)
                if pt is not None:
                    path_data['commands'].append({
                        'type': 'L',
                        'x': pt.get('x'),
                        'y': pt.get('y')
                    })

            elif tag_name == 'cubicBezTo':
                points = child.findall('a:pt', self.ns)
                if len(points) >= 3:
                    path_data['commands'].append({
                        'type': 'C',
                        'x1': points[0].get('x'),
                        'y1': points[0].get('y'),
                        'x2': points[1].get('x'),
                        'y2': points[1].get('y'),
                        'x': points[2].get('x'),
                        'y': points[2].get('y')
                    })

            elif tag_name == 'arcTo':
                # Arc handling (complex, simplified version)
                path_data['commands'].append({
                    'type': 'A',
                    'wR': child.get('wR'),
                    'hR': child.get('hR'),
                    'stAng': child.get('stAng'),
                    'swAng': child.get('swAng'),
                    'x': child.get('x'),
                    'y': child.get('y')
                })

            elif tag_name == 'close':
                path_data['commands'].append({'type': 'Z'})

            elif tag_name == 'quadBezTo':
                points = child.findall('a:pt', self.ns)
                if len(points) >= 2:
                    path_data['commands'].append({
                        'type': 'Q',
                        'x1': points[0].get('x'),
                        'y1': points[0].get('y'),
                        'x': points[1].get('x'),
                        'y': points[1].get('y')
                    })

        return path_data

    def _generate_custom_svg(self, element: ShapeElement, geometry: Dict) -> str:
        """
        Generate SVG for custom geometry shapes.

        Args:
            element: ShapeElement
            geometry: Geometry data dictionary

        Returns:
            SVG string
        """
        width = element.width
        height = element.height

        # Build SVG path data
        path_d = ""
        for path in geometry.get('paths', []):
            # Get path dimensions for normalization
            path_width = float(path.get('w', width))
            path_height = float(path.get('h', height))

            # Avoid division by zero
            if path_width == 0:
                path_width = width
            if path_height == 0:
                path_height = height

            for cmd in path.get('commands', []):
                if cmd['type'] == 'M':
                    # Normalize coordinates
                    norm_x = (float(cmd['x']) / path_width) * width if path_width != 0 else 0
                    norm_y = (float(cmd['y']) / path_height) * height if path_height != 0 else 0
                    path_d += f"M {norm_x} {norm_y} "
                elif cmd['type'] == 'L':
                    norm_x = (float(cmd['x']) / path_width) * width if path_width != 0 else 0
                    norm_y = (float(cmd['y']) / path_height) * height if path_height != 0 else 0
                    path_d += f"L {norm_x} {norm_y} "
                elif cmd['type'] == 'C':
                    norm_x1 = (float(cmd['x1']) / path_width) * width if path_width != 0 else 0
                    norm_y1 = (float(cmd['y1']) / path_height) * height if path_height != 0 else 0
                    norm_x2 = (float(cmd['x2']) / path_width) * width if path_width != 0 else 0
                    norm_y2 = (float(cmd['y2']) / path_height) * height if path_height != 0 else 0
                    norm_x = (float(cmd['x']) / path_width) * width if path_width != 0 else 0
                    norm_y = (float(cmd['y']) / path_height) * height if path_height != 0 else 0
                    path_d += f"C {norm_x1} {norm_y1}, {norm_x2} {norm_y2}, {norm_x} {norm_y} "
                elif cmd['type'] == 'Q':
                    norm_x1 = (float(cmd['x1']) / path_width) * width if path_width != 0 else 0
                    norm_y1 = (float(cmd['y1']) / path_height) * height if path_height != 0 else 0
                    norm_x = (float(cmd['x']) / path_width) * width if path_width != 0 else 0
                    norm_y = (float(cmd['y']) / path_height) * height if path_height != 0 else 0
                    path_d += f"Q {norm_x1} {norm_y1}, {norm_x} {norm_y} "
                elif cmd['type'] == 'A':
                    # Arc commands need special handling
                    norm_x = (float(cmd['x']) / path_width) * width if path_width != 0 else 0
                    norm_y = (float(cmd['y']) / path_height) * height if path_height != 0 else 0
                    # Normalize radii
                    norm_wr = (float(cmd['wR']) / path_width) * width if path_width != 0 else 0
                    norm_hr = (float(cmd['hR']) / path_height) * height if path_height != 0 else 0
                    path_d += f"A {norm_wr} {norm_hr} {cmd['stAng']} {cmd['swAng']} {norm_x} {norm_y} "
                elif cmd['type'] == 'Z':
                    path_d += "Z "

        if not path_d:
            return self._generate_simple_svg(element)

        # Build SVG element
        svg_attrs = self._build_svg_attributes(element, width, height)
        # Use minimum dimensions for viewBox to ensure visibility
        svg_width = max(width, 2) if (height < 2 or width < 2) else width
        svg_height = max(height, 2) if (height < 2 or width < 2) else height
        svg_attrs.append(f'viewBox="0 0 {svg_width} {svg_height}"')
        svg_attrs.append(f'preserveAspectRatio="none"')
        # Allow shapes like cloudCallout to extend beyond their declared bounds
        svg_attrs.append(f'style="overflow: visible;"')

        # Build path element
        path_attrs = self._build_path_attributes(element)
        path_attrs.append(f'd="{path_d.strip()}"')

        svg_attr_str = " ".join(svg_attrs)
        path_attr_str = " ".join(path_attrs)

        # Check for blipFill (image fill)
        blip_content = self._generate_blip_fill_content(element, path_d.strip())
        marker_defs = self._generate_arrowhead_marker_defs(element)
        gradient_defs = self._generate_linear_gradient_defs(element)
        if blip_content:
            return f'<svg {svg_attr_str}>{gradient_defs}{marker_defs}{blip_content}</svg>'

        return f'<svg {svg_attr_str}>{gradient_defs}{marker_defs}<path {path_attr_str}/></svg>'

    def _generate_preset_svg(self, element: ShapeElement, geometry: Dict) -> str:
        """
        Generate SVG for preset geometry shapes.

        Args:
            element: ShapeElement
            geometry: Geometry data dictionary

        Returns:
            SVG string
        """
        # Get preset type
        prst = geometry.get('prst', 'rectangle')

        width = element.width
        height = element.height

        # Try to get a predefined path for the preset first
        paths_data = self._get_preset_paths(prst, width, height, geometry)

        # Calculate actual content bounding box to handle shapes
        # (like cloudCallout) whose paths extend beyond declared dimensions
        content_width = width
        content_height = height
        if paths_data and paths_data.get('all'):
            bbox = self._calculate_path_bbox(paths_data['all'])
            if bbox:
                content_width = max(width, bbox['max_x'] + 1)
                content_height = max(height, bbox['max_y'] + 1)

        # Use minimum dimensions for viewBox to ensure visibility
        svg_width = max(content_width, 2) if (height < 2 or width < 2) else content_width
        svg_height = max(content_height, 2) if (height < 2 or width < 2) else content_height

        svg_attrs = self._build_svg_attributes(element, width, height)
        # Override width/height with expanded content dimensions
        svg_attrs = [a for a in svg_attrs
                     if not a.startswith('width="') and not a.startswith('height="')]
        svg_attrs.append(f'width="{svg_width:.2f}"')
        svg_attrs.append(f'height="{svg_height:.2f}"')
        svg_attrs.append(f'viewBox="0 0 {svg_width:.2f} {svg_height:.2f}"')
        svg_attrs.append(f'preserveAspectRatio="none"')
        svg_attrs.append(f'style="overflow: visible;"')

        if paths_data:
            svg_attr_str = " ".join(svg_attrs)

            # Generate arrowhead marker defs (if any)
            marker_defs = self._generate_arrowhead_marker_defs(element)
            gradient_defs = self._generate_linear_gradient_defs(element)

            # Check for blipFill (image fill)
            # Use combined path for clip path
            blip_content = self._generate_blip_fill_content(element, paths_data.get('all', ''))

            if blip_content:
                # For shapes with image fill, start with the clipped image
                svg_content = blip_content

                # Add decorative stroke lines on top of the image
                if paths_data.get('stroke_only'):
                    stroke_attrs = self._build_stroke_attributes(element)
                    stroke_attrs.append(f'd="{paths_data["stroke_only"]}"')
                    stroke_attrs.append('fill="none"')
                    stroke_attr_str = " ".join(stroke_attrs)
                    svg_content += f'<path {stroke_attr_str}/>'

                return f'<svg {svg_attr_str}>{gradient_defs}{marker_defs}{svg_content}</svg>'

            # Build separate path elements for filled and stroke-only paths
            path_elements = []
            has_stroke_only = bool(paths_data.get('stroke_only'))
            has_fill = (element.fill_color and element.fill_color != "none") or (
                hasattr(element, 'fill_gradient') and element.fill_gradient)

            # Add filled path(s) — only render when the shape has a fill color.
            # When a separate stroke_only path exists (e.g., braces), the filled
            # path is the fillable area (XML stroke="false"). If no fill color,
            # skip it entirely — appending stroke="none" produces duplicate SVG
            # attributes which browsers ignore (first attr wins).
            if paths_data.get('filled') and (has_fill or not has_stroke_only):
                path_attrs = self._build_path_attributes(element)
                if has_stroke_only:
                    path_attrs.append('stroke="none"')
                path_attrs.append(f'd="{paths_data["filled"]}"')
                path_attr_str = " ".join(path_attrs)
                path_elements.append(f'<path {path_attr_str}/>'  )

            # Add stroke-only path(s) — the visible outline
            # For shapes like braces, this is the actual open stroke path
            if has_stroke_only:
                stroke_attrs = self._build_stroke_attributes(element)
                stroke_attrs.append(f'd="{paths_data["stroke_only"]}"')
                stroke_attrs.append('fill="none"')  # Explicitly no fill
                stroke_attr_str = " ".join(stroke_attrs)
                path_elements.append(f'<path {stroke_attr_str}/>')

            if path_elements:
                return f'<svg {svg_attr_str}>{gradient_defs}{marker_defs}{"".join(path_elements)}</svg>'

        # Fallback to simple SVG
        return self._generate_simple_svg(element)

    def _generate_simple_svg(self, element: ShapeElement) -> str:
        """
        Generate simple SVG fallback for basic shapes.

        Args:
            element: ShapeElement

        Returns:
            SVG string
        """
        width = element.width
        height = element.height
        shape_type = element.shape_type

        path_attrs = self._build_path_attributes(element)

        # Try to generate detailed paths based on the preset name in shape_type
        paths_data = self._get_preset_paths(shape_type, width, height)

        # Calculate actual content bounding box for shapes whose paths
        # extend beyond declared dimensions (e.g., cloudCallout tail)
        content_width = width
        content_height = height
        if paths_data and paths_data.get('all'):
            bbox = self._calculate_path_bbox(paths_data['all'])
            if bbox:
                content_width = max(width, bbox['max_x'] + 1)
                content_height = max(height, bbox['max_y'] + 1)

        svg_width = max(content_width, 2) if (height < 2 or width < 2) else content_width
        svg_height = max(content_height, 2) if (height < 2 or width < 2) else content_height

        svg_attrs = self._build_svg_attributes(element, width, height)
        svg_attrs = [a for a in svg_attrs
                     if not a.startswith('width="') and not a.startswith('height="')]
        svg_attrs.append(f'width="{svg_width:.2f}"')
        svg_attrs.append(f'height="{svg_height:.2f}"')
        svg_attrs.append(f'viewBox="0 0 {svg_width:.2f} {svg_height:.2f}"')
        svg_attrs.append(f'preserveAspectRatio="none"')
        svg_attrs.append(f'style="overflow: visible;"')

        if paths_data and (paths_data.get('filled') or paths_data.get('stroke_only')):
            svg_attr_str = " ".join(svg_attrs)
            path_elements = []
            has_stroke_only = bool(paths_data.get('stroke_only'))
            has_fill = (element.fill_color and element.fill_color != "none") or (
                hasattr(element, 'fill_gradient') and element.fill_gradient)

            # Generate arrowhead marker defs (if any)
            marker_defs = self._generate_arrowhead_marker_defs(element)
            gradient_defs = self._generate_linear_gradient_defs(element)

            # Add filled path(s) — skip when no fill and stroke_only exists
            if paths_data.get('filled') and (has_fill or not has_stroke_only):
                path_attrs = self._build_path_attributes(element)
                if has_stroke_only:
                    path_attrs.append('stroke="none"')
                path_attrs.append(f'd="{paths_data["filled"]}"')
                path_attr_str = " ".join(path_attrs)
                path_elements.append(f'<path {path_attr_str}/>')

            # Add stroke-only path(s) - the visible outline
            if has_stroke_only:
                stroke_attrs = self._build_stroke_attributes(element)
                stroke_attrs.append(f'd="{paths_data["stroke_only"]}"')
                stroke_attrs.append('fill="none"')  # Explicitly no fill
                stroke_attr_str = " ".join(stroke_attrs)
                path_elements.append(f'<path {stroke_attr_str}/>')

            # Check for blipFill (image fill)
            # Use combined path for clip path
            blip_content = self._generate_blip_fill_content(element, paths_data.get('all', ''))
            if blip_content:
                # For shapes with image fill, start with the clipped image
                svg_content = blip_content

                # Add decorative stroke lines on top of the image
                if paths_data.get('stroke_only'):
                    stroke_attrs = self._build_stroke_attributes(element)
                    stroke_attrs.append(f'd="{paths_data["stroke_only"]}"')
                    stroke_attrs.append('fill="none"')
                    stroke_attr_str = " ".join(stroke_attrs)
                    svg_content += f'<path {stroke_attr_str}/>'

                return f'<svg {svg_attr_str}>{gradient_defs}{marker_defs}{svg_content}</svg>'

            if path_elements:
                return f'<svg {svg_attr_str}>{gradient_defs}{marker_defs}{"".join(path_elements)}</svg>'

        # Fallback: try the old single-path method for backward compatibility
        path_d = self._get_preset_path_d(shape_type, width, height)
        if path_d:
            path_attrs.append(f'd="{path_d}"')
            svg_attr_str = " ".join(svg_attrs)
            path_attr_str = " ".join(path_attrs)

            # Check for blipFill (image fill)
            blip_content = self._generate_blip_fill_content(element, path_d)
            marker_defs = self._generate_arrowhead_marker_defs(element)
            gradient_defs = self._generate_linear_gradient_defs(element)
            if blip_content:
                return f'<svg {svg_attr_str}>{gradient_defs}{marker_defs}{blip_content}</svg>'

            return f'<svg {svg_attr_str}>{gradient_defs}{marker_defs}<path {path_attr_str}/></svg>'

        # Fallback to basic shapes by type name
        shape_type_upper = shape_type.upper()

        if shape_type_upper in ['RECTANGLE', 'ROUNDED_RECTANGLE']:
            rx = "10" if 'ROUNDED' in shape_type_upper else "0"
            return f'<svg {" ".join(svg_attrs)}><rect x="0" y="0" width="{width}" height="{height}" rx="{rx}" {" ".join(path_attrs)}/></svg>'

        elif shape_type_upper in ['OVAL', 'ELLIPSE', 'CIRCLE']:
            cx = width / 2
            cy = height / 2
            rx = width / 2
            ry = height / 2
            return f'<svg {" ".join(svg_attrs)}><ellipse cx="{cx}" cy="{cy}" rx="{rx}" ry="{ry}" {" ".join(path_attrs)}/></svg>'

        elif shape_type_upper == 'TRIANGLE':
            points = f"{width/2},0 {width},{height} 0,{height}"
            return f'<svg {" ".join(svg_attrs)}><polygon points="{points}" {" ".join(path_attrs)}/></svg>'

        elif shape_type_upper == 'DIAMOND':
            points = f"{width/2},0 {width},{height/2} {width/2},{height} 0,{height/2}"
            return f'<svg {" ".join(svg_attrs)}><polygon points="{points}" {" ".join(path_attrs)}/></svg>'

        elif 'LINE' in shape_type_upper:
            marker_defs = self._generate_arrowhead_marker_defs(element)
            return f'<svg {" ".join(svg_attrs)}>{marker_defs}<line x1="0" y1="{height/2}" x2="{width}" y2="{height/2}" {" ".join(path_attrs)}/></svg>'

        else:
            # Generic rectangle as fallback
            return f'<svg {" ".join(svg_attrs)}><rect x="0" y="0" width="{width}" height="{height}" {" ".join(path_attrs)}/></svg>'

    # ── Preset path definitions ──────────────────────────────────────
    #
    # These methods generate SVG path data (the "d" attribute) for
    # PowerPoint preset shapes.  Coordinates use the actual element
    # width/height so they don't need further normalisation.

    def _get_preset_paths(self, prst: str, width: float, height: float,
                          geometry: Dict = None) -> Optional[Dict]:
        """
        Get detailed SVG path data for a PowerPoint preset shape.

        Uses official ECMA-376 preset shape definitions.
        Returns separate paths for filled and stroke-only (decorative) paths.

        Args:
            prst: Preset shape name
            width: Shape width in pixels
            height: Shape height in pixels
            geometry: Optional geometry data with adjustments

        Returns:
            Dictionary with 'filled', 'stroke_only', and 'all' path data, or None
        """
        if self.preset_cache is None:
            return None

        # Extract adjustments from geometry
        adjustments = self._extract_adjustments(geometry)

        # Try original case first (preset names are case-sensitive in official XML)
        # Then try common variations (lowercase, camelCase)
        for name_variant in [prst, prst.lower(), prst[0].upper() + prst[1:] if prst else prst]:
            try:
                paths = self.preset_cache.get_svg_paths(name_variant, width, height, adjustments)
                if paths and (paths.get('filled') or paths.get('stroke_only')):
                    return paths
            except Exception:
                continue

        return None

    def _get_preset_path_d(self, prst: str, width: float, height: float,
                           geometry: Dict = None) -> Optional[str]:
        """
        Get SVG path data for a PowerPoint preset shape.

        Uses official ECMA-376 preset shape definitions.
        Returns None if the shape is not available.
        """
        if self.preset_cache is None:
            return None

        # Extract adjustments from geometry
        adjustments = self._extract_adjustments(geometry)

        # Try original case first (preset names are case-sensitive in official XML)
        # Then try common variations (lowercase, camelCase)
        for name_variant in [prst, prst.lower(), prst[0].upper() + prst[1:] if prst else prst]:
            try:
                path = self.preset_cache.get_svg_path(name_variant, width, height, adjustments)
                if path:
                    return path
            except Exception:
                continue

        return None

    def _extract_adjustments(self, geometry: Dict = None) -> Optional[Dict[str, int]]:
        """
        Extract adjustment values from geometry data.

        Args:
            geometry: Geometry data dictionary

        Returns:
            Dictionary of adjustment values or None
        """
        if not geometry or not geometry.get('adjustments'):
            return None

        adjustments = {}
        for adj in geometry['adjustments']:
            name = adj.get('name')
            value = adj.get('value')

            # If formula is a simple 'val XXXXX', extract the value
            formula = adj.get('formula', '')
            if formula.startswith('val '):
                try:
                    value = int(formula[4:])
                except ValueError:
                    pass

            if name and value is not None:
                adjustments[name] = value

        return adjustments if adjustments else None

    # ── BlipFill (image fill) support ────────────────────────────────

    def _generate_blip_fill_content(self, element: ShapeElement,
                                     clip_path_d: str = None) -> Optional[str]:
        """
        Generate SVG content for a shape with blipFill (image fill).

        Renders the image clipped to the shape's path (if available).
        For shapes like cloudCallout, the image is a rectangular texture
        that must be clipped to the shape outline to look correct.

        Args:
            element: ShapeElement with blip_fill data
            clip_path_d: SVG path data string for clipping (optional)

        Returns:
            SVG content string, or None if no blipFill
        """
        if not hasattr(element, 'blip_fill') or not element.blip_fill:
            return None

        image_bytes = element.blip_fill.get('image_bytes')
        image_type = element.blip_fill.get('image_type', 'png')

        if not image_bytes:
            return None

        # EMF/WMF can't be displayed natively in browsers
        if image_type in ('emf', 'wmf'):
            return None

        # Determine image href (either data URI or external file path)
        if self.use_base64:
            # Encode image as base64
            b64_data = base64.b64encode(image_bytes).decode('ascii')
            mime_type = f"image/{image_type}"
            if image_type == 'jpg':
                mime_type = "image/jpeg"

            image_href = f"data:{mime_type};base64,{b64_data}"
        else:
            # Save as external file
            image_href = self._save_svg_image(image_bytes, image_type)

        if clip_path_d:
            # Clip image to shape path
            clip_id = f"clip-{id(element)}-{hash(clip_path_d) % 100000}"

            # Expand image dimensions to cover paths that extend beyond
            # the shape's declared bounds (e.g., cloudCallout tail circles)
            img_width = element.width
            img_height = element.height
            bbox = self._calculate_path_bbox(clip_path_d)
            if bbox:
                img_width = max(img_width, bbox['max_x'] + 1)
                img_height = max(img_height, bbox['max_y'] + 1)

            return (
                f'<defs><clipPath id="{clip_id}">'
                f'<path d="{clip_path_d}"/>'
                f'</clipPath></defs>'
                f'<image href="{image_href}" '
                f'width="{img_width:.2f}" height="{img_height:.2f}" '
                f'clip-path="url(#{clip_id})" preserveAspectRatio="none"/>'
            )
        else:
            # No clip path available — render image directly
            return f'<image href="{image_href}" width="{element.width}" height="{element.height}" preserveAspectRatio="none"/>'

    def _save_svg_image(self, image_bytes: bytes, image_type: str) -> str:
        """
        Save SVG image as external file and return relative path.

        Args:
            image_bytes: Image data bytes
            image_type: Image file extension (e.g., "png", "jpeg")

        Returns:
            Relative file path for SVG href attribute
        """
        # Determine file extension
        ext = f".{image_type.lower()}" if image_type.lower() in ['png', 'jpeg', 'jpg'] else ".png"

        # Generate unique filename
        filename = f"svg-image-{self.svg_image_counter}{ext}"
        self.svg_image_counter += 1

        # Create assets directory if needed
        if self.output_dir is None:
            # Use default assets directory
            assets_dir = os.path.join("output_assets", "images")
        else:
            assets_dir = os.path.join(self.output_dir, "images")

        # Create directory if it doesn't exist
        if assets_dir not in self._created_svg_dirs:
            os.makedirs(assets_dir, exist_ok=True)
            self._created_svg_dirs.add(assets_dir)

        # Save image file
        filepath = os.path.join(assets_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(image_bytes)

        # Return relative path: "output_assets/images/filename"
        rel_path = os.path.join("output_assets", "images", filename)
        return rel_path.replace(os.sep, '/')

    # ── SVG attribute builders ───────────────────────────────────────

    def _get_marker_id(self, element: ShapeElement, end_key: str) -> str:
        """Generate a deterministic marker ID based on element identity."""
        return f"arrow-{id(element)}-{end_key}"

    def _generate_arrowhead_marker_defs(self, element: ShapeElement) -> str:
        """
        Generate SVG <defs><marker> elements for arrowhead endpoints.

        Returns:
            SVG defs string (empty string if no arrowheads)
        """
        if not hasattr(element, 'metadata') or not element.metadata:
            return ""

        markers = []

        # Get line width for sizing
        line_width = element.metadata.get('line_width', 2.0)
        if line_width < 1:
            line_width = 1.0
        stroke_color = element.line_color or '#666666'

        for end_key in ('head_end', 'tail_end'):
            end_data = element.metadata.get(end_key)
            if not end_data:
                continue

            arrow_type = end_data.get('type', 'triangle')
            path_info = self._ARROWHEAD_PATHS.get(arrow_type)
            if not path_info:
                continue

            path_d, ref_x, ref_y = path_info
            w_size = end_data.get('w', 'med')
            l_size = end_data.get('len', 'med')

            marker_w = line_width * self._WIDTH_MULT.get(w_size, 2.5)
            marker_h = line_width * self._LENGTH_MULT.get(l_size, 3.0)

            marker_id = self._get_marker_id(element, end_key)

            # Filled arrowheads use stroke color; open types have no fill
            is_filled = arrow_type in ('triangle', 'stealth', 'diamond', 'oval')
            fill_attr = f'fill="{stroke_color}"' if is_filled else 'fill="none"'

            markers.append(
                f'<marker id="{marker_id}" '
                f'viewBox="0 0 10 10" '
                f'refX="{ref_x}" refY="{ref_y}" '
                f'markerWidth="{marker_w:.1f}" markerHeight="{marker_h:.1f}" '
                f'orient="auto-start-reverse" '
                f'markerUnits="userSpaceOnUse">'
                f'<path d="{path_d}" {fill_attr} stroke="{stroke_color}" '
                f'stroke-width="0.5"/>'
                f'</marker>'
            )

        if not markers:
            return ""

        return f'<defs>{"".join(markers)}</defs>'

    def _get_arrowhead_marker_attrs(self, element: ShapeElement) -> List[str]:
        """Build marker-start/marker-end attributes for arrowheads."""
        attrs = []
        if not hasattr(element, 'metadata') or not element.metadata:
            return attrs

        for end_key, marker_attr in [('head_end', 'marker-start'), ('tail_end', 'marker-end')]:
            end_data = element.metadata.get(end_key)
            if not end_data:
                continue
            arrow_type = end_data.get('type', 'triangle')
            if arrow_type and arrow_type != 'none' and self._ARROWHEAD_PATHS.get(arrow_type):
                marker_id = self._get_marker_id(element, end_key)
                attrs.append(f'{marker_attr}="url(#{marker_id})"')

        return attrs

    def _build_svg_attributes(self, element: ShapeElement, width: float, height: float) -> List[str]:
        """Build SVG container attributes."""
        # For LINE shapes or very thin shapes, ensure minimum dimensions for visibility
        is_line_shape = (
            'LINE' in element.shape_type.upper() or
            '(9)' in element.shape_type or  # MSO_SHAPE_TYPE.LINE
            element.shape_type.strip() == '9'  # Numeric enum value
        )

        # Also check if shape is very thin (likely a line)
        is_very_thin = height < 2 or width < 2

        if is_line_shape or is_very_thin:
            svg_height = max(height, 2)
            svg_width = max(width, 2)
        else:
            svg_height = height
            svg_width = width

        return [
            f'width="{svg_width}"',
            f'height="{svg_height}"',
            f'class="shape-svg-inner"',
            f'data-pptx-shape-type="{element.shape_type}"',
        ]

    def _build_path_attributes(self, element: ShapeElement) -> List[str]:
        """Build SVG path/shape attributes."""
        attrs = []

        # Fill color - use original color if available
        # If shape has blipFill, the fill will be handled separately
        has_blip = hasattr(element, 'blip_fill') and element.blip_fill
        has_gradient = hasattr(element, 'fill_gradient') and element.fill_gradient

        if has_gradient:
            grad_id = f"grad-{id(element)}"
            attrs.append(f'fill="url(#{grad_id})"')
        elif element.fill_color:
            if element.fill_color == "none":
                # Explicit noFill from PPT (<a:noFill/>) — transparent, no default
                attrs.append('fill="none"')
            else:
                attrs.append(f'fill="{element.fill_color}"')
        elif has_blip:
            # Blip fill will overlay; use transparent fill on the path
            attrs.append('fill="none"')
        else:
            # No fill specified — default to transparent
            attrs.append('fill="none"')

        # Determine stroke width from metadata or default
        stroke_w = "2"
        if hasattr(element, 'metadata') and element.metadata and element.metadata.get('line_width'):
            lw = element.metadata['line_width']
            stroke_w = f"{lw:.1f}" if lw >= 1 else "1"

        # Stroke color - use original if available
        if element.line_color:
            attrs.append(f'stroke="{element.line_color}"')
            attrs.append(f'stroke-width="{stroke_w}"')
        else:
            # No stroke specified — lines still need a default stroke to be visible
            if element.shape_type in ['LINE', 'LINE (9)']:
                attrs.append('stroke="#666666"')
                attrs.append(f'stroke-width="{stroke_w}"')

        # Dash style (PPTX prstDash -> SVG stroke-dasharray)
        if hasattr(element, 'dash_style') and element.dash_style:
            dash_map = {
                'solid': None,
                'dash': '8,4',
                'dashDot': '8,4,2,4',
                'dashDotDot': '8,4,2,4,2,4',
                'dot': '2,4',
                'lgDash': '12,6',
                'lgDashDot': '12,6,2,6',
                'lgDashDotDot': '12,6,2,6,2,6',
                'sysDash': '4,4',
                'sysDashDot': '4,4,2,4',
                'sysDashDotDot': '4,4,2,4,2,4',
                'sysDot': '2,4',
            }
            dasharray = dash_map.get(element.dash_style)
            if dasharray:
                attrs.append(f'stroke-dasharray="{dasharray}"')

        # Opacity (if available in metadata)
        if hasattr(element, 'metadata') and element.metadata:
            if 'fill_opacity' in element.metadata:
                attrs.append(f'fill-opacity="{element.metadata["fill_opacity"]}"')
            if 'stroke_opacity' in element.metadata:
                attrs.append(f'stroke-opacity="{element.metadata["stroke_opacity"]}"')

        # Arrowhead markers
        attrs.extend(self._get_arrowhead_marker_attrs(element))

        return attrs

    def _build_stroke_attributes(self, element: ShapeElement) -> List[str]:
        """
        Build SVG attributes for stroke-only paths.

        For shapes like braces/brackets, this is the primary visible outline.
        Uses the element's actual stroke width, not a hardcoded thin value.

        Args:
            element: ShapeElement with style data

        Returns:
            List of SVG attribute strings
        """
        attrs = []

        # Stroke color - use original if available
        if element.line_color:
            attrs.append(f'stroke="{element.line_color}"')
        else:
            attrs.append('stroke="#666666"')

        # Stroke width - use the element's actual line width
        if hasattr(element, 'metadata') and element.metadata and element.metadata.get('line_width'):
            lw = element.metadata['line_width']
            attrs.append(f'stroke-width="{lw:.1f}"' if lw >= 1 else 'stroke-width="1"')
        else:
            attrs.append('stroke-width="2"')

        # Arrowhead markers
        attrs.extend(self._get_arrowhead_marker_attrs(element))

        return attrs

    def _generate_linear_gradient_defs(self, element: ShapeElement) -> str:
        """Generate SVG <defs><linearGradient> for shape gradient fill."""
        if not hasattr(element, 'fill_gradient_data') or not element.fill_gradient_data:
            return ""

        data = element.fill_gradient_data
        grad_id = f"grad-{id(element)}"

        # Convert CSS angle to SVG gradientTransform rotation
        # CSS 0deg = bottom-to-top, 90deg = left-to-right
        # SVG linearGradient default: left-to-right (x1=0,y1=0,x2=1,y2=0)
        # We use gradientTransform rotate to match CSS angle
        css_angle = data.get('angle', 180)
        svg_rotate = (css_angle - 90) % 360

        # Build stop elements
        stops_svg = []
        for stop in data.get('stops', []):
            color = stop['color']
            opacity = stop.get('opacity', 1.0)
            position = stop.get('position', 0.0)

            if opacity < 1.0:
                stops_svg.append(
                    f'<stop offset="{position:.1f}%" stop-color="{color}" stop-opacity="{opacity:.4f}"/>'
                )
            else:
                stops_svg.append(
                    f'<stop offset="{position:.1f}%" stop-color="{color}"/>'
                )

        stops_str = "".join(stops_svg)
        return (
            f'<defs><linearGradient id="{grad_id}" '
            f'gradientUnits="objectBoundingBox" x1="0" y1="0" x2="1" y2="0" '
            f'gradientTransform="rotate({svg_rotate:.1f}, 0.5, 0.5)">'
            f'{stops_str}</linearGradient></defs>'
        )

    def _generate_polygon_points(self, prst: str, width: float, height: float) -> str:
        """Generate polygon points for preset shapes."""
        # Simplified polygon generation
        if 'triangle' in prst.lower() or prst == 'triangle':
            return f"{width/2},0 {width},{height} 0,{height}"
        elif 'diamond' in prst.lower() or prst == 'diamond':
            return f"{width/2},0 {width},{height/2} {width/2},{height} 0,{height/2}"
        elif 'pentagon' in prst.lower() or prst == 'pentagon':
            # Regular pentagon
            points = []
            for i in range(5):
                angle = (i * 72 - 90) * 3.14159 / 180
                x = width/2 + (width/2 - 5) * (1 if i % 2 == 0 else 0.7) * (1 if i < 3 else -1)
                y = height/2 + (height/2 - 5) * (1 if i % 2 == 0 else 0.7) * (1 if i < 3 else -1)
                points.append(f"{x},{y}")
            return " ".join(points)
        elif 'hexagon' in prst.lower() or prst == 'hexagon':
            # Regular hexagon
            points = []
            for i in range(6):
                angle = (i * 60 - 90) * 3.14159 / 180
                x = width/2 + (width/2 - 5) * (0 if i in [2, 5] else 0.866) * (1 if i < 4 else -1)
                y = height/2 + (height/2 - 5) * (0.5 if i in [1, 4] else 0) * (1 if i < 3 else -1)
                points.append(f"{x},{y}")
            return " ".join(points)
        elif 'star' in prst.lower() or prst == 'star':
            # 5-pointed star
            points = []
            for i in range(10):
                angle = (i * 36 - 90) * 3.14159 / 180
                r = (width/2 - 5) if i % 2 == 0 else (width/4)
                x = width/2 + r * 0.951  # cos(angle)
                y = height/2 + r * 0.309  # sin(angle)
                points.append(f"{x},{y}")
            return " ".join(points)
        else:
            # Default to diamond
            return f"{width/2},0 {width},{height/2} {width/2},{height} 0,{height/2}"

    def _generate_preset_path(self, prst: str, width: float, height: float) -> str:
        """Generate SVG path for arrow and other complex preset shapes."""
        path_d = self._get_preset_path_d(prst, width, height)
        if path_d:
            return path_d
        # Default path
        return f"M 0,0 L {width},0 L {width},{height} L 0,{height} Z"

    @staticmethod
    def _calculate_path_bbox(path_d: str) -> Optional[Dict[str, float]]:
        """
        Calculate bounding box from SVG path d attribute.

        Parses M, L, A, C, Q commands and extracts coordinate points.
        For arc commands (A), only the endpoint is used.

        Args:
            path_d: SVG path data string

        Returns:
            Dict with min_x, min_y, max_x, max_y, or None if no coords found
        """
        min_x = min_y = float('inf')
        max_x = max_y = float('-inf')
        found = False

        # M x y / L x y - move/line commands
        for m in _re.finditer(r'[ML]\s+([-\d.]+)\s+([-\d.]+)', path_d):
            x, y = float(m.group(1)), float(m.group(2))
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            found = True

        # A rx ry rotation large_arc sweep x y - arc endpoint only
        for m in _re.finditer(
            r'A\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+[-\d.]+\s+([-\d.]+)\s+([-\d.]+)',
            path_d
        ):
            x, y = float(m.group(1)), float(m.group(2))
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)
            found = True

        # C x1 y1 x2 y2 x y - cubic bezier (all points)
        for m in _re.finditer(
            r'C\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)',
            path_d
        ):
            for i in range(1, 7, 2):
                x, y = float(m.group(i)), float(m.group(i + 1))
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
            found = True

        # Q x1 y1 x y - quadratic bezier
        for m in _re.finditer(r'Q\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)', path_d):
            for i in range(1, 5, 2):
                x, y = float(m.group(i)), float(m.group(i + 1))
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
            found = True

        if not found:
            return None

        return {'min_x': min_x, 'min_y': min_y, 'max_x': max_x, 'max_y': max_y}

    @staticmethod
    def supports_svg(shape_type: str) -> bool:
        """
        Check if a shape type supports SVG conversion.

        Args:
            shape_type: Shape type string

        Returns:
            True if SVG is supported, False otherwise
        """
        # All shapes support SVG in our implementation
        return True
