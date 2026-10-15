"""
axor-claude — Claude adapter for axor-core.

Quick start:

    import axor_claude
    session = axor_claude.make_session()
    result = await session.run("refactor the auth module")
"""

from axor_claude.executor import ClaudeCodeExecutor
from axor_claude.tools.read import ReadHandler
from axor_claude.tools.write import WriteHandler
from axor_claude.tools.bash import BashHandler
from axor_claude.tools.search import SearchHandler
from axor_claude.tools.glob import GlobHandler
from axor_claude.extensions.skill_loader import ClaudeSkillLoader
from axor_claude.extensions.plugin_loader import ClaudePluginLoader
from axor_claude.tool_definitions import register_tool_definition
from axor_claude.security import SecurityObservers, build_observers
from axor_claude.gate import Gate
from axor_claude.capability_gate import GatedCapabilityExecutor, GatedSession, GovernanceRefused
from axor_claude._version import get_version

__version__ = get_version("axor-claude")

__all__ = [
    "ClaudeCodeExecutor",
    "ReadHandler", "WriteHandler", "BashHandler",
    "SearchHandler", "GlobHandler",
    "ClaudeSkillLoader", "ClaudePluginLoader",
    "register_tool_definition",
    "SecurityObservers", "build_observers",
    "Gate", "GatedCapabilityExecutor", "GatedSession", "GovernanceRefused",
    "make_session",
]


def make_session(
    api_key=None,
    *,
    tools=("read", "write", "bash", "search", "glob"),
    load_skills=True,
    load_plugins=True,
    model=None,
    system_prompt=None,
    daemon_socket: str | None = None,
    mode: str = "library",
    probe_pipeline=None,
    enable_sentinel: bool = False,
    governance=None,
    confirmer=None,
    structured=None,
    **session_kwargs,
):
    """
    Convenience factory. Creates GovernedSession with all standard handlers.

    Args:
        daemon_socket: Unix socket path for AxorDaemon. When set, all tool
            execution is delegated to the daemon process via DaemonCapabilityClient
            instead of running in-process. The daemon must be started separately.
        mode: Execution isolation mode — "library" (default, in-process),
            "production" (LockedExecutor + governance bypass blocked),
            "strict" (superset of production with additional containment).
        probe_pipeline: Optional axor-probe ``ProbePipeline``. Passing one wires an
            axor-probe context tap onto the session (requires the ``[security]``
            extra). Leaving it ``None`` keeps probe observation off.
        enable_sentinel: When ``True``, wire an axor-sentinel session sink onto the
            session (requires the ``[security]`` extra).
        governance: The kernel gate's configuration — a ``GovernanceConfig``, the path
            of its YAML, or its mapping. Every in-process tool call goes through that
            gate (``ToolCallGovernor``: grants, references, floor, consequence class);
            ``None`` gates under an empty configuration. With ``daemon_socket`` the
            daemon's own ``--governance`` decides, and the request is sent to it.
        confirmer: The operator's L4 confirmation channel (in process only).
        structured: The platform's parser of a description source's output
            (axor-core MODEL.md 9.15): ``structured(tool, result) -> structure | None``.
            Written by the operator for the tools it deploys; the package ships none.
            Without it no description grant is computed, so a call only a description
            would authorize is refused. With ``daemon_socket``: the daemon's
            ``--structured``.

    Example:
        session = axor_claude.make_session(soft_token_limit=100_000)
        result = await session.run("refactor auth module")
    """
    from axor_core.contracts.mode import ExecutionMode

    if daemon_socket is not None:
        if governance is not None or confirmer is not None or structured is not None:
            raise ValueError("with daemon_socket the daemon's --governance decides; "
                             "pass governance / confirmer to the daemon, not here")
        from axor_core.capability.daemon_client import DaemonCapabilityClient
        cap = DaemonCapabilityClient(socket_path=daemon_socket)
    else:
        _handlers = {
            "read": ReadHandler, "write": WriteHandler, "bash": BashHandler,
            "search": SearchHandler, "glob": GlobHandler,
        }
        cap = GatedCapabilityExecutor(Gate.from_config(governance or {}, confirmer=confirmer,
                                                       structured=structured))
        for name in tools:
            if cls := _handlers.get(name):
                cap.register(cls())

    loaders = []
    if load_skills:
        loaders.append(ClaudeSkillLoader())
    if load_plugins:
        loaders.append(ClaudePluginLoader())

    executor_kwargs = {}
    if model:
        executor_kwargs["model"] = model
    if system_prompt:
        executor_kwargs["system_prompt"] = system_prompt

    # Composition root: merge caller-supplied taps/sinks with the optional
    # security observers (probe tap / sentinel sink) before building the session.
    context_taps = list(session_kwargs.pop("context_taps", None) or [])

    observers = build_observers(
        probe_pipeline=probe_pipeline,
        enable_sentinel=enable_sentinel,
    )
    if observers.context_tap is not None:
        context_taps.append(observers.context_tap)
    # the kernel's GovernedSession takes one session_sink (the caller's wins)
    if observers.session_sink is not None and "session_sink" not in session_kwargs:
        session_kwargs["session_sink"] = observers.session_sink

    session = GatedSession(
        executor=ClaudeCodeExecutor(api_key=api_key, **executor_kwargs),
        capability_executor=cap,
        extension_loaders=loaders,
        mode=ExecutionMode(mode),
        context_taps=context_taps or None,
        **session_kwargs,
    )
    session.axor_security = observers
    return session
