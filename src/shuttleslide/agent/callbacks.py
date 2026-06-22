"""Default observer callbacks for LLM response events.

The agent pipeline accepts a single `on_llm_response` callback (set on
`AgentConfig`) that gets invoked after every `chat_with_tools` call inside
each node. This module ships ready-to-use implementations:

- `print_llm_response` — print formatted output to stdout (streams live)
- `make_file_logger(path)` — return a callback that appends to a file
- `format_event(event)` — pure formatter, useful for composing your own sink

For "print AND log to file", just call both inside your own callback:

    def my_callback(event):
        print_llm_response(event)
        file_logger(event)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from shuttleslide.agent.llm.tool_call import LLMResponseEvent

# Truncation limits — keep output readable. Callers wanting the full payload
# can read it from `event` directly instead of using these helpers.
_REASONING_PREVIEW = 800  # max chars of chain-of-thought to render (CoT can be long)
_CONTENT_PREVIEW = 500    # max chars of final answer text to render
_ARG_PREVIEW = 200        # max chars per tool-call arguments string


def _truncate(text: str, limit: int) -> str:
    """Collapse whitespace runs and truncate to `limit` chars with a trailing marker."""
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "..."
    return text


def format_event(event: LLMResponseEvent) -> str:
    """Render an event as a multi-line human-readable string.

    Layout:
        [stage] slide 3/8, iteration 2/12     <- header (slide context only when relevant)
          reasoning: <CoT preview>             <- chain-of-thought from reasoning models
          content:  <answer preview>           <- final answer text (often empty with tool calls)
          tool: name(arg=..., arg=...)         <- one line per tool call
          tokens: 412 in / 96 out              <- omitted if no usage info
    """
    lines: list[str] = []

    # ---- header ----
    if event.slide_index is not None and event.slide_total is not None:
        header = (
            f"[{event.stage}] slide {event.slide_index}/{event.slide_total}, "
            f"iteration {event.iteration}/{event.max_iterations}"
        )
    else:
        header = f"[{event.stage}] attempt {event.iteration}/{event.max_iterations}"
    lines.append(header)

    # ---- reasoning (chain-of-thought) ----
    # This is the real "thinking" from reasoning models like GLM-4.7 / DeepSeek-R1.
    if event.reasoning:
        lines.append(f"  reasoning: {_truncate(event.reasoning, _REASONING_PREVIEW)}")

    # ---- content (final answer text) ----
    if event.content:
        lines.append(f"  content: {_truncate(event.content, _CONTENT_PREVIEW)}")

    # ---- tool calls ----
    for call in event.tool_calls:
        args = call.arguments or ""
        if len(args) > _ARG_PREVIEW:
            args = args[:_ARG_PREVIEW].rstrip() + "..."
        lines.append(f"  tool: {call.name}({args})")

    # ---- token usage ----
    if event.usage:
        prompt = event.usage.get("prompt_tokens", 0)
        completion = event.usage.get("completion_tokens", 0)
        total = event.usage.get("total_tokens", prompt + completion)
        lines.append(f"  tokens: {prompt} in / {completion} out / {total} total")

    return "\n".join(lines)


def print_llm_response(event: LLMResponseEvent) -> None:
    """Print a formatted event to stdout. Flushes so output streams live.

    Pair with `make_file_logger` if you want both stdout and a persistent log.
    """
    print(format_event(event), flush=True)


def make_file_logger(path: Path) -> Callable[[LLMResponseEvent], None]:
    """Return a callback that appends formatted events to `path`.

    The file is opened in append mode on every call so partial output
    survives a crash. Use a fresh path per run if you want a clean log.
    """
    path = Path(path)

    def _log(event: LLMResponseEvent) -> None:
        # Append mode + open/close per call = crash-safe, low throughput cost
        # for the volumes we're dealing with (a few hundred LLM calls max).
        with path.open("a", encoding="utf-8") as f:
            f.write(format_event(event) + "\n\n")

    return _log


def make_jsonl_logger(path: Path) -> Callable[[LLMResponseEvent], None]:
    """Return a callback that appends raw events as JSONL (one JSON object per line).

    Useful for post-run analysis / metric collection when you want the
    structured payload rather than the human-readable rendering.
    """
    path = Path(path)

    def _log(event: LLMResponseEvent) -> None:
        payload = {
            "stage": event.stage,
            "iteration": event.iteration,
            "max_iterations": event.max_iterations,
            "slide_index": event.slide_index,
            "slide_total": event.slide_total,
            "content": event.content,
            "reasoning": event.reasoning,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": c.arguments}
                for c in event.tool_calls
            ],
            "usage": event.usage,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    return _log
