"""PlaywrightTextMeasurer — pixel-accurate text width measurement.

Purpose
=======
The TextConverter uses measured text widths to drive HTML-mode
shrink-on-overflow: when a paragraph would wrap to more lines than the
PPT-declared shape height can fit, font_size + line_height shrink
proportionally so the content fits (mirrors PPT's <a:normAutofit
fontScale/>).  The only source of truth for "what width will this string
render at" is the browser's own text-shaping pipeline — Python-side
heuristic estimates (character count × average glyph width) drift badly
for variable-width fonts and CJK.

Design
======
One Chromium instance + one Page for the lifetime of the converter.  Each
`measure_batch` call:

  1. Builds a hidden <svg> with one <text> per item, populated with the
     item's font-family / font-size / font-weight / font-style.
  2. Replaces the page body with that SVG.
  3. Single `evaluate` call walks the SVG and returns the
     `getComputedTextLength()` of each <text>, in order.

Batching matters because IPC round-trips through Playwright are expensive
(a few ms each); a slide with 50 text shapes × ~5 paragraphs each would
otherwise take seconds.

Lifecycle
=========
Caller (CLI / layout) is responsible for `start()` before any
`measure_batch` and `close()` when done.  Use try/finally to guarantee
cleanup even on errors.

Failure mode
============
If Playwright isn't installed or the browser binary is missing, `start()`
raises — the caller decides whether to fall back to no-shrink mode
(v1 behaviour: text may overflow) or surface the error.
"""

from __future__ import annotations

from typing import Optional


# SVG measurement skeleton.  We replace {{ITEMS}} at runtime; each item
# becomes a <text> element styled with its own font attributes.  The
# outer <svg> is hidden so layout work doesn't paint to the viewport.
_MEASURE_SVG_TEMPLATE = """\
<svg id="measure-root" xmlns="http://www.w3.org/2000/svg" \
style="position:absolute;left:-99999px;top:-99999px;visibility:hidden;">
{items}
</svg>
"""

_ITEM_TEMPLATE = (
    '<text x="0" y="0"{font_family}{font_size}{font_weight}{font_style}>'
    '{content}</text>'
)


class PlaywrightTextMeasurer:
    """Batched text-width measurement via headless Chromium.

    Lifecycle:
        m = PlaywrightTextMeasurer()
        m.start()
        try:
            widths = m.measure_batch([...])
        finally:
            m.close()
    """

    def __init__(self):
        self._playwright = None
        self._browser = None
        self._page = None
        self._started = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Launch Chromium and prepare a measurement page.

        Raises:
            ImportError: if playwright is not installed.
            Exception: if the Chromium binary cannot be launched.
        """
        if self._started:
            return
        from playwright.sync_api import sync_playwright  # noqa: F401 - lazy

        # Import each call so environments without playwright can still
        # import this module (TextConverter falls back to no-shrink mode).
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch()
        ctx = self._browser.new_context(viewport={"width": 1024, "height": 768})
        self._page = ctx.new_page()
        # Set a base document so subsequent set_content calls are fast.
        self._page.set_content("<html><body></body></html>")
        self._started = True

    def close(self) -> None:
        """Release the browser process.  Idempotent."""
        if not self._started:
            return
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            try:
                if self._playwright is not None:
                    self._playwright.stop()
            finally:
                self._playwright = None
                self._browser = None
                self._page = None
                self._started = False

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------------
    # Measurement
    # ------------------------------------------------------------------

    def measure_batch(self, items: list[dict]) -> list[float]:
        """Measure text widths for a batch of items.

        Args:
            items: list of dicts with keys:
                - text (str): the string to measure (already escaped by caller
                  is NOT required; we HTML-escape here).
                - font_family (str|None)
                - font_size_pt (float|None): if given, emitted as "<n>pt"
                  (matches the unit TextConverter uses for CSS font-size).
                - font_weight (str|None): "bold" or None.
                - font_style (str|None): "italic" or None.

        Returns:
            list of float, same length and order as `items`.  Each entry
            is the rendered text width in CSS pixels.

        Raises:
            RuntimeError: if start() was not called.
        """
        if not self._started:
            raise RuntimeError(
                "PlaywrightTextMeasurer.start() must be called before measure_batch()"
            )
        if not items:
            return []

        # Build all <text> nodes at once.  Use a stable id per item so the
        # JS walker can return widths in input order.
        rendered_items = []
        for i, it in enumerate(items):
            attrs = []
            family = it.get("font_family")
            if family:
                # Escape quotes inside family names; SVG attribute value.
                family_escaped = (family.replace("&", "&amp;")
                                        .replace('"', "&quot;")
                                        .replace("<", "&lt;"))
                attrs.append(f' font-family="{family_escaped}"')
            size = it.get("font_size_pt")
            if size is not None:
                attrs.append(f' font-size="{size}pt"')
            weight = it.get("font_weight")
            if weight:
                attrs.append(f' font-weight="{weight}"')
            style = it.get("font_style")
            if style:
                attrs.append(f' font-style="{style}"')
            text = it.get("text", "")
            # HTML-escape content.  &quot; is for attribute context, but
            # here we're in element-text context — use &lt;/&amp;/&gt;.
            text_escaped = (text.replace("&", "&amp;")
                                .replace("<", "&lt;")
                                .replace(">", "&gt;"))
            rendered_items.append(
                f'<text id="m{i}"{"".join(attrs)}>{text_escaped}</text>'
            )

        svg_html = _MEASURE_SVG_TEMPLATE.format(items="\n".join(rendered_items))

        # Inject into page body.  set_content would reset the page; we
        # only want to add an SVG, so use evaluate to assign innerHTML.
        n = len(items)
        widths = self._page.evaluate(
            """(svgHtml) => {
                document.body.innerHTML = svgHtml;
                const out = new Array(%n%);
                const root = document.getElementById('measure-root');
                if (!root) return out;
                const texts = root.querySelectorAll('text');
                for (let i = 0; i < texts.length; i++) {
                    out[i] = texts[i].getComputedTextLength();
                }
                return out;
            }""".replace("%n%", str(n)),
            svg_html,
        )
        return [float(w) for w in widths]


def safe_measure_batch(
    items: list[dict], measurer: Optional[PlaywrightTextMeasurer]
) -> Optional[list[float]]:
    """Convenience: measure if a measurer is provided, else return None.

    Returns None when `measurer` is None so the caller can fall back to
    single-tspan (no wrap) mode without distinguishing None-as-failure
    from None-as-disabled.
    """
    if measurer is None:
        return None
    return measurer.measure_batch(items)
