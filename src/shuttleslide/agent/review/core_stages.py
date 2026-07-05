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
from shuttleslide.agent.nodes.slide_builder import (
    run_slide_builder,
    run_slide_builder_incremental,
)
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

    async def regenerate_item(
        self,
        ctx: StageContext,
        target_id: str,
        *,
        mode: str = "incremental",
    ) -> None:
        """Per-item image regeneration.

        MVP: always ``fresh`` (re-run image_acquirer for the target).
        The ``mode`` parameter is accepted but ignored — image edits
        are uncommon enough that incremental complexity isn't worth
        it, and the SVG generator's anti-patterns would need separate
        work to articulate "preserve user color tweaks".

        ``target_id="slide:N"`` → re-acquire every image slot on that
        slide (clears + repopulates ``state.slide_images[N]``).
        ``"slide:N:slot:ID"`` → re-acquire just that slot.
        ``"all"`` → re-acquire every image slot in the deck.

        Relies on ``run_image_acquirer``'s idempotency (it skips slots
        that already have a payload). We delete the target slot from
        ``state.slide_images`` before the call so the acquirer sees an
        empty slot and refills it. Other slides' slots are left intact
        and skipped.
        """
        import re

        from shuttleslide.agent.nodes.image_acquirer import run_image_acquirer

        slide_re = re.compile(r"^slide:(\d+)$")
        slide_slot_re = re.compile(r"^slide:(\d+):slot:(.+)$")
        state = ctx.state

        if target_id == "all":
            # Clear all slots so acquirer re-runs every spec.
            state.slide_images.clear()
        else:
            m = slide_slot_re.match(target_id)
            if m is not None:
                slide_idx = int(m.group(1))
                slot_id = m.group(2)
                slots = state.slide_images.get(slide_idx) or {}
                slots.pop(slot_id, None)
                if slots:
                    state.slide_images[slide_idx] = slots
                else:
                    state.slide_images.pop(slide_idx, None)
            else:
                m = slide_re.match(target_id)
                if m is None:
                    raise ValueError(
                        f"images.regenerate_item: unsupported target_id {target_id!r}"
                    )
                slide_idx = int(m.group(1))
                state.slide_images.pop(slide_idx, None)

        await run_image_acquirer(
            state=state,
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

    async def regenerate_item(
        self,
        ctx: StageContext,
        target_id: str,
        *,
        mode: str = "incremental",
    ) -> None:
        """Per-item slide regeneration (review pipeline).

        ``target_id="slide:N"`` → regenerate that slide. ``"all"``
        iterates every slide (used when the user explicitly clicks
        "regenerate all" — uncommon path, but supported for parity
        with Restart without dropping the rest of the pipeline).

        ``mode="incremental"`` (default) preserves user manual edits
        by showing the LLM the current HTML and asking for a minimal
        patch reflecting the upstream diff (see
        :func:`build_slide_builder_incremental_prompt`).

        ``mode="fresh"`` falls back to the original
        :func:`run_slide_builder` — same code path as the initial
        build, no preservation guarantees.
        """
        import re

        slide_re = re.compile(r"^slide:(\d+)$")
        if target_id == "all":
            indices = list(range(len(ctx.state.outline)))
        else:
            m = slide_re.match(target_id)
            if m is None:
                raise ValueError(
                    f"slides.regenerate_item: unsupported target_id {target_id!r}"
                )
            indices = [int(m.group(1))]

        # Pull the upstream before-state from the stale mark's
        # context_snapshot, if available. The caller (RegenerateCoordinator)
        # leaves the snapshot on state.stale_marks so we can read it
        # back here. Missing snapshot → fall back to "no upstream diff"
        # (the incremental prompt degrades gracefully to "match the
        # current outline").
        from shuttleslide.agent.review.stale import StaleStore

        store = StaleStore.from_dict(ctx.state.stale_marks)
        mark = store.get("slides", target_id) if target_id != "all" else None
        snapshot = mark.context_snapshot if mark is not None else None
        old_outline = (snapshot or {}).get("outline_before") if isinstance(
            snapshot, dict
        ) else None
        # outline_before is sometimes the full list (outline+item rule)
        # or a single item (per-slide). Normalize: pick out the slide
        # we're regenerating.
        if isinstance(old_outline, list):
            for idx in indices:
                if idx < len(old_outline):
                    old_outline_for_slide = old_outline[idx]
                    break
            else:
                old_outline_for_slide = None
        elif isinstance(old_outline, dict):
            old_outline_for_slide = old_outline
        else:
            old_outline_for_slide = None
        theme_before = (snapshot or {}).get("theme_before") if isinstance(
            snapshot, dict
        ) else None

        for idx in indices:
            current_html = ""
            if idx < len(ctx.state.slides):
                slide = ctx.state.slides[idx]
                if slide is not None and hasattr(slide, "slots"):
                    current_html = slide.slots.get("html", "")
            if mode == "fresh":
                await run_slide_builder(
                    state=ctx.state,
                    llm=ctx.llm,
                    tools=ctx.tool_registry,
                    slide_index=idx,
                    temperature=max(0.0, ctx.config.temperature - 0.1),
                    max_tokens=ctx.config.max_tokens,
                    max_iterations=ctx.config.max_tool_iterations,
                    on_llm_response=ctx.config.on_llm_response,
                    output_dir=ctx.output_dir,
                )
            else:
                await run_slide_builder_incremental(
                    state=ctx.state,
                    llm=ctx.llm,
                    tools=ctx.tool_registry,
                    slide_index=idx,
                    current_html=current_html,
                    old_outline=old_outline_for_slide,
                    theme_before=theme_before,
                    theme_after=ctx.state.theme,
                    temperature=max(0.0, ctx.config.temperature - 0.3),
                    max_tokens=ctx.config.max_tokens,
                    max_iterations=ctx.config.max_tool_iterations,
                    on_llm_response=ctx.config.on_llm_response,
                    output_dir=ctx.output_dir,
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
            [p for p in state.html_paths if isinstance(p, str) and p]
            if state.html_paths else []
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

    async def regenerate_item(
        self,
        ctx: StageContext,
        target_id: str,
        *,
        mode: str = "incremental",
    ) -> None:
        """Re-render a single slide's HTML file on disk.

        No LLM call — ``rendered`` is a deterministic transform of
        ``slides + theme``. ``mode`` is ignored (kept in the signature
        for symmetry with the LLM-bearing stages).

        ``target_id="slide:N"`` → re-render that slide's HTML file
        (overwrites ``{output_dir}/{N+1}.html``). ``"all"`` re-renders
        every slide (equivalent to a full RenderedStage.run, but
        without rebuilding ``state.html_paths`` from scratch — paths
        already exist).
        """
        import re

        from shuttleslide.agent.theme_tokens import substitute_theme_tokens

        slide_re = re.compile(r"^slide:(\d+)$")
        state = ctx.state
        output_dir = (
            Path(ctx.config.output_dir) if ctx.config.output_dir else None
        )
        if output_dir is None or not state.html_paths:
            return  # nothing to re-render
        if ctx.renderer is None:
            return

        if target_id == "all":
            indices = list(range(len(state.slides)))
        else:
            m = slide_re.match(target_id)
            if m is None:
                raise ValueError(
                    f"rendered.regenerate_item: unsupported target_id {target_id!r}"
                )
            indices = [int(m.group(1))]

        presentation = _state_to_presentation(state)
        presentation.slide_width_emu = state.canvas_width_emu
        presentation.slide_height_emu = state.canvas_height_emu
        for idx in indices:
            if idx >= len(state.slides) or idx >= len(state.html_paths):
                continue
            slide = state.slides[idx]
            if slide is None:
                continue
            title = (
                state.outline[idx].get("title", "")[:80]
                if idx < len(state.outline) and isinstance(
                    state.outline[idx], dict
                )
                else f"Slide {idx + 1}"
            )
            html = ctx.renderer.render_slide(
                slide,
                presentation.theme,
                title=title,
                canvas_width_emu=state.canvas_width_emu,
                canvas_height_emu=state.canvas_height_emu,
            )
            # html_paths[idx] is normally the on-disk path written by run().
            # Re-use it so external links don't break. When the slot was
            # padded with None by a structural op (insert_slide / append),
            # synthesize the same {idx+1}.html naming convention run() uses
            # and write it back so subsequent snapshots / export see the
            # populated path. Without this, Path(None) raises and the
            # freshly built slide's render is lost.
            existing = state.html_paths[idx]
            if existing is None:
                path = output_dir / f"{idx + 1}.html"
                state.html_paths[idx] = str(path)
            else:
                path = Path(existing)
            path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_to_presentation(state: AgentState) -> PresentationDSL:
    """Build a PresentationDSL from the orchestrator state.

    Theme is built by filtering ``state.theme`` through
    ``ThemeDef.__dataclass_fields__`` so any field added to ThemeDef
    flows through automatically. Slides are passed through verbatim;
    callers that need ``slide.elements`` populated (e.g. PPTX export)
    must run the result through ``RuleSlideTransformer`` themselves.
    """
    theme_dict = state.theme or {}
    # Filter to ThemeDef's known fields so any new ThemeDef field is picked
    # up automatically. Same pattern as server._theme_from_snapshot and
    # transformer.RuleSlideTransformer. A hardcoded whitelist previously
    # dropped title_color (and any future field), causing presentation.json
    # to fall back to ThemeDef defaults while the live preview showed the
    # LLM-set value.
    known = {
        k: v for k, v in theme_dict.items()
        if k in ThemeDef.__dataclass_fields__
    }
    theme = ThemeDef(**known)
    slides: List[SlideDSL] = [s for s in state.slides if s is not None]
    return PresentationDSL(theme=theme, slides=slides)
