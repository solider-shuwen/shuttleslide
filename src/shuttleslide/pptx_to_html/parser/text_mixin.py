"""
Text parsing mixin for PPTXParser.

Handles parsing of placeholder shapes and text box shapes with paragraph-level
structure, run formatting, and bullet properties.
"""

import re
from typing import Optional

from shuttleslide.pptx_to_html.models import (
    RunElement, ParagraphElement, TextElement,
)
from shuttleslide.pptx_to_html.utils.text_sanitizer import sanitize_pptx_text
from shuttleslide.pptx_to_html.utils.namespaces import NS_A, NAMESPACES
from shuttleslide.pptx_to_html.utils.units import emu_to_pt, emu_to_px, angle_to_degrees


class TextMixin:
    """Text element parsing methods for placeholders and text boxes."""

    @staticmethod
    def _detect_autofit(body_pr, ns):
        """Inspect <a:bodyPr> for the autofit-mode child element.

        OpenXML defines three mutually exclusive children that control how a
        text frame reacts when its content doesn't fit the shape box:

          - <a:spAutoFit/>   — shape grows to fit the text (soft height)
          - <a:normAutofit fontScale="..." lnSpcReduction="..."/> — text is
                              shrunk so it fits the box; fontScale is in
                              1/1000 of a percent (100000 = 100%)
          - <a:noAutofit/>   — text overflows the box (clipped in our renderer)

        PPT's effective default when none of the three is present is
        noAutofit (per ECMA-376).

        Returns:
            (mode: str, fontScale: Optional[str]) — the fontScale string is
            returned raw (still in PPT units) so callers can decide whether
            and how to apply it; None for non-normAutofit modes.
        """
        if body_pr is None:
            return "noAutofit", None
        if body_pr.find("a:spAutoFit", ns) is not None:
            return "spAutoFit", None
        norm = body_pr.find("a:normAutofit", ns)
        if norm is not None:
            return "normAutofit", norm.get("fontScale")
        return "noAutofit", None

    @staticmethod
    def _build_text_box_metadata(scene3d_camera, autofit_mode, autofit_fontScale):
        """Build metadata dict for a text-box element.

        Always carries 'autofit' so layout code can branch on it; carries
        'scene3d_camera' / 'normAutofit_fontScale' only when relevant so we
        don't pollute every element with None values.
        """
        metadata = {"autofit": autofit_mode}
        if scene3d_camera:
            metadata["scene3d_camera"] = scene3d_camera
        if autofit_fontScale is not None:
            metadata["normAutofit_fontScale"] = autofit_fontScale
        return metadata

    def _parse_placeholder(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> TextElement:
        """Parse a placeholder shape with paragraph support."""
        text = ""
        paragraphs = []

        if hasattr(shape, "text_frame") and shape.text_frame:
            text_frame = shape.text_frame
            text = sanitize_pptx_text(text_frame.text)  # Sanitize for backward compatibility

            # Determine placeholder type once — it picks master titleStyle
            # vs bodyStyle for every paragraph in this shape. Previously this
            # was computed AFTER the paragraph loop, which made the cascade
            # comment "override body with title styles once is_title is known"
            # impossible to honor — ctrTitle silently picked up bodyStyle.
            ph_type = None
            if hasattr(shape, "placeholder_format") and shape.placeholder_format:
                try:
                    ph_type = int(shape.placeholder_format.type)
                except (TypeError, ValueError):
                    ph_type = shape.placeholder_format.type
            # PowerPoint PP_PLACEHOLDER enum: 1=TITLE, 3=CENTER_TITLE (ctrTitle),
            # 14=VERTICAL_TITLE. The previous (0, 14) check was dead code —
            # 0 is not a valid placeholder type, so is_title was always False
            # and every title placeholder silently picked up bodyStyle instead
            # of titleStyle.
            is_title = ph_type in (1, 3, 14)

            # Extract all paragraphs with their formatting
            for para in text_frame.paragraphs:
                # Extract paragraph text and sanitize it
                para_text = sanitize_pptx_text(para.text)

                # Get paragraph level
                para_level = para.level if hasattr(para, 'level') else 0

                # Get paragraph alignment
                alignment = None
                if hasattr(para, 'alignment') and para.alignment is not None:
                    alignment_map = {1: 'left', 2: 'center', 3: 'right', 4: 'justify', 5: 'distribute'}
                    alignment = alignment_map.get(int(para.alignment), None)

                # Extract paragraph spacing
                line_spacing = None
                line_spacing_pts = None
                spacing_before = None
                spacing_after = None

                try:
                    ls = para.line_spacing
                    if ls is not None:
                        if isinstance(ls, float):
                            line_spacing = ls
                        else:
                            line_spacing_pts = ls.pt
                except Exception:
                    pass

                try:
                    sb = para.space_before
                    if sb is not None:
                        spacing_before = sb.pt
                except Exception:
                    pass

                try:
                    sa = para.space_after
                    if sa is not None:
                        spacing_after = sa.pt
                except Exception:
                    pass

                # Extract formatting from all runs
                font_name = None
                font_size = None
                bold = False
                italic = False
                color = None
                runs = []

                if para.runs:
                    for run in para.runs:
                        font = run.font
                        run_bold = font.bold
                        run_italic = font.italic
                        run_font_name = font.name
                        run_font_size = font.size.pt if font.size else None
                        run_color = self._extract_run_color(font, run._r)
                        run_text = sanitize_pptx_text(run.text)

                        runs.append(RunElement(
                            text=run_text,
                            bold=run_bold,
                            italic=run_italic,
                            font_name=run_font_name,
                            font_size=run_font_size,
                            color=run_color,
                        ))

                    # Paragraph-level defaults from first run (L1)
                    first = runs[0]
                    font_name = first.font_name
                    font_size = first.font_size
                    bold = first.bold if first.bold is not None else False
                    italic = first.italic if first.italic is not None else False
                    color = first.color

                # Apply master defaults when paragraph has no explicit spacing
                if line_spacing is None and line_spacing_pts is None:
                    if self._current_master is not None:
                        line_spacing = self._get_master_spacing_for(self._current_master)
                    if line_spacing is None:
                        line_spacing = self.default_line_spacing

                # ECMA-376 font-property inheritance chain (priority high → low):
                #   L1 run.rPr  →  L2 pPr.defRPr  →  L3 list style  →
                #   L4 master txStyles  →  (L5 theme, deferred)  →  18pt floor
                # Every layer is `if X is None/unset`, so the FIRST layer that
                # provides a value wins. Order is load-bearing.
                lst_level = para_level + 1  # lvl1pPr is 1-based; para.level is 0-based

                # L2: paragraph-level <a:pPr><a:defRPr>. Common when slide XML
                # sets sz/bold on the paragraph instead of on each run — e.g.
                # sample.pptx stores <a:pPr><a:defRPr sz="6000" b="1"/> for its
                # 60pt bold title with no run-level sz. Previously dropped
                # entirely, every such paragraph fell through to the 18pt floor.
                para_def = self._get_paragraph_def_rpr(para._p, self._ns)
                if para_def:
                    if font_size is None and para_def.font_size is not None:
                        font_size = para_def.font_size
                    if bold is False and para_def.bold is not None:
                        bold = para_def.bold
                    if italic is False and para_def.italic is not None:
                        italic = para_def.italic
                    if font_name is None and para_def.font_name is not None:
                        font_name = para_def.font_name
                    if color is None and para_def.color is not None:
                        color = para_def.color

                # L3: layout placeholder lstStyle. Per ECMA-376 §21.1.2.2.16,
                # list style outranks master txStyles, so this MUST run before
                # the master cascade below. The previous code had them in the
                # wrong order, which silently let master bodyStyle override the
                # layout's per-placeholder size (e.g. layout ctrTitle 44pt
                # beaten by master bodyStyle lvl1 sz).
                if (font_size is None or font_name is None or color is None
                        or bold is False or italic is False):
                    if (hasattr(self, '_current_layout') and self._current_layout
                            and hasattr(shape, 'placeholder_format') and shape.placeholder_format):
                        try:
                            layout_defaults = self._get_layout_placeholder_defaults(
                                self._current_layout, int(shape.placeholder_format.type))
                            if layout_defaults and lst_level in layout_defaults:
                                lst_style = layout_defaults[lst_level]
                                if font_size is None and lst_style.font_size:
                                    font_size = lst_style.font_size
                                if font_name is None and lst_style.font_name:
                                    font_name = lst_style.font_name
                                if color is None and lst_style.color:
                                    color = lst_style.color
                                if bold is False and lst_style.bold is not None:
                                    bold = lst_style.bold
                                if italic is False and lst_style.italic is not None:
                                    italic = lst_style.italic
                        except Exception:
                            pass

                # L4: master txStyles. Title placeholders (type 0/14) use
                # <p:titleStyle>; everything else uses <p:bodyStyle>. The old
                # comment promised a title-override "after is_title is
                # determined" that never ran — is_title was computed after the
                # paragraph loop, so ctrTitle silently picked up bodyStyle.
                title_styles, body_styles, _ = self._get_current_master_styles()
                master_styles = title_styles if is_title else body_styles
                if master_styles and lst_level in master_styles:
                    ms = master_styles[lst_level]
                    if font_name is None and ms.font_name:
                        font_name = ms.font_name
                    if font_size is None and ms.font_size:
                        font_size = ms.font_size
                    if color is None and ms.color:
                        color = ms.color
                    if bold is False and ms.bold is not None:
                        bold = ms.bold
                    if italic is False and ms.italic is not None:
                        italic = ms.italic

                # Parse bullet properties from OpenXML
                bullet = None
                margin_left_pt = None
                indent_pt = None
                try:
                    p_elem = para._p  # The <a:p> XML element
                    ppr = p_elem.find('a:pPr', self._ns)
                    bullet = self._parse_bullet_properties(ppr, para_level, shape)
                    if ppr is not None:
                        marL = ppr.get('marL')
                        if marL:
                            margin_left_pt = emu_to_pt(int(marL))
                        indent_val = ppr.get('indent')
                        if indent_val:
                            indent_pt = emu_to_pt(int(indent_val))
                except Exception:
                    pass

                # PPT body default when the entire inheritance chain (run →
                # paragraph → master body → layout lstStyle) fails to specify
                # a size.  Hardcoded per ECMA-376 / Office docs: 18pt for
                # unspecified body text.  Without this floor, downstream code
                # (line-height emission, shrink estimator, bullet column math)
                # sees None and either crashes or falls back to ad-hoc
                # constants at each call site.
                if font_size is None:
                    font_size = 18.0

                paragraphs.append(ParagraphElement(
                    text=para_text,
                    level=para_level,
                    alignment=alignment,
                    font_name=font_name,
                    font_size=font_size,
                    bold=bold,
                    italic=italic,
                    color=color,
                    line_spacing=line_spacing,
                    line_spacing_pts=line_spacing_pts,
                    spacing_before=spacing_before,
                    spacing_after=spacing_after,
                    margin_left=margin_left_pt,
                    indent=indent_pt,
                    bullet=bullet,
                    runs=runs,
                ))

        # Resolve bullet inheritance for all paragraphs
        for p in paragraphs:
            if p.bullet and p.bullet.type == 'inherited':
                p.bullet = self._resolve_bullet_inheritance(
                    p.bullet, p.level, is_title=is_title, is_placeholder=True
                )

        # Extract rotation and transform information
        rotation = None
        vert = None
        flip_h = False
        flip_v = False
        scene3d_camera = None
        vertical_align = None
        autofit_mode = "noAutofit"
        autofit_fontScale = None

        if hasattr(shape, "_element"):
            elem = shape._element
            ns = NS_A

            # Check body properties for vertical text and alignment
            body_pr = elem.find('.//a:bodyPr', ns)
            if body_pr is not None:
                vert = body_pr.get('vert')  # eaVert, mongolianVert, etc.

                # Extract vertical alignment from anchor attribute
                anchor = body_pr.get('anchor')
                if anchor:
                    anchor_map = {'t': 'top', 'ctr': 'middle', 'b': 'bottom'}
                    vertical_align = anchor_map.get(anchor)

            # Detect autofit mode (spAutoFit / normAutofit / noAutofit)
            autofit_mode, autofit_fontScale = self._detect_autofit(body_pr, ns)

            # Check transformation for flip and rotation
            xfrm = elem.find('.//a:xfrm', ns)
            if xfrm is not None:
                flip_h = xfrm.get('flipH') == '1'
                flip_v = xfrm.get('flipV') == '1'
                # Rotation is in EMU (1/60,000 of a degree)
                rot_emu = xfrm.get('rot')
                if rot_emu:
                    try:
                        rotation = angle_to_degrees(float(rot_emu))
                    except (ValueError, TypeError):
                        pass

            # Extract scene3d camera preset
            camera = elem.find('.//a:scene3d/a:camera', ns)
            if camera is not None:
                prst = camera.get('prst')
                if prst:
                    scene3d_camera = prst

        metadata = {"placeholder_type": shape.placeholder_format.type if hasattr(shape, "placeholder_format") else None}
        if scene3d_camera:
            metadata['scene3d_camera'] = scene3d_camera
        metadata['autofit'] = autofit_mode
        if autofit_fontScale is not None:
            metadata['normAutofit_fontScale'] = autofit_fontScale

        # Note: _parse_text_box builds the equivalent dict via
        # _build_text_box_metadata() — kept inline here because the
        # placeholder_type entry is specific to this code path.

        return TextElement(
            element_type="text",
            left=left,
            top=top,
            width=width,
            height=height,
            z_order=z_order,
            text=text,
            paragraphs=paragraphs,
            is_title=is_title,
            metadata=metadata,
            rotation=rotation,
            vert=vert,
            flip_h=flip_h,
            flip_v=flip_v,
            vertical_align=vertical_align,
        )

    def _parse_text_box(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> TextElement:
        """Parse a text box shape with paragraph-level structure."""
        text_frame = shape.text_frame
        text = sanitize_pptx_text(text_frame.text)  # Sanitize for backward compatibility

        # L3 source: this shape's own <p:txBody><a:lstStyle>. Parsed once for
        # the whole shape — every paragraph in this textbox shares it. May be
        # empty when the textbox has no lstStyle (the common case for plain
        # add_textbox output); the cascade below just skips it.
        txbody_lst = self._get_shape_txbody_lst_style(shape)

        # Extract all paragraphs with their formatting
        paragraphs = []
        for para in text_frame.paragraphs:
            # Extract paragraph text and sanitize it
            para_text = sanitize_pptx_text(para.text)

            # Get paragraph level
            para_level = para.level if hasattr(para, 'level') else 0

            # Get paragraph alignment
            alignment = None
            if hasattr(para, 'alignment') and para.alignment is not None:
                alignment_map = {1: 'left', 2: 'center', 3: 'right', 4: 'justify', 5: 'distribute'}
                alignment = alignment_map.get(int(para.alignment), None)

            # Extract paragraph spacing
            line_spacing = None
            line_spacing_pts = None
            spacing_before = None
            spacing_after = None

            try:
                ls = para.line_spacing
                if ls is not None:
                    if isinstance(ls, float):
                        line_spacing = ls
                    else:
                        line_spacing_pts = ls.pt
            except Exception:
                pass

            try:
                sb = para.space_before
                if sb is not None:
                    spacing_before = sb.pt
            except Exception:
                pass

            try:
                sa = para.space_after
                if sa is not None:
                    spacing_after = sa.pt
            except Exception:
                pass

            # Extract formatting from all runs
            font_name = None
            font_size = None
            bold = False
            italic = False
            color = None
            runs = []

            if para.runs:
                for run in para.runs:
                    font = run.font
                    run_bold = font.bold
                    run_italic = font.italic
                    run_font_name = font.name
                    run_font_size = font.size.pt if font.size else None
                    run_color = self._extract_run_color(font, run._r)
                    run_text = sanitize_pptx_text(run.text)

                    runs.append(RunElement(
                        text=run_text,
                        bold=run_bold,
                        italic=run_italic,
                        font_name=run_font_name,
                        font_size=run_font_size,
                        color=run_color,
                    ))

                # Paragraph-level defaults from first run (L1)
                first = runs[0]
                font_name = first.font_name
                font_size = first.font_size
                bold = first.bold if first.bold is not None else False
                italic = first.italic if first.italic is not None else False
                color = first.color

            # Apply master defaults when paragraph has no explicit spacing
            if line_spacing is None and line_spacing_pts is None:
                line_spacing = self.default_line_spacing

            # ECMA-376 inheritance for non-placeholder shapes (§21.1.2.2.16):
            #   L1 run.rPr → L2 pPr.defRPr → L3 txBody lstStyle →
            #   L4 master otherStyle → (L5 theme, deferred) → 18pt floor
            lst_level = para_level + 1  # lvl1pPr is 1-based; para.level is 0-based

            # L2: paragraph-level <a:pPr><a:defRPr>. Previously dropped here
            # too — same bug class as the placeholder path.
            para_def = self._get_paragraph_def_rpr(para._p, self._ns)
            if para_def:
                if font_size is None and para_def.font_size is not None:
                    font_size = para_def.font_size
                if bold is False and para_def.bold is not None:
                    bold = para_def.bold
                if italic is False and para_def.italic is not None:
                    italic = para_def.italic
                if font_name is None and para_def.font_name is not None:
                    font_name = para_def.font_name
                if color is None and para_def.color is not None:
                    color = para_def.color

            # L3: this shape's own <p:txBody><a:lstStyle> (parsed once above
            # the paragraph loop). The old comment listed this layer in the
            # chain but the code never read it — non-placeholder shapes with
            # a txBody lstStyle silently fell through to otherStyle.
            if txbody_lst and lst_level in txbody_lst:
                ts = txbody_lst[lst_level]
                if font_size is None and ts.font_size:
                    font_size = ts.font_size
                if font_name is None and ts.font_name:
                    font_name = ts.font_name
                if color is None and ts.color:
                    color = ts.color
                if bold is False and ts.bold is not None:
                    bold = ts.bold
                if italic is False and ts.italic is not None:
                    italic = ts.italic

            # L4: master <p:otherStyle>. Free TextBoxes inherit ONLY from
            # otherStyle — NOT titleStyle/bodyStyle (those are placeholder-only).
            # poster.pptx: bodyStyle lvl1 sz=4409 but otherStyle lvl1 sz=2835;
            # PPT renders these TextBoxes at 28.35pt, so otherStyle is correct.
            _, _, current_other_styles = self._get_current_master_styles()
            if current_other_styles and lst_level in current_other_styles:
                other_style = current_other_styles[lst_level]
                if font_name is None and other_style.font_name:
                    font_name = other_style.font_name
                if font_size is None and other_style.font_size:
                    font_size = other_style.font_size
                if color is None and other_style.color:
                    color = other_style.color
                if bold is False and other_style.bold is not None:
                    bold = other_style.bold
                if italic is False and other_style.italic is not None:
                    italic = other_style.italic

            # Parse bullet properties from OpenXML
            bullet = None
            try:
                p_elem = para._p  # The <a:p> XML element
                ppr = p_elem.find('a:pPr', self._ns)
                bullet = self._parse_bullet_properties(ppr, para_level, shape)
                margin_left_pt = None
                indent_pt = None
                if ppr is not None:
                    marL = ppr.get('marL')
                    if marL:
                        margin_left_pt = emu_to_pt(int(marL))
                    indent_val = ppr.get('indent')
                    if indent_val:
                        indent_pt = emu_to_pt(int(indent_val))
            except Exception:
                pass

            # PPT body default when the entire inheritance chain fails to
            # specify a size.  18pt per ECMA-376 / Office docs.  Without
            # this floor, downstream code sees None and falls back to
            # ad-hoc constants at each call site.
            if font_size is None:
                font_size = 18.0

            paragraphs.append(ParagraphElement(
                text=para_text,
                level=para_level,
                alignment=alignment,
                font_name=font_name,
                font_size=font_size,
                bold=bold,
                italic=italic,
                color=color,
                line_spacing=line_spacing,
                line_spacing_pts=line_spacing_pts,
                spacing_before=spacing_before,
                spacing_after=spacing_after,
                margin_left=margin_left_pt,
                indent=indent_pt,
                bullet=bullet,
                runs=runs,
            ))

        # Resolve bullet inheritance for text box paragraphs
        # Non-placeholder text boxes don't inherit bullets from master
        for p in paragraphs:
            if p.bullet and p.bullet.type == 'inherited':
                p.bullet = self._resolve_bullet_inheritance(
                    p.bullet, p.level, is_title=False, is_placeholder=False
                )

        # For backward compatibility, use first paragraph's formatting
        font_name = None
        font_size = None
        bold = False
        italic = False
        color = None

        if paragraphs and paragraphs[0].runs:
            first_para = paragraphs[0]
            font_name = first_para.font_name
            font_size = first_para.font_size
            bold = first_para.bold
            italic = first_para.italic
            color = first_para.color

        # If no explicit text color from runs, try fontRef in <p:style>
        # (e.g., shapes like rightArrow with theme style: <a:fontRef><a:schemeClr val="lt1"/></a:fontRef>)
        if color is None and hasattr(shape, '_element'):
            try:
                _ns_style = {'a': NAMESPACES['a'], 'p': NAMESPACES['p']}
                _style = shape._element.find('.//p:style', _ns_style)
                if _style is not None:
                    _font_ref = _style.find('.//a:fontRef', _ns_style)
                    if _font_ref is not None:
                        _scheme_clr = _font_ref.find('.//a:schemeClr', _ns_style)
                        if _scheme_clr is not None:
                            _val = _scheme_clr.get('val')
                            if _val and self.theme_color_extractor:
                                color = self._scheme_clr_to_color(_val)
            except Exception:
                pass
            except Exception:
                pass

        # Extract rotation and transform information
        rotation = None
        vert = None
        flip_h = False
        flip_v = False
        scene3d_camera = None
        vertical_align = None
        autofit_mode = "noAutofit"
        autofit_fontScale = None

        if hasattr(shape, "_element"):
            elem = shape._element
            ns = NS_A

            # Check body properties for vertical text and alignment
            body_pr = elem.find('.//a:bodyPr', ns)
            if body_pr is not None:
                vert = body_pr.get('vert')  # eaVert, mongolianVert, etc.

                # Extract vertical alignment from anchor attribute
                anchor = body_pr.get('anchor')
                if anchor:
                    anchor_map = {'t': 'top', 'ctr': 'middle', 'b': 'bottom'}
                    vertical_align = anchor_map.get(anchor)

            # Detect autofit mode (spAutoFit / normAutofit / noAutofit)
            autofit_mode, autofit_fontScale = self._detect_autofit(body_pr, ns)

            # Check transformation for flip and rotation
            xfrm = elem.find('.//a:xfrm', ns)
            if xfrm is not None:
                flip_h = xfrm.get('flipH') == '1'
                flip_v = xfrm.get('flipV') == '1'
                # Rotation is in EMU (1/60,000 of a degree)
                rot_emu = xfrm.get('rot')
                if rot_emu:
                    try:
                        rotation = angle_to_degrees(float(rot_emu))
                    except (ValueError, TypeError):
                        pass

            # Extract scene3d camera preset
            camera = elem.find('.//a:scene3d/a:camera', ns)
            if camera is not None:
                prst = camera.get('prst')
                if prst:
                    scene3d_camera = prst

        # Extract outline/border properties (line_color and line_width)
        line_color = None
        line_width = None

        # Check for noFill in XML BEFORE accessing python-pptx line properties
        # Accessing shape.line.color triggers python-pptx to resolve style references,
        # which can modify the XML and replace <noFill/> with <solidFill/>
        _line_has_noFill = False
        _ln_exists = False
        if hasattr(shape, '_element'):
            try:
                _ns = {'a': NAMESPACES['a'], 'p': NAMESPACES['p']}
                _spPr = shape._element.find('./p:spPr', _ns)
                if _spPr is not None:
                    _ln = _spPr.find('./a:ln', _ns)
                    if _ln is not None:
                        _ln_exists = True
                        _noFill = _ln.find('./a:noFill', _ns)
                        if _noFill is not None:
                            _line_has_noFill = True
            except Exception:
                pass

        # Extract line color from shape.line (same logic as _parse_generic_shape)
        if _ln_exists and not _line_has_noFill and hasattr(shape, "line") and shape.line:
            try:
                # Try color attribute first
                if hasattr(shape.line, "color") and shape.line.color:
                    line_color_obj = shape.line.color

                    # Determine color type to pick the right extraction path.
                    # MSO_COLOR_TYPE: RGB(1), SCHEME(2)
                    # Do NOT rely on theme_color is not None — NOT_THEME_COLOR (0) is not None.
                    color_type = getattr(line_color_obj, 'type', None)

                    if color_type == 2:  # SCHEME — theme color reference
                        theme_color = line_color_obj.theme_color
                        if self.theme_color_extractor:
                            theme_rgb = self.theme_color_extractor.get_theme_color(theme_color)
                            if theme_rgb:
                                line_color = theme_rgb

                    # RGB or other concrete color
                    if not line_color and hasattr(line_color_obj, "rgb") and line_color_obj.rgb:
                        rgb_obj = line_color_obj.rgb
                        # Handle RGBColor objects
                        if hasattr(rgb_obj, '__class__') and 'RGBColor' in str(rgb_obj.__class__):
                            rgb_str = str(rgb_obj).strip()
                            if len(rgb_str) == 6 and rgb_str.isalnum():
                                line_color = f"#{rgb_str}"
                        # Handle string RGB values
                        elif isinstance(rgb_obj, str):
                            rgb_str = rgb_obj.strip()
                            if len(rgb_str) >= 6:
                                line_color = f"#{rgb_str[:6]}"
                        # Handle integer RGB values
                        elif isinstance(rgb_obj, int) and rgb_obj > 0:
                            line_color = f"#{rgb_obj:06x}"
            except (AttributeError, TypeError, ValueError):
                pass

        # If no direct line color, try to extract from style/lnRef (theme style reference)
        if _ln_exists and not _line_has_noFill and not line_color and hasattr(shape, '_element'):
            try:
                ns_xml = {'a': NAMESPACES['a'], 'p': NAMESPACES['p']}
                style = shape._element.find('.//p:style', ns_xml)
                if style is not None:
                    ln_ref = style.find('.//a:lnRef', ns_xml)
                    if ln_ref is not None:
                        scheme_clr = ln_ref.find('.//a:schemeClr', ns_xml)
                        if scheme_clr is not None:
                            val = scheme_clr.get('val')
                            line_color = self._scheme_clr_to_color(val)
            except Exception:
                pass

        # Extract line width from XML (<a:ln w="...">)
        if hasattr(shape, '_element'):
            try:
                ns_xml = NS_A
                ln_elem = shape._element.find('.//a:ln', ns_xml)
                if ln_elem is not None:
                    w_attr = ln_elem.get('w')
                    if w_attr:
                        line_width = emu_to_px(int(w_attr))
            except Exception:
                pass

        return TextElement(
            element_type="text",
            left=left,
            top=top,
            width=width,
            height=height,
            z_order=z_order,
            text=text,
            paragraphs=paragraphs,
            font_name=font_name,
            font_size=font_size,
            bold=bold,
            italic=italic,
            color=color,
            rotation=rotation,
            vert=vert,
            flip_h=flip_h,
            flip_v=flip_v,
            vertical_align=vertical_align,
            line_color=line_color,
            line_width=line_width,
            metadata=self._build_text_box_metadata(scene3d_camera, autofit_mode, autofit_fontScale),
        )
