"""Tests for axor_claude.events.StreamNormalizer — no API key required."""
from __future__ import annotations

import pytest
from axor_claude.events import StreamNormalizer
from axor_core.contracts.result import ExecutorEventKind


class MockEvt:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class MockUsage:
    def __init__(
        self,
        input_tokens=0,
        output_tokens=0,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    ):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


def norm(node_id="n_test"):
    return StreamNormalizer(node_id=node_id)


def message_start(input_tokens=100):
    return MockEvt(type="message_start",
                   message=MockEvt(usage=MockUsage(input_tokens=input_tokens)))


def block_start(index, block_type, tool_id="", tool_name=""):
    if block_type == "text":
        return MockEvt(type="content_block_start", index=index,
                       content_block=MockEvt(type="text"))
    return MockEvt(type="content_block_start", index=index,
                   content_block=MockEvt(type="tool_use", id=tool_id, name=tool_name))


def text_delta(index, text):
    return MockEvt(type="content_block_delta", index=index, delta=MockEvt(text=text))


def json_delta(index, partial_json):
    return MockEvt(type="content_block_delta", index=index,
                   delta=MockEvt(partial_json=partial_json))


def block_stop(index):
    return MockEvt(type="content_block_stop", index=index)


def message_delta(output_tokens=50):
    return MockEvt(type="message_delta", usage=MockUsage(output_tokens=output_tokens))


def message_stop():
    return MockEvt(type="message_stop")


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestTextResponse:

    def test_simple_text_yields_text_then_stop(self):
        n = norm()
        events = []
        for sdk_evt in [
            message_start(100),
            block_start(0, "text"),
            text_delta(0, "Hello "), text_delta(0, "world"),
            block_stop(0),
            message_delta(20),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))

        kinds = [e.kind for e in events]
        assert kinds == [ExecutorEventKind.TEXT, ExecutorEventKind.STOP]

    def test_text_accumulated_from_deltas(self):
        n = norm()
        events = []
        for sdk_evt in [
            block_start(0, "text"),
            text_delta(0, "part1"), text_delta(0, "_part2"), text_delta(0, "_part3"),
            block_stop(0),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        text_events = [e for e in events if e.kind == ExecutorEventKind.TEXT]
        assert len(text_events) == 1
        assert text_events[0].payload["text"] == "part1_part2_part3"

    def test_empty_text_block_not_yielded(self):
        n = norm()
        events = []
        for sdk_evt in [
            block_start(0, "text"),
            block_stop(0),  # no deltas
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        text_events = [e for e in events if e.kind == ExecutorEventKind.TEXT]
        assert len(text_events) == 0

    def test_token_usage_accumulated(self):
        n = norm()
        events = []
        for sdk_evt in [
            message_start(500),
            block_start(0, "text"), text_delta(0, "x"), block_stop(0),
            message_delta(120),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        stop = next(e for e in events if e.kind == ExecutorEventKind.STOP)
        assert stop.payload["usage"]["input_tokens"] == 500
        assert stop.payload["usage"]["output_tokens"] == 120

    def test_cache_tokens_extracted_from_message_start(self):
        n = norm()
        events = []
        cached_start = MockEvt(
            type="message_start",
            message=MockEvt(usage=MockUsage(
                input_tokens=200,
                cache_creation_input_tokens=1000,
                cache_read_input_tokens=8000,
            )),
        )
        for sdk_evt in [
            cached_start,
            block_start(0, "text"), text_delta(0, "ok"), block_stop(0),
            message_delta(50),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        stop = next(e for e in events if e.kind == ExecutorEventKind.STOP)
        assert stop.payload["usage"]["cache_creation_input_tokens"] == 1000
        assert stop.payload["usage"]["cache_read_input_tokens"] == 8000

    def test_cache_tokens_default_zero_when_absent(self):
        # message_start with no cache_* attrs at all (older models)
        class BareUsage:
            input_tokens = 100
            output_tokens = 0
        n = norm()
        events = []
        for sdk_evt in [
            MockEvt(type="message_start", message=MockEvt(usage=BareUsage())),
            block_start(0, "text"), text_delta(0, "x"), block_stop(0),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        stop = next(e for e in events if e.kind == ExecutorEventKind.STOP)
        assert stop.payload["usage"]["cache_creation_input_tokens"] == 0
        assert stop.payload["usage"]["cache_read_input_tokens"] == 0


class TestToolUseResponse:

    def test_tool_use_yields_tool_use_event(self):
        n = norm()
        events = []
        for sdk_evt in [
            block_start(0, "tool_use", tool_id="tu_01", tool_name="bash"),
            json_delta(0, '{"command": "ls"}'),
            block_stop(0),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        kinds = [e.kind for e in events]
        assert ExecutorEventKind.TOOL_USE in kinds

    def test_tool_args_parsed_from_json_deltas(self):
        n = norm()
        events = []
        for sdk_evt in [
            block_start(0, "tool_use", tool_id="tu_01", tool_name="read"),
            json_delta(0, '{"path"'),
            json_delta(0, ': "auth.py"}'),
            block_stop(0),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        tool_events = [e for e in events if e.kind == ExecutorEventKind.TOOL_USE]
        assert len(tool_events) == 1
        assert tool_events[0].payload["tool"] == "read"
        assert tool_events[0].payload["args"] == {"path": "auth.py"}
        assert tool_events[0].payload["tool_use_id"] == "tu_01"

    def test_malformed_json_returns_raw(self):
        n = norm()
        events = []
        for sdk_evt in [
            block_start(0, "tool_use", tool_id="tu_02", tool_name="bash"),
            json_delta(0, '{not valid json'),
            block_stop(0),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        tool_events = [e for e in events if e.kind == ExecutorEventKind.TOOL_USE]
        assert len(tool_events) == 1
        assert "_raw" in tool_events[0].payload["args"]

    def test_multiple_tool_uses(self):
        n = norm()
        events = []
        for sdk_evt in [
            block_start(0, "tool_use", tool_id="tu_01", tool_name="read"),
            json_delta(0, '{"path": "a.py"}'),
            block_stop(0),
            block_start(1, "tool_use", tool_id="tu_02", tool_name="bash"),
            json_delta(1, '{"command": "pytest"}'),
            block_stop(1),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        tool_events = [e for e in events if e.kind == ExecutorEventKind.TOOL_USE]
        assert len(tool_events) == 2
        assert tool_events[0].payload["tool"] == "read"
        assert tool_events[1].payload["tool"] == "bash"

    def test_node_id_preserved(self):
        n = StreamNormalizer(node_id="my_node")
        events = []
        for sdk_evt in [
            block_start(0, "text"), text_delta(0, "hi"), block_stop(0),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        assert all(e.node_id == "my_node" for e in events)


class TestMixedResponse:

    def test_text_then_tool_use(self):
        n = norm()
        events = []
        for sdk_evt in [
            block_start(0, "text"),
            text_delta(0, "I'll check the file"),
            block_stop(0),
            block_start(1, "tool_use", tool_id="tu_01", tool_name="read"),
            json_delta(1, '{"path": "main.py"}'),
            block_stop(1),
            message_stop(),
        ]:
            events.extend(n.process(sdk_evt))
        kinds = [e.kind for e in events]
        assert kinds == [ExecutorEventKind.TEXT, ExecutorEventKind.TOOL_USE, ExecutorEventKind.STOP]


class TestErrorHandling:

    def test_error_event_yields_error(self):
        n = norm()
        events = n.process(MockEvt(
            type="error",
            error=MockEvt(type="overloaded_error", message="Server overloaded"),
        ))
        assert len(events) == 1
        assert events[0].kind == ExecutorEventKind.ERROR
        assert events[0].payload["type"] == "overloaded_error"

    def test_unknown_event_type_ignored(self):
        n = norm()
        events = n.process(MockEvt(type="unknown_future_event"))
        assert events == []

    def test_stateful_blocks_cleaned_up_on_stop(self):
        n = norm()
        # start a block but don't stop it before message_stop
        n.process(MockEvt(type="content_block_start", index=0,
                          content_block=MockEvt(type="text")))
        n.process(MockEvt(type="content_block_delta", index=0, delta=MockEvt(text="partial")))
        # message_stop without content_block_stop — block should be abandoned
        events = n.process(MockEvt(type="message_stop"))
        assert any(e.kind == ExecutorEventKind.STOP for e in events)


class TestToolResultEvent:

    def test_string_result(self):
        n = norm()
        result = n.tool_result_event("tu_01", "file contents", "n1")
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "tu_01"
        assert result["content"] == "file contents"

    def test_dict_result_serialized(self):
        n = norm()
        result = n.tool_result_event("tu_02", {"key": "value"}, "n1")
        assert '"key"' in result["content"]
        assert '"value"' in result["content"]

    def test_none_result_serialized(self):
        n = norm()
        result = n.tool_result_event("tu_03", None, "n1")
        assert result["content"] == "null"
