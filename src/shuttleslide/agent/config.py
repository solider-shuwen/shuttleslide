"""Configuration for the agent pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from shuttleslide.agent.llm.tool_call import LLMResponseEvent


@dataclass
class AgentConfig:
    """Configuration for the agent pipeline.

    All LLM-related fields can be set via constructor arguments or
    environment variables. Constructor arguments take precedence.

    Environment variables:
      SHUTTLESLIDE_API_BASE  – OpenAI-compatible base URL
      SHUTTLESLIDE_API_KEY   – API key
      SHUTTLESLIDE_MODEL     – Model name (e.g. "glm-4.7", "gpt-4o-mini")
    """

    # LLM connection (required for actual generation; can be left blank for tests)
    api_base: str = ""
    api_key: str = ""
    model: str = ""

    # Provider compatibility flag. Flip to True when targeting an endpoint
    # whose thinking mode rejects ``tool_choice="required"`` — the only
    # known case today is DeepSeek's deepseek-reasoner under thinking mode
    # (HTTP 400 ``Thinking mode does not support this tool_choice``). When
    # True, LLMClient silently rewrites ``"required"`` -> ``"auto"``; the
    # node code keeps ``tool_choice="required"`` everywhere so it stays
    # provider-agnostic.
    disable_required_tool_choice: bool = False

    # Generation parameters
    temperature: float = 0.7
    # None = don't send max_tokens to the API (let the model use its default
    # maximum output length). Needed for some provider/model combinations
    # that reject explicit caps (e.g. DeepSeek deepseek-reasoner under
    # thinking mode). A non-None integer is forwarded as-is.
    max_tokens: Optional[int] = 16384
    # SVG generation can need more output tokens than the default — the
    # generated markup can hit several thousand characters even for simple
    # illustrations. When the SVG_GENERATOR_PROMPT's target char range is
    # wider than what the default max_tokens allows, tool-call arguments
    # get truncated mid-JSON, parse_arguments silently returns {}, and
    # set_svg reports "svg must be a non-empty string" — which the LLM
    # cannot diagnose. The svg_generator stage reads this override and
    # forwards it instead of max_tokens. None = inherit max_tokens.
    svg_generator_max_tokens: Optional[int] = 16384

    # Pipeline control
    topic: str = ""
    style_hint: str = "business"
    target_slide_count: Optional[int] = None  # None = LLM infers from topic
    max_tool_iterations: int = 12  # per slide

    # Output directory (None = don't write files; caller handles result)
    output_dir: Optional[str] = None

    # Optional observer invoked after every chat_with_tools call inside each node.
    # Signature: (event: LLMResponseEvent) -> None. None = silent (default).
    on_llm_response: Optional[Callable[["LLMResponseEvent"], None]] = None

    # ------------------------------------------------------------------
    # Web image acquisition (Stage 2.5 web path)
    # ------------------------------------------------------------------
    # When the outline declares source_type="web" image specs, the
    # image_acquirer routes them through a search provider + VLM
    # verifier. All fields below are optional — the pipeline still runs
    # without them, but web specs fall back to SVG (with a warning).

    # Image search provider name. Supported: "bing_web" (Playwright
    # scrape of bing.com/images/search — no API key needed), "stub"
    # (canned candidates for tests). Empty = no provider; web specs
    # cannot acquire and fall back to svg.
    image_search_provider: str = ""
    # Bing base URL for the scraping provider. Defaults to cn.bing.com
    # (works behind the GFW). Override to www.bing.com for international
    # results, or any other Bing regional subdomain.
    image_search_base_url: str = "https://cn.bing.com"
    # API key for the chosen provider. Reserved for future providers
    # that need credentials; bing_web ignores it.
    image_search_api_key: str = ""

    # VLM connection. Defaults to the text LLM endpoint when blank so a
    # single OpenAI-compatible deployment that also serves a vision model
    # (e.g. gpt-4o, glm-4.6v) just works. Override when the VLM lives on
    # a separate endpoint.
    vlm_api_base: str = ""
    vlm_api_key: str = ""
    vlm_model: str = ""

    # Master switch for VLM verification. When False, web candidates are
    # accepted without verification (faster, cheaper, but the photo may
    # not match the description — use only for offline dev / tests).
    enable_vlm_verification: bool = True

    # ------------------------------------------------------------------
    # Canvas dimensions
    # ------------------------------------------------------------------
    # canvas_width_emu / canvas_height_emu drive:
    #   - the .ppt-slide CSS dimensions emitted into the rendered HTML
    #   - the slide_width_emu / slide_height_emu written into the PPTX
    #   - the position-percentage denominators in extract_layout.js
    # Defaults reproduce the historical 16:9 slide (1280x720 CSS px).
    canvas_width_emu: int = 12192000
    canvas_height_emu: int = 6858000

    @classmethod
    def from_env(cls, **overrides) -> "AgentConfig":
        """Build a config from environment variables, allowing caller overrides."""
        defaults = dict(
            api_base=os.environ.get("SHUTTLESLIDE_API_BASE", ""),
            api_key=os.environ.get("SHUTTLESLIDE_API_KEY", ""),
            model=os.environ.get("SHUTTLESLIDE_MODEL", ""),
            image_search_provider=os.environ.get(
                "SHUTTLESLIDE_IMAGE_SEARCH_PROVIDER", ""
            ),
            image_search_base_url=os.environ.get(
                "SHUTTLESLIDE_IMAGE_SEARCH_BASE_URL",
                "https://cn.bing.com",
            ),
            image_search_api_key=os.environ.get(
                "SHUTTLESLIDE_IMAGE_SEARCH_API_KEY", ""
            ),
            vlm_api_base=os.environ.get("SHUTTLESLIDE_VLM_API_BASE", ""),
            vlm_api_key=os.environ.get("SHUTTLESLIDE_VLM_API_KEY", ""),
            vlm_model=os.environ.get("SHUTTLESLIDE_VLM_MODEL", ""),
        )
        # Bool flag — same string-parsing path as enable_vlm_verification.
        # Empty / unset leaves the dataclass default (False).
        drtc = os.environ.get("SHUTTLESLIDE_DISABLE_REQUIRED_TOOL_CHOICE", "")
        if drtc:
            defaults["disable_required_tool_choice"] = drtc.lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
        # enable_vlm_verification reads from env as a string flag — but
        # env vars are strings, so we parse it explicitly. Empty / unset
        # defaults to True (the safer choice for production).
        vlm_switch = os.environ.get("SHUTTLESLIDE_ENABLE_VLM_VERIFICATION", "")
        if vlm_switch:
            defaults["enable_vlm_verification"] = vlm_switch.lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
        defaults.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**defaults)

    def validate(self) -> None:
        """Raise ValueError if required fields are missing."""
        if not self.api_base:
            raise ValueError(
                "api_base is required (set SHUTTLESLIDE_API_BASE env var or pass --api-base)"
            )
        if not self.api_key:
            raise ValueError(
                "api_key is required (set SHUTTLESLIDE_API_KEY env var or pass --api-key)"
            )
        if not self.model:
            raise ValueError(
                "model is required (set SHUTTLESLIDE_MODEL env var or pass --model)"
            )
        # Web path validation: surface configuration mistakes loudly at
        # startup rather than as silent fallbacks at runtime. A web spec
        # hitting an unconfigured pipeline is still recoverable (falls
        # back to SVG), but the user almost certainly meant to configure
        # something and didn't.
        if self.image_search_provider:
            if self.image_search_provider.lower() not in ("bing_web", "stub"):
                raise ValueError(
                    f"image_search_provider {self.image_search_provider!r} "
                    f"is not supported (use 'bing_web' or 'stub')"
                )
        if self.image_search_provider and self.enable_vlm_verification and not self.vlm_model:
            raise ValueError(
                "image_search_provider + enable_vlm_verification require vlm_model "
                "(set SHUTTLESLIDE_VLM_MODEL or pass --vlm-model; "
                "or set enable_vlm_verification=False to skip VLM checks)"
            )

        # VLM endpoint pairing: when the user sets vlm_model, the VLM
        # almost always lives on a SEPARATE endpoint from the text LLM
        # (e.g. text on DeepSeek, vision on Zhipu GLM-4V). The
        # orchestrator silently falls back to api_base/api_key when
        # vlm_api_base/vlm_api_key are blank — that fallback produces
        # requests to the wrong server, which then fail with deeply
        # misleading errors like "unknown variant `image_url`, expected
        # `text`" (the text endpoint's deserializer rejecting multimodal
        # content). Force the user to be explicit here.
        if self.vlm_model and not (self.vlm_api_base and self.vlm_api_key):
            # Allow the rare same-endpoint case: user can set
            # vlm_api_base = api_base explicitly to opt in.
            raise ValueError(
                f"vlm_model {self.vlm_model!r} is set but vlm_api_base / "
                f"vlm_api_key are blank. The VLM usually lives on a "
                f"separate endpoint from the text LLM — set both "
                f"explicitly. If your text endpoint also serves the "
                f"vision model (same provider, same key), pass "
                f"vlm_api_base=api_base and vlm_api_key=api_key to opt in. "
                f"Without this check, the orchestrator silently falls back "
                f"to the text endpoint and VLM calls fail with confusing "
                f"deserialization errors."
            )
