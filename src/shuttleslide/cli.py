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
            if output:
                output_path = Path(output)
            else:
                output_path = input_path.with_suffix(".html")

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

        if layout == "flow":
            layout_engine = FlowLayout(output_dir=assets_dir)
        elif layout == "pptview":
            layout_engine = PPTLayout(use_base64=use_base64, output_dir=assets_dir)
        else:  # slideshow
            layout_engine = SlideshowLayout(enable_animations=enable_animations, use_base64=use_base64, output_dir=assets_dir)

        # Convert to HTML
        if verbose:
            click.echo(f"Converting with {layout} layout...")

        html = layout_engine.convert(slides)

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
        if output:
            output_path = Path(output)
        else:
            output_path = input_path.with_suffix(".pptx")

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

        if output:
            output_path = Path(output)
        else:
            output_path = input_path.with_suffix(".pptx")

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
    click.echo("For more information, visit: https://github.com/yourusername/shuttleslide")


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


if __name__ == "__main__":
    main()
