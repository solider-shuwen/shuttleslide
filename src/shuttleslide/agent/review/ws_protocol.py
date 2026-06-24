"""WebSocket message schemas for the review UI.

PR1 keeps this module dependency-free (just dataclasses) so the gate and
snapshot code can be tested without fastapi installed. PR2's server
serialises these with ``json.dumps(msg_to_dict(...))``.

Wire format
-----------
Every message is a JSON object with a ``type`` field. Client-to-server
messages carry a ``ref_id`` (any client-generated string) so the server
can pair the eventual response (``edit_applied`` / ``edit_rejected``)
back to the originating request — this is necessary because edits are
asynchronous (LLM call) and other server-to-client messages
(``stage_started`` etc.) may interleave.

Versioning
----------
``PROTOCOL_VERSION`` is semver-ish ("1.0.0"). Client and server both
emit it on connect; mismatches produce a clear error rather than silent
confusion. We will bump on breaking changes only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Literal, Optional, Tuple, Union


PROTOCOL_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Server -> Client
# ---------------------------------------------------------------------------


@dataclass
class StageStartedMsg:
    """Fired when the orchestrator enters a new stage."""

    type: Literal["stage_started"] = "stage_started"
    stage: str = ""
    timestamp: float = 0.0


@dataclass
class StageCompleteMsg:
    """Fired when a stage's snapshot is ready for review.

    ``snapshot`` is the StageSnapshot dict (already JSON-safe). The UI
    uses ``editable_targets`` to render per-element edit buttons.
    """

    type: Literal["stage_complete"] = "stage_complete"
    stage: str = ""
    snapshot: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class EditAppliedMsg:
    """A user-initiated edit landed. ``new_preview`` is what the UI re-renders."""

    type: Literal["edit_applied"] = "edit_applied"
    ref_id: str = ""
    target_path: Tuple[Any, ...] = ()
    new_preview: str = ""
    diff: Optional[str] = None


@dataclass
class EditRejectedMsg:
    """LLM/validation failure for a request_edit. State is unchanged."""

    type: Literal["edit_rejected"] = "edit_rejected"
    ref_id: str = ""
    error: str = ""


@dataclass
class ErrorMsg:
    """Generic error. ``fatal=True`` means the pipeline is dead."""

    type: Literal["error"] = "error"
    message: str = ""
    fatal: bool = False


@dataclass
class PipelineDoneMsg:
    """All stages complete. ``html_paths`` is ready to open in a browser."""

    type: Literal["pipeline_done"] = "pipeline_done"
    html_paths: List[str] = field(default_factory=list)


@dataclass
class PipelineStateMsg:
    """Pipeline lifecycle state change.

    Drives the UI's high-level screen switching (config form ↔ pipeline
    review). ``state`` is one of:
      - ``idle``     — no run active (initial / after reset)
      - ``starting`` — config received, orchestrator being constructed
      - ``running``  — orchestrator task is executing
      - ``done``     — ``emit_pipeline_done`` already fired; success
      - ``failed``   — fatal error; ``error`` carries the message

    ``error`` is non-empty only when ``state == "failed"``.
    """

    type: Literal["pipeline_state"] = "pipeline_state"
    state: str = "idle"
    error: Optional[str] = None


@dataclass
class LogEntryMsg:
    """A single log entry surfaced to the review UI's log drawer.

    Fired from the ``on_llm_response`` callback (one per LLM call) and
    potentially from future instrumentation points. Distinct from
    :class:`ErrorMsg` (which is fatal/error-level) and
    :class:`StageCompleteMsg` (which fires once per stage boundary).
    """

    type: Literal["log_entry"] = "log_entry"
    scope: str = ""        # e.g. "llm:slide_builder:3/10", "tool:bing_web"
    message: str = ""      # formatted summary — one line
    level: str = "info"    # "info" | "warn" | "error" | "ok"
    timestamp: float = 0.0


@dataclass
class StageProgressMsg:
    """Live progress for the currently-running stage.

    Fires from ``_on_llm_response`` (one per LLM call) so the UI can render
    a real progress bar instead of inferring from log lines. For countable
    stages (outline / images / slides), ``current`` / ``total`` / ``percent``
    / ``eta_seconds`` are populated from the LLM event's ``slide_index`` and
    ``slide_total`` fields. For atomic stages (theme / rendered) those fields
    are ``None`` and the UI shows an elapsed timer + indeterminate animation.

    Distinct from :class:`StageCompleteMsg` (which fires once at the stage
    boundary) and :class:`LogEntryMsg` (which carries a textual log line for
    the drawer; this message carries structured progress for the strip).
    """

    type: Literal["stage_progress"] = "stage_progress"
    stage: str = ""
    current: Optional[int] = None        # e.g. 4 slides done
    total: Optional[int] = None          # e.g. 12 slides total
    percent: Optional[float] = None      # 0-100; None = indeterminate
    elapsed_seconds: Optional[float] = None  # since first event for this stage
    eta_seconds: Optional[float] = None  # rolling-average estimate; None = unknown
    label: str = ""                      # "Slide 4 / 12" — display-friendly
    timestamp: float = 0.0


# ---------------------------------------------------------------------------
# Client -> Server
# ---------------------------------------------------------------------------


@dataclass
class ApproveStageMsg:
    """Reviewer approves the current snapshot; orchestrator proceeds."""

    type: Literal["approve_stage"] = "approve_stage"
    stage: str = ""


@dataclass
class RequestEditMsg:
    """Edit request. ``mode='llm'`` -> payload has user_message; ``mode='direct'``
    -> payload has new_value. ``target_path`` matches an EditTarget.path
    from the most recent StageCompleteMsg.
    """

    type: Literal["request_edit"] = "request_edit"
    ref_id: str = ""
    target_path: Tuple[Any, ...] = ()
    mode: Literal["llm", "direct"] = "llm"
    payload: Dict[str, str] = field(default_factory=dict)


@dataclass
class UploadImageMsg:
    """Small image upload via WebSocket. Larger uploads use HTTP POST /upload.

    data_b64 is base64-encoded image bytes WITHOUT the data: URI prefix
    (kept short to fit comfortably under WS frame limits).
    """

    type: Literal["upload_image"] = "upload_image"
    ref_id: str = ""
    target_path: Tuple[Any, ...] = ()
    mime: str = ""
    data_b64: str = ""


@dataclass
class UndoMsg:
    """Revert the most recent edit on a target_path."""

    type: Literal["undo"] = "undo"
    target_path: Tuple[Any, ...] = ()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

ServerMessage = Union[
    StageStartedMsg,
    StageCompleteMsg,
    EditAppliedMsg,
    EditRejectedMsg,
    ErrorMsg,
    PipelineDoneMsg,
    PipelineStateMsg,
    LogEntryMsg,
    StageProgressMsg,
]

ClientMessage = Union[
    ApproveStageMsg,
    RequestEditMsg,
    UploadImageMsg,
    UndoMsg,
]


def msg_to_dict(msg: Any) -> Dict[str, Any]:
    """Convert a message dataclass to a JSON-safe dict.

    Tuples are converted to lists (JSON has no tuple type). None values
    are kept so optional fields like ``diff`` round-trip correctly.
    """
    d = asdict(msg)
    # asdict recursively converts nested dataclasses but leaves tuples as
    # tuples. JSON needs lists.
    return _tuples_to_lists(d)


def _tuples_to_lists(obj: Any) -> Any:
    if isinstance(obj, tuple):
        return [_tuples_to_lists(item) for item in obj]
    if isinstance(obj, list):
        return [_tuples_to_lists(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _tuples_to_lists(v) for k, v in obj.items()}
    return obj
