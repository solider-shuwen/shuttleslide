"""InteractiveOrchestrator — AgentOrchestrator subclass that pauses at
each stage boundary for human review.

Design
------
The base orchestrator fires ``_post_stage_hook(stage, state)`` after
every stage. This subclass overrides the hook to:

    1. Optionally skip review (when the stage isn't in ``review_stages``)
    2. Build a StageSnapshot from the current state
    3. Await ``gate.pause_for_review(snapshot)`` — blocks until the
       reviewer (web UI / test) calls ``gate.release(action)``

Cancellation
------------
If the reviewer releases with action="cancel", we raise
``ReviewCancelledError``. The base orchestrator's ``run()`` wraps
``_run_pipeline`` in a try/finally that stops the browser, so cleanup
runs normally and the exception propagates to the caller.

Auto-approve mode
-----------------
``auto_approve=True`` skips the gate entirely. Used by the regression
test that compares InteractiveOrchestrator output to the base class.
"""

from __future__ import annotations

from typing import Optional, Set

from shuttleslide.agent.config import AgentConfig
from shuttleslide.agent.orchestrator import AgentOrchestrator
from shuttleslide.agent.review.review_gate import (
    STAGE_NAMES,
    ReviewAction,
    ReviewGate,
    StageName,
)
from shuttleslide.agent.review.snapshots import build_snapshot
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
        None / "all" / ""  -> all stages
        "theme,outline"    -> {"theme", "outline"}
        "theme, slides"    -> {"theme", "slides"}  (whitespace tolerated)

    Raises ValueError on unknown stage names so the CLI fails fast.
    """
    if spec is None or spec.strip() == "" or spec.strip().lower() == "all":
        return set(STAGE_NAMES)
    parts = [p.strip() for p in spec.split(",") if p.strip()]
    invalid = [p for p in parts if p not in STAGE_NAMES]
    if invalid:
        raise ValueError(
            f"unknown review stage(s) {invalid!r}; valid stages: {list(STAGE_NAMES)}"
        )
    return set(parts)  # type: ignore[return-value]


class InteractiveOrchestrator(AgentOrchestrator):
    """AgentOrchestrator that pauses between stages for human review.

    Constructor parameters beyond the base class:
        gate: ReviewGate instance shared with the web server (PR2) or
              driven directly by tests (PR1).
        review_stages: which of the 5 stages trigger a pause. Use
                       ``set(STAGE_NAMES)`` for all, or parse from a CLI
                       flag via ``_parse_review_stages``.
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
        registry: Optional[ToolRegistry] = None,
        renderer: Optional[SlideHTMLRenderer] = None,
    ) -> None:
        super().__init__(config=config, registry=registry, renderer=renderer)
        self.gate = gate
        # Default to reviewing all stages. ``set(STAGE_NAMES)`` returns
        # a set of literals but mypy needs the explicit cast.
        self.review_stages: Set[StageName] = (
            set(STAGE_NAMES) if review_stages is None else set(review_stages)
        )
        self.auto_approve = auto_approve

    async def _post_stage_hook(self, stage: str, state: AgentState) -> None:
        """Pause for review after each stage (if enabled for this stage)."""
        # Re-type stage: the base class passes a str, but we know it's
        # always one of STAGE_NAMES because the base only emits those.
        if stage not in STAGE_NAMES:
            # Defensive — if a future stage is added to the base class
            # without updating STAGE_NAMES, we should not silently
            # misbehave.
            raise RuntimeError(
                f"InteractiveOrchestrator._post_stage_hook got unknown "
                f"stage {stage!r}; known: {list(STAGE_NAMES)}"
            )
        if self.auto_approve:
            return
        typed_stage: StageName = stage  # type: ignore[assignment]
        if typed_stage not in self.review_stages:
            return
        snapshot = build_snapshot(typed_stage, state)
        action: ReviewAction = await self.gate.pause_for_review(snapshot)
        if action == "cancel":
            raise ReviewCancelledError(typed_stage)
