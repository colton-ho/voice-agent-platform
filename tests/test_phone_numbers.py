"""Unit tests for bot._list_twilio_vc_numbers (admin phone-number view).

U-207 (decision (d) FINAL 2026-11-06): content re-sourced Chime → Twilio
Incoming Phone Numbers behind the PINNED {voice_connectors, error} envelope.
No network / no twilio SDK needed: tests monkeypatch the injectable client
seam ``bot._twilio_vc_client`` with a fake whose
``incoming_phone_numbers.list()`` yields Twilio-shaped resource objects
(``sid`` / ``friendly_name`` / ``phone_number`` / ``status``).

Pinned invariants asserted here (product-impact C-4):
  * envelope keys stay exactly {voice_connectors, error};
  * every row has exactly the legacy field names
    {voice_connector_id, voice_connector_name, e164, status} (content swap
    only — field names are never removed/renamed);
  * on any Twilio failure the envelope degrades to
    {"voice_connectors": [], "error": "<reason>"} — no exception, no 500,
    and no credentials in the error text.
"""

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bot  # noqa: E402

PINNED_ROW_FIELDS = {"voice_connector_id", "voice_connector_name", "e164", "status"}


def _num(sid, friendly_name, phone_number, status):
    return SimpleNamespace(sid=sid, friendly_name=friendly_name,
                           phone_number=phone_number, status=status)


def _fake_client(monkeypatch, numbers=None, error=None):
    """Install a fake Twilio client at the bot._twilio_vc_client seam."""
    class _Incoming:
        def list(self):
            if error is not None:
                raise error
            return numbers or []

    client = SimpleNamespace(incoming_phone_numbers=_Incoming())
    monkeypatch.setattr(bot, "_twilio_vc_client", lambda: client)
    return client


def test_maps_twilio_numbers_to_pinned_rows(monkeypatch):
    _fake_client(monkeypatch, numbers=[
        _num("PNaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "yue-test", "+15550001111", "in-use"),
        _num("PNbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "nova-bot-test", "+15550002222", "in-use"),
    ])
    out = bot._list_twilio_vc_numbers()
    assert out["error"] is None
    rows = {r["e164"]: r for r in out["voice_connectors"]}
    assert rows["+15550001111"]["voice_connector_id"] == "PNaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert rows["+15550001111"]["voice_connector_name"] == "yue-test"
    assert rows["+15550001111"]["status"] == "in-use"
    assert rows["+15550002222"]["voice_connector_id"] == "PNbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    assert rows["+15550002222"]["voice_connector_name"] == "nova-bot-test"
    assert rows["+15550002222"]["status"] == "in-use"


def test_envelope_and_row_field_names_are_pinned(monkeypatch):
    _fake_client(monkeypatch, numbers=[
        _num("PN1", "A", "+1", "in-use"),
        _num("PN2", "B", "+2", "in-use"),
    ])
    out = bot._list_twilio_vc_numbers()
    assert set(out.keys()) == {"voice_connectors", "error"}
    assert len(out["voice_connectors"]) == 2
    for row in out["voice_connectors"]:
        assert set(row.keys()) == PINNED_ROW_FIELDS


def test_missing_source_attributes_become_neutral(monkeypatch):
    # e.g. a resource without status/friendly_name: field stays present, None.
    _fake_client(monkeypatch, numbers=[SimpleNamespace(phone_number="+19998887777")])
    out = bot._list_twilio_vc_numbers()
    assert out["error"] is None
    assert out["voice_connectors"] == [
        {"voice_connector_id": None, "voice_connector_name": None,
         "e164": "+19998887777", "status": None}]


def test_empty_account_returns_empty_rows(monkeypatch):
    _fake_client(monkeypatch, numbers=[])
    out = bot._list_twilio_vc_numbers()
    assert out == {"voice_connectors": [], "error": None}


def test_twilio_error_degrades_to_empty(monkeypatch):
    _fake_client(monkeypatch, error=RuntimeError("HTTP 403 AccessDenied: not authorized"))
    out = bot._list_twilio_vc_numbers()
    assert out["voice_connectors"] == []
    assert out["error"] and "AccessDenied" in out["error"]


def test_error_does_not_leak_credentials(monkeypatch):
    secret = "super-secret-auth-token"
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", secret)
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "ACffffffffffffffffffffffffffffffff")
    _fake_client(monkeypatch, error=RuntimeError("HTTP 401: Authenticate"))
    out = bot._list_twilio_vc_numbers()
    assert out["voice_connectors"] == []
    assert out["error"]
    assert secret not in out["error"]
    assert "ACffff" not in out["error"]


def test_client_construction_failure_degrades(monkeypatch):
    # _twilio_vc_client itself raising (bad creds / missing SDK) must still
    # degrade, not propagate.
    def _boom():
        raise RuntimeError("no creds")
    monkeypatch.setattr(bot, "_twilio_vc_client", _boom)
    out = bot._list_twilio_vc_numbers()
    assert out["voice_connectors"] == []
    assert "no creds" in out["error"]


def test_route_delegates_to_helper(monkeypatch):
    # Call site check: the /api/admin/phone-numbers route returns the same
    # pinned envelope from the renamed helper.
    _fake_client(monkeypatch, numbers=[_num("PN1", "A", "+1", "in-use")])
    out = asyncio.run(bot.admin_phone_numbers(_={}))
    assert set(out.keys()) == {"voice_connectors", "error"}
    assert out["error"] is None
    assert out["voice_connectors"][0]["voice_connector_name"] == "A"
