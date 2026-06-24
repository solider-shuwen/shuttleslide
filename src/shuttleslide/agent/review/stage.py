"""Stage Protocol + StageContext + StageBase.

A Stage is the unit of pipeline work in the registry-driven orchestrator.
Stages are self-contained: they know their own name, where they sit
relative to other stages (via ``after`` / ``before`` anchors), and how
to produce / inspect / snapshot their output.

Why a Protocol + base class
---------------------------
The Protocol describes the contract; the base class gives concrete stages
a default implementation of the optional methods so they only override
what they need. External packages (e.g. ``shuttleslide-pro``) can either
inherit from ``StageBase`` or implement the Protocol from scratch.

StageContext
------------
Stages do NOT receive a back-reference to ``AgentOrchestrator``. Doing so
would create an import cycle (orchestrator imports stages, stages import
orchestrator). Instead the orchestrator constructs a ``StageContext``
dataclass carrying the deps a stage needs: state, llm, config, tool
registry, output_dir, and the optional web-image acquisition deps.
Stages that need more should propose a context field addition rather
than reach back into the orchestrator.

needs_review
------------
There is no separate ``needs_review`` flag. A stage is considered
reviewable iff ``build_snapshot(state)`` returns a non-None snapshot.
A stage that wants to run silently returns ``None`` from
``build_snapshot``; the orchestrator skips the gate pause and only
broadcasts a minimal progress snapshot for UI visibility.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional, Protocol, TYPE_CHECKING

from shuttleslide.agent.review.review_gate import StageSnapshot

if TYPE_CHECKING:
    # Avoid runtime cycle: state.py and config.py / llm.py / tools are
    # all imported by orchestrator.py which we don't want to pull in here.
    from shuttleslide.agent.config import AgentConfig
    from shuttleslide.agent.llm import LLMClient
    from shuttleslide.agent.state import AgentState
    from shuttleslide.agent.tools.registry import ToolRegistry
    from shuttleslide.agent.orchestrator import OrchestratorResult


ArtifactKind = Literal["json", "html", "svg", "image", "mixed", "audio"]


@dataclass
class StageContext:
    """Deps handed to every ``Stage.run`` invocation.

    Frozen in the sense that stages should not mutate these fields; the
    orchestrator builds one fresh context per stage call. ``state`` is
    the one mutable thing stages are expected to write into.

    ``web_search_provider`` / ``vlm_verifier`` / ``browser_manager`` are
    ``Any``-typed because importing their concrete types would pull in
    optional deps (Playwright, httpx clients) — stages that care about
    the shape should isinstance-check at run time.
    """

    state: "AgentState"
    llm: "LLMClient"
    config: "AgentConfig"
    tool_registry: "ToolRegistry"
    output_dir: Optional[Path]
    # Renderer is used only by RenderedStage; ``Any``-typed to avoid
    # importing SlideHTMLRenderer here (would drag jinja2 into the
    # import graph of every stage file).
    renderer: Any = None
    web_search_provider: Any = None
    vlm_verifier: Any = None
    browser_manager: Any = None


class Stage(Protocol):
    """Unit of pipeline work.

    Lifecycle (per pipeline run):
        1. ``is_cached(state)`` — orchestrator asks if the stage's output
           is already in state (e.g. loaded from disk). True = skip
           ``run`` entirely (the stage still fires ``build_snapshot`` +
           ``finalize`` so downstream review sees the loaded output).
        2. ``run(ctx)`` — does the work. Writes results into ``ctx.state``.
        3. ``build_snapshot(state)`` — produces a JSON-safe view for the
           review UI. Return ``None`` to mark this stage as silent (no
           gate pause, only a minimal progress event broadcast).
        4. ``finalize(state)`` — for the terminal stage only, produces
           the ``OrchestratorResult`` that ``_run_pipeline`` returns.
    """

    name: str
    artifact_kind: ArtifactKind
    after: Optional[str]
    before: Optional[str]
    terminal: bool

    async def run(self, ctx: StageContext) -> None: ...

    def is_cached(self, state: "AgentState") -> bool: ...

    def build_snapshot(
        self, state: "AgentState"
    ) -> Optional[StageSnapshot]: ...

    def finalize(
        self, state: "AgentState"
    ) -> "Optional[OrchestratorResult]": ...


class StageBase:
    """Default no-op implementations of the optional Stage methods.

    Concrete stages inherit from this and override ``run`` (always) plus
    whichever of ``is_cached`` / ``build_snapshot`` / ``finalize`` are
    relevant. This base is deliberately NOT a dataclass — stages that
    want config fields define their own ``__init__`` and call
    ``super().__init__()``.

    Class attributes (``name`` etc.) match the Protocol fields so
    subclasses can set them as class-level constants.
    """

    name: str = ""  # subclasses must override
    artifact_kind: ArtifactKind = "json"
    after: Optional[str] = None
    before: Optional[str] = None
    terminal: bool = False

    async def run(self, ctx: StageContext) -> None:
        raise NotImplementedError(
            f"{type(self).__name__}.run() not implemented"
        )

    def is_cached(self, state: "AgentState") -> bool:
        """Default: never report cache hit. Override when the stage
        writes a specific field that can be pre-populated from disk."""
        return False

    def build_snapshot(
        self, state: "AgentState"
    ) -> Optional[StageSnapshot]:
        """Default: return None — stage runs silently (no review UI pause)."""
        return None

    def finalize(
        self, state: "AgentState"
    ) -> "Optional[OrchestratorResult]":
        """Default: not a terminal stage. Only one stage per pipeline
        should override this to return an ``OrchestratorResult``."""
        return None
