"""Image search providers for the web image acquisition path.

Each provider implements a single async method ``search(query, ...)``.
Async is required because the production-grade provider (Bing web scrape)
drives Playwright; even the stub returns awaitable results so callers
don't have to branch on provider type.

Adding a new provider means: write a class with the same async method
signature, register it in ``make_search_provider(name, ...)``.

The aspect_ratio → Bing aspect mapping is intentionally coarse: Bing
only exposes Square/Tall/Wide, so 16:9/4:3/3:2 all map to Wide. The
download step preserves the candidate's native dimensions; if a strict
crop is needed, downstream code can PIL-resize after download.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class ImageCandidate:
    """One search hit returned by a provider."""

    url: str
    width: Optional[int] = None
    height: Optional[int] = None
    thumb_url: Optional[str] = None
    # Provider name for diagnostics ("bing_web" / "stub" / ...).
    source: str = ""
    # Free-form provider metadata (Bing's contentSource, etc.).
    meta: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class ImageSearchProvider(Protocol):
    """Async search interface.

    The production provider (BingWebScrapeSearchProvider) drives
    Playwright; the stub returns canned results. Callers ``await``
    the result either way — no thread juggling required.
    """

    async def search(
        self,
        query: str,
        max_results: int = 3,
        aspect_ratio: Optional[str] = None,
    ) -> List[ImageCandidate]:
        ...


class StubImageSearchProvider:
    """Returns a fixed list of candidates — for tests and offline dev.

    Async to match the protocol even though it does no I/O. Inject via
    ``make_search_provider("stub", candidates=[...])``.
    """

    requires_browser: bool = False

    def __init__(self, candidates: Optional[List[ImageCandidate]] = None):
        self._candidates = candidates or []

    async def search(
        self,
        query: str,
        max_results: int = 3,
        aspect_ratio: Optional[str] = None,
    ) -> List[ImageCandidate]:
        return list(self._candidates[:max_results])


def make_search_provider(
    name: str,
    *,
    base_url: str = "",
    candidates: Optional[List[ImageCandidate]] = None,
) -> ImageSearchProvider:
    """Factory keyed by provider name.

    Centralises the mapping so config code only needs to pass a string
    (e.g. "bing_web" / "stub") plus credentials/base_url, without
    knowing the class hierarchy. Unknown names raise ValueError — fail
    loud at config time, not silently at search time.
    """
    name = name.lower()
    if name == "bing_web":
        # Default to cn.bing.com (accessible behind the GFW). Callers
        # who want the international results pass www.bing.com via the
        # image_search_base_url config field.
        url = base_url or "https://cn.bing.com"
        # Late import: keeps the BingWebScrapeSearchProvider import
        # chain (which pulls playwright transitively) out of test
        # collection when only the stub is exercised.
        from shuttleslide.agent.nodes.image_sources.bing_web import (
            BingWebScrapeSearchProvider,
        )
        return BingWebScrapeSearchProvider(base_url=url)
    if name == "stub":
        return StubImageSearchProvider(candidates=candidates)
    raise ValueError(
        f"unknown image search provider {name!r}; "
        f"supported: 'bing_web', 'stub'"
    )
