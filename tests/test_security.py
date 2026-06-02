"""Tests for the security composition root (axor_claude.security).

These exercise build_observers deterministically WITHOUT requiring the real
axor-probe / axor-sentinel packages, by injecting fake lazy-import modules into
sys.modules. P-34 is the point: the base adapter must never hard-depend on them.
"""
from __future__ import annotations

import sys
import types

import pytest

from axor_claude.security import SecurityObservers, build_observers


# ── Fakes mirroring the real structural shapes ────────────────────────────────

class _FakeCoreSessionSink:
    """Stands in for axor_sentinel.integration.core_sink.CoreSessionSink."""

    def __init__(self) -> None:
        self.drained = False

    def drain_pending(self) -> list:
        self.drained = True
        return ["summary-a", "summary-b"]


class _FakeCoreContextTap:
    """Stands in for axor_probe.integration.core_tap.CoreContextTap."""

    def __init__(self, pipeline, scheduler) -> None:
        self.pipeline = pipeline
        self.scheduler = scheduler


class _FakeViewSnapshotFactory:
    """Stands in for axor_probe.integration.core_tap.ViewSnapshotFactory."""

    def __init__(self, tap) -> None:
        self.tap = tap


class _FakeProbePipeline:
    """Mutable object with .scheduler and .snapshot_factory, like ProbePipeline."""

    def __init__(self) -> None:
        self.scheduler = object()
        self.snapshot_factory = None


# ── Fixtures that install / remove the fake lazy-import modules ────────────────

def _install_module(monkeypatch, name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


@pytest.fixture
def fake_sentinel(monkeypatch):
    _install_module(
        monkeypatch,
        "axor_sentinel.integration.core_sink",
        CoreSessionSink=_FakeCoreSessionSink,
    )


@pytest.fixture
def fake_probe(monkeypatch):
    _install_module(
        monkeypatch,
        "axor_probe.integration.core_tap",
        CoreContextTap=_FakeCoreContextTap,
        ViewSnapshotFactory=_FakeViewSnapshotFactory,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_no_args_yields_empty_observers():
    observers = build_observers()
    assert isinstance(observers, SecurityObservers)
    assert observers.context_tap is None
    assert observers.session_sink is None
    assert observers.drain_sentinel() == []


def test_enable_sentinel_constructs_sink_and_drain_delegates(fake_sentinel):
    observers = build_observers(enable_sentinel=True)
    assert isinstance(observers.session_sink, _FakeCoreSessionSink)
    assert observers.context_tap is None

    result = observers.drain_sentinel()
    assert result == ["summary-a", "summary-b"]
    assert observers.session_sink.drained is True


def test_enable_sentinel_missing_package_raises_runtimeerror(monkeypatch):
    # Make the lazy import fail by mapping the module to None in sys.modules,
    # which forces `import` to raise ImportError.
    monkeypatch.setitem(sys.modules, "axor_sentinel.integration.core_sink", None)
    with pytest.raises(RuntimeError, match=r"\[security\]"):
        build_observers(enable_sentinel=True)


def test_probe_pipeline_wires_tap_and_breaks_cycle(fake_probe):
    pipeline = _FakeProbePipeline()
    original_scheduler = pipeline.scheduler

    observers = build_observers(probe_pipeline=pipeline)

    # A CoreContextTap is constructed with (pipeline, pipeline.scheduler).
    assert isinstance(observers.context_tap, _FakeCoreContextTap)
    assert observers.context_tap.pipeline is pipeline
    assert observers.context_tap.scheduler is original_scheduler

    # The cycle is closed: snapshot_factory is rebound to a ViewSnapshotFactory
    # bound to the freshly constructed tap.
    assert isinstance(pipeline.snapshot_factory, _FakeViewSnapshotFactory)
    assert pipeline.snapshot_factory.tap is observers.context_tap

    assert observers.session_sink is None


def test_probe_pipeline_missing_package_raises_runtimeerror(monkeypatch):
    monkeypatch.setitem(sys.modules, "axor_probe.integration.core_tap", None)
    with pytest.raises(RuntimeError, match=r"\[security\]"):
        build_observers(probe_pipeline=_FakeProbePipeline())


def test_both_observers_can_be_built_together(fake_probe, fake_sentinel):
    pipeline = _FakeProbePipeline()
    observers = build_observers(probe_pipeline=pipeline, enable_sentinel=True)
    assert isinstance(observers.context_tap, _FakeCoreContextTap)
    assert isinstance(observers.session_sink, _FakeCoreSessionSink)
    assert observers.drain_sentinel() == ["summary-a", "summary-b"]
