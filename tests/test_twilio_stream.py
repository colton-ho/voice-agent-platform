"""U-202 tests — Twilio stream reservation/correlation + frame handling.

Hermetic, stdlib-only. Covers the task AC (§6.1.5–6.1.7):

  * Reservation lifecycle: reserve -> accept -> close (state transitions + the
    stream_sid binding at accept).
  * Rejection at accept: unknown call_sid, expired reservation, account_sid
    mismatch, double-accept (stream_sid already taken). Expiry is driven by an
    injected clock — no sleeps, fully deterministic.
  * Frame parsing: valid media frame -> base64-decoded μ-law bytes; missing
    payload -> None; invalid base64 -> None.
  * Mark/clear event shapes match Twilio's documented format.

Style mirrors tests/test_twilio_adapter.py (module-level functions).
"""
from __future__ import annotations

import base64

from providers.telephony.twilio_stream import (
    StreamSessionDirectory,
    StreamState,
    build_clear_event,
    build_mark_event,
    parse_media_frame,
)

CALL = "CA1234567890ABCDE"
ACCOUNT = "ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
STREAM = "MZaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _FakeClock:
    """Deterministic, advanceable clock for TTL-expiry tests."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


# ===========================================================================
# Reservation lifecycle: reserve -> accept -> close
# ===========================================================================
def test_reserve_returns_reserved_state_without_stream():
    d = StreamSessionDirectory()
    r = d.reserve(CALL, ACCOUNT)
    assert r.call_sid == CALL
    assert r.account_sid == ACCOUNT
    assert r.stream_sid is None
    assert r.state == StreamState.RESERVED
    assert d.get(CALL) is r


def test_accept_binds_stream_and_activates():
    d = StreamSessionDirectory()
    d.reserve(CALL, ACCOUNT)
    r = d.accept(STREAM, CALL, ACCOUNT)
    assert r is not None
    assert r.stream_sid == STREAM
    assert r.state == StreamState.ACTIVE
    # get reflects the active snapshot
    got = d.get(CALL)
    assert got.stream_sid == STREAM and got.state == StreamState.ACTIVE


def test_close_marks_closed_but_record_remains():
    d = StreamSessionDirectory()
    d.reserve(CALL, ACCOUNT)
    d.accept(STREAM, CALL, ACCOUNT)
    d.close(CALL)
    got = d.get(CALL)
    assert got is not None
    assert got.state == StreamState.CLOSED


def test_get_unknown_returns_none():
    d = StreamSessionDirectory()
    assert d.get("CA-does-not-exist") is None


# ===========================================================================
# Rejection cases at accept (§6.1.5)
# ===========================================================================
def test_accept_unknown_call_sid_rejected():
    d = StreamSessionDirectory()
    assert d.accept(STREAM, "CA-unknown", ACCOUNT) is None


def test_accept_expired_reservation_rejected():
    clock = _FakeClock(start=1_000.0)
    d = StreamSessionDirectory(ttl_seconds=300.0, clock=clock)
    d.reserve(CALL, ACCOUNT)  # expires at 1300.0
    clock.now = 1_301.0  # advance past expiry
    assert d.accept(STREAM, CALL, ACCOUNT) is None


def test_accept_before_expiry_succeeds():
    clock = _FakeClock(start=1_000.0)
    d = StreamSessionDirectory(ttl_seconds=300.0, clock=clock)
    d.reserve(CALL, ACCOUNT)
    clock.now = 1_299.0  # still within TTL
    assert d.accept(STREAM, CALL, ACCOUNT) is not None


def test_accept_account_sid_mismatch_rejected():
    d = StreamSessionDirectory()
    d.reserve(CALL, ACCOUNT)
    assert d.accept(STREAM, CALL, "AC-different-account") is None


def test_accept_double_accept_rejected():
    d = StreamSessionDirectory()
    d.reserve(CALL, ACCOUNT)
    first = d.accept(STREAM, CALL, ACCOUNT)
    assert first is not None
    # Second accept of the same stream (same call) must be refused.
    assert d.accept(STREAM, CALL, ACCOUNT) is None
    # A different stream on the already-active reservation is also refused.
    assert d.accept("MZbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", CALL, ACCOUNT) is None


def test_accept_stream_sid_already_taken_by_other_call():
    d = StreamSessionDirectory()
    d.reserve(CALL, ACCOUNT)
    other = "CA-other-call"
    d.reserve(other, ACCOUNT)
    assert d.accept(STREAM, CALL, ACCOUNT) is not None
    # Another call must not steal the already-bound stream_sid.
    assert d.accept(STREAM, other, ACCOUNT) is None


def test_accept_closed_session_rejected():
    d = StreamSessionDirectory()
    d.reserve(CALL, ACCOUNT)
    d.close(CALL)
    assert d.accept(STREAM, CALL, ACCOUNT) is None


# ===========================================================================
# Frame parsing (§6.1.7)
# ===========================================================================
def test_parse_media_frame_valid_returns_mulaw_bytes():
    mulaw = b"\x00\x7f\x80\xff\x55"
    event = {
        "event": "media",
        "streamSid": STREAM,
        "media": {"track": "inbound", "chunk": "1", "timestamp": "10",
                  "payload": base64.b64encode(mulaw).decode()},
    }
    assert parse_media_frame(event) == mulaw


def test_parse_media_frame_accepts_inner_media_object():
    mulaw = b"\x11\x22\x33"
    media_obj = {"payload": base64.b64encode(mulaw).decode()}
    assert parse_media_frame(media_obj) == mulaw


def test_parse_media_frame_missing_payload_none():
    # media present but no payload field
    assert parse_media_frame({"event": "media", "media": {"track": "inbound"}}) is None
    # no media object at all
    assert parse_media_frame({"event": "media"}) is None


def test_parse_media_frame_invalid_base64_none():
    event = {"event": "media", "media": {"payload": "!!!not-base64!!!"}}
    assert parse_media_frame(event) is None


def test_parse_media_frame_empty_payload_none():
    assert parse_media_frame({"event": "media", "media": {"payload": ""}}) is None


def test_parse_media_frame_non_dict_none():
    assert parse_media_frame(None) is None  # type: ignore[arg-type]
    assert parse_media_frame("just-a-string") is None  # type: ignore[arg-type]


# ===========================================================================
# Mark / clear event shapes (Twilio documented format)
# ===========================================================================
def test_build_mark_event_shape():
    assert build_mark_event("myMark") == {"event": "mark", "name": "myMark"}


def test_build_clear_event_shape():
    assert build_clear_event() == {"event": "clear"}
