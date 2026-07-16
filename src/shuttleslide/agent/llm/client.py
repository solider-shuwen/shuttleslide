"""OpenAI-compatible LLM client.

Thin wrapper around the official `openai` Python SDK. Works with any
OpenAI-compatible endpoint (Zhipu GLM, DeepSeek, OpenAI, vLLM, Ollama, ...)
by setting `base_url`.

Both sync and async entry points are provided.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from shuttleslide.agent.llm.tool_call import LLMResponse, ToolCall


def _sanitize_temperature(t: float) -> float:
    """Clamp + round temperature before handing it to the API.

    Many OpenAI-compatible providers (notably Zhipu GLM) reject
    temperatures with more than 2 decimal places and return
    HTTP 400 ``Invalid temperature parameter: limited to 2 decimal places``. This bites when
    callers do floating-point arithmetic like ``temp + 0.1`` —
    ``0.7 + 0.1`` evaluates to ``0.7999999999999999`` in IEEE 754.

    Centralising the cleanup at the client boundary means every call
    site (chat_with_tools, chat_with_tools_sync, chat_with_vision) and
    every future entry point inherits the fix without each one having
    to remember to round.
    """
    # Clamp to OpenAI's documented [0, 2] range first, then round to 2 dp.
    # Negative or >2 values are almost certainly caller bugs; we clamp
    # rather than raise so a misconfigured AgentConfig doesn't crash a
    # long-running pipeline.
    return round(max(0.0, min(2.0, float(t))), 2)


class LLMClient:
    """Thin wrapper over openai.OpenAI / openai.AsyncOpenAI."""

    # Retries are delegated to the OpenAI SDK, which already handles the
    # right set of retryable conditions:
    #   - connection errors (DNS, reset, timeout-handshake)
    #   - HTTP 408 / 409 / 429 / 5xx
    #   - x-should-retry: true header (some providers)
    # The SDK honors Retry-After on 429 and uses exponential backoff with
    # jitter. Non-retryable codes (400 / 401 / 403 / 404 / 422) raise
    # immediately — correctly, since retrying a malformed request can't help.
    #
    # Defaults below override the SDK's own defaults:
    #   - max_retries=3 (vs SDK default 2): LLM calls are expensive to fail;
    #     one extra attempt is cheap insurance against a flaky provider.
    #   - timeout=60s (vs SDK default 600s read!): a hung LLM call should
    #     fail fast, not block the pipeline for 10 minutes. 60s is
    #     generous — p99 chat_with_tools latency is ~15-25s.
    DEFAULT_MAX_RETRIES = 3
    DEFAULT_TIMEOUT_SECONDS = 60.0

    def __init__(
        self,
        api_base: str,
        api_key: str,
        model: str,
        max_retries: int = DEFAULT_MAX_RETRIES,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        disable_required_tool_choice: bool = False,
    ):
        self.api_base = api_base
        self.api_key = api_key
        self.model = model
        self.max_retries = max_retries
        self.timeout = timeout
        # When True, ``tool_choice="required"`` is silently downgraded to
        # ``"auto"`` before the request leaves this client. Some providers
        # reject ``"required"`` under specific modes — notably DeepSeek's
        # deepseek-reasoner under thinking mode returns HTTP 400
        # ``Thinking mode does not support this tool_choice``. The caller
        # (AgentConfig / orchestrator) flips this on; the node code stays
        # provider-agnostic and keeps ``tool_choice="required"`` everywhere.
        self.disable_required_tool_choice = disable_required_tool_choice
        self._async = None
        self._sync = None

    # -- provider quirk normalization -------------------------------------
    #
    # Centralizing tool_choice normalization here means every entry point
    # (async, sync, future streaming variants) inherits the same fix, and
    # the node code stays provider-agnostic. This is the only place that
    # knows about DeepSeek's thinking-mode limitation.

    def _normalize_tool_choice(self, tool_choice: str) -> Optional[str]:
        """Normalize tool_choice for the configured provider/model.

        Returns the value to send, or ``None`` to omit the field from the
        request entirely.

        DeepSeek's thinking-mode models (deepseek-reasoner, deepseek-v4-flash
        with thinking enabled) reject ANY non-default ``tool_choice`` value
        with HTTP 400 ``Thinking mode does not support this tool_choice`` —
        not just ``"required"`` but also ``"auto"``. An earlier version of
        this method downgraded ``"required"`` -> ``"auto"``, which swapped
        one 400 for another. When the caller has opted in via
        ``disable_required_tool_choice=True``, we omit ``tool_choice``
        entirely for the "force a tool call" intents (``required``, ``auto``).

        ``"none"`` is preserved: it's an explicit "don't call tools" opt-out
        that doesn't conflict with thinking mode, and callers may pass it
        intentionally (e.g. plain chat-with-tools-but-no-call-allowed).
        """
        if self.disable_required_tool_choice and tool_choice in ("required", "auto"):
            return None
        return tool_choice

    # -- lazy client construction ------------------------------------------

    def _get_async(self):
        if self._async is None:
            from openai import AsyncOpenAI

            self._async = AsyncOpenAI(
                base_url=self.api_base,
                api_key=self.api_key,
                max_retries=self.max_retries,
                timeout=self.timeout,
            )
        return self._async

    def _get_sync(self):
        if self._sync is None:
            from openai import OpenAI

            self._sync = OpenAI(
                base_url=self.api_base,
                api_key=self.api_key,
                max_retries=self.max_retries,
                timeout=self.timeout,
            )
        return self._sync

    # -- async entry point -------------------------------------------------

    async def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],  # message list including conversation history
        tools: Optional[List[Dict[str, Any]]] = None,  # optional tool definitions
        temperature: float = 0.7,  # temperature parameter controlling output randomness
        max_tokens: Optional[int] = 4096,  # max tokens to generate; None = unlimited
        tool_choice: str = "auto",  # tool choice strategy; defaults to auto
    ) -> LLMResponse:  # return type is LLMResponse
        """Call the model with optional tool definitions.

        Returns a parsed LLMResponse. Even when the model emits tool calls,
        the assistant message in the response is in a format ready to append
        to the messages list.

        ``max_tokens=None`` omits the field from the request, letting the
        model use its default maximum output length. Some providers
        (notably DeepSeek's deepseek-reasoner) reject explicit ``max_tokens``
        caps under thinking mode — passing None is the way to opt out of
        the cap entirely. A non-None integer is forwarded as-is.

        Retries are delegated entirely to the OpenAI SDK (configured via
        ``max_retries`` on this client). The SDK handles connection errors,
        408/409/429/5xx, and Retry-After with exponential backoff + jitter.
        An earlier version of this method wrapped the call in a manual
        ``while retry <= 3: time.sleep(10)`` loop — that had four bugs
        (blocking ``time.sleep`` inside an async function, ``except Exception``
        catching non-retryable 4xx, N×M multiplication with the SDK's own
        retries, and coverage gaps in the sync / vision paths). Removing the
        wrapper entirely fixes all four at once and makes the three entry
        points behave identically.
        """
        client = self._get_async()  # get async client
        kwargs: Dict[str, Any] = dict(  # build request kwargs
            model=self.model,  # model name
            messages=messages,  # conversation message list
            temperature=_sanitize_temperature(temperature),
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            tc = self._normalize_tool_choice(tool_choice)
            if tc is not None:
                kwargs["tool_choice"] = tc
        resp = await client.chat.completions.create(**kwargs)
        return self._parse(resp)

    # -- sync entry point --------------------------------------------------

    def chat_with_tools_sync(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = 4096,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        """Sync version of chat_with_tools. See that method for semantics."""
        client = self._get_sync()
        kwargs: Dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            temperature=_sanitize_temperature(temperature),
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            tc = self._normalize_tool_choice(tool_choice)
            if tc is not None:
                kwargs["tool_choice"] = tc

        resp = client.chat.completions.create(**kwargs)
        return self._parse(resp)

    # -- vision entry point ------------------------------------------------
    #
    # The same client class serves both text and vision calls. When the
    # VLM lives on a separate endpoint (different model / api_base), the
    # caller constructs a second LLMClient with the VLM credentials and
    # uses it directly — no subclassing, no global state. See
    # AgentOrchestrator._build_vlm_client for the wiring.

    async def chat_with_vision(
        self,
        prompt: str,
        image_b64: str,
        mime: str = "image/jpeg",
        temperature: float = 0.2,
        max_tokens: Optional[int] = 256,
    ) -> str:
        """Send one image + prompt to a vision-capable model.

        ``image_b64`` is raw base64 (no data: prefix). Returns the raw
        text response — the caller decides how to parse (JSON, boolean,
        etc.). Temperature defaults low (0.2) because verification is a
        judgement task, not a creative one.

        ``max_tokens=None`` omits the cap; see chat_with_tools for details.
        """
        client = self._get_async()
        # OpenAI vision message format. Most OpenAI-compatible endpoints
        # (GLM-4.6v, Qwen-VL, gpt-4o, ...) accept this shape verbatim.
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime};base64,{image_b64}",
                        },
                    },
                ],
            }
        ]
        request_kwargs: Dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            temperature=_sanitize_temperature(temperature),
        )
        if max_tokens is not None:
            request_kwargs["max_tokens"] = max_tokens
        resp = await client.chat.completions.create(**request_kwargs)
        return resp.choices[0].message.content or ""

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def _parse(resp) -> LLMResponse:
        """Convert an openai ChatCompletion to LLMResponse."""
        choice = resp.choices[0]
        msg = choice.message

        tool_calls: List[ToolCall] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=fn.name,
                    arguments=fn.arguments or "",
                )
            )

        # Reasoning models (GLM-4.6+, DeepSeek-R1, ...) put chain-of-thought in
        # `reasoning_content`. The attribute is non-standard so it lives in the
        # SDK's permissive extra fields — try several known names defensively.
        reasoning = (
            getattr(msg, "reasoning_content", None)
            or getattr(msg, "reasoning", None)
        )

        # Build assistant message ready to append back to the conversation.
        # NOTE: `reasoning_content` is intentionally NOT included here — the chat
        # API doesn't expect it back, and including it can confuse some endpoints.
        #
        # OpenAI Chat Completions requires the assistant message to carry at
        # least one of `content` / `tool_calls`. Reasoning models under
        # thinking mode (notably deepseek-reasoner) routinely emit a non-empty
        # `reasoning_content` with both `content` and `tool_calls` empty — the
        # entire output lives in the chain-of-thought. Other providers (OpenAI,
        # GLM) tolerate a bare ``{"role": "assistant"}`` on the next request,
        # but DeepSeek strictly enforces the spec and returns HTTP 400
        # ``Invalid assistant message: content or tool_calls must be set``.
        # Patching at this boundary fixes the whole class (truncation, empty
        # tool-only replies that the SDK flattens, etc.), not just DeepSeek.
        assistant_message: Dict[str, Any] = {"role": "assistant"}
        if msg.content:
            assistant_message["content"] = msg.content
        if tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in tool_calls
            ]
        if "content" not in assistant_message and "tool_calls" not in assistant_message:
            # Inject an empty-string content — every OpenAI-compatible
            # endpoint accepts this and treats it as "model produced no
            # user-visible output this turn". The downstream retry loop in
            # each node already notices the missing tool call and prompts
            # the model to retry, so this empty content never reaches the
            # final deck.
            assistant_message["content"] = ""

        usage = None
        if getattr(resp, "usage", None) is not None:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens,
                "completion_tokens": resp.usage.completion_tokens,
                "total_tokens": resp.usage.total_tokens,
            }

        return LLMResponse(
            assistant_message=assistant_message,
            tool_calls=tool_calls,
            content=msg.content,
            reasoning=reasoning,
            finish_reason=choice.finish_reason,
            usage=usage,
        )
