"""
Playwright-based HTML analyzer — extracts precise layout data from rendered HTML.
"""

from shuttleslide.html_to_pptx.analyzer.extractor import analyze_html
from shuttleslide.html_to_pptx.analyzer.browser import BrowserManager

__all__ = ["analyze_html", "BrowserManager"]
