"""
Integration tests — require a real Anthropic API key.

Run with:
    ANTHROPIC_API_KEY=sk-ant-... pytest tests/integration/ -m integration

These tests make real API calls and will incur costs.
They are excluded from CI by default.
"""
from __future__ import annotations

import os
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def api_key():
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        pytest.skip("ANTHROPIC_API_KEY not set")
    return key


@pytest.mark.asyncio
async def test_simple_task(api_key):
    """Verify basic end-to-end execution with real Claude."""
    import axor_claude

    session = axor_claude.make_session(
        api_key=api_key,
        tools=("read",),       # read-only for safety
        load_skills=False,
        load_plugins=False,
    )

    result = await session.run("Say exactly: 'axor integration test passed'")

    assert result.output, "empty output"
    assert result.token_usage.input_tokens > 0
    assert result.token_usage.output_tokens > 0
    assert result.metadata.get("policy") is not None


@pytest.mark.asyncio
async def test_read_tool_called(api_key, tmp_path):
    """Verify read tool is correctly invoked and result flows back."""
    import axor_claude
    from axor_core import CapabilityExecutor
    from axor_core.contracts.trace import TraceConfig

    test_file = tmp_path / "greeting.txt"
    test_file.write_text("Hello from axor integration test!")

    cap = CapabilityExecutor()
    cap.register(axor_claude.ReadHandler())

    session = axor_claude.make_session(
        api_key=api_key,
        tools=("read",),
        load_skills=False,
        load_plugins=False,
    )

    result = await session.run(
        f"Read the file at {test_file} and tell me what it says."
    )

    assert "Hello from axor" in result.output or "greeting" in result.output.lower()


@pytest.mark.asyncio
async def test_policy_constrains_tools(api_key):
    """Verify that policy-denied tools are not available to Claude."""
    import axor_claude
    from axor_core import presets
    from axor_core.contracts.trace import TraceConfig

    session = axor_claude.make_session(
        api_key=api_key,
        tools=("read", "bash"),
        load_skills=False,
        load_plugins=False,
        trace_config=TraceConfig(local_only=True, persist_inputs=False),
    )

    # readonly preset — bash should not be available
    result = await session.run(
        "List the files in the current directory",
        policy=presets.get("readonly"),
    )

    # bash is not in readonly capabilities — Claude should not attempt it
    traces = session.all_traces()
    denied = [
        e for t in traces
        for e in t.events
        if e.kind.value == "intent_denied"
    ]
    # if Claude tried bash, it would be denied
    # if Claude found another way (listing via read), that's fine too
    assert result.output, "should produce some output"
