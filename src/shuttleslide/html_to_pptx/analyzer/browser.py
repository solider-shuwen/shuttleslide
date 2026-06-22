"""
Playwright browser manager — lifecycle management for headless Chromium.
"""

from __future__ import annotations

import logging
from typing import Optional

from playwright.async_api import async_playwright, Browser, Page, Playwright

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
        self._browser = await self._playwright.chromium.launch(headless=False)
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
