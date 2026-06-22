"""Playwright screenshot capture — for source_ref that is a URL.

When the outline gives source_type="web" + source_ref="https://..." the
acquire path routes here instead of through image search. Typical use
cases: "screenshot the GitHub homepage", "capture the Stripe pricing page",
"show our competitor's landing page".

Reuses BrowserManager (html_to_pptx.analyzer.browser) for chromium
lifecycle. The caller may pass a long-lived BrowserManager so a deck
with N URL screenshots only pays the chromium startup cost once; if
none is passed, this module starts and stops its own.

The screenshot is JPEG-encoded by playwright (quality=85) and then
handed through ``_prepare_for_slide`` for cap-to-display-size downscaling
+ ``_persist_image_bytes`` for disk storage. The slide HTML then carries
a short ``<img src="images/slide_N_slot.jpg">`` reference — see
acquire.py's module docstring for why images are file-externalized.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Any, Optional

from shuttleslide.agent.nodes.image_sources.acquire import (
    _persist_image_bytes,
    _prepare_for_slide,
)
from shuttleslide.agent.state import AgentState

logger = logging.getLogger(__name__)

# Viewport per aspect_ratio. Matches the SVG viewBox dimensions in
# prompts._ASPECT_VIEWBOX so a slide that mixes svg + screenshot art
# keeps the source art at the same aspect as its slot.
_ASPECT_VIEWPORT = {
    "16:9": (1280, 720),
    "4:3": (1024, 768),
    "1:1": (800, 800),
    "3:2": (1200, 800),
    "2:3": (800, 1200),
}

# How long to wait for the page to render before screenshotting. SPAs
# often need this; static pages finish well under it.
_GOTO_TIMEOUT_MS = 20_000
_NETWORK_IDLE_TIMEOUT_MS = 10_000


async def capture_url_screenshot(
    *,
    state: AgentState,
    slide_idx: int,
    slot_id: str,
    spec: dict[str, Any],
    url: str,
    vlm_verifier: Optional[Any] = None,
    browser_manager: Any,
    output_dir: Optional[Path] = None,
) -> bool:
    """Navigate to ``url`` and capture a screenshot as a slide image.

    Returns True if a payload was stored in
    ``state.slide_images[slide_idx][slot_id]``, False otherwise (the
    caller falls back to svg).

    The caller MUST pass a started BrowserManager. We don't spin up our
    own so that a deck with N URL screenshots plus M bing_web searches
    shares one Chromium instance across the whole pipeline.

    ``output_dir`` is required (the file-externalized model needs a
    target directory). When None, the function returns False so the
    caller falls back to svg.
    """
    if output_dir is None:
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"screenshot requires output_dir; skipping"
        )
        return False
    if browser_manager is None:
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"screenshot requested but no browser_manager was provided"
        )
        return False

    try:
        page = await browser_manager.new_page()
    except Exception as exc:
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"failed to open page for screenshot: {exc}"
        )
        return False

    try:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
            # networkidle can hang on long-poll sites; treat as best-effort.
            try:
                await page.wait_for_load_state(
                    "networkidle", timeout=_NETWORK_IDLE_TIMEOUT_MS
                )
            except Exception as exc:
                logger.debug(
                    "networkidle wait failed for %s (continuing): %s", url, exc
                )
        except Exception as exc:
            state.add_warning(
                f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
                f"page.goto({url!r}) failed: {exc}"
            )
            return False

        # JPEG quality=85 is the same starting point as the downscale loop
        # in _encode_for_slide; the encoder will step down further if the
        # base64 length is still over budget.
        try:
            screenshot_bytes = await page.screenshot(
                full_page=False, type="jpeg", quality=85
            )
        except Exception as exc:
            state.add_warning(
                f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
                f"screenshot of {url!r} failed: {exc}"
            )
            return False
    finally:
        try:
            await page.close()
        except Exception:
            pass

    mime, prepared_bytes, ok = _prepare_for_slide(screenshot_bytes)
    if not ok:
        state.add_warning(
            f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
            f"screenshot of {url!r} could not be prepared"
        )
        return False

    vlm_verified = False
    if vlm_verifier is not None:
        b64 = base64.b64encode(prepared_bytes).decode("ascii")
        try:
            verdict = await vlm_verifier.verify(
                b64, mime, spec.get("description", ""),
                slide_index=slide_idx + 1,
                iteration=1,
                max_iterations=1,
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
        "mime": mime,
        "meta": {
            "source_type": "web",
            "source_ref": url,
            "vlm_verified": vlm_verified,
            "attempts": 1,
            "source": "screenshot",
            "original_url": url,
        },
    }
    return True
