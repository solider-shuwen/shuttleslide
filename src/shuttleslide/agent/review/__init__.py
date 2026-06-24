"""Interactive stage review for the agent pipeline.

Public surface (kept intentionally narrow). Imports below never require
fastapi / uvicorn — only the standard library + the existing
shuttleslide.agent deps. ``ReviewServer`` is therefore NOT re-exported
here; import it explicitly when you need the web stack::

    from shuttleslide.agent.review.server import ReviewServer

Core stages (registered by default in ``default_registry()``):
    theme, outline, images, slides, rendered

Extension stages (pro / external packages) register via the
``shuttleslide.review.stages`` entry-point group and are picked up by
``full_registry()``. The orchestrator uses ``full_registry()`` so pro
stages run automatically when their package is installed.

PR1 scope:
    - ReviewGate (async pause/release primitive)
    - StageSnapshot + build_snapshot
    - InteractiveOrchestrator (subclass that fires the gate)
    - auto_approve=True test mode (no real server required)

PR2 adds:
    - Broadcaster Protocol (the seam ReviewServer implements)
    - ``InteractiveOrchestrator(broadcaster=...)`` parameter

The concrete ``ReviewServer`` lives in ``server.py`` and is opt-in.
PR3 will add the per-element editors.
"""

from __future__ import annotations

from shuttleslide.agent.review.broadcaster import Broadcaster
from shuttleslide.agent.review.interactive_orchestrator import (
    InteractiveOrchestrator,
    ReviewCancelledError,
)
from shuttleslide.agent.review.registry import (
    StageRegistry,
    default_registry,
    full_registry,
    load_extensions,
)
from shuttleslide.agent.review.review_gate import (
    STAGE_NAMES,
    CoreStageName,
    ReviewAction,
    ReviewGate,
    StageName,
    StageSnapshot,
)
from shuttleslide.agent.review.snapshots import EditTarget, UndoStack, build_snapshot
from shuttleslide.agent.review.stage import Stage, StageBase, StageContext

__all__ = [
    # Core registry
    "StageRegistry",
    "default_registry",
    "full_registry",
    "load_extensions",
    # Stage protocol
    "Stage",
    "StageBase",
    "StageContext",
    # Legacy stage names (still tuple-of-core-five)
    "STAGE_NAMES",
    "CoreStageName",
    "StageName",
    # Review primitives
    "Broadcaster",
    "EditTarget",
    "InteractiveOrchestrator",
    "ReviewAction",
    "ReviewCancelledError",
    "ReviewGate",
    "StageSnapshot",
    "UndoStack",
    "build_snapshot",
]
