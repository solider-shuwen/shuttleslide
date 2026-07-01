// Shuttleslide Studio — review UI client logic.
// Talks to /ws and renders pipeline state. See ws_protocol.py for message types.
"use strict";

// =====================================================================
// Theme toggle — local state synced to <html data-theme> + localStorage.
// The initial data-theme value is set by an inline head script (see
// index.html) so the first paint already matches user preference.
// CSS handles showing sun vs moon icon based on data-theme.
// =====================================================================
const themeToggle = document.getElementById("theme-toggle");
if (themeToggle) {
  themeToggle.addEventListener("click", () => {
    const current = document.documentElement.dataset.theme || "light";
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try {
      localStorage.setItem("shuttleslide:theme", next);
    } catch (e) {
      // localStorage may be disabled (private mode, sandboxed iframe);
      // theme still works in-session via dataset.theme.
    }
  });
}

// Canonical stage order mirrors STAGE_NAMES in review_gate.py.
// This is the **builtin fallback** list — used by getAllStages() only
// before the server has declared the actual stage_order (via the
// pipeline_stages WS message or /api/state.stage_order). Once the
// server speaks, declaredStages replaces this entirely and the UI
// shows extension stages (script, voiceover, motion_design,
// render_video) in their true execution order.
const STAGES = ["theme", "outline", "images", "slides", "rendered"];
// Display labels for stage tabs. Internal stage ids stay stable
// (they're a contract with the backend / ws_protocol / snapshots);
// only the user-facing label changes. "rendered" → "export" because
// that stage's job is to write standalone HTML files to disk and
// surface "Open file" links — it's an export action, not a re-preview.
const STAGE_LABELS = {
  theme: "theme",
  outline: "outline",
  images: "images",
  slides: "slides",
  rendered: "export",
};
function stageLabel(stage) {
  if (STAGE_LABELS[stage]) return STAGE_LABELS[stage];
  // Fallback for dynamic / extension stages without a registered
  // label: title-case the identifier ("motion_design" → "Motion
  // Design", "script" → "Script"). Stages that registered via
  // SlidecraftReview.registerStageRenderer(..., {label: "..."})
  // skip this path because STAGE_LABELS was populated at register
  // time (see registerStageRenderer below).
  return String(stage)
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

// Server-declared stage order. Populated from /api/state.stage_order
// or the WS pipeline_stages message. Null until the server speaks —
// getAllStages() falls back to STAGES so a freshly-loaded page doesn't
// render an empty sidebar before /api/state responds.
//
// Replaces the legacy extraStages[] array, which filled from ext.js
// registration order + stage_complete arrival order and produced
// sidebar tabs in the wrong sequence (script appeared AFTER voiceover
// even though it runs BEFORE rendered). Server-side registry.resolve_order()
// is now the single source of truth for stage existence + ordering;
// extraStageRenderers below is a SEPARATE concern (how to render a
// tab's content, not whether the tab exists).
let declaredStages = null;
const extraStageRenderers = new Map();

function getAllStages() {
  // Server-declared list when available, else builtin STAGES as
  // bootstrap fallback. Used by renderStageTabs / stage_complete /
  // hydrateFromDisk so every code path sees the same ordering.
  return declaredStages || STAGES;
}

function setDeclaredStages(stages) {
  // Accept the server's stage list and rebuild the sidebar to match.
  // Idempotent: receiving the same list twice is a no-op for the
  // stageState / snapshots maps (we only backfill missing keys, never
  // overwrite a stage's current state).
  if (!Array.isArray(stages) || stages.length === 0) return;
  declaredStages = stages.slice();
  // Backfill stageState / snapshots for any new stages so renderStageTabs()
  // finds them. Existing state (e.g. a stage already marked "completed"
  // by hydrateFromDisk before setDeclaredStages runs) is preserved.
  for (const s of stages) {
    if (!(s in stageState)) stageState[s] = "pending";
    if (!(s in snapshots)) snapshots[s] = null;
  }
  // Re-render the sidebar with the new tab set. renderAll would also
  // re-paint the preview area; we only need the tabs here, but
  // renderStageTabs is the canonical entry point.
  try { renderStageTabs(); } catch (e) { /* DOM not yet ready */ }
}

// Drain any registrations queued by the index.html stub before app.js
// finished loading. Each entry: {name, renderer, opts}.
//
// NOTE: this only populates extraStageRenderers (the renderer map) and
// STAGE_LABELS (display labels). It does NOT push names into a stage
// list — the server's stage_order / pipeline_stages drives which tabs
// exist. Decoupling these two concerns fixes the bug where ext.js
// registration order determined tab order instead of execution order.
if (window.SlidecraftReview && Array.isArray(window.SlidecraftReview._pendingRegistrations)) {
  for (const reg of window.SlidecraftReview._pendingRegistrations) {
    extraStageRenderers.set(reg.name, { render: reg.renderer, opts: reg.opts || {} });
    if (!STAGE_LABELS[reg.name] && reg.opts && reg.opts.label) {
      STAGE_LABELS[reg.name] = reg.opts.label;
    }
  }
}

// Public namespace for extension scripts. Replaces the index.html stub
// so any LATE-loaded extension finds the same API surface. The stub's
// _pendingRegistrations queue is no longer needed after this point —
// direct mutation of extraStageRenderers takes over.
window.SlidecraftReview = {
  stageRenderers: extraStageRenderers,
  registerStageRenderer(name, renderer, opts) {
    extraStageRenderers.set(name, { render: renderer, opts: opts || {} });
    if (!STAGE_LABELS[name] && opts && opts.label) {
      STAGE_LABELS[name] = opts.label;
    }
    // No stage-list mutation: server's stage_order drives getAllStages().
    // Re-render so the new renderer applies to an already-existing tab
    // (e.g. ext.js loaded late after pipeline_stages built the sidebar).
    // Wrapped in try/catch because registerStageRenderer can be called
    // before renderAll is fully wired (extension loaded during app.js init).
    try { renderAll(); } catch (e) { /* renderAll not yet defined */ }
  },
  // Expose fileUrl() so extensions can resolve server-side absolute
  // paths (e.g. "D:\\...\\run_X\\audio\\slide_0.wav") to /files/ URLs
  // without duplicating the path-mangling logic.
  fileUrl(p) { return fileUrl(p); },
  // Expose a WS send helper so extensions can trigger server-side
  // actions (e.g. regenerate_item) without grabbing the `ws` global.
  // Best-effort: drops the message if the socket isn't open.
  send(msg) {
    try {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify(msg));
      }
    } catch (e) { /* best-effort — caller can retry on next event */ }
  },
};

// Per-stage state for the top bar.
// Values: "pending" | "running" | "completed" | "cancelled"
// Plain object (not Map) so extension-registered stages can be added
// dynamically as keys — `stageState["voiceover"] = "completed"` works
// without any pre-registration.
const stageState = Object.fromEntries(STAGES.map(s => [s, "pending"]));

// Snapshot cache — one entry per completed stage. Lets the user click
// back through stage history in the top bar without re-fetching.
const snapshots = Object.fromEntries(STAGES.map(s => [s, null]));

let activeStage = null;        // which stage the UI is currently showing
let activeItemIdx = 0;         // which thumbnail is selected (0-based)
let themeDraft = null;         // local uncommitted theme ({key:color}); null = not in edit mode
let outlineDraft = null;       // local uncommitted outline list; null = not in edit mode
let lastHistoryEntries = [];   // last received history_snapshot entries; re-filtered on stage switch
// Stale-mark cache — server pushes a fresh ``stale_marks_updated`` after
// every edit / undo / revert / regenerate so badges stay in sync. Shape:
//   { stage_name: [{ target_id, source_stage, source_id, reason, created_at, context_snapshot? }] }
// Keys are downstream stage names (images/slides/rendered). Used to:
//   - render orange "stale" badges on affected thumbnails
//   - show the "Update this slide" / "Dismiss" affordance in the preview
//     banner when the user focuses a stale item.
let staleMarks = {};
// Track in-flight regenerate requests so we can show a spinner on the
// originating button until ``item_regenerated`` arrives (paired by ref_id).
const pendingRegens = new Map();   // ref_id → { stage, target_id, mode }
const pendingRevertIds = new Set();   // entry.idx values whose Restore was clicked but not yet Undo/Commit
let pendingGateStage = null;   // which stage the gate is paused on —
                               // Approve/Cancel always target THIS,
                               // not activeStage, so the user can
                               // safely browse history without
                               // accidentally approving the wrong stage.
let pipelineDone = false;

const ws = new WebSocket(`ws://${location.host}/ws`);
const approveBtn = document.getElementById("approve-btn");
// Dedicated PPTX download button — revealed as soon as the `rendered`
// stage completes (HTML is on disk, PPTX render endpoint can produce a
// file on demand). Stays visible through any subsequent pro stages
// (narration, motion design, ...). Decoupled from the Approve button so
// pro flows can keep advancing stages without losing the download entry
// point; non-pro flows see both buttons briefly, then Approve is
// disabled by setPipelineDone() once the last stage finishes.
const downloadPptxBtn = document.getElementById("download-pptx-btn");
// The status banner's DOM was replaced by #progress-strip in PR-X. The
// var is kept as a reference to the new strip so existing setStatusBanner
// callers don't have to change — they route through updateProgressStrip()
// internally. Direct textContent on this var is gone (the strip has
// nested children now); use updateProgressStrip / setStatusBanner instead.
const statusBanner = document.getElementById("progress-strip");
const previewContent = document.getElementById("preview-content");
const doneBanner = document.getElementById("done-banner");
const doneBannerBody = document.getElementById("done-banner-body");
const doneToggle = document.getElementById("done-toggle");
const doneFileCount = document.getElementById("done-file-count");
const stageList = document.getElementById("stage-list");
const thumbList = document.getElementById("thumb-list");
const thumbTitle = document.getElementById("thumb-title");

// Log drawer — collapsible per-stage + conversion log surface.
// LOG_CAP trims oldest entries so a long pipeline run doesn't blow
// out DOM size. _logSize tracks the live count (kept in sync with
// the count badge in the drawer header).
const logDrawer = document.getElementById("log-drawer");
const logList = document.getElementById("log-list");
const logToggle = document.getElementById("log-toggle");
const logClearBtn = document.getElementById("log-clear");
const logCountEl = document.getElementById("log-count");
const LOG_CAP = 200;
let _logSize = 0;
// Auto-expand the drawer on the first log_entry of a new run. Reset
// in resetPipelineUiState so each run gets a fresh auto-expand. Once
// expanded, the user's manual collapses persist until the next reset —
// we don't fight them.
let _autoExpandedThisRun = false;

function appendLog(scope, message, level = "info") {
  if (!logList) return;
  const entry = document.createElement("div");
  entry.className = `log-entry log-level-${level}`;
  const t = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const ts = `${pad(t.getHours())}:${pad(t.getMinutes())}:${pad(t.getSeconds())}`;
  // escapeHtml avoids injection from snapshot fields that may carry
  // user content (e.g. error messages from the LLM). Defined below.
  entry.innerHTML =
    `<span class="log-time">${ts}</span>` +
    `<span class="log-scope">${escapeHtml(scope)}</span>` +
    `<span class="log-msg">${escapeHtml(message)}</span>`;
  // Only auto-stick to bottom if the user is already parked there —
  // if they've scrolled up to read history, don't yank them down.
  const stick = _logParkedAtBottom();
  logList.appendChild(entry);
  _logSize++;
  // Trim oldest entries above the cap. Removing from the top keeps
  // the most-recent context, which is what users expect.
  while (_logSize > LOG_CAP && logList.firstChild) {
    logList.removeChild(logList.firstChild);
    _logSize--;
  }
  if (logCountEl) logCountEl.textContent = String(_logSize);
  if (stick) logList.scrollTop = logList.scrollHeight;
}

function _logParkedAtBottom() {
  if (!logList) return false;
  // 24px slop — anything smaller counts as "at bottom" even with
  // sub-pixel scroll heights on high-DPI displays.
  return logList.scrollHeight - logList.scrollTop - logList.clientHeight < 24;
}

function clearLog() {
  if (!logList) return;
  logList.innerHTML = "";
  _logSize = 0;
  if (logCountEl) logCountEl.textContent = "0";
}

function expandLogDrawer() {
  if (!logDrawer) return;
  logDrawer.classList.add("expanded");
  logDrawer.classList.remove("collapsed");
  logToggle?.setAttribute("aria-expanded", "true");
  // Jump to latest so the user sees the new entry.
  if (logList) logList.scrollTop = logList.scrollHeight;
}

logToggle?.addEventListener("click", () => {
  const expanded = !logDrawer.classList.contains("expanded");
  logDrawer.classList.toggle("expanded", expanded);
  logDrawer.classList.toggle("collapsed", !expanded);
  logToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  if (expanded && logList) logList.scrollTop = logList.scrollHeight;
});
logClearBtn?.addEventListener("click", clearLog);

// Done-banner toggle — same .collapsed/.expanded + caret pattern as
// #log-drawer. Default state on each pipeline completion is .expanded
// (set in renderPipelineDone); user clicks collapse to get the banner
// out of the way without losing the file-list affordance.
doneToggle?.addEventListener("click", () => {
  if (!doneBanner) return;
  const expanded = !doneBanner.classList.contains("expanded");
  doneBanner.classList.toggle("expanded", expanded);
  doneBanner.classList.toggle("collapsed", !expanded);
  doneToggle.setAttribute("aria-expanded", expanded ? "true" : "false");
});

function setStatusBanner(text, cls) {
  // Thin shim that routes existing banner-text callers through the new
  // progress strip. cls values: "" | "error" | "done" — each maps to a
  // strip state (message / error / done) so the colour + bar reflect
  // severity without callers needing to know about the new structure.
  const state = cls === "error" ? "error"
              : cls === "done"  ? "done"
              :                   "message";
  updateProgressStrip(state, { message: text });
}

// ---------------------------------------------------------------------
// Progress strip — the prominent stage indicator with animated bar.
// States:
//   "idle"    — pre-pipeline; dim text, empty bar
//   "running" — accent border + ring, pulsing icon, fill bar OR indeterminate
//               flowing stripe (opts.indeterminate) for atomic stages
//   "message" — review-gate pause / status text; bar frozen at last width
//   "done"    — success colour, bar full
//   "error"   — error colour, bar frozen
// opts fields (running): stage, current, total, percent, elapsed, eta,
// label, indeterminate. opts.message for message/done/error/idle.
// ---------------------------------------------------------------------
function updateProgressStrip(state, opts = {}) {
  if (!statusBanner) return;
  const cls = `state-${state}${opts.indeterminate ? " indeterminate" : ""}`;
  statusBanner.className = cls;

  const nameEl = statusBanner.querySelector(".progress-stage-name");
  const statsEl = statusBanner.querySelector(".progress-stats");
  const etaEl = statusBanner.querySelector(".progress-eta");
  const fillEl = statusBanner.querySelector(".progress-bar-fill");
  if (!nameEl || !statsEl || !etaEl || !fillEl) return;

  // Reset transient fields; each branch below sets what it needs.
  statsEl.textContent = "";
  etaEl.textContent = "";

  switch (state) {
    case "running": {
      const label = opts.stage ? stageLabel(opts.stage) : "";
      nameEl.textContent = label ? `Running: ${label}` : "Running...";
      if (opts.indeterminate) {
        // Atomic stage (theme / rendered) — elapsed timer only.
        statsEl.textContent = formatElapsed(opts.elapsed);
        // Indeterminate fill animation is driven purely by CSS class
        // (`.indeterminate`); don't touch inline width here so the
        // keyframe isn't overridden.
      } else {
        const pct = (typeof opts.percent === "number" && !isNaN(opts.percent))
          ? Math.max(0, Math.min(100, opts.percent))
          : 0;
        const statsParts = [];
        if (opts.label) statsParts.push(opts.label);
        statsParts.push(`${Math.round(pct)}%`);
        statsEl.textContent = statsParts.join(" · ");
        etaEl.textContent = (opts.eta && opts.eta > 0)
          ? `~${formatDuration(opts.eta)} remaining`
          : "";
        fillEl.style.width = `${pct}%`;
      }
      break;
    }
    case "done": {
      nameEl.textContent = opts.message || "Pipeline complete";
      fillEl.style.width = "100%";
      break;
    }
    case "error": {
      nameEl.textContent = opts.message || "Error";
      // Leave bar width as-is — frozen at point of failure.
      break;
    }
    case "message":
    case "idle":
    default: {
      nameEl.textContent = opts.message || "";
      // Don't touch fillEl.style.width — preserve last known position so
      // a review-gate pause shows the bar frozen at 100% rather than
      // snapping back to 0.
      break;
    }
  }
}

function formatElapsed(seconds) {
  if (seconds == null || seconds < 0 || !isFinite(seconds)) return "";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s elapsed`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s elapsed`;
}

function formatDuration(seconds) {
  if (seconds == null || seconds < 0 || !isFinite(seconds)) return "";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return `${m}m ${s % 60}s`;
}

function setPreview(html) {
  previewContent.innerHTML = html;
  // Preview content was just rebuilt — re-decide whether the stale
  // banner shows above it. Cheap no-op when no mark applies.
  renderStaleBanner();
}

// Scale the .big-slide-wrapper iframe (sized to --canvas-w × --canvas-h)
// to fit the wrapper's actual rendered width. Called after each
// slides-stage preview render and on window resize. Without this the
// iframe viewport would be smaller than the slide canvas and content
// would clip / scroll inside the iframe.
//
// Reads canvas dims from the CSS custom properties on documentElement
// (set by setCanvasDimsFromState from /api/state). Falls back to 1280
// if the vars aren't set yet — same as the historical hardcode.
function getCanvasWidthPx() {
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue('--canvas-w-num')
    .trim();
  const n = parseInt(v, 10);
  return Number.isFinite(n) && n > 0 ? n : 1280;
}

// Apply canvas dimensions from /api/state to the documentElement's CSS
// custom properties. Thumbnails + preview iframe + demo slide all read
// these vars (see styles.css), so a single write updates every surface.
// Skipped when stateData lacks canvas_*_px (no run active yet) — the
// :root defaults (16:9) keep the UI usable.
function setCanvasDimsFromState(stateData) {
  if (!stateData) return;
  const w = stateData.canvas_width_px;
  const h = stateData.canvas_height_px;
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return;
  const root = document.documentElement.style;
  root.setProperty('--canvas-w', `${w}px`);
  root.setProperty('--canvas-h', `${h}px`);
  root.setProperty('--canvas-w-num', String(w));
  root.setProperty('--canvas-h-num', String(h));
  // Re-scale the big-slide iframe if it's already in the DOM — the
  // previous scale was computed against the old canvas dims. Safe no-op
  // when the preview hasn't rendered yet.
  scaleBigSlide();
}

function scaleBigSlide() {
  const wrapper = document.querySelector('.big-slide-wrapper');
  if (!wrapper) return;
  const iframe = wrapper.querySelector('iframe');
  if (!iframe) return;
  const canvasW = getCanvasWidthPx();
  const scale = wrapper.clientWidth / canvasW;
  iframe.style.transform = `scale(${scale})`;
}
window.addEventListener('resize', scaleBigSlide);

// =====================================================================
// Top stage bar — horizontal tabs, clickable when completed
// =====================================================================
function renderStageTabs() {
  stageList.innerHTML = "";
  // Iterate builtin + extension stages so pro-registered tabs
  // (voiceover / render_video / ...) appear after the builtins.
  for (const stage of getAllStages()) {
    const state = stageState[stage] || "pending";
    const tab = document.createElement("div");
    const isActive = (stage === activeStage);
    tab.className = `stage-tab ${state}${isActive ? " active" : ""}`;
    tab.dataset.stage = stage;
    tab.innerHTML = `<span class="icon"></span><span>${stageLabel(stage)}</span>`;
    if (state === "completed") {
      tab.addEventListener("click", () => {
        // Leaving theme stage without committing discards the draft.
        if (activeStage === "theme" && stage !== "theme") themeDraft = null;
        activeStage = stage;
        activeItemIdx = 0;
        renderAll();
      });
    }
    stageList.appendChild(tab);
  }
}

// =====================================================================
// Thumbnails — left column. Content depends on activeStage.
// =====================================================================
function flattenImages(snap) {
  // Convert nested {slide_idx: {slot_id: payload}} into a flat list
  // so each image gets one thumbnail. Order: by slide_idx then slot_id.
  const slotMap = snap.state_view.slide_images || {};
  const out = [];
  for (const [slideIdx, slots] of Object.entries(slotMap)) {
    for (const [slotId, payload] of Object.entries(slots)) {
      out.push({ slideIdx, slotId, payload });
    }
  }
  return out;
}

function buildThumbItems(snap) {
  const stage = snap.stage;
  const view = snap.state_view;
  // Cache-buster appended to every slide iframe URL so the browser
  // reloads (rather than serving from in-memory iframe cache) when this
  // snapshot is re-rendered. Without it, theme edits re-emit the slides
  // snapshot but the iframe src is byte-identical → browser keeps the
  // old render and the user never sees the new theme. One bust value
  // per buildThumbItems call keeps the thumbs in a single render round
  // mutually consistent; the next round gets a fresh one.
  const thumbBust = `?t=${Date.now()}`;
  if (stage === "theme") {
    const json = JSON.stringify(view.theme || {}, null, 2);
    const truncated = json.length > 400 ? json.slice(0, 400) + "\n..." : json;
    return [{
      html: `<div class="thumb-label">Theme JSON</div>
             <pre style="margin:0;font-size:10px;line-height:1.3;white-space:pre-wrap;color:var(--text-secondary);">${escapeHtml(truncated)}</pre>`,
    }];
  }
  if (stage === "outline") {
    const outline = view.outline || [];
    return outline.map((item, i) => {
      const title = escapeHtml(item.title || "(untitled)");
      const layout = escapeHtml(item.layout || "");
      return {
        html: `<div class="thumb-label">Slide ${i + 1}</div>
               <div class="thumb-body" style="font-weight:500;">${title}</div>
               ${layout ? `<div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;">[${layout}]</div>` : ""}`,
      };
    });
  }
  if (stage === "images") {
    return flattenImages(snap).map(({ slideIdx, slotId, payload }) => {
      let body = "";
      let badge = "";
      if (payload.type === "svg" || payload.type === "svg_file") {
        // svg_file is the production shape (svg_tools.py:330);
        // svg is grandfathered. Both carry inline ``data``.
        body = `<div class="thumb-svg">${payload.data || ""}</div>`;
        badge = `<span class="thumb-type-badge thumb-type-svg" title="SVG vector — edit via chat (Direct / Ask LLM)">SVG</span>`;
      } else if (payload.type === "image_file" || payload.type === "image") {
        const src = `/artifact/images/${slideIdx}/${encodeURIComponent(slotId)}`;
        body = `<img class="thumb-image" src="${src}" />`;
        // Replace button — clicking opens file picker (jumpToImageTarget
        // handler below switches to this thumbnail + triggers upload).
        badge = `<button class="thumb-type-badge thumb-type-image thumb-replace-btn"
                         data-slide="${slideIdx}" data-slot="${escapeAttr(slotId)}"
                         title="Replace with a local image">↻ Replace</button>`;
      } else {
        body = `<div class="thumb-svg" style="color:var(--text-tertiary);font-size:11px;">unknown type</div>`;
      }
      // slideIdx is a 0-indexed snapshot key; display label is 1-indexed
      // to match saved file names (1.html, 2.html, ...). URL stays 0-indexed.
      return {
        html: `<div class="thumb-label">Slide ${Number(slideIdx) + 1} / ${escapeHtml(slotId)}</div>
               <div class="thumb-image-wrap">${body}${badge}</div>`,
      };
    });
  }
  if (stage === "slides") {
    const slides = view.slides || [];
    return slides.map((s, i) => ({
      html: `<div class="thumb-label">Slide ${i + 1}</div>
             <div class="thumb-slide">
               <iframe src="/artifact/slides/${i}${thumbBust}" scrolling="no"></iframe>
             </div>`,
    }));
  }
  if (stage === "rendered") {
    // export stage: files are already on disk (orchestrator._finalize
    // runs before _post_stage_hook). Show one entry per file with an
    // "Open" link — don't re-render slide thumbnails, those belong to
    // the slides stage.
    // Filter non-string entries defensively: state.html_paths can
    // contain None elements in degraded runs (e.g. add_slide rollback
    // repopulating from a pre-state that had Nones, or a renderer
    // returning sparse output). Without this guard, p.split() throws
    // "Cannot read properties of null" and breaks hydration.
    const paths = (view.html_paths || []).filter(
      (p) => typeof p === "string" && p,
    );
    return paths.map((p, i) => {
      const filename = p.split(/[\\/]/).pop() || p;
      const href = fileUrl(p);
      return {
        html: `<div class="thumb-label">File ${i + 1}</div>
               <div class="thumb-body" style="font-weight:500;">
                 <a href="${href}" target="_blank" rel="noopener">${escapeHtml(filename)}</a>
               </div>
               <div style="font-size:11px;color:var(--text-tertiary);margin-top:2px;word-break:break-all;">${escapeHtml(p)}</div>`,
      };
    });
  }
  return [];
}

function renderThumbnails() {
  thumbList.innerHTML = "";
  if (!activeStage || !snapshots[activeStage]) {
    thumbTitle.textContent = "Thumbnails";
    thumbList.innerHTML = `<p style="color:var(--text-tertiary);font-size:12px;padding:8px;">No items yet.</p>`;
    return;
  }
  const snap = snapshots[activeStage];
  const items = buildThumbItems(snap);
  thumbTitle.textContent = `${stageLabel(activeStage)} (${items.length})`;
  if (items.length === 0) {
    thumbList.innerHTML = `<p style="color:var(--text-tertiary);font-size:12px;padding:8px;">No items in this stage.</p>`;
    return;
  }
  items.forEach((item, idx) => {
    const div = document.createElement("div");
    div.className = `thumb${idx === activeItemIdx ? " active" : ""}`;
    div.dataset.idx = String(idx);
    div.innerHTML = item.html;
    div.addEventListener("click", () => {
      activeItemIdx = idx;
      renderPreview();
      // Update active class on existing thumbs rather than full re-render
      // (avoids reloading every iframe on every click).
      thumbList.querySelectorAll(".thumb").forEach(t => {
        t.classList.toggle("active", t.dataset.idx === String(idx));
      });
      // Re-render the chat panel so it targets the newly-selected item
      // and fetch any server-side chat history for it.
      renderChatPanel();
      refreshChatHistoryForActive();
      // Active item changed → re-decide whether the stale banner shows.
      renderStaleBanner();
    });
    thumbList.appendChild(div);
  });
  // Decorate any thumbs whose (stage, idx) carries a stale mark. Done
  // after the loop so the badges sit on top of the thumb content.
  decorateStaleBadges();
}

// =====================================================================
// Preview — middle column. Renders activeStage @ activeItemIdx.
// =====================================================================
function renderPreview() {
  if (!activeStage || !snapshots[activeStage]) {
    setPreview(`<p style="color:var(--text-tertiary);">No stage output yet.</p>`);
    return;
  }
  const snap = snapshots[activeStage];
  const idx = activeItemIdx;

  // Extension-registered renderer takes precedence over builtin UI.
  // If the renderer throws, fall through to the builtin branch so the
  // user still sees SOMETHING (typically the generic JSON fallback).
  // Builtin stages (theme/outline/images/slides/rendered) are never in
  // extraStageRenderers, so this is a strict no-op for them.
  const extEntry = extraStageRenderers.get(activeStage);
  if (extEntry && typeof extEntry.render === "function") {
    let extOk = false;
    try {
      previewContent.innerHTML = "";
      const host = document.createElement("div");
      host.className = "ext-renderer-host";
      host.dataset.stage = activeStage;
      previewContent.appendChild(host);
      extEntry.render(snap, host);
      extOk = true;
    } catch (e) {
      console.error(`[review] extension renderer for "${activeStage}" threw:`, e);
    }
    if (extOk) return;
    // else: fall through to builtin rendering as a fallback.
  }

  if (activeStage === "theme") {
    // Theme preview is a single rich view (color swatches + demo slide).
    renderThemePreview(snap.state_view.theme || {});
    return;
  }
  if (activeStage === "outline") {
    renderOutlineEditor();
    return;
  }
  if (activeStage === "images") {
    const flat = flattenImages(snap);
    const item = flat[idx];
    if (!item) { setPreview(`<p style="color:var(--text-tertiary);">No such image.</p>`); return; }
    let body = "";
    let action = "";
    let attachEditor = null;
    if (item.payload.type === "svg" || item.payload.type === "svg_file") {
      // SVG slot — 默认非编辑态：仅显示 SVG + 一个「双击进入编辑」浮层。
      // 用户双击 host 后才注入 toolbar + 启用拖拽/文字编辑。
      // SVG payload 已 inline 在 parent 文档（不在 iframe），可直接 attach。
      body = `<div class="svg-edit-host" data-slide="${item.slideIdx}" data-slot="${escapeAttr(item.slotId)}">
        <div class="svg-canvas">${item.payload.data || ""}</div>
        <div class="svg-edit-overlay">Double-click to edit this SVG</div>
      </div>`;
      attachEditor = () => {
        const host = previewContent.querySelector(".svg-edit-host");
        if (host) attachSvgHostActivator(host, item.slideIdx, item.slotId);
      };
    } else {
      const src = `/artifact/images/${item.slideIdx}/${encodeURIComponent(item.slotId)}`;
      body = `<img src="${src}" style="max-width:100%;border:1px solid #eee;border-radius:4px;" />`;
      // Replace button mirrors the thumbnail's "↻ Replace" badge — both
      // route through the same jumpToImageTarget handler so a single
      // upload path serves both entry points.
      action = `<button class="preview-replace-btn"
                        data-slide="${item.slideIdx}" data-slot="${escapeAttr(item.slotId)}">
                  Replace image
                </button>`;
    }
    setPreview(`<h4>Slide ${Number(item.slideIdx) + 1} / ${escapeHtml(item.slotId)}</h4>${body}
                <div style="margin-top:12px;">${action}</div>`);
    if (attachEditor) requestAnimationFrame(attachEditor);
    return;
  }
  if (activeStage === "slides") {
    const slides = snap.state_view.slides || [];
    if (idx < 0 || idx >= slides.length) {
      setPreview(`<p style="color:var(--text-tertiary);">No such slide.</p>`);
      return;
    }
    // Slide idx in the UI is 0-indexed (matches snapshot); display
    // label is 1-indexed to match saved file names (1.html, 2.html).
    // Cache-buster on the iframe src forces a reload when the user
    // re-opens the same slide after a theme edit (otherwise the browser
    // may keep the previously-rendered iframe content).
    const previewBust = `?t=${Date.now()}`;
    setPreview(`<h4>Slide ${idx + 1}</h4>
                <div class="big-slide-wrapper">
                  <iframe class="big-slide" src="/artifact/slides/${idx}${previewBust}"></iframe>
                </div>`);
    // After the iframe loads, scale it to the wrapper width AND attach
    // the inline text editor (double-click to edit). rAF alone isn't
    // enough — contentDocument isn't usable until load fires.
    const iframe = previewContent.querySelector("iframe.big-slide");
    if (iframe) {
      const onLoaded = () => {
        scaleBigSlide();
        attachInlineEdit(iframe, idx);
        // The previously-highlighted element lived in the OLD iframe's
        // document — that node is gone now. Tear down the overlay so
        // it doesn't linger over the new slide pointing at nothing.
        hideLayerOverlay();
        // Rebuild layers list for the new slide.
        currentLayers = buildLayers(iframe, idx);
        const panel = document.getElementById("layers-panel");
        if (panel && !panel.hidden) renderLayersPanel(currentLayers);
      };
      // Same-origin iframe (served from /artifact/) → contentDocument
      // is accessible. The readyState short-circuit covers cached loads,
      // BUT a fresh iframe's initial about:blank doc is also "complete"
      // — attach there and the listener dies when the real slide HTML
      // loads and swaps the document. Skip that case explicitly.
      const doc = iframe.contentDocument;
      const isRealDoc = doc && doc.readyState === "complete" &&
                        doc.location && doc.location.href !== "about:blank";
      if (isRealDoc) {
        onLoaded();
      } else {
        iframe.addEventListener("load", onLoaded, { once: true });
      }
    }
    return;
  }
  if (activeStage === "rendered") {
    // export stage preview: show the exported file with an "Open in
    // new tab" link. _finalize has already written the file by the
    // time this snapshot arrives.
    // Defensive filter mirrors buildThumbItems: state.html_paths can
    // contain null entries, and p.split() would throw on them.
    const paths = (snap.state_view.html_paths || []).filter(
      (p) => typeof p === "string" && p,
    );
    if (idx < 0 || idx >= paths.length) {
      setPreview(`<p style="color:var(--text-tertiary);">No exported file at this index.</p>`);
      return;
    }
    const p = paths[idx];
    const filename = p.split(/[\\/]/).pop() || p;
    const href = fileUrl(p);
    setPreview(`<h4>Exported file ${idx + 1}: ${escapeHtml(filename)}</h4>
                <p style="font-size:13px;color:var(--text-secondary);margin:8px 0 16px;">
                  Path on disk:
                  <code style="background:var(--bg-elevated);padding:2px 6px;border-radius:3px;font-size:12px;word-break:break-all;">${escapeHtml(p)}</code>
                </p>
                <p>
                  <a href="${href}" target="_blank" rel="noopener"
                     style="display:inline-block;padding:8px 16px;background:var(--success);color:white;
                            text-decoration:none;border-radius:4px;font-size:14px;font-weight:500;">
                    Open in new tab →
                  </a>
                </p>
                <p style="font-size:12px;color:var(--text-tertiary);margin-top:16px;">
                  The slide is rendered as a standalone HTML document
                  (1280×720, Tailwind CDN inlined, theme colors applied) —
                  what you see is what was saved.
                </p>`);
    return;
  }
  setPreview(`<pre>${escapeHtml(JSON.stringify(snap.state_view, null, 2))}</pre>`);
}

function renderAll() {
  renderStageTabs();
  renderThumbnails();
  renderPreview();
  renderChatPanel();
  // History panel is filtered by activeStage, so re-render on every
  // stage switch (not just when a fresh snapshot arrives from server).
  renderHistoryPanel(lastHistoryEntries);
}

// =====================================================================
// Chat panel — per-element LLM / Direct / Upload editing (PR3)
// =====================================================================
// chatMode: "llm" | "direct" | "upload" — drives the Send path.
// chatHistories: per-target_path message list. Keyed by JSON.stringify(path)
// so two distinct slide_N_slot_X targets don't collide.
let chatMode = "llm";
const chatHistories = Object.create(null);
// Tracks whether each target has at least one applied edit — drives
// the Undo button's enabled state.
const chatEditedFlags = Object.create(null);

const chatTarget = document.getElementById("chat-target");
const chatHistoryEl = document.getElementById("chat-history");
const chatInput = document.getElementById("chat-input");
const chatSendBtn = document.getElementById("chat-send");
const chatUndoBtn = document.getElementById("chat-undo");
const chatDeleteBtn = document.getElementById("chat-delete");
const chatFileInput = document.getElementById("chat-file-input");
const chatImgDesc = document.getElementById("chat-img-desc");
const chatErrorEl = document.getElementById("chat-error");
const chatModeBtns = document.querySelectorAll(".chat-mode");

// Edit-in-progress UI elements. Visible globally while an LLM edit is in
// flight; body.edit-in-progress (set in setEditInProgress) dims every
// mutating affordance via CSS so the user can't kick off a second edit.
const editProgressBar = document.getElementById("edit-progress-bar");
const editProgressLabel = document.getElementById("edit-progress-label");
const editCancelBtn = document.getElementById("edit-cancel-btn");
// ref_id of the in-flight LLM edit. Set when request_edit (mode=llm) is
// sent; cleared when edit_applied / edit_rejected / edit_cancelled lands.
// The cancel button sends this ref_id so the server can match it.
let activeEditRefId = null;

// Resolve the EditTarget matching the current activeStage + activeItemIdx.
// Returns null when there's no editable target at that index (e.g.
// activeStage=rendered has no editable_targets; or activeItemIdx is past
// the end of a sparse list).
function getActiveTarget() {
  if (!activeStage) return null;
  const snap = snapshots[activeStage];
  if (!snap || !Array.isArray(snap.editable_targets)) return null;
  const targets = snap.editable_targets;
  if (activeItemIdx < 0 || activeItemIdx >= targets.length) return null;
  return targets[activeItemIdx];
}

function targetKey(path) {
  // path arrives as a list (wire format). JSON.stringify gives a stable
  // key as long as element types are stable (string/int — they are).
  return JSON.stringify(path || []);
}

function targetLabel(target) {
  if (!target) return "";
  const meta = target.meta || {};
  if (meta.slide_idx != null && meta.slot_id) {
    return `slide ${Number(meta.slide_idx) + 1} · ${meta.slot_id}`;
  }
  // Stage-level JSON targets (motion_design spec, script slides) carry
  // no slide/slot meta and would otherwise render as a raw dotted state
  // path like "stage_outputs.motion_design.spec". Show a friendly
  // "StageLabel · field" instead, reusing stageLabel() so the badge
  // matches the sidebar tab. Only triggers when path has more than one
  // segment — single-segment paths (theme, outline) already render
  // cleanly as path[0].
  if (target.stage && target.path.length > 1) {
    return `${stageLabel(target.stage)} · ${target.path[target.path.length - 1]}`;
  }
  return target.path.join(".");
}

function setChatError(msg) {
  if (!chatErrorEl) return;
  if (!msg) {
    chatErrorEl.hidden = true;
    chatErrorEl.textContent = "";
    return;
  }
  chatErrorEl.textContent = msg;
  chatErrorEl.hidden = false;
}

function renderChatPanel() {
  const target = getActiveTarget();
  if (!target) {
    if (chatTarget) { chatTarget.hidden = true; chatTarget.textContent = ""; }
    chatHistoryEl.innerHTML = `<p class="placeholder">Select an editable element to start chatting.</p>`;
    chatInput.disabled = true;
    chatInput.placeholder = "No target selected";
    chatSendBtn.disabled = true;
    chatUndoBtn.disabled = true;
    chatDeleteBtn.hidden = true;
    setChatError("");
    return;
  }
  if (chatTarget) {
    chatTarget.hidden = false;
    chatTarget.textContent = targetLabel(target);
  }
  // Image targets only support upload — force switch + hide LLM/Direct.
  // SVG targets show Upload disabled + tooltip so the user understands
  // why it's greyed out instead of being silently absent.
  const modeBtnsForKind = target.kind === "image"
    ? ["upload"]
    : ["llm", "direct"];
  chatModeBtns.forEach(btn => {
    const m = btn.dataset.mode;
    const visible = modeBtnsForKind.includes(m);
    btn.hidden = !visible;
    btn.disabled = false;
    btn.title = "";
    if (!visible && btn.classList.contains("active")) {
      btn.classList.remove("active");
    }
    if (visible && chatMode === m) {
      btn.classList.add("active");
    }
    // SVG slot: surface the Upload button as disabled with a tooltip so
    // users hunting for "how do I upload?" realise SVG slots can't accept
    // raster uploads — they need to switch to a raster slot, or use Direct
    // to paste new SVG markup.
    if (m === "upload" && target.kind === "svg") {
      btn.hidden = false;
      btn.disabled = true;
      btn.title = "SVG slots don't accept image uploads — use Direct or Ask LLM to edit the SVG, or select a raster image slot";
    }
  });
  // Re-select a valid mode if the current one is hidden.
  if (!modeBtnsForKind.includes(chatMode)) {
    chatMode = modeBtnsForKind[0];
  }
  // Mark the active mode button.
  chatModeBtns.forEach(btn => {
    btn.classList.toggle("active", btn.dataset.mode === chatMode && !btn.hidden);
  });
  // Adjust input surface for the mode.
  if (chatMode === "upload") {
    chatInput.hidden = true;
    chatSendBtn.textContent = "Choose File";
    chatSendBtn.disabled = false;
    chatInput.placeholder = "";
    // Image upload mode surfaces an optional description textarea.
    // Only relevant for kind=image (svg slots can't be uploaded to).
    // Hidden in every other mode so the panel doesn't grow extra rows.
    chatImgDesc.hidden = target.kind !== "image";
  } else {
    chatInput.hidden = false;
    chatSendBtn.textContent = "Send";
    chatInput.disabled = false;
    chatInput.placeholder = chatMode === "llm"
      ? "Ask for a change — e.g. 'make the hero more vibrant'"
      : "Paste the full replacement value (SVG / HTML / JSON)";
    chatImgDesc.hidden = true;
    // Send enabled iff there's non-whitespace input.
    chatSendBtn.disabled = !chatInput.value.trim();
  }
  chatUndoBtn.disabled = !chatEditedFlags[targetKey(target.path)];
  // Image targets get a Delete button next to Upload. Only visible when
  // the slot actually has an image (current_value is the on-disk path).
  // Non-image targets never show it.
  const hasImage = target.kind === "image" && (target.current_value || "").trim();
  chatDeleteBtn.hidden = !hasImage;
  renderChatHistory(target.path);
  setChatError("");
}

function renderChatHistory(path) {
  const key = targetKey(path);
  const history = chatHistories[key] || [];
  if (history.length === 0) {
    chatHistoryEl.innerHTML = `<p class="placeholder">No LLM conversation yet. Use direct edits (double-click preview, color picker, image upload) or type a message below.</p>`;
    return;
  }
  chatHistoryEl.innerHTML = "";
  history.forEach(entry => {
    const div = document.createElement("div");
    div.className = `chat-msg chat-msg-${entry.role}`;
    if (entry.role === "applied") {
      const diffHtml = entry.diff
        ? `<details class="chat-diff"><summary>diff</summary><pre>${escapeHtml(entry.diff)}</pre></details>`
        : "";
      div.innerHTML = `<span class="chat-msg-marker">✓</span>
                      <span class="chat-msg-body">${escapeHtml(entry.body || "Applied")}</span>
                      ${diffHtml}`;
    } else if (entry.role === "rejected") {
      div.innerHTML = `<span class="chat-msg-marker chat-msg-marker-fail">✗</span>
                      <span class="chat-msg-body">${escapeHtml(entry.body || "Rejected")}</span>`;
    } else if (entry.role === "cancelled") {
      div.classList.add("cancelled");
      div.innerHTML = `<span class="chat-msg-body">${escapeHtml(entry.body || "（已取消）")}</span>`;
    } else if (entry.role === "out_of_scope") {
      // Deck-level request the editor couldn't apply to a single item.
      // Render a guidance card with a "Go to <stage>" button so the user
      // can act on the suggestion in one click. Falls back to a plain
      // body if (somehow) no suggested_stage was attached.
      const stage = entry.suggested_stage;
      const label = stageLabel(stage);
      const btn = stage
        ? `<button class="chat-msg-goto" data-stage="${escapeAttr(stage)}">→ Go to ${escapeHtml(label)}</button>`
        : "";
      div.innerHTML = `<span class="chat-msg-marker chat-msg-marker-warn">⚠</span>
                      <span class="chat-msg-body">${escapeHtml(entry.body || "Out of scope")}</span>
                      ${btn}`;
      if (stage) {
        div.querySelector(".chat-msg-goto").addEventListener("click", () => {
          // Only completed stages are clickable in the tab bar; jumping
          // to a not-yet-run stage would silently fail. The reject path
          // only fires after the pipeline has progressed past slides /
          // images, so outline is always available — but guard anyway.
          if ((stageState[stage] || "pending") === "completed") {
            if (activeStage === "theme" && stage !== "theme") themeDraft = null;
            activeStage = stage;
            activeItemIdx = 0;
            renderAll();
          }
        });
      }
    } else if (entry.role === "pending") {
      // Local-only indicator shown while an LLM edit is in flight.
      // Pairs with the top edit-progress-bar but lives where the user's
      // gaze actually is — right under the message they just sent.
      // Removed by clearPendingAssistant when the response arrives.
      div.innerHTML = `<span class="chat-msg-pending-spinner" aria-hidden="true"></span>
                      <span class="chat-msg-body chat-msg-pending-body">${escapeHtml(entry.body || "正在生成回复…")}</span>`;
    } else {
      div.innerHTML = `<span class="chat-msg-role">${escapeHtml(entry.role)}</span>
                      <span class="chat-msg-body">${escapeHtml(entry.body || "")}</span>`;
    }
    chatHistoryEl.appendChild(div);
  });
  // Scroll to bottom so the latest message is visible.
  chatHistoryEl.scrollTop = chatHistoryEl.scrollHeight;
}

function appendChatEntry(path, role, body, extra) {
  const key = targetKey(path);
  if (!chatHistories[key]) chatHistories[key] = [];
  chatHistories[key].push({ role, body, ...extra });
  // If this is the active target, re-render to show the new entry.
  const active = getActiveTarget();
  if (active && targetKey(active.path) === key) {
    renderChatHistory(active.path);
  }
}

// ---- Pending assistant indicator -------------------------------------
//
// LLM edits take 30-60s. The top-of-page edit-progress-bar is the
// global lock indicator (cancel button + dimmed affordances), but the
// user's gaze is on the chat panel — they need a *local* indicator
// right below the message they just sent, or it looks like nothing
// happened.
//
// We append a "pending" chat history entry (role="pending") on send
// and remove it when any response (applied / rejected / cancelled)
// for the same ref_id arrives. The response handlers already append
// the final entry, so the pending marker just needs to vanish — no
// content merging.
//
// ``pathByRefId`` tracks the target path the user sent the request
// against, so ``edit_rejected`` / ``edit_cancelled`` (whose WS
// messages don't carry ``target_path`` — only ``edit_applied`` does)
// can still route their chat entries to the right history. Without
// this, an out_of_scope rejection would get appended under the empty
// path key ``[]`` and silently vanish from the UI.

// Map of ref_id -> { key, idx } so we can locate the pending entry
// across target paths (the user might have switched targets between
// send and response, though the chat panel usually stays put).
const pendingByRefId = new Map();
// Map of ref_id -> target path (array). Populated on send, consumed
// when a response arrives. Survives clearPendingAssistant because
// rejection / cancellation handlers need it AFTER the pending entry
// has been removed.
const pathByRefId = new Map();

function appendPendingAssistant(path, refId) {
  const key = targetKey(path);
  if (!chatHistories[key]) chatHistories[key] = [];
  const idx = chatHistories[key].length;
  chatHistories[key].push({
    role: "pending",
    body: "正在生成回复…",
    ref_id: refId,
  });
  pendingByRefId.set(refId, { key, idx });
  // Stash a copy of the path so reject/cancel handlers can find the
  // right chat history even after pendingByRefId is cleared.
  pathByRefId.set(refId, Array.from(path));
  const active = getActiveTarget();
  if (active && targetKey(active.path) === key) {
    renderChatHistory(active.path);
  }
}

function clearPendingAssistant(refId) {
  const pending = pendingByRefId.get(refId);
  if (!pending) return;
  pendingByRefId.delete(refId);
  const list = chatHistories[pending.key];
  if (!list) return;
  // The stored idx may have drifted if other entries were removed; find
  // the actual pending entry by role + ref_id so we don't accidentally
  // splice the wrong row.
  const actualIdx = list.findIndex(
    e => e.role === "pending" && e.ref_id === refId
  );
  if (actualIdx === -1) return;
  list.splice(actualIdx, 1);
  const active = getActiveTarget();
  if (active && targetKey(active.path) === pending.key) {
    renderChatHistory(active.path);
  }
}

// Resolve the target path for a response message. Falls back to the
// send-time path when the message itself doesn't carry target_path
// (the case for EditRejectedMsg / EditCancelledMsg). Returns an empty
// array as a last resort — callers should treat that as "no chat
// history to append to" and skip the appendChatEntry call.
function resolveResponsePath(msg) {
  if (msg.target_path && msg.target_path.length > 0) {
    return msg.target_path;
  }
  const tracked = msg.ref_id ? pathByRefId.get(msg.ref_id) : null;
  if (tracked && tracked.length > 0) {
    return tracked;
  }
  return [];
}

// Cleanup hook for response handlers: drop the path tracking entry
// once the response has been fully processed. Called at the END of
// each handler so resolveResponsePath still works during processing.
function forgetRefId(refId) {
  if (refId) pathByRefId.delete(refId);
}

function setChatEditedFlag(path, edited) {
  chatEditedFlags[targetKey(path)] = !!edited;
  const active = getActiveTarget();
  if (active && targetKey(active.path) === targetKey(path)) {
    chatUndoBtn.disabled = !edited;
  }
}

// Mode toggle — clicking a visible mode button switches chatMode.
chatModeBtns.forEach(btn => {
  btn.addEventListener("click", () => {
    if (btn.hidden) return;
    chatMode = btn.dataset.mode;
    renderChatPanel();
  });
});

// Input typing — toggles Send button enabled state.
chatInput.addEventListener("input", () => {
  if (chatMode === "upload") return;
  chatSendBtn.disabled = !chatInput.value.trim();
});

// Enter (without Shift) sends the message. Shift+Enter inserts newline.
chatInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    if (!chatSendBtn.disabled) chatSendBtn.click();
  }
});

function newRefId() {
  return `r-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

// Show the "正在修改..." bar + flip body class so every mutating affordance
// is dimmed/locked. The cancel button stays enabled so the user can abort.
// label is the progress text to display (e.g. "正在修改 slide 2…").
function setEditInProgress(refId, label) {
  activeEditRefId = refId;
  if (editProgressLabel) editProgressLabel.textContent = label || "正在修改…";
  if (editProgressBar) editProgressBar.classList.remove("hidden");
  document.body.classList.add("edit-in-progress");
  if (editCancelBtn) editCancelBtn.disabled = false;
}

// Hide the progress bar and release the global lock. Safe to call when
// no edit is in progress — it just no-ops.
function clearEditInProgress() {
  activeEditRefId = null;
  document.body.classList.remove("edit-in-progress");
  if (editProgressBar) editProgressBar.classList.add("hidden");
  if (editCancelBtn) editCancelBtn.disabled = false;
}

// Cancel button — sends a cancel_edit WS message with the active ref_id.
// We don't clear UI locally: the server will reply edit_cancelled (which
// triggers clearEditInProgress + appends the grey marker). If the edit
// already finished, the server silently ignores the cancel and we'll get
// edit_applied / edit_rejected as normal, which also clears the bar.
// Disable the button until the round-trip completes so the user can't
// spam-cancel.
if (editCancelBtn) {
  editCancelBtn.addEventListener("click", () => {
    if (!activeEditRefId) return;
    ws.send(JSON.stringify({
      type: "cancel_edit",
      ref_id: activeEditRefId,
    }));
    editCancelBtn.disabled = true;
  });
}

// Send button — dispatches by mode.
chatSendBtn.addEventListener("click", () => {
  const target = getActiveTarget();
  if (!target) return;
  setChatError("");
  if (chatMode === "upload") {
    chatFileInput.click();
    return;
  }
  const value = chatInput.value.trim();
  if (!value) return;
  const ref_id = newRefId();
  const payload = chatMode === "llm"
    ? { user_message: value }
    : { new_value: value };
  ws.send(JSON.stringify({
    type: "request_edit",
    ref_id,
    target_path: target.path,
    mode: chatMode,
    payload,
  }));
  // Echo the user message into local history immediately so the user
  // sees feedback before the server's edit_applied ack arrives. For
  // LLM mode this also gives the conversation a "you said:" beat.
  if (chatMode === "llm") {
    appendChatEntry(target.path, "user", value);
    // Local "正在生成回复…" indicator right under the user message.
    // The top edit-progress-bar is the global lock, but the user's
    // eye is on the chat panel — without this they think nothing
    // happened for the 30-60s the LLM call takes.
    appendPendingAssistant(target.path, ref_id);
  } else {
    // Direct mode — surface what was sent as a one-liner.
    const preview = value.length > 120 ? value.slice(0, 120) + "…" : value;
    appendChatEntry(target.path, "user", `[direct edit] ${preview}`);
  }
  chatInput.value = "";
  chatSendBtn.disabled = true;
  // LLM edits are long-running (30-60s). Flip the global "正在修改" lock
  // on so the user gets a visible progress bar + cancel button and every
  // other mutating affordance is dimmed. The label is built from the
  // active target so the user can tell which slide/element is being
  // edited at a glance.
  if (chatMode === "llm") {
    const label = buildEditProgressLabel(target);
    setEditInProgress(ref_id, label);
  }
});

// Build the human-readable progress label for an in-flight LLM edit.
// Falls back to a generic "正在修改…" when target metadata is missing
// (e.g. theme/outline targets carry no slide_idx).
function buildEditProgressLabel(target) {
  if (!target) return "正在修改…";
  const meta = target.meta || {};
  if (typeof meta.slide_idx === "number" || typeof meta.slide_idx === "string") {
    return `正在修改 slide ${meta.slide_idx}…`;
  }
  const stage = target.stage || "element";
  return `正在修改 ${stage}…`;
}

// Delete button — image targets only. Pops the slot from state; the
// slide regenerates without the <img>. Rides the existing request_edit
// path with a {"delete": true} sentinel so undo / history / stale
// marks all work the same as a direct edit.
chatDeleteBtn.addEventListener("click", () => {
  const target = getActiveTarget();
  if (!target || target.kind !== "image") return;
  setChatError("");
  const ref_id = newRefId();
  ws.send(JSON.stringify({
    type: "request_edit",
    ref_id,
    target_path: target.path,
    mode: "direct",
    payload: { delete: true },
  }));
  appendChatEntry(target.path, "user", "[delete image]");
  // Optimistic hide — stage_complete will re-render the panel and
  // current_value will be empty, so the button stays hidden.
  chatDeleteBtn.hidden = true;
});

// File picker — chosen via the Upload-mode Send button.
chatFileInput.addEventListener("change", async () => {
  const target = getActiveTarget();
  if (!target || !chatFileInput.files.length) return;
  const file = chatFileInput.files[0];
  const ref_id = newRefId();
  const meta = target.meta || {};
  const slideIdx = meta.slide_idx;
  const slotId = meta.slot_id;
  // 2 MB threshold — below it, WS base64 is fine; above it, multipart
  // avoids bloating WS frame buffers (and uvicorn's per-frame limit).
  const WS_MAX = 2 * 1024 * 1024;
  if (file.size <= WS_MAX) {
    // Small file — read as base64, send via WS.
    const buf = await file.arrayBuffer();
    // btoa needs a binary string. Build from bytes to survive UTF-8.
    const bytes = new Uint8Array(buf);
    let bin = "";
    for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
    const data_b64 = btoa(bin);
    // Trim user-typed description; empty string is the "auto-generate
    // via VLM" sentinel for the server. Pulled fresh at upload time so
    // the user can edit between selecting and sending.
    const desc = (chatImgDesc.value || "").trim();
    ws.send(JSON.stringify({
      type: "upload_image",
      ref_id,
      target_path: target.path,
      mime: file.type || "application/octet-stream",
      data_b64,
      filename: file.name,
      description: desc,
    }));
  } else {
    // Larger file — multipart POST to /upload.
    setChatError("Uploading large file…");
    const fd = new FormData();
    fd.append("slide_idx", String(slideIdx));
    fd.append("slot_id", String(slotId));
    fd.append("ref_id", ref_id);
    fd.append("description", (chatImgDesc.value || "").trim());
    fd.append("file", file, file.name);
    try {
      const resp = await fetch("/upload", { method: "POST", body: fd });
      const json = await resp.json();
      if (!json.ok) {
        setChatError(json.error || "upload failed");
        return;
      }
      setChatError("");
      // Surface the landed description (mirrors the WS ack path —
      // image uploads carry no diff, so without this the multipart
      // upload would be silent). The drag-drop path skips this on
      // purpose: drag-drop has no chat context to write to.
      if (json.description !== null && json.description !== undefined) {
        const label = json.description
          ? `Description: "${json.description}"`
          : "Uploaded (no description — VLM unavailable or disabled)";
        appendChatEntry(target.path, "applied", label);
      }
      setChatEditedFlag(target.path, true);
    } catch (err) {
      setChatError(`upload failed: ${err}`);
    }
  }
  // Reset so picking the same file twice still fires change.
  chatFileInput.value = "";
  // Match the file-input clear: a fresh upload is a fresh caption
  // context. The description that landed is visible in the slide
  // snapshot's image meta.
  chatImgDesc.value = "";
});

// Undo — pops the server's undo stack.
chatUndoBtn.addEventListener("click", () => {
  const target = getActiveTarget();
  if (!target) return;
  const ref_id = newRefId();
  ws.send(JSON.stringify({
    type: "undo",
    ref_id,
    target_path: target.path,
  }));
  setChatError("");
});

// Theme color picker — any .swatch-color-input change inside preview.
// Edit mode only: updates the local draft and re-renders. The server is
// only contacted when the user clicks Commit (see commitThemeDraft).
previewContent.addEventListener("change", (e) => {
  if (!e.target.matches(".swatch-color-input")) return;
  if (themeDraft === null) return;   // not in edit mode, ignore
  const swatch = e.target.closest(".color-swatch");
  if (!swatch) return;
  const key = swatch.dataset.colorKey;
  themeDraft[key] = e.target.value;
  // Re-render against the draft. Pass serverTheme as a fallback for
  // fields the draft doesn't override (draft is a shallow clone of
  // serverTheme on entry, so this is just defensive).
  const serverTheme = snapshots.theme?.state_view?.theme || {};
  renderThemePreview(serverTheme);
});

// Image Replace — thumbnail badge OR preview "Replace image" button.
// Both carry data-slide + data-slot; this handler finds the matching
// thumbnail, selects it (so chat panel targets the right slot), then
// opens the file picker. Only fires on raster image targets — SVG
// slots have no Replace badge (their badge is non-interactive).
previewContent.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-slide][data-slot]");
  if (!btn) return;
  // Only the Replace button should trigger; thumbnail badge for SVG
  // is a <span> without data-slide/data-slot (filtered out by closest).
  if (!btn.matches(".thumb-replace-btn, .preview-replace-btn")) return;
  jumpToImageTarget(Number(btn.dataset.slide), btn.dataset.slot);
});

// Switch the UI to the images stage and select the matching thumbnail.
// Shared by the Replace button (opens file picker afterwards) and the
// layers panel image-row click (no picker — just focus + flash).
//
// slot_id matching: callers pass whatever they have. Replace buttons
// carry the bare state key ("hero"), but slide HTML's img.src may carry
// the filename ("slide_1_hero.svg"). Substring fallback covers both.
function jumpToImageTarget(slideIdx, slotId, openPicker = true) {
  const snap = snapshots.images;
  if (!snap) return;
  const flat = flattenImages(snap);
  // Exact match first.
  let idx = flat.findIndex(
    (it) => Number(it.slideIdx) === Number(slideIdx) && it.slotId === slotId
  );
  // Substring fallback: covers "slide_1_hero.svg" ↔ "hero".
  if (idx < 0) {
    idx = flat.findIndex((it) => {
      if (Number(it.slideIdx) !== Number(slideIdx)) return false;
      const a = it.slotId, b = slotId;
      if (!a || !b) return false;
      return a.includes(b) || b.includes(a);
    });
  }
  if (idx < 0) return;
  // Switch focus to the matching thumbnail so the chat panel targets
  // the correct slot before the file picker opens.
  activeStage = "images";
  activeItemIdx = idx;
  renderAll();
  if (!openPicker) return;
  // Give renderAll a tick to lay out the chat panel before opening
  // the file picker (some browsers refuse .click() during reflow).
  requestAnimationFrame(() => {
    const target = getActiveTarget();
    if (target && target.kind === "image") {
      chatFileInput.click();
    }
  });
}

// =====================================================================
// Inline text editing on the slide preview iframe (#1).
// Double-click a text element (h1-h6, p, li, span, ...) → contenteditable
// + floating commit bar. Commit sends the slide's .ppt-slide outerHTML
// via the existing ["slide", N, "html"] direct-edit path; the server
// re-broadcasts stage_complete and the iframe reloads with new content.
// =====================================================================

// CSS injected into the iframe document so the edit affordances style
// correctly inside the slide (the parent doc's styles.css does NOT
// cascade into iframe content).
const _INLINE_EDIT_CSS = `
.shuttleslide-inline-editing {
  outline: 2px solid #6366f1 !important;
  outline-offset: 2px;
  background: rgba(99, 102, 241, 0.06);
  cursor: text !important;
  border-radius: 2px;
}
.shuttleslide-inline-bar {
  position: fixed;
  top: 8px;
  right: 12px;
  z-index: 99999;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: rgba(255, 255, 255, 0.97);
  border: 1px solid rgba(0, 0, 0, 0.12);
  border-radius: 6px;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18);
  font-family: system-ui, -apple-system, sans-serif;
  font-size: 12px;
  color: #1c1c1e;
}
.shuttleslide-inline-bar button {
  padding: 4px 10px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid rgba(0, 0, 0, 0.15);
  border-radius: 3px;
  background: white;
  color: #1c1c1e;
  cursor: pointer;
}
.shuttleslide-inline-bar .shuttleslide-inline-commit {
  background: #6366f1;
  color: white;
  border-color: #6366f1;
}
.shuttleslide-inline-bar .shuttleslide-inline-commit:hover { background: #5457e5; }
.shuttleslide-inline-bar .shuttleslide-inline-cancel:hover { background: #fef2f2; }
.shuttleslide-inline-bar .shuttleslide-inline-hint {
  color: #6e6e73;
  font-size: 11px;
  margin-left: 4px;
}
/* SVG <img> object editor (#5b) — selected outline + corner handles */
.shuttleslide-img-selected {
  outline: 2px solid #6366f1 !important;
  outline-offset: 2px;
}
.shuttleslide-img-handle {
  position: fixed !important;
  width: 12px !important;
  height: 12px !important;
  margin: -6px !important;
  background: #ffffff !important;
  border: 1.5px solid #6366f1 !important;
  border-radius: 50% !important;
  z-index: 99998 !important;
  box-shadow: 0 1px 4px rgba(0,0,0,0.2) !important;
}
.shuttleslide-img-handle-nw, .shuttleslide-img-handle-se { cursor: nwse-resize !important; }
.shuttleslide-img-handle-ne, .shuttleslide-img-handle-sw { cursor: nesw-resize !important; }
.shuttleslide-img-toolbar {
  position: fixed !important;
  bottom: 12px !important;
  right: 12px !important;
  z-index: 99999 !important;
  display: flex !important;
  gap: 6px !important;
  padding: 6px 10px !important;
  background: rgba(255, 255, 255, 0.97) !important;
  border: 1px solid rgba(0, 0, 0, 0.12) !important;
  border-radius: 6px !important;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18) !important;
  font-family: system-ui, -apple-system, sans-serif !important;
  font-size: 12px !important;
  color: #1c1c1e !important;
}
.shuttleslide-img-toolbar button {
  padding: 4px 10px !important;
  font-size: 12px !important;
  font-weight: 500 !important;
  border-radius: 3px !important;
  cursor: pointer !important;
  border: 1px solid transparent !important;
}
.shuttleslide-img-toolbar .img-commit-btn {
  background: #6366f1 !important;
  color: white !important;
  border-color: #6366f1 !important;
}
.shuttleslide-img-toolbar .img-commit-btn:hover { background: #5457e5 !important; }
.shuttleslide-img-toolbar .img-cancel-btn {
  background: white !important;
  color: #1c1c1e !important;
  border-color: rgba(0, 0, 0, 0.15) !important;
}
.shuttleslide-img-toolbar .img-cancel-btn:hover { background: #fef2f2 !important; }
.shuttleslide-img-toolbar .img-delete-btn {
  background: white !important;
  color: #b91c1c !important;
  border-color: #fecaca !important;
}
.shuttleslide-img-toolbar .img-delete-btn:hover {
  background: #fef2f2 !important;
  border-color: #fca5a5 !important;
}
.shuttleslide-img-hint {
  position: fixed !important;
  top: 50% !important;
  left: 50% !important;
  transform: translate(-50%, -50%) !important;
  padding: 10px 18px !important;
  background: rgba(28, 28, 30, 0.95) !important;
  color: white !important;
  border-radius: 8px !important;
  font-family: system-ui, sans-serif !important;
  font-size: 13px !important;
  z-index: 100000 !important;
  pointer-events: none !important;
  box-shadow: 0 4px 20px rgba(0, 0, 0, 0.3) !important;
}
/* Drag-and-drop image onto slide (#6) — visual states while a file is
   being dragged over the slide, while bytes are uploading, and on
   error. Lives inside the iframe doc (same context as .ppt-slide) so
   coordinates are slide-local. */
.shuttleslide-drop-overlay {
  position: absolute !important;
  inset: 0 !important;
  pointer-events: none !important;
  z-index: 99990 !important;
  background: rgba(99, 102, 241, 0.12) !important;
  border: 2px dashed #6366f1 !important;
  border-radius: 4px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  font-family: system-ui, sans-serif !important;
  font-size: 18px !important;
  font-weight: 600 !important;
  color: #6366f1 !important;
  text-shadow: 0 1px 0 white !important;
}
.shuttleslide-drop-overlay::before { content: "Drop image to add to slide"; }
.shuttleslide-drop-pending {
  position: absolute !important;
  z-index: 99991 !important;
  min-width: 120px !important;
  min-height: 60px !important;
  padding: 12px 16px !important;
  background: rgba(99, 102, 241, 0.18) !important;
  border: 2px dashed #6366f1 !important;
  border-radius: 4px !important;
  display: flex !important;
  align-items: center !important;
  justify-content: center !important;
  font-family: system-ui, sans-serif !important;
  font-size: 12px !important;
  color: #312e81 !important;
  background-color: rgba(238, 242, 255, 0.95) !important;
  pointer-events: none !important;
}
.shuttleslide-drop-error {
  position: absolute !important;
  z-index: 99992 !important;
  min-width: 200px !important;
  max-width: 360px !important;
  padding: 10px 14px !important;
  background: #fef2f2 !important;
  color: #991b1b !important;
  border: 1px solid #fecaca !important;
  border-radius: 4px !important;
  font-family: system-ui, sans-serif !important;
  font-size: 12px !important;
  pointer-events: auto !important;
  cursor: pointer !important;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1) !important;
}
img.shuttleslide-dropped-img {
  cursor: move !important;
  user-select: none !important;
  -webkit-user-drag: none !important;
}
/* Layers panel highlight lives in the parent doc, not the iframe —
   see showLayerOverlay() in app.js and #shuttleslide-layer-overlay in
   styles.css. We tried painting inside the iframe first; it failed
   because (a) slide HTML commonly wraps hero images in opacity:0.x
   containers, which dilutes any outline/box-shadow along with the
   image, (b) the iframe is CSS-scaled to ~0.49x so a 4-8px border
   becomes 2-4px on screen, and (c) ancestor overflow:hidden clips any
   outward outline. A parent-doc overlay sidesteps all three. */
`;

function _injectInlineEditStyles(doc) {
  if (doc.getElementById("shuttleslide-inline-edit-styles")) return;
  const style = doc.createElement("style");
  style.id = "shuttleslide-inline-edit-styles";
  style.textContent = _INLINE_EDIT_CSS;
  doc.head.appendChild(style);
}

function attachInlineEdit(iframe, slideIdx) {
  const doc = iframe.contentDocument;
  if (!doc) return;
  // Avoid double-binding if the same iframe fires load twice.
  if (doc._shuttleslideInlineEditBound) return;
  doc._shuttleslideInlineEditBound = true;
  _injectInlineEditStyles(doc);
  doc.addEventListener("dblclick", (e) => {
    // One active inline edit at a time across the document.
    if (doc.querySelector("[contenteditable='true']")) return;
    // Limit to text-bearing containers; exclude any element that
    // itself contains an img/svg/iframe (would be a wrapper, not text).
    const el = e.target.closest(
      "h1, h2, h3, h4, h5, h6, p, li, span, a, strong, em, td, th, blockquote"
    );
    if (!el) return;
    if (el.querySelector("img, svg, iframe")) return;
    e.preventDefault();
    enterInlineEdit(iframe, el, slideIdx);
  });
  // #5b: attach SVG <img> object editor (drag / resize whole image).
  _attachImgObjectEditor(iframe, slideIdx);
  // #6: attach drag-drop image target — drop a local PNG/JPEG onto the
  // slide to insert a brand-new <img>. See _attachSlideDropTarget.
  _attachSlideDropTarget(iframe, slideIdx);
}

function enterInlineEdit(iframe, el, slideIdx) {
  const doc = iframe.contentDocument;
  // Snapshot original innerHTML so cancel can restore it byte-for-byte.
  el.dataset.shuttleslideInlineOriginal = el.innerHTML;
  el.contentEditable = "true";
  el.classList.add("shuttleslide-inline-editing");
  el.focus();
  // Select all text inside the element so the user can type-over.
  const range = doc.createRange();
  range.selectNodeContents(el);
  const sel = iframe.contentWindow.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  // Floating commit bar — appended to body so it survives reflow.
  const bar = doc.createElement("div");
  bar.className = "shuttleslide-inline-bar";
  bar.innerHTML = `
    <button type="button" class="shuttleslide-inline-commit">✓ Commit</button>
    <button type="button" class="shuttleslide-inline-cancel">✗ Cancel</button>
    <span class="shuttleslide-inline-hint">Ctrl+Enter · Esc</span>
  `;
  doc.body.appendChild(bar);
  // Keymap scoped to this edit — removed on commit/cancel to avoid
  // stacking handlers across edits.
  const onKey = (ev) => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      _commitInlineEdit(iframe, el, slideIdx, bar, onKey);
    } else if (ev.key === "Escape") {
      ev.preventDefault();
      _cancelInlineEdit(iframe, el, bar, onKey);
    }
  };
  el.addEventListener("keydown", onKey);
  bar.querySelector(".shuttleslide-inline-commit").addEventListener("click", () =>
    _commitInlineEdit(iframe, el, slideIdx, bar, onKey)
  );
  bar.querySelector(".shuttleslide-inline-cancel").addEventListener("click", () =>
    _cancelInlineEdit(iframe, el, bar, onKey)
  );
}

function _commitInlineEdit(iframe, el, slideIdx, bar, onKey) {
  const doc = iframe.contentDocument;
  // Teardown edit affordances BEFORE serialising so the outline /
  // data-attribute don't leak into the committed HTML.
  el.removeEventListener("keydown", onKey);
  el.contentEditable = "false";
  el.classList.remove("shuttleslide-inline-editing");
  delete el.dataset.shuttleslideInlineOriginal;
  if (bar.parentNode) bar.parentNode.removeChild(bar);
  // Reuse the shared _commitSlideHtml helper so the innerHTML-vs-
  // outerHTML fix (free_form template wraps slots.html in .ppt-slide)
  // stays in one place. Inline edits go through the same SlideEditor
  // direct-edit path as drag-drop auto-commits.
  _commitSlideHtml(slideIdx, doc);
}

function _cancelInlineEdit(iframe, el, bar, onKey) {
  // Restore original HTML so any partial typing is discarded.
  if (el.dataset.shuttleslideInlineOriginal !== undefined) {
    el.innerHTML = el.dataset.shuttleslideInlineOriginal;
    delete el.dataset.shuttleslideInlineOriginal;
  }
  el.removeEventListener("keydown", onKey);
  el.contentEditable = "false";
  el.classList.remove("shuttleslide-inline-editing");
  if (bar.parentNode) bar.parentNode.removeChild(bar);
}

// =====================================================================
// SVG element editor for the images stage (#5a).
// The SVG payload is rendered inline in the parent document (not in an
// iframe), so we can attach drag/double-click listeners directly to its
// child elements. Commit serialises the modified <svg> back to the
// server via the ["slide", N, "slot", id] SVG direct-edit path.
// =====================================================================

// 默认非编辑态：监听 host 双击 → 进入编辑模式。
// 不直接 attach 编辑监听 — 让用户先决定是否要改 SVG。
function attachSvgHostActivator(host, slideIdx, slotId) {
  host.addEventListener("dblclick", (e) => {
    if (host.classList.contains("editing")) return;
    enterSvgEditMode(host, slideIdx, slotId);
  });
}

function enterSvgEditMode(host, slideIdx, slotId) {
  host.classList.add("editing");
  // 注入 toolbar（CSS 控制默认隐藏 overlay，编辑态隐藏由 .editing 类驱动）
  const toolbar = document.createElement("div");
  toolbar.className = "svg-edit-toolbar";
  toolbar.innerHTML = `
    <span class="svg-edit-hint">Click shape to drag · Double-click text to edit</span>
    <button type="button" class="svg-commit-btn">✓ Commit</button>
    <button type="button" class="svg-cancel-btn">✗ Cancel</button>
  `;
  host.prepend(toolbar);
  attachSvgElementEditor(host, slideIdx, slotId);
}

function attachSvgElementEditor(host, slideIdx, slotId) {
  const svgCanvas = host.querySelector(".svg-canvas");
  const svgEl = svgCanvas && svgCanvas.querySelector("svg");
  if (!svgEl) return;

  let selected = null, dragStart = null, origTransform = null;
  const SHAPE_SELECTOR =
    "rect, circle, ellipse, path, g, text, line, polygon, polyline";

  // mousedown on shape/text → 一步到位：选中 + 启动 drag。
  // text 与 shape 一样可以拖动（修改 transform）。
  // 双击 text 触发浮层编辑是在 mouseup 后的 dblclick 事件，与此不冲突。
  svgEl.addEventListener("mousedown", (e) => {
    // 浮层 input 显示中，不启动 drag（用户在改文字）
    if (svgCanvas.querySelector(".svg-text-editor-input")) return;

    const el = e.target.closest(SHAPE_SELECTOR);
    if (!el || el === svgEl) return;
    e.preventDefault();
    // 切换选中（单击别的元素 = 改选）
    if (selected !== el) {
      _selectSvgElement(el, svgEl);
      selected = el;
    }
    dragStart = { x: e.clientX, y: e.clientY };
    origTransform = el.getAttribute("transform") || "";
  });

  // mousemove (anywhere on the canvas) → drag the selected element.
  svgCanvas.addEventListener("mousemove", (e) => {
    if (!selected || !dragStart) return;
    const ctm = svgEl.getScreenCTM();
    if (!ctm) return;
    const inv = ctm.inverse();
    // 把屏幕坐标的两个绝对点变到 SVG 坐标系，再相减得到 SVG 空间下的 delta。
    // 直接对 delta 做 matrixTransform 会错误应用 CTM 的平移分量 —
    // SVGPoint.matrixTransform 把输入当作点而非方向向量，平移分量会被
    // 加到 delta 上，导致元素飞出画布。
    const ptStart = svgEl.createSVGPoint();
    ptStart.x = dragStart.x; ptStart.y = dragStart.y;
    const ptNow = svgEl.createSVGPoint();
    ptNow.x = e.clientX; ptNow.y = e.clientY;
    const svgStart = ptStart.matrixTransform(inv);
    const svgNow = ptNow.matrixTransform(inv);
    const svgDx = svgNow.x - svgStart.x;
    const svgDy = svgNow.y - svgStart.y;
    selected.setAttribute(
      "transform",
      `translate(${svgDx},${svgDy}) ${origTransform}`.trim()
    );
  });

  svgCanvas.addEventListener("mouseup", () => { dragStart = null; });
  svgCanvas.addEventListener("mouseleave", () => { dragStart = null; });

  // 双击 <text>/<tspan> → 浮层 input 所见即所得编辑。
  // 双击 SVG 空白处 → 退出选中状态。
  svgEl.addEventListener("dblclick", (e) => {
    const text = e.target.closest("text, tspan");
    if (text) {
      e.preventDefault();
      openSvgTextEditor(text, svgCanvas);
      return;
    }
    if (e.target === svgEl) {
      _clearSvgSelection(svgEl);
      selected = null;
      dragStart = null;
    }
  });

  // 单击 SVG 空白处 → 取消选中（mousedown 没 preventDefault 的话才会冒泡到 click）
  svgEl.addEventListener("click", (e) => {
    if (e.target === svgEl) {
      _clearSvgSelection(svgEl);
      selected = null;
      dragStart = null;
    }
  });

  // Toolbar buttons.
  host.querySelector(".svg-commit-btn").addEventListener("click", () => {
    _commitSvgEdit(svgEl, slideIdx, slotId);
  });
  host.querySelector(".svg-cancel-btn").addEventListener("click", () => {
    // 重画丢弃所有 DOM 修改（包括未提交的拖拽/文字编辑）。
    // host 是新 DOM 元素，所有监听自动失效。
    renderPreview();
  });
}

// 浮层 HTML input 覆盖在 SVG <text> 位置，所见即所得编辑。
// 替代之前的 window.prompt() — 体验与 PPT 内嵌文字编辑一致。
//
// 设计要点：
// - input 字体/字号/颜色尽量匹配 SVG text（getComputedStyle 已含 CSS
//   缩放转换后的 px 字号），实现近似所见即所得
// - input 实时同步 text.textContent，用户每按一个字 SVG 立即更新
// - blur/Enter 提交；Escape 恢复原始值
// - input remove() 不触发 blur（spec），无需额外守卫
function openSvgTextEditor(textEl, svgCanvas) {
  // 已存在 editor → 先移除（防止多个 input 堆积）
  svgCanvas.querySelectorAll(".svg-text-editor-input").forEach(n => n.remove());

  const textRect = textEl.getBoundingClientRect();
  const canvasRect = svgCanvas.getBoundingClientRect();
  const left = textRect.left - canvasRect.left;
  const top = textRect.top - canvasRect.top;

  // 字体匹配（近似）：getComputedStyle 返回的 fontSize 已经是屏幕 px
  const computed = window.getComputedStyle(textEl);
  const fontSize = parseFloat(computed.fontSize) || 14;
  const fill = textEl.getAttribute("fill") || computed.fill || "#000";
  const fontFamily = computed.fontFamily || "inherit";
  const fontWeight = computed.fontWeight || "normal";
  const fontStyle = computed.fontStyle || "normal";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "svg-text-editor-input";
  input.value = textEl.textContent || "";
  input.dataset.original = input.value;
  input.style.left = `${left}px`;
  input.style.top = `${top}px`;
  input.style.fontSize = `${fontSize}px`;
  input.style.fontFamily = fontFamily;
  input.style.fontWeight = fontWeight;
  input.style.fontStyle = fontStyle;
  input.style.color = fill;
  input.style.minWidth = `${Math.max(60, textRect.width)}px`;
  svgCanvas.appendChild(input);
  input.focus();
  input.select();

  // 实时同步 — 所见即所得
  input.addEventListener("input", () => {
    textEl.textContent = input.value;
  });

  let finished = false;
  const finish = (commit) => {
    if (finished) return;
    finished = true;
    if (!commit) {
      textEl.textContent = input.dataset.original || "";
    }
    input.remove();
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); finish(true); }
    else if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
  });
}

function _selectSvgElement(el, svgEl) {
  _clearSvgSelection(svgEl);
  el.classList.add("svg-element-selected");
}

function _clearSvgSelection(svgEl) {
  svgEl.querySelectorAll(".svg-element-selected").forEach((n) =>
    n.classList.remove("svg-element-selected"));
}

function _commitSvgEdit(svgEl, slideIdx, slotId) {
  // Teardown selection state BEFORE serialising so the dashed outline
  // doesn't leak into the saved SVG markup.
  _clearSvgSelection(svgEl);
  // XMLSerializer preserves the SVG namespace and attribute order;
  // outerHTML would drop the xmlns on some browsers.
  const serializer = new XMLSerializer();
  const newSvg = serializer.serializeToString(svgEl);
  ws.send(JSON.stringify({
    type: "request_edit",
    ref_id: newRefId(),
    target_path: ["slide", slideIdx, "slot", slotId],
    mode: "direct",
    payload: { new_value: newSvg },
  }));
}

// =====================================================================
// SVG <img> object editor for the slides stage (#5b).
// In slide HTML, SVG slots are rendered as <img class="shuttleslide-
// svg-placeholder" src="svgs/...">. We support selecting the whole img,
// dragging it (translate), and resizing via 4 corner handles (scale,
// aspect-ratio preserved). Internal SVG editing happens in the images
// stage (#5a); double-clicking an img here surfaces a hint telling the
// user where to go.
// =====================================================================

function _attachImgObjectEditor(iframe, slideIdx) {
  const doc = iframe.contentDocument;
  if (!doc) return;
  // Per-iframe state — selection, current transform, active handles.
  let selected = null;
  let handles = [];
  let toolbar = null;
  let transform = { tx: 0, ty: 0, scale: 1 };
  let initialTransformCss = "";
  // Set by resize onUp so the click that follows mouseup doesn't get
  // misread as "click on empty space" and cancel the edit. Cleared next tick.
  let suppressClick = false;

  const HANDLES = ["nw", "ne", "sw", "se"];

  function clearSelection() {
    if (selected) selected.classList.remove("shuttleslide-img-selected");
    selected = null;
    handles.forEach((h) => h.parentNode && h.parentNode.removeChild(h));
    handles = [];
    if (toolbar && toolbar.parentNode) toolbar.parentNode.removeChild(toolbar);
    toolbar = null;
  }

  function positionHandles() {
    if (!selected) return;
    const r = selected.getBoundingClientRect();
    HANDLES.forEach((pos, i) => {
      const h = handles[i];
      if (!h) return;
      // corners of the img in viewport coords; handle CSS uses margin:-6
      // so its centre lands exactly on the corner.
      if (pos === "nw") { h.style.left = `${r.left}px`; h.style.top = `${r.top}px`; }
      if (pos === "ne") { h.style.left = `${r.right}px`; h.style.top = `${r.top}px`; }
      if (pos === "sw") { h.style.left = `${r.left}px`; h.style.top = `${r.bottom}px`; }
      if (pos === "se") { h.style.left = `${r.right}px`; h.style.top = `${r.bottom}px`; }
    });
  }

  function applyTransform() {
    if (!selected) return;
    const parts = [];
    if (initialTransformCss) parts.push(initialTransformCss);
    if (transform.tx !== 0 || transform.ty !== 0) {
      parts.push(`translate(${transform.tx}px, ${transform.ty}px)`);
    }
    if (transform.scale !== 1) parts.push(`scale(${transform.scale})`);
    selected.style.transform = parts.join(" ");
    positionHandles();
  }

  function selectImg(img) {
    if (selected === img) return;
    clearSelection();
    selected = img;
    img.classList.add("shuttleslide-img-selected");
    transform = { tx: 0, ty: 0, scale: 1 };
    initialTransformCss = img.style.transform || "";
    // Spawn 4 corner handles in body (position: fixed).
    HANDLES.forEach((pos) => {
      const h = doc.createElement("div");
      h.className = `shuttleslide-img-handle shuttleslide-img-handle-${pos}`;
      h.dataset.handle = pos;
      doc.body.appendChild(h);
      handles.push(h);
    });
    // Spawn toolbar.
    toolbar = doc.createElement("div");
    toolbar.className = "shuttleslide-img-toolbar";
    toolbar.innerHTML = `
      <button type="button" class="img-commit-btn">✓ Commit</button>
      <button type="button" class="img-delete-btn">🗑 Delete</button>
      <button type="button" class="img-cancel-btn">✗ Cancel</button>
      <span style="color:#6e6e73;font-size:11px;">Ctrl+Enter · Esc</span>
    `;
    doc.body.appendChild(toolbar);
    toolbar.querySelector(".img-commit-btn").addEventListener("click", commit);
    toolbar.querySelector(".img-delete-btn").addEventListener("click", del);
    toolbar.querySelector(".img-cancel-btn").addEventListener("click", cancel);
    positionHandles();
  }

  function commit() {
    if (!selected) return;
    // Remove edit-only state before serialising so it doesn't leak.
    selected.classList.remove("shuttleslide-img-selected");
    clearSelection();
    _commitSlideHtml(slideIdx, doc);
  }

  function del() {
    if (!selected) return;
    const img = selected;
    // data-slot is the universal anchor for state-backed images — it
    // covers svg_file (SVG placeholder) AND image_file (raster web
    // photo) payloads. The class check is kept as a legacy fallback for
    // slide HTML predating the data-slot convention. Dropped-img has
    // neither and lives only in the slide HTML.
    const slotId = img.dataset.slot ||
      extractSlotIdFromSrc(img.getAttribute("src") || "");
    const isStateBacked = img.hasAttribute("data-slot") ||
      img.classList.contains("shuttleslide-svg-placeholder");
    // Tear down the toolbar first — the selected reference goes stale
    // once the slide re-renders (backend delete) or the img is removed
    // (HTML-only delete).
    clearSelection();
    if (isStateBacked && slotId) {
      // State-backed slot (svg_file or image_file): send the delete
      // sentinel so the backend pops state.slide_images[slideIdx][slotId]
      // and strips the matching <img> from slide HTML. Undo restores via
      // _restore_image_path. Mirrors the chat-panel delete button's wire
      // format exactly.
      ws.send(JSON.stringify({
        type: "request_edit",
        ref_id: newRefId(),
        target_path: ["slide", slideIdx, "slot", slotId],
        mode: "direct",
        payload: { delete: true },
      }));
    } else {
      // Dropped raster image (shuttleslide-dropped-img): lives only in
      // the slide HTML — no entry in state.slide_images. Remove the
      // <img> from the live DOM and commit, same path as drag-drop
      // insert but subtracting instead of adding. Also covers the
      // defensive case where a state-backed img's src didn't parse to
      // a slotId — HTML removal is the only available lever then.
      img.remove();
      _commitSlideHtml(slideIdx, doc);
    }
  }

  function cancel() {
    if (selected) {
      // Revert transform to its pre-edit value.
      selected.style.transform = initialTransformCss;
    }
    clearSelection();
  }

  // Click selects an img (or clears selection if clicking empty space).
  // Selector covers every editable image:
  //   - SVG placeholders (class-based, the original affordance)
  //   - User-dropped raster (class-based, drag-drop feature #6)
  //   - State-backed raster image_file (data-slot anchor). These come
  //     from web image search and have no class — only data-slot. Without
  //     this branch, image_file imgs in the slides stage were never
  //     selectable, which is why the toolbar appeared in SVG-heavy runs
  //     but not in runs that used raster web photos.
  doc.addEventListener("click", (e) => {
    // Skip the synthetic click that fires right after a resize drag —
    // its target often lands outside the handle (the handle moves with
    // the scaling image) and would otherwise trigger cancel() below.
    if (suppressClick) return;
    if (e.target.matches && e.target.matches(
      "img.shuttleslide-svg-placeholder, img.shuttleslide-dropped-img, img[data-slot]"
    )) {
      // Don't re-select the same one (would reset transform mid-edit).
      if (selected !== e.target) selectImg(e.target);
      return;
    }
    // Click outside any img and outside the toolbar/handles → deselect.
    if (selected && !e.target.closest(".shuttleslide-img-toolbar, .shuttleslide-img-handle")) {
      cancel();
    }
  });

  // Double-click hint — differs by image type:
  //   SVG placeholder: jump to images stage to edit SVG internals
  //   state-backed raster (image_file): editable here, no SVG internals
  //   dropped-img: reminder that move/resize live in this view
  doc.addEventListener("dblclick", (e) => {
    if (!e.target.matches) return;
    if (e.target.matches("img.shuttleslide-svg-placeholder")) {
      e.preventDefault();
      _showImgHint(doc, "Switch to the images stage to edit SVG internals");
    } else if (e.target.matches("img.shuttleslide-dropped-img")) {
      e.preventDefault();
      _showImgHint(doc, "Drag to move · corner handles to resize · Commit (✓) to save");
    } else if (e.target.matches("img[data-slot]")) {
      // Raster image_file (web photo): same affordance as dropped —
      // the images stage has no SVG internals to edit for raster slots.
      e.preventDefault();
      _showImgHint(doc, "Drag to move · corner handles to resize · Commit (✓) to save");
    }
  });

  // Mousedown on selected img body → start drag.
  doc.addEventListener("mousedown", (e) => {
    if (!selected) return;
    if (e.target === selected) {
      e.preventDefault();
      const startX = e.clientX, startY = e.clientY;
      const orig = { ...transform };
      const onMove = (ev) => {
        transform.tx = orig.tx + (ev.clientX - startX);
        transform.ty = orig.ty + (ev.clientY - startY);
        applyTransform();
      };
      const onUp = () => {
        doc.removeEventListener("mousemove", onMove);
        doc.removeEventListener("mouseup", onUp);
      };
      doc.addEventListener("mousemove", onMove);
      doc.addEventListener("mouseup", onUp);
      return;
    }
    // Mousedown on a handle → start resize.
    const handle = e.target.closest && e.target.closest(".shuttleslide-img-handle");
    if (handle && selected) {
      e.preventDefault();
      e.stopPropagation();
      const startX = e.clientX, startY = e.clientY;
      const origW = selected.offsetWidth || 1;
      const origScale = transform.scale;
      const onMove = (ev) => {
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;
        // Each corner's "outward" direction differs — project the drag
        // onto that vector so a positive projection always means grow.
        // The previous (dx+dy)>=0 sign logic only worked for se; nw was
        // inverted and ne/sw were unstable.
        const outward = { se: [1, 1], nw: [-1, -1], ne: [1, -1], sw: [-1, 1] };
        const [ox, oy] = outward[handle.dataset.handle] || [1, 1];
        const proj = dx * ox + dy * oy;
        const factor = 1 + proj / origW;
        transform.scale = Math.max(0.1, origScale * factor);
        applyTransform();
      };
      const onUp = () => {
        suppressClick = true;
        doc.removeEventListener("mousemove", onMove);
        doc.removeEventListener("mouseup", onUp);
        // click fires synchronously right after mouseup; clear once it
        // has been dispatched so the next genuine click is honored.
        setTimeout(() => { suppressClick = false; }, 0);
      };
      doc.addEventListener("mousemove", onMove);
      doc.addEventListener("mouseup", onUp);
    }
  });

  // Ctrl+Enter commits, Esc cancels (only when an img is selected).
  doc.addEventListener("keydown", (e) => {
    if (!selected) return;
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      commit();
    } else if (e.key === "Escape") {
      e.preventDefault();
      cancel();
    }
  });

  // Reposition handles on iframe resize (scaleBigSlide changes viewport).
  iframe.contentWindow.addEventListener("resize", positionHandles);
}

function _showImgHint(doc, message) {
  // Transient floating hint — auto-dismissed after 1.8s.
  const existing = doc.querySelector(".shuttleslide-img-hint");
  if (existing) existing.parentNode && existing.parentNode.removeChild(existing);
  const hint = doc.createElement("div");
  hint.className = "shuttleslide-img-hint";
  hint.textContent = message;
  doc.body.appendChild(hint);
  setTimeout(() => {
    if (hint.parentNode) hint.parentNode.removeChild(hint);
  }, 1800);
}

// Shared slide-HTML commit path — used by the in-place img editor's
// Commit button AND by the drag-drop insert flow. Serialises the live
// .ppt-slide innerHTML and posts a direct edit to the server, which
// replaces state.slides[idx].slots["html"] wholesale (SlideEditor).
//
// IMPORTANT: persist .innerHTML, NOT .outerHTML. The free_form layout
// template (free_form.html.j2) wraps slots.html inside a .ppt-slide
// container when serving /artifact/slides/<idx>. Sending outerHTML
// would persist the wrapper too, and the next render would wrap it
// AGAIN — producing nested .ppt-slide divs every commit. This latent
// bug also affects the existing SVG-img commit path; fixing it here
// covers both call sites (commit button + drag-drop auto-commit).
//
// Guard: blur any [contenteditable] in the slide doc before serialising.
// Without this, an unrelated in-progress text edit would (a) lose its
// focus-driven caret state and (b) leak the contenteditable attribute
// into persisted HTML.
function _commitSlideHtml(slideIdx, doc) {
  const slideEl = doc.querySelector(".ppt-slide");
  if (!slideEl) return;
  doc.querySelectorAll("[contenteditable=true]").forEach((el) => {
    try { el.blur(); } catch (_) {}
  });
  const newHtml = slideEl.innerHTML;
  ws.send(JSON.stringify({
    type: "request_edit",
    ref_id: newRefId(),
    target_path: ["slide", slideIdx, "html"],
    mode: "direct",
    payload: { new_value: newHtml },
  }));
}

// =====================================================================
// Drag-and-drop image onto slide (#6)
//
// User drops a local PNG/JPEG on the slide iframe. We:
//   1. validate MIME (raster only; SVG rejected for v1)
//   2. compute slide-canvas drop coords (undo the iframe CSS scale)
//   3. show a placeholder div at the drop point
//   4. upload bytes via the existing /upload or WS upload_image path
//      — target_path = ["slide", idx, "slot", "user_<ts>_<rand>"]
//      — server's _resolve_target synthesises an EditTarget for the
//        brand-new slot_id, ImageUploader._resolve_slot_payload creates
//        state.slide_images[idx][slot_id] on the fly
//   5. on ack: swap placeholder → <img class=shuttleslide-dropped-img>
//   6. auto-commit slide HTML immediately (per plan: eliminates the
//      reload race where another edit could wipe the un-persisted img)
//
// pendingDrops maps the WS ref_id to the placeholder element so the
// edit_applied dispatcher can finish the insert when the ack lands.
// =====================================================================
const pendingDrops = new Map();  // ref_id -> { placeholderEl, slideIdx, slotId, iframe }

function _attachSlideDropTarget(iframe, slideIdx) {
  const doc = iframe.contentDocument;
  if (!doc) return;
  if (doc._shuttleslideDropBound) return;
  doc._shuttleslideDropBound = true;

  // Depth counter — dragenter/dragleave fire on every nested element;
  // we only want to remove the overlay when the cursor leaves .ppt-slide
  // entirely.
  let dragDepth = 0;
  let overlay = null;

  function showOverlay() {
    if (overlay) return;
    const slide = doc.querySelector(".ppt-slide");
    if (!slide) return;
    overlay = doc.createElement("div");
    overlay.className = "shuttleslide-drop-overlay";
    slide.appendChild(overlay);
  }
  function hideOverlay() {
    if (overlay && overlay.parentNode) overlay.parentNode.removeChild(overlay);
    overlay = null;
  }

  doc.addEventListener("dragenter", (e) => {
    if (!_dropHasFile(e)) return;
    e.preventDefault();
    dragDepth += 1;
    showOverlay();
  });
  doc.addEventListener("dragover", (e) => {
    if (!_dropHasFile(e)) return;
    // preventDefault is mandatory — otherwise the browser would treat
    // the drop as "navigate to the file" and blow away the iframe.
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
  });
  doc.addEventListener("dragleave", (e) => {
    if (!_dropHasFile(e)) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) hideOverlay();
  });
  doc.addEventListener("drop", (e) => {
    if (!_dropHasFile(e)) return;
    e.preventDefault();
    dragDepth = 0;
    hideOverlay();
    const files = e.dataTransfer && e.dataTransfer.files;
    if (!files || !files.length) return;
    const file = files[0];
    // Slide-canvas coords: e.clientX/Y is in the iframe's viewport
    // (CSS-scaled). Recover canvas coords by dividing by the same
    // scale factor scaleBigSlide applies.
    const wrapper = document.querySelector(".big-slide-wrapper");
    const scale = wrapper ? (wrapper.clientWidth / getCanvasWidthPx()) : 1;
    const x = e.clientX / scale;
    const y = e.clientY / scale;
    _handleDroppedFile(file, iframe, slideIdx, x, y);
  });
}

function _dropHasFile(e) {
  if (!e.dataTransfer) return false;
  // Browsers expose types during dragover as ["Files"] without the
  // actual file list (security: avoid leaking file paths). On drop we
  // get the real files.
  if (e.type === "drop") return e.dataTransfer.files && e.dataTransfer.files.length > 0;
  const types = e.dataTransfer.types;
  if (!types) return false;
  for (let i = 0; i < types.length; i++) {
    if (types[i] === "Files") return true;
  }
  return false;
}

// Shared entry from drag-drop and the toolbar "add image" button.
// `x`/`y` are slide-canvas px; for the button path they default to
// a top-left offset (10% of slide size).
function _handleDroppedFile(file, iframe, slideIdx, x, y) {
  const doc = iframe.contentDocument;
  if (!doc) return;
  // MIME gate: image/* required, SVG rejected for v1.
  const mime = (file.type || "").toLowerCase();
  if (!mime.startsWith("image/")) {
    _showImgHint(doc, "Only image files can be dropped here");
    return;
  }
  if (mime === "image/svg+xml" || mime === "image/svg") {
    _showImgHint(
      doc,
      "SVG drop is not supported in v1 — use Direct mode in the images stage"
    );
    return;
  }

  const slide = doc.querySelector(".ppt-slide");
  if (!slide) return;

  // Insert placeholder so the user sees immediate feedback at the drop
  // point while bytes are uploading. Best-effort: if the iframe reloads
  // before upload completes (e.g. user triggers an unrelated edit),
  // the placeholder vanishes but the upload still finalises correctly
  // because _finalizeDrop reads from the cached snapshot, not the DOM.
  const placeholder = doc.createElement("div");
  placeholder.className = "shuttleslide-drop-pending";
  placeholder.textContent = "Uploading…";
  placeholder.style.left = `${x}px`;
  placeholder.style.top = `${y}px`;
  slide.appendChild(placeholder);

  const slotId = "user_" + Date.now().toString(36) + "_" +
    Math.random().toString(36).slice(2, 8);
  _uploadDroppedImage(file, slideIdx, slotId, x, y);
}

function _uploadDroppedImage(file, slideIdx, slotId, x, y) {
  const ref_id = newRefId();
  pendingDrops.set(ref_id, { slideIdx, slotId, x, y });
  const target_path = ["slide", slideIdx, "slot", slotId];

  // 2 MB WS / POST threshold — mirrors chatFileInput.change (app.js).
  const WS_MAX = 2 * 1024 * 1024;
  if (file.size <= WS_MAX) {
    file.arrayBuffer().then((buf) => {
      const bytes = new Uint8Array(buf);
      let bin = "";
      for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      const data_b64 = btoa(bin);
      ws.send(JSON.stringify({
        type: "upload_image",
        ref_id,
        target_path,
        mime: file.type || "application/octet-stream",
        data_b64,
        filename: file.name,
      }));
    }).catch((err) => _failDrop(ref_id, `Could not read file: ${err}`));
  } else {
    const fd = new FormData();
    fd.append("slide_idx", String(slideIdx));
    fd.append("slot_id", slotId);
    fd.append("ref_id", ref_id);
    fd.append("file", file, file.name);
    fetch("/upload", { method: "POST", body: fd })
      .then((r) => r.json())
      .then((json) => {
        if (!json.ok) {
          _failDrop(ref_id, json.error || "upload rejected");
          return;
        }
        _finalizeDrop(ref_id, json.new_path, json.width, json.height);
      })
      .catch((err) => _failDrop(ref_id, `Network error: ${err}`));
  }
}

// Called from the WS edit_applied dispatcher for ref_ids that match a
// pending drag-drop upload, AND directly from the HTTP /upload .then()
// chain.
//
// Race-resilient: builds the new slide HTML from the cached snapshot
// (snapshots["slides"]) rather than from the live iframe DOM. The
// server's broadcast for an image upload fires stage_complete BEFORE
// edit_applied (orchestrator._refresh_after_edit runs inside apply_edit,
// before _handle_upload_image sends the ack). The client processes
// stage_complete first → renderAll() → iframe.src reassigned → the
// placeholder we injected before upload is now in a detached doc.
// Reading from snapshot sidesteps the race entirely.
function _finalizeDrop(ref_id, relPath, naturalW, naturalH) {
  const pending = pendingDrops.get(ref_id);
  if (!pending) return;
  pendingDrops.delete(ref_id);
  const { slideIdx, x, y } = pending;

  // URL = server's base href (server injects <base> into the iframe doc
  // so relative paths resolve to /files/<run_dir>/). For persistence we
  // store the path RELATIVE to the run dir so free_form's base href
  // resolves correctly on every render. Cache-bust via ?t= so the
  // browser re-fetches if the user re-uploads to the same path.
  const cacheBust = `?t=${Date.now()}`;
  // Leading slash on relPath would break the base-href resolution
  // (base is /files/<run>/, a path like "/images/x.png" overrides it).
  const rel = (relPath || "").replace(/^\/+/, "");
  const srcAttr = `${rel}${cacheBust}`;

  // Display size — clamp natural dims so a 4K photo doesn't fill the
  // slide. Slide canvas is read from the CSS var so portrait / square
  // canvases clamp relative to their actual width, not the historical
  // 1280px landscape default.
  const slideW = getCanvasWidthPx();
  const maxW = Math.min(320, slideW * 0.4);
  const ratio = (naturalW && naturalH) ? naturalH / naturalW : 1;
  const dispW = naturalW ? Math.min(naturalW, maxW) : maxW;
  const dispH = Math.round(dispW * ratio);
  const dispWPx = Math.round(dispW);

  // Build the new <img> tag as a string and append to the cached
  // snapshot's slide HTML. Appending (vs. injecting at a specific
  // node) is correct because the free_form template wraps the entire
  // slots.html in a single .ppt-slide container — the absolute img
  // positions against that container regardless of where in the
  // innerHTML string it lives.
  const imgTag = `<img class="shuttleslide-dropped-img" src="${srcAttr}" style="position:absolute;left:${x}px;top:${y}px;width:${dispWPx}px;height:${dispH}px;z-index:9999;">`;

  const snap = snapshots["slides"];
  if (!snap) { _failDrop(ref_id, "no slides snapshot available"); return; }
  const slides = (snap.state_view && snap.state_view.slides) || [];
  const slide = slides[slideIdx];
  if (!slide) { _failDrop(ref_id, `slide ${slideIdx} not in snapshot`); return; }
  const oldHtml = slide.html || "";
  const newHtml = oldHtml + imgTag;

  // Persist immediately — see plan "Auto-commit" rationale. Without
  // this any unrelated reload (inline edit, theme change) would wipe
  // the inserted img before the user has a chance to commit.
  // request_edit → apply_edit → stage_complete will trigger renderAll
  // which reloads the iframe with the new HTML (img visible).
  ws.send(JSON.stringify({
    type: "request_edit",
    ref_id: newRefId(),
    target_path: ["slide", slideIdx, "html"],
    mode: "direct",
    payload: { new_value: newHtml },
  }));
}

function _failDrop(ref_id, errorMessage) {
  const pending = pendingDrops.get(ref_id);
  if (!pending) return;
  pendingDrops.delete(ref_id);
  // Surface the error via toast — the placeholder may have already
  // vanished (iframe reloaded by an unrelated edit) so trying to
  // replace it in-DOM is unreliable. flashToast guarantees the user
  // sees the failure regardless of iframe state.
  flashToast(`Image upload failed: ${errorMessage}`);
}

// =====================================================================
// Layers panel — Photoshop-style layer list for the current slide.
// Lists every .ppt-slide direct child + every img.shuttleslide-svg-placeholder
// (even when nested) so users can click-pick elements visually obscured
// by siblings stacked above. Image layers also jump to the matching
// thumbnail in the Images sidebar tab.
//
// Lifecycle: buildLayers runs in onLoaded (after attachInlineEdit), caches
// into currentLayers. The toggle button re-renders without rebuilding.
// =====================================================================
let currentLayers = [];         // [{id, el, type, label, slotId?, tag}]
let currentLayersSlideIdx = -1; // slide the cached layers belong to

function buildLayers(iframe, slideIdx) {
  currentLayersSlideIdx = slideIdx;
  if (!iframe) return [];
  const doc = iframe.contentDocument;
  if (!doc) return [];
  const slide = doc.querySelector(".ppt-slide");
  if (!slide) return [];

  // Walk in DOM order; reverse for display (topmost z first).
  const seen = new Set();
  const items = [];

  // 1. Direct children of .ppt-slide.
  for (const child of slide.children) {
    if (seen.has(child)) continue;
    seen.add(child);
    items.push(buildLayerEntry(child));
  }
  // 2. Every state-backed img (svg_file OR image_file). Both carry
  //    data-slot — the universal anchor. svg_file additionally has the
  //    shuttleslide-svg-placeholder class but querying by data-slot
  //    catches both kinds without needing a per-class branch. Dedup
  //    against direct children (an img could be both).
  doc.querySelectorAll(".ppt-slide img[data-slot]").forEach((img) => {
    if (seen.has(img)) return;
    seen.add(img);
    items.push(buildLayerEntry(img));
  });
  // 3. Every user-dropped raster img (drag-drop feature #6). Also
  //    nested-tolerant. A dropped img is a direct child of .ppt-slide
  //    in practice (we append it there in _finalizeDrop), but the
  //    queryAll keeps it consistent if a future LLM edit rewraps it.
  //    Has no data-slot (HTML-only, not in state.slide_images) so the
  //    data-slot query above doesn't catch it.
  doc.querySelectorAll(".ppt-slide img.shuttleslide-dropped-img").forEach((img) => {
    if (seen.has(img)) return;
    seen.add(img);
    items.push(buildLayerEntry(img));
  });
  return items;
}

function buildLayerEntry(el) {
  // Stable-per-load client id. Regenerated on each iframe load — only
  // used to look up the DOM row ↔ element mapping within this panel.
  if (!el.dataset.layerId) {
    el.dataset.layerId = (crypto && crypto.randomUUID && crypto.randomUUID()) ||
      `layer-${Math.random().toString(36).slice(2, 10)}`;
  }
  const id = el.dataset.layerId;
  const tag = el.tagName.toLowerCase();
  // data-slot is the universal anchor for state-backed imgs (svg_file
  // AND image_file). The svg-placeholder class is kept as a legacy
  // fallback for slide HTML predating the data-slot convention.
  const isStateImg = !!(el.matches && el.matches("img") &&
    (el.hasAttribute("data-slot") ||
     el.classList.contains("shuttleslide-svg-placeholder")));
  const isDroppedImg = el.matches && el.matches("img.shuttleslide-dropped-img");
  const isImg = isStateImg || isDroppedImg;
  let type, label, slotId = null;
  if (isImg) {
    type = "image";
    if (isDroppedImg) {
      // User-dropped uploads use opaque user_* slot ids — not meaningful
      // to display. Show a stable label instead.
      slotId = extractSlotIdFromSrc(el.getAttribute("src") || "");
      label = "IMG · user upload";
    } else {
      // State-backed: data-slot is canonical, src is the legacy fallback.
      slotId = el.dataset.slot ||
        extractSlotIdFromSrc(el.getAttribute("src") || "");
      label = slotId ? `IMG · ${slotId}` : "IMG";
    }
  } else if (/^h[1-6]$/.test(tag) ||
             ["p","li","span","a","strong","em","blockquote","label","button"].includes(tag)) {
    type = "text";
    const text = (el.textContent || "").trim().replace(/\s+/g, " ").slice(0, 30);
    label = text || `(empty ${tag})`;
  } else {
    type = "container";
    const childCount = el.children.length;
    label = childCount ? `${tag} · ${childCount}` : tag;
  }
  return { id, el, type, label, slotId, tag };
}

function extractSlotIdFromSrc(src) {
  if (!src) return null;
  // URLs reach the img as "svgs/{slotId}" (relative) or
  // "/artifact/slides/{idx}/svgs/{slotId}" (absolute). Take the last
  // non-empty path segment, stripping any ?query or #hash.
  const noQuery = src.split("?")[0].split("#")[0];
  const parts = noQuery.split("/").filter(Boolean);
  return parts[parts.length - 1] || null;
}

function renderLayersPanel(layers) {
  const list = document.getElementById("layers-list");
  if (!list) return;
  if (!layers || !layers.length) {
    list.innerHTML = `<p class="layers-panel-empty">No layers on this slide.</p>`;
    return;
  }
  // Reverse DOM order: last child = topmost z = first row, matching
  // Photoshop / Figma layer-panel convention.
  const reversed = [...layers].reverse();
  list.innerHTML = reversed.map((layer) => {
    const icon = layer.type === "image" ? "▥" :
                 layer.type === "text"   ? "T"  : "▢";
    const cls = `layer-item layer-item--${layer.type}`;
    const tagBadge = escapeHtml(layer.tag.toUpperCase());
    const label = escapeHtml(layer.label);
    // Row title doubles as inline help for the click vs dblclick split.
    const title = layer.type === "image" && layer.slotId
      ? "单击高亮位置 · 双击进入 image 阶段编辑"
      : "单击高亮位置";
    return `<div class="${cls}" data-layer-id="${escapeAttr(layer.id)}" role="button" title="${escapeAttr(title)}">
              <span class="layer-item__icon" aria-hidden="true">${icon}</span>
              <span class="layer-item__label">${label}</span>
              <span class="layer-item__tag">${tagBadge}</span>
            </div>`;
  }).join("");
  list.querySelectorAll(".layer-item").forEach((row) => {
    // Single click: locate the element in the slide (flash outline +
    // scrollIntoView). Harmless visual feedback, runs every click.
    row.addEventListener("click", () => {
      const layer = layers.find((l) => l.id === row.dataset.layerId);
      if (layer) onLayerClick(layer);
    });
    // Double click: image rows only — jump into the images stage to
    // edit the SVG / replace the raster. Stacks on top of single-click
    // (no need to delay single-click; the outline flash is cheap).
    row.addEventListener("dblclick", () => {
      const layer = layers.find((l) => l.id === row.dataset.layerId);
      if (layer && layer.type === "image" && layer.slotId) {
        jumpToImageThumb(currentLayersSlideIdx, layer.slotId);
        // Drop the slide-preview highlight overlay — we've left the slide
        // stage for the images stage, so the outline is no longer rooted
        // in a visible element and its scroll/resize listeners would
        // leak across the stage switch.
        hideLayerOverlay();
      }
    });
  });
}

function onLayerClick(layer) {
  const { el } = layer;
  // Single-click behaviour is purely visual: light up the element so the
  // user sees where it lives, even when it's obscured by siblings
  // stacked above. The outline stays on until another layer is picked
  // or the panel closes — that's the "PS layer selection" feel the user
  // asked for. Stage jump only happens on dblclick.
  showLayerOverlay(el);
  if (typeof el.scrollIntoView === "function") {
    el.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
  // Mark active row in the panel.
  document.querySelectorAll(".layer-item").forEach((r) =>
    r.classList.toggle("layer-item--active", r.dataset.layerId === layer.id));
}

// Track the highlighted element + its iframe so we can reposition the
// overlay on resize / scroll, and tear it down when the panel closes
// or the slide changes.
let _layerOverlayEl = null;
let _layerOverlayIframe = null;
let _layerOverlayRAF = 0;

// Position a #shuttleslide-layer-overlay div in the parent doc over the
// selected iframe element. Bypasses parent opacity, ancestor
// overflow:hidden, and iframe CSS scale — all of which made in-iframe
// outlines invisible on hero images.
function showLayerOverlay(el) {
  const iframe = document.querySelector("iframe.big-slide");
  if (!iframe || !el) return;
  hideLayerOverlay();
  _layerOverlayEl = el;
  _layerOverlayIframe = iframe;

  const overlay = document.createElement("div");
  overlay.id = "shuttleslide-layer-overlay";
  // Append to #preview (position:relative) so absolute coords line up
  // with the iframe inside it.
  const preview = document.getElementById("preview") || iframe.parentElement;
  preview.appendChild(overlay);
  positionLayerOverlay();
  // Keep the overlay glued on resize/scroll. Cheap (rAF-throttled).
  window.addEventListener("resize", _layerOverlayPosition);
  window.addEventListener("scroll", _layerOverlayPosition, true);
  iframe.contentWindow.addEventListener("scroll", _layerOverlayPosition);
}

function _layerOverlayPosition() {
  cancelAnimationFrame(_layerOverlayRAF);
  _layerOverlayRAF = requestAnimationFrame(positionLayerOverlay);
}

function positionLayerOverlay() {
  const overlay = document.getElementById("shuttleslide-layer-overlay");
  if (!overlay || !_layerOverlayEl || !_layerOverlayIframe) return;
  const el = _layerOverlayEl;
  const iframe = _layerOverlayIframe;
  // el.getBoundingClientRect() returns coords in the iframe's viewport,
  // already in CSS pixels. The iframe itself is CSS-scaled, so we need
  // to map iframe-local coords back to parent-doc coords by applying
  // the inverse: divide by (iframe.offsetWidth / iframe.getBoundingClientRect().width).
  const elRect = el.getBoundingClientRect();  // iframe-local CSS px
  const ifRect = iframe.getBoundingClientRect();  // parent-doc px
  const scale = ifRect.width / iframe.offsetWidth;  // ~0.49
  // Anchor element: same parent as the iframe so we share its coord
  // origin. Use the iframe's parentElement as our reference frame.
  const preview = overlay.parentElement;
  const previewRect = preview.getBoundingClientRect();
  const left = ifRect.left - previewRect.left + elRect.left * scale;
  const top = ifRect.top - previewRect.top + elRect.top * scale;
  overlay.style.left = left + "px";
  overlay.style.top = top + "px";
  overlay.style.width = (elRect.width * scale) + "px";
  overlay.style.height = (elRect.height * scale) + "px";
}

function clearSelectedLayerEl() {
  hideLayerOverlay();
}

function hideLayerOverlay() {
  if (_layerOverlayRAF) {
    cancelAnimationFrame(_layerOverlayRAF);
    _layerOverlayRAF = 0;
  }
  if (_layerOverlayIframe && _layerOverlayIframe.contentWindow) {
    try { _layerOverlayIframe.contentWindow.removeEventListener("scroll", _layerOverlayPosition); } catch (_) {}
  }
  window.removeEventListener("resize", _layerOverlayPosition);
  window.removeEventListener("scroll", _layerOverlayPosition, true);
  const overlay = document.getElementById("shuttleslide-layer-overlay");
  if (overlay) overlay.remove();
  _layerOverlayEl = null;
  _layerOverlayIframe = null;
}

// Jump from a slide's image layer into the images stage. Reuses the
// existing jumpToImageTarget (which sets activeStage="images", picks
// the right thumb, re-renders synchronously) with openPicker=false so
// we don't spring the file picker on the user. After return, flash
// the now-active thumbnail so the user sees where they landed.
function jumpToImageThumb(slideIdx, slotId) {
  jumpToImageTarget(slideIdx, slotId, false);
  // renderAll is synchronous — DOM is already up to date here.
  const active = document.querySelector("#thumb-list .thumb.active") ||
                 document.querySelector(".thumb.active");
  if (!active) return;
  active.classList.remove("thumb-flash");
  void active.offsetWidth;
  active.classList.add("thumb-flash");
  setTimeout(() => active.classList.remove("thumb-flash"), 1600);
}

// Ask the server for the chat history of a target on first focus so
// edits from a prior session (within the same server lifetime) are
// visible. Cheap WS message; safe to re-send.
function refreshChatHistoryForActive() {
  const target = getActiveTarget();
  if (!target) return;
  const ref_id = newRefId();
  ws.send(JSON.stringify({
    type: "chat_history",
    ref_id,
    target_path: target.path,
  }));
}

// =====================================================================
// renderThemePreview — color swatches + demo slide (theme middle view)
// =====================================================================
function renderThemePreview(theme) {
  // Always work against a local draft so color-picker changes are
  // preview-only until Commit. Initialize lazily from serverTheme on
  // first render (or after a Commit/Cancel that cleared the draft).
  if (themeDraft === null) themeDraft = { ...(theme || {}) };
  theme = themeDraft;
  // Canonical color keys mirror theme_tools.py color_fields.
  const colorKeys = [
    ["primary_color", "Primary"],
    ["accent_color",  "Accent"],
    ["bg_color",      "Background"],
    ["text_color",    "Body text"],
    ["title_color",   "Title text"],
  ];
  const swatches = colorKeys.map(([k, label]) => {
    const v = theme[k] || "";
    // <input type="color"> only accepts 6-digit hex (#rrggbb); 3/8-digit
    // variants are coerced to the fallback so the picker still opens.
    const safeHex = /^#[0-9a-fA-F]{6}$/.test(v) ? v : "#cccccc";
    const display = v ? escapeHtml(v) : "(unset)";
    return `<label class="color-swatch" data-color-key="${escapeAttr(k)}" title="Click to edit ${escapeHtml(label)}">
      <span class="swatch-circle" style="background:${safeHex};"></span>
      <span class="swatch-label">${escapeHtml(label)}</span>
      <span class="swatch-hex">${display}</span>
      <input type="color" class="swatch-color-input" value="${safeHex}" aria-label="${escapeAttr(label)} color" />
    </label>`;
  }).join("");

  // Demo slide — render a 16:9 card using every theme field at once.
  const bg = /^#[0-9a-fA-F]{3,8}$/.test(theme.bg_color) ? theme.bg_color : "#FEFEFE";
  const titleColor = /^#[0-9a-fA-F]{3,8}$/.test(theme.title_color) ? theme.title_color
                   : /^#[0-9a-fA-F]{3,8}$/.test(theme.primary_color) ? theme.primary_color
                   : "#222222";
  const textColor = /^#[0-9a-fA-F]{3,8}$/.test(theme.text_color) ? theme.text_color : "#333333";
  const primary = /^#[0-9a-fA-F]{3,8}$/.test(theme.primary_color) ? theme.primary_color : "#133EFF";
  const accent = /^#[0-9a-fA-F]{3,8}$/.test(theme.accent_color) ? theme.accent_color : "#00CD82";
  const titleFont = theme.font_title ? `'${escapeAttr(theme.font_title)}', ` : "";
  const bodyFont = theme.font_body ? `'${escapeAttr(theme.font_body)}', ` : "";
  const decoration = theme.decoration_style ? escapeHtml(theme.decoration_style) : "(unset)";

  const demoSlide = `<div style="
    background:${bg};
    color:${textColor};
    font-family:${bodyFont}system-ui, sans-serif;
    padding:24px;
    border-radius:8px;
    border:1px solid #ddd;
    aspect-ratio:var(--canvas-w-num) / var(--canvas-h-num);
    display:flex;
    flex-direction:column;
    justify-content:space-between;
  ">
    <div>
      <div style="font-family:${titleFont}system-ui, sans-serif;color:${titleColor};font-size:28px;font-weight:700;margin-bottom:6px;">
        Slide Title Preview
      </div>
      <div style="height:3px;width:48px;background:${accent};border-radius:2px;margin-bottom:10px;"></div>
      <div style="font-size:13px;line-height:1.5;max-width:80%;opacity:0.85;">
        Body copy uses font_body. Subtle accents (the bar above) use accent_color.
        decoration_style: <strong>${decoration}</strong>.
      </div>
    </div>
    <div style="display:flex;gap:8px;align-items:center;">
      <span style="background:${primary};color:white;padding:5px 12px;border-radius:4px;font-size:12px;font-weight:500;">Primary action</span>
      <span style="background:${accent};color:white;padding:5px 12px;border-radius:4px;font-size:12px;font-weight:500;">Accent action</span>
    </div>
  </div>`;

  const jsonBlock = `<details style="margin-top:16px;">
    <summary style="cursor:pointer;color:var(--text-secondary);font-size:12px;user-select:none;">Raw theme JSON</summary>
    <pre style="margin-top:8px;"><code>${escapeHtml(JSON.stringify({theme}, null, 2))}</code></pre>
  </details>`;

  // Always show the Commit/Cancel toolbar — no separate view mode.
  // Color-picker changes update themeDraft and re-render locally;
  // only Commit contacts the server.
  const toolbar = `<div class="theme-edit-toolbar">
    <span class="theme-edit-hint">💡 Adjust colors, then commit to apply</span>
    <button type="button" class="theme-commit-btn">✓ Commit</button>
    <button type="button" class="theme-cancel-btn">✗ Cancel</button>
  </div>`;

  setPreview(`
    <div class="theme-edit-host">
      ${toolbar}
      <div class="theme-edit-body">
        <h4 style="margin:0 0 10px 0;font-size:13px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;">Color palette</h4>
        <div style="margin-bottom:8px;">${swatches}</div>

        <h4 style="margin:16px 0 10px 0;font-size:13px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;">Slide preview</h4>
        ${demoSlide}

        ${jsonBlock}
      </div>
    </div>
  `);

  // Wire up toolbar buttons. setPreview synchronously replaces
  // innerHTML so the host exists now.
  const host = previewContent.querySelector(".theme-edit-host");
  if (host) {
    host.querySelector(".theme-commit-btn")?.addEventListener("click", commitThemeDraft);
    host.querySelector(".theme-cancel-btn")?.addEventListener("click", cancelThemeDraft);
  }
}

function commitThemeDraft() {
  if (!themeDraft) return;
  const serverTheme = snapshots.theme?.state_view?.theme || {};
  // No-op short-circuit: draft identical to server means the user
  // opened the editor but changed nothing. Skip the WS round-trip
  // entirely — server would silently drop it anyway (apply_edit no_op),
  // so save the network hop and the stage_complete re-render work.
  if (JSON.stringify(themeDraft) === JSON.stringify(serverTheme)) {
    themeDraft = null;
    renderThemePreview(serverTheme);
    return;
  }
  ws.send(JSON.stringify({
    type: "request_edit",
    ref_id: newRefId(),
    target_path: ["theme"],
    mode: "direct",
    payload: { new_value: JSON.stringify(themeDraft) },
  }));
  // Clear draft; the next stage_complete broadcast will re-render with
  // serverTheme (which now includes the committed values).
  themeDraft = null;
}

function cancelThemeDraft() {
  themeDraft = null;
  const serverTheme = snapshots.theme?.state_view?.theme || {};
  renderThemePreview(serverTheme);
}

// =====================================================================
// Outline structured editor
// =====================================================================
// Replaces the raw-JSON <pre> preview with a structured per-entry form.
// Mirrors the theme color-swatch pattern: local outlineDraft, lazy-init
// from server outline, Commit sends a single request_edit with mode="direct".
//
// Out-of-band operations (delete / add / rebalance) bypass the draft and
// fire dedicated WS messages — server re-broadcasts stage_complete which
// resets outlineDraft on the next render, so draft and server state can't
// diverge.

const OUTLINE_ENTRY_FIELDS = [
  ["title", "Title", "input"],
  ["purpose", "Purpose", "textarea"],
  ["layout_hint", "Layout hint", "textarea"],
];

function defaultImageSpec() {
  return {
    slot_id: "hero",
    aspect_ratio: "16:9",
    image_type: "photo",
    source_type: "web",
    description: "",
    source_ref: "",
  };
}

function defaultNewEntry() {
  return {
    title: "",
    purpose: "",
    key_points: [],
    layout_hint: "",
    images: [],
    _detail_filled: true,
  };
}

function renderOutlineEditor() {
  // Lazy-init from server snapshot. Deep-clone via JSON round-trip so
  // nested arrays (key_points / images) are isolated from server state.
  if (outlineDraft === null) {
    const serverOutline = snapshots.outline?.state_view?.outline || [];
    outlineDraft = JSON.parse(JSON.stringify(serverOutline));
  }
  const outline = outlineDraft;

  // Only render the active slide's form — mirrors how slides / rendered
  // stages render a single item per preview. Clicking a thumbnail in the
  // left rail swaps activeItemIdx and re-renders this view; edits on
  // other slides accumulate in outlineDraft and ship on Commit. Clamp
  // activeItemIdx because outline length changes between renders (add /
  // delete via WS) and a stale index would crash the entry lookup.
  if (outline.length === 0) {
    activeItemIdx = 0;
  } else if (activeItemIdx < 0 || activeItemIdx >= outline.length) {
    activeItemIdx = Math.min(activeItemIdx, outline.length - 1);
    if (activeItemIdx < 0) activeItemIdx = 0;
  }

  const toolbar = `<div class="outline-edit-toolbar">
    <span class="outline-edit-hint">💡 Click a thumbnail to switch slides. Commit sends the whole outline (edits on other slides are kept).</span>
    <button type="button" class="outline-add-btn">+ Add Slide</button>
    <button type="button" class="outline-rebalance-btn">↻ Rebalance</button>
    <button type="button" class="outline-commit-btn">✓ Commit</button>
    <button type="button" class="outline-cancel-btn">✗ Cancel</button>
  </div>`;

  const body = outline.length
    ? renderOutlineEntry(outline[activeItemIdx], activeItemIdx)
    : `<div class="outline-empty">Outline is empty. Click "+ Add Slide" to start.</div>`;

  // Collapsible raw JSON for debugging — same pattern as theme editor.
  const jsonBlock = `<details style="margin-top:16px;">
    <summary style="cursor:pointer;color:var(--text-secondary);font-size:12px;user-select:none;">Raw outline JSON</summary>
    <pre style="margin-top:8px;"><code>${escapeHtml(JSON.stringify({outline}, null, 2))}</code></pre>
  </details>`;

  setPreview(`
    <div class="outline-edit-host">
      ${toolbar}
      <div class="outline-edit-body">
        ${body}
        ${jsonBlock}
      </div>
    </div>
  `);

  const host = previewContent.querySelector(".outline-edit-host");
  if (host) {
    host.querySelector(".outline-commit-btn")?.addEventListener("click", commitOutlineDraft);
    host.querySelector(".outline-cancel-btn")?.addEventListener("click", cancelOutlineDraft);
    host.querySelector(".outline-add-btn")?.addEventListener("click", () => openAddSlideModal(outline.length));
    host.querySelector(".outline-rebalance-btn")?.addEventListener("click", confirmRebalance);
    attachOutlineEntryHandlers(host);
  }
}

function renderOutlineEntry(entry, idx) {
  // Header — slide label + delete (✕) button. Delete bypasses the draft.
  const header = `<div class="outline-entry-header">
    <span class="outline-entry-label">Slide ${idx + 1}</span>
    <button type="button" class="outline-entry-delete" data-idx="${idx}" title="Delete this slide">✕</button>
  </div>`;

  // Scalar fields: title / purpose / layout_hint
  const fields = OUTLINE_ENTRY_FIELDS.map(([key, label, kind]) => {
    const v = escapeHtml(String(entry[key] ?? ""));
    if (kind === "textarea") {
      return `<label class="outline-field">
        <span class="outline-field-label">${label}</span>
        <textarea data-field="${key}" rows="2">${v}</textarea>
      </label>`;
    }
    return `<label class="outline-field">
      <span class="outline-field-label">${label}</span>
      <input type="text" data-field="${key}" value="${v}">
    </label>`;
  }).join("");

  // Key points — list editor with add / remove
  const kps = Array.isArray(entry.key_points) ? entry.key_points : [];
  const kpRows = kps.map((kp, i) => `
    <div class="outline-list-row">
      <input type="text" data-field="key_points" data-i="${i}" value="${escapeHtml(String(kp ?? ""))}">
      <button type="button" class="outline-list-remove" data-field="key_points" data-i="${i}" title="Remove point">✕</button>
    </div>`).join("");
  const keyPointsBlock = `<div class="outline-field">
    <span class="outline-field-label">Key points</span>
    <div class="outline-list">${kpRows}</div>
    <button type="button" class="outline-list-add" data-field="key_points">+ Add point</button>
  </div>`;

  // Images — collapsible, structured sub-forms
  const imgs = Array.isArray(entry.images) ? entry.images : [];
  const imgRows = imgs.map((img, i) => renderOutlineImageSpec(img, i)).join("");
  const imagesBlock = `<details class="outline-images-group">
    <summary>Images (${imgs.length})</summary>
    <div class="outline-images-list">${imgRows}</div>
    <button type="button" class="outline-list-add" data-field="images">+ Add image</button>
  </details>`;

  return `<div class="outline-entry" data-idx="${idx}">
    ${header}
    <div class="outline-entry-body">
      ${fields}
      ${keyPointsBlock}
      ${imagesBlock}
    </div>
  </div>`;
}

function renderOutlineImageSpec(img, imgIdx) {
  const safe = (k) => escapeHtml(String(img?.[k] ?? ""));
  return `<div class="outline-image-spec" data-i="${imgIdx}">
    <div class="outline-image-spec-header">
      <span>Image ${imgIdx + 1}</span>
      <button type="button" class="outline-list-remove" data-field="images" data-i="${imgIdx}" title="Remove image">✕</button>
    </div>
    <div class="outline-image-spec-grid">
      <label>slot_id<input type="text" data-field="images" data-subfield="slot_id" data-i="${imgIdx}" value="${safe("slot_id")}"></label>
      <label>aspect_ratio<input type="text" data-field="images" data-subfield="aspect_ratio" data-i="${imgIdx}" value="${safe("aspect_ratio")}"></label>
      <label>image_type<input type="text" data-field="images" data-subfield="image_type" data-i="${imgIdx}" value="${safe("image_type")}"></label>
      <label>source_type<input type="text" data-field="images" data-subfield="source_type" data-i="${imgIdx}" value="${safe("source_type")}"></label>
      <label class="full-width">description<input type="text" data-field="images" data-subfield="description" data-i="${imgIdx}" value="${safe("description")}"></label>
      <label class="full-width">source_ref<input type="text" data-field="images" data-subfield="source_ref" data-i="${imgIdx}" value="${safe("source_ref")}"></label>
    </div>
  </div>`;
}

function attachOutlineEntryHandlers(host) {
  // Event delegation — single listener per host. Avoids re-binding after
  // every render and keeps the focus intact (re-render would blur inputs).
  host.addEventListener("input", (ev) => {
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    const entryEl = t.closest(".outline-entry");
    const modalEntryEl = t.closest("[data-modal-entry]");
    const container = entryEl || modalEntryEl;
    if (!container) return;
    const idx = parseInt(container.dataset.idx ?? container.dataset.modalEntry ?? "-1", 10);
    if (!outlineDraft || idx < 0 || idx >= outlineDraft.length) {
      // Modal manual mode: edit a draft-not-yet-in-outline entry
      if (modalEntryEl && typeof pendingAddEntry !== "undefined" && pendingAddEntry) {
        applyInputToEntry(pendingAddEntry, t);
      }
      return;
    }
    applyInputToEntry(outlineDraft[idx], t);
  });

  host.addEventListener("click", (ev) => {
    const t = ev.target;
    if (!(t instanceof HTMLElement)) return;
    // Delete entry (✕ on header) — bypass draft, send WS
    const delBtn = t.closest(".outline-entry-delete");
    if (delBtn) {
      const idx = parseInt(delBtn.dataset.idx, 10);
      if (!Number.isNaN(idx)) confirmDeleteSlide(idx);
      return;
    }
    // Add / remove list items
    const addBtn = t.closest(".outline-list-add");
    const rmBtn = t.closest(".outline-list-remove");
    if (addBtn || rmBtn) {
      const entryEl = t.closest(".outline-entry");
      const modalEntryEl = t.closest("[data-modal-entry]");
      const container = entryEl || modalEntryEl;
      if (!container) return;
      const idx = parseInt(container.dataset.idx ?? container.dataset.modalEntry ?? "-1", 10);
      const entry = (entryEl && outlineDraft && outlineDraft[idx])
        ? outlineDraft[idx]
        : (modalEntryEl && typeof pendingAddEntry !== "undefined" ? pendingAddEntry : null);
      if (!entry) return;
      const field = (addBtn || rmBtn).dataset.field;
      if (addBtn) {
        if (field === "key_points") entry.key_points.push("");
        else if (field === "images") entry.images.push(defaultImageSpec());
        // Re-render this entry's section
        if (entryEl) {
          const newHtml = renderOutlineEntry(entry, idx);
          entryEl.outerHTML = newHtml;
        } else {
          renderAddSlideModalEntry(pendingAddEntry);
        }
      } else { // remove
        const i = parseInt(rmBtn.dataset.i, 10);
        if (field === "key_points" && Array.isArray(entry.key_points)) {
          entry.key_points.splice(i, 1);
        } else if (field === "images" && Array.isArray(entry.images)) {
          entry.images.splice(i, 1);
        }
        if (entryEl) {
          const newHtml = renderOutlineEntry(entry, idx);
          entryEl.outerHTML = newHtml;
        } else {
          renderAddSlideModalEntry(pendingAddEntry);
        }
      }
    }
  });
}

function applyInputToEntry(entry, t) {
  const field = t.dataset.field;
  if (!field) return;
  if (field === "key_points") {
    const i = parseInt(t.dataset.i, 10);
    if (Array.isArray(entry.key_points) && i >= 0 && i < entry.key_points.length) {
      entry.key_points[i] = t.value;
    }
    return;
  }
  if (field === "images") {
    const i = parseInt(t.dataset.i, 10);
    const sub = t.dataset.subfield;
    if (Array.isArray(entry.images) && i >= 0 && i < entry.images.length && sub) {
      entry.images[i][sub] = t.value;
    }
    return;
  }
  // scalar
  entry[field] = t.value;
}

function commitOutlineDraft() {
  if (!outlineDraft) return;
  const serverOutline = snapshots.outline?.state_view?.outline || [];
  // No-op short-circuit: identical to server means user opened editor
  // without changing anything. Skip WS round-trip.
  if (JSON.stringify(outlineDraft) === JSON.stringify(serverOutline)) {
    outlineDraft = null;
    renderOutlineEditor();
    return;
  }
  ws.send(JSON.stringify({
    type: "request_edit",
    ref_id: newRefId(),
    target_path: ["outline"],
    mode: "direct",
    payload: { new_value: JSON.stringify(outlineDraft) },
  }));
  // Don't reset outlineDraft yet — server's stage_complete broadcast will
  // drive the next render and reset there. If the edit is rejected, the
  // draft stays for the user to fix.
}

function cancelOutlineDraft() {
  outlineDraft = null;
  renderOutlineEditor();
}

// =====================================================================
// Add / Delete / Rebalance (structural outline ops)
// =====================================================================

function confirmDeleteSlide(idx) {
  if (!confirm(`Delete slide ${idx + 1}?\n\nThis removes its outline entry, ` +
      `generated HTML, images, and PPTX slot. Later slides shift down ` +
      `by one. Undo is NOT available for structural changes.`)) {
    return;
  }
  ws.send(JSON.stringify({
    type: "delete_slide",
    ref_id: newRefId(),
    index: idx,
  }));
  // Reset draft so next render pulls fresh from server.
  outlineDraft = null;
}

function confirmRebalance() {
  const ok = confirm(
    "Let the LLM rewrite all outline entries to improve narrative flow?\n\n" +
    "- Per-entry keys stay the same.\n" +
    "- Values (titles, purposes, key points) MAY change.\n" +
    "- Your manual edits will be preserved where possible but not guaranteed.\n" +
    "- All downstream slides will be marked stale (badges shown).\n" +
    "- You'll need to Regenerate each slide to apply the new outline.\n\n" +
    "Optional: add a hint below (cancel to skip)."
  );
  if (!ok) return;
  const hint = prompt("Optional hint for the rewrite (e.g. 'tighten the middle section'):", "");
  if (hint === null) return; // user clicked cancel on prompt
  ws.send(JSON.stringify({
    type: "rebalance_outline",
    ref_id: newRefId(),
    user_hint: hint,
  }));
}

// ---- Add Slide modal ----

// Per-entry draft for the modal's manual mode. Held outside outlineDraft
// because the entry isn't in the outline yet — only inserted on submit.
let pendingAddEntry = null;
let pendingAddMode = "manual";

function openAddSlideModal(suggestedIndex) {
  pendingAddEntry = defaultNewEntry();
  pendingAddMode = "manual";

  // Build the modal shell, then inject the manual entry form.
  const insertOptions = buildInsertOptions(suggestedIndex);

  const modalHtml = `<div class="ss-modal-overlay" id="add-slide-modal">
    <div class="ss-modal-card">
      <div class="ss-modal-header">
        <h3>Add Slide</h3>
        <button type="button" class="ss-modal-close" title="Close">✕</button>
      </div>
      <div class="ss-modal-body">
        <div class="ss-modal-mode-toggle">
          <label><input type="radio" name="add-mode" value="llm" ${pendingAddMode === "llm" ? "checked" : ""}> Describe with AI</label>
          <label><input type="radio" name="add-mode" value="manual" ${pendingAddMode === "manual" ? "checked" : ""}> Manual form</label>
        </div>

        <div class="ss-modal-section" data-section="llm" hidden>
          <label class="ss-modal-field">
            <span>Describe the slide you want:</span>
            <textarea id="add-slide-intent" rows="4" placeholder="e.g. A comparison slide of React vs Vue, placed between the intro and the deep-dive"></textarea>
          </label>
          <label class="ss-modal-field">
            <span>Insert at:</span>
            <select id="add-slide-index-llm">${insertOptions}</select>
          </label>
        </div>

        <div class="ss-modal-section" data-section="manual">
          <div data-modal-entry="0" id="add-slide-manual-entry"></div>
          <label class="ss-modal-field">
            <span>Insert at:</span>
            <select id="add-slide-index-manual">${insertOptions}</select>
          </label>
        </div>
      </div>
      <div class="ss-modal-footer">
        <span class="ss-modal-error" id="add-slide-error" hidden></span>
        <button type="button" class="ss-modal-submit">Insert</button>
      </div>
    </div>
  </div>`;

  // Append to body so the modal floats above everything.
  const old = document.getElementById("add-slide-modal");
  if (old) old.remove();
  document.body.insertAdjacentHTML("beforeend", modalHtml);

  // Wire up.
  const modal = document.getElementById("add-slide-modal");
  modal.querySelector(".ss-modal-close")?.addEventListener("click", closeAddSlideModal);
  modal.querySelectorAll('input[name="add-mode"]').forEach(r => {
    r.addEventListener("change", (ev) => {
      pendingAddMode = ev.target.value;
      updateAddSlideModeVisibility();
    });
  });
  modal.querySelector(".ss-modal-submit")?.addEventListener("click", submitAddSlide);
  // Close on overlay click
  modal.addEventListener("click", (ev) => {
    if (ev.target === modal) closeAddSlideModal();
  });

  renderAddSlideModalEntry(pendingAddEntry);
  updateAddSlideModeVisibility();
}

function buildInsertOptions(suggestedIndex) {
  const n = snapshots.outline?.state_view?.outline?.length ?? outlineDraft?.length ?? 0;
  const suggested = Math.max(0, Math.min(suggestedIndex, n));
  const opts = [];
  opts.push(`<option value="${n}" ${suggested === n ? "selected" : ""}>End (after slide ${n})</option>`);
  for (let i = 0; i <= n; i++) {
    const label = i === 0 ? "Start (before slide 1)" : `Before slide ${i + 1}`;
    opts.push(`<option value="${i}" ${i === suggested ? "selected" : ""}>${label}</option>`);
  }
  return opts.join("");
}

function updateAddSlideModeVisibility() {
  const modal = document.getElementById("add-slide-modal");
  if (!modal) return;
  modal.querySelectorAll(".ss-modal-section").forEach(s => {
    s.hidden = s.dataset.section !== pendingAddMode;
  });
}

function renderAddSlideModalEntry(entry) {
  // Reuse renderOutlineEntry but swap the data-idx for data-modal-entry so
  // the delegation handler routes inputs to pendingAddEntry instead of
  // outlineDraft. We render manually here to inject the right attribute.
  const html = renderOutlineEntry(entry, 0)
    .replace('class="outline-entry" data-idx="0"', 'class="outline-entry outline-entry-modal" data-modal-entry="0"')
    .replace(/data-idx="0"/g, 'data-modal-entry="0"');
  const slot = document.getElementById("add-slide-manual-entry");
  if (slot) slot.innerHTML = html;
  // Wire list-add / remove / image-spec handlers via delegation on the modal too.
  const modal = document.getElementById("add-slide-modal");
  if (modal && !modal.dataset.handlersAttached) {
    attachOutlineEntryHandlers(modal);
    modal.dataset.handlersAttached = "1";
  }
}

function submitAddSlide() {
  const modal = document.getElementById("add-slide-modal");
  if (!modal) return;
  const errEl = modal.querySelector("#add-slide-error");
  const submitBtn = modal.querySelector(".ss-modal-submit");

  const indexSelect = modal.querySelector(
    pendingAddMode === "llm" ? "#add-slide-index-llm" : "#add-slide-index-manual"
  );
  const index = parseInt(indexSelect?.value ?? "-1", 10);

  if (pendingAddMode === "llm") {
    const intent = (modal.querySelector("#add-slide-intent")?.value || "").trim();
    if (!intent) {
      if (errEl) { errEl.textContent = "Please describe what the slide should be about."; errEl.hidden = false; }
      return;
    }
    if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Generating…"; }
    if (errEl) errEl.hidden = true;
    const refId = newRefId();
    pendingAddRefId = refId;
    ws.send(JSON.stringify({
      type: "add_slide",
      ref_id: refId,
      index,
      mode: "llm",
      payload: { intent },
    }));
    // Modal stays open until EditAppliedMsg arrives (matched on ref_id).
    // See handleClose_addSlide_onAck (wired in the edit_applied handler).
    return;
  }

  // Manual: ship the draft entry as-is. Server validates required keys.
  if (submitBtn) { submitBtn.disabled = true; submitBtn.textContent = "Inserting…"; }
  if (errEl) errEl.hidden = true;
  const refId = newRefId();
  pendingAddRefId = refId;
  ws.send(JSON.stringify({
    type: "add_slide",
    ref_id: refId,
    index,
    mode: "manual",
    payload: { entry: pendingAddEntry },
  }));
}

let pendingAddRefId = null;

function closeAddSlideModal() {
  const modal = document.getElementById("add-slide-modal");
  if (modal) modal.remove();
  pendingAddEntry = null;
  pendingAddRefId = null;
}

function handleAddSlideAck(refId, ok, errorMsg) {
  // Closes the modal on matching ref_id. Called from the existing
  // edit_applied / edit_rejected handlers via a small hook.
  if (pendingAddRefId !== refId) return;
  if (ok) {
    closeAddSlideModal();
    outlineDraft = null; // refresh from server on next render
  } else {
    const modal = document.getElementById("add-slide-modal");
    const errEl = modal?.querySelector("#add-slide-error");
    const submitBtn = modal?.querySelector(".ss-modal-submit");
    if (errEl) { errEl.textContent = errorMsg || "Failed to add slide."; errEl.hidden = false; }
    if (submitBtn) { submitBtn.disabled = false; submitBtn.textContent = "Insert"; }
  }
  pendingAddRefId = null;
}

// =====================================================================
// Pipeline done + helpers
// =====================================================================
// Two distinct concerns, split so they can fire at different times:
//
//   showPptxDownloadButton(paths) — reveals the done-banner with HTML
//   file links. Called as soon as the `rendered` stage completes
//   (stage_complete for "rendered"), so the user can preview exported
//   HTML while pro stages (voiceover/render_video) are still running.
//   Does NOT touch the approveBtn — that stays on "Approve" so the
//   pipeline can advance.
//
//   setPipelineDone() — marks pipelineDone=true (entire order done,
//   including pro extensions), disables approveBtn (no more stages),
//   shows "Pipeline complete." status banner. Called on the
//   `pipeline_done` WS message, which the orchestrator only emits
//   after the LAST stage in resolve_order() (see
//   interactive_orchestrator.py:_post_stage_hook).
function showPptxDownloadButton(htmlPaths) {
  if (!doneBanner) return;
  // Unhide + force expanded — each new pipeline completion resets any
  // user-set collapse so they notice the result. They can re-collapse.
  doneBanner.hidden = false;
  doneBanner.classList.add("expanded");
  doneBanner.classList.remove("collapsed");
  doneToggle?.setAttribute("aria-expanded", "true");

  const count = (htmlPaths || []).length;
  if (doneFileCount) {
    doneFileCount.textContent = count > 0 ? `${count} file${count === 1 ? "" : "s"}` : "";
  }

  if (!doneBannerBody) return;
  if (count === 0) {
    doneBannerBody.innerHTML = "<p style=\"margin:0;color:var(--text-secondary);\">No HTML files were written.</p>";
  } else {
    // Each file is served by the /files/ StaticFiles mount and is
    // a standalone HTML document, so target=_blank opens it
    // directly in a new tab — no preview wrapper needed.
    // Defensive filter: state.html_paths can carry null entries in
    // degraded runs; p.split() would otherwise throw and break the
    // whole done-banner render.
    const safePaths = htmlPaths.filter((p) => typeof p === "string" && p);
    const items = safePaths.map((p, i) => {
      const filename = p.split(/[\\/]/).pop() || p;
      const href = fileUrl(p);
      return `<li>
                 <a href="${href}" target="_blank" rel="noopener"
                    style="color:var(--success);font-weight:500;">Slide ${i + 1}: ${escapeHtml(filename)}</a>
                 <span style="color:var(--text-secondary);font-size:11px;margin-left:6px;">${escapeHtml(p)}</span>
               </li>`;
    }).join("");
    doneBannerBody.innerHTML = `<div style="font-size:12px;color:var(--text-secondary);">Click a file to open in a new tab:</div><ul style="margin-top:6px;line-height:1.8;">${items}</ul>`;
  }
}

function setPipelineDone() {
  pipelineDone = true;
  setStatusBanner("Pipeline complete.", "done");
  // All stages finished — Approve has nothing left to advance to.
  // Disable it (don't repurpose — the Download PPTX button is a
  // separate control revealed when `rendered` completed). The label
  // stays "Approve" so the user recognises it as the same control
  // they've been clicking throughout the pipeline.
  approveBtn.disabled = true;
  approveBtn.textContent = "Approve";
  // Defensive: in case the rendered stage_complete was missed (client
  // connected late, snapshot replay arrived but stage_complete did
  // not), make sure the Download button is visible here too. Normally
  // it's already been revealed by the time pipeline_done fires.
  downloadPptxBtn.hidden = false;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
function escapeAttr(s) {
  // For srcdoc attributes we need both html-escaping and quote-escaping.
  return escapeHtml(s);
}

// =====================================================================
// Stale-mark helpers — match cached staleMarks against the active stage
// and the focused thumbnail. Used by both the thumbnail badges and the
// preview banner.
// =====================================================================

// target_id grammar matches StaleMark: "all" | "slide:N" | "slide:N:slot:ID"
// Returns the mark (or null) that applies to ``slideIdx`` on ``stage``.
function getStaleMark(stage, slideIdx) {
  const marks = staleMarks[stage];
  if (!Array.isArray(marks) || marks.length === 0) return null;
  // "all" marks the entire stage — return it if present.
  let allMark = null;
  let slideMark = null;
  for (const m of marks) {
    if (!m || typeof m.target_id !== "string") continue;
    if (m.target_id === "all") {
      allMark = m;
    } else if (slideIdx !== null && slideIdx !== undefined) {
      const id = m.target_id;
      if (id === `slide:${slideIdx}`) slideMark = m;
      else if (id.startsWith(`slide:${slideIdx}:slot:`)) slideMark = m;
    }
  }
  // Per-slide mark wins over "all" (more specific signal — the user
  // already regenerated the others).
  return slideMark || allMark;
}

// Returns true if the active thumbnail on ``stage`` carries a stale mark.
function isActiveItemStale(stage) {
  if (!stage) stage = activeStage;
  if (!stage) return false;
  let idx = null;
  if (stage === "outline" || stage === "images" || stage === "slides" || stage === "rendered") {
    idx = activeItemIdx;
  }
  return getStaleMark(stage, idx) !== null;
}

// Inject stale badges into thumbnails. Called from renderThumbnails
// AFTER the thumbs exist in the DOM. We add rather than rebuild so
// we don't disturb iframe reloads on the slides stage.
function decorateStaleBadges() {
  // Clear any previously-injected badges first.
  thumbList.querySelectorAll(".thumb-stale-badge").forEach(el => el.remove());
  thumbList.querySelectorAll(".thumb-stale").forEach(el => el.classList.remove("thumb-stale"));
  const stage = activeStage;
  if (!stage) return;
  const thumbs = thumbList.querySelectorAll(".thumb");
  thumbs.forEach((thumb, idx) => {
    const mark = getStaleMark(stage, idx);
    if (!mark) return;
    thumb.classList.add("thumb-stale");
    const badge = document.createElement("span");
    badge.className = "thumb-stale-badge";
    badge.title = mark.reason || "stale — upstream changed";
    badge.textContent = mark.target_id === "all" ? "Stale (all)" : "Stale";
    thumb.appendChild(badge);
  });
}

// Banner shown above the preview when the focused item carries a stale
// mark. Three actions: Update (incremental), From scratch (fresh), Dismiss.
function renderStaleBanner() {
  // Remove any prior banner — survives preview re-renders.
  const prior = document.getElementById("stale-banner");
  if (prior) prior.remove();
  const stage = activeStage;
  if (!stage) return;
  let idx = null;
  if (stage === "outline" || stage === "images" || stage === "slides" || stage === "rendered") {
    idx = activeItemIdx;
  }
  const mark = getStaleMark(stage, idx);
  if (!mark) return;
  // Outline + theme are sources — no per-item regenerate (user must
  // edit upstream directly). Don't show the banner for those.
  if (stage === "outline" || stage === "theme") return;

  const banner = document.createElement("div");
  banner.id = "stale-banner";
  banner.className = "stale-banner";
  const reasonText = mark.reason || "upstream value changed";
  banner.innerHTML = `
    <div class="stale-banner-icon" title="${escapeAttr(reasonText)}">⚠</div>
    <div class="stale-banner-body">
      <div class="stale-banner-title">This ${escapeHtml(stage === "slides" ? "slide" : (stage === "images" ? "image" : "render"))} may be out of date</div>
      <div class="stale-banner-reason">${escapeHtml(reasonText)}</div>
    </div>
    <div class="stale-banner-actions">
      <button class="stale-btn stale-btn-primary" data-action="regenerate" data-mode="incremental">Update this ${escapeHtml(stage === "slides" ? "slide" : (stage === "images" ? "image" : "render"))}</button>
      <button class="stale-btn" data-action="regenerate" data-mode="fresh" title="Regenerate from scratch — discards manual edits">From scratch</button>
      <button class="stale-btn stale-btn-ghost" data-action="dismiss" title="Keep the current value as-is">Dismiss</button>
    </div>
  `;
  banner.querySelectorAll("button[data-action]").forEach(btn => {
    btn.addEventListener("click", () => {
      const action = btn.dataset.action;
      const targetId = mark.target_id;
      if (action === "regenerate") {
        const mode = btn.dataset.mode === "fresh" ? "fresh" : "incremental";
        // Fresh mode is destructive — confirm before sending.
        if (mode === "fresh" && !window.confirm(
          "Regenerate from scratch?\n\nThis will overwrite any manual edits to this item. Use \"Update\" if you want to preserve your changes."
        )) return;
        sendRegenerateItem(stage, targetId, mode);
      } else if (action === "dismiss") {
        sendDismissStale(stage, targetId);
      }
    });
  });

  // Insert above the preview content so it doesn't push the iframe
  // below the fold. previewContent is the main body element.
  previewContent.parentElement.insertBefore(banner, previewContent);
}

// Send a regenerate_item WS message + track the pending ref_id so we
// can show a spinner on the originating button.
function sendRegenerateItem(stage, targetId, mode) {
  const refId = `rg-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  pendingRegens.set(refId, { stage, target_id: targetId, mode });
  ws.send(JSON.stringify({
    type: "regenerate_item",
    ref_id: refId,
    stage,
    target_id: targetId,
    mode,
  }));
  // Disable the buttons + show a transient "regenerating…" label until
  // the ack arrives. The orchestrator will broadcast stale_marks_updated
  // + item_regenerated when done; that handler re-enables the banner.
  const banner = document.getElementById("stale-banner");
  if (banner) {
    banner.querySelectorAll("button[data-action]").forEach(btn => {
      btn.disabled = true;
    });
    const title = banner.querySelector(".stale-banner-title");
    if (title) title.textContent = "Regenerating…";
  }
}

// Send a dismiss_stale WS message.
function sendDismissStale(stage, targetId) {
  const refId = `dm-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  ws.send(JSON.stringify({
    type: "dismiss_stale",
    ref_id: refId,
    stage,
    target_id: targetId,
  }));
}

// Tiny toast — bottom-right transient message. Auto-created on first
// use; reused thereafter.
let toastEl = null;
let toastTimer = null;
function flashToast(message, kind) {
  if (!toastEl) {
    toastEl = document.createElement("div");
    toastEl.className = "toast";
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = message;
  toastEl.dataset.kind = kind || "info";
  toastEl.classList.add("toast-visible");
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (toastEl) toastEl.classList.remove("toast-visible");
  }, 2400);
}
// Build the URL for a file path returned by the server.
//
// html_paths entries look like
//   "D:\\Project\\shuttleslide\\tmp\\web_review\\run_20260623_132834\\1.html"
// or on Unix
//   "/home/user/runs/run_20260623_132834/1.html"
//
// The /files/ StaticFiles mount is at the *base* output_dir, so the
// URL must include the run dirname: /files/run_20260623_132834/1.html.
// Legacy/test mode (no per-run subdir, files directly in output_dir)
// falls back to /files/<filename>.
function fileUrl(fullPath) {
  const normalized = String(fullPath).replace(/\\/g, "/");
  const m = normalized.match(/(run_\d{8}_\d{6})\/(.+)$/);
  if (m) {
    const subPath = m[2].split("/").map(encodeURIComponent).join("/");
    return `/files/${m[1]}/${subPath}`;
  }
  const filename = normalized.split("/").pop() || normalized;
  return `/files/${encodeURIComponent(filename)}`;
}

function enableApprovalButtons() {
  if (pipelineDone) return;
  approveBtn.disabled = false;
}
function disableApprovalButtons() {
  approveBtn.disabled = true;
}

// =====================================================================
// WebSocket event handling
// =====================================================================
ws.onopen = () => {
  // Don't override the status banner here. syncStatusOnLoad runs in
  // parallel and sets the right banner based on /api/status + /api/state
  // (e.g. "Pipeline complete." after hydration, or "Viewing previous
  // run..." when browsing history). Clobbering it with a generic
  // "Connected" message loses that signal — see the bug where refresh
  // after a completed run showed "Connected. Waiting for stage output..."
  // instead of the hydration result.
};

ws.onmessage = (event) => {
  let msg;
  try {
    msg = JSON.parse(event.data);
  } catch (e) {
    console.error("invalid JSON from server:", event.data);
    return;
  }
  switch (msg.type) {
    case "pipeline_state": {
      // Drives config-screen ↔ pipeline-screen switching.
      // Defined further down (showScreen); setStatusBanner is in scope.
      if (typeof handlePipelineState === "function") {
        handlePipelineState(msg.state, msg.error);
      }
      appendLog(
        "pipeline",
        `state → ${msg.state}${msg.error ? " (" + msg.error + ")" : ""}`,
        msg.state === "failed" ? "error" : "info",
      );
      break;
    }
    case "pipeline_stages": {
      // Server declares the full ordered stage list (mirrors
      // registry.resolve_order() names). Replaces the builtin STAGES
      // fallback in getAllStages() so extension tabs (script /
      // voiceover / motion_design / render_video) appear in the
      // correct execution order BEFORE the first stage_complete lands.
      // Idempotent: re-receiving the same list just re-renders tabs.
      setDeclaredStages(msg.stages || []);
      break;
    }
    case "stage_complete": {
      const snap = msg.snapshot;
      console.log(`[ws] stage_complete: stage=${snap.stage} items=${snap.state_view?.outline?.length ?? snap.state_view?.slides?.length ?? "?"}`);
      // Theme draft must always defer to a freshly-pushed server theme —
      // otherwise revert / repushed snapshots get masked by stale local
      // edits and the preview appears unchanged.
      if (snap.stage === "theme") themeDraft = null;
      // Outline draft same — structural ops (add/delete/rebalance) push
      // fresh snapshots and the draft would mask them.
      if (snap.stage === "outline") outlineDraft = null;
      // 区分两种 stage_complete:
      //   - 首次进入：snapshots[snap.stage] 之前是 null
      //     → 切到这个 stage + 重置到第一项 + 弹 banner / log / arm approve
      //   - 编辑后 refresh：apply_edit 成功后 server 重新广播同 stage 的快照
      //     → 保留用户的 activeItemIdx（用户在编辑第 N 张，提交后应保持在第 N 张）
      const wasAlreadyCached = (snapshots[snap.stage] != null);
      // Cache + mark completed + advance running indicator.
      snapshots[snap.stage] = snap;
      stageState[snap.stage] = "completed";
      // Stage list comes from the server (pipeline_stages WS message
      // or /api/state.stage_order via hydrateFromDisk) — NOT from
      // stage_complete arrivals. If the server hasn't declared this
      // stage yet, it's a registry/orchestrator drift bug worth a
      // loud warning rather than a silent client-side patch (the old
      // behavior pushed the stage onto extraStages, which broke tab
      // ordering: script appeared AFTER voiceover even though it runs
      // BEFORE rendered).
      let idx = getAllStages().indexOf(snap.stage);
      if (idx === -1) {
        console.warn(
          `[review] received stage_complete for "${snap.stage}" but it's` +
          ` not in stage_order (${getAllStages().join(", ")}). ` +
          `This is a server bug — orchestrator and registry are out of sync.`
        );
      }
      const allStages = getAllStages();
      if (idx >= 0 && idx + 1 < allStages.length) {
        const next = allStages[idx + 1];
        if (stageState[next] === "pending" || stageState[next] === undefined) {
          stageState[next] = "running";
        }
      }
      if (!wasAlreadyCached) {
        // 首次进入：自动切焦点 + 重置 item + 武装 Approve/Cancel。
        // 即使后续用户切到别的 stage tab 浏览历史，pendingGateStage
        // 仍指向这里直到下个 stage_complete 来。
        activeStage = snap.stage;
        activeItemIdx = 0;
        pendingGateStage = snap.stage;
        // Approve button always says "Approve" — the rendered stage is
        // no longer terminal (pro stages follow), so even after the
        // PPTX export the user clicks Approve to advance to voiceover.
        // Approve is only disabled by setPipelineDone() (fires after
        // the LAST stage in resolve_order). The dedicated Download
        // PPTX button is revealed separately when `rendered` completes.
        approveBtn.textContent = "Approve";
        renderAll();
        setStatusBanner(`Stage "${stageLabel(snap.stage)}" ready for review.`);
        enableApprovalButtons();
        // PPTX/HTML export artifacts are ready as soon as `rendered`
        // completes — reveal the done-banner AND the dedicated
        // Download PPTX button immediately so the user can grab the
        // file while voiceover/render_video run. Does NOT flip
        // pipelineDone (that waits for the last stage) and does NOT
        // disable Approve (pro stages still need it).
        if (snap.stage === "rendered") {
          const paths =
            (snap.state_view && snap.state_view.html_paths) || [];
          showPptxDownloadButton(paths);
          downloadPptxBtn.hidden = false;
        }
        const count =
          snap.slide_count ??
          (Array.isArray(snap.items) ? snap.items.length : null);
        appendLog(
          `stage:${snap.stage}`,
          count != null
            ? `Completed (${count} items) — ready for review`
            : "Completed — ready for review",
          "ok",
        );
      } else {
        // 编辑后 refresh：只更新缓存 + 重画当前 preview。
        // 不切 stage（用户本来就在这个 stage）、不重置 item（保留用户选择）、
        // 不弹 banner（避免每次微调都闪一下）。
        renderAll();
      }
      break;
    }
    case "pipeline_done": {
      const paths = msg.html_paths || [];
      // Whole pipeline finished (orchestrator ran the LAST stage in
      // resolve_order). Set pipelineDone (flips approveBtn to
      // "Download PPTX") and ensure the banner is shown in case the
      // stage_complete for "rendered" was missed (e.g., client
      // connected late).
      setPipelineDone();
      showPptxDownloadButton(paths);
      appendLog(
        "pipeline",
        `Pipeline complete — ${paths.length} slide(s) exported`,
        "ok",
      );
      // One log line per produced file so users can see what was
      // written without expanding #done-banner. Mirrors the same
      // info as the banner (path is the source of truth), but lives
      // in the log's scrollable history — useful when the banner
      // gets cleared by a later status change.
      paths.forEach((p, i) => {
        const filename = String(p).split(/[\\/]/).pop() || p;
        appendLog(`slide ${i + 1}`, filename, "info");
      });
      break;
    }
    case "error": {
      setStatusBanner(`Error: ${msg.message}`, "error");
      appendLog("server", msg.message || "unknown error", "error");
      if (msg.fatal) {
        // Same systematic fix as resetPipelineUiState: iterate
        // Object.keys(stageState) so pro extension stages also get
        // marked cancelled on a fatal error mid-pro-stage run.
        for (const s of Object.keys(stageState)) {
          if (stageState[s] === "pending" || stageState[s] === "running") {
            stageState[s] = "cancelled";
          }
        }
        renderStageTabs();
        disableApprovalButtons();
      }
      break;
    }
    case "stage_started":
      // Reserved for PR3 — for now we infer running state from
      // gaps between stage_complete events.
      break;
    case "stage_progress": {
      // Live progress from _on_llm_response — drives the strip during a
      // running stage. Atomic stages (theme / rendered) arrive with
      // percent=null and trigger the indeterminate flowing-stripe CSS.
      const indeterminate = (msg.percent == null);
      updateProgressStrip("running", {
        stage: msg.stage,
        current: msg.current,
        total: msg.total,
        percent: msg.percent,
        elapsed: msg.elapsed_seconds,
        eta: msg.eta_seconds,
        label: msg.label,
        indeterminate,
      });
      break;
    }
    case "log_entry": {
      // Per-LLM-call progress from the on_llm_response callback.
      // Auto-expand on first log_entry of a new run so the user sees
      // the pipeline's progress without having to open the drawer
      // manually. Subsequent collapses persist until next reset.
      appendLog(msg.scope || "log", msg.message || "", msg.level || "info");
      if (!_autoExpandedThisRun) {
        expandLogDrawer();
        _autoExpandedThisRun = true;
      }
      break;
    }
    case "edit_applied": {
      // Remove the local "正在生成回复…" marker now that the real
      // response (applied/rejected/etc.) is being appended below.
      if (msg.ref_id) clearPendingAssistant(msg.ref_id);
      // Add-slide modal ack: close the modal if its ref_id matches.
      if (msg.ref_id && typeof handleAddSlideAck === "function") {
        handleAddSlideAck(msg.ref_id, true, null);
      }
      // Drop upload ack (WS path): if this ref_id matches a pending
      // drag-drop upload, finalize the placeholder → img swap before
      // the iframe reloads. HTTP /upload path calls _finalizeDrop
      // directly from its .then() chain.
      if (msg.ref_id && pendingDrops.has(msg.ref_id)) {
        _finalizeDrop(msg.ref_id, msg.new_preview || "", msg.width, msg.height);
      }
      // Append an "applied" entry carrying the unified diff so the
      // user can see exactly what changed. The sidebar History panel
      // only carries action_label + new_value_summary (short prose)
      // — the unified diff lives here, otherwise it has no home.
      //
      // This entry is ephemeral: switching targets and back triggers
      // refreshChatHistoryForActive, whose server response replaces
      // local cache with SessionStore contents (user/assistant only).
      // SessionStore intentionally doesn't persist diffs — the
      // conversation context is what matters for future LLM turns,
      // not the diff. Re-runs and screenshots are the durable record.
      const path = msg.target_path || [];
      if (msg.diff) {
        appendChatEntry(path, "applied", "Applied", { diff: msg.diff });
      }
      // Image upload acks carry the description that landed in state
      // (user-supplied or VLM-generated). Diff is None for image
      // uploads, so without this branch the upload would be silent —
      // particularly bad in the VLM case where the user wants to
      // sanity-check the auto-generated caption.
      if (msg.description !== null && msg.description !== undefined) {
        const label = msg.description
          ? `Description: "${msg.description}"`
          : "Uploaded (no description — VLM unavailable or disabled)";
        appendChatEntry(path, "applied", label);
      }
      setChatEditedFlag(path, true);
      setChatError("");
      // LLM edits flip the global lock on send; release it now that
      // the edit resolved. No-op for direct/image edits (lock was
      // never set). Safe to call unconditionally.
      if (msg.ref_id && msg.ref_id === activeEditRefId) {
        clearEditInProgress();
      }
      // Re-enable Send — chatSendBtn was disabled on send; restore
      // whenever an edit resolves so the user can immediately retry.
      forgetRefId(msg.ref_id);
      chatSendBtn.disabled = !chatInput.value.trim();
      break;
    }
    case "edit_rejected": {
      // Same pending-marker removal as edit_applied.
      if (msg.ref_id) clearPendingAssistant(msg.ref_id);
      // Add-slide modal rejection: surface inline error in the modal.
      if (msg.ref_id && typeof handleAddSlideAck === "function") {
        handleAddSlideAck(msg.ref_id, false, msg.error);
      }
      // A pending drag-drop upload can be rejected by the server (e.g.
      // target lookup fails, image decode fails). Surface the error on
      // the placeholder so the user knows the drop didn't take.
      if (msg.ref_id && pendingDrops.has(msg.ref_id)) {
        _failDrop(msg.ref_id, msg.error || "upload rejected");
      }
      // EditRejectedMsg doesn't carry target_path (legacy schema); fall
      // back to the path we stashed on send so the entry lands in the
      // right chat history instead of under the empty-path key.
      const path = resolveResponsePath(msg);
      if (msg.kind === "out_of_scope") {
        // Editor recognised the request as deck-level (add/remove slides,
        // theme change, ...) and returned structured guidance instead of
        // mutating the per-slide target. Render a guidance card with a
        // stage-switch button instead of a plain error. We do NOT call
        // setChatError here — this isn't a failure the user needs to
        // "fix" by rephrasing; the chat card tells them what to do next.
        if (path.length > 0) {
          appendChatEntry(path, "out_of_scope", msg.guidance || msg.error, {
            ref_id: msg.ref_id,
            suggested_stage: msg.suggested_stage,
          });
        }
      } else {
        if (path.length > 0) {
          appendChatEntry(path, "rejected", msg.error || "Rejected", {
            ref_id: msg.ref_id,
          });
        }
        setChatError(msg.error || "edit rejected");
      }
      // Same lock-release as edit_applied. Rejection of an LLM edit
      // also releases the global lock.
      if (msg.ref_id && msg.ref_id === activeEditRefId) {
        clearEditInProgress();
      }
      forgetRefId(msg.ref_id);
      chatSendBtn.disabled = !chatInput.value.trim();
      break;
    }
    case "edit_cancelled": {
      // Server confirmed: the in-flight LLM edit was cancelled. The
      // orchestrator already rolled back in-memory state to the
      // pre-edit snapshot, so no snapshot refresh is needed — the
      // client-side cache still reflects the unchanged state.
      //
      // Drop the pending marker (if still present — typically the
      // cancel round-trip lands before any applied/rejected follows)
      // then append a grey "（已取消）" marker so the chat log shows
      // what the user attempted. The just-sent user message stays
      // above it (already echoed locally on send) so the conversation
      // reads naturally: "user: 把标题改成红色 → （已取消）".
      if (msg.ref_id) clearPendingAssistant(msg.ref_id);
      if (msg.ref_id && msg.ref_id === activeEditRefId) {
        clearEditInProgress();
        // EditCancelledMsg has no target_path; use the stashed send-time
        // path so the cancelled marker lands on the right history even
        // if the user switched targets mid-edit.
        const path = resolveResponsePath(msg);
        if (path.length > 0) {
          appendChatEntry(path, "cancelled", "（已取消）");
        } else {
          // Last-resort fallback: assume the active target is where the
          // edit was happening. Pre-fix behaviour.
          const target = getActiveTarget();
          if (target) {
            appendChatEntry(target.path, "cancelled", "（已取消）");
          }
        }
      }
      forgetRefId(msg.ref_id);
      chatSendBtn.disabled = !chatInput.value.trim();
      break;
    }
    case "chat_history": {
      // Server push: per-target LLM conversation (user/assistant only,
      // what SessionStore persists). Merge with local-only entries
      // (applied/rejected) which carry diffs and rejection reasons and
      // are NOT tracked server-side.
      //
      // Why merge instead of replace: the orchestrator's LLM-mode edit
      // path emits chat_history from inside apply_edit, but the
      // server's per-client EditAppliedMsg ack is unicast BEFORE the
      // broadcast lands (anyio ws send is FIFO; the unicast's await
      // enters the queue before the broadcast task runs). Without
      // preservation, the late-arriving chat_history would replace
      // local cache and erase the applied+diff entry that edit_applied
      // just appended — diff would be invisible.
      //
      // Cap local-only entries to the most recent 3 so repeated
      // target switches don't accumulate unbounded history.
      const path = msg.target_path || [];
      const key = targetKey(path);
      const serverMsgs = Array.isArray(msg.messages) ? msg.messages : [];
      const localExtras = (chatHistories[key] || [])
        .filter(e => e.role === "applied" || e.role === "rejected" || e.role === "cancelled" || e.role === "out_of_scope" || e.role === "pending")
        .slice(-3);
      chatHistories[key] = [...serverMsgs, ...localExtras];
      const active = getActiveTarget();
      if (active && targetKey(active.path) === key) {
        renderChatHistory(active.path);
      }
      break;
    }
    case "history_snapshot": {
      // Edit history stack — drives the sidebar History panel.
      // Cache so stage switches can re-filter without a round-trip.
      lastHistoryEntries = Array.isArray(msg.entries) ? msg.entries : [];
      renderHistoryPanel(lastHistoryEntries);
      break;
    }
    case "stale_marks_updated": {
      // Server pushes this after every edit / undo / revert / regenerate.
      // Cache + re-render thumbnails + preview banner so badges stay
      // in sync. We don't gate on activeStage here — every stage's
      // badges might need to update (e.g. slides[i] regen clears
      // slides[i] mark AND adds rendered[i] mark).
      staleMarks = (msg.marks && typeof msg.marks === "object") ? msg.marks : {};
      // Resolve any pending regen — the matching item_regenerated
      // arrives shortly after, but stale_marks_updated fires first
      // (orchestrator emits them in that order). The spinner stays
      // until item_regenerated to keep the UI consistent.
      renderThumbnails();
      renderStaleBanner();
      break;
    }
    case "item_regenerated": {
      // Ack for a regenerate_item request. The snapshot has already
      // been re-emitted via stage_complete (orchestrator refreshes
      // the stage snapshot before broadcasting this). Clear the
      // spinner on the originating button + pop a transient toast.
      const refId = msg.ref_id || "";
      const pending = refId ? pendingRegens.get(refId) : null;
      if (refId) pendingRegens.delete(refId);
      // The stage snapshot for the regenerated stage has been re-emitted
      // by the orchestrator (via _refresh_after_edit). Make sure the
      // preview picks it up if the user is currently viewing the
      // regenerated stage — renderThumbnails / renderPreview read
      // from the cached snapshot, which has been updated in-place
      // by the stage_complete handler.
      renderThumbnails();
      renderPreview();
      renderStaleBanner();
      if (pending) {
        const modeLabel = pending.mode === "fresh" ? "from scratch" : "incremental";
        flashToast(`Updated ${pending.stage}:${pending.target_id} (${modeLabel})`);
      }
      // Reset the progress strip. The regenerate path runs the LLM
      // (slide_builder / image_acquirer), and _on_llm_response drives
      // stage_progress → updateProgressStrip("running", ...) the whole
      // time. Nothing else flips the strip back when regen finishes,
      // so without this reset the user sees a perpetual "Running: ..."
      // strip even after the new HTML has been written to state.
      if (pendingGateStage) {
        setStatusBanner(`Stage "${stageLabel(pendingGateStage)}" ready for review.`);
      } else {
        setStatusBanner(`Updated ${msg.stage}:${msg.target_id}.`);
      }
      break;
    }
    case "stale_dismissed": {
      // Server ack for dismiss_stale. The stale_marks_updated broadcast
      // has already cleared the badge; this just confirms the request.
      flashToast(`Dismissed stale mark`);
      break;
    }
    default:
      console.warn("unknown server message:", msg);
  }
};

ws.onclose = (event) => {
  setStatusBanner(
    event.wasClean ? "Connection closed." : "Connection lost.",
    event.wasClean ? "" : "error"
  );
  disableApprovalButtons();
};

ws.onerror = (err) => {
  console.error("WS error:", err);
  setStatusBanner("WebSocket error.", "error");
};

// Approve/Cancel always target the gate's pending stage — never
// activeStage — so browsing history is non-destructive.
approveBtn.onclick = () => {
  // Post-pipeline_done the button is disabled by setPipelineDone(),
  // so this branch is unreachable in normal flow. Defensive return in
  // case a stale handler fires.
  if (pipelineDone) return;
  if (!pendingGateStage) return;
  ws.send(JSON.stringify({ type: "approve_stage", stage: pendingGateStage }));
  disableApprovalButtons();
  const verb = (pendingGateStage === "rendered") ? "Exported" : "Approved";
  setStatusBanner(`${verb} "${stageLabel(pendingGateStage)}". Continuing...`);
};

// Dedicated click handler for the download button. Decoupled from the
// Approve button so pro flows can keep using Approve to advance stages
// while still being able to download the PPTX after `rendered` lands.
downloadPptxBtn.onclick = () => {
  downloadPptx();
};

async function downloadPptx() {
  // Re-entry guard — if a render is already in flight, ignore.
  // The button is also disabled (see finally block), but belt+suspenders
  // against double-clicks before the DOM updates.
  if (downloadPptxBtn.dataset.converting === "true") return;
  downloadPptxBtn.dataset.converting = "true";
  downloadPptxBtn.disabled = true;
  downloadPptxBtn.textContent = "Rendering...";
  // Auto-expand the drawer so the user sees the progress log immediately,
  // even if they hadn't opened it manually.
  expandLogDrawer();
  appendLog(
    "pptx",
    "Starting PPTX render (Playwright + python-pptx — first run takes ~10-20s, cached after)",
  );
  const t0 = performance.now();
  try {
    const resp = await fetch("/api/pptx");
    // The endpoint returns JSONResponse on error (400/500). Detect
    // via Content-Type so we can surface the message instead of
    // downloading a JSON blob as presentation.pptx.
    const ct = resp.headers.get("Content-Type") || "";
    if (!resp.ok || ct.includes("application/json")) {
      let detail = `HTTP ${resp.status}`;
      try {
        const j = await resp.json();
        if (j.detail) detail = j.detail;
      } catch (_) { /* not JSON, fall through with status detail */ }
      appendLog("pptx", `Render failed: ${detail}`, "error");
      setStatusBanner(`PPTX render failed: ${detail}`, "error");
      return;
    }
    const blob = await resp.blob();
    // Hidden anchor so the browser treats the blob URL as a download.
    // revokeObjectURL on a delay — Chrome needs the URL to live until
    // the download has actually started (1000ms is well past that).
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "presentation.pptx";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    const kb = Math.round(blob.size / 1024);
    const sec = Math.round((performance.now() - t0) / 1000);
    appendLog(
      "pptx",
      `Render complete in ${sec}s — downloaded presentation.pptx (${kb} KB)`,
      "ok",
    );
    setStatusBanner("PPTX downloaded.");
  } catch (err) {
    appendLog("pptx", `Network error: ${err.message || err}`, "error");
    setStatusBanner(`Download failed: ${err.message || err}`, "error");
  } finally {
    downloadPptxBtn.dataset.converting = "false";
    downloadPptxBtn.disabled = false;
    downloadPptxBtn.textContent = "Download PPTX";
  }
}

// =====================================================================
// Config screen — form, persistence, screen switching
// =====================================================================
const configForm = document.getElementById("config-form");
const configError = document.getElementById("config-error");
const startBtn = document.getElementById("start-btn");
const homeBtn = document.getElementById("home-btn");
const cancelSelectionBtn = document.getElementById("cancel-selection-btn");
const resumeBanner = document.getElementById("resume-banner");
const resumeRunId = document.getElementById("resume-run-id");
const configScreen = document.getElementById("config-screen");

// --- Topic input tab switching (direct text / HTML upload / MD-TXT upload) ---
document.querySelectorAll(".topic-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    const mode = btn.dataset.topicMode;
    document.querySelectorAll(".topic-tab").forEach(b => b.classList.toggle("active", b === btn));
    document.querySelectorAll(".topic-tab-body").forEach(body => {
      body.classList.toggle("active", body.dataset.topicMode === mode);
    });
  });
});

// --- User-uploaded image library (homepage "Image assets" fieldset) ---
// Maintains an in-memory list of selected images (each row carries a
// File + a description textarea + an Auto-describe button + a Remove
// button). The submit handler serializes the list into the JSON
// `user_images` payload key. The server's _extract_config_kwargs
// re-encodes via Pillow, persists to a temp staging dir, and (when
// VLM is enabled) auto-fills blank descriptions at submit time.
const userImagesInput = document.getElementById("user-images-input");
const userImageListEl = document.getElementById("user-image-list");
// Array of {file: File, filename: str, mime: str, description: str,
// data_b64: str|null}. data_b64 is lazy-populated by readFileAsBase64
// the first time it's needed (Auto-describe click or submit).
let userImageLibrary = [];
const USER_IMAGE_MAX_BYTES = 10 * 1024 * 1024;
const USER_IMAGE_OK_MIMES = new Set(["image/png", "image/jpeg", "image/webp"]);

function renderUserImageList() {
  if (!userImageListEl) return;
  userImageListEl.innerHTML = "";
  userImageLibrary.forEach((entry, idx) => {
    const row = document.createElement("div");
    row.className = "user-image-row";
    row.dataset.idx = String(idx);

    const thumb = document.createElement("img");
    thumb.className = "user-image-thumb";
    // createObjectURL is cheaper than base64 for the thumbnail; we
    // revoke on Remove to avoid leaks.
    const objUrl = URL.createObjectURL(entry.file);
    thumb.src = objUrl;
    thumb.alt = entry.filename;
    entry._objUrl = objUrl;

    const meta = document.createElement("div");
    meta.className = "user-image-meta";
    const name = document.createElement("div");
    name.className = "user-image-name";
    name.textContent = entry.filename;
    const sub = document.createElement("div");
    sub.className = "user-image-sub";
    const sizeKb = Math.round(entry.file.size / 1024);
    sub.textContent = `${entry.mime} · ${sizeKb} KB`;
    meta.appendChild(name);
    meta.appendChild(sub);

    const descWrap = document.createElement("div");
    descWrap.className = "user-image-desc-wrap";
    const desc = document.createElement("textarea");
    desc.className = "user-image-desc";
    desc.rows = 2;
    desc.placeholder = "Description (optional — blank = VLM auto-fill at submit)";
    desc.value = entry.description;
    desc.addEventListener("input", () => { entry.description = desc.value; });
    descWrap.appendChild(desc);

    const actions = document.createElement("div");
    actions.className = "user-image-actions";
    const auto = document.createElement("button");
    auto.type = "button";
    auto.className = "small-btn user-image-autodescribe";
    auto.textContent = "Auto-describe";
    auto.addEventListener("click", () => onAutoDescribeClick(idx, auto, desc));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "small-btn user-image-remove";
    remove.textContent = "Remove";
    remove.addEventListener("click", () => onUserImageRemove(idx));
    actions.appendChild(auto);
    actions.appendChild(remove);

    row.appendChild(thumb);
    row.appendChild(meta);
    row.appendChild(descWrap);
    row.appendChild(actions);
    userImageListEl.appendChild(row);
  });
}

async function ensureDataB64(entry) {
  if (entry.data_b64) return entry.data_b64;
  entry.data_b64 = await readFileAsBase64(entry.file);
  return entry.data_b64;
}

async function onAutoDescribeClick(idx, btn, descEl) {
  const entry = userImageLibrary[idx];
  if (!entry) return;
  // Read current creds from the form so the user can describe an
  // image without yet submitting the whole form. effective_defaults
  // on the server is the fallback when fields are blank.
  const creds = {};
  for (const k of ["vlm_api_base", "vlm_api_key", "vlm_model", "api_base", "api_key", "model"]) {
    const el = configForm.elements.namedItem(k);
    if (el && el.value && el.value.trim()) creds[k] = el.value.trim();
  }
  let data_b64;
  try {
    data_b64 = await ensureDataB64(entry);
  } catch (err) {
    descEl.placeholder = `Failed to read file: ${err.message || err}`;
    return;
  }
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "Describing…";
  try {
    const resp = await fetch("/api/vlm_describe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ data_b64, mime: entry.mime, ...creds }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      descEl.placeholder = `VLM error: ${data.detail || resp.status}`;
      return;
    }
    if (data.description) {
      descEl.value = data.description;
      entry.description = data.description;
    }
  } catch (err) {
    descEl.placeholder = `Network error: ${err.message || err}`;
  } finally {
    btn.disabled = false;
    btn.textContent = prev;
  }
}

function onUserImageRemove(idx) {
  const entry = userImageLibrary[idx];
  if (entry && entry._objUrl) {
    try { URL.revokeObjectURL(entry._objUrl); } catch (_) {}
  }
  userImageLibrary.splice(idx, 1);
  renderUserImageList();
}

if (userImagesInput) {
  userImagesInput.addEventListener("change", () => {
    const files = userImagesInput.files;
    if (!files || !files.length) return;
    let added = 0;
    for (const f of files) {
      const mime = (f.type || "").toLowerCase();
      if (!USER_IMAGE_OK_MIMES.has(mime)) {
        showConfigError(`Skipped ${f.name}: unsupported type ${mime || "(unknown)"}. Use PNG / JPEG / WebP.`);
        continue;
      }
      if (f.size > USER_IMAGE_MAX_BYTES) {
        showConfigError(`Skipped ${f.name}: ${Math.round(f.size / 1024)} KB exceeds 10 MB limit.`);
        continue;
      }
      userImageLibrary.push({
        file: f,
        filename: f.name,
        mime,
        description: "",
        data_b64: null,
      });
      added++;
    }
    // Reset the input so the same file can be re-picked after Remove.
    userImagesInput.value = "";
    if (added) renderUserImageList();
  });
}

async function readUserImageLibraryForSubmit() {
  // Returns the JSON array sent to the server: each entry must carry
  // filename / mime / data_b64 / description. data_b64 is lazily read
  // here (we don't pre-base64 all files on selection — they might be
  // Removed before submit).
  const out = [];
  for (const entry of userImageLibrary) {
    const data_b64 = await ensureDataB64(entry);
    out.push({
      filename: entry.filename,
      mime: entry.mime,
      data_b64,
      description: (entry.description || "").trim(),
    });
  }
  return out;
}

// --- Persistence ---
// data-persist="local"  → localStorage (non-sensitive, survives close)
// data-persist="session" → sessionStorage (api keys, cleared on tab close)
function _persistStore(input) {
  return input.dataset.persist === "session" ? sessionStorage : localStorage;
}
function _persistKey(input) {
  return `shuttleslide.config.${input.name}`;
}

// Reverse-derive a ratio string ("9:16", "1:1", "4:3", ...) from a
// (width_emu, height_emu) pair saved in agent_state.json. Used by
// onRunCardClick so the ratio picker reflects the loaded run instead
// of the last-used localStorage value.
//
// Mirrors the math in shuttleslide.agent.geometry.aspect_ratio_to_dimensions:
// longest side is fixed at _CANVAS_BASELINE_PX = 1280 CSS px. We accept
// ±1 px tolerance to absorb rounding from EMU→px integer division.
const EMU_PER_CSS_PX = 9525;
const _CANVAS_BASELINE_PX = 1280;
const _STANDARD_RATIOS = ["16:9", "9:16", "1:1", "3:4", "4:3"];
function _gcd(a, b) {
  a = Math.abs(a); b = Math.abs(b);
  while (b) { [a, b] = [b, a % b]; }
  return a || 1;
}
function emuPairToRatioString(wEmu, hEmu) {
  if (!Number.isFinite(wEmu) || !Number.isFinite(hEmu) || wEmu <= 0 || hEmu <= 0) return null;
  const wPx = Math.round(wEmu / EMU_PER_CSS_PX);
  const hPx = Math.round(hEmu / EMU_PER_CSS_PX);
  for (const r of _STANDARD_RATIOS) {
    const [rw, rh] = r.split(":").map(Number);
    const expectW = rw >= rh ? _CANVAS_BASELINE_PX : Math.round(_CANVAS_BASELINE_PX * rw / rh);
    const expectH = rh >= rw ? _CANVAS_BASELINE_PX : Math.round(_CANVAS_BASELINE_PX * rh / rw);
    if (Math.abs(wPx - expectW) <= 1 && Math.abs(hPx - expectH) <= 1) return r;
  }
  // Non-standard ratio (e.g. user-typed "5:4") — GCD-simplify the px
  // pair so the custom text field gets a clean "W:H" string.
  const g = _gcd(wPx, hPx);
  return `${wPx / g}:${hPx / g}`;
}

// Apply a ratio string to the canvas-aspect-ratio radio group. Built-in
// ratios (16:9, 9:16, 1:1, 3:4) just check the matching radio; anything
// else (4:3, 5:4, ...) switches to the custom radio and fills its text
// field, triggering sync() to update the radio's .value.
function applyCanvasRatioToForm(ratioStr) {
  if (!ratioStr) return;
  const matchRadio = document.querySelector(
    `input[type="radio"][name="canvas_aspect_ratio"][value="${ratioStr}"]`
  );
  if (matchRadio) {
    matchRadio.checked = true;
    return;
  }
  const customRadio = document.querySelector(
    'input[type="radio"][name="canvas_aspect_ratio"][value="custom"]'
  );
  const customText = document.querySelector('input[name="canvas_aspect_ratio_custom"]');
  if (customRadio && customText) {
    customRadio.checked = true;
    customText.value = ratioStr;
    // sync() (registered on customText 'input' in setupCanvasRatioPicker)
    // reads customText.value and rewrites customRadio.value to match —
    // without this dispatch, the radio's value stays "custom" and the
    // server would reject the submit.
    customText.dispatchEvent(new Event("input", { bubbles: true }));
  }
}
function loadPersistedForm() {
  configForm.querySelectorAll("[data-persist]").forEach(input => {
    const v = _persistStore(input).getItem(_persistKey(input));
    if (v === null) return;
    if (input.type === "checkbox") {
      input.checked = (v === "1");
    } else if (input.type === "radio") {
      // Radio .value is the option's own identity (e.g. "16:9", "9:16").
      // Assigning input.value = v would OVERWRITE that identity, so every
      // radio in the group ends up with the same value and whichever
      // happens to be .checked (e.g. the HTML-default "16:9") submits the
      // persisted value regardless of which label the user sees selected.
      // Check this radio only if its existing value already matches.
      if (input.value === v) input.checked = true;
    } else {
      input.value = v;
    }
  });
}
function savePersistedField(input) {
  if (!input.dataset.persist) return;
  const v = input.type === "checkbox" ? (input.checked ? "1" : "0") : input.value;
  _persistStore(input).setItem(_persistKey(input), v);
}
configForm.addEventListener("input", e => {
  if (e.target && e.target.dataset && e.target.dataset.persist) {
    savePersistedField(e.target);
  }
});
loadPersistedForm();

// --- Error helpers ---
function showConfigError(msg) {
  configError.textContent = msg;
  configError.style.display = "block";
}
function clearConfigError() {
  configError.style.display = "none";
  configError.textContent = "";
}

// --- File → base64 (without the data: URI prefix) ---
function readFileAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result);
      const idx = s.indexOf(",");
      resolve(idx >= 0 ? s.slice(idx + 1) : "");
    };
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// --- Recent runs: list + select a previous run to load ---
// When a run is selected, the form's topic / style / target_count
// fields are filled from the run's agent_state.json (fetched from
// /files/<run_dirname>/agent_state.json), the Submit button label
// changes to "Load run", and submit includes ``load_state_from``.
// The server then starts the orchestrator with
// load_state_on_start=True so all LLM calls are skipped and the
// cached stage snapshots are re-emitted for re-review.
const recentRunsList = document.getElementById("recent-runs-list");
const refreshRunsBtn = document.getElementById("refresh-runs-btn");
let selectedRunDirname = null;

function formatTimestamp(runDirname) {
  // run_YYYYMMDD_HHMMSS -> "YYYY-MM-DD HH:MM:SS"
  const m = /^run_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})$/.exec(runDirname);
  if (!m) return runDirname;
  return `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}:${m[6]}`;
}

async function fetchAndRenderRuns() {
  recentRunsList.innerHTML = `<p class="recent-runs-empty" style="color:var(--text-tertiary);font-size:12px;">Loading...</p>`;
  try {
    const resp = await fetch("/api/runs");
    const data = await resp.json();
    renderRuns(data.runs || []);
  } catch (err) {
    recentRunsList.innerHTML = `<p class="recent-runs-empty" style="color:var(--error);font-size:12px;">Failed to load: ${escapeHtml(err.message || err)}</p>`;
  }
}

function renderRuns(runs) {
  if (runs.length === 0) {
    recentRunsList.innerHTML = `<p class="recent-runs-empty" style="color:var(--text-tertiary);font-size:12px;">No previous runs yet. Run a pipeline and they'll show up here.</p>`;
    return;
  }
  recentRunsList.innerHTML = "";
  runs.forEach(run => {
    const card = document.createElement("div");
    card.className = "run-card";
    if (run.run_dirname === selectedRunDirname) card.classList.add("selected");
    card.dataset.runDirname = run.run_dirname;
    const statusBadge = run.has_html
      ? `<span class="badge done">complete</span>`
      : `<span class="badge partial">partial</span>`;
    card.innerHTML = `
      <div class="run-card-row1">
        <span class="run-card-timestamp">${escapeHtml(formatTimestamp(run.run_dirname))}</span>
        <span class="run-card-topic" title="${escapeAttr(run.topic_preview)}">${escapeHtml(run.topic_preview || "(no topic)")}</span>
      </div>
      <div class="run-card-meta">
        ${statusBadge}
        ${run.slide_count} slide${run.slide_count === 1 ? "" : "s"}
      </div>
    `;
    card.addEventListener("click", () => onRunCardClick(run));
    recentRunsList.appendChild(card);
  });
}

// Field names that belong to the "task" (not the environment).
// When entering resume mode we disable these; when cancelling we
// also clear them so values from the loaded run don't leak into the
// next fresh run. Credential / advanced fields are NOT cleared —
// they're environment config, not task parameters.
const RUN_TASK_FIELDS = ["topic", "html_file", "text_file", "style_hint", "target_slide_count"];
const RUN_TASK_CLEAR_FIELDS = ["topic", "style_hint", "target_slide_count"];

function setResumeMode(isOn, runDirname) {
  // Toggle the four task-scope inputs + the three topic-tab buttons.
  // Disabled so they don't submit (defense-in-depth — the submit
  // handler also deletes payload.topic in resume mode).
  RUN_TASK_FIELDS.forEach(name => {
    const el = configForm.elements.namedItem(name);
    if (el) el.disabled = isOn;
  });
  document.querySelectorAll(".topic-tab").forEach(btn => { btn.disabled = isOn; });

  // Visual indicator: banner + class on the screen container.
  if (resumeBanner) resumeBanner.hidden = !isOn;
  if (resumeRunId) resumeRunId.textContent = runDirname || "";
  if (configScreen) configScreen.classList.toggle("resume-mode", isOn);
  if (cancelSelectionBtn) cancelSelectionBtn.hidden = !isOn;

  // Button label doubles as mode indicator.
  startBtn.textContent = isOn ? "Resume run →" : "Start pipeline";
}

function clearRunTaskFields() {
  RUN_TASK_CLEAR_FIELDS.forEach(name => {
    const el = configForm.elements.namedItem(name);
    if (el) el.value = "";
  });
  // Also reset file inputs (HTML / MD upload) — value is the only
  // way to clear them; setting to "" works cross-browser.
  ["html_file", "text_file"].forEach(name => {
    const el = configForm.elements.namedItem(name);
    if (el) el.value = "";
  });
}

async function onRunCardClick(run) {
  // Toggle: clicking the selected card again deselects it.
  if (selectedRunDirname === run.run_dirname) {
    selectedRunDirname = null;
    document.querySelectorAll(".run-card").forEach(c => c.classList.remove("selected"));
    setResumeMode(false);
    clearRunTaskFields();
    return;
  }
  selectedRunDirname = run.run_dirname;
  document.querySelectorAll(".run-card").forEach(c => {
    c.classList.toggle("selected", c.dataset.runDirname === run.run_dirname);
  });

  // Pre-fill the form from the state file so the user can SEE what
  // they're resuming — then immediately disable the fields so they
  // can't edit them. Pre-fill-then-lock is the whole point: the
  // server reads from disk regardless, so any edits would have
  // been silently dropped (the bug this UI fixes).
  try {
    const resp = await fetch(`/files/${encodeURIComponent(run.run_dirname)}/agent_state.json`);
    if (resp.ok) {
      const state = await resp.json();
      if (state.topic) {
        const topicEl = configForm.elements.namedItem("topic");
        if (topicEl) topicEl.value = state.topic;
      }
      if (state.style_hint) {
        const styleEl = configForm.elements.namedItem("style_hint");
        if (styleEl) styleEl.value = state.style_hint;
      }
      if (state.target_count != null) {
        const tcEl = configForm.elements.namedItem("target_slide_count");
        if (tcEl) tcEl.value = String(state.target_count);
      }
      // Canvas aspect-ratio: agent_state.json persists canvas_*_emu
      // (integers) but not the ratio string. Reverse-derive it so the
      // radio picker reflects the loaded run instead of the user's
      // last-fresh-run selection (loadPersistedForm's localStorage value).
      if (state.canvas_width_emu && state.canvas_height_emu) {
        const ratioStr = emuPairToRatioString(state.canvas_width_emu, state.canvas_height_emu);
        if (ratioStr) applyCanvasRatioToForm(ratioStr);
      }
    }
  } catch (err) {
    // Non-fatal — server still loads the state from disk.
    console.warn("Could not pre-fill form from state:", err);
  }

  setResumeMode(true, run.run_dirname);
}

refreshRunsBtn.addEventListener("click", fetchAndRenderRuns);

// Cancel-selection button — exits resume mode, clears task-scope
// fields so the next "Start pipeline" is unambiguously a fresh run
// (no stale topic / style / count from the previously-selected run).
cancelSelectionBtn.addEventListener("click", () => {
  selectedRunDirname = null;
  document.querySelectorAll(".run-card").forEach(c => c.classList.remove("selected"));
  setResumeMode(false);
  clearRunTaskFields();
});

// --- Form submission ---
configForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  clearConfigError();

  // Build the JSON payload from all text inputs. Checkboxes need
  // explicit handling because unchecked ones don't appear in FormData.
  // Radio buttons (canvas_aspect_ratio) need the same: only the checked
  // option's value goes upstream; HTMLFormElement.elements exposes every
  // option, and the default loop would have picked the first one regardless
  // of which was checked.
  const payload = {};
  const seen = new Set();
  for (const el of configForm.elements) {
    if (!el.name) continue;
    if (el.type === "file") continue; // handled below
    if (el.type === "checkbox") {
      payload[el.name] = el.checked;
      seen.add(el.name);
      continue;
    }
    if (el.type === "radio") {
      if (seen.has(el.name)) continue;
      // Send value only if a radio in the group is checked. If none is,
      // skip the field entirely (server treats absent as default).
      if (el.checked) {
        payload[el.name] = el.value;
        seen.add(el.name);
      }
      continue;
    }
    if (seen.has(el.name)) continue; // first occurrence wins
    payload[el.name] = el.value;
    seen.add(el.name);
  }
  // Drop the helper field used only to drive the custom-ratio UX — the
  // server has no knowledge of it and would otherwise see it as an
  // unknown form key (silently dropped, but no point sending it).
  delete payload.canvas_aspect_ratio_custom;

  // Determine topic source — whichever tab is active.
  const activeTab = document.querySelector(".topic-tab.active");
  const mode = activeTab ? activeTab.dataset.topicMode : "text";

  // Load-state mode: short-circuits the topic-tab logic entirely.
  // The server reads topic / style / target_count from the chosen
  // run's agent_state.json, so we don't need any of the form's
  // topic-related fields. We DO still send api creds if present
  // (server uses placeholders if missing in load mode).
  if (selectedRunDirname) {
    payload.load_state_from = selectedRunDirname;
    // Strip fields the server would otherwise treat as conflicting
    // topic sources. (Server's _extract_config_kwargs enforces
    // mutual exclusion.)
    delete payload.topic;
    delete payload.html_file_b64;
    delete payload.text_file_b64;
  } else {
    try {
    if (mode === "text") {
      if (!payload.topic || !payload.topic.trim()) {
        showConfigError("Topic text is empty. Type a topic, or switch to a file upload tab.");
        return;
      }
    } else if (mode === "html") {
      const fileEl = configForm.elements.namedItem("html_file");
      if (!fileEl.files || !fileEl.files[0]) {
        showConfigError("Select an HTML file, or switch back to direct text.");
        return;
      }
      payload.html_file_b64 = await readFileAsBase64(fileEl.files[0]);
      delete payload.topic; // make sure server uses the file, not a stale textarea value
    } else if (mode === "textfile") {
      const fileEl = configForm.elements.namedItem("text_file");
      if (!fileEl.files || !fileEl.files[0]) {
        showConfigError("Select a Markdown / text file, or switch back to direct text.");
        return;
      }
      payload.text_file_b64 = await readFileAsBase64(fileEl.files[0]);
      delete payload.topic;
    }
    } catch (err) {
      showConfigError(`Failed to read file: ${err.message || err}`);
      return;
    }

    // Attach the user-uploaded image library (if any). Empty list is
    // omitted entirely so legacy paths on the server skip the new
    // branch (and agent_state.json round-trips without an empty key).
    try {
      const userImages = await readUserImageLibraryForSubmit();
      if (userImages.length) payload.user_images = userImages;
    } catch (err) {
      showConfigError(`Failed to read user images: ${err.message || err}`);
      return;
    }
  } // end of !selectedRunDirname branch

  startBtn.disabled = true;
  startBtn.textContent = selectedRunDirname ? "Resuming run..." : "Starting pipeline...";
  try {
    const resp = await fetch("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      const msg = data.detail || data.error || `Server returned ${resp.status}`;
      showConfigError(msg);
      startBtn.disabled = false;
      startBtn.textContent = selectedRunDirname ? "Resume run →" : "Start pipeline";
      return;
    }
    // Server emits pipeline_state=starting/running via WS — that's
    // what drives the actual screen switch. Keep the button disabled;
    // WS handler re-enables it if state goes back to idle.
    startBtn.textContent = "Waiting for pipeline...";
    // Fetch canvas dims now so the pipeline-screen shell + first
    // thumbnail render at the right aspect ratio. The server captures
    // canvas_*_emu synchronously in api_start, so /api/state already
    // reports them — no need to wait for the first snapshot. Errors
    // are non-fatal: CSS :root defaults (16:9) keep the UI working.
    try {
      const stateResp = await fetch("/api/state");
      const stateData = await stateResp.json();
      if (stateData) setCanvasDimsFromState(stateData);
    } catch (_err) {
      /* fall back to 16:9 defaults */
    }
  } catch (err) {
    showConfigError(`Network error: ${err.message || err}`);
    startBtn.disabled = false;
    startBtn.textContent = selectedRunDirname ? "Resume run →" : "Start pipeline";
  }
});

// --- Home button → POST /api/reset (return to config screen) ---
// Always available in pipeline screen — user can bail out from any
// state (running / awaiting approve / done / failed). Server cancels
// any in-flight pipeline task and transitions back to idle; the
// pipeline_state WS handler then switches the screen back.
homeBtn.addEventListener("click", async () => {
  try {
    await fetch("/api/reset", { method: "POST" });
  } catch (err) {
    console.error("reset failed:", err);
  }
});

// --- Screen switching driven by pipeline_state WS messages ---
function showScreen(state) {
  const isPipeline = (state !== "idle");
  document.body.classList.toggle("config-active", !isPipeline);
  document.body.classList.toggle("pipeline-active", isPipeline);
  if (state === "idle") {
    startBtn.disabled = false;
    startBtn.textContent = selectedRunDirname ? "Load run" : "Start pipeline";
    clearConfigError();
    // Reset the post-pipeline state so the next run starts fresh:
    // approve button back to enabled "Approve", Download PPTX button
    // hidden, pipelineDone cleared. Without this, pipelineDone stays
    // true and enableApprovalButtons would bail out on the first
    // stage_complete of the next run.
    resetPipelineUiState();
  }
}
function resetPipelineUiState() {
  // Clear all run-scoped UI state so the next run starts fresh.
  // Called when entering "idle" (Home button) AND when entering
  // "starting" (POST /api/start for a new run). Without the "starting"
  // reset, thumbnails / stage tabs / preview from the previous run
  // leak into the new run's UI until each stage_complete arrives.
  console.log("[reset] firing resetPipelineUiState; caller=", new Error().stack?.split("\n")[2]?.trim() || "unknown");
  // Iterate Object.keys(stageState|snapshots) rather than the STAGES
  // constant so pro extension stages (script / voiceover / motion_design
  // / render_video / ...) are cleared too. The hardcoded STAGES list
  // only covers builtin stages, leaving pro stages' "completed" state
  // + previous-run snapshots leaking into the new run's sidebar.
  for (const s of Object.keys(snapshots)) snapshots[s] = null;
  for (const s of Object.keys(stageState)) stageState[s] = "pending";
  activeStage = null;
  activeItemIdx = 0;
  pendingGateStage = null;
  pipelineDone = false;
  // Re-hide the Download PPTX button — the next run hasn't reached
  // `rendered` yet, so the affordance shouldn't be live. Will be
  // re-revealed by stage_complete when rendered completes again.
  downloadPptxBtn.hidden = true;
  downloadPptxBtn.dataset.converting = "false";
  downloadPptxBtn.disabled = false;
  downloadPptxBtn.textContent = "Download PPTX";
  if (doneBanner) {
    doneBanner.hidden = true;
    doneBanner.classList.remove("expanded");
    doneBanner.classList.add("collapsed");
    doneToggle?.setAttribute("aria-expanded", "false");
    if (doneBannerBody) doneBannerBody.innerHTML = "";
    if (doneFileCount) doneFileCount.textContent = "";
  }
  if (previewContent) {
    previewContent.innerHTML = `<p style="color: var(--text-tertiary);">No stage output yet.</p>`;
  }
  // Reset the approve button so its label/dataset match a fresh run.
  // dataset.converting may be stuck if a download was in flight when
  // the user navigated — clear it so a later click doesn't no-op.
  approveBtn.textContent = "Approve";
  approveBtn.disabled = true;
  approveBtn.dataset.converting = "false";
  // Logs are session-scoped — a new run gets a fresh drawer.
  clearLog();
  // Reset the auto-expand flag so the next run's first log_entry
  // expands the drawer again (matches user choice: "auto-expand on
  // first entry of a new run"). User-set collapsed state from the
  // previous run doesn't carry over because clearLog + renderAll
  // already blank the drawer visually.
  _autoExpandedThisRun = false;
  // Re-render visible DOM immediately. Without this, stage tabs /
  // thumbnails / preview keep showing the previous run's content
  // until the next stage_complete arrives — for an incomplete run
  // where the first stage's LLM takes 10-30s, that's a long stretch
  // of stale UI. renderAll is safe to call with no active stage and
  // empty snapshots — it renders placeholders.
  renderAll();
}
function handlePipelineState(state, error) {
  // "starting" is emitted exactly once per new run (POST /api/start),
  // so it's the right hook to wipe state from any previous run.
  console.log(`[ws] pipeline_state: ${state}${error ? " (" + error + ")" : ""}`);
  if (state === "starting") {
    resetPipelineUiState();
  }
  showScreen(state);
  if (state === "failed" && error) {
    setStatusBanner(`Pipeline failed: ${error}`, "error");
  } else if (state === "done") {
    setStatusBanner("Pipeline complete.", "done");
  } else if (state === "starting") {
    setStatusBanner("Starting pipeline...");
  } else if (state === "running") {
    setStatusBanner("Pipeline running. Awaiting first stage output...");
  } else if (state === "idle") {
    setStatusBanner("Ready.");
  }
}

// --- Page-load recovery (handles browser refresh) ---
// Two-step fetch: /api/status is the authoritative view selector
// (idle → config screen, anything else → pipeline screen). Only when
// non-idle do we also fetch /api/state to hydrate the pipeline view
// with the on-disk snapshots from the current or most recent run.
async function syncStatusOnLoad() {
  let status;
  try {
    const resp = await fetch("/api/status");
    status = await resp.json();
  } catch (err) {
    // Network failure: assume idle so the user can still type a config.
    showScreen("idle");
    return;
  }
  if (!status || !status.state) {
    showScreen("idle");
    return;
  }

  // pipeline_state is the single authoritative signal for which view
  // the user should land on. When idle, stay on the config screen no
  // matter what snapshots exist on disk — POST /api/reset clears
  // in-memory state but leaves agent_state.json behind, so the
  // previous run's snapshots are still served by /api/state. The
  // prior logic used snapshots.length>0 to decide the view and
  // explicitly overrode idle→"running", which forced a refresh after
  // Home into the stage view. Users wanting to view a prior run can
  // load it explicitly via the "Load previous run" control on config.
  if (status.state === "idle") {
    showScreen("idle");
    return;
  }

  // Non-idle: fetch state and hydrate the pipeline view.
  let data = null;
  try {
    const stateResp = await fetch("/api/state");
    data = await stateResp.json();
    // Apply canvas dims early so any subsequent render (thumbnails,
    // preview iframe, demo slide) uses the right aspect ratio. Even
    // when there are no snapshots yet, the dims are useful for the
    // pipeline-screen shell.
    if (data) setCanvasDimsFromState(data);
  } catch (err) {
    // Fall through — data stays null, status banner logic below.
  }

  if (data && data.snapshots && data.snapshots.length > 0) {
    showScreen(status.state);
    hydrateFromDisk(data);
    return;
  }

  // No snapshots to hydrate — fall back to state-driven screen +
  // banner. This branch covers: pipeline running but no stage has
  // completed (legitimate "resuming..." case), pipeline failed
  // before any snapshot, and pipeline done with no on-disk state
  // (rare race — server reports "done" but _resolve_active_run_dir
  // found nothing to hydrate).
  //
  // EVERY branch here is non-idle (idle returned early above) and
  // must set a banner. Otherwise the initial HTML placeholder
  // ("Waiting for pipeline to start...") leaks through and the user
  // sees a stale message after refresh.
  showScreen(status.state);
  if (status.state === "running" || status.state === "starting") {
    setStatusBanner("Pipeline 仍在后台运行 — resuming...");
  } else if (status.state === "failed") {
    setStatusBanner(
      status.error ? `Pipeline failed: ${status.error}` : "Pipeline failed.",
      "error",
    );
  } else if (status.state === "done") {
    setStatusBanner("Pipeline complete.", "done");
  } else {
    // Unknown non-idle state — set SOMETHING so the placeholder
    // doesn't leak.
    setStatusBanner("Ready.");
  }
  // Render the stage-tab skeleton (every stage pending) so the main
  // area isn't blank while the pipeline catches up. Without this, the
  // fallback branch only flips the screen + sets the banner, leaving
  // #stage-list / #preview-content completely empty until the next
  // WS stage_complete arrives — which on a slow first stage can be
  // 10-30s of "blank pipeline screen".
  if (data && Array.isArray(data.stage_order) && data.stage_order.length > 0) {
    setDeclaredStages(data.stage_order);
  }
  renderAll();
}

// Replay disk-loaded snapshots into the same maps that WS events
// populate. After this runs, the UI is in the same state as if the
// user had been connected for the whole pipeline — they can approve
// the latest pending gate, browse earlier thumbnails, or download
// the PPTX if the pipeline is done.
function hydrateFromDisk(data) {
  // Wrap the whole body in try/catch so any unexpected field shape
  // (e.g. a snapshot missing state_view, an extension stage name that
  // stageLabel can't resolve) cannot leave the user staring at the
  // initial HTML placeholder ("Loading..." after fix 3) and a blank
  // main area. The catch forces a fallback banner + renderAll so the
  // UI is always in a usable state, and logs the error so the actual
  // root cause is diagnosable.
  try {
  // Adopt the server-declared stage list FIRST so extension stages
  // (script / motion_design) get a tab even without an ext.js
  // renderer. Without this, renderStageTabs() below would only know
  // about the builtin STAGES fallback and pro tabs would be missing
  // on refresh. Idempotent: just sets declaredStages + backfills
  // stageState/snapshots; the loop below overwrites completed entries.
  if (Array.isArray(data.stage_order) && data.stage_order.length > 0) {
    setDeclaredStages(data.stage_order);
  }
  // Mark each completed stage and cache its snapshot. Mirrors what
  // the WS stage_complete handler does (app.js ~line 517).
  for (const entry of data.snapshots) {
    snapshots[entry.stage] = entry.snapshot;
    stageState[entry.stage] = "completed";
  }
  // Mark the next stage as running if the pipeline is actively
  // executing it. Two conditions must hold:
  //   1. pipeline state is running/starting (not idle/done/failed)
  //   2. gate is NOT paused — when paused, the orchestrator is blocked
  //      at active_stage's review gate; the next stage has not started.
  //      Without the gate_paused check, refreshing while paused at
  //      "images" would mark "slides" as running even though the
  //      pipeline is waiting for approve, not executing slides.
  // Also skipped for failed/done — failed leaves the next stage
  // untouched (its state is governed by the pipeline_state handler),
  // and done has no "next" stage.
  const isActive = (data.state === "running" || data.state === "starting");
  // Strict check: only skip "mark next stage running" when the server
  // definitively reports the gate is paused. Old servers without the
  // field return undefined → falls through to the original behaviour.
  const isGatePaused = (data.gate_paused === true);
  if (isActive && !data.pipeline_done && !isGatePaused && data.active_stage) {
    const allStages = getAllStages();
    const idx = allStages.indexOf(data.active_stage);
    if (idx >= 0 && idx + 1 < allStages.length) {
      stageState[allStages[idx + 1]] = "running";
    }
  }
  // Task 3: jump to the latest completed stage so the user doesn't
  // have to re-approve earlier stages they already reviewed. This
  // matches the auto-focus behavior of a live stage_complete event.
  if (data.active_stage) {
    activeStage = data.active_stage;
    activeItemIdx = 0;
    pendingGateStage = data.active_stage;
    // "Export" label was for when rendered was terminal — with pro
    // extensions appended after it, even rendered's approve advances
    // the pipeline. Approve is disabled (not relabeled) by
    // setPipelineDone() when the entire order is done; the dedicated
    // Download PPTX button is revealed when `rendered` lands.
    approveBtn.textContent = "Approve";
  }
  // PPTX/HTML artifacts are downloadable as soon as `rendered` is in
  // the snapshot cache, regardless of whether pro stages have run.
  // Reveal the banner AND the dedicated Download button so the user
  // can grab the file on refresh even mid-pro-pipeline.
  // setPipelineDone() (which disables approveBtn) only fires when
  // data.pipeline_done is true.
  if (snapshots["rendered"]) {
    const renderedPaths =
      (snapshots["rendered"].state_view &&
        snapshots["rendered"].state_view.html_paths) ||
      data.html_paths ||
      [];
    showPptxDownloadButton(renderedPaths);
    downloadPptxBtn.hidden = false;
  }
  if (data.pipeline_done) {
    setPipelineDone();
  } else if (data.gate_paused) {
    // Refreshed while orchestrator was actually paused at the gate.
    // Approve will release the gate and advance the pipeline — same
    // behavior as a live stage_complete event. Name the stage so the
    // user knows exactly where they are in the pipeline.
    const stageText = data.active_stage ? stageLabel(data.active_stage) : "stage";
    const allStages = getAllStages();
    const nextIdx = data.active_stage ? allStages.indexOf(data.active_stage) + 1 : -1;
    const nextText = (nextIdx > 0 && nextIdx < allStages.length)
      ? stageLabel(allStages[nextIdx])
      : null;
    setStatusBanner(
      nextText
        ? `"${stageText}" ready for review — Approve to start ${nextText}`
        : `"${stageText}" ready for review`
    );
  } else if (isActive) {
    // Refreshed while orchestrator is mid-stage execution (e.g.
    // slide_builder iterating). gate.release would be a silent no-op
    // (review_gate.py:139-143), so we keep Approve disabled until
    // the next WS stage_complete arrives and the real gate engages.
    setStatusBanner(
      `Pipeline still running in background — will pause after current stage`
    );
  } else {
    // state=idle/failed (e.g. server restarted after a completed run,
    // or pipeline failed with partial state on disk). We're viewing a
    // historical run — no live task, no gate to release. Surface the
    // state honestly so the user knows they're browsing, not running.
    if (data.state === "failed") {
      setStatusBanner(
        `Pipeline failed${data.error ? ": " + data.error : ""} — showing last saved state`,
        "error",
      );
    } else {
      setStatusBanner("Viewing previous run — Home to start a new pipeline.");
    }
  }
  renderAll();
  // Only enable Approve when the server confirms an active gate pause.
  // gate_paused missing = old server, default to enabled for back-compat.
  // pipelineDone short-circuits (Download button takes over approveBtn).
  const gatePaused = (data.gate_paused !== false);
  if (pipelineDone || gatePaused) {
    enableApprovalButtons();
  } else {
    // Mid-stage execution: gate is not engaged. Approve would be a
    // silent no-op (see review_gate.py:139-143), so disable it until
    // the next WS stage_complete arrives and the real gate engages.
    // User can still bail out via Home (which cancels via task.cancel
    // in /api/reset — works in any pipeline state).
    approveBtn.disabled = true;
  }
  // One summary line — WS stage_complete handlers didn't fire because
  // these snapshots came from REST. Log a single consolidated line so
  // the drawer still shows "what happened" without spamming per-stage.
  appendLog(
    "pipeline",
    `Loaded ${data.snapshots.length} completed stage(s) from disk` +
      (data.pipeline_done ? "" : " — pipeline still running"),
    "info",
  );
  } catch (err) {
    // Any unexpected snapshot shape / undefined field / DOM op throwing
    // would otherwise leave the user stuck on the placeholder banner +
    // blank main area. Force a usable banner + render so they can
    // still interact (Home, retry, etc.) and log the error for diagnosis.
    console.error("[hydrateFromDisk] failed:", err);
    setStatusBanner("Loaded stage data — refresh if anything looks off.");
    try { renderAll(); } catch (e) { console.error("[hydrateFromDisk] renderAll also failed:", e); }
  }
}

// --- Credential defaults + locks (CLI flags / .env) ---
// Server may be launched with --api-base / --api-key / etc. or
// SHUTTLESLIDE_* in a .env file. Those fields become readonly in
// the form. When all 3 required credentials are locked, the
// credentials section collapses to a "Using model X" note.
async function syncDefaultsOnLoad() {
  try {
    const resp = await fetch("/api/defaults");
    const data = await resp.json();
    if (!data) return;
    applyDefaultsAndLocks(data.defaults || {}, data.locked || []);
    // Mock mode (slidecraft review --mock): hide all credential inputs
    // and show a banner explaining that no real LLM will be called.
    // Credentials are irrelevant — the backend uses a stub orchestrator.
    if (data.mock_mode) {
      applyMockModeBanner();
    }
    // Canvas mode (slidecraft review --canvas): reveal the aspect-ratio
    // picker at the top of the Style fieldset. Without --canvas the
    // picker stays hidden AND its radios are disabled so the form
    // serializer skips them — `hidden` alone doesn't stop radios from
    // being submitted, and a stale localStorage value restored by
    // loadPersistedForm would otherwise leak canvas_aspect_ratio into
    // the next run and flip the deck off 16:9. Server-side guard lives
    // in _extract_config_kwargs (drops the field when !canvas_mode).
    if (data.canvas_mode) {
      applyCanvasModePicker();
    } else {
      disableCanvasRatioPicker();
    }
  } catch (err) {
    // Network failure or older server without the endpoint — leave
    // the form fully editable. Don't break the page.
    console.warn("could not load /api/defaults:", err);
  }
}

function applyMockModeBanner() {
  // Hide every credential input + label inside the required fieldset.
  // The start button + topic/style inputs stay (mock still needs a topic).
  const CREDENTIAL_FIELDS = [
    "api_base", "api_key", "model",
    "vlm_api_base", "vlm_api_key", "vlm_model",
  ];
  for (const field of CREDENTIAL_FIELDS) {
    const input = document.querySelector(`[name="${field}"]`);
    if (!input) continue;
    // Mock mode bypasses the LLM entirely — drop `required` so HTML5
    // validation doesn't block submit on fields the user can't see or
    // fill. Restore isn't needed: a page reload re-runs this branch.
    input.removeAttribute("required");
    const label = input.closest("label");
    if (label) label.style.display = "none";
  }
  // Insert a banner at the top of the form so the user knows what
  // they're testing. Replaces the "Credentials locked" note path —
  // mock_mode short-circuits before that branch runs.
  const requiredFieldset = document.querySelector("#config-form fieldset");
  if (!requiredFieldset) return;
  // Don't double-insert if the page is reloaded mid-session.
  if (requiredFieldset.querySelector(".mock-mode-note")) return;
  const note = document.createElement("div");
  note.className = "mock-mode-note credentials-locked-note";
  note.innerHTML =
    `<strong>Mock mode</strong> — synthetic events, no real LLM calls. ` +
    `Use this to test the UI fast without API credentials.`;
  requiredFieldset.insertBefore(note, requiredFieldset.firstChild);
}

function applyCanvasModePicker() {
  // Unhide the ratio picker (hidden by default in index.html; visible
  // only when the server reports canvas_mode === true via /api/defaults).
  const picker = document.getElementById("canvas-ratio-picker");
  if (!picker) return;
  picker.hidden = false;
  // Re-enable any radios disableCanvasRatioPicker turned off (covers a
  // user who reloads the page against a server restarted without --canvas
  // then with --canvas — radio state must follow the live canvas_mode).
  picker.querySelectorAll('input[type="radio"][name="canvas_aspect_ratio"]')
    .forEach(r => { r.disabled = false; });
  // "Custom" radio has value="custom" as a sentinel — when the user
  // types a real W:H into the custom text input, rewrite the radio's
  // value to match so the form serializer picks up the typed ratio.
  // Server validates format via aspect_ratio_to_dimensions.
  const customRadio = picker.querySelector(
    'input[type="radio"][value="custom"]'
  );
  const customText = picker.querySelector(
    'input[name="canvas_aspect_ratio_custom"]'
  );
  if (customRadio && customText) {
    const sync = () => {
      const v = (customText.value || "").trim();
      // Only accept W:H integer pattern. Anything else leaves the radio
      // value as "custom", which the server rejects with a clear error
      // — better than silently substituting a guess.
      if (/^[0-9]+:[0-9]+$/.test(v)) {
        customRadio.value = v;
      } else {
        customRadio.value = "custom";
      }
    };
    customText.addEventListener("input", sync);
    // Clicking the custom label should focus the text input so the user
    // can start typing without an extra click.
    customText.addEventListener("focus", () => {
      customRadio.checked = true;
      sync();
    });
    // Initialize on load — a persisted custom value from localStorage
    // (data-persist="local") needs to populate the radio's value too.
    sync();
  }
}

function disableCanvasRatioPicker() {
  // Counterpart to applyCanvasModePicker for the non-canvas-mode path.
  // `hidden` on the picker container does NOT stop the radios inside
  // from being serialized into the form payload (HTML only skips
  // disabled / nameless / parent-<disabled> elements). The default
  // 16:9 radio is `checked` in index.html, so without this disable a
  // stale localStorage value (data-persist="local") would submit
  // whatever the user last picked in a canvas-mode session and silently
  // change the deck's aspect ratio. Server-side backstop lives in
  // ReviewServer._extract_config_kwargs (drops canvas_aspect_ratio
  // when !canvas_mode); this front-end disable is the primary gate.
  const picker = document.getElementById("canvas-ratio-picker");
  if (!picker) return;
  picker.querySelectorAll('input[type="radio"][name="canvas_aspect_ratio"]')
    .forEach(r => { r.disabled = true; });
}

function applyDefaultsAndLocks(defaults, lockedFields) {
  const CREDENTIAL_FIELDS = [
    "api_base", "api_key", "model",
    "vlm_api_base", "vlm_api_key", "vlm_model",
  ];
  // Fields whose real value is never sent by the server (see
  // _SENSITIVE_FIELDS in server.py). UI shows a mask placeholder
  // — server re-injects the real value at POST /api/start.
  const SECRET_FIELDS = new Set(["api_key", "vlm_api_key"]);
  const MASK = "••••••••••••";

  // 1. Pre-fill + lock each credential field.
  for (const field of CREDENTIAL_FIELDS) {
    const input = document.querySelector(`[name="${field}"]`);
    if (!input) continue;

    if (lockedFields.includes(field)) {
      // Locked: never trust a returned secret value (the server
      // filters them, but defense-in-depth). Show mask for secrets,
      // real value for non-sensitive fields (api_base, model).
      if (SECRET_FIELDS.has(field)) {
        input.value = MASK;
        input.type = "password";
        input.autocomplete = "off";
      } else {
        input.value = defaults[field] || "";
      }
      // readOnly (not disabled) so the value still submits with the
      // form. Server enforces the lock independently, but the form
      // submit path keeps working without special cases.
      input.readOnly = true;
      input.classList.add("locked-field");
      input.dataset.locked = "true";
    } else if (defaults[field] != null) {
      input.value = defaults[field];
    }
  }

  // 2. If all 3 required credentials are locked, hide those inputs
  //    and show a "Credentials locked" note. Topic input stays.
  const REQUIRED = ["api_base", "api_key", "model"];
  const allRequiredLocked = REQUIRED.every(f => lockedFields.includes(f));
  if (allRequiredLocked) {
    const hiddenLabels = document.querySelectorAll(
      '[name="api_base"], [name="api_key"], [name="model"]'
    );
    hiddenLabels.forEach(el => {
      const label = el.closest("label");
      if (label) label.style.display = "none";
    });
    const note = document.createElement("div");
    note.className = "credentials-locked-note";
    note.innerHTML =
      `<strong>Credentials locked</strong> via CLI/.env. ` +
      `Using model: <code>${escapeHtml(defaults.model || "")}</code>`;
    // Insert as the first child of the Required fieldset so it
    // appears where the inputs used to be.
    const requiredFieldset = document.querySelector("#config-form fieldset");
    if (requiredFieldset) {
      requiredFieldset.insertBefore(note, requiredFieldset.firstChild);
    }
  }
}

syncStatusOnLoad();
syncDefaultsOnLoad();
fetchAndRenderRuns();

// Initial render — empty state.
renderStageTabs();

// ---------------------------------------------------------------------------
// Sidebar History panel (#4)
// ---------------------------------------------------------------------------
// The server pushes a fresh history_snapshot after every edit / undo /
// revert, so an already-connected client keeps its panel in sync
// automatically. We also request a snapshot:
//   - on initial WS handshake (covers a page refresh mid-session)
//   - the first time the user clicks the History tab (covers a panel
//     the user never opened before the first edit landed)
//
// Both paths are idempotent — the server always returns the full stack
// newest-first, and renderHistoryPanel replaces whatever is shown.

let historyTabActivated = false;

function requestHistorySnapshot() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  ws.send(JSON.stringify({
    type: "get_history",
    ref_id: newRefId(),
  }));
}

function renderHistoryPanel(entries) {
  const list = document.getElementById("history-list");
  if (!list) return;
  // Filter to the active stage. idx still points to server-side global
  // position so revert_to keeps working across all entries.
  const stageEntries = (Array.isArray(entries) ? entries : []).filter(
    e => !activeStage || e.stage === activeStage
  );
  const prevCount = Number(list.dataset.count || "0");
  const newCount = stageEntries.length;
  list.dataset.loaded = "1";
  list.dataset.count = String(newCount);
  if (!newCount) {
    const stageName = activeStage || "this";
    list.innerHTML = `<p class="placeholder">No edits in ${escapeHtml(stageName)} stage yet.</p>`;
  } else {
    list.innerHTML = stageEntries.map(e => {
      const t = e.timestamp ? formatHistoryTime(e.timestamp) : "";
      const label = escapeHtml(e.action_label || "edit");
      const summary = escapeHtml(e.new_value_summary || "");
      const idx = Number(e.idx);
      // Pending-revert cards (Restore clicked, awaiting Undo/Commit)
      // swap the single Restore button for an Undo + Commit pair.
      const isPending = pendingRevertIds.has(idx);
      const pendingCls = isPending ? " pending" : "";
      const action = isPending
        ? `<div class="history-pending-actions">
             <button class="history-unrevert-btn" data-entry-idx="${idx}" title="Re-apply this edit's value">Undo</button>
             <button class="history-commit-btn" data-entry-idx="${idx}" title="Permanently remove this edit and card">Commit</button>
           </div>`
        : `<button class="history-restore-btn" data-entry-idx="${idx}" title="Apply this edit's previous value (other edits stay)">Restore</button>`;
      return `<div class="history-entry${pendingCls}" data-entry-idx="${idx}">
        <div class="history-row">
          <span class="history-action">${label}</span>
          <span class="history-time">${t}</span>
        </div>
        ${summary ? `<div class="history-summary">${summary}</div>` : ""}
        ${action}
      </div>`;
    }).join("");
  }
  // 如果 History tab 不是当前激活的，且有新增条目，给 tab 加 pulse 提示。
  const historyTab = document.querySelector('.sidebar-tab[data-sidebar-tab="history"]');
  if (historyTab && newCount > prevCount) {
    const isActive = historyTab.classList.contains("active");
    if (!isActive) {
      historyTab.classList.add("has-new");
    }
  }
}

function formatHistoryTime(ts) {
  // ts is epoch seconds (from time.time() on the server). Show HH:MM:SS
  // for today's edits, fallback to ISO date for older.
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) {
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }
  return d.toLocaleDateString([], { month: "short", day: "numeric" }) +
         " " +
         d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

// History card click — delegated so re-renders don't rebind.
// Three action buttons, all gated on data-entry-idx so clicks on the
// entry whitespace don't accidentally fire (the outer .history-entry
// also carries data-entry-idx for future use).
document.getElementById("history-list")?.addEventListener("click", (e) => {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const restoreBtn = e.target.closest(".history-restore-btn");
  if (restoreBtn) {
    const idx = Number(restoreBtn.dataset.entryIdx);
    if (Number.isNaN(idx)) return;
    pendingRevertIds.add(idx);
    ws.send(JSON.stringify({
      type: "revert_to",
      ref_id: newRefId(),
      entry_idx: idx,
    }));
    // Optimistic re-render: don't wait for server round-trip to swap
    // the button to Undo/Commit. renderHistoryPanel reads the same
    // lastHistoryEntries cache and applies the pending flag locally.
    renderHistoryPanel(lastHistoryEntries);
    return;
  }
  const unrevertBtn = e.target.closest(".history-unrevert-btn");
  if (unrevertBtn) {
    const idx = Number(unrevertBtn.dataset.entryIdx);
    if (Number.isNaN(idx)) return;
    pendingRevertIds.delete(idx);
    ws.send(JSON.stringify({
      type: "unrevert",
      ref_id: newRefId(),
      entry_idx: idx,
    }));
    renderHistoryPanel(lastHistoryEntries);
    return;
  }
  const commitBtn = e.target.closest(".history-commit-btn");
  if (commitBtn) {
    const idx = Number(commitBtn.dataset.entryIdx);
    if (Number.isNaN(idx)) return;
    pendingRevertIds.delete(idx);
    ws.send(JSON.stringify({
      type: "delete_history_entry",
      ref_id: newRefId(),
      entry_idx: idx,
    }));
    // Server will broadcast fresh history_snapshot; no optimistic
    // re-render needed (the card disappears from the list).
    return;
  }
});

// Sidebar tab switching (Slides | History).
document.querySelectorAll(".sidebar-tab").forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.sidebarTab;
    document.querySelectorAll(".sidebar-tab").forEach(b => {
      const isActive = b === btn;
      b.classList.toggle("active", isActive);
      b.setAttribute("aria-selected", isActive ? "true" : "false");
      // 切到某 tab 时清除该 tab 的「有新内容」提示
      if (isActive) b.classList.remove("has-new");
    });
    document.querySelectorAll("[data-sidebar-body]").forEach(body => {
      body.hidden = body.dataset.sidebarBody !== tab;
    });
    // Lazy-load history on first open — covers the case where edits
    // happened before the user ever clicked the History tab.
    if (tab === "history" && !historyTabActivated) {
      historyTabActivated = true;
      requestHistorySnapshot();
    }
  });
});

// On WS open, also pull a fresh snapshot (covers refresh-mid-session).
// We hook into the existing ws.onopen by appending a listener via
// addEventListener — app.js sets ws.onopen elsewhere; this is additive.
// (Works whether onopen is set before or after this script block runs
// because we're appending at the end of file parse.)
const _origOnOpen = ws && ws.onopen;
if (ws) {
  ws.addEventListener("open", () => {
    // Give the server a tick to settle the connection.
    requestHistorySnapshot();
  });
}

// ============ Preview fullscreen toggle ============
// Covers sidebar + chat when active; Esc or click button again to exit.
// position:fixed is enough — no need to hide other columns.
(function attachFullscreenToggle() {
  const btn = document.getElementById("preview-fullscreen-btn");
  const previewSection = document.getElementById("preview");
  if (!btn || !previewSection) return;
  function toggleFullscreen() {
    previewSection.classList.toggle("fullscreen");
    requestAnimationFrame(() => scaleBigSlide());
  }
  btn.addEventListener("click", toggleFullscreen);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && previewSection.classList.contains("fullscreen")) {
      previewSection.classList.remove("fullscreen");
      requestAnimationFrame(() => scaleBigSlide());
    }
  });
})();

// ============ Layers panel toggle ============
// Floating overlay in #preview top-right; lists slide elements so users
// can click-pick ones that are visually obscured. See buildLayers and
// friends above for the data flow.
(function attachLayersToggle() {
  const btn = document.getElementById("preview-layers-btn");
  const panel = document.getElementById("layers-panel");
  const closeBtn = document.getElementById("layers-close");
  if (!btn || !panel) return;
  function isOpen() { return !panel.hidden; }
  function openPanel() {
    panel.hidden = false;
    btn.setAttribute("aria-pressed", "true");
    renderLayersPanel(currentLayers);
  }
  function closePanel() {
    panel.hidden = true;
    btn.setAttribute("aria-pressed", "false");
    // Clear the persistent outline so it doesn't linger after the user
    // is done inspecting layers.
    clearSelectedLayerEl();
  }
  function togglePanel() { isOpen() ? closePanel() : openPanel(); }
  btn.addEventListener("click", togglePanel);
  if (closeBtn) closeBtn.addEventListener("click", closePanel);
  // Esc closes the panel (only when open and not in a text field).
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !isOpen()) return;
    const t = e.target;
    if (t && typeof t.matches === "function" &&
        t.matches("input, textarea, [contenteditable]")) return;
    e.preventDefault();
    closePanel();
  });
})();

// ============ Add-image toolbar button ============
// Mirror of drag-drop: clicking opens the OS file picker; picking a
// PNG/JPEG uploads it and inserts it at the top-left of the current
// slide. Reuses the same _handleDroppedFile pipeline as drag-drop, so
// commit / placeholder / error handling is identical. The synthetic
// drop position is slideW*0.1 / slideH*0.1 so the new img doesn't
// land centered on top of existing content.
(function attachAddImageButton() {
  const btn = document.getElementById("preview-add-image-btn");
  const input = document.getElementById("preview-add-image-input");
  if (!btn || !input) return;

  btn.addEventListener("click", () => {
    if (activeStage !== "slides") {
      flashToast("Add image is only available in the slides stage");
      return;
    }
    // Reset value so change fires even if the user re-picks the same file.
    input.value = "";
    input.click();
  });

  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) return;
    const iframe = previewContent.querySelector("iframe.big-slide");
    if (!iframe) { flashToast("No slide preview to attach to"); return; }
    const doc = iframe.contentDocument;
    const slide = doc && doc.querySelector(".ppt-slide");
    if (!slide) { flashToast("Slide not loaded yet"); return; }
    // Drop coordinates are in slide canvas space (the same space the
    // placeholder left/top live in). 10% offset keeps the new img clear
    // of the very top-left corner where titles usually sit.
    const x = slide.clientWidth * 0.1;
    const y = slide.clientHeight * 0.1;
    _handleDroppedFile(file, iframe, activeItemIdx, x, y);
  });
})();
