"""Agent pipeline: convert any content into PPT-style HTML.

High-level entry point:

    from shuttleslide.agent import generate_slides

    result = await generate_slides(
        topic="Introduction to Machine Learning",
        style_hint="business",
        output_dir="tmp/gen_output/",
    )

Pipeline:
  Stage 1: Theme Designer   — define_theme tool (1 call)
  Stage 2: Outline Planner  — define_outline tool (1 call)
  Stage 3: Slide Builder    — element-by-element tools (N calls, 1 per slide)
  Stage 4: HTML Renderer    — deterministic Jinja2 rendering (no LLM)

Works with any OpenAI-compatible LLM endpoint (Zhipu, DeepSeek, OpenAI,
vLLM, Ollama) by setting api_base / api_key / model.
"""

from shuttleslide.agent.callbacks import (
    format_event,
    make_file_logger,
    make_jsonl_logger,
    print_llm_response,
)
from shuttleslide.agent.config import AgentConfig
from shuttleslide.agent.dsl_to_html import SlideHTMLRenderer
from shuttleslide.agent.llm import LLMClient, LLMResponse, LLMResponseEvent, ToolCall
from shuttleslide.agent.orchestrator import (
    AgentOrchestrator,
    OrchestratorResult,
    generate_slides,
)
from shuttleslide.agent.state import AgentState
from shuttleslide.agent.tools.registry import ToolRegistry, ToolResult, get_default_registry

# Importing the tool modules populates the default registry via the @tool decorator.
from shuttleslide.agent.tools import outline_tools as _outline_tools  # noqa: F401
from shuttleslide.agent.tools import slide_tools as _slide_tools  # noqa: F401
from shuttleslide.agent.tools import svg_tools as _svg_tools  # noqa: F401
from shuttleslide.agent.tools import theme_tools as _theme_tools  # noqa: F401

__all__ = [
    "AgentConfig",
    "AgentOrchestrator",
    "AgentState",
    "LLMClient",
    "LLMResponse",
    "LLMResponseEvent",
    "ToolCall",
    "OrchestratorResult",
    "SlideHTMLRenderer",
    "ToolRegistry",
    "ToolResult",
    "format_event",
    "generate_slides",
    "get_default_registry",
    "make_file_logger",
    "make_jsonl_logger",
    "print_llm_response",
]
