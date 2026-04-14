"""Unit tests for normalizer.py and tool_definitions.py."""
from __future__ import annotations

import pytest
from axor_claude.normalizer import (
    extract_usage, extract_stop_reason, extract_text_content,
    build_response_metadata, is_tool_use_response, extract_tool_uses,
)
from axor_claude.tool_definitions import build_tool_definitions, register_tool_definition


class MockMsg:
    class usage:
        input_tokens = 500
        output_tokens = 120

    stop_reason = "end_turn"
    model = "claude-sonnet-4-5"
    id = "msg_abc123"

    class _TextBlock:
        type = "text"
        text = "Hello world"

    class _ToolBlock:
        type = "tool_use"
        id = "tu_001"
        name = "bash"
        input = {"command": "ls -la"}

    content = [_TextBlock(), _ToolBlock()]


class TestExtractUsage:

    def test_extracts_input_tokens(self):
        assert extract_usage(MockMsg())["input_tokens"] == 500

    def test_extracts_output_tokens(self):
        assert extract_usage(MockMsg())["output_tokens"] == 120

    def test_tool_tokens_zero(self):
        assert extract_usage(MockMsg())["tool_tokens"] == 0

    def test_missing_usage_returns_zeros(self):
        class NoUsage:
            pass
        result = extract_usage(NoUsage())
        assert result == {"input_tokens": 0, "output_tokens": 0, "tool_tokens": 0}


class TestExtractStopReason:

    def test_end_turn(self):
        assert extract_stop_reason(MockMsg()) == "end_turn"

    def test_tool_use(self):
        class ToolMsg:
            stop_reason = "tool_use"
        assert extract_stop_reason(ToolMsg()) == "tool_use"

    def test_none_defaults_to_end_turn(self):
        class NoneMsg:
            stop_reason = None
        assert extract_stop_reason(NoneMsg()) == "end_turn"

    def test_missing_defaults_to_end_turn(self):
        class Empty:
            pass
        assert extract_stop_reason(Empty()) == "end_turn"


class TestExtractTextContent:

    def test_extracts_text_blocks(self):
        text = extract_text_content(MockMsg())
        assert text == "Hello world"

    def test_skips_tool_use_blocks(self):
        text = extract_text_content(MockMsg())
        assert "bash" not in text
        assert "ls" not in text

    def test_empty_content(self):
        class Empty:
            content = []
        assert extract_text_content(Empty()) == ""


class TestIsToolUseResponse:

    def test_tool_use_stop_reason(self):
        class ToolMsg:
            stop_reason = "tool_use"
        assert is_tool_use_response(ToolMsg()) is True

    def test_end_turn_not_tool_use(self):
        assert is_tool_use_response(MockMsg()) is False


class TestExtractToolUses:

    def test_extracts_tool_use_blocks(self):
        tools = extract_tool_uses(MockMsg())
        assert len(tools) == 1
        assert tools[0]["tool"] == "bash"
        assert tools[0]["args"] == {"command": "ls -la"}
        assert tools[0]["tool_use_id"] == "tu_001"

    def test_skips_text_blocks(self):
        tools = extract_tool_uses(MockMsg())
        for t in tools:
            assert t["tool"] != "text"

    def test_empty_content(self):
        class Empty:
            content = []
        assert extract_tool_uses(Empty()) == []


class TestBuildToolDefinitions:

    def test_read_included_when_allowed(self):
        defs = build_tool_definitions(frozenset(["read"]))
        names = [d["name"] for d in defs]
        assert "read" in names

    def test_only_allowed_tools_included(self):
        defs = build_tool_definitions(frozenset(["read", "write"]))
        names = {d["name"] for d in defs}
        assert "bash" not in names
        assert "search" not in names

    def test_unknown_tool_skipped(self):
        defs = build_tool_definitions(frozenset(["read", "nonexistent_xyz"]))
        names = [d["name"] for d in defs]
        assert "nonexistent_xyz" not in names
        assert "read" in names

    def test_empty_allowed_tools(self):
        defs = build_tool_definitions(frozenset())
        assert defs == []

    def test_all_canonical_tools(self):
        allowed = frozenset(["read", "write", "bash", "search", "glob", "spawn_child"])
        defs = build_tool_definitions(allowed)
        names = {d["name"] for d in defs}
        assert names == allowed

    def test_definition_has_required_fields(self):
        defs = build_tool_definitions(frozenset(["read"]))
        d = defs[0]
        assert "name" in d
        assert "description" in d
        assert "input_schema" in d
        assert d["input_schema"]["type"] == "object"

    def test_register_custom_tool(self):
        register_tool_definition("my_custom_tool", {
            "name": "my_custom_tool",
            "description": "Does something",
            "input_schema": {"type": "object", "properties": {}},
        })
        defs = build_tool_definitions(frozenset(["my_custom_tool"]))
        assert len(defs) == 1
        assert defs[0]["name"] == "my_custom_tool"
