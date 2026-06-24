"""Snapshot construction + UndoStack.

``build_snapshot`` is a thin dispatcher that delegates to the Stage's
own ``build_snapshot`` method. Per-stage snapshot bodies live on the
Stage classes (see ``core_stages.py`` for the 5 built-ins).

Keeps ``UndoStack`` and ``EditTarget`` helpers here — they're general
review primitives not specific to any one stage.
"""

from __future__ import annotations

from typing import Optional

from shuttleslide.agent.review.review_gate import (
    EditTarget,
    StageSnapshot,
)
from shuttleslide.agent.review.registry import default_registry
from shuttleslide.agent.review.stage import Stage
from shuttleslide.agent.state import AgentState

__all__ = [
    "EditTarget",
    "UndoStack",
    "build_snapshot",
]


def build_snapshot(
    stage_name: str,
    state: AgentState,
    registry=None,
) -> StageSnapshot:
    """Dispatch to ``Stage.build_snapshot`` for the named stage.

    ``registry`` defaults to ``default_registry()`` (core stages only).
    Callers that have a custom registry (e.g. with pro extensions)
    should pass it explicitly so pro-stage snapshots resolve correctly.

    Raises ``KeyError`` if the stage isn't registered. Raises
    ``ValueError`` if the stage's ``build_snapshot`` returns None —
    the dispatcher contract is "always return a snapshot"; silent
    stages are handled by the orchestrator (which checks for None and
    synthesises a minimal progress snapshot).
    """
    reg = registry if registry is not None else default_registry()
    stage: Stage = reg.get(stage_name)
    snapshot = stage.build_snapshot(state)
    if snapshot is None:
        raise ValueError(
            f"stage {stage_name!r} returned no snapshot; "
            f"the orchestrator should handle this case before calling "
            f"build_snapshot"
        )
    return snapshot


# ---------------------------------------------------------------------------
# UndoStack
# ---------------------------------------------------------------------------


from dataclasses import dataclass
from typing import List, Tuple


@dataclass
class _UndoEntry:
    target: EditTarget
    old_value: str


class UndoStack:
    """LIFO of (target, old_value) pairs for one review session.

    Bounded to MAX_ENTRIES to keep memory predictable on long sessions.
    When the cap is hit the oldest entry is dropped (FIFO eviction on a
    stack is unusual but correct — we never want to lose the most recent
    edit's undo).

    Thread safety: the stack is owned by the orchestrator loop; all push
    and pop calls must happen there. The server's WS handler dispatches
    via ``loop.call_soon_threadsafe`` (PR2).
    """

    MAX_ENTRIES = 100

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._entries: List[_UndoEntry] = []
        self._max = max_entries

    def push(self, target: EditTarget, old_value: str) -> None:
        """Record the pre-edit value of target. Called before mutation."""
        if len(self._entries) >= self._max:
            # Drop oldest. list.pop(0) is O(n) but n <= 100 so fine.
            self._entries.pop(0)
        self._entries.append(_UndoEntry(target=target, old_value=old_value))

    def pop(self) -> Optional[Tuple[EditTarget, str]]:
        """Pop the most recent undo entry, or None if empty."""
        if not self._entries:
            return None
        entry = self._entries.pop()
        return (entry.target, entry.old_value)

    def peek(self) -> Optional[Tuple[EditTarget, str]]:
        """Look at the most recent entry without popping."""
        if not self._entries:
            return None
        entry = self._entries[-1]
        return (entry.target, entry.old_value)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
