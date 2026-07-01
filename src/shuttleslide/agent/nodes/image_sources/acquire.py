"""Web image acquisition: search → download → optional VLM verify → store.

This is the inner implementation called by ``image_acquirer._try_acquire_web``.
Returns True on success (payload stored in state.slide_images) or False
on failure (caller falls back to SVG).

Pipeline:
  1. Resolve the source_ref: URL → screenshot; otherwise → search query.
  2. Call the search provider → list of candidate URLs.
  3. For each candidate (max N): download → ensure PPTX-compatible →
     downscale to slide-display size → (optional) VLM verify against
     the description → persist to disk → store first match.
  4. All candidates fail → return False.

Persistence model (file-externalized):
  Web images are written to ``{output_dir}/images/slide_{N}_{slot_id}.jpg``
  and referenced from slide HTML via a short relative URL. This decouples
  image byte size from the slide HTML's 12000-char cap (see
  prompts._HTML_BUDGET) — a hero photo can be 200KB on disk while the
  HTML only carries the ~50-byte ``<img src="images/slide_3_hero.jpg">``
  tag. The previous base64-inline approach was architecturally broken:
  any useful photo at 600x400 q=70 produced 35-55KB of base64, already
  3-5x over the entire HTML budget.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from shuttleslide.agent.nodes.image_sources.search import (
    ImageCandidate,
    ImageSearchProvider,
)
from shuttleslide.agent.state import AgentState
from shuttleslide.html_to_pptx.image_utils import (
    download_image,
    ensure_pptx_compatible,
)

logger = logging.getLogger(__name__)

# Display-size cap. The slide canvas is 1280x720 CSS px; a hero image
# rarely needs to be larger than that on screen, and anything bigger
# just bloats the file. We cap the longest edge at this many pixels.
_MAX_DISPLAY_DIMENSION = 1280

# Floor for downscaling. Below this the image is too degraded to be
# useful as slide art — reject rather than ship a pixelated blob.
_MIN_DIMENSION = 200

# JPEG quality for the persisted file. Quality 80 is visually clean and
# keeps typical photos in the 50-300KB range — fine on disk, irrelevant
# to the HTML budget now that images are file-externalized.
_JPEG_QUALITY = 80

# Filesystem layout. Images for slide N are written under
# ``{output_dir}/{_IMAGES_SUBDIR}/slide_{N}_{slot_id}.{ext}`` and
# referenced from the HTML at the relative URL
# ``{_IMAGES_SUBDIR}/slide_{N}_{slot_id}.{ext}``.
_IMAGES_SUBDIR = "images"

# Match http:// or https:// — used to route source_ref between the
# screenshot path (URL) and the search path (free-text query).
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)

# Max candidates we'll try before giving up. Each one is a network
# round-trip + potential VLM call; 3 keeps latency bounded.
_MAX_CANDIDATES = 10


async def acquire_web_image(
    *,
    state: AgentState,
    slide_idx: int,
    slot_id: str,
    spec: Dict[str, Any],
    web_search_provider: Optional[ImageSearchProvider],
    vlm_verifier: Optional[Any] = None,
    browser_manager: Optional[Any] = None,
    output_dir: Optional[Path] = None,
) -> bool:
    """Acquire one image via the web path.

    Returns True if a payload was stored in
    ``state.slide_images[slide_idx][slot_id]``, False otherwise.

    ``output_dir`` is where acquired image files will be written
    (under a ``images/`` subdirectory). When None, the web path is
    skipped with a warning — without a persistent location for the
    file, the slide HTML cannot reference it, so we fall back to svg.
    """
    if output_dir is None:
        # No output_dir means there's nowhere to persist the downloaded
        # image. The file-externalized model requires a target directory.
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"web path requires output_dir; configure AgentConfig.output_dir "
            f"to enable web image acquisition"
        )
        return False

    source_ref = spec.get("source_ref", "")
    if not source_ref:
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"source_type=web but source_ref is empty"
        )
        return False

    # Route 1: URL → screenshot. Uses the shared browser_manager; if it
    # wasn't started (no bing_web provider, no other web spec yet), we
    # fail back to svg rather than spinning up Chromium just for this.
    if _URL_RE.match(source_ref):
        try:
            from shuttleslide.agent.nodes.image_sources.screenshot import (
                capture_url_screenshot,
            )
        except ImportError:
            state.add_warning(
                f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
                f"screenshot module unavailable, cannot capture URL"
            )
            return False
        return await capture_url_screenshot(
            state=state,
            slide_idx=slide_idx,
            slot_id=slot_id,
            spec=spec,
            url=source_ref,
            vlm_verifier=vlm_verifier,
            browser_manager=browser_manager,
            output_dir=output_dir,
        )

    # Route 2: free-text search query.
    if web_search_provider is None:
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"web source_ref requires a search provider; configure one "
            f"in AgentConfig to enable web image acquisition"
        )
        return False

    aspect = spec.get("aspect_ratio")
    description = spec.get("description", "")
    try:
        candidates: list[ImageCandidate] = await web_search_provider.search(
            source_ref, _MAX_CANDIDATES, aspect
        )
    except Exception as exc:
        # Provider blew up — don't crash the pipeline; just fall back.
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"search provider raised {type(exc).__name__}: {exc}"
        )
        return False

    if not candidates:
        return False

    # Iterate candidates with 1-based ordinal so the VLM verifier event
    # can report "candidate 2 of 3" — useful for diagnosing why a web
    # spec burned 3 VLM calls before falling back to svg.
    max_attempts = min(len(candidates), _MAX_CANDIDATES)
    for ordinal, cand in enumerate(candidates[:_MAX_CANDIDATES], start=1):
        if not cand.url:
            continue
        ok = await _try_one_candidate(
            state=state,
            slide_idx=slide_idx,
            slot_id=slot_id,
            spec=spec,
            candidate=cand,
            description=description,
            vlm_verifier=vlm_verifier,
            output_dir=output_dir,
            candidate_ordinal=ordinal,
            total_candidates=max_attempts,
        )
        if ok:
            return True

    return False


async def _try_one_candidate(
    *,
    state: AgentState,
    slide_idx: int,
    slot_id: str,
    spec: Dict[str, Any],
    candidate: ImageCandidate,
    description: str,
    vlm_verifier: Optional[Any],
    output_dir: Path,
    candidate_ordinal: int = 1,
    total_candidates: int = 1,
) -> bool:
    """Download + downscale + verify + persist one candidate. Returns True on store."""
    image_bytes = await asyncio.to_thread(download_image, candidate.url)
    if not image_bytes:
        return False

    image_bytes = ensure_pptx_compatible(image_bytes)
    mime, prepared_bytes, ok, pil_w, pil_h = _prepare_for_slide(image_bytes)
    if not ok:
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"candidate {candidate.url} could not be prepared "
            f"(too small or undecodable)"
        )
        return False

    # VLM verification is optional. When the verifier is None we accept
    # the first downloadable candidate — fine for offline dev, risky for
    # production (a mismatched photo will ship to the slide).
    vlm_verified = False
    if vlm_verifier is not None:
        # Verifier takes base64 — encode the prepared bytes (post-resize).
        # Pass slide_index + candidate ordinal so the on_llm_response
        # event can attribute the call.
        b64 = base64.b64encode(prepared_bytes).decode("ascii")
        try:
            verdict = await vlm_verifier.verify(
                b64, mime, description,
                slide_index=slide_idx + 1,
                iteration=candidate_ordinal,
                max_iterations=total_candidates,
            )
        except Exception as exc:
            state.add_warning(
                f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
                f"VLM verifier raised {type(exc).__name__}: {exc}"
            )
            return False
        if not verdict.get("match"):
            return False
        vlm_verified = True

    # Persist the image to disk. Deterministic filename so retries
    # overwrite cleanly (no orphan files piling up). The relative URL
    # is what the slide-builder embeds in the HTML; the renderer's
    # headless browser resolves it against the HTML file's location.
    rel_path = _persist_image_bytes(
        prepared_bytes,
        slide_idx=slide_idx,
        slot_id=slot_id,
        mime=mime,
        output_dir=output_dir,
    )

    state.slide_images.setdefault(slide_idx, {})[slot_id] = {
        "type": "image_file",
        "path": rel_path,
        "description": spec.get("description", ""),
        "image_type": spec.get("image_type", "illustration"),
        "width": pil_w,
        "height": pil_h,
        "mime": mime,
        "meta": {
            "source_type": "web",
            "source_ref": spec.get("source_ref", ""),
            "vlm_verified": vlm_verified,
            "attempts": 1,
            "source": candidate.source,
            "original_url": candidate.url,
        },
    }
    return True


def _prepare_for_slide(image_bytes: bytes) -> Tuple[str, bytes, bool, int, int]:
    """Downscale + JPEG-encode an image for slide display.

    Returns ``(mime, jpeg_bytes, ok, width, height)``. ``ok=False`` when
    the bytes can't be decoded as an image, or when the decoded image is
    already smaller than ``_MIN_DIMENSION`` (we don't upscale — tiny
    inputs are rejected rather than shipped as pixelated blobs). On
    ``ok=False`` the dimension fields are ``(0, 0)``.

    The dimensions reflect the post-resize image (what actually ships to
    the slide HTML); the original larger source's dimensions are
    intentionally not surfaced. ``(0, 0)`` is also returned on the
    Pillow-missing fallback path — callers that need real dimensions
    should require Pillow rather than degrade silently.

    Unlike the previous ``_encode_for_slide`` (which targeted a ~3KB
    base64 budget), this targets a display-size budget: the longest edge
    is capped at ``_MAX_DISPLAY_DIMENSION`` (1280px). The resulting JPEG
    is typically 50-300KB — fine on disk, decoupled from the slide HTML's
    12000-char cap thanks to the file-externalized persistence model.
    """
    if not image_bytes:
        return "", b"", False, 0, 0

    try:
        from PIL import Image
    except ImportError:
        # No Pillow — pass through the bytes as-is. ensure_pptx_compatible
        # already normalized the format to something PPTX can handle.
        mime = _guess_mime_from_bytes(image_bytes)
        return mime, image_bytes, True, 0, 0

    try:
        img = Image.open(io.BytesIO(image_bytes))
    except Exception as exc:
        logger.warning("PIL could not open image: %s", exc)
        return "", b"", False, 0, 0

    # Flatten transparency onto white — JPEG has no alpha channel and
    # transparent PNGs saved as JPEG without a matte turn solid black.
    if img.mode in ("RGBA", "LA"):
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    # Reject images that are already too small. We don't upscale.
    w, h = img.size
    if w < _MIN_DIMENSION or h < _MIN_DIMENSION:
        return "", b"", False, 0, 0

    # Cap the longest edge at _MAX_DISPLAY_DIMENSION. Preserve aspect.
    if max(w, h) > _MAX_DISPLAY_DIMENSION:
        scale = _MAX_DISPLAY_DIMENSION / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))

    final_w, final_h = img.size
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=_JPEG_QUALITY)
    return "image/jpeg", buf.getvalue(), True, final_w, final_h


def _persist_image_bytes(
    image_bytes: bytes,
    *,
    slide_idx: int,
    slot_id: str,
    mime: str,
    output_dir: Path,
) -> str:
    """Write image bytes to ``{output_dir}/images/slide_{N}_{slot}.{ext}``.

    Creates the ``images/`` subdirectory if missing. Returns the
    image's URL relative to ``output_dir`` — that's the string the
    slide-builder embeds in the HTML so the headless browser can
    resolve it against the per-slide HTML file's location.
    """
    ext = "jpg" if mime in ("image/jpeg", "image/pjpeg") else (
        "png" if mime == "image/png" else "img"
    )
    images_dir = Path(output_dir) / _IMAGES_SUBDIR
    images_dir.mkdir(parents=True, exist_ok=True)
    # slide_idx is 0-based internally; deck files are 1-based (1.html, ...).
    filename = f"slide_{slide_idx + 1}_{slot_id}.{ext}"
    file_path = images_dir / filename
    file_path.write_bytes(image_bytes)
    # Use forward slashes so the URL works on Windows + non-Windows alike.
    return f"{_IMAGES_SUBDIR}/{filename}"


def _guess_mime_from_bytes(image_bytes: bytes) -> str:
    """Sniff the image format from magic bytes. Falls back to JPEG."""
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"
