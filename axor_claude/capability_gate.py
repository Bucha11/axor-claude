"""The kernel gate on a ``GovernedSession``'s tool execution.

``GovernedSession`` hands every approved tool intent to its capability executor; this
executor puts the current kernel's per-call gate (``gate.Gate``: task-scoped grants,
references, the floor, the consequence class) between that intent and the handler, so
no tool of the session runs without it. ``GatedSession`` gives the gate the user's
request at the start of each ``run``. The session's own loop (capabilities, budget,
degradation) still runs first; this adds the gate, it removes nothing.

Byte-identical in axor-claude and axor-openrouter.
"""
from __future__ import annotations

from typing import Any

from axor_core.capability.daemon_client import DaemonCapabilityClient
from axor_core.capability.executor import CapabilityExecutor
from axor_core.contracts.envelope import Capabilities
from axor_core.contracts.intent import Intent, IntentKind
from axor_core.errors.exceptions import AxorError, ToolNotAllowedError, ToolNotFoundError
from axor_core.worker.session import GovernedSession

from .gate import Admitted, Gate


class GovernanceRefused(ToolNotAllowedError):
    """The kernel gate refused the call. A ``ToolNotAllowedError``, so the session's loop
    records a denial and the agent receives its coarse denial result."""

    def __init__(self, tool: str, category: str, reason: str) -> None:
        self.tool, self.category, self.reason = tool, category, reason
        self.allowed = set()
        AxorError.__init__(self, f"governance ({category}): {reason}")


class GatedCapabilityExecutor(CapabilityExecutor):
    def __init__(self, gate: Gate) -> None:
        super().__init__()
        self.gate = gate
        self._calls = 0

    async def execute(self, intent: Intent, capabilities: Capabilities) -> Any:
        if intent.kind != IntentKind.TOOL_CALL:
            raise ValueError(f"GatedCapabilityExecutor only handles TOOL_CALL, got {intent.kind}")
        tool: str = intent.payload.get("tool", "")
        args: dict[str, Any] = intent.payload.get("args", {}) or {}
        if tool not in capabilities.allowed_tools:
            raise ToolNotAllowedError(tool=tool, allowed=set(capabilities.allowed_tools))
        handler = self._handlers.get(tool)
        if handler is None:
            raise ToolNotFoundError(tool=tool)
        self._calls += 1
        verdict = self.gate.check(tool, args, call_key=f"{tool}#{self._calls}")
        if not isinstance(verdict, Admitted):
            raise GovernanceRefused(tool, verdict.category, verdict.reason)
        try:
            result = await handler.execute(verdict.args)
        except BaseException:
            self.gate.fail(verdict)
            raise
        view = self.gate.finish(verdict, tool, result)
        for callback in self._post_callbacks:
            try:
                await callback(tool, verdict.args, view)
            except Exception:
                pass  # callbacks must never break execution
        return view


class GatedSession(GovernedSession):
    """A ``GovernedSession`` whose ``run(task)`` first gives the gate the user's request:
    to a ``GatedCapabilityExecutor`` in process, or as the daemon session's task when the
    capability executor is a daemon client that has not connected yet (one daemon
    connection carries one request; start a new session for a new one)."""

    async def run(self, task: str, *args: Any, **kwargs: Any):
        cap = self._cap_executor
        if isinstance(cap, GatedCapabilityExecutor):
            if not task.strip().startswith("/"):
                cap.gate.begin(task)
        elif isinstance(cap, DaemonCapabilityClient) and not cap._connected and not cap._task:
            cap._task = task  # sent in the hello of the first connection
        return await super().run(task, *args, **kwargs)
