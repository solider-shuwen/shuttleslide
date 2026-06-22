"""Interactive stage review for the agent pipeline.

Public surface (kept intentionally narrow until the FastAPI server lands
in PR2). Imports below are lazy-friendly: pulling this package never
requires fastapi / uvicorn — only the standard library + the existing
shuttleslide.agent deps.

Stages with review points (see orchestrator._run_stage_*):
    theme, outline, images, slides, rendered

PR1 scope:
    - ReviewGate (async pause/release primitive)
    - StageSnapshot + build_snapshot
    - InteractiveOrchestrator (subclass that fires the gate)
    - auto_approve=True test mode (no real server required)

PR2 will add the FastAPI ReviewServer; PR3 the per-element editors.
"""

from __future__ import annotations

from shuttleslide.agent.review.interactive_orchestrator import (
    InteractiveOrchestrator,
    ReviewCancelledError,
)
from shuttleslide.agent.review.review_gate import (
    STAGE_NAMES,
    ReviewAction,
    ReviewGate,
    StageName,
    StageSnapshot,
)
from shuttleslide.agent.review.snapshots import EditTarget, UndoStack, build_snapshot

__all__ = [
    "STAGE_NAMES",
    "EditTarget",
    "InteractiveOrchestrator",
    "ReviewAction",
    "ReviewCancelledError",
    "ReviewGate",
    "StageName",
    "StageSnapshot",
    "UndoStack",
    "build_snapshot",
]
