"""U-203 tests — μ-law ↔ PCM16 codec layer for the Twilio media-stream path.

Hermetic, stdlib-only. Covers the task AC:

  * G.711 μ-law decode vectors (±8-zero convention): 0xFF -> +8, 0x7F -> -8,
    0x80/0x00 -> full-scale extremes (+/-32132).
  * Codec invariants: output length scaling, empty input, odd-length ValueError,
    code round-trip stability (encode(decode(u)) == u for all 256 codes).
  * Round-trip quantization error: exact for code-decodable values, <= 33 for
    the in-range band (-1800..1800).
  * Resampler: length-ratio rule, src==dst identity, empty input, odd length /
    non-positive-rate ValueError, sine-wave preservation within tolerance.
  * build_media_event shape + resample path, and its round-trip through
    twilio_stream.parse_media_frame and decode_inbound_frame.
  * decode_inbound_frame: valid base64 -> PCM16; invalid/empty/non-str -> None.

Style mirrors tests/test_twilio_stream.py (module-level functions).
"""
from __future__ import annotations

import base64
import math
import struct

from providers.telephony.twilio_audio import (
    MEDIA_STREAM_RATE,
    build_media_event,
    decode_inbound_frame,
    pcm16_to_ulaw,
    resample_pcm16,
    ulaw_to_pcm16,
)
from providers.telephony.twilio_stream import parse_media_frame

STREAM = "MZaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _pcm(*values: int) -> bytes:
    """Pack ints as little-endian PCM16."""
    return struct.pack(f"<{len(values)}h", *values)


def _sample(pcm: bytes, index: int = 0) -> int:
    """Unpack the index-th PCM16 sample."""
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)[index]


# ===========================================================================
# G.711 μ-law decode vectors (±8-zero convention)
# ===========================================================================


def test_ulaw_ff_decodes_to_plus_8_bias():
    """Code 0xFF (positive zero) decodes to the documented +8 bias value."""
    assert _sample(ulaw_to_pcm16(b"\xff")) == 8


def test_ulaw_7f_decodes_to_minus_8():
    """Code 0x7F (negative zero) decodes to -8."""
    assert _sample(ulaw_to_pcm16(b"\x7f")) == -8


def test_ulaw_80_decodes_to_positive_full_scale():
    """Code 0x80 decodes to the positive full-scale extreme +32132."""
    assert _sample(ulaw_to_pcm16(b"\x80")) == 32132


def test_ulaw_00_decodes_to_negative_full_scale():
    """Code 0x00 decodes to the negative full-scale extreme -32132."""
    assert _sample(ulaw_to_pcm16(b"\x00")) == -32132


# ===========================================================================
# Codec invariants
# ===========================================================================


def test_ulaw_to_pcm16_length_scales_by_two():
    data = b"\xff\x7f\x80\x00"
    out = ulaw_to_pcm16(data)
    assert len(out) == 2 * len(data)


def test_ulaw_to_pcm16_empty_input():
    assert ulaw_to_pcm16(b"") == b""


def test_pcm16_to_ulaw_empty_input():
    assert pcm16_to_ulaw(b"") == b""


def test_pcm16_to_ulaw_odd_length_raises():
    """Odd byte count means a truncated/corrupt PCM buffer — must not be silent."""
    try:
        pcm16_to_ulaw(b"\x00\x00\x00")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for odd-length pcm16")


def test_pcm16_to_ulaw_length_halves():
    data = _pcm(0, 100, -100, 8, -8)
    assert len(pcm16_to_ulaw(data)) == len(data) // 2


def test_code_round_trip_is_stable():
    """For every μ-law code u: encode(decode(u)) == u (nearest-neighbour table)."""
    all_codes = bytes(range(256))
    decoded = ulaw_to_pcm16(all_codes)
    assert pcm16_to_ulaw(decoded) == all_codes


def test_round_trip_error_exact_for_code_values():
    """Values that are exact decode-table outputs survive round-trip losslessly."""
    decoded = ulaw_to_pcm16(bytes(range(256)))
    assert pcm16_to_ulaw(decoded) == bytes(range(256))


def test_round_trip_error_within_33_in_range():
    """Max abs error <= 33 for the in-range band (-1800..1800)."""
    worst = 0
    for v in range(-1800, 1801):
        back = _sample(ulaw_to_pcm16(pcm16_to_ulaw(_pcm(v))))
        worst = max(worst, abs(back - v))
    assert worst <= 33


# ===========================================================================
# Resampler
# ===========================================================================


def test_resample_length_ratio_24k_to_8k():
    n_in = 300  # 300 samples at 24kHz -> 100 samples at 8kHz
    data = _pcm(*[0] * n_in)
    out = resample_pcm16(data, 24000, 8000)
    assert len(out) // 2 == round(n_in * 8000 / 24000) == 100


def test_resample_identity_when_rates_equal():
    data = _pcm(0, 1234, -1234, 32767, -32768)
    assert resample_pcm16(data, 16000, 16000) is data


def test_resample_empty_input():
    assert resample_pcm16(b"", 48000, 8000) == b""


def test_resample_odd_length_raises():
    try:
        resample_pcm16(b"\x00\x00\x00", 24000, 8000)
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for odd-length pcm16")


def test_resample_non_positive_rate_raises():
    for src, dst in ((0, 8000), (8000, 0), (-8000, 8000), (8000, -8000)):
        try:
            resample_pcm16(_pcm(0, 1), src, dst)
        except ValueError:
            pass
        else:
            raise AssertionError(f"expected ValueError for rates {src}->{dst}")


def test_resample_preserves_sine_within_tolerance():
    """A 440Hz sine at 44.1kHz resampled to 8kHz still tracks the sine envelope."""
    src_rate, dst_rate = 44100, 8000
    freq = 440.0
    n_in = 4410  # 100ms
    samples = [int(round(8000 * math.sin(2 * math.pi * freq * i / src_rate))) for i in range(n_in)]
    out = resample_pcm16(_pcm(*samples), src_rate, dst_rate)
    n_out = len(out) // 2
    assert n_out == round(n_in * dst_rate / src_rate)
    worst = 0
    for i in range(n_out):
        expected = 8000 * math.sin(2 * math.pi * freq * (i * src_rate / dst_rate) / src_rate)
        worst = max(worst, abs(_sample(out, i) - expected))
    # Linear interpolation error on a 440Hz sine at these rates stays well under 200.
    assert worst <= 200


# ===========================================================================
# build_media_event
# ===========================================================================


def test_build_media_event_shape():
    ev = build_media_event(STREAM, _pcm(0, 8, -8))
    assert ev["event"] == "media"
    assert ev["streamSid"] == STREAM
    payload = ev["media"]["payload"]
    ulaw = base64.b64decode(payload)
    assert len(ulaw) == 3  # 3 PCM samples -> 3 μ-law bytes
    assert ulaw == pcm16_to_ulaw(_pcm(0, 8, -8))


def test_build_media_event_resamples_non_8k_source():
    """24kHz input is downsampled to the 8kHz media-stream rate."""
    n_in = 300
    ev = build_media_event(STREAM, _pcm(*[0] * n_in), src_rate=24000)
    ulaw = base64.b64decode(ev["media"]["payload"])
    assert len(ulaw) == round(n_in * MEDIA_STREAM_RATE / 24000) == 100


def test_build_media_event_8k_source_is_passthrough_rate():
    n_in = 16
    ev = build_media_event(STREAM, _pcm(*[0] * n_in), src_rate=8000)
    assert len(base64.b64decode(ev["media"]["payload"])) == n_in


# ===========================================================================
# decode_inbound_frame
# ===========================================================================


def test_decode_inbound_frame_valid_base64():
    pcm = _pcm(8, -8, 32132, -32132)
    payload_b64 = base64.b64encode(pcm16_to_ulaw(pcm)).decode("ascii")
    out = decode_inbound_frame(payload_b64)
    assert out == pcm


def test_decode_inbound_frame_invalid_base64_returns_none():
    assert decode_inbound_frame("!!!not-base64!!!") is None


def test_decode_inbound_frame_empty_returns_none():
    assert decode_inbound_frame("") is None


def test_decode_inbound_frame_non_str_returns_none():
    assert decode_inbound_frame(None) is None  # type: ignore[arg-type]
    assert decode_inbound_frame(b"\xff\x7f") is None  # type: ignore[arg-type]


# ===========================================================================
# End-to-end round-trip: PCM -> media event -> parse -> decode -> PCM
# ===========================================================================


def test_media_event_round_trip_through_stream_parsing():
    """build_media_event output is consumable by parse_media_frame + decode_inbound_frame."""
    # Amplitude 1200 keeps samples inside the in-range band (-1800..1800) where
    # the <= 33 quantization bound holds (μ-law steps coarsen at higher amplitudes).
    original = [int(round(1200 * math.sin(2 * math.pi * 440 * i / 8000))) for i in range(80)]
    pcm = _pcm(*original)
    ev = build_media_event(STREAM, pcm, src_rate=8000)

    ulaw_bytes = parse_media_frame(ev)  # media event -> raw μ-law bytes
    assert ulaw_bytes == pcm16_to_ulaw(pcm)

    recovered = decode_inbound_frame(ev["media"]["payload"])  # base64 payload -> PCM16
    assert recovered == ulaw_to_pcm16(ulaw_bytes)
    worst = max(
        abs(_sample(recovered, i) - original[i]) for i in range(len(original))
    )
    assert worst <= 33  # quantization bound
