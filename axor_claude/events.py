from __future__ import annotations

"""
Claude SDK stream event → ExecutorEvent normalization.

This module is the only place in axor-claude that knows about
the internal structure of Anthropic SDK stream events.
Everything else in axor-claude works with normalized ExecutorEvent.

Claude SDK streaming emits these event types:
    message_start         → begin of message, has model info
    content_block_start   → begin of a content block (text or tool_use)
    content_block_delta   → incremental content (text delta or input_json_delta)
    content_block_stop    → end of a content block
    message_delta         → stop reason + usage
    message_stop          → end of stream

We normalize these into four ExecutorEvent kinds:
    TEXT     — text content (accumulated from deltas)
    TOOL_USE — tool call with complete args (accumulated from json deltas)
    STOP     — end of execution with token usage
    ERROR    — any error condition
"""

from dataclasses import dataclass, field
from typing import Any

from axor_core.contracts.result import ExecutorEvent, ExecutorEventKind


# ── Internal accumulator state ─────────────────────────────────────────────────

@dataclass
class _TextBlock:
    """Accumulates text deltas into a complete text block."""
    index: int
    text: str = ""

    def apply_delta(self, delta) -> None:
        if hasattr(delta, "text"):
            self.text += delta.text


@dataclass
class _ToolUseBlock:
    """Accumulates input_json deltas into a complete tool call."""
    index: int
    tool_use_id: str
    name: str
    input_json: str = ""

    def apply_delta(self, delta) -> None:
        if hasattr(delta, "partial_json"):
            self.input_json += delta.partial_json

    def parse_args(self) -> dict[str, Any]:
        """Decode the streamed tool_use input JSON.

        Returns a dict on success; on malformed JSON returns
        ``{"_axor_parse_error": "<reason>", "_raw": "<original>"}`` so
        downstream tool handlers receive an obviously-flagged dict
        instead of being handed a `str` they will then crash on. Empty
        input is treated as no-args.
        """
        import json
        if not self.input_json.strip():
            return {}
        try:
            parsed = json.loads(self.input_json)
        except json.JSONDecodeError as exc:
            # Cap raw payload to avoid pushing megabytes back through the
            # tool result if the model produced garbage.
            return {
                "_axor_parse_error": f"invalid JSON in tool input: {exc.msg}",
                "_raw": self.input_json[:4000],
            }
        # The Anthropic schema mandates an object for tool_use.input. If a
        # model returns an array/string/number, surface it as a parse error
        # rather than letting the handler choke on `args.get(...)`.
        if not isinstance(parsed, dict):
            return {
                "_axor_parse_error": f"tool_use input is not a JSON object (got {type(parsed).__name__})",
                "_raw": self.input_json[:4000],
            }
        return parsed


@dataclass
class StreamNormalizer:
    """
    Stateful normalizer for one Claude SDK streaming response.

    One instance per executor.stream() call.
    Accumulates partial events and emits complete ExecutorEvents
    when content blocks are finalized.

    Usage:
        normalizer = StreamNormalizer(node_id=envelope.node_id)
        async with client.messages.stream(...) as stream:
            async for sdk_event in stream:
                events = normalizer.process(sdk_event)
                for event in events:
                    yield event
    """
    node_id: str
    _blocks: dict[int, _TextBlock | _ToolUseBlock] = field(default_factory=dict)
    _input_tokens: int = 0
    _output_tokens: int = 0
    _cache_creation_input_tokens: int = 0
    _cache_read_input_tokens: int = 0

    def process(self, sdk_event) -> list[ExecutorEvent]:
        """
        Process one SDK event. Returns 0 or more ExecutorEvents.

        Most events return [] — they accumulate state.
        content_block_stop returns a complete TEXT or TOOL_USE event.
        message_delta returns a STOP event.
        """
        event_type = getattr(sdk_event, "type", None)

        match event_type:

            case "message_start":
                # capture initial usage if present
                msg = getattr(sdk_event, "message", None)
                if msg:
                    usage = getattr(msg, "usage", None)
                    if usage:
                        self._input_tokens = getattr(usage, "input_tokens", 0)
                        self._cache_creation_input_tokens = getattr(
                            usage, "cache_creation_input_tokens", 0
                        ) or 0
                        self._cache_read_input_tokens = getattr(
                            usage, "cache_read_input_tokens", 0
                        ) or 0
                return []

            case "content_block_start":
                block = getattr(sdk_event, "content_block", None)
                index = getattr(sdk_event, "index", 0)
                if block is None:
                    return []

                block_type = getattr(block, "type", None)
                if block_type == "text":
                    self._blocks[index] = _TextBlock(index=index)
                elif block_type == "tool_use":
                    self._blocks[index] = _ToolUseBlock(
                        index=index,
                        tool_use_id=getattr(block, "id", ""),
                        name=getattr(block, "name", ""),
                    )
                return []

            case "content_block_delta":
                index = getattr(sdk_event, "index", 0)
                delta = getattr(sdk_event, "delta", None)
                block = self._blocks.get(index)
                if block and delta:
                    block.apply_delta(delta)
                return []

            case "content_block_stop":
                index = getattr(sdk_event, "index", 0)
                block = self._blocks.pop(index, None)
                if block is None:
                    return []

                if isinstance(block, _TextBlock) and block.text:
                    return [ExecutorEvent(
                        kind=ExecutorEventKind.TEXT,
                        payload={"text": block.text},
                        node_id=self.node_id,
                    )]

                if isinstance(block, _ToolUseBlock):
                    return [ExecutorEvent(
                        kind=ExecutorEventKind.TOOL_USE,
                        payload={
                            "tool":        block.name,
                            "args":        block.parse_args(),
                            "tool_use_id": block.tool_use_id,
                        },
                        node_id=self.node_id,
                    )]

                return []

            case "message_delta":
                # final usage + stop reason
                # Anthropic streaming spec: message_delta carries the final
                # aggregated usage. Cache fields land HERE (not message_start)
                # on first call when cache is being created — the API only
                # knows the cache_creation_input_tokens after writing the cache.
                # message_start has initial input_tokens, message_delta has
                # the final cache attribution. Take the max() so we never lose
                # values that were already populated at message_start.
                usage = getattr(sdk_event, "usage", None)
                if usage:
                    self._output_tokens = getattr(usage, "output_tokens", 0)
                    self._cache_creation_input_tokens = max(
                        self._cache_creation_input_tokens,
                        getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    )
                    self._cache_read_input_tokens = max(
                        self._cache_read_input_tokens,
                        getattr(usage, "cache_read_input_tokens", 0) or 0,
                    )
                return []

            case "message_stop":
                return [ExecutorEvent(
                    kind=ExecutorEventKind.STOP,
                    payload={
                        "usage": {
                            "input_tokens":  self._input_tokens,
                            "output_tokens": self._output_tokens,
                            "tool_tokens":   0,  # included in input_tokens by Anthropic
                            "cache_creation_input_tokens": self._cache_creation_input_tokens,
                            "cache_read_input_tokens":     self._cache_read_input_tokens,
                        }
                    },
                    node_id=self.node_id,
                )]

            case "error":
                error = getattr(sdk_event, "error", None)
                return [ExecutorEvent(
                    kind=ExecutorEventKind.ERROR,
                    payload={
                        "error":   str(error) if error else "unknown error",
                        "type":    getattr(error, "type", "unknown"),
                        "message": getattr(error, "message", str(error)),
                    },
                    node_id=self.node_id,
                )]

            case _:
                # unknown event type — ignore
                return []

    def tool_result_event(
        self,
        tool_use_id: str,
        result: Any,
        node_id: str,
    ) -> dict:
        """
        Build a tool_result message for the Anthropic API.

        After intent_loop approves and executes a tool,
        the result must be fed back into the conversation
        as a tool_result content block.
        """
        import json
        content = result if isinstance(result, str) else json.dumps(result, default=str)
        return {
            "type":        "tool_result",
            "tool_use_id": tool_use_id,
            "content":     content,
        }
