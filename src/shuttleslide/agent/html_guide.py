"""HTML authoring guide injected into the slide-builder prompt.

Teaches the LLM how to write HTML that `html_to_pptx` will correctly
classify and convert to PPTX shapes. The guide is driven by the actual
classifier heuristics in `src/shuttleslide/html_to_pptx/rule/classifier.py`
and the multi-element pattern detectors in `rule/containment.py` — the
LLM does not need to know the rules exist, but the patterns it must
produce map 1:1 to them.

Keep this guide in sync with the classifier. When the rule chain changes,
update the "Recognized element patterns" section here.

The FORBIDDEN CSS / FORBIDDEN TAILWIND sections at the end are
auto-generated from `agent/html_contract.py` so the prompt never drifts
from the linter that enforces the same rules. Edit the contract, not
this file, to change those lists.
"""

from __future__ import annotations

from shuttleslide.agent.html_contract import (
    render_forbidden_css_markdown,
    render_forbidden_tailwind_markdown,
)


_HTML_AUTHORING_GUIDE_BASE = """\
HTML AUTHORING GUIDE (read carefully — the PPTX converter needs these patterns)

You write the INNER HTML of one slide. The system wraps it in a fixed
1280x720 px `.ppt-slide` container with overflow hidden. Your HTML is
rendered by a real browser (Tailwind classes resolve, inline styles apply),
then each rendered element is classified into a PPTX shape type. Write
HTML that produces recognizable, well-bounded elements.

== CONTAINER & LAYOUT ==

Root: start with a full-size wrapper. The default includes
`justify-between` so content distributes across the full 720px height
instead of collapsing to the top (see VERTICAL DISTRIBUTION below):
    <div class="w-full h-full flex flex-col justify-between p-12"> ... </div>

Layout primitives (Tailwind utility classes work — the browser resolves
them before extraction):
  - flex / flex-col / flex-row / items-center / justify-between
  - grid grid-cols-2 / grid-cols-3 / gap-6 / gap-8
  - spacing: p-8 p-12 p-16, mt-8 mb-4, mx-auto
  - sizing: w-full h-full w-1/2 w-1/3

Avoid: position:absolute (use flex/grid instead), position:relative with
large top/left offsets, transforms beyond simple rotations.

== VERTICAL DISTRIBUTION (avoid top-heavy and overflow) ==

The slide canvas is 1280x720 with overflow:hidden. A bare `flex flex-col`
lets content collapse to the top, leaving the bottom half empty — OR,
when content is dense (4+ cards), overflow past 720px and get clipped.
The root wrapper MUST therefore declare a vertical distribution strategy.

Pattern A — content spans top→bottom (DEFAULT for content slides):
  <div class="w-full h-full flex flex-col justify-between p-12">
    <header>...title...</header>
    <main>...body...</main>
    <footer>...tags/notes...</footer>
  </div>
  Use when: 3+ major blocks that should distribute across the canvas.

Pattern B — centered cluster (DEFAULT for hero / cover / divider):
  <div class="w-full h-full flex flex-col items-center justify-center p-12">
    <h1>...</h1><p>...</p>
  </div>
  Use when: one focal block (title + subtitle, or one icon + caption).

Pattern C — grid (preferred for 4+ parallel cards):
  <div class="w-full h-full grid grid-cols-2 gap-6 p-12">
    <div class="card">...</div><div class="card">...</div>
    <div class="card">...</div><div class="card">...</div>
  </div>
  Use when: 4-6 equal-weight cards. The grid distributes rows
  automatically without manual justify-*.

Rules:
- NEVER write bare `flex flex-col` without a justify-* — the bottom
  half will be empty.
- Use `gap-4` or `gap-6` between siblings inside flex-col / grid.
  NEVER rely on `margin-bottom` chains — gaps scale better.
- If content still overflows 720px after applying justify-between, you
  have too much content. Either split into two slides, switch to a grid
  (Pattern C), or trim bullet counts to 3-5.

== TYPOGRAPHY ==

Use semantic tags with PX font sizes:
  - <h1 style="font-size: 56px; color: #133EFF;">Big Title</h1>
  - <h2 style="font-size: 36px; ...">Section Heading</h2>
  - <h3 style="font-size: 24px; ...">Sub Heading</h3>
  - <p style="font-size: 18px; ...">Body text</p>

REQUIRED: every font-size must be a PX value. FORBIDDEN: rem, em, %, and
Tailwind text-size classes (text-xs text-sm text-lg text-xl text-2xl).
The px->pt converter only reads inline px values.

== COLORS ==

Pull literal hex values from the THEME block (above). Use them in inline
styles:
  - style="color: #133EFF;"
  - style="background-color: #00CD82;"
  - style="background: linear-gradient(135deg, #133EFF 0%, #00CD82 100%);"
  - style="border: 2px solid #133EFF;"

NEVER invent colors that aren't in the theme. NEVER mix color systems.

== ICONS ==

Material Icons via:
  <i class="material-icons" style="font-size: 32px; color: #00CD82;">visibility</i>
  <span style="margin-left: 8px; font-size: 20px;">Label</span>

Common names: visibility, code, cloud, psychology, image, auto_awesome,
speed, check_circle, close, arrow_forward, warning, lightbulb, school,
insights, hub, security, group, business, science, memory, developer_board.

ICON + GLOW — Material Icons may carry an outer glow via filter: drop-shadow.
This is the ONE place drop-shadow is allowed (it is rendered as a PPTX
outerShdw on the icon shape). Use it for emphasis on hero icons:
  <i class="material-icons" style="font-size: 48px; color: #00FF88;
      filter: drop-shadow(0 0 20px rgba(0, 255, 136, 0.6));">auto_awesome</i>
Format: drop-shadow(<offsetX>px <offsetY>px <blur>px <color>). The color's
alpha controls glow strength (0.4-0.8 typical). For a uniform halo use
offset 0,0. Do NOT apply drop-shadow to non-icon elements — use box-shadow
there instead.

== RECOGNIZED ELEMENT PATTERNS ==

The classifier walks every rendered element and assigns ONE type. Write
your HTML so the elements you care about are recognized as one of these:

CARD — a bordered container with content inside. Use `class="card"` OR
build it from scratch:
  <div class="card" style="background-color: #F5F7FF; border-radius: 12px; padding: 24px;">
    <h3 style="font-size: 24px; color: #133EFF;">Card Title</h3>
    <p style="font-size: 16px;">Body text inside the card.</p>
  </div>
Rules the classifier applies: width > 25% of slide, height > 5%, has a
background-color (or gradient), has border-radius > 0, and at least one
child with text. Children render as separate elements on top of the card.

BADGE — small pill/tag. Use `class="badge"` OR build small:
  <span class="badge" style="background-color: #00CD82; color: white;
        border-radius: 9999px; padding: 4px 12px; font-size: 14px;">New</span>
Rules: width < 25%, height < 18%, has text, has bg-color, has border-radius.
Do NOT use this for the number circle in a numbered step (that's detected
separately below).

TITLE BAR — full-width top header strip with a background:
  <div style="background: linear-gradient(90deg, #133EFF 0%, #00CD82 100%);
              color: white; padding: 16px 32px; width: 100%;">
    <h1 style="font-size: 40px;">Deck Title</h1>
  </div>
Rules: top 15% of slide, width > 90%, height < 20%, has bg or gradient,
has text. Put the slide title here on content slides.

DIVIDER LINE — thin horizontal separator:
  <div style="background-color: #133EFF; height: 3px; width: 60%;
              margin: 16px 0;"></div>
Rules: height < 1.5% of slide (~10px), no text, has bg-color, width > 20%.

NUMBERED STEPS — vertical sequence of numbered circles. The detector
groups small square-ish number circles by x-center and verifies the
sequence starts at 1 and is monotonically increasing:
  <div class="flex flex-col gap-6">
    <div class="flex items-center gap-4">
      <div style="width: 48px; height: 48px; border-radius: 9999px;
                  background-color: #133EFF; color: white;
                  display: flex; align-items: center; justify-content: center;
                  font-size: 24px;">1</div>
      <div>
        <h3 style="font-size: 22px;">Step Title</h3>
        <p style="font-size: 16px;">Step description.</p>
      </div>
    </div>
    <!-- repeat with "2", "3", ... -->
  </div>
Rules: number circle width < 10% (~128px), height < 12% (~86px), text
matches "1", "2.", "3)" etc. (digits, optional trailing . or )), starts
at 1, sequential. Wrapping container and step body render naturally
beside the number.

BULLET LIST — use <ul><li> tags:
  <ul style="list-style: disc; padding-left: 24px; font-size: 18px;">
    <li>First point</li>
    <li>Second point</li>
    <li>Third point</li>
  </ul>
Alternative: any element with class containing "list", "bullet", or
"check" works. Needs at least 2 siblings under the same parent.

TABLE — for tabular data (rows of the same shape with aligned columns),
USE A REAL <table>. Do NOT fake tables with flex + spans — that produces
fifteen disconnected text_boxes with no row/column structure. A real
<table> lets each cell become its own text_box in a clean grid:
  <table style="width: 100%; border-collapse: collapse; font-size: 18px;">
    <thead>
      <tr style="background-color: rgba(19, 62, 255, 0.2);">
        <th style="padding: 12px 16px; text-align: left; color: #FFFFFF;
                   border-bottom: 2px solid #133EFF;">Variant</th>
        <th style="padding: 12px 16px; text-align: left; color: #FFFFFF;
                   border-bottom: 2px solid #133EFF;">Python</th>
        <th style="padding: 12px 16px; text-align: left; color: #FFFFFF;
                   border-bottom: 2px solid #133EFF;">ROCm</th>
      </tr>
    </thead>
    <tbody>
      <tr>
        <td style="padding: 12px 16px; color: #FFFFFF; font-weight: bold;">rocm700</td>
        <td style="padding: 12px 16px; color: #E2E8F0;">3.12</td>
        <td style="padding: 12px 16px; color: #E2E8F0;">7.0</td>
      </tr>
      <tr>
        <td style="padding: 12px 16px; color: #FFFFFF; font-weight: bold;">rocm721</td>
        <td style="padding: 12px 16px; color: #E2E8F0;">3.12</td>
        <td style="padding: 12px 16px; color: #E2E8F0;">7.2.1</td>
      </tr>
    </tbody>
  </table>
Rules: each <th>/<td> renders as a separate text_box positioned by the
browser's table layout — the PPTX output preserves row/column alignment.
Use this whenever you have 2+ rows of the same schema (specs, schedules,
comparisons with consistent columns). Use border-collapse: collapse and
explicit padding/alignment on every cell so the browser layout is
deterministic.

ICON + TEXT ROW — for short callouts:
  <div class="flex items-center gap-2">
    <i class="material-icons" style="font-size: 24px; color: #00CD82;">check_circle</i>
    <span style="font-size: 18px;">Verified</span>
  </div>

BLUR GLOW (decorative) — soft light circle behind content:
  <div style="position: absolute; top: -100px; right: -100px;
              width: 400px; height: 400px; border-radius: 9999px;
              background-color: #133EFF; opacity: 0.15;
              filter: blur(60px);"></div>
Rules: no text, low opacity (< 0.5) OR low-alpha bg, AND (square-ish OR
big radius OR has filter). Use sparingly — 1-2 per slide max.

GRADIENT OVERLAY — full-bleed background gradient (no text):
  <div style="position: absolute; inset: 0;
              background: linear-gradient(135deg, #133EFF 0%, #000000 100%);"></div>
Rules: gradient bg, width > 30%, height > 20%, no text. Put text in a
sibling layer with higher z-index.

== FORBIDDEN (the tool will reject these) ==

  <script>, <iframe>, <object>, <embed>, <link>, <style> tags
  javascript: URLs
  on* event handlers (onclick=, onload=, ...)
  <form>, hand-authored <svg> (see SVG section below for the ONLY allowed form)
  FAKE tables (flex + spans used to look like a table) — use a real <table> instead

== SVG (vector images) ==

  Inline <svg data-slot="..."> is allowed ONLY when the SVG Generator
  stage hands you a pre-built snippet (the prompt's PRE-GENERATED
  IMAGES block). The converter turns each such snippet into editable
  native PPT shapes via svg_to_pptx.

  PLACEMENT STRATEGY IS DRIVEN BY image_type (each snippet's
  PRE-GENERATED IMAGES header tells you its type):

  - hero         → full-bleed wrapper with `opacity: 0.3` (max 0.35) as
                   ambient background, OR position on the non-text half
                   of the slide (e.g. right 50%) at full opacity. NEVER
                   full-bleed at opacity 1.0 — overlaid text becomes
                   unreadable. The opacity MUST sit on the WRAPPER DIV,
                   not inside the SVG (the markup is verbatim).

                   GOOD: <div style="position:absolute; inset:0; opacity:0.3">{svg}</div>
                   GOOD: <div style="position:absolute; right:0; top:0; width:640px; height:720px">{svg}</div>
                   BAD:  <div style="position:absolute; inset:0">{svg}</div>   (full-bleed opacity 1)

  - flowchart / diagram / chart
                 → own region with explicit margins. The wrapper MUST
                   be at least 800px wide (smaller wrappers downscale
                   the viewBox=1280 SVG so the text inside becomes
                   unreadable; the SVG generator writes text at
                   font-size 28+ in viewBox units expecting ≥ 800px
                   render width). Text on the slide lives in a
                   SEPARATE region (above / below / beside), NEVER
                   overlapping the SVG.

                   GOOD: <div style="position:absolute; left:80px; top:140px; width:1120px; height:460px">{svg}</div>
                   GOOD: <div style="position:absolute; left:160px; top:160px; width:960px; height:480px">{svg}</div>
                   BAD:  <div style="max-width:560px">{svg}</div>          (< 800px → text unreadable)
                   BAD:  <div style="width:50%">{svg}</div>                (50% of 1280 = 640 < 800)

  - illustration → standalone spot, never full-bleed. Place beside or
                   below text, not behind it.

  - icon_cluster → corner accent or inside a card.

  Rules:
  - Copy each snippet verbatim into your HTML. Wrap it in a positioning
    container as shown above. Do NOT modify the <svg> markup.
  - Load-bearing SVGs (hero / flowchart / diagram / chart): MUST appear.
    No exceptions. If a load-bearing SVG + content exceeds the 12000-char
    HTML cap, simplify the CONTENT (fewer cards, shorter text), never
    drop the SVG.
  - Decorative SVGs (illustration / icon_cluster): MUST appear by default.
    You MAY omit ONE only when (a) the slide has 3+ keypoint cards AND
    (b) the SVG consumes > 50% of the HTML budget (size is shown in the
    PRE-GENERATED IMAGES header). If you omit, state it in your reasoning:
    `omitting <slot_id> to stay within HTML budget`.
  - Do NOT author your own <svg>. Do NOT use <svg> for icons — use
    Material Icons (the ICON+GLOW pattern).

== CSS LIMITS (these work but with constraints) ==

box-shadow: single layer only, no inset, no spread (4th length).
  GOOD: box-shadow: 4px 4px 12px rgba(0,0,0,0.3);
  BAD:  box-shadow: 0 0 10px 5px #000;            (spread not read)
  BAD:  box-shadow: inset 0 0 10px #000;          (inset unsupported)
  BAD:  box-shadow: 0 0 10px #000, 0 0 20px #111; (2nd layer dropped)

border-radius: 9999px means "as round as possible":
  - On a square element (width ≈ height) → becomes a perfect circle.
  - On a pill/wide element (ratio > 2:1) → becomes a capsule.
  Use 9999px for these two cases; for normal rectangles use explicit
  px values (8/12/16/20).

linear-gradient: any number of stops is supported, but explicit stop
positions are honored only if ALL stops have them:
  GOOD: linear-gradient(90deg, #133EFF 0%, #00CD82 50%, #FFFFFF 100%)
  DOWNGRADED: linear-gradient(90deg, #133EFF 0%, #00CD82, #FFFFFF 100%)
              (middle stop has no position → evenly distributed)

filter: blur(): only on decorative BLUR GLOW elements. blur on real
content elements (cards, text) loses its effect.

opacity: applies to cards, badges, shapes, text — but the element MUST
have an opaque-ish background for the effect to be visible against the
slide background.

== STAY IN BOUNDS ==

Total content must fit inside 1280x720. The browser clips overflow.
If a `flex-col justify-between` column STILL runs past 720px after
applying the VERTICAL DISTRIBUTION patterns above, you have too much
content — split into two slides, switch to a grid (Pattern C), or
trim bullet counts to 3-5. Use compact spacing (gap-4 not gap-12).

Max HTML size: 12000 chars.
"""


# The full guide sent to the LLM. The CSS-level forbidden lists are
# auto-generated from `html_contract.py` so that the linter
# (`slide_tools._validate_free_form_html`) and the prompt agree on
# exactly what is rejected. Edit the contract, not this file.
HTML_AUTHORING_GUIDE = (
    _HTML_AUTHORING_GUIDE_BASE
    + "\n\n"
    + render_forbidden_css_markdown()
    + "\n\n"
    + render_forbidden_tailwind_markdown()
)
