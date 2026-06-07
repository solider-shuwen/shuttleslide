"""
Tests for PPTX to HTML conversion.
"""

import pytest
from pathlib import Path
from shuttleslide.pptx_to_html.parser import PPTXParser, TextElement, TableElement
from shuttleslide.pptx_to_html.converters.text import TextConverter
from shuttleslide.pptx_to_html.converters.tables import TableConverter
from shuttleslide.pptx_to_html.layouts.flow import FlowLayout
from shuttleslide.pptx_to_html.layouts.absolute import AbsoluteLayout


class TestTextConverter:
    """Tests for TextConverter."""

    def test_convert_text_element(self):
        """Test basic text element conversion."""
        element = TextElement(
            element_type="text",
            left=100,
            top=200,
            width=300,
            height=50,
            z_order=1,
            text="Hello World",
        )

        converter = TextConverter()
        html = converter.convert(element)

        assert "<p" in html
        assert "Hello World" in html
        assert 'data-pptx-left="100"' in html

    def test_convert_title_element(self):
        """Test title element conversion."""
        element = TextElement(
            element_type="text",
            left=0,
            top=0,
            width=960,
            height=100,
            z_order=1,
            text="Presentation Title",
            is_title=True,
            font_size=36.0,
            bold=True,
        )

        converter = TextConverter()
        html = converter.convert(element)

        assert "<h1" in html
        assert "Presentation Title" in html
        assert 'data-pptx-is-title="true"' in html
        assert 'data-pptx-bold="true"' in html

    def test_convert_list_item(self):
        """Test list item conversion."""
        element = TextElement(
            element_type="text",
            left=100,
            top=200,
            width=300,
            height=50,
            z_order=1,
            text="- First item",
        )

        converter = TextConverter()
        html = converter.convert(element)

        assert "<li" in html
        assert "First item" in html


class TestTableConverter:
    """Tests for TableConverter."""

    def test_convert_table(self):
        """Test basic table conversion."""
        element = TableElement(
            element_type="table",
            left=100,
            top=200,
            width=400,
            height=300,
            z_order=1,
            rows=2,
            cols=2,
            data=[
                ["Header 1", "Header 2"],
                ["Data 1", "Data 2"],
            ],
            cell_styles=[
                [{"background_color": "#CCCCCC"}, {"background_color": "#CCCCCC"}],
                [{"background_color": None}, {"background_color": None}],
            ],
        )

        converter = TableConverter()
        html = converter.convert(element)

        assert "<table" in html
        assert "Header 1" in html
        assert "Data 1" in html
        assert 'data-pptx-rows="2"' in html
        assert 'data-pptx-cols="2"' in html


class TestPPTXParser:
    """Tests for PPTXParser."""

    def test_parser_initialization(self):
        """Test parser initialization."""
        parser = PPTXParser("test.pptx")
        assert parser.pptx_path.name == "test.pptx"

    def test_parser_with_invalid_path(self):
        """Test parser with invalid path."""
        parser = PPTXParser("nonexistent.pptx")
        with pytest.raises(Exception):
            parser.parse()


class TestFlowLayout:
    """Tests for FlowLayout."""

    def test_layout_initialization(self):
        """Test flow layout initialization."""
        layout = FlowLayout()
        assert layout.text_converter is not None
        assert layout.table_converter is not None
        assert layout.image_converter is not None
        assert layout.shape_converter is not None


class TestAbsoluteLayout:
    """Tests for AbsoluteLayout."""

    def test_layout_initialization(self):
        """Test absolute layout initialization."""
        layout = AbsoluteLayout()
        assert layout.text_converter is not None
        assert layout.table_converter is not None
        assert layout.image_converter is not None
        assert layout.shape_converter is not None


@pytest.mark.integration
class TestEndToEnd:
    """End-to-end integration tests."""

    def test_convert_simple_presentation(self):
        """
        Test converting a simple presentation.

        Note: This test requires a sample.pptx file in tests/fixtures/
        The test will be skipped if the file doesn't exist.
        """
        fixture_path = Path(__file__).parent / "fixtures" / "sample.pptx"

        if not fixture_path.exists():
            pytest.skip("sample.pptx fixture not found")

        parser = PPTXParser(fixture_path)
        slides = parser.parse()

        assert len(slides) > 0

        # Test flow layout
        flow_layout = FlowLayout()
        flow_html = flow_layout.convert(slides)

        assert "<!DOCTYPE html>" in flow_html
        assert "<html>" in flow_html
        assert flow_html.count("<section") == len(slides)

        # Test absolute layout
        abs_layout = AbsoluteLayout()
        abs_html = abs_layout.convert(slides)

        assert "<!DOCTYPE html>" in abs_html
        assert "<html>" in abs_html
        assert abs_html.count("<section") == len(slides)
