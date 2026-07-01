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
            Tuple of (title_styles, body_styles, other_styles) dicts.
            other_styles is for non-placeholder shapes (free TextBoxes etc.).
        """
        if self._current_master is not None:
            return self._get_master_text_styles_for(self._current_master)
        return self.master_title_styles, self.master_body_styles, self.master_other_styles

    def _extract_master_text_styles(self):
        """Extract default text styles from the first slide master's txStyles element."""
        try:
            prs = Presentation(str(self.pptx_path))
            if not prs.slide_masters:
                return
            master = prs.slide_masters[0]
            title_styles, body_styles, other_styles = self._get_master_text_styles_for(master)

            # Set as global defaults (for backward compat) and cache
            self.master_title_styles = title_styles
            self.master_body_styles = body_styles
            self.master_other_styles = other_styles
            self._master_text_styles_cache[id(master)] = (title_styles, body_styles, other_styles)

        except Exception:
            pass

    def _get_master_text_styles_for(self, master) -> tuple:
        """
        Get title, body, and other text styles for a specific master, using cache.

        Args:
            master: python-pptx slide master object

        Returns:
            Tuple of (title_styles_dict, body_styles_dict, other_styles_dict).
            other_styles comes from <p:otherStyle> and applies to non-placeholder
            shapes (free TextBoxes, autoshapes with text) per ECMA-376 §21.1.2.2.16.
        """
        mid = id(master)
        if mid in self._master_text_styles_cache:
            return self._master_text_styles_cache[mid]

        title_styles = {}
        body_styles = {}
        other_styles = {}
        try:
            ns = self._ns
            elem = master._element

            title_style = elem.find('.//p:titleStyle', ns)
            if title_style is not None:
                title_styles = self._parse_level_styles(title_style, ns)

            body_style = elem.find('.//p:bodyStyle', ns)
            if body_style is not None:
                body_styles = self._parse_level_styles(body_style, ns)

            other_style = elem.find('.//p:otherStyle', ns)
            if other_style is not None:
                other_styles = self._parse_level_styles(other_style, ns)

        except Exception:
            pass

        self._master_text_styles_cache[mid] = (title_styles, body_styles, other_styles)
        return title_styles, body_styles, other_styles

    def _parse_def_rpr(self, container, ns, parse_bullets: bool = True) -> 'MasterTextStyle':
        """Parse a single <a:defRPr> held inside ``container``.

        ``container`` is the element that owns the defRPr — typically an
        ``<a:lvlNpPr>`` (master / layout / txBody lstStyle level styles) or
        an ``<a:pPr>`` (paragraph-level defRPr). The same five ECMA-376
        attributes — sz, b, i, latin typeface, solidFill color — travel on
        defRPr regardless of which ancestor holds it, so this is the single
        extraction point for the entire font-property inheritance chain.

        Returns an *empty* ``MasterTextStyle`` (every field None) when
        ``container`` has no ``<a:defRPr>`` — callers distinguish "container
        present but no defRPr" from "container absent" via the separate
        :meth:`_get_paragraph_def_rpr` wrapper.

        ``parse_bullets``: bullet children (``<a:buNone>`` / ``<a:buChar>`` /
        ``<a:buAutoNum>`` / ``<a:buFont>``) only appear on ``<a:lvlNpPr>``,
        never on a paragraph ``<a:pPr>``. Paragraph-level callers pass False.
        """
        text_style = MasterTextStyle()
        def_rpr = container.find('a:defRPr', ns)
        if def_rpr is None:
            return text_style

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

        if parse_bullets:
            # Bullet properties live on the lvlNpPr ancestor, not on defRPr.
            bu_none = container.find('a:buNone', ns)
            bu_char = container.find('a:buChar', ns)
            bu_auto = container.find('a:buAutoNum', ns)

            if bu_none is not None:
                text_style.bullet_type = None  # Explicitly no bullet
            elif bu_char is not None:
                text_style.bullet_type = 'char'
                text_style.bullet_char = bu_char.get('char', '\u2022')
            elif bu_auto is not None:
                text_style.bullet_type = 'autonum'
                text_style.bullet_autonum_type = bu_auto.get('type', 'arabicPeriod')

            bu_font = container.find('a:buFont', ns)
            if bu_font is not None:
                text_style.bullet_font = bu_font.get('typeface')

        return text_style

    def _get_paragraph_def_rpr(self, p_elem, ns) -> Optional['MasterTextStyle']:
        """Return the paragraph-level ``<a:pPr><a:defRPr>`` as a
        ``MasterTextStyle``, or ``None`` when the paragraph has no pPr
        or the pPr has no defRPr.

        This is ECMA-376 inheritance layer 2 — it sits between run.rPr (L1)
        and list/master styles (L3/L4). Returning None vs an empty
        MasterTextStyle lets callers short-circuit the whole cascade block
        when there's nothing to apply.
        """
        pPr = p_elem.find('a:pPr', ns)
        if pPr is None:
            return None
        if pPr.find('a:defRPr', ns) is None:
            return None
        return self._parse_def_rpr(pPr, ns, parse_bullets=False)

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
            if lvl_pr.find('a:defRPr', ns) is None:
                continue
            styles[level] = self._parse_def_rpr(lvl_pr, ns, parse_bullets=True)
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

    def _get_shape_txbody_lst_style(self, shape) -> Dict[int, 'MasterTextStyle']:
        """Return a shape's own ``<p:txBody><a:lstStyle>`` parsed into a
        level-keyed MasterTextStyle dict.

        This is ECMA-376 inheritance layer 3 for **non-placeholder** shapes:
        it sits between paragraph ``pPr.defRPr`` (L2) and master
        ``<p:otherStyle>`` (L4). Returns an empty dict when the shape has no
        txBody or no lstStyle — the common case for plain ``add_textbox``
        output, where the cascade just skips this layer.

        Parsing reuses :meth:`_parse_level_styles` (same ``<a:lvlNpPr>``
        shape as master/layout lstStyle), so attribute extraction stays in
        :meth:`_parse_def_rpr`.
        """
        if not hasattr(shape, '_element'):
            return {}
        txBody = shape._element.find('p:txBody', self._ns)
        if txBody is None:
            return {}
        lst = txBody.find('a:lstStyle', self._ns)
        if lst is None:
            return {}
        return self._parse_level_styles(lst, self._ns)
