"""Stale markers for the review pipeline.

A ``StaleMark`` records that a downstream stage's product (slides / images /
rendered) may be out-of-date relative to its upstream source. Marks are
produced by ``compute_stale_marks`` (see :mod:`stale_propagation`) whenever
the user edits a stage that already has generated downstream artifacts.

Stale is a *signal*, not a *command*: marks do not auto-trigger regeneration.
The review UI surfaces them as badges with two actions:
  - regenerate the affected item (incremental or fresh)
  - dismiss the mark (keep the existing artifact as-is)

The store is keyed by downstream stage name (``"slides"`` / ``"images"`` /
``"rendered"``); within a stage, marks are deduplicated by ``target_id`` so
the same downstream item carries at most one mark. Re-marking an item
replaces the prior mark in place — keeps insertion order, updates ``reason``
/ ``created_at`` / ``context_snapshot`` so the latest source is reported.

Persistence
-----------
``StaleMark`` is JSON-safe (all fields are primitives or nested dicts).
``StaleStore.as_dict`` produces the wire shape that ``state_persistence``
writes to ``agent_state.json`` and ``StaleMarksUpdatedMsg`` broadcasts to
the UI. Round-trip via ``StaleStore.from_dict`` is loss-less for well-formed
input and tolerant of malformed input (a corrupted ``agent_state.json``
should not kill resume).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional


@dataclass
class StaleMark:
    """A single stale marker on a downstream stage.

    Identity for dedup: ``(target_id)`` within a single stage. The store
    enforces "at most one mark per ``(stage, target_id)``" — the stage is
    the bucket key, ``target_id`` is the in-bucket key.
    """

    # "all" | "slide:2" | "slide:2:slot:hero"
    # "all" — every item on the stage is stale (structural upstream change)
    # "slide:N" — slide N as a whole (slides[N] / images[N] all slots /
    #             rendered[N])
    # "slide:N:slot:ID" — images stage only: one specific image slot
    target_id: str

    # Triggering upstream stage: "theme" / "outline" / "images" / "slides".
    # Lets the UI badge say "stale because theme was edited".
    source_stage: str

    # Upstream's own target_id. "all" for theme / structural outline change;
    # "slide:1" for per-slide upstream edits. Paired with ``source_stage``
    # this is enough to point the user at the offending edit in history.
    source_id: str

    # Human-readable one-liner for the badge tooltip and the history panel.
    # Example: "outline[2] edited", "theme.decoration_style changed".
    reason: str

    # epoch seconds. Used only for UI display ("staled 3s ago"); not load-bearing.
    created_at: float

    # Snapshot of upstream state at the moment the mark was created.
    # Consumed by the incremental regenerator to build before/after diffs
    # for the LLM prompt. Shape is convention, not contract — producers
    # and consumers coordinate via the stage name (``source_stage``).
    # Example for outline-triggered slide stale:
    #   {"outline_before": {...}, "outline_after": {...}}
    # Optional: structural ("all") marks carry no snapshot because the
    # incremental path doesn't apply (you can't "incrementally" regenerate
    # when the slide ordering itself has shifted).
    context_snapshot: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        """JSON-safe dict. Round-trips via :meth:`from_dict`."""
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StaleMark":
        """Inverse of :meth:`to_dict`.

        Tolerates missing optional fields so older persisted state
        (written before ``context_snapshot`` was added, say) loads
        without error. Only ``target_id`` is strictly required.
        """
        if "target_id" not in d:
            raise KeyError("target_id")
        return cls(
            target_id=d["target_id"],
            source_stage=d.get("source_stage", ""),
            source_id=d.get("source_id", ""),
            reason=d.get("reason", ""),
            created_at=d.get("created_at", 0.0),
            context_snapshot=d.get("context_snapshot"),
        )


class StaleStore:
    """In-memory store of stale marks, keyed by downstream stage name.

    The store is the authoritative source for ``state.stale_marks``. All
    reads return fresh lists (callers cannot mutate internal state by
    accident); all writes go through :meth:`merge` / :meth:`dismiss` /
    :meth:`clear_*` so dedup and ordering stay centralised.

    The store does NOT own persistence — the orchestrator snapshots the
    underlying dict via :meth:`as_dict` whenever it persists ``state``
    (same pattern as the existing ``_save_state`` after edits).
    """

    def __init__(self, marks: Optional[Dict[str, List[StaleMark]]] = None) -> None:
        # Copy on construct — callers may keep mutating the source dict
        # (e.g. state.stale_marks loaded from disk) and surprises here
        # would be hard to debug.
        self._marks: Dict[str, List[StaleMark]] = {
            stage: list(marks_list) for stage, marks_list in (marks or {}).items()
        }

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def for_stage(self, stage: str) -> List[StaleMark]:
        """All marks currently on ``stage``. Empty list if none."""
        return list(self._marks.get(stage, []))

    def get(self, stage: str, target_id: str) -> Optional[StaleMark]:
        """Look up a single mark by identity. ``None`` if not present."""
        for mark in self._marks.get(stage, []):
            if mark.target_id == target_id:
                return mark
        return None

    def has(self, stage: str, target_id: str) -> bool:
        """Convenience predicate."""
        return self.get(stage, target_id) is not None

    def stages(self) -> List[str]:
        """Stages that currently carry at least one mark."""
        return [s for s, lst in self._marks.items() if lst]

    def total_count(self) -> int:
        """Total marks across all stages. For the global stale summary badge."""
        return sum(len(lst) for lst in self._marks.values())

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def merge(self, stage: str, new_marks: Iterable[StaleMark]) -> None:
        """Merge ``new_marks`` into ``stage``'s bucket.

        Dedup by ``target_id``: if a mark with the same id already exists,
        it is replaced in place (preserves insertion order, updates
        ``reason`` / ``created_at`` / ``context_snapshot`` to reflect the
        most recent trigger). Otherwise the new mark is appended.

        This is the only sanctioned way to add marks — direct mutation
        of the bucket list would bypass dedup.
        """
        bucket = self._marks.setdefault(stage, [])
        # Build id->index map once per merge call; rebuilding inside the
        # loop would be O(n²) on bulk merges.
        existing_by_id = {m.target_id: i for i, m in enumerate(bucket)}
        for mark in new_marks:
            idx = existing_by_id.get(mark.target_id)
            if idx is not None:
                bucket[idx] = mark
            else:
                existing_by_id[mark.target_id] = len(bucket)
                bucket.append(mark)

    def add(self, stage: str, mark: StaleMark) -> None:
        """Single-mark convenience wrapper around :meth:`merge`."""
        self.merge(stage, [mark])

    def dismiss(self, stage: str, target_id: str) -> bool:
        """Remove a single mark. Returns ``True`` if something was removed.

        Special case: ``target_id == "all"`` clears every mark on ``stage``
        (mirrors the WS protocol's ``DismissStaleMsg`` semantics — "all"
        on the wire means "every mark on this stage", not "every stage").
        """
        if target_id == "all":
            had = bool(self._marks.get(stage))
            self._marks[stage] = []
            return had

        bucket = self._marks.get(stage, [])
        for i, mark in enumerate(bucket):
            if mark.target_id == target_id:
                del bucket[i]
                return True
        return False

    def clear_stage(self, stage: str) -> None:
        """Drop every mark on ``stage`` regardless of ``target_id``."""
        self._marks[stage] = []

    def clear_all(self) -> None:
        """Drop every mark on every stage. Used on full pipeline restart."""
        self._marks = {}

    def clear_slide_everywhere(self, slide_idx: int) -> None:
        """Remove per-slide marks for one slide from every stage.

        Clears ``slide:N`` and any finer-grained ``slide:N:slot:*`` marks
        across all stages. ``all`` marks are left untouched — they signal
        structural upstream change (theme rewrite, outline length change)
        and a single-slide regenerate does not invalidate that signal
        for the remaining slides.

        Used after a per-item regenerate: the freshly-regenerated item no
        longer carries its old mark, and any sub-granular marks (e.g. an
        individual slot stale under a slide-level stale) are subsumed by
        the regeneration.
        """
        slide_id = f"slide:{slide_idx}"
        slot_prefix = f"slide:{slide_idx}:slot:"
        for stage, bucket in list(self._marks.items()):
            self._marks[stage] = [
                m
                for m in bucket
                if m.target_id != slide_id and not m.target_id.startswith(slot_prefix)
            ]

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def as_dict(self) -> Dict[str, List[Dict[str, Any]]]:
        """JSON-safe snapshot for state persistence / WS broadcast.

        Stages with empty mark lists are omitted to keep the payload small
        and the on-disk state tidy.
        """
        return {
            stage: [m.to_dict() for m in marks]
            for stage, marks in self._marks.items()
            if marks
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "StaleStore":
        """Inverse of :meth:`as_dict`.

        Malformed entries are skipped (not raised): a partially-corrupted
        ``agent_state.json`` should not block resume. The user can always
        re-trigger any missing stale marks by editing the upstream again.
        """
        marks: Dict[str, List[StaleMark]] = {}
        for stage, lst in (d or {}).items():
            if not isinstance(lst, list):
                continue
            parsed: List[StaleMark] = []
            for item in lst:
                if not isinstance(item, dict):
                    continue
                try:
                    parsed.append(StaleMark.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
            if parsed:
                marks[stage] = parsed
        return cls(marks=marks)


def now() -> float:
    """Wall-clock seconds. Pulled out so tests can monkeypatch deterministically."""
    return time.time()
