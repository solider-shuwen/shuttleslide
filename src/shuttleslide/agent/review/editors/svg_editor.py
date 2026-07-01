"""SvgEditor — direct + LLM editing for slide_images SVG payloads.

Targets ``kind="svg"`` (svg_file payloads produced by set_svg). The
editor mutates ``state.slide_images[idx][slot_id]`` — specifically the
``data`` field (inline markup) and the on-disk file at ``path``.

Validation
----------
Reuses ``validate_svg_for_slot`` from svg_tools.py — the exact same
checks the pipeline runs. This is why Step 1 of PR3 extracted it to a
pure function: editors and pipeline must agree on what a valid SVG is.
Direct edits go through the same validator, so user-pasted SVGs cannot
sneak unsupported tags past the reviewer.

LLM mode
--------
Uses ``set_svg``'s tool schema with ``tool_choice="required"`` — the
model must emit a tool call. The LLM gets the current SVG in the
system prompt and is asked to call ``set_svg`` with the revised
markup. Tool-call arguments are then run through
``validate_svg_for_slot`` for an independent check (defence in depth
against the model emitting something the schema permits but our
converter chokes on).
"""

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any, Dict, List, Optional

from shuttleslide.agent.review.editors.base import (
    EditResult,
    Editor,
    build_llm_client,
    truncate_for_prompt,
)
from shuttleslide.agent.tools.svg_tools import validate_svg_for_slot


# How many times apply_llm_edit retries after a validation / parse
# failure before giving up. Total LLM calls = 1 + _LLM_EDIT_MAX_RETRIES.
# Mirrors the pipeline's _acquire_svg retry budget
# (image_acquirer.py:max_attempts default).
_LLM_EDIT_MAX_RETRIES = 3


class SvgEditor(Editor):
    """Editor for ``kind="svg"`` targets.

    The slide_idx / slot_id live in ``target.meta`` (set by
    ``ImagesStage.build_snapshot``). The spec passed to
    ``validate_svg_for_slot`` is reconstructed from the current payload
    + meta — the payload carries ``aspect_ratio`` (post-PR3) so the
    validator can enforce the viewBox match.
    """

    kind = "svg"

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
        return slots.get(slot_id)

    def _build_spec(self, target, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Reconstruct the spec dict ``validate_svg_for_slot`` expects."""
        return {
            "slide_idx": int(target.meta.get("slide_idx", 0)),
            "slot_id": target.meta.get("slot_id", ""),
            "aspect_ratio": payload.get("aspect_ratio", ""),
            "description": payload.get("description", ""),
            "image_type": payload.get("image_type", "illustration"),
        }

    def _write_svg_to_disk(
        self, svg: str, payload: Dict[str, Any], output_dir
    ) -> Optional[str]:
        """Persist the new SVG markup to the payload's path.

        Returns None on success, or an error string if the write fails.
        The payload's ``path`` is the same one set_svg wrote — we
        overwrite it so the existing inliner + html_to_pptx picks up
        the new bytes automatically. Relative paths resolve against
        ``output_dir`` (matching set_svg's behaviour).
        """
        rel = payload.get("path")
        if not rel:
            return None  # svg (inline) payload, no file to write
        path = Path(rel)
        if not path.is_absolute():
            if output_dir is None:
                return (
                    f"cannot write SVG to relative path {rel!r} without "
                    f"config.output_dir"
                )
            path = Path(output_dir) / path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(svg, encoding="utf-8")
        except OSError as exc:
            return f"failed to write SVG to {path}: {exc}"
        return None

    def _apply(
        self, target, svg: str, state, config
    ) -> EditResult:
        payload = self._resolve_slot_payload(target, state)
        if payload is None:
            return EditResult(
                ok=False,
                error="SVG slot not found in state (it may have been cleared)",
            )
        spec = self._build_spec(target, payload)
        err = validate_svg_for_slot(svg, spec)
        if err is not None:
            return EditResult(ok=False, error=err)
        old_value = payload.get("data", "")
        # On-disk write happens first; if it fails we abort before mutating state.
        write_err = self._write_svg_to_disk(svg, payload, config.output_dir)
        if write_err is not None:
            return EditResult(ok=False, error=write_err)
        payload["data"] = svg
        # Refresh dimensions in case the viewBox changed. Validation
        # pins viewBox to aspect_ratio's expected value when aspect is
        # set, so this is a no-op in the common case — but legacy
        # payloads with empty aspect_ratio can drift, and keeping the
        # field fresh is cheaper than auditing every consumer for
        # "what if width is stale".
        from shuttleslide.agent.tools.svg_tools import _parse_svg_dimensions

        payload["width"], payload["height"] = _parse_svg_dimensions(svg)
        return EditResult(
            ok=True,
            new_value=svg,
            diff=_unified_diff_xml(old_value, svg),
            assistant_msg=None,
        )

    async def apply_direct_edit(
        self, target, new_value, state, config
    ) -> EditResult:
        if not isinstance(new_value, str) or not new_value.strip():
            return EditResult(ok=False, error="SVG must be a non-empty string")
        return self._apply(target, new_value, state, config)

    async def apply_llm_edit(
        self, target, user_message, history, state, config
    ) -> EditResult:
        payload = self._resolve_slot_payload(target, state)
        if payload is None:
            return EditResult(
                ok=False,
                error="SVG slot not found in state (it may have been cleared)",
            )
        spec = self._build_spec(target, payload)
        current_svg = payload.get("data", "") or target.current_value
        llm = build_llm_client(config)
        # Tool schema mirrors set_svg so the LLM is constrained to emit
        # exactly the markup we want.
        tool_schema = {
            "type": "function",
            "function": {
                "name": "set_svg",
                "description": (
                    "Submit the complete revised inline SVG markup for "
                    f"slide {spec['slide_idx']} slot {spec['slot_id']!r}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "svg": {
                            "type": "string",
                            "description": (
                                "Complete <svg>...</svg> markup. Must declare "
                                f"viewBox matching aspect_ratio={spec['aspect_ratio']!r}."
                            ),
                        },
                    },
                    "required": ["svg"],
                },
            },
        }
        reject_schema = {
            "type": "function",
            "function": {
                "name": "reject_request",
                "description": (
                    "Call this if the user's request is out of scope for a "
                    "single SVG slot — adding/removing/reordering slides, "
                    "changing the deck-wide theme, or rewriting the entire "
                    "slide that contains this SVG. Do NOT call it for normal "
                    "per-slot edits (recoloring, swapping an icon, restyling "
                    "the illustration)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": (
                                "Short explanation of why the request is out "
                                "of scope for this SVG slot. Shown to the user."
                            ),
                        },
                        "suggested_stage": {
                            "type": "string",
                            "enum": ["outline", "slides"],
                            "description": (
                                "The stage the user should switch to. Use "
                                "'outline' for add/remove/reorder or deck-"
                                "wide theme/structure changes. Use 'slides' "
                                "if the request implies rewriting the whole "
                                "slide (e.g. 'redesign this slide entirely')."
                            ),
                        },
                    },
                    "required": ["reason", "suggested_stage"],
                },
            },
        }
        system_prompt = (
            f"You are revising an SVG for slide {spec['slide_idx']} slot "
            f"{spec['slot_id']!r} (aspect_ratio={spec['aspect_ratio']!r}).\n"
            f"Current SVG:\n\n```xml\n{current_svg}\n```\n\n"
            f"Call set_svg with the complete new <svg> markup. Keep the same "
            f"root id and data-slot attributes. Only use supported SVG tags.\n"
            f"Before calling set_svg, write ONE short sentence in your text "
            f"response describing what you changed (e.g. 'Removed the "
            f"full-bleed background rect and shifted icons right'). This "
            f"sentence is shown in the reviewer's chat history so they can "
            f"scan what each edit did without re-reading the whole SVG.\n\n"
            f"SCOPE — this editor only edits one SVG slot on slide "
            f"{spec['slide_idx']}. If the user asks for any of the following, "
            f"call reject_request instead of set_svg (do NOT attempt to "
            f"encode the change in the SVG markup):\n"
            f"  - add / insert / delete / duplicate / reorder slides\n"
            f"  - change the deck-wide theme, colour palette, or fonts\n"
            f"  - redesign the whole slide (layout, all icons, all text)\n"
            f"Normal per-slot edits — recolouring, restyling, swapping an "
            f"icon, adjusting this slot's viewBox — should call set_svg as "
            f"usual. For per-slot changes use suggested_stage='slides' only "
            f"when the user clearly wants the whole slide rebuilt.\n"
            f"You MUST call one of the two tools on every response. Never "
            f"reply with text only — if you cannot apply the request to "
            f"this SVG slot, call reject_request."
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # Retry loop: feed validator / parse errors back to the LLM so it
        # can correct the SVG (drop full-bleed rect, swap unsupported tag,
        # match viewBox, fix malformed JSON). Mirrors the pipeline's
        # _acquire_svg pattern (image_acquirer.py) where tool-dispatch
        # errors surface as next-turn context. Network errors still
        # short-circuit — only validation / parse failures retry. Out-of-
        # scope rejections also short-circuit — the LLM made a deliberate
        # routing decision and retrying would only produce a different
        # phrasing of the same.
        #
        # "No tool call" is also retryable: when
        # ``disable_required_tool_choice`` silently drops
        # ``tool_choice="required"`` (DeepSeek thinking-mode requirement),
        # the LLM is free to reply with text only. We nudge it once with
        # explicit instructions; if it still refuses, we treat that as
        # an out_of_scope signal (most likely deck-level) and route to
        # the guidance card rather than surfacing a useless "LLM did
        # not call X" error.
        last_error: Optional[str] = None
        last_no_tool_content: Optional[str] = None
        for _ in range(_LLM_EDIT_MAX_RETRIES + 1):
            try:
                resp = await llm.chat_with_tools(
                    messages=messages,
                    tools=[tool_schema, reject_schema],
                    temperature=max(0.0, min(1.0, config.temperature)),
                    max_tokens=config.svg_generator_max_tokens or 4096,
                    tool_choice="required",
                )
            except Exception as exc:
                return EditResult(ok=False, error=f"LLM call failed: {exc}")

            if not resp.tool_calls:
                last_no_tool_content = (resp.content or "").strip()
                messages.append(resp.assistant_message)
                messages.append({
                    "role": "user",
                    "content": (
                        "You must respond by calling ONE of the two tools:\n"
                        "  - set_svg: normal per-slot SVG edit\n"
                        "  - reject_request: the request is deck-level "
                        "(add/remove slides, theme change, redesign whole "
                        "slide, ...) and cannot be applied to this single "
                        "SVG slot\n"
                        "Do not reply with text only. Call a tool now."
                    ),
                })
                continue
            call = resp.tool_calls[0]

            # Out-of-scope rejection: the LLM recognised the request as
            # deck-level (add/remove slides, theme change, ...). Return a
            # structured EditResult so the server can route it to the
            # guidance card instead of a plain error.
            if call.name == "reject_request":
                args, parse_err = call.parse_arguments_strict()
                if parse_err:
                    return EditResult(
                        ok=False,
                        error=(
                            f"LLM tried to reject the request but its tool "
                            f"arguments were malformed: {parse_err}"
                        ),
                    )
                suggested = args.get("suggested_stage", "outline")
                if suggested not in ("outline", "slides"):
                    suggested = "outline"
                if suggested == "outline":
                    guidance = (
                        "This request affects the whole deck and can't be "
                        "applied to a single SVG. Switch to the Outline "
                        "stage to make structural changes (add/remove/"
                        "reorder slides or change the deck-wide theme)."
                    )
                else:
                    guidance = (
                        "This request implies rebuilding the whole slide, "
                        "which is out of scope for a single SVG edit. "
                        "Switch to the Slides stage to re-generate the "
                        "slide's HTML."
                    )
                return EditResult(
                    ok=False,
                    error=args.get("reason", "Request is out of scope."),
                    kind="out_of_scope",
                    suggested_stage=suggested,
                    guidance=guidance,
                    assistant_msg=(resp.content or "").strip()
                    or args.get("reason"),
                )

            args, parse_err = call.parse_arguments_strict()
            if parse_err:
                # Malformed JSON args (trailing commas, unquoted keys, etc.).
                # Push back as a tool result so the LLM re-emits valid JSON
                # on the next turn. MUST be role="tool" with the matching
                # tool_call_id — OpenAI rejects messages where an assistant
                # tool_calls block is followed by anything else (user msg,
                # another assistant turn, etc.) with HTTP 400 "insufficient
                # tool messages following tool_calls message".
                messages.append(resp.assistant_message)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": (
                        f"Could not parse your tool arguments: {parse_err}\n\n"
                        f"Call set_svg again with valid JSON arguments."
                    ),
                })
                last_error = parse_err
                continue

            svg = args.get("svg", "")
            result = self._apply(target, svg, state, config)
            if result.ok:
                # tool_choice="required" lets LLMs emit tool_call without
                # text content; the system prompt asks for one descriptive
                # sentence, but the fallback covers models that ignore it.
                result.assistant_msg = (resp.content or "").strip() or "Updated SVG markup."
                return result

            # Validation failed — feed the validator's error back as a tool
            # result. Same role="tool" contract as the parse-error branch.
            # Mirrors the pipeline's _acquire_svg pattern (image_acquirer.py)
            # where tool-dispatch errors surface as next-turn context.
            messages.append(resp.assistant_message)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": (
                    f"SVG validation failed:\n{result.error}\n\n"
                    f"Fix the issue and call set_svg again with the complete "
                    f"revised SVG markup."
                ),
            })
            last_error = result.error

        # Retries exhausted. Two cases (mirrors slide_editor):
        #   1. The LLM kept replying with text (no tool call) → treat
        #      as out_of_scope; route to guidance card.
        #   2. The LLM kept producing invalid SVG → surface last error.
        if last_no_tool_content is not None:
            return EditResult(
                ok=False,
                error=last_no_tool_content
                or "The LLM could not apply this request to the SVG slot.",
                kind="out_of_scope",
                suggested_stage="outline",
                guidance=(
                    "The model couldn't apply this request to a single "
                    "SVG slot. This usually means the request is deck-"
                    "level (add/remove slides, theme change, redesign "
                    "whole slide, ...). Switch to the Outline stage to "
                    "make structural changes, or the Slides stage to "
                    "rebuild this slide's HTML."
                ),
                assistant_msg=last_no_tool_content,
            )

        return EditResult(
            ok=False,
            error=(
                f"After {_LLM_EDIT_MAX_RETRIES + 1} attempts the LLM still "
                f"produced invalid SVG. Last error: {last_error}"
            ),
        )


def _unified_diff_xml(old: str, new: str) -> str:
    """Lightweight line diff for XML/markup."""
    if not old or not new:
        return ""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))
    return "\n".join(diff) if diff else ""
