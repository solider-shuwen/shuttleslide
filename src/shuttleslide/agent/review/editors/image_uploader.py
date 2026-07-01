"""ImageUploader — replace an image slot with a user-supplied file.

Targets ``kind="image"`` (image_file / image payloads in
``state.slide_images[idx][slot_id]``). The upload path is the same
regardless of transport (WS base64 or HTTP multipart): bytes in →
Pillow re-encode → write to ``{output_dir}/images/slide_{N}_{slot}.{ext}``
→ update state.slide_images payload.

Description
-----------
The payload's ``description`` field follows the same contract as the
web / svg acquire paths (used by slide-builder prompt, motion design
context, stale propagation, ...). Two sources, in priority order:

  1. User-supplied. The client may attach ``description`` to the upload
     message; it lands verbatim in the payload.
  2. VLM-generated. When the user left it blank AND
     ``config.enable_vlm_description`` is True AND a VLM endpoint is
     configured, ``VLMDescriber`` generates one short sentence from
     the re-encoded image bytes. Fail-open: any VLM error leaves the
     description empty rather than blocking the upload.

``meta.described_by`` records which path produced the value
(``"user"`` / ``"vlm"`` / ``"none"``) so downstream observers can
distinguish authoritative input from auto-generated copy.

Security
--------
Two layers keep user-supplied bytes from being a vector:

  1. Pillow re-encode. ``Image.open`` parses the bytes, then we save
     to a fresh JPEG/PNG buffer. This strips EXIF, embedded thumbnails,
     polyglot payloads, and any non-image data that might have ridden
     along. We trust Pillow's decoder, not the bytes.
  2. MIME sniffing on the decoded image. The declared MIME from the
     client is ignored — we read Pillow's ``format`` attribute and
     emit the canonical mime. A file claiming to be image/png that
     decodes as JPEG is reported as JPEG.

Size cap
--------
``MAX_UPLOAD_BYTES = 10 * 1024 * 1024`` (10MB). Bigger uploads are
rejected before Pillow touches them — pillow-sad-path DoS via a
huge but well-formed PNG is the obvious failure mode.

LLM mode
--------
Not applicable. ``apply_llm_edit`` raises NotImplementedError; the
chat UI hides the LLM affordance for image slots and surfaces Upload
as the only edit option.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from shuttleslide.agent.review.editors.base import EditResult, Editor

logger = logging.getLogger(__name__)

# Hard cap on upload size. 10MB is generous for slide-quality images
# (a 4K JPEG is typically 3-5MB); bigger uploads are almost certainly
# accidents or abuse.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# Same convention as acquire.py — 1-based slide numbers on disk.
_IMAGES_SUBDIR = "images"


class ImageUploader(Editor):
    """Editor for ``kind="image"`` targets.

    Direct edit takes raw bytes + mime; the orchestrator/server layer
    handles WS-decode / multipart-parse before calling into the editor.
    LLM edit is not supported — images aren't text the model can revise.
    """

    kind = "image"

    async def apply_direct_edit(
        self, target, new_value, state, config
    ) -> EditResult:
        """Direct mode takes bytes + mime packaged in ``new_value``.

        ``new_value`` here is a dict (not a string) with ``data`` (bytes)
        and ``source_ref`` (filename for attribution). The server's WS
        handler adapts the string-based protocol to this dict shape.
        Optional ``description`` (str) carries a user-typed caption; if
        absent / blank, the VLM describer fills it in (see module
        docstring).

        Delete sentinel: ``{"delete": True}`` from the chat panel's
        Delete button pops the slot from ``state.slide_images``. The
        slide builder then regenerates HTML without the ``<img>``.
        """
        if isinstance(new_value, dict) and new_value.get("delete"):
            return self._delete_image(target, state)
        if not isinstance(new_value, dict):
            return EditResult(
                ok=False,
                error="image upload requires a dict payload {data, source_ref}",
            )
        data = new_value.get("data")
        source_ref = new_value.get("source_ref", "upload")
        if not isinstance(data, (bytes, bytearray)) or not data:
            return EditResult(ok=False, error="upload data must be non-empty bytes")
        description = new_value.get("description")
        if not isinstance(description, str):
            description = None
        return await self._apply(
            target,
            bytes(data),
            source_ref,
            state,
            config,
            description=description,
        )

    async def apply_llm_edit(
        self, target, user_message, history, state, config
    ) -> EditResult:
        # Images aren't text the model can revise; the UI hides LLM mode
        # for image slots and only surfaces Upload.
        return EditResult(
            ok=False,
            error="LLM edit not supported for image slots; use Upload instead",
        )

    # ------------------------------------------------------------------
    # core path
    # ------------------------------------------------------------------

    def _resolve_slot_payload(
        self, target, state
    ) -> Optional[Dict[str, Any]]:
        slide_idx = target.meta.get("slide_idx")
        slot_id = target.meta.get("slot_id")
        if slide_idx is None or slot_id is None:
            return None
        slots = state.slide_images.get(int(slide_idx))
        if slots is None:
            return None
        payload = slots.get(slot_id)
        if payload is None:
            # Initialise a fresh payload — the slot may have been declared
            # in the spec but never acquired. Mirrors how acquire.py
            # setdefault + assign.
            payload = {}
            slots[slot_id] = payload
        return payload

    async def _apply(
        self,
        target,
        data: bytes,
        source_ref: str,
        state,
        config,
        *,
        description: Optional[str] = None,
    ) -> EditResult:
        if len(data) > MAX_UPLOAD_BYTES:
            return EditResult(
                ok=False,
                error=(
                    f"uploaded file is {len(data)} bytes; max is "
                    f"{MAX_UPLOAD_BYTES} bytes"
                ),
            )
        try:
            from PIL import Image
        except ImportError as exc:
            return EditResult(
                ok=False,
                error=(
                    "Pillow is required for image uploads. Install with "
                    f"`pip install Pillow` ({exc.name})"
                ),
            )
        # Re-encode via Pillow to strip EXIF / polyglot payloads.
        try:
            with Image.open(io.BytesIO(data)) as img:
                img.load()
                pil_format = img.format or "PNG"
                # Capture pre-reencode natural dims so the reviewing UI
                # can size a freshly inserted <img> with the correct
                # aspect ratio (used by the slides-stage drag-drop flow).
                natural_w, natural_h = img.size
                reencoded, mime = _normalize_image_format(img, pil_format)
        except Exception as exc:
            return EditResult(
                ok=False,
                error=(
                    f"could not decode image: {exc}. The file may be "
                    f"corrupt or not a real image."
                ),
            )

        output_dir = config.output_dir
        if not output_dir:
            return EditResult(
                ok=False,
                error="no output_dir configured for this run",
            )

        slide_idx = int(target.meta.get("slide_idx", 0))
        slot_id = target.meta.get("slot_id", "slot")
        payload = self._resolve_slot_payload(target, state)
        if payload is None:
            return EditResult(
                ok=False,
                error="image slot not found in state",
            )

        # Resolve the description BEFORE we mutate state — if the VLM
        # call blows up we want to leave the slot untouched rather than
        # half-updated. ``final_desc`` is the value that lands in
        # ``payload["description"]``; ``described_by`` records provenance
        # for downstream observers (which path produced the string).
        final_desc, described_by = await _resolve_description(
            description,
            reencoded,
            mime,
            slide_idx=slide_idx,
            config=config,
        )

        rel_path = _persist_image_bytes(
            reencoded,
            slide_idx=slide_idx,
            slot_id=slot_id,
            mime=mime,
            output_dir=Path(output_dir),
        )
        # Preserve any pre-existing aspect_ratio/image_type so a
        # downstream SvgEditor-style validation on the slot still works
        # (slide_images doesn't have a single schema — payloads carry
        # whatever the original acquire path set). The description is
        # intentionally NOT preserved: a re-upload implies the old
        # caption no longer describes what's in the slot.
        payload.clear()
        payload.update(
            {
                "type": "image_file",
                "path": rel_path,
                "description": final_desc,
                "image_type": payload.get("image_type", "illustration"),
                "width": natural_w,
                "height": natural_h,
                "mime": mime,
                "meta": {
                    "source_type": "user_upload",
                    "source_ref": source_ref,
                    "described_by": described_by,
                    # User uploads are never "verified" by the VLM — the
                    # VLM may have *described* them, but that's a
                    # different operation. Keep this False so consumers
                    # branching on ``vlm_verified`` don't treat a user
                    # upload as a vetted web-search hit.
                    "vlm_verified": False,
                    "attempts": 1,
                },
            }
        )
        return EditResult(
            ok=True,
            new_value=rel_path,
            diff=None,
            assistant_msg=f"Uploaded {source_ref} → {rel_path}",
            width=natural_w,
            height=natural_h,
            description=final_desc,
        )

    def _delete_image(self, target, state) -> EditResult:
        """Pop the slot from ``state.slide_images`` and strip the
        matching ``<img>`` from the cached slide HTML.

        Without the HTML strip, ``state.slides[idx].slots["html"]``
        keeps an ``<img>`` pointing at the popped slot — showing a
        broken image (or the stale file, since we keep the bytes for
        undo) until the user manually regenerates the slide. The strip
        makes the deletion reflect immediately in the slides stage.

        File on disk is intentionally kept — undo needs it back, and
        orphaned files in ``output_dir/images/`` are harmless (the
        pipeline only reads paths referenced by
        ``state.slide_images``).
        """
        slide_idx = target.meta.get("slide_idx")
        slot_id = target.meta.get("slot_id")
        if slide_idx is None or slot_id is None:
            return EditResult(
                ok=False, error="image target missing slide_idx/slot_id"
            )
        slots = state.slide_images.get(int(slide_idx))
        if not slots or slot_id not in slots:
            return EditResult(
                ok=False,
                error="image slot is already empty — nothing to delete",
            )
        old_path = slots[slot_id].get("path", "")
        slots.pop(slot_id, None)
        # Mirror the deletion into the slide HTML so the slides stage
        # reflects it immediately. ``data-slot`` is the primary anchor
        # (always present on pipeline-generated ``<img>``); src-path
        # match is the fallback for LLM-improvised HTML that dropped
        # the attribute.
        _strip_img_from_slide_html(state, int(slide_idx), slot_id, old_path)
        return EditResult(
            ok=True,
            new_value="",
            assistant_msg=f"Removed image from slot {slot_id}",
        )


async def _resolve_description(
    user_description: Optional[str],
    image_bytes: bytes,
    mime: str,
    *,
    slide_idx: int,
    config,
) -> Tuple[str, str]:
    """Decide which description lands in the upload payload.

    Returns ``(description, described_by)`` where ``described_by`` is one
    of ``"user"`` / ``"vlm"`` / ``"none"``:

      * ``"user"``  — user supplied a non-blank description; used verbatim.
      * ``"vlm"``   — VLM produced a non-empty description.
      * ``"none"``  — VLM disabled, endpoint not configured, or the call
                      returned empty / raised. ``description`` is "".

    Fail-open: any VLM error degrades to ``("none", "")`` rather than
    blocking the upload. The image still lands in state with a blank
    description that the user can fill in via the review UI.
    """
    user_desc = (user_description or "").strip()
    if user_desc:
        return user_desc, "user"

    if not getattr(config, "enable_vlm_description", True):
        return "", "none"

    vlm_client = _build_vlm_client(config)
    if vlm_client is None:
        return "", "none"

    # Lazy import keeps editors/ importable when the image_sources
    # package isn't on the path (e.g. minimal test environments that
    # only exercise the text editors).
    from shuttleslide.agent.nodes.image_sources.describer import VLMDescriber

    describer = VLMDescriber(vlm_client)
    b64 = base64.b64encode(image_bytes).decode("ascii")
    try:
        desc = await describer.describe(b64, mime, slide_index=slide_idx)
    except Exception as exc:
        # VLMDescriber already swallows chat_with_vision errors and
        # returns "" — this catch is a defensive belt-and-braces for
        # anything the describer itself might raise (e.g. a bug in
        # _normalize_description). Never let it surface to the user.
        logger.warning("VLM describer raised on upload: %s", exc)
        return "", "none"
    if desc:
        return desc, "vlm"
    return "", "none"


def _build_vlm_client(config):
    """Construct a VLM LLMClient from config, or None if unreachable.

    Mirrors ``editors/base.py:build_llm_client`` but routes through the
    VLM endpoint (falls back to the text LLM endpoint when the VLM
    fields are blank — same fallback rule the orchestrator uses for
    ``_build_vlm_client``). Per-edit construction is cheap: the
    underlying openai client is lazy-built on first call.

    Returns None when any of (api_base, api_key, model) is missing —
    callers (only ``_resolve_description`` today) treat that as
    "VLM unavailable, fall back to empty description".
    """
    from shuttleslide.agent.llm.client import LLMClient

    api_base = (config.vlm_api_base or config.api_base or "").strip()
    api_key = (config.vlm_api_key or config.api_key or "").strip()
    model = (config.vlm_model or config.model or "").strip()
    if not (api_base and api_key and model):
        return None
    return LLMClient(
        api_base=api_base,
        api_key=api_key,
        model=model,
        disable_required_tool_choice=config.disable_required_tool_choice,
    )


def _normalize_image_format(img, pil_format: str) -> Tuple[bytes, str]:
    """Re-encode ``img`` to a canonical PNG or JPEG.

    PNG stays PNG (lossless, supports transparency). Everything else
    becomes JPEG (smaller, universally supported). Returns
    ``(bytes, mime)``.
    """
    buf = io.BytesIO()
    if pil_format.upper() == "PNG":
        img.save(buf, format="PNG")
        mime = "image/png"
    else:
        # Flatten transparency onto white — JPEG doesn't support alpha.
        if img.mode in ("RGBA", "LA", "P"):
            from PIL import Image as _Image

            background = _Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            background.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=92)
        mime = "image/jpeg"
    return buf.getvalue(), mime


def _persist_image_bytes(
    image_bytes: bytes,
    slide_idx: int,
    slot_id: str,
    mime: str,
    output_dir: Path,
) -> str:
    """Mirror of acquire._persist_image_bytes for the upload path.

    Uses 1-based slide numbers on disk (matching the pipeline's
    convention) so a slot's on-disk path is stable whether it was
    acquired by the LLM or uploaded by the user — overwriting the
    file the slide-builder HTML already points at.
    """
    ext = "jpg" if mime in ("image/jpeg", "image/pjpeg") else (
        "png" if mime == "image/png" else "img"
    )
    images_dir = Path(output_dir) / _IMAGES_SUBDIR
    images_dir.mkdir(parents=True, exist_ok=True)
    filename = f"slide_{slide_idx + 1}_{slot_id}.{ext}"
    file_path = images_dir / filename
    file_path.write_bytes(image_bytes)
    return f"{_IMAGES_SUBDIR}/{filename}"


# Stripping a deleted image's ``<img>`` from slide HTML. Two anchors,
# tried in order:
#   1. ``data-slot="{slot_id}"`` — always present on pipeline-generated
#      ``<img>`` (see prompts.py image snippet templates). Robust: slot_id
#      doesn't change with cache-bust query strings or LLM src rewrites.
#   2. ``src`` containing the slot's on-disk path — fallback for
#      LLM-improvised HTML that dropped ``data-slot`` but kept the src.
# Both patterns accept self-closing ``<img ... />`` and bare ``<img ...>``,
# and are case-insensitive (``<IMG>`` shows up in some legacy HTML).
_IMG_DATA_SLOT_RE_TEMPLATE = (
    r'<img\b[^>]*\bdata-slot\s*=\s*["\']{slot}["\'][^>]*/?>'
)
_IMG_SRC_PATH_RE_TEMPLATE = (
    r'<img\b[^>]*\bsrc\s*=\s*["\'][^"\']*{path}(?:\?[^"\']*)?["\'][^>]*/?>'
)


def _strip_img_from_slide_html(
    state, slide_idx: int, slot_id: str, old_path: str
) -> None:
    """Remove the ``<img>`` for ``slot_id`` from the cached slide HTML.

    The slides stage renders ``state.slides[idx].slots["html"]`` directly;
    popping just ``state.slide_images`` leaves a dangling reference. This
    patches the HTML in place so the deletion is visible without waiting
    for a manual regen.

    Best-effort: silently no-ops on malformed state (missing slide, empty
    HTML, out-of-range idx). Undo does NOT re-add the ``<img>`` — see the
    ``_delete_image`` docstring.
    """
    if slide_idx < 0 or slide_idx >= len(state.slides):
        return
    slide = state.slides[slide_idx]
    if slide is None or not hasattr(slide, "slots"):
        return
    html = slide.slots.get("html", "")
    if not html or not slot_id:
        return
    new_html = _remove_img_tags(html, slot_id, old_path)
    if new_html != html:
        slide.slots["html"] = new_html


def _remove_img_tags(html: str, slot_id: str, old_path: str) -> str:
    """Strip ``<img>`` tags matching ``slot_id`` (primary) or
    ``old_path`` (fallback) from ``html``.

    Returns the HTML unchanged if neither anchor matches.
    """
    slot_esc = re.escape(slot_id)
    # Anchor 1: data-slot match. If it hits, return immediately —
    # falling through to src would risk double-stripping a different
    # ``<img>`` that happens to share the path.
    new_html, n1 = re.subn(
        re.compile(
            _IMG_DATA_SLOT_RE_TEMPLATE.format(slot=slot_esc),
            re.IGNORECASE | re.DOTALL,
        ),
        "",
        html,
    )
    if n1 > 0:
        return new_html
    # Anchor 2: src-path match. Strip any ``?query`` suffix from the
    # stored path before escaping — cache-bust queries on the src
    # should not block the match.
    base_path = (old_path or "").split("?")[0]
    if not base_path:
        return html
    path_esc = re.escape(base_path)
    new_html, _ = re.subn(
        re.compile(
            _IMG_SRC_PATH_RE_TEMPLATE.format(path=path_esc),
            re.IGNORECASE | re.DOTALL,
        ),
        "",
        html,
    )
    return new_html
