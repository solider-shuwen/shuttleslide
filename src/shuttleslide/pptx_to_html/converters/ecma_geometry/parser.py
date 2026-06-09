"""
Preset Shape Definition Parser

Parses ECMA-376 presetShapeDefinitions.xml file to extract
official PowerPoint preset shape definitions.
"""

import os
from typing import Dict, List, Optional
from xml.etree import ElementTree as ET

from shuttleslide.pptx_to_html.utils.namespaces import NAMESPACES


class PresetShapeParser:
    """
    Parser for ECMA-376 preset shape definitions.

    Extracts shape definitions from presetShapeDefinitions.xml
    including adjustment values, formulas, and path commands.
    """

    DRAWINGML_NS = NAMESPACES['a']

    def __init__(self, xml_path: str):
        """
        Initialize the parser.

        Args:
            xml_path: Path to presetShapeDefinitions.xml file
        """
        self.xml_path = xml_path
        self.shape_definitions: Dict[str, Dict] = {}
        self._parse_xml()

    def _parse_xml(self):
        """Parse the XML file and extract shape definitions."""
        if not os.path.exists(self.xml_path):
            raise FileNotFoundError(f"Shape definitions file not found: {self.xml_path}")

        tree = ET.parse(self.xml_path)
        root = tree.getroot()

        # Root element should be <presetShapeDefinitons> (note: typo in official spec)
        for shape_elem in root:
            shape_name = shape_elem.tag
            definition = self._parse_shape_definition(shape_elem)
            self.shape_definitions[shape_name] = definition

    def _parse_shape_definition(self, shape_elem) -> Dict:
        """
        Parse a single shape definition.

        Args:
            shape_elem: XML element for a shape

        Returns:
            Dictionary with shape definition data
        """
        return {
            'av_lst': self._parse_av_lst(shape_elem),
            'gd_lst': self._parse_gd_lst(shape_elem),
            'path_lst': self._parse_path_lst(shape_elem),
            'rect': self._parse_rect(shape_elem),
        }

    def _parse_av_lst(self, shape_elem) -> Dict[str, int]:
        """
        Parse adjustment value list (default parameters).

        Args:
            shape_elem: XML element for a shape

        Returns:
            Dictionary mapping adjustment name to default value
        """
        av_lst = {}

        # Find avLst element
        av_elem = shape_elem.find(f'{{{self.DRAWINGML_NS}}}avLst')
        if av_elem is None:
            return av_lst

        # Parse gd (guide) elements
        for gd in av_elem.findall(f'{{{self.DRAWINGML_NS}}}gd'):
            name = gd.get('name')
            formula = gd.get('fmla', '')

            # Extract default value from "val XXXXX" formula
            if formula.startswith('val '):
                try:
                    value = int(formula[4:])
                    av_lst[name] = value
                except ValueError:
                    pass

        return av_lst

    def _parse_gd_lst(self, shape_elem) -> List[Dict]:
        """
        Parse formula list (guide list).

        Args:
            shape_elem: XML element for a shape

        Returns:
            List of formula definitions
        """
        gd_lst = []

        # Find gdLst element
        gd_elem = shape_elem.find(f'{{{self.DRAWINGML_NS}}}gdLst')
        if gd_elem is None:
            return gd_lst

        # Parse gd (guide) elements
        for gd in gd_elem.findall(f'{{{self.DRAWINGML_NS}}}gd'):
            gd_lst.append({
                'name': gd.get('name'),
                'formula': gd.get('fmla', '')
            })

        return gd_lst

    def _parse_path_lst(self, shape_elem) -> List[Dict]:
        """
        Parse path list.

        Args:
            shape_elem: XML element for a shape

        Returns:
            List of path definitions
        """
        path_lst = []

        # Find pathLst element
        path_lst_elem = shape_elem.find(f'{{{self.DRAWINGML_NS}}}pathLst')
        if path_lst_elem is None:
            return path_lst

        # Parse path elements
        for path in path_lst_elem.findall(f'{{{self.DRAWINGML_NS}}}path'):
            path_data = self._parse_path(path)
            path_lst.append(path_data)

        return path_lst

    def _parse_path(self, path_elem) -> Dict:
        """
        Parse a single path definition.

        Args:
            path_elem: XML path element

        Returns:
            Dictionary with path data
        """
        path_data = {
            'width': path_elem.get('w'),
            'height': path_elem.get('h'),
            'fill': path_elem.get('fill', 'norm'),  # norm, none, darken, etc.
            'stroke': path_elem.get('stroke', 'true'),
            'extrusionOk': path_elem.get('extrusionOk', 'false'),
            'commands': []
        }

        # Parse path commands
        for child in path_elem:
            tag_name = child.tag.split('}')[-1]  # Remove namespace

            if tag_name == 'moveTo':
                cmd = self._parse_move_to(child)
                if cmd:
                    path_data['commands'].append(cmd)

            elif tag_name == 'lnTo':
                cmd = self._parse_line_to(child)
                if cmd:
                    path_data['commands'].append(cmd)

            elif tag_name == 'cubicBezTo':
                cmd = self._parse_cubic_bezier(child)
                if cmd:
                    path_data['commands'].append(cmd)

            elif tag_name == 'quadBezTo':
                cmd = self._parse_quadratic_bezier(child)
                if cmd:
                    path_data['commands'].append(cmd)

            elif tag_name == 'arcTo':
                cmd = self._parse_arc_to(child)
                if cmd:
                    path_data['commands'].append(cmd)

            elif tag_name == 'close':
                path_data['commands'].append({'type': 'close'})

        return path_data

    def _parse_move_to(self, elem) -> Optional[Dict]:
        """Parse moveTo command."""
        pt = elem.find(f'{{{self.DRAWINGML_NS}}}pt')
        if pt is not None:
            return {
                'type': 'moveTo',
                'x': pt.get('x'),
                'y': pt.get('y')
            }
        return None

    def _parse_line_to(self, elem) -> Optional[Dict]:
        """Parse lnTo command."""
        pt = elem.find(f'{{{self.DRAWINGML_NS}}}pt')
        if pt is not None:
            return {
                'type': 'lnTo',
                'x': pt.get('x'),
                'y': pt.get('y')
            }
        return None

    def _parse_cubic_bezier(self, elem) -> Optional[Dict]:
        """Parse cubicBezTo command."""
        points = elem.findall(f'{{{self.DRAWINGML_NS}}}pt')
        if len(points) >= 3:
            return {
                'type': 'cubicBezTo',
                'control1': {'x': points[0].get('x'), 'y': points[0].get('y')},
                'control2': {'x': points[1].get('x'), 'y': points[1].get('y')},
                'end': {'x': points[2].get('x'), 'y': points[2].get('y')}
            }
        return None

    def _parse_quadratic_bezier(self, elem) -> Optional[Dict]:
        """Parse quadBezTo command."""
        points = elem.findall(f'{{{self.DRAWINGML_NS}}}pt')
        if len(points) >= 2:
            return {
                'type': 'quadBezTo',
                'control': {'x': points[0].get('x'), 'y': points[0].get('y')},
                'end': {'x': points[1].get('x'), 'y': points[1].get('y')}
            }
        return None

    def _parse_arc_to(self, elem) -> Optional[Dict]:
        """Parse arcTo command."""
        return {
            'type': 'arcTo',
            'wR': elem.get('wR', '0'),
            'hR': elem.get('hR', '0'),
            'stAng': elem.get('stAng', '0'),
            'swAng': elem.get('swAng', '0')
        }

    def _parse_rect(self, shape_elem) -> Optional[Dict]:
        """
        Parse bounding rectangle.

        Args:
            shape_elem: XML element for a shape

        Returns:
            Dictionary with rectangle bounds or None
        """
        rect_elem = shape_elem.find(f'{{{self.DRAWINGML_NS}}}rect')
        if rect_elem is not None:
            return {
                'l': rect_elem.get('l', 'l'),
                't': rect_elem.get('t', 't'),
                'r': rect_elem.get('r', 'r'),
                'b': rect_elem.get('b', 'b')
            }
        return None

    def get_shape(self, shape_name: str) -> Optional[Dict]:
        """
        Get definition for a specific shape.

        Args:
            shape_name: Name of the shape (e.g., 'rect', 'cloudCallout')

        Returns:
            Shape definition dictionary or None if not found
        """
        return self.shape_definitions.get(shape_name)

    def list_shapes(self) -> List[str]:
        """
        Get list of all available shape names.

        Returns:
            List of shape names
        """
        return sorted(self.shape_definitions.keys())
