---
name: slidecraft
description: Convert PowerPoint (PPTX) files to HTML and back, analyze PPTX structure, generate slide decks from topics via LLM, pre-cache CDN assets, or launch the interactive review web UI. Use this when the user wants to work with PowerPoint presentations -- convert ppt to html, html to pptx, inspect pptx contents, or generate slides.
allowed-tools: Bash(slidecraft *)
---

# slidecraft -- PPTX and HTML conversion toolkit

The slidecraft CLI is installed. Use it for PowerPoint-related tasks.

## Commands

### Convert PPTX to HTML
    slidecraft to-html input.pptx -o output.html
    slidecraft to-html input.pptx --layout flow -o output.html
    slidecraft to-html input.pptx --theme modern -o output.html
    slidecraft to-html input.pptx --base64 -o output.html

Layout modes: slideshow (default, interactive), flow (scrollable), pptview (editor view).

### Convert HTML to PPTX
    slidecraft to-pptx slides.html -o output.pptx

Only supports the Slide HTML subset, not arbitrary web pages.

### JSON to PPTX (skip LLM)
    slidecraft json-to-pptx slides.json -o output.pptx

### Analyze PPTX structure
    slidecraft analyze input.pptx
    slidecraft analyze input.pptx -v

### Generate slides from a topic (requires LLM)
    slidecraft generate "Introduction to ML" -o tmp/gen/
    slidecraft generate -i outline.md --style cute -o tmp/gen/

Needs SHUTTLESLIDE_API_BASE, SHUTTLESLIDE_API_KEY, SHUTTLESLIDE_MODEL env vars or --api-base/--api-key/--model flags.

### Pre-cache CDN assets
    slidecraft warm-cache
    slidecraft warm-cache --force

### Interactive review web UI
    slidecraft review
    slidecraft review --port 9000 --no-browser

## Common patterns
- Output path: -o accepts file path or directory (trailing slash).
- Verbose: add -v to any command.
- All conversions preserve formatting for round-trip fidelity.
