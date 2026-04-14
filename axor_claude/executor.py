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

DEFAULT_MODEL = "claude-sonnet-4-5"
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
    It is reset at the start of each stream() execution.
    Thread-safe via asyncio.Queue.
    """

    def __init__(self) -> None:
        # queue of (tool_use_id, result) tuples
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._expected: int = 0

    def reset(self) -> None:
        """Reset bus state before a new stream() execution starts."""
        self._queue = asyncio.Queue()
        self._expected = 0

    def expect(self, count: int) -> None:
        """Tell the bus how many tool results to expect this round."""
        self._expected = count

    def push(self, tool_use_id: str, result: Any) -> None:
        """intent_loop calls this after executing a tool."""
        self._queue.put_nowait((tool_use_id, result))

    async def drain(self, timeout: float = 30.0) -> dict[str, Any]:
        """
        Wait for all expected tool results and return them.
        Returns {tool_use_id: result} mapping.
        """
        results: dict[str, Any] = {}
        deadline = asyncio.get_event_loop().time() + timeout

        while len(results) < self._expected:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                tool_use_id, result = await asyncio.wait_for(
                    self._queue.get(), timeout=remaining
                )
                results[tool_use_id] = result
            except asyncio.TimeoutError:
                break

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
        model:      Claude model. Default: claude-sonnet-4-5
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
            got_stop = False

            try:
                async with self._client.messages.stream(
                    model=self._model,
                    max_tokens=self._max_tokens,
                    messages=messages,
                    tools=tools if tools else [],
                    system=self._system_prompt,
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
                                        # fire streaming callback for CLI
                                        if self._text_callback is not None:
                                            self._text_callback(text)
                                    yield event

                                case ExecutorEventKind.STOP:
                                    got_stop = True
                                    yield event

                                case ExecutorEventKind.ERROR:
                                    yield event
                                    return

            except Exception as exc:
                # distinguish transient from fatal errors
                exc_type = type(exc).__name__
                is_transient = exc_type in (
                    "RateLimitError",
                    "InternalServerError",
                    "APIConnectionError",
                    "APITimeoutError",
                )

                if is_transient and not envelope.cancel_token.is_cancelled():
                    # transient — wait and let caller retry via next run()
                    import asyncio as _aio

                    wait = 2.0 if "RateLimitError" in exc_type else 1.0
                    await _aio.sleep(wait)

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

            results = await self._bus.drain(timeout=60.0)

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
