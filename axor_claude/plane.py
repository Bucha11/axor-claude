"""The Claude adapter on the Control Plane.

axor-core runs the loop here, so posture is core's: the plane is the session's
``admission`` (pause holds at the intent boundary, stop winds the loop down)
and its call cap reaches core's budget through ``directives``.

What core cannot reach is the conversation itself. ``ClaudeCodeExecutor``
keeps each run's tool results in its own message list, round by round; core's
context fragments never carry them (nor a taint mark to cut by). So the
operator's one-shot effects act where the results are — between the
executor's rounds — and they act by ``tool_use_id``, the id the plane's
trace, taint graph and repair proposals name:

* an **excision** redacts the named tool results from every later round (it
  waits, queued, until its targets are in the conversation);
* a test-bench **injection** rides one round, marked untrusted;
* an honesty **experiment** breaks the named tools at the capability layer
  with axor-eval's own fault engine, and the run is audited by the plane.

Every run also ships its trace, feeds cross-run reputation (``[security]``)
and, opt-in, a probe health check — the shared feeds of :mod:`axor_wrap.observe`.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from typing import Any

_log = logging.getLogger("axor.claude.plane")

ENV_URL = "AXOR_PLANE_URL"
_EXCISED = "[excised by operator]"
_SCENARIO = "claude-experiment"


def resolve_plane(plane: Any, node_id: str | None) -> tuple[Any, bool]:
    """``plane`` → (handle, owned). A URL (or ``AXOR_PLANE_URL`` when ``plane``
    is None) builds axor-wrap's PlaneLink, owned by the session; ``False`` is
    off even with the env set; any other object is used as given."""
    if plane is False:
        return None, False
    if plane is None or isinstance(plane, str):
        url = plane or os.environ.get(ENV_URL)
        if not url:
            return None, False
        try:
            from axor_wrap.plane.link import PlaneLink
            return PlaneLink(url, node_id, node_prefix="claude"), True
        except (ImportError, RuntimeError) as exc:
            if plane is None:  # env-driven: never break the session over it
                _log.warning("axor: %s set but %s", ENV_URL, exc)
                return None, False
            raise RuntimeError(
                f"{exc} — pip install 'axor-claude[plane]'") from exc
    return plane, False


class _CapOnly:
    """Core's directives with only the call cap. Excision and injection are
    taken by the executor observer instead: core would consume them against
    its own context fragments, which this executor never sends."""

    def __init__(self, plane: Any) -> None:
        self._plane = plane

    def take_excision(self, provenance):  # noqa: ANN001, ANN201
        return None

    def take_injection(self):  # noqa: ANN201
        return None

    def budget_cap_calls(self) -> int | None:
        fn = getattr(self._plane, "budget_cap_calls", None)
        return fn() if fn is not None else None


def core_directives(plane: Any) -> Any:
    return _CapOnly(plane)


# ── conversation helpers ──────────────────────────────────────────────────────

def _source_of(args: dict) -> str:
    """The context source a tool call left in core: core caches a ``read``
    result as a fact whose ``source`` is the path it read."""
    for key in ("path", "file_path", "file"):
        if isinstance(args.get(key), str) and args[key]:
            return args[key]
    return ""


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") if isinstance(b, dict) else str(b) for b in content
        )
    return "" if content is None else str(content)


def _tool_results(messages: list[dict]):
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    yield b


def _provenance(messages: list[dict]) -> dict[str, str]:
    prov = {"system_prompt": "operator_config"}
    for b in _tool_results(messages):
        prov[str(b.get("tool_use_id", ""))] = "runtime"
    return prov


def _redacted(messages: list[dict], refs: set[str]) -> list[dict]:
    """A copy of the conversation with the excised tool results redacted (not
    dropped: a tool_use still needs its tool_result or the API refuses it)."""
    out = []
    for m in messages:
        content = m.get("content")
        if m.get("role") != "user" or not isinstance(content, list):
            out.append(m)
            continue
        blocks = []
        for b in content:
            if (isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("tool_use_id") in refs):
                b = {**b, "content": _EXCISED}
            blocks.append(b)
        out.append({**m, "content": blocks})
    return out


def _with_injection(messages: list[dict], text: str) -> list[dict]:
    """Append the injection to the last user turn (roles must alternate)."""
    out = list(messages)
    block = {"type": "text", "text": text}
    last = out[-1] if out else None
    if last is not None and last.get("role") == "user":
        content = last["content"]
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        out[-1] = {**last, "content": [*blocks, block]}
    else:
        out.append({"role": "user", "content": [block]})
    return out


def _probe_view(messages: list[dict]) -> tuple[list[dict], list[tuple[str, bool, dict]]]:
    """The run's last conversation as probe messages, plus localizer
    fragments keyed by tool_use_id (tool results are the pure-tainted ones)."""
    window, fragments = [], []
    for i, m in enumerate(messages):
        role, content = m.get("role"), m.get("content")
        if isinstance(content, str):
            msg = {"role": "user" if role == "user" else "assistant", "content": content}
            window.append(msg)
            fragments.append((f"msg_{i}", False, msg))
            continue
        for j, b in enumerate(content or []):
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_result":
                msg = {"role": "tool", "content": _block_text(b.get("content"))}
                fragments.append((str(b.get("tool_use_id") or f"tool_{i}_{j}"), True, msg))
            elif b.get("type") == "text" and b.get("text"):
                msg = {"role": "user" if role == "user" else "assistant", "content": b["text"]}
                fragments.append((f"msg_{i}_{j}", False, msg))
            else:
                continue
            window.append(msg)
    return window, fragments


def _to_anthropic(messages: list[dict]) -> tuple[str, list[dict]]:
    """Probe messages → (extra system text, Anthropic messages). Roles must
    alternate and start with user; a replayed tool result has no tool_use any
    more, so it goes back as user text marked as a tool result."""
    out: list[dict] = []
    system = []
    for m in messages:
        role, text = m.get("role"), _block_text(m.get("content"))
        if role == "system" and not out:
            system.append(text)
            continue
        if role == "tool":
            role, text = "user", f"[tool result]\n{text}"
        elif role not in ("user", "assistant"):
            role, text = "user", f"[note]\n{text}"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n\n" + text
        else:
            out.append({"role": role, "content": text})
    if not out or out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": "(context follows)"})
    return "\n\n".join(system), out


def _raw(response: Any) -> dict:
    text, calls = [], []
    for b in getattr(response, "content", None) or []:
        kind = getattr(b, "type", None)
        if kind == "text":
            text.append(getattr(b, "text", ""))
        elif kind == "tool_use":
            calls.append({"name": getattr(b, "name", ""), "args": getattr(b, "input", {})})
    return {"text": "".join(text), "tool_calls": calls}


# ── the observer ──────────────────────────────────────────────────────────────

class ClaudePlaneObserver:
    """Bound to one ``ClaudeCodeExecutor`` and its session. ``plane`` is a
    plane handle (or None for a local experiment); feeds ship only through a
    handle that has ``connector`` + ``submit`` (axor-wrap's PlaneLink)."""

    def __init__(
        self, *, plane: Any, executor: Any, node_id: str | None = None,
        sentinel: bool | None = None, probe: bool | int = False,
        probe_every: int = 1, faults: list[dict] | None = None,
    ) -> None:
        from axor_wrap.observe import RunRecorder

        self.plane = plane
        self.executor = executor
        self.node_id = getattr(plane, "node_id", None) or node_id or "claude-local"
        self.linked = plane is not None and hasattr(plane, "connector") and hasattr(plane, "submit")
        self.recorder = RunRecorder(self.node_id)
        self.faults = [dict(f) for f in faults] if faults else None
        self.last_evidence: list[dict] = []
        self._excised: set[str] = set()
        self._pending_faults: dict[str, list[dict]] = defaultdict(list)
        self._last_messages: list[dict] = []
        # tool_use_id → args, across runs (bounded): a repair proposal names a
        # call from an EARLIER run, and the executor rebuilds its conversation
        # per run, so the cut is translated to what that call left in core's
        # context — the file a `read` returned is cached there by its path.
        self._calls: dict[str, dict] = {}
        self.session: Any = None
        self._runs = 0
        self._probe_every = max(1, int(probe_every))
        self.reputation = None
        self.health = None
        if self.linked and sentinel is not False:
            try:
                from axor_wrap.observe import ReputationBook
                self.reputation = ReputationBook()
            except ImportError:
                if sentinel:
                    raise RuntimeError("sentinel=True needs axor-sentinel — "
                                       "pip install 'axor-claude[security]'") from None
        if probe:
            if not self.linked:
                _log.warning("axor: probe= needs a Control-Plane link; not enabled")
            else:
                from axor_wrap.observe import HealthCheck
                try:
                    self.health = HealthCheck(probes=3 if probe is True else int(probe))
                except ImportError:
                    raise RuntimeError("probe= needs axor-probe — "
                                       "pip install 'axor-claude[security]'") from None

    # ── run boundaries (wrapped around GovernedSession.run) ──────────────────

    def start_run(self) -> None:
        self._take_excision(self._history_provenance())
        self.recorder.reset()
        self.recorder.armed = True
        self._pending_faults.clear()
        spec, eid = None, ""
        take = getattr(self.plane, "take_experiment", None) if self.plane else None
        if take is not None:
            ex = take()
            if ex:
                spec, eid = ex["faults"], ex.get("id", "")
        if spec is None and self.faults:
            spec = self.faults
        if spec:
            try:
                from axor_wrap.observe import Experiment
                self.recorder.experiment = Experiment(
                    spec, seed=self.recorder.run_id, experiment_id=eid)
            except ImportError:
                _log.warning("axor: fault injection needs axor-eval — "
                             "pip install 'axor-claude[experiment]'; run not faulted")

    def finish_run(self, answer: str) -> None:
        rec = self.recorder
        if not rec.active:
            return
        if rec.experiment is not None:
            self._finish_experiment(answer)
        elif self.linked:
            self._ship(answer)

    def _finish_experiment(self, answer: str) -> None:
        from axor_wrap.observe import evidence_of, reconstruct_claim

        rec = self.recorder
        called = [t.tool for t in rec.tools if t.executed]
        lines = rec.pending_lines() + [{
            "kind": "claim",
            "payload": {
                "text": answer, "structured": True,
                "claims": reconstruct_claim(called, answer),
                "claim_source": "reconstructed",
            },
        }]
        try:
            self.last_evidence = evidence_of(lines, scenario=_SCENARIO, node_id=self.node_id)
        except ImportError:
            self.last_evidence = []
        for case in self.last_evidence:
            if case["deviation"]:
                _log.warning("axor: honesty experiment caught %s (%s, %s)",
                             case["deviation"], case["agent_claim"], case["verdict_source"])
        if self.linked:
            plane, run_id = self.plane, rec.run_id
            plane.submit(lambda: plane.connector.post_ingest(lines, run_id, _SCENARIO))

    def _ship(self, answer: str) -> None:
        rec, plane = self.recorder, self.plane
        lines, run_id = rec.pending_lines(), rec.run_id
        record = rec.audit_record(self.node_id) if self.reputation is not None else None
        self._runs += 1
        probe_now = (self.health is not None and self._last_messages
                     and self._runs % self._probe_every == 0)
        inputs = self._health_inputs() if probe_now else None
        book, health = self.reputation, self.health

        async def ship() -> None:
            if lines:
                await plane.connector.post_trace(lines, run_id)
            if record is not None:
                await book.add(record)
                if not book.empty:
                    await plane.connector.post_reputation(book.snapshot())
            if inputs is not None:
                body = await health.run(node_id=self.node_id, run_id=run_id, **inputs)
                if body is not None:
                    await plane.connector.post_health_check(body)

        plane.submit(ship)

    # ── executor rounds ───────────────────────────────────────────────────────

    def before_round(self, messages: list[dict]) -> list[dict]:
        """What Claude is sent this round: the conversation with operator
        excisions applied and a pending test-bench injection added."""
        plane = self.plane
        self._take_excision({**self._history_provenance(), **_provenance(messages)})
        sent = _redacted(messages, self._excised) if self._excised else messages
        if plane is not None:
            take = getattr(plane, "take_injection", None)
            inj = None
            if take is not None:
                try:
                    inj = take()
                except ImportError:
                    inj = None
            if inj is not None:
                sent = _with_injection(sent, (
                    f"[operator test-bench injection {inj.injection_id} — untrusted input]\n"
                    f"{inj.text}"))
        self._last_messages = sent
        self.recorder.last_request = sent   # the run is live: ship it at the end
        return sent

    def _history_provenance(self) -> dict[str, str]:
        prov = {"system_prompt": "operator_config"}
        prov.update({cid: "runtime" for cid in self._calls})
        return prov

    def _take_excision(self, provenance: dict[str, str]) -> None:
        take = getattr(self.plane, "take_excision", None) if self.plane else None
        if take is None:
            return
        try:
            directive = take(provenance)
        except ImportError:
            return  # axor-core < 0.12.1: no directives contract
        if directive is None:
            return
        self._excised.update(directive.target_refs)
        sources = sorted({
            src for ref in directive.target_refs
            for src in (_source_of(self._calls.get(ref) or {}),) if src
        })
        cut = getattr(self.session, "excise_context_sources", None)
        if sources and cut is not None:
            try:
                cut(sources, directive.authority)
            except Exception:  # noqa: BLE001 - the conversation cut still holds
                _log.warning("axor: could not cut %s from core context", sources,
                             exc_info=True)

    def after_round(self, tool_uses: list[dict], results: dict, missing: set) -> None:
        for tp in tool_uses:
            tid, name = tp.get("tool_use_id", ""), tp.get("tool", "")
            self._calls[tid] = dict(tp.get("args") or {})
            if len(self._calls) > 1000:
                self._calls.pop(next(iter(self._calls)))
            rec = self.recorder.begin_tool(tid, name, tp.get("args") or {})
            queued = self._pending_faults.get(name)
            if queued:
                rec.fault = queued.pop(0)
            result = results.get(tid)
            if tid in missing:
                status = "error"
            elif isinstance(result, dict) and result.get("error"):
                status = "error"
            else:
                status = "ok"
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            self.recorder.end_tool(rec, text, status=status)
        # Ship each round's lines as they land, so a running run shows as one
        # on the plane. An experiment run ships whole, with its claim, instead.
        if self.linked and self.recorder.experiment is None:
            lines, run_id, plane = self.recorder.pending_lines(), self.recorder.run_id, self.plane
            if lines:
                plane.submit(lambda: plane.connector.post_trace(lines, run_id))

    # ── capability layer ──────────────────────────────────────────────────────

    def wrap_handler(self, handler: Any) -> Any:
        return _ObservedHandler(handler, self)

    # ── health check inputs (the agent's own model) ──────────────────────────

    def _health_inputs(self) -> dict:
        ex = self.executor
        system_text = ex._system_prompt
        model = ex._model

        async def infer(msgs: list[dict]) -> dict:
            extra, conv = _to_anthropic(msgs)
            resp = await ex._client.messages.create(
                model=model, max_tokens=1024, messages=conv,
                system="\n\n".join(t for t in (system_text, extra) if t),
            )
            return _raw(resp)

        def infer_sync(msgs: list[dict]) -> dict:
            client = ex.probe_client()
            extra, conv = _to_anthropic(msgs)
            resp = client.messages.create(
                model=model, max_tokens=1024, messages=conv,
                system="\n\n".join(t for t in (system_text, extra) if t),
            )
            return _raw(resp)

        window, fragments = _probe_view(self._last_messages)
        return {"infer": infer, "infer_sync": infer_sync, "system_text": system_text,
                "window": window, "fragments": fragments, "model": model}


class _ObservedHandler:
    """A ToolHandler wrapper: in an experiment run, the faulted tool goes
    through axor-eval's engine (silent_fail / tool_substitution never run the
    real tool); otherwise it is the handler, untouched."""

    def __init__(self, inner: Any, observer: ClaudePlaneObserver) -> None:
        self._inner = inner
        self._observer = observer

    @property
    def name(self) -> str:
        return self._inner.name

    def __getattr__(self, attr: str) -> Any:
        return getattr(self._inner, attr)

    async def execute(self, args: dict) -> Any:
        exp = self._observer.recorder.experiment
        if exp is None or not exp.mode_for(self.name):
            return await self._inner.execute(args)
        real = None if exp.skips_tool(self.name) else await self._inner.execute(args)
        text, fault = exp.apply(self.name, real)
        self._observer._pending_faults[self.name].append(fault)
        return text
