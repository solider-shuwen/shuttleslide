"""Tool-call data structures returned by the LLM client."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ToolCall:
    """A single tool call requested by the LLM.

    `arguments` is the raw JSON string as returned by the OpenAI API.
    The tool registry parses it lazily inside `dispatch()`.
    """

    id: str
    name: str
    arguments: str  # raw JSON string

    def parse_arguments(self) -> Dict[str, Any]:
        """Parse the arguments JSON string. Returns {} on parse failure.

        Kept for backward compatibility. New callers should use
        ``parse_arguments_strict`` instead — it surfaces *why* parsing
        failed so the LLM gets actionable feedback during retry loops
        rather than a generic "X must be a non-empty string" error
        from the tool handler.
        """
        import json

        if not self.arguments:
            return {}
        try:
            parsed = json.loads(self.arguments)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    def parse_arguments_strict(self) -> Tuple[Dict[str, Any], Optional[str]]:
        """Parse arguments JSON, returning ``(dict, error)``.

        On success: ``(parsed_dict, None)``.
        On failure: ``({}, error_message)`` where ``error_message``
        explains the likely root cause — empty arguments (LLM emitted
        nothing) or malformed JSON (response was truncated mid-JSON by
        the API, typically because ``max_tokens`` is too small for the
        requested output). The message is suitable for direct inclusion
        in a ``ToolResult.failure`` summary so the retry loop can feed
        it back to the LLM.
        """
        import json

        if not self.arguments:
            return {}, (
                "tool call carried no arguments JSON. This usually means "
                "the API response was truncated before any arguments "
                "could be emitted — increase max_tokens for this call. "
                "(Rare cause: the LLM genuinely forgot to fill in the "
                "arguments object.)"
            )
        try:
            parsed = json.loads(self.arguments)
        except json.JSONDecodeError as exc:
            return {}, (
                f"tool call arguments are not valid JSON ({exc.msg} at "
                f"pos {exc.pos}). The response was almost certainly "
                f"truncated mid-string by the API — increase max_tokens "
                f"for this call so the full arguments object fits."
            )
        if not isinstance(parsed, dict):
            return {}, (
                f"tool call arguments parsed to "
                f"{type(parsed).__name__}, expected a JSON object "
                f"(dict). The LLM emitted a scalar/array instead of "
                f"the expected {{...}} shape."
            )
        return parsed, None


@dataclass
class LLMResponse:
    """Parsed response from a chat completion call.

    `assistant_message` is ready to append to the messages list (contains
    role, content, and tool_calls fields in OpenAI format).

    `reasoning` holds the chain-of-thought from reasoning models (GLM-4.6+,
    DeepSeek-R1, etc.) — exposed via `reasoning_content` on the message.
    Not part of `assistant_message` since the chat API doesn't expect it back.
    """

    assistant_message: Dict[str, Any]
    tool_calls: List[ToolCall] = field(default_factory=list)
    content: Optional[str] = None       # final answer text (often empty when tools are called)
    reasoning: Optional[str] = None     # chain-of-thought from reasoning models, if any
    finish_reason: str = ""
    usage: Optional[Dict[str, int]] = None

    @property
    def has_tool_calls(self) -> bool:
        return len(self.tool_calls) > 0


@dataclass
class LLMResponseEvent:
    """A single LLM response surfaced to an observer callback.

    Emitted by each node after every `chat_with_tools` call. Lets callers
    print progress, log to file, or collect metrics without touching node internals.
    """

    stage: str                          # "theme_designer" | "outline_planner" | "slide_builder"
    iteration: int                      # 1-based attempt/iteration number
    max_iterations: int                 # bound for this loop (for "iteration 2/12" display)
    slide_index: Optional[int] = None   # 1-based when stage == "slide_builder", else None
    slide_total: Optional[int] = None   # total slide count, only for slide_builder
    content: Optional[str] = None       # final answer text
    reasoning: Optional[str] = None     # chain-of-thought (reasoning models only)
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Optional[Dict[str, int]] = None  # {prompt_tokens, completion_tokens, total_tokens}
