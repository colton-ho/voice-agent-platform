"""U-205 tests: Twilio status callbacks (spec §6.1.8) + fallback TwiML (§6.1.9).

Hermetic, no network. Conventions mirror tests/test_twilio_endpoints.py (same
env seam ``bot._twilio_config`` read fresh per request, real signatures computed
with the stdlib oracle ``twilio_sig``) and the FakeQueue pattern from
tests/fake_servicebus.py for the ``CallRepository`` double.

Covered (pinned contracts from product-impact §2.3 stay unbroken — the existing
tests/test_twilio_* suites run unchanged alongside this file):

  * POST /twilio/status-callback — 503 when unconfigured, 403 on missing /
    invalid / wrong-host X-Twilio-Signature (FC-8 seam via
    providers/telephony/twilio_adapter — validated against the Twilio-visible
    external URL, decision A-1), query-string signing parity.
  * Persist-before-ack + retry dedup (§6.1.8): valid signature → event
    persisted then 200; duplicate delivery (same event_id) → single append;
    persistence failure → 5xx (Twilio retries); retry after transient failure →
    persisted then 200; event after finalize → 200-ack + dropped (the call is
    NOT revived); missing CallSid → 200-ack + dropped (a retry can never help).
  * event_id derivation: deterministic from (CallSid, CallStatus,
    SequenceNumber|Timestamp) so Twilio redeliveries dedup to one append.
  * Unknown-CallSid policy (U-205 decision — PERSIST AS ORPHAN, not drop):
    the event gets a minimal RECEIVED-state call record keyed by CallSid with
    provider_ids.twilio_call_sid (rationale in
    ``bot._persist_twilio_status_event``); retries still dedup to one append.
  * Fallback TwiML (§6.1.9): served from the voice webhook when stream setup
    fails (missing stream config / reservation failure); XML shape asserted
    (<Response><Say/><Hangup/></Response>, no <Connect>/<Stream>); the
    happy-path TwiML stays stream-only (no fallback leaks into it).

Two independent seams are exercised: the FC-8 signature seam (computed
signatures, plus an injected validator to prove the seam receives the external
URL) and ``bot._status_call_repo`` (the FakeCallRepository double).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

import bot
import twilio_sig
from providers.telephony import twilio_adapter
from storage.base import (
    CallEvent,
    CallFinalizedError,
    CallRecord,
    CallRepository,
    CallState,
)

PUBLIC_BASE = "https://twilio.example.com"
INTERNAL_HOST = "d28jyp0rnkqvcx.cloudfront.net"  # the host Twilio must NOT see
AUTH_TOKEN = "test-auth-token-123"
ACCOUNT_SID = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

STATUS_PATH = "/twilio/status-callback"


# ===========================================================================
# Doubles + helpers
# ===========================================================================
class FakeCallRepository(CallRepository):
    """In-memory CallRepository double (FakeQueue pattern — tests/fake_servicebus.py).

    Models exactly the seams U-205 relies on, matching CosmosCallRepository's
    observable semantics: event_id dedup in append_events (R-A-007),
    CallFinalizedError on finalized calls, KeyError on unknown calls, and
    create() idempotent on conflict. ``fail_persist`` injects a transient
    storage outage for the persist-before-ack 5xx leg.
    """

    def __init__(self):
        self.calls: dict[str, dict] = {}          # call_id -> {"record", "finalized"}
        self.events: dict[str, CallEvent] = {}    # event_id -> CallEvent (single append per id)
        self.appended_ids: list[str] = []         # append order, one entry per NEW event
        self.created: list[str] = []              # every create() call, in order
        self.fail_persist = False                 # transient failure switch

    def seed_call(self, call_id: str, *, state=CallState.ACTIVE, finalized: bool = False):
        self.calls[call_id] = {
            "record": CallRecord(
                call_id=call_id, state=state, started_at=datetime.now(tz=timezone.utc)
            ),
            "finalized": finalized,
        }

    async def create(self, record: CallRecord) -> CallRecord:
        self.created.append(record.call_id)
        # Cosmos create_item conflict → existing doc returned unchanged.
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
                continue  # event_id dedup (R-A-007)
            self.events[ev.event_id] = ev
            self.appended_ids.append(ev.event_id)
            appended += 1
        return appended

    async def finalize(self, call_id: str, *, outcome, expected_version):
        entry = self.calls[call_id]
        entry["finalized"] = True
        return entry["record"]

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


def _sign(params: dict, *, path: str = STATUS_PATH, query: str = "") -> str:
    full_url = twilio_sig.build_signed_url(PUBLIC_BASE, path, query)
    return twilio_sig.compute_twilio_signature(AUTH_TOKEN, full_url, params)


def _status_params(**overrides) -> dict:
    params = {
        "CallSid": "CAstatus0001",
        "CallStatus": "completed",
        "AccountSid": ACCOUNT_SID,
        "From": "+15551234567",
        "To": "+18005551212",
        "Timestamp": "2026-11-20T12:00:00+00:00",
        "SequenceNumber": "4",
    }
    params.update(overrides)
    return params


def _post_status(client, params: dict, *, query: str = "", path: str = STATUS_PATH):
    url = f"{path}?{query}" if query else path
    return client.post(url, data=params, headers={"X-Twilio-Signature": _sign(params, path=path, query=query)})


# ===========================================================================
# 503 / 403 contract (mirrors /twilio/incoming-call conventions)
# ===========================================================================
def test_status_callback_503_when_not_configured(client, monkeypatch):
    _disable_twilio(monkeypatch)
    resp = client.post(STATUS_PATH, data=_status_params())
    assert resp.status_code == 503


def test_status_callback_403_when_signature_missing(client, monkeypatch, repo):
    _enable_twilio(monkeypatch)
    resp = client.post(STATUS_PATH, data=_status_params())
    assert resp.status_code == 403
    assert repo.appended_ids == []  # never persisted on rejection


def test_status_callback_403_when_signature_invalid(client, monkeypatch, repo):
    _enable_twilio(monkeypatch)
    resp = client.post(
        STATUS_PATH,
        data=_status_params(),
        headers={"X-Twilio-Signature": "totally-wrong-signature"},
    )
    assert resp.status_code == 403
    assert repo.appended_ids == []


def test_status_callback_403_when_signature_for_wrong_host(client, monkeypatch, repo):
    """Canonical pitfall: a signature computed against the internal CloudFront
    host must NOT validate — signing is against TWILIO_PUBLIC_BASE_URL."""
    _enable_twilio(monkeypatch)
    params = _status_params()
    wrong_url = f"https://{INTERNAL_HOST}{STATUS_PATH}"
    bad_sig = twilio_sig.compute_twilio_signature(AUTH_TOKEN, wrong_url, params)
    resp = client.post(STATUS_PATH, data=params, headers={"X-Twilio-Signature": bad_sig})
    assert resp.status_code == 403
    assert repo.appended_ids == []


def test_status_callback_signature_includes_query_string(client, monkeypatch, repo):
    """When Twilio appends a query string, the signed full_url must include it
    (same canonical rule as /twilio/incoming-call)."""
    _enable_twilio(monkeypatch)
    repo.seed_call("CAstatus0001")
    params = _status_params()
    resp = _post_status(client, params, query="tenant=acme")
    assert resp.status_code == 200

    params2 = _status_params(CallStatus="in-progress", SequenceNumber="2")
    sig_no_query = _sign(params2)  # signature that omitted the query
    resp_bad = client.post(
        f"{STATUS_PATH}?tenant=acme",
        data=params2,
        headers={"X-Twilio-Signature": sig_no_query},
    )
    assert resp_bad.status_code == 403


# ===========================================================================
# FC-8 signature seam (providers/telephony/twilio_adapter, decision A-1)
# ===========================================================================
def test_status_callback_uses_fc8_adapter_seam_with_external_url(client, monkeypatch, repo):
    """The route must validate through the adapter seam (SDK-with-stdlib
    fallback) using the Twilio-visible external URL — never the inbound host."""
    _enable_twilio(monkeypatch)
    repo.seed_call("CAstatus0001")
    captured = {}

    def _fake_validate(config, signature, url, params):
        captured["config"] = config
        captured["signature"] = signature
        captured["url"] = url
        captured["params"] = dict(params)
        return True

    monkeypatch.setattr(twilio_adapter, "validate_incoming_call", _fake_validate)
    params = _status_params()
    resp = client.post(
        STATUS_PATH, data=params, headers={"X-Twilio-Signature": "any-sig"}
    )
    assert resp.status_code == 200
    assert captured["url"] == f"{PUBLIC_BASE}{STATUS_PATH}"  # external URL
    assert captured["config"].auth_token == AUTH_TOKEN
    assert captured["config"].public_base_url == PUBLIC_BASE
    assert captured["signature"] == "any-sig"
    assert captured["params"]["CallStatus"] == "completed"


# ===========================================================================
# Persist-before-ack + retry dedup (spec §6.1.8)
# ===========================================================================
def test_valid_signature_persists_event_then_200(client, monkeypatch, repo):
    _enable_twilio(monkeypatch)
    repo.seed_call("CAstatus0001")
    params = _status_params()
    resp = _post_status(client, params)
    assert resp.status_code == 200
    assert repo.appended_ids == ["CAstatus0001:completed:4"]

    ev = repo.events["CAstatus0001:completed:4"]
    assert isinstance(ev, CallEvent)
    assert ev.call_id == "CAstatus0001"
    assert ev.event_type == "completed"
    assert ev.occurred_at == datetime.fromisoformat("2026-11-20T12:00:00+00:00")
    # payload is the sanitized whitelist subset (§9.1 — no audio/credentials)
    assert set(ev.payload) <= set(bot._STATUS_EVENT_KEYS)
    assert ev.payload["CallStatus"] == "completed"
    assert ev.payload["From"] == "+15551234567"
    assert ev.payload["SequenceNumber"] == "4"
    assert "AccountSid" not in ev.payload


def test_event_id_derivation_formula():
    """event_id = CallSid:CallStatus:(SequenceNumber|Timestamp|'0') — stable
    across Twilio retry redeliveries so they dedup to a single append."""
    sid, st = "CAabc", "completed"
    # SequenceNumber preferred over Timestamp
    assert (
        bot._twilio_status_event_id(sid, st, {"SequenceNumber": "4", "Timestamp": "T1"})
        == "CAabc:completed:4"
    )
    # Timestamp fallback when SequenceNumber is absent
    assert (
        bot._twilio_status_event_id(sid, st, {"Timestamp": "2026-11-20T12:00:00+00:00"})
        == "CAabc:completed:2026-11-20T12:00:00+00:00"
    )
    # Neither → "0" (same-status duplicates without a sequence are retries)
    assert bot._twilio_status_event_id(sid, st, {}) == "CAabc:completed:0"
    # Missing CallStatus → "unknown"; fully deterministic
    assert bot._twilio_status_event_id(sid, "", {}) == "CAabc:unknown:0"
    assert bot._twilio_status_event_id(sid, st, {}) == bot._twilio_status_event_id(sid, st, {})


def test_duplicate_delivery_dedups_to_single_append(client, monkeypatch, repo):
    _enable_twilio(monkeypatch)
    repo.seed_call("CAstatus0001")
    params = _status_params()
    resp1 = _post_status(client, params)
    resp2 = _post_status(client, params)  # Twilio retry redelivery — same body
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert repo.appended_ids == ["CAstatus0001:completed:4"]  # single append
    assert len(repo.events) == 1


def test_persistence_failure_returns_5xx_and_never_acks(client, monkeypatch, repo):
    """Persist-before-ack: an unpersisted event is never acked — 5xx so Twilio
    retries (and the deterministic event_id keeps the retry a single append)."""
    _enable_twilio(monkeypatch)
    repo.seed_call("CAstatus0001")
    repo.fail_persist = True
    resp = _post_status(client, _status_params())
    assert resp.status_code >= 500
    assert repo.appended_ids == []  # nothing acknowledged as persisted


def test_retry_after_transient_failure_persists_then_200(client, monkeypatch, repo):
    _enable_twilio(monkeypatch)
    repo.seed_call("CAstatus0001")
    params = _status_params()
    repo.fail_persist = True
    resp1 = _post_status(client, params)
    assert resp1.status_code >= 500

    repo.fail_persist = False  # storage recovers; Twilio redelivers
    resp2 = _post_status(client, params)
    assert resp2.status_code == 200
    assert repo.appended_ids == ["CAstatus0001:completed:4"]  # exactly one


def test_event_after_finalize_acked_200_and_not_revived(client, monkeypatch, repo):
    """Late events after finalization are acked 200 and dropped (§6.1.8 +
    CallFinalizedError semantics) — the finalized call must not be revived."""
    _enable_twilio(monkeypatch)
    repo.seed_call("CAstatus0001", state=CallState.ENDED, finalized=True)
    resp = _post_status(client, _status_params(CallStatus="completed", SequenceNumber="9"))
    assert resp.status_code == 200
    assert repo.appended_ids == []          # dropped, not appended
    assert repo.created == []               # no record creation attempted
    assert repo.calls["CAstatus0001"]["finalized"] is True  # not revived
    assert repo.calls["CAstatus0001"]["record"].state == CallState.ENDED


def test_unknown_callsid_persists_orphan_event(client, monkeypatch, repo):
    """U-205 decision — ORPHAN PERSIST (documented in
    ``bot._persist_twilio_status_event``): a status event for a CallSid with no
    call record gets a minimal RECEIVED-state call record (spec §8.1 initial
    state) keyed by CallSid with provider_ids.twilio_call_sid, then the event
    is appended. §6.1.8 durability wins over dropping — busy/no-answer/failed
    calls never open a stream and would otherwise lose every event. Retries
    still dedup to a single append."""
    _enable_twilio(monkeypatch)
    params = _status_params(CallSid="CAorphan9", CallStatus="no-answer", SequenceNumber="1")
    resp = _post_status(client, params)
    assert resp.status_code == 200

    assert repo.created == ["CAorphan9"]
    record = repo.calls["CAorphan9"]["record"]
    assert record.state == CallState.RECEIVED
    assert record.provider_ids == {"twilio_call_sid": "CAorphan9"}
    assert repo.appended_ids == ["CAorphan9:no-answer:1"]

    # Twilio retry redelivery of the orphan event → still a single append
    resp2 = _post_status(client, params)
    assert resp2.status_code == 200
    assert repo.appended_ids == ["CAorphan9:no-answer:1"]
    # create() is idempotent on conflict — the record is not clobbered
    assert repo.calls["CAorphan9"]["record"] is record


def test_missing_callsid_acked_and_dropped_without_write(client, monkeypatch, repo):
    """Malformed payload without CallSid: permanently unpersistable, so ack 200
    and drop (a 5xx would make Twilio retry a payload that can never succeed)."""
    _enable_twilio(monkeypatch)
    params = _status_params()
    del params["CallSid"]
    resp = _post_status(client, params)
    assert resp.status_code == 200
    assert repo.created == []
    assert repo.appended_ids == []


# ===========================================================================
# Fallback TwiML (spec §6.1.9) — served from the voice webhook
# ===========================================================================
def test_fallback_twiml_when_reservation_fails(client, monkeypatch):
    """Stream setup fails (reservation failure) → the voice webhook answers
    with the approved fallback TwiML instead of <Connect><Stream>."""
    _enable_twilio(monkeypatch)
    captured = {}

    def _fake_reserve(call_sid, account_sid):
        captured["call_sid"] = call_sid
        captured["account_sid"] = account_sid
        return False  # reservation refused → stream cannot start

    monkeypatch.setattr(bot, "_twilio_reserve_stream", _fake_reserve)
    params = {"From": "+15551234567", "CallSid": "CAvoice1", "AccountSid": ACCOUNT_SID}
    full_url = twilio_sig.build_signed_url(PUBLIC_BASE, "/twilio/incoming-call", "")
    good_sig = twilio_sig.compute_twilio_signature(AUTH_TOKEN, full_url, params)
    resp = client.post(
        "/twilio/incoming-call", data=params, headers={"X-Twilio-Signature": good_sig}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/xml")
    assert captured == {"call_sid": "CAvoice1", "account_sid": ACCOUNT_SID}

    root = ET.fromstring(resp.text)
    assert root.tag == "Response"
    assert [child.tag for child in root] == ["Say", "Hangup"]
    assert (root.find("Say").text or "").strip()  # approved copy, non-empty
    assert root.find("Stream") is None and root.find("Connect") is None


def test_fallback_twiml_when_stream_config_missing():
    """Missing stream config (no Twilio-visible host → no wss:// URL) means the
    media stream cannot start → fallback. (The 503 pinned gate normally catches
    this earlier; this guard holds if that gate ever relaxes.)"""
    assert bot._twilio_stream_ready({}, {"public_base": ""}) is False
    assert bot._twilio_stream_ready({}, {"public_base": "https://x.example.com", "account_sid": ACCOUNT_SID}) is True


def test_fallback_twiml_shape():
    """The approved fallback TwiML is <Response><Say/></Say><Hangup/></Response>
    (style reference: build_twiml_connect_stream's Say fallback). A transfer
    target (<Dial>) is deliberately absent — config_contract.py has no
    fallback number key and U-205 adds no config keys (documented gap)."""
    xml_text = bot._twilio_fallback_twiml()
    assert xml_text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    root = ET.fromstring(xml_text)  # well-formed XML
    assert root.tag == "Response"
    assert [child.tag for child in root] == ["Say", "Hangup"]
    say = root.find("Say")
    assert (say.text or "") == bot._TWILIO_FALLBACK_SAY
    assert root.find("Dial") is None
    assert root.find("Connect") is None and root.find("Stream") is None


def test_happy_path_twiml_stays_stream_only(client, monkeypatch):
    """Regression guard on the pinned 200 TwiML: with stream setup OK the body
    is exactly the Connect/Stream shape — no fallback leaks into it."""
    _enable_twilio(monkeypatch)
    params = {"From": "+15557654321", "CallSid": "CA456", "AccountSid": ACCOUNT_SID}
    full_url = twilio_sig.build_signed_url(PUBLIC_BASE, "/twilio/incoming-call", "")
    good_sig = twilio_sig.compute_twilio_signature(AUTH_TOKEN, full_url, params)
    resp = client.post(
        "/twilio/incoming-call", data=params, headers={"X-Twilio-Signature": good_sig}
    )
    assert resp.status_code == 200
    body = resp.text
    assert "<Connect>" in body and "<Stream" in body
    assert "wss://twilio.example.com/twilio/media-stream" in body
    assert "<Say>" not in body and "<Hangup" not in body
