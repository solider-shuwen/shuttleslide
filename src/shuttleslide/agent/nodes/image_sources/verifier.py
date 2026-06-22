"""VLM verifier — judges whether a fetched image matches the description.

Used by acquire_web_image to filter out search hits that don't depict
what the outline asked for. One VLM call per candidate; the prompt
constrains the output to a strict JSON shape so parsing is deterministic.

The verifier is intentionally minimal: it holds a reference to an
LLMClient configured for vision (separate api_base/model from the text
LLM, see AgentOrchestrator._build_vlm_client) and exposes a single
async method. Subclassing to add retry / chain-of-thought / multi-image
comparison is the caller's prerogative — this module stays narrow.

Observability: ``on_llm_response`` is fired for every chat_with_vision
call (success or failure), mirroring how every other LLM call in the
pipeline surfaces its events. This is the only LLM call path that
doesn't go through ``chat_with_tools``; without firing the callback
here, VLM calls would be invisible to loggers / metrics collectors /
CLI progress displays that hook the event stream.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable, Dict, Optional

from shuttleslide.agent.llm import LLMClient
from shuttleslide.agent.llm.tool_call import LLMResponseEvent

logger = logging.getLogger(__name__)

# Strict prompt — the VLM must answer with one of two verdicts and a
# one-sentence reason. We strip the JSON out of any surrounding prose
# the model adds (some endpoints wrap JSON in ```json fences).
_VERIFY_PROMPT_TEMPLATE = """\
You are a strict image judge. Decide whether the attached image matches \
this description well enough to use in a professional presentation slide.

DESCRIPTION: {description}

Answer with a single JSON object, no other text. Shape:
{{"match": true|false, "reason": "<= 20 words explaining the verdict>"}}

Rules:
- "match" is true ONLY if the image's main subject, scene type, and \
  dominant mood align with the description. Minor stylistic differences \
  (color palette, exact composition) do NOT disqualify.
- "match" is false when the image depicts a clearly different subject \
  (e.g. description asks for a coffee shop interior, image shows a \
  landscape), when it is a placeholder / broken thumbnail, or when it \
  contains prominent text overlays / watermarks that would clash with \
  slide content.
- Do not nitpick lighting or framing. The bar is "would a reasonable \
  viewer recognise this as the described scene".
"""


class VLMVerifier:
    """Verify an image against a description via a vision LLM call."""

    def __init__(
        self,
        vlm_client: LLMClient,
        max_tokens: Optional[int] = 4096,
        on_llm_response: Optional[Callable[[LLMResponseEvent], None]] = None,
    ):
        self.vlm_client = vlm_client
        # 80 tokens is enough for {"match":bool,"reason":"..."} with a
        # ~15-word reason; keeps cost predictable per candidate.
        self.max_tokens = max_tokens
        self._on_llm_response = on_llm_response

    async def verify(
        self,
        image_b64: str,
        mime: str,
        description: str,
        *,
        slide_index: Optional[int] = None,
        iteration: int = 1,
        max_iterations: int = 1,
    ) -> Dict[str, Any]:
        """Return {"match": bool, "reason": str, "raw": str}.

        On any error (parse failure, network, malformed JSON), returns
        ``{"match": False, "reason": "<error>", "raw": "..."}`` — fail
        closed so a broken VLM never ships an unverified image.

        Observability kwargs (``slide_index`` / ``iteration`` /
        ``max_iterations``) are carried through to the
        ``on_llm_response`` event so observers can attribute calls to a
        specific slide + candidate attempt. They don't affect the
        verdict — purely informational. Mirrors the svg_generator event
        shape (slide_index only; no slot_id field on LLMResponseEvent).
        """
        prompt = _VERIFY_PROMPT_TEMPLATE.format(description=description)
        try:
            raw = await self.vlm_client.chat_with_vision(
                prompt=prompt,
                image_b64=image_b64,
                mime=mime,
                temperature=0.0,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:
            logger.warning("VLM verify call failed: %s", exc)
            self._fire_event(
                slide_index=slide_index,
                iteration=iteration,
                max_iterations=max_iterations,
                content=None,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {
                "match": False,
                "reason": f"vlm call failed: {type(exc).__name__}",
                "raw": "",
            }

        # Fire the callback with the raw VLM output. chat_with_vision
        # returns a bare string (no LLMResponse object), so we wrap it
        # into an LLMResponseEvent with stage="vlm_verifier" — observers
        # branching on stage know this is a verification call, not a
        # chat_with_tools call. No usage / reasoning is exposed by the
        # vision endpoint today; those fields stay None.
        self._fire_event(
            slide_index=slide_index,
            iteration=iteration,
            max_iterations=max_iterations,
            content=raw,
        )

        parsed = _parse_json_lenient(raw)
        if parsed is None:
            return {
                "match": False,
                "reason": "vlm returned non-JSON",
                "raw": raw,
            }
        match = bool(parsed.get("match", False))
        reason = str(parsed.get("reason", ""))[:200]
        return {"match": match, "reason": reason, "raw": raw}

    def _fire_event(
        self,
        *,
        slide_index: Optional[int],
        iteration: int,
        max_iterations: int,
        content: Optional[str],
        error: Optional[str] = None,
    ) -> None:
        """Emit an LLMResponseEvent if a callback is registered.

        Failures append the error to ``content`` (prefixed with
        ``[vlm error] ``) so a downstream logger sees both that the
        call happened AND that it failed, without needing a separate
        error channel.
        """
        if self._on_llm_response is None:
            return
        event_content = content
        if error is not None:
            event_content = f"[vlm error] {error}" if not content else f"{content}\n[vlm error] {error}"
        self._on_llm_response(
            LLMResponseEvent(
                stage="vlm_verifier",
                iteration=iteration,
                max_iterations=max_iterations,
                slide_index=slide_index,
                content=event_content,
            )
        )


def _parse_json_lenient(text: str) -> Dict[str, Any] | None:
    """Parse JSON from a possibly-fenced / prose-surrounded VLM response.

    VLMs sometimes wrap JSON in ```json ... ``` fences or add a lead-in
    sentence ("Here is the judgement: {...}"). We try, in order:
      1. Strip ``` fences if present, then json.loads.
      2. Find the first {...} substring, then json.loads.
      3. Give up.
    """
    if not text:
        return None
    stripped = text.strip()

    # Case 1: fenced code block.
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Case 2: direct JSON.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Case 3: first {...} substring.
    brace_match = re.search(r"\{[^{}]*\}", stripped, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return None
