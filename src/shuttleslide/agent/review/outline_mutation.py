"""Pure state-mutation helpers for inserting / deleting slides.

Used by the review-pipeline add-slide / delete-slide WS handlers
(``AddSlideMsg`` / ``DeleteSlideMsg``) to keep ``state.outline`` and
every parallel array (``slides`` / ``slide_images`` / ``html_paths``)
plus ``state.deck_skeleton`` indices and ``state.stale_marks``
``slide:N`` target ids consistent in a single coordinated operation.

Why centralise
--------------
Index realignment touches five fields and any mistake leaves the
pipeline in an inconsistent state (e.g. ``slides[i]`` whose HTML
references ``outline[j]``). Doing the shift inside a single function
makes the invariant ``len(outline) == len(slides) == len(html_paths)``
easy to audit and unit-test. The function is pure (no I/O, no LLM, no
broadcasts) so the orchestrator can wrap it with undo / persistence /
snapshot re-emit without entangling concerns.

Stale-mark reindex
------------------
Every ``StaleMark.target_id`` of the form ``slide:N`` or
``slide:N:slot:ID`` is shifted alongside the arrays: insert bumps every
``N >= pivot`` up by 1, delete drops the deleted slide's marks and
bumps every ``N > pivot`` down by 1. ``"all"`` marks are left alone —
they signal structural upstream changes that survive any per-slide
reorganisation.

``deck_skeleton`` alignment
---------------------------
``deck_skeleton.groups[*].slide_indices`` is shifted by the same rules.
``deck_skeleton.slide_intents`` (a list aligned with outline index) is
grown / shrunk at ``pivot`` — the inserted placeholder intent is a
minimal ``{"group_id": "", "image_intent": "none"}`` so downstream
prompts that read the field still find a value; the LLM is not asked
to re-plan grouping just because the user inserted a slide. If the
skeleton is ``None`` (one-shot outline fallback path) everything
skeleton-related is skipped silently.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from shuttleslide.agent.review.stale import StaleMark, StaleStore
from shuttleslide.agent.state import AgentState


_SLIDE_ID_RE = re.compile(r"^slide:(\d+)(:slot:.+)?$")


def insert_slide(state: AgentState, index: int, entry: Dict[str, Any]) -> int:
    """Insert ``entry`` at ``index`` and align every parallel array.

    Returns the resolved index (clamped into ``[0, len(outline)]``).
    """
    n = len(state.outline)
    if index < 0 or index > n:
        index = n

    state.outline.insert(index, entry)

    # Keep slides / html_paths length in lockstep with outline. Pad with
    # None first so the insert position is valid; None markers are the
    # "needs generation" sentinel that downstream stages overwrite.
    if len(state.slides) < n:
        state.slides.extend([None] * (n - len(state.slides)))
    state.slides.insert(index, None)

    if len(state.html_paths) < n:
        state.html_paths.extend([None] * (n - len(state.html_paths)))
    state.html_paths.insert(index, None)

    # slide_images keys may be int or str (str after JSON round-trip);
    # normalise to int so the new dict is consistent regardless of how
    # state was loaded.
    state.slide_images = _shift_slide_images(state.slide_images, index, +1)

    _shift_skeleton_indices(state, index, +1)

    # Stale marks: bump every slide:N where N >= index up by 1.
    store = StaleStore.from_dict(state.stale_marks)
    _shift_stale_marks(store, index, +1)
    state.stale_marks = store.as_dict()

    return index


def delete_slide(state: AgentState, index: int) -> None:
    """Remove slide at ``index`` and align every parallel array.

    Raises ``IndexError`` if ``index`` is out of range.
    """
    if not (0 <= index < len(state.outline)):
        raise IndexError(
            f"delete_slide: index {index} out of range for outline "
            f"of length {len(state.outline)}"
        )

    del state.outline[index]
    if index < len(state.slides):
        del state.slides[index]
    if index < len(state.html_paths):
        del state.html_paths[index]

    state.slide_images = _shift_slide_images(state.slide_images, index, -1)

    _shift_skeleton_indices(state, index, -1)

    # Stale marks: drop slide:index everywhere, shift slide:N (>index) down.
    store = StaleStore.from_dict(state.stale_marks)
    _shift_stale_marks(store, index, -1)
    state.stale_marks = store.as_dict()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _shift_slide_images(
    slide_images: Dict[Any, Any], pivot: int, delta: int
) -> Dict[int, Any]:
    """Return a new ``slide_images`` dict with shifted int keys.

    ``delta=+1`` (insert at ``pivot``): every ``k >= pivot`` becomes ``k+1``.
    ``delta=-1`` (delete at ``pivot``): ``k == pivot`` is dropped; every
    ``k > pivot`` becomes ``k-1``.
    """
    new: Dict[int, Any] = {}
    for k, v in slide_images.items():
        idx = int(k)
        if delta > 0:
            new_idx = idx + 1 if idx >= pivot else idx
        else:  # delta < 0
            if idx == pivot:
                continue  # deleted slide's images
            new_idx = idx - 1 if idx > pivot else idx
        new[new_idx] = v
    return new


def _shift_skeleton_indices(state: AgentState, pivot: int, delta: int) -> None:
    """Reindex ``state.deck_skeleton`` after a structural slide change.

    No-op when ``deck_skeleton`` is None (one-shot outline fallback path).
    """
    skeleton = state.deck_skeleton
    if skeleton is None:
        return

    groups = skeleton.get("groups") or []
    for group in groups:
        if not isinstance(group, dict):
            continue
        ids = group.get("slide_indices")
        if not isinstance(ids, list):
            continue
        group["slide_indices"] = _shift_index_list(ids, pivot, delta)

    # slide_intents is a list aligned with outline index — grow/shrink
    # at pivot to keep parallel with the new outline length.
    intents = skeleton.get("slide_intents")
    if isinstance(intents, list):
        if delta > 0:
            intents.insert(
                pivot, {"group_id": "", "image_intent": "none"}
            )
        else:  # delta < 0
            if 0 <= pivot < len(intents):
                del intents[pivot]


def _shift_index_list(
    indices: List[int], pivot: int, delta: int
) -> List[int]:
    """Shift a flat list of slide indices by the same rules as slide_images."""
    new: List[int] = []
    for idx in indices:
        if isinstance(idx, bool) or not isinstance(idx, int):
            # Pass through malformed entries unchanged rather than crash.
            new.append(idx)
            continue
        if delta > 0:
            new.append(idx + 1 if idx >= pivot else idx)
        else:  # delta < 0
            if idx == pivot:
                continue  # deleted slide
            new.append(idx - 1 if idx > pivot else idx)
    return new


def _shift_stale_marks(store: StaleStore, pivot: int, delta: int) -> None:
    """Rewrite every ``slide:N`` target_id in ``store`` per the shift rules.

    Operates in-place: rebuilds each stage's bucket with shifted marks.
    Marks whose target_id does not match the ``slide:N`` / ``slide:N:slot:ID``
    pattern (e.g. ``"all"``) are preserved verbatim.
    """
    for stage in store.stages():
        old_marks = store.for_stage(stage)
        new_marks: List[StaleMark] = []
        for mark in old_marks:
            new_id = _shift_target_id(mark.target_id, pivot, delta)
            if new_id is None:
                # Either non-slide id (preserve) or deleted slide (drop).
                if _is_slide_id(mark.target_id):
                    # Deleted slide's mark — skip.
                    continue
                new_marks.append(mark)
                continue
            # Rebuild the mark with the shifted id; preserve everything else.
            new_marks.append(
                StaleMark(
                    target_id=new_id,
                    source_stage=mark.source_stage,
                    source_id=_shift_target_id(mark.source_id, pivot, delta)
                    or mark.source_id,
                    reason=mark.reason,
                    created_at=mark.created_at,
                    context_snapshot=mark.context_snapshot,
                )
            )
        # Replace the stage's bucket. We can't reach into the private
        # dict, so dismiss-all then merge the new list.
        store.clear_stage(stage)
        store.merge(stage, new_marks)


def _is_slide_id(target_id: str) -> bool:
    return bool(_SLIDE_ID_RE.match(target_id or ""))


def _shift_target_id(target_id: str, pivot: int, delta: int):
    """Return the shifted form of a ``slide:N[:slot:ID]`` id, or None.

    None means "drop this id" (deleted slide) or "not a slide id" (caller
    should preserve verbatim). The caller distinguishes via ``_is_slide_id``.
    """
    if not target_id:
        return None
    m = _SLIDE_ID_RE.match(target_id)
    if m is None:
        return None
    n = int(m.group(1))
    suffix = m.group(2) or ""
    if delta > 0:
        new_n = n + 1 if n >= pivot else n
    else:  # delta < 0
        if n == pivot:
            return None  # signal "drop"
        new_n = n - 1 if n > pivot else n
    return f"slide:{new_n}{suffix}"
