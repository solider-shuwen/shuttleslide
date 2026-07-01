# Python API Reference

Shuttleslide exposes three top-level modules, one per capability:

| Module | Direction | Use case |
|---|---|---|
| [`shuttleslide.pptx_to_html`](#shuttleslidepptx_to_html) | `.pptx` → `.html` | Convert existing decks |
| [`shuttleslide.html_to_pptx`](#shuttleslidehtml_to_pptx) | `.html` → `.pptx` | Build decks from HTML |
| [`shuttleslide.agent`](#shuttleslideagent) | topic → deck | Generate slides via an LLM |

All APIs are also reachable via the `slidecraft` CLI — see [cli-reference.md](cli-reference.md).

---

## `shuttleslide.pptx_to_html`

Parse a PowerPoint file and render it as HTML using one of three layout engines.

### Public surface

```python
from shuttleslide.pptx_to_html import (
    PPTXParser,
    SlideElement, TextElement, TableElement, ImageElement,
    ShapeElement, GroupElement, ParsedSlide,
    TextConverter, TableConverter, ImageConverter, ShapeConverter,
    FlowLayout, PPTLayout,
)
```

### Quick example

```python
from pathlib import Path
from shuttleslide.pptx_to_html import PPTXParser, FlowLayout

parser = PPTXParser("deck.pptx")
slides = parser.parse()

# Inspect parsed structure
for slide in slides:
    print(f"Slide {slide.slide_number}: {len(slide.elements)} elements")

# Render
html = FlowLayout().convert(slides)
Path("out.html").write_text(html, encoding="utf-8")
```

### Core types

#### `PPTXParser(path: str | Path)`

The entry point. `parse()` returns a list of `ParsedSlide` objects. `get_presentation_metadata()` returns title, author, dimensions.

```python
parser = PPTXParser("deck.pptx")
slides   = parser.parse()
metadata = parser.get_presentation_metadata()
# metadata = {"title": ..., "author": ..., "slide_width": ..., "slide_height": ...}
```

#### Parsed element types

Each shape on a slide becomes one of:

- `TextElement` — paragraphs, runs, bullets, font styling
- `TableElement` — rows, cells, per-cell styling
- `ImageElement` — embedded image bytes + crop/fill info
- `ShapeElement` — auto-shapes, paths, gradients
- `GroupElement` — nested group of any of the above

All inherit from `SlideElement` and carry position (`left`, `top`, `width`, `height` in pixels after EMU conversion) plus the source-XML metadata that round-trips back to PPTX.

### Layout engines

| Class | Mode | Use case |
|---|---|---|
| `FlowLayout(output_dir=None, measurer=None)` | flow | Semantic scrollable page |
| `PPTLayout(use_base64=False, output_dir=None, measurer=None)` | pptview | Editor-style layout |
| `SlideshowLayout(enable_animations=True, use_base64=False, output_dir=None, measurer=None)` | slideshow | Interactive presentation |

Each layout's `.convert(slides: list[ParsedSlide]) -> str` returns a complete HTML document.

### Text measurement (optional, recommended)

Passing a `PlaywrightTextMeasurer` enables PPT-faithful font-shrink-on-overflow — text shapes that would exceed the PPT-declared height are auto-scaled, mirroring `<a:normAutofit fontScale>`.

```python
from shuttleslide.pptx_to_html.text_measure import PlaywrightTextMeasurer

measurer = PlaywrightTextMeasurer()
measurer.start()
try:
    html = FlowLayout(measurer=measurer).convert(slides)
finally:
    measurer.close()
```

---

## `shuttleslide.html_to_pptx`

Convert HTML back to PPTX via a three-stage pipeline: Playwright extraction → rule-based classification → python-pptx rendering.

### Public surface

```python
from shuttleslide.html_to_pptx import (
    PresentationDSL, SlideDSL, ThemeDef,
    load_presentation, dump_presentation,
    PPTXRenderer, RuleSlideTransformer,
    ImageCache, analyze_html, BrowserManager,
    parse_css_font_family, fetch_text_font_bytes, embed_fonts,
)
```

### Quick example

```python
import asyncio
from pathlib import Path
from shuttleslide.html_to_pptx import RuleSlideTransformer, PPTXRenderer

async def convert(html_path: str, pptx_path: str) -> None:
    html = Path(html_path).read_text(encoding="utf-8")
    base_dir = Path(html_path).parent

    transformer = RuleSlideTransformer()
    dsl = await transformer.transform_html(html, base_dir=base_dir)

    PPTXRenderer(base_dir=base_dir).render(dsl, pptx_path)

asyncio.run(convert("slides.html", "out.pptx"))
```

### Pipeline stages

1. **`analyze_html(html, base_dir)`** — launches headless Chromium, loads the HTML, returns computed layout data (positions, sizes, styles, text content) for every visible element.
2. **`RuleSlideTransformer.transform_html(html, base_dir, verbose=False)`** — runs `analyze_html`, then applies the rule-based classifier to produce a `PresentationDSL`.
3. **`PPTXRenderer(base_dir).render(dsl, output_path)`** — writes a `.pptx` with native DrawingML shapes, embedded fonts, and SVG→path conversion.

### DSL schema

`PresentationDSL` is a dataclass tree. Inspect or modify it between stages:

```python
from shuttleslide.html_to_pptx import load_presentation, dump_presentation

dsl = load_presentation(json.loads(Path("deck.json").read_text()))
for slide in dsl.slides:
    print(f"Slide {slide.index}: {len(slide.elements)} elements")
    for el in slide.elements:
        print(f"  - {el.type} at ({el.position.x_pct:.2%}, {el.position.y_pct:.2%})")

# Persist back
Path("modified.json").write_text(dump_presentation(dsl))
```

### Customization entry points

- **Per-element rules** — subclass `RuleSlideTransformer` and override `classify_element` / `transform_element`.
- **SVG handling** — `src/shuttleslide/_vendored/svg_to_pptx/` (vendored from [ppt-master](https://github.com/hugohe3/ppt-master)) handles SVG → DrawingML conversion. Override the entry call if you need custom SVG routing.
- **Font embedding** — `embed_fonts(pptx_path, font_bytes_dict)` inserts TrueType fonts so the deck renders identically without the fonts installed.

---

## `shuttleslide.agent`

LLM-driven agent pipeline that turns a topic into a multi-slide HTML deck. Requires the `[ai]` install extra.

### Public surface

```python
from shuttleslide.agent import (
    AgentConfig, AgentOrchestrator, AgentState,
    generate_slides, OrchestratorResult,
    LLMClient, LLMResponse, LLMResponseEvent, ToolCall,
    SlideHTMLRenderer,
    ToolRegistry, ToolResult, get_default_registry,
    format_event, make_file_logger, make_jsonl_logger, print_llm_response,
)
```

### Quick example

```python
import asyncio
from shuttleslide.agent import generate_slides

async def main():
    result = await generate_slides(
        topic="Introduction to Machine Learning",
        style_hint="business",
        output_dir="tmp/gen_output/",
    )
    print(f"Generated {len(result.html_paths)} slides")

asyncio.run(main())
```

### Configuration

`AgentConfig` controls the LLM endpoint, model, style, slide count, and output location. Construct it directly or use `from_env()`:

```python
from shuttleslide.agent import AgentConfig

config = AgentConfig(
    api_base="https://open.bigmodel.cn/api/paas/v4",
    api_key="...",
    model="glm-4.7",
    topic="Introduction to Machine Learning",
    style_hint="business",
    target_slide_count=10,
    temperature=0.7,
    output_dir="tmp/gen_output/",
)
config.validate()
```

### Pipeline stages

1. **Theme designer** — `define_theme` tool produces fonts, colors, gradients (1 LLM call).
2. **Outline planner** — `define_outline` tool produces the slide list (1 LLM call).
3. **Slide builder** — element-by-element tools build each slide (N calls).
4. **HTML renderer** — `SlideHTMLRenderer` deterministically renders structured data via Jinja2 (no LLM).

### Streaming events

The orchestrator emits progress events you can subscribe to:

```python
from shuttleslide.agent import AgentOrchestrator, format_event

orch = AgentOrchestrator(config)
async for event in orch.run_streaming():
    print(format_event(event))
```

`make_file_logger(path)` / `make_jsonl_logger(path)` are off-the-shelf loggers for debugging or UIs.

### Custom tools

The default tool registry covers outline, slide, SVG, and theme operations. Extend it by registering a function decorated with `@tool`:

```python
from shuttleslide.agent.tools.registry import tool, get_default_registry

@tool(name="my_custom_tool", description="Does something custom")
def my_tool(state, **kwargs):
    ...
```

See `src/shuttleslide/agent/tools/` for examples.

---

## Worked example: full round-trip

```python
import asyncio
from pathlib import Path
from shuttleslide.pptx_to_html import PPTXParser, FlowLayout
from shuttleslide.html_to_pptx import RuleSlideTransformer, PPTXRenderer

async def round_trip(pptx_in: str, html_path: str, pptx_out: str):
    # 1. PPTX → HTML
    slides = PPTXParser(pptx_in).parse()
    html = FlowLayout().convert(slides)
    Path(html_path).write_text(html, encoding="utf-8")

    # 2. HTML → PPTX
    dsl = await RuleSlideTransformer().transform_html(
        html, base_dir=Path(html_path).parent
    )
    PPTXRenderer(base_dir=Path(html_path).parent).render(dsl, pptx_out)
    print(f"Round-trip: {pptx_in} → {html_path} → {pptx_out}")

asyncio.run(round_trip("in.pptx", "middle.html", "out.pptx"))
```
