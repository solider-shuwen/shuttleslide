"""The five built-in pipeline stages.

Each class lifts the body of the corresponding ``_run_stage_X`` /
``_snapshot_X`` helper out of ``orchestrator.py`` / ``snapshots.py``
so the pipeline can be assembled from a registry rather than a
hardcoded method sequence.

Lift discipline
---------------
Behaviour must match the pre-refactor code byte-for-byte. The only
intentional changes are:

  * Each stage is a self-contained class instead of a method on
    ``AgentOrchestrator``. Deps come in through ``StageContext``.
  * The cache predicate (``is_cached``) no longer checks
    ``state_cache_path``; that's the orchestrator's concern. The
    stage only answers "is my output already in state?".
  * ``build_snapshot`` is a method, not a free function. The
    orchestrator / ``snapshots.build_snapshot`` dispatch into it.

Anchor model
------------
The default registry wires the five stages linearly:

    theme <- outline <- images <- slides <- rendered

Expressed as ``after`` references on each non-first stage. ``before``
is unused by the core set but supported by the registry for pro stages
that need to slot in (e.g. ``ScriptStage(before="rendered")``).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from shuttleslide.agent.nodes.image_acquirer import run_image_acquirer
from shuttleslide.agent.nodes.outline_planner import (
    run_outline_planner,
    run_slide_detail_generator,
    run_structure_planner,
)
from shuttleslide.agent.nodes.slide_builder import run_slide_builder
from shuttleslide.agent.nodes.theme_designer import run_theme_designer
from shuttleslide.agent.review.review_gate import (
    EditTarget,
    StageSnapshot,
)
from shuttleslide.agent.review.stage import StageBase, StageContext
from shuttleslide.agent.state import AgentState
from shuttleslide.html_to_pptx.schema import (
    PresentationDSL,
    SlideDSL,
    ThemeDef,
    dump_presentation,
)


# ---------------------------------------------------------------------------
# Shared helpers (moved verbatim from snapshots.py)
# ---------------------------------------------------------------------------


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _sanitise_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a slide_images payload, dropping anything not JSON-safe.

    Payloads today are already JSON-safe, but a defensive copy guards
    against future fields that might sneak in Path / bytes / dataclass
    values.
    """
    safe: Dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        elif isinstance(v, dict):
            safe[k] = _sanitise_payload(v)
        elif isinstance(v, list):
            safe[k] = [
                _sanitise_payload(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            safe[k] = str(v)
    return safe


# ---------------------------------------------------------------------------
# Stage 1: Theme
# ---------------------------------------------------------------------------


class ThemeStage(StageBase):
    """Designs global theme (colors / fonts / decoration).

    Output: ``state.theme`` dict (shape decided by the LLM).
    """

    name = "theme"
    artifact_kind = "json"
    after = None
    before = None

    async def run(self, ctx: StageContext) -> None:
        await run_theme_designer(
            state=ctx.state,
            llm=ctx.llm,
            tools=ctx.tool_registry,
            temperature=ctx.config.temperature,
            max_tokens=2048,
            on_llm_response=ctx.config.on_llm_response,
        )

    def is_cached(self, state: AgentState) -> bool:
        return bool(state.theme)

    def build_snapshot(self, state: AgentState) -> StageSnapshot:
        theme_dict = dict(state.theme) if state.theme else {}
        targets: List[EditTarget] = []
        if theme_dict:
            targets.append(
                EditTarget(
                    stage="theme",
                    path=("theme",),
                    kind="json",
                    current_value=_json_dumps(theme_dict),
                    meta={"slide_idx": None, "slot_id": None},
                )
            )
        return StageSnapshot(
            stage="theme",
            state_view={"theme": theme_dict},
            artifact_kind="json",
            editable_targets=targets,
            timestamp=time.time(),
        )


# ---------------------------------------------------------------------------
# Stage 2: Outline
# ---------------------------------------------------------------------------


class OutlineStage(StageBase):
    """Progressive outline: 2a skeleton + 2b per-slide detail.

    Falls back to the one-shot ``run_outline_planner`` if the
    progressive path raises. The original orchestrator body wrapped
    this in a broad except and logged to stderr; that behaviour is
    preserved here verbatim.
    """

    name = "outline"
    artifact_kind = "json"
    after = "theme"
    before = None

    async def run(self, ctx: StageContext) -> None:
        state = ctx.state
        try:
            await run_structure_planner(
                state=state,
                llm=ctx.llm,
                tools=ctx.tool_registry,
                temperature=ctx.config.temperature,
                max_tokens=ctx.config.max_tokens,
                on_llm_response=ctx.config.on_llm_response,
            )
            detail_max_tokens = None
            await run_slide_detail_generator(
                state=state,
                llm=ctx.llm,
                tools=ctx.tool_registry,
                temperature=max(0.0, min(1.0, ctx.config.temperature + 0.1)),
                max_tokens=detail_max_tokens,
                on_llm_response=ctx.config.on_llm_response,
            )
        except Exception as exc:
            import sys
            print(
                f"[shuttleslide] warning: progressive outline failed ({exc}); "
                f"falling back to one-shot outline_planner",
                file=sys.stderr,
            )
            state.add_warning(
                f"progressive outline failed ({exc}); fell back to one-shot"
            )
            state.outline = []
            state.deck_skeleton = None
            await run_outline_planner(
                state=state,
                llm=ctx.llm,
                tools=ctx.tool_registry,
                temperature=ctx.config.temperature,
                max_tokens=ctx.config.max_tokens,
                on_llm_response=ctx.config.on_llm_response,
            )

    def is_cached(self, state: AgentState) -> bool:
        return bool(state.outline)

    def build_snapshot(self, state: AgentState) -> StageSnapshot:
        outline = [dict(item) for item in state.outline] if state.outline else []
        targets: List[EditTarget] = []
        if outline:
            targets.append(
                EditTarget(
                    stage="outline",
                    path=("outline",),
                    kind="json",
                    current_value=_json_dumps(outline),
                    meta={"slide_idx": None, "slot_id": None},
                )
            )
        return StageSnapshot(
            stage="outline",
            state_view={"outline": outline},
            artifact_kind="json",
            editable_targets=targets,
            timestamp=time.time(),
        )


# ---------------------------------------------------------------------------
# Stage 2.5: Images
# ---------------------------------------------------------------------------


class ImagesStage(StageBase):
    """Acquires images for each slide (svg or web).

    Output: ``state.slide_images`` (Dict[slide_idx, Dict[slot_id, payload]]).
    No-op if no slide declares any images.
    """

    name = "images"
    artifact_kind = "mixed"
    after = "outline"
    before = None

    async def run(self, ctx: StageContext) -> None:
        await run_image_acquirer(
            state=ctx.state,
            llm=ctx.llm,
            tools=ctx.tool_registry,
            temperature=ctx.config.temperature,
            max_tokens=ctx.config.max_tokens,
            svg_max_tokens=ctx.config.svg_generator_max_tokens,
            web_search_provider=ctx.web_search_provider,
            vlm_verifier=ctx.vlm_verifier,
            browser_manager=ctx.browser_manager,
            output_dir=ctx.output_dir,
            on_llm_response=ctx.config.on_llm_response,
        )

    def is_cached(self, state: AgentState) -> bool:
        return bool(state.slide_images)

    def build_snapshot(self, state: AgentState) -> StageSnapshot:
        state_view: Dict[str, Any] = {"slide_images": {}}
        targets: List[EditTarget] = []
        has_svg = False
        has_image = False
        for slide_idx, slots in state.slide_images.items():
            slot_view: Dict[str, Any] = {}
            for slot_id, payload in slots.items():
                safe_payload = _sanitise_payload(payload)
                slot_view[slot_id] = safe_payload
                p_type = payload.get("type")
                if p_type in ("svg", "svg_file"):
                    kind = "svg"
                    has_svg = True
                    current_value = payload.get("data", "")
                elif p_type in ("image_file", "image"):
                    kind = "image"
                    has_image = True
                    current_value = payload.get(
                        "path", payload.get("data", "")
                    )
                else:
                    kind = "image"
                    has_image = True
                    current_value = ""
                targets.append(
                    EditTarget(
                        stage="images",
                        path=("slide", int(slide_idx), "slot", slot_id),
                        kind=kind,
                        current_value=current_value,
                        meta={
                            "slide_idx": int(slide_idx),
                            "slot_id": slot_id,
                            "mime": payload.get("mime", ""),
                            "payload_type": p_type,
                        },
                    )
                )
            state_view["slide_images"][str(slide_idx)] = slot_view
        artifact_kind = "mixed" if (has_svg and has_image) else (
            "svg" if has_svg else ("image" if has_image else "mixed")
        )
        return StageSnapshot(
            stage="images",
            state_view=state_view,
            artifact_kind=artifact_kind,
            editable_targets=targets,
            timestamp=time.time(),
        )


# ---------------------------------------------------------------------------
# Stage 3: Slides
# ---------------------------------------------------------------------------


class SlidesStage(StageBase):
    """Builds one SlideDSL per outline item via the slide-builder LLM.

    Output: ``state.slides`` (List[SlideDSL]).
    """

    name = "slides"
    artifact_kind = "html"
    after = "images"
    before = None

    async def run(self, ctx: StageContext) -> None:
        state = ctx.state
        output_dir = ctx.output_dir
        for i in range(len(state.outline)):
            await run_slide_builder(
                state=state,
                llm=ctx.llm,
                tools=ctx.tool_registry,
                slide_index=i,
                temperature=max(0.0, ctx.config.temperature - 0.1),
                max_tokens=ctx.config.max_tokens,
                max_iterations=ctx.config.max_tool_iterations,
                on_llm_response=ctx.config.on_llm_response,
                output_dir=output_dir,
            )

    def is_cached(self, state: AgentState) -> bool:
        return bool(state.slides)

    def build_snapshot(self, state: AgentState) -> StageSnapshot:
        state_view: Dict[str, Any] = {"slides": []}
        targets: List[EditTarget] = []
        for idx, slide in enumerate(state.slides):
            if slide is None:
                continue
            html = slide.slots.get("html", "") if hasattr(slide, "slots") else ""
            state_view["slides"].append({"index": idx, "html": html})
            targets.append(
                EditTarget(
                    stage="slides",
                    path=("slide", idx, "html"),
                    kind="html",
                    current_value=html,
                    meta={"slide_idx": idx, "slot_id": "html"},
                )
            )
        return StageSnapshot(
            stage="slides",
            state_view=state_view,
            artifact_kind="html",
            editable_targets=targets,
            timestamp=time.time(),
        )


# ---------------------------------------------------------------------------
# Stage 4: Rendered (terminal)
# ---------------------------------------------------------------------------


class RenderedStage(StageBase):
    """Renders slides to standalone HTML files and dumps the DSL JSON.

    Terminal stage: ``finalize`` produces the ``OrchestratorResult``
    consumed by callers.

    Output: ``state.html_paths`` (List[str]).
    """

    name = "rendered"
    artifact_kind = "html"
    after = "slides"
    before = None
    terminal = True

    async def run(self, ctx: StageContext) -> None:
        """Write HTML files to disk, populate ``state.html_paths``.

        ``finalize`` is called separately by the orchestrator after the
        snapshot/hook fires; this split lets the gate-pause path show
        the rendered snapshot before ``OrchestratorResult`` is built.
        """
        state = ctx.state
        output_dir = (
            Path(ctx.config.output_dir) if ctx.config.output_dir else None
        )
        if output_dir is None:
            # No output_dir configured — leave html_paths empty. The
            # original orchestrator._finalize had the same no-op path.
            state.html_paths = []
            return

        presentation = _state_to_presentation(state)
        presentation.slide_width_emu = state.canvas_width_emu
        presentation.slide_height_emu = state.canvas_height_emu

        renderer = ctx.renderer
        if renderer is None:
            # Defensive — orchestrator should always inject a renderer.
            # If missing, skip rendering (matches legacy no-output_dir path).
            state.html_paths = []
            return

        html_paths: List[Path] = renderer.render_slides_to_files(
            presentation,
            output_dir,
            title_prefix=(
                state.outline[0].get("title", "")[:80] or None
            )
            if state.outline
            else None,
            canvas_width_emu=state.canvas_width_emu,
            canvas_height_emu=state.canvas_height_emu,
        )
        dsl_path = output_dir / "presentation.json"
        dsl_path.write_text(
            json.dumps(
                dump_presentation(presentation), ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
        state.html_paths = [str(p) for p in html_paths]

    def is_cached(self, state: AgentState) -> bool:
        return bool(state.html_paths)

    def build_snapshot(self, state: AgentState) -> StageSnapshot:
        """Rendered view: slides + on-disk html_paths. Not editable."""
        # Reuse the slides snapshot body, then add html_paths and clear
        # editable_targets (kept identical to the pre-refactor helper).
        state_view: Dict[str, Any] = {"slides": []}
        for idx, slide in enumerate(state.slides):
            if slide is None:
                continue
            html = slide.slots.get("html", "") if hasattr(slide, "slots") else ""
            state_view["slides"].append({"index": idx, "html": html})
        state_view["html_paths"] = (
            list(state.html_paths) if state.html_paths else []
        )
        return StageSnapshot(
            stage="rendered",
            state_view=state_view,
            artifact_kind="html",
            editable_targets=[],
            timestamp=time.time(),
        )

    def finalize(self, state: AgentState):
        """Build the final ``OrchestratorResult`` from state.

        Called by the orchestrator after the terminal stage's ``run``
        + ``_post_stage_hook`` complete. The presentation object is
        rebuilt here (rather than stashed on the stage) because state
        may have been mutated by the hook / a reviewer's edit before
        finalize runs.
        """
        from shuttleslide.agent.orchestrator import OrchestratorResult

        presentation = _state_to_presentation(state)
        presentation.slide_width_emu = state.canvas_width_emu
        presentation.slide_height_emu = state.canvas_height_emu
        return OrchestratorResult(
            state=state,
            html_paths=[Path(p) for p in state.html_paths],
            presentation=presentation,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_to_presentation(state: AgentState) -> PresentationDSL:
    """Build a PresentationDSL from the orchestrator state.

    Lifted verbatim from orchestrator.py:_state_to_presentation.
    """
    theme_dict = state.theme or {}
    theme = ThemeDef(
        primary_color=theme_dict.get("primary_color", "#133EFF"),
        accent_color=theme_dict.get("accent_color", "#00CD82"),
        warn_color=theme_dict.get("warn_color", "#FF5722"),
        bg_color=theme_dict.get("bg_color", "#FEFEFE"),
        text_color=theme_dict.get("text_color", "#1F2937"),
        font_title=theme_dict.get("font_title", "Roboto"),
        font_body=theme_dict.get("font_body", "Roboto"),
    )
    slides: List[SlideDSL] = [s for s in state.slides if s is not None]
    return PresentationDSL(theme=theme, slides=slides)
