"""Stage 2: Outline Planner node.

Single LLM call with the `define_outline` tool. Loops a few times to let
the LLM correct invalid output.

The progressive path (Stage 2a + 2b) splits this into two nodes defined
in this same module: run_structure_planner + run_slide_detail_generator.
The orchestrator picks the progressive path and falls back to the
one-shot run_outline_planner on failure.
"""

from __future__ import annotations

import sys
from typing import Callable, List, Optional

from shuttleslide.agent.llm import LLMClient
from shuttleslide.agent.llm.tool_call import LLMResponseEvent
from shuttleslide.agent.prompts import (
    build_outline_planner_prompt,
    build_slide_detail_generator_prompt,
    build_structure_planner_prompt,
)
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry


async def run_outline_planner(
    state: AgentState,
    llm: LLMClient,
    tools: ToolRegistry,
    temperature: float = 0.7,
    max_tokens: Optional[int] = 4096,
    max_attempts: int = 3,
    on_llm_response: Optional[Callable[[LLMResponseEvent], None]] = None,
) -> List[dict]:
    """Run the outline planner stage. Returns the outline list."""
    if state.outline:
        return state.outline

    system_prompt = build_outline_planner_prompt(
        topic=state.topic,
        style_hint=state.style_hint,
        target_count=state.target_count,
        theme=state.theme,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Define the slide outline now using the define_outline tool."},
    ]
    tool_schemas = tools.openai_schema_for("outline_builder")

    for attempt in range(max_attempts):
        response = await llm.chat_with_tools(
            messages=messages,
            tools=tool_schemas,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice="required",
        )
        messages.append(response.assistant_message)

        if on_llm_response is not None:
            on_llm_response(
                LLMResponseEvent(
                    stage="outline_planner",
                    iteration=attempt + 1,
                    max_iterations=max_attempts,
                    content=response.content,
                    reasoning=response.reasoning,
                    tool_calls=response.tool_calls,
                    usage=response.usage,
                )
            )

        if not response.has_tool_calls:
            state.add_warning(f"outline_planner attempt {attempt + 1}: no tool call, retrying")
            messages.append(
                {"role": "user", "content": "You must call the define_outline tool."}
            )
            continue

        for call in response.tool_calls:
            result = await tools.dispatch(call, state=state)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result.summary}
            )
            if not result.ok:
                state.add_warning(
                    f"outline_planner attempt {attempt + 1}: {call.name} failed — {result.error}"
                )

        if state.outline:
            return state.outline

        messages.append(
            {
                "role": "user",
                "content": "The previous call was invalid. Please correct it and call define_outline again.",
            }
        )

    raise RuntimeError(
        f"outline_planner failed to produce a valid outline after {max_attempts} attempts; "
        f"warnings: {state.warnings[-3:]}"
    )


# ---------------------------------------------------------------------------
# Progressive outline (Stage 2a + 2b)
#
# Two nodes that together replace run_outline_planner. The orchestrator
# prefers this path and falls back to run_outline_planner on exception.
# Each node is self-contained and can be unit-tested in isolation by
# stubbing the LLM.
# ---------------------------------------------------------------------------


async def run_structure_planner(
    state: AgentState,
    llm: LLMClient,
    tools: ToolRegistry,
    temperature: float = 0.7,
    max_tokens: Optional[int] = 4096,
    max_attempts: int = 3,
    on_llm_response: Optional[Callable[[LLMResponseEvent], None]] = None,
) -> List[dict]:
    """Stage 2a: plan deck skeleton via the define_skeleton tool.

    On success, state.deck_skeleton is populated and state.outline is
    initialized with lightweight skeleton entries (no key_points /
    images). Stage 2b (run_slide_detail_generator) upgrades each slot
    in-place.

    Raises RuntimeError when all retries fail; the orchestrator catches
    and falls back to run_outline_planner.
    """
    if state.deck_skeleton is not None and state.outline:
        # Idempotent: skeleton already produced (e.g. caller re-runs).
        return state.outline

    system_prompt = build_structure_planner_prompt(
        topic=state.topic,
        style_hint=state.style_hint,
        target_count=state.target_count,
        theme=state.theme,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": "Plan the deck skeleton now using the define_skeleton tool.",
        },
    ]
    tool_schemas = tools.openai_schema_for("skeleton_builder")

    for attempt in range(max_attempts):
        response = await llm.chat_with_tools(
            messages=messages,
            tools=tool_schemas,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice="required",
        )
        messages.append(response.assistant_message)

        if on_llm_response is not None:
            on_llm_response(
                LLMResponseEvent(
                    stage="structure_planner",
                    iteration=attempt + 1,
                    max_iterations=max_attempts,
                    content=response.content,
                    reasoning=response.reasoning,
                    tool_calls=response.tool_calls,
                    usage=response.usage,
                )
            )

        if not response.has_tool_calls:
            state.add_warning(
                f"structure_planner attempt {attempt + 1}: no tool call, retrying"
            )
            messages.append(
                {"role": "user", "content": "You must call the define_skeleton tool."}
            )
            continue

        for call in response.tool_calls:
            result = await tools.dispatch(call, state=state)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result.summary}
            )
            if not result.ok:
                state.add_warning(
                    f"structure_planner attempt {attempt + 1}: {call.name} "
                    f"failed — {result.error}"
                )

        if state.deck_skeleton is not None and state.outline:
            return state.outline

        messages.append(
            {
                "role": "user",
                "content": "The previous call was invalid. Please correct it and call define_skeleton again.",
            }
        )

    raise RuntimeError(
        f"structure_planner failed to produce a skeleton after {max_attempts} "
        f"attempts; warnings: {state.warnings[-3:]}"
    )


async def run_slide_detail_generator(
    state: AgentState,
    llm: LLMClient,
    tools: ToolRegistry,
    temperature: float = 0.7,
    max_tokens: Optional[int] = 2048,
    max_attempts_per_slide: int = 10,
    on_llm_response: Optional[Callable[[LLMResponseEvent], None]] = None,
) -> List[dict]:
    """Stage 2b: enrich each slide with key_points + images (N LLM calls).

    Iterates over state.outline (populated by run_structure_planner) and
    calls define_slide_detail for each slot. Per-slide retry with
    graceful degradation: a slide that fails all attempts is left with
    a placeholder key_point so downstream stages don't crash.

    The per-slide prompt carries the deck thesis + group structure + a
    summary of previously-generated slides' layouts so the LLM can
    actively vary its layout. This is the main quality lever that the
    one-shot planner cannot offer.
    """
    if not state.outline:
        raise RuntimeError(
            "slide_detail_generator requires state.outline to be populated "
            "by run_structure_planner first"
        )

    tool_schemas = tools.openai_schema_for("slide_detail_builder")
    total = len(state.outline)

    for slide_index in range(total):
        skeleton = state.outline[slide_index]
        # Idempotent: skip slides that already have detail (e.g. retry
        # after partial failure).
        if skeleton.get("_detail_filled"):
            continue

        # Snapshot prev slides' enriched state for layout diversity.
        prev_slides = [dict(s) for s in state.outline[:slide_index]]
        prompt = build_slide_detail_generator_prompt(
            slide_index=slide_index,
            total=total,
            skeleton=skeleton,
            prev_slides=prev_slides,
            deck_skeleton=state.deck_skeleton,
            topic=state.topic,
            theme=state.theme,
        )
        messages = [
            {"role": "system", "content": prompt},
            {
                "role": "user",
                "content": (
                    f"Fill in the detail for slide {slide_index + 1} of {total} "
                    f"using the define_slide_detail tool."
                ),
            },
        ]

        succeeded = False
        for attempt in range(max_attempts_per_slide):
            response = await llm.chat_with_tools(
                messages=messages,
                tools=tool_schemas,
                temperature=temperature,
                max_tokens=max_tokens,
                tool_choice="required",
            )
            messages.append(response.assistant_message)

            if on_llm_response is not None:
                on_llm_response(
                    LLMResponseEvent(
                        stage="slide_detail_generator",
                        iteration=attempt + 1,
                        max_iterations=max_attempts_per_slide,
                        content=response.content,
                        reasoning=response.reasoning,
                        tool_calls=response.tool_calls,
                        usage=response.usage,
                    )
                )

            if not response.has_tool_calls:
                messages.append(
                    {
                        "role": "user",
                        "content": "You must call the define_slide_detail tool.",
                    }
                )
                continue

            for call in response.tool_calls:
                result = await tools.dispatch(call, state=state)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": result.summary,
                    }
                )
                if not result.ok:
                    state.add_warning(
                        f"slide_detail_generator slide {slide_index + 1} "
                        f"attempt {attempt + 1}: {call.name} failed — {result.error}"
                    )

            # define_slide_detail flips _detail_filled on success.
            if state.outline[slide_index].get("_detail_filled"):
                succeeded = True
                break

            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"The previous call for slide {slide_index + 1} was "
                        f"invalid. Correct it and call define_slide_detail again."
                    ),
                }
            )

        if not succeeded:
            # Graceful degradation: leave the skeleton fields in place,
            # inject a placeholder key_point so slide_builder has something
            # to render. Image specs remain empty (image_acquirer is a no-op
            # for this slide). This keeps the deck shippable even when one
            # slide's detail call fails.
            print(
                f"[shuttleslide] warning: slide {slide_index + 1} detail "
                f"generation failed after {max_attempts_per_slide} attempts; "
                f"using skeleton fallback",
                file=sys.stderr,
            )
            state.add_warning(
                f"slide {slide_index + 1} detail generation failed; skeleton fallback"
            )
            state.outline[slide_index]["key_points"] = [
                f"({state.outline[slide_index].get('purpose', 'content')})"
            ]
            state.outline[slide_index]["images"] = []
            state.outline[slide_index]["_detail_filled"] = True

    return state.outline
