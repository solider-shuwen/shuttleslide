"""Image source subpackage — web image acquisition for the agent pipeline.

Public API:
    acquire_web_image: orchestrate search → download → verify → store.
    ImageSearchProvider / ImageCandidate: search provider interface.
    BingWebScrapeSearchProvider / StubImageSearchProvider: implementations.
    make_search_provider: factory keyed by provider name.
    VLMVerifier: judges whether a fetched image matches a description.
    capture_url_screenshot: playwright screenshot for URL source_refs.
"""

from shuttleslide.agent.nodes.image_sources.acquire import acquire_web_image
from shuttleslide.agent.nodes.image_sources.bing_web import (
    BingWebScrapeSearchProvider,
)
from shuttleslide.agent.nodes.image_sources.screenshot import capture_url_screenshot
from shuttleslide.agent.nodes.image_sources.search import (
    ImageCandidate,
    ImageSearchProvider,
    StubImageSearchProvider,
    make_search_provider,
)
from shuttleslide.agent.nodes.image_sources.verifier import VLMVerifier

__all__ = [
    "acquire_web_image",
    "capture_url_screenshot",
    "BingWebScrapeSearchProvider",
    "ImageCandidate",
    "ImageSearchProvider",
    "StubImageSearchProvider",
    "make_search_provider",
    "VLMVerifier",
]
