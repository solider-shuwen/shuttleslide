"""Stale propagation rules — translate upstream edits into downstream marks.

This module is the *rules engine* of the stale system. Given an upstream
edit (theme field changed, outline item rewritten, slide HTML edited, ...)
it produces the set of stale marks that should land on downstream stages.

The output is a ``Dict[stage_name, List[StaleMark]]`` ready for
``StaleStore.merge``. The engine is **pure**: no I/O, no store mutation,
no orchestrator coupling. Each rule is a function from :class:`EditEvent`
to a mark-set; the dispatch table at the bottom of the file maps
``(source_stage, change_type)`` to its rule. New rules slot in as new
table rows — there is no inheritance or registration ceremony.

Rules (summary — see ``_DISPATCH`` for the authoritative table):

    ============== ============= ==========================================
    source         change_type  downstream effect
    ============== ============= ==========================================
    theme          visual_only  (none — handled by sibling render cascade)
    theme          semantic     images/slides/rendered: ``all``
    theme          mixed        images/slides/rendered: ``all``
    outline        structural   images/slides/rendered: ``all``
    outline        item         images/slides/rendered: ``slide:N`` (changed)
    images         item         slides/rendered: ``slide:N``
    slides         item         rendered: ``slide:N``
    ============== ============= ==========================================

Theme field classification
--------------------------
``THEME_VISUAL_FIELDS`` (colours + fonts) are substituted at render time
via ``{{theme.<field>}}`` placeholders; editing them is handled by the
already-implemented cascade in
:func:`InteractiveOrchestrator._refresh_after_edit` and produces no stale.

``THEME_SEMANTIC_FIELDS`` (``decoration_style`` / ``cover_bg_strategy`` /
``layout_conventions``) are LLM *decision inputs* — they shape how the
slide-builder lays out a slide. Changing them requires LLM regeneration,
so they trigger full downstream stale marks.

Source for field classification: ``theme_tools.py:55-68`` (``define_theme``
schema) and ``theme_tokens.py`` (``_ALLOWED_FIELDS`` whitelist).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple

from shuttleslide.agent.review.stale import StaleMark, now


# ---------------------------------------------------------------------------
# Theme field classification
# ---------------------------------------------------------------------------

# Visual fields — substituted at render time via {{theme.<field>}} placeholders.
# Editing any of these triggers the live cascade (already implemented in
# InteractiveOrchestrator._refresh_after_edit) and does NOT need a stale
# mark: re-rendering with the new values is sufficient.
THEME_VISUAL_FIELDS: frozenset[str] = frozenset({
    "primary_color",
    "accent_color",
    "warn_color",
    "bg_color",
    "text_color",
    "title_color",
    "font_title",
    "font_body",
})

# Semantic fields — LLM decision inputs at slide-builder time.
# These shape layout/decoration choices; changing them requires the LLM
# to regenerate affected slides. Render-time substitution does not apply.
THEME_SEMANTIC_FIELDS: frozenset[str] = frozenset({
    "decoration_style",     # enum: minimal/glassmorphism/neon/editorial/playful
    "cover_bg_strategy",    # enum: dark_gradient/image_overlay/solid_color/geometric
    "layout_conventions",   # free-form text: 1-3 sentence layout description
})

# Stages that can carry marks. ``theme`` and ``outline`` are sources —
# they never receive marks. Order matters only for deterministic test
# output; the dispatch functions do not rely on it.
MARKABLE_STAGES: Tuple[str, ...] = ("images", "slides", "rendered")


# ---------------------------------------------------------------------------
# Edit event description
# ---------------------------------------------------------------------------


ChangeType = Literal[
    "visual_only",   # theme: only visual fields changed
    "semantic",      # theme: only semantic fields changed
    "mixed",         # theme: both visual + semantic changed
    "structural",    # outline: length changed (slides added/removed/reordered)
    "item",          # per-item edit (slide N, image slot, outline[i] content)
]


@dataclass
class EditEvent:
    """Description of an upstream edit, ready for the rules engine.

    Fields are deliberately redundant (``stage`` + ``change_type`` +
    ``slide_idx`` could in principle be derived from ``before`` / ``after``)
    because the upstream caller already knows all three. Forcing the
    propagation engine to re-derive them from values would couple it to
    each stage's data shape — keeping them explicit keeps the engine
    stage-agnostic.

    ``target_path`` is carried for diagnostics only (logging / debugging
    "why did this mark appear?"); it does not affect rule selection.
    """

    stage: str                 # "theme" | "outline" | "images" | "slides"
    change_type: ChangeType
    before: Any                # stage-specific shape (dict for theme, list for outline)
    after: Any                 # same shape as ``before``
    slide_idx: Optional[int] = None  # required for change_type == "item"
    slot_id: Optional[str] = None    # images-only sub-granularity
    target_path: Tuple[Any, ...] = ()  # original EditTarget.path (diagnostics)


# ---------------------------------------------------------------------------
# Change-type detection helpers
# ---------------------------------------------------------------------------


def detect_theme_change_type(
    before: Dict[str, Any], after: Dict[str, Any]
) -> ChangeType:
    """Classify a theme edit by which field bucket changed.

    Returns ``"visual_only"`` / ``"semantic"`` / ``"mixed"``. Scans both
    buckets (does not short-circuit on the first diff) so that a mixed
    edit — e.g. an LLM regen that touched ``primary_color`` AND
    ``decoration_style`` — is correctly reported as ``"mixed"`` and
    triggers the semantic propagation path.

    A no-op edit (``before == after``) returns ``"visual_only"``; the
    rule handler returns an empty mark-set, which is the correct outcome.
    """
    diff_visual = any(
        before.get(f) != after.get(f) for f in THEME_VISUAL_FIELDS
    )
    diff_semantic = any(
        before.get(f) != after.get(f) for f in THEME_SEMANTIC_FIELDS
    )
    if diff_visual and diff_semantic:
        return "mixed"
    if diff_semantic:
        return "semantic"
    # diff_visual only, OR no diff at all — both produce no stale marks.
    return "visual_only"


def detect_outline_change_type(
    before: List[Any], after: List[Any]
) -> ChangeType:
    """``"structural"`` if length changed, ``"item"`` otherwise.

    Length change is the only structural signal we need: an outline that
    grew/shrunk invalidates every downstream slide's index mapping (we
    cannot tell which slide moved where without solving an assignment
    problem, which is overkill). Content-only edits preserve indices and
    propagate per-slide.
    """
    if len(before) != len(after):
        return "structural"
    return "item"


def diff_outline_slide_indices(
    before: List[Dict[str, Any]], after: List[Dict[str, Any]]
) -> List[int]:
    """Indices where ``before[i] != after[i]`` for same-length outlines.

    Used by the ``outline + item`` rule to decide which per-slide marks
    to emit. Only the changed indices propagate — emitting marks for
    unchanged slides would create false-positive stale badges and waste
    user attention.

    Returns ascending-ordered indices. Comparison is whole-item ``!=``;
    the orchestrator replaces whole outline items on edit (not individual
    fields), so deep equality is the right granularity.
    """
    if len(before) != len(after):
        # Defensive — callers route structural cases through a different
        # branch. Returning [] here means a misrouted event produces no
        # marks rather than crashing.
        return []
    return [i for i, (b, a) in enumerate(zip(before, after)) if b != a]


# ---------------------------------------------------------------------------
# Mark-set builders (shared by rules)
# ---------------------------------------------------------------------------


def _build_full_stale_set(
    source_stage: str,
    source_id: str,
    reason: str,
    *,
    context_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[StaleMark]]:
    """Emit one ``"all"`` mark per downstream stage.

    Used by theme semantic + outline structural rules — both cases where
    every downstream item is potentially affected and per-slide granularity
    does not apply.
    """
    timestamp = now()
    return {
        stage: [
            StaleMark(
                target_id="all",
                source_stage=source_stage,
                source_id=source_id,
                reason=reason,
                created_at=timestamp,
                context_snapshot=context_snapshot,
            )
        ]
        for stage in MARKABLE_STAGES
    }


def _build_per_slide_set(
    source_stage: str,
    source_id_template: str,
    reason_template: str,
    slide_indices: List[int],
    downstream_stages: Tuple[str, ...],
    context_snapshot_factory: Optional[Callable[[int], Dict[str, Any]]] = None,
) -> Dict[str, List[StaleMark]]:
    """Emit one mark per (downstream stage, slide index).

    ``source_id_template`` and ``reason_template`` are ``str.format``-style
    templates with a single ``{idx}`` placeholder — each slide's mark
    carries its own ``source_id`` (e.g. ``"slide:2"``) so the UI can
    point the user at the offending upstream item.

    ``context_snapshot_factory`` is optional. When provided, it is called
    once per slide index to produce a stage-agnostic snapshot (callers
    embed whatever the regenerator will need for that slide).
    """
    if not slide_indices:
        return {}
    timestamp = now()
    out: Dict[str, List[StaleMark]] = {stage: [] for stage in downstream_stages}
    for idx in slide_indices:
        snap = context_snapshot_factory(idx) if context_snapshot_factory else None
        source_id = source_id_template.format(idx=idx)
        reason = reason_template.format(idx=idx)
        for stage in downstream_stages:
            out[stage].append(
                StaleMark(
                    target_id=f"slide:{idx}",
                    source_stage=source_stage,
                    source_id=source_id,
                    reason=reason,
                    created_at=timestamp,
                    context_snapshot=snap,
                )
            )
    return out


# ---------------------------------------------------------------------------
# Per-rule handlers
# ---------------------------------------------------------------------------


def _propagate_theme_semantic(event: EditEvent) -> Dict[str, List[StaleMark]]:
    """``theme.decoration_style`` / ``cover_bg_strategy`` / ``layout_conventions`` changed.

    Every downstream stage goes fully stale: layout decisions were made
    against the old theme, so every slide needs regeneration.

    Snapshot carries both before/after theme dicts so the incremental
    regenerator can describe the diff to the LLM (``"theme X→Y, update
    this slide's decorations to match"``).
    """
    snapshot = {"theme_before": event.before, "theme_after": event.after}
    changed_fields = sorted(
        f for f in THEME_SEMANTIC_FIELDS
        if event.before.get(f) != event.after.get(f)
    )
    reason = f"theme changed: {', '.join(changed_fields)}"
    return _build_full_stale_set(
        source_stage="theme",
        source_id="all",
        reason=reason,
        context_snapshot=snapshot,
    )


def _propagate_outline_structural(event: EditEvent) -> Dict[str, List[StaleMark]]:
    """Outline length changed — downstream slide indices are invalid.

    Even if the user "only" appended a slide at the end, every downstream
    stage that keyed off ``slide_idx`` needs to be treated as fully stale.
    We do not try to be clever about "the new slide is at index N, so
    0..N-1 are fine" because the LLM may also have shifted deck context
    (a new section divider at index 2 reshapes the whole narrative).

    Snapshot carries the full before/after outline so the regenerator
    can show the LLM the structural change.
    """
    snapshot = {"outline_before": event.before, "outline_after": event.after}
    reason = (
        f"outline structural change ({len(event.before)}→{len(event.after)} slides)"
    )
    return _build_full_stale_set(
        source_stage="outline",
        source_id="all",
        reason=reason,
        context_snapshot=snapshot,
    )


def _propagate_outline_item(event: EditEvent) -> Dict[str, List[StaleMark]]:
    """Outline content changed at specific indices, length unchanged.

    Per-slide propagation: only the changed indices get stale marks.
    Snapshot is per-slide (just the affected outline item before/after)
    so the incremental prompt stays small.
    """
    before = event.before
    after = event.after
    changed = diff_outline_slide_indices(before, after)
    if not changed:
        return {}  # no-op edit (caller misrouted an unchanged edit)

    def _snap(idx: int) -> Dict[str, Any]:
        return {"outline_before": before[idx], "outline_after": after[idx]}

    return _build_per_slide_set(
        source_stage="outline",
        source_id_template="slide:{idx}",
        reason_template="outline[{idx}] edited",
        slide_indices=changed,
        downstream_stages=("images", "slides", "rendered"),
        context_snapshot_factory=_snap,
    )


def _propagate_images_item(event: EditEvent) -> Dict[str, List[StaleMark]]:
    """One image slot (or all slots on one slide) changed.

    Propagates to slides + rendered for that slide only. Snapshot records
    the slot id and the before/after payload so the regenerator can
    describe what specifically was swapped.
    """
    idx = event.slide_idx
    if idx is None:
        return {}  # misrouted; per-item images edit must have slide_idx
    slot_desc = f":slot:{event.slot_id}" if event.slot_id else ""
    snapshot = {
        "image_before": event.before,
        "image_after": event.after,
        "slide_idx": idx,
        "slot_id": event.slot_id,
    }
    return _build_per_slide_set(
        source_stage="images",
        source_id_template=f"slide:{{idx}}{slot_desc}",
        reason_template=f"image for slide {{idx}}{slot_desc} changed",
        slide_indices=[idx],
        downstream_stages=("slides", "rendered"),
        context_snapshot_factory=lambda _i: snapshot,
    )


def _propagate_slides_item(event: EditEvent) -> Dict[str, List[StaleMark]]:
    """Slide HTML edited by user — only ``rendered[i]`` goes stale.

    ``rendered`` is a deterministic transform of ``slides + theme`` with
    no LLM call, so no incremental snapshot is needed — the renderer
    just re-renders the affected slide.
    """
    idx = event.slide_idx
    if idx is None:
        return {}
    return _build_per_slide_set(
        source_stage="slides",
        source_id_template="slide:{idx}",
        reason_template="slide[{idx}] HTML edited",
        slide_indices=[idx],
        downstream_stages=("rendered",),
        context_snapshot_factory=None,
    )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------


# Key = (source_stage, change_type). Missing keys fall through to empty
# output in ``compute_stale_marks`` (defensive — better to miss a
# propagation than to crash the edit path).
_DISPATCH: Dict[
    Tuple[str, ChangeType],
    Callable[[EditEvent], Dict[str, List[StaleMark]]],
] = {
    ("theme",   "visual_only"): lambda e: {},
    ("theme",   "semantic"):    _propagate_theme_semantic,
    ("theme",   "mixed"):       _propagate_theme_semantic,
    ("outline", "structural"):  _propagate_outline_structural,
    ("outline", "item"):        _propagate_outline_item,
    ("images",  "item"):        _propagate_images_item,
    ("slides",  "item"):        _propagate_slides_item,
}


def compute_stale_marks(event: EditEvent) -> Dict[str, List[StaleMark]]:
    """Compute the stale marks produced by an upstream edit.

    Returns a dict keyed by downstream stage name (``"images"`` /
    ``"slides"`` / ``"rendered"``). Empty dict if the edit produces no
    downstream stale (e.g. theme visual-only edit, or a no-op).

    Output is ready to feed straight into ``StaleStore.merge(stage, marks)``
    for each stage key. The function does not mutate any state — pure
    derivation from ``event``.
    """
    handler = _DISPATCH.get((event.stage, event.change_type))
    if handler is None:
        # Unknown edit shape — return empty rather than raise. Propagation
        # is a *should*, not a *must*: a missed propagation just means the
        # user might see a slightly stale slide, which they can fix by
        # manually triggering regenerate. Raising here would break the
        # entire edit path, which is worse.
        return {}
    return handler(event)


# ---------------------------------------------------------------------------
# EditEvent construction helper
# ---------------------------------------------------------------------------


def build_edit_event(
    stage: str,
    before: Any,
    after: Any,
    *,
    slide_idx: Optional[int] = None,
    slot_id: Optional[str] = None,
    target_path: Tuple[Any, ...] = (),
) -> EditEvent:
    """Build an :class:`EditEvent` from raw edit values.

    Encapsulates the per-stage ``change_type`` detection so callers
    (orchestrator / undo / regenerator) don't need to know the rules.
    Callers supply the before/after values for the *specific* edited
    target (whole theme dict for theme edits, whole outline list for
    outline, single slide's HTML for slides, single slot payload for
    images) plus the optional ``slide_idx`` / ``slot_id`` carried on
    ``EditTarget.meta``.

    For theme and outline the change_type is derived from the values
    (visual/semantic/mixed, structural/item). For images and slides
    the change_type is always ``"item"`` — there's no structural
    per-item edit.
    """
    if stage == "theme":
        change_type: ChangeType = detect_theme_change_type(before, after)
    elif stage == "outline":
        change_type = detect_outline_change_type(before, after)
    elif stage in ("images", "slides"):
        change_type = "item"
    else:
        # Unknown / extension stage — fall through to the empty-result
        # path in compute_stale_marks. We still build the event so the
        # diagnostic ``target_path`` is preserved if anyone inspects it.
        change_type = "item"
    return EditEvent(
        stage=stage,
        change_type=change_type,
        before=before,
        after=after,
        slide_idx=slide_idx,
        slot_id=slot_id,
        target_path=target_path,
    )
