"""
Flow Layout - converts slides to HTML in a simple scrollable page.

Shows all slides stacked vertically with absolute-positioned elements.
No sidebar, no play mode — just a clean scrollable document.
"""

from typing import List, Optional
import base64

from shuttleslide.pptx_to_html.models import ParsedSlide
from shuttleslide.pptx_to_html.layouts.pptview import PPTLayout


class FlowLayout(PPTLayout):
    """
    Generates HTML with absolute positioning in a scrollable document layout.

    Inherits all element rendering logic from AbsoluteLayout.
    Overrides convert() to produce a simple scrollable page instead of the
    PPT-style editor interface.
    """

    def __init__(self, output_dir: str = None):
        """Initialize the flow layout with converters."""
        super().__init__(use_base64=False, output_dir=output_dir)

    def convert(self, slides: List[ParsedSlide]) -> str:
        """
        Convert slides to a simple scrollable HTML page.

        Args:
            slides: List of parsed slides

        Returns:
            Complete HTML document string
        """
        html_parts = [
            "<!DOCTYPE html>",
            "<html>",
            "<head>",
            "    <meta charset='UTF-8'>",
            "    <meta name='viewport' content='width=device-width, initial-scale=1.0'>",
            "    <title>Presentation</title>",
            self._get_flow_styles(),
            "</head>",
            "<body>",
            "    <div class='presentation'>",
        ]

        for slide in slides:
            slide_html = self._convert_flow_slide(slide)
            html_parts.append(f"        {slide_html}")

        html_parts.extend([
            "    </div>",
            "</body>",
            "</html>",
        ])

        return "\n".join(html_parts)

    def _convert_flow_slide(self, slide: ParsedSlide) -> str:
        """Convert a single slide to a section with absolute positioning."""
        section_styles = [
            f"width: {slide.width}px",
            f"height: {slide.height}px",
            f"aspect-ratio: {slide.width}/{slide.height}",
        ]

        bg_style = self._get_background_style(slide)
        if bg_style:
            section_styles.append(bg_style)

        section_style_str = "; ".join(section_styles)

        html_parts = [
            f"<section class='slide'",
            f"    data-pptx-slide-number='{slide.slide_number}'",
            f"    data-pptx-layout='{slide.layout_name}'",
            f"    style='{section_style_str}'>",
        ]

        # Render elements using inherited method
        elements_html = self._render_slide_elements(slide)
        if elements_html:
            html_parts.append(f"    {elements_html}")

        html_parts.append("</section>")
        return "\n".join(html_parts)

    def _get_flow_styles(self) -> str:
        """Get CSS styles for flow layout."""
        return """    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            line-height: 1;
        }

        body {
            font-family: 'Arial', sans-serif;
            background-color: #f0f0f0;
            padding: 20px;
            display: flex;
            justify-content: center;
            align-items: flex-start;
            min-height: 100vh;
        }

        .presentation {
            width: 100%;
            max-width: 100%;
        }

        .slide {
            position: relative;
            background-color: white;
            margin: 20px auto;
            overflow: hidden;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
        }

        /* Slide elements */
        .slide-element {
            position: absolute;
        }

        /* Typography base styles */
        h1, h2, h3, h4, h5, h6, p {
            margin: 0;
            padding: 0;
        }

        /* Multi-level bullet paragraphs */
        .bullet-paragraph {
            display: flex;
            align-items: baseline;
        }

        .bullet-paragraph .bullet {
            flex-shrink: 0;
            line-height: 1;
        }

        .bullet-paragraph .bullet img {
            display: inline-block;
            vertical-align: middle;
        }

        .bullet-paragraph .text {
            flex-grow: 1;
        }

        .text-paragraph {
            margin: 0;
            padding: 0;
        }

        /* Responsive scaling */
        @media (max-width: 1200px) {
            .slide {
                transform: scale(0.8);
                transform-origin: top center;
            }
        }

        @media (max-width: 800px) {
            .slide {
                transform: scale(0.6);
                transform-origin: top center;
            }
        }

        @media (max-width: 600px) {
            .slide {
                transform: scale(0.4);
                transform-origin: top center;
            }
        }

        /* Print styles */
        @media print {
            body {
                background-color: white;
                padding: 0;
            }

            .presentation {
                max-width: 100%;
            }

            .slide {
                margin: 0;
                page-break-after: always;
                box-shadow: none;
                transform: none !important;
            }
        }
    </style>"""
