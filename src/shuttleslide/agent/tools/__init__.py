"""Tools subpackage."""

from shuttleslide.agent.tools.registry import (
    ToolRegistry,
    ToolResult,
    ToolSpec,
    get_default_registry,
    tool,
)

# Importing slide_tools registers its tools with the default registry.
# We do this here so a single `from shuttleslide.agent.tools import get_default_registry`
# gives callers a fully-populated registry.
from shuttleslide.agent.tools import slide_tools as _slide_tools  # noqa: F401
from shuttleslide.agent.tools import theme_tools as _theme_tools  # noqa: F401
from shuttleslide.agent.tools import outline_tools as _outline_tools  # noqa: F401

__all__ = [
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "get_default_registry",
    "tool",
]
