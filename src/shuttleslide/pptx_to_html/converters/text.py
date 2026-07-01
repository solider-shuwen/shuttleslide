"""
Text Converter - converts text elements to HTML.
"""

import base64
import math
import os
from html import escape
from typing import Optional

from shuttleslide.pptx_to_html.models import TextElement


# ---------------------------------------------------------------------------
# Shrink-on-overflow constants
# ---------------------------------------------------------------------------
# When wrapped or single-line content would exceed the PPT-declared shape
# height (computed analytically from Playwright-measured text widths), the
# converter shrinks font_size and line_height proportionally so the content
# fits — or hits the MIN_SCALE floor.  Mirrors PPT's own <a:normAutofit
# fontScale="..."/> behaviour, which the parser exposes via
# element.metadata['normAutofit_fontScale'] (100000 = 100%).
MIN_SCALE = 0.5
PT_TO_PX = 96.0 / 72.0


# Line height adjustment factor for PPT to CSS conversion
#
# Technical Background:
# =====================
#
# PPT (OpenXML) Line Spacing Model:
# - Uses baseline-to-baseline distance measurement
# - spcPct val="90000" means 90% of font size as line spacing
# - Single spacing typically includes 1.2x leading factor (traditional typesetting)
# - Formula: baseline_distance = font_size × spacing_value × 1.2
#
# CSS line-height Model:
# - Uses line-box height measurement
# - line-height includes character height + spacing
# - Measured from top to bottom of line box
# - Formula: line_height = font_size × multiplier
#
# Model Differences:
# =================
# 1. Measurement basis: PPT uses baseline distance, CSS uses line box height
# 2. Leading inclusion: PPT adds leading separately, CSS includes it in line-height
# 3. Font rendering: GDI+ (PPT) vs browser engines have different metrics
#
# Theoretical Conversion:
# ======================
# If PPT spacing = 0.9 and font_size = 12pt:
# - PPT baseline distance = 12 × 0.9 × 1.2 = 12.96pt
# - CSS line-height should match this visual appearance
# - Theoretical adjustment ≈ 1/1.2 = 0.833
#
# Empirical Results:
# =================
# Browser testing (2024-06-08) yielded: 1.823
# However, this appears affected by measurement methodology.
# Current value (0.92) was empirically determined through visual comparison
# and provides good results for typical use cases.
#
# This 0.92 value accounts for:
# 1. Font rendering differences (GDI+ vs browser engines)
# 2. Baseline position calculation variations
# 3. Line box computation methods
# 4. Potential differences in font metrics
#
# Adjustment Guide:
# =================
# - Values < 0.92: Make lines tighter (use if HTML lines too loose)
# - Values > 0.92: Make lines looser (use if HTML lines too tight)
# - Typical range: 0.85-0.98
#
# CLI Override:
# ==============
# Users can override this via --line-height-factor parameter:
# slidecraft to-html input.pptx --line-height-factor 0.95
LINE_HEIGHT_ADJUSTMENT = 1  # Expand line spacing beyond PPT value

# Paragraph spacing adjustment factors
# ====================================
#
# Technical Background:
# ====================
#
# PPT Paragraph Spacing Model (OpenXML):
# - Units: spcPts (1/100 point), spcPct (percentage/10000)
# - Example: val="1000" = 10pt, val="500" = 5pt
# - Measurement: Space between paragraphs (baseline to baseline offset)
# - 0pt spacing: Uses default tight spacing
#
# CSS margin Model:
# - Units: pt, px, em, etc.
# - Measurement: Space outside paragraph box
# - margin-top: Space before paragraph
# - margin-bottom: Space after paragraph
#
# Model Differences:
# ==================
# 1. Rendering engines: PPT uses GDI+, browsers use CSS engines
# 2. Baseline calculation: Different algorithms for text positioning
# 3. Default spacing: PPT's 0pt is tighter than CSS margin: 0
#
# Empirical Testing Results (2024-06-08):
# ======================================
# Browser-based testing yielded:
# - PPT 5pt → HTML 19.47px (ratio: 2.92x)
# - PPT 10pt → HTML 27.20px (ratio: 2.04x)
# - PPT 15pt → HTML 34.92px (ratio: 1.75x)
# - Global average ratio: 1.858x
# - Recommended ratio: 0.538 (1 / 1.858)
#
# This means HTML renders paragraph spacing ~186% of PPT values.
# To match PPT appearance, multiply PPT spacing by 0.538.
#
# Adjustment Guide:
# =================
# - For PPT values > 0: Apply PARAGRAPH_SPACING_RATIO (0.538)
# - For PPT 0pt: Apply PARAGRAPH_SPACING_ADJUSTMENT (-0.2em)
#   (PPT's 0pt appears as ~9.6px in HTML, needs negative margin)

# Ratio for non-zero spacing values (from empirical testing)
PARAGRAPH_SPACING_RATIO = 0.538  # Multiply PPT spacing by this for CSS

# Adjustment for zero spacing (special case)
# PPT's 0pt spacing is the default — paragraphs have normal line-height gap between
# them, not compressed. Negative margins were previously used to compress spacing,
# but this caused overlap when adjacent paragraphs have very different font sizes
# (e.g., 52pt title → 17pt subtitle). Use 0 to match PPT's actual 0pt behavior.
PARAGRAPH_SPACING_ADJUSTMENT = 0  # 0pt: no extra compression for PPT's default 0pt spacing


class TextConverter:
    """
    Converts text elements from PPTX to HTML.
    """

    def __init__(self, use_base64: bool = False, output_dir: Optional[str] = None,
                 measurer=None):
        """
        Initialize the text converter.

        Args:
            use_base64: Whether to embed images as base64.
            output_dir: Directory for saving image assets. If None, uses default.
            measurer: Optional PlaywrightTextMeasurer (already started).
                When provided, enables shrink-on-overflow: text that would
                exceed the PPT-declared shape height is font-scaled to fit
                (or hits MIN_SCALE=0.5 floor).  When None, no shrink fires
                and behaviour is identical to v1.
        """
        self.use_base64 = use_base64
        self.output_dir = output_dir
        self._measurer = measurer
        self._bullet_counter = 0
        self._created_dirs = set()
        self._current_scale = 1.0  # per-convert shrink scale; reset after each call

    def convert(self, element: TextElement) -> str:
        """
        Convert a text element to HTML with multi-level bullet support.

        Args:
            element: TextElement to convert

        Returns:
            HTML string representation
        """
        # Compute shrink scale once per element; threaded through every
        # font-size / line-height emission point below.  _compute_shrink_scale
        # always returns a value (1.0 or computed scale), so each convert()
        # call starts from a fresh scale rather than carrying state forward.
        self._current_scale = self._compute_shrink_scale(element)

        # Check if we have paragraph structure (new format)
        if element.paragraphs:
            return self._convert_paragraphs(element)

        # Fall back to legacy single-text format
        text = element.text.strip()

        if not text:
            return ""

        # Determine HTML tag based on element properties
        if element.is_title:
            tag = "h1"
        elif element.level > 0:
            # Map level to heading tag (1 -> h2, 2 -> h3, etc.)
            tag = f"h{min(element.level + 1, 6)}"
        else:
            # Check for list-like content
            if text.startswith(("- ", "* ", "• ", "· ")):
                return self._convert_list_item(text, element)
            elif text[0:1].isdigit() and text[1:2] in [". ", ") "]:
                return self._convert_list_item(text, element, ordered=True)
            else:
                tag = "p"

        # Build HTML with styling
        styles = self._build_text_styles(element)

        # Build attributes
        attrs = []
        if styles:
            attrs.append(f'style="{styles}"')

        # Add data attributes for round-trip
        attrs.extend(self._build_data_attributes(element))

        # Build HTML
        attr_str = " ".join(attrs)
        escaped_text = escape(text)

        return f"<{tag} {attr_str}>{escaped_text}</{tag}>"

    # ------------------------------------------------------------------
    # Shrink-on-overflow
    # ------------------------------------------------------------------

    def _compute_shrink_scale(self, element: TextElement) -> float:
        """Compute the font-size scale to apply for this element.

        Mirrors PPT's <a:normAutofit fontScale="..."/> behaviour: if the
        wrapped content would exceed the PPT-declared shape height, shrink
        font_size + line_height proportionally so it fits (or hits the
        MIN_SCALE floor).  Returns 1.0 when no shrink is needed.

        Skipped (returns 1.0) when:
          - No paragraphs (legacy single-text path; height rarely an issue).
          - No measurer (caller opted out — graceful v1 behaviour).

        Note on spAutoFit: we DO shrink these.  PPT's "shape grows with
        text" semantics only works in PPT's editor — the saved XML has
        fixed shape coordinates, so in absolute-positioned HTML layouts
        an overflowing spAutoFit shape visually overlaps its neighbours.
        Shrink prevents that overlap.
        """
        if not element.paragraphs or not self._measurer:
            return 1.0

        meta = getattr(element, 'metadata', None) or {}
        scale = self._initial_scale_from_metadata(meta)

        # element.width/height are already in CSS px (parser converted
        # EMU -> px at parse time; layouts emit them directly as
        # `width: {el.width}px` etc.).  So no PT_TO_PX conversion here.
        shape_h_px = element.height
        shape_w_px = element.width

        h = self._estimate_total_height_px(element, scale, shape_w_px)
        if h <= shape_h_px + 0.5:
            return scale

        # Binary search for the largest scale in [MIN_SCALE, scale] at
        # which content still fits.  We cannot use a one-shot analytic
        # divide (scale = scale * shape_h / h) because wrap line count
        # is NOT constant in scale: when text barely overflows at the
        # starting scale and wraps to N+1 lines, a slightly smaller
        # font often fits on N lines — dropping total height by far
        # more than the linear projection predicts.  The analytic thus
        # overshoots (e.g. a 28pt title in a 44px shape gets clamped
        # to MIN_SCALE=0.5 → 14pt, when in fact 27pt fits on one line).
        #
        # 12 iterations gives ~0.0005 precision across [0.5, 1.0] —
        # more than enough; converges to "just barely fits".
        lo, hi = MIN_SCALE, scale
        # Invariant: at `hi' content overflows; at `lo' it fits (or we
        # hit MIN_SCALE floor).  When even MIN_SCALE overflows we
        # return MIN_SCALE and accept the residual.
        h_lo = self._estimate_total_height_px(element, lo, shape_w_px)
        if h_lo > shape_h_px + 0.5:
            return lo  # accept overflow at MIN_SCALE
        for _ in range(12):
            mid = (lo + hi) / 2
            h_mid = self._estimate_total_height_px(element, mid, shape_w_px)
            if h_mid <= shape_h_px + 0.5:
                lo = mid
            else:
                hi = mid
        return lo

    @staticmethod
    def _initial_scale_from_metadata(meta: dict) -> float:
        """Read PPT normAutofit fontScale (100000 = 100%) as the starting
        scale.  PPT already shrunk via this value at save time; we honor
        it as the floor and may shrink further when our renderer's font
        metrics (Playwright) diverge from PPT's (GDI+)."""
        raw = meta.get('normAutofit_fontScale')
        if raw is None:
            return 1.0
        try:
            s = int(raw) / 100000.0
        except (TypeError, ValueError):
            return 1.0
        return max(0.1, min(1.0, s))

    def _estimate_total_height_px(self, element: TextElement,
                                   scale: float, shape_w_px: float) -> float:
        """Analytically estimate the rendered total height (in CSS px) of
        the element's paragraphs at the given scale.

        For each paragraph:
          lines = wrap_line_count (via measurer) or 1 for empty paragraphs
          height += lines * effective_line_height_px + paragraph_spacing_px

        effective_line_height respects the CSS line-box floor
        (line_height >= font_size) so the estimate tracks what the browser
        will actually render.
        """
        total = 0.0
        for para in element.paragraphs:
            fs_pt = (para.font_size or element.font_size or 12.0) * scale

            # Resolve effective line-height (pt) honoring CSS line-box floor.
            if para.line_spacing_pts is not None:
                lh_pt = max(para.line_spacing_pts * scale, fs_pt)
            elif para.line_spacing is not None:
                lh_pt = fs_pt * para.line_spacing * LINE_HEIGHT_ADJUSTMENT
            else:
                # CSS default line-height ~1.2 (matches most browsers).
                lh_pt = fs_pt * 1.2

            # Compute the available text width for this paragraph.  For
            # bullet paragraphs the renderer reserves a bullet column
            # (= PPT marL) on the left via a flex `<span class="bullet">`
            # with `width: <em>em`.  That column is NOT available for
            # text wrap; if we count it, the estimator thinks the text
            # has more room than it really does, under-counts wrap
            # lines, and shrink fails to fire — leaving the actual
            # render to wrap and overflow the shape (poster.html
            # "Size Risk: 0.326 (highest)" regression).
            text_w_px = shape_w_px
            if getattr(para, 'has_bullet', False):
                if para.margin_left is not None:
                    bullet_col_em = para.margin_left / fs_pt
                else:
                    bullet_col_em = (para.level + 1) * 1.5
                text_w_px = max(0.0, shape_w_px - bullet_col_em * fs_pt * PT_TO_PX)

            # Resolve paragraph text (runs-only paragraphs have text=None).
            para_text = para.text
            if para_text is None and para.runs:
                para_text = "".join(r.text or "" for r in para.runs)
            if not para_text or not para_text.strip():
                lines = 1  # empty paragraph still occupies a line (spacer)
            else:
                lines = self._estimate_wrap_lines_text(
                    para_text, para, fs_pt, text_w_px
                )

            total += lines * lh_pt * PT_TO_PX

            # Paragraph spacing before/after (already adjusted by
            # PARAGRAPH_SPACING_RATIO at emission; mirror it here).
            if para.spacing_before and para.spacing_before > 0:
                total += para.spacing_before * PARAGRAPH_SPACING_RATIO * PT_TO_PX
            if para.spacing_after and para.spacing_after > 0:
                total += para.spacing_after * PARAGRAPH_SPACING_RATIO * PT_TO_PX

        return total

    def _estimate_wrap_lines_text(self, text: str, para,
                                   font_size_pt: float,
                                   shape_w_px: float) -> int:
        """Estimate how many visual lines this paragraph will wrap to.

        Uses greedy word-wrap: tokenizes the text into wrap-units (words
        for Latin, characters for CJK), measures each unit's width in
        headless Chromium, then packs units into lines until the next
        unit would overflow shape_w.  This matches what a browser's own
        wrap engine produces far more closely than the prior "total
        width / shape width" estimate, which underestimated line count
        because it ignored word-boundary slack.

        Multi-run paragraphs are measured with the paragraph's default
        font_size — approximate but adequate for shrink estimation.
        """
        if not text:
            return 1
        if shape_w_px <= 0:
            return 1

        # Newlines force line breaks; each segment wraps independently.
        total_lines = 0
        for seg_idx, segment in enumerate(text.split('\n')):
            if not segment:
                total_lines += 1
                continue

            units = self._tokenize_for_wrap(segment)
            if not units:
                total_lines += 1
                continue

            try:
                widths = self._measurer.measure_batch([{
                    "text": u,
                    "font_family": para.font_name,
                    "font_size_pt": font_size_pt,
                    "font_weight": "bold" if para.bold else None,
                    "font_style": "italic" if para.italic else None,
                } for u in units])
            except Exception:
                # Defensive: measurer should not crash the converter.
                # Conservative fallback: assume 1 line per segment.
                # Underestimates height -> shrink won't fire for this case,
                # but caller still gets working HTML.
                total_lines += 1
                continue

            # Greedy pack.  Inter-word space: approximate as 0.27em,
            # close to typical Latin space width.  CJK runs have no
            # inter-char space (each char is its own unit).
            space_w_px = font_size_pt * PT_TO_PX * 0.27
            lines = 1
            cur_w = 0.0
            for unit, w in zip(units, widths):
                is_cjk = self._is_cjk_char(unit[0]) if unit else False
                gap = 0.0 if is_cjk or cur_w == 0 else space_w_px
                if cur_w + gap + w > shape_w_px and cur_w > 0:
                    lines += 1
                    cur_w = w
                else:
                    cur_w += gap + w
            total_lines += lines

        return max(1, total_lines)

    @staticmethod
    def _tokenize_for_wrap(text: str) -> list[str]:
        """Split text into wrap-units.

        - CJK characters are individual units (CJK wraps per-character).
        - Latin/other runs split into words on whitespace.
        - Whitespace itself is dropped (gap is added at pack-time).
        """
        units = []
        # Walk the string char by char, grouping Latin into words and
        # emitting each CJK char as its own unit.
        buf = []
        for ch in text:
            if TextConverter._is_cjk_char(ch):
                if buf:
                    units.append(''.join(buf))
                    buf = []
                units.append(ch)
            elif ch.isspace():
                if buf:
                    units.append(''.join(buf))
                    buf = []
            else:
                buf.append(ch)
        if buf:
            units.append(''.join(buf))
        return units

    @staticmethod
    def _is_cjk_char(ch: str) -> bool:
        """True for Han / Hiragana / Katakana / Hangul / CJK punctuation."""
        if not ch:
            return False
        cp = ord(ch)
        return (
            0x3000 <= cp <= 0x9FFF        # CJK punctuation + Han ideographs
            or 0xAC00 <= cp <= 0xD7AF     # Hangul
            or 0xF900 <= cp <= 0xFAFF     # CJK compatibility ideographs
            or 0xFF00 <= cp <= 0xFFEF     # Half/Fullwidth
            or 0x3040 <= cp <= 0x30FF     # Hiragana + Katakana
        )

    def _convert_paragraphs(self, element: TextElement) -> str:
        """
        Convert paragraphs to HTML with multi-level bullet support.

        Args:
            element: TextElement with paragraphs

        Returns:
            HTML string with paragraph structure
        """
        html_parts = []

        # Track paragraph index for first-paragraph special handling
        para_index = 0

        # Track auto-number counters per level for this text element
        autonum_counters: dict[int, int] = {}

        for para in element.paragraphs:
            # Resolve effective text: para.text may be None for run-only
            # paragraphs (some PPT parsers omit the concatenated text).
            para_text = para.text
            if para_text is None:
                para_text = "".join(r.text or "" for r in para.runs) if para.runs else ""
            if not para_text.strip():
                # Render empty paragraphs as spacers — they take up vertical space
                # in PPT (especially when font_size is large), affecting bottom-anchored text position
                spacer_font_size = para.font_size if para.font_size else None
                spacer_style = "margin: 0; padding: 0; line-height: 1.0"
                if spacer_font_size:
                    spacer_style += (
                        f"; font-size: {spacer_font_size * self._current_scale}pt"
                    )
                html_parts.append(f'<p style="{spacer_style}">&nbsp;</p>')
                para_index += 1
                continue

            # Check if this should be a bullet point based on parsed bullet properties
            is_bullet = para.has_bullet

            if is_bullet:
                # Multi-level bullet
                html_parts.append(self._convert_bullet_paragraph(para, element, para_index, autonum_counters))
            else:
                # Regular paragraph
                html_parts.append(self._convert_regular_paragraph(para, element, para_index))

            para_index += 1

        return "\n".join(html_parts)

    def _convert_bullet_paragraph(self, para, element: TextElement, para_index: int = 0,
                                    autonum_counters = None) -> str:
        """
        Convert a bullet paragraph to HTML.

        Args:
            para: ParagraphElement
            element: Parent TextElement
            para_index: Index of this paragraph (for first-paragraph handling)
            autonum_counters: Dict tracking auto-number counters per level

        Returns:
            HTML string for bullet paragraph
        """
        # Determine bullet marker from parsed properties
        bullet_marker = self._get_bullet_marker(para, autonum_counters)

        # Build paragraph styles
        styles = self._build_paragraph_styles(para, element, para_index)

        # Compute the bullet column width from PPT marL (in em, relative to font size).
        # The bullet span gets this as its flex width, so wrapped text aligns at marL.
        # NOTE: Do NOT emit padding-left/text-indent on the container — Chromium applies
        # text-indent to the first flex line and breaks alignment of wrapped lines.
        font_size_pt = para.font_size or (element.font_size if element else None) or 12.0
        if para.margin_left is not None:
            bullet_col_em = para.margin_left / font_size_pt
        else:
            bullet_col_em = (para.level + 1) * 1.5

        # Build attributes
        attrs = []
        if styles:
            attrs.append(f'style="{"; ".join(styles)}"')
        attrs.append('class="bullet-paragraph"')
        attrs.append(f'data-pptx-level="{para.level}"')

        # Build HTML
        attr_str = " ".join(attrs)
        content = self._render_paragraph_content(para)

        # Build bullet span with optional styling
        bullet_html = self._build_bullet_span(para, bullet_marker, bullet_col_em)

        return f'<div {attr_str}>{bullet_html}<span class="text">{content}</span></div>'

    def _convert_regular_paragraph(self, para, element: TextElement, para_index: int = 0) -> str:
        """
        Convert a regular paragraph to HTML.

        Args:
            para: ParagraphElement
            element: Parent TextElement
            para_index: Index of this paragraph (for first-paragraph handling)

        Returns:
            HTML string for regular paragraph
        """
        # Build paragraph styles
        styles = self._build_paragraph_styles(para, element, para_index)

        # Build attributes
        attrs = []
        if styles:
            attrs.append(f'style="{"; ".join(styles)}"')
        attrs.append('class="text-paragraph"')
        attrs.extend(self._build_data_attributes(element))

        # Build HTML
        attr_str = " ".join(attrs)
        content = self._render_paragraph_content(para)

        return f'<p {attr_str}>{content}</p>'

    def _build_paragraph_styles(self, para, element: Optional[TextElement],
                                para_index: int = 0) -> list[str]:
        """
        Build CSS styles for a paragraph.

        Args:
            para: ParagraphElement with styling info
            element: Parent TextElement (used to resolve inherited font_size
                when the paragraph has none — PPT free TextBoxes with no
                <a:rPr sz> on any run and no placeholder lstStyle leave
                font_size as None at parse time)
            para_index: Index of this paragraph (for first-paragraph handling)

        Returns:
            List of CSS style declarations
        """
        styles = []

        if para.font_name:
            styles.append(f"font-family: '{para.font_name}'")

        if para.font_size:
            styles.append(
                f"font-size: {para.font_size * self._current_scale}pt"
            )

        if para.bold:
            styles.append("font-weight: bold")

        if para.italic:
            styles.append("font-style: italic")

        if para.color:
            styles.append(f"color: {para.color}")

        if para.alignment:
            styles.append(f"text-align: {para.alignment}")

        # Line spacing — use !important to ensure it overrides any CSS defaults
        # Apply adjustment factor to match PPT rendering
        if para.line_spacing is not None:
            adjusted_line_height = para.line_spacing * LINE_HEIGHT_ADJUSTMENT
            styles.append(f"line-height: {adjusted_line_height:.2f} !important")
        elif para.line_spacing_pts is not None:
            adjusted_line_height_pts = para.line_spacing_pts * LINE_HEIGHT_ADJUSTMENT
            # Floor at font-size: PPT's GDI+ tolerates spcPts < font_size
            # (baseline distance can be smaller than glyph height — adjacent
            # lines just visually overlap, which PPT renders deliberately).
            # CSS line boxes cannot shrink below font-size, so emitting the
            # raw value produces multi-line text with characters from
            # neighbouring lines stacked on top of each other.
            #
            # When the paragraph itself has no font_size, walk up to the
            # parent text element.  Free TextBoxes with no <a:rPr sz> on
            # any run and no placeholder lstStyle leave font_size as None
            # at parse time; without this fallback we'd emit the raw
            # sub-font-size spcPts and the browser would overlap adjacent
            # line glyphs (poster.html "Covariance Contribution" block:
            # 10pt line-spacing on an unsized paragraph stacked lines on
            # top of each other).
            #
            # Apply shrink scale: absolute pt line-height does NOT auto-scale
            # with font-size in CSS (unlike unitless line-height), so we must
            # multiply by _current_scale here.  The floor check uses the
            # scaled font_size to keep CSS line-box semantics consistent.
            effective_fs_pt = (para.font_size
                               or (element.font_size if element else None)
                               or 12.0)
            scaled_fs = effective_fs_pt * self._current_scale
            scaled_lh = adjusted_line_height_pts * self._current_scale
            if scaled_lh < scaled_fs:
                scaled_lh = scaled_fs
            styles.append(f"line-height: {scaled_lh:.2f}pt !important")
        else:
            styles.append("line-height: 1 !important")

        # Spacing before/after — use !important to override any CSS
        #
        # Apply empirical adjustment ratio based on browser testing (2024-06-08)
        # HTML renders paragraph spacing at ~186% of PPT values
        # Multiply by PARAGRAPH_SPACING_RATIO (0.538) to match PPT appearance
        #
        # Special handling for first paragraph: Don't apply negative margin-top
        # to prevent text from being cut off at the top of the text box
        if para.spacing_before is not None and para.spacing_before > 0:
            adjusted_spacing = para.spacing_before * PARAGRAPH_SPACING_RATIO
            styles.append(f"margin-top: {adjusted_spacing:.2f}pt !important")
        elif para_index == 0:
            # First paragraph: Use 0 instead of negative margin to prevent cutoff
            # This applies whether spacing_before is None or 0
            styles.append("margin-top: 0 !important")
        else:
            # Non-first paragraph with 0pt spacing: no extra compression needed
            styles.append(f"margin-top: {PARAGRAPH_SPACING_ADJUSTMENT}pt !important")

        if para.spacing_after is not None and para.spacing_after > 0:
            adjusted_spacing = para.spacing_after * PARAGRAPH_SPACING_RATIO
            styles.append(f"margin-bottom: {adjusted_spacing:.2f}pt !important")
        else:
            # PPT's 0pt: normal paragraph spacing, no compression
            styles.append(f"margin-bottom: {PARAGRAPH_SPACING_ADJUSTMENT}pt !important")

        return styles

    def _render_paragraph_content(self, para) -> str:
        """
        Render paragraph content, using <span> tags when runs have
        different formatting from the paragraph defaults.

        Args:
            para: ParagraphElement with optional runs

        Returns:
            HTML string for paragraph content
        """
        if not para.runs:
            return escape(para.text)

        # Check if any run differs from paragraph defaults
        needs_spans = False
        for run in para.runs:
            if run.color is not None and run.color != para.color:
                needs_spans = True
                break
            if run.bold is not None and run.bold != para.bold:
                needs_spans = True
                break
            if run.italic is not None and run.italic != para.italic:
                needs_spans = True
                break
            if run.font_name is not None and run.font_name != para.font_name:
                needs_spans = True
                break
            if run.font_size is not None and run.font_size != para.font_size:
                needs_spans = True
                break

        if not needs_spans:
            return escape(para.text)

        # Build spans for runs with different formatting
        parts = []
        for run in para.runs:
            run_styles = []

            # Whitespace-only runs (e.g. the "\n" PPT emits as a separate
            # <a:r> with sz="2400" between bullets in a multi-line
            # paragraph) must NOT carry a font-size override.  The
            # override has zero visual effect on whitespace but, per
            # CSS inline-box rules, any inline element with a larger
            # font-size grows the line-box vertically — inflating
            # every line that the run sits on and causing overflow
            # plus uneven line spacing.  Other style props (color,
            # bold, etc.) are similarly inert on whitespace, but
            # font-size is the only one with a layout side-effect.
            is_ws_only = not (run.text or "").strip()

            if run.color is not None and run.color != para.color:
                run_styles.append(f"color: {run.color}")
            if run.bold is not None and run.bold != para.bold:
                run_styles.append("font-weight: bold" if run.bold else "font-weight: normal")
            if run.italic is not None and run.italic != para.italic:
                run_styles.append("font-style: italic" if run.italic else "font-style: normal")
            if run.font_name is not None and run.font_name != para.font_name:
                run_styles.append(f"font-family: '{run.font_name}'")
            if (run.font_size is not None and run.font_size != para.font_size
                    and not is_ws_only):
                run_styles.append(
                    f"font-size: {run.font_size * self._current_scale}pt"
                )

            escaped = escape(run.text)
            if run_styles:
                style_str = "; ".join(run_styles)
                parts.append(f'<span style="{style_str}">{escaped}</span>')
            else:
                parts.append(escaped)

        return "".join(parts)

    def _convert_list_item(self, text: str, element: TextElement, ordered: bool = False) -> str:
        """
        Convert a list item to HTML.

        Args:
            text: Text content
            element: TextElement with styling info
            ordered: Whether this is an ordered list item

        Returns:
            HTML string for list item
        """
        # Remove bullet/number
        if ordered:
            # Remove "1. " or "1) " prefix
            clean_text = text.split(". ", 1)[-1].split(") ", 1)[-1]
            tag = "li"
        else:
            # Remove bullet prefix
            clean_text = text[2:] if text[:2] in ["- ", "* "] else text[3:]
            tag = "li"

        # Build HTML with styling
        styles = self._build_text_styles(element)

        # Build attributes
        attrs = []
        if styles:
            attrs.append(f'style="{styles}"')

        attrs.extend(self._build_data_attributes(element))

        attr_str = " ".join(attrs)
        escaped_text = escape(clean_text.strip())

        return f"<{tag} {attr_str}>{escaped_text}</{tag}>"

    def _build_text_styles(self, element: TextElement) -> str:
        """
        Build CSS style string from text element properties.

        Args:
            element: TextElement with styling info

        Returns:
            CSS style string
        """
        styles = []

        if element.font_name:
            styles.append(f"font-family: '{element.font_name}'")

        if element.font_size:
            styles.append(
                f"font-size: {element.font_size * self._current_scale}pt"
            )

        if element.bold:
            styles.append("font-weight: bold")

        if element.italic:
            styles.append("font-style: italic")

        if element.color:
            styles.append(f"color: {element.color}")

        # Apply rotation and transform styles
        transform_parts = []

        # Handle vertical text (writing-mode is a separate CSS property, not a transform)
        if element.vert:
            # East Asian vertical text
            if element.vert == 'eaVert':
                # For eaVert, use writing-mode: vertical-rl
                styles.append("writing-mode: vertical-rl")
            elif element.vert == 'mongolianVert':
                styles.append("writing-mode: vertical-rl")
            elif element.vert == 'wordVert':
                styles.append("writing-mode: vertical-lr")

        if element.rotation:
            transform_parts.append(f"rotate({element.rotation}deg)")

        # Combine all transforms
        if transform_parts:
            styles.append(f"transform: {' '.join(transform_parts)}")

        return "; ".join(styles)

    def _build_data_attributes(self, element: TextElement) -> list[str]:
        """
        Build data-pptx-* attributes for round-trip conversion.

        Args:
            element: TextElement with metadata

        Returns:
            List of data attribute strings
        """
        attrs = []

        # Store original position and size
        attrs.append(f'data-pptx-left="{element.left}"')
        attrs.append(f'data-pptx-top="{element.top}"')
        attrs.append(f'data-pptx-width="{element.width}"')
        attrs.append(f'data-pptx-height="{element.height}"')
        attrs.append(f'data-pptx-z-order="{element.z_order}"')

        # Store font information
        if element.font_name:
            attrs.append(f'data-pptx-font-name="{element.font_name}"')

        if element.font_size:
            attrs.append(f'data-pptx-font-size="{element.font_size}"')

        if element.bold:
            attrs.append('data-pptx-bold="true"')

        if element.italic:
            attrs.append('data-pptx-italic="true"')

        if element.color:
            attrs.append(f'data-pptx-color="{element.color}"')

        if element.is_title:
            attrs.append('data-pptx-is-title="true"')

        return attrs

    @staticmethod
    def detect_heading_level(text: str) -> int:
        """
        Detect heading level from text content.

        Args:
            text: Text to analyze

        Returns:
            Heading level (0 = body text, 1 = h1, 2 = h2, etc.)
        """
        # Simple heuristic: if text is short and on its own line, it might be a heading
        if len(text) < 50 and text == text.upper():
            return 1  # Likely h1
        elif len(text) < 80:
            return 2  # Likely h2
        else:
            return 0  # Body text

    @staticmethod
    def is_list_text(text: str) -> tuple[bool, bool]:
        """
        Check if text is a list item.

        Args:
            text: Text to check

        Returns:
            Tuple of (is_list, is_ordered)
        """
        # Check for unordered list bullets
        if text.startswith(("- ", "* ", "• ", "· ")):
            return True, False

        # Check for ordered list numbers
        if text[0:1].isdigit() and text[1:2] in [". ", ") "]:
            return True, True

        return False, False

    def _get_bullet_marker(self, para, autonum_counters = None) -> str:
        """
        Get the bullet marker string for a paragraph.

        Args:
            para: ParagraphElement with bullet properties
            autonum_counters: Dict tracking auto-number counters per level

        Returns:
            Bullet marker string (e.g., '•', '1.', 'a.')
        """
        if para.bullet is None:
            # Fallback to level-based symbols
            bullet_symbols = ["\u2022", "\u25e6", "\u25aa"]
            return bullet_symbols[min(para.level, 2)]

        if para.bullet.type == 'char':
            return para.bullet.char or '\u2022'

        if para.bullet.type == 'autonum':
            # `or {}` would discard an empty-but-valid dict (counter at
            # initial state), resetting the sequence to 1 on every call.
            # Use `is not None` so we mutate the caller's dict in place.
            return self._format_autonum(
                para, autonum_counters if autonum_counters is not None else {}
            )

        if para.bullet.type == 'blip':
            if para.bullet.blip_image_bytes:
                return ''  # Image bullets use <img>, not text markers
            return '\u2022'  # Fallback if image data missing

        # Fallback for 'inherited' that wasn't resolved, or 'none'
        bullet_symbols = ["\u2022", "\u25e6", "\u25aa"]
        return bullet_symbols[min(para.level, 2)]

    def _format_autonum(self, para, counters: dict) -> str:
        """
        Format an auto-numbered bullet marker.

        Args:
            para: ParagraphElement with autonum bullet properties
            counters: Dict tracking counters per level

        Returns:
            Formatted number string (e.g., '1.', 'a)', 'ii.')
        """
        level = para.level
        num_type = para.bullet.autonum_type or 'arabicPeriod'
        start_at = para.bullet.autonum_start or 1

        # Initialize counter for this level if needed
        if level not in counters:
            counters[level] = start_at

        num = counters[level]
        counters[level] += 1

        return self._number_to_format(num, num_type)

    @staticmethod
    def _number_to_format(num: int, fmt: str) -> str:
        """
        Convert a number to the specified format string.

        Args:
            num: Number to format
            fmt: OpenXML auto-number format type

        Returns:
            Formatted string (e.g., '1.', 'a)', 'ii.')
        """
        if fmt in ('arabicPeriod', 'arabicParenR', 'arabicPlain', 'arabicParenBoth'):
            if fmt == 'arabicPeriod':
                return f'{num}.'
            elif fmt == 'arabicParenR':
                return f'{num})'
            elif fmt == 'arabicParenBoth':
                return f'({num})'
            else:
                return f'{num}'

        if fmt in ('alphaLcPeriod', 'alphaLcParenR', 'alphaUcPeriod',
                    'alphaUcParenR', 'alphaLcParenBoth', 'alphaUcParenBoth'):
            letter = chr(ord('a') + (num - 1) % 26)
            if fmt.startswith('alphaUc'):
                letter = letter.upper()
            if 'Period' in fmt:
                return f'{letter}.'
            elif 'ParenBoth' in fmt:
                return f'({letter})'
            else:
                return f'{letter})'

        if fmt in ('romanLcPeriod', 'romanUcPeriod', 'romanLcParenBoth', 'romanUcParenBoth'):
            roman = TextConverter._to_roman(num)
            if fmt.startswith('romanUc'):
                roman = roman.upper()
            if 'ParenBoth' in fmt:
                return f'({roman})'
            else:
                return f'{roman}.'

        # Default fallback: arabic with period
        return f'{num}.'

    @staticmethod
    def _to_roman(num: int) -> str:
        """Convert an integer to lowercase Roman numeral string."""
        val = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
        syms = ['m', 'cm', 'd', 'cd', 'c', 'xc', 'l', 'xl', 'x', 'ix', 'v', 'iv', 'i']
        result = ''
        for i in range(len(val)):
            while num >= val[i]:
                result += syms[i]
                num -= val[i]
        return result

    def _build_bullet_span(self, para, bullet_marker: str, bullet_col_em: float = 1.5) -> str:
        """
        Build the bullet <span> HTML with optional styling.

        Args:
            para: ParagraphElement with bullet properties
            bullet_marker: The bullet marker string
            bullet_col_em: Width of the bullet column in em (= PPT marL).
                The bullet span gets this as its flex width so wrapped text
                lines align at marL.

        Returns:
            HTML string for the bullet span
        """
        # Reserve the bullet column width (= PPT marL). Wrapped text aligns at
        # the right edge of this column. flex-shrink: 0 prevents the column
        # from collapsing when the bullet glyph is narrower than marL.
        bullet_styles = [f"width: {bullet_col_em:.2f}em", "flex-shrink: 0"]
        if para.bullet:
            if para.bullet.color:
                bullet_styles.append(f"color: {para.bullet.color}")
            if para.bullet.font_size_pct:
                size_pct = para.bullet.font_size_pct / 100000.0
                bullet_styles.append(f"font-size: {size_pct:.2f}em")
            if para.bullet.font_typeface:
                bullet_styles.append(f"font-family: '{para.bullet.font_typeface}'")

        # Image bullet: render as inline <img> within the bullet span
        if para.bullet and para.bullet.type == 'blip' and para.bullet.blip_image_bytes:
            img_styles = ["vertical-align: middle"]
            if para.bullet.font_size_pct:
                img_height = para.bullet.font_size_pct / 100000.0
            else:
                # OpenXML spec default: 100% of text font size
                img_height = 1.0
            # Image bullets render larger in browsers than in PPT at the same em value
            img_height *= 0.7
            img_styles.append(f"height: {img_height:.2f}em")
            img_styles.append("width: auto")

            image_type = para.bullet.blip_image_type or 'png'
            if self.use_base64:
                mime_types = {
                    "png": "image/png", "jpeg": "image/jpeg", "jpg": "image/jpeg",
                    "gif": "image/gif", "bmp": "image/bmp", "tiff": "image/tiff",
                    "webp": "image/webp", "emf": "image/x-emf", "wmf": "image/x-wmf",
                }
                mime_type = mime_types.get(image_type.lower(), "image/png")
                encoded = base64.b64encode(para.bullet.blip_image_bytes).decode("utf-8")
                src = f"data:{mime_type};base64,{encoded}"
            else:
                src = self._save_bullet_image(para.bullet.blip_image_bytes, image_type)

            img_style_str = "; ".join(img_styles)
            img_html = f'<img src="{src}" style="{img_style_str}" alt="bullet" />'

            # Bullet column width (= marL) reserves the gap; image is left-aligned
            # within the column, no margin-right needed.
            style_str = "; ".join(bullet_styles)
            return f'<span class="bullet" style="{style_str}">{img_html}</span>'

        # Text bullet (char or autonum)
        escaped_marker = escape(bullet_marker)
        style_str = "; ".join(bullet_styles)
        return f'<span class="bullet" style="{style_str}">{escaped_marker}</span>'

    def _save_bullet_image(self, image_bytes: bytes, image_type: str) -> str:
        """Save a bullet image to file and return the relative path."""
        ext = f".{image_type.lower()}"
        filename = f"bullet-{self._bullet_counter}{ext}"
        self._bullet_counter += 1

        if self.output_dir is None:
            assets_dir = os.path.join("output_assets", "images")
        else:
            assets_dir = os.path.join(self.output_dir, "images")

        if assets_dir not in self._created_dirs:
            os.makedirs(assets_dir, exist_ok=True)
            self._created_dirs.add(assets_dir)

        filepath = os.path.join(assets_dir, filename)
        with open(filepath, 'wb') as f:
            f.write(image_bytes)

        return os.path.join("output_assets", "images", filename).replace(os.sep, '/')
