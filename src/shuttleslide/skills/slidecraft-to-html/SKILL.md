---
name: slidecraft-to-html
description: Convert PowerPoint PPTX files to HTML for web display, RAG ingestion, or AI processing. Use when the user says convert this ppt to html, turn this presentation into a web page, extract content from PowerPoint for an AI pipeline, or wants to view slides in a browser.
allowed-tools: Bash(slidecraft to-html *) Bash(slidecraft *)
---

# Convert PPTX to HTML

## Basic usage
    slidecraft to-html input.pptx -o output.html

## Layout modes
    slidecraft to-html input.pptx --layout slideshow -o output.html
    slidecraft to-html input.pptx --layout flow -o output.html
    slidecraft to-html input.pptx --layout pptview -o output.html

## Options
    --theme corporate|modern|default
    --base64                           # embed images inline
    --no-animations                    # disable CSS animations
    --no-shrink                        # disable auto-fit text scaling
    --stdout                           # output to stdout
    -v / --verbose                     # progress

## RAG and content extraction
For feeding PowerPoint content to AI systems:
    slidecraft to-html slides.pptx --layout flow --base64 -o slides.html

## Output
Prints the output file path on success. Image assets saved alongside HTML by default, or inlined with --base64.
