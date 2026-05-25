from __future__ import annotations

import asyncio

import pytest

from axor_claude.events import StreamNormalizer
from axor_claude.executor import ToolResultBus, _tool_result_block
from axor_core.contracts.cancel import CancelReason, CancelToken


pytestmark = pytest.mark.asyncio


async def test_drain_returns_when_cancel_token_fires():
    """drain() must honour cancel_token even if expected results never arrive."""
    bus = ToolResultBus()
    bus.expect(2)
    token = CancelToken()

    async def cancel_soon():
        await asyncio.sleep(0.05)
        token.cancel(CancelReason.USER_ABORT)

    asyncio.create_task(cancel_soon())

    start = asyncio.get_event_loop().time()
    results = await bus.drain(timeout=10.0, cancel_token=token)
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < 1.0, "drain should return promptly on cancel, not wait full timeout"
    assert results == {}


async def test_drain_returns_partial_on_cancel():
    bus = ToolResultBus()
    bus.expect(3)
    token = CancelToken()

    bus.push("id-1", "result-1")

    async def cancel_after_first():
        await asyncio.sleep(0.05)
        token.cancel(CancelReason.USER_ABORT)

    asyncio.create_task(cancel_after_first())

    results = await bus.drain(timeout=5.0, cancel_token=token)
    assert results == {"id-1": "result-1"}


async def test_reset_drops_stale_queued_items():
    """Late pushes from a prior cancelled run must not leak into next drain."""
    bus = ToolResultBus()
    bus.expect(1)
    bus.push("stale-id", "stale-result")
    bus.push("another-stale", "x")

    bus.reset()
    bus.expect(1)
    bus.push("fresh-id", "fresh-result")

    results = await bus.drain(timeout=1.0)
    assert results == {"fresh-id": "fresh-result"}
    assert "stale-id" not in results


async def test_drain_without_cancel_token_unchanged():
    """Legacy timeout-only drain still works for code that doesn't pass a token."""
    bus = ToolResultBus()
    bus.expect(1)
    bus.push("x", "y")
    results = await bus.drain(timeout=1.0)
    assert results == {"x": "y"}


async def test_missing_tool_result_becomes_error_block():
    block = _tool_result_block(
        normalizer=StreamNormalizer(node_id="n"),
        tool_use_id="tu_missing",
        result=None,
        missing=True,
        node_id="n",
    )
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "tu_missing"
    assert block["is_error"] is True
    assert "tool_result_unavailable" in block["content"]
