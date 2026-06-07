# Shuttleslide

Bidirectional conversion between PowerPoint (PPTX) files and HTML, with a focus on round-trip format preservation.

## Overview

Shuttleslide is a Python library that converts PowerPoint presentations to HTML and back. Unlike existing tools, Shuttleslide aims to preserve formatting through round-trip conversions, making it ideal for:

- **Enterprise PPT to web/RAG conversion**: Feed PowerPoint content to AI systems
- **Content creators**: Convert HTML presentations to PowerPoint
- **AI-assisted PPT editing**: Convert to HTML, edit with AI tools, convert back without losing formatting

## Current Status: Phase 1 - PPTX → HTML

**Phase 1 is currently implemented**: PPTX to HTML conversion with:
- Text extraction with formatting preservation
- Table support with styling
- Image embedding (base64)
- Shape rendering
- Two layout modes: Flow and Absolute positioning
- CLI interface for easy usage

**Planned phases:**
- **Phase 2**: HTML → PPTX conversion (Slide HTML subset)
- **Phase 3**: Round-trip conversion with metadata preservation

## Installation

### Prerequisites

- Python 3.8 or higher
- Conda (recommended) or virtual environment

### Using Conda (Recommended)

```bash
# Create and activate conda environment
conda create -n shuttleslide python=3.10
conda activate shuttleslide

# Install in development mode
pip install -e .
```

### Using pip

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install
pip install -e .
```

### Development Installation

```bash
# Install with development dependencies
pip install -e ".[dev]"
```

## Quick Start

### Basic Usage

```bash
# Convert PPTX to HTML with flow layout
slidecraft to-html presentation.pptx -o output.html

# Convert with absolute positioning
slidecraft to-html presentation.pptx -o output.html --layout=absolute

# Show verbose output
slidecraft to-html presentation.pptx -o output.html -v

# Output to stdout
slidecraft to-html presentation.pptx --stdout
```

### Analyze a Presentation

```bash
# Show presentation structure
slidecraft analyze presentation.pptx

# Show detailed element information
slidecraft analyze presentation.pptx -v
```

## Layout Modes

### Flow Layout (Default)

Elements flow naturally in HTML document flow. Best for:
- Text-heavy slides
- Simple presentations
- SEO-friendly output
- Mobile-responsive design

```bash
slidecraft to-html input.pptx --layout=flow
```

### Absolute Layout

Preserves exact positioning using CSS absolute positioning. Best for:
- Design-fidelity requirements
- Pixel-perfect layout preservation
- Complex slide compositions

```bash
slidecraft to-html input.pptx --layout=absolute
```

## Development

### Running Tests

```bash
# Activate environment first
conda activate shuttleslide

# Run all tests
pytest

# Run with coverage
pytest --cov=shuttleslide

# Run specific test
pytest tests/test_pptx_to_html.py::TestTextConverter::test_convert_text_element
```

### Code Formatting

```bash
# Format code with black
black src/ tests/

# Check type hints with mypy
mypy src/
```

### Project Structure

```
shuttleslide/
├── src/shuttleslide/
│   ├── __init__.py
│   ├── cli.py                  # CLI interface
│   ├── pptx_to_html/           # PPTX → HTML conversion
│   │   ├── parser.py           # PPTX parser
│   │   ├── converters/         # Element converters
│   │   │   ├── text.py
│   │   │   ├── tables.py
│   │   │   ├── images.py
│   │   │   └── shapes.py
│   │   └── layouts/            # Layout engines
│   │       ├── flow.py
│   │       └── absolute.py
│   └── html_to_pptx/           # HTML → PPTX (Phase 2)
├── tests/
│   ├── test_pptx_to_html.py
│   └── fixtures/
└── docs/
```

## Key Features

### Element Support

- **Text**: Titles, paragraphs, lists with formatting preservation
- **Tables**: Native PPTX tables with cell styling
- **Images**: Embedded as base64 for single-file output
- **Shapes**: Basic shapes (rectangles, circles, triangles, etc.)

### Round-Trip Metadata

All converted elements include `data-pptx-*` attributes for future round-trip support:
- Position and size information
- Font styling
- Element types
- Z-order

This enables Phase 3: editing HTML and converting back to PPTX without losing formatting.

## Roadmap

### Phase 1: PPTX → HTML ✅ (Current)
- ✅ PPTX parser
- ✅ Text, table, image, shape converters
- ✅ Flow and absolute layout modes
- ✅ CLI interface
- ✅ Basic testing

### Phase 2: HTML → PPTX (Planned)
- [ ] Slide HTML subset parser
- [ ] PPTX generator
- [ ] Layout system
- [ ] Theme support
- [ ] CLI: `slidecraft to-pptx`

### Phase 3: Round-Trip (Planned)
- [ ] Enhanced metadata preservation
- [ ] Incremental updates
- [ ] Format integrity verification
- [ ] AI workflow integration

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests for new functionality
5. Submit a pull request

## License

MIT License - see LICENSE file for details

## Acknowledgments

Built with:
- [python-pptx](https://github.com/scanny/python-pptx) - PPTX file manipulation
- [Click](https://click.palletsprojects.com/) - CLI interface
- [pytest](https://pytest.org/) - Testing framework

## Links

- [GitHub Repository](https://github.com/yourusername/shuttleslide)
- [Issue Tracker](https://github.com/yourusername/shuttleslide/issues)
- [Documentation](https://github.com/yourusername/shuttleslide/wiki)
