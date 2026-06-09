"""
Preset Shape Cache Manager

Caches converted SVG paths for preset shapes to avoid
repeated formula calculations and path conversions.
"""

import os
from typing import Dict, Optional

from shuttleslide.pptx_to_html.converters.ecma_geometry.parser import PresetShapeParser
from shuttleslide.pptx_to_html.converters.ecma_geometry.formula_engine import FormulaEngine
from shuttleslide.pptx_to_html.converters.ecma_geometry.coordinate_system import CoordinateSystem
from shuttleslide.pptx_to_html.converters.ecma_geometry.path_converter import PathConverter


class PresetShapeCache:
    """
    Cache manager for preset shape SVG paths.

    Loads official definitions from presetShapeDefinitions.xml,
    calculates formulas, converts paths, and caches results.
    """

    def __init__(self, xml_path: str):
        """
        Initialize the cache manager.

        Args:
            xml_path: Path to presetShapeDefinitions.xml file
        """
        self.xml_path = xml_path
        self.parser: Optional[PresetShapeParser] = None
        self.formula_engine: Optional[FormulaEngine] = None
        self.coord_system: Optional[CoordinateSystem] = None
        self.path_converter: Optional[PathConverter] = None
        self.cache: Dict[str, str] = {}
        self._initialized = False

    def _initialize(self):
        """Lazy initialization of all components."""
        if self._initialized:
            return

        # Initialize components
        self.parser = PresetShapeParser(self.xml_path)
        self.formula_engine = FormulaEngine()
        self.coord_system = CoordinateSystem()
        self.path_converter = PathConverter(self.coord_system)
        self._initialized = True

    def get_svg_path(
        self,
        shape_name: str,
        width: float,
        height: float,
        adjustments: Optional[Dict[str, int]] = None
    ) -> Optional[str]:
        """
        Get SVG path data for a preset shape.

        Args:
            shape_name: Name of the preset shape (e.g., 'rect', 'cloudCallout')
            width: Shape width in pixels
            height: Shape height in pixels
            adjustments: Optional user adjustment values

        Returns:
            SVG path data string or None if shape not found

        Note: For backward compatibility, this returns a combined path string.
            For shapes with fill="none" paths, use get_svg_paths() instead.
        """
        self._initialize()

        # Create cache key
        cache_key = self._make_cache_key(shape_name, width, height, adjustments)

        # Check cache
        if cache_key in self.cache:
            return self.cache[cache_key]

        # Calculate path
        path_data = self._calculate_shape_path(shape_name, width, height, adjustments)

        # Store in cache
        if path_data:
            self.cache[cache_key] = path_data

        return path_data

    def get_svg_paths(
        self,
        shape_name: str,
        width: float,
        height: float,
        adjustments: Optional[Dict[str, int]] = None
    ) -> Optional[Dict]:
        """
        Get detailed SVG path data for a preset shape, separating filled and stroke-only paths.

        Args:
            shape_name: Name of the preset shape (e.g., 'rect', 'cloudCallout')
            width: Shape width in pixels
            height: Shape height in pixels
            adjustments: Optional user adjustment values

        Returns:
            Dictionary with:
                - 'filled': SVG path data for paths with fill (fill="norm" or default)
                - 'stroke_only': SVG path data for paths without fill (fill="none")
                - 'all': Combined SVG path data (backward compatible)
            Or None if shape not found
        """
        self._initialize()

        # Calculate shape paths with detailed info
        paths_data = self._calculate_shape_paths_detailed(shape_name, width, height, adjustments)

        return paths_data

    def _calculate_shape_path(
        self,
        shape_name: str,
        width: float,
        height: float,
        adjustments: Optional[Dict[str, int]]
    ) -> Optional[str]:
        """
        Calculate SVG path for a shape.

        Args:
            shape_name: Name of the preset shape
            width: Shape width in pixels
            height: Shape height in pixels
            adjustments: Optional user adjustment values

        Returns:
            SVG path data string or None
        """
        # Get shape definition
        shape_def = self.parser.get_shape(shape_name)
        if shape_def is None:
            return None

        # Merge default adjustments with user adjustments
        av_lst = shape_def.get('av_lst', {})
        merged_adjustments = {**av_lst}
        if adjustments:
            merged_adjustments.update(adjustments)

        # Setup calculation context
        context = self.coord_system.setup_shape_context(width, height, merged_adjustments)

        # Evaluate formulas
        gd_lst = shape_def.get('gd_lst', [])
        if gd_lst:
            evaluated_formulas = self.formula_engine.evaluate_formulas(
                gd_lst, av_lst, context
            )
            context.update(evaluated_formulas)

        # Add angle constants to context so arcTo can resolve them
        # (e.g., stAng="3cd4" needs 3cd4=16200000 in context)
        context.update(self.formula_engine.ANGLE_CONSTANTS)

        # Convert paths — include both filled and stroke-only paths
        # (stroke-only fill="none" paths are the visible outline for shapes like braces)
        path_lst = shape_def.get('path_lst', [])
        if not path_lst:
            return None

        all_svg_parts = []
        for path in path_lst:
            svg_path = self.path_converter.convert_path_to_svg(
                path, context, width, height
            )
            if svg_path:
                all_svg_parts.append(svg_path)

        return ' '.join(all_svg_parts)

    def _calculate_shape_paths_detailed(
        self,
        shape_name: str,
        width: float,
        height: float,
        adjustments: Optional[Dict[str, int]]
    ) -> Optional[Dict]:
        """
        Calculate SVG paths for a shape, separating filled and stroke-only paths.

        Args:
            shape_name: Name of the preset shape
            width: Shape width in pixels
            height: Shape height in pixels
            adjustments: Optional user adjustment values

        Returns:
            Dictionary with 'filled', 'stroke_only', and 'all' path data, or None
        """
        # Get shape definition
        shape_def = self.parser.get_shape(shape_name)
        if shape_def is None:
            return None

        # Merge default adjustments with user adjustments
        av_lst = shape_def.get('av_lst', {})
        merged_adjustments = {**av_lst}
        if adjustments:
            merged_adjustments.update(adjustments)

        # Setup calculation context
        context = self.coord_system.setup_shape_context(width, height, merged_adjustments)

        # Evaluate formulas
        gd_lst = shape_def.get('gd_lst', [])
        if gd_lst:
            evaluated_formulas = self.formula_engine.evaluate_formulas(
                gd_lst, av_lst, context
            )
            context.update(evaluated_formulas)

        # Add angle constants to context so arcTo can resolve them
        context.update(self.formula_engine.ANGLE_CONSTANTS)

        # Convert paths, separating filled and stroke-only paths.
        # Many shapes (e.g., rightBrace, leftBrace) define two paths:
        #   1. A closed path with stroke="false" — the fillable area
        #   2. An open path with fill="none" — the visible outline/stroke
        # For stroke-only shapes (braces, brackets), the fill="none" path
        # is the one that should actually be rendered.
        path_lst = shape_def.get('path_lst', [])
        if not path_lst:
            return None

        filled_paths = []
        stroke_only_paths = []

        for path in path_lst:
            svg_path = self.path_converter.convert_path_to_svg(
                path, context, width, height
            )
            if not svg_path:
                continue
            if path.get('fill') == 'none':
                stroke_only_paths.append(svg_path)
            else:
                filled_paths.append(svg_path)

        all_paths = filled_paths + stroke_only_paths
        return {
            'filled': ' '.join(filled_paths) if filled_paths else '',
            'stroke_only': ' '.join(stroke_only_paths) if stroke_only_paths else '',
            'all': ' '.join(all_paths) if all_paths else ''
        }

    def _make_cache_key(
        self,
        shape_name: str,
        width: float,
        height: float,
        adjustments: Optional[Dict[str, int]]
    ) -> str:
        """
        Create a cache key for a shape request.

        Args:
            shape_name: Name of the preset shape
            width: Shape width
            height: Shape height
            adjustments: Adjustment values

        Returns:
            Cache key string
        """
        # Sort adjustments for consistent keys
        if adjustments:
            adj_str = ','.join(f"{k}:{v}" for k, v in sorted(adjustments.items()))
        else:
            adj_str = ''

        # Create key with limited precision for dimensions
        w_key = int(width)
        h_key = int(height)

        return f"{shape_name}_{w_key}_{h_key}_{adj_str}"

    def clear_cache(self):
        """Clear all cached paths."""
        self.cache.clear()

    def list_available_shapes(self) -> list:
        """
        Get list of all available shape names.

        Returns:
            List of shape names
        """
        self._initialize()
        return self.parser.list_shapes()
