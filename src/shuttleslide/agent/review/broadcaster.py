"""Broadcaster Protocol — the seam between the orchestrator and the review UI.

This module deliberately imports *only* from ``review_gate`` (which itself
has zero third-party deps). That keeps ``interactive_orchestrator.py`` —
and therefore the entire core agent pipeline — free of any fastapi/uvicorn
dependency. PR2's ``ReviewServer`` is the concrete implementation; tests
and other consumers can substitute any object that satisfies this Protocol
without dragging the web stack into the import graph.

Why a Protocol instead of an ABC
--------------------------------
We want duck typing: tests can pass in a ``MagicMock`` or a tiny stub
class without inheriting from anything. ``@runtime_checkable`` is added
so ``isinstance(x, Broadcaster)`` works when needed (mostly for
defensive asserts in tests).
"""

from __future__ import annotations

from typing import List, Optional, Protocol, runtime_checkable

from shuttleslide.agent.review.review_gate import StageSnapshot


@runtime_checkable
class Broadcaster(Protocol):
    """Sink for orchestrator events. All methods are synchronous and
    fire-and-forget — implementations must not block the orchestrator loop.

    Implementations are responsible for any cross-thread dispatch
    (typically ``loop.call_soon_threadsafe``) needed to deliver the
    payload to the actual UI clients.
    """

    def emit_stage_complete(self, snapshot: StageSnapshot) -> None:
        """A stage finished and its snapshot is ready for review."""
        ...

    def emit_stage_progress(
        self,
        stage: str,
        current: Optional[int],
        total: Optional[int],
        elapsed_seconds: Optional[float],
        eta_seconds: Optional[float],
        label: str,
    ) -> None:
        """Sub-stage progress for the running stage. Fire-and-forget; called
        once per LLM response from inside the orchestrator task.

        ``current`` / ``total`` carry the per-stage unit count (e.g. slide
        index and total) when the stage has a natural denominator. ``None``
        for both signals an atomic stage — the UI shows an elapsed timer +
        indeterminate animation instead of a fill bar. ``eta_seconds`` is a
        rolling-average estimate (``elapsed / current * remaining``); ``None``
        means unknown (e.g. before the first unit completes).
        """
        ...

    def emit_pipeline_done(self, html_paths: List[str]) -> None:
        """All stages complete; ``html_paths`` are on disk and openable."""
        ...

    def emit_error(self, message: str, fatal: bool = False) -> None:
        """An error occurred. ``fatal=True`` means the pipeline is dead."""
        ...

    def emit_pipeline_state(self, state: str, error: Optional[str] = None) -> None:
        """Pipeline lifecycle state changed.

        ``state`` is one of: idle | starting | running | done | failed.
        Drives the UI's config-form ↔ pipeline-screen switching. ``error``
        is non-empty only when ``state == "failed"``.
        """
        ...
