"""
Bullet parsing mixin for PPTXParser.

Handles extraction and inheritance resolution of bullet/numbering properties
from paragraph XML elements and master styles.
"""

from shuttleslide.pptx_to_html.models import BulletProperties
from shuttleslide.pptx_to_html.utils.namespaces import NS_R_CLARK


class BulletsMixin:
    """Bullet property parsing and inheritance resolution methods."""

    def _parse_bullet_properties(self, ppr, para_level: int, shape=None) -> BulletProperties:
        """
        Parse bullet properties from a paragraph's <a:pPr> XML element.

        Args:
            ppr: <a:pPr> XML element (can be None)
            para_level: Paragraph indent level (0-8)

        Returns:
            BulletProperties with parsed bullet info
        """
        ns = self._ns

        if ppr is None:
            return BulletProperties(type='inherited')

        # Check for explicit no-bullet
        bu_none = ppr.find('a:buNone', ns)
        if bu_none is not None:
            return BulletProperties(type='none')

        # Check for character bullet
        bu_char = ppr.find('a:buChar', ns)
        if bu_char is not None:
            bullet = BulletProperties(type='char')
            bullet.char = bu_char.get('char', '\u2022')

            # Bullet styling from pPr siblings
            bu_font = ppr.find('a:buFont', ns)
            if bu_font is not None:
                bullet.font_typeface = bu_font.get('typeface')

            bu_sz = ppr.find('a:buSzPct', ns)
            if bu_sz is not None:
                try:
                    bullet.font_size_pct = int(bu_sz.get('val', 100000))
                except (ValueError, TypeError):
                    pass

            bu_clr = ppr.find('a:buClr', ns)
            if bu_clr is not None:
                bullet.color = self._resolve_xml_color(bu_clr)

            return bullet

        # Check for auto-numbered bullet
        bu_auto = ppr.find('a:buAutoNum', ns)
        if bu_auto is not None:
            bullet = BulletProperties(type='autonum')
            bullet.autonum_type = bu_auto.get('type', 'arabicPeriod')
            start_at = bu_auto.get('startAt')
            if start_at:
                try:
                    bullet.autonum_start = int(start_at)
                except (ValueError, TypeError):
                    bullet.autonum_start = 1

            bu_font = ppr.find('a:buFont', ns)
            if bu_font is not None:
                bullet.font_typeface = bu_font.get('typeface')

            bu_sz = ppr.find('a:buSzPct', ns)
            if bu_sz is not None:
                try:
                    bullet.font_size_pct = int(bu_sz.get('val', 100000))
                except (ValueError, TypeError):
                    pass

            bu_clr = ppr.find('a:buClr', ns)
            if bu_clr is not None:
                bullet.color = self._resolve_xml_color(bu_clr)

            return bullet

        # Check for image bullet (buBlip)
        bu_blip = ppr.find('a:buBlip', ns)
        if bu_blip is not None:
            bullet = BulletProperties(type='blip')

            # Resolve the embedded image via relationship
            blip = bu_blip.find('a:blip', ns)
            if blip is not None and shape is not None:
                embed_id = blip.get(f'{NS_R_CLARK}embed')
                if embed_id:
                    try:
                        rel = shape.part.rels[embed_id]
                        image_part = rel.target_part
                        bullet.blip_image_bytes = image_part.blob
                        content_type = image_part.content_type
                        image_type = content_type.split('/')[-1] if '/' in content_type else 'png'
                        if image_type == 'jpeg':
                            image_type = 'jpg'
                        elif image_type == 'x-emf':
                            image_type = 'emf'
                        elif image_type == 'x-wmf':
                            image_type = 'wmf'
                        bullet.blip_image_type = image_type
                    except (KeyError, AttributeError):
                        pass

            # Bullet styling (same pattern as buChar/buAutoNum)
            bu_font = ppr.find('a:buFont', ns)
            if bu_font is not None:
                bullet.font_typeface = bu_font.get('typeface')

            bu_sz = ppr.find('a:buSzPct', ns)
            if bu_sz is not None:
                try:
                    bullet.font_size_pct = int(bu_sz.get('val', 100000))
                except (ValueError, TypeError):
                    pass

            bu_clr = ppr.find('a:buClr', ns)
            if bu_clr is not None:
                bullet.color = self._resolve_xml_color(bu_clr)

            return bullet

        # No bullet element -- inherited from master/style
        return BulletProperties(type='inherited')

    def _resolve_bullet_inheritance(self, bullet: BulletProperties,
                                     para_level: int,
                                     is_title: bool = False,
                                     is_placeholder: bool = False) -> BulletProperties:
        """
        Resolve inherited bullet properties from master styles.

        Only placeholder text boxes inherit bullets from master.
        Non-placeholder text boxes default to no bullet.

        Args:
            bullet: Parsed BulletProperties (type='inherited')
            para_level: Paragraph indent level (0-8)
            is_title: Whether this is a title placeholder
            is_placeholder: Whether this shape is a placeholder

        Returns:
            Resolved BulletProperties
        """
        if bullet.type != 'inherited':
            return bullet

        # Non-placeholder text boxes don't inherit bullets from master
        if not is_placeholder:
            return BulletProperties(type='none')

        # Title placeholders typically don't have bullets
        if is_title:
            return BulletProperties(type='none')

        # Look up master body styles
        _, body_styles, _ = self._get_current_master_styles()
        # Master levels are 1-9, paragraph levels are 0-8
        master_key = para_level + 1
        master_style = body_styles.get(master_key) if body_styles else None

        if master_style is None or master_style.bullet_type is None:
            return BulletProperties(type='none')

        resolved = BulletProperties(type=master_style.bullet_type)
        if master_style.bullet_type == 'char':
            resolved.char = master_style.bullet_char or '\u2022'
        elif master_style.bullet_type == 'autonum':
            resolved.autonum_type = master_style.bullet_autonum_type or 'arabicPeriod'
        resolved.font_typeface = master_style.bullet_font

        return resolved
