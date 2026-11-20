"""U-204 tests — barge-in controller + mark/clear ledger (§6.3, R-A-004).

Hermetic and deterministic: injected clock (no sleeps), no network. Covers the
task AC and the R-A-004 acceptance assertions:

  * Epoch/generation: increments per confirmed speech; guard() admits only the
    current epoch (stale + future rejected).
  * Single-clear semantics: exactly one build_clear_event() per barge-in epoch
    transition; rapid double confirmation inside the debounce window is
    absorbed (one epoch, one clear); after the window a new clear is emitted.
  * Cancellation seams: cancel_llm/cancel_tts invoked on barge-in, after the
    clear event is sent (silence first, then stop producers).
  * Late-callback discard matrix: stale TTS chunks (queue_audio), stale LLM
    completions (note_assistant_content) and stale mark acks (ack_mark) are all
    discarded; no audio from a prior epoch is emitted after clear.
  * Mark ledger: queued -> sent -> played | cleared; marks arriving after
    clear stay classified cleared (R-A-004); duplicate names rejected.
  * Duration accounting: generated == played + cleared at quiescence; played
    counts only acked marks; drop-newest rejected frames count generated+cleared.
  * Queue bounds + drop policy: frame and byte caps enforced, newest dropped,
    accepted prefix preserved in order on the wire.
  * Output pacing: one 20ms media frame per clock interval via injected clock
    (catch-up when late, no debt when idle); marks ride immediately after the
    audio they cover; pumped frames survive the codec round-trip.
  * History policy: interrupted assistant content is recorded as generated with
    interrupted: true, carrying the generated/played/cleared split; unplayed
    interrupted content never lands in played_duration; records are per-epoch.

Style mirrors tests/test_twilio_stream.py (module-level functions + fake clock).
"""
from __future__ import annotations

import struct

import pytest

from providers.telephony.barge_in import BargeInController, MarkState
from providers.telephony.twilio_audio import (
    decode_inbound_frame,
    pcm16_to_ulaw,
    ulaw_to_pcm16,
)
from providers.telephony.twilio_stream import parse_media_frame

STREAM = "MZaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _FakeClock:
    """Deterministic, advanceable clock — pacing/debounce without real sleeps."""

    def __init__(self, start: float = 1_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Harness:
    """Controller wired to recording fakes for every injectable seam."""

    def __init__(self, **kwargs):
        self.clock = _FakeClock()
        self.log: list[tuple] = []  # ("send", evt) | ("cancel", name) | ("history", rec)
        self.controller = BargeInController(
            stream_sid=STREAM,
            send_event=self._send,
            cancel_llm=lambda: self._cancel("llm"),
            cancel_tts=lambda: self._cancel("tts"),
            history_sink=self._history,
            clock=self.clock,
            **kwargs,
        )

    async def _send(self, event: dict) -> None:
        self.log.append(("send", event))

    async def _cancel(self, name: str) -> None:
        self.log.append(("cancel", name))

    async def _history(self, record: dict) -> None:
        self.log.append(("history", record))

    def events(self) -> list[dict]:
        return [e for kind, e in self.log if kind == "send"]

    def event_types(self) -> list[str]:
        return [e["event"] for e in self.events()]

    def cancels(self) -> list[str]:
        return [n for kind, n in self.log if kind == "cancel"]

    def history(self) -> list[dict]:
        return [r for kind, r in self.log if kind == "history"]


def _frame(value: int, samples: int = 160) -> bytes:
    """One 20ms frame of constant-value PCM16 (easy to identify on the wire)."""
    return struct.pack(f"<{samples}h", *([value] * samples))


def _payload_first_sample(event: dict) -> int:
    pcm = decode_inbound_frame(event["media"]["payload"])
    assert pcm is not None
    return struct.unpack("<h", pcm[:2])[0]


def _assert_wire_values(events: list[dict], expected: list[int]) -> None:
    """Media payloads decode to (near) the source values — μ-law quantization is
    lossy (<= 33 within the ±1800 band, U-203 contract). Identification values
    stay inside that band and are spaced 400 apart, so the tolerance also proves
    frame identity and order."""
    media = [e for e in events if e["event"] == "media"]
    values = [_payload_first_sample(e) for e in media]
    assert len(values) == len(expected)
    for got, want in zip(values, expected):
        assert abs(got - want) <= 33


# ===========================================================================
# Epoch / guard
# ===========================================================================


@pytest.mark.asyncio
async def test_epoch_increments_per_barge_in():
    h = _Harness(debounce_seconds=0)
    assert h.controller.epoch == 0
    assert await h.controller.on_user_speech_confirmed() is True
    assert h.controller.epoch == 1
    assert await h.controller.on_user_speech_confirmed() is True
    assert h.controller.epoch == 2


def test_guard_admits_only_current_epoch():
    h = _Harness()
    assert h.controller.guard(0) is True
    assert h.controller.guard(1) is False
    assert h.controller.guard(-1) is False


# ===========================================================================
# Single-clear semantics + debounce
# ===========================================================================


@pytest.mark.asyncio
async def test_exactly_one_clear_event_per_barge_in():
    h = _Harness()
    await h.controller.on_user_speech_confirmed()
    assert h.event_types() == ["clear"]
    assert h.events()[0] == {"event": "clear", "streamSid": STREAM}


@pytest.mark.asyncio
async def test_rapid_double_barge_in_yields_single_clear_and_epoch():
    """Debounce policy: repeated confirmed-speech signals for ONE interruption
    are absorbed — one epoch transition, one clear, one cancellation sweep."""
    h = _Harness()  # default 0.25s debounce window
    assert await h.controller.on_user_speech_confirmed() is True
    h.clock.advance(0.05)  # inside the window
    assert await h.controller.on_user_speech_confirmed() is False
    assert h.controller.epoch == 1
    assert h.event_types() == ["clear"]
    assert h.cancels() == ["llm", "tts"]  # not invoked twice


@pytest.mark.asyncio
async def test_barge_in_after_debounce_window_emits_second_clear():
    h = _Harness()
    await h.controller.on_user_speech_confirmed()
    h.clock.advance(1.0)  # past the window: a new interruption episode
    assert await h.controller.on_user_speech_confirmed() is True
    assert h.controller.epoch == 2
    assert h.event_types() == ["clear", "clear"]


# ===========================================================================
# Cancellation seams
# ===========================================================================


@pytest.mark.asyncio
async def test_cancel_seams_invoked_after_clear():
    """Clear goes to the wire first (fastest silence), then producers cancel."""
    h = _Harness()
    await h.controller.on_user_speech_confirmed()
    kinds = [kind for kind, _ in h.log]
    assert kinds == ["send", "cancel", "cancel"]
    assert h.cancels() == ["llm", "tts"]


# ===========================================================================
# Stale-epoch discard matrix (LLM completions / TTS chunks / mark acks)
# ===========================================================================


@pytest.mark.asyncio
async def test_stale_tts_chunk_discarded_after_clear():
    h = _Harness()
    assert h.controller.queue_audio(_frame(100)) is True
    await h.controller.on_user_speech_confirmed()  # epoch 0 -> 1
    assert h.controller.queue_audio(_frame(200), epoch=0) is False  # stale TTS chunk
    await h.controller.pump()
    assert h.event_types() == ["clear"]  # no media from either epoch leaked


@pytest.mark.asyncio
async def test_stale_llm_completion_discarded_after_clear():
    h = _Harness(debounce_seconds=0)
    assert h.controller.note_assistant_content("A", epoch=0) is True
    await h.controller.on_user_speech_confirmed()
    assert h.controller.note_assistant_content("B", epoch=0) is False  # stale LLM output
    await h.controller.on_user_speech_confirmed()
    assert [r["content"] for r in h.history()] == ["A"]  # stale "B" never recorded


@pytest.mark.asyncio
async def test_mark_ack_arriving_after_clear_stays_cleared():
    """R-A-004: marks arriving after clear are classified as cleared."""
    h = _Harness()
    h.controller.queue_audio(_frame(100))
    h.controller.mark("m")
    await h.controller.pump()  # frame + mark -> wire (mark now SENT)
    assert h.controller.mark_state("m", epoch=0) is MarkState.SENT
    await h.controller.on_user_speech_confirmed()  # clear wipes Twilio buffer
    assert h.controller.mark_state("m", epoch=0) is MarkState.CLEARED
    assert h.controller.ack_mark("m", epoch=0) is False  # stale-epoch ack discarded
    assert h.controller.ack_mark("m", epoch=1) is False  # unknown in new epoch
    assert h.controller.mark_state("m", epoch=0) is MarkState.CLEARED


@pytest.mark.asyncio
async def test_no_prior_epoch_audio_emitted_after_clear():
    """R-A-004: audio queued pre-clear never reaches the wire post-clear."""
    h = _Harness()
    h.controller.queue_audio(_frame(200))
    h.controller.queue_audio(_frame(600))
    h.controller.queue_audio(_frame(1000))
    await h.controller.pump()  # only frame 200 goes out
    await h.controller.on_user_speech_confirmed()  # clears 600/1000 locally + remotely
    h.controller.queue_audio(_frame(1400))  # new epoch
    h.clock.advance(0.02)
    await h.controller.pump()
    assert h.event_types() == ["media", "clear", "media"]
    _assert_wire_values(h.events(), [200, 1400])  # 600/1000 never emitted


# ===========================================================================
# Mark ledger lifecycle
# ===========================================================================


@pytest.mark.asyncio
async def test_mark_moves_queued_to_sent_to_played():
    h = _Harness()
    h.controller.queue_audio(_frame(100))
    h.controller.mark("m")
    assert h.controller.mark_state("m", epoch=0) is MarkState.QUEUED
    await h.controller.pump()  # frame then mark ride together
    assert h.controller.mark_state("m", epoch=0) is MarkState.SENT
    assert h.controller.ack_mark("m", epoch=0) is True
    assert h.controller.mark_state("m", epoch=0) is MarkState.PLAYED
    assert h.controller.played_duration == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_mark_sent_then_cleared_on_barge_in():
    h = _Harness()
    h.controller.queue_audio(_frame(100))
    h.controller.mark("m")
    await h.controller.pump()
    await h.controller.on_user_speech_confirmed()
    assert h.controller.mark_state("m", epoch=0) is MarkState.CLEARED


@pytest.mark.asyncio
async def test_queued_mark_cleared_without_being_sent():
    h = _Harness()
    h.controller.queue_audio(_frame(100))
    h.controller.mark("m")
    await h.controller.on_user_speech_confirmed()  # no pump: mark never hit the wire
    assert h.controller.mark_state("m", epoch=0) is MarkState.CLEARED
    assert h.event_types() == ["clear"]  # and no mark event was ever emitted


def test_duplicate_mark_name_rejected_within_epoch():
    h = _Harness()
    h.controller.mark("m")
    with pytest.raises(ValueError):
        h.controller.mark("m")


@pytest.mark.asyncio
async def test_mark_name_reusable_in_new_epoch():
    h = _Harness(debounce_seconds=0)
    h.controller.mark("m")
    await h.controller.on_user_speech_confirmed()
    h.controller.mark("m")  # new epoch: no collision
    assert h.controller.mark_state("m", epoch=1) is MarkState.QUEUED


# ===========================================================================
# Duration accounting
# ===========================================================================


@pytest.mark.asyncio
async def test_generated_equals_played_plus_cleared_at_quiescence():
    h = _Harness(debounce_seconds=0)
    c = h.controller
    c.queue_audio(_frame(0))
    c.queue_audio(_frame(0))
    c.mark("m1")
    c.queue_audio(_frame(0))
    c.queue_audio(_frame(0))
    c.mark("m2")
    h.clock.advance(0.06)
    await c.pump()  # catch-up: 4 frames + both marks to the wire
    assert c.ack_mark("m1", epoch=0) is True  # first half played
    await c.on_user_speech_confirmed()  # second half cleared
    assert c.generated_duration == pytest.approx(0.08)
    assert c.played_duration == pytest.approx(0.04)
    assert c.cleared_duration == pytest.approx(0.04)
    assert c.pending_duration == pytest.approx(0.0)
    assert c.generated_duration == pytest.approx(c.played_duration + c.cleared_duration)


@pytest.mark.asyncio
async def test_played_duration_counts_only_acked_marks():
    h = _Harness(debounce_seconds=0)
    c = h.controller
    c.queue_audio(_frame(0))
    c.queue_audio(_frame(0))
    c.mark("m1")  # covers 0.04s
    c.queue_audio(_frame(0))
    c.mark("m2")  # covers 0.02s
    h.clock.advance(0.06)
    await c.pump()
    assert c.ack_mark("m1", epoch=0) is True
    await c.on_user_speech_confirmed()  # m2's audio interrupted
    assert c.mark_state("m1", epoch=0) is MarkState.PLAYED
    assert c.mark_state("m2", epoch=0) is MarkState.CLEARED
    assert c.played_duration == pytest.approx(0.04)  # only the acked half
    assert c.cleared_duration == pytest.approx(0.02)
    assert c.generated_duration == pytest.approx(0.06)


@pytest.mark.asyncio
async def test_rejected_frames_counted_generated_and_cleared():
    """Drop-newest frames were produced by TTS but never play: generated+cleared,
    so the identity still holds at quiescence."""
    h = _Harness(max_queue_frames=2)
    c = h.controller
    for value in (100, 200, 300, 400):
        accepted = c.queue_audio(_frame(value))
        assert accepted is (value <= 200)
    c.mark("m")
    h.clock.advance(0.04)
    await c.pump()
    assert c.ack_mark("m", epoch=0) is True
    assert c.generated_duration == pytest.approx(0.08)  # all four were produced
    assert c.played_duration == pytest.approx(0.04)  # two reached the wire
    assert c.cleared_duration == pytest.approx(0.04)  # two rejected at the door
    assert c.generated_duration == pytest.approx(c.played_duration + c.cleared_duration)


# ===========================================================================
# Queue bounds + drop policy (drop-newest)
# ===========================================================================


@pytest.mark.asyncio
async def test_queue_frame_bound_enforced_drop_newest():
    h = _Harness(max_queue_frames=3)
    c = h.controller
    for i, value in enumerate((200, 600, 1000, 1400, 1800)):
        assert c.queue_audio(_frame(value)) is (i < 3)
    h.clock.advance(0.04)
    await c.pump()
    _assert_wire_values(h.events(), [200, 600, 1000])  # prefix kept, newest dropped
    assert c.generated_duration == pytest.approx(0.10)  # 5 frames produced
    assert c.cleared_duration == pytest.approx(0.04)  # 2 frames dropped


@pytest.mark.asyncio
async def test_queue_byte_bound_enforced():
    h = _Harness(max_queue_bytes=640)  # exactly two 320-byte frames
    c = h.controller
    assert c.queue_audio(_frame(600)) is True
    assert c.queue_audio(_frame(1000)) is True
    assert c.queue_audio(_frame(1400)) is False
    assert c.queue_audio(_frame(1800)) is False
    h.clock.advance(0.04)
    await c.pump()
    _assert_wire_values(h.events(), [600, 1000])


# ===========================================================================
# Output pacing (injected clock, 20ms media-frame cadence)
# ===========================================================================


@pytest.mark.asyncio
async def test_pacing_one_frame_per_20ms_interval():
    h = _Harness()
    for value in (1, 2, 3, 4, 5):
        h.controller.queue_audio(_frame(value))
    assert await h.controller.pump() == 1  # first frame due immediately
    h.clock.advance(0.02)
    assert await h.controller.pump() == 1
    h.clock.advance(0.06)  # three intervals elapsed -> catch-up burst of 3
    assert await h.controller.pump() == 3
    assert h.event_types() == ["media"] * 5


@pytest.mark.asyncio
async def test_pacing_idle_reset_has_no_catch_up_debt():
    h = _Harness()
    h.controller.queue_audio(_frame(1))
    assert await h.controller.pump() == 1
    h.clock.advance(1.0)  # long idle with an empty queue
    assert await h.controller.pump() == 0  # resets the cadence, no debt
    h.controller.queue_audio(_frame(2))
    h.controller.queue_audio(_frame(3))
    assert await h.controller.pump() == 1  # fresh cadence: one frame, not fifty


@pytest.mark.asyncio
async def test_mark_rides_immediately_after_its_audio():
    h = _Harness()
    h.controller.queue_audio(_frame(100))
    h.controller.queue_audio(_frame(200))
    h.controller.mark("m")
    h.controller.queue_audio(_frame(300))
    await h.controller.pump()  # frame 100
    h.clock.advance(0.02)
    await h.controller.pump()  # frame 200 + its trailing mark
    h.clock.advance(0.02)
    await h.controller.pump()  # frame 300
    assert h.event_types() == ["media", "media", "mark", "media"]
    mark_event = [e for e in h.events() if e["event"] == "mark"][0]
    assert mark_event == {"event": "mark", "name": "m", "streamSid": STREAM}


@pytest.mark.asyncio
async def test_pumped_frames_survive_codec_round_trip():
    """Wire frames are μ-law base64 decodable back to (near) the source PCM —
    integration with the U-203 codec layer and U-202 frame parsing."""
    h = _Harness()
    h.controller.queue_audio(_frame(1200))
    h.controller.queue_audio(_frame(-1200))
    h.clock.advance(0.04)
    await h.controller.pump()
    media = [e for e in h.events() if e["event"] == "media"]
    for event, expected in zip(media, (1200, -1200)):
        raw_ulaw = parse_media_frame(event)  # U-202 parser agrees on the payload
        assert raw_ulaw == pcm16_to_ulaw(_frame(expected))
        recovered = ulaw_to_pcm16(raw_ulaw)
        assert abs(struct.unpack("<h", recovered[:2])[0] - expected) <= 33


# ===========================================================================
# Interrupted-assistant-content history policy
# ===========================================================================


@pytest.mark.asyncio
async def test_interrupted_content_recorded_with_flag_and_split():
    h = _Harness(debounce_seconds=0)
    c = h.controller
    c.note_assistant_content("Hel")
    c.note_assistant_content("lo")
    c.queue_audio(_frame(0))
    c.queue_audio(_frame(0))
    c.mark("m1")
    await h.controller.pump()
    h.clock.advance(0.02)
    await h.controller.pump()
    assert c.ack_mark("m1", epoch=0) is True  # 0.04s confirmed played
    c.queue_audio(_frame(0))  # 0.02s generated after the mark, still pending
    await c.on_user_speech_confirmed()
    records = h.history()
    assert len(records) == 1
    rec = records[0]
    assert rec["role"] == "assistant"
    assert rec["content"] == "Hello"
    assert rec["interrupted"] is True
    assert rec["epoch"] == 0
    assert rec["generated_duration"] == pytest.approx(0.06)
    assert rec["played_duration"] == pytest.approx(0.04)  # only mark-confirmed
    assert rec["cleared_duration"] == pytest.approx(0.02)  # the interrupted tail


@pytest.mark.asyncio
async def test_interrupted_unplayed_content_never_in_played_duration():
    h = _Harness()
    c = h.controller
    c.note_assistant_content("Hi")
    c.queue_audio(_frame(0))  # produced but never acked
    await c.on_user_speech_confirmed()
    rec = h.history()[0]
    assert rec["interrupted"] is True
    assert rec["played_duration"] == pytest.approx(0.0)  # never counted as played
    assert rec["cleared_duration"] == pytest.approx(0.02)
    assert rec["generated_duration"] == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_no_history_record_without_noted_content():
    h = _Harness()
    h.controller.queue_audio(_frame(0))  # audio-only epoch, no LLM text
    await h.controller.on_user_speech_confirmed()
    assert h.history() == []


@pytest.mark.asyncio
async def test_history_records_are_per_epoch():
    h = _Harness(debounce_seconds=0)
    h.controller.note_assistant_content("A")
    await h.controller.on_user_speech_confirmed()
    h.controller.note_assistant_content("B")
    await h.controller.on_user_speech_confirmed()
    assert [(r["epoch"], r["content"]) for r in h.history()] == [(0, "A"), (1, "B")]


# ===========================================================================
# Resampler integration (U-203 import-only contract)
# ===========================================================================


@pytest.mark.asyncio
async def test_queue_audio_resamples_non_8k_source():
    h = _Harness()
    c = h.controller
    assert c.queue_audio(_frame(500, samples=480), src_rate=24000) is True  # -> 160 samples @8k
    assert await c.pump() == 1
    media = [e for e in h.events() if e["event"] == "media"][0]
    assert abs(_payload_first_sample(media) - 500) <= 33


@pytest.mark.asyncio
async def test_queue_audio_rejects_odd_length_input():
    h = _Harness()
    with pytest.raises(ValueError):
        h.controller.queue_audio(b"\x00\x00\x00")
