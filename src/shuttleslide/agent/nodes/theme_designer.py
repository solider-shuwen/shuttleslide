"""Stage 1: Theme Designer node.

Single LLM call with the `define_theme` tool. The LLM must call this tool
once with all theme fields. We loop a couple of times to allow the LLM to
self-correct if its first call is invalid.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from shuttleslide.agent.llm import LLMClient
from shuttleslide.agent.llm.tool_call import LLMResponseEvent
from shuttleslide.agent.prompts import build_theme_designer_prompt
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry


async def run_theme_designer(
    state: AgentState,
    llm: LLMClient,
    tools: ToolRegistry,
    temperature: float = 0.7,
    max_tokens: Optional[int] = 2048,
    max_attempts: int = 3,
    on_llm_response: Optional[Callable[[LLMResponseEvent], None]] = None,
) -> Dict[str, Any]:
    """Run the theme designer stage. Returns the theme dict."""
    if state.theme:
        # Already populated (e.g. caller injected one); skip.
        return state.theme

    system_prompt = build_theme_designer_prompt(
        state.topic,
        state.style_hint,
        canvas_width_px=state.canvas_width_px,
        canvas_height_px=state.canvas_height_px,
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "Define the theme now using the define_theme tool."},
    ]
    tool_schemas = tools.openai_schema_for("theme_builder")

    for attempt in range(max_attempts):
        response = await llm.chat_with_tools(
            messages=messages,
            tools=tool_schemas,
            temperature=temperature,
            max_tokens=max_tokens,
            tool_choice="required",  # force a tool call on the first turn
        )
        messages.append(response.assistant_message)

        if on_llm_response is not None:
            on_llm_response(
                LLMResponseEvent(
                    stage="theme_designer",
                    iteration=attempt + 1,
                    max_iterations=max_attempts,
                    content=response.content,
                    reasoning=response.reasoning,
                    tool_calls=response.tool_calls,
                    usage=response.usage,
                )
            )

        if not response.has_tool_calls:
            state.add_warning(f"theme_designer attempt {attempt + 1}: no tool call, retrying")
            messages.append(
                {"role": "user", "content": "You must call the define_theme tool."}
            )
            continue

        # Execute the (single expected) tool call.
        for call in response.tool_calls:
            result = await tools.dispatch(call, state=state)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result.summary}
            )
            if not result.ok:
                state.add_warning(
                    f"theme_designer attempt {attempt + 1}: {call.name} failed — {result.error}"
                )

        if state.theme:
            return state.theme

        # Tool failed validation; let the LLM try again.
        messages.append(
            {
                "role": "user",
                "content": "The previous call was invalid. Please correct it and call define_theme again.",
            }
        )

    raise RuntimeError(
        f"theme_designer failed to produce a valid theme after {max_attempts} attempts; "
        f"errors: {state.errors[-3:]}"
    )
