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
import json
import re
import threading
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from shuttleslide.agent.dsl_to_html import SlideHTMLRenderer
from shuttleslide.agent.review.review_gate import (
    ReviewGate,
    StageName,
    StageSnapshot,
)
from shuttleslide.agent.review.snapshots import build_snapshot
from shuttleslide.agent.review.state_persistence import load_state
from shuttleslide.agent.review.ws_protocol import (
    ErrorMsg,
    LogEntryMsg,
    PipelineDoneMsg,
    PipelineStateMsg,
    StageCompleteMsg,
    StageProgressMsg,
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
    ) -> None:
        self.gate = gate
        self.orchestrator_loop = orchestrator_loop
        self.host = host
        self.port = port

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
        self._run_output_dir: Optional[Path] = None  # per-run subdir under output_dir

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
            if path in ("/", "/app.js", "/styles.css", "/index.html"):
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

        @app.websocket("/ws")
        async def ws_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            self._connections.add(ws)
            try:
                # Replay buffered messages so a late-connecting client
                # sees the full stage history.
                for msg in list(self._early_messages):
                    await ws.send_json(msg)
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

            # Reset run-scoped state from any previous run.
            self._last_snapshots.clear()
            self._html_paths = []
            self._pipeline_error = None
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
            delivered live. Returns an empty snapshots list when no
            run is active or the state file is missing — the caller
            falls back to WS-only flow in that case.
            """
            if self._run_output_dir is None:
                return JSONResponse({
                    "state": self._pipeline_state,
                    "snapshots": [],
                    "active_stage": None,
                    "pipeline_done": False,
                    "gate_paused": False,
                    "html_paths": [],
                    "run_dir": None,
                })

            state_file = self._run_output_dir / "agent_state.json"
            if not state_file.exists():
                return JSONResponse({
                    "state": self._pipeline_state,
                    "error": self._pipeline_error,
                    "snapshots": [],
                    "active_stage": None,
                    "pipeline_done": self._pipeline_state == "done",
                    "gate_paused": False,
                    "html_paths": list(self._html_paths),
                    "run_dir": str(self._run_output_dir),
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

            snapshots_payload = []
            for stage_name in completed:
                # Prefer in-memory snapshot (richer; carries editable
                # targets as built by the live orchestrator). Fall back
                # to rebuild-from-state if memory was cleared or this is
                # a fresh server process reading a prior run.
                snap = self._last_snapshots.get(stage_name)
                if snap is None:
                    snap = build_snapshot(stage_name, loaded)
                    # Write-back so /artifact/* routes (which read
                    # _last_snapshots) can serve slide/image/theme
                    # bytes for stages that completed in a prior
                    # session. Without this, hydration tells the client
                    # "slides stage is done" but /artifact/slides/0
                    # returns 404 because the orchestrator hasn't
                    # actually emitted slides yet in this server
                    # process. Dict key assignment is atomic under the
                    # GIL; same race window /api/status already accepts.
                    self._last_snapshots[stage_name] = snap
                snapshots_payload.append({
                    "stage": stage_name,
                    "snapshot": _to_json_safe(asdict(snap)),
                    "timestamp": snap.timestamp,
                })

            return JSONResponse({
                "state": self._pipeline_state,
                "error": self._pipeline_error,
                "snapshots": snapshots_payload,
                "active_stage": completed[-1] if completed else None,
                # pipeline_done is "can the user download / view final
                # output?" — true when html_paths are on disk, even if
                # the orchestrator hasn't returned yet (the rendered
                # stage's _post_stage_hook emits pipeline_done BEFORE
                # the gate pause, so a UI can show the Download button
                # while _pipeline_state is still "running"). Using
                # _pipeline_state alone returns false in that window,
                # which breaks post-refresh hydration.
                "pipeline_done": bool(loaded.html_paths) or self._pipeline_state == "done",
                # True only when the orchestrator is actually blocked
                # at gate.pause_for_review. Distinguishes "refreshed
                # while paused" (Approve should work) from "refreshed
                # while mid-stage execution" (Approve would be a silent
                # no-op — see review_gate.py:139-143). Client uses this
                # to decide whether to enable the Approve button.
                "gate_paused": bool(self._active_gate and self._active_gate.is_paused),
                "html_paths": list(self._html_paths),
                "run_dir": str(self._run_output_dir),
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
            })

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

        PR2 supports ``approve_stage``; everything else gets a clear
        ``not implemented`` reply so the UI doesn't silently hang when
        a future client tries an edit before PR3 ships. Pipeline
        cancellation routes through Home → /api/reset → task.cancel()
        (works in any pipeline state), not through a WS message.
        """
        msg_type = msg.get("type")
        if msg_type == "approve_stage":
            gate = self._active_gate
            if gate is None:
                await self._send(ws, ErrorMsg(message="no active pipeline to approve"))
                return
            gate.release("approve")
            return
        if msg_type in ("request_edit", "upload_image", "undo"):
            await self._send(
                ws, ErrorMsg(message=f"{msg_type} not implemented in PR2")
            )
            return
        if msg_type is None:
            raise _MalformedMessage("client message missing 'type' field")
        raise _MalformedMessage(f"unknown client message type {msg_type!r}")

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
        rendered = self._renderer.render_slide(
            slide,
            theme,
            title=f"Slide {slide_idx + 1}",  # 1-indexed to match saved 1.html, 2.html, ...
            canvas_width_emu=12192000,   # 1280 CSS px (default; snapshot has no canvas dims)
            canvas_height_emu=6858000,   # 720 CSS px
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

    # Known AgentConfig scalar fields the form is allowed to set.
    # Excludes output_dir (server-controlled), on_llm_response (callback),
    # canvas_*_emu (advanced; future UI), and the bing base URL (rarely
    # overridden). Anything not in this set is silently dropped.
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
                if expected is bool:
                    if isinstance(raw, bool):
                        out[key] = raw
                    else:
                        out[key] = str(raw).lower() in ("1", "true", "yes", "on")
                elif expected is int:
                    out[key] = int(raw)
                elif expected is float:
                    out[key] = float(raw)
                else:
                    out[key] = str(raw)
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

        # Enforce locked credential fields server-side, overriding
        # whatever the form sent. UI marks these readonly (so the right
        # value submits), but a user with devtools can still bypass —
        # server is the source of truth. Topic / style / counts are
        # never locked (not in effective_defaults).
        for field, value in self.effective_defaults.items():
            if field in self._ALLOWED_CONFIG_FIELDS:
                out[field] = value

        return out

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
        from shuttleslide.agent.review.registry import default_registry
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
            self._orchestrator = _OrchestratorClass(
                config=config,
                gate=gate,
                review_stages=set(default_registry().all_names()),
                auto_approve=False,
                broadcaster=self,
                state_cache_path=state_cache_path,
                load_state_on_start=load_state_on_start,
            )
            self.emit_pipeline_state("running")
            # Sanity probe for the log_entry broadcast channel — if this
            # line appears in the log drawer, the WS path works end-to-end
            # and any missing llm:* entries point to the on_llm_response
            # callback wiring (not the broadcast). If this line is also
            # missing, the issue is in the broadcast path or stale browser
            # cache (Phase 10 cache-bust in index.html handles the latter).
            self.emit_log_entry(
                "pipeline",
                f"orchestrator started — load_state={load_state_on_start}",
                "info",
            )
            result = await self._orchestrator.run()
            # orchestrator hook fires emit_pipeline_done inside _post_stage_hook;
            # capture html_paths here for /api/status to serve on refresh.
            if result is not None and result.html_paths:
                self._html_paths = [str(p) for p in result.html_paths]
            self.emit_pipeline_state("done")
        except asyncio.CancelledError:
            # User triggered POST /api/reset or server shutdown. Don't
            # surface as an error — the cancellation was intentional.
            self._orchestrator = None
            raise
        except Exception as e:
            self._pipeline_error = str(e)
            self.emit_error(f"pipeline failed: {e}", fatal=True)
            self.emit_pipeline_state("failed", error=str(e))
        finally:
            self._orchestrator = None

    async def _render_run_to_pptx_dsl(self, state_file: Path) -> Any:
        """Build a PPTX-ready PresentationDSL from a saved run.

        Reads ``html_paths`` from the saved state, runs each HTML file
        through :class:`RuleSlideTransformer` (Playwright extracts the
        layout data → elements), and merges the per-slide DSLs into one
        presentation using the saved state's theme and canvas dimensions.

        Why not use ``_state_to_presentation`` directly?
        That helper produces a DSL whose slides carry only ``slots.html``
        — fine for re-rendering HTML, but PPTXRenderer iterates over
        ``slide.elements`` and would produce an empty PPTX. The transform
        step populates elements from the rendered HTML's layout.

        Raises ``ValueError`` when ``html_paths`` is empty (nothing to
        export — typically a crashed run). Other exceptions bubble up to
        the caller (the /api/pptx endpoint) which converts them to 500.
        """
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
        # customizations). _state_to_presentation already does this
        # mapping; reusing its ThemeDef construction keeps the two paths
        # consistent. Inline import to avoid the orchestrator import
        # pulling fastapi-free concerns into modules that don't need it.
        from shuttleslide.agent.orchestrator import _state_to_presentation
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
