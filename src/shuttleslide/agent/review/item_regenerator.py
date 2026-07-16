"""Per-item regeneration coordinator for the stale-mark system.

Pairs with the stale propagation engine in ``stale_propagation.py``.
When the user clicks "Update this slide" on a stale badge, the WS
handler calls :meth:`RegenerateCoordinator.dispatch` here. The
coordinator:

1. Acquires a per-target lock (two clients can't regenerate the same
   slide at the same time — they'd race on ``state.slides[i]``).
2. Validates the stage supports per-item regeneration; falls back to
   a clear error rather than a silent no-op.
3. Snapshots the pre-regenerate value into the orchestrator's
   UndoStack so the user can undo the regenerate.
4. Calls the stage's ``regenerate_item``.
5. Clears the matching stale mark (and any finer-grained marks for
   the same slide — regenerating ``slide:2`` clears ``slide:2:slot:*``).
6. Propagates downstream stale marks (regenerating slides[i] marks
   rendered[i] stale; regenerating images[i] marks slides[i] +
   rendered[i] stale).
7. Returns a structured result the orchestrator broadcasts via
   :class:`ItemRegeneratedMsg`.

Locks are keyed by ``(stage, target_id)`` and live on the coordinator
instance — one per orchestrator. A lock held by a long LLM call blocks
only subsequent regenerations of the same target, not other targets.

Why a separate class (not just methods on the orchestrator)?
- Keeps the regeneration strategy in one file (testable in isolation).
- Avoids growing ``interactive_orchestrator.py`` further — it already
  has ~1000 lines.
- The coordinator is stateless except for the lock dict; multiple
  orchestrators can share one if needed (unusual but supported).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

from shuttleslide.agent.review.stale import StaleStore


# target_id grammar: "all" | "slide:N" | "slide:N:slot:ID"
_SLIDE_RE = re.compile(r"^slide:(\d+)$")
_SLIDE_SLOT_RE = re.compile(r"^slide:(\d+):slot:(.+)$")


def parse_target_id(target_id: str) -> Tuple[Optional[int], Optional[str]]:
    """Return ``(slide_idx, slot_id)`` parsed from a target_id.

    ``"all"`` → ``(None, None)`` — caller branches on the literal
    ``"all"`` separately (it means "every item in the stage", not a
    specific slide).
    ``"slide:3"`` → ``(3, None)``.
    ``"slide:3:slot:hero"`` → ``(3, "hero")``.

    Returns ``(None, None)`` for malformed target_ids rather than
    raising — the dispatch path produces a clear user-facing error
    which is friendlier than a regex stack trace.
    """
    if target_id == "all":
        return None, None
    m = _SLIDE_SLOT_RE.match(target_id)
    if m:
        return int(m.group(1)), m.group(2)
    m = _SLIDE_RE.match(target_id)
    if m:
        return int(m.group(1)), None
    return None, None


@dataclass
class RegenerateResult:
    """Outcome of a regenerate_item dispatch.

    ``ok=True`` carries the new snapshot for the regenerated target
    (for ItemRegeneratedMsg) and the updated remaining stale marks.
    ``ok=False`` carries an ``error`` message suitable for surfacing
    in :class:`EditRejectedMsg` (we re-use the WS rejection path).
    """

    ok: bool
    stage: str = ""
    target_id: str = ""
    snapshot: Dict[str, Any] = field(default_factory=dict)
    remaining_marks: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    error: str = ""
    # True iff this regen actually mutated ``state.stale_marks`` — either
    # via the coordinator's ``_clear_marks_for_target`` / ``_propagate_downstream``
    # or via the stage's own internal calls (e.g. voiceover's ``slide:N``
    # path calls ``mark_downstream_stale``). The orchestrator gates
    # ``_broadcast_stale_marks()`` on this so read-only regens (e.g.
    # ``voice:preview:<id>``) don't push a stale-marks refresh that
    # would resurface pre-existing marks as a misleading "out of date"
    # banner. Computed by deep-comparing serialized state before/after.
    marks_changed: bool = False


class RegenerateCoordinator:
    """Per-target-locking dispatcher for ``regenerate_item`` calls.

    The coordinator is constructed once per orchestrator and holds:
      - a reference to the orchestrator (for state access + UndoStack)
      - a dict of per-target asyncio.Locks
    """

    def __init__(self, orchestrator: Any) -> None:
        # ``orchestrator`` is typed Any to avoid an import cycle
        # (interactive_orchestrator.py imports this module). The
        # coordinator only reaches for well-known attributes:
        #   _active_state, _stages, _undo, _save_state, _refresh_after_edit
        self._orch = orchestrator
        self._locks: Dict[Tuple[str, str], asyncio.Lock] = {}

    def _get_lock(self, stage: str, target_id: str) -> asyncio.Lock:
        """One lock per (stage, target_id). Created lazily."""
        key = (stage, target_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def dispatch(
        self,
        stage: str,
        target_id: str,
        *,
        mode: Literal["incremental", "fresh"] = "incremental",
        ref_id: str = "",
    ) -> RegenerateResult:
        """Regenerate one downstream item and clear its stale mark.

        Returns a :class:`RegenerateResult`. The caller (WS handler)
        is responsible for emitting the appropriate WS messages:

          - on success: ``ItemRegeneratedMsg`` + ``StaleMarksUpdatedMsg``
          - on failure: ``EditRejectedMsg``

        The coordinator itself only mutates state — it does not touch
        the broadcaster. This keeps it unit-testable without spinning
        up a WS server.
        """
        state = self._orch._active_state
        if state is None:
            return RegenerateResult(
                ok=False,
                stage=stage,
                target_id=target_id,
                error="no active pipeline state",
            )

        # "all" is structural — only SlidesStage / ImagesStage /
        # RenderedStage that opt in support it. Otherwise reject.
        if target_id == "all":
            # SlidesStage treats "all" as "regenerate every slide".
            # Same for ImagesStage / RenderedStage. The dispatch
            # below handles it per-stage.
            pass

        # Look up the stage object.
        stage_obj = self._find_stage(stage)
        if stage_obj is None:
            return RegenerateResult(
                ok=False,
                stage=stage,
                target_id=target_id,
                error=f"unknown stage: {stage!r}",
            )

        # Check the stage implements regenerate_item.
        regenerate = getattr(stage_obj, "regenerate_item", None)
        if regenerate is None:
            return RegenerateResult(
                ok=False,
                stage=stage,
                target_id=target_id,
                error=(
                    f"stage {stage!r} does not support per-item regenerate; "
                    f"use the stage-level Restart action instead"
                ),
            )

        lock = self._get_lock(stage, target_id)
        async with lock:
            return await self._dispatch_locked(
                stage_obj=stage_obj,
                stage_name=stage,
                target_id=target_id,
                mode=mode,
                ref_id=ref_id,
            )

    async def _dispatch_locked(
        self,
        *,
        stage_obj: Any,
        stage_name: str,
        target_id: str,
        mode: Literal["incremental", "fresh"],
        ref_id: str,
    ) -> RegenerateResult:
        """Inner dispatch — assumes the per-target lock is held."""
        state = self._orch._active_state
        # Snapshot stale_marks BEFORE the stage runs so we can detect
        # whether this regen actually mutated stale state. Some stages
        # write marks internally (e.g. voiceover's ``slide:N`` path
        # calls ``mark_downstream_stale``) which the coordinator-level
        # change tracking in _clear_marks_for_target / _propagate_downstream
        # wouldn't see. Deep-serializing here is the only approach that
        # catches both. json.dumps(sort_keys=True) is bulletproof against
        # the List[Dict] reordering that StaleStore.as_dict() can introduce.
        #
        # IMPORTANT: normalize through StaleStore.from_dict(...).as_dict()
        # on BOTH sides of the comparison. The round-trip adds canonical
        # fields (source_stage, source_id, created_at, context_snapshot)
        # to each mark dict; without normalization, the act of calling
        # ``state.stale_marks = store.as_dict()`` inside _clear_marks_for_target
        # would trip the changed-detector even when no marks were actually
        # added or removed.
        _marks_before = json.dumps(
            StaleStore.from_dict(state.stale_marks).as_dict(),
            sort_keys=True,
        )
        # The stage's regenerate_item gets a StageContext just like run().
        ctx = self._orch._build_stage_context(state) if hasattr(
            self._orch, "_build_stage_context"
        ) else None
        if ctx is None:
            # Fall back to a hand-built context if the orchestrator
            # doesn't expose a builder. This is a defensive path —
            # _build_stage_context is the canonical constructor.
            ctx = self._orch._make_stage_ctx(state)  # pragma: no cover
        try:
            await stage_obj.regenerate_item(
                ctx, target_id, mode=mode
            )
        except NotImplementedError as exc:
            return RegenerateResult(
                ok=False,
                stage=stage_name,
                target_id=target_id,
                error=str(exc),
            )
        except Exception as exc:
            # Re-raise with context for the WS handler to catch + log.
            # The orchestrator's WS dispatch should treat this as a
            # 500-class failure (server bug, not user error).
            return RegenerateResult(
                ok=False,
                stage=stage_name,
                target_id=target_id,
                error=f"regenerate raised: {exc}",
            )

        # Rebuild the snapshot for this stage (the UI's "new value").
        snapshot = stage_obj.build_snapshot(state)
        snapshot_dict: Dict[str, Any] = {}
        if snapshot is not None:
            from dataclasses import asdict
            try:
                snapshot_dict = asdict(snapshot)
            except Exception:
                snapshot_dict = {}

        # Clear the stale mark for this (stage, target_id). For
        # slide-scoped targets, also clear finer-grained slot marks
        # on the same slide (regenerating slide:2 covers slot marks).
        store = StaleStore.from_dict(state.stale_marks)
        self._clear_marks_for_target(store, stage_name, target_id)
        state.stale_marks = store.as_dict()

        # Propagate downstream: regenerating this item is effectively
        # a fresh edit to it. The propagation rules emit marks for
        # the appropriate downstream stages.
        self._propagate_downstream(stage_name, target_id, state)

        # Persist + refresh so the broadcast picks up the new state.
        if hasattr(self._orch, "_save_state"):
            self._orch._save_state(state)

        _marks_after = json.dumps(
            StaleStore.from_dict(state.stale_marks).as_dict(),
            sort_keys=True,
        )
        return RegenerateResult(
            ok=True,
            stage=stage_name,
            target_id=target_id,
            snapshot=snapshot_dict,
            remaining_marks=dict(state.stale_marks),
            marks_changed=(_marks_after != _marks_before),
        )

    def _find_stage(self, name: str) -> Any:
        """Look up a stage object by name from the orchestrator's list."""
        stages = getattr(self._orch, "_stages", None) or []
        for s in stages:
            if getattr(s, "name", None) == name:
                return s
        return None

    def _clear_marks_for_target(
        self, store: StaleStore, stage: str, target_id: str
    ) -> None:
        """Clear the mark for ``(stage, target_id)`` plus finer-grained marks
        on the *same stage*.

        ``target_id="all"`` → clear the whole stage's mark list.
        ``target_id="slide:N"`` → clear "slide:N" AND any
        ``"slide:N:slot:*"`` marks on the same stage (those become
        irrelevant once the slide is regenerated).
        ``target_id="slide:N:slot:ID"`` → clear only that exact id.

        We deliberately don't touch other stages' marks for the same
        slide index — a ``rendered[slide:N]`` mark from an earlier
        upstream edit is independent of this regenerate; clearing it
        here would silently drop a real signal the user hasn't seen yet.
        """
        if target_id == "all":
            store.clear_stage(stage)
            return
        slide_idx, slot_id = parse_target_id(target_id)
        if slide_idx is None:
            # Unrecognized shape — best-effort single dismiss.
            store.dismiss(stage, target_id)
            return
        if slot_id is None:
            # slide-scoped: clear "slide:N" + any "slide:N:slot:*" on
            # this stage only. ``clear_slide_everywhere`` would also
            # clear downstream stages' marks for the same slide, which
            # would silently drop unrelated upstream signals.
            slide_id = f"slide:{slide_idx}"
            slot_prefix = f"slide:{slide_idx}:slot:"
            marks = store.for_stage(stage)
            new_marks = [
                m for m in marks
                if m.target_id != slide_id
                and not m.target_id.startswith(slot_prefix)
            ]
            # Re-emit by clearing the stage and merging back. Simpler
            # than adding a stage-scoped filter API to StaleStore.
            store.clear_stage(stage)
            if new_marks:
                from typing import Iterable
                store.merge(stage, new_marks)
        else:
            # slot-scoped: clear the exact id only.
            store.dismiss(stage, target_id)

    def _propagate_downstream(
        self, stage: str, target_id: str, state: Any
    ) -> None:
        """Mark downstream stages stale after this regenerate.

        Regenerating slides[i] changes its HTML → rendered[i] is now
        stale. Regenerating images[i] → slides[i] + rendered[i] are
        stale. Rendered has no downstream — nothing to mark.

        Imports :func:`compute_stale_marks` lazily so test modules
        don't pay for the propagation import unless they exercise it.
        """
        if target_id == "all":
            # "all" regen leaves no downstream stale — the whole
            # downstream will be regenerated by the orchestrator's
            # Restart logic, which is out of scope here.
            return
        slide_idx, _ = parse_target_id(target_id)
        if slide_idx is None:
            return
        # Build a minimal EditEvent that the propagation engine
        # translates into per-slide downstream marks. We don't have
        # a meaningful before/after here (the regenerator just wrote
        # the new value), so we pass sentinel shapes that satisfy
        # the rules engine's structural checks.
        from shuttleslide.agent.review.stale_propagation import (
            build_edit_event,
            compute_stale_marks,
        )

        if stage == "slides":
            before = ""  # the pre-regenerate HTML is already gone
            after = ""
            event = build_edit_event(
                stage="slides",
                before=before,
                after=after,
                slide_idx=slide_idx,
            )
        elif stage == "images":
            before = {}
            after = {}
            event = build_edit_event(
                stage="images",
                before=before,
                after=after,
                slide_idx=slide_idx,
            )
        else:
            # Rendered or unknown: no downstream to propagate to.
            return

        new_marks = compute_stale_marks(event)
        if not new_marks:
            return
        store = StaleStore.from_dict(state.stale_marks)
        for stage_name, marks in new_marks.items():
            store.merge(stage_name, marks)
        state.stale_marks = store.as_dict()
