"""Stage 2.5: Image Acquirer node.

Reads every image spec from each slide's outline and produces one image
per spec via one of two paths:

  - source_type="svg"  (default): one LLM call per spec with the
    `set_svg` tool. The result is stored as a typed payload and fed
    back into the slide-builder prompt as a pre-generated snippet.

  - source_type="web": delegate to the image_sources subpackage, which
    searches a provider, downloads candidates, and (optionally) asks a
    VLM to verify each against the spec's description. On failure, the
    spec is downgraded to source_type="svg" and re-acquired via the SVG
    path. The fallback guarantees the slide always gets an image (as
    long as SVG generation succeeds) — web path is best-effort.

Iteration model mirrors outline_planner / theme_designer: one bounded
inner loop per spec, retry on validation failure (SVG path only).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from shuttleslide.agent.llm import LLMClient
from shuttleslide.agent.llm.tool_call import LLMResponseEvent
from shuttleslide.agent.prompts import build_svg_generator_prompt
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry


async def run_image_acquirer(
    state: AgentState,
    llm: LLMClient,
    tools: ToolRegistry,
    temperature: float = 0.7,
    max_tokens: Optional[int] = 4096,
    svg_max_tokens: Optional[int] = None,
    max_attempts: int = 3,
    web_search_provider: Optional[Any] = None,
    vlm_verifier: Optional[Any] = None,
    browser_manager: Optional[Any] = None,
    output_dir: Optional[Any] = None,
    on_llm_response: Optional[Callable[[LLMResponseEvent], None]] = None,
) -> Dict[int, Dict[str, Dict[str, Any]]]:
    """Generate one image per spec across every slide's outline.

    Returns ``state.slide_images``. Idempotent: skips specs that already
    have a payload stored (e.g. on a re-run after a downstream failure).

    Parameters
    ----------
    web_search_provider
        Optional image search provider (ImageSearchProvider protocol).
        Required for source_type="web" with a search-query source_ref;
        if None, web specs fall back to svg with a warning.
    vlm_verifier
        Optional VLM verifier (callable returning {"match": bool, ...}).
        If None, web candidates are accepted without verification (not
        recommended for production but useful for tests).
    browser_manager
        Optional shared BrowserManager. Used by the bing_web scraping
        provider and the URL-screenshot path. The caller owns its
        lifecycle (start before / stop after this call).
    output_dir
        Directory where web image files will be persisted (under an
        ``images/`` subdirectory). Required for source_type="web" —
        the file-externalized model needs a target location so the
        slide HTML can reference the image via a short relative URL.
        When None, web specs fall back to svg with a warning.
    svg_max_tokens
        Optional override of ``max_tokens`` for the SVG generation LLM
        call only. SVG markup can run several thousand characters even
        for simple illustrations, and thinking-mode models (DeepSeek)
        spend additional tokens on chain-of-thought before emitting the
        tool call — the default ``max_tokens=4096`` can truncate the
        tool-call arguments mid-JSON, which ``parse_arguments`` silently
        swallows into an empty dict, surfacing as a confusing
        ``"svg must be a non-empty string"`` error. When None, inherits
        ``max_tokens``. The AgentConfig default for
        ``svg_generator_max_tokens`` is 16384.
    """
    specs = _collect_specs(state)
    if not specs:
        # No images requested — nothing to do. Make this explicit in logs.
        return state.slide_images

    for slide_idx, slot_id, spec in specs:
        # Skip if already generated (idempotency).
        if state.slide_images.get(slide_idx, {}).get(slot_id):
            continue

        source_type = spec.get("source_type", "svg")

        if source_type == "web":
            web_ok = await _try_acquire_web(
                state=state,
                slide_idx=slide_idx,
                slot_id=slot_id,
                spec=spec,
                web_search_provider=web_search_provider,
                vlm_verifier=vlm_verifier,
                browser_manager=browser_manager,
                output_dir=output_dir,
            )
            if web_ok:
                continue
            # Downgrade: re-acquire as svg. The slide still gets art; it
            # just loses the photorealistic intent. Warn so the user
            # knows their web spec regressed.
            state.add_warning(
                f"image_acquirer slide {slide_idx + 1} slot {slot_id!r}: "
                f"web acquisition failed, falling back to svg"
            )
            spec = {**spec, "source_type": "svg"}

        # SVG path (also the fallback destination for failed web specs).
        await _acquire_svg(
            state=state,
            llm=llm,
            tools=tools,
            slide_idx=slide_idx,
            slot_id=slot_id,
            spec=spec,
            temperature=temperature,
            max_tokens=max_tokens,
            svg_max_tokens=svg_max_tokens,
            max_attempts=max_attempts,
            on_llm_response=on_llm_response,
            output_dir=output_dir,
        )

    # Clear the scratch pointer on exit so a stray set_svg call later in
    # the pipeline doesn't silently overwrite state.
    state.current_svg_spec = None
    return state.slide_images


async def _acquire_svg(
    *,
    state: AgentState,
    llm: LLMClient,
    tools: ToolRegistry,
    slide_idx: int,
    slot_id: str,
    spec: Dict[str, Any],
    temperature: float,
    max_tokens: Optional[int],
    svg_max_tokens: Optional[int],
    max_attempts: int,
    on_llm_response: Optional[Callable[[LLMResponseEvent], None]],
    output_dir: Optional[Any] = None,
) -> None:
    """Drive the LLM through max_attempts retries to produce one SVG.

    On success, the set_svg tool writes the payload into
    ``state.slide_images[slide_idx][slot_id]``. On failure, records an
    error and leaves the slot unset — the slide-builder's "must use
    every declared image" check will then flag the slide for retry.

    ``output_dir`` is the on-disk location where the SVG file is
    persisted (under the ``svgs/`` subdirectory). Passed through to
    ``set_svg`` via the dispatch ctx. Required — set_svg will fail the
    tool call if missing, triggering retry.

    ``svg_max_tokens`` overrides ``max_tokens`` for the chat_with_tools
    call. SVG markup is denser than typical chat output and may need
    more headroom; see run_image_acquirer's docstring for the full
    rationale. When None, ``max_tokens`` is used.
    """
    # svg_max_tokens takes precedence over the generic max_tokens for
    # this stage only. Both default to 4096 at the AgentConfig level,
    # but svg_generator_max_tokens defaults to 16384 there — so the
    # override typically relaxes the cap.
    effective_max_tokens = svg_max_tokens if svg_max_tokens is not None else max_tokens
    tool_schemas = tools.openai_schema_for("svg_builder")

    # Make the spec identifiable to the set_svg tool. The tool reads
    # this to know which (slide_idx, slot_id) to store under.
    state.current_svg_spec = {"slide_idx": slide_idx, **spec}

    system_prompt = build_svg_generator_prompt(spec, state.theme)
    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": (
                f"Draw the {spec['image_type']} for slot {slot_id!r} now "
                f"using the set_svg tool."
            ),
        },
    ]

    produced = False
    for attempt in range(max_attempts):
        response = await llm.chat_with_tools(
            messages=messages,
            tools=tool_schemas,
            temperature=temperature,
            max_tokens=effective_max_tokens,
            tool_choice="required",
        )
        messages.append(response.assistant_message)

        if on_llm_response is not None:
            on_llm_response(
                LLMResponseEvent(
                    stage="svg_generator",
                    iteration=attempt + 1,
                    max_iterations=max_attempts,
                    content=response.content,
                    reasoning=response.reasoning,
                    tool_calls=response.tool_calls,
                    usage=response.usage,
                    slide_index=slide_idx + 1,
                    slide_total=spec.get("_slide_total"),
                )
            )

        if not response.has_tool_calls:
            state.add_warning(
                f"svg_generator slide {slide_idx + 1} slot {slot_id!r} "
                f"attempt {attempt + 1}: no tool call, retrying"
            )
            messages.append(
                {"role": "user", "content": "You must call the set_svg tool."}
            )
            continue

        for call in response.tool_calls:
            result = await tools.dispatch(call, state=state, output_dir=output_dir)
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": result.summary}
            )
            if not result.ok:
                state.add_warning(
                    f"svg_generator slide {slide_idx + 1} slot {slot_id!r} "
                    f"attempt {attempt + 1}: {call.name} failed — {result.error}"
                )

        if state.slide_images.get(slide_idx, {}).get(slot_id):
            produced = True
            break

        messages.append(
            {
                "role": "user",
                "content": (
                    "The previous SVG was invalid. Fix the issue and call "
                    "set_svg again with a corrected SVG."
                ),
            }
        )

    if not produced:
        # Soft-fail: record the miss and let the slide-builder proceed
        # without this image. The slide-builder's "must use every
        # declared image" check will then flag the slide for retry.
        state.add_error(
            f"svg_generator: could not produce valid SVG for slide "
            f"{slide_idx + 1} slot {slot_id!r} after {max_attempts} attempts"
        )

    # Clear the scratch pointer after each spec so the next iteration
    # (or the next spec in the outer loop) starts clean. The final clear
    # in run_image_acquirer is defensive.
    state.current_svg_spec = None


async def _try_acquire_web(
    *,
    state: AgentState,
    slide_idx: int,
    slot_id: str,
    spec: Dict[str, Any],
    web_search_provider: Optional[Any],
    vlm_verifier: Optional[Any],
    browser_manager: Optional[Any],
    output_dir: Optional[Any] = None,
) -> bool:
    """Acquire one image from the web path.

    Returns True on success (payload stored in state.slide_images), False
    on failure (caller falls back to svg).

    MVP implementation: delegates to the image_sources subpackage. If
    the subpackage or required providers are unavailable, returns False
    so the caller falls back to svg gracefully — web path is opt-in.
    """
    try:
        from shuttleslide.agent.nodes.image_sources import acquire_web_image
    except ImportError:
        # image_sources subpackage not yet wired in. Fall back to svg.
        return False

    return await acquire_web_image(
        state=state,
        slide_idx=slide_idx,
        slot_id=slot_id,
        spec=spec,
        web_search_provider=web_search_provider,
        vlm_verifier=vlm_verifier,
        browser_manager=browser_manager,
        output_dir=output_dir,
    )


def _collect_specs(state: AgentState) -> List[tuple[int, str, Dict[str, Any]]]:
    """Flatten every (slide_idx, slot_id, spec) tuple from the outline."""
    total = len(state.outline)
    out: List[tuple[int, str, Dict[str, Any]]] = []
    for slide_idx, slide in enumerate(state.outline):
        for spec in slide.get("images", []) or []:
            # Attach _slide_total once so the LLMResponseEvent carries it.
            spec_copy = dict(spec)
            spec_copy["_slide_total"] = total
            out.append((slide_idx, spec_copy["slot_id"], spec_copy))
    return out


# Backward-compat alias. Existing callers that imported run_svg_generator
# keep working; the canonical name is run_image_acquirer.
run_svg_generator = run_image_acquirer
