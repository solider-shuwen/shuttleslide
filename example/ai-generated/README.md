# AI-generated example: "How Shuttleslide Works"

A 6-slide deck about Shuttleslide itself, generated end-to-end by `slidecraft generate` working against an OpenAI-compatible LLM endpoint. The deck is the project dogfooding its own generation pipeline.

## Files

| File | Role |
|---|---|
| `1.html` … `6.html` | The six generated slides. Open any in a browser to view. |
| `svgs/` | SVG assets the LLM chose to embed (referenced by the HTML via `<img src="svgs/...">`). |
| `presentation.json` | The intermediate PPT-DSL JSON emitted by the slide-builder stage. Useful for inspecting what the rule extractor would consume. |

## Slide outline

| # | Title | Visual |
|---|---|---|
| 1 | The Shuttleslide Round Trip | Hero SVG: bidirectional loop |
| 2 | PPTX → HTML pipeline | Flowchart SVG |
| 3 | HTML → PPTX pipeline | (typography-driven) |
| 4 | `data-pptx-*` metadata | Diagram SVG |
| 5 | Fidelity story | (data-driven) |
| 6 | Closing / call to action | (typography-driven) |

## How it was produced

```bash
# Requires: pip install shuttleslide[ai]  and  an OpenAI-compatible endpoint
slidecraft generate \
    "How Shuttleslide Works: The Round-Trip Story — bidirectional PPTX and HTML conversion with metadata preservation" \
    --style tech \
    --slides 6 \
    -o .
```

The pipeline runs four stages:

1. **Theme designer** — picks fonts, colors, gradients for the `tech` style
2. **Outline planner** — breaks the topic into 6 slides
3. **Slide builder** — emits structured element data per slide (tool-call driven, 1 LLM call per slide)
4. **HTML renderer** — deterministically renders the structured data with Jinja2 (no LLM)

`svgs/` and `presentation.json` are kept here as reference output so you can see how the pipeline is structured.

## Notes

- The HTML loads Tailwind CSS, Google Fonts, and Material Icons from CDNs. To view offline, run `slidecraft warm-cache` first.
- Each `N.html` is a self-contained slide — open them individually, or stack them in a slideshow wrapper.
- To produce your own deck on a different topic, change the topic string and `--style` (try `business`, `editorial`, `cute`, `anime`).

## Reproduce

```bash
pip install shuttleslide[ai]
export SHUTTLESLIDE_API_BASE=...
export SHUTTLESLIDE_API_KEY=...
export SHUTTLESLIDE_MODEL=...

slidecraft generate "Your topic" --style tech --slides 6 -o ./my-deck/
```
