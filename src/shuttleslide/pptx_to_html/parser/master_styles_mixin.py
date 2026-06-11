"""
Master styles parsing mixin for PPTXParser.

Handles extraction of default text styles, paragraph spacing, and bullet
properties from slide masters.
"""

from typing import Optional, Dict

from pptx import Presentation

from shuttleslide.pptx_to_html.models import MasterTextStyle


class MasterStylesMixin:
    """Slide master text style and spacing extraction methods."""

    def _extract_master_default_spacing(self):
        """Extract default line spacing from the first slide master's body style."""
        try:
            prs = Presentation(str(self.pptx_path))
            if not prs.slide_masters:
                return
            master = prs.slide_masters[0]
            spacing = self._get_master_spacing_for(master)

            # Set as global default
            self.default_line_spacing = spacing
        except Exception:
            pass

    def _get_master_spacing_for(self, master) -> Optional[float]:
        """
        Get default line spacing for a specific master, using cache.

        Args:
            master: python-pptx slide master object

        Returns:
            Default line spacing as float multiplier, or None
        """
        mid = id(master)
        if mid in self._master_spacing_cache:
            return self._master_spacing_cache[mid]

        spacing = None
        try:
            ns = self._ns
            elem = master._element

            body_style = elem.find('.//p:bodyStyle', ns)
            if body_style is not None:
                lvl1 = body_style.find('a:lvl1pPr', ns)
                if lvl1 is not None:
                    ln_spc = lvl1.find('a:lnSpc', ns)
                    if ln_spc is not None:
                        spc_pct = ln_spc.find('a:spcPct', ns)
                        spc_pts = ln_spc.find('a:spcPts', ns)
                        if spc_pct is not None:
                            val = int(spc_pct.get('val', 100000))
                            spacing = val / 100000.0
                        elif spc_pts is not None:
                            val = int(spc_pts.get('val', 0))
                            spacing = val / 100.0
        except Exception:
            pass

        self._master_spacing_cache[mid] = spacing
        return spacing

    def _get_current_master_styles(self) -> tuple:
        """
        Get text styles for the current slide's master.

        Returns:
            Tuple of (title_styles, body_styles) dicts
        """
        if self._current_master is not None:
            return self._get_master_text_styles_for(self._current_master)
        return self.master_title_styles, self.master_body_styles

    def _extract_master_text_styles(self):
        """Extract default text styles from the first slide master's txStyles element."""
        try:
            prs = Presentation(str(self.pptx_path))
            if not prs.slide_masters:
                return
            master = prs.slide_masters[0]
            title_styles, body_styles = self._get_master_text_styles_for(master)

            # Set as global defaults (for backward compat) and cache
            self.master_title_styles = title_styles
            self.master_body_styles = body_styles
            self._master_text_styles_cache[id(master)] = (title_styles, body_styles)

        except Exception:
            pass

    def _get_master_text_styles_for(self, master) -> tuple:
        """
        Get title and body text styles for a specific master, using cache.

        Args:
            master: python-pptx slide master object

        Returns:
            Tuple of (title_styles_dict, body_styles_dict)
        """
        mid = id(master)
        if mid in self._master_text_styles_cache:
            return self._master_text_styles_cache[mid]

        title_styles = {}
        body_styles = {}
        try:
            ns = self._ns
            elem = master._element

            title_style = elem.find('.//p:titleStyle', ns)
            if title_style is not None:
                title_styles = self._parse_level_styles(title_style, ns)

            body_style = elem.find('.//p:bodyStyle', ns)
            if body_style is not None:
                body_styles = self._parse_level_styles(body_style, ns)

        except Exception:
            pass

        self._master_text_styles_cache[mid] = (title_styles, body_styles)
        return title_styles, body_styles

    def _parse_level_styles(self, style_elem, ns) -> Dict[int, 'MasterTextStyle']:
        """
        Parse paragraph-level styles from a titleStyle or bodyStyle element.

        Args:
            style_elem: <p:titleStyle> or <p:bodyStyle> XML element
            ns: XML namespaces dict

        Returns:
            Dict mapping level (int) to MasterTextStyle
        """
        styles = {}
        for level in range(1, 10):
            lvl_pr = style_elem.find(f'a:lvl{level}pPr', ns)
            if lvl_pr is None:
                continue

            def_rpr = lvl_pr.find('a:defRPr', ns)
            if def_rpr is None:
                continue

            text_style = MasterTextStyle()

            # Font size: sz attribute is in hundredths of a point (4400 = 44pt)
            sz = def_rpr.get('sz')
            if sz:
                text_style.font_size = int(sz) / 100.0

            # Bold
            b = def_rpr.get('b')
            if b == '1':
                text_style.bold = True
            elif b == '0':
                text_style.bold = False

            # Italic
            i = def_rpr.get('i')
            if i == '1':
                text_style.italic = True
            elif i == '0':
                text_style.italic = False

            # Font name from <a:latin typeface="...">
            latin = def_rpr.find('a:latin', ns)
            if latin is not None:
                typeface = latin.get('typeface')
                if typeface and not typeface.startswith('+'):
                    # Skip theme references like "+mj-lt", "+mn-lt"
                    text_style.font_name = typeface

            # Color from <a:solidFill>
            solid_fill = def_rpr.find('a:solidFill', ns)
            if solid_fill is not None:
                text_style.color = self._resolve_xml_color(solid_fill)

            # Bullet properties from the level style
            bu_none = lvl_pr.find('a:buNone', ns)
            bu_char = lvl_pr.find('a:buChar', ns)
            bu_auto = lvl_pr.find('a:buAutoNum', ns)

            if bu_none is not None:
                text_style.bullet_type = None  # Explicitly no bullet
            elif bu_char is not None:
                text_style.bullet_type = 'char'
                text_style.bullet_char = bu_char.get('char', '\u2022')
            elif bu_auto is not None:
                text_style.bullet_type = 'autonum'
                text_style.bullet_autonum_type = bu_auto.get('type', 'arabicPeriod')

            bu_font = lvl_pr.find('a:buFont', ns)
            if bu_font is not None:
                text_style.bullet_font = bu_font.get('typeface')

            styles[level] = text_style

        return styles

    def _get_layout_placeholder_defaults(self, layout, placeholder_type) -> dict:
        """
        Get default text styles from the matching placeholder in a slide layout.

        Each layout shape (placeholder) can have its own <a:lstStyle> inside
        <p:txBody> with level-specific defaults (font size, bold, etc.).
        These apply when the slide's placeholder text has no explicit formatting.

        Args:
            layout: python-pptx slide layout object
            placeholder_type: placeholder type string (e.g., 'ctrTitle', 'title', 'body')

        Returns:
            Dict mapping level (int) to MasterTextStyle, or empty dict
        """
        if layout is None:
            return {}

        # Use a cache key based on layout id + placeholder type
        layout_id = id(layout)
        cache_key = (layout_id, placeholder_type)
        if hasattr(self, '_layout_placeholder_cache') and cache_key in self._layout_placeholder_cache:
            return self._layout_placeholder_cache[cache_key]

        defaults = {}
        try:
            ns = self._ns
            layout_elem = layout._element

            # Build a mapping from python-pptx placeholder type enum values to
            # OpenXML ph type strings.  python-pptx exposes this as the
            # ``placeholder_format.idx`` / ``placeholder_format.type`` attributes,
            # but it's easier to walk the XML directly.
            #
            # OpenXML ph types we care about:
            PH_TYPE_MAP = {
                1: 'title',      # PP_PLACEHOLDER.TITLE
                2: 'body',       # PP_PLACEHOLDER.BODY
                3: 'ctrTitle',   # PP_PLACEHOLDER.CENTERED_TITLE
                14: 'ctrTitle',  # PP_PLACEHOLDER.CENTERED_TITLE (some versions)
            }
            target_types = PH_TYPE_MAP.get(placeholder_type, None)
            if target_types is None:
                # Try string match directly
                target_types = str(placeholder_type).lower() if isinstance(placeholder_type, str) else None
            if target_types is None:
                return {}

            # Accept either a single string or a set
            if isinstance(target_types, str):
                target_types = {target_types}
            # Normalize to lowercase for comparison
            target_types = {t.lower() for t in target_types}

            # Walk layout shapes to find matching placeholder
            for sp in layout_elem.findall('.//p:sp', ns):
                ph = sp.find('.//p:nvSpPr/p:nvPr/p:ph', ns)
                if ph is None:
                    continue
                ph_type = ph.get('type', 'body').lower()

                if ph_type not in target_types:
                    continue

                # Found matching placeholder — extract lstStyle
                txBody = sp.find('p:txBody', ns)
                if txBody is None:
                    continue
                lstStyle = txBody.find('a:lstStyle', ns)
                if lstStyle is None:
                    continue

                # Parse level styles using the existing _parse_level_styles helper
                # (it expects a parent element containing <a:lvl{i}pPr> children)
                defaults = self._parse_level_styles(lstStyle, ns)
                break

        except Exception:
            pass

        # Cache the result
        if not hasattr(self, '_layout_placeholder_cache'):
            self._layout_placeholder_cache = {}
        self._layout_placeholder_cache[cache_key] = defaults
        return defaults
