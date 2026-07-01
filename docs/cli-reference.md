# CLI Reference

`slidecraft` is the single entry point for all of Shuttleslide's capabilities. Every subcommand is independently usable and scriptable.

```text
$ slidecraft --help
Usage: slidecraft [OPTIONS] COMMAND [ARGS]...

  Shuttleslide - Bidirectional PPTX ↔ HTML conversion library.

  Convert PowerPoint presentations to HTML and back with round-trip
  format preservation.

Options:
  --version  Show the version and exit.
  --help     Show this message and exit.

Commands:
  to-html       Convert PPTX file to HTML.
  to-pptx       Convert HTML file to PPTX using deterministic rule-based...
  json-to-pptx  Convert a PPT-DSL JSON file directly to PPTX (no LLM required).
  analyze       Analyze a PPTX file and show its structure.
  generate      Generate a slide deck from a topic using an LLM agent pipeline.
  review        Launch the web review client.
  warm-cache    Pre-populate the CDN asset cache so generation works offline.
  info          Show information about the Shuttleslide project.
```

---

## `slidecraft to-html`

Convert a `.pptx` file to a single self-contained HTML file.

```text
slidecraft to-html INPUT_PPTX [-o OUTPUT] [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `INPUT_PPTX` (arg) | — | Path to the `.pptx` or `.ppt` file |
| `-o, --output PATH` | derived from input | Output HTML path (or directory) |
| `--layout {flow,pptview,slideshow}` | `slideshow` | Layout mode |
| `--stdout` | off | Print HTML to stdout instead of writing a file |
| `--theme {default,corporate,modern}` | `default` | CSS theme |
| `--verbose, -v` | off | Show progress messages |
| `--animations` | on (default) | Enable CSS entrance/transition animations (`slideshow` only) |
| `--no-animations` | — | Disable CSS animations |
| `--base64` | off | Embed images as base64 (default: separate files under `output_assets/`) |
| `--no-shrink` | off | Disable Playwright-based font-shrink-on-overflow for text shapes |

### Layout modes

- **`flow`** — semantic, scrollable page. Best for RAG / web publishing.
- **`pptview`** — PowerPoint editor look. Best for design fidelity.
- **`slideshow`** — interactive presentation with keyboard navigation (`←` `→`, `Space`, `Home`, `End`) and slide transitions.

### Examples

```bash
# Default interactive slideshow
slidecraft to-html deck.pptx -o presentation.html

# Single scrollable page for RAG ingestion
slidecraft to-html deck.pptx -o presentation.html --layout flow

# Fully self-contained file (no external assets)
slidecraft to-html deck.pptx -o single-file.html --base64

# Pipe to another tool
slidecraft to-html deck.pptx --stdout | grep -i "title"
```

---

## `slidecraft to-pptx`

Convert HTML to a `.pptx` file using deterministic rule-based extraction. **No LLM, no network access required.**

```text
slidecraft to-pptx INPUT_HTML [-o OUTPUT] [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `INPUT_HTML` (arg) | — | Path to the HTML file |
| `-o, --output PATH` | derived from input | Output PPTX path |
| `--verbose, -v` | off | Show stage-by-stage progress |

### Pipeline

1. HTML is loaded into headless Chromium via Playwright; computed positions and styles are extracted.
2. The rule-based classifier groups elements into shapes, text boxes, tables, and SVG paths.
3. A `PPT-DSL` JSON document is emitted.
4. `PPTXRenderer` writes a `.pptx` with native DrawingML shapes.

### Example

```bash
slidecraft to-pptx slides.html -o output.pptx -v
```

---

## `slidecraft json-to-pptx`

Render a pre-built `PPT-DSL` JSON document directly to PPTX. Skips the HTML-extraction stage entirely — useful when you hand-craft or pre-generate the DSL.

```text
slidecraft json-to-pptx INPUT_JSON [-o OUTPUT] [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `INPUT_JSON` (arg) | — | Path to the PPT-DSL JSON file |
| `-o, --output PATH` | derived from input | Output PPTX path |
| `--verbose, -v` | off | Show progress |

### Example

```bash
slidecraft json-to-pptx deck.dsl.json -o deck.pptx
```

---

## `slidecraft analyze`

Inspect a `.pptx` file and print its structure (metadata, slide count, layouts, element types and positions).

```text
slidecraft analyze INPUT_PPTX [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `INPUT_PPTX` (arg) | — | Path to the PowerPoint file |
| `--verbose, -v` | off | Print per-element details |

### Example

```bash
slidecraft analyze deck.pptx -v
```

---

## `slidecraft generate`

Generate a multi-slide deck from a topic using an LLM agent. Requires the `[ai]` install extra and an OpenAI-compatible endpoint.

```text
slidecraft generate [TOPIC] [-i INPUT_FILE] --output-dir DIR [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `TOPIC` (arg, optional) | — | The presentation topic |
| `-i, --input PATH` | — | Read the topic from a markdown/text file |
| `--style TEXT` | `business` | Style hint: `business`, `cute`, `anime`, `tech`, `editorial`, ... |
| `--slides INT` | inferred (6–15) | Target slide count |
| `-o, --output-dir PATH` (required) | — | Where to write `1.html`, `2.html`, ... |
| `--api-base URL` | `$SHUTTLESLIDE_API_BASE` | OpenAI-compatible API base URL |
| `--api-key TEXT` | `$SHUTTLESLIDE_API_KEY` | API key |
| `--model NAME` | `$SHUTTLESLIDE_MODEL` | Model name (e.g. `glm-4.7`, `gpt-4o-mini`) |
| `--temperature FLOAT` | `0.7` | Sampling temperature |
| `--verbose, -v` | off | Show progress |

### Examples

```bash
# Inline topic
slidecraft generate "Introduction to Machine Learning" -o tmp/gen/

# From a file
slidecraft generate -i outline.md --style tech -o tmp/gen/

# With explicit credentials
slidecraft generate "AI in healthcare" \
    --api-base https://open.bigmodel.cn/api/paas/v4 \
    --api-key  $SHUTTLESLIDE_API_KEY \
    --model    glm-4.7 \
    -o         tmp/gen/
```

### Environment variables

The `SHUTTLESLIDE_*` variables are read as fallbacks when the corresponding CLI flag is omitted. They also work for `slidecraft review`:

- `SHUTTLESLIDE_API_BASE`
- `SHUTTLESLIDE_API_KEY`
- `SHUTTLESLIDE_MODEL`

---

## `slidecraft review`

Launch a local web server with a human-in-the-loop review UI. Each pipeline stage (theme, outline, slide-build) pauses for your approval; request edits, approve to proceed, and export when done.

Requires the `[review]` install extra.

```text
slidecraft review [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--host ADDR` | `127.0.0.1` | Bind address |
| `--port INT` | `8765` | Bind port |
| `-o, --output-dir PATH` | `./tmp/web_review/` | Base directory for runs (each run gets a timestamped subdir) |
| `--no-browser` | off | Don't auto-open the browser; still print the URL |
| `--api-base URL` | — | Lock the LLM API base (UI field becomes read-only) |
| `--api-key TEXT` | — | Lock the LLM API key |
| `--model NAME` | — | Lock the LLM model name |

### Examples

```bash
# Default — opens browser, runs on 127.0.0.1:8765
slidecraft review

# Different port, no auto-open
slidecraft review --port 9000 --no-browser

# Lock credentials via flags (UI hides them)
slidecraft review --api-base $URL --api-key $KEY --model glm-4.7
```

### Credential resolution order (later wins)

1. The web form (typed at runtime)
2. `.env` file in CWD using `SHUTTLESLIDE_*` keys
3. CLI flags above

---

## `slidecraft warm-cache`

Pre-download the Tailwind JIT script and Material Icons CSS into `~/.shuttleslide/cdn/` so `generate` works offline. Run once when you have network access; afterwards generation works without it.

```text
slidecraft warm-cache [OPTIONS]
```

### Options

| Option | Default | Description |
|---|---|---|
| `--force` | off | Re-download even if cache files exist |
| `--include-google-fonts` | off | Also fetch Google Fonts CSS (~47 MB; **not** used by the default renderer) |

### Example

```bash
slidecraft warm-cache
```

---

## `slidecraft info`

Print version, capabilities, and project URL.

```text
slidecraft info
```

---

## Extension points

External packages can register additional `slidecraft` subcommands via the `shuttleslide.cli_commands` Python entry-point group:

```toml
# In your package's pyproject.toml
[project.entry-points."shuttleslide.cli_commands"]
my-cmd = "my_package.cli:my_cmd"
```

`my_cmd` must be a `click.Command`. Once your package is installed, `slidecraft my-cmd` is available — no changes to shuttleslide required. The same isolation contract applies to the other entry-point groups (`shuttleslide.review.stages`, `shuttleslide.review.house_rules`); see the project README for the full list.
