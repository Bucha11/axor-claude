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
from axor_claude._version import get_version

__version__ = get_version("axor-claude")

__all__ = [
    "ClaudeCodeExecutor",
    "ReadHandler", "WriteHandler", "BashHandler",
    "SearchHandler", "GlobHandler",
    "ClaudeSkillLoader", "ClaudePluginLoader",
    "register_tool_definition",
    "SecurityObservers", "build_observers",
    "make_session",
]


def _supports_directives(session_cls) -> bool:
    import inspect
    return "directives" in inspect.signature(session_cls.__init__).parameters


class _FanOutSink:
    """Several session sinks behind core's single ``session_sink`` slot. One
    failing sink never stops the others (the SessionSink contract: never raise)."""

    def __init__(self, sinks):
        self.sinks = list(sinks)

    async def on_session_closed(self, record) -> None:
        for sink in self.sinks:
            try:
                await sink.on_session_closed(record)
            except Exception:  # noqa: BLE001 — observer failure must not leak
                import logging
                logging.getLogger("axor_claude").exception("session sink failed")


def _combine_sinks(sinks):
    if not sinks:
        return None
    if len(sinks) == 1:
        return sinks[0]
    return _FanOutSink(sinks)


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
    plane=None,
    node_id: str | None = None,
    sentinel: bool | None = None,
    probe: bool | int = False,
    probe_every: int = 1,
    faults: list[dict] | None = None,
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
        plane: The Control Plane. A URL — or nothing, with ``AXOR_PLANE_URL``
            set — connects this session as a node (``[plane]`` extra; a
            background link, configured by ``AXOR_NODE_ID`` /
            ``AXOR_INGEST_KEY`` / ``AXOR_TEST_BENCH``). A handle you built
            (axor-wrap ``PlaneLink`` / ``PlaneAdmission``) is used as given;
            ``False`` turns it off. Pause/stop hold the loop at the intent
            boundary and the call cap reaches core's budget; excision and
            test-bench injection act on the conversation between rounds, by
            ``tool_use_id``. Every run ships its trace; see
            :mod:`axor_claude.plane`.
        node_id: The node's name on the plane (default ``claude-<host>``).
        sentinel: Cross-run reputation on the plane (default: on when
            ``[security]`` is installed and a link is up).
        probe: Health check after each run (``True`` = 3 probes, or a count),
            with the session's own model — it costs model calls.
        probe_every: Run the health check every N runs.
        faults: A local honesty experiment, e.g.
            ``[{"tool": "read", "mode": "silent_fail"}]`` (``[experiment]``):
            every run is audited and ``session.axor_plane.last_evidence``
            holds the verdict.

    Example:
        session = axor_claude.make_session(soft_token_limit=100_000)
        result = await session.run("refactor auth module")
    """
    from axor_core import GovernedSession, CapabilityExecutor
    from axor_core.contracts.mode import ExecutionMode

    executor_kwargs = {}
    if model:
        executor_kwargs["model"] = model
    if system_prompt:
        executor_kwargs["system_prompt"] = system_prompt
    executor = ClaudeCodeExecutor(api_key=api_key, **executor_kwargs)

    from axor_claude.plane import ClaudePlaneObserver, core_directives, resolve_plane
    plane_handle, owns_plane = resolve_plane(plane, node_id)
    observer = None
    if plane_handle is not None or faults:
        observer = ClaudePlaneObserver(
            plane=plane_handle, executor=executor, node_id=node_id,
            sentinel=sentinel, probe=probe, probe_every=probe_every, faults=faults,
        )
        executor.set_observer(observer)

    if daemon_socket is not None:
        from axor_core.capability.daemon_client import DaemonCapabilityClient
        cap = DaemonCapabilityClient(socket_path=daemon_socket)
        if faults:
            import logging
            logging.getLogger("axor_claude").warning(
                "faults= needs in-process tools; the daemon runs them — not faulted")
    else:
        _handlers = {
            "read": ReadHandler, "write": WriteHandler, "bash": BashHandler,
            "search": SearchHandler, "glob": GlobHandler,
        }
        cap = CapabilityExecutor()
        for name in tools:
            if cls := _handlers.get(name):
                handler = cls()
                cap.register(observer.wrap_handler(handler) if observer else handler)

    loaders = []
    if load_skills:
        loaders.append(ClaudeSkillLoader())
    if load_plugins:
        loaders.append(ClaudePluginLoader())

    # Composition root: merge caller-supplied taps/sinks with the optional
    # security observers (probe tap / sentinel sink) before building the session.
    # Core takes ONE ``session_sink``; ``session_sinks`` is accepted as a list
    # for back-compat and fanned out.
    context_taps = list(session_kwargs.pop("context_taps", None) or [])
    sinks = list(session_kwargs.pop("session_sinks", None) or [])
    if (one := session_kwargs.pop("session_sink", None)) is not None:
        sinks.insert(0, one)

    observers = build_observers(
        probe_pipeline=probe_pipeline,
        enable_sentinel=enable_sentinel,
    )
    if observers.context_tap is not None:
        context_taps.append(observers.context_tap)
    if observers.session_sink is not None:
        sinks.append(observers.session_sink)

    if plane_handle is not None:
        session_kwargs.setdefault("admission", plane_handle)
        if _supports_directives(GovernedSession):
            # the call cap only: excision/injection act on the executor's
            # conversation (the observer), which core's context never holds
            session_kwargs.setdefault("directives", core_directives(plane_handle))

    session = GovernedSession(
        executor=executor,
        capability_executor=cap,
        extension_loaders=loaders,
        mode=ExecutionMode(mode),
        context_taps=context_taps or None,
        session_sink=_combine_sinks(sinks),
        **session_kwargs,
    )
    session.axor_security = observers
    if observer is not None:
        _bind_observer(session, observer, plane_handle if owns_plane else None)
    return session


def _bind_observer(session, observer, owned_link) -> None:
    """Run boundaries for the observer: each ``session.run`` is one plane run
    (its trace, audit, health check); ``aclose`` closes a link we opened."""
    run, aclose = session.run, session.aclose

    async def observed_run(task, *args, **kwargs):
        observer.start_run()
        answer = ""
        try:
            result = await run(task, *args, **kwargs)
            answer = getattr(result, "output", "") or ""
            return result
        finally:
            observer.finish_run(answer if isinstance(answer, str) else str(answer))

    async def observed_aclose():
        try:
            await aclose()
        finally:
            if owned_link is not None:
                owned_link.close()

    session.run = observed_run
    session.aclose = observed_aclose
    session.axor_plane = observer
    observer.session = session
