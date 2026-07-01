"""Shared state object threaded through the agent pipeline.

One AgentState instance is created at pipeline start and passed (by reference,
langgraph-style) through every stage. Each stage reads inputs from the state
and writes its outputs back to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from shuttleslide.agent.geometry import EMU_PER_CSS_PX
from shuttleslide.html_to_pptx.schema import SlideDSL


@dataclass
class AgentState:
    """Pipeline state.

    Fields are populated progressively:
      - topic / style_hint / target_count: inputs (immutable after init)
      - canvas_width_emu / canvas_height_emu: canvas geometry (immutable
        after init; threaded through to the renderer and PPTX writer)
      - theme: dict produced by Stage 1 (kept as dict — LLM decides its shape)
      - outline: list of dicts produced by Stage 2 (layout, title, key_points)
      - slides: list[SlideDSL] produced by Stage 3 (one per outline item)
      - html_paths: list[str] produced by Stage 4
      - current_slide_messages: per-slide scratch conversation buffer (cleared
        at slide start; never carried across slides)
      - errors: non-fatal errors collected for diagnostics
    """

    # Inputs
    topic: str = ""
    style_hint: str = "business"
    target_count: Optional[int] = None  # None = LLM infers from topic

    # Canvas geometry — defaults reproduce 16:9 widescreen (1280x720 CSS px).
    # The orchestrator overrides these from AgentConfig before any node runs.
    canvas_width_emu: int = 12192000
    canvas_height_emu: int = 6858000

    # Stage outputs
    theme: Dict[str, Any] = field(default_factory=dict)
    outline: List[Dict[str, Any]] = field(default_factory=list)
    # Stage 2a output (progressive outline): high-level deck structure
    # produced by run_structure_planner. None when the progressive path
    # is not used or not yet run; populated before run_slide_detail_generator
    # starts. Shape:
    #   {
    #     "thesis": str,
    #     "groups": [{"id": "g1", "name": "...", "slide_indices": [0,1,2]}],
    #     "slide_intents": [
    #       {"group_id": "g1", "layout_intent": "hero_cover",
    #        "image_intent": "hero"|"diagram"|"flowchart"|"chart"|
    #                        "icon_cluster"|"illustration"|"none"},
    #       ...
    #     ]
    #   }
    # Consumed only by build_slide_detail_generator_prompt to pass the
    # deck-wide structure into each per-slide LLM call. Downstream stages
    # (image_acquirer, slide_builder) read state.outline only and do NOT
    # need this field.
    deck_skeleton: Optional[Dict[str, Any]] = None
    slides: List[SlideDSL] = field(default_factory=list)
    html_paths: List[str] = field(default_factory=list)

    # Image acquirer outputs: slide_idx -> {slot_id: image_payload}
    # Populated by Stage 2.5 (run_image_acquirer). Consumed by the slide
    # builder via build_slide_builder_prompt(slide_images=...).
    #
    # image_payload is a dict (not a bare string) so it can carry one of
    # several typed shapes. Downstream consumers branch on payload["type"].
    #
    #   - SVG on disk (current production svg path; the slide HTML embeds
    #     a short <img class="shuttleslide-svg-placeholder"
    #     src="svgs/slide_N_slot.svg"> reference and html_to_pptx inlines
    #     the SVG back during the Playwright pass — bytes never flow
    #     through the slide-builder LLM context or the 12000-char HTML cap):
    #       {
    #         "type": "svg_file",
    #         "path": "svgs/slide_3_hero.svg",  # rel to output_dir
    #         "data": "<svg>...</svg>",          # inline markup (retained
    #                                             # so html_to_pptx can inline
    #                                             # without re-reading the file)
    #         "description": "...",              # from spec.description
    #         "image_type": "flowchart",         # from spec.image_type
    #         "mime": "image/svg+xml",
    #         "meta": {source_type, source_ref, vlm_verified, ...}
    #       }
    #   - Web image on disk (current production web path; same
    #     file-externalization pattern as svg_file):
    #       {
    #         "type": "image_file",
    #         "path": "images/slide_3_hero.jpg",  # rel to output_dir
    #         "description": "...",                # from spec.description
    #         "image_type": "hero",                # from spec.image_type
    #         "mime": "image/jpeg",
    #         "meta": {source_type="web", source_ref, vlm_verified, ...}
    #       }
    #   - Legacy inline SVG (kept for backward-compat with older tests /
    #     state shapes; not produced by the current set_svg path):
    #       {
    #         "type": "svg",
    #         "data": "<svg>...</svg>",
    #         "mime": "image/svg+xml",
    #         "meta": {...}
    #       }
    #   - Legacy base64 image (kept for backward-compat with older tests
    #     / state shapes; not produced by the current acquire path):
    #       {
    #         "type": "image",
    #         "data": "<base64>",
    #         "mime": "image/jpeg",
    #         "meta": {...}
    #       }
    # The "svg_file" and "image_file" shapes are the production paths.
    # The "svg" and "image" shapes are grandfathered; new code should
    # produce "svg_file" / "image_file".
    slide_images: Dict[int, Dict[str, Dict[str, Any]]] = field(default_factory=dict)

    # User-uploaded image library — populated at pipeline start from
    # AgentConfig.user_image_library (the review server fills the config
    # from POST /api/start's user_images payload). The outline planner
    # reads this list to honour the "user uploads MUST be used first"
    # rule; the image_acquirer's source_type="user_upload" branch reads
    # it to resolve source_ref (== image_id) into a concrete file.
    # Shape parity with AgentConfig.user_image_library — see config.py
    # for the per-entry contract.
    user_image_library: List[Dict[str, Any]] = field(default_factory=list)

    # Extension stage outputs: stage_name -> JSON-safe dict.
    #
    # This is the ONLY sanctioned place for pro / extension stages
    # (script generation, voice-over, subtitles, ...) to write their
    # outputs. Pro stages must NOT add new top-level fields to
    # AgentState — the dataclass shape stays stable across versions
    # so state_persistence round-trips and tests don't break every
    # time a stage is added.
    #
    # Core stages continue to use their dedicated fields (state.theme,
    # state.outline, ...). This dict is purely the extension channel.
    stage_outputs: Dict[str, Any] = field(default_factory=dict)

    # Scratch pointer for the set_svg tool to know which spec it's serving.
    # image_acquirer sets this immediately before each LLM call; the set_svg
    # tool reads it to know where to store the result. None outside the
    # Image Acquirer stage.
    current_svg_spec: Optional[Dict[str, Any]] = None

    # Per-slide scratch (reset by the slide builder)
    current_slide_messages: List[Dict[str, Any]] = field(default_factory=list)

    # Diagnostics
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    # Stale markers for downstream stages (review pipeline).
    #
    # When the user edits an upstream stage after downstream stages have
    # already produced output, the affected downstream items are marked
    # stale rather than auto-regenerated. Marks carry the upstream source
    # identity and an optional snapshot of the pre-edit state (used by
    # the incremental regenerator to build before/after diffs).
    #
    # Shape: ``{stage_name: [mark_dict, ...]}``. Each mark_dict is the
    # JSON-safe form of ``StaleMark`` (see ``review/stale.py``). Keys
    # are downstream stage names (``"images"`` / ``"slides"`` /
    # ``"rendered"``); ``theme`` and ``outline`` are sources and never
    # appear here.
    #
    # Operated on through ``StaleStore`` — direct dict mutation bypasses
    # dedup. The dict form (not List[StaleMark]) keeps AgentState JSON-safe
    # and avoids importing the dataclass into state.py.
    stale_marks: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Canvas geometry derived helpers (CSS px). Convenience properties so
    # downstream code reads ``state.canvas_width_px`` instead of redoing
    # the EMU-to-px division everywhere.
    # ------------------------------------------------------------------
    @property
    def canvas_width_px(self) -> int:
        """Canvas width in CSS px (96 DPI)."""
        return self.canvas_width_emu // EMU_PER_CSS_PX

    @property
    def canvas_height_px(self) -> int:
        """Canvas height in CSS px (96 DPI)."""
        return self.canvas_height_emu // EMU_PER_CSS_PX

    def add_error(self, msg: str) -> None:
        self.errors.append(msg)

    def add_warning(self, msg: str) -> None:
        self.warnings.append(msg)
