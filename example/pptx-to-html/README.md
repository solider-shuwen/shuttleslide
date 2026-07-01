# PPTX → HTML example

A minimal 3-slide deck that exercises the common PPTX features Shuttleslide handles:

1. **Title slide** — large bold title + subtitle
2. **Bulleted content** — multi-level bullets with size differentiation
3. **Table** — 3×3 grid with a styled header row

## Files

| File | Role |
|---|---|
| `sample.pptx` | Input (built by [_build_sample_pptx.py](../_build_sample_pptx.py)) |
| `sample.html` | Output produced by `slidecraft to-html` |

## Reproduce

```bash
# Rebuild the source PPTX (only needed if you change the script)
python ../_build_sample_pptx.py

# Convert
slidecraft to-html sample.pptx -o sample.html

# Or with a different layout
slidecraft to-html sample.pptx -o sample-flow.html --layout flow
slidecraft to-html sample.pptx -o sample-pptview.html --layout pptview
```

Open `sample.html` in a browser to see the interactive slideshow with keyboard navigation (← → to move, Space for next).
