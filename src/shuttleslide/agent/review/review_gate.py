"""ReviewGate — async primitive that lets the orchestrator pause between
stages until a reviewer (web UI in PR2, tests in PR1) releases it.

Threading model
---------------
The orchestrator runs on its own asyncio loop. The web server (PR2) will
run uvicorn on a separate thread with its own loop. ``release`` must be
safe to call from any thread — it just sets the underlying ``asyncio.Event``
via ``loop.call_soon_threadsafe`` when the caller passes the orchestrator
loop, or directly when on the same loop.

State machine
-------------
    IDLE  --pause_for_review(snapshot)-->  PAUSED  (pending = snapshot)
    PAUSED --release("approve")--------->  IDLE    (pause_for_review returns "approve")
    PAUSED --release("cancel")---------->  IDLE    (pause_for_review returns "cancel")

Only one snapshot can be pending at a time. Calling ``pause_for_review``
while another pause is in flight raises ``RuntimeError`` — the
orchestrator is single-threaded so this should never happen in practice,
but the check catches logic bugs early.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple

# Core stage names — the 5 built-in stages shipped with shuttleslide.
# This tuple stays static for CLI flag validation (``--review-stages``
# defaults to this set). For the *current* resolved pipeline order
# (which may include pro extension stages), use
# ``StageRegistry.all_names()`` instead.
STAGE_NAMES = ("theme", "outline", "images", "slides", "rendered")
# ``StageName`` is the runtime type used everywhere a stage name flows
# through the system (snapshots, gate, broadcaster, persistence). It's
# ``str`` rather than a Literal so extension stages (script / voiceover
# / etc.) are representable without editing this file every time one is
# added.
StageName = str
# ``CoreStageName`` is the Literal narrow type for code that only ever
# deals with the 5 built-ins (e.g. type-narrowing inside core_stages).
# Use this when you want mypy / pyright to catch a typo in core-stage
# dispatch code.
CoreStageName = Literal["theme", "outline", "images", "slides", "rendered"]
ReviewAction = Literal["approve", "cancel"]


@dataclass
class EditTarget:
    """Pointer to a single editable element within a stage snapshot.

    Paths are tuples of int/str so they serialise to JSON cleanly and can
    address nested structures (e.g. ``("slide", 3, "slot", "hero")`` for
    slide 3's hero slot). ``kind`` is the dispatch key for the editor
    registry in PR3.
    """

    stage: StageName
    path: tuple
    kind: Literal["json", "html", "svg", "image"]
    current_value: str
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StageSnapshot:
    """Frozen view of one stage's output, ready to ship to a reviewer.

    ``state_view`` is JSON-safe (no dataclasses, no Path, no datetime) so
    it can be sent over WebSocket verbatim in PR2. ``editable_targets``
    tells the UI which elements the user can modify.
    """

    stage: StageName
    state_view: Dict[str, Any]
    artifact_kind: Literal["json", "html", "svg", "image", "mixed"]
    editable_targets: List[EditTarget] = field(default_factory=list)
    timestamp: float = 0.0


class ReviewGate:
    """Async pause/release primitive coordinating orchestrator and reviewer.

    Lifecycle:
        1. Orchestrator stage finishes -> calls ``pause_for_review(snapshot)``
        2. Gate stores snapshot, clears event, awaits event
        3. Reviewer (server / test) inspects ``pending`` snapshot
        4. Reviewer calls ``release("approve")`` or ``release("cancel")``
        5. Orchestrator wakes up, returns the action, continues / raises

    The gate is single-use-per-stage: each pause must be paired with
    exactly one release before the next pause.
    """

    def __init__(self) -> None:
        # Owned by whichever loop first calls pause_for_review. We can't
        # capture the loop at __init__ time because the orchestrator may
        # be constructed before asyncio.run() starts.
        self._event: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: Optional[StageSnapshot] = None
        self._action: ReviewAction = "approve"

    # ------------------------------------------------------------------
    # Orchestrator side
    # ------------------------------------------------------------------

    async def pause_for_review(self, snapshot: StageSnapshot) -> ReviewAction:
        """Block until release() is called. Returns the chosen action.

        Raises RuntimeError if called re-entrantly (i.e. before a previous
        pause was released). The orchestrator is single-threaded across
        stages so this is a logic-guard, not a runtime concern.
        """
        if self._pending is not None:
            raise RuntimeError(
                "ReviewGate.pause_for_review called while another snapshot "
                f"is pending (stage={self._pending.stage}). Call release() first."
            )
        # Lazily bind the event + loop the first time we're awaited. This
        # ensures we capture the running loop rather than the loop that
        # constructed the gate (which may be None at construction time).
        if self._event is None:
            self._event = asyncio.Event()
            self._loop = asyncio.get_running_loop()
        self._pending = snapshot
        self._action = "approve"
        self._event.clear()
        await self._event.wait()
        # Release clears _pending before setting the event, so by the time
        # we wake up _pending is already None. Defensive: clear anyway.
        action = self._action
        self._pending = None
        return action

    # ------------------------------------------------------------------
    # Reviewer side
    # ------------------------------------------------------------------

    def release(self, action: ReviewAction = "approve") -> None:
        """Wake up the paused orchestrator with the chosen action.

        Safe to call from any thread: if the gate captured an orchestrator
        loop different from the calling thread's (or the calling thread
        has no running loop at all — typical for worker threads), we
        delegate the event set via ``loop.call_soon_threadsafe``.
        """
        if self._pending is None:
            # No active pause — likely a stray release from the UI. Silent
            # no-op keeps the UI forgiving (double-clicking Approve should
            # not crash the server).
            return
        if action not in ("approve", "cancel"):
            raise ValueError(f"action must be 'approve' or 'cancel', got {action!r}")
        self._action = action
        self._pending = None
        assert self._event is not None
        # Detect whether we're being called from the gate's owner loop,
        # from a different loop, or from a thread with no loop at all.
        # asyncio.get_running_loop() raises RuntimeError when the caller
        # has no running loop (e.g. a plain threading.Thread worker) —
        # in that case we must go through call_soon_threadsafe.
        try:
            current_loop = asyncio.get_running_loop()
            same_loop = current_loop is self._loop
        except RuntimeError:
            same_loop = False
        if not same_loop and self._loop is not None:
            # Cross-thread/cross-loop release: schedule the set on the
            # owner loop. asyncio.Event is not thread-safe.
            self._loop.call_soon_threadsafe(self._event.set)
        else:
            # Same-loop release (typical in tests).
            self._event.set()

    # ------------------------------------------------------------------
    # Introspection (non-blocking; for server / tests)
    # ------------------------------------------------------------------

    @property
    def pending(self) -> Optional[StageSnapshot]:
        """The snapshot awaiting review, or None if no pause is active."""
        return self._pending

    @property
    def is_paused(self) -> bool:
        return self._pending is not None
