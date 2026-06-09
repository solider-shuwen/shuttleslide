"""
Shape Converter - converts shape elements to SVG-based HTML.
"""

from typing import Optional, Dict
from html import escape
from shuttleslide.pptx_to_html.models import ShapeElement
from shuttleslide.pptx_to_html.converters.svg_generator import SVGShapeGenerator


class ShapeConverter:
    """
    Converts shape elements from PPTX to SVG-based HTML.
    """

    def __init__(self, use_svg: bool = True, use_base64: bool = False):
        """
        Initialize the shape converter.

        Args:
            use_svg: If True, generate SVG. If False, use CSS classes.
            use_base64: If True, embed images as base64. If False, save as separate files (default).
        """
        self.use_svg = use_svg
        self.svg_generator = SVGShapeGenerator(use_base64=use_base64)

    # Mapping of PPTX shape types to CSS classes (fallback when not using SVG)
    SHAPE_TYPE_MAP = {
        # Basic shapes
        "RECTANGLE": "shape-rectangle",
        "ROUNDED_RECTANGLE": "shape-rounded-rectangle",
        "OVAL": "shape-oval",
        "ELLIPSE": "shape-ellipse",
        "CIRCLE": "shape-circle",
        "TRIANGLE": "shape-triangle",
        "DIAMOND": "shape-diamond",
        "STAR": "shape-star",
        "ARROW": "shape-arrow",
        "LINE": "shape-line",
        "LINE (9)": "shape-line",  # MSO_SHAPE_TYPE.LINE
        "ARC": "shape-arc",

        # Additional common shapes
        "ROUNDED_SINGLE_CORNER_RECTANGLE": "shape-rounded-rectangle",
        "SNIP_SINGLE_CORNER_RECTANGLE": "shape-snipped-rectangle",
        "PLAQUE": "shape-plaque",
        "HEXAGON": "shape-hexagon",
        "OCTAGON": "shape-octagon",
        "PARALLELOGRAM": "shape-parallelogram",
        "TRAPEZOID": "shape-trapezoid",
        "DECAGON": "shape-decagon",
        "DODECAGON": "shape-dodecagon",
        "CHORD": "shape-chord",
        "SECTOR": "shape-sector",
        "PIE": "shape-pie",
        "BLOCK_ARC": "shape-block-arc",
        "DONUT": "shape-donut",
        "NO_SMOKING": "shape-no-smoking",

        # Arrow variations
        "RIGHT_ARROW": "shape-arrow-right",
        "LEFT_ARROW": "shape-arrow-left",
        "UP_ARROW": "shape-arrow-up",
        "DOWN_ARROW": "shape-arrow-down",
        "LEFT_RIGHT_ARROW": "shape-arrow-left-right",
        "UP_DOWN_ARROW": "shape-arrow-up-down",
        "STRIPED_RIGHT_ARROW": "shape-arrow-striped-right",
        "NOTCHED_RIGHT_ARROW": "shape-arrow-notched-right",
        "BENT_ARROW": "shape-arrow-bent",
        "UTURN_ARROW": "shape-arrow-uturn",
        "LEFT_RIGHT_UP_ARROW": "shape-arrow-left-right-up",

        # Star variations
        "STAR_5_POINTS": "shape-star-5",
        "STAR_6_POINTS": "shape-star-6",
        "STAR_7_POINTS": "shape-star-7",
        "STAR_8_POINTS": "shape-star-8",
        "STAR_10_POINTS": "shape-star-10",
        "STAR_12_POINTS": "shape-star-12",
        "STAR_16_POINTS": "shape-star-16",
        "STAR_24_POINTS": "shape-star-24",
        "STAR_32_POINTS": "shape-star-32",
        "EXPLOSION1": "shape-explosion",
        "EXPLOSION2": "shape-explosion",

        # Callouts
        "ROUNDED_RECTANGULAR_CALLOUT": "shape-callout-rounded",
        "OVAL_CALLOUT": "shape-callout-oval",
        "CLOUD_CALLOUT": "shape-callout-cloud",
        "LINE_CALLOUT": "shape-callout-line",
        "BENT_BORDER_CALLOUT": "shape-callout-bent",
        "UNDERLINE_CALLOUT": "shape-callout-underline",
        "WEDGE_RECTANGLE_CALLOUT": "shape-callout-wedge",
        "WEDGE_ROUND_RECTANGLE_CALLOUT": "shape-callout-wedge-round",
        "WEDGE_ELLIPSE_CALLOUT": "shape-callout-wedge-ellipse",
        "BUTTON_CALLOUT": "shape-callout-button",

        # Flowchart shapes
        "FLOWCHART_PROCESS": "shape-flowchart-process",
        "FLOWCHART_DECISION": "shape-flowchart-decision",
        "FLOWCHART_DATA": "shape-flowchart-data",
        "FLOWCHART_PREDEFINED_PROCESS": "shape-flowchart-predefined",
        "FLOWCHART_INTERNAL_STORAGE": "shape-flowchart-storage",
        "FLOWCHART_DOCUMENT": "shape-flowchart-document",
        "FLOWCHART_MULTIDOCUMENT": "shape-flowchart-multidocument",
        "FLOWCHART_TERMINATOR": "shape-flowchart-terminator",
        "FLOWCHART_PREPARATION": "shape-flowchart-preparation",
        "FLOWCHART_MANUAL_INPUT": "shape-flowchart-manual-input",
        "FLOWCHART_MANUAL_OPERATION": "shape-flowchart-manual-operation",
        "FLOWCHART_CONNECTOR": "shape-flowchart-connector",
        "FLOWCHART_OFFPAGE_CONNECTOR": "shape-flowchart-offpage",
        "FLOWCHART_CARD": "shape-flowchart-card",
        "FLOWCHART_PUNCHED_TAPE": "shape-flowchart-punched-tape",
        "FLOWCHART_SUMMING_JUNCTION": "shape-flowchart-summing",
        "FLOWCHART_OR": "shape-flowchart-or",
        "FLOWCHART_COLLATE": "shape-flowchart-collate",
        "FLOWCHART_SORT": "shape-flowchart-sort",
        "FLOWCHART_EXTRACT": "shape-flowchart-extract",
        "FLOWCHART_MERGE": "shape-flowchart-merge",
        "FLOWCHART_DELAY": "shape-flowchart-delay",
        "FLOWCHART_STORED_DATA": "shape-flowchart-stored-data",
        "FLOWCHART_SEQUENTIAL_ACCESS": "shape-flowchart-sequential",
        "FLOWCHART_MAGNETIC_DISK": "shape-flowchart-disk",
        "FLOWCHART_DIRECT_ACCESS_STORAGE": "shape-flowchart-direct-access",
        "FLOWCHART_DISPLAY": "shape-flowchart-display",

        # Special shapes
        "TEAR_DROP": "shape-teardrop",
        "FRAME": "shape-frame",
        "HALF_FRAME": "shape-half-frame",
        "CORNER": "shape-corner",
        "CORNER_TABS": "shape-corner-tabs",
        "SQUARE_TABS": "shape-square-tabs",
        "PLAQUE_TABS": "shape-plaque-tabs",
        "CHART_PLUS": "shape-chart-plus",
        "CHART_STAR": "shape-chart-star",
        "CHART_X": "shape-chart-x",

        # Lines and connectors
        "BENT_CONNECTOR": "shape-connector-bent",
        "CURVED_CONNECTOR": "shape-connector-curved",
        "CURVED_CONNECTOR_2": "shape-connector-curved-2",
        "CURVED_CONNECTOR_3": "shape-connector-curved-3",
        "CURVED_CONNECTOR_4": "shape-connector-curved-4",
        "CURVED_CONNECTOR_5": "shape-connector-curved-5",
        "STRAIGHT_CONNECTOR": "shape-connector-straight",

        # Placeholder types for when parsing fails
        "GROUP_PLACEHOLDER": "shape-group-placeholder",
        "PICTURE_PLACEHOLDER": "shape-picture-placeholder",
        "GROUP": "shape-group",
    }

    def convert(self, element: ShapeElement, pct: Dict[str, float] = None) -> str:
        """
        Convert a shape element to HTML using SVG.

        Args:
            element: ShapeElement to convert
            pct: Dictionary with percentage position values

        Returns:
            HTML string representation
        """
        # Get geometry data from metadata
        geometry = element.metadata.get('geometry') if element.metadata else None

        # Generate SVG content
        svg_content = self.svg_generator.generate_svg(element, geometry)

        # Build wrapper div with positioning
        wrapper_attrs = []

        # Add positioning
        if pct is not None:
            # For LINE shapes or very thin shapes, ensure minimum height for visibility
            # 0.3% = ~2px on 720px slide
            is_line_shape = (
                'LINE' in element.shape_type.upper() or
                '(9)' in element.shape_type or
                pct['height_pct'] < 0.3  # Very thin shape
            )
            height_value = max(pct['height_pct'], 0.3) if is_line_shape else pct['height_pct']
            width_value = max(pct['width_pct'], 0.3) if is_line_shape else pct['width_pct']

            wrapper_styles = [
                f"position: absolute",
                f"left: {pct['left_pct']:.3f}%",
                f"top: {pct['top_pct']:.3f}%",
                f"width: {width_value:.3f}%",
                f"height: {height_value:.3f}%",
                f"z-index: {element.z_order}",
            ]
        else:
            # For LINE shapes or very thin shapes, ensure minimum dimensions for visibility
            is_line_shape = (
                'LINE' in element.shape_type.upper() or
                '(9)' in element.shape_type or
                element.height < 2 or element.width < 2
            )
            height_value = max(element.height, 2) if is_line_shape else element.height
            width_value = max(element.width, 2) if is_line_shape else element.width

            wrapper_styles = [
                f"position: absolute",
                f"left: {element.left}px",
                f"top: {element.top}px",
                f"width: {width_value}px",
                f"height: {height_value}px",
                f"z-index: {element.z_order}",
            ]

        wrapper_attrs.append(f'style="{"; ".join(wrapper_styles)}"')

        # Apply scene3D CSS transform (approximate isometric effects)
        # PPT uses orthographic (parallel) projection for isometric cameras —
        # no perspective/vanishing point. We use perspective(99999px) to
        # approximate orthographic projection, preventing the parent
        # container's perspective from distorting isometric elements.
        #
        # Isometric tilt angle: arctan(1/sqrt(2)) ≈ 35.264° (standard)
        transform_parts = []
        if element.metadata and element.metadata.get('scene3d_camera'):
            camera = element.metadata['scene3d_camera']
            if camera == 'isometricRightUp':
                transform_parts.append("perspective(99999px) rotateX(35.264deg) rotateY(-45deg)")
            elif camera == 'isometricLeftUp':
                transform_parts.append("perspective(99999px) rotateX(35.264deg) rotateY(45deg)")
            elif camera == 'isometricTopUp':
                transform_parts.append("perspective(99999px) rotateX(54.736deg) rotateZ(45deg)")
            elif camera == 'isometricBottomUp':
                transform_parts.append("perspective(99999px) rotateX(-54.736deg) rotateZ(45deg)")
            elif camera == 'isometricRightDown':
                transform_parts.append("perspective(99999px) rotateX(-35.264deg) rotateY(-45deg)")
            elif camera == 'isometricLeftDown':
                transform_parts.append("perspective(99999px) rotateX(-35.264deg) rotateY(45deg)")

        # flipH/flipV is handled by SVG transform in svg_generator.py
        # (_apply_svg_flip), which mirrors shape geometry within the SVG
        # coordinate system. Do NOT apply CSS scaleX/Y here — doing both
        # causes a double flip that cancels out.
        # LINE shapes are also handled by svg_generator.py, so no CSS flip
        # is needed for any shape type.

        # Apply rotation (PPT stores rotation in 1/60000 degree units,
        # parser already converts to degrees)
        if element.rotation:
            transform_parts.append(f"rotate({element.rotation}deg)")

        # Inject transforms into the style attribute
        if transform_parts:
            transform_str = " ".join(transform_parts)
            wrapper_attrs[-1] = wrapper_attrs[-1].rstrip('"') + f'; transform: {transform_str}"'

        # Add class for shape wrapper
        wrapper_attrs.append('class="slide-element shape-wrapper"')

        # Add data attributes for round-trip
        wrapper_attrs.extend(self._build_data_attributes(element))

        wrapper_attr_str = " ".join(wrapper_attrs)

        # If shape has text, add text overlay
        if element.text:
            escaped_text = escape(element.text)
            text_styles = [
                "position: absolute",
                "top: 50%",
                "left: 50%",
                "transform: translate(-50%, -50%)",
                "text-align: center",
                "pointer-events: none",
                "white-space: nowrap",
            ]
            text_html = f'<div style="{"; ".join(text_styles)}">{escaped_text}</div>'
            return f"<div {wrapper_attr_str}>{svg_content}{text_html}</div>"
        else:
            return f"<div {wrapper_attr_str}>{svg_content}</div>"

    def _get_shape_class(self, element: ShapeElement) -> str:
        """
        Get CSS class for shape type.

        Args:
            element: ShapeElement

        Returns:
            CSS class name
        """
        shape_type = element.shape_type.upper()

        return self.SHAPE_TYPE_MAP.get(shape_type, "shape-generic")

    def _build_shape_styles(self, element: ShapeElement, pct: Dict[str, float] = None) -> list[str]:
        """
        Build CSS styles for shape element with percentage-based responsive positioning.

        Args:
            element: ShapeElement with styling info
            pct: Dictionary with percentage position values (optional, uses pixels if not provided)

        Returns:
            List of CSS style declarations
        """
        # Use percentage positioning if provided, otherwise use pixels
        if pct is not None:
            styles = [
                f"position: absolute",
                f"left: {pct['left_pct']:.3f}%",
                f"top: {pct['top_pct']:.3f}%",
                f"width: {pct['width_pct']:.3f}%",
                f"height: {pct['height_pct']:.3f}%",
                f"z-index: {element.z_order}",
            ]
        else:
            styles = [
                f"position: absolute",
                f"left: {element.left}px",
                f"top: {element.top}px",
                f"width: {element.width}px",
                f"height: {element.height}px",
                f"z-index: {element.z_order}",
            ]

        # Add fill color
        if element.fill_color:
            styles.append(f"background-color: {element.fill_color}")

        # Add border (line color)
        if element.line_color:
            styles.extend([
                f"border: 1px solid {element.line_color}",
            ])

        # Add shape-specific styles
        shape_type = element.shape_type.upper()

        # Handle line shapes specially
        if shape_type in ["LINE", "LINE (9)"]:
            # For lines, use border-top or border-left depending on orientation
            if element.height < 1:  # Horizontal line
                # Use percentage positioning if provided
                if pct is not None:
                    styles = [
                        f"position: absolute",
                        f"left: {pct['left_pct']:.3f}%",
                        f"top: {pct['top_pct']:.3f}%",
                        f"width: {pct['width_pct']:.3f}%",
                        f"height: {max(pct['height_pct'], 0.1):.3f}%",  # Ensure minimum height for visibility
                        f"z-index: {element.z_order}",
                    ]
                else:
                    styles = [
                        f"position: absolute",
                        f"left: {element.left}px",
                        f"top: {element.top}px",
                        f"width: {element.width}px",
                        f"height: {max(element.height, 2)}px",  # Ensure minimum height for visibility
                        f"z-index: {element.z_order}",
                    ]
                # Use line color as border color
                line_color = element.line_color if element.line_color else (element.fill_color if element.fill_color else "#000")
                styles.append(f"border-top: 2px solid {line_color}")
                styles.append(f"background-color: transparent")
            elif element.width < 1:  # Vertical line
                # Use percentage positioning if provided
                if pct is not None:
                    styles = [
                        f"position: absolute",
                        f"left: {pct['left_pct']:.3f}%",
                        f"top: {pct['top_pct']:.3f}%",
                        f"width: {max(pct['width_pct'], 0.1):.3f}%",  # Ensure minimum width for visibility
                        f"height: {pct['height_pct']:.3f}%",
                        f"z-index: {element.z_order}",
                    ]
                else:
                    styles = [
                        f"position: absolute",
                        f"left: {element.left}px",
                        f"top: {element.top}px",
                        f"width: {max(element.width, 2)}px",  # Ensure minimum width for visibility
                        f"height: {element.height}px",
                        f"z-index: {element.z_order}",
                    ]
                line_color = element.line_color if element.line_color else (element.fill_color if element.fill_color else "#000")
                styles.append(f"border-left: 2px solid {line_color}")
                styles.append(f"background-color: transparent")
            return styles

        elif shape_type in ["OVAL", "ELLIPSE", "CIRCLE"]:
            styles.append("border-radius: 50%")

        elif shape_type == "ROUNDED_RECTANGLE":
            styles.append("border-radius: 10px")

        elif shape_type == "TRIANGLE":
            # Use clip-path for triangle
            styles.append("clip-path: polygon(50% 0%, 0% 100%, 100% 100%)")

        elif shape_type == "DIAMOND":
            # Use clip-path for diamond
            styles.append("clip-path: polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)")

        # Placeholder styles for when parsing fails
        elif "PLACEHOLDER" in shape_type:
            styles.extend([
                "border: 2px dashed #666666",
                "background-color: #f0f0f0",
                "display: flex",
                "align-items: center",
                "justify-content: center",
                "font-size: 14px",
                "color: #666666",
            ])

        elif shape_type == "GROUP":
            styles.extend([
                "border: 1px solid #999999",
                "background-color: rgba(200, 200, 200, 0.3)",
                "display: flex",
                "align-items: center",
                "justify-content: center",
            ])

        # Add text styles if shape has text
        if element.text:
            styles.extend([
                "display: flex",
                "align-items: center",
                "justify-content: center",
                "text-align: center",
            ])

        return styles

    def _build_data_attributes(self, element: ShapeElement) -> list[str]:
        """
        Build data-pptx-* attributes for round-trip conversion.

        Args:
            element: ShapeElement with metadata

        Returns:
            List of data attribute strings
        """
        attrs = []

        # Store position and size
        attrs.append(f'data-pptx-element-type="shape"')
        attrs.append(f'data-pptx-shape-type="{element.shape_type}"')
        attrs.append(f'data-pptx-left="{element.left}"')
        attrs.append(f'data-pptx-top="{element.top}"')
        attrs.append(f'data-pptx-width="{element.width}"')
        attrs.append(f'data-pptx-height="{element.height}"')
        attrs.append(f'data-pptx-z-order="{element.z_order}"')

        # Store colors
        if element.fill_color:
            attrs.append(f'data-pptx-fill-color="{element.fill_color}"')

        if element.line_color:
            attrs.append(f'data-pptx-line-color="{element.line_color}"')

        return attrs

    def convert_with_text_wrapper(self, element: ShapeElement, pct: Dict[str, float] = None) -> str:
        """
        Convert shape with separate wrapper for text content.

        Args:
            element: ShapeElement to convert

        Returns:
            HTML string with shape and text separated
        """
        if not element.text:
            return self.convert(element)

        # Build outer shape div
        shape_attrs = [
            f'class="{self._get_shape_class(element)}"',
            f'style="{"; ".join(self._build_shape_styles(element, pct))}"',
        ]

        # Remove text-related styles from shape div
        shape_attrs_cleaned = []
        for attr in shape_attrs:
            if "display: flex" not in attr and "align-items" not in attr:
                shape_attrs_cleaned.append(attr)

        shape_html = f"<div {' '.join(shape_attrs_cleaned)}></div>"

        # Build text wrapper
        # Use percentage positioning if provided, otherwise use pixels
        if pct is not None:
            text_styles = [
                f"position: absolute",
                f"left: {pct['left_pct']:.3f}%",
                f"top: {pct['top_pct']:.3f}%",
                f"width: {pct['width_pct']:.3f}%",
                f"height: {pct['height_pct']:.3f}%",
                f"z-index: {element.z_order + 1}",
                f"display: flex",
                f"align-items: center",
                f"justify-content: center",
                f"text-align: center",
            ]
        else:
            text_styles = [
                f"position: absolute",
                f"left: {element.left}px",
                f"top: {element.top}px",
                f"width: {element.width}px",
                f"height: {element.height}px",
                f"z-index: {element.z_order + 1}",
                f"display: flex",
                f"align-items: center",
                f"justify-content: center",
                f"text-align: center",
            ]

        text_attrs = [
            f'style="{"; ".join(text_styles)}"',
            f'data-pptx-shape-text="true"',
        ]

        escaped_text = escape(element.text)
        text_html = f"<div {' '.join(text_attrs)}>{escaped_text}</div>"

        return f"{shape_html}\n{text_html}"
