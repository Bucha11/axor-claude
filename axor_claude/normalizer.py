from __future__ import annotations

"""
Normalize Anthropic API response objects into axor-core types.

Used for:
  - Extracting token usage from completed messages
  - Building ExecutionResult metadata from API response
  - Parsing stop reasons for governance decisions

StreamNormalizer in events.py handles the streaming path.
This module handles response-level metadata that is only
available after the full message completes.
"""

from typing import Any


def extract_usage(message) -> dict[str, int]:
    """
    Extract token usage from an Anthropic message object.

    Works with both streaming (via accumulated usage) and
    non-streaming (direct message.usage) responses.

    Returns dict with:
        input_tokens, output_tokens, tool_tokens,
        cache_creation_input_tokens, cache_read_input_tokens
    """
    empty = {
        "input_tokens": 0,
        "output_tokens": 0,
        "tool_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    usage = getattr(message, "usage", None)
    if usage is None:
        return empty

    return {
        "input_tokens":  getattr(usage, "input_tokens", 0),
        "output_tokens": getattr(usage, "output_tokens", 0),
        # Anthropic includes tool definition tokens in input_tokens
        # tool_tokens is tracked separately for axor budget accounting
        "tool_tokens":   0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens":     getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def extract_stop_reason(message) -> str:
    """
    Extract stop reason from Anthropic message.

    Anthropic stop reasons:
        end_turn        — natural completion
        tool_use        — model wants to call a tool
        max_tokens      — hit max_tokens limit
        stop_sequence   — hit a stop sequence
    """
    return getattr(message, "stop_reason", "end_turn") or "end_turn"


def extract_text_content(message) -> str:
    """
    Extract all text content from an Anthropic message's content blocks.
    """
    content = getattr(message, "content", [])
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "")
            if text:
                parts.append(text)
    return "".join(parts)


def build_response_metadata(
    message,
    policy_name: str,
    node_id: str,
    depth: int,
) -> dict[str, Any]:
    """
    Build ExecutionResult metadata from a completed Anthropic message.

    Combines API response metadata with governance metadata.
    """
    return {
        "policy":       policy_name,
        "node_id":      node_id,
        "depth":        depth,
        "model":        getattr(message, "model", "unknown"),
        "stop_reason":  extract_stop_reason(message),
        "message_id":   getattr(message, "id", ""),
    }


def is_tool_use_response(message) -> bool:
    """Check if message contains tool_use blocks (model wants to call tools)."""
    return extract_stop_reason(message) == "tool_use"


def extract_tool_uses(message) -> list[dict[str, Any]]:
    """
    Extract tool_use blocks from a completed message.

    Returns list of:
        {"tool_use_id": str, "tool": str, "args": dict}
    """
    content = getattr(message, "content", [])
    result = []
    for block in content:
        if getattr(block, "type", None) == "tool_use":
            result.append({
                "tool_use_id": getattr(block, "id", ""),
                "tool":        getattr(block, "name", ""),
                "args":        getattr(block, "input", {}),
            })
    return result
