"""Stage 3: Slide Builder node.

For each slide in the outline, run a bounded tool-call loop where the LLM
authors the slide's inner HTML via `set_free_form_html`. Loop ends when
the LLM calls `finish_slide` or when `max_iterations` is hit.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from shuttleslide.agent.llm import LLMClient
from shuttleslide.agent.llm.tool_call import LLMResponseEvent
from shuttleslide.agent.prompts import build_slide_builder_prompt
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry
from shuttleslide.html_to_pptx.schema import SlideDSL

if TYPE_CHECKING:
    pass


async def run_slide_builder(
    state: AgentState,
    llm: LLMClient,
    tools: ToolRegistry,
    slide_index: int,
    temperature: float = 0.6,
    max_tokens: Optional[int] = 4096,
    max_iterations: int = 6,
    on_llm_response: Optional[Callable[[LLMResponseEvent], None]] = None,
    output_dir: Optional[Path] = None,
) -> SlideDSL:
    """Build a single slide via tool calls.

    The slide is appended to `state.slides` and also returned. The per-slide
    message buffer is stored on `state.current_slide_messages`.
    """
    if slide_index >= len(state.outline):
        raise IndexError(f"slide_index {slide_index} out of range for outline")

    outline = state.outline[slide_index]
    total = len(state.outline)
    # Pre-generated SVGs for this slide (slide_idx → {slot_id: svg_markup}).
    slide_images = state.slide_images.get(slide_index, {})
    # Reverse-lookup image_type per slot_id from the outline. Mirrors the
    # same lookup in build_slide_builder_prompt so finish_slide can apply
    # the decorative-vs-load-bearing rule consistently.
    image_types = {
        img["slot_id"]: img.get("image_type", "illustration")
        for img in outline.get("images", [])
        if isinstance(img, dict) and "slot_id" in img
    }

    # Reset the per-slide scratch buffer.
    system_prompt = build_slide_builder_prompt(
        theme=state.theme,
        outline=outline,
        slide_index=slide_index + 1,
        total_count=total,
        slide_images=slide_images,
    )
    state.current_slide_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": (
            "Author the HTML for this slide now using `set_free_form_html`, "
            "then call `finish_slide`. Follow the HTML AUTHORING GUIDE "
            "in the system prompt."
        )},
    ]

    # Create the slide and append immediately so tools can mutate it in place.
    # Pure free-form pipeline: layout is always "free_form".
    slide = SlideDSL(layout="free_form")
    # Keep state.slides aligned with outline indices: pad if needed.
    while len(state.slides) <= slide_index:
        state.slides.append(None)  # type: ignore[arg-type]
    state.slides[slide_index] = slide

    tool_schemas = tools.openai_schema_for("slide_builder")

    for iteration in range(max_iterations):
        response = await llm.chat_with_tools(
            messages=state.current_slide_messages,
            tools=tool_schemas,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        state.current_slide_messages.append(response.assistant_message)

        if on_llm_response is not None:
            on_llm_response(
                LLMResponseEvent(
                    stage="slide_builder",
                    iteration=iteration + 1,
                    max_iterations=max_iterations,
                    slide_index=slide_index + 1,
                    slide_total=total,
                    content=response.content,
                    reasoning=response.reasoning,
                    tool_calls=response.tool_calls,
                    usage=response.usage,
                )
            )

        if not response.has_tool_calls:
            # LLM stopped without calling finish_slide — accept partial slide.
            state.add_warning(
                f"slide {slide_index + 1}: LLM stopped after {iteration + 1} "
                f"iterations without calling finish_slide"
            )
            break

        finish_called = False
        for call in response.tool_calls:
            result = await tools.dispatch(
                call,
                slide=slide,
                theme=state.theme,
                slide_images=slide_images,
                image_types=image_types,
                # Pass the LLM's latest reasoning so finish_slide can
                # recognize an explicitly-declared decorative SVG omission
                # (the escape hatch that prevents infinite size-retry loops).
                last_assistant_reasoning=response.reasoning or "",
            )
            state.current_slide_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result.summary,
                }
            )
            # Only break when finish_slide actually succeeds. The
            # finish_slide handler rejects when set_free_form_html has not
            # set the HTML yet (or set_free_form_html's CSS lint failed in
            # the same response). If we broke regardless of result.ok, an
            # earlier tool call failing in the same response would leave
            # the slide permanently empty and the LLM would never see the
            # failure feedback to retry.
            if call.name == "finish_slide" and result.ok:
                finish_called = True
            # Surface tool failures to stdout so the user can see WHY a
            # slide went empty (the on_llm_response callback fires before
            # dispatch, so failures are otherwise invisible in the log).
            # Successes are silent — the LLM-facing summary already covers
            # them, and printing every success would drown the signal.
            if not result.ok:
                preview = result.summary if len(result.summary) <= 400 else (
                    result.summary[:397] + "..."
                )
                print(
                    f"  [slide {slide_index + 1} iter {iteration + 1}] "
                    f"tool {call.name} FAILED: {preview}",
                    flush=True,
                )

        if finish_called:
            break
    else:
        # Loop exited via max_iterations without finish_slide.
        has_html = "html" in slide.slots
        state.add_warning(
            f"slide {slide_index + 1}: reached max_iterations={max_iterations} "
            f"without finish_slide; "
            f"{'HTML was set' if has_html else 'no HTML produced'}"
        )
        # Dump the last-tried HTML + tool failure messages to disk so the
        # user can diagnose why the loop didn't converge. Common causes:
        # CSS lint failures, HTML > 12000 chars, or a regression like the
        # svg_file/image_file payload recognition bug. Without this dump
        # the failure is invisible — only a warning string reaches stdout.
        _dump_failure_artifacts(
            output_dir=output_dir,
            slide_index=slide_index,
            html=slide.slots.get("html"),
            messages=state.current_slide_messages,
        )

    return slide


def _dump_failure_artifacts(
    *,
    output_dir: Optional[Path],
    slide_index: int,
    html: Optional[str],
    messages: list,
) -> None:
    """Write the last HTML + recent tool failures to ``{output_dir}/debug/``.

    Best-effort — any IO error is swallowed so a diagnostics bug can't
    mask the underlying pipeline error that triggered the dump.
    """
    if output_dir is None:
        return
    try:
        debug_dir = output_dir / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        slide_n = slide_index + 1
        if html:
            (debug_dir / f"slide_{slide_n}_failed.html").write_text(
                html, encoding="utf-8"
            )
        # Collect the last few tool-role messages (failure summaries from
        # set_free_form_html / finish_slide). They are the diagnosis signal.
        tool_msgs = [
            m.get("content", "")
            for m in messages
            if isinstance(m, dict) and m.get("role") == "tool"
        ]
        if tool_msgs:
            # Tail to keep the file readable — last 8 tool messages is
            # typically enough to see the repeating failure pattern.
            tail = tool_msgs[-8:]
            (debug_dir / f"slide_{slide_n}_failed.txt").write_text(
                "\n\n---\n\n".join(tail), encoding="utf-8"
            )
    except OSError:
        # Diagnostics must never shadow the real error.
        return
