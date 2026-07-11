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

import asyncio
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from shuttleslide.agent.config import AgentConfig
from shuttleslide.agent.orchestrator import AgentOrchestrator, OrchestratorResult
from shuttleslide.agent.review.broadcaster import Broadcaster
from shuttleslide.agent.review.editors import EditorRegistry, default_editors
from shuttleslide.agent.review.registry import StageRegistry, default_registry
from shuttleslide.agent.review.review_gate import (
    EditTarget,
    ReviewAction,
    ReviewGate,
    StageName,
    StageSnapshot,
)
from shuttleslide.agent.review.sessions import SessionStore
from shuttleslide.agent.review.snapshots import UndoStack, build_snapshot
from shuttleslide.agent.review.stale import StaleStore
from shuttleslide.agent.review.stale_propagation import build_edit_event, compute_stale_marks
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


def _restore_image_path(target, old_path: str, state) -> "EditResult":
    """Best-effort undo for an image edit: point the slot back at ``old_path``.

    Image undo cannot restore original bytes — those are gone once the
    user uploaded a replacement (the file path is deterministic, so a
    same-slot overwrite loses the prior bytes). What we CAN do is
    restore the slot's path reference, which is what the slide HTML
    embeds. When the original image was at a different path, this fully
    restores the prior visual. When the original was at the same path
    (overwritten), the HTML still points at the new bytes — best-effort.

    Two empty-slot cases land here, distinguished by ``old_path``:

    - ``old_path`` truthy → slot was *deleted* (popped by ``_delete_image``).
      Undo re-creates the slot pointing at the original path. The file
      on disk is unchanged (delete only pops state, never unlinks), so
      the image fully reappears.
    - ``old_path`` empty → slot was empty before an *upload* landed.
      Undo drops the upload (pops the slot back to empty).
    """
    from shuttleslide.agent.review.editors.base import EditResult

    slide_idx = target.meta.get("slide_idx")
    slot_id = target.meta.get("slot_id")
    if slide_idx is None or slot_id is None:
        return EditResult(ok=False, error="image target missing slide_idx/slot_id")
    slots = state.slide_images.get(int(slide_idx))
    payload = slots.get(slot_id) if slots else None
    if payload is None:
        if old_path:
            # Slot was deleted (popped) — undo re-creates it pointing at
            # the original path. File on disk is unchanged (delete only
            # pops state, never unlinks), so the image fully reappears.
            if slots is None:
                state.slide_images[int(slide_idx)] = {}
                slots = state.slide_images[int(slide_idx)]
            slots[slot_id] = {"path": old_path}
            return EditResult(
                ok=True,
                new_value=old_path,
                assistant_msg=f"Restored path {old_path!r}",
            )
        # Empty slot was the pre-edit state — drop the upload entirely.
        if slots:
            slots.pop(slot_id, None)
        return EditResult(ok=True, new_value="", assistant_msg="Reverted to empty slot")
    payload["path"] = old_path
    return EditResult(
        ok=True,
        new_value=old_path,
        assistant_msg=f"Restored path {old_path!r}",
    )


def _read_target_value_for_stale(target: EditTarget, state: AgentState):
    """Read the value targeted by ``target`` from ``state`` in the shape
    :func:`compute_stale_marks` expects.

    Returns the *specific* edited value, not the whole stage's artifact:

      - ``theme``    → full theme dict (theme edits replace the whole dict)
      - ``outline``  → full outline list (outline edits replace the whole list)
      - ``slides``   → ``state.slides[idx].slots['html']`` (the edited slide's HTML)
      - ``images``   → ``state.slide_images[idx][slot_id]`` (the edited slot payload)

    The shape is what the stale propagation rules compare before/after to
    classify the edit and to embed in ``context_snapshot`` for the
    incremental regenerator. Reading the whole stage artifact for
    slides/images would conflate edits to other slides/slots that
    happened earlier — we want the targeted value only.
    """
    stage = target.stage
    meta = target.meta or {}
    if stage == "theme":
        return dict(state.theme)
    if stage == "outline":
        return list(state.outline)
    if stage == "slides":
        idx = meta.get("slide_idx")
        if idx is None:
            return None
        idx = int(idx)
        if 0 <= idx < len(state.slides):
            slide = state.slides[idx]
            if slide is not None and hasattr(slide, "slots"):
                return slide.slots.get("html", "")
        return None
    if stage == "images":
        idx = meta.get("slide_idx")
        slot_id = meta.get("slot_id")
        if idx is None or slot_id is None:
            return None
        slots = state.slide_images.get(int(idx))
        if slots is None:
            return None
        payload = slots.get(slot_id)
        return dict(payload) if isinstance(payload, dict) else payload
    # Extension stages: no stale propagation defined (yet).
    return None


def _write_target_value_for_stale(target: EditTarget, state: AgentState, value) -> bool:
    """Inverse of :func:`_read_target_value_for_stale`.

    Writes ``value`` back to the state slot that ``target`` points at.
    Used by the cancellation rollback path: when an LLM edit is
    cancelled after the editor has already mutated in-memory state
    (e.g. ``SlideEditor._apply`` wrote the new HTML to
    ``slide.slots['html']``) but before ``_save_state`` persists it,
    we restore the pre-edit snapshot to keep the live session's state
    consistent with the (unchanged) on-disk state.

    Returns ``True`` if the write succeeded, ``False`` if the target
    could not be resolved (slide index out of range, missing slot, …)
    — in which case the caller logs but still re-raises the
    cancellation. The unresolved case is rare and indicates the editor
    never reached the in-memory mutation step either.
    """
    stage = target.stage
    meta = target.meta or {}
    if stage == "theme":
        # ``state.theme`` is a dict field; replace its contents rather
        # than rebinding the attribute so any other references to the
        # same dict observe the rollback.
        state.theme.clear()
        if isinstance(value, dict):
            state.theme.update(value)
        return True
    if stage == "outline":
        # ``state.outline`` is a list field — same clear/extend dance.
        state.outline.clear()
        if isinstance(value, list):
            state.outline.extend(value)
        return True
    if stage == "slides":
        idx = meta.get("slide_idx")
        if idx is None:
            return False
        idx = int(idx)
        if not (0 <= idx < len(state.slides)):
            return False
        slide = state.slides[idx]
        if slide is None or not hasattr(slide, "slots"):
            return False
        slide.slots["html"] = value if isinstance(value, str) else ""
        return True
    if stage == "images":
        idx = meta.get("slide_idx")
        slot_id = meta.get("slot_id")
        if idx is None or slot_id is None:
            return False
        slots = state.slide_images.get(int(idx))
        if slots is None:
            return False
        slots[slot_id] = value
        return True
    # Extension stages: no rollback path defined (yet).
    return False


def _propagate_stale_for_target(
    target: EditTarget,
    state: AgentState,
    pre_value,
) -> bool:
    """Compute and merge stale marks into ``state.stale_marks``.

    Called after the editor has mutated ``state`` but before
    :meth:`InteractiveOrchestrator._save_state` — so the persisted state
    carries the new marks. Reads the post-edit value from ``state`` via
    :func:`_read_target_value_for_stale`, builds an :class:`EditEvent`,
    and merges the result into the existing store.

    ``pre_value`` is the value the caller snapshotted *before* the editor
    ran. Caller is responsible for that snapshot — by the time this
    function runs, ``state`` already reflects the post-edit value.

    Returns ``True`` if any new marks were added (caller should broadcast
    a ``stale_marks_updated`` so the UI's badges update). Returns
    ``False`` for no-op edits (theme visual-only, or identical values)
    — caller should skip the broadcast in that case to avoid spurious
    traffic on every visual color tweak.

    Theme visual-only edits short-circuit inside the propagation engine
    (returns empty mark set) — those edits rely on the sibling render
    cascade in :meth:`_refresh_after_edit` instead.
    """
    post_value = _read_target_value_for_stale(target, state)
    event = build_edit_event(
        stage=target.stage,
        before=pre_value,
        after=post_value,
        slide_idx=(target.meta or {}).get("slide_idx"),
        slot_id=(target.meta or {}).get("slot_id"),
        target_path=tuple(target.path),
    )
    new_marks = compute_stale_marks(event)
    if not new_marks:
        return False
    store = StaleStore.from_dict(state.stale_marks)
    for stage_name, marks in new_marks.items():
        store.merge(stage_name, marks)
    state.stale_marks = store.as_dict()
    return True


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
        editor_registry: Optional[EditorRegistry] = None,
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
        # PR3: per-element editors + chat sessions + undo stack.
        # ``_active_state`` holds the state currently under review (set
        # in _prepare_state, cleared in run()'s finally). The server's
        # request_edit handler reads ``orch._active_state`` through
        # apply_edit(); web-client mode shares the server's loop so the
        # call is direct, not cross-loop.
        self._editors: EditorRegistry = (
            editor_registry if editor_registry is not None else default_editors()
        )
        self._sessions: SessionStore = SessionStore()
        self._undo: UndoStack = UndoStack()
        self._active_state: Optional[AgentState] = None
        # Stale-mark regenerator coordinator (PR: stale propagation).
        # Lazy import to keep the module-level import graph clean —
        # item_regenerator imports from stale_propagation, which is
        # already loaded by the time __init__ runs in production, but
        # the lazy form keeps test modules that patch InteractiveOrchestrator
        # from paying for the import when they don't exercise regen.
        from shuttleslide.agent.review.item_regenerator import (
            RegenerateCoordinator,
        )
        self._regenerator: RegenerateCoordinator = RegenerateCoordinator(self)

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
            state = await super()._prepare_state(
                topic=topic, style_hint=style_hint, target_count=target_count
            )
            # PR3: stash the state ref so apply_edit can mutate it from
            # the server's WS handler. Lives until the next run()
            # overwrites it (see run()'s docstring for why we don't
            # clear in finally anymore).
            self._active_state = state
            return state

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
        # PR3: stash the state ref here too — both load paths must set
        # _active_state so apply_edit works regardless of resume mode.
        self._active_state = state
        return state

    async def run(self, *args, **kwargs):
        """Run the pipeline.

        Note: ``_active_state`` is intentionally NOT cleared after run.
        The review server keeps the orchestrator alive after
        pipeline_done so the user can regenerate / edit / undo without
        re-running the pipeline (see ``server.py``'s ``_run_pipeline``
        docstring — "Keeping it alive ... lets the user click
        Regenerate ... without re-running the whole pipeline"). The
        next run's ``_prepare_state`` overwrites ``_active_state``.

        Previously this method wrapped ``super().run()`` in try/finally
        to clear ``_active_state``, but that broke every post-done edit
        path — ``regenerate_item`` / ``apply_edit`` / ``undo_last`` /
        ``revert_to`` / ``unrevert`` all gate on
        ``_active_state is not None`` and returned "no active pipeline
        state" the moment the user clicked any of those buttons after
        the pipeline finished.
        """
        return await super().run(*args, **kwargs)

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
            # pipeline_done fires only after the LAST stage in the
            # resolved order, not after any "terminal" stage. Without
            # this, pro stages that slot after rendered (voiceover,
            # motion_design, render_video) would never get to run — the
            # UI receives pipeline_done at `rendered` and permanently
            # disables the Approve button (see app.js enableApprovalButtons).
            # ``stage.terminal`` is preserved as a property meaning
            # "PPTX export ready" (used by the UI to show the download
            # button immediately on stage_complete for "rendered"), but
            # it no longer doubles as the pipeline-done signal.
            is_last_stage = bool(self._stages) and stage is self._stages[-1]
            if is_last_stage:
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

    async def _on_stage_failed(
        self, stage: Stage, exc: BaseException, state: AgentState
    ) -> None:
        """Broadcast stage failures to connected review clients.

        Without this override, ``_run_pipeline``'s except block only
        lands ``state.add_warning(...)`` — invisible to the UI. The
        pipeline then runs the next stage (or finishes), eventually
        emitting ``pipeline_state="done"`` while the failing stage's
        tab stays empty and the user has no signal about what went
        wrong. The canonical case: ``RenderVideoStage`` raising
        ``VideoSetupError`` / ``VideoRenderError`` after the user
        approves ``motion_design`` — the UI flips to "Pipeline
        complete." with no MP4 and no error trail.

        Non-fatal (``fatal=False``) because the orchestrator continues
        to the next stage by design — earlier stages' artifacts (HTML
        / PPTX export) are still usable. The UI surfaces a banner +
        log entry without flipping pipeline_state.
        """
        if self.broadcaster is None:
            return
        try:
            self.broadcaster.emit_error(
                f"stage {stage.name!r} failed: {exc}",
                fatal=False,
            )
        except Exception:  # pragma: no cover - defensive
            pass

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

    # ------------------------------------------------------------------
    # PR3: per-element editing
    # ------------------------------------------------------------------

    def active_stage_for_review(self) -> Optional[str]:
        """Name of the stage currently paused at the gate, if any.

        The server uses this to know which stage's snapshot to refresh
        after an edit lands. ``None`` when no pause is active.
        """
        if not self.gate.is_paused or self.gate.pending is None:
            return None
        return self.gate.pending.stage

    async def apply_edit(
        self,
        target: EditTarget,
        mode: str,
        payload: dict,
    ):
        """Mutate the active state per ``target`` + ``mode`` + ``payload``.

        Server entry point — called from the WS handler in web-client
        mode (same loop, direct await). Steps:

          1. Verify pipeline is paused at a stage boundary.
          2. Look up the editor for ``target.kind``.
          3. Call apply_direct_edit / apply_llm_edit / image upload.
          4. On success: push undo, save state, rebuild + re-emit snapshot.
          5. On failure: return the EditResult error verbatim.

        Returns the editor's ``EditResult``. The server turns that into
        ``edit_applied`` / ``edit_rejected`` for the requesting client.
        """
        # Local import to avoid a circular dependency at module load.
        from shuttleslide.agent.review.editors.base import EditResult

        if self._active_state is None:
            return EditResult(
                ok=False, error="no active pipeline state to edit"
            )
        if not self.gate.is_paused:
            return EditResult(
                ok=False,
                error=(
                    "pipeline is not paused at a stage boundary — wait for "
                    "a stage_complete event before editing"
                ),
            )
        editor = self._editors.get(target.kind)
        if editor is None:
            return EditResult(
                ok=False,
                error=(
                    f"no editor registered for kind {target.kind!r}; "
                    f"registered: {self._editors.kinds()}"
                ),
            )
        state = self._active_state
        old_value = target.current_value

        # Snapshot the targeted value BEFORE the editor runs. After the
        # editor returns we read the post-edit value from ``state`` and
        # feed both into the stale propagation engine. Stale marks land
        # on downstream stages (images/slides/rendered) whose generated
        # output may now be out-of-date relative to this upstream change.
        #
        # Snapshot is taken unconditionally — even no-op edits are cheap
        # to read, and the propagation engine handles no-ops cleanly
        # (returns an empty mark set).
        pre_edit_value = _read_target_value_for_stale(target, state)

        # Image uploads carry a dict payload (bytes + source_ref) rather
        # than a string. JsonEditor / SvgEditor / SlideEditor all want
        # strings; route the image case to its own direct path.
        if target.kind == "image":
            if mode != "direct":
                return EditResult(
                    ok=False,
                    error="image slots only support direct upload (use Upload)",
                )
            try:
                result = await editor.apply_direct_edit(
                    target, payload, state, self.config
                )
            except Exception as exc:
                return EditResult(ok=False, error=f"editor raised: {exc}")
        elif mode == "direct":
            new_value = payload.get("new_value", "")
            try:
                result = await editor.apply_direct_edit(
                    target, new_value, state, self.config
                )
            except Exception as exc:
                return EditResult(ok=False, error=f"editor raised: {exc}")
        elif mode == "llm":
            history = self._sessions.get(target.path)
            user_message = payload.get("user_message", "")
            try:
                result = await editor.apply_llm_edit(
                    target,
                    user_message,
                    history,
                    state,
                    self.config,
                )
            except asyncio.CancelledError:
                # User cancelled mid-LLM. The editor may have already
                # mutated in-memory state (e.g. SlideEditor._apply wrote
                # the new HTML on the last successful retry iteration)
                # — roll back to the pre-edit snapshot so the live
                # session's state matches the unchanged on-disk state.
                # _save_state below was never reached, so the snapshot
                # taken at line 597 (pre_edit_value) is authoritative.
                _write_target_value_for_stale(target, state, pre_edit_value)
                raise
            except Exception as exc:
                return EditResult(ok=False, error=f"editor raised: {exc}")
            # Only successful LLM edits get appended to chat history.
            if result.ok and result.assistant_msg:
                self._sessions.append(target.path, "user", user_message)
                self._sessions.append(
                    target.path, "assistant", result.assistant_msg
                )
                # Broadcast the updated chat history so the chat panel
                # surfaces the assistant's natural-language reply without
                # the user needing to switch targets to trigger a refresh.
                # Mirrors emit_history_snapshot / emit_stale_marks: the
                # orchestrator owns the moment-of-change, the broadcaster
                # delivers to every connected client (and replays to late
                # joiners via _early_messages on the server).
                if self.broadcaster is not None:
                    history = self._sessions.get(target.path)
                    self.broadcaster.emit_chat_history(
                        target.path,
                        [
                            {"role": m["role"], "body": m["content"]}
                            for m in history
                        ],
                    )
        else:
            return EditResult(
                ok=False, error=f"unknown edit mode {mode!r}"
            )

        if not result.ok:
            return result

        # No-op detection: if the editor reports the value didn't change
        # (post-edit value identical to the pre-edit current_value), skip
        # the undo push, state save, snapshot re-broadcast, AND mark the
        # result so the server sends a no_op EditAppliedMsg ack (rather
        # than the default ack). The History panel stays clean; the
        # client clears any pending indicator without flipping the
        # "edited" flag.
        if (
            result.new_value is not None
            and old_value is not None
            and result.new_value == old_value
        ):
            return EditResult(
                ok=True,
                new_value=result.new_value,
                no_op=True,
            )

        # Push undo entry with history metadata, persist, re-broadcast.
        action_label, new_value_summary = _describe_edit(target, mode, payload, result)
        self._undo.push(
            target,
            old_value,
            new_value=result.new_value or "",
            new_value_summary=new_value_summary,
            action_label=action_label,
        )
        # Propagate stale marks to downstream stages BEFORE _save_state so
        # the marks land in the persisted agent_state.json. Theme visual-
        # only edits short-circuit inside the engine (no marks emitted) —
        # the live render cascade in _refresh_after_edit handles those.
        marks_changed = _propagate_stale_for_target(target, state, pre_edit_value)
        self._save_state(state)
        # Give the owning stage a chance to regenerate derived artifacts
        # (preview HTML files, cached renders, ...) BEFORE the snapshot
        # is rebuilt and re-emitted — otherwise the UI would re-render
        # with stale on-disk files. Errors here are non-fatal: the edit
        # itself has already succeeded + persisted, so we surface a
        # broadcaster warning instead of failing the apply_edit call.
        await self._invoke_post_edit_refresh(target, state)
        await self._refresh_after_edit(target.stage, state)
        await self._broadcast_history()
        if marks_changed:
            await self._broadcast_stale_marks()
        return result

    async def undo_last(self, target_path: tuple):
        """Pop the most recent undo entry and re-apply the old value.

        ``target_path`` is checked for diagnostics only — the undo
        stack is global, not per-target. If the most recent edit was
        to a different target, we still undo it (the UI's undo button
        shows "undo last edit", not "undo on this target").
        """
        from shuttleslide.agent.review.editors.base import EditResult

        entry = self._undo.pop_entry()
        if entry is None:
            return EditResult(ok=False, error="nothing to undo")
        target, old_value = entry.target, entry.old_value

        if self._active_state is None:
            # Re-push; state is gone so we can't apply. Defensive —
            # the server shouldn't expose undo when no state is active.
            self._undo.push_entry(entry)
            return EditResult(ok=False, error="no active pipeline state")

        editor = self._editors.get(target.kind)
        if editor is None:
            self._undo.push_entry(entry)
            return EditResult(
                ok=False,
                error=f"editor for kind {target.kind!r} unavailable",
            )
        # Snapshot pre-undo value so we can propagate stale marks for
        # the value change "post-edit → pre-edit" — downstream stages
        # may now be stale relative to the restored upstream value.
        pre_undo_value = _read_target_value_for_stale(target, self._active_state)
        try:
            if target.kind == "image":
                # Image undo: the old_value is a path string, not bytes.
                # We can't re-run apply_direct_edit (it would need the
                # original bytes). Restore by pointing the slot back at
                # the old path — the file on disk is unchanged when the
                # previous image lived at a different path. When the
                # previous image lived at the SAME path (overwritten),
                # undo is best-effort: state will show the new image
                # until the user re-uploads. This is documented in
                # apply_direct_edit's docstring.
                result = _restore_image_path(
                    target, old_value, self._active_state
                )
            else:
                result = await editor.apply_direct_edit(
                    target, old_value, self._active_state, self.config
                )
        except Exception as exc:
            self._undo.push_entry(entry)
            return EditResult(ok=False, error=f"undo raised: {exc}")
        if result.ok:
            # Treat undo as another edit for stale propagation purposes —
            # the upstream value changed (back), so downstream stale
            # marks may need to update. The engine dedups per target_id,
            # so re-marking an already-stale slide just refreshes its
            # reason/timestamp without producing duplicates.
            marks_changed = _propagate_stale_for_target(
                target, self._active_state, pre_undo_value
            )
            self._save_state(self._active_state)
            await self._refresh_after_edit(target.stage, self._active_state)
            await self._broadcast_history()
            if marks_changed:
                await self._broadcast_stale_marks()
        return result

    async def revert_to(self, entry_idx: int):
        """Apply a history entry's ``old_value``, marking the card as
        pending-revert (client-side state).

        Non-destructive: the entry stays in the stack so the client can
        render Undo / Commit affordances on the same card. ``Undo`` calls
        ``unrevert`` (re-applies ``new_value``); ``Commit`` calls
        ``delete_history_entry`` (permanently removes the entry).
        """
        from shuttleslide.agent.review.editors.base import EditResult

        target_entry = self._undo.peek_at(entry_idx)
        if target_entry is None:
            total = len(self._undo.entries())
            return EditResult(
                ok=False,
                error=f"history entry {entry_idx} out of range "
                f"(have {total} entries)",
            )
        target = target_entry.target
        old_value = target_entry.old_value

        if self._active_state is None:
            return EditResult(ok=False, error="no active pipeline state")

        editor = self._editors.get(target.kind)
        if editor is None:
            return EditResult(
                ok=False,
                error=f"editor for kind {target.kind!r} unavailable",
            )
        pre_revert_value = _read_target_value_for_stale(target, self._active_state)
        try:
            if target.kind == "image":
                result = _restore_image_path(
                    target, old_value, self._active_state
                )
            else:
                result = await editor.apply_direct_edit(
                    target, old_value, self._active_state, self.config
                )
        except Exception as exc:
            return EditResult(ok=False, error=f"revert raised: {exc}")
        if result.ok:
            # No ack push — the entry stays so the client can show
            # Undo / Commit on the same card. Just persist + re-broadcast
            # so the preview reflects the reverted value.
            marks_changed = _propagate_stale_for_target(
                target, self._active_state, pre_revert_value
            )
            self._save_state(self._active_state)
            await self._refresh_after_edit(target.stage, self._active_state)
            await self._broadcast_history()
            if marks_changed:
                await self._broadcast_stale_marks()
        return result

    async def unrevert(self, entry_idx: int):
        """Re-apply a reverted entry's ``new_value`` (un-revert).

        Pairs with ``revert_to``: after the user clicks Restore, the
        card's Undo button calls this to put the value back to the
        post-edit state. The entry stays in the stack (back to normal
        Restore affordance).
        """
        from shuttleslide.agent.review.editors.base import EditResult

        target_entry = self._undo.peek_at(entry_idx)
        if target_entry is None:
            total = len(self._undo.entries())
            return EditResult(
                ok=False,
                error=f"history entry {entry_idx} out of range "
                f"(have {total} entries)",
            )
        if not target_entry.new_value:
            return EditResult(
                ok=False,
                error="entry has no stored new_value (older edit; cannot un-revert)",
            )
        target = target_entry.target
        new_value = target_entry.new_value
        if self._active_state is None:
            return EditResult(ok=False, error="no active pipeline state")
        editor = self._editors.get(target.kind)
        if editor is None:
            return EditResult(
                ok=False,
                error=f"editor for kind {target.kind!r} unavailable",
            )
        pre_unrevert_value = _read_target_value_for_stale(target, self._active_state)
        try:
            if target.kind == "image":
                result = _restore_image_path(
                    target, new_value, self._active_state
                )
            else:
                result = await editor.apply_direct_edit(
                    target, new_value, self._active_state, self.config
                )
        except Exception as exc:
            return EditResult(ok=False, error=f"unrevert raised: {exc}")
        if result.ok:
            # Same propagation logic as revert_to — the upstream value
            # changed (this time forward rather than back), so downstream
            # marks may need to refresh. The engine dedups per target_id,
            # so an already-stale slide just gets an updated reason.
            marks_changed = _propagate_stale_for_target(
                target, self._active_state, pre_unrevert_value
            )
            self._save_state(self._active_state)
            await self._refresh_after_edit(target.stage, self._active_state)
            await self._broadcast_history()
            if marks_changed:
                await self._broadcast_stale_marks()
        return result

    async def regenerate_item(
        self,
        stage: str,
        target_id: str,
        *,
        mode: str = "incremental",
        ref_id: str = "",
    ):
        """Dispatch a per-item regenerate to the RegenerateCoordinator.

        Snapshots the pre-regenerate value into the UndoStack so the
        user can undo the regenerate (same UX as undoing a manual edit).
        On success, broadcasts ItemRegeneratedMsg + StaleMarksUpdatedMsg;
        on failure, broadcasts EditRejectedMsg via the broadcaster.

        Returns the coordinator's :class:`RegenerateResult` so the WS
        handler can craft the appropriate ack.
        """
        from shuttleslide.agent.review.editors.base import EditResult
        from shuttleslide.agent.review.item_regenerator import parse_target_id

        if self._active_state is None:
            return EditResult(ok=False, error="no active pipeline state")

        # Snapshot the pre-regenerate value for undo. We build a
        # synthetic EditTarget so the existing UndoStack infrastructure
        # works without special-casing. The "before" value is whatever
        # the targeted stage currently holds (e.g. the slide HTML).
        slide_idx, slot_id = parse_target_id(target_id)
        pre_value = None
        if slide_idx is not None:
            if stage == "slides" and 0 <= slide_idx < len(self._active_state.slides):
                slide = self._active_state.slides[slide_idx]
                if slide is not None and hasattr(slide, "slots"):
                    pre_value = slide.slots.get("html", "")
            elif stage == "images":
                slots = self._active_state.slide_images.get(slide_idx, {})
                if slot_id is not None:
                    pre_value = slots.get(slot_id)
                else:
                    pre_value = dict(slots) if slots else None
            elif stage == "rendered" and 0 <= slide_idx < len(self._active_state.html_paths):
                pre_value = self._active_state.html_paths[slide_idx]

        result = await self._regenerator.dispatch(
            stage=stage,
            target_id=target_id,
            mode=mode,
            ref_id=ref_id,
        )
        if result.ok:
            # Push a synthetic history entry so undo works. We use
            # kind="html" for slides/rendered (those editors know how
            # to apply_direct_edit on HTML) and kind="image" for images.
            # The target.path matches what the slide-builder stage
            # emits so existing editor dispatch routes correctly.
            if slide_idx is not None and pre_value is not None:
                kind = "image" if stage == "images" else "html"
                if stage == "slides":
                    path_tuple = ("slide", slide_idx, "html")
                    meta = {"slide_idx": slide_idx, "slot_id": "html"}
                elif stage == "images":
                    if slot_id is not None:
                        path_tuple = ("slide", slide_idx, "slot", slot_id)
                        meta = {
                            "slide_idx": slide_idx,
                            "slot_id": slot_id,
                            "mime": "",
                            "payload_type": None,
                        }
                    else:
                        path_tuple = ("slide", slide_idx, "images")
                        meta = {"slide_idx": slide_idx}
                else:  # rendered
                    path_tuple = ("slide", slide_idx, "rendered")
                    meta = {"slide_idx": slide_idx}

                from shuttleslide.agent.review.review_gate import EditTarget

                synthetic_target = EditTarget(
                    stage=stage,
                    path=path_tuple,
                    kind=kind,
                    current_value=pre_value,  # value AFTER regen
                    meta=meta,
                )
                self._undo.push(
                    synthetic_target,
                    pre_value,  # the pre-regenerate value
                    new_value=pre_value,  # placeholder; not used by undo path
                    action_label=f"regenerate {stage} {target_id} ({mode})",
                )

            # Refresh downstream views so the UI shows the new value.
            await self._refresh_after_edit(stage, self._active_state)
            await self._broadcast_history()
            await self._broadcast_stale_marks()

            # ItemRegeneratedMsg — only if broadcaster supports it.
            if self.broadcaster is not None:
                emit_item = getattr(self.broadcaster, "emit_item_regenerated", None)
                if emit_item is not None:
                    try:
                        emit_item(
                            ref_id=ref_id,
                            stage=stage,
                            target_id=target_id,
                            snapshot=result.snapshot,
                            remaining_marks=result.remaining_marks,
                        )
                    except Exception as exc:  # pragma: no cover
                        self.broadcaster.emit_error(
                            f"item_regenerated broadcast failed: {exc}",
                            fatal=False,
                        )
        else:
            # Surface failure via the existing error channel.
            if self.broadcaster is not None:
                try:
                    self.broadcaster.emit_error(
                        f"regenerate {stage}:{target_id} failed: {result.error}",
                        fatal=False,
                    )
                except Exception:  # pragma: no cover
                    pass
        return result

    async def dismiss_stale(
        self, stage: str, target_id: str, *, ref_id: str = ""
    ) -> bool:
        """Dismiss a stale mark without regenerating.

        ``target_id="all"`` clears every mark on the stage; otherwise
        a single ``(stage, target_id)`` mark is removed. Persists state
        + broadcasts StaleMarksUpdatedMsg so every client's badge clears.

        Returns True if any mark was actually removed. Dismissal is
        final — the mark does not come back unless a new upstream edit
        re-triggers it.
        """
        from shuttleslide.agent.review.stale import StaleStore

        if self._active_state is None:
            return False
        store = StaleStore.from_dict(self._active_state.stale_marks)
        if target_id == "all":
            had_any = bool(store.for_stage(stage))
            store.clear_stage(stage)
            removed = had_any
        else:
            removed = store.dismiss(stage, target_id)
        if not removed:
            return False
        self._active_state.stale_marks = store.as_dict()
        self._save_state(self._active_state)
        await self._broadcast_stale_marks()
        return True

    # ------------------------------------------------------------------
    # Add / Delete / Rebalance slides (structural outline mutations)
    # ------------------------------------------------------------------

    async def add_slide(
        self,
        *,
        index: int,
        mode: str,
        payload: dict,
        ref_id: str = "",
    ):
        """Insert a new slide. See :class:`AddSlideMsg` for the contract.

        ``mode="llm"`` drafts the outline entry via the LLM (feeding
        neighbour entries as context). ``mode="manual"`` takes the
        ``payload["entry"]`` dict verbatim after key-shape validation.

        After the entry lands, schedules background generation for the
        new slide's images / HTML / rendered output via the existing
        ``RegenerateCoordinator``. The method itself returns as soon as
        the entry is inserted and the outline snapshot is re-broadcast
        — generation progress streams through normal stage channels.

        Not pushed to UndoStack: structural mutations touch five
        parallel arrays and the existing undo path only restores the
        targeted value. Confirmation is the user's safety net (the WS
        handler asks for confirm on the client).
        """
        import json as _json

        from shuttleslide.agent.review.editors.base import EditResult
        from shuttleslide.agent.review.outline_mutation import insert_slide
        from shuttleslide.agent.review.stale import StaleMark, StaleStore

        if self._active_state is None:
            return EditResult(ok=False, error="no active pipeline state")
        state = self._active_state
        pre_outline = list(state.outline)
        pre_slide_images = dict(state.slide_images)
        pre_slides = list(state.slides)
        pre_html_paths = list(state.html_paths)
        pre_stale = {k: list(v) for k, v in state.stale_marks.items()}

        # Resolve the entry to insert.
        if mode == "llm":
            intent = (payload or {}).get("intent", "") if isinstance(payload, dict) else ""
            if not intent.strip():
                return EditResult(
                    ok=False, error="add_slide: payload.intent is required for llm mode"
                )
            try:
                entry = await self._draft_outline_entry_llm(
                    intent=intent,
                    insert_index=max(0, min(index, len(state.outline))),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return EditResult(
                    ok=False, error=f"add_slide: LLM draft failed: {exc}"
                )
        elif mode == "manual":
            entry = (payload or {}).get("entry") if isinstance(payload, dict) else None
            try:
                _validate_outline_entry(entry)
            except ValueError as exc:
                return EditResult(ok=False, error=f"add_slide: {exc}")
            entry = dict(entry)
            entry.setdefault("_detail_filled", True)
        else:
            return EditResult(ok=False, error=f"add_slide: unknown mode {mode!r}")

        new_idx = insert_slide(state, index, entry)

        # Mark downstream stages stale for the new slide so the badge
        # shows "needs generation" while the background task runs.
        store = StaleStore.from_dict(state.stale_marks)
        now = time.time()
        for stage_name in ("images", "slides", "rendered"):
            store.add(
                stage_name,
                StaleMark(
                    target_id=f"slide:{new_idx}",
                    source_stage="outline",
                    source_id="all",
                    reason="new slide added",
                    created_at=now,
                    context_snapshot=None,
                ),
            )
        state.stale_marks = store.as_dict()

        self._save_state(state)
        await self._refresh_after_edit("outline", state)
        await self._broadcast_stale_marks()

        # Background generation for the new slide. Non-cancellable from
        # the user's perspective except via the global Cancel mechanism
        # (the WS handler registers this as the active cancellable task).
        asyncio.create_task(
            self._build_new_slide(new_idx, ref_id, pre_outline, pre_slides,
                                  pre_slide_images, pre_html_paths, pre_stale)
        )

        return EditResult(
            ok=True,
            new_value=_json.dumps(state.outline, ensure_ascii=False, indent=2),
            assistant_msg=(
                f"Added slide {new_idx + 1}"
                + (f" ({intent[:60]}...)" if mode == "llm" else "")
            ),
        )

    async def delete_slide(self, *, index: int, ref_id: str = ""):
        """Remove the slide at ``index``. See :class:`DeleteSlideMsg`.

        Symmetric to :meth:`add_slide`: drops the outline entry plus
        every parallel-array slot and reindexes stale marks. Not in
        UndoStack (see :meth:`add_slide` for rationale).
        """
        import json as _json

        from shuttleslide.agent.review.editors.base import EditResult
        from shuttleslide.agent.review.outline_mutation import delete_slide as _delete

        if self._active_state is None:
            return EditResult(ok=False, error="no active pipeline state")
        state = self._active_state
        if not (0 <= index < len(state.outline)):
            return EditResult(
                ok=False,
                error=(
                    f"delete_slide: index {index} out of range for outline "
                    f"of length {len(state.outline)}"
                ),
            )

        try:
            _delete(state, index)
        except IndexError as exc:
            return EditResult(ok=False, error=f"delete_slide: {exc}")

        self._save_state(state)
        # All four downstream views change length: re-emit each.
        for stage_name in ("outline", "images", "slides", "rendered"):
            await self._refresh_after_edit(stage_name, state)
        await self._broadcast_stale_marks()

        return EditResult(
            ok=True,
            new_value=_json.dumps(state.outline, ensure_ascii=False, indent=2),
            assistant_msg=f"Deleted slide {index + 1}",
        )

    async def rebalance_outline(self, *, user_hint: str = "", ref_id: str = ""):
        """LLM-rewrite of the entire outline. See :class:`RebalanceOutlineMsg`.

        Every entry's values may change; key sets are enforced to stay
        stable. All downstream stages are marked stale but NOT auto-
        regenerated — the user triggers per-slide Regenerate themselves
        (avoids N×3 LLM calls firing at once).
        """
        import json as _json

        from shuttleslide.agent.review.editors.base import EditResult
        from shuttleslide.agent.review.stale import StaleMark, StaleStore

        if self._active_state is None:
            return EditResult(ok=False, error="no active pipeline state")
        state = self._active_state
        if not state.outline:
            return EditResult(
                ok=False, error="rebalance_outline: outline is empty"
            )

        try:
            new_outline = await self._rewrite_outline_llm(
                current=list(state.outline), user_hint=user_hint
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return EditResult(
                ok=False, error=f"rebalance_outline: LLM rewrite failed: {exc}"
            )

        # Per-entry key check (skip _detail_filled tolerance — LLM may
        # legitimately drop it; we re-add the default below).
        try:
            _enforce_rebalance_keys(new_outline, state.outline)
        except ValueError as exc:
            return EditResult(ok=False, error=f"rebalance_outline: {exc}")

        # Preserve _detail_filled from old entries (LLM may omit it).
        for i, new_entry in enumerate(new_outline):
            if i < len(state.outline) and "_detail_filled" in state.outline[i]:
                new_entry.setdefault(
                    "_detail_filled", state.outline[i]["_detail_filled"]
                )

        state.outline = new_outline

        # Mark every slide stale on every downstream stage so the user
        # sees badges and can Regenerate the ones they care about.
        store = StaleStore.from_dict(state.stale_marks)
        now = time.time()
        for i in range(len(state.outline)):
            for stage_name in ("images", "slides", "rendered"):
                store.add(
                    stage_name,
                    StaleMark(
                        target_id=f"slide:{i}",
                        source_stage="outline",
                        source_id="all",
                        reason="outline rebalanced",
                        created_at=now,
                        context_snapshot=None,
                    ),
                )
        state.stale_marks = store.as_dict()

        self._save_state(state)
        await self._refresh_after_edit("outline", state)
        await self._broadcast_stale_marks()

        return EditResult(
            ok=True,
            new_value=_json.dumps(state.outline, ensure_ascii=False, indent=2),
            assistant_msg=(
                "Rebalanced outline. Regenerate slides to apply changes."
            ),
        )

    async def _build_new_slide(
        self,
        slide_idx: int,
        ref_id: str,
        pre_outline: List,
        pre_slides: List,
        pre_slide_images: dict,
        pre_html_paths: List,
        pre_stale: dict,
    ) -> None:
        """Background generation chain for a freshly inserted slide.

        Runs images → slides → rendered via the existing coordinator.
        Each step clears its own stale mark on success. On any step
        failure, leaves the slide marked stale — the user can retry via
        the existing per-slide Regenerate button.

        Failures are non-fatal: the outline entry is permanent and the
        UI surfaces the stuck badge. The user can also delete the new
        slide if they don't want to deal with it.
        """
        try:
            for stage_name in ("images", "slides", "rendered"):
                result = await self._regenerator.dispatch(
                    stage=stage_name,
                    target_id=f"slide:{slide_idx}",
                    mode="fresh",
                    ref_id=ref_id,
                )
                # Broadcast after every stage attempt (success OR failure).
                # Without this the UI never sees the snapshot refresh /
                # stale-badge clear / per-item completion signal — the
                # coordinator mutates state but does not touch the
                # broadcaster (see RegenerateCoordinator docstring). The
                # regular Regenerate button path gets these via
                # orch.regenerate_item; _build_new_slide bypasses that
                # wrapper, so we replicate the broadcasts inline.
                await self._refresh_after_edit(stage_name, self._active_state)
                await self._broadcast_stale_marks()
                if result.ok:
                    if self.broadcaster is not None:
                        emit_item = getattr(
                            self.broadcaster,
                            "emit_item_regenerated",
                            None,
                        )
                        if emit_item is not None:
                            try:
                                emit_item(
                                    ref_id=ref_id,
                                    stage=stage_name,
                                    target_id=f"slide:{slide_idx}",
                                    snapshot=result.snapshot,
                                    remaining_marks=result.remaining_marks,
                                )
                            except Exception as exc:  # pragma: no cover
                                self.broadcaster.emit_error(
                                    f"new slide {slide_idx + 1}: "
                                    f"broadcast failed: {exc}",
                                    fatal=False,
                                )
                else:
                    if self.broadcaster is not None:
                        self.broadcaster.emit_error(
                            f"new slide {slide_idx + 1}: "
                            f"{stage_name} generation failed: {result.error}",
                            fatal=False,
                        )
                    # Stop the chain — images failure means slides/rendered
                    # have nothing to operate on. Stale badges already
                    # broadcast above stay up so the user can retry via the
                    # per-slide Regenerate button.
                    return
        except asyncio.CancelledError:
            # Roll back the structural insertion so the live state
            # matches the unchanged on-disk state from before add_slide
            # ran. (Disk was already written — we re-save the rolled-
            # back state below.)
            state = self._active_state
            if state is not None:
                state.outline = pre_outline
                state.slides = pre_slides
                state.slide_images = pre_slide_images
                state.html_paths = pre_html_paths
                state.stale_marks = pre_stale
                self._save_state(state)
                for stage_name in ("outline", "images", "slides", "rendered"):
                    await self._refresh_after_edit(stage_name, state)
                await self._broadcast_stale_marks()
            raise
        except Exception as exc:  # pragma: no cover
            if self.broadcaster is not None:
                self.broadcaster.emit_error(
                    f"new slide {slide_idx + 1}: generation crashed: {exc}",
                    fatal=False,
                )

    async def _draft_outline_entry_llm(
        self, *, intent: str, insert_index: int
    ) -> dict:
        """Ask the LLM to draft ONE outline entry fitting ``intent``.

        Feeds neighbour entries (prev / next) as context so the new
        entry's narrative flows. Returns the parsed entry dict.

        Retries up to ``_LLM_DRAFT_MAX_RETRIES`` times on JSON parse
        failure (mirrors JsonEditor.apply_llm_edit's retry pattern).
        """
        import json as _json

        from shuttleslide.agent.review.editors.base import build_llm_client
        from shuttleslide.agent.review.editors.json_editor import _strip_code_fence

        state = self._active_state
        prev_entry = (
            state.outline[insert_index - 1] if 0 < insert_index <= len(state.outline) else None
        )
        next_entry = (
            state.outline[insert_index] if 0 <= insert_index < len(state.outline) else None
        )

        keys = (
            "title", "purpose", "key_points", "layout_hint",
            "images", "_detail_filled"
        )
        system_prompt = (
            "You are drafting ONE outline entry to insert into an existing "
            "presentation deck. The entry must match the schema of the "
            "neighbour entries.\n\n"
            f"Required keys: {list(keys)}\n"
            "- title: short string\n"
            "- purpose: 1-2 sentence string describing the slide's role\n"
            "- key_points: list[str] of 2-5 bullet points\n"
            "- layout_hint: string describing desired layout\n"
            "- images: list of image specs (each with slot_id, aspect_ratio, "
            "image_type, source_type, description, source_ref). Empty list "
            "if no images.\n"
            "- _detail_filled: boolean (set to true)\n\n"
            f"PREVIOUS slide (for narrative context):\n{_json.dumps(prev_entry, ensure_ascii=False, indent=2) if prev_entry else '(none — inserting at start)'}\n\n"
            f"NEXT slide (for narrative context):\n{_json.dumps(next_entry, ensure_ascii=False, indent=2) if next_entry else '(none — appending at end)'}\n\n"
            "Output ONLY the JSON object for the new entry — no prose, no "
            "code fences, no trailing comma."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"User's intent for the new slide:\n\n{intent}"},
        ]
        llm = build_llm_client(self.config)
        last_error = None
        for _ in range(_LLM_DRAFT_MAX_RETRIES + 1):
            try:
                resp = await llm.chat_with_tools(
                    messages=messages,
                    tools=None,
                    temperature=max(0.0, min(1.0, self.config.temperature)),
                    max_tokens=self.config.max_tokens or 4096,
                )
            except Exception as exc:
                raise RuntimeError(f"LLM call failed: {exc}") from exc

            content = (resp.content or "").strip()
            if content.startswith("```"):
                content = _strip_code_fence(content)
            try:
                parsed = _json.loads(content)
            except _json.JSONDecodeError as exc:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Previous response was not valid JSON: {exc.msg}. "
                        f"Output ONLY the JSON object for ONE outline entry — "
                        f"no prose, no code fences."
                    ),
                })
                last_error = f"value is not valid JSON: {exc.msg}"
                continue
            if not isinstance(parsed, dict):
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "Previous response was not a JSON object. Output ONE "
                        "JSON object for the new outline entry."
                    ),
                })
                last_error = "response was not a JSON object"
                continue

            # Normalise: ensure required keys exist; tolerate missing
            # images / _detail_filled so a partial LLM response still
            # produces a usable entry.
            parsed.setdefault("key_points", [])
            parsed.setdefault("images", [])
            parsed.setdefault("_detail_filled", True)
            return parsed

        raise RuntimeError(
            f"After {_LLM_DRAFT_MAX_RETRIES + 1} attempts the LLM still "
            f"produced invalid output. Last error: {last_error}"
        )

    async def _rewrite_outline_llm(
        self, *, current: List[dict], user_hint: str
    ) -> List[dict]:
        """LLM rewrite of the entire outline (Rebalance narrative).

        Returns the new list of entries. The caller enforces key-set
        preservation; we just relay the LLM's output.
        """
        import json as _json

        from shuttleslide.agent.review.editors.base import (
            build_llm_client,
            truncate_for_prompt,
        )
        from shuttleslide.agent.review.editors.json_editor import _strip_code_fence

        current_json = truncate_for_prompt(
            _json.dumps(current, ensure_ascii=False, indent=2)
        )
        system_prompt = (
            "You are revising an entire presentation outline to improve "
            "narrative flow and coherence.\n\n"
            f"Current outline (JSON list):\n\n```json\n{current_json}\n```\n\n"
            "Rules:\n"
            "- Output a JSON LIST of the same length.\n"
            "- Each entry must keep the SAME KEYS as the corresponding input "
            "entry. Do not add, remove, or rename keys.\n"
            "- Improve narrative flow, remove redundancy, tighten purpose "
            "statements, rebalance key_points.\n"
            "- Preserve the user's intent and any specific content the user "
            "called out. Where the user made manual edits earlier, prefer "
            "keeping them.\n"
            "- Output ONLY the JSON list — no prose, no code fences.\n"
            "\n"
            "The output is consumed by an automated JSON parser; any "
            "surrounding prose will break the pipeline."
        )

        user_msg = (
            user_hint.strip()
            if user_hint and user_hint.strip()
            else "Rebalance the narrative flow of this outline."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]
        llm = build_llm_client(self.config)
        last_error = None
        for _ in range(_LLM_DRAFT_MAX_RETRIES + 1):
            try:
                resp = await llm.chat_with_tools(
                    messages=messages,
                    tools=None,
                    temperature=max(0.0, min(1.0, self.config.temperature)),
                    max_tokens=self.config.max_tokens or 8192,
                )
            except Exception as exc:
                raise RuntimeError(f"LLM call failed: {exc}") from exc

            content = (resp.content or "").strip()
            if content.startswith("```"):
                content = _strip_code_fence(content)
            try:
                parsed = _json.loads(content)
            except _json.JSONDecodeError as exc:
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        f"Previous response was not valid JSON: {exc.msg}. "
                        f"Output ONLY the JSON list, no prose, no code fences."
                    ),
                })
                last_error = f"invalid JSON: {exc.msg}"
                continue
            if not isinstance(parsed, list):
                messages.append({"role": "assistant", "content": resp.content or ""})
                messages.append({
                    "role": "user",
                    "content": (
                        "Previous response was not a JSON list. Output the "
                        "complete outline as a JSON list."
                    ),
                })
                last_error = "response was not a list"
                continue
            return parsed

        raise RuntimeError(
            f"After {_LLM_DRAFT_MAX_RETRIES + 1} attempts the LLM still "
            f"produced invalid output. Last error: {last_error}"
        )

    def delete_history_entry(self, entry_idx: int) -> bool:
        """Permanently remove a history entry (Commit on a pending-revert card).

        The value is already at ``old_value`` (revert was applied
        earlier), so this only mutates the stack. Returns True if the
        index was in range.
        """
        return self._undo.remove_entry(entry_idx)

    def get_history(self) -> list:
        """Return edit history as JSON-safe dicts (newest first).

        Each dict: ``{idx, action_label, new_value_summary, timestamp,
        target_path}``. Used by the server to build
        ``HistorySnapshotMsg``.
        """
        return self._undo.as_history_dicts()

    def get_chat_history(self, target_path: tuple) -> list:
        """Return the chat history for ``target_path`` (read-only).

        Used by the server to populate the chat panel when the user
        selects an editable target.
        """
        return list(self._sessions.messages_for(target_path))

    async def _refresh_after_edit(self, stage: str, state: AgentState) -> None:
        """Rebuild the stage snapshot and re-broadcast it.

        All connected clients see the new snapshot and re-render. The
        requesting client gets a per-message ack via the WS handler's
        edit_applied message; that's separate from this full re-emit.

        Cascades: a theme edit changes how every slide renders
        ({{theme.*}} placeholders resolve against the new theme, and
        the free_form template re-computes solid/gradient/geometric
        backgrounds from theme fields). Re-emit the slides snapshot
        too so thumbnails and the big preview reflect the new theme
        without forcing the user to re-open each slide. ``rendered``
        is re-emitted when it has already run (html_paths populated)
        so the export preview also stays consistent.
        """
        if self.broadcaster is None:
            return
        stages_to_refresh = [stage]
        if stage == "theme":
            if state.slides:
                stages_to_refresh.append("slides")
            if state.html_paths:
                stages_to_refresh.append("rendered")
        elif stage == "images":
            # SVG/image edits change bytes served via /files/svgs/... into
            # the slide iframe's <img src>. Re-emit slides + rendered so
            # iframes reload (app.js cache-busts the iframe src) and the
            # /files/ middleware's no-cache header forces the embedded
            # <img> to revalidate against the updated disk file.
            if state.slides:
                stages_to_refresh.append("slides")
            if state.html_paths:
                stages_to_refresh.append("rendered")
        elif stage == "script":
            # Script edits mirror-write the new text into
            # voiceover.last_scripts (script.py:regenerate_item). Re-emit
            # the voiceover snapshot so its textarea reflects the new
            # value without forcing the user to re-run voiceover. Skip
            # if voiceover hasn't run yet (snapshot build would be empty).
            vo_out = (state.stage_outputs or {}).get("voiceover")
            if isinstance(vo_out, dict) and vo_out:
                stages_to_refresh.append("voiceover")
        elif stage == "voiceover":
            # Voiceover's Apply Edit mirrors the script text back into
            # script.slides[N].script AND cascades into
            # SubtitleStage.regenerate_item (which rewrites the .srt +
            # subtitle state). Re-emit both so the UIs stay in sync.
            script_out = (state.stage_outputs or {}).get("script")
            if isinstance(script_out, dict) and script_out:
                stages_to_refresh.append("script")
            sub_out = (state.stage_outputs or {}).get("subtitle")
            if isinstance(sub_out, dict) and sub_out:
                stages_to_refresh.append("subtitle")
        for s in stages_to_refresh:
            try:
                snapshot = build_snapshot(
                    s, state, registry=self._stage_registry_for_snapshot()
                )
            except Exception as exc:
                # Snapshot build failure shouldn't lose the edit. Log via
                # broadcaster and continue with the remaining cascades so
                # a broken downstream stage doesn't mask the upstream
                # snapshot that did succeed.
                self.broadcaster.emit_error(
                    f"snapshot rebuild failed for {s}: {exc}", fatal=False
                )
                continue
            self.broadcaster.emit_stage_complete(snapshot)

    def _stage_registry_for_snapshot(self):
        """The registry ``build_snapshot`` should consult.

        ``self._stages`` was resolved from ``stage_registry`` at
        construction time — we don't keep the original ``StageRegistry``
        around. Rebuild a temporary one from the resolved list so
        snapshots work even for pro-stage extensions.
        """
        from shuttleslide.agent.review.registry import StageRegistry

        reg = StageRegistry()
        for stage in self._stages:
            reg.register(stage)
        return reg

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

    async def _invoke_post_edit_refresh(
        self, target: EditTarget, state: AgentState
    ) -> None:
        """Call ``stage.post_edit_refresh`` for the target's owning stage.

        Generic extension point: stages that maintain on-disk artifacts
        derived from ``state`` (preview HTML files, cached renders, ...)
        override ``post_edit_refresh`` to regenerate them after a
        successful edit. Best-effort — failures surface as broadcaster
        warnings rather than failing the edit, since the edit itself
        has already been persisted by the time this runs.
        """
        stage_obj = None
        for s in self._stages:
            if getattr(s, "name", None) == target.stage:
                stage_obj = s
                break
        if stage_obj is None:
            return
        hook = getattr(stage_obj, "post_edit_refresh", None)
        if hook is None:
            return
        try:
            ctx = self._build_stage_context(state)
        except Exception as exc:  # pragma: no cover - defensive
            if self.broadcaster is not None:
                self.broadcaster.emit_error(
                    f"post_edit_refresh: failed to build stage context for "
                    f"{target.stage!r}: {exc}",
                    fatal=False,
                )
            return
        try:
            await hook(ctx, state, target)
        except Exception as exc:
            # Best-effort: log + broadcast but don't fail the edit.
            if self.broadcaster is not None:
                self.broadcaster.emit_error(
                    f"post_edit_refresh for {target.stage!r} failed: {exc} "
                    f"(edit was applied; derived artifacts may be stale)",
                    fatal=False,
                )

    async def _broadcast_history(self) -> None:
        """Push a fresh ``HistorySnapshotMsg`` to every connected client.

        Keeps the sidebar History panel in sync across multi-client
        sessions (one client edits, all see the new entry). No-op when
        no broadcaster is attached (e.g. test mode).
        """
        if self.broadcaster is None:
            return
        try:
            self.broadcaster.emit_history_snapshot(self.get_history())
        except Exception as exc:  # pragma: no cover - defensive
            self.broadcaster.emit_error(
                f"history broadcast failed: {exc}", fatal=False
            )

    async def _broadcast_stale_marks(self) -> None:
        """Push the current ``stale_marks`` to every connected client.

        Fired after every edit (and after undo / revert) so the UI's
        stale badges update in real time. The wire payload is the
        ``state.stale_marks`` dict verbatim (already JSON-safe via
        StaleMark.to_dict shape). No-op without a broadcaster (test mode).
        """
        if self.broadcaster is None or self._active_state is None:
            return
        try:
            self.broadcaster.emit_stale_marks(self._active_state.stale_marks)
        except Exception as exc:  # pragma: no cover - defensive
            self.broadcaster.emit_error(
                f"stale broadcast failed: {exc}", fatal=False
            )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _describe_edit(target: EditTarget, mode: str, payload: dict, result) -> tuple:
    """Build ``(action_label, new_value_summary)`` for the history entry.

    Best-effort: derives short human-readable labels from the target's
    path / meta and a clipped preview of the new value. The summary
    shows up in the sidebar History panel so the user can tell edits
    apart at a glance.
    """
    path = target.path
    slide_idx = target.meta.get("slide_idx") if target.meta else None
    slot_id = target.meta.get("slot_id") if target.meta else None

    # Build action_label.
    if path and path[0] == "theme":
        action_label = "theme"
    elif path and path[0] == "slide" and len(path) >= 2:
        idx_str = str(slide_idx) if slide_idx is not None else str(path[1])
        if slot_id and slot_id != "html":
            action_label = f"slide {idx_str} · {slot_id}"
        elif target.kind == "svg":
            action_label = f"slide {idx_str} · svg"
        elif target.kind == "image":
            action_label = f"slide {idx_str} · image"
        else:
            action_label = f"slide {idx_str} · html"
    else:
        action_label = "/".join(str(p) for p in path) or "edit"

    # Build new_value_summary.
    mode_tag = "llm" if mode == "llm" else "direct"
    if target.kind == "image":
        mime = (target.meta or {}).get("mime", "") or "image"
        action_tag = "uploaded" if result.ok else "upload failed"
        summary = f"[{mode_tag}] {action_tag} ({mime})"
    else:
        new_val = payload.get("new_value", "") if mode == "direct" else ""
        if not new_val and mode == "llm":
            user_msg = payload.get("user_message", "")
            new_val = user_msg
        if not new_val and result.new_value:
            new_val = result.new_value
        if new_val:
            clipped = new_val if len(new_val) <= 60 else new_val[:57] + "..."
            summary = f"[{mode_tag}] {clipped}"
        else:
            summary = f"[{mode_tag}]"

    return action_label, summary


# ---------------------------------------------------------------------------
# Outline structural-edit helpers
# ---------------------------------------------------------------------------
#
# Module-level so unit tests can exercise the validation logic without
# spinning up an InteractiveOrchestrator (which needs an event loop and
# a registry). Keep them pure (no I/O, no LLM, no orchestrator state).

# How many times the LLM-drafted add-slide / rebalance calls will retry
# after a JSON parse / shape failure before giving up. Mirrors the
# JsonEditor's _LLM_EDIT_MAX_RETRIES so every LLM-aware code path shares
# the same retry budget.
_LLM_DRAFT_MAX_RETRIES = 3

# Keys every outline entry must have. ``_detail_filled`` is a pipeline-
# internal progress flag and may be absent on user-supplied entries
# (the form doesn't render it); ``add_slide`` adds the default on insert.
_OUTLINE_REQUIRED_KEYS = ("title", "purpose", "key_points", "layout_hint", "images")
_OUTLINE_OPTIONAL_KEYS = {"_detail_filled"}


def _validate_outline_entry(entry: Any) -> None:
    """Raise ``ValueError`` if ``entry`` is not a usable outline entry.

    Used by ``add_slide(mode="manual")`` to fail fast on malformed
    payloads. Accepts dicts whose key set is a superset of
    ``_OUTLINE_REQUIRED_KEYS`` (extra keys are tolerated — forwarded
    verbatim to ``state.outline``).
    """
    if not isinstance(entry, dict):
        raise ValueError(
            f"entry must be a JSON object, got {type(entry).__name__}"
        )
    missing = [k for k in _OUTLINE_REQUIRED_KEYS if k not in entry]
    if missing:
        raise ValueError(f"entry missing required keys: {missing}")
    if not isinstance(entry.get("key_points"), list):
        raise ValueError("entry.key_points must be a list")
    if not isinstance(entry.get("images"), list):
        raise ValueError("entry.images must be a list")
    if not isinstance(entry.get("title"), str):
        raise ValueError("entry.title must be a string")
    if not isinstance(entry.get("purpose"), str):
        raise ValueError("entry.purpose must be a string")
    if not isinstance(entry.get("layout_hint"), str):
        raise ValueError("entry.layout_hint must be a string")


def _enforce_rebalance_keys(
    new_outline: List[Dict[str, Any]], old_outline: List[Dict[str, Any]]
) -> None:
    """Reject rebalance output that changes the outline's shape.

    Differs from ``_enforce_outline_entry_keys`` in json_editor: rebalance
    may legitimately change VALUES but the per-entry key SET must stay
    identical (the LLM is told this in the system prompt). Length must
    also match — the rebalance flow is value-only, not structural.

    Tolerates ``_detail_filled`` on either side.
    """
    if not isinstance(new_outline, list):
        raise ValueError("rebalance output must be a list")
    if len(new_outline) != len(old_outline):
        raise ValueError(
            f"rebalance output length changed: {len(old_outline)} -> "
            f"{len(new_outline)} (length must stay the same)"
        )
    for i, (new_entry, old_entry) in enumerate(zip(new_outline, old_outline)):
        if not isinstance(new_entry, dict):
            raise ValueError(
                f"rebalance output entry {i} must be an object, got "
                f"{type(new_entry).__name__}"
            )
        old_keys = {
            k for k in old_entry.keys() if k not in _OUTLINE_OPTIONAL_KEYS
        }
        new_keys = {
            k for k in new_entry.keys() if k not in _OUTLINE_OPTIONAL_KEYS
        }
        if old_keys != new_keys:
            missing = sorted(old_keys - new_keys)
            extra = sorted(new_keys - old_keys)
            parts = []
            if missing:
                parts.append(f"missing: {missing}")
            if extra:
                parts.append(f"extra: {extra}")
            raise ValueError(
                f"rebalance changed entry {i} keys ({'; '.join(parts)})"
            )
