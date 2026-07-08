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
    """A user-initiated edit landed. ``new_preview`` is what the UI re-renders.

    ``width`` / ``height`` are populated only for image uploads (the
    decoded pixel dims from Pillow); they let the slides-stage drag-drop
    flow size a newly inserted <img> with the correct aspect ratio
    without a second round-trip. ``None`` for text / JSON / SVG / HTML
    edits — clients treat absence as "no dim hint available".

    ``description`` is populated only for image uploads — the actual
    description string written to state (user-supplied or VLM-generated).
    Lets the client surface "what landed" without refetching the snapshot.

    ``no_op=True`` signals the editor returned ``new_value == old_value``,
    so no undo entry was pushed and no snapshot re-broadcast is warranted.
    Clients MUST still clear any pending chat indicator (the LLM-mode
    flow sets one on send) but MUST NOT flip the "edited" flag, append
    an "applied" chat entry, or treat this as a state change. ``diff``
    is always ``None`` for no_op acks.
    """

    type: Literal["edit_applied"] = "edit_applied"
    ref_id: str = ""
    target_path: Tuple[Any, ...] = ()
    new_preview: str = ""
    diff: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    description: Optional[str] = None
    no_op: bool = False


@dataclass
class EditRejectedMsg:
    """LLM/validation failure for a request_edit. State is unchanged.

    ``kind`` discriminates the failure mode:

      * ``"error"`` (default) — plain LLM/validation failure. The UI
        renders ``error`` as a chat-side rejection notice. This is the
        pre-existing behaviour and the default for backwards compat.
      * ``"out_of_scope"`` — the editor recognised the request as
        deck-level (adds/removes slides, changes theme, restructures
        outline) and returned structured guidance instead of mutating
        the per-slide target. The UI renders a guidance card with a
        "Go to {stage}" button; ``suggested_stage`` is the destination
        (typically "outline"), ``guidance`` is the user-visible text.
    """

    type: Literal["edit_rejected"] = "edit_rejected"
    ref_id: str = ""
    error: str = ""
    kind: str = "error"
    suggested_stage: Optional[str] = None
    guidance: Optional[str] = None


@dataclass
class EditCancelledMsg:
    """User cancelled an in-progress LLM edit. State is rolled back.

    Distinct from :class:`EditRejectedMsg` (server-side failure) —
    ``edit_cancelled`` means the user explicitly hit Cancel while the
    LLM call was pending. ``ref_id`` pairs back to the originating
    :class:`RequestEditMsg`.

    The server broadcasts this to every connected client (not just the
    requester) so that a client which disconnected and reconnected mid-
    edit still observes the cancellation and clears its local
    "edit in progress" UI state.

    State-rollback contract: the orchestrator restores the target's
    in-memory value to its pre-edit snapshot before this message is
    emitted. The on-disk ``agent_state.json`` is unaffected because
    ``_save_state`` had not yet been called. Caveat: in the very small
    window *after* ``_save_state`` succeeded but before the task fully
    unwound, the disk write is not rolled back — the undo stack already
    has the entry, so the client may issue an ``undo`` to revert. MVP
    accepts this edge case.
    """

    type: Literal["edit_cancelled"] = "edit_cancelled"
    ref_id: str = ""


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
class PipelineStagesMsg:
    """Declare the full ordered list of stages in the resolved pipeline.

    Fired once at pipeline startup (right after ``pipeline_state=running``)
    so clients can pre-create all stage tabs in the correct execution
    order before the first ``stage_complete`` lands. Without this,
    extension stages that don't register a renderer (``script``,
    ``motion_design``) would either appear out of order (when their
    ``stage_complete`` eventually arrives and pushes them into the
    client-side stage list) or fail to appear at all on browser refresh
    (the hydration path doesn't know their names).

    The list mirrors ``registry.resolve_order()`` names; clients treat
    it as the single source of truth for the sidebar, replacing the
    legacy ``STAGES + extraStages`` client-side discovery.
    """

    type: Literal["pipeline_stages"] = "pipeline_stages"
    stages: List[str] = field(default_factory=list)
    timestamp: float = 0.0


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


@dataclass
class HistorySnapshotMsg:
    """Full edit-history snapshot for the sidebar History panel.

    Pushed to all clients after every successful edit, undo, or revert
    so each client's History panel reflects the latest stack. Also
    unicasted in response to ``GetHistoryMsg`` for late-joining clients
    who need to populate their panel from scratch.

    ``entries`` is newest-first (idx=0 is the most recent edit). Each
    entry carries an ``idx`` (positional), ``action_label``,
    ``new_value_summary``, ``timestamp``, and ``target_path``.
    """

    type: Literal["history_snapshot"] = "history_snapshot"
    entries: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: float = 0.0


@dataclass
class StaleMarksUpdatedMsg:
    """Push the current ``state.stale_marks`` to all connected clients.

    Fired after every successful edit / undo / revert (and after a
    regenerate completes) so the UI's stale badges update in real time.

    ``marks`` is the verbatim ``state.stale_marks`` dict — JSON-safe via
    the ``StaleMark.to_dict`` shape. Keyed by downstream stage name
    (``"images"`` / ``"slides"`` / ``"rendered"``); each value is a list
    of mark dicts (``target_id``, ``source_stage``, ``source_id``,
    ``reason``, ``created_at``, optional ``context_snapshot``).

    The UI uses this both to add badges (when a mark appears) and to
    remove them (when a mark is cleared after regenerate / dismiss).
    Late-connecting clients replay the most recent snapshot from
    ``_early_messages`` alongside the history snapshot.
    """

    type: Literal["stale_marks_updated"] = "stale_marks_updated"
    marks: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass
class ItemRegeneratedMsg:
    """Ack for a successful ``regenerate_item`` request.

    Carries the new snapshot for the regenerated item plus the remaining
    stale marks after the (stage, target_id) mark was cleared. The UI
    removes the stale badge for this target and re-renders the snapshot.

    ``ref_id`` pairs this ack back to the originating ``RegenerateItemMsg``.
    ``remaining_marks`` is the full ``state.stale_marks`` dict (same shape
    as :class:`StaleMarksUpdatedMsg`) — server emits both messages in
    sequence so non-requesting clients also see the badge disappear.
    """

    type: Literal["item_regenerated"] = "item_regenerated"
    ref_id: str = ""
    stage: str = ""                              # "images" | "slides" | "rendered"
    target_id: str = ""                          # "all" | "slide:2" | "slide:2:slot:hero"
    snapshot: Dict[str, Any] = field(default_factory=dict)
    remaining_marks: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)


@dataclass
class ChatHistoryMsg:
    """Per-target chat history push.

    Fired by the orchestrator after every successful LLM-mode edit
    (right after the assistant reply lands in SessionStore) so the
    chat panel surfaces the LLM's natural-language reply without
    needing a target-switch refresh. Also unicast in response to
    ``chat_history`` client requests for late-joining / target-focus
    hydration.

    ``messages`` is the ``[{role, body}]`` wire format the frontend
    expects (app.js renderChatHistory reads ``entry.body``). Translated
    from SessionStore's ``{role, content}`` shape at the call site.
    ``ref_id`` is empty for orchestrator-initiated broadcasts (the
    push goes to every client); the ``chat_history`` request handler
    echoes the requester's ``ref_id`` so the client can resolve its
    pending affordance.
    """

    type: Literal["chat_history"] = "chat_history"
    ref_id: str = ""
    target_path: Tuple[Any, ...] = ()
    messages: List[Dict[str, str]] = field(default_factory=list)
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
class CancelEditMsg:
    """Cancel an in-progress LLM edit.

    ``ref_id`` MUST match the originating :class:`RequestEditMsg`'s ref_id
    — the server ignores mismatches silently to avoid race conditions
    (e.g. the edit already finished between the user clicking Cancel
    and the message arriving). The cancel can be issued by any client,
    not just the originator, because the review UI uses a global
    "edit in progress" lock — any connected reviewer seeing a stuck
    edit should be able to bail out.
    """

    type: Literal["cancel_edit"] = "cancel_edit"
    ref_id: str = ""


@dataclass
class UploadImageMsg:
    """Small image upload via WebSocket. Larger uploads use HTTP POST /upload.

    data_b64 is base64-encoded image bytes WITHOUT the data: URI prefix
    (kept short to fit comfortably under WS frame limits).

    description is an optional user-typed caption for the image. When
    blank, ImageUploader falls back to VLM-generated description (see
    ``enable_vlm_description`` in AgentConfig). Empty string is the
    default so legacy clients that don't send the field keep working.
    """

    type: Literal["upload_image"] = "upload_image"
    ref_id: str = ""
    target_path: Tuple[Any, ...] = ()
    mime: str = ""
    data_b64: str = ""
    description: str = ""


@dataclass
class UndoMsg:
    """Revert the most recent edit on a target_path."""

    type: Literal["undo"] = "undo"
    target_path: Tuple[Any, ...] = ()


@dataclass
class GetHistoryMsg:
    """Request the full edit history for the sidebar History panel (#4).

    Server responds with a single ``HistorySnapshotMsg`` unicasted to
    the requester. Subsequent edits will push fresh
    ``HistorySnapshotMsg`` broadcasts to all clients automatically.
    """

    type: Literal["get_history"] = "get_history"
    ref_id: str = ""


@dataclass
class RevertToMsg:
    """Revert history to a specific entry.

    ``entry_idx`` is the index into ``UndoStack.entries()`` (newest =
    0). The server applies that entry's ``old_value`` and drops every
    edit newer than it (including itself). Behaviour is destructive —
    no redo stack is maintained.
    """

    type: Literal["revert_to"] = "revert_to"
    ref_id: str = ""
    entry_idx: int = -1


@dataclass
class RegenerateItemMsg:
    """Request per-item regeneration of a stale downstream artifact.

    Pairs with a stale mark on ``(stage, target_id)``. The server finds
    the matching mark, snapshots the current value (for undo), calls
    the stage's ``regenerate_item``, and replies with
    :class:`ItemRegeneratedMsg` on success.

    ``mode`` controls the prompt strategy:

      - ``"incremental"`` (default): preserve user edits. The LLM sees
        the current HTML and the upstream before/after diff, and is
        asked to apply only the minimum changes needed to reflect the
        new upstream. Anti-patterns in the prompt discourage wholesale
        rewrites.
      - ``"fresh"``: regenerate from scratch. Discards any user edits
        on this item; the UI confirms before sending. Use only when
        the user actively wants a clean regeneration.

    ``stage`` is one of ``"images"`` / ``"slides"`` / ``"rendered"``;
    theme and outline have no per-item regenerate (those are sources,
    not regeneratable targets). ``target_id`` follows the same scheme
    as :class:`StaleMark` (``"all"`` / ``"slide:N"`` / ``"slide:N:slot:ID"``).
    """

    type: Literal["regenerate_item"] = "regenerate_item"
    ref_id: str = ""
    stage: str = ""                                              # "images" | "slides" | "rendered"
    target_id: str = ""                                          # "all" | "slide:2" | "slide:2:slot:hero"
    mode: Literal["incremental", "fresh"] = "incremental"


@dataclass
class DismissStaleMsg:
    """Dismiss a stale mark without regenerating.

    The user is okay with the current downstream value and does not
    want to regenerate. The server removes the mark from
    ``state.stale_marks`` and broadcasts a fresh
    :class:`StaleMarksUpdatedMsg` so every client's badge clears.

    ``target_id="all"`` clears every mark on the stage (useful when
    the user has reviewed all stale items at once). Otherwise a single
    ``(stage, target_id)`` mark is removed.

    Dismissal is explicit and final — the mark does not come back
    unless a new upstream edit re-triggers it.
    """

    type: Literal["dismiss_stale"] = "dismiss_stale"
    ref_id: str = ""
    stage: str = ""
    target_id: str = ""                                          # single id; or "all" to clear stage


@dataclass
class AddSlideMsg:
    """Insert a new slide into the outline at ``index``.

    Two modes:

      - ``mode="llm"``: ``payload={"intent": "<user prose>"}``. The
        server drafts a full outline entry via the LLM (feeding
        neighbour entries as context so the new slide fits the
        narrative), inserts it, then kicks off background generation
        of images / slide HTML / rendered output for the new index.
      - ``mode="manual"``: ``payload={"entry": <full outline dict>}``.
        The user has already filled the structured form; the server
        validates the entry's key set and inserts it verbatim, then
        runs the same background generation chain.

    ``index=-1`` (or any value ``>= len(outline)``) appends. ``0..N``
    inserts before the existing slide at that position. The server
    re-indexes ``state.slides`` / ``state.slide_images`` /
    ``state.html_paths`` and bumps stale-mark ``slide:N`` ids for
    downstream slides via :func:`outline_mutation.insert_slide`.

    The orchestrator replies with ``EditAppliedMsg`` (``target_path=
    ["outline"]``) on success or ``EditRejectedMsg`` on validation
    failure. Background generation progress streams via the existing
    ``StageProgressMsg`` / ``ItemRegeneratedMsg`` path.
    """

    type: Literal["add_slide"] = "add_slide"
    ref_id: str = ""
    index: int = -1
    mode: Literal["llm", "manual"] = "manual"
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeleteSlideMsg:
    """Remove slide at ``index`` from outline and all downstream arrays.

    Symmetric to :class:`AddSlideMsg`: drops ``outline[i]`` /
    ``slides[i]`` / ``html_paths[i]`` / ``slide_images[i]`` and shifts
    every later index down by one (stale marks included). No filesystem
    cleanup — ``state.html_paths`` is the source of truth for the
    rendered PPTX.

    Ack is ``EditAppliedMsg`` (``target_path=["outline"]``). The
    orchestrator broadcasts fresh ``stage_complete`` messages for
    outline + images + slides + rendered so every connected client
    sees the list shrink.
    """

    type: Literal["delete_slide"] = "delete_slide"
    ref_id: str = ""
    index: int = -1


@dataclass
class RebalanceOutlineMsg:
    """Ask the LLM to rewrite the entire outline for narrative flow.

    Optional ``user_hint`` carries a free-form instruction
    (e.g. "make the intro more punchy", "tighten the middle section").
    When empty, the server uses a default "rebalance narrative flow"
    prompt. The LLM sees the current outline and is asked to preserve
    manual edits where possible; per-entry keys MUST stay the same
    (server validates).

    After the rewrite, every downstream stage is marked stale
    (``images`` / ``slides`` / ``rendered`` for each slide index) but
    NOT auto-regenerated — the user triggers per-slide Regenerate
    themselves. This avoids N×3 LLM calls firing at once.

    Ack is ``EditAppliedMsg`` (``target_path=["outline"]``). Runs as a
    cancellable background task (same path as ``add_slide`` LLM mode).
    """

    type: Literal["rebalance_outline"] = "rebalance_outline"
    ref_id: str = ""
    user_hint: str = ""


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

ServerMessage = Union[
    StageStartedMsg,
    StageCompleteMsg,
    EditAppliedMsg,
    EditRejectedMsg,
    EditCancelledMsg,
    ErrorMsg,
    PipelineDoneMsg,
    PipelineStateMsg,
    PipelineStagesMsg,
    LogEntryMsg,
    StageProgressMsg,
    HistorySnapshotMsg,
    StaleMarksUpdatedMsg,
    ItemRegeneratedMsg,
    ChatHistoryMsg,
]

ClientMessage = Union[
    ApproveStageMsg,
    RequestEditMsg,
    CancelEditMsg,
    UploadImageMsg,
    UndoMsg,
    GetHistoryMsg,
    RevertToMsg,
    RegenerateItemMsg,
    DismissStaleMsg,
    AddSlideMsg,
    DeleteSlideMsg,
    RebalanceOutlineMsg,
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
