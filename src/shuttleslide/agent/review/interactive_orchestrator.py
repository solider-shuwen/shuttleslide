"""InteractiveOrchestrator — AgentOrchestrator subclass that pauses at
each stage boundary for human review.

Design
------
The base orchestrator drives the pipeline via ``_run_pipeline`` which
iterates registered stages. This subclass overrides three hooks:

  * ``_prepare_state`` — load from disk when configured, so the next
    run can resume without redoing LLM work.
  * ``_pre_stage_hook`` — short-circuit a stage when its output is
    already in state (loaded from disk). Delegates to ``stage.is_cached``.
  * ``_post_stage_hook`` — build a snapshot, broadcast progress, save
    state, and pause at the gate (for reviewed stages).

Caching snapshot
----------------
``_cached_stages_from_load`` is the source of truth for "did this stage
come from disk?". It's populated once in ``_prepare_state`` BEFORE any
stage runs. Querying ``stage.is_cached`` later (in ``_post_stage_hook``)
would be wrong because the stage that just ran has populated its own
output — ``is_cached`` would report True for it incorrectly.

Auto-approve mode
-----------------
``auto_approve=True`` skips the gate entirely. Used by the regression
test that compares InteractiveOrchestrator output to the base class.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional, Set

from shuttleslide.agent.config import AgentConfig
from shuttleslide.agent.orchestrator import AgentOrchestrator, OrchestratorResult
from shuttleslide.agent.review.broadcaster import Broadcaster
from shuttleslide.agent.review.registry import StageRegistry, default_registry
from shuttleslide.agent.review.review_gate import (
    ReviewAction,
    ReviewGate,
    StageName,
    StageSnapshot,
)
from shuttleslide.agent.review.stage import Stage
from shuttleslide.agent.review.state_persistence import load_state, save_state
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry
from shuttleslide.agent.dsl_to_html import SlideHTMLRenderer


class ReviewCancelledError(Exception):
    """Raised when a reviewer cancels the pipeline via gate.release('cancel').

    The CLI catches this and exits with a non-zero status + message
    rather than a stack trace.
    """

    def __init__(self, stage: StageName, reason: str = "") -> None:
        self.stage = stage
        self.reason = reason
        super().__init__(
            f"pipeline cancelled by reviewer at stage {stage!r}"
            + (f": {reason}" if reason else "")
        )


def _parse_review_stages(spec: Optional[str]) -> Set[StageName]:
    """Parse the ``--review-stages`` CLI flag into a set of stage names.

    Accepted forms:
        None / "all" / ""  -> all registered stages (core + extensions)
        "theme,outline"    -> {"theme", "outline"}
        "theme, slides"    -> {"theme", "slides"}  (whitespace tolerated)

    Raises ValueError on unknown stage names so the CLI fails fast.

    Pro extension stages become CLI-addressable through this function —
    e.g. ``--review-stages script,voiceover`` works once those stages
    are registered via the ``shuttleslide.review.stages`` entry point.
    """
    known = set(default_registry().all_names())
    if spec is None or spec.strip() == "" or spec.strip().lower() == "all":
        return known
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    invalid = [p for p in parts if p not in known]
    if invalid:
        raise ValueError(
            f"unknown review stage(s) {invalid!r}; valid stages: {sorted(known)}"
        )
    return set(parts)


class InteractiveOrchestrator(AgentOrchestrator):
    """AgentOrchestrator that pauses between stages for human review.

    Constructor parameters beyond the base class:
        gate: ReviewGate instance shared with the web server (PR2) or
              driven directly by tests (PR1).
        review_stages: which stages trigger a pause. Use
                       ``set(default_registry().all_names())`` for all,
                       or parse from a CLI flag via ``_parse_review_stages``.
        auto_approve: if True, skip the gate entirely. Used by the
                      regression test to verify this subclass produces
                      identical output to the base class.
    """

    def __init__(
        self,
        config: AgentConfig,
        gate: ReviewGate,
        review_stages: Optional[Set[StageName]] = None,
        auto_approve: bool = False,
        broadcaster: Optional[Broadcaster] = None,
        registry: Optional[ToolRegistry] = None,
        renderer: Optional[SlideHTMLRenderer] = None,
        stage_registry: Optional[StageRegistry] = None,
        state_cache_path: Optional[Path] = None,
        load_state_on_start: bool = False,
    ) -> None:
        super().__init__(
            config=config,
            registry=registry,
            renderer=renderer,
            stage_registry=stage_registry,
        )
        self.gate = gate
        # Default to reviewing all registered stages. The set is built
        # from self._stages (the resolved list) so pro stages picked up
        # via entry points are included by default.
        self.review_stages: Set[str] = (
            {s.name for s in self._stages}
            if review_stages is None
            else set(review_stages)
        )
        self.auto_approve = auto_approve
        # Optional broadcaster (PR2: ReviewServer). When None, the hook is
        # a pure gate pause — preserving PR1's behaviour exactly so the
        # existing test suite keeps passing without modification.
        self.broadcaster: Optional[Broadcaster] = broadcaster
        # State persistence — when ``state_cache_path`` is set, the hook
        # saves state to disk after every stage. ``load_state_on_start``
        # short-circuits _prepare_state to load from disk; combined with
        # the _pre_stage_hook override below, this skips LLM work for
        # any stage whose output is already in the loaded state.
        self._state_cache_path: Optional[Path] = (
            Path(state_cache_path) if state_cache_path is not None else None
        )
        self._load_state_on_start: bool = bool(load_state_on_start)
        # Snapshot of which stages had their output already populated
        # at load time. Populated by _prepare_state (load branch) BEFORE
        # any stage runs; consulted by _post_stage_hook to decide
        # whether to skip the gate pause. Empty in non-load mode and
        # before the load branch runs — no pauses skipped in either
        # case.
        self._cached_stages_from_load: Set[str] = set()

    # ------------------------------------------------------------------
    # _prepare_state — load-from-disk path
    # ------------------------------------------------------------------

    async def _prepare_state(
        self,
        topic: Optional[str],
        style_hint: Optional[str],
        target_count: Optional[int],
    ) -> AgentState:
        """Build state fresh OR hydrate from disk depending on config.

        When ``load_state_on_start`` is True and the cache file exists,
        we bypass ``_make_state`` entirely and load the saved state.
        ``_pre_stage_hook`` then short-circuits each stage whose data is
        already populated. Falls through to the base implementation if
        the cache is missing or loading is disabled — so a missing file
        behaves like a fresh run.
        """
        if not (
            self._load_state_on_start
            and self._state_cache_path is not None
            and self._state_cache_path.exists()
        ):
            return await super()._prepare_state(
                topic=topic, style_hint=style_hint, target_count=target_count
            )

        state = load_state(self._state_cache_path)
        # Re-stamp inputs from this run's CLI args — matches what
        # _make_state would have set, so config overrides still apply.
        state.topic = topic if topic is not None else state.topic
        state.style_hint = (
            style_hint if style_hint is not None else state.style_hint
        )
        state.target_count = (
            target_count if target_count is not None else state.target_count
        )
        # Snapshot which stages came from the cache BEFORE any work
        # runs. _pre_stage_hook re-asked later (in _post_stage_hook)
        # would return True for stages that ran fresh — their state
        # was just populated by the stage itself — which incorrectly
        # skips the gate pause for those stages. This snapshot is
        # the source of truth for "should we skip the pause?".
        self._cached_stages_from_load = {
            stage.name for stage in self._stages if stage.is_cached(state)
        }
        return state

    # ------------------------------------------------------------------
    # Hooks — delegated to the Stage objects
    # ------------------------------------------------------------------

    async def _pre_stage_hook(self, stage: Stage, state: AgentState) -> bool:
        """Skip work when the stage's output is already in state.

        Only active when ``_state_cache_path`` is configured — stub-test
        and production paths (no cache) always run the stage. Delegates
        to ``stage.is_cached(state)`` so each stage owns its own
        "what does my output look like in state?" predicate.
        """
        if self._state_cache_path is None:
            return False
        return stage.is_cached(state)

    async def _post_stage_hook(self, stage: Stage, state: AgentState) -> None:
        """Pause for review after each stage (if enabled for this stage).

        Broadcast logic (PR2): when a ``broadcaster`` is wired up, every
        stage — even ones not in ``review_stages`` — emits a
        ``stage_complete`` so the UI can show progress. The pause is still
        gated by ``review_stages``; only the broadcast is unconditional.

        Stage contract: ``stage.build_snapshot(state)`` returns None for
        silent stages (no review UI). We synthesise a minimal snapshot
        in that case so the broadcaster still has something to emit.
        """
        if self.auto_approve:
            # Still save state — auto_approve is a test affordance, not
            # a "skip everything" switch. The regression test that uses
            # auto_approve doesn't configure state_cache_path, so the
            # save is a no-op there.
            self._save_state(state)
            return
        stage_name = stage.name

        # Build snapshot via the stage's own method. Silent stages
        # return None; we synthesise a minimal progress snapshot so
        # the broadcaster still has something to emit (UI shows a
        # greyed-out "ran without review" affordance).
        snapshot = stage.build_snapshot(state)
        if snapshot is None:
            snapshot = StageSnapshot(
                stage=stage_name,
                state_view={},
                artifact_kind="mixed",
                editable_targets=[],
                timestamp=time.time(),
            )

        if self.broadcaster is not None:
            self.broadcaster.emit_stage_complete(snapshot)
            # pipeline_done fires after the terminal stage instead of
            # being hardcoded to "rendered". Pro stages that slot
            # after rendered (e.g. voiceover) still run before the
            # pipeline_done signal goes out.
            if stage.terminal:
                paths: List[str] = (
                    [str(p) for p in state.html_paths]
                    if state.html_paths
                    else []
                )
                self.broadcaster.emit_pipeline_done(paths)

        # Persist state BEFORE the pause — if the user closes the browser
        # mid-review (or the process dies), the next run can resume from
        # this stage. Save happens for both reviewed and non-reviewed
        # stages; the cache file is the source of truth for resume.
        self._save_state(state)

        if stage_name not in self.review_stages:
            return
        # When a state is loaded from disk (Load run in the review UI),
        # skip the gate pause for stages that were cached at load time.
        # "Load run" of a fully-completed prior run is a browse-only
        # affordance — forcing re-approve of every stage is wrong UX.
        # Stages the loaded state did NOT have populated still pause,
        # so partial loads resume interactively from the first missing
        # stage.
        #
        # Boundary exception: if THIS stage is cached but the NEXT
        # stage is fresh, pause anyway. Without this, loading a partial
        # run skips straight from cached stages into the first fresh
        # stage with no chance to review what was loaded — the user
        # clicks Load and immediately sees "slides running" without
        # ever getting to look at theme/outline/images first.
        if stage_name in self._cached_stages_from_load:
            idx = self._stage_index(stage_name)
            next_stage_name = (
                self._stages[idx + 1].name
                if idx is not None and idx + 1 < len(self._stages)
                else None
            )
            if (
                next_stage_name is None
                or next_stage_name in self._cached_stages_from_load
            ):
                return
            # else: next stage will run fresh → fall through to pause
            # so the user can review the loaded state first.
        action: ReviewAction = await self.gate.pause_for_review(snapshot)
        if action == "cancel":
            if self.broadcaster is not None:
                self.broadcaster.emit_error(
                    f"pipeline cancelled by reviewer at stage {stage_name!r}",
                    fatal=True,
                )
            raise ReviewCancelledError(stage_name)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _stage_index(self, name: str) -> Optional[int]:
        """Index of ``name`` in the resolved stage order.

        Returns None if not found (defensive — every reviewed stage
        should be in ``self._stages`` by construction).
        """
        for i, s in enumerate(self._stages):
            if s.name == name:
                return i
        return None

    def _save_state(self, state: AgentState) -> None:
        """Write ``state`` to ``self._state_cache_path`` if configured.

        No-op when caching is disabled (stub-test / production path).
        Errors are surfaced as warnings on ``state`` rather than raised
        — a failed save shouldn't kill the pipeline, and the user can
        re-run to regenerate state.
        """
        if self._state_cache_path is None:
            return
        try:
            save_state(state, self._state_cache_path)
        except Exception as exc:  # pragma: no cover - defensive
            state.add_warning(f"state save failed: {exc}")
