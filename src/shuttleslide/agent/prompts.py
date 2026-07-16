"""System prompts for each pipeline stage.

These prompts are the single most important quality lever in the system.
Each one is kept as a string constant for easy iteration.

The slide-builder uses a FREE-FORM HTML model: the LLM authors the entire
inner HTML of each slide directly. The system wraps it in a fixed
1280x720 `.ppt-slide` container; `html_to_pptx` then renders the HTML in
a headless browser, classifies each element by structure/position, and
maps it to PPTX shapes.

The constants below (``HOUSE_RULES``, ``THEME_DESIGNER_PROMPT``, etc.)
are the canonical 16:9 slide-deck prompts. The ``build_*`` functions
format them with state data; external callers may temporarily swap a
constant (e.g. ``HOUSE_RULES``) for a non-default value if they need
mode-specific behaviour, but the defaults reproduce the historical
``slidecraft generate`` output byte-for-byte.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from shuttleslide.agent.html_guide import (
    HTML_AUTHORING_GUIDE,
    build_html_authoring_guide,
)
from shuttleslide.agent.tools.slide_tools import _FREE_FORM_HTML_MAX_LEN as _HTML_BUDGET


# ---------------------------------------------------------------------------
# House rules — shared conventions across all slide-builder prompts.
# ---------------------------------------------------------------------------

HOUSE_RULES = """\
HOUSE RULES (apply to every slide):
- Step 1: Optionally call `set_slide_background` if the slide needs a
  non-default background (image_overlay for hero/cover, gradient for
  section dividers, solid for content slides).
- Step 2: Call `set_free_form_html(html=...)` ONCE with the complete
  inner HTML for the slide body. The system wraps it in a
  .ppt-slide container of the canvas dimensions you've been given;
  you provide everything inside.
- Step 3: Call `finish_slide` when done.
- The HTML authoring guide below defines EXACTLY which patterns the
  PPTX converter will recognize (cards, badges, title bars, numbered
  steps, bullet lists, etc.). Reuse those patterns.
- Root wrapper background: the OUTERMOST `<div>` of your HTML (the
  `w-full h-full` wrapper around the slide body) MUST NOT set a
  non-theme background-color. Either omit `background-color` entirely
  (the outer `.ppt-slide` container provides `theme.bg_color` and will
  show through) or write `background-color: {{theme.bg_color}};`
  explicitly. NEVER write a literal hex like `#FFFFFF` or `#F8F9FA` on
  the root — it covers the slide background and breaks theme edits.
  Inner cards / panels / badges may use literals freely.
- Color management: ALWAYS use `{{theme.<field>}}` placeholders for
  theme colors and fonts in inline styles — NEVER hardcode hex from the
  THEME block. The renderer substitutes the live value at render time,
  so the slide tracks theme edits without re-generation. Tokens:
  {{theme.primary_color}}, {{theme.accent_color}}, {{theme.warn_color}},
  {{theme.bg_color}}, {{theme.text_color}}, {{theme.title_color}},
  {{theme.font_title}}, {{theme.font_body}}. Derived:
  {{theme.<color>_rgba:<alpha>}} (e.g. {{theme.primary_rgba:0.2}}),
  {{theme.<color>_darken:<factor>}}. Place tokens wherever a hex string
  or font name would appear: color:, background:, border:,
  linear-gradient(...), font-family:, inside rgba(). The HTML AUTHORING
  GUIDE has full placeholder rules. Non-theme neutrals (grays, pure
  black overlays) stay as literals.
- Font sizes: always inline px (e.g. style="font-size: 24px"). NEVER use
  rem/em or Tailwind text-size classes — they break the px->pt converter.
- Content density: 5-15 recognizable elements per slide is the sweet
  spot. Too few looks empty; too many gets clipped at the canvas
  height.
- Tabular data: if you have 2+ rows of the same schema (specs,
  schedules, comparisons with aligned columns), use a real `<table>`.
  NEVER fake tables with flex + spans — that produces disconnected
  text_boxes with no row/column structure.
- Stay inside the canvas dimensions. The browser clips overflow — if
  a column runs past the canvas height the bottom items disappear.
"""


# ---------------------------------------------------------------------------
# Stage 1: Theme Designer
# ---------------------------------------------------------------------------

# layout_conventions example, injected into THEME_DESIGNER_PROMPT based on
# canvas orientation. The example is the single strongest steer on how the
# LLM writes `layout_conventions`, and `layout_conventions` is then injected
# into every downstream slide_builder call — so a landscape-only example
# here drags the whole deck toward landscape composition even on a portrait
# canvas. Keep these in sync with _LAYOUT_LANDSCAPE / _LAYOUT_PORTRAIT /
# _LAYOUT_SQUARE in html_guide.py.
_LAYOUT_CONVENTIONS_LANDSCAPE = (
    'e.g. "title bar at top with gradient, two-column body, '
    'dashed divider between columns"'
)
_LAYOUT_CONVENTIONS_PORTRAIT = (
    'e.g. "hero block (image or large icon) anchoring the top ~40%, '
    'single-column narrative flow below, 3-5 stacked cards with dashed '
    'dividers between sections" — portrait canvases are tall, so prefer '
    'stacking over side-by-side; NEVER propose two-column or multi-column '
    'body layouts'
)
_LAYOUT_CONVENTIONS_SQUARE = (
    'e.g. "centered focal composition, symmetric top/bottom distribution, '
    '2x2 card grid for parallel items"'
)


def _layout_conventions_hint(canvas_width_px: int, canvas_height_px: int) -> str:
    """Pick the layout_conventions example matching the canvas orientation."""
    w, h = canvas_width_px, canvas_height_px
    if h > w:
        return _LAYOUT_CONVENTIONS_PORTRAIT
    if w > h:
        return _LAYOUT_CONVENTIONS_LANDSCAPE
    return _LAYOUT_CONVENTIONS_SQUARE


THEME_DESIGNER_PROMPT = """\
You are a senior presentation designer. Your job is to define the global \
THEME for a slide deck based on the topic and a style hint.

The theme is locked ONCE here and re-injected into every subsequent slide \
generation, so it MUST be complete and self-consistent.

OUTPUT: Call the `define_theme` tool ONCE with all fields. Do not call any \
other tool.

THEME FIELDS:
- primary_color: main brand color, as 6-digit hex (#RRGGBB). Used for titles, bars, \
primary accents. Pick a color that fits the style hint — do NOT copy any example \
value verbatim from this prompt.
- accent_color: secondary highlight color (#RRGGBB). Used for icons, dividers, key data.
- warn_color: alert/problem color (#RRGGBB). Used sparingly for warnings or contrasts.
- bg_color: default slide background. Light decks: near-white; dark decks: near-black.
- text_color: default body text color. MUST contrast with bg_color at WCAG AA \
(ratio ≥ 4.5:1) — the tool rejects lower ratios.
- title_color: title text color. MUST contrast with bg_color at WCAG AA \
(ratio ≥ 3:1 for large text). White or near-white for dark decks; primary_color \
or a dark variant for light decks.
- font_title: font for titles (Roboto is safe; consider Playfair Display for elegant, \
Inter for tech, Noto Sans SC for Chinese).
- font_body: font for body text.
- decoration_style: one of "minimal", "glassmorphism", "neon", "editorial", "playful".
  Controls how decoration elements (blur_glow, gradients, badges) are used.
- cover_bg_strategy: how the title slide background should look. One of:
  "dark_gradient", "image_overlay", "solid_color", "geometric".
- layout_conventions: a short description (1-3 sentences) of how content slides \
should be laid out ({layout_conventions_hint}).

STYLE HINT: "{style_hint}"
TOPIC: "{topic}"

Pick colors and fonts that fit the style hint. For example:
- business → deep blues, Roboto/Inter, minimal decoration
- cute → pastel pinks/peaches, rounded shapes, playful decoration
- anime → vivid colors, high contrast, neon-style accents
- tech → dark backgrounds, bright accents, glassmorphism
- editorial → serif titles, generous whitespace, restrained palette

Decide confidently. Do not hedge.
"""


# ---------------------------------------------------------------------------
# Layout examples shared across Stage 2 prompts (one-shot + progressive).
#
# Same logic as _layout_conventions_hint above: the layout_hint examples in
# OUTLINE_PLANNER_PROMPT and the layout_intent / LAYOUT DIVERSITY examples
# in the progressive prompts were originally landscape-only ("2-column
# grid", "3-card horizontal grid"). On a portrait canvas those examples
# drag the outline toward landscape composition. Each set below is picked
# by canvas orientation.
# ---------------------------------------------------------------------------

_LAYOUT_HINT_EXAMPLES_LANDSCAPE = """\
Examples:
    * "Full-bleed hero cover with big icon, title, subtitle, and 3 tech \\
       tag pills at the bottom. Background image with dark gradient overlay."
    * "Title bar at top, then a 2-column grid: left column is a card with \\
       a key concept, right column is a vertical numbered list of 4 steps."
    * "Section divider: huge ghosted '02' number in background, heading \\
       and one-line description centered."
    * "Title bar, then a 3-card horizontal grid showing three options, \\
       each with an icon, title, and 2 bullet points. Bottom dashed \\
       insight banner with the key takeaway."
    * "Synthesis slide: centered check_circle icon, big takeaway h1, \\
       three small recap pills (one per argument group), and a Q&A subtitle."\
"""

_LAYOUT_HINT_EXAMPLES_PORTRAIT = """\
Examples (PORTRAIT canvas — stack vertically, never multi-column):
    * "Hero cover: full-bleed image or large icon anchoring the top ~40%, \\
       title + subtitle + 3 tag pills stacked in a single column below. \\
       Background image with dark gradient overlay."
    * "Title block at top, then 3 stacked cards (one per key_point) each \\
       with icon-left + text-right, connected by dashed dividers down the page."
    * "Section divider: huge ghosted '02' number occupying the top half, \\
       heading + one-line description centered below."
    * "Synthesis slide: large check_circle icon at top, big takeaway h1 \\
       centered, three small recap pills stacked vertically, Q&A subtitle \\
       pinned near the bottom."
NEVER propose two-column / grid-cols-2+ / horizontal-multi-card layouts on \
a portrait canvas — columns become too narrow for readable text.\
"""

_LAYOUT_HINT_EXAMPLES_SQUARE = """\
Examples (SQUARE canvas — symmetric, centered focal works best):
    * "Hero cover: centered focal image or large icon, title + subtitle \\
       stacked below, 2x2 tag pill grid at the bottom."
    * "Title block at top, then a 2x2 card grid (4 parallel concepts) \\
       with dashed cross dividers."
    * "Section divider: huge ghosted '02' number centered, heading + \\
       one-line description below."
    * "Synthesis slide: large check_circle icon centered, big takeaway h1 \\
       below, four small recap pills in a 2x2 grid."\
"""


def _layout_hint_examples(canvas_width_px: int, canvas_height_px: int) -> str:
    """Pick the layout_hint example block matching the canvas orientation."""
    w, h = canvas_width_px, canvas_height_px
    if h > w:
        return _LAYOUT_HINT_EXAMPLES_PORTRAIT
    if w > h:
        return _LAYOUT_HINT_EXAMPLES_LANDSCAPE
    return _LAYOUT_HINT_EXAMPLES_SQUARE


# layout_intent tag list for STRUCTURE_PLANNER_PROMPT (1-3 word tags).
# Same orientation rule — the old hard-coded list ("two_column_compare",
# "title_bar_3_cards") was landscape-only.
_LAYOUT_INTENT_TAGS_LANDSCAPE = (
    '"hero_cover", "two_column_compare", "section_divider", '
    '"title_bar_3_cards", "numbered_steps"'
)
_LAYOUT_INTENT_TAGS_PORTRAIT = (
    '"hero_cover", "stacked_cards", "section_divider", '
    '"title_bar_numbered_steps", "single_column_narrative"'
)
_LAYOUT_INTENT_TAGS_SQUARE = (
    '"hero_cover", "grid_2x2", "section_divider", '
    '"centered_focal", "numbered_steps"'
)


def _layout_intent_tags(canvas_width_px: int, canvas_height_px: int) -> str:
    """Pick the layout_intent tag list matching the canvas orientation."""
    w, h = canvas_width_px, canvas_height_px
    if h > w:
        return _LAYOUT_INTENT_TAGS_PORTRAIT
    if w > h:
        return _LAYOUT_INTENT_TAGS_LANDSCAPE
    return _LAYOUT_INTENT_TAGS_SQUARE


# LAYOUT DIVERSITY block for SLIDE_DETAIL_GENERATOR_PROMPT. The default
# block told the LLM to "try a 2-column compare" / "3-card grid" — both
# landscape idioms. The portrait variant steers toward vertical variation.
_LAYOUT_DIVERSITY_LANDSCAPE = """\
LAYOUT DIVERSITY:
  - DO NOT copy the previous slide's layout structure verbatim.
  - Vary: column count, card vs list vs table, hero position, accent placement.
  - If the previous slide was a 3-card grid, try a 2-column compare, a \\
numbered list, or a title-bar-with-bullets.\
"""

_LAYOUT_DIVERSITY_PORTRAIT = """\
LAYOUT DIVERSITY (PORTRAIT canvas — every layout stacks vertically):
  - DO NOT copy the previous slide's layout structure verbatim.
  - Vary: hero position (top full-bleed vs top-half), card count (3 vs 4 \\
stacked), accent placement, divider style.
  - If the previous slide was 3 stacked cards, try a hero-top + \\
single-column narrative, a title-bar-with-numbered-steps, or a centered \\
focal quote. NEVER switch to a multi-column / grid-cols-2+ layout to vary \\
things — on a portrait canvas that just produces unreadable columns.\
"""

_LAYOUT_DIVERSITY_SQUARE = """\
LAYOUT DIVERSITY:
  - DO NOT copy the previous slide's layout structure verbatim.
  - Vary: 2x2 grid vs centered focal vs symmetric top/bottom split, accent \\
placement, divider style.
  - If the previous slide was a 2x2 grid, try a centered focal, a stacked \\
single-column, or a symmetric top/bottom split.\
"""


def _layout_diversity_hint(canvas_width_px: int, canvas_height_px: int) -> str:
    """Pick the LAYOUT DIVERSITY block matching the canvas orientation."""
    w, h = canvas_width_px, canvas_height_px
    if h > w:
        return _LAYOUT_DIVERSITY_PORTRAIT
    if w > h:
        return _LAYOUT_DIVERSITY_LANDSCAPE
    return _LAYOUT_DIVERSITY_SQUARE


# ---------------------------------------------------------------------------
# Stage 2: Outline Planner
# ---------------------------------------------------------------------------

OUTLINE_PLANNER_PROMPT = """\
You are a presentation architect. Plan the OUTLINE of {count_instruction} \
on the topic below.

OUTPUT LANGUAGE: Follow the topic's language. If the topic is in Chinese, \
all user-facing text you produce (titles, purposes, key_points, layout_hints, \
image descriptions) MUST be in Chinese. If the topic is in English, output \
English. If the topic mixes languages, default to the topic's primary language.

OUTPUT: Call `define_outline` ONCE with a list of slides. Do not call any \
other tool.

Each slide in the outline has:
- title: the slide title (concise, 2-8 words). This will become an <h1> or
  the title-bar text.
- purpose: one-sentence description of the slide's role in the deck.
- key_points: 2-5 bullet points covering what content the slide should
  communicate. Each key_point is a single short sentence. These are the
  single source of truth for slide content.
- layout_hint: a short free-text description of the desired visual
  structure. Be specific — this hint guides the slide-builder's HTML
  design. {layout_hint_examples}

PYRAMID PRINCIPLE (Minto Pyramid Principle — Overview · Decomposition · Synthesis):
Every deck MUST follow Overview -> Decomposition -> Synthesis.

- Slide 1 — OVERVIEW: Hero cover that states the CENTRAL THESIS in one
  clear sentence. The audience must hear the punchline before any detail.
  key_points[0] should be the thesis verbatim.

- Slides 2..N-1 — DECOMPOSITION: Break the thesis into 2-4 MECE
  (Mutually Exclusive, Collectively Exhaustive) argument groups. Each group
  gets 1-3 slides developing its claim with evidence, examples, or steps.
  Optionally insert a section_divider between groups (counted toward total).
  Vary content layouts across slides — do not stack 3 identical layouts in
  a row.

- Slide N — SYNTHESIS: Recap each argument group in one sentence,
  reinforce the central thesis, and point forward (next steps / decision /
  Q&A). NOT a generic "Thank You" slide — it must restate the value.

Before calling define_outline, run a MECE check: are the argument groups
non-overlapping AND jointly sufficient to prove the thesis? Regroup if not.

STRUCTURE GUIDE by deck size:
- 4-6 slides: Overview + 1 group developed across N-2 slides + Synthesis.
- 7-12 slides: Overview + 2-3 MECE groups (1-3 slides each) + optional
  section dividers + Synthesis.
- 13+ slides: Overview + 3-4 MECE groups with explicit dividers between
  them + Synthesis.

IMAGE PLANNING (decide source_type per image):

The default is 0 images per slide. When you do plan an image, the FIRST
decision is its SOURCE — svg vs web — because the two paths produce
fundamentally different art and the system cannot recover if you pick
wrong. Then pick image_type, which drives placement on the slide.

SOURCE DECISION (source_type field):

Select the source that matches the actual SUBJECT. Both paths are
production-ready and fall back gracefully, so the decision should be
driven by what the image depicts, not by perceived safety.

  source_type="web"  when the subject is a real, photorealistic thing
                      that exists in the world and carries meaning
                      through its literal appearance:
                        - realistic scenes ("modern coffee shop interior",
                          "hospital operating room", "Tokyo street at night")
                        - named products / brands ("Tesla Model 3",
                          "iPhone 15 Pro", "Coca-Cola logo")
                        - people / portraits / lifestyle ("diverse team
                          collaborating in an office", "chef plating a dish")
                        - recognizable places / landmarks ("Eiffel Tower",
                          "Grand Canyon", "Google headquarters")
                        - textures / materials ("marble surface",
                          "oak wood grain", "brushed aluminum")
                        - food / physical objects ("freshly baked croissant",
                          "mechanical watch movement")
                      Requires source_ref = a search query (Chinese or
                      English). The pipeline searches, downloads
                      candidates, and uses a VLM to pick the best match
                      against your description. On verification failure it
                      cleanly falls back to svg — so web is the right call
                      whenever the subject is photorealistic.

  source_type="svg"  when the image is a STRUCTURAL or ABSTRACT
                      representation whose meaning lives in shapes,
                      arrows, or geometry rather than in literal pixels:
                        - flowcharts / pipelines / state machines
                        - architecture / component / layer diagrams
                        - bar / line / pie charts with concrete numbers
                        - icon clusters (3-6 icons + short labels)
                        - abstract geometric hero covers (concentric
                          rings, layered planes, gradient mesh)
                      No source_ref. SVG produces editable native PPT
                      shapes — the right choice when the value is in
                      structure.

  Rule of thumb: ask "would a photographer stock-photo search capture
  this better than a vector drawing?" If yes → web. If the meaning
  is in boxes-arrows-numbers → svg. NEVER default to svg when the
  subject is a real scene, product, person, place, brand, or texture —
  a vector drawing of a coffee shop interior is strictly worse than a
  verified photo of one.

CONTENT PATTERNS (pick image_type + source_type by content):

  - Hero cover with abstract / geometric / branded metaphor   -> hero + svg
  - Hero cover with photorealistic scene or product shot      -> hero + web
  - Pipelines / workflows / state machines                    -> flowchart + svg
  - System architecture / layer diagrams                      -> diagram + svg
  - Number trends / quantitative comparison                   -> chart + svg
  - Icon + short-label grid (3-6 icons)                       -> icon_cluster + svg
  - Spot icon next to text                                    -> illustration + svg
  - Spot product/scene photo next to text                     -> illustration + web

If the topic names a real product, place, person, brand, food, or
texture, at least one slide (often the hero) should use source_type="web".

Stay at 0 images for: pure text statements, bullet lists, section
dividers (just a number + heading), and the Synthesis recap slide.

- Cover (slide 1) MAY have 1 image with image_type="hero".
- Synthesis (slide N) usually has 0 images.
- Each image spec has these fields:
    * slot_id       — snake_case id unique within the slide (e.g. "flow1",
                       "arch", "hero"). Used by the slide-builder to
                       reference this exact image.
    * aspect_ratio  — one of "16:9", "4:3", "1:1", "3:2", "2:3".
    * image_type    — one of "hero", "flowchart", "diagram",
                       "illustration", "icon_cluster", "chart".
                       Choose by INTENT — the type drives how the image
                       is placed on the slide:
                       - hero        → slide 1 cover visual. Will be composited
                                       as ambient background (low opacity) or
                                       right-half focal art. MUST coexist with
                                       overlaid title/subtitle text. Pairs with
                                       EITHER source_type: "svg" for abstract
                                       geometric/branded covers, "web" for
                                       photorealistic scene covers (e.g. an
                                       office interior, a product hero shot, a
                                       city skyline).
                       - flowchart   → process diagram with nodes + arrows.
                                       Lives in its own region; text
                                       labels live inside the nodes.
                       - diagram     → architecture / relationship /
                                       comparison diagram. Lives in its
                                       own region.
                       - illustration→ decorative spot illustration.
                                       Lives alongside text, NOT behind it.
                       - icon_cluster→ 3-6 icons in a grid with short
                                       labels. Decorative accent.
                       - chart       → bar or line chart. Lives in its
                                       own region.
    * source_type   — "svg" or "web", chosen by SUBJECT (see SOURCE
                       DECISION above). Use "web" for any photorealistic
                       scene, product, person, place, brand, or texture.
                       source_type="web" requires source_ref.
    * source_ref    — Required when source_type="web". A search query
                       (e.g. "modern coffee shop interior") or an absolute
                       https URL. Ignored when source_type="svg".
    * description   — concrete enough that an illustrator (svg) or a VLM
                       (web) could judge the result against it without
                       more context. For flowcharts: name every node and
                       arrow direction. For diagrams: name every part
                       and how they connect. For web: describe the scene
                       concretely enough that the VLM can verify the
                       downloaded photo matches (e.g. "a bright modern
                       coffee shop interior with wooden tables and
                       pendant lights").
- Hard limits: 0-3 images per slide. The system rejects more.

WORKED EXAMPLES — source_type decisions by topic:

  "Tesla Model 3 intro" hero      -> source_type="web" (subject IS the literal car)
  "Sanya travel guide" hero       -> source_type="web" (real scenery)
  "Our CI/CD pipeline architecture" -> source_type="svg" (boxes + arrows)
  "Org structure" diagram         -> source_type="svg" (tree)
  "Cafe brand refresh" hero       -> source_type="web" (interior photo)
  "Cafe brand refresh" mood board -> source_type="web" (marble texture)

Image spec field shape (one item in slide.images — write fields, not braces):
  slot_id="hero", aspect_ratio="16:9", image_type="hero",
  source_type="web",
  source_ref="modern coffee shop interior",
  description="bright cafe with wooden tables and pendant lights"

Remember: every define_outline call MUST have a top-level "slides" array
containing 3+ slide objects. Each slide object has title, purpose,
key_points, and optionally images (0-3 specs like the one above).

The slide-builder will translate your layout_hint into actual HTML. Be
specific enough that a designer reading just the hint could sketch the
slide, but don't write HTML yourself.

TOPIC: "{topic}"
STYLE HINT: "{style_hint}"

THEME (for color/layout reference): {theme_json}

{user_image_library_block}

{final_count_instruction}
"""


# ---------------------------------------------------------------------------
# Stage 3: Slide Builder (per slide)
# ---------------------------------------------------------------------------

SLIDE_BUILDER_PROMPT = """\
You are a presentation designer building ONE slide by authoring its HTML.
You write the inner HTML of the slide; the system wraps it in a fixed
{canvas_width_px}x{canvas_height_px} .ppt-slide container.

You will be given:
1. The global THEME — reference its fields as `{{theme.<field>}}`
   placeholders in your inline styles (see HOUSE RULES + HTML AUTHORING
   GUIDE for the placeholder rules). The renderer substitutes live
   values at render time.
2. This slide's OUTLINE (title, purpose, key_points, layout_hint).
3. Your slide index and the total slide count.
4. PRE-GENERATED SVG snippets (if any) that MUST appear in your HTML
   verbatim, wrapped in positioning containers.

INSTRUCTIONS:
1. Optionally call `set_slide_background` if the slide needs a non-default
   background (image_overlay for hero covers, gradient for section
   dividers, solid for content slides).
2. Call `set_free_form_html(html=...)` ONCE with the complete inner HTML
   of the slide. Follow the HTML AUTHORING GUIDE below precisely — it
   defines the patterns the PPTX converter recognizes.
2b. If the PRE-GENERATED IMAGES block contains SVG snippets, place each
    snippet according to its image_type — see the HTML AUTHORING GUIDE's
    SVG section for the per-type rules (hero → ambient bg with wrapper
    opacity ≤ 0.35 OR non-text half; flowchart/diagram/chart → own
    region with no text overlap; etc.). SVG opacity MUST be set on the
    WRAPPER DIV, not inside the SVG markup (the markup must be verbatim).
3. Call `finish_slide` when done.

{house_rules}

{images_block}

THEME (current values for contrast reasoning — reference these fields as
{{theme.<field>}} placeholders in your HTML; the renderer substitutes the
live value at render time, so the slide tracks theme edits):
{theme_json}

YOUR SLIDE (index {slide_index} of {total_count}):
- title: {title}
- purpose: {purpose}
- layout_hint: {layout_hint}
- key_points:
{key_points_formatted}

{html_guide}

Author the slide HTML now.
"""


# ---------------------------------------------------------------------------
# Stage 3: Slide Builder — INCREMENTAL update (review pipeline regenerate)
# ---------------------------------------------------------------------------
#
# Used by InteractiveOrchestrator.regenerate_item when the user clicks
# "Update this slide" on a stale mark. Preserves user manual edits by
# showing the LLM the current HTML and asking it to apply only the
# minimum changes needed to reflect the upstream diff.
#
# Pairs with ``build_slide_builder_incremental_prompt`` below, which
# assembles before/after outline context (and optionally before/after
# theme) from the stale mark's ``context_snapshot``.
SLIDE_BUILDER_INCREMENTAL_PROMPT = """\
You are updating an EXISTING slide's HTML based on upstream changes.
The current HTML may contain user customizations you MUST preserve.

CURRENT SLIDE HTML (may include user edits):
<div class="ppt-slide">
{current_html}
</div>

UPSTREAM CHANGE CONTEXT:
- Outline for this slide:
  - Before: {old_outline_json}
  - After:  {new_outline_json}
{optional_theme_change_section}

YOUR TASK:
1. Identify what specifically changed in the outline (reworded key_points,
   added/removed points, title change, layout_hint change).
2. Apply ONLY the changes needed to reflect the new outline.
3. PRESERVE everything else:
   - User's manual text edits (unless directly contradicted by new outline)
   - Structural choices (cards, badges, tables, layout patterns)
   - Inline styles, classes, decoration choices
   - SVG/image placements and wrappers

ANTI-PATTERNS (do NOT do these):
- Do NOT rewrite the slide from scratch
- Do NOT reorganize the layout unless layout_hint changed
- Do NOT "improve" wording that wasn't part of the outline change
- Do NOT convert user's literal colors to {{{{theme.*}}}} placeholders
- Do NOT touch SVG wrappers or image positioning
- Do NOT remove decorative elements (badges, icons, gradients) you didn't add

{house_rules}

{images_block}

THEME (current values — reference via {{{{theme.<field>}}}} placeholders):
{theme_json}

YOUR SLIDE (index {slide_index} of {total_count}):
- title: {title}
- purpose: {purpose}
- layout_hint: {layout_hint}
- key_points:
{key_points_formatted}

{html_guide}

Call `set_free_form_html(html=...)` ONCE with the updated HTML. Call \
`finish_slide` when done.
"""


# ---------------------------------------------------------------------------
# Stage 2.5: SVG Generator (per image spec)
# ---------------------------------------------------------------------------
#
# Aspect ratio → viewBox mapping. Must match the OUTLINE_PLANNER_PROMPT
# enum and the validator in outline_tools.py.
_ASPECT_VIEWBOX = {
    "16:9": "0 0 1280 720",
    "4:3":  "0 0 1024 768",
    "1:1":  "0 0 800 800",
    "3:2":  "0 0 1200 800",
    "2:3":  "0 0 800 1200",
}


SVG_GENERATOR_PROMPT = """\
You are a vector illustrator producing ONE inline SVG that the system \
will convert into editable native PowerPoint shapes.

OUTPUT: Call `set_svg(svg=...)` ONCE with the complete SVG markup. Do \
not call any other tool.

SPEC:
- aspect_ratio: {aspect_ratio}
- image_type:   {image_type}
- description:  {description}
- slot_id:      {slot_id}   (use this as the id of the root <svg>)

STORAGE: The SVG markup is written to svgs/slide_N_{slot_id}.svg on \
disk. The slide-builder LLM never sees the raw markup — it embeds a \
short <img class="shuttleslide-svg-placeholder" src="svgs/..."> \
reference instead, and html_to_pptx inlines the SVG back during the \
PowerPoint conversion pass. Author the SVG for quality, not for size.

SVG CONSTRAINTS (HARD — violating any of these fails the slide and \
triggers a retry):
- Root <svg> must have viewBox="{viewbox}" and xmlns="http://www.w3.org/2000/svg".
- Root <svg> must have id="{slot_id}" and data-slot="{slot_id}".
- The FIRST CHILD of <svg> must be <desc>{description}</desc>. This is \
  required for accessibility and so downstream tools reading the DOM \
  (browser a11y tree, future LLM round-trip edits) can identify what \
  the image depicts without parsing geometry. Put <desc> before any \
  <defs>, shapes, or other elements. The <desc> text must match the \
  SPEC description above.
- DO NOT include a full-bleed background <rect> spanning the entire \
  viewBox. The SVG is composited on top of the slide background, so \
  a full-bleed rect will mask the slide. SVG must be transparent. \
  If the image needs a LOCAL panel background (e.g. a flowchart card, \
  a poster frame), size the rect to that panel — never to the viewBox.
- Use ONLY elements the converter supports: rect, circle, ellipse, \
  line, path, polygon, polyline, text, tspan, g, linearGradient, \
  radialGradient, defs, use, marker, stop, title, desc. NO <image>, \
  NO <foreignObject>, NO animations, NO <style>, NO <script>. \
  Drop shadows / glow on icons MUST use <filter> inside <defs> with \
  these primitives only: feDropShadow, feGaussianBlur, feOffset, \
  feFlood, feFuncA, feComponentTransfer (as feFuncA container), \
  feMerge / feMergeNode (to compose the shadow behind SourceGraphic). \
  NO feColorMatrix, \
  feTurbulence, feDisplacementMap, feComposite, feBlend, feImage, \
  feMorphology, feSpecularLighting, \
  feDiffuseLighting — the converter silently drops them. \
  Hatched / cross-hatch fills use <pattern> inside <defs>, referenced \
  via fill="url(#...)". Clipped shapes use <clipPath> inside <defs>, \
  referenced via clip-path="url(#...)".
- Inline ALL styles as element attributes (fill="...", stroke="...", \
  font-family="...", font-size="..." etc). NO <style> tags — the \
  sanitizer rejects them. Use generic font families like sans-serif.
- Use theme colors where appropriate: {theme_colors_json}
- Min font size 28 in viewBox units (the SVG is authored at viewBox=\
  1280x720 but the slide-builder may downscale the wrapper to ~600-\
  800px wide; 28 viewBox units → ~13-18px rendered text). Use 32-40 \
  for primary labels, 28 for secondary. Text must be real <text>, \
  not paths.
- For hero: design as AMBIENT background art, NOT a focal subject. Use \
  line-art / outline / low-saturation palette (max 3 muted colors). \
  Keep dense detail on ONE side (left or right half) so the other half \
  stays visually quiet for overlaid title/subtitle text. The SVG will \
  be composited at low opacity (≤ 0.35) OR placed on the non-text half \
  of the slide — design must work either way. Avoid large solid fills.
- For flowcharts: rect nodes with rounded corners (rx=8), <marker> \
  arrowheads defined in <defs>, legible labels inside nodes.
- For diagrams: clear geometric hierarchy, accent colors for emphasis.
- For illustrations: flat style, 3-8 colors total, no photorealism.
- For icon_cluster: 3-6 large icons in a balanced grid with short labels.
- For chart: bar or line only, axes drawn as <line>, data points labeled.
- Max 80 elements total. Every element becomes a separate editable PPT \
  shape, so simpler is better.
- Target 2000-5000 chars of SVG markup for typical illustrations, \
  5000-8000 for complex flowcharts/diagrams. The tool accepts up to \
  50000 chars as a sanity guard against runaway output, but every \
  extra character costs LLM output tokens AND produces additional PPT \
  shapes that slow down html_to_pptx rendering. Err on the simple side: \
  fewer shapes, fewer labels, more whitespace. If your drawing needs \
  more than 8000 chars, simplify — merge similar paths, drop decorative \
  shapes, shorten labels.

Draw the {image_type} now.
"""


# ---------------------------------------------------------------------------
# Prompt builders — assemble prompts with state data.
# ---------------------------------------------------------------------------

def build_theme_designer_prompt(
    topic: str,
    style_hint: str,
    canvas_width_px: int = 1280,
    canvas_height_px: int = 720,
) -> str:
    return THEME_DESIGNER_PROMPT.format(
        topic=topic,
        style_hint=style_hint,
        layout_conventions_hint=_layout_conventions_hint(
            canvas_width_px, canvas_height_px
        ),
    )


def build_outline_planner_prompt(
    topic: str,
    style_hint: str,
    target_count: Optional[int],
    theme: Dict[str, Any],
    user_image_library: Optional[List[Dict[str, Any]]] = None,
    canvas_width_px: int = 1280,
    canvas_height_px: int = 720,
) -> str:
    theme_json = json.dumps(theme, ensure_ascii=False, indent=2)
    if target_count is not None:
        count_instruction = f"a {target_count}-slide deck"
        final_count_instruction = (
            f"Write exactly {target_count} slides arranged as "
            f"Overview + Decomposition + Synthesis. Be specific in key_points — "
            f"the next stage will expand each into actual on-slide content."
        )
    else:
        count_instruction = "a deck — YOU decide the slide count based on content depth"
        final_count_instruction = (
            "Choose the slide count yourself based on the MECE breakdown: "
            "6-10 for a focused topic, 10-15 for multi-faceted, 15-25 only for "
            "dense reference material. Minimum 4 (Overview + 2 groups + Synthesis), "
            "max 30. State your chosen count and the MECE groups in your reasoning "
            "before the tool call. Be specific in key_points — the next stage will "
            "expand each into actual on-slide content."
        )
    return OUTLINE_PLANNER_PROMPT.format(
        topic=topic,
        style_hint=style_hint,
        count_instruction=count_instruction,
        final_count_instruction=final_count_instruction,
        theme_json=theme_json,
        user_image_library_block=_format_user_image_library_block(user_image_library),
        layout_hint_examples=_layout_hint_examples(canvas_width_px, canvas_height_px),
    )


# ---------------------------------------------------------------------------
# Stage 2a + 2b: Progressive outline (structure planner + slide detail)
#
# Two prompts that together replace OUTLINE_PLANNER_PROMPT for the
# progressive path. The orchestrator falls back to OUTLINE_PLANNER_PROMPT
# when the progressive tools fail; both paths must stay valid.
#
# Why two stages:
#   - Stage 2a commits image_intent at deck-planning time, so overall
#     image distribution is designed once (not discovered slide-by-slide).
#   - Stage 2b sees prior slides' layouts and can vary its own to avoid
#     repetition, which a one-shot call cannot do.
#   - Per-slide calls reduce token pressure: each call writes ~400 tokens
#     of detail instead of ~3000 tokens of full outline.
# ---------------------------------------------------------------------------

STRUCTURE_PLANNER_PROMPT = """\
You are a presentation architect. Plan the STRUCTURE of {count_instruction} \
on the topic below. Do NOT write detailed key_points or image specs yet — \
a follow-up stage will fill those in per slide.

OUTPUT LANGUAGE: Follow the topic's language. If the topic is in Chinese, \
all user-facing text you produce (thesis, group names, titles, purposes) \
MUST be in Chinese. If the topic is in English, output English. If the \
topic mixes languages, default to the topic's primary language.

OUTPUT: Call `define_skeleton` ONCE with three top-level fields:
  - thesis: the central claim of the deck in ONE clear sentence.
  - groups: 2-4 MECE argument groups (Mutually Exclusive, Collectively \
Exhaustive). Each group has id / name / slide_indices.
  - slides: per-slide skeleton — title / purpose / group_id / \
layout_intent / image_intent. Every slide index must appear in exactly \
one group's slide_indices.

PYRAMID PRINCIPLE (Minto Pyramid Principle — Overview · Decomposition · Synthesis):
  - Slide 1 (index 0) — OVERVIEW: states the thesis. key_points[0] will \
become the deck's tagline.
  - Slides 2..N-1 — DECOMPOSITION: 1-3 slides per MECE group, developing \
the group's claim with evidence / steps / examples.
  - Slide N (last index) — SYNTHESIS: recap of each group in one sentence, \
reinforce the thesis, point forward (decision / next step / Q&A).

IMAGE_INTENT guidance (committed here, fulfilled by the detail stage):
  - "hero"          slide 1 cover visual. SET THIS for slide 1 unless the \
                    topic is purely abstract/structural.
  - "flowchart"     process / pipeline / state machine.
  - "diagram"       architecture / component relationships / layer stack.
  - "chart"         quantitative comparison / trend.
  - "icon_cluster"  3-6 option grid with icon + short label.
  - "illustration"  spot visual next to text (icon, photo, motif).
  - "none"          pure-text slide (Synthesis recap, section dividers, \
                    dense bullet lists).

Density rule: aim for 40-60% of CONTENT slides (indices 1..N-2) to carry \
a non-"none" image_intent. A deck with zero images looks empty; a deck \
where every slide has an image looks cluttered. The Cover (slide 1) \
SHOULD almost always have image_intent="hero" — only pick "none" if the \
topic is highly abstract (e.g. a philosophy lecture, a math proof).

LAYOUT_INTENT is a 1-3 word tag for the visual structure (e.g. \
{layout_intent_tags}). The detail stage will expand this into a concrete \
layout_hint. Vary layout_intent across consecutive slides. The tag list \
above is already filtered to fit the canvas shape — vertical-stack tags \
on portrait canvases, wide-canvas tags on landscape, symmetric tags on \
square. Stay within the listed vocabulary.

GROUP design (MECE):
  - Mutually Exclusive: no slide belongs to two groups.
  - Collectively Exhaustive: EVERY slide index 0..N-1 MUST appear in \
exactly one group's slide_indices. The tool REJECTS any uncovered \
slide — this is the #1 most common failure, especially for slide 0 \
(Overview) and slide N-1 (Synthesis). Double-check coverage before \
emitting.
  - Group ids: snake_case (e.g. "market_analysis", "product_solution").
  - Recommended: create a dedicated "overview" group whose \
slide_indices covers BOTH slide 0 and slide N-1 — they share the \
"frame the deck" structural role and don't belong to any content \
group. Example for an 8-slide deck, write groups as a list of objects \
with these three fields each:
      group 1: id="overview",  name="Frame",        slide_indices=[0, 7]
      group 2: id="problem",   name="Problem",      slide_indices=[1, 2]
      group 3: id="solution",  name="Our Solution", slide_indices=[3, 4, 5]
      group 4: id="market",    name="Market",       slide_indices=[6]
  - Alternative: if you'd rather not introduce an "overview" group, \
fold slide 0 and slide N-1 into the MOST relevant content group. \
Either way, every slide MUST be covered.

STYLE HINT: "{style_hint}"
TOPIC: "{topic}"

THEME (for color/layout reference): {theme_json}

{final_count_instruction}
"""


SLIDE_DETAIL_GENERATOR_PROMPT = """\
You are filling in the DETAIL for slide {slide_position} of {total_slides} \
on the topic "{topic}". The skeleton stage already committed this slide's \
title, purpose, group, and image_intent — your job is to produce concrete \
key_points + a varied layout_hint + (if image_intent != "none") image specs.

OUTPUT LANGUAGE: Follow the topic's language. If the topic is in Chinese, \
all user-facing text you produce (key_points, layout_hints, image \
descriptions) MUST be in Chinese. If the topic is in English, output \
English. If the topic mixes languages, default to the topic's primary \
language. Do not translate the skeleton's existing title/purpose — keep \
them in whatever language they already are.

CURRENT SLIDE SKELETON (do not change these):
  title:        {skeleton_title}
  purpose:      {skeleton_purpose}
  group:        {skeleton_group}
  image_intent: {skeleton_image_intent}

DECK STRUCTURE (for your awareness — do not re-plan):
  thesis: {deck_thesis}
  groups: {deck_groups_summary}

{previous_slides_block}

OUTPUT: Call `define_slide_detail` with these fields:
  - slide_index: {slide_index}
  - key_points: 2-5 concrete content-bearing sentences (NOT generic filler).
  - layout_hint: concrete free-text description of the slide's visual \
structure. MUST differ from previous slides' layouts listed above.
  - images: 0-3 image specs. The count MUST respect image_intent:
      * image_intent="none"          images=[]
      * image_intent="hero"          images has 1 spec with image_type="hero"
      * image_intent="flowchart"     images has 1 spec with image_type="flowchart"
      * image_intent="diagram"       images has 1 spec with image_type="diagram"
      * image_intent="chart"         images has 1 spec with image_type="chart"
      * image_intent="icon_cluster"  images has 1 spec with image_type="icon_cluster"
      * image_intent="illustration"  images has 1 spec with image_type="illustration" or "hero"

KEY_POINTS QUALITY:
  - Concrete > abstract. "Tesla Model 3 starts at $38,990" beats "good value".
  - Each key_point is ONE complete sentence, not a bullet fragment.
  - 2-5 points; 3 is the sweet spot for content slides.
  - Key_points must support the slide's purpose — not just be topically related.

{layout_diversity_hint}

IMAGE PLANNING (only when image_intent != "none"):
  Pick source_type by SUBJECT — see SOURCE DECISION below. Each image \
spec needs slot_id (snake_case), aspect_ratio, image_type, source_type, \
description, and source_ref (when source_type="web").

SOURCE DECISION (source_type field):

Select the source that matches the actual SUBJECT. Both paths are \
production-ready; the decision is driven by what the image depicts.

  source_type="web"  when the subject is a real, photorealistic thing:
                        - realistic scenes ("modern coffee shop interior")
                        - named products / brands ("Tesla Model 3")
                        - people / portraits / lifestyle
                        - recognizable places / landmarks
                        - textures / materials ("marble surface")
                        - food / physical objects
                      Requires source_ref = a search query (Chinese or \
                      English). On VLM verification failure it cleanly \
                      falls back to svg.

  source_type="svg"  when the image is STRUCTURAL or ABSTRACT:
                        - flowcharts / pipelines / state machines
                        - architecture / component / layer diagrams
                        - bar / line / pie charts with concrete numbers
                        - icon clusters (3-6 icons + short labels)
                        - abstract geometric hero covers

  Rule of thumb: would a stock-photo search capture this better than a \
vector drawing? If yes -> web. If the meaning is in boxes-arrows-numbers \
-> svg. NEVER default to svg when the subject is a real scene, product, \
person, place, brand, or texture.

{user_image_library_block}

THEME (use these EXACT colors/fonts in any inline styles referenced): \
{theme_json}
"""


def _format_user_image_library_block(
    user_image_library: Optional[List[Dict[str, Any]]],
) -> str:
    """Render the user-uploaded library as a force-priority prompt section.

    Returns an empty string when no library is supplied — the prompt
    reads identically to the legacy shape and the LLM behaves as before.
    When non-empty, the LLM is REQUIRED to consume every entry before
    falling back to svg/web for remaining slots.
    """
    if not user_image_library:
        return ""
    lines = [
        "USER-UPLOADED IMAGE LIBRARY (source_type='user_upload'):",
        "  You have a curated library below. These images MUST be placed",
        "  before any svg/web spec — every library entry needs to land on",
        "  a slide. Match each one to the slot whose semantic need is the",
        "  closest fit (don't bunch them all onto one slide; spread across",
        "  hero / illustration slots as the content allows). Only after",
        "  every library image is assigned may remaining slots use",
        "  source_type='svg' or 'web'.",
        "",
    ]
    for entry in user_image_library:
        image_id = entry.get("image_id", "?")
        desc = (entry.get("description") or "").strip() or "(no description)"
        fname = entry.get("original_filename") or ""
        mime = entry.get("mime") or ""
        lines.append(
            f'  - image_id="{image_id}"  {desc}  [{fname} {mime}]'.rstrip()
        )
    lines.append("")
    lines.append("To use one, emit a spec with:")
    lines.append("  source_type=\"user_upload\", source_ref=\"<image_id>\",")
    lines.append('  description="<the library description verbatim>".')
    return "\n".join(lines)


def build_structure_planner_prompt(
    topic: str,
    style_hint: str,
    target_count: Optional[int],
    theme: Dict[str, Any],
    canvas_width_px: int = 1280,
    canvas_height_px: int = 720,
) -> str:
    """Format STRUCTURE_PLANNER_PROMPT for a given topic/target/theme.

    Mirrors build_outline_planner_prompt's count_instruction logic so
    callers can swap one for the other without changes.
    """
    theme_json = json.dumps(theme, ensure_ascii=False, indent=2)
    if target_count is not None:
        count_instruction = f"a {target_count}-slide deck"
        final_count_instruction = (
            f"Plan exactly {target_count} slides arranged as "
            f"Overview + Decomposition + Synthesis. Decide the MECE groups "
            f"and which slides belong to each group. Set image_intent per "
            f"slide based on the content that slide will carry."
        )
    else:
        count_instruction = "a deck — YOU decide the slide count based on content depth"
        final_count_instruction = (
            "Choose the slide count yourself based on the MECE breakdown: "
            "6-10 for a focused topic, 10-15 for multi-faceted, 15-25 only for "
            "dense reference material. Minimum 4 (Overview + 2 groups + Synthesis), "
            "max 30. State your chosen count and the MECE groups in your reasoning "
            "before the tool call."
        )
    return STRUCTURE_PLANNER_PROMPT.format(
        topic=topic,
        style_hint=style_hint,
        count_instruction=count_instruction,
        final_count_instruction=final_count_instruction,
        theme_json=theme_json,
        layout_intent_tags=_layout_intent_tags(canvas_width_px, canvas_height_px),
    )


def _format_previous_slides_block(
    prev_slides: List[Dict[str, Any]],
) -> str:
    """Render a compact summary of already-generated slides for layout diversity.

    The detail generator sees prior slides' titles + layout_hints so it
    can actively avoid repeating them. Also surfaces user_upload
    image_ids already consumed, so the LLM doesn't re-assign a library
    entry that's already been placed on an earlier slide (the library is
    a fixed set — each entry can only land once). Returns an empty-ish
    block when prev_slides is empty (slide_index == 0 case).
    """
    if not prev_slides:
        return (
            "PREVIOUS SLIDES: (this is slide 1 — no prior layouts to avoid)"
        )
    lines = ["PREVIOUS SLIDES (avoid repeating these layouts):"]
    consumed_user_ids: List[str] = []
    for i, s in enumerate(prev_slides):
        title = s.get("title", "(untitled)")
        layout = s.get("layout_hint", "(no layout)")
        img_types = sorted(
            {
                img.get("image_type", "?")
                for img in (s.get("images") or [])
                if isinstance(img, dict)
            }
        )
        img_note = f" [images: {', '.join(img_types)}]" if img_types else ""
        lines.append(f'  slide {i + 1}: "{title}" — layout: {layout}{img_note}')
        for img in (s.get("images") or []):
            if not isinstance(img, dict):
                continue
            if img.get("source_type") == "user_upload":
                ref = img.get("source_ref") or ""
                if ref and ref not in consumed_user_ids:
                    consumed_user_ids.append(ref)
    if consumed_user_ids:
        lines.append("")
        lines.append(
            "Already-placed user library image_ids (do NOT reuse): "
            + ", ".join(consumed_user_ids)
        )
    return "\n".join(lines)


def build_slide_detail_generator_prompt(
    slide_index: int,
    total: int,
    skeleton: Dict[str, Any],
    prev_slides: List[Dict[str, Any]],
    deck_skeleton: Optional[Dict[str, Any]],
    topic: str,
    theme: Dict[str, Any],
    user_image_library: Optional[List[Dict[str, Any]]] = None,
    canvas_width_px: int = 1280,
    canvas_height_px: int = 720,
) -> str:
    """Format SLIDE_DETAIL_GENERATOR_PROMPT for one slide.

    ``skeleton`` is state.outline[slide_index] as written by
    define_skeleton (title / purpose / layout_hint placeholder).
    ``prev_slides`` is state.outline[:slide_index] (already enriched
    with detail by prior Stage 2b iterations).
    ``deck_skeleton`` carries thesis / groups so the detail LLM sees
    the deck-wide context without re-planning it.
    ``user_image_library`` (when non-empty) is injected as a force-
    priority section: the LLM must consume every library entry across
    the deck before falling back to svg/web for remaining slots.
    """
    theme_json = json.dumps(theme, ensure_ascii=False, indent=2)

    # Resolve the slide's group name + image_intent from deck_skeleton.
    group_name = "(unknown)"
    image_intent = "none"
    if deck_skeleton is not None:
        intents = deck_skeleton.get("slide_intents") or []
        if 0 <= slide_index < len(intents):
            intent = intents[slide_index] or {}
            image_intent = intent.get("image_intent", "none")
            gid = intent.get("group_id")
            for g in deck_skeleton.get("groups") or []:
                if g.get("id") == gid:
                    group_name = g.get("name", gid)
                    break

    groups_summary = "; ".join(
        f"{g.get('id')}: {g.get('name')} (slides {g.get('slide_indices')})"
        for g in (deck_skeleton or {}).get("groups") or []
    ) or "(no groups)"

    deck_thesis = (deck_skeleton or {}).get("thesis", "(no thesis)")

    return SLIDE_DETAIL_GENERATOR_PROMPT.format(
        slide_position=slide_index + 1,
        total_slides=total,
        slide_index=slide_index,
        topic=topic,
        skeleton_title=skeleton.get("title", ""),
        skeleton_purpose=skeleton.get("purpose", ""),
        skeleton_group=group_name,
        skeleton_image_intent=image_intent,
        deck_thesis=deck_thesis,
        deck_groups_summary=groups_summary,
        previous_slides_block=_format_previous_slides_block(prev_slides),
        theme_json=theme_json,
        user_image_library_block=_format_user_image_library_block(user_image_library),
        layout_diversity_hint=_layout_diversity_hint(canvas_width_px, canvas_height_px),
    )


def build_slide_builder_prompt(
    theme: Dict[str, Any],
    outline: Dict[str, Any],
    slide_index: int,
    total_count: int,
    slide_images: Optional[Dict[str, Dict[str, Any]]] = None,
    canvas_width_px: int = 1280,
    canvas_height_px: int = 720,
) -> str:
    theme_json = json.dumps(theme, ensure_ascii=False, indent=2)
    key_points = outline.get("key_points", [])
    if isinstance(key_points, list):
        key_points_formatted = "\n".join(f"  - {p}" for p in key_points)
    else:
        key_points_formatted = f"  - {key_points}"
    # Reverse-lookup image_type per slot_id from the outline. state.slide_images
    # values are typed payloads ({"type": "svg"|"image", "data": ...}); the
    # image_type isn't stored on the payload because it already lives on the
    # outline spec and duplicating it invites drift.
    image_types = {
        img["slot_id"]: img.get("image_type", "illustration")
        for img in outline.get("images", [])
        if isinstance(img, dict) and "slot_id" in img
    }
    images_block = _format_images_block(slide_images or {}, image_types)
    return SLIDE_BUILDER_PROMPT.format(
        house_rules=HOUSE_RULES,
        theme_json=theme_json,
        slide_index=slide_index,
        total_count=total_count,
        title=outline.get("title", ""),
        purpose=outline.get("purpose", ""),
        layout_hint=outline.get("layout_hint", ""),
        key_points_formatted=key_points_formatted,
        canvas_width_px=canvas_width_px,
        canvas_height_px=canvas_height_px,
        html_guide=build_html_authoring_guide(canvas_width_px, canvas_height_px),
        images_block=images_block,
    )


def build_slide_builder_incremental_prompt(
    *,
    current_html: str,
    new_outline: Dict[str, Any],
    old_outline: Optional[Dict[str, Any]],
    theme_before: Optional[Dict[str, Any]] = None,
    theme_after: Optional[Dict[str, Any]] = None,
    slide_index: int = 0,
    total_count: int = 1,
    slide_images: Optional[Dict[str, Dict[str, Any]]] = None,
    canvas_width_px: int = 1280,
    canvas_height_px: int = 720,
) -> str:
    """Build the incremental slide-builder prompt for review regenerate.

    The user clicked "Update this slide" on a stale mark after editing
    upstream (outline or theme semantic fields). We show the LLM the
    current HTML + before/after upstream state and ask it to apply only
    the changes implied by the diff — preserving any manual user edits.

    ``old_outline`` and ``theme_before`` come from the stale mark's
    ``context_snapshot`` (captured at edit time). If either is missing
    (older stale mark, or theme didn't change), we fall back gracefully:

      - ``old_outline=None`` → only show ``new_outline`` (the LLM gets
        less context but can still regen against current upstream).
      - ``theme_before`` / ``theme_after`` both None → omit the theme
        change section entirely.

    ``slide_images`` is the current ``state.slide_images[idx]`` — kept
    so any pre-generated SVG/image placeholders stay accessible in
    case the upstream change needs to reposition them.
    """
    # Use new_outline's image specs (if any) for image_type lookup. Old
    # outline images don't matter for incremental — we render current
    # state and let the LLM preserve existing <img> wrappers.
    image_types = {
        img["slot_id"]: img.get("image_type", "illustration")
        for img in new_outline.get("images", [])
        if isinstance(img, dict) and "slot_id" in img
    }
    images_block = _format_images_block(slide_images or {}, image_types)
    theme_json = json.dumps(theme_after or {}, ensure_ascii=False, indent=2)
    key_points = new_outline.get("key_points", [])
    if isinstance(key_points, list):
        key_points_formatted = "\n".join(f"  - {p}" for p in key_points)
    else:
        key_points_formatted = f"  - {key_points}"

    # Build the optional theme-change section: included only if both
    # before/after were snapshotted and they differ on semantic fields.
    optional_theme_change_section = ""
    if (
        theme_before is not None
        and theme_after is not None
        and theme_before != theme_after
    ):
        before_json = json.dumps(theme_before, ensure_ascii=False, indent=2)
        after_json = json.dumps(theme_after, ensure_ascii=False, indent=2)
        optional_theme_change_section = (
            f"- Theme (semantic fields changed — affects layout decisions):\n"
            f"  - Before: {before_json}\n"
            f"  - After:  {after_json}"
        )
    old_outline_json = json.dumps(old_outline or {}, ensure_ascii=False, indent=2)
    new_outline_json = json.dumps(new_outline, ensure_ascii=False, indent=2)
    return SLIDE_BUILDER_INCREMENTAL_PROMPT.format(
        current_html=current_html,
        old_outline_json=old_outline_json,
        new_outline_json=new_outline_json,
        optional_theme_change_section=optional_theme_change_section,
        house_rules=HOUSE_RULES,
        images_block=images_block,
        theme_json=theme_json,
        slide_index=slide_index,
        total_count=total_count,
        title=new_outline.get("title", ""),
        purpose=new_outline.get("purpose", ""),
        layout_hint=new_outline.get("layout_hint", ""),
        key_points_formatted=key_points_formatted,
        html_guide=build_html_authoring_guide(canvas_width_px, canvas_height_px),
    )


def _format_images_block(
    slide_images: Dict[str, Dict[str, Any]],
    image_types: Optional[Dict[str, str]] = None,
) -> str:
    """Render the PRE-GENERATED IMAGES section for the slide-builder prompt.

    Empty input → a one-line note that there are no pre-generated images,
    so the prompt slot is never blank (which would look like a template bug).

    Each payload in ``slide_images`` is a typed dict (see
    ``AgentState.slide_images`` docstring for the full shapes):
      - svg_file: persisted to disk; renders as a short <img> placeholder
        with class "shuttleslide-svg-placeholder". The slide-builder
        embeds the placeholder verbatim; html_to_pptx inlines the SVG
        back during the Playwright pass.
      - image_file: persisted raster (web/screenshot); renders as a short
        <img src> tag.
      - svg (legacy inline): renders raw markup verbatim. Kept as fallback.
      - image (legacy base64): renders a data-URL <img> tag.

    ``image_types`` (slot_id → type) is reverse-looked up from the outline
    by the caller; it lets the slide-builder pick a placement strategy
    per type (see HTML AUTHORING GUIDE's SVG section). Missing entries
    default to "illustration".
    """
    image_types = image_types or {}
    if not slide_images:
        return (
            "PRE-GENERATED IMAGES: none for this slide. Do NOT author any "
            "<svg> yourself."
        )
    lines = [
        "PRE-GENERATED IMAGES (use these EXACT snippets inside your HTML,",
        "each wrapped in a positioning container like",
        "`<div style=\"position:absolute; left:..; top:..; width:..\">...</div>`).",
        "Do NOT modify the markup. Do NOT author your own <svg> or <img>.",
        "",
        f"BUDGET RULES (total HTML cap is {_HTML_BUDGET} chars — each item's "
        "size is shown below):",
        "  - ALL pre-generated images (svg_file + image_file) are",
        "    trivially small (~150 chars each). Treat them as free; the",
        "    actual image bytes live on disk under svgs/ and images/,",
        "    NOT in the HTML. Always include every declared image. Never",
        "    omit one for budget reasons — there is no budget pressure",
        "    from images anymore.",
        "  - svg_file placeholders are inlined back into real <svg> markup",
        "    by html_to_pptx before Playwright renders the slide, so the",
        "    PPTX output gets the full vector art. The LLM never has to",
        "    copy SVG markup into the HTML.",
        "",
        "Placement strategy is driven by image_type — see the HTML",
        "AUTHORING GUIDE's image section for the per-type rules. In short:",
        "  - hero         → ambient bg (wrapper opacity ≤ 0.35) OR right-half",
        "  - flowchart / diagram / chart → own region, no text overlap",
        "  - illustration → standalone spot, never full-bleed",
        "  - icon_cluster → corner accent or inside a card",
        "",
    ]
    for slot_id, payload in slide_images.items():
        img_type = image_types.get(slot_id, "illustration")
        # Default to svg_file (the new production shape). Legacy "svg"
        # payload (inline) is still handled below as a fallback.
        payload_type = (
            payload.get("type", "svg_file") if isinstance(payload, dict) else "svg_file"
        )
        if payload_type == "svg_file":
            # Placeholder reference to svgs/slide_N_X.svg. The actual SVG
            # markup never enters the LLM context or the HTML cap.
            rel_path = payload.get("path", "")
            description = (
                payload.get("description", "")
                if isinstance(payload, dict) else ""
            )
            payload_img_type = (
                payload.get("image_type", img_type)
                if isinstance(payload, dict) else img_type
            )
            snippet = (
                f'<img data-slot="{slot_id}" '
                f'src="{rel_path}" '
                f'class="shuttleslide-svg-placeholder" '
                f'data-image-type="{_escape_attr(payload_img_type)}" '
                f'data-description="{_escape_attr(description)}" '
                f'style="width:100%;height:100%;" />'
            )
        elif payload_type == "image_file":
            # File-externalized raster (web photo / screenshot). Carries
            # the same description/image_type attrs so downstream LLMs
            # reading the HTML can understand what each image depicts
            # without parsing the pixels.
            rel_path = payload.get("path", "")
            description = (
                payload.get("description", "")
                if isinstance(payload, dict) else ""
            )
            payload_img_type = (
                payload.get("image_type", img_type)
                if isinstance(payload, dict) else img_type
            )
            snippet = (
                f'<img data-slot="{slot_id}" '
                f'src="{rel_path}" '
                f'data-image-type="{_escape_attr(payload_img_type)}" '
                f'data-description="{_escape_attr(description)}" '
                f'style="width:100%;height:100%;object-fit:cover;" />'
            )
        elif payload_type == "image":
            # Legacy inlined-base64 path. Kept for backward compatibility
            # with tests / older state shapes. Production web acquisitions
            # now go through "image_file".
            data = payload.get("data", "") if isinstance(payload, dict) else str(payload)
            mime = payload.get("mime", "image/jpeg") if isinstance(payload, dict) else "image/jpeg"
            snippet = (
                f'<img data-slot="{slot_id}" '
                f'src="data:{mime};base64,{data}" '
                f'style="width:100%;height:100%;object-fit:cover;" />'
            )
        else:
            # Legacy inline-svg payload. Renders raw markup verbatim.
            snippet = payload.get("data", "") if isinstance(payload, dict) else str(payload)
        size = len(snippet)
        pct = round(100 * size / _HTML_BUDGET)
        lines.append(
            f"--- slot_id: {slot_id} | image_type: {img_type} "
            f"| source: {payload_type} | size: {size} chars "
            f"({pct}% of {_HTML_BUDGET}-char HTML budget) ---"
        )
        lines.append(snippet)
        lines.append(f"--- end slot_id: {slot_id} ---")
        lines.append("")
    return "\n".join(lines)


def _escape_attr(value: str) -> str:
    """Escape a string for use as an HTML attribute value.

    Minimal escape — handles the four chars that break attribute parsing
    (quotes, ampersand, angle brackets). Sufficient for natural-language
    descriptions coming from the LLM outline; we don't need full HTML5
    sanitization here because these values are authoring-time constants,
    not user input.
    """
    if not value:
        return ""
    return (
        value.replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_svg_generator_prompt(
    spec: Dict[str, Any],
    theme: Dict[str, Any],
) -> str:
    """Build the prompt for one SVG generation call.

    `spec` is one entry from outline[i]["images"]: must contain slot_id,
    aspect_ratio, image_type, description (validated upstream by
    define_outline).
    """
    aspect = spec["aspect_ratio"]
    viewbox = _ASPECT_VIEWBOX.get(aspect)
    if viewbox is None:
        raise ValueError(
            f"unknown aspect_ratio {aspect!r}; expected one of "
            f"{list(_ASPECT_VIEWBOX)}"
        )
    theme_colors = {
        k: v
        for k, v in theme.items()
        if isinstance(v, str) and k.endswith("_color")
    }
    theme_colors_json = json.dumps(theme_colors, ensure_ascii=False, indent=2)
    return SVG_GENERATOR_PROMPT.format(
        slot_id=spec["slot_id"],
        aspect_ratio=aspect,
        image_type=spec["image_type"],
        description=spec["description"],
        viewbox=viewbox,
        theme_colors_json=theme_colors_json,
    )
