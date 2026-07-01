"""
Playwright browser manager — lifecycle management for headless Chromium.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page, Playwright

logger = logging.getLogger(__name__)

DEFAULT_VIEWPORT = (1280, 720)


class BrowserManager:
    """Manages a Playwright browser instance lifecycle.

    Usage:
        mgr = BrowserManager()
        await mgr.start()
        page = await mgr.new_page()
        # ... use page ...
        await mgr.stop()

    For callers that need to influence how the site responds (locale /
    user agent), use ``new_context()`` instead of ``new_page()`` — some
    sites (notably cn.bing.com) serve different content based on these
    headers, and the default ``new_page()`` skips them entirely.
    """

    def __init__(self, viewport: tuple[int, int] = DEFAULT_VIEWPORT):
        self.viewport = viewport
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None

    @property
    def browser(self) -> Browser:
        assert self._browser is not None, "Browser not started. Call start() first."
        return self._browser

    async def start(self) -> None:
        """Launch headless Chromium."""
        if self._browser is not None:
            return  # Already started
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        logger.info("Browser started (viewport=%dx%d)", self.viewport[0], self.viewport[1])

    async def stop(self) -> None:
        """Close browser and release resources."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None
        logger.info("Browser stopped")

    async def new_page(self) -> Page:
        """Create a new page with the configured viewport."""
        page = await self.browser.new_page(
            viewport={"width": self.viewport[0], "height": self.viewport[1]},
        )
        return page

    async def new_context(
        self,
        locale: Optional[str] = None,
        user_agent: Optional[str] = None,
        **extra: Any,
    ) -> BrowserContext:
        """Create a new browser context with optional locale / UA overrides.

        Use this instead of ``new_page()`` when the target site responds
        differently to different locales or user agents. Concrete case:
        cn.bing.com silently ignores the ``site:`` URL filter when the
        request carries the default ``HeadlessChrome`` UA + system locale
        (typically ``zh-CN`` on a Chinese Windows box), so callers that
        rely on ``site:`` (e.g. BingWebScrapeSearchProvider) must open a
        context with ``locale="en-US"`` and a real-Chrome UA to make the
        filter actually take effect.

        Caller owns the returned context's lifecycle — close it when
        done (typically in a ``finally`` block around ``context.new_page()``).
        """
        kwargs: dict[str, Any] = {
            "viewport": {"width": self.viewport[0], "height": self.viewport[1]},
        }
        if locale:
            kwargs["locale"] = locale
        if user_agent:
            kwargs["user_agent"] = user_agent
        kwargs.update(extra)
        return await self.browser.new_context(**kwargs)
