"""make_session builds a real GovernedSession without network — wiring only."""
from __future__ import annotations

import asyncio

import pytest

import axor_claude
from axor_claude import _FanOutSink, _supports_directives


class _Sink:
    def __init__(self, fail: bool = False) -> None:
        self.records: list = []
        self.fail = fail

    async def on_session_closed(self, record) -> None:
        if self.fail:
            raise RuntimeError("boom")
        self.records.append(record)


class _Plane:
    """Minimal PlaneAdmission stand-in: admission + directives surface."""

    async def await_admission(self) -> None:
        return None

    def take_excision(self, provenance):
        return None

    def take_injection(self):
        return None

    def budget_cap_calls(self):
        return None


def _make(**kw):
    return axor_claude.make_session(
        api_key="x", load_skills=False, load_plugins=False, **kw
    )


def test_make_session_builds_on_installed_core():
    session = _make()
    assert session is not None


def test_single_sink_passed_through():
    sink = _Sink()
    session = _make(session_sink=sink)
    assert session._session_sink is sink


def test_legacy_session_sinks_list_fans_out_and_isolates_failures():
    good, bad = _Sink(), _Sink(fail=True)
    session = _make(session_sinks=[bad, good])
    fan = session._session_sink
    assert isinstance(fan, _FanOutSink)
    asyncio.run(fan.on_session_closed("rec"))
    assert good.records == ["rec"]


def test_plane_becomes_admission_and_directives():
    plane = _Plane()
    session = _make(plane=plane)
    assert session._admission is plane
    if _supports_directives(type(session)):
        # core gets the call cap only; excision/injection act in the executor
        plane.budget_cap_calls = lambda: 7
        assert session._directives.budget_cap_calls() == 7
        assert session._directives.take_excision({}) is None
    assert session._executor._observer is session.axor_plane


def test_explicit_admission_wins_over_plane():
    plane, other = _Plane(), _Plane()
    session = _make(plane=plane, admission=other)
    assert session._admission is other


@pytest.mark.parametrize("bad", ["session_sinks"])
def test_no_unknown_kwargs_leak_to_core(bad):
    # Regression: 0.4.0 forwarded `session_sinks=` to GovernedSession → TypeError.
    _make(**{bad: []})
