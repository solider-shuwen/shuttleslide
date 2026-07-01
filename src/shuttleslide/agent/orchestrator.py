"""Pipeline orchestrator.

Wires the four stages together and threads a single AgentState through them:
  Stage 1: Theme Designer   -> state.theme
  Stage 2: Outline Planner  -> state.outline
  Stage 3: Slide Builder    -> state.slides  (one call per slide)
  Stage 4: HTML Renderer    -> state.html_paths

This is intentionally a sequential pipeline. Stage order and dispatch
come from the ``StageRegistry`` (see ``agent/review/registry.py``);
core stages live in ``agent/review/core_stages.py`` and external
packages add their own via the ``shuttleslide.review.stages`` entry
point group.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional, Union

from shuttleslide.agent.config import AgentConfig
from shuttleslide.agent.dsl_to_html import SlideHTMLRenderer
from shuttleslide.agent.llm import LLMClient
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry, get_default_registry
from shuttleslide.html_to_pptx.schema import PresentationDSL

if TYPE_CHECKING:
    # Imported only for type-checking to avoid the cycle:
    # ``review.__init__`` eagerly pulls in ``interactive_orchestrator``
    # which imports back into ``agent.orchestrator``. Runtime references
    # go through lazy imports inside _resolve_stages / _build_stage_context.
    from shuttleslide.agent.review.registry import StageRegistry
    from shuttleslide.agent.review.stage import Stage, StageContext


@dataclass
class OrchestratorResult:
    """Final result of an orchestrator run."""

    state: AgentState
    html_paths: List[Path]
    presentation: PresentationDSL


class AgentOrchestrator:
    """Runs the registry-driven stage pipeline."""

    def __init__(
        self,
        config: AgentConfig,
        registry: Optional[ToolRegistry] = None,
        renderer: Optional[SlideHTMLRenderer] = None,
        stage_registry: Optional[StageRegistry] = None,
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
        # Resolve stage order once at construction time. A RegistryError
        # (cycle, multiple terminals, broken pro entry-point) falls back
        # to the core-only registry so a broken extension cannot wedge
        # the pipeline. Tests that want a stub stage set pass
        # ``stage_registry`` explicitly.
        self._stages: List[Stage] = self._resolve_stages(stage_registry)

    def _resolve_stages(self, stage_registry: Optional[StageRegistry]) -> List[Stage]:
        """Return the resolved stage order, falling back to core-only
        on registry errors.

        ``stage_registry`` lets tests swap in a stub registry without
        touching entry points. ``None`` means "use the full registry"
        (core + entry-point extensions); callers that explicitly want
        core-only can pass ``default_registry()``.
        """
        # Lazy import — see module-level TYPE_CHECKING note.
        from shuttleslide.agent.review.registry import (
            RegistryError,
            default_registry,
            full_registry,
        )

        if stage_registry is not None:
            try:
                return stage_registry.resolve_order()
            except RegistryError as exc:
                # A caller-provided registry that fails to resolve is a
                # programming error — surface it rather than silently
                # fall back. The fallback below is for the entry-point
                # loading path where pro packages may be broken.
                raise
        try:
            return full_registry().resolve_order()
        except RegistryError as exc:
            import sys
            print(
                f"[shuttleslide] warning: stage registry failed to resolve "
                f"({exc}); falling back to core-only stages",
                file=sys.stderr,
            )
            return default_registry().resolve_order()

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
        """Drive every registered stage in resolved order.

        Per stage:
          1. ``_pre_stage_hook`` may short-circuit (returns True) when
             the stage's output is already in state (e.g. loaded from
             disk). The hook still fires so review/telemetry sees the
             snapshot.
          2. ``stage.run(ctx)`` does the work.
          3. ``_post_stage_hook`` fires after completion.
          4. The terminal stage's ``finalize`` produces the return value.

        A stage that raises is caught here: the error is logged via
        ``state.add_warning`` and the broadcaster (when attached) gets
        a non-fatal ``emit_error``. Downstream stages continue unless
        one of them refuses to tolerate the missing input — that
        decision belongs to each stage's ``run``, not this loop.
        """
        state = await self._prepare_state(
            topic=topic, style_hint=style_hint, target_count=target_count
        )
        result: Optional[OrchestratorResult] = None
        for stage in self._stages:
            try:
                if await self._pre_stage_hook(stage, state):
                    await self._post_stage_hook(stage, state)
                    if stage.terminal:
                        result = stage.finalize(state)
                    continue
                ctx = self._build_stage_context(state)
                await stage.run(ctx)
                await self._post_stage_hook(stage, state)
                if stage.terminal:
                    result = stage.finalize(state)
            except Exception as exc:
                # InteractiveOrchestrator's ReviewCancelledError is
                # re-raised so the CLI can surface "user cancelled".
                # We import lazily to avoid a static cycle.
                from shuttleslide.agent.review.interactive_orchestrator import (
                    ReviewCancelledError,
                )
                if isinstance(exc, ReviewCancelledError):
                    raise
                state.add_warning(f"stage {stage.name!r} failed: {exc}")
                # Hook for subclasses to broadcast the failure (e.g. via
                # the review UI's broadcaster). Base default is no-op so
                # the warning silently lands in state.warnings — keeping
                # the legacy non-review CLI path quiet. Without this hook
                # the InteractiveOrchestrator has no seam to surface the
                # error, and stage failures (e.g. RenderVideoStage's
                # Node/Remotion errors) get swallowed: pipeline_state
                # flips to "done" with the failing stage's tab empty and
                # no signal to the user about what went wrong.
                await self._on_stage_failed(stage, exc, state)
                if stage.terminal and result is None:
                    # Terminal stage failed — synthesise a partial result
                    # so callers get an OrchestratorResult instead of None.
                    result = OrchestratorResult(
                        state=state,
                        html_paths=[],
                        presentation=_empty_presentation(),
                    )
        if result is None:
            # No terminal stage produced a result (only happens if the
            # registry was misconfigured). Defensive — orchestrators
            # built through the normal path always have a terminal.
            result = OrchestratorResult(
                state=state,
                html_paths=[],
                presentation=_empty_presentation(),
            )
        return result

    def _build_stage_context(self, state: AgentState) -> StageContext:
        """Build a fresh ``StageContext`` for the current state.

        One context per stage call — stages should not assume the same
        instance is reused. The web-deps fields are populated lazily
        by ``_build_web_acquisition_deps`` at the start of ``run()``.
        """
        # Lazy import — see module-level TYPE_CHECKING note.
        from shuttleslide.agent.review.stage import StageContext

        output_dir = (
            Path(self.config.output_dir) if self.config.output_dir else None
        )
        return StageContext(
            state=state,
            llm=self.llm,
            config=self.config,
            tool_registry=self.registry,
            output_dir=output_dir,
            renderer=self.renderer,
            web_search_provider=self._web_search_provider,
            vlm_verifier=self._vlm_verifier,
            browser_manager=self._browser_manager,
            broadcaster=getattr(self, "broadcaster", None),
        )

    async def _prepare_state(
        self,
        topic: Optional[str],
        style_hint: Optional[str],
        target_count: Optional[int],
    ) -> AgentState:
        """Build (or load) the AgentState for this run.

        Base implementation always builds fresh. Subclasses override
        to hydrate from disk — see ``InteractiveOrchestrator._prepare_state``
        which loads from ``state_cache_path`` when configured.
        """
        return self._make_state(
            topic=topic, style_hint=style_hint, target_count=target_count
        )

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
            user_image_library=self.config.user_image_library,
        )

    async def _post_stage_hook(self, stage: Stage, state: AgentState) -> None:
        """Hook invoked after each stage completes. Default is no-op.

        Subclasses (e.g. InteractiveOrchestrator) override this to insert
        review/telemetry/checkpoint behaviour between stages without
        duplicating the pipeline wiring.
        """
        return None

    async def _on_stage_failed(
        self, stage: Stage, exc: BaseException, state: AgentState
    ) -> None:
        """Hook invoked when a stage raises. Default is no-op — the
        failure is already recorded in ``state.warnings`` by the caller.

        Subclasses (e.g. InteractiveOrchestrator) override this to
        broadcast the error to a UI / log sink. Without an override the
        failure is silent except for the warning entry, which the
        non-review CLI path intends; review UIs need the override so
        users see *why* a stage's tab is empty after pipeline_done.
        """
        return None

    async def _pre_stage_hook(self, stage: Stage, state: AgentState) -> bool:
        """Hook invoked before each stage runs. Return True to skip the
        stage's work entirely (the stage still fires
        ``_post_stage_hook`` so downstream review/telemetry sees the
        snapshot).

        Default is False — always run the stage. Subclasses override
        to short-circuit when state was loaded from a cache (see
        InteractiveOrchestrator + state_persistence).
        """
        return False

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


def _empty_presentation() -> PresentationDSL:
    """Synthesise a minimal PresentationDSL for failure paths.

    Used when the terminal stage raised before producing output, so
    callers still get an ``OrchestratorResult`` shape. The presentation
    has default theme and zero slides — sufficient for ``html_paths=[]``
    round-tripping through downstream consumers.
    """
    from shuttleslide.html_to_pptx.schema import ThemeDef

    return PresentationDSL(theme=ThemeDef(), slides=[])


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
