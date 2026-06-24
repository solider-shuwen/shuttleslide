"""Input extraction helpers for the web review client.

The config page accepts three input modes for the presentation topic:
  - direct text (textarea)
  - HTML file upload (extracted via trafilatura)
  - Markdown / plain-text file upload (used verbatim)

Only the HTML path needs real processing; the others are pass-through.
This module isolates the trafilatura dependency so the rest of the
review package stays import-light.
"""

from __future__ import annotations

import re


def extract_topic_from_html(html: str, max_chars: int = 20000) -> str:
    """Extract main content from an HTML document as Markdown.

    Uses trafilatura to strip boilerplate (nav/footer/scripts/ads) and
    emit clean Markdown that preserves headings, lists, tables, and
    links — gives the agent much more structural signal than a flat
    text blob.

    ``max_chars`` caps the output so we stay within LLM context limits.
    A 7.5 MB docs page will produce way too much text otherwise.

    Falls back to a crude tag strip when trafilatura returns None
    (minimal fragments, non-HTML input, parse failure) so the pipeline
    doesn't crash on edge cases.
    """
    import trafilatura

    text = trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_images=False,
        include_tables=True,
        favor_recall=True,
    )
    if not text:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_chars:
        cut = text.rfind("\n\n", 0, max_chars)
        if cut == -1 or cut < max_chars // 2:
            cut = max_chars
        text = text[:cut].rstrip() + "\n\n[... content truncated for length ...]"
    return text


def extract_topic_from_text(content: str, max_chars: int = 20000) -> str:
    """Use a markdown / plain-text upload verbatim, capped to ``max_chars``.

    No structural extraction — the file is already the topic body the
    user wants the agent to see.
    """
    if len(content) > max_chars:
        cut = content.rfind("\n\n", 0, max_chars)
        if cut == -1 or cut < max_chars // 2:
            cut = max_chars
        content = content[:cut].rstrip() + "\n\n[... content truncated for length ...]"
    return content
