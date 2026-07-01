"""SVG Generator tool — single atomic tool that stores one LLM-authored SVG.

The image_acquirer node (Stage 2.5) makes one LLM call per image spec on
the svg path. Before each call it sets ``state.current_svg_spec`` so this
tool knows which (slide_idx, slot_id) the incoming SVG belongs to.

The SVG markup is written to ``{output_dir}/svgs/slide_{N}_{slot}.svg``
and the prompt-facing surface becomes a short placeholder reference
``<img class="shuttleslide-svg-placeholder" src="svgs/...">``. The full
SVG markup is inlined back into HTML only inside ``html_to_pptx`` right
before Playwright rendering — see ``_inline_svg_placeholders`` in
``html_to_pptx/html_utils.py``. This keeps SVG bytes out of the
slide-builder LLM context and out of the 12000-char free-form HTML cap.

Validation is intentionally strict — invalid SVG fails the tool, which
triggers the existing retry loop in run_image_acquirer._acquire_svg.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional
from xml.etree import ElementTree as ET

from shuttleslide.agent.tools.outline_tools import ALLOWED_ASPECT_RATIOS
from shuttleslide.agent.tools.registry import ToolResult, tool
from shuttleslide.agent.prompts import _ASPECT_VIEWBOX

# SVG elements the vendored converter supports. Anything else is rejected
# up-front so the LLM retries instead of producing shapes that silently
# drop in the PPTX output.
#
# The set is DERIVED from the vendored converter's own tag registries so
# the validator cannot drift when new shapes land downstream. There are
# three sources of truth, all in
# src/shuttleslide/_vendored/svg_to_pptx/drawingml_converter.py:
#
#   1. _CONVERTERS — tags with a registered handler (rect/circle/path/...).
#      Auto-derived; we only subtract a small forbidden subset (see below).
#   2. _NON_VISUAL_TAGS — skipped by _collect_unsupported_visuals
#      (defs/title/desc/metadata/style). Auto-derived.
#   3. _SUPPORTED_VISUAL_CHILD_TAGS — child tags of visual elements that
#      are acceptable inside a parent shape (just 'tspan'). Auto-derived.
#
# Referenced-defs tags (filter/pattern/clipPath/gradients/markers/use/...)
# cannot be auto-derived because they don't have registered converters —
# they're looked up ad-hoc by id (e.g. fill="url(#...)"). They live in
# _REFERENCED_DEFS_TAGS below and need manual maintenance. Drift on this
# set is much rarer than drift on shapes — new referenced-defs features
# land maybe once a quarter; new shapes used to land silently and break
# the validator within hours.
from shuttleslide._vendored.svg_to_pptx.drawingml_converter import (
    _CONVERTERS as _VENDORED_CONVERTERS,
    _NON_VISUAL_TAGS as _VENDORED_NON_VISUAL_TAGS,
    _SUPPORTED_VISUAL_CHILD_TAGS as _VENDORED_SUPPORTED_CHILDREN,
)

# Tags that have a registered converter handler but should still be
# rejected by this validator:
#   - 'image': raster <image> is forbidden via the substring guard below
#     (LLM-authored SVGs must use vector shapes only).
#   - 'svg': nested <svg> root inside an LLM-authored SVG is almost
#     always a mistake (the outer <svg> is the slide SVG). The nested-svg
#     converter exists for general SVG files, not for slide slot SVGs.
_CONVERTER_TAGS_FORBIDDEN = frozenset({"image", "svg"})

# Tags that live inside <defs> and are referenced by attribute
# (fill="url(#...)", stroke="url(#...)", filter="url(#...)",
# clip-path="url(#...)", marker-start=..., <use href="#...">).
#
# The vendored converter's _collect_unsupported_visuals skips <defs>
# subtrees via its in_defs early-return, so any tag inside <defs> is
# accepted by the converter. We still need to enumerate the ones we want
# the LLM to be able to emit because _collect_unsupported_tags (this
# file) walks the entire tree including <defs> children.
#
# When adding a tag here, also extend the prompt (prompts.py) so the LLM
# knows it's available. The drift test
# (test_allowlist_covers_all_vendored_shape_converters) catches shape
# drift automatically; referenced-defs drift is caught only by the
# pipeline failing in production.
_REFERENCED_DEFS_TAGS = frozenset(
    {
        # Gradients — drawingml_styles.build_gradient_fill
        "linearGradient",
        "radialGradient",
        "stop",
        # Markers (arrow-heads) — drawingml_styles marker code
        "marker",
        # <use> — use_expander.expand_use_data_icons handles the
        # data-icon attribute case. General <use href="#..."> is NOT
        # resolved; kept allowed because the data-icon path is the
        # standard icon system used across the pipeline.
        "use",
        # Filter container + primitives parsed by
        # drawingml_styles._parse_filter_params. _parse_filter_params
        # extracts shadow params from feDropShadow / feGaussianBlur /
        # feOffset / feFlood / feFuncA; everything else it iterates over
        # is silently skipped.
        #
        # Truly unsupported primitives stay excluded — they describe
        # effects the converter has no DrawingML equivalent for, so
        # emitting them WOULD be misleading: feTurbulence / feDisplacementMap
        # / feComposite / feBlend / feColorMatrix / feImage / lighting
        # primitives / feMorphology.
        "filter",
        "feDropShadow",
        "feGaussianBlur",
        "feOffset",
        "feFlood",
        "feFuncA",
        # Pattern fills — drawingml_styles.build_pattern_fill → <a:pattFill>
        "pattern",
        # Clip paths — drawingml_elements.resolve_clip_path_geometry
        "clipPath",
    }
)

# Container / compositor tags that the vendored converter does NOT
# explicitly dispatch on (so they don't appear in its source as string
# literals), but safely passes through via .iter() walks. Allowed in the
# validator because they're structural parts of textbook SVG1.1 filter
# chains — excluding them forces the LLM off the most natural SVG
# drop-shadow pattern. Drift detection in test_svg_externalization.py
# skips this set (the "appears in vendored source" heuristic doesn't
# apply to implicitly-handled tags).
#
#   - feComponentTransfer is the standard container for feFuncA
#     (LLM writes <feComponentTransfer><feFuncA .../></feComponentTransfer>;
#     the converter's .iter() walk still finds the feFuncA child).
#   - feMerge / feMergeNode are the canonical "compose shadow behind
#     SourceGraphic" step of an SVG1.1 drop-shadow chain:
#       <feGaussianBlur in="SourceAlpha" .../>
#       <feOffset .../>
#       <feComponentTransfer><feFuncA .../></feComponentTransfer>
#       <feMerge><feMergeNode/><feMergeNode in="SourceGraphic"/></feMerge>
#     For this pattern, dropping feMerge produces an outerShdw that is
#     visually equivalent — feMerge's only job here is "stack shadow
#     layer behind original", which is exactly what DrawingML
#     <a:outerShdw> renders by default.
_SILENTLY_PASSTHROUGH_DEFS_TAGS = frozenset(
    {
        "feComponentTransfer",
        "feMerge",
        "feMergeNode",
    }
)

_SUPPORTED_SVG_TAGS = (
    (frozenset(_VENDORED_CONVERTERS.keys()) - _CONVERTER_TAGS_FORBIDDEN)
    | _VENDORED_NON_VISUAL_TAGS
    | _VENDORED_SUPPORTED_CHILDREN
    | _REFERENCED_DEFS_TAGS
    | _SILENTLY_PASSTHROUGH_DEFS_TAGS
)

# Cap SVG markup size as a sanity guard against runaway LLM output. This
# no longer gates the slide-builder HTML budget — SVG bytes live on disk
# and are inlined only inside html_to_pptx's Playwright path. The limit
# exists purely to fail fast on degenerate output (e.g. the LLM entering
# a repeat-loop that spams thousands of path elements).
_MAX_SVG_LEN = 50000

# Subdirectory under output_dir where SVG files are persisted. Matches
# the naming convention of raster images (images/slide_N_slot.jpg), just
# under a parallel "svgs/" namespace.
_SVGS_SUBDIR = "svgs"

# Tolerance for "full-bleed" detection. 1% absorbs float drift and
# LLM-ish writes like width="1279" on a viewBox of 1280.
_FULL_BLEED_TOL = 0.01

# Root <svg> must declare the SVG namespace or ET will mangle tags on round-trip.
_SVG_NS = "http://www.w3.org/2000/svg"

# Match the viewBox against the aspect_ratio declared in the spec.
_VIEWBOX_RE = re.compile(
    r"""viewbox\s*=\s*["']\s*([-0-9.eE+]+)\s+([-0-9.eE+]+)\s+([-0-9.eE+]+)\s+([-0-9.eE+]+)\s*["']""",
    re.IGNORECASE,
)


def validate_svg_for_slot(svg: str, spec: Dict[str, Any]) -> Optional[str]:
    """Return an error string if ``svg`` fails validation for ``spec``, else None.

    Pure function — no state, no ctx, no I/O. The validation logic is
    shared between the ``set_svg`` tool (svg_tools.py) and the review
    SvgEditor (PR3). Keeping it pure lets the editor validate user-pasted
    or LLM-revised SVGs without needing the full pipeline context.

    ``spec`` must contain:
      - ``slot_id``: str — the slot this SVG belongs to (validated against
        root ``<svg id>`` and ``data-slot`` attributes).
      - ``aspect_ratio``: str — one of ``_ASPECT_VIEWBOX`` keys. Empty
        string skips the viewBox match check (legacy payloads loaded
        from disk before aspect_ratio was persisted).
      - ``description``: str (optional) — used in the ``<desc>`` error
        hint when the SVG is missing one.

    Returns ``None`` on success. The caller can trust that the SVG is
    well-formed XML, has the right root id/data-slot/viewBox, contains
    only supported tags, and has a ``<desc>`` as its first child.
    """
    if not isinstance(svg, str) or not svg.strip():
        return "svg must be a non-empty string"
    if len(svg) > _MAX_SVG_LEN:
        return (
            f"svg too large ({len(svg)} chars > {_MAX_SVG_LEN}); simplify the drawing"
        )

    # XML well-formedness.
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        return f"svg is not well-formed XML: {exc}"

    # Root tag must be <svg>.
    tag = root.tag
    if tag.startswith("{"):
        tag = tag.split("}", 1)[1]
    if tag != "svg":
        return f"root element must be <svg> (got <{tag}>)"

    # Required attributes on root — id and data-slot must equal slot_id.
    slot_id = spec["slot_id"]
    root_id = root.get("id")
    if root_id != slot_id:
        return f"root <svg id> must be {slot_id!r} (got {root_id!r})"
    root_data_slot = root.get("data-slot")
    if root_data_slot != slot_id:
        return f"root <svg data-slot> must be {slot_id!r} (got {root_data_slot!r})"

    # viewBox must match aspect_ratio (skipped when aspect_ratio is empty —
    # legacy payloads loaded from disk before the field was persisted).
    aspect = spec.get("aspect_ratio", "")
    if aspect:
        if aspect not in _ASPECT_VIEWBOX:
            return (
                f"unknown aspect_ratio {aspect!r}; expected one of "
                f"{sorted(_ASPECT_VIEWBOX.keys())}"
            )
        expected_viewbox = _ASPECT_VIEWBOX[aspect]
        viewbox_attr = root.get("viewBox") or root.get("viewbox")
        if not viewbox_attr:
            return (
                f"root <svg> must declare viewBox (expected {expected_viewbox!r} "
                f"for aspect_ratio {aspect!r})"
            )
        normalised = " ".join(viewbox_attr.split())
        if normalised != expected_viewbox:
            return (
                f"root <svg viewBox> must be {expected_viewbox!r} for "
                f"aspect_ratio {aspect!r} (got {normalised!r})"
            )
    else:
        expected_viewbox = ""  # legacy path — viewBox check skipped

    # Reject full-bleed background rects (would mask the slide background).
    if expected_viewbox:
        vb_parts = expected_viewbox.split()
        vb_w, vb_h = float(vb_parts[2]), float(vb_parts[3])
        full_bleed_fill = _has_full_bleed_bg_rect(root, vb_w, vb_h)
        if full_bleed_fill is not None:
            return (
                f"svg contains a full-bleed background rect (fill="
                f"{full_bleed_fill!r}) matching the viewBox dimensions "
                f"({vb_w:g}x{vb_h:g}). The SVG is composited on top of the "
                f"slide background and would mask it. Make the SVG transparent; "
                f"if the image needs a LOCAL panel background, size the rect "
                f"to the panel, not the viewBox."
            )

    # Walk the tree and reject unsupported tags (skip the root <svg>).
    unsupported = _collect_unsupported_tags(root)
    if unsupported:
        preview = ", ".join(sorted(unsupported)[:8])
        suffix = "" if len(unsupported) <= 8 else f", +{len(unsupported) - 8} more"
        return (
            f"unsupported SVG element(s): {preview}{suffix}. "
            f"Allowed: {', '.join(sorted(_SUPPORTED_SVG_TAGS))}. "
            f"filter, pattern, clipPath, linearGradient, radialGradient, "
            f"marker live inside <defs> and are referenced via url(#...)."
        )

    # Reject forbidden substrings (XSS / animation / raster fallback).
    forbidden_substrings = (
        "<image",
        "<foreignObject",
        "<script",
        "<style",
        "<animate",
        "<animateTransform",
        "<animateMotion",
        "<set ",
    )
    lowered = svg.lower()
    for needle in forbidden_substrings:
        if needle.lower() in lowered:
            return (
                f"svg contains forbidden element near {needle!r}; only static "
                f"vector shapes are allowed"
            )

    # <desc> as first child — required for self-describing SVGs.
    desc_err = _validate_desc_tag(root, spec.get("description", ""))
    if desc_err is not None:
        return desc_err

    return None


@tool(
    name="set_svg",
    description=(
        "Submit ONE inline SVG for the image currently being drawn. Call "
        "ONCE per spec. The SVG markup must be self-contained (no external "
        "fonts, images, or CSS), must declare viewBox matching the spec's "
        "aspect_ratio, and must use only supported SVG elements."
    ),
    params={
        "type": "object",
        "properties": {
            "svg": {
                "type": "string",
                "description": "Complete <svg>...</svg> markup.",
            },
        },
        "required": ["svg"],
    },
    groups=["svg_builder"],
)
async def set_svg(params: Dict[str, Any], ctx: Dict[str, Any]) -> ToolResult:
    state = ctx.get("state")
    if state is None:
        return ToolResult.failure("no state in context")

    spec = state.current_svg_spec
    if spec is None:
        return ToolResult.failure(
            "set_svg called outside the image_acquirer stage "
            "(state.current_svg_spec is None)"
        )

    svg = params.get("svg")
    # Delegate all structural validation to the shared pure function so
    # the review SvgEditor (PR3) enforces the exact same checks.
    err = validate_svg_for_slot(svg, spec)
    if err is not None:
        return ToolResult.failure(err)

    slide_idx = spec["slide_idx"]
    slot_id = spec["slot_id"]

    # Persist the SVG to disk so its bytes stay out of the slide-builder
    # LLM context (saves ~1200 tokens/image) and out of the 12000-char
    # free-form HTML cap. The slide-builder prompt sees only a short
    # <img class="shuttleslide-svg-placeholder" src="svgs/..."> reference.
    output_dir = ctx.get("output_dir")
    if output_dir is None:
        # Match the web path's contract (acquire.py:_acquire_web raises
        # if output_dir is None). Agent pipeline always sets output_dir —
        # without it the final HTML cannot be written either.
        return ToolResult.failure(
            "set_svg requires ctx['output_dir'] to persist the SVG; "
            "configure AgentConfig.output_dir"
        )
    try:
        rel_path = _persist_svg_markup(
            svg,
            slide_idx=slide_idx,
            slot_id=slot_id,
            output_dir=Path(output_dir),
        )
    except OSError as exc:
        return ToolResult.failure(f"failed to write SVG file: {exc}")

    # Store as a typed payload (see AgentState.slide_images docstring).
    # svg_file is the new production shape — parallel to image_file.
    # The "data" field is retained so html_to_pptx (or any other inliner)
    # can inline without re-reading the file, but it never enters an LLM
    # prompt: _format_images_block renders only the path + description.
    # ``aspect_ratio`` is persisted so PR3's SvgEditor can re-validate
    # user edits without reverse-looking-up the viewBox. Legacy state
    # files (pre-PR3) lack this field; load_state defaults it to "".
    # ``width`` / ``height`` come from the viewBox (already validated
    # above against ``_ASPECT_VIEWBOX[aspect]`` for non-empty aspect
    # ratios) so raster and vector payloads expose the same dimension
    # fields. Fallback ``(0, 0)`` only fires for malformed SVG markup
    # that slipped past validation — the dimensions are best-effort.
    svg_w, svg_h = _parse_svg_dimensions(svg)
    state.slide_images.setdefault(slide_idx, {})[slot_id] = {
        "type": "svg_file",
        "path": rel_path,
        "data": svg,
        "description": spec.get("description", ""),
        "image_type": spec.get("image_type", "illustration"),
        "aspect_ratio": spec.get("aspect_ratio", ""),
        "width": svg_w,
        "height": svg_h,
        "mime": "image/svg+xml",
        "meta": {
            "source_type": spec.get("source_type", "svg"),
            "source_ref": spec.get("source_ref", ""),
            "vlm_verified": False,
            "attempts": 1,
        },
    }
    aspect = spec.get("aspect_ratio", "")
    expected_viewbox = _ASPECT_VIEWBOX.get(aspect, "(unknown)")
    return ToolResult.success(
        f"svg stored for slide {slide_idx} slot {slot_id!r} "
        f"({len(svg)} chars → {rel_path}, viewBox={expected_viewbox!r})"
    )


def _parse_svg_dimensions(svg: str) -> tuple[int, int]:
    """Extract ``(width, height)`` from an SVG's viewBox or root attrs.

    Tries, in order:

      1. ``viewBox`` attribute on the root ``<svg>`` — the canonical
         source. Format ``"minX minY W H"``; we read the last two ints.
      2. ``width`` / ``height`` attributes on the root ``<svg>`` (some
         hand-authored SVGs skip viewBox).
      3. ``_ASPECT_VIEWBOX[aspect_ratio]`` lookup via the spec, when
         the markup itself has neither. (Caller passes None here —
         aspect-ratio fallback is the caller's responsibility.)

    Returns ``(0, 0)`` on any parse failure. Best-effort: dimensions
    are surfaced for parity with the raster image payload, not used
    for layout decisions (the validated viewBox above already covers
    layout correctness).
    """
    import re

    if not svg:
        return 0, 0
    # ViewBox: "0 0 W H" (whitespace-separated). Tolerate commas and
    # extra spaces; grab the trailing two numbers.
    vb_match = re.search(
        r"<svg\b[^>]*\bviewBox\s*=\s*[\"']\s*[\d.,\-eE\s]+[\"']",
        svg,
        re.IGNORECASE,
    )
    if vb_match:
        nums = re.findall(r"-?\d+(?:\.\d+)?", vb_match.group(0))
        if len(nums) >= 2:
            try:
                return int(float(nums[-2])), int(float(nums[-1]))
            except (ValueError, IndexError):
                pass
    # Fallback: width / height attributes. Strip ``px`` / ``%`` suffixes.
    w_match = re.search(
        r"<svg\b[^>]*\bwidth\s*=\s*[\"']\s*(\d+(?:\.\d+)?)", svg, re.IGNORECASE
    )
    h_match = re.search(
        r"<svg\b[^>]*\bheight\s*=\s*[\"']\s*(\d+(?:\.\d+)?)", svg, re.IGNORECASE
    )
    if w_match and h_match:
        try:
            return int(float(w_match.group(1))), int(float(h_match.group(1)))
        except ValueError:
            pass
    return 0, 0


def _persist_svg_markup(
    svg: str,
    *,
    slide_idx: int,
    slot_id: str,
    output_dir: Path,
) -> str:
    """Write SVG markup to ``{output_dir}/svgs/slide_{N}_{slot}.svg``.

    Creates the ``svgs/`` subdirectory if missing. Returns the file's
    path relative to ``output_dir`` (forward slashes) — that's the URL
    the slide-builder embeds in the HTML so the headless browser can
    resolve it against the per-slide HTML file's location.

    Mirrors ``_persist_image_bytes`` in acquire.py, but writes text
    (UTF-8) instead of bytes and uses a parallel "svgs/" subdir.
    """
    svgs_dir = output_dir / _SVGS_SUBDIR
    svgs_dir.mkdir(parents=True, exist_ok=True)
    # slide_idx is 0-based internally; deck files are 1-based (1.html, ...).
    filename = f"slide_{slide_idx + 1}_{slot_id}.svg"
    file_path = svgs_dir / filename
    file_path.write_text(svg, encoding="utf-8")
    return f"{_SVGS_SUBDIR}/{filename}"


def _validate_desc_tag(root: ET.Element, expected_description: str) -> Optional[str]:
    """Ensure ``<svg>`` has a non-empty ``<desc>`` as its first child.

    The <desc> is the SVG spec's accessibility / description element.
    We require it to be the first child so screen readers and downstream
    LLM DOM readers see the description before the geometry.

    Returns an error string if invalid, else None.
    """
    # Filter out comments / processing instructions — ET.iter() yields
    # them as functions / special tags but they're not "real" children
    # for ordering purposes.
    real_children = [
        child for child in root
        if isinstance(child.tag, str) and not child.tag.startswith("{http://www.w3.org/2000/xmlns/}")
    ]
    if not real_children:
        return (
            "svg must contain a <desc> child as its first element "
            "(found empty <svg>)"
        )
    first = real_children[0]
    first_tag = first.tag
    if first_tag.startswith("{"):
        first_tag = first_tag.split("}", 1)[1]
    if first_tag != "desc":
        return (
            f"<desc> must be the first child of <svg> (got <{first_tag}> "
            f"instead). Move <desc>{expected_description or '...'}</desc> "
            f"to the top of the <svg>."
        )
    desc_text = (first.text or "").strip()
    if not desc_text:
        return (
            f"<desc> is empty; write a 1-sentence description of what the "
            f"image depicts (e.g. {expected_description!r})"
        )
    return None


def _collect_unsupported_tags(root: ET.Element) -> set[str]:
    """Walk the tree and collect any tags not in _SUPPORTED_SVG_TAGS.

    Skips the root element (the caller validates it separately as the
    <svg> root). Only walks children/descendants.
    """
    unsupported: set[str] = set()
    for elem in root:
        for node in elem.iter():
            tag = node.tag
            if tag.startswith("{"):
                # Strip namespace. Anything outside the SVG namespace is unsupported.
                ns, _, local = tag[1:].partition("}")
                if ns != _SVG_NS:
                    unsupported.add(f"{{{ns}}}{local}")
                    continue
                tag = local
            if tag not in _SUPPORTED_SVG_TAGS:
                unsupported.add(tag)
    return unsupported


def _has_full_bleed_bg_rect(
    root: ET.Element, vb_w: float, vb_h: float,
) -> Optional[str]:
    """Detect any <rect> that covers the full viewBox area.

    Returns the offending rect's ``fill`` attribute (for the error message)
    or ``None`` if no such rect exists.

    A rect is "full-bleed" when its width ≈ viewBox width AND height ≈
    viewBox height AND origin ≈ (0, 0). Such a rect masks the slide
    background when the SVG is composited — it's almost certainly an
    LLM attempt to paint a slide background inside the SVG. SVGs should
    be transparent; local panel backgrounds are fine because they don't
    span the viewBox.
    """
    tol_w = vb_w * _FULL_BLEED_TOL
    tol_h = vb_h * _FULL_BLEED_TOL
    for elem in root.iter():
        tag = elem.tag
        if tag.startswith("{"):
            ns, _, local = tag[1:].partition("}")
            if ns != _SVG_NS:
                continue
            tag = local
        if tag != "rect":
            continue
        try:
            w = float(elem.get("width", "0"))
            h = float(elem.get("height", "0"))
            x = float(elem.get("x", "0"))
            y = float(elem.get("y", "0"))
        except (TypeError, ValueError):
            continue
        if (abs(w - vb_w) <= tol_w and abs(h - vb_h) <= tol_h
                and abs(x) <= tol_w and abs(y) <= tol_h):
            return elem.get("fill", "(unspecified)")
    return None
