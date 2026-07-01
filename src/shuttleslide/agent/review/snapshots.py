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
# UndoStack / HistoryStack
# ---------------------------------------------------------------------------


from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple


@dataclass
class _UndoEntry:
    target: EditTarget
    old_value: str
    # new_value: the value the edit wrote (post-edit). Stored so the
    # "Undo" affordance on a pending-revert card can re-apply it without
    # a separate snapshot round-trip. Old callers default to "".
    new_value: str = ""
    # History metadata — optional, populated by apply_edit for the
    # sidebar History panel (#4). Old callers (e.g. tests) can push
    # without these and they default to empty strings.
    new_value_summary: str = ""
    action_label: str = ""
    timestamp: float = 0.0


class UndoStack:
    """LIFO of edit entries for one review session.

    Backed by a list (not a true linked-list stack) so the History UI
    can enumerate every entry by index for the "Restore to here"
    affordance.

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

    def push(
        self,
        target: EditTarget,
        old_value: str,
        *,
        new_value: str = "",
        new_value_summary: str = "",
        action_label: str = "",
        timestamp: float = 0.0,
    ) -> None:
        """Record the pre-edit value of target. Called before mutation.

        ``new_value`` is the post-edit value — captured so the History
        panel's "Undo" affordance on a pending-revert card can re-apply
        the edit without going back to the editor. ``new_value_summary``
        and ``action_label`` are surfaced in the History panel so the
        user can identify edits at a glance. All three default to empty
        for backwards compatibility with callers that don't care about
        history display.
        """
        if len(self._entries) >= self._max:
            # Drop oldest. list.pop(0) is O(n) but n <= 100 so fine.
            self._entries.pop(0)
        import time as _time
        if timestamp == 0.0:
            timestamp = _time.time()
        self._entries.append(_UndoEntry(
            target=target,
            old_value=old_value,
            new_value=new_value,
            new_value_summary=new_value_summary,
            action_label=action_label,
            timestamp=timestamp,
        ))

    def pop(self) -> Optional[Tuple[EditTarget, str]]:
        """Pop the most recent undo entry, or None if empty."""
        if not self._entries:
            return None
        entry = self._entries.pop()
        return (entry.target, entry.old_value)

    def pop_entry(self) -> Optional[_UndoEntry]:
        """Pop the most recent entry, returning the full record.

        Used by callers that need the history metadata (action_label,
        new_value_summary) to re-push on failure without losing it.
        ``pop()`` is the legacy variant that discards metadata.
        """
        if not self._entries:
            return None
        return self._entries.pop()

    def push_entry(self, entry: _UndoEntry) -> None:
        """Re-insert a previously popped entry (preserves metadata).

        Used by ``undo_last`` / ``revert_to`` callers that need to undo
        a destructive pop when the apply step fails. Bounded by the
        same ``_max`` cap as ``push()`` (and applies the same FIFO
        eviction when over cap, though that path is rare).
        """
        if len(self._entries) >= self._max:
            self._entries.pop(0)
        self._entries.append(entry)

    def peek(self) -> Optional[Tuple[EditTarget, str]]:
        """Look at the most recent entry without popping."""
        if not self._entries:
            return None
        entry = self._entries[-1]
        return (entry.target, entry.old_value)

    def entries(self) -> List[_UndoEntry]:
        """Return all entries, newest first (for the History panel)."""
        # Copy + reverse so callers can't mutate our internal list.
        return list(reversed(self._entries))

    def revert_to(self, idx_from_newest: int) -> Optional[Tuple[EditTarget, str]]:
        """Pop entries newer than ``idx_from_newest`` (and that entry
        itself), returning that entry's ``(target, old_value)``.

        ``idx_from_newest`` is the index into ``entries()`` (which is
        newest-first). ``revert_to(0)`` is equivalent to a single undo.
        Out-of-range indices return None.
        """
        if idx_from_newest < 0 or idx_from_newest >= len(self._entries):
            return None
        # Translate newest-first index → oldest-first list position.
        target_pos = len(self._entries) - 1 - idx_from_newest
        # Pop everything newer (positions > target_pos) then the entry
        # itself. Pop from the top so list stays consistent.
        entries_to_pop = len(self._entries) - target_pos  # count including target
        target_entry = self._entries[target_pos]
        self._entries = self._entries[:target_pos]
        return (target_entry.target, target_entry.old_value)

    def peek_at(self, idx_from_newest: int) -> Optional[_UndoEntry]:
        """Read entry at ``idx_from_newest`` without mutating the stack.

        Unlike ``revert_to`` (which truncates everything newer), this is
        non-destructive: the entry stays in the stack so a "restore this
        one edit" can later be re-applied (git-revert semantics) without
        losing intervening edits.
        """
        if idx_from_newest < 0 or idx_from_newest >= len(self._entries):
            return None
        target_pos = len(self._entries) - 1 - idx_from_newest
        return self._entries[target_pos]

    def remove_entry(self, idx_from_newest: int) -> bool:
        """Delete a single entry by index (newest=0), leaving others intact.

        Used by the History panel's "Commit" affordance on a pending-
        revert card: the value is already at old_value (reverted), so
        removing the entry just drops the card. Returns True if the
        index was in range, False otherwise.
        """
        if idx_from_newest < 0 or idx_from_newest >= len(self._entries):
            return False
        target_pos = len(self._entries) - 1 - idx_from_newest
        del self._entries[target_pos]
        return True

    def as_history_dicts(self) -> List[Dict[str, Any]]:
        """Return entries as JSON-safe dicts for the HistorySnapshotMsg.

        Each dict carries an ``idx`` (position in entries(), i.e. newest
        = 0) plus the display fields. ``target_path`` lets the client
        decide which thumbnail to focus after a Restore. ``stage`` lets
        the client filter the history panel to just the active stage.
        """
        out: List[Dict[str, Any]] = []
        for idx, e in enumerate(self.entries()):
            out.append({
                "idx": idx,
                "stage": e.target.stage,
                "action_label": e.action_label,
                "new_value_summary": e.new_value_summary,
                "timestamp": e.timestamp,
                "target_path": list(e.target.path),
            })
        return out

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)
