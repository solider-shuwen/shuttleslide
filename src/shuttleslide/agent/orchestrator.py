"""Pipeline orchestrator.

Wires the four stages together and threads a single AgentState through them:
  Stage 1: Theme Designer   -> state.theme
  Stage 2: Outline Planner  -> state.outline
  Stage 3: Slide Builder    -> state.slides  (one call per slide)
  Stage 4: HTML Renderer    -> state.html_paths

This is intentionally a sequential pipeline with one bounded inner loop
(in Stage 3). No graph framework needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Union

from shuttleslide.agent.config import AgentConfig
from shuttleslide.agent.dsl_to_html import SlideHTMLRenderer
from shuttleslide.agent.llm import LLMClient
from shuttleslide.agent.nodes.image_acquirer import run_image_acquirer
from shuttleslide.agent.nodes.outline_planner import (
    run_outline_planner,
    run_slide_detail_generator,
    run_structure_planner,
)
from shuttleslide.agent.nodes.slide_builder import run_slide_builder
from shuttleslide.agent.nodes.theme_designer import run_theme_designer
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry, get_default_registry
from shuttleslide.html_to_pptx.schema import (
    PresentationDSL,
    SlideDSL,
    ThemeDef,
    dump_presentation,
)


@dataclass
class OrchestratorResult:
    """Final result of an orchestrator run."""

    state: AgentState
    html_paths: List[Path]
    presentation: PresentationDSL


class AgentOrchestrator:
    """Runs the 4-stage pipeline."""

    def __init__(
        self,
        config: AgentConfig,
        registry: Optional[ToolRegistry] = None,
        renderer: Optional[SlideHTMLRenderer] = None,
    ) -> None:
        self.config = config
        # Use the module-level default registry (already populated by importing
        # the tool modules). Callers can pass a custom registry for testing.
        self.registry = registry if registry is not None else get_default_registry()
        self.renderer = renderer if renderer is not None else SlideHTMLRenderer()
        self.llm = LLMClient(
            api_base=config.api_base,
            api_key=config.api_key,
            model=config.model,
            disable_required_tool_choice=config.disable_required_tool_choice,
        )
        # Lazy-built web image acquisition deps. None when the user hasn't
        # configured the web path (or when build_* raises and we soft-disable).
        # Built on first call to run() rather than __init__ so a misconfigured
        # VLM/search provider doesn't break orchestrator construction.
        self._web_search_provider = None
        self._vlm_verifier = None
        self._browser_manager = None
        self._web_deps_built = False

    async def run(
        self,
        topic: Optional[str] = None,
        style_hint: Optional[str] = None,
        target_count: Optional[int] = None,
    ) -> OrchestratorResult:
        """Run the full pipeline. Returns OrchestratorResult with HTML paths."""
        self.config.validate()
        self._build_web_acquisition_deps()
        await self._ensure_browser_started()
        try:
            return await self._run_pipeline(
                topic=topic, style_hint=style_hint, target_count=target_count
            )
        finally:
            await self._stop_browser_if_started()

    async def _run_pipeline(
        self,
        topic: Optional[str],
        style_hint: Optional[str],
        target_count: Optional[int],
    ) -> OrchestratorResult:
        state = self._make_state(topic=topic, style_hint=style_hint, target_count=target_count)
        await self._run_stage_theme(state)
        await self._run_stage_outline(state)
        await self._run_stage_images(state)
        await self._run_stage_slides(state)
        return await self._finalize(state)

    def _make_state(
        self,
        topic: Optional[str],
        style_hint: Optional[str],
        target_count: Optional[int],
    ) -> AgentState:
        """Build a fresh AgentState for one pipeline run.

        Per-call overrides fall back to the config defaults.
        """
        topic = topic if topic is not None else self.config.topic
        style_hint = style_hint if style_hint is not None else self.config.style_hint
        target_count = (
            target_count if target_count is not None else self.config.target_slide_count
        )

        if not topic:
            raise ValueError("topic is required (pass to run() or set on config)")

        return AgentState(
            topic=topic,
            style_hint=style_hint,
            target_count=target_count,
            canvas_width_emu=self.config.canvas_width_emu,
            canvas_height_emu=self.config.canvas_height_emu,
        )

    async def _post_stage_hook(self, stage: str, state: AgentState) -> None:
        """Hook invoked after each stage completes. Default is no-op.

        Subclasses (e.g. InteractiveOrchestrator) override this to insert
        review/telemetry/checkpoint behaviour between stages without
        duplicating the pipeline wiring.
        """
        return None

    async def _run_stage_theme(self, state: AgentState) -> None:
        """Stage 1: Theme Designer — designs global theme (colors/fonts/decoration)."""
        await run_theme_designer(
            state=state,
            llm=self.llm,
            tools=self.registry,
            temperature=self.config.temperature,
            max_tokens=2048,
            on_llm_response=self.config.on_llm_response,
        )
        await self._post_stage_hook("theme", state)

    async def _run_stage_outline(self, state: AgentState) -> None:
        """Stage 2: Outline Planner — progressive (2a skeleton + 2b detail).

        Two-stage path: structure planner (1 LLM call) + per-slide detail
        generator (N LLM calls). Falls back to the one-shot
        run_outline_planner when either stage raises — the one-shot path
        is still maintained as the production fallback so a bad model day
        never blocks deck generation entirely.
        """
        try:
            # Stage 2a: deck skeleton (thesis + MECE groups + per-slide intent)
            await run_structure_planner(
                state=state,
                llm=self.llm,
                tools=self.registry,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                on_llm_response=self.config.on_llm_response,
            )
            # Stage 2b: per-slide detail (key_points + layout_hint + images)
            # Slightly higher temperature for variety across slides.
            detail_max_tokens = None
            await run_slide_detail_generator(
                state=state,
                llm=self.llm,
                tools=self.registry,
                temperature=max(0.0, min(1.0, self.config.temperature + 0.1)),
                max_tokens=detail_max_tokens,
                on_llm_response=self.config.on_llm_response,
            )
        except Exception as exc:
            # Progressive path failed — wipe any partial state and retry
            # with the one-shot planner. We intentionally catch broadly
            # here because both stages can raise (skeleton_planner raises
            # RuntimeError after retries; slide_detail_generator raises
            # only on hard preconditions like missing outline).
            import sys
            print(
                f"[shuttleslide] warning: progressive outline failed ({exc}); "
                f"falling back to one-shot outline_planner",
                file=sys.stderr,
            )
            state.add_warning(
                f"progressive outline failed ({exc}); fell back to one-shot"
            )
            # Clear half-built state so run_outline_planner starts fresh.
            # The detail generator may have enriched some slides already;
            # define_outline will overwrite state.outline wholesale.
            state.outline = []
            state.deck_skeleton = None
            await run_outline_planner(
                state=state,
                llm=self.llm,
                tools=self.registry,
                temperature=self.config.temperature,
                max_tokens=self.config.max_tokens,
                on_llm_response=self.config.on_llm_response,
            )
        await self._post_stage_hook("outline", state)

    async def _run_stage_images(self, state: AgentState) -> None:
        """Stage 2.5: Image Acquirer — one call per image spec.

        Routes each spec to svg or web path based on spec.source_type.
        No-op if no slide declares any images. Web specs fall back to
        svg when web_search_provider / vlm_verifier are unavailable.
        output_dir is required for web specs (file-externalized model):
        without it, acquire_web_image returns False and the spec falls
        back to svg.
        """
        output_dir = Path(self.config.output_dir) if self.config.output_dir else None
        await run_image_acquirer(
            state=state,
            llm=self.llm,
            tools=self.registry,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            svg_max_tokens=self.config.svg_generator_max_tokens,
            web_search_provider=self._web_search_provider,
            vlm_verifier=self._vlm_verifier,
            browser_manager=self._browser_manager,
            output_dir=output_dir,
            on_llm_response=self.config.on_llm_response,
        )
        await self._post_stage_hook("images", state)

    async def _run_stage_slides(self, state: AgentState) -> None:
        """Stage 3: Slide Builder — one LLM call per slide."""
        output_dir = (
            Path(self.config.output_dir) if self.config.output_dir else None
        )
        for i in range(len(state.outline)):
            await run_slide_builder(
                state=state,
                llm=self.llm,
                tools=self.registry,
                slide_index=i,
                # Slightly lower temperature for layout precision.
                temperature=max(0.0, self.config.temperature - 0.1),
                max_tokens=self.config.max_tokens,
                max_iterations=self.config.max_tool_iterations,
                on_llm_response=self.config.on_llm_response,
                output_dir=output_dir,
            )
        await self._post_stage_hook("slides", state)

    async def _finalize(self, state: AgentState) -> OrchestratorResult:
        """Stage 4: HTML Renderer — render slides to standalone HTML files.

        Also dumps the DSL JSON next to the HTML for inspection / debugging.
        """
        presentation = _state_to_presentation(state)
        # Thread canvas dimensions into the PPTX (schema defaults reproduce
        # 16:9; the caller may have overridden them via AgentConfig).
        presentation.slide_width_emu = state.canvas_width_emu
        presentation.slide_height_emu = state.canvas_height_emu
        html_paths: List[Path] = []
        if self.config.output_dir:
            out_dir = Path(self.config.output_dir)
            html_paths = self.renderer.render_slides_to_files(
                presentation,
                out_dir,
                title_prefix=(state.outline[0].get("title", "")[:80] or None) if state.outline else None,
                canvas_width_emu=state.canvas_width_emu,
                canvas_height_emu=state.canvas_height_emu,
            )
            dsl_path = out_dir / "presentation.json"
            dsl_path.write_text(
                json.dumps(dump_presentation(presentation), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        # Fire the rendered hook AFTER files are on disk so reviewers can
        # open them. Note this is async to match the other stages; the base
        # implementation is a no-op so callers that don't override it pay
        # only an awaitable-returning call.
        await self._post_stage_hook("rendered", state)

        return OrchestratorResult(state=state, html_paths=html_paths, presentation=presentation)

    # ------------------------------------------------------------------
    # Web image acquisition deps
    # ------------------------------------------------------------------

    def _build_web_acquisition_deps(self) -> None:
        """Construct the search provider + VLM verifier from config.

        Idempotent — built once per orchestrator. Soft-disables the web
        path (leaves both as None) when required fields are missing;
        validate() already catches the loud misconfigurations, so the
        remaining branches here are the "user didn't opt in" cases.

        Does NOT start the browser — that's deferred to
        ``_ensure_browser_started`` so a stub-only deck skips Chromium
        entirely (saves 3-5s of startup).
        """
        if self._web_deps_built:
            return
        self._web_deps_built = True

        cfg = self.config
        # Search provider: built only when the user opted in by setting
        # image_search_provider. An empty string means "svg-only deck".
        if cfg.image_search_provider:
            try:
                from shuttleslide.agent.nodes.image_sources import (
                    make_search_provider,
                )
                self._web_search_provider = make_search_provider(
                    cfg.image_search_provider,
                    base_url=cfg.image_search_base_url,
                )
            except Exception as exc:
                # Don't crash the pipeline — log and continue with svg-only.
                # The validate() step catches unknown provider names with a
                # ValueError before we get here, so this branch is for
                # unexpected import / construction failures only.
                import sys
                print(
                    f"[shuttleslide] warning: failed to build image search "
                    f"provider {cfg.image_search_provider!r}: {exc}",
                    file=sys.stderr,
                )
                self._web_search_provider = None

        # VLM verifier: requires a vision-capable model. When the user
        # configures a separate vlm endpoint we use it; otherwise we
        # reuse the text LLM endpoint (works when the deployment hosts
        # a vision model under the same base_url).
        if cfg.vlm_model and cfg.enable_vlm_verification:
            try:
                from shuttleslide.agent.nodes.image_sources import VLMVerifier
                vlm_client = LLMClient(
                    api_base=cfg.vlm_api_base or cfg.api_base,
                    api_key=cfg.vlm_api_key or cfg.api_key,
                    model=cfg.vlm_model,
                )
                self._vlm_verifier = VLMVerifier(
                    vlm_client=vlm_client,
                    on_llm_response=cfg.on_llm_response,
                )
            except Exception as exc:
                import sys
                print(
                    f"[shuttleslide] warning: failed to build VLM verifier "
                    f"for model {cfg.vlm_model!r}: {exc}",
                    file=sys.stderr,
                )
                self._vlm_verifier = None

    async def _ensure_browser_started(self) -> None:
        """Start a shared BrowserManager if any web dep needs one.

        Conditions for starting:
          - The search provider declares ``requires_browser = True``
            (currently only BingWebScrapeSearchProvider does).
          - We don't pre-emptively start for URL-screenshot specs
            because we can't know yet whether the outline will contain
            any. URL specs that hit a missing browser fall back to SVG.

        We attach the running browser to the provider so all searches
        in one deck share one Chromium instance.
        """
        if self._browser_manager is not None:
            return  # already started
        provider = self._web_search_provider
        if not getattr(provider, "requires_browser", False):
            return
        try:
            from shuttleslide.html_to_pptx.analyzer.browser import BrowserManager
        except ImportError as exc:
            import sys
            print(
                f"[shuttleslide] warning: BrowserManager import failed ({exc}); "
                f"web image search disabled (URL/specs will fall back to svg)",
                file=sys.stderr,
            )
            self._web_search_provider = None
            return
        self._browser_manager = BrowserManager()
        try:
            await self._browser_manager.start()
        except Exception as exc:
            import sys
            print(
                f"[shuttleslide] warning: failed to start browser ({exc}); "
                f"web image search disabled",
                file=sys.stderr,
            )
            self._browser_manager = None
            self._web_search_provider = None
            return
        # Hand the running browser to the provider. Screenshot path
        # reads it off self._browser_manager directly when needed.
        attach = getattr(provider, "attach_browser_manager", None)
        if attach is not None:
            attach(self._browser_manager)

    async def _stop_browser_if_started(self) -> None:
        """Stop the shared browser if we started one. Safe to call always."""
        if self._browser_manager is None:
            return
        try:
            await self._browser_manager.stop()
        except Exception as exc:
            import sys
            print(
                f"[shuttleslide] warning: browser stop failed: {exc}",
                file=sys.stderr,
            )
        finally:
            self._browser_manager = None


def _state_to_presentation(state: AgentState) -> PresentationDSL:
    """Build a PresentationDSL from the orchestrator state."""
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


# ---------------------------------------------------------------------------
# High-level convenience function
# ---------------------------------------------------------------------------

async def generate_slides(
    topic: str,
    style_hint: str = "business",
    target_slide_count: Optional[int] = None,
    api_base: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    output_dir: Optional[Union[str, Path]] = None,
    config: Optional[AgentConfig] = None,
) -> OrchestratorResult:
    """One-shot entry point for generating a slide deck.

    Either pass explicit api_base/api_key/model + output_dir, or pass a fully
    built AgentConfig. Env vars (SHUTTLESLIDE_API_BASE etc.) are picked up
    automatically by AgentConfig.from_env().
    """
    if config is None:
        config = AgentConfig.from_env(
            api_base=api_base,
            api_key=api_key,
            model=model,
            topic=topic,
            style_hint=style_hint,
            target_slide_count=target_slide_count,
            output_dir=str(output_dir) if output_dir else None,
        )

    orch = AgentOrchestrator(config)
    return await orch.run(topic=topic, style_hint=style_hint, target_count=target_slide_count)
