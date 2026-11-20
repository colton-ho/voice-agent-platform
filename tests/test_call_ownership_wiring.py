"""U-305 / U-306 wiring on the live Twilio media path (bot.py).

Covers the integration seams added to bot.py:
  * owner identity + FC-7 directory gate (MAX_CONCURRENT_CALLS_PER_REPLICA —
    an existing §11 contract key, no new config keys);
  * acquire / renew / release around the media-stream lifecycle, including the
    honest degradation paths (coordination-plane failure → unleased, admission
    refusal → stream refused);
  * §8.1 call state machine transitions (observational) fed by the media
    stream and the status-callback mapping;
  * §6.4 call-update trigger (U-206 deferral) — Twilio CallSid plumbing and
    the <Dial> call-update itself.

All deterministic: no network, no real Cosmos, no sleeps beyond a single
renew-tick with a tiny interval.
"""
import asyncio
import json

import pytest

import bot


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------
class _FakeDirectory:
    """Minimal FC-7 directory double (the Cosmos impl is tested separately)."""

    def __init__(self, *, capacity_error=False, acquire_error=False,
                 renew_error=False, release_error=False):
        self.capacity_error = capacity_error
        self.acquire_error = acquire_error
        self.renew_error = renew_error
        self.release_error = release_error
        self.calls = []

    async def reserve_capacity(self, *, call_id, ttl_seconds):
        self.calls.append(("reserve", call_id, ttl_seconds))
        if self.acquire_error:
            raise RuntimeError("cosmos unavailable")
        if self.capacity_error:
            from storage.base import CapacityExceededError
            raise CapacityExceededError("at capacity")

    async def bind_owner(self, *, call_id, owner_id):
        self.calls.append(("bind", call_id, owner_id))
        if self.acquire_error:
            raise RuntimeError("cosmos unavailable")

    async def renew_lease(self, *, call_id, owner_id):
        self.calls.append(("renew", call_id, owner_id))
        if self.renew_error:
            raise RuntimeError("lease lost")

    async def release(self, *, call_id, owner_id):
        self.calls.append(("release", call_id, owner_id))
        if self.release_error:
            raise RuntimeError("release failed")


class _FakeTwilioWS:
    def __init__(self, frames=()):
        self._frames = list(frames)
        self.accepted = False
        self.closed_code = None
        self.sent = []

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        if not self._frames:
            raise RuntimeError("client disconnected")
        return self._frames.pop(0)

    async def close(self, code=1000):
        self.closed_code = code

    async def send_text(self, data):
        self.sent.append(data)


_START_FRAME = json.dumps({
    "event": "start",
    "streamSid": "MZ123",
    "callSid": "CA456",
    "start": {
        "streamSid": "MZ123",
        "callSid": "CA456",
        "customParameters": {"caller": "+15550001111"},
    },
})


@pytest.fixture(autouse=True)
def _reset_wiring_state(monkeypatch):
    """Fresh module-level wiring state + Twilio enabled for every test."""
    monkeypatch.setattr(bot, "_OWNERSHIP_TRIED", False, raising=False)
    monkeypatch.setattr(bot, "_OWNERSHIP_DIR", None, raising=False)
    bot._CALL_MACHINES.clear()
    bot._TWILIO_CALL_SIDS.clear()
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "tok")
    monkeypatch.setenv("TWILIO_PUBLIC_BASE_URL", "https://twilio.example.com")
    monkeypatch.delenv("MAX_CONCURRENT_CALLS_PER_REPLICA", raising=False)
    yield
    bot._CALL_MACHINES.clear()
    bot._TWILIO_CALL_SIDS.clear()


# ---------------------------------------------------------------------------
# owner identity + directory gate
# ---------------------------------------------------------------------------
def test_owner_id_prefers_replica_name(monkeypatch):
    monkeypatch.setenv("CONTAINER_APP_REPLICA_NAME", "runtime--green-abc")
    monkeypatch.setenv("HOSTNAME", "host-1")
    assert bot._runtime_owner_id() == "runtime--green-abc"


def test_owner_id_falls_back_to_hostname(monkeypatch):
    monkeypatch.delenv("CONTAINER_APP_REPLICA_NAME", raising=False)
    monkeypatch.setenv("HOSTNAME", "host-1")
    assert bot._runtime_owner_id() == "host-1"


def test_ownership_disabled_without_gate(monkeypatch):
    assert bot._ownership_directory() is None


def test_ownership_disabled_on_invalid_gate(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_CALLS_PER_REPLICA", "many")
    assert bot._ownership_directory() is None


def test_ownership_disabled_on_nonpositive_gate(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_CALLS_PER_REPLICA", "0")
    assert bot._ownership_directory() is None


def test_ownership_enabled_with_gate_and_cached(monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_CALLS_PER_REPLICA", "7")
    first = bot._ownership_directory()
    assert first is not None
    # Cached: flipping the gate afterwards does not rebuild the directory.
    monkeypatch.delenv("MAX_CONCURRENT_CALLS_PER_REPLICA")
    assert bot._ownership_directory() is first


# ---------------------------------------------------------------------------
# acquire / renew / release
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_acquire_degrades_when_disabled(monkeypatch):
    # No gate → no directory → permissive (single-replica validation config).
    assert await bot._ownership_acquire("twilio-MZ1") is True


@pytest.mark.asyncio
async def test_acquire_reserves_then_binds(monkeypatch):
    directory = _FakeDirectory()
    monkeypatch.setattr(bot, "_ownership_directory", lambda: directory)
    assert await bot._ownership_acquire("twilio-MZ1") is True
    assert [c[0] for c in directory.calls] == ["reserve", "bind"]
    assert directory.calls[0][1] == "twilio-MZ1"
    assert directory.calls[0][2] == bot._CALL_RESERVE_TTL_SECONDS
    assert directory.calls[1][2] == bot._runtime_owner_id()


@pytest.mark.asyncio
async def test_acquire_refuses_at_capacity(monkeypatch):
    directory = _FakeDirectory(capacity_error=True)
    monkeypatch.setattr(bot, "_ownership_directory", lambda: directory)
    assert await bot._ownership_acquire("twilio-MZ1") is False
    assert [c[0] for c in directory.calls] == ["reserve"]  # never binds


@pytest.mark.asyncio
async def test_acquire_degrades_on_coordination_failure(monkeypatch):
    # Cosmos down must not turn into a total call outage (no HA claim made).
    directory = _FakeDirectory(acquire_error=True)
    monkeypatch.setattr(bot, "_ownership_directory", lambda: directory)
    assert await bot._ownership_acquire("twilio-MZ1") is True


@pytest.mark.asyncio
async def test_renew_loop_renews_until_cancelled(monkeypatch):
    directory = _FakeDirectory()
    monkeypatch.setattr(bot, "_ownership_directory", lambda: directory)
    monkeypatch.setattr(bot, "_OWNERSHIP_RENEW_SECONDS", 0.01)
    task = asyncio.create_task(bot._ownership_renew_loop("twilio-MZ1"))
    for _ in range(50):
        if len(directory.calls) >= 1:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert directory.calls[0][0] == "renew"
    assert directory.calls[0][2] == bot._runtime_owner_id()


@pytest.mark.asyncio
async def test_renew_loop_survives_failures(monkeypatch):
    directory = _FakeDirectory(renew_error=True)
    monkeypatch.setattr(bot, "_ownership_directory", lambda: directory)
    monkeypatch.setattr(bot, "_OWNERSHIP_RENEW_SECONDS", 0.01)
    task = asyncio.create_task(bot._ownership_renew_loop("twilio-MZ1"))
    for _ in range(50):
        if len(directory.calls) >= 2:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(directory.calls) >= 2  # kept ticking after a failed renewal


@pytest.mark.asyncio
async def test_release_calls_directory_and_swallows_errors(monkeypatch):
    directory = _FakeDirectory(release_error=True)
    monkeypatch.setattr(bot, "_ownership_directory", lambda: directory)
    await bot._ownership_release("twilio-MZ1")  # must not raise
    assert directory.calls == [("release", "twilio-MZ1", bot._runtime_owner_id())]


# ---------------------------------------------------------------------------
# §8.1 machine (observational)
# ---------------------------------------------------------------------------
def test_machine_key_prefers_call_sid():
    assert bot._call_machine_key("twilio-MZ1", "CA456") == "CA456"
    assert bot._call_machine_key("twilio-MZ1", None) == "twilio-MZ1"
    assert bot._call_machine_key("twilio-MZ1", "") == "twilio-MZ1"


def test_machine_apply_walks_connect_to_end():
    bot._machine_apply("CA456", "connecting")
    bot._machine_apply("CA456", "active")
    bot._machine_apply("CA456", "ended")
    machine = bot._CALL_MACHINES["CA456"]
    assert machine.state is bot.CallState.ENDED
    assert machine.finalized is True


def test_machine_apply_swallows_invalid_events():
    bot._machine_apply("CA456", "connecting")
    # Unknown event type must never raise into the call path.
    bot._machine_apply("CA456", "no-such-event")
    assert bot._CALL_MACHINES["CA456"].state is bot.CallState.CONNECTING


def test_machine_apply_status_mapping():
    bot._machine_apply_status("CA456", "in-progress")
    assert bot._CALL_MACHINES["CA456"].state is bot.CallState.ACTIVE
    bot._machine_apply_status("CA456", "completed")
    assert bot._CALL_MACHINES["CA456"].state is bot.CallState.ENDED


def test_machine_apply_status_failed_carries_reason_and_cleanup():
    bot._machine_apply_status("CA456", "busy")
    machine = bot._CALL_MACHINES["CA456"]
    assert machine.state is bot.CallState.FAILED
    assert machine.failure["reason"] == "twilio:busy"
    assert machine.failure["cleanup"]


def test_machine_apply_status_unknown_status_is_noop():
    bot._machine_apply_status("CA456", "some-new-status")
    assert "CA456" not in bot._CALL_MACHINES


# ---------------------------------------------------------------------------
# media-stream lifecycle wiring
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_media_stream_refused_at_capacity(monkeypatch):
    async def _refuse(*a, **k):
        return False

    ran = {}
    async def _kernel(*a, **k):
        ran["yes"] = True

    monkeypatch.setattr(bot, "_ownership_acquire", _refuse)
    monkeypatch.setattr(bot, "_run_phone_session", _kernel)

    ws = _FakeTwilioWS([_START_FRAME])
    await bot.twilio_media_stream(ws)

    assert ws.closed_code == 1013
    assert "yes" not in ran
    assert bot._TWILIO_CALL_SIDS == {}  # CallSid map cleaned on refusal


@pytest.mark.asyncio
async def test_media_stream_wires_ownership_machine_and_release(monkeypatch):
    events = []

    async def _acquire(call_id):
        events.append(("acquire", call_id))
        return True

    async def _release(call_id):
        events.append(("release", call_id))

    async def _renew_loop(call_id):  # avoid a live 10s task in the test
        events.append(("renew-start", call_id))

    async def _kernel(websocket, *, call_id, caller, serializer=None):
        # Inside the call: lelease held, CallSid bound, machine ACTIVE.
        events.append(("kernel", call_id))
        assert bot._TWILIO_CALL_SIDS[call_id] == "CA456"
        assert bot._CALL_MACHINES["CA456"].state is bot.CallState.ACTIVE

    monkeypatch.setattr(bot, "_ownership_acquire", _acquire)
    monkeypatch.setattr(bot, "_ownership_release", _release)
    monkeypatch.setattr(bot, "_ownership_renew_loop", _renew_loop)
    monkeypatch.setattr(bot, "_run_phone_session", _kernel)
    monkeypatch.setattr(bot, "session_register", _noop_register)

    ws = _FakeTwilioWS([_START_FRAME])
    await bot.twilio_media_stream(ws)

    assert events[0] == ("acquire", "twilio-MZ123")
    assert ("kernel", "twilio-MZ123") in events
    assert events[-1] == ("release", "twilio-MZ123")
    # Teardown cleanup: no leaked CallSid or machine.
    assert bot._TWILIO_CALL_SIDS == {}
    assert bot._CALL_MACHINES == {}


async def _noop_register(call_id, *, caller=None, primary_emit):
    return {"call_id": call_id}


# ---------------------------------------------------------------------------
# §6.4 call-update trigger (U-206 deferral)
# ---------------------------------------------------------------------------
class _FakeCalls:
    def __init__(self, sink):
        self._sink = sink

    def __call__(self, call_sid):
        self._sink.append(("calls", call_sid))
        return self

    def update(self, *, twiml):
        self._sink.append(("update", twiml))


class _FakeClient:
    def __init__(self, sink):
        self.calls = _FakeCalls(sink)


@pytest.mark.asyncio
async def test_start_transfer_without_call_sid_fails_closed(monkeypatch):
    class _Plan:
        target = "+15550002222"

    ok = await bot._phone_start_transfer("twilio-MZ123")(_Plan())
    assert ok is False  # no CallSid bound → D-5 fallback, caller never dropped


@pytest.mark.asyncio
async def test_start_transfer_without_credentials_fails_closed(monkeypatch):
    bot._TWILIO_CALL_SIDS["twilio-MZ123"] = "CA456"
    monkeypatch.setattr(bot, "_twilio_call_update_client", lambda: None)

    class _Plan:
        target = "+15550002222"

    assert await bot._phone_start_transfer("twilio-MZ123")(_Plan()) is False


@pytest.mark.asyncio
async def test_start_transfer_issues_dial_call_update(monkeypatch):
    bot._TWILIO_CALL_SIDS["twilio-MZ123"] = "CA456"
    sink = []
    monkeypatch.setattr(bot, "_twilio_call_update_client", lambda: _FakeClient(sink))

    class _Plan:
        target = "+15550002222"

    assert await bot._phone_start_transfer("twilio-MZ123")(_Plan()) is True
    assert sink[0] == ("calls", "CA456")
    assert sink[1][0] == "update"
    twiml = sink[1][1]
    assert "<Dial" in twiml and "+15550002222" in twiml
    assert "/twilio/transfer-result" in twiml  # action_url → outcome endpoint


@pytest.mark.asyncio
async def test_start_transfer_update_error_fails_closed(monkeypatch):
    bot._TWILIO_CALL_SIDS["twilio-MZ123"] = "CA456"

    class _Boom:
        def calls(self, sid):
            raise RuntimeError("twilio down")

    monkeypatch.setattr(bot, "_twilio_call_update_client", lambda: _Boom())

    class _Plan:
        target = "+15550002222"

    assert await bot._phone_start_transfer("twilio-MZ123")(_Plan()) is False
