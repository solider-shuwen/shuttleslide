"""
Image parsing mixin for PPTXParser.

Handles image extraction, cropping (srcRect), and color replacement (clrChange).
"""

from typing import Optional, Dict, Any

from shuttleslide.pptx_to_html.models import ImageElement
from shuttleslide.pptx_to_html.utils.units import px_to_emu, EMU_PER_INCH, angle_to_degrees
from shuttleslide.pptx_to_html.utils.namespaces import NAMESPACES, NS_R_CLARK


class ImageMixin:
    """Image element parsing methods."""

    def _parse_image(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> Optional[ImageElement]:
        """Parse an image shape, including srcRect cropping and clrChange effects."""
        try:
            # Get image bytes — try python-pptx high-level API first,
            # fall back to blipFill XML relationship extraction
            try:
                image = shape.image
                image_bytes = image.blob
                image_type = image.ext
            except (ValueError, AttributeError):
                # Fallback 1: standard blipFill r:embed
                blip_data = self._extract_blip_fill(shape)
                if blip_data is not None:
                    image_bytes = blip_data['image_bytes']
                    image_type = blip_data['image_type']
                else:
                    # Fallback 2: OLE/SVG — blip has no r:embed, but image may be
                    # in asvg:svgBlip extension or via OLE object image relationship
                    blip_data = self._extract_ole_image(shape)
                    if blip_data is None:
                        return None
                    image_bytes = blip_data['image_bytes']
                    image_type = blip_data['image_type']

            # Get alt text
            alt_text = "" if not hasattr(shape, "alt_text") else shape.alt_text

            src_rect = None
            clr_change = None
            scene3d_camera = None
            fill_mode = "stretch"
            rotation = None

            # Extract special properties from XML
            ns = self._ns
            if hasattr(shape, '_element'):
                elem = shape._element

                # Extract srcRect (image cropping)
                src_rect_elem = elem.find('.//a:srcRect', ns)
                if src_rect_elem is not None:
                    src_rect = {}
                    for attr in ['l', 't', 'r', 'b']:
                        val = src_rect_elem.get(attr)
                        if val:
                            src_rect[attr] = int(val)
                    if not src_rect:
                        src_rect = None

                # Extract clrChange (color replacement, typically white→transparent)
                clr_change_elem = elem.find('.//a:blip/a:clrChange', ns)
                if clr_change_elem is not None:
                    clr_from = clr_change_elem.find('a:clrFrom/a:srgbClr', ns)
                    clr_to = clr_change_elem.find('a:clrTo/a:srgbClr', ns)
                    if clr_from is not None and clr_to is not None:
                        from_color = '#' + clr_from.get('val', '')
                        # Check for alpha=0 in clrTo (means transparent)
                        alpha_elems = clr_to.findall('a:alpha', ns)
                        is_transparent = any(
                            int(a.get('val', '100000')) == 0 for a in alpha_elems
                        )
                        if is_transparent:
                            clr_change = {'from': from_color, 'to': 'transparent'}

                # Extract scene3d camera preset
                scene3d = elem.find('.//a:scene3d/a:camera', ns)
                if scene3d is not None:
                    prst = scene3d.get('prst')
                    if prst:
                        scene3d_camera = prst

                # Extract rotation from <a:xfrm>
                xfrm = elem.find('.//a:xfrm', ns)
                if xfrm is not None:
                    rot_val = xfrm.get('rot')
                    if rot_val:
                        rotation = angle_to_degrees(float(rot_val))

                # Extract fill mode from <a:blipFill>
                blipFill_elem = elem.find('p:blipFill', ns)
                if blipFill_elem is not None:
                    if blipFill_elem.find('a:stretch', ns) is not None:
                        fill_mode = "stretch"
                    elif blipFill_elem.find('a:tile', ns) is not None:
                        fill_mode = "tile"
                    else:
                        fill_mode = "none"

            # Calculate PPT image scale for scene3d images.
            # PPT "Scale" = shape_EMU / (cropped_img_px * 914400 / img_dpi)
            # This captures user stretching applied before the 3D transform.
            scale_w = None
            scale_h = None
            if scene3d_camera:
                try:
                    from PIL import Image as PILImage
                    import io as _io
                    pil_img = PILImage.open(_io.BytesIO(image_bytes))
                    img_w, img_h = pil_img.size
                    dpi = pil_img.info.get('dpi', (96, 96))
                    dpi_x = dpi[0] if dpi and dpi[0] > 0 else 96
                    dpi_y = dpi[1] if dpi and dpi[1] > 0 else 96
                    pil_img.close()

                    # srcRect crop factors (1/100000ths)
                    sl = (src_rect or {}).get('l', 0)
                    sr = (src_rect or {}).get('r', 0)
                    st = (src_rect or {}).get('t', 0)
                    sb = (src_rect or {}).get('b', 0)
                    cropped_w_px = img_w * (100000 - sl - sr) / 100000
                    cropped_h_px = img_h * (100000 - st - sb) / 100000

                    # Cropped image size in EMU (using image DPI)
                    cropped_w_emu = cropped_w_px * EMU_PER_INCH / dpi_x
                    cropped_h_emu = cropped_h_px * EMU_PER_INCH / dpi_y

                    # Shape dimensions in EMU (width/height are in px = EMU/9525)
                    shape_w_emu = px_to_emu(width)
                    shape_h_emu = px_to_emu(height)

                    if cropped_w_emu > 0 and cropped_h_emu > 0:
                        scale_w = round(shape_w_emu / cropped_w_emu, 4)
                        scale_h = round(shape_h_emu / cropped_h_emu, 4)
                except Exception:
                    pass

            # SVG images cannot be processed by Pillow — skip raster operations
            # and preserve the original SVG bytes.
            is_svg = image_type in ('svg', 'svg+xml')

            # Apply srcRect cropping
            if src_rect:
                if is_svg:
                    image_bytes = self._crop_svg_src_rect(image_bytes, src_rect)
                else:
                    image_bytes = self._crop_image_src_rect(image_bytes, src_rect)

            # Apply clrChange with Pillow (skip for SVG)
            if clr_change and clr_change.get('to') == 'transparent' and not is_svg:
                image_bytes = self._apply_color_change(
                    image_bytes, clr_change['from'], tolerance=30
                )

            # Ensure image_type is PNG after Pillow processing (transparency requires PNG)
            # But keep SVG as SVG — the browser handles it natively.
            if (clr_change or src_rect) and not is_svg:
                image_type = 'png'

            element = ImageElement(
                element_type="image",
                left=left,
                top=top,
                width=width,
                height=height,
                z_order=z_order,
                image_bytes=image_bytes,
                image_type=image_type,
                alt_text=alt_text,
                src_rect=src_rect,
                clr_change=clr_change,
                fill_mode=fill_mode,
                scale_w=scale_w,
                scale_h=scale_h,
                rotation=rotation,
            )

            # Store scene3d in metadata for CSS rendering
            if scene3d_camera:
                element.metadata = element.metadata or {}
                element.metadata['scene3d_camera'] = scene3d_camera

            return element
        except (ValueError, AttributeError):
            # No embedded image or other error
            return None

    def _crop_image_src_rect(self, image_bytes: bytes, src_rect: dict) -> bytes:
        """Crop image according to OpenXML srcRect percentages.

        srcRect values are in 1/100000ths of the image dimension.
        l=10000 means crop 10% from left, r=20000 means crop 20% from right.
        """
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(image_bytes))
            w, h = img.size

            l = int(src_rect.get('l', 0) / 100000 * w)
            t = int(src_rect.get('t', 0) / 100000 * h)
            r = int(src_rect.get('r', 0) / 100000 * w)
            b = int(src_rect.get('b', 0) / 100000 * h)

            # Clamp to image bounds
            l = max(0, min(l, w))
            t = max(0, min(t, h))
            r = max(0, min(r, w - l))
            b = max(0, min(b, h - t))

            cropped = img.crop((l, t, w - r, h - b))
            output = io.BytesIO()
            fmt = 'PNG' if img.mode == 'RGBA' else (img.format or 'PNG')
            cropped.save(output, format=fmt)
            return output.getvalue()
        except Exception:
            return image_bytes

    def _crop_svg_src_rect(self, image_bytes: bytes, src_rect: dict) -> bytes:
        """Crop SVG image by adjusting its viewBox to match OpenXML srcRect.

        srcRect values are in 1/100000ths of the image dimension.
        Instead of cropping pixels (impossible for vector SVG), we shift and
        shrink the viewBox so only the desired region is visible.
        """
        try:
            import re

            svg_str = image_bytes.decode('utf-8', errors='replace')

            # Parse existing viewBox or infer from width/height
            vb_match = re.search(r'viewBox=["\']([^"\']*)["\']', svg_str)
            if vb_match:
                parts = vb_match.group(1).split()
                vb_x, vb_y, vb_w, vb_h = float(parts[0]), float(parts[1]), float(parts[2]), float(parts[3])
            else:
                # Infer from width/height attributes
                w_match = re.search(r'<svg[^>]*\swidth=["\']([^"\']*)["\']', svg_str)
                h_match = re.search(r'<svg[^>]*\sheight=["\']([^"\']*)["\']', svg_str)
                if not w_match or not h_match:
                    return image_bytes
                vb_x, vb_y = 0.0, 0.0
                vb_w = float(w_match.group(1))
                vb_h = float(h_match.group(1))

            # Calculate crop percentages (srcRect values are in 1/100000ths)
            l_pct = src_rect.get('l', 0) / 100000
            t_pct = src_rect.get('t', 0) / 100000
            r_pct = src_rect.get('r', 0) / 100000
            b_pct = src_rect.get('b', 0) / 100000

            # New viewBox: shift origin and shrink to cropped region
            new_x = vb_x + vb_w * l_pct
            new_y = vb_y + vb_h * t_pct
            new_w = vb_w * (1 - l_pct - r_pct)
            new_h = vb_h * (1 - t_pct - b_pct)

            if new_w <= 0 or new_h <= 0:
                return image_bytes

            new_viewbox = f"{new_x:.4f} {new_y:.4f} {new_w:.4f} {new_h:.4f}"

            # Replace viewBox
            if vb_match:
                svg_str = svg_str[:vb_match.start(1)] + new_viewbox + svg_str[vb_match.end(1):]
            else:
                # Add viewBox to <svg> tag
                svg_str = svg_str.replace('<svg', f'<svg viewBox="{new_viewbox}"', 1)

            # Remove fixed width/height so SVG scales to container
            svg_str = re.sub(r'\swidth=["\'][^"\']*["\']', '', svg_str, count=1)
            svg_str = re.sub(r'\sheight=["\'][^"\']*["\']', '', svg_str, count=1)
            svg_str = svg_str.replace('<svg', '<svg width="100%" height="100%"', 1)

            return svg_str.encode('utf-8')
        except Exception:
            return image_bytes

    def _apply_color_change(self, image_bytes: bytes, from_color: str,
                            tolerance: int = 30) -> bytes:
        """Apply color replacement to image: make near-'from_color' pixels transparent."""
        try:
            from PIL import Image
            import io

            img = Image.open(io.BytesIO(image_bytes)).convert('RGBA')
            pixels = img.load()

            # Parse target color
            r_target = int(from_color[1:3], 16)
            g_target = int(from_color[3:5], 16)
            b_target = int(from_color[5:7], 16)

            w, h = img.size
            for y in range(h):
                for x in range(w):
                    r, g, b, a = pixels[x, y]
                    if (abs(r - r_target) <= tolerance and
                            abs(g - g_target) <= tolerance and
                            abs(b - b_target) <= tolerance):
                        pixels[x, y] = (r, g, b, 0)  # Make transparent

            output = io.BytesIO()
            img.save(output, format='PNG')
            return output.getvalue()
        except Exception:
            return image_bytes

    def _extract_ole_image(self, shape) -> Optional[Dict[str, Any]]:
        """Extract image from OLE-embedded picture shapes.

        Some PPTX shapes store their image as an OLE object. The <a:blip>
        has no r:embed attribute, but the image is available via:
        1. asvg:svgBlip r:embed in blip extension list (SVG version)
        2. An image relationship from the OLE object (EMF version)

        Returns:
            Dictionary with {image_bytes, image_type} or None
        """
        if not hasattr(shape, '_element'):
            return None

        try:
            ns = {'a': NAMESPACES['a'], 'r': NAMESPACES['r'],
                  'asvg': 'http://schemas.microsoft.com/office/drawing/2016/SVG/main',
                  'p': NAMESPACES['p']}
            elem = shape._element

            # Try 1: look for asvg:svgBlip r:embed in blip extension list
            svg_blip = elem.find('.//a:blip/a:extLst/a:ext/asvg:svgBlip', ns)
            if svg_blip is not None:
                embed_id = svg_blip.get(f'{NS_R_CLARK}embed')
                if embed_id:
                    try:
                        rel = shape.part.rels[embed_id]
                        target = rel.target_part
                        return {
                            'image_bytes': target.blob,
                            'image_type': 'svg+xml',
                        }
                    except (KeyError, AttributeError):
                        pass

            # Try 2: look for any image relationship (EMF fallback)
            image_rel_type = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image'
            for rel_id, rel in shape.part.rels.items():
                if rel.reltype == image_rel_type:
                    try:
                        target = rel.target_part
                        content_type = target.content_type
                        image_type = content_type.split('/')[-1] if '/' in content_type else 'png'
                        if image_type == 'x-emf':
                            image_type = 'emf'
                        elif image_type == 'x-wmf':
                            image_type = 'wmf'
                        return {
                            'image_bytes': target.blob,
                            'image_type': image_type,
                        }
                    except (KeyError, AttributeError):
                        continue

        except Exception:
            pass

        return None
