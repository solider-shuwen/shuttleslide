# Round-trip conversion: how Shuttleslide preserves formatting

Round-trip — the ability to take a `.pptx`, convert it to HTML, then convert the HTML back to a `.pptx` without losing the original layout and styling — is Shuttleslide's central differentiator. This document explains how it works, what's covered today, and how to write HTML that round-trips well.

## The core idea: `data-pptx-*` attributes

PPTX → HTML conversion doesn't just produce visual HTML. Every converted element is annotated with a set of `data-pptx-*` attributes that record the *original* PPTX properties — coordinates, font, shape type, table position, z-order, and so on.

When HTML → PPTX runs, those attributes are read first. The renderer doesn't have to *guess* that a `<div>` was originally a `roundedRect` auto-shape, or that a `<td>` was row 2 column 3 of the source table — the attributes say so explicitly. The result is a PPTX that closely matches the original, not a CSS-flavored reinterpretation.

### Attributes today

| Category | Attributes |
|---|---|
| Common | `data-pptx-element-type`, `data-pptx-left`, `data-pptx-top`, `data-pptx-width`, `data-pptx-height`, `data-pptx-z-order` |
| Text | `data-pptx-level`, `data-pptx-font-name`, `data-pptx-font-size`, `data-pptx-bold`, `data-pptx-italic`, `data-pptx-color`, `data-pptx-is-title` |
| Shape | `data-pptx-shape-type`, `data-pptx-fill-color`, `data-pptx-line-color`, `data-pptx-shape-text` |
| Table | `data-pptx-row`, `data-pptx-col` |
| Slide | `data-pptx-slide-number`, `data-pptx-layout` |
| SVG round-trip | `data-pptx-prst`, `data-pptx-adj1`, `data-pptx-adj2`, `data-pptx-pattern`, `data-pptx-fg`, `data-pptx-bg` |

The list grows over time — the goal is for every lossy CSS approximation to be backed by an explicit round-trip attribute.

## The three stages

```
   ┌──────────────┐  emit        ┌──────────────┐  preserve    ┌──────────────┐  consume    ┌──────────────┐
   │  PPTXParser  │ ───────────▶ │  HTML +      │ ───────────▶ │ RuleSlide    │ ─────────▶ │  PPTX with   │
   │  + converters│  attributes  │  data-pptx-* │  any edits   │ Transformer  │  attributes │  native      │
   │              │              │              │              │ + Renderer   │             │  DrawingML   │
   └──────────────┘              └──────────────┘              └──────────────┘             └──────────────┘
```

### 1. Emit

The PPTX parser walks every slide and produces structured `SlideElement` objects. The converters (`TextConverter`, `TableConverter`, `ImageConverter`, `ShapeConverter`) render each one as HTML **and** stamp the source properties onto the element as `data-pptx-*` attributes.

The source code: [`src/shuttleslide/pptx_to_html/converters/`](../src/shuttleslide/pptx_to_html/converters/).

### 2. Preserve

You can edit the HTML freely — change text content, fix typos, swap an image, restyle a single card. As long as you don't delete the `data-pptx-*` attributes on the elements you want to round-trip, the next stage will pick them up.

Edits that survive round-trip well:

- Updating text inside an existing element
- Replacing image `src`
- Adjusting `font-size` or `color` on a styled run
- Reordering bullet items

Edits that won't survive cleanly (yet):

- Adding new elements that have no source metadata (they'll be classified by rules, not matched)
- Changing element type (a text box becoming a table)
- Restructuring the slide's grid

### 3. Consume

`RuleSlideTransformer` runs the HTML through Playwright to get computed layout data, then for each element prefers the `data-pptx-*` attribute value over the CSS-computed value when emitting the `PPT-DSL` JSON. `PPTXRenderer` then writes a `.pptx` whose shapes carry those original properties.

The vendored `svg_to_pptx` engine (from [ppt-master](https://github.com/hugohe3/ppt-master)) converts any inline SVG into native DrawingML paths — meaning vector shapes round-trip as editable PowerPoint shapes, not as flat images.

## PPT-aware typography

HTML and PowerPoint measure text differently. A direct value-for-value mapping produces visibly wrong output. Shuttleslide applies empirically calibrated adjustments (measured via Playwright against a 45-case test set, see `tests/test_paragraph_spacing_accuracy.py`).

### Line height

- **PPT model**: baseline-to-baseline distance; `spcPct val="90000"` = 90% of single spacing.
- **CSS model**: line-box height (top of line to bottom of line).
- **Adjustment**: `LINE_HEIGHT_ADJUSTMENT = 0.92` — PPT spacing is multiplied by this factor.

### Paragraph spacing (before / after)

- **PPT model**: `spcPts val="1000"` = 10pt (1/100 point); `spcPts val="500"` = 5pt.
- **CSS model**: `margin-top` / `margin-bottom`.
- **Measured ratios** (PPT → HTML):

  | PPT spacing | HTML actual | Ratio |
  |---|---|---|
  | 0pt (default) | 9.62px | — |
  | 5pt | 19.47px | 2.92× |
  | 10pt | 27.20px | 2.04× |
  | 20pt | 41.60px | 1.56× |

  Global average: HTML renders at **186%** of the PPT value.

- **Adjustments**:
  - `PARAGRAPH_SPACING_RATIO = 0.538` — multiply all non-zero PPT spacing by this.
  - `PARAGRAPH_SPACING_ADJUSTMENT = -0.2em` — for PPT's `0pt`, apply a negative margin for tightness.
  - **First paragraph exception**: never apply negative `margin-top` on the first paragraph of a text element, otherwise the top of the text gets clipped by `overflow: hidden`.

### Tuning

If spacing still looks off:

- **Lines too loose**: decrease `LINE_HEIGHT_ADJUSTMENT` (try 0.85–0.90).
- **Lines too tight**: increase it (try 0.93–0.98).
- **Paragraph gaps too large**: decrease `PARAGRAPH_SPACING_RATIO` (try 0.4–0.5).
- **Paragraph gaps too small**: increase it (try 0.55–0.65).

The calibration constants live in [`src/shuttleslide/pptx_to_html/converters/text.py`](../src/shuttleslide/pptx_to_html/converters/text.py). Re-run `python tests/test_paragraph_spacing_accuracy.py` to re-measure.

## Font shrink-on-overflow (`normAutofit`)

PowerPoint's `<a:normAutofit fontScale>` shrinks text until it fits the shape's declared height. Shuttleslide mirrors this by measuring the rendered HTML in Playwright and applying a `font-size` scale when text would otherwise overflow.

CLI toggle: `--no-shrink` disables this measurement step (faster, but text may overlap on tight shapes).

Python API: pass a `PlaywrightTextMeasurer` to the layout engine (see [python-api.md](python-api.md#text-measurement-optional-recommended)).

## Writing round-trip-friendly HTML

If you're hand-authoring HTML for the HTML → PPTX direction and want the output to be precisely editable:

- **Use absolute positioning** with explicit pixel values for `top`, `left`, `width`, `height`. The renderer converts pixels to PPT EMU via the slide's declared dimensions.
- **Keep one slide per `.slide` element** with explicit `width` and `height`. 1280×720 maps cleanly to a standard 16:9 deck.
- **Avoid CSS layout black holes**: no flexbox grids, no `position: sticky`, no `aspect-ratio: ...` for primary layout. Use them decoratively if at all.
- **Use SVG for vector shapes** — they convert to native DrawingML paths. Use raster images (`<img>`) only for photos.
- **For tables, use real `<table>` markup** with `<tr>` and `<td>`. Div-flex "div-tables" work too via spatial grid detection, but real tables round-trip more reliably.
- **Embed fonts** if you need pixel-identical rendering: `embed_fonts(pptx_path, font_bytes)` writes TrueType data into the PPTX.

## Current round-trip status

| Element type | Emit | Consume | Notes |
|---|:---:|:---:|---|
| Text boxes | ✅ | ✅ | Including bullets, levels, font styling |
| Tables | ✅ | ✅ | Cell merging (colspan/rowspan) is partial |
| Images | ✅ | ✅ | Embedded as base64 or external file |
| Auto-shapes | ✅ | ✅ | Via SVG → DrawingML |
| Charts | ⚠️ partial | ⚠️ partial | Detected; rendering under development |
| SmartArt | ⚠️ partial | ❌ | Detected as grouped shapes |
| Animations | ✅ (HTML side) | 🚧 | Slide-level entrance animations supported |

The direction is toward closing every row of this table.
