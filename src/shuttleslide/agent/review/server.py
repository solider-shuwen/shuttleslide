"""FastAPI-based review server — the concrete ``Broadcaster`` implementation.

Lifecycle
---------
PR4's CLI will call ``start_in_thread()`` from the main thread, then run
the orchestrator on its own loop via ``asyncio.run``. The server thread
runs ``uvicorn.Server.serve()`` on a separate asyncio loop. Both loops
share the same ``ReviewGate`` instance, which is the only state they use
to coordinate.

The cross-loop hand-off is encapsulated in ``_enqueue_broadcast``: when
the orchestrator loop calls ``emit_stage_complete`` etc., the payload is
appended to an early-message buffer (so clients connecting later still
see it) AND scheduled onto the server loop via
``loop.call_soon_threadsafe(self._broadcast_now, payload)``.

Test seam
--------
``serve()``/``stop()`` are public async methods so tests can drive the
server in-process (with ``httpx.AsyncClient`` + ASGI transport + ``httpx-ws``)
without spawning a real thread or binding a real socket.

Routes (PR2)
------------
- ``GET /``                       → ``static/index.html``
- ``WS  /ws``                     → bidirectional review channel
- ``GET /artifact/slides/{idx}``  → ``text/html`` slide fragment
- ``GET /artifact/images/{slide}/{slot}`` → image bytes (svg inline or file)

``/upload`` and edit endpoints arrive in PR3.

Import contract
---------------
Importing this module requires fastapi/uvicorn installed. The
``review/__init__.py`` does NOT re-export ``ReviewServer`` so the core
``InteractiveOrchestrator`` path stays fastapi-free; users explicitly
opt in with ``from shuttleslide.agent.review.server import ReviewServer``.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from fastapi import FastAPI, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from shuttleslide.agent.dsl_to_html import SlideHTMLRenderer
from shuttleslide.agent.geometry import emu_to_px
from shuttleslide.agent.review.review_gate import (
    ReviewGate,
    StageName,
    StageSnapshot,
)
from shuttleslide.agent.review.snapshots import build_snapshot
from shuttleslide.agent.review.state_persistence import load_state
from shuttleslide.agent.review.ws_protocol import (
    ChatHistoryMsg,
    EditAppliedMsg,
    EditCancelledMsg,
    EditRejectedMsg,
    ErrorMsg,
    HistorySnapshotMsg,
    ItemRegeneratedMsg,
    LogEntryMsg,
    PipelineDoneMsg,
    PipelineStateMsg,
    PipelineStagesMsg,
    StageCompleteMsg,
    StageProgressMsg,
    StaleMarksUpdatedMsg,
    msg_to_dict,
)
from shuttleslide.html_to_pptx.schema import SlideDSL, ThemeDef


# Standalone HTML wrapper for slide fragments is produced by the production
# renderer (SlideHTMLRenderer below) — review preview and saved files share
# the same code path so they render identically. See _wrap_slide_html.


# Sentinel for malformed client messages — we want to reply with a helpful
# error rather than drop the connection (PR2's contract is forgiving).
class _MalformedMessage(Exception):
    pass


# Client message types that mutate review state and therefore must wait
# while an LLM edit task is running. Read-only types (``get_history``,
# ``chat_history``) are excluded so the UI can still hydrate during a
# long edit. ``cancel_edit`` is handled before this check.
_EDIT_BLOCKING_TYPES: frozenset = frozenset({
    "request_edit",
    "upload_image",
    "undo",
    "revert_to",
    "unrevert",
    "regenerate_item",
    "delete_history_entry",
    "approve_stage",
    "dismiss_stale",
})


def _normalize_path_elem(x: Any) -> Any:
    """Normalize one path element for cross-JSON comparison.

    JSON object keys are forced to string by the spec, so a Python
    ``state_view = {"slide_images": {2: ...}}`` round-trips through
    JSON as ``{"2": ...}`` — the int key becomes a string. When the
    client echoes back ``target_path: ["slide", "2", "slot", ...]``
    and the server compares against the snapshot's
    ``target.path == ("slide", 2, "slot", ...)``, the ``"2"`` vs ``2``
    mismatch breaks the lookup.

    This helper coerces both sides to a canonical form: int if the
    value parses as int (so ``2`` and ``"2"`` both → ``2``), else the
    string form. Non-numeric path segments (slot ids, "slide", etc.)
    are unaffected.
    """
    if isinstance(x, bool):
        # bool is a subclass of int in Python; treat it as a string so
        # ``True``/``False`` (unlikely in paths but defensive) don't
        # collide with ``1``/``0``.
        return str(x)
    if isinstance(x, int):
        return x
    if isinstance(x, str):
        try:
            return int(x)
        except ValueError:
            return x
    return str(x)


class ReviewServer:
    """Concrete ``Broadcaster`` implementation backed by FastAPI + uvicorn.

    Construct in any thread; call ``start_in_thread()`` to launch uvicorn
    on a daemon thread.

    Two operating modes:

    * **External orchestrator (legacy / tests):** caller passes ``gate``
      and ``orchestrator_loop``; pipeline is started externally and the
      server is purely the broadcaster + artifact server. Used by
      ``tmp/test_agent_generate.py`` and existing tests.

    * **Web-client mode:** ``gate=None`` and ``orchestrator_loop=None``.
      Server starts idle, showing a config page. ``POST /api/start``
      constructs an :class:`InteractiveOrchestrator` internally and
      runs the pipeline as a task on the server loop. Supports rerun
      via ``POST /api/reset``.
    """

    EARLY_MESSAGE_BUFFER = 50

    def __init__(
        self,
        gate: Optional[ReviewGate] = None,
        orchestrator_loop: Optional[asyncio.AbstractEventLoop] = None,
        host: str = "127.0.0.1",
        port: int = 8765,
        static_dir: Optional[Path] = None,
        output_dir: Optional[Path] = None,
        env_defaults: Optional[Dict[str, str]] = None,
        cli_overrides: Optional[Dict[str, str]] = None,
        mock_mode: bool = False,
        canvas_mode: bool = False,
        extra_static_dirs: Optional[Dict[str, Path]] = None,
        ui_extension_scripts: Optional[List[str]] = None,
    ) -> None:
        self.gate = gate
        self.orchestrator_loop = orchestrator_loop
        self.host = host
        self.port = port
        # Canvas mode (set by ``slidecraft review --canvas``). When True,
        # the config-screen JS renders an aspect-ratio picker and the
        # form submits a ``canvas_aspect_ratio`` field. Server-side, the
        # ratio is converted to EMU and threaded through AgentConfig;
        # pro's canvas house_rules provider (registered via the
        # shuttleslide.review.house_rules entry-point group) swaps in
        # canvas-specific prompts for the run.
        self.canvas_mode: bool = bool(canvas_mode)

        # Credential defaults / locks — populated by cli.py from .env
        # (env_defaults) and CLI flags (cli_overrides). effective_defaults
        # is what we actually inject into AgentConfig on POST /api/start;
        # locked_fields is the union (any source locks the field).
        # UI fetches both via GET /api/defaults to pre-fill + readonly-ify.
        self.env_defaults: Dict[str, str] = dict(env_defaults or {})
        self.cli_overrides: Dict[str, str] = dict(cli_overrides or {})
        self.effective_defaults: Dict[str, str] = {
            **self.env_defaults,
            **self.cli_overrides,  # CLI wins on conflict
        }
        self.locked_fields: Set[str] = (
            set(self.env_defaults) | set(self.cli_overrides)
        )
        # Mock mode (set by ``slidecraft review --mock``). When True, the
        # /api/start handler constructs MockInteractiveOrchestrator instead
        # of the real one — synthetic events + canned state, no LLM calls.
        self.mock_mode: bool = bool(mock_mode)

        # Resolve the static dir relative to this file so the server
        # works regardless of CWD.
        if static_dir is None:
            static_dir = Path(__file__).parent / "static"
        self.static_dir = Path(static_dir)

        # image_acquirer writes image_file payloads with ``path`` *relative
        # to* AgentConfig.output_dir (see state.py:96 — e.g. "images/slide_3_hero.jpg").
        # The artifact route needs the same base to resolve them, otherwise
        # Path(path_str).exists() looks at the server process's CWD and 404s
        # on real LLM output. Abs paths (used by existing stub tests) pass
        # through unchanged — see _resolve_image_path below.
        self.output_dir = Path(output_dir) if output_dir is not None else None

        # Server state — mutated only from the server loop.
        self._connections: Set[WebSocket] = set()
        self._early_messages: Deque[Dict[str, Any]] = deque(maxlen=self.EARLY_MESSAGE_BUFFER)
        # Cache of the most recent snapshot per stage so artifact routes
        # can resolve indices without reaching into AgentState. Snapshots
        # are JSON-safe (PR1 contract), so this is safe to ship verbatim.
        # Key type is ``str`` (not the legacy ``StageName`` Literal) so
        # pro extension stages picked up via the registry are valid keys.
        self._last_snapshots: Dict[str, StageSnapshot] = {}

        # Lifecycle handles — populated by start_in_thread / serve.
        self._server_loop: Optional[asyncio.AbstractEventLoop] = None
        self._server: Any = None  # uvicorn.Server
        self._thread: Optional[threading.Thread] = None
        self._started_event = threading.Event()

        # UI extension static mounts + script URLs. Each entry in
        # ``extra_static_dirs`` is mounted at ``/ext/<key>/`` so external
        # packages can ship JS that the review UI loads dynamically via
        # ``/api/ui-extensions``. ``ui_extension_scripts`` is the URL
        # list advertised by that endpoint — populated from the
        # ``shuttleslide.review.ui_extensions`` entry-point group at
        # construction (each entry point value is ``"<package>:<rel/path.js>"``,
        # the package's root directory is auto-mounted).
        # Core never knows what *kind* of UI an extension renders; it only
        # provides the loader hook so pro can register stage renderers
        # via ``window.SlidecraftReview.registerStageRenderer``.
        # Pass ``ui_extension_scripts=[...]`` explicitly to bypass
        # entry-point discovery (used by tests; production relies on the
        # auto-discovery path).
        self.extra_static_dirs: Dict[str, Path] = {}
        self.ui_extension_scripts: List[str] = (
            list(ui_extension_scripts) if ui_extension_scripts is not None else []
        )
        for key, dir_path in (extra_static_dirs or {}).items():
            self.extra_static_dirs[str(key)] = Path(dir_path)
        if ui_extension_scripts is None:
            self._discover_ui_extensions()
        # Cached full registry (core + entry-point stages). Built lazily
        # by _get_full_registry on first /api/state call so the import
        # cost is paid only when refresh-hydration actually needs it.
        # ``False`` is the "tried and failed" sentinel so we don't
        # retry the (potentially slow) entry-point load on every call.
        self._full_registry_cache: Any = None

        # Production slide renderer — reused so preview HTML matches saved
        # files byte-for-byte (.ppt-slide container, theme background,
        # canvas dimensions, font stack). inline_cdn_assets=False keeps
        # live CDN URLs so the preview works without first populating
        # ~/.shuttleslide/cdn/ during a test or first-run scenario.
        self._renderer = SlideHTMLRenderer(inline_cdn_assets=False)

        # Web-client mode state. When gate is None, the orchestrator is
        # constructed per-run by POST /api/start and executed as a task
        # on the server loop. ``_pipeline_state`` drives the UI's
        # config-screen ↔ pipeline-screen switching.
        self._pipeline_state: str = "idle"
        self._pipeline_task: Optional[asyncio.Task] = None
        self._pipeline_error: Optional[str] = None
        self._orchestrator: Any = None  # InteractiveOrchestrator, set on start
        # Active LLM edit tracking. When a request_edit (mode=llm) lands
        # we spin its handling off as a background task so the WS read
        # loop stays free to receive ``cancel_edit``. Other mutating
        # message types are rejected while a task is running — see
        # ``_EDIT_BLOCKING_TYPES``. ``_active_edit_ref_id`` pairs with
        # the originating request so cancel matching is unambiguous.
        self._active_edit_task: Optional[asyncio.Task] = None
        self._active_edit_ref_id: Optional[str] = None
        self._run_output_dir: Optional[Path] = None  # per-run subdir under output_dir
        # Canvas dimensions captured from the active run's AgentConfig.
        # Used by ``_wrap_slide_html`` so the preview iframe is wrapped
        # at the true canvas size rather than the legacy 1280×720 default.
        # Reset on each POST /api/start; cleared on POST /api/reset.
        self._run_canvas_emu: Optional[tuple[int, int]] = None

        # Per-stage progress timing — populated by ``_on_llm_response``
        # (one entry per stage on first event) and consumed to compute
        # elapsed / ETA for the UI's progress strip. Cleared on idle.
        # ``_stage_start_ts`` records the first LLM event for each stage;
        # ``_stage_finish_ts`` is set when ``emit_stage_complete`` fires.
        # ``_stage_total`` caches the slide_total seen for the stage so the
        # final ``stage_progress`` at 100% can still carry a denominator.
        self._stage_start_ts: Dict[str, float] = {}
        self._stage_finish_ts: Dict[str, float] = {}
        self._stage_total: Dict[str, Optional[int]] = {}
        self._html_paths: List[str] = []

        self._app = FastAPI(title="Shuttleslide Studio")
        self._register_routes()

    # ------------------------------------------------------------------
    # Broadcaster Protocol (sync, fire-and-forget)
    # ------------------------------------------------------------------

    def emit_stage_complete(self, snapshot: StageSnapshot) -> None:
        """Broadcast a ``stage_complete`` message with the snapshot.

        Also caches the snapshot so ``/artifact/...`` routes can resolve
        per-element indices without re-walking AgentState. Safe to call
        before any client has connected (message lands in the replay
        buffer and is sent on connect).

        Before the boundary fires, emits one final ``stage_progress`` at
        100% so the UI's progress strip animates to full before flipping
        to the next stage's running state. Skipped if the stage never
        reported progress (atomic stage with no LLM events — e.g.
        ``rendered`` writing files directly).
        """
        stage = snapshot.stage
        self._stage_finish_ts[stage] = time.time()
        # Emit a final 100% progress beat when we have a denominator. The
        # UI uses this for a clean "fill to 100%" transition before the
        # next stage starts. Without a cached total there's nothing
        # meaningful to send — atomic stages already showed elapsed-only.
        total = self._stage_total.get(stage)
        start_ts = self._stage_start_ts.get(stage)
        if total is not None and start_ts is not None:
            self.emit_stage_progress(
                stage=stage,
                current=total,
                total=total,
                elapsed_seconds=self._stage_finish_ts[stage] - start_ts,
                eta_seconds=0.0,
                label=self._stage_label(stage, total, total),
            )

        self._last_snapshots[stage] = snapshot
        payload = msg_to_dict(
            StageCompleteMsg(
                stage=stage,
                # Convert snapshot dataclass to JSON-safe dict (msg_to_dict
                # recursively turns the EditTarget tuples into lists).
                snapshot=_to_json_safe(asdict(snapshot)),
                timestamp=time.time(),
            )
        )
        self._enqueue_broadcast(payload)

    def emit_stage_progress(
        self,
        stage: str,
        current: Optional[int],
        total: Optional[int],
        elapsed_seconds: Optional[float],
        eta_seconds: Optional[float],
        label: str,
    ) -> None:
        """Broadcast a ``stage_progress`` message.

        Called once per LLM response from ``_on_llm_response`` (and once
        more from ``emit_stage_complete`` with the 100% beat). Same
        broadcast path as the other emitters — late-connecting clients
        get a replay from ``_early_messages``.
        """
        percent: Optional[float]
        if current is not None and total is not None and total > 0:
            percent = min(100.0, (current / total) * 100.0)
        else:
            percent = None
        payload = msg_to_dict(StageProgressMsg(
            stage=stage,
            current=current,
            total=total,
            percent=percent,
            elapsed_seconds=elapsed_seconds,
            eta_seconds=eta_seconds,
            label=label,
            timestamp=time.time(),
        ))
        self._enqueue_broadcast(payload)

    @staticmethod
    def _stage_label(stage: str, current: int, total: int) -> str:
        """Human-readable numerator/denominator string for the strip.

        Maps each countable stage to its natural unit (slide / image);
        unknown stages fall back to the generic "item" word.
        """
        unit_map = {"outline": "Slide", "images": "Image", "slides": "Slide"}
        unit = unit_map.get(stage, "Item")
        return f"{unit} {current} / {total}"

    def emit_pipeline_done(self, html_paths: List[str]) -> None:
        """Broadcast the final ``pipeline_done`` message."""
        payload = msg_to_dict(PipelineDoneMsg(html_paths=list(html_paths)))
        self._enqueue_broadcast(payload)

    def emit_error(self, message: str, fatal: bool = False) -> None:
        """Broadcast an error message."""
        payload = msg_to_dict(ErrorMsg(message=message, fatal=fatal))
        self._enqueue_broadcast(payload)

    def emit_pipeline_state(self, state: str, error: Optional[str] = None) -> None:
        """Broadcast a ``pipeline_state`` message and update internal cache.

        Called by ``_run_pipeline`` on each lifecycle transition (idle →
        starting → running → done / failed). The UI switches its config
        and pipeline screens off this message.
        """
        self._pipeline_state = state
        if state == "failed":
            self._pipeline_error = error
        elif state in ("idle", "starting", "running", "done"):
            # Don't clear error on every transition — keep the last error
            # visible until the user explicitly resets. ``failed`` sets it,
            # ``idle`` (via POST /api/reset) clears it.
            pass
        payload = msg_to_dict(PipelineStateMsg(state=state, error=error))
        self._enqueue_broadcast(payload)

    def _resolve_active_run_dir(self) -> Optional[Path]:
        """Return the run directory whose state should be served on refresh.

        Prefers the in-memory ``_run_output_dir`` whenever it is set —
        it represents the user's current intent (the dir POST /api/start
        just created). Even when ``agent_state.json`` hasn't been written
        yet (first stage still mid-execution), returning this dir lets
        ``/api/state`` serve canvas dims + an empty snapshot list so the
        UI shows "Pipeline still starting" rather than hydrating the
        PREVIOUS run's snapshots — which was the "刷新后进入了另外的项目"
        bug.

        Falls back to the NEWEST prior run on disk that has state only
        when ``_run_output_dir`` is genuinely unset. ``_pipeline_state``
        is also idle at that point (POST /api/reset or fresh server boot),
        so the UI's syncStatusOnLoad takes the idle branch and doesn't
        call /api/state at all — the fallback is mostly defensive for
        code paths that fetch state without checking status first.

        Returns ``None`` only when there is no run with state anywhere
        under ``output_dir`` (fresh install, never completed a stage).

        Cost: the directory scan runs only on the fallback path; the
        common case (active run, with or without state) short-circuits
        at step 1.
        """
        # Step 1: the active run dir is the source of truth for "what
        # the user is currently doing". State file existence is handled
        # by /api/state — don't second-guess the active dir here.
        if self._run_output_dir is not None:
            return self._run_output_dir
        # Step 2: fall back to the newest run with state on disk.
        if self.output_dir is None:
            return None
        best: Optional[Path] = None
        best_mtime: float = -1.0
        try:
            for candidate in self.output_dir.glob("run_*/agent_state.json"):
                try:
                    m = candidate.stat().st_mtime
                except OSError:
                    continue
                if m > best_mtime:
                    best_mtime = m
                    best = candidate.parent
        except OSError:
            return None
        return best

    def emit_pipeline_stages(self, stages: List[str]) -> None:
        """Broadcast the full ordered stage list for the resolved pipeline.

        Called once at pipeline startup (right after ``pipeline_state=
        running``). The UI uses this as the single source of truth for
        the sidebar's tab list, so extension stages without a registered
        renderer (``script``, ``motion_design``) still get a tab up front
        in the correct execution order. Without this, the client's
        ``getAllStages()`` falls back to the hardcoded builtin list and
        pro tabs appear out of order or not at all.
        """
        payload = msg_to_dict(
            PipelineStagesMsg(stages=list(stages), timestamp=time.time())
        )
        self._enqueue_broadcast(payload)

    def emit_log_entry(
        self, scope: str, message: str, level: str = "info"
    ) -> None:
        """Broadcast a log entry to all connected clients.

        Same broadcast path as :meth:`emit_stage_complete` — late-connecting
        clients get a replay from ``_early_messages`` (within the 50-msg
        cap). Used by the ``on_llm_response`` callback to surface per-LLM-call
        progress to the UI's log drawer.
        """
        payload = msg_to_dict(LogEntryMsg(
            scope=scope,
            message=message,
            level=level,
            timestamp=time.time(),
        ))
        self._enqueue_broadcast(payload)

    def emit_history_snapshot(self, entries: List[Dict[str, Any]]) -> None:
        """Broadcast the current edit-history stack to all clients.

        Fired from the orchestrator after every successful edit / undo /
        revert. ``entries`` is newest-first (idx=0 = most recent), as
        produced by ``UndoStack.as_history_dicts()``. Late-connecting
        clients replay the most recent snapshot from ``_early_messages``.
        """
        payload = msg_to_dict(HistorySnapshotMsg(
            entries=list(entries),
            timestamp=time.time(),
        ))
        self._enqueue_broadcast(payload)

    def emit_stale_marks(self, marks: Dict[str, List[Dict[str, Any]]]) -> None:
        """Broadcast the current ``stale_marks`` dict to all clients.

        Fired from the orchestrator after every successful edit (and
        after undo / revert / regenerate) so the UI's stale badges
        reflect the current state. ``marks`` is the verbatim
        ``state.stale_marks`` dict (keyed by downstream stage name, each
        value a list of StaleMark dicts). Late-connecting clients replay
        the most recent snapshot from ``_early_messages``.
        """
        payload = msg_to_dict(StaleMarksUpdatedMsg(
            marks={stage: list(m) for stage, m in marks.items()},
            timestamp=time.time(),
        ))
        self._enqueue_broadcast(payload)

    def emit_chat_history(
        self,
        target_path: Any,
        messages: List[Dict[str, str]],
    ) -> None:
        """Broadcast per-target chat history to all connected clients.

        Triggered by the orchestrator after a successful LLM-mode edit
        (right after the assistant reply is appended to SessionStore)
        so the chat panel surfaces the LLM's natural-language reply
        without the user needing to switch targets to trigger a refresh.
        Late-connecting clients replay the most recent push via
        ``_early_messages``.

        ``messages`` is the ``[{role, body}]`` wire format. ``ref_id``
        is left empty — this is a server-initiated broadcast, not an ack
        for a specific client's request. The per-requester
        ``chat_history`` handler echoes the client's ``ref_id`` itself.
        """
        payload = msg_to_dict(ChatHistoryMsg(
            target_path=list(target_path),
            messages=list(messages),
            timestamp=time.time(),
        ))
        self._enqueue_broadcast(payload)

    def emit_item_regenerated(
        self,
        ref_id: str,
        stage: str,
        target_id: str,
        snapshot: Dict[str, Any],
        remaining_marks: Dict[str, List[Dict[str, Any]]],
    ) -> None:
        """Broadcast (unicast-style) the result of a ``regenerate_item`` call.

        ``ref_id`` pairs the ack back to the originating request so the
        requesting client can resolve its pending affordance. The
        regenerated ``snapshot`` carries the new value so all clients
        re-render. ``remaining_marks`` is the post-regenerate
        ``state.stale_marks`` (this also implicitly clears the badge for
        the just-regenerated target on every client).

        Broadcast (not true unicast) so that any other connected clients
        also see the new value and badge change. The requesting client
        additionally resolves its ``ref_id`` affordance via this message.
        """
        payload = msg_to_dict(ItemRegeneratedMsg(
            ref_id=ref_id,
            stage=stage,
            target_id=target_id,
            snapshot=snapshot,
            remaining_marks={
                stage: list(m) for stage, m in remaining_marks.items()
            },
        ))
        self._enqueue_broadcast(payload)

    # Map internal LLM event stage names (the names each node passes to
    # ``LLMResponseEvent(stage=...)``) to the canonical pipeline-stage names
    # the UI cares about. Without this mapping, the progress strip would
    # show "Running: slide_builder" instead of "Running: slides" — the
    # event-stage name is implementation detail, the pipeline-stage name
    # is what the user sees in the stage bar and what STAGE_LABELS maps.
    _EVENT_TO_PIPELINE_STAGE: Dict[str, str] = {
        "theme_designer": "theme",
        "structure_planner": "outline",
        "slide_detail_generator": "outline",
        "outline_planner": "outline",
        "image_acquirer": "images",
        "slide_builder": "slides",
        "svg_generator": "slides",
    }

    def _on_llm_response(self, event: Any) -> None:
        """Convert one ``LLMResponseEvent`` into a log drawer entry AND
        a structured ``stage_progress`` message for the progress strip.

        Log format: ``iter 2/12 · 3 tools · 1234 tok`` — concise enough to
        scan at a glance, informative enough to know what the pipeline is
        doing. Slide-scoped stages (slide_detail_generator, slide_builder,
        svg_generator) get a ``:N/M`` suffix on the scope so the user can
        watch per-slide progress.

        Progress format: structured fields (current / total / percent /
        elapsed / eta) carried straight through to the UI's progress
        strip. Atomic stages (no ``slide_total``) still emit a progress
        message so the strip can show an elapsed timer + indeterminate
        animation — ``current`` / ``total`` / ``percent`` are all ``None``
        in that case.

        Called from inside the orchestrator's asyncio task, which runs on
        the server loop — so ``emit_log_entry`` / ``emit_stage_progress``
        are safe to call directly (no ``call_soon_threadsafe`` needed;
        ``_enqueue_broadcast`` checks same-loop and short-circuits).
        ``event`` is typed as ``Any`` to avoid a circular import at module
        load (the agent package imports from review via
        InteractiveOrchestrator). The expected shape is
        ``LLMResponseEvent`` from ``shuttleslide.agent.llm.tool_call``.
        """
        event_stage = getattr(event, "stage", "") or ""
        # Translate the event's internal stage name (e.g. "slide_builder")
        # to the canonical pipeline stage name (e.g. "slides") so the
        # progress strip displays the user-facing label.
        stage = self._EVENT_TO_PIPELINE_STAGE.get(event_stage, event_stage)
        scope = f"llm:{event_stage}" if event_stage else "llm"
        slide_index = getattr(event, "slide_index", None)
        slide_total = getattr(event, "slide_total", None)
        if slide_index is not None and slide_total is not None:
            scope += f":{slide_index}/{slide_total}"

        # ---------------- progress-strip plumbing ----------------
        # Record stage start on first event; this is the timing anchor
        # for both elapsed display (atomic stages) and ETA computation
        # (countable stages). Cleared on idle in the reset path.
        now = time.time()
        if stage and stage not in self._stage_start_ts:
            self._stage_start_ts[stage] = now
        if stage and slide_total is not None:
            # Cache the latest total so emit_stage_complete can emit a
            # final 100% beat with a real denominator even if the last
            # LLM event came from a sub-node that didn't set slide_total.
            self._stage_total[stage] = slide_total

        if stage:
            elapsed: Optional[float] = None
            eta: Optional[float] = None
            current: Optional[int] = None
            total: Optional[int] = None
            label: str = ""
            start_ts = self._stage_start_ts.get(stage)
            if start_ts is not None:
                elapsed = now - start_ts
            if slide_index is not None and slide_total is not None and slide_total > 0:
                current = int(slide_index)
                total = int(slide_total)
                label = self._stage_label(stage, current, total)
                # Rolling-average ETA: average time-per-unit-so-far times
                # remaining units. Skips when current == 0 to avoid div-by-
                # zero (the first unit hasn't finished yet, so we have no
                # rate signal).
                if current > 0 and elapsed is not None and elapsed > 0:
                    eta = (elapsed / current) * (total - current)
            self.emit_stage_progress(
                stage=stage,
                current=current,
                total=total,
                elapsed_seconds=elapsed,
                eta_seconds=eta,
                label=label,
            )

        # ---------------- log drawer (unchanged) ----------------
        parts: List[str] = []
        iteration = getattr(event, "iteration", None)
        max_iterations = getattr(event, "max_iterations", None)
        if iteration is not None and max_iterations is not None:
            parts.append(f"iter {iteration}/{max_iterations}")
        tool_calls = getattr(event, "tool_calls", None) or []
        if tool_calls:
            n = len(tool_calls)
            parts.append(f"{n} tool{'s' if n != 1 else ''}")
        usage = getattr(event, "usage", None) or {}
        total = usage.get("total_tokens") if isinstance(usage, dict) else None
        if total:
            parts.append(f"{total} tok")
        self.emit_log_entry(scope, " · ".join(parts), "info")

    @property
    def _active_gate(self) -> Optional[ReviewGate]:
        """Return the gate that WS approve/cancel should target.

        In web-client mode the orchestrator (and its gate) are created
        per-run; in legacy/test mode the gate is fixed at construction.
        """
        if self._orchestrator is not None:
            return self._orchestrator.gate
        return self.gate

    def _canvas_dim_px(self, idx: int) -> Optional[int]:
        """Return one canvas dimension in CSS px, or None when no run active.

        ``idx=0`` → width, ``idx=1`` → height. Reads ``self._run_canvas_emu``
        (captured at POST /api/start from AgentConfig); legacy/test mode
        or pre-start returns None so /api/state omits canvas dims and the
        UI falls back to its 16:9 CSS defaults.
        """
        if self._run_canvas_emu is None:
            return None
        return emu_to_px(self._run_canvas_emu[idx])

    def _discover_ui_extensions(self) -> None:
        """Populate ``extra_static_dirs`` + ``ui_extension_scripts`` from
        the ``shuttleslide.review.ui_extensions`` entry-point group.

        Each entry point's value is ``"<package>:<rel/path/to.js>"``. The
        package's root directory is resolved via ``importlib.resources``
        and mounted at ``/ext/<sanitized-name>/`` so the JS file is
        reachable at ``/ext/<sanitized-name>/<rel/path/to.js>``. The URL
        is appended to ``ui_extension_scripts`` so ``/api/ui-extensions``
        can advertise it to the index.html loader.

        Failure isolation: any single broken entry point is skipped
        (logged at debug level) so other extensions still load. The
        method is best-effort throughout — if ``importlib.metadata``
        itself is broken, the server still starts with no extensions.
        """
        try:
            from importlib import metadata
            eps = metadata.entry_points()
            if hasattr(eps, "select"):
                group = eps.select(group="shuttleslide.review.ui_extensions")
            else:  # pragma: no cover — Python 3.9 compat
                group = eps.get("shuttleslide.review.ui_extensions", [])
        except Exception:  # noqa: BLE001 — best-effort discovery
            return

        for ep in group:
            value = (getattr(ep, "value", "") or "").strip()
            if ":" not in value:
                continue
            pkg_name, _, rel_path = value.partition(":")
            pkg_name = pkg_name.strip()
            rel_path = rel_path.strip().lstrip("/").replace("\\", "/")
            if not pkg_name or not rel_path:
                continue
            try:
                import importlib.resources
                pkg_dir = Path(str(importlib.resources.files(pkg_name)))
            except Exception:  # noqa: BLE001 — missing package / resolution error
                continue
            if not pkg_dir.is_dir():
                continue
            # URL-safe mount key. Prefer the entry-point name (stable
            # across package renames); fall back to a sanitized pkg name.
            raw_key = (getattr(ep, "name", "") or "").strip() or pkg_name
            mount_key = re.sub(r"[^A-Za-z0-9_-]", "_", raw_key)
            if not mount_key:
                continue
            # Constructor-supplied dirs take precedence over entry-point
            # ones (test overrides win).
            if mount_key not in self.extra_static_dirs:
                self.extra_static_dirs[mount_key] = pkg_dir
            self.ui_extension_scripts.append(f"/ext/{mount_key}/{rel_path}")

    def _get_full_registry(self) -> Any:
        """Lazily build + cache the full stage registry (core + extensions).

        Used by ``/api/state`` to detect pro stages (voiceover /
        render_video / ...) whose outputs live in ``state.stage_outputs``
        rather than the top-level fields (``state.theme`` etc.) the
        legacy detection checks. Each stage's ``is_cached(state)`` is
        the source of truth — core never names pro stages directly.

        Returns the cached ``StageRegistry`` (a fresh build on first
        call), ``None`` if the build failed (broken entry point etc.).
        Subsequent calls return the cache, so the entry-point load is
        amortised across all refresh-hydration requests in this server
        process.
        """
        if self._full_registry_cache is not None:
            return self._full_registry_cache if self._full_registry_cache is not False else None
        try:
            from shuttleslide.agent.review.registry import full_registry
            self._full_registry_cache = full_registry()
        except Exception:  # noqa: BLE001 — best-effort; /api/state still works for builtins
            self._full_registry_cache = False
            return None
        return self._full_registry_cache

    # ------------------------------------------------------------------
    # Broadcast dispatch — thread-safe handoff between loops
    # ------------------------------------------------------------------

    def _enqueue_broadcast(self, payload: Dict[str, Any]) -> None:
        """Append to the replay buffer and dispatch to the server loop.

        - If the server loop hasn't started yet (or has closed), the
          payload still lands in ``_early_messages`` and will be replayed
          on the first connection.
        - If we're being called from the server loop itself, dispatch
          directly to avoid the ``call_soon_threadsafe`` round-trip.
        - Otherwise (typical orchestrator-loop caller), schedule
          ``_broadcast_now`` on the server loop.
        """
        self._early_messages.append(payload)
        loop = self._server_loop
        if loop is None or loop.is_closed():
            return
        try:
            current = asyncio.get_running_loop()
            same_loop = current is loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            self._broadcast_now(payload)
        else:
            loop.call_soon_threadsafe(self._broadcast_now, payload)

    def _broadcast_now(self, payload: Dict[str, Any]) -> None:
        """Send payload to every connected WS client. Server-loop only."""
        if not self._connections:
            return
        dead = []
        for ws in list(self._connections):
            try:
                asyncio.create_task(ws.send_json(payload))
            except RuntimeError:
                # Connection closed mid-iteration — clean up.
                dead.append(ws)
        for ws in dead:
            self._connections.discard(ws)

    # ------------------------------------------------------------------
    # HTTP / WS routes
    # ------------------------------------------------------------------

    def _register_routes(self) -> None:
        app = self._app

        # Force browser revalidation of static assets. Without this,
        # Starlette's StaticFiles serves Last-Modified/ETag headers but
        # no Cache-Control — soft refreshes can keep serving a stale
        # app.js even after edits, which makes UI bug fixes look like
        # they didn't land. ``no-cache`` still allows conditional
        # requests (If-Modified-Since), so a 304 on unchanged files is
        # cheap; only changed files re-download.
        @app.middleware("http")
        async def bust_static_cache(request: Request, call_next):
            response = await call_next(request)
            path = request.url.path
            # Core UI assets + /files/ (slide-mounted svgs/ and images/).
            # /files/ needs no-cache so SVG/image edits land in the
            # browser: StaticFiles serves Last-Modified, the browser
            # revalidates each fetch (304 cheap, 200 + new bytes when
            # the editor wrote new content). Without this, heuristic
            # caching serves stale <img src="svgs/slide_N_X.svg"> bytes
            # even after the iframe itself reloads.
            if (path in ("/", "/app.js", "/styles.css", "/index.html")
                    or path.startswith("/files/")
                    or path.startswith("/ext/")):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

        # Mount output_dir as /files/ so relative paths inside served slide
        # fragments (e.g. ``<img src="images/slide_0_hero.jpg">``) resolve
        # to real bytes. The mount must exist BEFORE we serve any wrapped
        # slide HTML — see _wrap_slide_html which injects ``<base href="/files/">``.
        # Skipped when output_dir is None (stub-test scenario) — in that case
        # /files/ simply 404s and stub tests don't exercise iframe rendering.
        if self.output_dir is not None and self.output_dir.is_dir():
            app.mount(
                "/files",
                StaticFiles(directory=str(self.output_dir)),
                name="files",
            )

        # Mount UI extension static dirs (pro ships ext.js for custom
        # stage renderers like voiceover audio players / video players).
        # Mounted BEFORE the static-root at "/" so requests aren't
        # shadowed by the catch-all. Constructor-supplied entries + those
        # discovered from the ``shuttleslide.review.ui_extensions`` entry-
        # point group at construction. Each dir is mounted at /ext/<key>/.
        for mount_key, dir_path in self.extra_static_dirs.items():
            if dir_path.is_dir():
                app.mount(
                    f"/ext/{mount_key}",
                    StaticFiles(directory=str(dir_path)),
                    name=f"ext-{mount_key}",
                )

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            self._connections.add(ws)
            try:
                # Replay buffered messages so a late-connecting client
                # sees the full stage history.
                for msg in list(self._early_messages):
                    await ws.send_json(msg)
                # Always emit the current pipeline_state to a freshly-
                # connected client so the UI switches to the right
                # screen even when _early_messages was cleared (last-
                # client disconnect, see finally block below) or never
                # populated (server just booted, _pipeline_state
                # restored from disk by _restore_pipeline_state_from_disk).
                # Without this, a refresh where _early_messages is empty
                # leaves the UI reliant on syncStatusOnLoad's HTTP fetch
                # alone, with no WS-side redundancy if that path hiccups.
                await ws.send_json(
                    msg_to_dict(
                        PipelineStateMsg(
                            state=self._pipeline_state,
                            error=self._pipeline_error,
                        )
                    )
                )
                # Main read loop. We exit when the client disconnects
                # (WebSocketDisconnect) or sends an explicit close frame.
                while True:
                    try:
                        raw = await ws.receive_text()
                    except WebSocketDisconnect:
                        break
                    try:
                        msg = json.loads(raw)
                        await self._handle_client_message(ws, msg)
                    except _MalformedMessage as exc:
                        await self._send(ws, ErrorMsg(message=str(exc)))
                    except json.JSONDecodeError:
                        await self._send(
                            ws, ErrorMsg(message="client message was not valid JSON")
                        )
                    except Exception as exc:  # pragma: no cover - defensive
                        # Never let one bad message kill the socket.
                        await self._send(ws, ErrorMsg(message=f"server error: {exc}"))
            finally:
                self._connections.discard(ws)
                if not self._connections:
                    # Last client gone — release the replay buffer. State
                    # is already on disk (per-stage save in _post_stage_hook
                    # at interactive_orchestrator.py:240), and _last_snapshots
                    # stays because /artifact/* routes depend on it
                    # (server.py /artifact/slides, /artifact/images,
                    # /artifact/theme). Late reconnects hydrate via
                    # GET /api/state instead of WS replay.
                    self._early_messages.clear()

        @app.get("/artifact/slides/{idx}")
        async def slide_artifact(idx: int) -> Response:
            snap = self._last_snapshots.get("slides") or self._last_snapshots.get("rendered")
            if snap is None:
                return JSONResponse(
                    {"detail": "no slides snapshot available"}, status_code=404
                )
            slides = snap.state_view.get("slides", [])
            if idx < 0 or idx >= len(slides):
                return JSONResponse(
                    {"detail": f"slide index {idx} out of range (have {len(slides)})"},
                    status_code=404,
                )
            html = slides[idx].get("html", "")
            wrapped = self._wrap_slide_html(html, idx)
            return Response(content=wrapped, media_type="text/html")

        @app.get("/artifact/images/{slide_idx}/{slot_id}")
        async def image_artifact(slide_idx: int, slot_id: str) -> Response:
            snap = self._last_snapshots.get("images")
            if snap is None:
                return JSONResponse(
                    {"detail": "no images snapshot available"}, status_code=404
                )
            slot_map = snap.state_view.get("slide_images", {}).get(str(slide_idx), {})
            if slot_id not in slot_map:
                return JSONResponse(
                    {"detail": f"slot {slot_id!r} not found on slide {slide_idx}"},
                    status_code=404,
                )
            payload = slot_map[slot_id]
            p_type = payload.get("type")
            if p_type in ("svg", "svg_file"):
                # Inline SVG — return as image/svg+xml so <img> tags
                # render it directly. ``svg_file`` is the production
                # payload shape (set_svg in svg_tools.py:330); legacy
                # ``svg`` is grandfathered per state.py:118-120.
                # ``data`` is always populated by set_svg; we only fall
                # back to disk read if a future producer skips it.
                data = payload.get("data", "")
                if not data:
                    path = self._resolve_image_path(payload.get("path", ""))
                    if path is None or not path.exists():
                        return JSONResponse(
                            {"detail": "svg payload has no data and file missing"},
                            status_code=404,
                        )
                    data = path.read_text(encoding="utf-8")
                return Response(content=data, media_type="image/svg+xml")
            if p_type in ("image_file", "image"):
                # Resolve from filesystem. ``path`` is relative to
                # AgentConfig.output_dir when image_acquirer wrote it
                # (see state.py:96); abs paths (e.g. from stub tests)
                # pass through unchanged. PR3's uploader will reuse
                # this route to serve freshly uploaded files.
                path = self._resolve_image_path(payload.get("path", ""))
                if path is None:
                    return JSONResponse(
                        {"detail": "image payload has no path"}, status_code=404
                    )
                if not path.exists():
                    return JSONResponse(
                        {"detail": f"image file missing: {path}"}, status_code=404
                    )
                mime = payload.get("mime", "application/octet-stream")
                return Response(content=path.read_bytes(), media_type=mime)
            return JSONResponse(
                {"detail": f"unsupported payload type {p_type!r}"}, status_code=404
            )

        # ------------------------------------------------------------------
        # Web-client mode endpoints — drive the config → pipeline lifecycle.
        # In legacy/test mode (gate passed at construction) these are still
        # safe to call but POST /api/start returns 400 "external orchestrator
        # already running" because the orchestrator isn't owned by the server.
        # ------------------------------------------------------------------

        @app.post("/api/start")
        async def api_start(payload: Dict[str, Any]) -> JSONResponse:
            """Accept user-submitted AgentConfig, kick off the pipeline.

            Request body fields (all strings unless noted):
              - Required: api_base, api_key, model
              - Topic source (mutually exclusive — exactly one):
                - topic:          str (direct text)
                - html_file_b64:  str (HTML upload, extracted via trafilatura)
                - text_file_b64:  str (markdown/text upload, used verbatim)
                - text_filename:  str (used to detect markdown vs plain)
              - Optional: style_hint, target_slide_count (int|null),
                temperature (float), vlm_api_base, vlm_api_key, vlm_model,
                image_search_provider, disable_required_tool_choice (bool)

            Returns 200 + {state} on success, 400 on validation failure,
            409 if a pipeline is already running.
            """
            if self._pipeline_state in ("starting", "running"):
                return JSONResponse(
                    {"detail": "a pipeline is already running; call POST /api/reset first"},
                    status_code=409,
                )
            if self.gate is not None and self.orchestrator_loop is not None:
                # Legacy mode: orchestrator is owned by the caller, the
                # server can't start a new one. Surface this clearly.
                return JSONResponse(
                    {"detail": "server was constructed with an external orchestrator; "
                               "POST /api/start is only available in web-client mode"},
                    status_code=400,
                )

            # Build AgentConfig from form fields. We accept only known
            # fields so the JSON body can't inject arbitrary kwargs
            # (e.g. on_llm_response callback).
            try:
                cfg_kwargs = self._extract_config_kwargs(payload)
                # load_state_from mode: stash the run dirname here so we
                # can route the orchestrator to load mode below. The key
                # is private (double underscore) and must be popped before
                # AgentConfig construction — AgentConfig would reject it.
                load_state_from = cfg_kwargs.pop("__load_state_from__", None)
                # user_images staging dir — moved into the run dir once
                # _make_run_dir succeeds (see below). Popped here so it
                # never reaches AgentConfig construction.
                user_image_staging = cfg_kwargs.pop("__user_image_staging__", None)
                if load_state_from:
                    # Load mode skips all LLM calls; api creds aren't used.
                    # Substitute placeholders so AgentConfig.validate() still
                    # passes (it requires api_base/api_key/model).
                    cfg_kwargs.setdefault("api_base", "http://load.local")
                    cfg_kwargs.setdefault("api_key", "load-mode-no-llm")
                    cfg_kwargs.setdefault("model", "loaded-state")
                if self.mock_mode:
                    # Mock mode also skips all LLM calls; same placeholder
                    # pattern so validate() doesn't reject the request.
                    # The user might not have any creds configured at all
                    # (entire point of --mock), so we set all three
                    # unconditionally rather than just defaulting.
                    cfg_kwargs["api_base"] = "http://mock.local"
                    cfg_kwargs["api_key"] = "mock-mode-no-llm"
                    cfg_kwargs["model"] = "mock-mode"
                # Wire the LLM response observer so each chat_with_tools
                # call broadcasts a log entry to the review UI's log
                # drawer. _extract_config_kwargs excludes this field (it's
                # a callback, not a form value), so we inject it here. In
                # load-state mode, completed stages skip their LLM calls
                # (_pre_stage_hook returns True), so no log entries fire
                # for them — only for stages that actually need to run.
                cfg_kwargs["on_llm_response"] = self._on_llm_response
                from shuttleslide.agent.config import AgentConfig
                config = AgentConfig(**cfg_kwargs)
                config.validate()
            except (ValueError, TypeError) as e:
                return JSONResponse({"detail": str(e)}, status_code=400)

            # Resolve the per-run output directory.
            # - Fresh run: timestamped subdir under base output_dir.
            # - Load mode: reuse the chosen run dir (state file already
            #   lives there; orchestrator's _save_state will overwrite in
            #   place). User mental model: "I'm re-reviewing this run,"
            #   not "I'm creating a new run."
            if load_state_from:
                run_dir = self.output_dir / load_state_from
                run_dir.mkdir(parents=True, exist_ok=True)
            else:
                run_dir = self._make_run_dir()
            config.output_dir = str(run_dir)
            self._run_output_dir = run_dir

            # Migrate staged user-uploaded images into the run dir. The
            # _extract_config_kwargs step wrote them to a tempdir because
            # the run dir didn't exist yet; now that it does, move each
            # file into run_dir/user_images/ and rewrite the library
            # paths to be relative to run_dir (so agent_state.json round-
            # trips cleanly and the image_acquirer's relative-path
            # resolution works after a load_state_from resume).
            if user_image_staging and config.user_image_library:
                user_img_dir = run_dir / "user_images"
                user_img_dir.mkdir(parents=True, exist_ok=True)
                staging_p = Path(user_image_staging)
                for entry in config.user_image_library:
                    old_path = Path(entry["path"])
                    if not old_path.exists():
                        continue
                    new_path = user_img_dir / old_path.name
                    shutil.move(str(old_path), str(new_path))
                    entry["path"] = f"user_images/{old_path.name}"
                # Best-effort cleanup of the now-empty staging dir.
                shutil.rmtree(staging_p, ignore_errors=True)

                # Fill in descriptions the user left blank via the VLM
                # describer. Mirrors the review-phase ImageUploader's
                # _resolve_description: user input wins, VLM fills the
                # rest, fail-open to "" when VLM is unavailable / errors.
                # We do this AFTER migration so file paths in the
                # library are already relative to run_dir (avoids
                # leaking staging paths into any error telemetry).
                await self._autofill_user_image_descriptions(config)

            # Reset run-scoped state from any previous run.
            self._last_snapshots.clear()
            self._html_paths = []
            self._pipeline_error = None
            # Capture the active run's canvas dims so /artifact/slides/*
            # (which doesn't have AgentConfig in scope) can wrap fragments
            # at the right size. The config object is fully built at this
            # point (validate() has passed), so reading its canvas_*_emu
            # is safe.
            self._run_canvas_emu = (config.canvas_width_emu, config.canvas_height_emu)
            # Clear per-stage progress timing so the new run's first
            # stage_progress event records a fresh start timestamp.
            self._stage_start_ts.clear()
            self._stage_finish_ts.clear()
            self._stage_total.clear()

            self.emit_pipeline_state("starting")
            self._pipeline_task = asyncio.create_task(
                self._run_pipeline(config, load_state_on_start=bool(load_state_from))
            )
            return JSONResponse({"state": self._pipeline_state, "run_dir": str(run_dir)})

        @app.post("/api/vlm_describe")
        async def api_vlm_describe(payload: Dict[str, Any]) -> JSONResponse:
            """Generate a one-sentence description for an image via the VLM.

            Used by the homepage "Image assets" fieldset's per-row
            Auto-describe button: the user picks a file, clicks the
            button, and the result lands in that row's textarea before
            the form is submitted. The endpoint reuses the same VLM
            describer as the review-phase ImageUploader, so output
            format and fail-open behaviour are identical.

            Request body: ``{data_b64: str, mime: str,
            vlm_api_base?: str, vlm_api_key?: str, vlm_model?: str,
            api_base?: str, api_key?: str, model?: str}``.

            The VLM creds can be passed inline (so the user can describe
            an image without yet submitting the whole form), or omitted
            to fall back to ``effective_defaults``.

            Returns ``{description: str}`` on success, 400 with
            ``{detail: ...}`` when VLM is not configured.
            """
            data_b64 = payload.get("data_b64")
            mime = (payload.get("mime") or "image/jpeg").strip().lower()
            if not isinstance(data_b64, str) or not data_b64.strip():
                return JSONResponse(
                    {"detail": "data_b64 is required"}, status_code=400
                )

            # Build a stub AgentConfig carrying the VLM creds. The form
            # may have sent them inline (the user typed creds and clicked
            # Auto-describe before submitting); otherwise fall back to
            # the server's effective_defaults.
            from shuttleslide.agent.config import AgentConfig

            def _cred(key: str) -> str:
                v = payload.get(key)
                if isinstance(v, str) and v.strip():
                    return v.strip()
                return str(self.effective_defaults.get(key, "") or "")

            stub = AgentConfig(
                api_base=_cred("api_base"),
                api_key=_cred("api_key"),
                model=_cred("model"),
                vlm_api_base=_cred("vlm_api_base"),
                vlm_api_key=_cred("vlm_api_key"),
                vlm_model=_cred("vlm_model"),
            )
            # ASCII check before LLMClient construction — non-ASCII in
            # api_key/vlm_api_key would otherwise crash inside httpx when
            # building the Authorization header. Stub can't call full
            # validate() because that requires api_base/api_key/model
            # non-empty, but Auto-describe only needs VLM creds.
            try:
                stub.validate_ascii_http_fields()
            except ValueError as exc:
                return JSONResponse(
                    {"detail": str(exc)}, status_code=400
                )

            from shuttleslide.agent.review.editors.image_uploader import (
                _build_vlm_client,
            )

            vlm_client = _build_vlm_client(stub)
            if vlm_client is None:
                return JSONResponse(
                    {"detail": "VLM endpoint not configured"},
                    status_code=400,
                )

            from shuttleslide.agent.nodes.image_sources.describer import (
                VLMDescriber,
            )

            try:
                image_bytes = base64.b64decode(data_b64, validate=False)
            except Exception as exc:
                return JSONResponse(
                    {"detail": f"base64 decode failed: {exc}"},
                    status_code=400,
                )
            if len(image_bytes) > self._USER_IMAGE_MAX_BYTES:
                return JSONResponse(
                    {"detail": f"image exceeds {_USER_IMAGE_MAX_BYTES} bytes"},
                    status_code=400,
                )

            describer = VLMDescriber(vlm_client)
            b64 = base64.b64encode(image_bytes).decode("ascii")
            desc = await describer.describe(b64, mime, slide_index=None)
            return JSONResponse({"description": desc})

        @app.get("/api/status")
        async def api_status() -> JSONResponse:
            """Return current pipeline state for UI recovery on refresh."""
            return JSONResponse({
                "state": self._pipeline_state,
                "error": self._pipeline_error,
                "stages_completed": [s for s in self._last_snapshots.keys()],
                "html_paths": list(self._html_paths),
                "run_dir": str(self._run_output_dir) if self._run_output_dir else None,
            })

        @app.get("/api/state")
        async def api_state() -> JSONResponse:
            """Full snapshot state for client hydration after reconnect.

            With ``_early_messages`` cleared on last-client disconnect
            (see ``ws_endpoint`` finally block), WS replay no longer
            works for late-connecting clients. This endpoint rebuilds
            snapshot state from on-disk ``agent_state.json`` so the UI
            hydrates as if all ``stage_complete`` messages had been
            delivered live.

            Always serves the NEWEST run with a saved state file under
            ``output_dir``. If the active ``_run_output_dir`` has state
            (the normal mid-run case), that wins; otherwise we fall
            back to the most recent prior run so a browser refresh
            always shows the last good state instead of a blank screen.
            Returns an empty snapshots list only when no run has ever
            completed a stage.
            """
            run_dir = self._resolve_active_run_dir()
            if run_dir is None:
                # No on-disk state yet — but if /api/start has cached
                # canvas dims for a freshly-started run (before the
                # first stage saves state), surface them so the UI can
                # size the preview iframe at the right aspect ratio
                # during the gap between POST /api/start and the first
                # agent_state.json write.
                canvas_w_px = (
                    emu_to_px(self._run_canvas_emu[0])
                    if self._run_canvas_emu else None
                )
                canvas_h_px = (
                    emu_to_px(self._run_canvas_emu[1])
                    if self._run_canvas_emu else None
                )
                return JSONResponse({
                    "state": self._pipeline_state,
                    "snapshots": [],
                    "active_stage": None,
                    "pipeline_done": False,
                    "gate_paused": False,
                    "html_paths": [],
                    "run_dir": None,
                    "canvas_width_px": canvas_w_px,
                    "canvas_height_px": canvas_h_px,
                })

            state_file = run_dir / "agent_state.json"
            if not state_file.exists():
                # Active run dir exists but no stage has saved state yet
                # (first stage still mid-execution). Return an empty-
                # snapshots response with canvas dims + run_dir so the
                # UI hydrates with the fallback "Pipeline still running"
                # banner rather than 500-ing or hydrating from a stale
                # prior run (which _resolve_active_run_dir no longer
                # falls through to).
                canvas_w_px = (
                    emu_to_px(self._run_canvas_emu[0])
                    if self._run_canvas_emu else None
                )
                canvas_h_px = (
                    emu_to_px(self._run_canvas_emu[1])
                    if self._run_canvas_emu else None
                )
                return JSONResponse({
                    "state": self._pipeline_state,
                    "snapshots": [],
                    "active_stage": None,
                    "pipeline_done": False,
                    "gate_paused": False,
                    "html_paths": [],
                    "run_dir": str(run_dir),
                    "canvas_width_px": canvas_w_px,
                    "canvas_height_px": canvas_h_px,
                })
            try:
                loaded = load_state(state_file)
            except Exception as exc:
                return JSONResponse(
                    {"detail": f"failed to load state: {exc}"},
                    status_code=500,
                )

            # Detect completed stages by populated fields. Stages run in
            # registry-resolved order, so this preserves chronological
            # ordering without needing a persisted "completed" list.
            completed: List[str] = []
            if loaded.theme:
                completed.append("theme")
            if loaded.outline:
                completed.append("outline")
            if loaded.slide_images:
                completed.append("images")
            if loaded.slides:
                completed.append("slides")
            if loaded.html_paths:
                completed.append("rendered")

            # Extension stages (voiceover, render_video, ...) aren't
            # hardcoded here — core doesn't know their names. Instead,
            # ask the registry: any stage whose is_cached(state) returns
            # True has output on disk. This is what makes /api/state
            # hydrate pro-stage tabs after a browser refresh, so the
            # user sees "Voiceover" / "Video" tabs without re-running.
            registry = self._get_full_registry()
            if registry is not None:
                try:
                    for stage in registry.resolve_order():
                        if stage.name in completed:
                            continue
                        try:
                            if stage.is_cached(loaded):
                                completed.append(stage.name)
                        except Exception:  # noqa: BLE001 — best-effort
                            continue
                except Exception:  # noqa: BLE001 — registry.resolve_order may raise
                    pass

            snapshots_payload = []
            for stage_name in completed:
                # Prefer in-memory snapshot (richer; carries editable
                # targets as built by the live orchestrator). Fall back
                # to rebuild-from-state if memory was cleared or this is
                # a fresh server process reading a prior run.
                snap = self._last_snapshots.get(stage_name)
                if snap is None:
                    # ``build_snapshot`` is the dispatcher; it accepts the
                    # caller's registry so pro stages (voiceover/render_video)
                    # resolve via ``full_registry()`` rather than the
                    # builtin-only ``default_registry()``. Without passing
                    # it, the helper raises KeyError on extension stages
                    # before the fallback below can run. ValueError covers
                    # stages whose build_snapshot() returned None (silent
                    # stage w/ partial state on disk) — treat as "no snap".
                    try:
                        snap = build_snapshot(
                            stage_name, loaded, registry=registry
                        )
                    except (KeyError, ValueError):
                        snap = None
                if snap is None:
                    # Stage ran but produced no snapshot — skip rather
                    # than ship a null entry that breaks client hydration.
                    continue
                # Write-back so /artifact/* routes (which read
                # _last_snapshots) can serve slide/image/theme bytes
                # for stages that completed in a prior session.
                self._last_snapshots[stage_name] = snap
                snapshots_payload.append({
                    "stage": stage_name,
                    "snapshot": _to_json_safe(asdict(snap)),
                    "timestamp": snap.timestamp,
                })

            # Full ordered stage list (resolved pipeline order, including
            # extension stages). Single source of truth for the UI's
            # sidebar tab list — clients use this to pre-create all tabs
            # in the correct execution order so stages without a
            # registered renderer (script, motion_design) still appear.
            # Fallback to builtin STAGE_NAMES if the registry can't be
            # built (broken entry point) so the client always receives a
            # non-empty list and the sidebar never blanks out.
            stage_order: List[str] = []
            if registry is not None:
                try:
                    stage_order = [s.name for s in registry.resolve_order()]
                except Exception:  # noqa: BLE001 — best-effort
                    stage_order = []
            if not stage_order:
                from shuttleslide.agent.review.review_gate import STAGE_NAMES
                stage_order = list(STAGE_NAMES)

            return JSONResponse({
                "state": self._pipeline_state,
                "error": self._pipeline_error,
                "snapshots": snapshots_payload,
                "stage_order": stage_order,
                "active_stage": completed[-1] if completed else None,
                # pipeline_done is "did the orchestrator finish the
                # ENTIRE resolved stage order?" — only true when
                # _pipeline_state == "done". Previously this also OR'd
                # in `bool(loaded.html_paths)`, which was correct when
                # `rendered` was always the last stage; with pro
                # extensions (voiceover/motion_design/render_video)
                # appended after it, html_paths is populated while pro
                # stages are still mid-flight, and OR-ing it in would
                # permanently disable the Approve button on refresh.
                # The PPTX download button is now shown client-side
                # whenever a "rendered" snapshot exists (see app.js
                # showPptxDownloadButton), decoupled from this flag.
                "pipeline_done": self._pipeline_state == "done",
                # True only when the orchestrator is actually blocked
                # at gate.pause_for_review. Distinguishes "refreshed
                # while paused" (Approve should work) from "refreshed
                # while mid-stage execution" (Approve would be a silent
                # no-op — see review_gate.py:139-143). Client uses this
                # to decide whether to enable the Approve button.
                "gate_paused": bool(self._active_gate and self._active_gate.is_paused),
                "html_paths": list(self._html_paths),
                "run_dir": str(run_dir),
                # Canvas dims from the loaded state — the UI uses these
                # to size thumbnails + preview iframe at the true aspect
                # ratio (e.g. 9:16 portrait instead of the legacy 16:9).
                # Preferred over self._run_canvas_emu because the loaded
                # state is authoritative for what's actually on disk.
                "canvas_width_px": emu_to_px(loaded.canvas_width_emu),
                "canvas_height_px": emu_to_px(loaded.canvas_height_emu),
            })

        @app.get("/api/defaults")
        async def api_defaults() -> JSONResponse:
            """Return credential defaults + which fields are locked.

            Used by the UI on page load to pre-fill credential inputs
            and mark them read-only when locked. Locked fields cannot be
            overridden by the form — POST /api/start enforces
            ``effective_defaults`` server-side regardless of payload.

            ``defaults`` is the effective value (CLI overrides win over
            .env). ``locked`` is the union of fields set by either
            source. The UI uses ``locked`` containing all of
            ``api_base``/``api_key``/``model`` to hide the credentials
            section entirely and show only the model name.

            Sensitive values (``api_key``, ``vlm_api_key``) are NEVER
            included in ``defaults`` — the UI shows a mask placeholder.
            Their names still appear in ``locked`` so the UI can mark
            them readonly. ``_extract_config_kwargs`` re-injects the
            real values server-side at POST /api/start.
            """
            safe_defaults = {
                k: v for k, v in self.effective_defaults.items()
                if k not in self._SENSITIVE_FIELDS
            }
            return JSONResponse({
                "defaults": safe_defaults,
                "locked": sorted(self.locked_fields),
                # Surfaced so the UI can hide the credentials section
                # entirely and show a "Mock mode" badge. When true, the
                # backend uses MockInteractiveOrchestrator (no real LLM
                # calls); credentials are irrelevant.
                "mock_mode": self.mock_mode,
                # Surfaced so the UI knows to render the aspect-ratio
                # picker on the config screen. When False the picker is
                # omitted and the form doesn't submit canvas_aspect_ratio
                # (AgentConfig's 16:9 defaults apply).
                "canvas_mode": self.canvas_mode,
            })

        @app.get("/api/ui-extensions")
        async def api_ui_extensions() -> JSONResponse:
            """Advertise extension script URLs the UI should load.

            Each URL was discovered at construction time from the
            ``shuttleslide.review.ui_extensions`` entry-point group + any
            constructor-supplied ``extra_static_dirs``. The UI's
            index.html loader fetches this endpoint and dynamically
            injects one ``<script>`` per URL **before** app.js runs —
            so external packages can register custom stage renderers
            via ``window.SlidecraftReview.registerStageRenderer(...)``.

            Returns ``{"scripts": ["/ext/<key>/<path>.js", ...]}``. Empty
            list when no extensions are installed (the loader then
            short-circuits and only app.js loads — review's builtin 5
            stages work as usual).
            """
            return JSONResponse({"scripts": list(self.ui_extension_scripts)})

        @app.get("/api/runs")
        async def api_runs() -> JSONResponse:
            """List previous runs (those with ``agent_state.json``).

            Each entry includes ``run_dirname`` (the URL-safe subdir name),
            a short ``topic_preview``, ``slide_count``, ``saved_at``
            timestamp, and ``has_html`` (whether ``html_paths`` is
            non-empty — distinguishes "pipeline finished" from "crashed
            mid-run"). Sorted by ``saved_at`` descending (most recent
            first) so the UI can show a "Recent runs" list with the
            newest on top.

            Skips silently when ``output_dir`` is missing, and skips
            individual runs whose state file is corrupt or missing —
            one bad run must never break the listing endpoint.
            """
            if self.output_dir is None or not self.output_dir.exists():
                return JSONResponse({"runs": []})
            runs = []
            for run_dir in sorted(self.output_dir.iterdir()):
                if not run_dir.is_dir():
                    continue
                if not _RUN_DIRNAME_RE.match(run_dir.name):
                    continue
                state_file = run_dir / "agent_state.json"
                if not state_file.exists():
                    continue
                try:
                    data = json.loads(state_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue  # corrupted — skip, don't crash the endpoint
                topic = data.get("topic", "") or ""
                runs.append({
                    "run_dirname": run_dir.name,
                    "topic_preview": topic[:100],
                    "slide_count": len(data.get("slides", [])),
                    "saved_at": data.get("saved_at", 0.0),
                    "has_html": bool(data.get("html_paths")),
                })
            runs.sort(key=lambda r: r["saved_at"], reverse=True)
            return JSONResponse({"runs": runs})

        @app.post("/api/reset")
        async def api_reset() -> JSONResponse:
            """Return to idle so the user can reconfigure and re-run.

            Cancels an in-flight pipeline task, drops the orchestrator
            reference, and clears the snapshot cache. Does NOT delete
            files on disk — the user may want to keep previous outputs.
            """
            if self._pipeline_task is not None and not self._pipeline_task.done():
                self._pipeline_task.cancel()
            self._pipeline_task = None
            self._orchestrator = None
            self._pipeline_error = None
            self._html_paths = []
            self._run_canvas_emu = None
            # Clear per-stage progress timing so the next run starts fresh
            # (otherwise the first stage_progress of the new run would
            # compute elapsed against the previous run's start timestamp).
            self._stage_start_ts.clear()
            self._stage_finish_ts.clear()
            self._stage_total.clear()
            # Drop any cached PPTX so a re-run doesn't serve stale exports.
            # Kept inside the run dir, so a different run never sees it —
            # but if the user re-uses the same run dir via load_state_from,
            # they'd otherwise hit the previous run's export.pptx.
            if self._run_output_dir is not None:
                cached = self._run_output_dir / "export.pptx"
                if cached.exists():
                    try:
                        cached.unlink()
                    except OSError:
                        pass  # best-effort; not worth failing reset over
            # Keep _last_snapshots so user can still browse the previous
            # run via the artifact routes after going back to config.
            self.emit_pipeline_state("idle")
            return JSONResponse({"state": self._pipeline_state})

        @app.get("/api/pptx")
        async def api_pptx() -> Response:
            """Render the current run's state to PPTX and stream as a download.

            Requires an active ``_run_output_dir`` with ``agent_state.json``
            and at least one HTML file in ``html_paths``. Each HTML file is
            fed through :class:`RuleSlideTransformer` (Playwright + rule
            extraction) to populate slide ``elements`` — the saved state's
            DSL keeps slides as ``slots.html`` for HTML rendering, which
            would produce an empty PPTX if passed to PPTXRenderer directly.

            Renders to a cached ``export.pptx`` inside the run dir and
            reuses it on subsequent calls — clicking the button twice
            doesn't redo the work. To force a re-render, delete the file
            or call POST /api/reset (which clears the cache).

            Returns 400 when no run is active, the state file is missing,
            or no HTML files are present. 500 when Playwright/PPTXRenderer
            raises. The 500 body includes the error string so the UI can
            surface it.
            """
            if self._run_output_dir is None:
                return JSONResponse(
                    {"detail": "no active run; start a pipeline or load a previous run first"},
                    status_code=400,
                )
            state_file = self._run_output_dir / "agent_state.json"
            if not state_file.exists():
                return JSONResponse(
                    {"detail": f"state file not found: {state_file}"},
                    status_code=400,
                )
            # Bail early on a run whose pipeline never finished writing
            # HTML files — Playwright transform would silently produce
            # an empty deck, which is worse than a clear error.
            try:
                from shuttleslide.agent.review.state_persistence import load_state
                pre_check_state = load_state(state_file)
            except Exception as e:
                return JSONResponse(
                    {"detail": f"failed to read state file: {e}"},
                    status_code=500,
                )
            if not pre_check_state.html_paths:
                return JSONResponse(
                    {"detail": "no HTML files in saved state; pipeline did not finish"},
                    status_code=400,
                )
            out_path = self._run_output_dir / "export.pptx"
            if not out_path.exists():
                try:
                    presentation = await self._render_run_to_pptx_dsl(state_file)
                    from shuttleslide.html_to_pptx import PPTXRenderer
                    renderer = PPTXRenderer(base_dir=self._run_output_dir)
                    renderer.render(presentation, str(out_path))
                except Exception as e:
                    return JSONResponse(
                        {"detail": f"PPTX render failed: {e}"},
                        status_code=500,
                    )
            return FileResponse(
                str(out_path),
                media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
                filename="presentation.pptx",
            )

        @app.post("/upload")
        async def api_upload(
            request: Request,
            slide_idx: int = Form(...),
            slot_id: str = Form(...),
            ref_id: str = Form(""),
            description: str = Form(""),
            file: UploadFile = UploadFile(...),
        ) -> JSONResponse:
            """Multipart image upload for ``kind=image`` slots.

            Companion to the WS ``upload_image`` message — used when the
            payload is too large for a comfortable WS frame (the JS
            client switches routes at 2MB). The flow is identical once
            the bytes land: ImageUploader re-encodes via Pillow, writes
            to ``{run_output_dir}/images/``, and updates state.

            Form fields:
              * ``slide_idx``    — 0-based slide index
              * ``slot_id``      — slot identifier
              * ``ref_id``       — optional client id for ack correlation
              * ``description``  — optional user-typed caption; blank
                triggers VLM auto-description (when enabled).
              * ``file``         — the image file

            Returns ``{ok, ref_id, new_path, error}`` so the client can
            react identically to the WS path.
            """
            orch = self._orchestrator
            if orch is None:
                return JSONResponse(
                    {"ok": False, "ref_id": ref_id, "error": "no orchestrator active"},
                    status_code=400,
                )
            # Resolve target by (slide_idx, slot_id) — images stage paths
            # are always ("slide", N, "slot", SLOT).
            target_path = ["slide", int(slide_idx), "slot", slot_id]
            target = self._resolve_target(target_path)
            if target is None:
                return JSONResponse(
                    {
                        "ok": False,
                        "ref_id": ref_id,
                        "error": f"no editable target for slide_idx={slide_idx} slot_id={slot_id}",
                    },
                    status_code=404,
                )
            raw = await file.read()
            result = await orch.apply_edit(
                target,
                "direct",
                {
                    "data": raw,
                    "source_ref": file.filename or "upload",
                    "description": description,
                },
            )
            if not result.ok:
                return JSONResponse(
                    {"ok": False, "ref_id": ref_id, "error": result.error},
                    status_code=400,
                )
            return JSONResponse(
                {
                    "ok": True,
                    "ref_id": ref_id,
                    "new_path": result.new_value,
                    "target_path": list(target.path),
                    # Carry Pillow-decoded dims so the slides-stage
                    # drag-drop flow can size the inserted <img> with
                    # the correct aspect ratio client-side. None for
                    # non-image editors — clients treat absence as
                    # "no dim hint" and fall back to a square default.
                    "width": result.width,
                    "height": result.height,
                    # Description that actually landed in state (user-
                    # supplied or VLM-generated). Lets the client update
                    # its display without a snapshot refetch.
                    "description": result.description,
                }
            )

        # Static mount MUST come after all explicit routes above so the
        # mount at "/" doesn't shadow /ws, /files, /artifact/*, /api/*.
        # html=True makes the bare "/" path resolve to index.html.
        app.mount(
            "/",
            StaticFiles(directory=str(self.static_dir), html=True),
            name="static-root",
        )

    async def _handle_client_message(self, ws: WebSocket, msg: Dict[str, Any]) -> None:
        """Dispatch one decoded client message.

        Routes:
          * ``approve_stage``   → release the active gate
          * ``request_edit``    → resolve target → orch.apply_edit → ack/reject
          * ``cancel_edit``     → cancel the in-progress LLM edit task
          * ``upload_image``    → base64-decode → ImageUploader → ack/reject
          * ``undo``            → orch.undo_last → ack/reject
          * ``chat_history``    → return per-target history for the chat panel
          * unknown             → ``_MalformedMessage`` (logged + replied)

        While an LLM edit task is running (``_active_edit_task`` is not
        done), every other mutating message type is rejected with
        ``edit_rejected`` so concurrent edits can't race against the
        in-flight LLM call. Read-only routes (``get_history`` /
        ``chat_history``) are still allowed so the UI can hydrate.

        Pipeline cancellation routes through Home → /api/reset → task.cancel()
        (works in any pipeline state), not through a WS message.
        """
        msg_type = msg.get("type")
        if msg_type == "cancel_edit":
            await self._handle_cancel_edit(ws, msg)
            return
        # Reject other mutating messages while an LLM edit is running.
        # The frontend also disables these affordances client-side; this
        # is the server-side backstop for any client that races.
        if (
            self._active_edit_task is not None
            and not self._active_edit_task.done()
            and msg_type in _EDIT_BLOCKING_TYPES
        ):
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=msg.get("ref_id", ""),
                    error="另一个修改正在进行中，请等待或取消后再试",
                ),
            )
            return
        if msg_type == "approve_stage":
            gate = self._active_gate
            if gate is None:
                await self._send(ws, ErrorMsg(message="no active pipeline to approve"))
                return
            gate.release("approve")
            return
        if msg_type == "request_edit":
            await self._handle_request_edit(ws, msg)
            return
        if msg_type == "upload_image":
            await self._handle_upload_image(ws, msg)
            return
        if msg_type == "undo":
            await self._handle_undo(ws, msg)
            return
        if msg_type == "get_history":
            await self._handle_get_history(ws, msg)
            return
        if msg_type == "revert_to":
            await self._handle_revert_to(ws, msg)
            return
        if msg_type == "unrevert":
            await self._handle_unrevert(ws, msg)
            return
        if msg_type == "delete_history_entry":
            await self._handle_delete_history_entry(ws, msg)
            return
        if msg_type == "chat_history":
            await self._handle_chat_history(ws, msg)
            return
        if msg_type == "regenerate_item":
            await self._handle_regenerate_item(ws, msg)
            return
        if msg_type == "dismiss_stale":
            await self._handle_dismiss_stale(ws, msg)
            return
        if msg_type == "add_slide":
            await self._handle_add_slide(ws, msg)
            return
        if msg_type == "delete_slide":
            await self._handle_delete_slide(ws, msg)
            return
        if msg_type == "rebalance_outline":
            await self._handle_rebalance_outline(ws, msg)
            return
        if msg_type is None:
            raise _MalformedMessage("client message missing 'type' field")
        raise _MalformedMessage(f"unknown client message type {msg_type!r}")

    async def _handle_cancel_edit(self, ws: WebSocket, msg: Dict[str, Any]) -> None:
        """Cancel the currently-running LLM edit task.

        ``ref_id`` MUST match the originating ``request_edit``'s ref_id.
        Mismatches are silently ignored (no error to client) — they
        happen in legitimate races: the edit finished between the user
        clicking Cancel and the message arriving, or two clients both
        attempted to cancel. Either way, there is nothing to cancel.

        Any connected client can cancel — the review UI's global lock
        means any reviewer seeing a stuck edit should be able to bail
        out, not just the originator.
        """
        ref_id = msg.get("ref_id", "")
        if (
            self._active_edit_task is None
            or self._active_edit_ref_id != ref_id
        ):
            return
        if not self._active_edit_task.done():
            self._active_edit_task.cancel()

    def _on_edit_done(self, task: asyncio.Task) -> None:
        """Done callback for the active edit task.

        Two responsibilities, in order:

          1. Consume any exception so asyncio doesn't log a
             "Task exception was never retrieved" warning. Both
             ``CancelledError`` (user-initiated cancel) and unexpected
             crashes are accounted for.

          2. Clear ``_active_edit_task`` / ``_active_edit_ref_id`` so
             the next ``request_edit`` can start. Done callbacks run
             synchronously on the event loop, so this assignment is
             race-free against the WS read loop's check in
             :meth:`_handle_client_message`.
        """
        if task.cancelled():
            pass  # User cancellation — expected path.
        else:
            exc = task.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                # Unhandled crash inside the edit task. The handler
                # already sent an ``edit_rejected`` for caught
                # exceptions, so this is something deeper (e.g.
                # AttributeError in our own bookkeeping). Surface to
                # stderr for debugging.
                import sys

                print(
                    f"review edit task crashed: {exc!r}",
                    file=sys.stderr,
                )
        self._active_edit_task = None
        self._active_edit_ref_id = None

    # ------------------------------------------------------------------
    # PR3 edit handlers
    # ------------------------------------------------------------------

    def _resolve_target(self, target_path: Any) -> Optional[Any]:
        """Look up an ``EditTarget`` by path in the active stage's snapshot.

        ``target_path`` arrives as a JSON list (the wire format). We
        compare against each ``editable_targets[i].path`` of the cached
        snapshot for the stage the gate is currently paused at (or, if
        not paused, the most recent snapshot for any stage that had
        the target — supports late edits after approve).

        Returns the ``EditTarget`` (a dataclass from review_gate) or
        None if no match. Path elements are compared after
        :func:`_normalize_path_elem` — int vs str mismatches from JSON
        round-trip (e.g. dict keys force-cast int→str) don't break
        the lookup.

        **Synthesis for new image slots**: if no snapshot declares a
        target at ``("slide", N, "slot", X)`` but X is a non-empty
        string, we synthesise an ``image`` EditTarget. This backs the
        slides-stage drag-drop feature: the user drops a brand-new
        local image, the client generates a ``user_*`` slot_id, and
        ImageUploader's ``_resolve_slot_payload`` creates the slot
        on the fly. Without synthesis the lookup here would 404 the
        request before the editor ever runs.
        """
        if not isinstance(target_path, list):
            return None
        target_tuple = tuple(_normalize_path_elem(x) for x in target_path)
        # Prefer the snapshot for the paused stage — that's where the
        # user is most likely editing. Fall back to scanning all
        # snapshots for stages that already ran.
        stages_to_check: List[str] = []
        gate = self._active_gate
        if gate is not None and gate.pending is not None:
            stages_to_check.append(gate.pending.stage)
        for stage_name in self._last_snapshots:
            if stage_name not in stages_to_check:
                stages_to_check.append(stage_name)
        for stage_name in stages_to_check:
            snap = self._last_snapshots.get(stage_name)
            if snap is None:
                continue
            for target in snap.editable_targets:
                snap_tuple = tuple(
                    _normalize_path_elem(x) for x in target.path
                )
                if snap_tuple == target_tuple:
                    return target

        # Synthesis fallback: brand-new image slot from drag-drop.
        # Path shape ("slide", int, "slot", non-empty-str) → image target.
        # The editor (ImageUploader) handles slot creation in state.
        if (
            len(target_tuple) == 4
            and target_tuple[0] == "slide"
            and isinstance(target_tuple[1], int)
            and target_tuple[2] == "slot"
            and isinstance(target_tuple[3], str)
            and target_tuple[3]
        ):
            from shuttleslide.agent.review.review_gate import EditTarget

            slide_idx = target_tuple[1]
            slot_id = target_tuple[3]
            # Always route to the ``images`` stage. Earlier code tried to
            # inherit the paused stage (typically ``slides`` for drag-drop),
            # but that stage owns ``state.slides[idx].slots["html"]`` —
            # ``_refresh_after_edit`` would refresh slides but never images,
            # so the new thumb didn't appear in the images stage until the
            # server restarted and rebuilt the snapshot from disk. The
            # ``images`` cascade already fans out to slides + rendered, so
            # nothing downstream is lost by pinning the stage here.
            # Stale propagation also reads the correct slice: ``images``
            # compares state.slide_images[idx][slotId] before/after, which
            # is what actually mutated. Routing as ``slides`` accidentally
            # short-circuited stale marking because the slide HTML didn't
            # change at upload time (the HTML commit fires later as a
            # separate edit from _finalizeDrop).
            return EditTarget(
                stage="images",
                path=target_tuple,
                kind="image",
                current_value="",
                meta={
                    "slide_idx": slide_idx,
                    "slot_id": slot_id,
                    "mime": "",
                    "payload_type": None,
                },
            )
        return None

    async def _handle_request_edit(self, ws: WebSocket, msg: Dict[str, Any]) -> None:
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        target_path = msg.get("target_path", [])
        mode = msg.get("mode", "llm")
        if orch is None:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error="no orchestrator available (server not in web-client mode)",
                ),
            )
            return
        target = self._resolve_target(target_path)
        if target is None:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=(
                        f"target path {list(target_path)} not found in any "
                        f"cached snapshot"
                    ),
                ),
            )
            return
        payload = msg.get("payload") or {}
        # LLM edits run in a background task so the WS read loop stays
        # free to receive ``cancel_edit``. Direct / image-upload edits
        # are synchronous (millisecond-scale) and stay inline — they
        # don't need cancellation and don't benefit from fire-and-track
        # overhead.
        if mode == "llm":
            coro = self._run_edit_with_cancellation(ws, target, mode, payload, ref_id)
            task = asyncio.create_task(coro)
            self._active_edit_task = task
            self._active_edit_ref_id = ref_id
            task.add_done_callback(self._on_edit_done)
            return
        result = await orch.apply_edit(target, mode, payload)
        if result.no_op:
            # Silent skip: editor reported new_value == current_value, so
            # the orchestrator pushed no undo entry and broadcast no
            # stage_complete. Withhold the EditAppliedMsg too — client
            # closes its edit toolbar locally and history stays clean.
            return
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=list(target.path),
                    new_preview=result.new_value or "",
                    diff=result.diff,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=result.error or "edit rejected",
                    kind=getattr(result, "kind", "error"),
                    suggested_stage=getattr(result, "suggested_stage", None),
                    guidance=getattr(result, "guidance", None),
                ),
            )

    async def _run_edit_with_cancellation(
        self,
        ws: WebSocket,
        target: Any,
        mode: str,
        payload: Dict[str, Any],
        ref_id: str,
    ) -> None:
        """Background task body for a cancellable LLM edit.

        Wraps ``orch.apply_edit`` with cancellation handling:

          * ``asyncio.CancelledError`` (from ``cancel_edit``) → broadcast
            ``EditCancelledMsg`` to every client, then re-raise so the
            task ends in cancelled state (``_on_edit_done`` checks this
            to skip the crash log).
          * Any other escaped ``Exception`` → reply ``edit_rejected``.
            ``orch.apply_edit`` already catches editor exceptions and
            returns ``EditResult(ok=False)``, so this only triggers on
            unexpected crashes from save / refresh / broadcast paths.

        ``EditCancelledMsg`` is broadcast (not unicasted to the
        requester) so a client that disconnected mid-edit and reconnected
        also clears its local "edit in progress" UI state. It is NOT
        added to ``_early_messages`` (the replay buffer) because it's a
        one-shot notification with no meaning after the moment passes —
        late joiners won't have entered the editing state anyway.
        """
        orch = self._orchestrator
        try:
            result = await orch.apply_edit(target, mode, payload)
        except asyncio.CancelledError:
            self._broadcast_now(msg_to_dict(EditCancelledMsg(ref_id=ref_id)))
            raise  # MUST re-raise so task.cancelled() == True in _on_edit_done
        except Exception as exc:
            await self._send(
                ws,
                EditRejectedMsg(ref_id=ref_id, error=f"edit crashed: {exc}"),
            )
            return
        if result.no_op:
            return
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=list(target.path),
                    new_preview=result.new_value or "",
                    diff=result.diff,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=result.error or "edit rejected",
                    kind=getattr(result, "kind", "error"),
                    suggested_stage=getattr(result, "suggested_stage", None),
                    guidance=getattr(result, "guidance", None),
                ),
            )

    async def _run_op_with_cancellation(
        self,
        ws: WebSocket,
        ref_id: str,
        op_coro: Any,
        target_path_for_ack: Any,
    ) -> None:
        """Background task body for cancellable structural operations.

        Mirrors :meth:`_run_edit_with_cancellation` but for operations
        that don't go through ``apply_edit`` — ``add_slide(mode="llm")``
        and ``rebalance_outline``. ``op_coro`` is the orchestrator
        coroutine already bound to its kwargs; it returns an
        :class:`EditResult`.

        ``target_path_for_ack`` is the path emitted in the
        ``EditAppliedMsg`` (always ``("outline",)`` for the structural
        ops, since outline is the stage whose snapshot changed).
        """
        try:
            result = await op_coro
        except asyncio.CancelledError:
            self._broadcast_now(msg_to_dict(EditCancelledMsg(ref_id=ref_id)))
            raise  # MUST re-raise so task.cancelled() == True in _on_edit_done
        except Exception as exc:
            await self._send(
                ws,
                EditRejectedMsg(ref_id=ref_id, error=f"op crashed: {exc}"),
            )
            return
        if result.no_op:
            return
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=list(target_path_for_ack),
                    new_preview=result.new_value or "",
                    diff=result.diff,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=result.error or "op rejected",
                ),
            )


    async def _handle_upload_image(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        import base64

        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        target_path = msg.get("target_path", [])
        if orch is None:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error="no orchestrator available (server not in web-client mode)",
                ),
            )
            return
        target = self._resolve_target(target_path)
        if target is None:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=f"target path {list(target_path)} not found",
                ),
            )
            return
        data_b64 = msg.get("data_b64", "") or ""
        try:
            data = base64.b64decode(data_b64, validate=False)
        except Exception as exc:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=f"could not decode base64 image data: {exc}",
                ),
            )
            return
        result = await orch.apply_edit(
            target,
            "direct",
            {
                "data": data,
                "source_ref": msg.get("filename", "upload"),
                "description": msg.get("description", ""),
            },
        )
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=list(target.path),
                    new_preview=result.new_value or "",
                    width=result.width,
                    height=result.height,
                    description=result.description,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(ref_id=ref_id, error=result.error or "upload rejected"),
            )

    async def _handle_undo(self, ws: WebSocket, msg: Dict[str, Any]) -> None:
        orch = self._orchestrator
        target_path = msg.get("target_path", []) or []
        ref_id = msg.get("ref_id", "")
        if orch is None:
            await self._send(
                ws, ErrorMsg(message="no orchestrator available for undo")
            )
            return
        result = await orch.undo_last(tuple(target_path))
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=list(target_path),
                    new_preview=result.new_value or "",
                    diff=result.diff,
                ),
            )
        else:
            # Undo with empty stack is a soft warning, not a hard error.
            await self._send(
                ws,
                EditRejectedMsg(ref_id=ref_id, error=result.error or "nothing to undo"),
            )

    async def _handle_get_history(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Unicast the full edit-history snapshot to the requester.

        The orchestrator already broadcasts ``HistorySnapshotMsg`` after
        every edit / undo / revert, so this is only needed for the
        late-joining client (or a tab refresh) that missed those pushes
        and needs to populate its History panel from scratch.
        """
        orch = self._orchestrator
        if orch is None:
            await self._send(
                ws,
                HistorySnapshotMsg(entries=[], timestamp=time.time()),
            )
            return
        entries = orch.get_history()
        await self._send(
            ws,
            HistorySnapshotMsg(entries=entries, timestamp=time.time()),
        )

    async def _handle_revert_to(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Apply a history entry's old_value, leaving the card in place.

        ``entry_idx`` is the index into ``UndoStack.entries()`` (newest
        = 0). The entry itself is NOT removed — the client renders Undo /
        Commit affordances on the same card (tracked client-side).
        ``Undo`` re-applies the post-edit value via ``unrevert``;
        ``Commit`` permanently removes the entry via
        ``delete_history_entry``.
        """
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        entry_idx = int(msg.get("entry_idx", -1))
        if orch is None:
            await self._send(
                ws, ErrorMsg(message="no orchestrator available for revert")
            )
            return
        result = await orch.revert_to(entry_idx)
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=[],
                    new_preview=result.new_value or "",
                    diff=result.diff,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=result.error or "revert failed",
                ),
            )

    async def _handle_unrevert(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Re-apply a reverted entry's new_value (Undo on pending card)."""
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        entry_idx = int(msg.get("entry_idx", -1))
        if orch is None:
            await self._send(
                ws, ErrorMsg(message="no orchestrator available for unrevert")
            )
            return
        result = await orch.unrevert(entry_idx)
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=[],
                    new_preview=result.new_value or "",
                    diff=result.diff,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=result.error or "unrevert failed",
                ),
            )

    async def _handle_delete_history_entry(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Permanently remove a history entry (Commit on pending card).

        No value change — the live state is already at old_value from
        the prior revert_to. Just mutates the stack and broadcasts the
        updated history snapshot.
        """
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        entry_idx = int(msg.get("entry_idx", -1))
        if orch is None:
            await self._send(
                ws, ErrorMsg(message="no orchestrator available for delete")
            )
            return
        ok = orch.delete_history_entry(entry_idx)
        if ok:
            # No EditAppliedMsg — nothing changed in the preview. Push
            # a fresh history snapshot so the client removes the card.
            self.emit_history_snapshot(orch.get_history())
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=f"history entry {entry_idx} out of range",
                ),
            )

    async def _handle_chat_history(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Return per-target chat history directly to the requester.

        Single-response to the requester only — used by the frontend's
        ``refreshChatHistoryForActive`` on target focus switch. Proactive
        post-edit pushes go through ``emit_chat_history`` instead.
        """
        orch = self._orchestrator
        target_path = tuple(msg.get("target_path", []) or [])
        ref_id = msg.get("ref_id", "")
        if orch is None:
            return
        history = orch.get_chat_history(target_path)
        # Translate SessionStore's {role, content} (OpenAI chat-format
        # convention) to the wire format the frontend expects: {role, body}.
        # Frontend renderChatHistory reads entry.body — without this
        # translation server-pushed history shows up blank.
        await self._send(
            ws,
            ChatHistoryMsg(
                ref_id=ref_id,
                target_path=list(target_path),
                messages=[
                    {"role": m.get("role", ""), "body": m.get("content", "")}
                    for m in history
                ],
                timestamp=time.time(),
            ),
        )

    # ------------------------------------------------------------------
    # Stale-mark handlers (regenerate / dismiss)
    # ------------------------------------------------------------------

    async def _handle_regenerate_item(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Dispatch a per-item regenerate through the orchestrator.

        ``stage`` + ``target_id`` identify the stale mark. ``mode`` is
        ``"incremental"`` (default — preserve user edits) or
        ``"fresh"`` (regenerate from scratch).

        On success the orchestrator has already broadcast
        ``ItemRegeneratedMsg`` + ``StaleMarksUpdatedMsg`` via the
        Broadcaster hooks (emit_item_regenerated / emit_stale_marks),
        so this handler only sends the per-client ack to the
        requesting socket. Late-connecting clients replay the
        broadcast via ``_early_messages``.
        """
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        stage = msg.get("stage", "")
        target_id = msg.get("target_id", "")
        mode = msg.get("mode", "incremental")
        if mode not in ("incremental", "fresh"):
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=f"invalid mode {mode!r}; expected 'incremental' or 'fresh'",
                ),
            )
            return
        if orch is None:
            await self._send(
                ws, ErrorMsg(message="no orchestrator available for regenerate")
            )
            return
        result = await orch.regenerate_item(
            stage=stage,
            target_id=target_id,
            mode=mode,
            ref_id=ref_id,
        )
        if result.ok:
            # The broadcaster already pushed ItemRegeneratedMsg to all
            # clients; send a per-requester ack so the originating
            # client can resolve its ref_id affordance.
            await self._send(
                ws,
                ItemRegeneratedMsg(
                    ref_id=ref_id,
                    stage=stage,
                    target_id=target_id,
                    snapshot=result.snapshot,
                    remaining_marks=result.remaining_marks,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=result.error or "regenerate failed",
                ),
            )

    async def _handle_dismiss_stale(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Dismiss a stale mark without regenerating.

        ``target_id="all"`` clears every mark on the stage; otherwise a
        single ``(stage, target_id)`` mark is removed. The orchestrator
        broadcasts ``StaleMarksUpdatedMsg`` to all clients on success,
        so badges disappear in real time.
        """
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        stage = msg.get("stage", "")
        target_id = msg.get("target_id", "")
        if orch is None:
            await self._send(
                ws, ErrorMsg(message="no orchestrator available for dismiss")
            )
            return
        removed = await orch.dismiss_stale(
            stage, target_id, ref_id=ref_id
        )
        if removed:
            # Orchestrator already broadcast stale marks; send ack.
            await ws.send_json({
                "type": "stale_dismissed",
                "ref_id": ref_id,
                "stage": stage,
                "target_id": target_id,
                "timestamp": time.time(),
            })
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=f"no stale mark for {stage}:{target_id}",
                ),
            )

    # ------------------------------------------------------------------
    # Add / Delete / Rebalance slide handlers
    # ------------------------------------------------------------------

    async def _handle_add_slide(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Dispatch :meth:`orch.add_slide` to insert a new outline entry.

        ``mode="manual"`` runs synchronously (no LLM, just validation +
        state mutation + background generation kick-off). ``mode="llm"``
        runs as a cancellable background task — the LLM draft takes a
        few seconds and the user may want to cancel.
        """
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        index = msg.get("index", -1)
        mode = msg.get("mode", "manual")
        payload = msg.get("payload") or {}
        if mode not in ("llm", "manual"):
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=f"invalid mode {mode!r}; expected 'llm' or 'manual'",
                ),
            )
            return
        if orch is None:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error="no orchestrator available (server not in web-client mode)",
                ),
            )
            return
        op_coro = orch.add_slide(
            index=index, mode=mode, payload=payload, ref_id=ref_id
        )
        if mode == "llm":
            coro = self._run_op_with_cancellation(
                ws, ref_id, op_coro, ("outline",)
            )
            task = asyncio.create_task(coro)
            self._active_edit_task = task
            self._active_edit_ref_id = ref_id
            task.add_done_callback(self._on_edit_done)
            return
        # manual mode: synchronous
        try:
            result = await op_coro
        except Exception as exc:
            await self._send(
                ws,
                EditRejectedMsg(ref_id=ref_id, error=f"add_slide crashed: {exc}"),
            )
            return
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=["outline"],
                    new_preview=result.new_value or "",
                    diff=result.diff,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=result.error or "add_slide failed",
                ),
            )

    async def _handle_delete_slide(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Dispatch :meth:`orch.delete_slide` to remove an outline entry.

        Always synchronous (microsecond-scale) — no LLM call, no
        cancellation needed.
        """
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        index = msg.get("index", -1)
        if orch is None:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error="no orchestrator available (server not in web-client mode)",
                ),
            )
            return
        try:
            result = await orch.delete_slide(index=index, ref_id=ref_id)
        except Exception as exc:
            await self._send(
                ws,
                EditRejectedMsg(ref_id=ref_id, error=f"delete_slide crashed: {exc}"),
            )
            return
        if result.ok:
            await self._send(
                ws,
                EditAppliedMsg(
                    ref_id=ref_id,
                    target_path=["outline"],
                    new_preview=result.new_value or "",
                    diff=result.diff,
                ),
            )
        else:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error=result.error or "delete_slide failed",
                ),
            )

    async def _handle_rebalance_outline(
        self, ws: WebSocket, msg: Dict[str, Any]
    ) -> None:
        """Dispatch :meth:`orch.rebalance_outline` to LLM-rewrite the outline.

        Runs as a cancellable background task — the LLM rewrite can take
        10-30s and the user may want to bail out.
        """
        orch = self._orchestrator
        ref_id = msg.get("ref_id", "")
        user_hint = msg.get("user_hint", "")
        if orch is None:
            await self._send(
                ws,
                EditRejectedMsg(
                    ref_id=ref_id,
                    error="no orchestrator available (server not in web-client mode)",
                ),
            )
            return
        op_coro = orch.rebalance_outline(user_hint=user_hint, ref_id=ref_id)
        coro = self._run_op_with_cancellation(
            ws, ref_id, op_coro, ("outline",)
        )
        task = asyncio.create_task(coro)
        self._active_edit_task = task
        self._active_edit_ref_id = ref_id
        task.add_done_callback(self._on_edit_done)

    async def _send(self, ws: WebSocket, message: Any) -> None:
        """Serialise a dataclass message and send to one client."""
        await ws.send_json(msg_to_dict(message))

    # ------------------------------------------------------------------
    # Lifecycle — production (thread) and test (in-loop)
    # ------------------------------------------------------------------

    def start_in_thread(self) -> None:
        """Launch uvicorn on a daemon thread. Returns immediately.

        Blocks until the server socket is bound (or up to ~5s) so the
        caller can immediately ``webbrowser.open(self.url)`` without
        racing the bind.
        """
        if self._thread is not None:
            raise RuntimeError("server already started in a thread")
        self._started_event.clear()
        self._thread = threading.Thread(
            target=self._thread_main, name="shuttleslide-review-server", daemon=True
        )
        self._thread.start()
        # Wait for serve() to signal that the loop is captured and the
        # socket is about to bind. Bounded so a broken uvicorn doesn't
        # hang the caller forever.
        if not self._started_event.wait(timeout=5.0):
            raise RuntimeError("review server failed to start within 5s")

    def _thread_main(self) -> None:
        try:
            asyncio.run(self.serve())
        except Exception:  # pragma: no cover - defensive
            # The daemon thread should never raise; if it does, the
            # main thread will notice via failed health-checks.
            import traceback

            traceback.print_exc()

    async def serve(self) -> None:
        """Run uvicorn on the current loop until ``stop()`` is called.

        Use directly in tests (same-loop mode). For production use
        ``start_in_thread()`` instead.
        """
        import uvicorn

        self._server_loop = asyncio.get_running_loop()
        from shuttleslide.agent.asyncio_diag import install_noise_filter

        install_noise_filter(self._server_loop)
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="warning",
            # ``proxy_headers`` and ``access_log`` off to keep test output
            # and CLI noise clean.
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        # Signal start_in_thread that we're about to enter serve().
        self._started_event.set()
        try:
            await self._server.serve()
        finally:
            self._server_loop = None

    async def stop(self) -> None:
        """Signal the uvicorn server to exit (test-mode, same loop)."""
        if self._server is not None:
            self._server.should_exit = True
            # Give uvicorn a moment to drain in-flight requests.
            # 100ms is generous for our use case (only WS + tiny artifacts).
            await asyncio.sleep(0.1)

    def shutdown(self) -> None:
        """Sync shutdown — call from any thread.

        Joins the server thread with a 5s timeout. If the join times out
        the daemon thread is abandoned (will die when the process
        exits).
        """
        if self._thread is None:
            return
        loop = self._server_loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(asyncio.create_task, self.stop())
            except RuntimeError:
                # Loop already closed — nothing to do.
                pass
        self._thread.join(timeout=5.0)
        self._thread = None
        self._server = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def _resolve_image_path(self, path_str: str) -> Optional[Path]:
        """Resolve an image payload's ``path`` field to a Path, or None.

        Abs paths pass through unchanged (the stub tests use ``tmp_path``
        abs paths). Relative paths are joined to the per-run output dir
        (web-client mode) when set, falling back to ``self.output_dir``
        (legacy/test mode). image_acquirer writes paths relative to
        ``AgentConfig.output_dir``; in web-client mode that's the run
        subdir, not the base. Returns None if ``path_str`` is empty so
        the caller can produce a clean 404 rather than a FileNotFoundError.
        """
        if not path_str:
            return None
        candidate = Path(path_str)
        if not candidate.is_absolute():
            base = self._run_output_dir or self.output_dir
            if base is not None:
                candidate = base / path_str
        return candidate

    def _theme_from_snapshot(self) -> ThemeDef:
        """Build a ``ThemeDef`` from the cached 'theme' snapshot, or default.

        The snapshot's ``state_view['theme']`` is the plain dict from
        ``state.theme``. Field names match ``ThemeDef`` one-for-one (see
        ``_snapshot_theme`` in snapshots.py and ``_state_to_presentation``
        in orchestrator.py). Returns a default ThemeDef when no theme
        snapshot is available yet (e.g. preview opened before the theme
        stage completes).
        """
        snap = self._last_snapshots.get("theme")
        if snap is None:
            return ThemeDef()
        theme_dict = snap.state_view.get("theme") or {}
        # Defensive filter: snapshots are JSON-safe dicts but LLM output
        # may carry extra keys that ThemeDef.__init__ would reject.
        known = {k: v for k, v in theme_dict.items()
                 if k in ThemeDef.__dataclass_fields__}
        return ThemeDef(**known)

    def _wrap_slide_html(self, html: str, slide_idx: int) -> str:
        """Render a slide fragment using the production SlideHTMLRenderer.

        Builds a ``SlideDSL`` from the fragment and a ``ThemeDef`` from the
        cached theme snapshot, then delegates to
        ``SlideHTMLRenderer.render_slide()``. This guarantees the preview
        HTML matches the saved file byte-for-byte for the ``.ppt-slide``
        container, theme background, and canvas dimensions — the two paths
        previously diverged because preview used a hardcoded template that
        skipped the ``free_form.html.j2`` wrapper entirely.

        Canvas dimensions come from ``self._run_canvas_emu`` (captured at
        POST /api/start from the active AgentConfig). Falls back to the
        legacy 1280×720 default when no run is active (legacy/test mode)
        — matching the historical behaviour and keeping stub tests green.

        Injects ``<base href="...">`` after ``<head>`` so relative paths in
        the fragment (``images/...``, ``svgs/...``) resolve to the
        StaticFiles mount:

        - **Web-client mode** (per-run subdir): ``/files/<run_dirname>/``
          so a fragment path ``images/x.jpg`` resolves to the active
          run's images folder under the base output_dir mount.
        - **Legacy/test mode**: ``/files/`` (mount root = output_dir).
        - **No output_dir**: ``/`` (relative URLs will 404; stub tests
          don't exercise iframe rendering).
        """
        theme = self._theme_from_snapshot()
        slide = SlideDSL(layout="free_form", slots={"html": html})
        if self._run_canvas_emu is not None:
            canvas_w_emu, canvas_h_emu = self._run_canvas_emu
        else:
            # Legacy/test mode or pre-start: use the historical default.
            # Matches AgentConfig's dataclass defaults so stub tests that
            # don't exercise canvas dims still render at 1280×720.
            canvas_w_emu, canvas_h_emu = 12192000, 6858000
        rendered = self._renderer.render_slide(
            slide,
            theme,
            title=f"Slide {slide_idx + 1}",  # 1-indexed to match saved 1.html, 2.html, ...
            canvas_width_emu=canvas_w_emu,
            canvas_height_emu=canvas_h_emu,
        )
        base_href = self._compute_base_href()
        return _inject_base_href(rendered, base_href)

    def _compute_base_href(self) -> str:
        """Compute the ``<base href>`` for served slide HTML.

        See ``_wrap_slide_html`` for the resolution rules.
        """
        if self.output_dir is None or not self.output_dir.is_dir():
            return "/"
        if self._run_output_dir is not None:
            try:
                rel = self._run_output_dir.relative_to(self.output_dir)
                return f"/files/{rel.as_posix()}/"
            except ValueError:
                # run_output_dir isn't under output_dir — fall back to
                # the bare mount root and hope for the best.
                return "/files/"
        return "/files/"

    # ------------------------------------------------------------------
    # Web-client mode helpers — config form → orchestrator launch
    # ------------------------------------------------------------------

    def _coerce_config_field(self, key: str, raw: Any) -> Any:
        """Coerce a raw value to the type declared in _ALLOWED_CONFIG_FIELDS.

        Shared between the payload-parsing loop (form values) and the
        effective_defaults override (env-var values from os.environ).
        Without this, env-driven bool flags landed in AgentConfig as the
        STRING ``"true"`` rather than ``True`` — dataclasses don't enforce
        types at assignment, so the bad value silently flowed through to
        ``if self.disable_required_tool_choice`` checks downstream.
        """
        expected = self._ALLOWED_CONFIG_FIELDS.get(key)
        if expected is None:
            # Field isn't in the allowed set — return as-is and let the
            # caller decide what to do (payload loop drops it; override
            # loop ignores it via the `if field in _ALLOWED_CONFIG_FIELDS`
            # guard).
            return raw
        if expected is bool:
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in ("1", "true", "yes", "on")
        if expected is int:
            return int(raw)
        if expected is float:
            return float(raw)
        return str(raw)

    # Known AgentConfig scalar fields the form is allowed to set.
    # Excludes output_dir (server-controlled), on_llm_response (callback),
    # canvas_*_emu (derived from canvas_aspect_ratio in _extract_config_kwargs),
    # and the bing base URL (rarely overridden). Anything not in this set
    # is silently dropped.
    _ALLOWED_CONFIG_FIELDS: Dict[str, type] = {
        "api_base": str,
        "api_key": str,
        "model": str,
        "disable_required_tool_choice": bool,
        "temperature": float,
        "max_tokens": int,
        "svg_generator_max_tokens": int,
        "topic": str,
        "style_hint": str,
        "target_slide_count": int,
        "max_tool_iterations": int,
        "image_search_provider": str,
        "image_search_api_key": str,
        "vlm_api_base": str,
        "vlm_api_key": str,
        "vlm_model": str,
        "enable_vlm_verification": bool,
        # Canvas aspect-ratio string ("9:16", "1:1", ...). When set,
        # _extract_config_kwargs derives canvas_*_emu from it via
        # aspect_ratio_to_dimensions and threads both through to
        # AgentConfig. None / empty → AgentConfig defaults (16:9).
        "canvas_aspect_ratio": str,
    }

    # Fields whose values must never be sent to the browser via
    # ``/api/defaults``. The UI shows a mask placeholder instead; the
    # real value is injected server-side at POST /api/start time from
    # ``effective_defaults``. The field *names* still appear in the
    # ``locked`` array so the UI knows to render them readonly.
    _SENSITIVE_FIELDS: frozenset = frozenset({"api_key", "vlm_api_key"})

    def _extract_config_kwargs(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Build AgentConfig kwargs from the JSON form payload.

        Handles the three input modes (direct topic / HTML upload / text
        upload) by populating ``topic`` from whichever source was sent.
        Coerces numeric and boolean fields from their string form
        (form fields always arrive as strings; JSON POST sends proper
        types but we coerce defensively).

        Returns a dict of kwargs suitable for ``AgentConfig(**kwargs)``.
        Caller is responsible for calling ``config.validate()``.
        """
        out: Dict[str, Any] = {}
        for key, expected in self._ALLOWED_CONFIG_FIELDS.items():
            if key not in payload:
                continue
            raw = payload[key]
            if raw is None or raw == "":
                # Skip empty strings so AgentConfig defaults kick in
                # (otherwise "" would shadow e.g. the default style_hint).
                continue
            try:
                out[key] = self._coerce_config_field(key, raw)
            except (ValueError, TypeError) as e:
                raise ValueError(f"invalid value for {key}: {raw!r} ({e})")

        # Topic source — exactly one of:
        #   topic            (direct textarea)
        #   html_file_b64    (HTML upload, trafilatura-extracted)
        #   text_file_b64    (Markdown / text upload, verbatim)
        #   load_state_from  (dirname of a previous run — state's topic wins)
        topic_sources = [
            k for k in ("topic", "html_file_b64", "text_file_b64", "load_state_from")
            if payload.get(k)
        ]
        if len(topic_sources) > 1:
            raise ValueError(
                f"only one of topic / html_file_b64 / text_file_b64 / load_state_from "
                f"may be set; got {topic_sources}"
            )
        if not topic_sources:
            raise ValueError(
                "no topic source provided; pass one of: "
                "topic, html_file_b64, text_file_b64, load_state_from"
            )

        if "topic" in topic_sources:
            # Already populated above by the generic loop.
            pass
        elif "html_file_b64" in topic_sources:
            import base64
            from shuttleslide.agent.review.input_extract import extract_topic_from_html
            html_bytes = base64.b64decode(payload["html_file_b64"])
            html_str = html_bytes.decode("utf-8", errors="replace")
            out["topic"] = extract_topic_from_html(html_str)
        elif "text_file_b64" in topic_sources:
            import base64
            from shuttleslide.agent.review.input_extract import extract_topic_from_text
            text_bytes = base64.b64decode(payload["text_file_b64"])
            text_str = text_bytes.decode("utf-8", errors="replace")
            out["topic"] = extract_topic_from_text(text_str)
        elif "load_state_from" in topic_sources:
            # Resolve the run directory + state file, then populate
            # topic / style_hint / target_count from the saved state.
            # The orchestrator's load logic will overwrite state.topic
            # with config.topic — so we copy state's value into the config
            # to make the override a no-op (i.e. the loaded state wins).
            # Stash the dirname via private key; api_start pops it before
            # constructing AgentConfig (AgentConfig doesn't accept this kwarg).
            run_dirname = str(payload["load_state_from"])
            if not _RUN_DIRNAME_RE.match(run_dirname):
                raise ValueError(f"invalid load_state_from: {run_dirname!r}")
            if self.output_dir is None:
                raise ValueError("server has no output_dir; cannot load state")
            state_file = self.output_dir / run_dirname / "agent_state.json"
            if not state_file.exists():
                raise ValueError(f"state file not found: {state_file}")
            try:
                state_data = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                raise ValueError(f"failed to read state file: {e}")
            out["topic"] = state_data.get("topic", "") or ""
            if state_data.get("style_hint"):
                out["style_hint"] = state_data["style_hint"]
            if state_data.get("target_count") is not None:
                out["target_slide_count"] = state_data["target_count"]
            out["__load_state_from__"] = run_dirname

        # User-uploaded image library (homepage "Image assets" fieldset).
        # Each entry arrives as base64; we decode, Pillow re-encode, and
        # stage to a temp dir. api_start moves the staged files into the
        # run dir once _make_run_dir succeeds (the run dir isn't known
        # here). Descriptions: user input wins; blanks fall back to the
        # VLM describer when enable_vlm_description=True. The private
        # __user_image_staging__ key is popped by api_start before
        # AgentConfig construction — same pattern as __load_state_from__.
        user_images_raw = payload.get("user_images")
        if user_images_raw:
            staging_dir = tempfile.mkdtemp(prefix="shuttleslide_userimg_")
            try:
                library = self._process_user_images_payload(
                    user_images_raw, staging_dir, out,
                )
            except Exception as exc:
                # Failed mid-processing — clean up the staging dir so we
                # don't leak temp files. Surface the error as a 400.
                shutil.rmtree(staging_dir, ignore_errors=True)
                raise ValueError(f"user_images processing failed: {exc}")
            if library:
                out["user_image_library"] = library
                out["__user_image_staging__"] = staging_dir
            else:
                # All entries rejected — clean up and proceed as if no
                # uploads were sent.
                shutil.rmtree(staging_dir, ignore_errors=True)

        # Canvas aspect-ratio → EMU derivation. Only honoured when the
        # server was started with --canvas (canvas_mode=True); without
        # --canvas the field is dropped silently so a stale localStorage
        # value restored by the form (data-persist="local" on the ratio
        # radios) can't leak into a non-canvas run, which must always
        # use AgentConfig's 16:9 default. The front-end also disables
        # the radios (see applyCanvasModePicker / disableCanvasRatioPicker
        # in app.js); this guard is the server-side backstop.
        if not self.canvas_mode:
            out.pop("canvas_aspect_ratio", None)
        ratio_str = out.get("canvas_aspect_ratio")
        if ratio_str:
            from shuttleslide.agent.geometry import aspect_ratio_to_dimensions
            try:
                w_emu, h_emu = aspect_ratio_to_dimensions(ratio_str)
            except ValueError as exc:
                raise ValueError(f"invalid canvas_aspect_ratio: {exc}")
            out["canvas_width_emu"] = w_emu
            out["canvas_height_emu"] = h_emu

        # Enforce locked credential fields server-side, overriding
        # whatever the form sent. UI marks these readonly (so the right
        # value submits), but a user with devtools can still bypass —
        # server is the source of truth. Topic / style / counts are
        # never locked (not in effective_defaults).
        #
        # Coerce via the same type map used for payload values above:
        # effective_defaults values come from os.environ (always strings),
        # so without coercion bool fields would be the STRING "true" rather
        # than True. AgentConfig's dataclass doesn't enforce types at
        # assignment, so the string slipped through and broke the
        # `if self.disable_required_tool_choice` check in LLMClient.
        for field, value in self.effective_defaults.items():
            if field in self._ALLOWED_CONFIG_FIELDS:
                out[field] = self._coerce_config_field(field, value)

        return out

    # Hard cap on upload size per file (mirrors editors.image_uploader).
    _USER_IMAGE_MAX_BYTES = 10 * 1024 * 1024
    # Accepted upload MIME types. Matches the <input accept="..."> list
    # in index.html — kept here so the server remains authoritative.
    _ACCEPTED_USER_IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")

    def _process_user_images_payload(
        self,
        user_images_raw: Any,
        staging_dir: str,
        cfg_out: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """Decode + Pillow-reencode the homepage image-library payload.

        Called synchronously from ``_extract_config_kwargs`` (which is
        sync). Writes each accepted file to ``staging_dir`` and returns
        the library list (without VLM description resolution — that runs
        in api_start, which is async and can await the VLM client). The
        ``cfg_out`` dict is the in-progress AgentConfig kwargs; we read
        VLM creds from it to decide whether the post-decode VLM step
        will be a no-op (no creds ⇒ we can skip queueing the work).

        Each entry in ``user_images_raw`` must be a dict with
        ``filename`` / ``mime`` / ``data_b64`` and optional
        ``description``. Invalid entries are skipped with a stderr
        warning rather than rejecting the whole batch — one bad file
        shouldn't sink nine good ones.
        """
        if not isinstance(user_images_raw, list):
            raise ValueError("user_images must be an array")

        try:
            from PIL import Image
        except ImportError as exc:
            raise ValueError(
                f"Pillow is required for image uploads ({exc.name})"
            )

        # Local imports — both helpers are module-level functions in
        # the review editors package.
        from shuttleslide.agent.review.editors.image_uploader import (
            _normalize_image_format,
        )

        library: List[Dict[str, Any]] = []
        staging_path = Path(staging_dir)
        for i, entry in enumerate(user_images_raw):
            if not isinstance(entry, dict):
                # Non-dict entry is a client bug; surface and skip.
                print(
                    f"[shuttleslide] user_images[{i}] not a dict — skipped",
                    file=sys.stderr,
                )
                continue
            filename = str(entry.get("filename") or "").strip()
            mime = str(entry.get("mime") or "").strip().lower()
            data_b64 = entry.get("data_b64")
            description = entry.get("description")
            if not (filename and mime and isinstance(data_b64, str)):
                print(
                    f"[shuttleslide] user_images[{i}] ({filename or '?'}) "
                    f"missing filename/mime/data_b64 — skipped",
                    file=sys.stderr,
                )
                continue
            if mime not in self._ACCEPTED_USER_IMAGE_MIMES:
                print(
                    f"[shuttleslide] user_images[{i}] ({filename}) mime "
                    f"{mime!r} not in {self._ACCEPTED_USER_IMAGE_MIMES} — skipped",
                    file=sys.stderr,
                )
                continue

            try:
                raw_bytes = base64.b64decode(data_b64, validate=False)
            except Exception as exc:
                print(
                    f"[shuttleslide] user_images[{i}] ({filename}) base64 "
                    f"decode failed: {exc} — skipped",
                    file=sys.stderr,
                )
                continue
            if len(raw_bytes) > self._USER_IMAGE_MAX_BYTES:
                print(
                    f"[shuttleslide] user_images[{i}] ({filename}) is "
                    f"{len(raw_bytes)} bytes; max "
                    f"{self._USER_IMAGE_MAX_BYTES} — skipped",
                    file=sys.stderr,
                )
                continue

            # Pillow re-encode: strips EXIF / polyglot payloads and
            # normalises format (PNG stays PNG; webp / others → JPEG).
            try:
                with Image.open(io.BytesIO(raw_bytes)) as img:
                    img.load()
                    pil_format = img.format or "PNG"
                    reencoded, canonical_mime = _normalize_image_format(
                        img, pil_format
                    )
            except Exception as exc:
                print(
                    f"[shuttleslide] user_images[{i}] ({filename}) could "
                    f"not be decoded as an image: {exc} — skipped",
                    file=sys.stderr,
                )
                continue

            # Stable image_id + on-disk name. uuid4 hex truncated to 12
            # chars is plenty for collision avoidance within one deck.
            image_id = uuid.uuid4().hex[:12]
            ext = "png" if canonical_mime == "image/png" else "jpg"
            staged_name = f"{image_id}.{ext}"
            (staging_path / staged_name).write_bytes(reencoded)

            if not isinstance(description, str):
                description = None
            library.append({
                "image_id": image_id,
                # Absolute path to the staging file. api_start rewrites
                # this to a run_dir-relative path once the run dir exists.
                "path": str(staging_path / staged_name),
                "description": (description or "").strip(),
                "mime": canonical_mime,
                "original_filename": filename,
            })

        return library

    async def _autofill_user_image_descriptions(self, config) -> None:
        """Fill blank descriptions in config.user_image_library via VLM.

        Mirrors editors.image_uploader._resolve_description's logic but
        runs across all library entries in one pass. User-supplied
        descriptions are preserved verbatim; blanks are sent to the VLM
        describer when ``enable_vlm_description`` is True and a VLM
        endpoint is configured. Fail-open: any error (or no VLM creds)
        leaves the description as "".

        Mutates ``config.user_image_library[i]["description"]`` in place.
        """
        if not config.user_image_library:
            return
        if not getattr(config, "enable_vlm_description", True):
            return

        # Lazy import — keeps the server importable when the editors
        # package or image_sources subpackage isn't on the path.
        from shuttleslide.agent.review.editors.image_uploader import (
            _build_vlm_client,
            _resolve_description,
        )

        blanks = [
            (i, entry)
            for i, entry in enumerate(config.user_image_library)
            if not (entry.get("description") or "").strip()
        ]
        if not blanks:
            return

        for i, entry in blanks:
            path = entry.get("path", "")
            if not path:
                continue
            # Resolve to an absolute path. By the time this method
            # runs, api_start has already migrated staging paths to
            # run_dir-relative paths, so we resolve against output_dir.
            p = Path(path)
            if not p.is_absolute():
                p = Path(config.output_dir) / path
            if not p.exists():
                continue
            try:
                image_bytes = p.read_bytes()
            except OSError as exc:
                print(
                    f"[shuttleslide] failed to read user image "
                    f"{entry.get('image_id')} for VLM describe: {exc}",
                    file=sys.stderr,
                )
                continue
            mime = entry.get("mime") or "image/jpeg"
            try:
                desc, described_by = await _resolve_description(
                    None,  # no user description — that's the whole point
                    image_bytes,
                    mime,
                    slide_idx=-1,  # not yet assigned to a slide
                    config=config,
                )
            except Exception as exc:
                print(
                    f"[shuttleslide] VLM describe for user image "
                    f"{entry.get('image_id')} raised: {exc}",
                    file=sys.stderr,
                )
                continue
            if desc:
                entry["description"] = desc

    def _make_run_dir(self) -> Path:
        """Create a timestamped subdirectory under output_dir for this run.

        Web-client mode only. Each POST /api/start gets a fresh dir so
        re-runs don't overwrite previous outputs. The directory name
        also serves as the URL path component under /files/ (see
        ``_compute_base_href``).
        """
        import datetime
        if self.output_dir is None:
            # Shouldn't happen — POST /api/start requires output_dir to
            # mount /files/. Defensive fallback: cwd/tmp/web_review.
            base = Path.cwd() / "tmp" / "web_review"
            base.mkdir(parents=True, exist_ok=True)
        else:
            base = self.output_dir
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        run_dir = base / f"run_{ts}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    async def _run_pipeline(
        self,
        config: Any,
        load_state_on_start: bool = False,
    ) -> None:
        """Build an InteractiveOrchestrator on the server loop and run it.

        Wrapped in try/except so failures (bad API key, network down,
        LLM-side errors) land in ``_pipeline_error`` and surface via
        ``emit_error(fatal=True)`` rather than crashing the task
        silently. ``emit_pipeline_state`` transitions drive the UI.

        ``load_state_on_start=True`` makes the orchestrator skip all LLM
        calls and re-emit cached stage snapshots to the UI — the "load a
        previous run" path. ``state_cache_path`` must point at an
        existing ``agent_state.json`` for this to work.
        """
        from shuttleslide.agent.review.interactive_orchestrator import (
            InteractiveOrchestrator,
        )
        # In mock mode, swap in the stub orchestrator that fires synthetic
        # events + populates canned state instead of calling real LLMs.
        # Same constructor signature, so the rest of the run path is
        # unchanged. Imported lazily so non-mock runs don't pay the
        # import cost (and so test suites that don't exercise mock mode
        # aren't forced to drag it in).
        if self.mock_mode:
            from shuttleslide.agent.review.mock_orchestrator import (
                MockInteractiveOrchestrator as _OrchestratorClass,
            )
        else:
            _OrchestratorClass = InteractiveOrchestrator
        try:
            gate = ReviewGate()
            state_cache_path = self._run_output_dir / "agent_state.json" if self._run_output_dir else None
            # Wrap orchestrator construction + run in the house_rules
            # override context so any registered provider (e.g. pro's
            # canvas hook) can swap shuttleslide.agent.prompts.HOUSE_RULES
            # for the duration of this run. No-op when no provider
            # registers or all return None — the module constant stays
            # at its public-package default.
            from shuttleslide.agent.review.house_rules_hook import (
                override_house_rules_for_config,
            )
            with override_house_rules_for_config(config) as applied_rules:
                # ``review_stages`` deliberately not passed: the default
                # in InteractiveOrchestrator.__init__ is ``{s.name for s
                # in self._stages}``, built from ``full_registry()`` so
                # pro extension stages (script / voiceover / motion_design
                # / render_video) are automatically reviewed too. Pinning
                # it to ``default_registry().all_names()`` here would
                # exclude pro stages — they'd emit stage_complete without
                # pausing the gate, so the UI's Approve button (armed by
                # every stage_complete) would target a non-paused gate
                # and surface "no active pipeline to approve" once the
                # orchestrator finishes or fails.
                self._orchestrator = _OrchestratorClass(
                    config=config,
                    gate=gate,
                    auto_approve=False,
                    broadcaster=self,
                    state_cache_path=state_cache_path,
                    load_state_on_start=load_state_on_start,
                )
                self.emit_pipeline_state("running")
                # Broadcast the full ordered stage list so the UI can
                # pre-create all stage tabs (including extension stages
                # without a registered renderer — script, motion_design)
                # in the correct execution order. Without this, the
                # client's getAllStages() falls back to STAGES
                # (builtin-only) and pro tabs appear out of order or not
                # at all on the live run path. /api/state covers refresh.
                _stage_registry = self._get_full_registry()
                _stage_names: List[str] = []
                if _stage_registry is not None:
                    try:
                        _stage_names = [s.name for s in _stage_registry.resolve_order()]
                    except Exception:  # noqa: BLE001 — best-effort
                        _stage_names = []
                if not _stage_names:
                    from shuttleslide.agent.review.review_gate import STAGE_NAMES
                    _stage_names = list(STAGE_NAMES)
                self.emit_pipeline_stages(_stage_names)
                # Sanity probe for the log_entry broadcast channel — if this
                # line appears in the log drawer, the WS path works end-to-end
                # and any missing llm:* entries point to the on_llm_response
                # callback wiring (not the broadcast). If this line is also
                # missing, the issue is in the broadcast path or stale browser
                # cache (Phase 10 cache-bust in index.html handles the latter).
                rules_note = (
                    f" (house_rules override: {applied_rules!r})"
                    if applied_rules is not None
                    else ""
                )
                self.emit_log_entry(
                    "pipeline",
                    f"orchestrator started — load_state={load_state_on_start}{rules_note}",
                    "info",
                )
                result = await self._orchestrator.run()
            # orchestrator hook fires emit_pipeline_done inside _post_stage_hook;
            # capture html_paths here for /api/status to serve on refresh.
            if result is not None and result.html_paths:
                # Filter None defensively: state.html_paths can carry None
                # elements in degraded runs (e.g. add_slide rollback
                # repopulating from a pre-state that had Nones). Without
                # the filter, downstream emits "None" strings to the UI
                # (str(None)) and shows misleading filenames. The same
                # hygiene is applied in RenderedStage.build_snapshot.
                self._html_paths = [
                    str(p) for p in result.html_paths if p is not None
                ]
            self.emit_pipeline_state("done")
            # NOTE: do NOT clear ``self._orchestrator`` here. Keeping it
            # alive after pipeline_done lets the user click "Regenerate"
            # in the review UI (e.g. motion_design's "Regenerate Preview"
            # button, voiceover's per-slide ↻) without re-running the
            # whole pipeline. The orchestrator holds the live state and
            # the stage registry the regen coordinator needs.
            #
            # Clear happens in three places:
            #   1. ``except asyncio.CancelledError`` below (user reset)
            #   2. ``except Exception`` below (pipeline failed — leave
            #      no half-run orchestrator around)
            #   3. ``POST /api/reset`` explicitly clears it for the
            #      "start a new run" flow.
        except asyncio.CancelledError:
            # User triggered POST /api/reset or server shutdown. Don't
            # surface as an error — the cancellation was intentional.
            self._orchestrator = None
            raise
        except Exception as e:
            self._pipeline_error = str(e)
            self.emit_error(f"pipeline failed: {e}", fatal=True)
            self.emit_pipeline_state("failed", error=str(e))
            # Drop the orchestrator on failure — its in-memory state may
            # be inconsistent (mid-stage mutation when the exception fired),
            # and the user will re-run via /api/start which constructs a
            # fresh one.
            self._orchestrator = None

    async def _render_run_to_pptx_dsl(self, state_file: Path) -> Any:
        """Build a PPTX-ready PresentationDSL from a saved run.

        Reads ``html_paths`` from the saved state, runs each HTML file
        through :class:`RuleSlideTransformer` (Playwright extracts the
        layout data → elements), and merges the per-slide DSLs into one
        presentation using the saved state's theme and canvas dimensions.

        ``_state_to_presentation`` is reused for theme extraction (it
        applies the ``ThemeDef.__dataclass_fields__`` filter so user
        customizations survive). We do NOT reuse its slides — they carry
        only ``slots.html``, but PPTXRenderer iterates over
        ``slide.elements`` and would produce an empty PPTX. The transform
        step populates elements from the rendered HTML's layout.

        Raises ``ValueError`` when ``html_paths`` is empty (nothing to
        export — typically a crashed run). Other exceptions bubble up to
        the caller (the /api/pptx endpoint) which converts them to 500.
        """
        from shuttleslide.agent.review.core_stages import _state_to_presentation
        from shuttleslide.agent.review.state_persistence import load_state
        from shuttleslide.html_to_pptx import (
            PresentationDSL,
            RuleSlideTransformer,
            ThemeDef,
        )

        state = load_state(state_file)
        if not state.html_paths:
            raise ValueError(
                "no HTML files in saved state — pipeline likely did not finish"
            )

        # Theme: thread through state's saved theme (preserves user
        # customizations). _state_to_presentation does the
        # ThemeDef(**filtered_dict) construction; reusing it keeps this
        # path consistent with the HTML-rendering path.
        base_presentation = _state_to_presentation(state)
        theme: ThemeDef = base_presentation.theme

        transformer = RuleSlideTransformer()
        merged_slides: List[Any] = []
        for html_path_str in state.html_paths:
            # html_paths are written as absolute paths by the orchestrator
            # (see orchestrator.py:322). Resolve relative to the run dir
            # as a fallback for portability across machines.
            p = Path(html_path_str)
            if not p.is_absolute():
                p = self._run_output_dir / p
            html = p.read_text(encoding="utf-8")
            slide_dsl = await transformer.transform_html(
                html, base_dir=p.parent,
            )
            merged_slides.extend(slide_dsl.slides)

        presentation = PresentationDSL(theme=theme, slides=merged_slides)
        presentation.slide_width_emu = state.canvas_width_emu
        presentation.slide_height_emu = state.canvas_height_emu
        return presentation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Matches the first <head ...> tag (case-insensitive, allows attributes).
# The production presentation.html.j2 always emits exactly one <head> with
# no attributes, so count=1 substitution is safe.
_BASE_HREF_INSERTION_RE = re.compile(r"(<head\b[^>]*>)", re.IGNORECASE)

# Per-run subdirectory name format. Matches ``run_<YYYYMMDD>_<HHMMSS>``.
# Used by ``GET /api/runs`` to list runs and by ``_extract_config_kwargs``
# to validate ``load_state_from`` — strict regex prevents path-traversal
# attacks like ``load_state_from="../../etc/passwd"``.
_RUN_DIRNAME_RE = re.compile(r"^run_\d{8}_\d{6}$")


def _inject_base_href(html: str, base_href: str) -> str:
    """Insert ``<base href="...">`` immediately after ``<head>``.

    The production ``presentation.html.j2`` has no ``<base>`` tag — saved
    files resolve relative URLs against their own directory. The preview
    iframe needs ``<base href="/files/">`` to redirect those same relative
    URLs to the StaticFiles mount so images/svgs load correctly.

    We insert as the first child of ``<head>`` so it precedes any other
    resource URLs. Returns the input unchanged if no ``<head>`` is found
    (defensive — should never happen given the template structure).
    """
    def _replace(match: "re.Match[str]") -> str:
        return f'{match.group(1)}\n    <base href="{base_href}" />'

    new_html, count = _BASE_HREF_INSERTION_RE.subn(_replace, html, count=1)
    return new_html if count else html


def _to_json_safe(value: Any) -> Any:
    """Recursively convert a dataclass-asdict output to JSON-safe types.

    ``msg_to_dict`` already handles the per-message conversion but we
    double-wrap nested snapshots to defend against any non-JSON fields
    that slip in via future EditTarget additions.
    """
    if isinstance(value, dict):
        return {k: _to_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_safe(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    # Unknown type — stringify defensively (matches snapshots.py behaviour).
    return str(value)
