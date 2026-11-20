"""U-201 tests — FC-1 Twilio adapter signature layer + TwiML handshake.

Hermetic, stdlib-only (no twilio SDK, no network). Covers per the task AC:

  * `TwilioSignatureValidator` / `validate_incoming_call` — known-good signature
    accepted, missing / wrong / wrong-host signatures rejected. The known vector
    mirrors tests/test_twilio_sig.py (the independent stdlib oracle), so the two
    implementations must agree.
  * SDK-vs-stdlib backend selection (decision A-1): stdlib oracle is forced and
    proven; an injected SDK validator is used and its verdict returned.
  * `build_twiml_connect_stream` — produces VALID XML, points <Stream> at
    wss://<public-host>/twilio/media-stream (never an internal host), carries the
    caller <Parameter>, and includes the <Say> fallback.

Style mirrors tests/test_twilio_sig.py (module-level functions, direct import).
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

import twilio_sig
from providers.telephony.twilio_adapter import (
    TwilioConfig,
    TwilioSignatureValidator,
    build_twiml_connect_stream,
    validate_incoming_call,
)
import providers.telephony.twilio_adapter as twilio_adapter

# Known vector — same frozen values as tests/test_twilio_sig.py (the oracle).
_TOKEN = "12345"
_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}
_SIG = "RSOYDt4T1cUTdK1PDd93/VVr8B8="

PUBLIC_BASE = "https://twilio.example.com"
INTERNAL_HOST = "d28jyp0rnkqvcx.cloudfront.net"  # host Twilio must NOT see


def _cfg() -> TwilioConfig:
    return TwilioConfig(auth_token=_TOKEN, public_base_url=PUBLIC_BASE)


# ===========================================================================
# validate_incoming_call — known-good / missing / wrong / wrong-host
# ===========================================================================
def test_validate_incoming_call_known_good_signature():
    assert validate_incoming_call(_cfg(), _SIG, _URL, dict(_PARAMS))


def test_validate_incoming_call_missing_signature_false():
    assert not validate_incoming_call(_cfg(), "", _URL, dict(_PARAMS))


def test_validate_incoming_call_wrong_signature_false():
    assert not validate_incoming_call(_cfg(), "not-a-real-signature", _URL, dict(_PARAMS))


def test_validate_incoming_call_wrong_host_false():
    # Signature was made for the public host; verifying against the internal
    # CloudFront host must fail (canonical Round-1 pitfall).
    internal_url = _URL.replace("mycompany.com", INTERNAL_HOST)
    assert not validate_incoming_call(_cfg(), _SIG, internal_url, dict(_PARAMS))


def test_validate_incoming_call_tampered_param_false():
    bad = dict(_PARAMS, Digits="9999")
    assert not validate_incoming_call(_cfg(), _SIG, _URL, bad)


# ===========================================================================
# TwilioSignatureValidator — FC-8 is_valid + A-1 backend selection
# ===========================================================================
def test_validator_is_valid_matches_oracle(monkeypatch):
    # Force the stdlib path so the known vector exercises twilio_sig directly.
    monkeypatch.setattr(twilio_adapter, "_load_sdk_validator", lambda _t: None)
    v = TwilioSignatureValidator(_TOKEN)
    assert v.is_valid(url=_URL, params=dict(_PARAMS), signature=_SIG)
    assert not v.is_valid(url=_URL, params=dict(_PARAMS), signature="wrong")


def test_validator_stdlib_backend_forced_explicit_none():
    v = TwilioSignatureValidator(_TOKEN, sdk_validator=None)
    assert v.is_valid(url=_URL, params=dict(_PARAMS), signature=_SIG)


def test_validator_missing_signature_or_token_false():
    v = TwilioSignatureValidator(_TOKEN)
    assert not v.is_valid(url=_URL, params=dict(_PARAMS), signature="")
    empty = TwilioSignatureValidator("")
    assert not empty.is_valid(url=_URL, params=dict(_PARAMS), signature=_SIG)


class _FakeSDKValidator:
    """Stand-in for twilio SDK RequestValidator: records calls, returns verdict."""

    def __init__(self, verdict: bool):
        self.verdict = verdict
        self.calls = []

    def validate(self, url, params, signature):
        self.calls.append((url, params, signature))
        return self.verdict


def test_validator_uses_injected_sdk_backend_true():
    fake = _FakeSDKValidator(True)
    v = TwilioSignatureValidator(_TOKEN, sdk_validator=fake)
    assert v.is_valid(url=_URL, params=dict(_PARAMS), signature=_SIG)
    assert fake.calls and fake.calls[0][0] == _URL


def test_validator_uses_injected_sdk_backend_false():
    fake = _FakeSDKValidator(False)
    v = TwilioSignatureValidator(_TOKEN, sdk_validator=fake)
    assert not v.is_valid(url=_URL, params=dict(_PARAMS), signature=_SIG)


def test_validator_sdk_backend_error_maps_to_false():
    class _Boom:
        def validate(self, url, params, signature):
            raise RuntimeError("boom")

    v = TwilioSignatureValidator(_TOKEN, sdk_validator=_Boom())
    assert not v.is_valid(url=_URL, params=dict(_PARAMS), signature=_SIG)


# ===========================================================================
# build_twiml_connect_stream — valid XML + correct stream URL + fallback
# ===========================================================================
def test_twiml_valid_xml_and_stream_url():
    xml = build_twiml_connect_stream(_cfg(), "")
    root = ET.fromstring(xml)  # raises if not well-formed → proves "valid XML"
    assert root.tag == "Response"
    stream = root.find("./Connect/Stream")
    assert stream is not None
    assert stream.get("url") == f"wss://twilio.example.com/twilio/media-stream"
    assert INTERNAL_HOST not in xml


def test_twiml_stream_url_derived_from_path():
    xml = build_twiml_connect_stream(_cfg(), "/twilio/media-stream")
    root = ET.fromstring(xml)
    assert root.find("./Connect/Stream").get("url") == (
        "wss://twilio.example.com/twilio/media-stream"
    )


def test_twiml_full_url_passthrough():
    full = "wss://media.example.org/custom/stream"
    xml = build_twiml_connect_stream(_cfg(), full)
    root = ET.fromstring(xml)
    assert root.find("./Connect/Stream").get("url") == full


def test_twiml_public_host_strips_scheme_and_slash():
    cfg = TwilioConfig(auth_token=_TOKEN, public_base_url="https://twilio.example.com/")
    root = ET.fromstring(build_twiml_connect_stream(cfg, ""))
    assert root.find("./Connect/Stream").get("url") == (
        "wss://twilio.example.com/twilio/media-stream"
    )


def test_twiml_includes_caller_parameter():
    xml = build_twiml_connect_stream(_cfg(), "", caller="+15557654321")
    root = ET.fromstring(xml)
    param = root.find("./Connect/Stream/Parameter")
    assert param is not None
    assert param.get("name") == "caller"
    assert param.get("value") == "+15557654321"


def test_twiml_includes_say_fallback():
    xml = build_twiml_connect_stream(_cfg(), "", caller="+15550001111")
    root = ET.fromstring(xml)
    say = root.find("./Say")
    assert say is not None and say.text and "unable to connect" in say.text


def test_twiml_escapes_caller_for_valid_xml():
    # A hostile caller string must not break the document (attribute injection).
    xml = build_twiml_connect_stream(_cfg(), "", caller='"/><evil x="')
    root = ET.fromstring(xml)  # still well-formed
    param = root.find("./Connect/Stream/Parameter")
    assert param.get("value") == '"/><evil x="'


def test_twiml_matches_reference_shape_substrings():
    # Parity with bot.py's pinned 200 TwiML: Connect + Stream + wss public host.
    xml = build_twiml_connect_stream(_cfg(), "", caller="+15557654321")
    assert "<Connect>" in xml and "<Stream" in xml
    assert "wss://twilio.example.com/twilio/media-stream" in xml
    assert 'name="caller"' in xml
