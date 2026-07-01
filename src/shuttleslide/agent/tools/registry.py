"""Tool registry: declarative tool definitions + dispatch.

Tools are registered via the `@tool` decorator with:
  - a name (used by the LLM as the function name)
  - a group (which node exposes this tool — e.g. "slide_builder")
  - a JSON schema for parameters (OpenAI function-calling format)
  - a description (shown to the LLM)
  - an async handler `handler(params_dict, ctx) -> ToolResult`

The registry's `dispatch()` method invokes the handler with a context
dict carrying whatever the calling node passed in (typically `slide`,
`theme`, etc.).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from shuttleslide.agent.llm.tool_call import ToolCall


@dataclass
class ToolResult:
    """Result returned by a tool handler."""

    ok: bool
    summary: str  # short string fed back to the LLM as a tool message
    error: Optional[str] = None

    @classmethod
    def success(cls, summary: str) -> "ToolResult":
        return cls(ok=True, summary=summary, error=None)

    @classmethod
    def failure(cls, error: str) -> "ToolResult":
        return cls(ok=False, summary=f"ERROR: {error}", error=error)


# Handler signature: async (params: dict, ctx: dict) -> ToolResult
ToolHandler = Callable[[Dict[str, Any], Dict[str, Any]], Any]


@dataclass
class ToolSpec:
    """Specification of a single tool."""

    name: str
    description: str
    params: Dict[str, Any]  # JSON schema (OpenAI function params format)
    handler: ToolHandler
    groups: List[str] = field(default_factory=list)


class ToolRegistry:
    """Holds all registered tools, indexed by name. Dispatches by group."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._tools.get(name)

    def names(self, group: Optional[str] = None) -> List[str]:
        if group is None:
            return list(self._tools.keys())
        return [name for name, spec in self._tools.items() if group in spec.groups]

    def openai_schema_for(self, group: str) -> List[Dict[str, Any]]:
        """Return tools in OpenAI function-calling JSON format for a group."""
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.params,
                },
            }
            for spec in self._tools.values()
            if group in spec.groups
        ]

    async def dispatch(self, call: ToolCall, **ctx: Any) -> ToolResult:
        """Execute a tool call. Always returns ToolResult, never raises."""
        spec = self._tools.get(call.name)
        if spec is None:
            return ToolResult.failure(f"unknown tool: {call.name}")

        # Use the strict parser so JSON-deserialization failures surface a
        # diagnosis the LLM can act on during retry (e.g. "truncated by
        # max_tokens"). The legacy silent {} return made every downstream
        # "missing required argument" error indistinguishable from
        # "LLM never sent arguments because the response was cut off".
        params, parse_error = call.parse_arguments_strict()
        if parse_error is not None:
            # Include a short preview of the raw arguments so the LLM
            # (and the human reading the log) can see whether the
            # payload was empty, partial, or malformed.
            preview = call.arguments[:200]
            return ToolResult.failure(
                f"could not parse arguments for tool {call.name!r}: "
                f"{parse_error} Raw arguments (first 200 chars): {preview!r}"
            )
        try:
            result = await spec.handler(params, ctx)
            if not isinstance(result, ToolResult):
                return ToolResult.failure(
                    f"tool {call.name} returned non-ToolResult: {type(result).__name__}"
                )
            return result
        except Exception as e:
            return ToolResult.failure(f"tool {call.name} raised: {e}")


# ---------------------------------------------------------------------------
# Decorator API
# ---------------------------------------------------------------------------

_DEFAULT_REGISTRY = ToolRegistry()


def tool(
    name: str,
    description: str,
    params: Dict[str, Any],
    groups: List[str],
    registry: ToolRegistry = _DEFAULT_REGISTRY,
):
    """Decorator to register a function as a tool.

    Usage:
        @tool("add_text_box", description="...", params={...}, groups=["slide_builder"])
        async def add_text_box(params, ctx) -> ToolResult: ...
    """

    def decorator(func: ToolHandler) -> ToolHandler:
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                params=params,
                handler=func,
                groups=list(groups),
            )
        )
        return func

    return decorator


def get_default_registry() -> ToolRegistry:
    """Returns the module-level default registry.

    Importing the tool modules (`slide_tools`, `theme_tools`, ...)
    populates this registry via the @tool decorator.
    """
    return _DEFAULT_REGISTRY


# ---------------------------------------------------------------------------
# Shared JSON-schema fragments (reused across tools)
# ---------------------------------------------------------------------------

POSITION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Position as percentages of the slide canvas (0–100). The canvas dimensions are determined by AgentConfig.canvas_width_emu / canvas_height_emu.",
    "properties": {
        "x_pct": {"type": "number"},
        "y_pct": {"type": "number"},
        "w_pct": {"type": "number"},
        "h_pct": {"type": "number"},
    },
    "required": ["x_pct", "y_pct", "w_pct", "h_pct"],
}

BACKGROUND_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Background fill: solid color, gradient, or image URL.",
    "properties": {
        "type": {"type": "string", "enum": ["solid", "gradient", "image", "none"]},
        "color": {"type": "string", "description": "Hex color, 6-digit (#RRGGBB) or 8-digit with alpha (#RRGGBBAA)"},
        "gradient": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["horizontal", "vertical", "diagonal_135", "diagonal_45"],
                },
                "stops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "color": {"type": "string"},
                            "position": {"type": "number", "description": "0.0 to 1.0"},
                            "opacity": {"type": "number", "description": "0.0 to 1.0"},
                        },
                        "required": ["color", "position", "opacity"],
                    },
                },
            },
            "required": ["direction", "stops"],
        },
        "image_url": {"type": "string"},
    },
    "required": ["type"],
}

BORDER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "color": {"type": "string"},
        "width_pt": {"type": "number"},
        "style": {"type": "string", "enum": ["solid", "dashed", "dotted"]},
    },
    "required": ["color", "width_pt", "style"],
}

SHADOW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "color": {"type": "string", "description": "Hex with alpha, e.g. #00000044"},
        "blur_pt": {"type": "number"},
        "offset_x_pt": {"type": "number"},
        "offset_y_pt": {"type": "number"},
    },
    "required": ["color", "blur_pt", "offset_x_pt", "offset_y_pt"],
}
