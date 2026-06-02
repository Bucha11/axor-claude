"""
Composition root for axor-core's neutral "session tap" observers.

axor-core exposes two read-only seams on GovernedSession — a ``ContextTap`` that
receives a ``SessionContextView`` per turn, and a ``SessionSink`` that receives a
``SessionAuditRecord`` on session close. The security observers that drive those
seams (axor-probe's ``CoreContextTap`` and axor-sentinel's ``CoreSessionSink``)
live in sibling packages. THIS adapter is where they get wired into the session
core builds — it is the composition root.

Invariant P-34 is preserved here:

  * axor-probe and axor-sentinel are OPTIONAL dependencies (the ``[security]``
    extra). The base adapter declares only ``axor-core`` (plus the model SDK), so
    importing axor_claude never drags in probe/sentinel.
  * Their imports are LAZY — performed inside ``build_observers`` only when the
    caller explicitly opts in — so a base install with neither package present
    keeps working untouched.
  * There is no dependency cycle: axor-probe and axor-sentinel depend only on
    axor-core (structurally, under TYPE_CHECKING), never on this adapter. The
    adapter depends on them; they never depend back.
"""
from __future__ import annotations

from dataclasses import dataclass

_SECURITY_HINT = "install: pip install axor-claude[security]"


@dataclass
class SecurityObservers:
    """The wired-up observers handed back to the composition root.

    ``context_tap`` structurally satisfies core's ``ContextTap``; ``session_sink``
    structurally satisfies core's ``SessionSink``. Either may be ``None`` when the
    corresponding feature was not enabled.
    """

    context_tap: object | None = None
    session_sink: object | None = None

    def drain_sentinel(self) -> list:
        """Drain buffered sentinel session summaries, or ``[]`` if no sink."""
        if self.session_sink is not None:
            return self.session_sink.drain_pending()
        return []


def build_observers(
    *,
    probe_pipeline=None,
    enable_sentinel: bool = False,
) -> SecurityObservers:
    """Construct the optional security observers for a GovernedSession.

    Args:
        probe_pipeline: A fully-built axor-probe ``ProbePipeline`` (which only the
            caller can build, since it needs inference-backed components). Passing
            a pipeline IS how probe observation is enabled — there is no separate
            ``enable_probe`` flag because a probe tap is meaningless without a
            pipeline behind it. When ``None``, no context tap is created.
        enable_sentinel: When ``True``, construct a ``CoreSessionSink`` to buffer
            per-session audit records for the sentinel audit cycle.

    Returns:
        A ``SecurityObservers`` with whichever observers were requested set, and
        the rest left ``None``.

    Raises:
        RuntimeError: If an observer is requested but its optional package is not
            installed (the ``[security]`` extra was not selected).
    """
    observers = SecurityObservers()

    if enable_sentinel:
        try:
            from axor_sentinel.integration.core_sink import CoreSessionSink
        except ImportError as exc:
            raise RuntimeError(
                f"axor-sentinel is required for enable_sentinel — {_SECURITY_HINT}"
            ) from exc
        observers.session_sink = CoreSessionSink()

    if probe_pipeline is not None:
        try:
            from axor_probe.integration.core_tap import (
                CoreContextTap,
                ViewSnapshotFactory,
            )
        except ImportError as exc:
            raise RuntimeError(
                f"axor-probe is required for probe observation — {_SECURITY_HINT}"
            ) from exc

        # Break the probe wiring cycle: the tap needs the pipeline, and the
        # pipeline's snapshot_factory must be a ViewSnapshotFactory bound to that
        # tap. Construct the tap first, then rebind snapshot_factory to close the
        # loop (ProbePipeline is a mutable dataclass, so this is supported).
        tap = CoreContextTap(probe_pipeline, probe_pipeline.scheduler)
        probe_pipeline.snapshot_factory = ViewSnapshotFactory(tap)
        observers.context_tap = tap

    return observers
