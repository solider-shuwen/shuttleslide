"""
Slideshow Layout - converts slides to HTML with interactive slideshow navigation.
Uses percentage-based responsive positioning for accurate layout preservation.
"""

from typing import List, Dict, Any, Optional

from shuttleslide.pptx_to_html.models import (
    ParsedSlide, SlideElement, TextElement, GroupElement,
    calculate_position_percentages,
)
from shuttleslide.pptx_to_html.layouts.base import BaseLayout
from shuttleslide.pptx_to_html.utils.units import scene3d_to_css


class SlideshowLayout(BaseLayout):
    """
    Generates HTML with interactive slideshow navigation.
    Only one slide is visible at a time with keyboard/mouse/touch navigation.
    """

    def __init__(self, enable_animations: bool = True, use_base64: bool = False, output_dir: str = None,
                 measurer=None):
        """
        Initialize the slideshow layout with converters and templates.

        Args:
            enable_animations: Whether to enable CSS animations for slide elements
            use_base64: Whether to embed images as base64 (True) or save as separate files (False, default).
            output_dir: Directory for saving image assets relative to the output HTML.
            measurer: Optional PlaywrightTextMeasurer (already started);
                enables shrink-on-overflow for text shapes.  See BaseLayout
                for lifecycle ownership.
        """
        super().__init__(use_base64=use_base64, output_dir=output_dir, measurer=measurer)
        self.enable_animations = enable_animations
        self.use_base64 = use_base64

    def convert(self, slides: List[ParsedSlide]) -> str:
        """
        Convert slides to HTML with slideshow navigation using templates.

        Args:
            slides: List of parsed slides

        Returns:
            Complete HTML document string
        """
        if not slides:
            return self._empty_html()

        # Get presentation dimensions from first slide
        first_slide = slides[0]
        slide_width = int(first_slide.width)
        slide_height = int(first_slide.height)

        # Prepare slides with HTML content
        slides_context = []
        for slide in slides:
            slide_context = {
                "slide_number": slide.slide_number,
                "hidden": slide.hidden,
                "has_animations": slide.has_animations,
                "elements": []
            }

            # Sort elements by z-order and convert each
            sorted_elements = sorted(slide.elements, key=lambda e: e.z_order)
            for element in sorted_elements:
                element_html = self._convert_element_absolute(element, slide)
                if element_html:
                    slide_context["elements"].append({
                        "html": element_html
                    })

            slides_context.append(slide_context)

            # Add background style if slide has a non-default background
            bg_style = self._get_background_style(slide)
            if bg_style:
                slide_context["background_style"] = bg_style

        # Load CSS template
        css_template = self.env.get_template("slideshow.css")
        styles = css_template.render(
            slide_width=slide_width,
            slide_height=slide_height,
            slide_width_plus_100=slide_width + 100,
            slide_width_80pct=int(slide_width * 0.8),
            slide_width_60pct=int(slide_width * 0.6)
        )

        # Load JavaScript template
        js_template = self.env.get_template("slideshow.js")
        script = js_template.render()

        # Load HTML template
        html_template = self.env.get_template("slideshow.html")
        return html_template.render(
            title="Presentation Slideshow",
            slides=slides_context,
            styles=styles,
            script=script
        )

    def _convert_element_absolute(self, element: SlideElement, slide: ParsedSlide) -> str:
        """
        Convert a single element to HTML with absolute positioning.

        Args:
            element: SlideElement to convert
            slide: ParsedSlide containing the slide for percentage calculations

        Returns:
            HTML string for the element with absolute positioning
        """
        if element.element_type == "text":
            return self._convert_text_absolute(element, slide)

        elif element.element_type == "table":
            return self._convert_table_absolute(element, slide)

        elif element.element_type == "image":
            return self._convert_image_absolute(element, slide)

        elif element.element_type == "shape":
            return self._convert_shape_absolute(element, slide)

        elif element.element_type == "group":
            return self._convert_group_absolute(element, slide)

        else:
            return ""

    def _convert_text_absolute(self, element: TextElement, slide: ParsedSlide) -> str:
        """
        Convert text element to HTML with percentage-based responsive positioning.

        Args:
            element: TextElement to convert
            slide: ParsedSlide containing slide dimensions for percentage calculations

        Returns:
            HTML string with percentage-based positioning
        """
        # Get basic text HTML
        text_html = self.text_converter.convert(element)

        # Calculate percentage positions
        pct = calculate_position_percentages(element, slide.width, slide.height)

        # Build position styles (percentage-based)
        position_styles = [
            f"position: absolute",
            f"left: {pct['left_pct']:.3f}%",
            f"top: {pct['top_pct']:.3f}%",
            f"width: {pct['width_pct']:.3f}%",
            self._height_style_for_element(element, f"{pct['height_pct']:.3f}%"),
            f"z-index: {element.z_order}",
        ]

        # Decoration styles from shared helper
        decoration_styles = self._build_text_decoration_styles(element)

        # Animation (SlideshowLayout-specific)
        animation_styles = []
        if self.enable_animations:
            animation_styles.append(f"animation: slideIn 0.5s ease-out {element.z_order * 0.05}s both")

        all_styles = position_styles + decoration_styles + animation_styles
        return self._build_text_wrapper_html(text_html, all_styles, "slide-element text-element")

    def _convert_table_absolute(self, element: SlideElement, slide: ParsedSlide) -> str:
        """
        Convert table element to HTML with percentage-based responsive positioning.

        Args:
            element: TableElement to convert
            slide: ParsedSlide containing slide dimensions for percentage calculations

        Returns:
            HTML string with percentage-based positioning
        """
        # Get basic table HTML
        table_html = self.table_converter.convert(element)

        # Calculate percentage positions
        pct = calculate_position_percentages(element, slide.width, slide.height)

        # Add positioning with percentages
        styles = [
            f"position: absolute",
            f"left: {pct['left_pct']:.3f}%",
            f"top: {pct['top_pct']:.3f}%",
            f"width: {pct['width_pct']:.3f}%",
            self._height_style_for_element(element, f"{pct['height_pct']:.3f}%"),
            f"z-index: {element.z_order}",
        ]

        if self.enable_animations:
            styles.append(f"animation: slideIn 0.5s ease-out {element.z_order * 0.05}s both")

        style_str = "; ".join(styles) + ";"

        return f"<div class='slide-element table-element' style='{style_str}'>{table_html}</div>"

    def _convert_image_absolute(self, element: SlideElement, slide: ParsedSlide) -> str:
        """
        Convert image element to HTML with percentage-based responsive positioning.

        Args:
            element: ImageElement to convert
            slide: ParsedSlide containing slide dimensions for percentage calculations

        Returns:
            HTML string with percentage-based positioning
        """
        # Calculate percentage positions
        pct = calculate_position_percentages(element, slide.width, slide.height)

        # Get image HTML with wrapper (using percentages)
        image_html = self.image_converter.convert_with_wrapper(element, pct)

        # Add slide-element class and animation if enabled
        if self.enable_animations:
            animation_style = f"animation: slideIn 0.5s ease-out {element.z_order * 0.05}s both;"
            # Insert animation into existing style
            image_html = image_html.replace(
                "style=\"",
                f"style=\"{animation_style}"
            )
        # Add slide-element class to existing class
        image_html = image_html.replace(
            'class="image-wrapper"',
            'class="slide-element image-wrapper"'
        )

        return image_html

    def _convert_shape_absolute(self, element: SlideElement, slide: ParsedSlide) -> str:
        """
        Convert shape element to HTML with percentage-based responsive positioning.

        Args:
            element: ShapeElement to convert
            slide: ParsedSlide containing slide dimensions for percentage calculations

        Returns:
            HTML string with percentage-based positioning
        """
        # Calculate percentage positions
        pct = calculate_position_percentages(element, slide.width, slide.height)

        # Get basic shape HTML with percentage positioning
        shape_html = self.shape_converter.convert(element, pct)

        # Add animation if enabled (slide-element class is already added by converter)
        if self.enable_animations:
            animation_style = f"animation: slideIn 0.5s ease-out {element.z_order * 0.05}s both;"
            # Add animation to shape wrapper div
            if "style='" in shape_html:
                shape_html = shape_html.replace(
                    "style='",
                    f"style='{animation_style}"
                )
            elif 'style="' in shape_html:
                shape_html = shape_html.replace(
                    'style="',
                    f'style="{animation_style}'
                )

        return shape_html

    def _convert_group_absolute(self, element: GroupElement, slide: ParsedSlide) -> str:
        """
        Convert group children to HTML.

        For groups with scene3d, renders a wrapper div with the 3D transform,
        keeping children in group-relative positions inside the wrapper.
        For other groups, flattens children to slide-absolute positions.

        Args:
            element: GroupElement with children
            slide: ParsedSlide for percentage calculations

        Returns:
            HTML string with all children rendered
        """
        if not element.children:
            return ""

        # Check if this group has a scene3d camera transform
        scene3d_camera = None
        if element.metadata and element.metadata.get('scene3d_camera'):
            scene3d_camera = element.metadata['scene3d_camera']

        if scene3d_camera:
            return self._convert_group_scene3d(element, slide, scene3d_camera)

        # Flatten: render children at slide-absolute positions
        children_html = []
        for child in element.children:
            # Save original group-relative position
            orig_left = child.left
            orig_top = child.top

            # Compute slide-absolute position
            child.left = element.left + child.left
            child.top = element.top + child.top

            # Render as a regular slide element at slide-absolute position
            if child.element_type == "group":
                child_html = self._convert_group_absolute(child, slide)
            else:
                child_html = self._convert_element_absolute(child, slide)

            # Restore original position (in case the element is reused)
            child.left = orig_left
            child.top = orig_top

            if child_html:
                children_html.append(child_html)

        return "\n".join(children_html)

    def _convert_group_scene3d(self, element: GroupElement, slide: ParsedSlide, camera: str) -> str:
        """
        Render a scene3d group as a wrapper div with 3D transform,
        children positioned relative to the wrapper.

        Args:
            element: GroupElement with scene3d
            slide: ParsedSlide for percentage calculations
            camera: scene3d camera preset name

        Returns:
            HTML string with 3D-transformed group wrapper
        """
        css_3d = scene3d_to_css(camera)

        # Wrapper positioned at group's slide-absolute position
        pct = calculate_position_percentages(element, slide.width, slide.height)

        wrapper_styles = [
            f"position: absolute",
            f"left: {pct['left_pct']:.3f}%",
            f"top: {pct['top_pct']:.3f}%",
            f"width: {pct['width_pct']:.3f}%",
            self._height_style_for_element(element, f"{pct['height_pct']:.3f}%"),
            f"z-index: {element.z_order}",
        ]

        if css_3d:
            wrapper_styles.append(f"transform: {css_3d}")
        wrapper_styles.append("transform-style: preserve-3d")

        if self.enable_animations:
            wrapper_styles.append(f"animation: slideIn 0.5s ease-out {element.z_order * 0.05}s both")

        # Remove scene3d from children so they don't get individual 3D transforms
        self._remove_scene3d_recursive(element.children)

        # Render children at group-relative positions
        children_html = []
        for child in element.children:
            # Children are already in group-relative coordinates
            # Convert to percentages relative to the group
            child_pct = calculate_position_percentages(child, element.width, element.height)

            if child.element_type == "image":
                child_html = self.image_converter.convert_with_wrapper(child, child_pct)
                child_html = child_html.replace(
                    'class="image-wrapper"',
                    'class="slide-element image-wrapper"'
                )
                if self.enable_animations:
                    anim = f"animation: slideIn 0.5s ease-out {child.z_order * 0.05}s both;"
                    child_html = child_html.replace('style="', f'style="{anim}')
            elif child.element_type == "shape":
                child_html = self.shape_converter.convert(child, child_pct)
            elif child.element_type == "text":
                child_html = self._convert_text_absolute(child, slide)
            else:
                continue

            if child_html:
                children_html.append(child_html)

        style_str = "; ".join(wrapper_styles)
        inner = "\n".join(children_html)
        return f"<div class='slide-element group-3d-wrapper' style='{style_str}'>\n{inner}\n</div>"

    def _expand_group_bounds(self, element: GroupElement, ref_width: float, ref_height: float) -> dict:
        """Expand group bounds to encompass all children, clamped to reference boundaries.

        The reference dimensions are either the slide (for top-level groups) or a
        parent group (for nested groups). The returned CSS box never exceeds the
        reference rectangle, but children can extend beyond via overflow: visible.

        Returns a dict with 'pct' (position percentages relative to ref dimensions)
        and width/height for children to calculate their percentages against.
        """
        children = element.children
        if not children:
            pct = calculate_position_percentages(element, ref_width, ref_height)
            return {'pct': pct, 'width': element.width, 'height': element.height}

        min_x = min(c.left for c in children)
        min_y = min(c.top for c in children)
        max_right = max(c.left + c.width for c in children)
        max_bottom = max(c.top + c.height for c in children)

        # Expanded bounding box in slide-relative pixels
        new_left = element.left + min_x
        new_top = element.top + min_y
        new_width = max_right - min_x
        new_height = max_bottom - min_y

        # Shift all children so they're non-negative relative to the expanded origin
        for c in children:
            c.left -= min_x
            c.top -= min_y

        # Clamp group container to reference boundaries (slide or parent group).
        # Children extending beyond will render via overflow: visible on the wrapper.
        clamped_left = max(0.0, new_left)
        clamped_top = max(0.0, new_top)
        clamped_width = max(0.0, min(new_width, ref_width - clamped_left))
        clamped_height = max(0.0, min(new_height, ref_height - clamped_top))

        # Adjust children to be relative to clamped origin
        offset_x = clamped_left - new_left
        offset_y = clamped_top - new_top
        if offset_x != 0 or offset_y != 0:
            for c in children:
                c.left -= offset_x
                c.top -= offset_y

        pct = {
            'left_pct': clamped_left / ref_width * 100 if ref_width else 0,
            'top_pct': clamped_top / ref_height * 100 if ref_height else 0,
            'width_pct': clamped_width / ref_width * 100 if ref_width else 0,
            'height_pct': clamped_height / ref_height * 100 if ref_height else 0,
        }

        return {'pct': pct, 'width': clamped_width, 'height': clamped_height}

    def _convert_group_child(self, child, group_info) -> str:
        """Convert a child element within a group, using group-relative percentages.

        Args:
            child: child element
            group_info: dict with 'width' and 'height' keys (expanded group dimensions),
                        or a GroupElement (for backwards compatibility)
        """
        # Support both dict (expanded) and GroupElement (direct) arguments
        if isinstance(group_info, dict):
            group_w = group_info['width']
            group_h = group_info['height']
        else:
            group_w = group_info.width
            group_h = group_info.height

        # Calculate child position as percentages of group dimensions
        child_pct = calculate_position_percentages(child, group_w, group_h)

        if child.element_type == "text":
            text_html = self.text_converter.convert(child)
            styles = [
                f"position: absolute",
                f"left: {child_pct['left_pct']:.3f}%",
                f"top: {child_pct['top_pct']:.3f}%",
                f"width: {child_pct['width_pct']:.3f}%",
                self._height_style_for_element(child, f"{child_pct['height_pct']:.3f}%"),
                f"z-index: {child.z_order}",
                "white-space: pre-wrap",
            ]
            if self._should_clip_element(child):
                styles.append("overflow: hidden")
            # Apply border/outline if present
            if child.line_color:
                border_width = child.line_width if child.line_width else 1
                styles.append(f"border: {border_width}px solid {child.line_color}")
            style_str = "; ".join(styles) + ";"
            return f"<div class='slide-element text-element' style=\"{style_str}\">{text_html}</div>"

        elif child.element_type == "image":
            return self.image_converter.convert_with_wrapper(child, child_pct)

        elif child.element_type == "shape":
            return self.shape_converter.convert(child, child_pct)

        elif child.element_type == "group":
            # Nested group - expand its own bounds to fit its children, then render
            if not child.children:
                return ""
            expanded = self._expand_group_bounds(child, group_w, group_h)
            child_style = [
                f"position: absolute",
                f"left: {child_pct['left_pct']:.3f}%",
                f"top: {child_pct['top_pct']:.3f}%",
                f"width: {expanded['pct']['width_pct']:.3f}%",
                f"height: {expanded['pct']['height_pct']:.3f}%",
                f"z-index: {child.z_order}",
                f"overflow: visible",
            ]
            child_style_str = "; ".join(child_style) + ";"
            inner_parts = []
            for sub_child in child.children:
                sub_html = self._convert_group_child(sub_child, expanded)
                if sub_html:
                    inner_parts.append(sub_html)
            inner = "\n".join(inner_parts)
            return f'<div class="slide-element group-wrapper" style="{child_style_str}">{inner}</div>'

        elif child.element_type == "table":
            table_html = self.table_converter.convert(child)
            styles = [
                f"position: absolute",
                f"left: {child_pct['left_pct']:.3f}%",
                f"top: {child_pct['top_pct']:.3f}%",
                f"z-index: {child.z_order}",
            ]
            style_str = "; ".join(styles) + ";"
            return f"<div class='slide-element table-element' style='{style_str}'>{table_html}</div>"

        return ""

    def _get_styles(self, slide_width: int, slide_height: int) -> str:
        """
        Get CSS styles for slideshow layout.

        Args:
            slide_width: Width of slides in pixels
            slide_height: Height of slides in pixels

        Returns:
            CSS style block
        """
        return f"""    <style>
        /* Base reset */
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            line-height: 1;
        }}

        /* Body and reveal container */
        html, body {{
            height: 100%;
            width: 100%;
            overflow: hidden;
        }}

        body {{
            background-color: #000;
            font-family: 'Arial', sans-serif;
        }}

        .reveal {{
            position: relative;
            width: 100%;
            height: 100%;
            overflow: hidden;
            touch-action: none;
        }}

        .reveal .slides {{
            position: relative;
            width: 100%;
            height: 100%;
            perspective: 1000px;
        }}

        /* Slide sections */
        .reveal section {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            opacity: 0;
            visibility: hidden;
            transition: opacity 0.5s ease-in-out, transform 0.5s ease-in-out;
            transform: translateX(100%);
            pointer-events: none;
        }}

        .reveal section.present {{
            opacity: 1;
            visibility: visible;
            transform: translateX(0);
            pointer-events: auto;
            z-index: 10;
        }}

        .reveal section.past {{
            transform: translateX(-100%);
        }}

        .reveal section.future {{
            transform: translateX(100%);
        }}

        /* Slide container */
        .slide-container {{
            position: relative;
            width: {slide_width}px;
            height: {slide_height}px;
            margin: 0 auto;
            background-color: white;
            overflow: hidden;
        }}

        /* Scale slides to fit viewport */
        .reveal {{
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .reveal .slides {{
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .reveal section {{
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        /* Slide elements */
        .slide-element {{
            position: absolute;
        }}

        /* Multi-level bullet paragraphs */
        .bullet-paragraph {{
            display: flex;
            align-items: baseline;
        }}

        .bullet-paragraph .bullet {{
            margin-right: 0.5em;
            flex-shrink: 0;
            line-height: 1;
        }}

        .bullet-paragraph .bullet img {{
            display: inline-block;
            vertical-align: middle;
        }}

        .bullet-paragraph .text {{
            flex-grow: 1;
        }}

        .text-paragraph {{
            margin: 0;
            padding: 0;
        }}

        /* Animations */
        @keyframes slideIn {{
            from {{
                opacity: 0;
                transform: translateY(20px);
            }}
            to {{
                opacity: 1;
                transform: translateY(0);
            }}
        }}

        /* Controls */
        .reveal .controls {{
            position: absolute;
            bottom: 20px;
            right: 20px;
            display: flex;
            gap: 10px;
            z-index: 1000;
        }}

        .reveal .controls button {{
            background-color: rgba(0, 0, 0, 0.5);
            color: white;
            border: none;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            font-size: 24px;
            cursor: pointer;
            transition: background-color 0.3s;
            display: flex;
            align-items: center;
            justify-content: center;
        }}

        .reveal .controls button:hover {{
            background-color: rgba(0, 0, 0, 0.8);
        }}

        .reveal .controls button:disabled {{
            opacity: 0.3;
            cursor: not-allowed;
        }}

        /* Progress bar */
        .reveal .progress {{
            position: absolute;
            bottom: 0;
            left: 0;
            height: 3px;
            background-color: rgba(255, 255, 255, 0.2);
            width: 100%;
            z-index: 1000;
        }}

        .reveal .progress-bar {{
            height: 100%;
            background-color: rgba(255, 255, 255, 0.8);
            transition: width 0.3s ease;
        }}

        /* Slide number */
        .reveal .slide-number {{
            position: absolute;
            bottom: 20px;
            left: 20px;
            color: rgba(255, 255, 255, 0.6);
            font-size: 14px;
            z-index: 1000;
        }}

        /* Responsive scaling */
        @media (max-width: {slide_width + 100}px) {{
            .slide-container {{
                transform: scale(0.8);
            }}
        }}

        @media (max-width: {int(slide_width * 0.8)}px) {{
            .slide-container {{
                transform: scale(0.6);
            }}
        }}

        @media (max-width: {int(slide_width * 0.6)}px) {{
            .slide-container {{
                transform: scale(0.4);
            }}
            .reveal .controls button {{
                width: 40px;
                height: 40px;
                font-size: 18px;
            }}
        }}

        /* Print styles */
        @media print {{
            body {{
                height: auto;
                overflow: visible;
                background-color: white;
            }}

            .reveal {{
                position: static;
                height: auto;
            }}

            .reveal .slides {{
                height: auto;
                perspective: none;
            }}

            .reveal section {{
                position: relative;
                opacity: 1;
                visibility: visible;
                transform: none;
                page-break-after: always;
                margin-bottom: 20px;
                display: block;
            }}

            .slide-container {{
                margin: 0;
                page-break-inside: avoid;
            }}

            .reveal .controls,
            .reveal .progress,
            .reveal .slide-number {{
                display: none;
            }}

            .slide-element {{
                animation: none !important;
                opacity: 1 !important;
            }}
        }}
    </style>"""

    def _get_script(self, slide_width: int, slide_height: int) -> str:
        """
        Get JavaScript for slideshow navigation.

        Args:
            slide_width: Width of slides in pixels
            slide_height: Height of slides in pixels

        Returns:
            JavaScript code block
        """
        return """    <script>
        (function() {
            'use strict';

            let currentSlide = 0;
            const slides = document.querySelectorAll('.reveal section');
            const totalSlides = slides.length;

            // Create UI elements
            function createControls() {
                const controls = document.createElement('div');
                controls.className = 'controls';

                const prevBtn = document.createElement('button');
                prevBtn.innerHTML = '&#10094;'; // Left arrow
                prevBtn.title = 'Previous slide';
                prevBtn.addEventListener('click', prevSlide);

                const nextBtn = document.createElement('button');
                nextBtn.innerHTML = '&#10095;'; // Right arrow
                nextBtn.title = 'Next slide';
                nextBtn.addEventListener('click', nextSlide);

                controls.appendChild(prevBtn);
                controls.appendChild(nextBtn);
                document.querySelector('.reveal').appendChild(controls);

                return { prevBtn, nextBtn };
            }

            function createProgress() {
                const progress = document.createElement('div');
                progress.className = 'progress';

                const progressBar = document.createElement('div');
                progressBar.className = 'progress-bar';
                progressBar.style.width = '0%';

                progress.appendChild(progressBar);
                document.querySelector('.reveal').appendChild(progress);

                return progressBar;
            }

            function createSlideNumber() {
                const slideNum = document.createElement('div');
                slideNum.className = 'slide-number';
                slideNum.textContent = '1 / ' + totalSlides;
                document.querySelector('.reveal').appendChild(slideNum);

                return slideNum;
            }

            function showSlide(index, direction) {
                // Wrap around
                if (index >= totalSlides) index = 0;
                if (index < 0) index = totalSlides - 1;

                const directionClass = direction > 0 ? 'future' : 'past';

                // Update slide classes
                slides.forEach((slide, i) => {
                    slide.classList.remove('past', 'present', 'future');

                    if (i < index) {
                        slide.classList.add('past');
                    } else if (i === index) {
                        slide.classList.add('present');
                    } else {
                        slide.classList.add('future');
                    }
                });

                currentSlide = index;

                // Update UI
                updateUI();

                // Reset and trigger animations
                if (window.SlideAnimations) {
                    window.SlideAnimations.reset(currentSlide);
                }
            }

            function nextSlide() {
                showSlide(currentSlide + 1, 1);
            }

            function prevSlide() {
                showSlide(currentSlide - 1, -1);
            }

            function updateUI() {
                // Update progress bar
                const progressBar = document.querySelector('.progress-bar');
                if (progressBar) {
                    const progress = ((currentSlide + 1) / totalSlides) * 100;
                    progressBar.style.width = progress + '%';
                }

                // Update slide number
                const slideNum = document.querySelector('.slide-number');
                if (slideNum) {
                    slideNum.textContent = (currentSlide + 1) + ' / ' + totalSlides;
                }

                // Update button states
                const prevBtn = document.querySelector('.controls button:first-child');
                const nextBtn = document.querySelector('.controls button:last-child');
                if (prevBtn) prevBtn.disabled = currentSlide === 0;
                if (nextBtn) nextBtn.disabled = currentSlide === totalSlides - 1;
            }

            function handleKeyPress(e) {
                switch(e.key) {
                    case 'ArrowRight':
                    case ' ': // Space
                    case 'Enter':
                    case 'PageDown':
                        e.preventDefault();
                        nextSlide();
                        break;
                    case 'ArrowLeft':
                    case 'PageUp':
                        e.preventDefault();
                        prevSlide();
                        break;
                    case 'Home':
                        e.preventDefault();
                        showSlide(0, 1);
                        break;
                    case 'End':
                        e.preventDefault();
                        showSlide(totalSlides - 1, -1);
                        break;
                    case 'f':
                    case 'F':
                        // Toggle fullscreen
                        if (document.documentElement.requestFullscreen) {
                            if (!document.fullscreenElement) {
                                document.documentElement.requestFullscreen();
                            } else {
                                document.exitFullscreen();
                            }
                        }
                        break;
                }
            }

            // Touch support
            let touchStartX = 0;
            let touchEndX = 0;

            function handleTouchStart(e) {
                touchStartX = e.changedTouches[0].screenX;
            }

            function handleTouchEnd(e) {
                touchEndX = e.changedTouches[0].screenX;
                handleSwipe();
            }

            function handleSwipe() {
                const swipeThreshold = 50;
                const diff = touchStartX - touchEndX;

                if (Math.abs(diff) > swipeThreshold) {
                    if (diff > 0) {
                        nextSlide();
                    } else {
                        prevSlide();
                    }
                }
            }

            // Initialize
            function init() {
                if (slides.length === 0) return;

                // Create UI elements
                const { prevBtn, nextBtn } = createControls();
                createProgress();
                createSlideNumber();

                // Show first slide
                showSlide(0, 1);

                // Event listeners
                document.addEventListener('keydown', handleKeyPress);
                document.addEventListener('touchstart', handleTouchStart, false);
                document.addEventListener('touchend', handleTouchEnd, false);

                // Mouse wheel support
                let wheelTimeout;
                document.addEventListener('wheel', function(e) {
                    clearTimeout(wheelTimeout);
                    wheelTimeout = setTimeout(function() {
                        if (e.deltaY > 0) {
                            nextSlide();
                        } else if (e.deltaY < 0) {
                            prevSlide();
                        }
                    }, 50);
                }, { passive: true });
            }

            // Start when DOM is ready
            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', init);
            } else {
                init();
            }
        })();
    </script>"""
