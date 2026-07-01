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

    def __init__(self, output_dir: str = None, measurer=None):
        """Initialize the flow layout with converters."""
        super().__init__(use_base64=False, output_dir=output_dir, measurer=measurer)

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
            self._get_flow_script(),
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
        """Convert a single slide to a section with absolute positioning.

        The <section> is wrapped in a <div class='slide-frame'>. The frame
        occupies the post-scale box (set by JS) so that transform: scale()
        on the inner section doesn't leave a 1889px-tall empty box behind.
        """
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
            f"<div class='slide-frame' data-pptx-slide-number='{slide.slide_number}'>",
            f"<section class='slide'",
            f"    data-pptx-slide-number='{slide.slide_number}'",
            f"    data-pptx-layout='{slide.layout_name}'",
            f"    style='{section_style_str}'>",
        ]

        # Render elements using inherited method
        elements_html = self._render_slide_elements(slide)
        if elements_html:
            html_parts.append(f"    {elements_html}")

        html_parts.extend([
            "</section>",
            "</div>",
        ])
        return "\n".join(html_parts)

    def _get_flow_styles(self) -> str:
        """Get CSS styles for flow layout.

        Why the .slide-frame / .slide split:
        transform: scale() doesn't shrink the element's layout box, so a
        1512x1889 poster scaled down to 0.3 would still reserve 1889px of
        vertical space, leaving a huge empty band beneath each slide. The
        .slide-frame wrapper occupies the post-scale box (width/height set
        dynamically by JS), and the inner .slide is absolutely positioned
        so its scaled visual fits exactly inside the frame.
        """
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

        .slide-frame {
            position: relative;
            margin: 20px auto;
            background-color: white;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3);
        }

        .slide {
            position: absolute;
            top: 0;
            left: 0;
            overflow: hidden;
            transform-origin: top left;
            transform: scale(var(--scale, 1));
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

        /* Print styles */
        @media print {
            body {
                background-color: white;
                padding: 0;
            }

            .presentation {
                max-width: 100%;
            }

            .slide-frame {
                margin: 0;
                page-break-after: always;
                box-shadow: none;
            }

            .slide {
                transform: none !important;
            }
        }
    </style>"""

    def _get_flow_script(self) -> str:
        """JS that rescales each .slide to fit the viewport.

        Previously this was done with three hard-coded @media breakpoints
        (1200/800/600px). Those breakpoints ignored the slide's actual
        pixel size, so a 1512x1889 poster never scaled down even on a
        1280px window — exactly the "can enlarge, can't shrink" symptom.

        Now each slide's scale is computed from its real width and the
        current viewport width. The matching .slide-frame gets the scaled
        width/height so the document flow reclaims the freed space.
        """
        return """    <script>
        (function () {
            // Horizontal padding reserved around the slide: body padding
            // (20px each side) + a small margin so the slide isn't flush
            // against the window edge.
            var SIDE_GUTTER = 60;

            function fitSlide(slide) {
                var frame = slide.parentElement;
                if (!frame || !frame.classList.contains('slide-frame')) return;

                // offsetWidth/Height ignore transform, so these are the
                // slide's intrinsic pixel dimensions regardless of scale.
                var sw = slide.offsetWidth;
                var sh = slide.offsetHeight;
                if (!sw) return;

                var available = window.innerWidth - SIDE_GUTTER;
                var scale = Math.min(available / sw, 1);
                slide.style.setProperty('--scale', scale);
                frame.style.width = (sw * scale) + 'px';
                frame.style.height = (sh * scale) + 'px';
            }

            function fitAll() {
                document.querySelectorAll('.slide').forEach(fitSlide);
            }

            if (document.readyState === 'loading') {
                document.addEventListener('DOMContentLoaded', fitAll);
            } else {
                fitAll();
            }
            window.addEventListener('resize', fitAll);
            window.addEventListener('load', fitAll);
        })();
    </script>"""
