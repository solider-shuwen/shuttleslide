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
  return STAGE_LABELS[stage] || stage;
}

// Per-stage state for the top bar.
// Values: "pending" | "running" | "completed" | "cancelled"
const stageState = Object.fromEntries(STAGES.map(s => [s, "pending"]));

// Snapshot cache — one entry per completed stage. Lets the user click
// back through stage history in the top bar without re-fetching.
const snapshots = Object.fromEntries(STAGES.map(s => [s, null]));

let activeStage = null;        // which stage the UI is currently showing
let activeItemIdx = 0;         // which thumbnail is selected (0-based)
let pendingGateStage = null;   // which stage the gate is paused on —
                               // Approve/Cancel always target THIS,
                               // not activeStage, so the user can
                               // safely browse history without
                               // accidentally approving the wrong stage.
let pipelineDone = false;

const ws = new WebSocket(`ws://${location.host}/ws`);
const approveBtn = document.getElementById("approve-btn");
// The status banner's DOM was replaced by #progress-strip in PR-X. The
// var is kept as a reference to the new strip so existing setStatusBanner
// callers don't have to change — they route through updateProgressStrip()
// internally. Direct textContent on this var is gone (the strip has
// nested children now); use updateProgressStrip / setStatusBanner instead.
const statusBanner = document.getElementById("progress-strip");
const previewContent = document.getElementById("preview-content");
const doneBanner = document.getElementById("done-banner");
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
}

// Scale the .big-slide-wrapper iframe (fixed 1280x720) to fit the
// wrapper's actual rendered width. Called after each slides-stage
// preview render and on window resize. Without this the iframe
// viewport would be smaller than the slide canvas and content would
// clip / scroll inside the iframe.
function scaleBigSlide() {
  const wrapper = document.querySelector('.big-slide-wrapper');
  if (!wrapper) return;
  const iframe = wrapper.querySelector('iframe');
  if (!iframe) return;
  const scale = wrapper.clientWidth / 1280;
  iframe.style.transform = `scale(${scale})`;
}
window.addEventListener('resize', scaleBigSlide);

// =====================================================================
// Top stage bar — horizontal tabs, clickable when completed
// =====================================================================
function renderStageTabs() {
  stageList.innerHTML = "";
  for (const stage of STAGES) {
    const state = stageState[stage];
    const tab = document.createElement("div");
    const isActive = (stage === activeStage);
    tab.className = `stage-tab ${state}${isActive ? " active" : ""}`;
    tab.dataset.stage = stage;
    tab.innerHTML = `<span class="icon"></span><span>${stageLabel(stage)}</span>`;
    if (state === "completed") {
      tab.addEventListener("click", () => {
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
      if (payload.type === "svg" || payload.type === "svg_file") {
        // svg_file is the production shape (svg_tools.py:330);
        // svg is grandfathered. Both carry inline ``data``.
        body = `<div class="thumb-svg">${payload.data || ""}</div>`;
      } else if (payload.type === "image_file" || payload.type === "image") {
        const src = `/artifact/images/${slideIdx}/${encodeURIComponent(slotId)}`;
        body = `<img class="thumb-image" src="${src}" />`;
      } else {
        body = `<div class="thumb-svg" style="color:var(--text-tertiary);font-size:11px;">unknown type</div>`;
      }
      // slideIdx is a 0-indexed snapshot key; display label is 1-indexed
      // to match saved file names (1.html, 2.html, ...). URL stays 0-indexed.
      return {
        html: `<div class="thumb-label">Slide ${Number(slideIdx) + 1} / ${escapeHtml(slotId)}</div>${body}`,
      };
    });
  }
  if (stage === "slides") {
    const slides = view.slides || [];
    return slides.map((s, i) => ({
      html: `<div class="thumb-label">Slide ${i + 1}</div>
             <div class="thumb-slide">
               <iframe src="/artifact/slides/${i}" scrolling="no"></iframe>
             </div>`,
    }));
  }
  if (stage === "rendered") {
    // export stage: files are already on disk (orchestrator._finalize
    // runs before _post_stage_hook). Show one entry per file with an
    // "Open" link — don't re-render slide thumbnails, those belong to
    // the slides stage.
    const paths = view.html_paths || [];
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
    });
    thumbList.appendChild(div);
  });
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

  if (activeStage === "theme") {
    // Theme preview is a single rich view (color swatches + demo slide).
    renderThemePreview(snap.state_view.theme || {});
    return;
  }
  if (activeStage === "outline") {
    const outline = snap.state_view.outline || [];
    const item = outline[idx];
    if (!item) { setPreview(`<p style="color:var(--text-tertiary);">No such item.</p>`); return; }
    const json = JSON.stringify(item, null, 2);
    setPreview(`<h4>Slide ${idx + 1} outline</h4>
                <pre><code>${escapeHtml(json)}</code></pre>`);
    return;
  }
  if (activeStage === "images") {
    const flat = flattenImages(snap);
    const item = flat[idx];
    if (!item) { setPreview(`<p style="color:var(--text-tertiary);">No such image.</p>`); return; }
    let body = "";
    if (item.payload.type === "svg" || item.payload.type === "svg_file") {
      body = `<div style="text-align:center;background:white;padding:12px;border-radius:4px;">${item.payload.data || ""}</div>`;
    } else {
      const src = `/artifact/images/${item.slideIdx}/${encodeURIComponent(item.slotId)}`;
      body = `<img src="${src}" style="max-width:100%;border:1px solid #eee;border-radius:4px;" />`;
    }
    setPreview(`<h4>Slide ${Number(item.slideIdx) + 1} / ${escapeHtml(item.slotId)}</h4>${body}`);
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
    setPreview(`<h4>Slide ${idx + 1}</h4>
                <div class="big-slide-wrapper">
                  <iframe class="big-slide" src="/artifact/slides/${idx}"></iframe>
                </div>`);
    // Scale the inner 1280x720 iframe to fit the wrapper's actual
    // width. rAF waits one frame so the wrapper has been laid out
    // and clientWidth is non-zero.
    requestAnimationFrame(scaleBigSlide);
    return;
  }
  if (activeStage === "rendered") {
    // export stage preview: show the exported file with an "Open in
    // new tab" link. _finalize has already written the file by the
    // time this snapshot arrives.
    const paths = snap.state_view.html_paths || [];
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
}

// =====================================================================
// renderThemePreview — color swatches + demo slide (theme middle view)
// =====================================================================
function renderThemePreview(theme) {
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
    // Invalid CSS colors are dropped by the browser, so the fallback
    // background shows through — no try/catch needed.
    const safeHex = /^#[0-9a-fA-F]{3,8}$/.test(v) ? v : "#cccccc";
    const display = v ? escapeHtml(v) : "<span style='color:var(--text-tertiary);'>(unset)</span>";
    return `<div style="display:inline-flex;flex-direction:column;align-items:center;margin:0 8px 8px 0;width:72px;">
      <div style="width:56px;height:56px;border-radius:50%;border:1px solid #ddd;background:${safeHex};box-shadow:inset 0 0 0 1px rgba(0,0,0,0.05);"></div>
      <div style="font-size:11px;color:var(--text-secondary);margin-top:4px;">${escapeHtml(label)}</div>
      <div style="font-size:11px;color:var(--text-tertiary);font-family:monospace;">${display}</div>
    </div>`;
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
    aspect-ratio:16/9;
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

  setPreview(`
    <h4 style="margin:0 0 10px 0;font-size:13px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;">Color palette</h4>
    <div style="margin-bottom:8px;">${swatches}</div>

    <h4 style="margin:16px 0 10px 0;font-size:13px;color:var(--text-secondary);text-transform:uppercase;letter-spacing:0.5px;">Slide preview</h4>
    ${demoSlide}

    ${jsonBlock}
  `);
}

// =====================================================================
// Pipeline done + helpers
// =====================================================================
function renderPipelineDone(htmlPaths) {
  pipelineDone = true;
  doneBanner.style.display = "block";
  if (!htmlPaths || htmlPaths.length === 0) {
    doneBanner.innerHTML = "<strong>Pipeline complete.</strong> No HTML files were written.";
  } else {
    // Each file is served by the /files/ StaticFiles mount and is
    // a standalone HTML document, so target=_blank opens it
    // directly in a new tab — no preview wrapper needed.
    const items = htmlPaths.map((p, i) => {
      const filename = p.split(/[\\/]/).pop() || p;
      const href = fileUrl(p);
      return `<li>
                 <a href="${href}" target="_blank" rel="noopener"
                    style="color:var(--success);font-weight:500;">Slide ${i + 1}: ${escapeHtml(filename)}</a>
                 <span style="color:var(--text-secondary);font-size:11px;margin-left:6px;">${escapeHtml(p)}</span>
               </li>`;
    }).join("");
    doneBanner.innerHTML = `<strong>Pipeline complete.</strong> Click a file to open in a new tab:<ul style="margin-top:6px;line-height:1.8;">${items}</ul>`;
  }
  setStatusBanner("Pipeline complete.", "done");
  // Keep approve button enabled — repurpose it as a PPTX download
  // trigger. Cancel has no action post-done (no stage to cancel).
  // The approveBtn.onclick handler routes to downloadPptx() when
  // pipelineDone is true; see that function for the download path.
  approveBtn.disabled = false;
  approveBtn.textContent = "Download PPTX";
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
  setStatusBanner("Connected. Waiting for stage output...");
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
    case "stage_complete": {
      const snap = msg.snapshot;
      console.log(`[ws] stage_complete: stage=${snap.stage} items=${snap.state_view?.outline?.length ?? snap.state_view?.slides?.length ?? "?"}`);
      // Cache + mark completed + advance running indicator.
      snapshots[snap.stage] = snap;
      stageState[snap.stage] = "completed";
      const idx = STAGES.indexOf(snap.stage);
      if (idx >= 0 && idx + 1 < STAGES.length) {
        const next = STAGES[idx + 1];
        if (stageState[next] === "pending") {
          stageState[next] = "running";
        }
      }
      // Auto-switch focus to the freshly-completed stage and arm
      // Approve/Cancel to target it. Even if the user later clicks
      // another completed tab (browsing history), pendingGateStage
      // stays pointing here until the next stage_complete arrives.
      activeStage = snap.stage;
      activeItemIdx = 0;
      pendingGateStage = snap.stage;
      // "rendered" is the export step — relabel Approve → Export so
      // the user understands the button writes files to disk, not
      // just advances the pipeline.
      approveBtn.textContent = (snap.stage === "rendered") ? "Export" : "Approve";
      renderAll();
      setStatusBanner(`Stage "${stageLabel(snap.stage)}" ready for review.`);
      enableApprovalButtons();
      // Item count heuristic — slide_count on slide-ish stages,
      // items.length on outline/theme, fall back to no count.
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
      break;
    }
    case "pipeline_done": {
      const paths = msg.html_paths || [];
      // renderPipelineDone repurposes the approve button as a PPTX
      // download trigger (enabled, relabeled). Don't disable it.
      renderPipelineDone(paths);
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
        for (const s of STAGES) {
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
    case "edit_applied":
    case "edit_rejected":
      console.log(`[PR3 message ignored] ${msg.type}`, msg);
      break;
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
  // After pipeline_done the button relabels to "Download PPTX"
  // (see renderPipelineDone). Route to the download path instead
  // of the (no-op) approve path.
  if (pipelineDone) {
    downloadPptx();
    return;
  }
  if (!pendingGateStage) return;
  ws.send(JSON.stringify({ type: "approve_stage", stage: pendingGateStage }));
  disableApprovalButtons();
  const verb = (pendingGateStage === "rendered") ? "Exported" : "Approved";
  setStatusBanner(`${verb} "${stageLabel(pendingGateStage)}". Continuing...`);
};

async function downloadPptx() {
  // Re-entry guard — if a render is already in flight, ignore.
  // The button is also disabled (see finally block), but belt+suspenders
  // against double-clicks before the DOM updates.
  if (approveBtn.dataset.converting === "true") return;
  approveBtn.dataset.converting = "true";
  approveBtn.disabled = true;
  approveBtn.textContent = "Rendering...";
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
    approveBtn.dataset.converting = "false";
    approveBtn.disabled = false;
    approveBtn.textContent = "Download PPTX";
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

// --- Persistence ---
// data-persist="local"  → localStorage (non-sensitive, survives close)
// data-persist="session" → sessionStorage (api keys, cleared on tab close)
function _persistStore(input) {
  return input.dataset.persist === "session" ? sessionStorage : localStorage;
}
function _persistKey(input) {
  return `shuttleslide.config.${input.name}`;
}
function loadPersistedForm() {
  configForm.querySelectorAll("[data-persist]").forEach(input => {
    const v = _persistStore(input).getItem(_persistKey(input));
    if (v === null) return;
    if (input.type === "checkbox") input.checked = (v === "1");
    else input.value = v;
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
    if (seen.has(el.name)) continue; // first occurrence wins
    payload[el.name] = el.value;
    seen.add(el.name);
  }

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
    // Reset the post-pipeline "Download PPTX" state so the next run
    // starts with the approve button in its default "Approve" mode.
    // Without this, pipelineDone stays true and enableApprovalButtons
    // would bail out on the first stage_complete of the next run.
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
  for (const s of STAGES) {
    snapshots[s] = null;
    stageState[s] = "pending";
  }
  activeStage = null;
  activeItemIdx = 0;
  pendingGateStage = null;
  pipelineDone = false;
  if (doneBanner) {
    doneBanner.style.display = "none";
    doneBanner.innerHTML = "";
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

// --- Page-load recovery (handles browser refresh mid-run) ---
// Two-step fetch: /api/status decides which screen to show, then
// /api/state hydrates snapshot state from disk so a reconnecting
// user sees the full history immediately (no waiting for WS events
// that may never arrive if the pipeline is paused at a gate, and
// no empty placeholders). Falls back gracefully on any error.
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
  showScreen(status.state);

  // Hydrate snapshot state from disk. _early_messages is cleared on
  // the server when the last client disconnects, so WS replay alone
  // is not reliable for reconnect — /api/state rebuilds from
  // agent_state.json (saved per-stage by the orchestrator).
  if (status.state !== "idle" && status.run_dir) {
    try {
      const stateResp = await fetch("/api/state");
      const data = await stateResp.json();
      if (data && data.snapshots && data.snapshots.length > 0) {
        hydrateFromDisk(data);
        return;
      }
    } catch (err) {
      // Fall through — WS will deliver what it can.
    }
  }

  // No snapshots hydrated — show a Task 1 banner if pipeline is
  // still running. Failed/done states are handled by showScreen
  // and the WS pipeline_state handler.
  if (status.state === "running" || status.state === "starting") {
    setStatusBanner("Pipeline 仍在后台运行 — resuming...");
  } else if (status.state === "failed" && status.error) {
    setStatusBanner(`Pipeline failed: ${status.error}`, "error");
  }
}

// Replay disk-loaded snapshots into the same maps that WS events
// populate. After this runs, the UI is in the same state as if the
// user had been connected for the whole pipeline — they can approve
// the latest pending gate, browse earlier thumbnails, or download
// the PPTX if the pipeline is done.
function hydrateFromDisk(data) {
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
    const idx = STAGES.indexOf(data.active_stage);
    if (idx >= 0 && idx + 1 < STAGES.length) {
      stageState[STAGES[idx + 1]] = "running";
    }
  }
  // Task 3: jump to the latest completed stage so the user doesn't
  // have to re-approve earlier stages they already reviewed. This
  // matches the auto-focus behavior of a live stage_complete event.
  if (data.active_stage) {
    activeStage = data.active_stage;
    activeItemIdx = 0;
    pendingGateStage = data.active_stage;
    approveBtn.textContent =
      (data.active_stage === "rendered") ? "Export" : "Approve";
  }
  if (data.pipeline_done) {
    pipelineDone = true;
    renderPipelineDone(data.html_paths || []);
  } else if (data.gate_paused) {
    // Refreshed while orchestrator was actually paused at the gate.
    // Approve will release the gate and advance the pipeline — same
    // behavior as a live stage_complete event. Name the stage so the
    // user knows exactly where they are in the pipeline.
    const stageText = data.active_stage ? stageLabel(data.active_stage) : "stage";
    const nextIdx = data.active_stage ? STAGES.indexOf(data.active_stage) + 1 : -1;
    const nextText = (nextIdx > 0 && nextIdx < STAGES.length)
      ? stageLabel(STAGES[nextIdx])
      : null;
    setStatusBanner(
      nextText
        ? `"${stageText}" ready for review — Approve to start ${nextText}`
        : `"${stageText}" ready for review`
    );
  } else {
    // Refreshed while orchestrator is mid-stage execution (e.g.
    // slide_builder iterating). gate.release would be a silent no-op
    // (review_gate.py:139-143), so we keep Approve disabled until
    // the next WS stage_complete arrives and the real gate engages.
    setStatusBanner(
      `Pipeline still running in background — will pause after current stage`
    );
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
