"""Mock orchestrator for fast UI testing — bypasses real LLM calls.

When ``slidecraft review --mock`` is set, the review server constructs
:class:`MockInteractiveOrchestrator` instead of the real one. The mock
installs Stage subclasses that fire synthetic ``LLMResponseEvent``
callbacks (driving the progress strip just like a real run) and
populate ``AgentState`` with canned but realistic content, so the
review UI has something to render.

Design goals
------------
- **Fast**: each stage finishes in <5s with 200-500ms gaps between fake
  LLM events. Total pipeline ~10-15s vs minutes for a real run.
- **Representative**: the progress strip sees the same ``stage_progress``
  messages it would in production, so UI animations can be tested.
- **No external deps**: no API key, no network, no Playwright for stage
  work (the final HTML render still uses the real renderer, which is a
  pure-Python file-write — no browser).

Migration to registry-driven stages
-----------------------------------
Previously the mock subclass overrode ``_run_stage_*`` methods. After
the refactor those methods no longer exist — stages are standalone
classes registered with a ``StageRegistry``. The mock now subclasses
each core Stage and overrides ``run()``; ``MockInteractiveOrchestrator``
builds a fresh registry with these mocks and passes it via
``stage_registry=``. The ``is_cached`` / ``build_snapshot`` /
``finalize`` methods inherited from the core stages are reused
unchanged — only the work-doing path is faked.

Trade-offs
----------
The mock does NOT exercise the real LLM plumbing (prompt construction,
tool-call dispatch, parsing). Bugs in node code, prompt templates, or
tool schemas won't surface here. Use a real run for end-to-end coverage;
use ``--mock`` for fast UI iteration only.
"""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from shuttleslide.agent.config import AgentConfig
from shuttleslide.agent.review.broadcaster import Broadcaster
from shuttleslide.agent.review.core_stages import (
    ImagesStage,
    OutlineStage,
    SlidesStage,
    ThemeStage,
)
from shuttleslide.agent.review.interactive_orchestrator import (
    InteractiveOrchestrator,
)
from shuttleslide.agent.review.registry import StageRegistry
from shuttleslide.agent.review.review_gate import ReviewGate
from shuttleslide.agent.review.stage import StageContext
from shuttleslide.agent.llm.tool_call import LLMResponseEvent
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry
from shuttleslide.agent.dsl_to_html import SlideHTMLRenderer
from shuttleslide.html_to_pptx.schema import SlideDSL


# Delay range (seconds) between synthetic LLM events. Picked to be slow
# enough that the progress bar animation is visible (the CSS transition
# is 400ms; shorter gaps make the bar look like it's teleporting), but
# fast enough that a full 5-stage pipeline finishes in well under 30s.
_EVENT_DELAY_RANGE = (0.22, 0.48)


async def _sleep_jitter() -> None:
    """Sleep for a small randomised interval so progress events arrive
    at a realistic cadence. Without this, all events for a stage fire
    within a few ms and the progress bar appears to jump from 0 to 100%."""
    await asyncio.sleep(random.uniform(*_EVENT_DELAY_RANGE))


# Minimal but valid theme dict. ``_state_to_presentation`` reads these
# exact keys when building ThemeDef, so unknown keys are ignored and
# missing keys fall back to ThemeDef's own defaults.
_MOCK_THEME: Dict[str, Any] = {
    "primary_color": "#133EFF",
    "accent_color": "#00CD82",
    "warn_color": "#FF5722",
    "bg_color": "#FEFEFE",
    "text_color": "#1F2937",
    "font_title": "Roboto",
    "font_body": "Roboto",
}


# Inline SVG used as the hero image for every mock slide. Rendered
# verbatim by /artifact/images/{n}/hero (server.py image_artifact) so
# the review UI's <img> tag shows a real, non-broken picture. Dashed
# rect + centred label makes it obvious this is mock output, not a
# real LLM-acquired image.
_MOCK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 180" '
    'width="320" height="180">'
    '<rect width="320" height="180" fill="#F4F6FA" '
    'stroke="#C8D0DC" stroke-width="1.5" stroke-dasharray="6 5" rx="8"/>'
    '<text x="160" y="95" font-family="Roboto, sans-serif" font-size="16" '
    'font-weight="500" fill="#8A94A6" text-anchor="middle">mock image</text>'
    "</svg>"
)


def _mock_outline(topic: str, count: int) -> List[Dict[str, Any]]:
    """Return ``count`` outline items with predictable titles. The
    shape matches what the outline snapshot and the slide builder
    expect: title / layout / key_points."""
    return [
        {
            "title": f"{topic} — slide {i + 1}",
            "layout": "title" if i == 0 else "content",
            "key_points": [f"Mock point {i + 1}.1", f"Mock point {i + 1}.2"],
        }
        for i in range(count)
    ]


def _mock_slide_html(outline_item: Dict[str, Any], idx: int) -> str:
    """Return a simple but renderable HTML fragment for one slide.

    The real slide builder emits rich HTML with theme colours, layout,
    and SVG slots; this mock just produces a styled title + bullet list
    so the preview iframe shows *something* and the renderer has valid
    markup to wrap.
    """
    title = outline_item.get("title", f"Slide {idx + 1}")
    points = outline_item.get("key_points", [])
    bullets = "".join(f"<li>{p}</li>" for p in points)
    return (
        '<div style="padding:48px;font-family:Roboto,sans-serif;">'
        f'<h1 style="font-size:36px;color:#133EFF;margin:0 0 24px;">{title}</h1>'
        f'<ul style="font-size:20px;color:#1F2937;line-height:1.6;">{bullets}</ul>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Mock Stage subclasses — each overrides run() to fire synthetic events
# + populate canned state. is_cached / build_snapshot / finalize are
# inherited from the parent Stage class unchanged.
# ---------------------------------------------------------------------------


class _MockThemeStage(ThemeStage):
    """Mock theme stage: 3 atomic events, then populate state.theme.

    Theme is atomic (single LLM call in production) so no
    slide_index is sent — the UI shows the indeterminate flowing
    animation + elapsed timer.
    """

    def __init__(self, orch: "MockInteractiveOrchestrator") -> None:
        super().__init__()
        self._orch = orch

    async def run(self, ctx: StageContext) -> None:
        for i in range(1, 4):
            await _sleep_jitter()
            self._orch._emit("theme", iteration=i, max_iterations=3)
        ctx.state.theme = dict(_MOCK_THEME)


class _MockOutlineStage(OutlineStage):
    """Mock outline stage: one event per slide, then populate
    state.outline with the canned item list."""

    def __init__(self, orch: "MockInteractiveOrchestrator") -> None:
        super().__init__()
        self._orch = orch

    async def run(self, ctx: StageContext) -> None:
        n = self._orch._slide_count(ctx.state)
        for i in range(1, n + 1):
            await _sleep_jitter()
            self._orch._emit(
                "outline",
                iteration=i,
                max_iterations=n,
                slide_index=i,
                slide_total=n,
            )
        ctx.state.outline = _mock_outline(ctx.state.topic, n)


class _MockImagesStage(ImagesStage):
    """Mock images stage: one event per slide, populating each slide's
    ``hero`` slot with a small inline SVG placeholder.

    Why a real SVG payload (not ``{}`` or a fake "no image" marker):
      - /api/state's "images stage complete" check is
        ``if loaded.slide_images:`` — any falsy value skips the stage,
        which then mis-reports ``active_stage`` on refresh.
      - snapshots.py + /artifact/images/{n}/{slot} route dispatch on
        ``payload["type"]``. The only type that needs no filesystem
        path is ``"svg"`` (inline ``data``). Any other type — or a
        payload without ``type`` — lands in the unsupported-type 404
        branch and the UI shows broken-image icons.
      - A minimum SVG therefore satisfies both without touching disk
        and gives the review UI something visible to render.
    """

    def __init__(self, orch: "MockInteractiveOrchestrator") -> None:
        super().__init__()
        self._orch = orch

    async def run(self, ctx: StageContext) -> None:
        n = self._orch._slide_count(ctx.state)
        for i in range(1, n + 1):
            await _sleep_jitter()
            self._orch._emit(
                "images",
                iteration=i,
                max_iterations=n,
                slide_index=i,
                slide_total=n,
            )
            ctx.state.slide_images[i - 1] = {
                "hero": {"type": "svg", "data": _MOCK_SVG}
            }


class _MockSlidesStage(SlidesStage):
    """Mock slides stage: one event per slide, then populate
    state.slides with simple SlideDSL objects whose ``slots["html"]``
    the snapshot + renderer can consume."""

    def __init__(self, orch: "MockInteractiveOrchestrator") -> None:
        super().__init__()
        self._orch = orch

    async def run(self, ctx: StageContext) -> None:
        n = self._orch._slide_count(ctx.state)
        slides: List[SlideDSL] = []
        for i in range(1, n + 1):
            await _sleep_jitter()
            self._orch._emit(
                "slides",
                iteration=i,
                max_iterations=n,
                slide_index=i,
                slide_total=n,
            )
            outline_item = (
                ctx.state.outline[i - 1]
                if i - 1 < len(ctx.state.outline)
                else {}
            )
            slides.append(
                SlideDSL(
                    layout=str(outline_item.get("layout", "free_form")),
                    slots={"html": _mock_slide_html(outline_item, i - 1)},
                )
            )
        ctx.state.slides = slides


class MockInteractiveOrchestrator(InteractiveOrchestrator):
    """InteractiveOrchestrator that fires synthetic events and populates
    canned state instead of calling real LLMs.

    Constructor signature matches the parent so the review server can
    drop it in unchanged. Behaviour differs only in the Stage classes
    registered into the pipeline — the gate, broadcaster, snapshot, and
    state-persistence machinery all work the same way.
    """

    def __init__(
        self,
        config: AgentConfig,
        gate: ReviewGate,
        review_stages: Optional[Set[str]] = None,
        auto_approve: bool = False,
        broadcaster: Optional[Broadcaster] = None,
        registry: Optional[ToolRegistry] = None,
        renderer: Optional[SlideHTMLRenderer] = None,
        state_cache_path: Optional[Path] = None,
        load_state_on_start: bool = False,
    ) -> None:
        # Build a fresh registry with mock Stage subclasses. The
        # ``after`` anchors inherited from the parent classes preserve
        # the canonical order. RenderedStage is taken from the default
        # registry unchanged — the real renderer is pure Python and
        # fast enough on canned slides.
        from shuttleslide.agent.review.core_stages import RenderedStage

        mock_registry = StageRegistry()
        mock_registry.register(_MockThemeStage(self))
        mock_registry.register(_MockOutlineStage(self))
        mock_registry.register(_MockImagesStage(self))
        mock_registry.register(_MockSlidesStage(self))
        mock_registry.register(RenderedStage())

        super().__init__(
            config=config,
            gate=gate,
            review_stages=review_stages,
            auto_approve=auto_approve,
            broadcaster=broadcaster,
            registry=registry,
            renderer=renderer,
            stage_registry=mock_registry,
            state_cache_path=state_cache_path,
            load_state_on_start=load_state_on_start,
        )

    # ------------------------------------------------------------------
    # Helpers — called by the mock Stage classes above
    # ------------------------------------------------------------------

    def _emit(
        self,
        stage: str,
        iteration: int,
        max_iterations: int,
        slide_index: Optional[int] = None,
        slide_total: Optional[int] = None,
    ) -> None:
        """Fire one synthetic LLMResponseEvent through the configured
        ``on_llm_response`` callback. No-op when no callback is set
        (e.g. unit tests). Uses pipeline-stage names (``"slides"`` not
        ``"slide_builder"``) so the server's stage mapping sees the
        canonical name directly."""
        cb = self.config.on_llm_response
        if cb is None:
            return
        cb(
            LLMResponseEvent(
                stage=stage,
                iteration=iteration,
                max_iterations=max_iterations,
                slide_index=slide_index,
                slide_total=slide_total,
                content=f"[mock] {stage} event {iteration}/{max_iterations}",
                usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            )
        )

    def _slide_count(self, state: AgentState) -> int:
        """Resolve the slide count for this run. Defaults to 5 — enough
        that per-slide progress animation is visible, small enough that
        the pipeline finishes fast."""
        return state.target_count or 5

    # The rendered (export) stage is NOT mocked — the real
    # ``RenderedStage`` handles it. The renderer is pure Python (writes
    # .html files), no Playwright needed, so it runs fast on the mock
    # state. The stage is atomic so the UI shows the indeterminate
    # animation for the brief rendering window.
