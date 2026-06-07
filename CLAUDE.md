# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Vision

**Shuttleslide** is a Python library for bidirectional conversion between PowerPoint (PPTX) files and HTML, with a focus on round-trip format preservation. This is a differentiator in the market - no existing tools reliably maintain formatting when converting PPTX → HTML → PPTX.

### Core Use Cases
- Enterprise PPT to web/RAG conversion (feeding PowerPoint content to AI systems)
- Content creators converting HTML presentations to PowerPoint
- AI-assisted PPT editing workflows (convert to HTML, edit, convert back without losing formatting)

## Development Strategy (from intro.md)

**Critical: Do PPTX → HTML first, then HTML → PPTX.**

Why this order matters:
- PPTX is structured XML - parsing is deterministic, 2-3 weeks for MVP
- HTML→PPTX requires a layout engine - complexity explodes with arbitrary HTML
- By limiting HTML→PPTX to a defined "Slide HTML" subset, complexity becomes manageable
- PPTX→HTML alone immediately attracts developers wanting to feed PPT to RAG systems

### Three Development Phases

**Phase 1 (2-3 weeks): PPTX → HTML**
- Text, tables, images, shapes → semantic HTML
- Support both flow and absolute layout modes
- CLI: `slidecraft to-html input.pptx -o output.html`

**Phase 2 (4-6 weeks): HTML → PPTX**
- Only supports "Slide HTML" subset (NOT arbitrary web pages)
- Preset layouts + theme system
- CLI: `slidecraft to-pptx slides.html -o output.pptx`

**Phase 3 (2-4 weeks): Round-trip (killer feature)**
- `pptx_to_html` outputs with `data-pptx-*` metadata
- `html_to_pptx` reads metadata to precisely update original elements
- Result: edit text content, formats/animations/layouts remain intact

## Key Constraints

### Three "Iron Rules" to Avoid
1. **Don't do "arbitrary HTML → PPT"** - only support the defined Slide HTML subset. The CSS layout黑洞 (black hole) will consume the project otherwise.

2. **Don't pursue pixel-perfect还原** - pursue semantic-level faithfulness. Use screenshot fallbacks for complex regions.

3. **Add CLI from day one** - `slidecraft to-html` / `slidecraft to-pptx`. CLI is the lowest-friction way for GitHub users to try the tool.

## Slide HTML Subset

The HTML→PPTX conversion will support a controlled subset of HTML:

```html
<!-- Structure: <section> = one slide -->
<section data-pptx-layout="two-column">
  <h1>Slide Title</h1>
  <div class="content">...</div>
</section>

<!-- Layout hints via data attributes -->
data-pptx-layout="two-column|title-only|blank|..."
data-pptx-theme="corporate|modern|..."
```

## Environment Setup

**Before running any tests or development commands, activate the conda environment:**

```bash
conda activate shuttleslide
```

All testing and development work must be done within this environment.

## Technical Stack (Planned)

- **Language**: Python
- **Core library**: python-pptx (read/write PPTX files)
- **CLI framework**: TBD (likely Click or Typer)
- **Testing**: TBD

## Development Commands

```bash
# Always activate environment first
conda activate shuttleslide

# Run tests
pytest

# Run specific test
pytest tests/test_specific.py
```

## Architecture Notes

- PPTX files are ZIP archives containing XML - parse deterministically
- Maintain metadata through round-trip using `data-pptx-*` attributes
- Layout engine only needs to handle Slide HTML subset, not general CSS
- Consider screenshot fallback for elements too complex to convert faithfully
