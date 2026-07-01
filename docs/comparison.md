# Comparison with other PowerPoint tools

This page gives an honest, detailed comparison of Shuttleslide against the other libraries and tools in the PowerPoint ↔ HTML / PowerPoint generation space. Every project here is good at something — the question is which one fits your job.

## Quick matrix

| Capability | Shuttleslide | [ppt-master](https://github.com/hugohe3/ppt-master) | [pptx-to-html5](https://github.com/shafe123/pptx-to-html5) | [python-pptx](https://github.com/scanny/python-pptx) | [Aspose.Slides](https://products.aspose.com/slides/python-family/) | [Spire.Presentation](https://www.e-iceblue.com/Introduce/presentation-for-python.html) | [LibreOffice](https://www.libreoffice.org/) | [pdf2htmlEX](https://github.com/pdf2htmlEX/pdf2htmlEX) |
|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| Direction | both | doc → PPTX | PPTX → HTML | PPTX r/w | both | both | both | PDF → HTML |
| Round-trip metadata | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Native DrawingML output | ✅ | ✅ | ❌ | ⚠️ manual | ✅ | ✅ | ✅ | n/a |
| Works without LLM | ✅ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Open source (MIT) | ✅ | ✅ | ✅ (unlicense) | ✅ (MIT) | ❌ commercial | ❌ commercial | ✅ (MPL) | ✅ (GPL) |
| Install via `pip` | ✅ | ❌ git clone | ✅ | ✅ | ✅ | ✅ | ❌ system pkg | ❌ system pkg |
| AI slide generation | ✅ (optional) | ✅ (required) | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## Shuttleslide vs [ppt-master](https://github.com/hugohe3/ppt-master)

**The two projects are complementary, not competitive.** This is the most important comparison on this page, so it gets the most detail.

### What ppt-master is

ppt-master (by [Hugo He](https://www.hehugo.com/)) is an **AI workflow skill**. It runs *inside* an AI IDE — Claude Code, Cursor, Copilot CLI, Codebuddy, Cline, Continue, etc. You chat with the agent ("make a deck from this PDF") and it follows the workflow to produce a natively editable `.pptx` on your machine. The agent uses the model (Claude, GPT, Gemini, Kimi) as the brain; ppt-master provides the workflow, prompts, scripts, and quality gates.

The standout technical piece of ppt-master is its **SVG → native DrawingML converter**, which turns any SVG into editable PowerPoint shapes (paths, gradients, presets) instead of flattened images.

### What Shuttleslide is

Shuttleslide is a **Python library + CLI**. You call it from code or shell — no AI IDE required. Three capabilities: deterministic PPTX ↔ HTML conversion, and an *optional* AI slide-generation pipeline for when you don't have a source deck.

### How they relate

- Shuttleslide **vendors** ppt-master's SVG → DrawingML converter at [`src/shuttleslide/_vendored/svg_to_pptx/`](../src/shuttleslide/_vendored/svg_to_pptx/). Same MIT license, full attribution. Without this, Shuttleslide's HTML → PPTX direction wouldn't reach the quality it has.
- The projects overlap on "AI generates slides from a topic" (Shuttleslide's `generate` command vs. ppt-master's full workflow), but the *form factor* is different: Shuttleslide is a library, ppt-master is an in-IDE skill.

### When to pick which

| You want… | Use |
|---|---|
| A Python library / CLI you can call from code or scripts | **Shuttleslide** |
| Round-trip PPTX → HTML → PPTX with format preservation | **Shuttleslide** (no one else does this) |
| To convert an existing `.pptx` to HTML for RAG / web publishing | **Shuttleslide** or pptx-to-html5 |
| To generate a deck from a PDF/DOCX via chat inside Claude Code / Cursor | **ppt-master** |
| Maximum output quality on a single deck, with a designer in the loop | **ppt-master** (its workflow is more polished for that) |

---

## Shuttleslide vs [pptx-to-html5](https://github.com/shafe123/pptx-to-html5)

pptx-to-html5 is a focused one-way PPTX → HTML converter. It's well-engineered for what it does: clean install, simple CLI, interactive slideshow with keyboard nav and touch support, percentage-based responsive positioning.

**Where pptx-to-html5 wins:**

- Simpler dependency footprint (no Playwright, no lxml juggling)
- Specifically tuned for the "presentation website" output
- Mature, stable, narrow scope

**Where Shuttleslide wins:**

- Bidirectional — PPTX → HTML **and** HTML → PPTX
- Round-trip metadata preservation
- Three layout modes (flow / pptview / slideshow) vs. one
- PPT-aware typography calibration (line height / paragraph spacing adjustments)
- Optional AI slide generation

**Pick pptx-to-html5 if:** you only ever need PPTX → HTML and want the smallest dependency tree.
**Pick Shuttleslide if:** you need to go back to PPTX, want richer layout options, or need to feed content into RAG/RAG-style pipelines via `flow` layout.

---

## Shuttleslide vs [python-pptx](https://github.com/scanny/python-pptx)

python-pptx is the **low-level PPTX read/write library** that Shuttleslide itself depends on. It gives you direct access to slides, shapes, runs, and XML — but it does *not* convert anything to HTML, and writing DrawingML shapes by hand is verbose.

**Use python-pptx directly when:**

- You're building your own PPTX tool and want a low-level API
- You need to do something very specific (e.g. tweak a single XML attribute) that higher-level tools don't expose

**Use Shuttleslide when:**

- You want to convert decks (either direction)
- You want an HTML-friendly abstraction on top of python-pptx

Shuttleslide wraps python-pptx; it doesn't replace it. Power users routinely mix the two.

---

## Shuttleslide vs [Aspose.Slides for Python](https://products.aspose.com/slides/python-family/)

Aspose is a mature **commercial** library covering PPTX, PDF, HTML, images, and more. Quality is high; pricing is per-developer with volume caps.

**Where Aspose wins:**

- Decades of edge-case handling, charts, SmartArt, embedded media
- Commercial support and SLA
- PowerPoint-clone fidelity on obscure features

**Where Shuttleslide wins:**

- Free and open source
- Bidirectional with round-trip metadata (Aspose goes PPTX ↔ HTML but doesn't preserve original-shape metadata across the trip)
- AI generation built in
- A community can extend it

**Pick Aspose if:** you're an enterprise with budget and need battle-tested fidelity on the broadest feature set.
**Pick Shuttleslide if:** open source matters, you need round-trip preservation, or you want an AI-native pipeline.

---

## Shuttleslide vs [Spire.Presentation for Python](https://www.e-iceblue.com/Introduce/presentation-for-python.html)

Spire is similar in positioning to Aspose — commercial, broad feature set, free tier with limits. The trade-offs are essentially the same as the Aspose comparison above. Shuttleslide's edge is openness, round-trip metadata, and the AI pipeline; Spire's edge is commercial-grade breadth.

---

## Shuttleslide vs [LibreOffice](https://www.libreoffice.org/) (headless)

LibreOffice in `--headless` mode can convert PPTX ↔ HTML via its Impress filter. It's free and already installed on many systems.

**Where LibreOffice wins:**

- Already installed on most Linux distros
- Handles a vast range of document formats
- No Python dependency at all

**Where Shuttleslide wins:**

- Predictable, deterministic output. LibreOffice's HTML filter is *lossy and idiosyncratic* — it tends to produce absolute-positioned `<div>` soup with inline styles, breaking semantic structure.
- Semantic HTML suitable for RAG ingestion (via `flow` layout)
- Round-trip preservation
- Native DrawingML output (LibreOffice's HTML output doesn't round-trip back through any tool)

**Pick LibreOffice if:** you already have it, you're doing a one-off conversion, and quality doesn't matter.
**Pick Shuttleslide if:** output quality, round-trip, or semantic structure matters.

---

## Shuttleslide vs [pdf2htmlEX](https://github.com/pdf2htmlEX/pdf2htmlEX)

pdf2htmlEX is excellent at what it does — converting PDF to HTML with very high visual fidelity. It's listed here because people sometimes reach for it when they actually want PPTX conversion.

**Don't use pdf2htmlEX for PowerPoint files.** It only reads PDF. Going PPTX → PDF → HTML loses all the structural information (text boxes, shape types, tables) that makes round-trip possible.

If you genuinely have a PDF and want HTML, pdf2htmlEX is great. If you have a `.pptx`, use Shuttleslide.

---

## Summary: which tool fits which job

| Job | First pick |
|---|---|
| Round-trip PPTX → HTML → PPTX with format preservation | **Shuttleslide** |
| PPTX → HTML for RAG / web (multiple layout modes) | **Shuttleslide** |
| HTML → editable PPTX | **Shuttleslide** |
| AI slide generation from a topic, via library/CLI | **Shuttleslide** (`[ai]` extra) |
| AI slide generation inside Claude Code / Cursor, full workflow | **ppt-master** |
| Low-level PPTX read/write | **python-pptx** |
| Quick one-off PPTX → HTML, simple deps | **pptx-to-html5** |
| Enterprise, budget, every PowerPoint feature | **Aspose** or **Spire** |
| PDF → HTML (not PPTX) | **pdf2htmlEX** |

## References

- [ppt-master on GitHub](https://github.com/hugohe3/ppt-master) · [live demo](https://hugohe3.github.io/ppt-master/) · MIT
- [pptx-to-html5 on GitHub](https://github.com/shafe123/pptx-to-html5) · [PyPI](https://pypi.org/project/pptx-to-html5/)
- [python-pptx on GitHub](https://github.com/scanny/python-pptx) · [docs](https://python-pptx.readthedocs.io/)
- [Aspose.Slides for Python](https://products.aspose.com/slides/python-family/)
- [Spire.Presentation for Python](https://www.e-iceblue.com/Introduce/presentation-for-python.html)
- [LibreOffice](https://www.libreoffice.org/)
- [pdf2htmlEX](https://github.com/pdf2htmlEX/pdf2htmlEX)
