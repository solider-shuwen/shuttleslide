"""SlideEditor — direct + LLM editing for slides' free-form HTML.

Targets ``kind="html"`` (slides[idx].slots["html"]). The editor
mutates the slot in place; the existing renderer picks up the new HTML
on the next render cycle.

Validation
----------
Reuses ``_validate_free_form_html`` from slide_tools.py — same checks
the slide_builder runs (max 12000 chars, no <script>/<iframe>/...,
no inline event handlers, no Tailwind text-size classes, no rem/em).
This is why slide_tools' validator was already a pure function —
editors get the same guarantees as the pipeline for free.

LLM mode
--------
Tool schema mirrors ``set_free_form_html``. The model gets the current
slide HTML in the system prompt and must call the tool with the
revised markup.
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional

from shuttleslide.agent.review.editors.base import (
    EditResult,
    Editor,
    build_llm_client,
    truncate_for_prompt,
)
from shuttleslide.agent.tools.slide_tools import _validate_free_form_html


# How many times apply_llm_edit will retry after a validation / parse
# error before giving up. Same value as svg_editor's _LLM_EDIT_MAX_RETRIES
# so all LLM-aware editors share the same retry budget — a slide HTML
# edit and an SVG edit can both fail validation in similar ways (LLM
# slips in a <script>, on*= handler, Tailwind text-size class, ...).
# Total attempts = 1 + _LLM_EDIT_MAX_RETRIES = 4.
_LLM_EDIT_MAX_RETRIES = 3


class SlideEditor(Editor):
    """Editor for ``kind="html"`` targets.

    ``target.meta["slide_idx"]`` is the index into ``state.slides``;
    the HTML body lives at ``slides[idx].slots["html"]``.
    """

    kind = "html"

    def _resolve_slide(self, target, state):
        idx = target.meta.get("slide_idx")
        if idx is None:
            return None, None
        idx = int(idx)
        if idx < 0 or idx >= len(state.slides):
            return None, None
        slide = state.slides[idx]
        if slide is None or not hasattr(slide, "slots"):
            return None, None
        return slide, idx

    def _apply(self, target, html: str, state) -> EditResult:
        slide, _ = self._resolve_slide(target, state)
        if slide is None:
            return EditResult(
                ok=False,
                error="slide not found in state (it may have been cleared)",
            )
        err = _validate_free_form_html(html)
        if err is not None:
            return EditResult(ok=False, error=err)
        old = slide.slots.get("html", "")
        slide.slots["html"] = html
        return EditResult(
            ok=True,
            new_value=html,
            diff=_unified_diff_html(old, html),
            assistant_msg=None,
        )

    async def apply_direct_edit(
        self, target, new_value, state, config
    ) -> EditResult:
        if not isinstance(new_value, str) or not new_value.strip():
            return EditResult(ok=False, error="HTML must be a non-empty string")
        return self._apply(target, new_value, state)

    async def apply_llm_edit(
        self, target, user_message, history, state, config
    ) -> EditResult:
        slide, _ = self._resolve_slide(target, state)
        if slide is None:
            return EditResult(
                ok=False,
                error="slide not found in state (it may have been cleared)",
            )
        current_html = slide.slots.get("html", "") or target.current_value
        llm = build_llm_client(config)
        slide_idx = target.meta.get("slide_idx")
        tool_schema = {
            "type": "function",
            "function": {
                "name": "set_free_form_html",
                "description": (
                    f"Submit the complete revised HTML for slide {slide_idx}."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "html": {
                            "type": "string",
                            "description": (
                                "Self-contained HTML fragment for the slide "
                                "body. No <script>/<iframe>/<style>/on*=. "
                                "Max 12000 chars."
                            ),
                        },
                    },
                    "required": ["html"],
                },
            },
        }
        reject_schema = {
            "type": "function",
            "function": {
                "name": "reject_request",
                "description": (
                    "Call this if the user's request is deck-level — it "
                    "affects multiple slides, adds/removes/reorders slides, "
                    "or changes the deck-wide theme / fonts / colour palette. "
                    "Do NOT call it for normal per-slide edits (rewording "
                    "title, recolouring an element, adding an icon, etc.)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "reason": {
                            "type": "string",
                            "description": (
                                "Short explanation of why the request is "
                                "deck-level. Shown to the user."
                            ),
                        },
                        "suggested_stage": {
                            "type": "string",
                            "enum": ["outline"],
                            "description": (
                                "The stage the user should switch to. Use "
                                "'outline' for add/remove/reorder/restructure "
                                "operations and deck-wide theme changes."
                            ),
                        },
                    },
                    "required": ["reason", "suggested_stage"],
                },
            },
        }
        system_prompt = (
            f"You are revising the HTML for slide {slide_idx}.\n"
            f"Current HTML:\n\n```html\n{truncate_for_prompt(current_html)}\n```\n\n"
            f"Call set_free_form_html with the complete new HTML. Keep the "
            f"same structure where possible. Forbidden: <script>, <iframe>, "
            f"<style>, <link>, on*= handlers, rem/em units, Tailwind "
            f"text-size classes.\n"
            f"Before calling set_free_form_html, write ONE short sentence "
            f"in your text response describing what you changed. This "
            f"sentence is shown in the reviewer's chat history so they can "
            f"scan what each edit did without re-reading the whole HTML.\n\n"
            f"SCOPE — this editor only edits slide {slide_idx}. If the user "
            f"asks for any of the following, call reject_request instead of "
            f"set_free_form_html (do NOT attempt to encode the change in the "
            f"slide's HTML):\n"
            f"  - add / insert / delete / duplicate / reorder slides\n"
            f"  - change the deck-wide theme, colour palette, or fonts\n"
            f"  - restructure the outline (merge / split slides, rewrite the "
            f"narrative across multiple slides)\n"
            f"Normal per-slide edits — rewording title/body, swapping icons, "
            f"adjusting layout or colours of THIS slide, adding an image to "
            f"THIS slide — should call set_free_form_html as usual.\n"
            f"You MUST call one of the two tools on every response. Never "
            f"reply with text only — if you cannot apply the request to "
            f"this slide, call reject_request."
        )
        messages: List[Dict[str, str]] = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # Retry loop: feed validator / parse errors back to the LLM so it
        # can correct the HTML (drop <script>, remove on*= handler, swap
        # Tailwind text-size class, ...). Mirrors svg_editor.apply_llm_edit's
        # retry pattern. Network errors still short-circuit — only
        # validation / parse failures retry. Out-of-scope rejections also
        # short-circuit — the LLM made a deliberate routing decision and
        # retrying would only produce a different phrasing of the same.
        #
        # "No tool call" is also retryable: when
        # ``disable_required_tool_choice`` silently drops
        # ``tool_choice="required"`` (DeepSeek thinking-mode requirement),
        # the LLM is free to reply with text only — typically when it
        # wants to refuse the request but doesn't know to call
        # reject_request. We nudge it once with explicit instructions;
        # if it still refuses, we treat that as an out_of_scope signal
        # (the LLM consistently refused → request is most likely deck-
        # level) and route to the guidance card rather than surfacing a
        # useless "LLM did not call X" error.
        last_error: Optional[str] = None
        last_no_tool_content: Optional[str] = None
        for _ in range(_LLM_EDIT_MAX_RETRIES + 1):
            try:
                resp = await llm.chat_with_tools(
                    messages=messages,
                    tools=[tool_schema, reject_schema],
                    temperature=max(0.0, min(1.0, config.temperature)),
                    max_tokens=config.max_tokens,
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
                        "  - set_free_form_html: normal per-slide edit\n"
                        "  - reject_request: the request is deck-level "
                        "(add/remove slides, theme change, ...) and cannot "
                        "be applied to this single slide\n"
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
                    # Treat parse failure on a reject call as a normal
                    # error — the user can re-issue the request.
                    return EditResult(
                        ok=False,
                        error=(
                            f"LLM tried to reject the request but its tool "
                            f"arguments were malformed: {parse_err}"
                        ),
                    )
                return EditResult(
                    ok=False,
                    error=args.get("reason", "Request affects the whole deck."),
                    kind="out_of_scope",
                    suggested_stage=args.get("suggested_stage", "outline"),
                    guidance=(
                        "This request affects the whole deck and can't be "
                        "applied to a single slide. Switch to the Outline "
                        "stage to make structural changes (add/remove/"
                        "reorder slides or change the deck-wide theme)."
                    ),
                    assistant_msg=(resp.content or "").strip()
                    or args.get("reason"),
                )

            args, parse_err = call.parse_arguments_strict()
            if parse_err:
                # Malformed JSON args (trailing commas, unquoted keys, ...).
                # Push back as a tool result so the LLM re-emits valid JSON
                # on the next turn. MUST be role="tool" with the matching
                # tool_call_id — OpenAI rejects messages where an assistant
                # tool_calls block is followed by anything else.
                messages.append(resp.assistant_message)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": (
                        f"Could not parse your tool arguments: {parse_err}\n\n"
                        f"Call set_free_form_html again with valid JSON arguments."
                    ),
                })
                last_error = parse_err
                continue

            html = args.get("html", "")
            result = self._apply(target, html, state)
            if result.ok:
                # tool_choice="required" lets LLMs emit tool_call without
                # text content; the system prompt asks for one descriptive
                # sentence, but the fallback covers models that ignore it.
                result.assistant_msg = (resp.content or "").strip() or "Updated slide HTML."
                return result

            # Validation failed — feed the validator's error back as a
            # tool result. Same role="tool" contract as the parse-error
            # branch.
            messages.append(resp.assistant_message)
            messages.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": (
                    f"HTML validation failed:\n{result.error}\n\n"
                    f"Fix the issue and call set_free_form_html again with "
                    f"the complete revised HTML."
                ),
            })
            last_error = result.error

        # Retries exhausted. Two cases:
        #   1. The LLM kept replying with text (no tool call) → most
        #      likely a deck-level request the LLM correctly refused
        #      but failed to route through reject_request. Treat it as
        #      out_of_scope so the user gets the guidance card.
        #   2. The LLM kept producing invalid HTML → surface the last
        #      validation error so the user can fix and retry.
        if last_no_tool_content is not None:
            return EditResult(
                ok=False,
                error=last_no_tool_content
                or "The LLM could not apply this request to the slide.",
                kind="out_of_scope",
                suggested_stage="outline",
                guidance=(
                    "The model couldn't apply this request to a single "
                    "slide. This usually means the request is deck-level "
                    "(add/remove slides, theme change, ...). Switch to "
                    "the Outline stage to make those changes."
                ),
                assistant_msg=last_no_tool_content,
            )

        return EditResult(
            ok=False,
            error=(
                f"After {_LLM_EDIT_MAX_RETRIES + 1} attempts the LLM still "
                f"produced invalid HTML. Last error: {last_error}"
            ),
        )


def _unified_diff_html(old: str, new: str) -> str:
    if not old or not new:
        return ""
    diff = list(difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2))
    return "\n".join(diff) if diff else ""
