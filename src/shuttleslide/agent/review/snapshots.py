"""Snapshot construction + UndoStack.

``build_snapshot`` walks an AgentState at a stage boundary and produces a
JSON-safe StageSnapshot. It is intentionally side-effect free — it does
not mutate state, only reads. The snapshot is what ships to the reviewer
UI in PR2.

``UndoStack`` is a simple LIFO of (target, old_value) pairs. PR3's
editors push to it before mutating state; the UI calls undo via WebSocket
to pop and restore.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from shuttleslide.agent.review.review_gate import (
    EditTarget,
    StageName,
    StageSnapshot,
)
from shuttleslide.agent.state import AgentState


def build_snapshot(stage: StageName, state: AgentState) -> StageSnapshot:
    """Capture a JSON-safe view of ``state`` at the given stage boundary.

    The returned snapshot's ``state_view`` contains only JSON-serialisable
    types (dict / list / str / int / float / bool / None). SlideDSL
    instances are converted to plain dicts; SVG/image payloads are
    preserved verbatim (they're already JSON-safe).

    Editable targets are derived per-stage so the UI knows which elements
    can be modified. PR1 produces the targets; PR3's editors consume them.
    """
    if stage == "theme":
        return _snapshot_theme(state)
    if stage == "outline":
        return _snapshot_outline(state)
    if stage == "images":
        return _snapshot_images(state)
    if stage == "slides":
        return _snapshot_slides(state)
    if stage == "rendered":
        return _snapshot_rendered(state)
    raise ValueError(f"unknown stage {stage!r}")


# ---------------------------------------------------------------------------
# Per-stage snapshot builders
# ---------------------------------------------------------------------------

def _snapshot_theme(state: AgentState) -> StageSnapshot:
    """Theme stage: single JSON target covering the entire theme dict."""
    theme_dict = dict(state.theme) if state.theme else {}
    targets: List[EditTarget] = []
    if theme_dict:
        targets.append(
            EditTarget(
                stage="theme",
                path=("theme",),
                kind="json",
                current_value=_json_dumps(theme_dict),
                meta={"slide_idx": None, "slot_id": None},
            )
        )
    return StageSnapshot(
        stage="theme",
        state_view={"theme": theme_dict},
        artifact_kind="json",
        editable_targets=targets,
        timestamp=time.time(),
    )


def _snapshot_outline(state: AgentState) -> StageSnapshot:
    """Outline stage: single JSON target covering the whole outline list."""
    outline = [dict(item) for item in state.outline] if state.outline else []
    targets: List[EditTarget] = []
    if outline:
        targets.append(
            EditTarget(
                stage="outline",
                path=("outline",),
                kind="json",
                current_value=_json_dumps(outline),
                meta={"slide_idx": None, "slot_id": None},
            )
        )
    return StageSnapshot(
        stage="outline",
        state_view={"outline": outline},
        artifact_kind="json",
        editable_targets=targets,
        timestamp=time.time(),
    )


def _snapshot_images(state: AgentState) -> StageSnapshot:
    """Image acquirer stage: one EditTarget per (slide_idx, slot_id) pair.

    Kind is determined by the payload's ``type`` field:
        - "svg"        -> svg
        - "image_file" -> image
        - "image"      -> image (legacy base64)
    """
    state_view: Dict[str, Any] = {"slide_images": {}}
    targets: List[EditTarget] = []
    has_svg = False
    has_image = False
    for slide_idx, slots in state.slide_images.items():
        slot_view: Dict[str, Any] = {}
        for slot_id, payload in slots.items():
            # Copy payload but strip any non-JSON-safe fields defensively.
            safe_payload = _sanitise_payload(payload)
            slot_view[slot_id] = safe_payload
            p_type = payload.get("type")
            if p_type == "svg":
                kind = "svg"
                has_svg = True
                current_value = payload.get("data", "")
            elif p_type in ("image_file", "image"):
                kind = "image"
                has_image = True
                # For images, the editable value is the path (user can't
                # edit bytes inline — they upload). LLM edits are disabled
                # for image kind in PR3.
                current_value = payload.get("path", payload.get("data", ""))
            else:
                # Unknown payload type — surface as image with empty value
                # so the UI shows *something* rather than hiding the slot.
                kind = "image"
                has_image = True
                current_value = ""
            targets.append(
                EditTarget(
                    stage="images",
                    path=("slide", int(slide_idx), "slot", slot_id),
                    kind=kind,
                    current_value=current_value,
                    meta={
                        "slide_idx": int(slide_idx),
                        "slot_id": slot_id,
                        "mime": payload.get("mime", ""),
                        "payload_type": p_type,
                    },
                )
            )
        state_view["slide_images"][str(slide_idx)] = slot_view
    artifact_kind = "mixed" if (has_svg and has_image) else (
        "svg" if has_svg else ("image" if has_image else "mixed")
    )
    return StageSnapshot(
        stage="images",
        state_view=state_view,
        artifact_kind=artifact_kind,
        editable_targets=targets,
        timestamp=time.time(),
    )


def _snapshot_slides(state: AgentState) -> StageSnapshot:
    """Slide builder stage: one HTML target per slide.

    Each SlideDSL has ``slots["html"]`` containing the inner HTML fragment
    produced by the slide builder. The snapshot exposes that fragment so
    the UI can render it in an iframe and let the user edit it.
    """
    state_view: Dict[str, Any] = {"slides": []}
    targets: List[EditTarget] = []
    for idx, slide in enumerate(state.slides):
        if slide is None:
            continue
        html = slide.slots.get("html", "") if hasattr(slide, "slots") else ""
        state_view["slides"].append({"index": idx, "html": html})
        targets.append(
            EditTarget(
                stage="slides",
                path=("slide", idx, "html"),
                kind="html",
                current_value=html,
                meta={"slide_idx": idx, "slot_id": "html"},
            )
        )
    return StageSnapshot(
        stage="slides",
        state_view=state_view,
        artifact_kind="html",
        editable_targets=targets,
        timestamp=time.time(),
    )


def _snapshot_rendered(state: AgentState) -> StageSnapshot:
    """Rendered stage: same view as 'slides' plus the on-disk html_paths.

    No editable targets — by the time we've rendered to disk the user can
    still undo upstream edits and re-render (PR5 regenerate territory).
    For PR1 we surface the rendered file list so the UI can show "open in
    browser" links.
    """
    base = _snapshot_slides(state)
    base.stage = "rendered"
    # state.html_paths is populated by orchestrator._finalize via the
    # renderer. If empty (output_dir not set), we still show slides.
    base.state_view["html_paths"] = list(state.html_paths) if state.html_paths else []
    # Rendered view is not editable in PR1.
    base.editable_targets = []
    return base


# ---------------------------------------------------------------------------
# UndoStack
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _json_dumps(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, indent=2)


def _sanitise_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copy a slide_images payload, dropping anything not JSON-safe.

    Payloads today are already JSON-safe (built by set_svg / image_acquirer),
    but a defensive copy guards against future fields that might sneak in
    Path / bytes / dataclass values.
    """
    safe: Dict[str, Any] = {}
    for k, v in payload.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        elif isinstance(v, dict):
            safe[k] = _sanitise_payload(v)
        elif isinstance(v, list):
            safe[k] = [
                _sanitise_payload(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            # Unknown type (Path, bytes, dataclass) — stringify so the UI
            # shows something rather than failing the whole snapshot.
            safe[k] = str(v)
    return safe
