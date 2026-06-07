"""
CLI interface for Shuttleslide.
"""

import sys
from pathlib import Path
from typing import Optional

import click

from shuttleslide.pptx_to_html.parser import PPTXParser
from shuttleslide.pptx_to_html.layouts.flow import FlowLayout
from shuttleslide.pptx_to_html.layouts.absolute import AbsoluteLayout


@click.group()
@click.version_option(version="0.1.0")
def main():
    """
    Shuttleslide - Bidirectional PPTX ↔ HTML conversion library.

    Convert PowerPoint presentations to HTML and back with round-trip
    format preservation.
    """
    pass


@main.command()
@click.argument("input_pptx", type=click.Path(exists=True))
@click.option("-o", "--output", type=click.Path(), help="Output HTML file path")
@click.option(
    "--layout",
    type=click.Choice(["flow", "absolute"], case_sensitive=False),
    default="flow",
    help="Layout mode: 'flow' for natural flow, 'absolute' for exact positioning",
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
def to_html(
    input_pptx: str,
    output: Optional[str],
    layout: str,
    stdout: bool,
    theme: str,
    verbose: bool,
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
        if layout == "flow":
            layout_engine = FlowLayout()
        else:  # absolute
            layout_engine = AbsoluteLayout()

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
    Convert HTML file to PPTX (Phase 2 - not yet implemented).

    INPUT_HTML: Path to the HTML file to convert

    Note: This command is part of Phase 2 and will be implemented in a future release.
    For now, only PPTX → HTML conversion is available.
    """
    click.echo(
        "Error: HTML → PPTX conversion is not yet implemented.",
        err=True,
    )
    click.echo(
        "This feature is planned for Phase 2 of the Shuttleslide project.",
        err=True,
    )
    click.echo(
        "Please use 'slidecraft to-html' to convert PPTX → HTML.",
        err=True,
    )
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


if __name__ == "__main__":
    main()
