"""VLM describer — produces a short natural-language description of an image.

The mirror of ``VLMVerifier``: where the verifier asks "does this image
match the description?", the describer asks "what is in this image?".
Used by the upload pipeline (``ImageUploader``) so that user-supplied
images enter ``state.slide_images`` with a real description string
instead of the legacy ``"(user upload)"`` placeholder.

Single-method surface (``describe``) keeps the module narrow. The prompt
constrains the output to one short line so the result is drop-in usable
wherever a ``description`` field is consumed downstream (slide-builder
prompt, motion-design context, stale propagation diffs, ...).

Failure mode is fail-open: any error (network, parse, empty response)
returns an empty string. A broken VLM never blocks an upload — the
description field is just left blank for the user to fill in via the
review UI.

Observability: ``on_llm_response`` is fired for every ``chat_with_vision``
call (success or failure), mirroring ``VLMVerifier`` so the upload-time
VLM call shows up in the same event stream as the verify-time ones.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from shuttleslide.agent.llm import LLMClient
from shuttleslide.agent.llm.tool_call import LLMResponseEvent

logger = logging.getLogger(__name__)

# Strict prompt — one short line, no preamble, no quotation. The model
# is told explicitly to skip watermarks / overlays (those would leak
# into the slide-builder prompt as if they were meaningful content).
_DESCRIBE_PROMPT = """\
Describe this image in a single concise sentence (<= 30 words).

Rules:
- Identify the main subject, scene type, and dominant mood.
- Do NOT transcribe or comment on any text, watermark, logo, or UI \
  overlay present in the image.
- Do NOT wrap the answer in quotes or fence it in markdown.
- Output ONLY the description sentence, nothing else.
"""


class VLMDescriber:
    """Generate a short description for an image via a vision LLM call."""

    def __init__(
        self,
        vlm_client: LLMClient,
        max_tokens: Optional[int] = 300,
        on_llm_response: Optional[Callable[[LLMResponseEvent], None]] = None,
    ):
        self.vlm_client = vlm_client
        # 300 tokens is plenty for a <=30-word sentence with headroom for
        # CJK expansion (one Chinese character ≈ 1-2 tokens). Keeps cost
        # predictable per upload.
        self.max_tokens = max_tokens
        self._on_llm_response = on_llm_response

    async def describe(
        self,
        image_b64: str,
        mime: str,
        *,
        slide_index: Optional[int] = None,
    ) -> str:
        """Return a one-sentence description of the image.

        Returns ``""`` on any error (network, malformed response, empty
        output). Fail-open: a broken VLM must never block an upload.

        ``slide_index`` is purely informational — passed through to the
        ``on_llm_response`` event so observers can attribute the call.
        Mirrors ``VLMVerifier.verify``'s observability kwargs.
        """
        try:
            raw = await self.vlm_client.chat_with_vision(
                prompt=_DESCRIBE_PROMPT,
                image_b64=image_b64,
                mime=mime,
                temperature=0.3,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            logger.warning("VLM describe call failed: %s", exc)
            self._fire_event(
                slide_index=slide_index,
                content=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            return ""

        self._fire_event(slide_index=slide_index, content=raw)
        return _normalize_description(raw)

    def _fire_event(
        self,
        *,
        slide_index: Optional[int],
        content: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        """Emit an LLMResponseEvent if a callback is registered.

        Mirrors ``VLMVerifier._fire_event``: failures annotate the
        content with ``[vlm error] ...`` so a downstream logger sees
        both that the call happened AND that it failed.
        """
        if self._on_llm_response is None:
            return
        event_content = content
        if error is not None:
            event_content = (
                f"[vlm error] {error}"
                if not content
                else f"{content}\n[vlm error] {error}"
            )
        self._on_llm_response(
            LLMResponseEvent(
                stage="vlm_describer",
                iteration=1,
                max_iterations=1,
                slide_index=slide_index,
                content=event_content,
            )
        )


def _normalize_description(raw: Any) -> str:
    """Coerce a raw VLM response into a single clean line.

    Strips surrounding whitespace and quote pairs (some models wrap the
    answer in ``"..."`` despite the prompt). If the response spans
    multiple lines, only the first non-empty one is kept — the prompt
    asks for one sentence, so anything past the first newline is either
    a model hallucination or an accidental postscript.
    """
    if not raw:
        return ""
    text = str(raw).strip()
    if not text:
        return ""
    # Strip matching outer quote pairs (single or double, full-width or
    # ASCII). Models that ignore "do NOT wrap in quotes" still produce
    # otherwise-usable text — we just lift the wrapper off.
    while len(text) >= 2 and text[0] in "\"'“‘" and text[-1] in "\"'”’":
        text = text[1:-1].strip()
    # First non-empty line — anything past the first newline violates
    # the "single sentence" constraint and is dropped.
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return ""
