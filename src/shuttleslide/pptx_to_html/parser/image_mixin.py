"""
Image parsing mixin for PPTXParser.

Handles image extraction, cropping (srcRect), and color replacement (clrChange).
"""

from typing import Optional

from shuttleslide.pptx_to_html.models import ImageElement
from shuttleslide.pptx_to_html.utils.units import px_to_emu, EMU_PER_INCH


class ImageMixin:
    """Image element parsing methods."""

    def _parse_image(
        self, shape, left: float, top: float, width: float, height: float, z_order: int
    ) -> Optional[ImageElement]:
        """Parse an image shape, including srcRect cropping and clrChange effects."""
        try:
            # Get image bytes
            image = shape.image
            image_bytes = image.blob
            image_type = image.ext

            # Get alt text
            alt_text = "" if not hasattr(shape, "alt_text") else shape.alt_text

            src_rect = None
            clr_change = None
            scene3d_camera = None
            fill_mode = "stretch"

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
                    pil_img = PILImage.open(_io.BytesIO(image.blob))
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

            # Apply srcRect cropping with Pillow
            if src_rect:
                image_bytes = self._crop_image_src_rect(image_bytes, src_rect)
                # Update dimensions after cropping - the shape's width/height
                # in PPTX already reflects the cropped area, so no change needed

            # Apply clrChange with Pillow
            if clr_change and clr_change.get('to') == 'transparent':
                image_bytes = self._apply_color_change(
                    image_bytes, clr_change['from'], tolerance=30
                )

            # Ensure image_type is PNG after Pillow processing (transparency requires PNG)
            if clr_change or src_rect:
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
