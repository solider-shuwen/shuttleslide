"""
CLI interface for Shuttleslide.
"""

import sys
from pathlib import Path
from typing import Optional

import click

from shuttleslide.pptx_to_html.parser import PPTXParser
from shuttleslide.pptx_to_html.layouts.flow import FlowLayout
from shuttleslide.pptx_to_html.layouts.pptview import PPTLayout
from shuttleslide.pptx_to_html.layouts.slideshow import SlideshowLayout


def _ensure_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8 so Unicode in docstrings and
    click.echo output doesn't UnicodeEncodeError on Windows GBK (cp936)
    consoles.

    Why: `slidecraft --help` reads main()'s docstring which contains
    "↔", "→", "—". On a stock zh-CN Windows console, sys.stdout
    defaults to cp936 and Click writes through it, failing to encode.
    The fix is class-level: force UTF-8 once at import time, so any
    future Unicode in any command Just Works.

    Idempotent: reconfiguring an already-UTF-8 stream is a no-op.
    Streams without `.reconfigure` (StringIO, pytest capture,
    sys.stdout=None under pythonw) are skipped silently.
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # pytest / custom harnesses may replace sys.stdout with a
            # stream that exposes reconfigure() but rejects the call.
            # Don't crash CLI startup over a cosmetic fix.
            pass


_ensure_utf8_stdio()


def _resolve_output_path(output: Optional[str], input_path: Path, suffix: str) -> Path:
    """Resolve the destination file path for a single-file conversion.

    Handles three forms of `-o/--output`:

    - None            -> derive from the input path (default).
    - A directory     -> write ``<input-stem><suffix>`` inside it. Detects
                         both "path points to an existing directory" and
                         "path ends with a path separator" (the user wrote
                         ``-o tmp/out/`` to mean "into this folder").
    - Anything else   -> treat as the literal file path.

    Why: ``Path("tmp/out/").write_text(...)`` raises ``PermissionError`` on
    Windows because the OS refuses to open a directory as a file. Catching
    both ``is_dir()`` and a trailing separator covers the cases where the
    directory does and does not yet exist.
    """
    if not output:
        return input_path.with_suffix(suffix)

    output_path = Path(output)
    looks_like_dir = output.endswith(("/", "\\")) or output_path.is_dir()
    if looks_like_dir:
        return output_path / input_path.with_suffix(suffix).name
    return output_path


@click.group()
@click.version_option(version="0.1.0")
def main():
    """
    Shuttleslide - Bidirectional PPTX ↔ HTML conversion library.

    Convert PowerPoint presentations to HTML and back with round-trip
    format preservation.
    """
    pass


# Attach any third-party / commercial extensions registered via the
# `shuttleslide.cli_commands` entry-point group. No-op when none are
# installed; isolated from per-extension load failures.
from shuttleslide.extensions import register_extensions  # noqa: E402

register_extensions(main)


@main.command()
@click.argument("input_pptx", type=click.Path(exists=True))
@click.option("-o", "--output", type=click.Path(), help="Output HTML file path")
@click.option(
    "--layout",
    type=click.Choice(["flow", "pptview", "slideshow"], case_sensitive=False),
    default="slideshow",
    help="Layout mode: 'flow' for scrollable page, 'pptview' for PPT-style editor, 'slideshow' for interactive presentation (default)",
)
@click.option("--stdout", is_flag=True, help="Output to stdout instead of file")
@click.option(
    "--theme",
    type=click.Choice(["default", "corporate", "modern"], case_sensitive=False),
    default="default",
    help="CSS theme for output HTML",
)
@click.option(
    "--verbose", "-v", is_flag=True, help="Show verbose output during conversion"
)
@click.option(
    "--animations", is_flag=True, default=True,
    help="Enable CSS animations for slide elements (default: enabled)"
)
@click.option(
    "--no-animations", is_flag=True,
    help="Disable CSS animations for slide elements"
)
@click.option(
    "--base64", is_flag=True,
    help="Embed images as base64 in HTML (default: save as separate files for better performance)"
)
@click.option(
    "--no-shrink", is_flag=True,
    help="Disable Playwright-based shrink-on-overflow for text shapes. "
         "By default, text that would exceed the PPT-declared shape height "
         "is font-scaled to fit (mirrors PPT's normAutofit behavior)."
)
def to_html(
    input_pptx: str,
    output: Optional[str],
    layout: str,
    stdout: bool,
    theme: str,
    verbose: bool,
    animations: bool,
    no_animations: bool,
    base64: bool,
    no_shrink: bool,
):
    """
    Convert PPTX file to HTML.

    INPUT_PPTX: Path to the PowerPoint file to convert
    """
    try:
        # Validate input file
        input_path = Path(input_pptx)
        if not input_path.suffix.lower() in [".pptx", ".ppt"]:
            click.echo("Error: Input file must be a PowerPoint file (.pptx or .ppt)", err=True)
            sys.exit(1)

        # Determine output path
        if not stdout:
            output_path = _resolve_output_path(output, input_path, ".html")

        # Show progress
        if verbose:
            click.echo(f"Parsing PPTX file: {input_path}")

        # Parse PPTX
        parser = PPTXParser(input_path)
        slides = parser.parse()

        if verbose:
            click.echo(f"Found {len(slides)} slides")

        # Get metadata
        metadata = parser.get_presentation_metadata()
        if verbose and metadata.get("title"):
            click.echo(f"Title: {metadata['title']}")

        # Select layout
        enable_animations = animations and not no_animations
        use_base64 = base64  # Default is False (external files), True only when --base64 is specified

        # Compute assets output directory (next to the HTML file)
        if not stdout and not use_base64:
            import os
            html_dir = str(output_path.parent.resolve())
            assets_dir = os.path.join(html_dir, "output_assets")
        else:
            assets_dir = None

        # Playwright measurer powers HTML-mode shrink-on-overflow: text
        # shapes that would exceed the PPT-declared shape height are
        # font-scaled to fit (mirrors PPT's <a:normAutofit fontScale>).
        # Enabled by default; --no-shrink disables.
        measurer = None
        if not no_shrink:
            from shuttleslide.pptx_to_html.text_measure import PlaywrightTextMeasurer
            if verbose:
                click.echo("Launching headless Chromium for text measurement...")
            measurer = PlaywrightTextMeasurer()
            try:
                measurer.start()
            except Exception as e:
                # Fall back to no-shrink mode rather than aborting the whole
                # conversion.  The user gets a working HTML, just without
                # shrink-on-overflow (text may overlap if shapes are tight).
                click.echo(
                    f"Warning: Playwright unavailable ({e}); "
                    f"shrink-on-overflow disabled.",
                    err=True,
                )
                measurer = None

        try:
            if layout == "flow":
                layout_engine = FlowLayout(output_dir=assets_dir, measurer=measurer)
            elif layout == "pptview":
                layout_engine = PPTLayout(use_base64=use_base64, output_dir=assets_dir,
                                           measurer=measurer)
            else:  # slideshow
                layout_engine = SlideshowLayout(enable_animations=enable_animations,
                                                 use_base64=use_base64, output_dir=assets_dir,
                                                 measurer=measurer)

            # Convert to HTML
            if verbose:
                click.echo(f"Converting with {layout} layout...")

            html = layout_engine.convert(slides)
        finally:
            if measurer is not None:
                measurer.close()

        # Output
        if stdout:
            click.echo(html)
        else:
            output_path.write_text(html, encoding="utf-8")
            if verbose:
                click.echo(f"Output written to: {output_path}")
            else:
                click.echo(str(output_path))

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        if verbose:
            import traceback

            traceback.print_exc()
        sys.exit(1)


@main.command()
@click.argument("input_html", type=click.Path(exists=True))
@click.option("-o", "--output", type=click.Path(), help="Output PPTX file path")
@click.option(
    "--verbose", "-v", is_flag=True, help="Show verbose output during conversion"
)
def to_pptx(input_html: str, output: Optional[str], verbose: bool):
    """
    Convert HTML file to PPTX using deterministic rule-based extraction.

    INPUT_HTML: Path to the HTML file to convert

    No LLM or network access required — layout extraction (Playwright) plus
    Python rules produce the PPT-DSL that the renderer consumes.
    """
    try:
        from shuttleslide.html_to_pptx import RuleSlideTransformer, PPTXRenderer
        import asyncio

        input_path = Path(input_html)
        html = input_path.read_text(encoding="utf-8")

        # Determine output path
        output_path = _resolve_output_path(output, input_path, ".pptx")

        if verbose:
            click.echo(f"Converting: {input_path}")

        # Stage 1: HTML → PPT-DSL JSON
        if verbose:
            click.echo("Stage 1: Extracting structured data via rules...")

        transformer = RuleSlideTransformer()
        dsl = asyncio.run(
            transformer.transform_html(
                html,
                verbose=verbose,
                base_dir=input_path.parent,
            )
        )

        if verbose:
            click.echo(f"Extracted {len(dsl.slides)} slide(s)")

        # Stage 2: PPT-DSL JSON → PPTX
        if verbose:
            click.echo("Stage 2: Rendering PPTX...")

        renderer = PPTXRenderer(base_dir=input_path.parent)
        renderer.render(dsl, str(output_path))

        click.echo(str(output_path))

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


@main.command("json-to-pptx")
@click.argument("input_json", type=click.Path(exists=True))
@click.option("-o", "--output", type=click.Path(), help="Output PPTX file path")
@click.option(
    "--verbose", "-v", is_flag=True, help="Show verbose output during conversion"
)
def json_to_pptx(input_json: str, output: Optional[str], verbose: bool):
    """
    Convert a PPT-DSL JSON file directly to PPTX (no LLM required).

    INPUT_JSON: Path to the PPT-DSL JSON file to convert

    This command skips the LLM extraction step and directly renders a
    hand-crafted or pre-generated JSON file to PPTX.
    """
    try:
        import json as json_mod
        from shuttleslide.html_to_pptx import load_presentation, PPTXRenderer

        input_path = Path(input_json)
        data = json_mod.loads(input_path.read_text(encoding="utf-8"))
        dsl = load_presentation(data)

        output_path = _resolve_output_path(output, input_path, ".pptx")

        if verbose:
            click.echo(f"Rendering {len(dsl.slides)} slide(s) to PPTX...")

        renderer = PPTXRenderer(base_dir=input_path.parent)
        renderer.render(dsl, str(output_path))

        click.echo(str(output_path))

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)


@main.command()
def info():
    """
    Show information about the Shuttleslide project.
    """
    click.echo("Shuttleslide v0.1.0")
    click.echo("")
    click.echo("Bidirectional PPTX ↔ HTML conversion library")
    click.echo("with round-trip format preservation.")
    click.echo("")
    click.echo("Current Phase: Phase 1 - PPTX → HTML")
    click.echo("")
    click.echo("For more information, visit: https://github.com/solider-shuwen/shuttleslide")


@main.command()
@click.argument("input_pptx", type=click.Path(exists=True))
@click.option(
    "--verbose", "-v", is_flag=True, help="Show verbose output"
)
def analyze(input_pptx: str, verbose: bool):
    """
    Analyze a PPTX file and show its structure.

    INPUT_PPTX: Path to the PowerPoint file to analyze
    """
    try:
        parser = PPTXParser(input_pptx)
        slides = parser.parse()
        metadata = parser.get_presentation_metadata()

        click.echo(f"Presentation: {metadata.get('title', 'Untitled')}")
        click.echo(f"Author: {metadata.get('author', 'Unknown')}")
        click.echo(f"Slide Count: {len(slides)}")
        click.echo(f"Dimensions: {metadata.get('slide_width', 0)} x {metadata.get('slide_height', 0)}")
        click.echo("")

        for slide in slides:
            click.echo(f"Slide {slide.slide_number}:")
            click.echo(f"  Layout: {slide.layout_name}")
            click.echo(f"  Elements: {len(slide.elements)}")

            if verbose:
                for element in slide.elements:
                    click.echo(f"    - {element.element_type} at ({element.left}, {element.top})")

            click.echo("")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


@main.command()
@click.argument("topic", required=False)
@click.option("--input", "-i", "input_file", type=click.Path(exists=True),
              help="Read topic from a markdown/text file instead of passing it as an argument.")
@click.option("--style", default="business", show_default=True,
              help="Style hint: business, cute, anime, tech, editorial, ...")
@click.option("--slides", "target_slide_count", type=int, default=None,
              help="Target slide count. Omit to let the LLM infer (typically 6-15).")
@click.option("-o", "--output", "output_dir", type=click.Path(), required=True,
              help="Output directory for generated HTML files (one per slide).")
@click.option("--api-base", default=None, help="OpenAI-compatible API base URL.")
@click.option("--api-key", default=None, help="API key for the LLM provider.")
@click.option("--model", default=None, help="Model name (e.g. glm-4.7, gpt-4o-mini).")
@click.option("--temperature", type=float, default=0.7, show_default=True)
@click.option("--verbose", "-v", is_flag=True)
def generate(
    topic: Optional[str],
    input_file: Optional[str],
    style: str,
    target_slide_count: int,
    output_dir: str,
    api_base: Optional[str],
    api_key: Optional[str],
    model: Optional[str],
    temperature: float,
    verbose: bool,
):
    """
    Generate a slide deck from a topic using an LLM agent pipeline.

    TOPIC: The presentation topic. Either pass as an argument or use --input
    to read from a file. Output is written as 1.html, 2.html, ... in the
    output directory, matching the format of tmp/example_html/.

    \b
    Examples:
      slidecraft generate "Introduction to Machine Learning" -o tmp/gen/
      slidecraft generate -i outline.md --style cute -o tmp/gen/
      slidecraft generate "AI in healthcare" --api-base $URL --api-key $KEY --model glm-4.7 -o tmp/gen/

    \b
    Environment variables (used as fallbacks for the options above):
      SHUTTLESLIDE_API_BASE
      SHUTTLESLIDE_API_KEY
      SHUTTLESLIDE_MODEL
    """
    import asyncio

    # Resolve topic from arg or input file.
    if input_file:
        topic = Path(input_file).read_text(encoding="utf-8").strip()
    if not topic:
        click.echo("Error: topic is required (positional arg or --input)", err=True)
        sys.exit(1)

    try:
        from shuttleslide.agent import AgentConfig, AgentOrchestrator

        config = AgentConfig.from_env(
            api_base=api_base,
            api_key=api_key,
            model=model,
            topic=topic,
            style_hint=style,
            target_slide_count=target_slide_count,
            temperature=temperature,
            output_dir=output_dir,
        )
        config.validate()
    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)

    if verbose:
        click.echo(f"Topic: {topic[:80]}{'...' if len(topic) > 80 else ''}")
        click.echo(f"Style: {style}")
        click.echo(f"Target slides: {target_slide_count if target_slide_count is not None else 'inferred by LLM'}")
        click.echo(f"Model: {config.model} @ {config.api_base}")
        click.echo(f"Output: {output_dir}")
        click.echo("")

    async def _run():
        from shuttleslide.agent.asyncio_diag import install_noise_filter

        install_noise_filter(asyncio.get_running_loop())
        orch = AgentOrchestrator(config)
        return await orch.run()

    try:
        result = asyncio.run(_run())
    except Exception as e:
        click.echo(f"Error during generation: {e}", err=True)
        if verbose:
            import traceback
            traceback.print_exc()
        sys.exit(1)

    click.echo(f"Generated {len(result.html_paths)} slide(s):")
    for p in result.html_paths:
        click.echo(f"  {p}")

    if result.state.warnings and verbose:
        click.echo("")
        click.echo("Warnings:")
        for w in result.state.warnings:
            click.echo(f"  - {w}")


@main.command("warm-cache")
@click.option("--force", is_flag=True,
              help="Re-download even if cache files already exist.")
@click.option("--include-google-fonts", is_flag=True,
              help="Also download Google Fonts CSS (~47 MB). Off by default — see cdn_assets "
                   "docstring for why Google Fonts is not inlined by default.")
def warm_cache(force: bool, include_google_fonts: bool):
    """Pre-populate the CDN asset cache so generation works offline.

    Downloads the Tailwind JIT script and Material Icons CSS into
    ~/.shuttleslide/cdn/. Run this once when you have network access
    (e.g. via VPN); afterwards `generate` works without network.

    Google Fonts is intentionally NOT downloaded by default — fetching
    the full Noto Sans SC TTFs as base64 produces a ~47 MB CSS file
    the renderer doesn't use. The template uses a system font stack
    instead. Pass --include-google-fonts if you want it anyway.

    Without this, `generate` will attempt the download on first render
    and fall back to the live CDN URL if it fails.
    """
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from shuttleslide.agent import cdn_assets

    cache_dir = cdn_assets._cache_dir()
    click.echo(f"Cache directory: {cache_dir}")

    if force:
        for f in cache_dir.glob("tailwind.js"):
            f.unlink()
        for f in cache_dir.glob("gfonts_*.css"):
            f.unlink()
        for f in cache_dir.glob("material_*.css"):
            f.unlink()
        click.echo("Cleared existing cache.")

    click.echo("Downloading Tailwind JIT...")
    tw = cdn_assets.get_tailwind_script()
    if tw:
        click.echo(f"  OK ({len(tw)} bytes)")
    else:
        click.echo("  FAILED - generation will fall back to live CDN")

    click.echo("Downloading Material Icons...")
    mi = cdn_assets.get_material_icons_css()
    if mi:
        click.echo(f"  OK ({len(mi)} bytes)")
    else:
        click.echo("  FAILED - generation will fall back to live CDN")

    if include_google_fonts:
        click.echo("Downloading Google Fonts (Roboto, Noto Sans SC)...")
        click.echo("  WARNING: produces ~47 MB CSS, NOT used by the default renderer.")
        gf = cdn_assets.get_google_fonts_css()
        if gf:
            click.echo(f"  OK ({len(gf)} bytes)")
        else:
            click.echo("  FAILED")
    else:
        gf = True  # treated as "skipped, not a failure"

    if tw and mi and gf:
        click.echo("")
        click.echo("All assets cached. `generate` will now work offline.")
    else:
        click.echo("")
        click.echo("Some assets failed to download. Check your network/VPN.")
        sys.exit(1)


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True,
              help="Bind address for the review web server.")
@click.option("--port", default=8765, show_default=True, type=int,
              help="Port for the review web server.")
@click.option("-o", "--output-dir", "output_dir", type=click.Path(),
              default=None,
              help="Base directory for runs. Each run creates a timestamped "
                   "subdirectory. Defaults to ./tmp/web_review/.")
@click.option("--no-browser", is_flag=True,
              help="Don't auto-open the browser (still prints the URL).")
@click.option("--api-base", default=None,
              help="Lock API base URL. Overrides .env / form; field becomes read-only in UI.")
@click.option("--api-key", default=None,
              help="Lock API key (read-only in UI).")
@click.option("--model", default=None,
              help="Lock model name (read-only in UI).")
@click.option("--vlm-api-base", default=None,
              help="Lock VLM API base URL (read-only in UI).")
@click.option("--vlm-api-key", default=None,
              help="Lock VLM API key (read-only in UI).")
@click.option("--vlm-model", default=None,
              help="Lock VLM model name (read-only in UI).")
@click.option("--mock", "mock_mode", is_flag=True,
              help="Mock mode: bypass real LLM/VLM calls. The pipeline uses "
                   "a stub orchestrator that fires synthetic progress events "
                   "and populates canned state. Fast end-to-end UI testing "
                   "without API credentials. Locks all credential fields "
                   "(hidden in the form).")
@click.option("--canvas", "canvas_mode", is_flag=True,
              help="Canvas mode: the config screen shows an aspect-ratio "
                   "picker (16:9 / 9:16 / 1:1 / 3:4 / custom W:H). The "
                   "chosen ratio threads through AgentConfig.canvas_*_emu "
                   "and the review UI renders thumbnails + preview at the "
                   "true aspect ratio instead of the 16:9 default. Pro's "
                   "canvas house_rules provider (registered via the "
                   "shuttleslide.review.house_rules entry-point group) "
                   "swaps in canvas-specific prompts when this flag is on.")
def review(
    host: str,
    port: int,
    output_dir: Optional[str],
    no_browser: bool,
    api_base: Optional[str],
    api_key: Optional[str],
    model: Optional[str],
    vlm_api_base: Optional[str],
    vlm_api_key: Optional[str],
    vlm_model: Optional[str],
    mock_mode: bool,
    canvas_mode: bool,
):
    """Launch the web review client.

    Opens a browser to a configuration page where you set API
    credentials, topic, and style. The pipeline runs with
    human-in-the-loop stage approval — review each stage's snapshot,
    request edits, then approve to proceed.

    Use `slidecraft generate` instead for the no-review CLI path that
    runs end-to-end without human intervention.

    \b
    Credential sources (later wins):
      1. Web form (user types at runtime)
      2. .env file in CWD using SHUTTLESLIDE_* keys (pre-fills + locks)
      3. CLI flags below (lock specific fields, override .env)

    When api_base + api_key + model are all locked (CLI or .env), the
    credentials section of the form is hidden and only the model name
    is shown.

    \b
    Examples:
      slidecraft review
      slidecraft review --port 9000 --no-browser
      slidecraft review -o tmp/my_review_runs/
      slidecraft review --api-base $URL --api-key $KEY --model glm-4.7
      slidecraft review --vlm-model glm-4v  # lock only VLM model
    """
    import os
    import signal
    import threading
    import time
    import webbrowser

    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from shuttleslide.agent.review.server import ReviewServer

    # Load .env from CWD before reading os.environ. Silent no-op when
    # python-dotenv isn't installed or .env doesn't exist — CLI flags
    # still work in that case. Reuses SHUTTLESLIDE_* env var names so
    # the same .env works for `review` and `generate` (AgentConfig.from_env).
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Map AgentConfig field names to their SHUTTLESLIDE_* env var.
    # Every field here is force-applied server-side at POST /api/start
    # (server.py _extract_config_kwargs overrides form values with
    # effective_defaults), so listing a field here also makes the UI
    # render it readonly via /api/defaults → locked[]. That's correct
    # for credentials AND for behavior flags the user explicitly set in
    # .env — editing .env becomes the single source of truth.
    #
    # MUST stay in sync with AgentConfig.from_env's env reads
    # (config.py). Any SHUTTLESLIDE_* var read there but missing here
    # is silently dropped in the review-server flow (the bug that hit
    # disable_required_tool_choice: .env said true, server got False).
    _ENV_MAP = {
        "api_base": "SHUTTLESLIDE_API_BASE",
        "api_key": "SHUTTLESLIDE_API_KEY",
        "model": "SHUTTLESLIDE_MODEL",
        "vlm_api_base": "SHUTTLESLIDE_VLM_API_BASE",
        "vlm_api_key": "SHUTTLESLIDE_VLM_API_KEY",
        "vlm_model": "SHUTTLESLIDE_VLM_MODEL",
        "image_search_provider": "SHUTTLESLIDE_IMAGE_SEARCH_PROVIDER",
        "image_search_api_key": "SHUTTLESLIDE_IMAGE_SEARCH_API_KEY",
        "disable_required_tool_choice": "SHUTTLESLIDE_DISABLE_REQUIRED_TOOL_CHOICE",
        "enable_vlm_verification": "SHUTTLESLIDE_ENABLE_VLM_VERIFICATION",
    }
    env_defaults = {
        field: os.environ[env_var]
        for field, env_var in _ENV_MAP.items()
        if os.environ.get(env_var)
    }
    cli_overrides = {
        "api_base": api_base,
        "api_key": api_key,
        "model": model,
        "vlm_api_base": vlm_api_base,
        "vlm_api_key": vlm_api_key,
        "vlm_model": vlm_model,
    }
    cli_overrides = {k: v for k, v in cli_overrides.items() if v is not None}

    if output_dir is None:
        output_dir = str(Path.cwd() / "tmp" / "web_review")
    base_dir = Path(output_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    server = ReviewServer(
        gate=None,              # created per-run inside _run_pipeline
        orchestrator_loop=None,  # unused under new arch (pipeline runs on server loop)
        host=host,
        port=port,
        output_dir=base_dir,
        env_defaults=env_defaults,
        cli_overrides=cli_overrides,
        mock_mode=mock_mode,
        canvas_mode=canvas_mode,
    )
    server.start_in_thread()
    click.echo(f"Review UI: {server.url}")
    click.echo(f"Output base directory: {base_dir}")
    if mock_mode:
        click.echo("MOCK MODE: synthetic events, no real LLM calls.")
    if canvas_mode:
        click.echo("CANVAS MODE: aspect-ratio picker enabled on config screen.")
    if env_defaults or cli_overrides:
        # Surface which fields are locked so the user isn't surprised
        # when the form greys them out.
        locked = sorted(set(env_defaults) | set(cli_overrides))
        click.echo(f"Locked credential fields: {', '.join(locked)}")
    click.echo("Press Ctrl+C to stop.")

    if not no_browser:
        try:
            webbrowser.open(server.url)
        except Exception:
            pass  # headless / no DE; user can open the URL manually

    # Block the main thread until Ctrl+C / SIGTERM. The server runs on a
    # daemon thread, so it dies automatically when we exit.
    stop_event = threading.Event()

    def _on_signal(signum, frame):
        stop_event.set()

    try:
        signal.signal(signal.SIGINT, _on_signal)
        # SIGTERM exists on Windows but isn't deliverable to Console apps;
        # signal.signal would raise. Guarded for cross-platform safety.
        if hasattr(signal, "SIGTERM") and platform_supports_sigterm():
            signal.signal(signal.SIGTERM, _on_signal)
    except (ValueError, OSError):
        # ValueError when not on the main thread (shouldn't happen here);
        # OSError when the signal isn't supported on this platform. In
        # either case, fall back to KeyboardInterrupt below.
        pass

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        click.echo("Shutting down...")
        try:
            server.shutdown()
        except Exception:
            pass


def platform_supports_sigterm() -> bool:
    """True on Unix; on Windows SIGTERM exists as a name but is not
    deliverable to console apps, so signal.signal would fail."""
    import platform
    return platform.system() != "Windows"


if __name__ == "__main__":
    main()
