"""
Post-process a saved PPTX file to embed icon fonts.

python-pptx has no font embedding API, so we reopen the PPTX as a ZIP
and inject the font binary + OpenXML references manually.
"""

from __future__ import annotations

import io
import logging
import zipfile
from typing import Dict

from lxml import etree

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# XML namespaces
# ---------------------------------------------------------------------------

_NS = {
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}

_FONT_REL_TYPE = (
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/font"
)
_FONT_CONTENT_TYPE = "application/x-font-ttf"


def _next_rid(rels_root: etree._Element) -> str:
    """Find the next available rId number in a relationships XML."""
    max_id = 0
    for rel in rels_root:
        rid = rel.get("Id", "")
        if rid.startswith("rId"):
            try:
                max_id = max(max_id, int(rid[3:]))
            except ValueError:
                pass
    return f"rId{max_id + 1}"


def _has_embedded_font(pres_root: etree._Element, font_name: str) -> bool:
    """Check if the font is already embedded in presentation.xml."""
    ns_p = _NS["p"]
    efl = pres_root.find(f"{{{ns_p}}}embeddedFontLst")
    if efl is None:
        return False
    for ef in efl.findall(f"{{{ns_p}}}embeddedFont"):
        font_el = ef.find(f"{{{ns_p}}}font")
        if font_el is not None and font_el.get("typeface") == font_name:
            return True
    return False


def _next_font_index(z: zipfile.ZipFile) -> int:
    """Find the next available font file index in the ZIP."""
    existing = set()
    for name in z.namelist():
        if name.startswith("ppt/fonts/font") and name.endswith(".ttf"):
            try:
                idx = int(name.split("font")[1].split(".")[0])
                existing.add(idx)
            except ValueError:
                pass
    return max(existing, default=0) + 1


def embed_fonts(pptx_path: str, fonts: Dict[str, bytes]) -> None:
    """Embed multiple fonts into a saved PPTX file.

    Args:
        pptx_path: Path to the already-saved PPTX file (will be overwritten).
        fonts: Mapping of font_name -> font_bytes to embed.
    """
    if not fonts:
        return

    # Read the entire PPTX into memory
    with open(pptx_path, "rb") as f:
        original_bytes = f.read()

    buf = io.BytesIO(original_bytes)
    with zipfile.ZipFile(buf, "r") as zin:
        # Read all existing entries
        entries: dict[str, bytes] = {}
        for item in zin.infolist():
            entries[item.filename] = zin.read(item.filename)

    # --- Modify XML entries ---

    # 1. Content_Types.xml — add .ttf default if missing
    ct_bytes = entries.get("[Content_Types].xml", b"")
    ct_root = etree.fromstring(ct_bytes)
    ns_ct = _NS["ct"]
    has_ttf = any(
        el.get("Extension") == "ttf"
        for el in ct_root.findall(f"{{{ns_ct}}}Default")
    )
    if not has_ttf:
        ttf_default = etree.SubElement(ct_root, f"{{{ns_ct}}}Default")
        ttf_default.set("Extension", "ttf")
        ttf_default.set("ContentType", _FONT_CONTENT_TYPE)
    entries["[Content_Types].xml"] = etree.tostring(
        ct_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # 2. presentation.xml.rels — add font relationships
    rels_path = "ppt/_rels/presentation.xml.rels"
    rels_bytes = entries.get(rels_path)
    if rels_bytes is None:
        raise ValueError(f"Missing {rels_path} in PPTX")
    rels_root = etree.fromstring(rels_bytes)

    # 3. presentation.xml — add embeddedFontLst
    pres_path = "ppt/presentation.xml"
    pres_bytes = entries.get(pres_path)
    if pres_bytes is None:
        raise ValueError(f"Missing {pres_path} in PPTX")
    pres_root = etree.fromstring(pres_bytes)
    ns_p = _NS["p"]

    # Find or create embeddedFontLst (after notesSz, before defaultTextStyle per ECMA-376)
    efl = pres_root.find(f"{{{ns_p}}}embeddedFontLst")
    if efl is None:
        efl = etree.Element(f"{{{ns_p}}}embeddedFontLst")
        # ECMA-376 order: ...notesSz, embeddedFontLst, defaultTextStyle...
        # Insert before defaultTextStyle if it exists, otherwise append
        default_ts = pres_root.find(f"{{{ns_p}}}defaultTextStyle")
        if default_ts is not None:
            default_ts.addprevious(efl)
        else:
            # Append after notesSz
            notes_sz = pres_root.find(f"{{{ns_p}}}notesSz")
            if notes_sz is not None:
                notes_sz.addnext(efl)
            else:
                pres_root.append(efl)

    # Determine font file index
    buf_for_index = io.BytesIO(original_bytes)
    with zipfile.ZipFile(buf_for_index, "r") as z_tmp:
        font_idx = _next_font_index(z_tmp)

    for font_name, font_bytes in fonts.items():
        # Skip if already embedded
        if _has_embedded_font(pres_root, font_name):
            logger.debug("Font '%s' already embedded, skipping", font_name)
            continue

        # Add relationship
        rid = _next_rid(rels_root)
        rel = etree.SubElement(rels_root, f"{{{_NS['rel']}}}Relationship")
        rel.set("Id", rid)
        rel.set("Type", _FONT_REL_TYPE)
        rel.set("Target", f"fonts/font{font_idx}.ttf")

        # Add embeddedFont entry
        ef = etree.SubElement(efl, f"{{{ns_p}}}embeddedFont")
        font_el = etree.SubElement(ef, f"{{{ns_p}}}font")
        font_el.set("typeface", font_name)
        font_el.set("charset", "0")
        regular = etree.SubElement(ef, f"{{{ns_p}}}regular")
        regular.set(f"{{{_NS['r']}}}id", rid)

        # Add font binary to entries
        entries[f"ppt/fonts/font{font_idx}.ttf"] = font_bytes
        font_idx += 1

        logger.info("Embedded font '%s' (rid=%s, %d bytes)", font_name, rid, len(font_bytes))

    # Write back modified XML
    entries[rels_path] = etree.tostring(
        rels_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    entries[pres_path] = etree.tostring(
        pres_root, xml_declaration=True, encoding="UTF-8", standalone=True
    )

    # --- Write new ZIP ---
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in entries.items():
            zout.writestr(name, data)

    with open(pptx_path, "wb") as f:
        f.write(out_buf.getvalue())

    logger.info("Saved PPTX with %d embedded font(s)", len(fonts))
