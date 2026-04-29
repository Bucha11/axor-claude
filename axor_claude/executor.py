from __future__ import annotations

"""
ClaudeCodeExecutor — Invokable implementation for Claude via Anthropic SDK.

Tool loop architecture:
    The key insight is that executor.stream() and intent_loop cannot both
    be "in control" at the same time within a single AsyncGenerator.

    Solution: executor yields ONE ROUND at a time.
    intent_loop drives the multi-turn loop externally:

        round 1: executor streams → yields TEXT + TOOL_USE events + STOP
        intent_loop: intercepts TOOL_USE → resolves → executes → collects results
        round 2: executor.stream() called again with tool_results injected
        ... repeat until no tool_use

    This is implemented via ToolResultBus — a per-execution object that
    intent_loop populates with results, and executor reads from at start
    of each round to inject tool_result messages.
"""

import asyncio
from typing import Any, AsyncIterator

from axor_core.contracts.envelope import ExecutionEnvelope
from axor_core.contracts.invokable import Invokable
from axor_core.contracts.result import ExecutorEvent, ExecutorEventKind

from axor_claude.events import StreamNormalizer
from axor_claude.tool_definitions import build_tool_definitions

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 8192

_DEFAULT_SYSTEM_PROMPT = """You are an expert software engineer assistant.
You have access to tools to read, write, and execute code.
Use tools to complete the task. Be precise and efficient."""

# context_mode → max fragments visible to Claude
_CONTEXT_MODE_FRAGMENT_LIMIT = {
    "minimal": 5,
    "moderate": 15,
    "broad": 40,
}


class ToolResultBus:
    """
    Communication channel between intent_loop and executor for tool results.

    intent_loop calls push() after executing each approved tool.
    executor calls drain() at the start of each round to get pending results.

    One stable bus per executor instance.
    It is reset at the start of each stream() execution; reset also drops any
    queued items so late pushes from a prior cancelled run cannot leak into
    the next round.
    Thread-safe via asyncio.Queue.
    """

    def __init__(self) -> None:
        # queue of (tool_use_id, result) tuples
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._expected: int = 0

    def reset(self) -> None:
        """Reset bus state before a new stream() execution starts.

        Drops any queued items left over from a previous run; cancelled or
        timed-out drains can otherwise leave entries in the queue that would
        be picked up by the next drain with mismatched tool_use_ids.
        """
        self._queue = asyncio.Queue()
        self._expected = 0

    def expect(self, count: int) -> None:
        """Tell the bus how many tool results to expect this round."""
        self._expected = count

    def push(self, tool_use_id: str, result: Any) -> None:
        """intent_loop calls this after executing a tool."""
        self._queue.put_nowait((tool_use_id, result))

    async def drain(
        self,
        timeout: float = 30.0,
        cancel_token: Any = None,
    ) -> dict[str, Any]:
        """
        Wait for all expected tool results and return them.
        Returns {tool_use_id: result} mapping.

        If `cancel_token` is supplied, drain returns early as soon as the
        token is set — partial results collected so far are returned. Without
        a token, behavior is the legacy timeout-only wait.
        """
        results: dict[str, Any] = {}
        deadline = asyncio.get_event_loop().time() + timeout

        # Build a cancel-wait task once if we have a token. asyncio.wait
        # accepts already-pending awaitables, so we re-add it each iteration
        # by checking is_cancelled directly — cheap and avoids managing a
        # background task lifetime here.
        while len(results) < self._expected:
            if cancel_token is not None and cancel_token.is_cancelled():
                break
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            # Cap each iteration's wait so cancel_token polling stays responsive.
            step = min(remaining, 0.25)
            try:
                tool_use_id, result = await asyncio.wait_for(
                    self._queue.get(), timeout=step
                )
                results[tool_use_id] = result
            except asyncio.TimeoutError:
                continue

        self._expected = 0
        return results


class ClaudeCodeExecutor(Invokable):
    """
    Invokable implementation backed by Anthropic Claude API.

    Manages the multi-turn conversation with Claude including
    the tool call / tool result loop.

    The tool loop is driven cooperatively with intent_loop:
        - executor yields TOOL_USE events
        - intent_loop intercepts them, resolves as Intents, executes tools
        - intent_loop pushes results to ToolResultBus
        - executor reads results from bus and continues conversation

    Args:
        api_key:    Anthropic API key. None → reads ANTHROPIC_API_KEY env var.
        model:      Claude model. Default: claude-sonnet-4-6
        max_tokens: Max tokens per response. Default: 8192
        base_url:   Optional custom API base URL.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        base_url: str | None = None,
        system_prompt: str | None = None,
        max_retries: int = 2,
        enable_prompt_cache: bool = True,
    ) -> None:
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "axor-claude requires the anthropic package. "
                "Install it: pip install anthropic"
            )

        self._model = model
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt or _DEFAULT_SYSTEM_PROMPT
        # Anthropic prompt caching — marks the stable prefix (system + tools)
        # with cache_control so re-sends cost 0.1x on hit, 1.25x on write.
        # Pinned/skill content lives in the user message and would need a
        # separate breakpoint; that's not covered here — only system + tool
        # defs, which is the always-stable part of the request.
        self._enable_prompt_cache = enable_prompt_cache
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key,
            max_retries=max_retries,
            **({"base_url": base_url} if base_url else {}),
        )

        # bus is stable across executions to avoid callback/drain races
        self._bus = ToolResultBus()
        self._messages: list[dict] = []
        self._text_callback = None  # set by CLI for streaming output

    def set_text_callback(self, callback) -> None:
        """
        Register a callback fired for each text chunk as it arrives.
        Called by axor-cli to stream output to the terminal in real time.
        callback(chunk: str) -> None
        """
        self._text_callback = callback

    def get_bus(self) -> ToolResultBus:
        """
        Returns the active ToolResultBus for the current execution.
        Called by intent_loop to push tool results back.
        """
        return self._bus

    async def stream(
        self, envelope: ExecutionEnvelope
    ) -> AsyncIterator[ExecutorEvent]:
        """
        Stream governed execution via Claude API.

        Drives the multi-turn tool loop:
            1. Build messages from envelope + any pending tool results
            2. Call Claude API, stream events
            3. If Claude requests tools → yield TOOL_USE events
               intent_loop intercepts them, executes, pushes results to bus
            4. Read results from bus → append to messages → next round
            5. If Claude produces no tool_use → yield STOP, done
        """
        tools = build_tool_definitions(envelope.capabilities.allowed_tools)
        if self._enable_prompt_cache and tools:
            # Cache breakpoint on the LAST tool def caches all tool defs +
            # everything before them (the system prompt). One breakpoint covers
            # the entire static prefix.
            tools = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]
        system_param = self._build_system_param()
        messages = self._build_initial_messages(envelope)
        normalizer = StreamNormalizer(node_id=envelope.node_id)

        # reset bus for this execution while keeping the same instance
        self._bus.reset()
        self._messages = messages

        while True:
            if envelope.cancel_token.is_cancelled():
                return

            # stream one round
            tool_uses_this_round: list[dict] = []
            assistant_content: list[dict] = []
            # (loop terminates on STOP via break or natural stream end)

            try:
                async with self._client.messages.stream(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=messages,
                    tools=tools if tools else [],
                    system=system_param,
                ) as sdk_stream:
                    async for sdk_event in sdk_stream:
                        if envelope.cancel_token.is_cancelled():
                            return

                        for event in normalizer.process(sdk_event):
                            match event.kind:
                                case ExecutorEventKind.TOOL_USE:
                                    tool_uses_this_round.append(event.payload)
                                    yield event

                                case ExecutorEventKind.TEXT:
                                    if event.payload.get("text"):
                                        text = event.payload["text"]
                                        assistant_content.append(
                                            {
                                                "type": "text",
                                                "text": text,
                                            }
                                        )
                                        # Fire streaming callback for CLI.
                                        # Catch broadly so a buggy callback
                                        # never crashes the executor, but log
                                        # the failure so it isn't invisible.
                                        # KeyboardInterrupt is BaseException —
                                        # not caught here — and propagates as
                                        # expected.
                                        if self._text_callback is not None:
                                            try:
                                                self._text_callback(text)
                                            except Exception as cb_exc:
                                                import logging as _lg
                                                _lg.getLogger("axor.claude.executor").warning(
                                                    "text_callback raised: %s", cb_exc,
                                                    exc_info=True,
                                                )
                                    yield event

                                case ExecutorEventKind.STOP:
                                    yield event

                                case ExecutorEventKind.ERROR:
                                    yield event
                                    return

            except Exception as exc:
                # distinguish transient from fatal errors. Anthropic SDK uses
                # OverloadedError (HTTP 529) and other names that change between
                # versions, so name-based matching errs on the side of inclusion.
                exc_type = type(exc).__name__
                is_transient = exc_type in (
                    "RateLimitError",
                    "InternalServerError",
                    "APIConnectionError",
                    "APITimeoutError",
                    "OverloadedError",
                )
                # We do not retry inside stream() — the SDK itself retries up to
                # `max_retries` on connect errors, and higher-level retry belongs
                # in the caller (intent_loop / session). Yield the error event
                # with `transient=...` so the caller can decide.
                yield ExecutorEvent(
                    kind=ExecutorEventKind.ERROR,
                    payload={
                        "error": str(exc),
                        "type": exc_type,
                        "message": str(exc),
                        "transient": is_transient,
                    },
                    node_id=envelope.node_id,
                )
                return

            # no tool calls this round — conversation complete
            if not tool_uses_this_round:
                return

            # reconstruct assistant turn (text + tool_use blocks together)
            assistant_blocks: list[dict] = []
            assistant_blocks.extend(assistant_content)
            for tp in tool_uses_this_round:
                assistant_blocks.append(
                    {
                        "type": "tool_use",
                        "id": tp["tool_use_id"],
                        "name": tp["tool"],
                        "input": tp["args"],
                    }
                )
            messages.append({"role": "assistant", "content": assistant_blocks})

            # tell bus how many results to expect, then wait
            # intent_loop will push results via get_bus().push() as it
            # processes the TOOL_USE events we just yielded
            self._bus.expect(len(tool_uses_this_round))

            if envelope.cancel_token.is_cancelled():
                return

            results = await self._bus.drain(
                timeout=60.0,
                cancel_token=envelope.cancel_token,
            )

            if envelope.cancel_token.is_cancelled():
                return

            # build tool_result message for next round
            tool_result_blocks = [
                StreamNormalizer(node_id=envelope.node_id).tool_result_event(
                    tool_use_id=tp["tool_use_id"],
                    result=results.get(
                        tp["tool_use_id"], "[tool result unavailable]"
                    ),
                    node_id=envelope.node_id,
                )
                for tp in tool_uses_this_round
            ]
            messages.append({"role": "user", "content": tool_result_blocks})

    def _build_system_param(self):
        """
        Build the `system` argument for messages.stream().

        With caching enabled, returns a list of text blocks with cache_control.
        Without caching, returns a plain string (the legacy form).

        Two breakpoints (system + last tool) are intentional: the system one
        keeps a cache hit on `system` alone when the tool list changes between
        turns (envelope.capabilities.allowed_tools is policy-derived and may
        vary). The tool one extends the cached prefix to include tool defs
        when the tool list is stable. Anthropic allows up to 4 breakpoints,
        so two is comfortable.
        """
        if not self._enable_prompt_cache:
            return self._system_prompt
        return [
            {
                "type": "text",
                "text": self._system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    def _build_initial_messages(
        self, envelope: ExecutionEnvelope
    ) -> list[dict]:
        """Build initial user message from governed envelope."""
        context = envelope.context
        parts = []

        if context.working_summary:
            parts.append(f"Context: {context.working_summary}")

        # fragment limit driven by context_mode — not arbitrary
        context_mode = envelope.policy.context_mode.value
        max_facts = _CONTEXT_MODE_FRAGMENT_LIMIT.get(context_mode, 10)
        facts = [
            f
            for f in context.visible_fragments
            if f.kind == "fact" and f.content != envelope.task
        ]
        if facts:
            parts.append(
                "Relevant context:\n"
                + "\n".join(f.content for f in facts[:max_facts])
            )

        parent = next(
            (
                f.content
                for f in context.visible_fragments
                if f.kind == "parent_export"
            ),
            None,
        )
        if parent:
            parts.append(f"From parent task:\n{parent}")

        parts.append(f"Task: {envelope.task}")

        return [{"role": "user", "content": "\n\n".join(parts)}]
