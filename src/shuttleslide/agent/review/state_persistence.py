"""AgentState save/load for fast review-UI iteration.

Saves the full pipeline state to ``{output_dir}/agent_state.json`` after
each stage so the next run can skip LLM calls and re-emit snapshots to
the review UI. Designed for UI/server development velocity — NOT for
caching LLM output across prompt changes (those require fresh runs).

Why not reuse ``html_to_pptx.schema.load_presentation``?
-------------------------------------------------------
``load_presentation`` (schema.py:524) drops ``SlideDSL.slots`` during
reconstruction. ``slots["html"]`` is exactly what the review UI renders,
so a round-trip through it would silently lose every slide's HTML.
This module ships its own ``_dict_to_slidedsl`` that preserves ``slots``
verbatim. If ``load_presentation`` ever grows slot support, we can
delete the helper here and reuse the schema one.

State file shape
----------------
```json
{
  "version": 2,
  "saved_at": 1718000000.0,
  "topic": "...",
  "style_hint": "cute",
  "target_count": null,
  "canvas_width_emu": 12192000,
  "canvas_height_emu": 6858000,
  "theme": {...},
  "outline": [...],
  "deck_skeleton": {...|null},
  "slide_images": {"0": {"hero": {...}}},
  "slides": [{"layout": "free_form", "slots": {"html": "..."}}],
  "html_paths": [...],
  "stage_outputs": {"script": {...}, "voiceover": {...}},
  "warnings": [],
  "errors": []
}
```

Version history:
  - v1: original schema (no ``stage_outputs``).
  - v2: adds ``stage_outputs`` (the only sanctioned extension channel
    for pro / extension stages; see AgentState). v1 files load with
    ``stage_outputs={}`` — no migration needed.

Not persisted: ``current_svg_spec`` and ``current_slide_messages`` are
scratch fields that don't carry meaning across runs.

Atomic writes
-------------
``save_state`` writes to ``{path}.tmp`` then ``os.replace`` — concurrent
readers (e.g. a separate process inspecting the file) never see a
half-written JSON. Single-process review sessions don't strictly need
this, but the cost is trivial and it future-proofs against concurrent
tools.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict

from shuttleslide.agent.state import AgentState
from shuttleslide.html_to_pptx.schema import (
    BackgroundDef,
    GradientDef,
    GradientStop,
    SlideDSL,
)


_STATE_VERSION = 2


def save_state(state: AgentState, path: Path) -> None:
    """Serialize ``state`` to ``path`` as JSON. Atomic write.

    Parent directory must exist (callers pass ``output_dir / filename``
    where ``output_dir`` is created by the orchestrator / CLI).
    """
    payload: Dict[str, Any] = {
        "version": _STATE_VERSION,
        "saved_at": time.time(),
        "topic": state.topic,
        "style_hint": state.style_hint,
        "target_count": state.target_count,
        "canvas_width_emu": state.canvas_width_emu,
        "canvas_height_emu": state.canvas_height_emu,
        "theme": state.theme,
        "outline": state.outline,
        "deck_skeleton": state.deck_skeleton,
        # JSON object keys must be str — slide_images has int slide indices.
        # Reverse mapping happens in load_state. We stringify here rather
        # than letting json.dumps do it so the format is explicit.
        "slide_images": {
            str(idx): slots for idx, slots in state.slide_images.items()
        },
        "slides": [_slidedsl_to_dict(s) for s in state.slides],
        "html_paths": list(state.html_paths),
        # Extension stage outputs (script / voiceover / etc.) — v2.
        # Stored as-is; values must already be JSON-safe.
        "stage_outputs": dict(state.stage_outputs),
        "warnings": list(state.warnings),
        "errors": list(state.errors),
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_state(path: Path) -> AgentState:
    """Deserialize an AgentState previously written by ``save_state``.

    Raises ``FileNotFoundError`` if ``path`` doesn't exist — let the
    caller decide whether that's an error or a "fresh run" signal.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    # slide_images str keys -> int (state.py:121 types it as Dict[int, ...]).
    # Downstream code does ``state.slide_images[0]`` and a str key would
    # KeyError on the first slide_builder call.
    raw_slide_images = payload.get("slide_images", {})
    slide_images = {
        int(idx): slots for idx, slots in raw_slide_images.items()
    }
    return AgentState(
        topic=payload.get("topic", ""),
        style_hint=payload.get("style_hint", "business"),
        target_count=payload.get("target_count"),
        canvas_width_emu=payload.get(
            "canvas_width_emu", AgentState.canvas_width_emu
        ),
        canvas_height_emu=payload.get(
            "canvas_height_emu", AgentState.canvas_height_emu
        ),
        theme=payload.get("theme", {}) or {},
        outline=payload.get("outline", []) or [],
        deck_skeleton=payload.get("deck_skeleton"),
        slide_images=slide_images,
        slides=[_dict_to_slidedsl(s) for s in payload.get("slides", [])],
        html_paths=list(payload.get("html_paths", [])),
        # v2 field — older v1 files don't have this; default to {}.
        stage_outputs=dict(payload.get("stage_outputs", {}) or {}),
        warnings=list(payload.get("warnings", [])),
        errors=list(payload.get("errors", [])),
    )


# ---------------------------------------------------------------------------
# SlideDSL helpers — preserve slots (production load_presentation drops them)
# ---------------------------------------------------------------------------


def _slidedsl_to_dict(slide: SlideDSL) -> Dict[str, Any]:
    """Convert SlideDSL to dict, preserving ``slots``.

    ``dataclasses.asdict`` recurses into nested dataclasses but leaves
    plain dicts (like ``slots``) untouched, which is what we want.
    Elements are dataclass instances — asdict flattens them too.
    """
    return asdict(slide)


def _dict_to_slidedsl(d: Dict[str, Any]) -> SlideDSL:
    """Reverse of ``_slidedsl_to_dict``.

    Reconstructs ``background`` and ``elements`` (as the production
    schema would), plus threads ``slots`` through verbatim. We don't
    reuse ``schema._dict_to_element`` because review-state loads only
    need ``slots['html']`` for the iframe — full element fidelity is
    only relevant if the loaded deck is re-rendered to PPTX, which is
    out of scope for the review-iteration loop.
    """
    bg_data = d.get("background")
    background = None
    if isinstance(bg_data, dict):
        gradient = None
        grad_data = bg_data.get("gradient")
        if isinstance(grad_data, dict):
            gradient = GradientDef(
                direction=grad_data.get("direction", "horizontal"),
                stops=[
                    GradientStop(**gs)
                    for gs in grad_data.get("stops", [])
                    if isinstance(gs, dict)
                ],
            )
        background = BackgroundDef(
            type=bg_data.get("type", "solid"),
            color=bg_data.get("color"),
            gradient=gradient,
            image_url=bg_data.get("image_url"),
        )
    return SlideDSL(
        layout=d.get("layout", "free_form"),
        background=background,
        elements=list(d.get("elements", [])),
        slots=dict(d.get("slots", {})),
    )
