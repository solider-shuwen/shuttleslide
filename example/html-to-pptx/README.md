# HTML → PPTX example

A hand-written single-slide HTML that demonstrates the Slide-HTML subset Shuttleslide accepts on the HTML → PPTX side. The page uses absolute positioning and explicit pixel sizes, which the rule extractor reads via Playwright and converts to percentage-based PPTX coordinates.

## Files

| File | Role |
|---|---|
| `sample.html` | Hand-authored Slide-HTML input |
| `sample.pptx` | Output produced by `slidecraft to-pptx` |

## What the example exercises

- Absolute-positioned title and subtitle text boxes
- Two card divs with backgrounds, borders, and shadows
- A footer line

After conversion, open `sample.pptx` in PowerPoint — every element is a natively editable shape, not a flattened image. Click the title to change its text; drag the cards to reposition them.

## Reproduce

```bash
slidecraft to-pptx sample.html -o sample.pptx
```

## Writing your own Slide-HTML

The Slide-HTML subset intentionally avoids general CSS layout. The reliable patterns are:

- One `.slide` element per slide, with explicit `width` / `height` in pixels
- Children positioned with `position: absolute` and `top` / `left` / `width` / `height`
- Text styling via standard CSS (`font-size`, `font-weight`, `color`, `text-align`)
- Backgrounds via `background-color`, `background-image`, or `linear-gradient(...)`
- Optional SVG shapes (rendered as native DrawingML paths)

For the full vocabulary and what round-trips losslessly, see [../../docs/round-trip.md](../../docs/round-trip.md).
