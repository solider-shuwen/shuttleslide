# Examples

End-to-end, reproducible samples showing each of Shuttleslide's three capabilities. Each subdirectory contains the input, the output, and the exact command that produced it.

| Subdirectory | Direction | What it shows |
|---|---|---|
| [pptx-to-html/](pptx-to-html/) | `.pptx` → `.html` | A 3-slide deck converted to interactive slideshow HTML |
| [html-to-pptx/](html-to-pptx/) | `.html` → `.pptx` | A hand-written Slide-HTML converted to native PPTX |
| [ai-generated/](ai-generated/) | topic → `.html` deck | One slide produced by `slidecraft generate` via an LLM |

## How to regenerate

```bash
# Install once
pip install shuttleslide
python -m playwright install chromium

# PPTX → HTML
slidecraft to-html pptx-to-html/sample.pptx -o pptx-to-html/sample.html

# HTML → PPTX
slidecraft to-pptx html-to-pptx/sample.html -o html-to-pptx/sample.pptx

# AI generation (requires shuttleslide[ai] and an OpenAI-compatible endpoint)
pip install shuttleslide[ai]
slidecraft generate "Your topic here" \
    --api-base $SHUTTLESLIDE_API_BASE \
    --api-key  $SHUTTLESLIDE_API_KEY \
    --model    $SHUTTLESLIDE_MODEL \
    -o         ai-generated/
```

## Adding screenshots

Each subdirectory has a `preview.png` slot. Drop a screenshot of the rendered HTML or PPTX there and the top-level README will display it inline.
