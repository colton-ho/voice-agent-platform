"""U-206 tests: §6.4 human handoff (decision D-5 gated; A-2 FINAL).

Hermetic, no network. Conventions mirror tests/test_status_callbacks.py (env
seam ``bot._twilio_config`` read fresh per request, real signatures computed
with the stdlib oracle ``twilio_sig``, ``bot._status_call_repo`` doubled) and
the in-file FakeLLM/FakeTask/FakeParams pattern from
tests/test_call_control_tools.py (those tests run UNCHANGED alongside this
file — the D-5 default path must stay byte-identical).

Covered:

  * D-5 record+hangup default unchanged: with no flow (or the capability flag
    off) transfer_to_human behaves exactly as before — transfer_requested +
    transfer_topic marked, outcome row written, EndTaskFrame queued; write
    ordering asserted (mark_transfer -> write_outcome -> teardown) on every
    exit path (R-P-005 write-before-teardown).
  * §6.4 behind supports_transfer (FC-1 capability flag,
    providers/base.py:95): approved plan -> <Dial> TwiML with the resolved
    target; resolution chain deny-by-default at every stage — empty allowlist
    denied, allowlist hit but tenant auth denied, destination policy denied;
    malformed approved target fails closed; missing/failed call-update
    trigger falls back to D-5 record+hangup (caller never dropped).
  * No caller drop on busy/no-answer/failure (R-P-005 acceptance): every
    dial outcome yields a <Say>+<Hangup> TwiML (audible, never a silent
    drop) and a recorded outcome state. The three acceptance cases
    (no-answer, busy, failure) have explicit tests at BOTH the flow layer
    and the /twilio/transfer-result endpoint.
  * /twilio/transfer-result wire contract: 503 unconfigured / 403 bad
    signature (FC-8 seam, external URL), durable outcome record with
    deterministic event_id dedup (duplicate callbacks -> single append),
    persist-before-ack 5xx then retry, missing CallSid -> fallback TwiML
    served (not an empty response that would drop the caller).
  * TwiML builders (providers/telephony/twilio_adapter, additive):
    build_twiml_dial_transfer (E.164 validation, timeout/record/action
    attrs, action vs trailing-fallback modes) and build_twiml_say_hangup
    (XML escaping).
"""

from __future__ import annotations

import asyncio
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

import bot
import call_control_tools
import twilio_sig
from call_control_tools import TransferFlow, register_call_control_handlers
from pipecat.frames.frames import EndTaskFrame
from providers.telephony.twilio_adapter import (
    TRANSFER_FALLBACK_SAY,
    TRANSFER_GOODBYE_SAY,
    build_twiml_dial_transfer,
    build_twiml_say_hangup,
)
from storage.base import (
    CallEvent,
    CallFinalizedError,
    CallRecord,
    CallRepository,
    CallState,
)

PUBLIC_BASE = "https://twilio.example.com"
AUTH_TOKEN = "test-auth-token-456"
ACCOUNT_SID = "ACyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
RESULT_PATH = "/twilio/transfer-result"
TARGET_E164 = "+85261234567"


# ===========================================================================
# Handler-layer fakes (tests/test_call_control_tools.py pattern) + ordered log
# ===========================================================================
class _FakeLLM:
    def __init__(self) -> None:
        self.handlers: dict = {}

    def register_function(self, name, handler) -> None:
        self.handlers[name] = handler


class _FakeTask:
    def __init__(self, log: list) -> None:
        self.queued: list = []
        self._log = log

    async def queue_frames(self, frames):
        self.queued.extend(frames)
        for f in frames:
            if isinstance(f, EndTaskFrame):
                self._log.append(("end_task", f.reason))


class _FakeParams:
    def __init__(self, arguments: dict | None = None) -> None:
        self.arguments = arguments or {}
        self.results: list = []

        async def _cb(result):
            self.results.append(result)

        self.result_callback = _cb


class _Harness:
    """Ordered side-effect log shared by every seam, so tests can assert
    write-before-teardown ordering across mark_transfer / write_outcome /
    start_transfer / EndTaskFrame."""

    def __init__(self, *, flow=None, trigger_ok: bool = True) -> None:
        self.log: list = []
        self.states: list = []          # (state, detail) via flow record_state
        self.trigger_plans: list = []   # plans passed to start_transfer
        self.trigger_ok = trigger_ok
        self.llm = _FakeLLM()
        self.task = _FakeTask(self.log)
        self.flow = flow

    def make_flow(self, **kw) -> TransferFlow:
        """TransferFlow wired to this harness's state recorder."""
        kw.setdefault(
            "record_state",
            lambda state, detail: self.states.append((state, dict(detail))),
        )
        flow = TransferFlow(**kw)
        self.flow = flow
        return flow

    def _mark(self, topic):
        self.log.append(("mark_transfer", topic))

    def _hist(self, kind, data):
        self.log.append(("history", kind))

    async def _write(self):
        self.log.append(("write_outcome",))

    async def _emit(self, payload):
        self.log.append(("emit", payload.get("name")))

    async def _start(self, plan):
        self.log.append(("start_transfer", plan))
        self.trigger_plans.append(plan)
        return self.trigger_ok

    async def fire_transfer(self, topic: str = "billing"):
        register_call_control_handlers(
            self.llm, self.task,
            call_id="c-1",
            emit=self._emit,
            scope="phone",
            history_append=self._hist,
            mark_transfer=self._mark,
            write_outcome=self._write,
            transfer_flow=self.flow,
            start_transfer=self._start,
            tenant_id="tenant-a",
        )
        params = _FakeParams(arguments={"topic": topic})
        await self.llm.handlers["transfer_to_human"](params)
        await asyncio.sleep(0.05)  # let the grace task finish (grace=0.01)
        return params

    async def fire_end_call(self, reason: str = "user_requested"):
        register_call_control_handlers(
            self.llm, self.task,
            call_id="c-1",
            emit=self._emit,
            scope="phone",
            history_append=self._hist,
            mark_transfer=self._mark,
            write_outcome=self._write,
            transfer_flow=self.flow,
            start_transfer=self._start,
            tenant_id="tenant-a",
        )
        params = _FakeParams(arguments={"reason": reason})
        await self.llm.handlers["end_call"](params)
        await asyncio.sleep(0.05)
        return params

    def kinds(self) -> list:
        return [entry[0] for entry in self.log]

    def assert_order(self, *kinds: str) -> None:
        """Each kind must occur, in the given relative order."""
        seen = []
        for kind in kinds:
            idx = next((i for i, k in enumerate(self.kinds()) if k == kind), None)
            assert idx is not None, f"{kind!r} never happened; log={self.log}"
            seen.append(idx)
        assert seen == sorted(seen), f"out of order {kinds}: log={self.log}"

    def end_frames(self) -> list:
        return [f for f in self.task.queued if isinstance(f, EndTaskFrame)]


@pytest.fixture(autouse=True)
def _fast_grace(monkeypatch):
    monkeypatch.setattr(call_control_tools, "GRACE_SECONDS", 0.01)


def _ok_allowlist_flow(h: _Harness, **kw) -> TransferFlow:
    """Full-pass resolution chain (all three stages approve)."""
    kw.setdefault("supports_transfer", True)
    kw.setdefault("resolve_target", lambda topic: TARGET_E164)
    kw.setdefault("tenant_authorize", lambda tenant_id, target: True)
    kw.setdefault("destination_policy", lambda target: True)
    return h.make_flow(**kw)


def _records(states: list):
    return lambda state, detail: states.append((state, dict(detail)))


# ===========================================================================
# 1. TwiML builders (providers/telephony/twilio_adapter — additive)
# ===========================================================================
def test_build_twiml_dial_transfer_default_has_fallback_tail():
    """No action_url -> the <Dial> is followed by <Say>+<Hangup>, so a
    busy/no-answer/failed dial falls through to an audible message instead
    of silently dropping the caller (R-P-005)."""
    xml = build_twiml_dial_transfer(TARGET_E164)
    root = ET.fromstring(xml)
    assert root.tag == "Response"
    dial = root.find("Dial")
    assert dial is not None
    assert dial.find("Number").text == TARGET_E164
    assert dial.get("timeout") == "30"
    assert dial.get("action") is None
    assert dial.get("record") is None
    say = root.find("Say")
    assert say is not None and say.text == TRANSFER_FALLBACK_SAY
    assert root.find("Hangup") is not None


def test_build_twiml_dial_transfer_action_mode_has_no_trailing_verbs():
    """With action_url the Dial's action endpoint owns everything after the
    dial (outcome recording + per-status TwiML) — no trailing verbs."""
    xml = build_twiml_dial_transfer(
        TARGET_E164,
        timeout_s=45,
        record=True,
        action_url="https://twilio.example.com/twilio/transfer-result",
    )
    root = ET.fromstring(xml)
    dial = root.find("Dial")
    assert dial is not None
    assert dial.get("timeout") == "45"
    assert dial.get("record") == "record-from-answer"
    assert dial.get("action") == "https://twilio.example.com/twilio/transfer-result"
    assert dial.get("method") == "POST"
    assert root.find("Say") is None
    assert root.find("Hangup") is None


def test_build_twiml_dial_transfer_rejects_non_e164_targets():
    """Non-numbers must never reach <Dial> (builder-boundary validation)."""
    for bad in ("", "12345", "not-a-number", "+0123456789", "+12; <x>", "+12345", " "):
        with pytest.raises(ValueError):
            build_twiml_dial_transfer(bad)


def test_build_twiml_dial_transfer_rejects_bad_timeout():
    with pytest.raises(ValueError):
        build_twiml_dial_transfer(TARGET_E164, timeout_s=0)
    with pytest.raises(ValueError):
        build_twiml_dial_transfer(TARGET_E164, timeout_s=-5)


def test_build_twiml_dial_transfer_custom_fallback_copy():
    xml = build_twiml_dial_transfer(TARGET_E164, fallback_say="custom msg")
    assert ET.fromstring(xml).find("Say").text == "custom msg"


def test_build_twiml_say_hangup_shape_and_escaping():
    xml = build_twiml_say_hangup("a < b & c")
    root = ET.fromstring(xml)  # well-formed despite the angle brackets
    assert root.tag == "Response"
    assert root.find("Say").text == "a < b & c"
    assert root.find("Hangup") is not None


# ===========================================================================
# 2. TransferFlow resolution chain (spec §6.4: allowlist -> tenant auth ->
#    destination policy, deny-by-default)
# ===========================================================================
def test_default_seams_deny_everything():
    """Conservative default: EMPTY allowlist -> transfer denied + outcome
    recorded; no TwiML is produced (D-5 record+hangup fallback)."""
    states: list = []
    flow = TransferFlow(supports_transfer=True, record_state=_records(states))
    plan = flow.plan(tenant_id="t1", topic="billing")
    assert plan.approved is False
    assert plan.target is None
    assert plan.state == "failed"
    assert "allowlist" in plan.reason
    assert plan.twiml == ""
    assert [s for s, _ in states] == ["requested", "failed"]


def test_capability_flag_off_denies_even_with_full_allowlist():
    """supports_transfer (FC-1 capability flag) gates the §6.4 path: flag
    off -> denied, whatever the allowlist says."""
    states: list = []
    flow = TransferFlow(
        supports_transfer=False,
        resolve_target=lambda topic: TARGET_E164,
        tenant_authorize=lambda tenant_id, target: True,
        destination_policy=lambda target: True,
        record_state=_records(states),
    )
    plan = flow.plan(topic="billing")
    assert plan.approved is False
    assert "supports_transfer" in plan.reason
    assert plan.twiml == ""


def test_allowlist_hit_but_tenant_auth_denied():
    states: list = []
    flow = TransferFlow(
        supports_transfer=True,
        resolve_target=lambda topic: TARGET_E164,
        tenant_authorize=lambda tenant_id, target: False,
        destination_policy=lambda target: True,
        record_state=_records(states),
    )
    plan = flow.plan(tenant_id="tenant-x", topic="billing")
    assert plan.approved is False
    assert plan.target == TARGET_E164      # allowlist DID resolve it
    assert "tenant" in plan.reason
    assert plan.twiml == ""
    assert [s for s, _ in states] == ["requested", "failed"]


def test_destination_policy_denied():
    states: list = []
    flow = TransferFlow(
        supports_transfer=True,
        resolve_target=lambda topic: TARGET_E164,
        tenant_authorize=lambda tenant_id, target: True,
        destination_policy=lambda target: False,
        record_state=_records(states),
    )
    plan = flow.plan(topic="billing")
    assert plan.approved is False
    assert plan.target == TARGET_E164
    assert "destination policy" in plan.reason
    assert plan.twiml == ""


def test_approved_plan_builds_dial_twiml_and_records_connecting():
    states: list = []
    flow = TransferFlow(
        supports_transfer=True,
        resolve_target=lambda topic: TARGET_E164,
        tenant_authorize=lambda tenant_id, target: True,
        destination_policy=lambda target: True,
        record_state=_records(states),
    )
    plan = flow.plan(tenant_id="t1", topic="billing")
    assert plan.approved is True
    assert plan.target == TARGET_E164
    assert plan.state == "connecting"
    root = ET.fromstring(plan.twiml)
    assert root.find("Dial/Number").text == TARGET_E164
    assert [s for s, _ in states] == ["requested", "connecting"]
    assert states[1][1]["target"] == TARGET_E164


def test_malformed_target_from_resolver_fails_closed():
    """A resolver that hands back a non-E.164 is denied, not dialed."""
    states: list = []
    flow = TransferFlow(
        supports_transfer=True,
        resolve_target=lambda topic: "totally-not-a-number",
        tenant_authorize=lambda tenant_id, target: True,
        destination_policy=lambda target: True,
        record_state=_records(states),
    )
    plan = flow.plan(topic="billing")
    assert plan.approved is False
    assert plan.twiml == ""
    assert [s for s, _ in states] == ["requested", "failed"]


def test_chain_order_allowlist_before_tenant_before_policy():
    """The chain short-circuits: allowlist denial never consults tenant or
    policy; tenant denial never consults policy."""
    calls: list = []
    flow = TransferFlow(
        supports_transfer=True,
        resolve_target=lambda topic: None,
        tenant_authorize=lambda t, tgt: calls.append("tenant") or True,
        destination_policy=lambda tgt: calls.append("policy") or True,
    )
    flow.plan(topic="x")
    assert calls == []

    flow2 = TransferFlow(
        supports_transfer=True,
        resolve_target=lambda topic: TARGET_E164,
        tenant_authorize=lambda t, tgt: calls.append("tenant") or False,
        destination_policy=lambda tgt: calls.append("policy") or True,
    )
    flow2.plan(topic="x")
    assert calls == ["tenant"]


# ===========================================================================
# 3. dial_result — the three acceptance cases (no-answer / busy / failure):
#    caller NOT dropped (Say+Hangup, never silent) + outcome recorded
# ===========================================================================
@pytest.mark.parametrize("status", ["no-answer", "busy", "failed"])
def test_dial_result_failure_cases_not_dropped_and_recorded(status):
    states: list = []
    flow = TransferFlow(record_state=_records(states))
    outcome = flow.dial_result(dial_status=status)
    assert outcome.state == "failed"
    assert outcome.detail == status
    root = ET.fromstring(outcome.twiml)  # caller hears a message first
    assert root.find("Say").text == TRANSFER_FALLBACK_SAY
    assert root.find("Hangup") is not None
    assert states == [("failed", {"dial_status": status})]


def test_dial_result_no_answer_acceptance_case():
    outcome = TransferFlow().dial_result(dial_status="no-answer")
    assert outcome.state == "failed" and outcome.detail == "no-answer"
    assert "<Say>" in outcome.twiml and "<Hangup/>" in outcome.twiml


def test_dial_result_busy_acceptance_case():
    outcome = TransferFlow().dial_result(dial_status="busy")
    assert outcome.state == "failed" and outcome.detail == "busy"
    assert "<Say>" in outcome.twiml and "<Hangup/>" in outcome.twiml


def test_dial_result_failure_acceptance_case():
    outcome = TransferFlow().dial_result(dial_status="failed")
    assert outcome.state == "failed" and outcome.detail == "failed"
    assert "<Say>" in outcome.twiml and "<Hangup/>" in outcome.twiml


def test_dial_result_completed_records_connected_with_goodbye():
    states: list = []
    flow = TransferFlow(record_state=_records(states))
    outcome = flow.dial_result(dial_status="completed")
    assert outcome.state == "connected"
    assert outcome.detail == "completed"
    root = ET.fromstring(outcome.twiml)
    assert root.find("Say").text == TRANSFER_GOODBYE_SAY
    assert root.find("Hangup") is not None
    assert states == [("connected", {"dial_status": "completed"})]


def test_dial_result_unknown_status_falls_back_conservatively():
    """Unknown DialCallStatus -> treated as failure: recorded + audible
    fallback (the caller is never dropped on an unknown outcome)."""
    states: list = []
    flow = TransferFlow(record_state=_records(states))
    outcome = flow.dial_result(dial_status="weird-status")
    assert outcome.state == "failed"
    assert outcome.detail == "weird-status"
    assert "<Say>" in outcome.twiml and "<Hangup/>" in outcome.twiml
    assert states == [("failed", {"dial_status": "weird-status"})]


def test_dial_result_canceled_caller_disconnect_recorded():
    """'canceled' = caller disconnected during transfer (spec §6.4 test
    list) — recorded; the TwiML is still the safe Say+Hangup."""
    states: list = []
    flow = TransferFlow(record_state=_records(states))
    outcome = flow.dial_result(dial_status="canceled")
    assert outcome.state == "failed"
    assert outcome.detail == "canceled"
    assert states == [("failed", {"dial_status": "canceled"})]


# ===========================================================================
# 4. Handler integration — D-5 default unchanged + write-before-teardown on
#    every exit path (R-P-005)
# ===========================================================================
@pytest.mark.asyncio
async def test_default_path_without_flow_is_unchanged():
    """Regression pin: no transfer_flow -> exactly the pre-U-206 behavior."""
    h = _Harness()
    params = await h.fire_transfer(topic="billing")
    assert params.results == [{"status": "ending", "reason": "transfer_requested"}]
    frames = h.end_frames()
    assert len(frames) == 1
    assert frames[0].reason == "tool:end_call:transfer_requested"
    assert h.trigger_plans == []
    h.assert_order("mark_transfer", "write_outcome", "end_task")


@pytest.mark.asyncio
async def test_write_before_teardown_default_path():
    """transfer_requested/transfer_topic (mark_transfer) -> outcome row
    (write_outcome) -> teardown (EndTaskFrame). Nothing may precede the
    mark, and the row write must complete before the teardown."""
    h = _Harness()
    await h.fire_transfer()
    h.assert_order("mark_transfer", "write_outcome", "end_task")
    assert h.kinds().count("write_outcome") == 1
    assert h.kinds().count("end_task") == 1


@pytest.mark.asyncio
async def test_denied_path_write_before_teardown_and_recorded():
    """Flag on but resolution denies (empty allowlist default): D-5
    record+hangup fallback with the same write-before-teardown ordering,
    the denial recorded, and NO call-update ever issued."""
    h = _Harness()
    h.make_flow(supports_transfer=True)   # default seams -> deny
    await h.fire_transfer(topic="billing")
    h.assert_order("mark_transfer", "write_outcome", "end_task")
    assert h.trigger_plans == []          # denied never dials
    assert [s for s, _ in h.states] == ["requested", "failed"]
    assert h.end_frames() and h.end_frames()[0].reason == "tool:end_call:transfer_requested"


@pytest.mark.asyncio
async def test_approved_path_write_before_call_update_before_teardown():
    """Approved dial: the outcome row is written BEFORE the call-update
    (write-before-teardown) and the EndTaskFrame teardown comes last —
    the chosen call-update/stream-close sequence for <Connect><Stream>
    (spec §6.4)."""
    h = _Harness()
    _ok_allowlist_flow(h)
    await h.fire_transfer(topic="billing")
    h.assert_order("mark_transfer", "write_outcome", "start_transfer", "end_task")
    assert len(h.trigger_plans) == 1
    plan = h.trigger_plans[0]
    assert plan.approved is True
    assert plan.target == TARGET_E164
    assert "<Dial" in plan.twiml
    assert [s for s, _ in h.states] == ["requested", "connecting"]


@pytest.mark.asyncio
async def test_start_transfer_failure_falls_back_to_record_hangup():
    """Call-update trigger returns False -> the D-5 record+hangup teardown
    still completes (caller not dropped) and the failure is recorded."""
    h = _Harness(trigger_ok=False)
    _ok_allowlist_flow(h)
    await h.fire_transfer()
    h.assert_order("mark_transfer", "write_outcome", "start_transfer", "end_task")
    assert len(h.end_frames()) == 1
    assert [s for s, _ in h.states] == ["requested", "connecting", "failed"]
    assert h.states[-1][1]["detail"] == "transfer trigger failed"


@pytest.mark.asyncio
async def test_start_transfer_exception_falls_back_to_record_hangup():
    h = _Harness()
    _ok_allowlist_flow(h)

    async def _boom(plan):
        raise RuntimeError("call-update exploded")

    register_call_control_handlers(
        h.llm, h.task,
        call_id="c-1",
        emit=h._emit,
        scope="phone",
        history_append=h._hist,
        mark_transfer=h._mark,
        write_outcome=h._write,
        transfer_flow=h.flow,
        start_transfer=_boom,
        tenant_id="tenant-a",
    )
    params = _FakeParams(arguments={"topic": "billing"})
    await h.llm.handlers["transfer_to_human"](params)
    await asyncio.sleep(0.05)

    assert len(h.end_frames()) == 1  # teardown still ran — no caller drop
    assert [s for s, _ in h.states] == ["requested", "connecting", "failed"]


@pytest.mark.asyncio
async def test_missing_trigger_fails_closed_to_record_hangup():
    """start_transfer=None (unwired trigger): approved plan still ends via
    the D-5 record+hangup path with the failure recorded."""
    h = _Harness()
    _ok_allowlist_flow(h)
    register_call_control_handlers(
        h.llm, h.task,
        call_id="c-1",
        emit=h._emit,
        scope="phone",
        history_append=h._hist,
        mark_transfer=h._mark,
        write_outcome=h._write,
        transfer_flow=h.flow,
        start_transfer=None,
        tenant_id="tenant-a",
    )
    params = _FakeParams(arguments={"topic": "billing"})
    await h.llm.handlers["transfer_to_human"](params)
    await asyncio.sleep(0.05)

    h.assert_order("mark_transfer", "write_outcome", "end_task")
    assert len(h.end_frames()) == 1
    assert [s for s, _ in h.states] == ["requested", "connecting", "failed"]


@pytest.mark.asyncio
async def test_end_call_never_engages_transfer_flow():
    """end_call must not run the transfer chain or record transfer states."""
    h = _Harness()
    _ok_allowlist_flow(h)
    await h.fire_end_call(reason="task_completed")
    assert h.states == []
    assert h.trigger_plans == []
    assert "mark_transfer" not in h.kinds()
    h.assert_order("write_outcome", "end_task")


@pytest.mark.asyncio
async def test_plan_exception_falls_closed_to_default_path():
    """A flow whose plan() raises must not break the call: the D-5
    record+hangup path runs to completion."""
    h = _Harness()
    flow = _ok_allowlist_flow(h)

    def _explode(**kw):
        raise RuntimeError("plan exploded")

    flow.plan = _explode  # type: ignore[method-assign]
    await h.fire_transfer()
    h.assert_order("mark_transfer", "write_outcome", "end_task")
    assert len(h.end_frames()) == 1
    assert h.trigger_plans == []


@pytest.mark.asyncio
async def test_disabled_flow_in_handler_is_byte_identical_to_default():
    """A wired-but-disabled flow (capability flag off) must not change
    anything: no states recorded, no call-update, same teardown."""
    h = _Harness()
    h.make_flow(supports_transfer=False)
    params = await h.fire_transfer()
    assert params.results == [{"status": "ending", "reason": "transfer_requested"}]
    assert h.states == []
    assert h.trigger_plans == []
    h.assert_order("mark_transfer", "write_outcome", "end_task")


# ===========================================================================
# 5. /twilio/transfer-result endpoint (bot.py) — U-205 wire conventions
# ===========================================================================
class FakeCallRepository(CallRepository):
    """In-memory CallRepository double (tests/test_status_callbacks.py
    pattern): event_id dedup, KeyError on unknown calls, CallFinalizedError
    on finalized calls, idempotent create()."""

    def __init__(self):
        self.calls: dict = {}
        self.events: dict = {}
        self.appended_ids: list = []
        self.fail_persist = False

    async def create(self, record: CallRecord) -> CallRecord:
        entry = self.calls.setdefault(
            record.call_id, {"record": record, "finalized": False}
        )
        return entry["record"]

    async def get(self, call_id: str):
        entry = self.calls.get(call_id)
        return entry["record"] if entry else None

    async def update(self, call_id, changes, *, expected_version):
        raise NotImplementedError

    async def append_events(self, call_id: str, events) -> int:
        if self.fail_persist:
            raise RuntimeError("simulated storage outage")
        entry = self.calls.get(call_id)
        if entry is None:
            raise KeyError(call_id)
        if entry["finalized"]:
            raise CallFinalizedError(call_id)
        appended = 0
        for ev in events:
            if ev.event_id in self.events:
                continue
            self.events[ev.event_id] = ev
            self.appended_ids.append(ev.event_id)
            appended += 1
        return appended

    async def finalize(self, call_id, *, outcome, expected_version):
        self.calls[call_id]["finalized"] = True
        return self.calls[call_id]["record"]

    async def query_history(self, *, caller=None, tenant_id=None, cursor=None, limit=50):
        raise NotImplementedError


@pytest.fixture
def client():
    return TestClient(bot.app)


@pytest.fixture
def repo(monkeypatch):
    fake = FakeCallRepository()
    monkeypatch.setattr(bot, "_status_call_repo", lambda: fake)
    return fake


def _enable_twilio(monkeypatch):
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setenv("TWILIO_PUBLIC_BASE_URL", PUBLIC_BASE)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", ACCOUNT_SID)


def _disable_twilio(monkeypatch):
    monkeypatch.delenv("TWILIO_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TWILIO_PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("TWILIO_ACCOUNT_SID", raising=False)


def _result_params(**overrides) -> dict:
    params = {
        "CallSid": "CAtransfer0001",
        "DialCallSid": "CAagent0001",
        "DialCallStatus": "busy",
        "From": "+15551234567",
        "To": TARGET_E164,
        "Timestamp": "2026-11-20T12:00:00+00:00",
        "SequenceNumber": "7",
    }
    params.update(overrides)
    return params


def _sign(params: dict, *, path: str = RESULT_PATH, query: str = "") -> str:
    full_url = twilio_sig.build_signed_url(PUBLIC_BASE, path, query)
    return twilio_sig.compute_twilio_signature(AUTH_TOKEN, full_url, params)


def _post_result(client, params: dict, *, query: str = ""):
    url = f"{RESULT_PATH}?{query}" if query else RESULT_PATH
    return client.post(
        url, data=params, headers={"X-Twilio-Signature": _sign(params, query=query)}
    )


def test_transfer_result_503_when_not_configured(client, monkeypatch):
    _disable_twilio(monkeypatch)
    resp = client.post(RESULT_PATH, data=_result_params())
    assert resp.status_code == 503


def test_transfer_result_403_when_signature_invalid(client, monkeypatch, repo):
    _enable_twilio(monkeypatch)
    resp = client.post(
        RESULT_PATH,
        data=_result_params(),
        headers={"X-Twilio-Signature": "totally-wrong"},
    )
    assert resp.status_code == 403
    assert repo.appended_ids == []  # never persisted on rejection


def test_transfer_result_403_when_signature_missing(client, monkeypatch, repo):
    _enable_twilio(monkeypatch)
    resp = client.post(RESULT_PATH, data=_result_params())
    assert resp.status_code == 403
    assert repo.appended_ids == []


def test_transfer_result_busy_serves_fallback_and_records(client, monkeypatch, repo):
    """Acceptance case BUSY: caller hears <Say>+<Hangup> (not dropped) and
    the failed outcome is durably recorded."""
    _enable_twilio(monkeypatch)
    resp = _post_result(client, _result_params(DialCallStatus="busy"))
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert root.find("Say").text == TRANSFER_FALLBACK_SAY
    assert root.find("Hangup") is not None
    assert root.find("Dial") is None
    assert len(repo.appended_ids) == 1
    event = next(iter(repo.events.values()))
    assert event.event_type == "transfer-failed"
    assert event.payload["DialCallStatus"] == "busy"


def test_transfer_result_no_answer_serves_fallback_and_records(client, monkeypatch, repo):
    """Acceptance case NO-ANSWER."""
    _enable_twilio(monkeypatch)
    resp = _post_result(client, _result_params(DialCallStatus="no-answer"))
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert root.find("Say").text == TRANSFER_FALLBACK_SAY
    assert root.find("Hangup") is not None
    assert len(repo.appended_ids) == 1
    event = next(iter(repo.events.values()))
    assert event.event_type == "transfer-failed"
    assert event.payload["DialCallStatus"] == "no-answer"


def test_transfer_result_failure_serves_fallback_and_records(client, monkeypatch, repo):
    """Acceptance case FAILURE."""
    _enable_twilio(monkeypatch)
    resp = _post_result(client, _result_params(DialCallStatus="failed"))
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert root.find("Say").text == TRANSFER_FALLBACK_SAY
    assert root.find("Hangup") is not None
    assert len(repo.appended_ids) == 1
    event = next(iter(repo.events.values()))
    assert event.event_type == "transfer-failed"
    assert event.payload["DialCallStatus"] == "failed"


def test_transfer_result_completed_records_connected(client, monkeypatch, repo):
    _enable_twilio(monkeypatch)
    resp = _post_result(client, _result_params(DialCallStatus="completed"))
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert root.find("Say").text == TRANSFER_GOODBYE_SAY
    assert root.find("Hangup") is not None
    assert len(repo.appended_ids) == 1
    event = next(iter(repo.events.values()))
    assert event.event_type == "transfer-connected"
    assert event.call_id == "CAtransfer0001"


def test_transfer_result_duplicate_callbacks_dedup(client, monkeypatch, repo):
    """spec §6.4: duplicate callbacks must not double-record — the
    deterministic event_id collapses redeliveries to a single append."""
    _enable_twilio(monkeypatch)
    params = _result_params(DialCallStatus="busy")
    first = _post_result(client, params)
    second = _post_result(client, params)   # identical redelivery
    assert first.status_code == 200
    assert second.status_code == 200
    assert len(repo.appended_ids) == 1
    assert ET.fromstring(second.text).find("Say") is not None  # still not dropped


def test_transfer_result_persist_before_ack_retries_to_single_append(client, monkeypatch, repo):
    """Persistence failure -> 5xx (Twilio retries, nothing acked); the
    retry after recovery persists exactly once."""
    _enable_twilio(monkeypatch)
    params = _result_params(DialCallStatus="no-answer")
    repo.fail_persist = True
    first = _post_result(client, params)
    assert first.status_code == 500
    assert repo.appended_ids == []

    repo.fail_persist = False
    retry = _post_result(client, params)
    assert retry.status_code == 200
    assert len(repo.appended_ids) == 1


def test_transfer_result_without_callsid_serves_fallback_not_drop(client, monkeypatch, repo):
    """No correlation key -> the outcome cannot be recorded, but the caller
    is still on the line: the fallback TwiML is served (never an empty
    response that would drop the caller)."""
    _enable_twilio(monkeypatch)
    params = _result_params()
    params.pop("CallSid")
    resp = _post_result(client, params)
    assert resp.status_code == 200
    root = ET.fromstring(resp.text)
    assert root.find("Say").text == TRANSFER_FALLBACK_SAY
    assert root.find("Hangup") is not None
    assert repo.appended_ids == []


def test_transfer_result_finalized_call_dropped_but_fallback_served(client, monkeypatch, repo):
    """A late transfer result for a finalized call must not revive it — the
    outcome is dropped (200-ack), but the caller still hears the fallback."""
    _enable_twilio(monkeypatch)
    repo.calls["CAtransfer0001"] = {
        "record": CallRecord(
            call_id="CAtransfer0001", state=CallState.ENDED, started_at=None
        ),
        "finalized": True,
    }
    resp = _post_result(client, _result_params(DialCallStatus="busy"))
    assert resp.status_code == 200
    assert ET.fromstring(resp.text).find("Say") is not None
    assert repo.appended_ids == []


def test_transfer_result_query_string_in_signature(client, monkeypatch, repo):
    """Signed full_url must include the query string (same canonical rule
    as the other /twilio/* webhooks)."""
    _enable_twilio(monkeypatch)
    resp = _post_result(client, _result_params(), query="tenant=acme")
    assert resp.status_code == 200

    params2 = _result_params(SequenceNumber="8")
    sig_no_query = _sign(params2)  # signature that omitted the query
    bad = client.post(
        f"{RESULT_PATH}?tenant=acme",
        data=params2,
        headers={"X-Twilio-Signature": sig_no_query},
    )
    assert bad.status_code == 403
