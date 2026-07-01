"""Bing image search via web scraping — no API key required.

Instead of calling the Bing Image Search v7 API (which needs an Azure
subscription key), this provider drives a headless browser to the public
``bing.com/images/search`` page and extracts candidates from the DOM.

Trade-offs vs the API (which we removed deliberately):
  + Zero config: no Azure key, no signup, no credit card.
  + Works behind the GFW via cn.bing.com.
  - Slower: 1-3s per query (browser startup + render) vs 100-300ms for API.
  - Brittle: if Bing ships a DOM redesign, ``a.iusc`` selectors break.
    The structure has been stable for years, but there's no SLA.
  - ToS: scraping technically violates Bing's terms. Personal /
    small-scale use is fine; for commercial deployment use a licensed
    image provider.

Bing stores each result's metadata as JSON in the ``m`` attribute of an
``<a class="iusc">`` anchor. The JSON includes ``murl`` (the original
image URL), ``mw`` / ``mh`` (dimensions), and ``turl`` (thumbnail). We
parse that and skip anything that doesn't yield an ``murl``.
"""

from __future__ import annotations

import logging
from typing import Any, List, Optional
from urllib.parse import quote

from shuttleslide.agent.nodes.image_sources.search import (
    ImageCandidate,
    ImageSearchProvider,
)

logger = logging.getLogger(__name__)

# Map our aspect_ratio enum onto Bing's URL filter syntax. Bing uses
# Wide / Tall / Square — anything 4:3 / 16:9 / 3:2 collapses to Wide.
# Without this filter Bing happily returns mixed aspects and the slide
# layout breaks.
_ASPECT_TO_BING_FILTER = {
    "16:9": "Wide",
    "4:3": "Wide",
    "3:2": "Wide",
    "1:1": "Square",
    "2:3": "Tall",
}

# How long to wait for Bing to render before we read the DOM. Bing Images
# hydrates results client-side, so domcontentloaded alone isn't enough.
_GOTO_TIMEOUT_MS = 15_000

# How many times to scroll-and-wait to trigger lazy-loaded results. Two
# scrolls typically surface 30+ candidates, which is plenty given we
# cap at _MAX_CANDIDATES downstream.
_SCROLL_ITERATIONS = 2
_SCROLL_WAIT_MS = 500

# Real-Chrome UA + en-US locale. cn.bing.com silently ignores the
# ``site:`` URL filter when the request carries the default HeadlessChrome
# UA + a zh-CN system locale — Bing's anti-bot layer treats that
# combination as automated and serves the unfiltered default result set.
# Spoofing a real Chrome UA + en-US locale makes ``site:`` actually take
# effect (verified 2026-06-30: same URL, default config → 0/5 pixabay,
# this config → 5/5 pixabay).
_REAL_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# JavaScript extractor. Runs in the page context. Takes the desired
# candidate count and returns up to 3x that number (so the Python side
# has room to filter broken URLs / wrong mime without re-scraping).
_EXTRACT_SCRIPT = """
(maxResults) => {
    const out = [];
    const anchors = document.querySelectorAll('a.iusc');
    for (const a of anchors) {
        if (out.length >= maxResults * 3) break;
        const m = a.getAttribute('m');
        if (!m) continue;
        try {
            const data = JSON.parse(m);
            if (data.murl) {
                out.push({
                    url: data.murl,
                    thumb_url: data.turl || '',
                    width: data.mw || null,
                    height: data.mh || null,
                });
            }
        } catch (e) {}
    }
    return out;
}
"""


class BingWebScrapeSearchProvider(ImageSearchProvider):
    """Scrape Bing image search via Playwright — no API key needed.

    Lifecycle: construct once, then call ``attach_browser_manager(bm)``
    before the first ``search`` call. The browser manager is owned by
    the caller (the orchestrator) so multiple searches in one deck
    share a single Chromium instance — paying the 3-5s startup only once.
    """

    # Tag for the orchestrator: this provider needs a browser. Other
    # providers (e.g. StubImageSearchProvider) don't and won't force
    # Chromium to launch.
    requires_browser: bool = True

    def __init__(
        self,
        base_url: str = "https://cn.bing.com",
        site_filter: Optional[str] = None#"pixabay.com",
    ):
        # Strip trailing slash so URL composition is uniform downstream.
        self.base_url = base_url.rstrip("/")
        # ``site:`` filter appended to Bing's ``qft`` URL parameter.
        # Defaults to pixabay.com because cn.bing.com's keyword matching is
        # poor — measured 2026-06-30:
        #   - "team collaboration office photo" → Microsoft Teams app
        #     screenshots (literal keyword hit on "team")
        #   - "minimalist poster design"       → minimalist room interiors
        #     (matched "minimalist", ignored "poster")
        #   - "modern coffee shop interior"    → zcool/danci/sinaimg (CN
        #     content pool, mostly irrelevant)
        # Restricting to pixabay.com (a stock-photo library indexed well by
        # cn.bing.com — 5/5 hits in testing) restores semantic-match
        # quality. unsplash.com / pexels.com return 0 results on cn.bing.com
        # due to thin index coverage, so pixabay is the only viable default.
        # Pass ``None`` to restore the unfiltered Bing behavior.
        self.site_filter = site_filter or None
        self._browser_manager: Optional[Any] = None

    def attach_browser_manager(self, browser_manager: Any) -> None:
        """Inject the shared BrowserManager. Must be called before search."""
        self._browser_manager = browser_manager

    async def search(
        self,
        query: str,
        max_results: int = 3,
        aspect_ratio: Optional[str] = None,
    ) -> List[ImageCandidate]:
        if self._browser_manager is None:
            logger.warning(
                "BingWebScrapeSearchProvider: no browser attached; "
                "call attach_browser_manager() before search()"
            )
            return []
        if not query.strip():
            return []

        url = self._build_search_url(query, aspect_ratio)
        # Open a context (not just a page) with locale=en-US + real Chrome
        # UA. Without this, cn.bing.com silently ignores ``site:`` filters
        # and returns its default (often irrelevant) result set — see
        # _REAL_CHROME_UA docstring.
        try:
            context = await self._browser_manager.new_context(
                locale="en-US",
                user_agent=_REAL_CHROME_UA,
            )
        except Exception as exc:
            logger.warning(
                "BingWebScrapeSearchProvider: failed to open context: %s", exc
            )
            return []
        try:
            page = await context.new_page()
        except Exception as exc:
            logger.warning(
                "BingWebScrapeSearchProvider: failed to open page: %s", exc
            )
            try:
                await context.close()
            except Exception:
                pass
            return []

        try:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)
            except Exception as exc:
                logger.warning(
                    "BingWebScrapeSearchProvider: goto %s failed: %s", url, exc
                )
                return []

            # Bing lazy-loads results as you scroll. Without these
            # nudges the first paint only has ~5 candidates.
            for _ in range(_SCROLL_ITERATIONS):
                try:
                    await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(_SCROLL_WAIT_MS)
                except Exception:
                    # Scroll failure is non-fatal — work with whatever
                    # Bing has already rendered.
                    break

            try:
                raw_candidates = await page.evaluate(_EXTRACT_SCRIPT, max_results)
            except Exception as exc:
                logger.warning(
                    "BingWebScrapeSearchProvider: evaluate failed: %s", exc
                )
                return []
        finally:
            try:
                await page.close()
            except Exception:
                pass
            try:
                await context.close()
            except Exception:
                pass

        out: List[ImageCandidate] = []
        for c in raw_candidates:
            url_val = c.get("url") if isinstance(c, dict) else None
            if not url_val:
                continue
            out.append(
                ImageCandidate(
                    url=url_val,
                    thumb_url=c.get("thumb_url") or None,
                    width=c.get("width"),
                    height=c.get("height"),
                    source="bing_web",
                )
            )
            if len(out) >= max_results:
                break
        # Surface the resolved URLs at INFO so the review-pipeline console
        # shows what the image searcher actually got back — essential for
        # diagnosing "search returned irrelevant results" complaints
        # (cn.bing.com keyword matching is poor without site_filter).
        if out:
            logger.info(
                "Bing image search %r (site=%s, aspect=%s) → %d candidate(s):",
                query,
                self.site_filter or "<none>",
                aspect_ratio,
                len(out),
            )
            for i, c in enumerate(out, 1):
                logger.info("  [%d] %s", i, c.url)
        else:
            logger.info(
                "Bing image search %r → 0 candidates (site=%s, aspect=%s)",
                query,
                self.site_filter or "<none>",
                aspect_ratio,
            )
        return out

    def _build_search_url(self, query: str, aspect_ratio: Optional[str]) -> str:
        """Compose the Bing image search URL with optional aspect + site filters.

        Bing chains multiple ``qft`` filters with ``+`` separators:
        ``&qft=+filterui:aspect-Wide+site:pixabay.com``. The leading ``+``
        is required by Bing's URL parser; subsequent filters are joined
        with ``+`` so the whole ``qft`` value stays in one query param.
        """
        url = f"{self.base_url}/images/search?q={quote(query)}&form=HDRSC2"
        qft_parts: List[str] = []
        aspect_filter = _ASPECT_TO_BING_FILTER.get(aspect_ratio or "")
        if aspect_filter:
            qft_parts.append(f"filterui:aspect-{aspect_filter}")
        if self.site_filter:
            qft_parts.append(f"site:{self.site_filter}")
        if qft_parts:
            url += "&qft=+" + "+".join(qft_parts)
        return url
