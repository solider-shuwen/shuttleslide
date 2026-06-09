"""
Group shape parsing mixin for PPTXParser.

Handles group shapes with coordinate transformation from child-space to slide-space.
"""

from typing import Optional

from shuttleslide.pptx_to_html.models import GroupElement, ImageElement
from shuttleslide.pptx_to_html.utils.units import emu_to_px, px_to_emu


class GroupMixin:
    """Group shape parsing and coordinate transformation methods."""

    def _parse_group(self, shape, z_order: int, depth: int = 0) -> Optional[GroupElement]:
        """Parse a group shape by recursively parsing children with coordinate transformation.

        The group has two coordinate systems:
        - Group position (off/ext): where the group sits on the slide
        - Child coordinate system (chOff/chExt): the coordinate space children use

        Children's coordinates are transformed: slide_pos = group_off + (child_pos - chOff) * scale
        Then made relative to the group's top-left for CSS rendering inside a container.
        """
        if depth > 5:
            return None  # Safety: prevent infinite recursion

        try:
            ns = self._ns
            elem = shape._element

            # Extract group transform from XML
            xfrm = elem.find('.//p:grpSpPr/a:xfrm', ns)
            if xfrm is None:
                return None

            off = xfrm.find('a:off', ns)
            ext = xfrm.find('a:ext', ns)
            ch_off = xfrm.find('a:chOff', ns)
            ch_ext = xfrm.find('a:chExt', ns)

            if off is None or ext is None:
                return None

            group_off_x = int(off.get('x', '0'))
            group_off_y = int(off.get('y', '0'))
            group_ext_cx = int(ext.get('cx', '0'))
            group_ext_cy = int(ext.get('cy', '0'))

            # Child coordinate system - defaults to group extent if not specified
            co_x = int(ch_off.get('x', '0')) if ch_off is not None else 0
            co_y = int(ch_off.get('y', '0')) if ch_off is not None else 0
            ce_cx = int(ch_ext.get('cx', str(group_ext_cx))) if ch_ext is not None else group_ext_cx
            ce_cy = int(ch_ext.get('cy', str(group_ext_cy))) if ch_ext is not None else group_ext_cy

            # Compute scale factors
            scale_x = group_ext_cx / ce_cx if ce_cx else 1.0
            scale_y = group_ext_cy / ce_cy if ce_cy else 1.0

            # Group position in pixels
            group_left_px = emu_to_px(group_off_x)
            group_top_px = emu_to_px(group_off_y)
            group_width_px = emu_to_px(group_ext_cx)
            group_height_px = emu_to_px(group_ext_cy)

            # Parse children recursively
            children = []
            child_z = z_order

            if not hasattr(shape, 'shapes'):
                return None

            for child_shape in shape.shapes:
                child_elem = self._parse_shape(child_shape, child_z)
                if child_elem is None:
                    continue

                # _parse_shape may return a list (e.g. shape + text)
                if isinstance(child_elem, list):
                    for item in child_elem:
                        self._transform_child_to_group_relative(
                            item, group_off_x, group_off_y,
                            scale_x, scale_y, co_x, co_y,
                            group_left_px, group_top_px
                        )
                        children.append(item)
                        child_z += 1
                else:
                    self._transform_child_to_group_relative(
                        child_elem, group_off_x, group_off_y,
                        scale_x, scale_y, co_x, co_y,
                        group_left_px, group_top_px
                    )
                    children.append(child_elem)
                    child_z += 1

            return GroupElement(
                element_type="group",
                left=group_left_px,
                top=group_top_px,
                width=group_width_px,
                height=group_height_px,
                z_order=z_order,
                children=children,
                metadata={"group_shape": True, "child_count": len(children)}
            )

        except Exception:
            return None

    def _transform_child_to_group_relative(self, child, group_off_x, group_off_y,
                                            scale_x, scale_y, ch_off_x, ch_off_y,
                                            group_left_px, group_top_px):
        """Transform a child element's coordinates from group child-space to group-relative pixels.

        Steps:
        1. Convert child's pixel coords back to EMU
        2. Map from child-space to slide-space using group transform
        3. Convert back to pixels
        4. Make relative to group's top-left
        """
        # Convert child's px back to EMU
        child_x_emu = px_to_emu(child.left)
        child_y_emu = px_to_emu(child.top)
        child_w_emu = px_to_emu(child.width)
        child_h_emu = px_to_emu(child.height)

        # Map from child-space to slide-space
        slide_x = group_off_x + (child_x_emu - ch_off_x) * scale_x
        slide_y = group_off_y + (child_y_emu - ch_off_y) * scale_y
        slide_w = child_w_emu * scale_x
        slide_h = child_h_emu * scale_y

        # Convert to pixels and make relative to group top-left
        child.left = emu_to_px(slide_x) - group_left_px
        child.top = emu_to_px(slide_y) - group_top_px
        child.width = emu_to_px(slide_w)
        child.height = emu_to_px(slide_h)

        # Adjust image scale for group coordinate transform.
        # scale_w/h was calculated in child-space; the group scale factor
        # converts it to visual (slide-space) scale.
        if isinstance(child, ImageElement) and child.scale_w is not None:
            child.scale_w = child.scale_w * scale_x
            child.scale_h = child.scale_h * scale_y

        # For nested groups, propagate the parent group's scale to all
        # descendants.  Without this, nested-group children only carry the
        # inner group's scale and miss the outer group's scale, causing
        # dimensions to be off (e.g. 2x too large when outer scale = 0.5).
        if isinstance(child, GroupElement):
            self._propagate_group_scale(child, scale_x, scale_y)

    def _propagate_group_scale(self, group: GroupElement, scale_x: float, scale_y: float):
        """Recursively scale all descendants of a group by the parent's scale factors."""
        for sub in group.children:
            sub.left *= scale_x
            sub.top *= scale_y
            sub.width *= scale_x
            sub.height *= scale_y
            if isinstance(sub, GroupElement):
                self._propagate_group_scale(sub, scale_x, scale_y)
