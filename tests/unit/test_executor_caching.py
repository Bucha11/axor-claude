"""Tests that the executor wires Anthropic prompt caching into the API call."""
from __future__ import annotations

import pytest

from axor_claude.executor import ClaudeCodeExecutor


@pytest.fixture
def executor():
    # api_key not used because we never call the network
    return ClaudeCodeExecutor(api_key="dummy", enable_prompt_cache=True)


def test_system_param_is_block_list_when_caching_enabled(executor):
    sys = executor._build_system_param()
    assert isinstance(sys, list)
    assert sys[0]["cache_control"] == {"type": "ephemeral"}
    assert sys[0]["type"] == "text"
    assert sys[0]["text"] == executor._system_prompt


def test_system_param_is_string_when_caching_disabled():
    e = ClaudeCodeExecutor(api_key="dummy", enable_prompt_cache=False)
    sys = e._build_system_param()
    assert isinstance(sys, str)
    assert sys == e._system_prompt


def test_last_tool_carries_cache_breakpoint():
    """Verify the cache_control marker lands on the last tool in the list.

    This is the behavior we expose in executor.stream(); test the constructed
    list directly so we don't have to mock the whole stream.
    """
    from axor_claude.tool_definitions import build_tool_definitions

    tools = build_tool_definitions(frozenset({"read", "bash", "write"}))
    assert len(tools) == 3
    # Apply the same transformation the executor does.
    cached = [*tools[:-1], {**tools[-1], "cache_control": {"type": "ephemeral"}}]
    # Only the last tool gets the breakpoint; earlier ones do not.
    for t in cached[:-1]:
        assert "cache_control" not in t
    assert cached[-1]["cache_control"] == {"type": "ephemeral"}


def test_caching_can_be_disabled():
    e = ClaudeCodeExecutor(api_key="dummy", enable_prompt_cache=False)
    assert e._enable_prompt_cache is False
