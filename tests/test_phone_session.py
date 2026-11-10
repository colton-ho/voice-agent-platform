"""T2 tests: serializer injection seam + _run_phone_session kernel extraction.

Verifies the zero-regression refactor of the inbound phone-call path:

  * Both pipeline builders (`_build_pipeline`, `_build_nova_sonic_pipeline`) gained
    an optional ``serializer: FrameSerializer | None = None`` parameter that falls
    back to ``RawPCMSerializer`` when ``None`` (byte-identical Chime/phone path).
  * The /phone/ws endpoint body was extracted into
    ``_run_phone_session(websocket, *, call_id, caller, serializer=None)``, which
    threads the serializer through to whichever builder runs.

Strategy: drive the *real* builders far enough to reach the
``FastAPIWebsocketTransport(... params=FastAPIWebsocketParams(serializer=...))``
construction, then monkeypatch the transport class to *capture the serializer*
and short-circuit (raise a sentinel) before any AWS/MCP/network work. This
mirrors how tests/test_demo_translate.py monkeypatches make_managed_llm to
capture the model handed to it. No real LLM/Foundry/Twilio calls.
"""

from __future__ import annotations

import importlib
import sys

import pytest


class _Sentinel(Exception):
    """Raised by the fake transport once it has captured the serializer, so the
    builder never proceeds to real AWS / MCP work."""


@pytest.fixture
def bot_mod(monkeypatch):
    # Dummy AWS creds so boto3 frozen-credential resolution (which runs *before*
    # the transport is constructed in both builders) succeeds fully offline.
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pwd")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_loader", "mcp_config"):
            del sys.modules[mod]
    return importlib.import_module("bot")


class _FakeWebSocket:
    """Minimal stand-in; the builders only pass it straight to the (faked)
    transport, so no methods are exercised before the sentinel fires."""

    def __init__(self):
        self.query_params = {}


def _install_capturing_transport(bot, monkeypatch):
    """Replace FastAPIWebsocketTransport with a stub that records the serializer
    handed to FastAPIWebsocketParams and aborts the builder via _Sentinel.

    Returns the dict that will receive ``captured["serializer"]``.
    """
    captured: dict = {}

    def _fake_transport(*, websocket, params):
        captured["serializer"] = params.serializer
        captured["params"] = params
        raise _Sentinel()

    monkeypatch.setattr(bot, "FastAPIWebsocketTransport", _fake_transport)
    return captured


def _set_phone_engine(bot, monkeypatch, engine: str):
    """Force RUNTIME_CONFIG.get_phone_defaults() to select ``engine``."""
    base = dict(bot.RUNTIME_CONFIG.get_phone_defaults())
    base["engine"] = engine
    monkeypatch.setattr(
        bot.RUNTIME_CONFIG, "get_phone_defaults", lambda: dict(base)
    )


async def _drive_kernel(bot, monkeypatch, *, engine, serializer):
    """Run _run_phone_session until the capturing transport fires its sentinel."""
    captured = _install_capturing_transport(bot, monkeypatch)
    _set_phone_engine(bot, monkeypatch, engine)
    # No history side effects.
    monkeypatch.setattr(bot, "_history", None)

    ws = _FakeWebSocket()
    call_id = "test-call-1"
    # session_register populates ACTIVE_SESSIONS (kernel reads it for "started").
    await bot.session_register(call_id, caller="+15551234567", primary_emit=lambda *a, **k: None)
    try:
        with pytest.raises(_Sentinel):
            await bot._run_phone_session(
                ws, call_id=call_id, caller="+15551234567", serializer=serializer
            )
    finally:
        await bot.session_unregister(call_id)
    return captured


# ---------------------------------------------------------------------------
# AC1 / AC3 — default None → RawPCMSerializer (byte-identical Chime/phone path)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_default_serializer_is_rawpcm(bot_mod, monkeypatch):
    captured = await _drive_kernel(bot_mod, monkeypatch, engine="pipeline", serializer=None)
    assert isinstance(captured["serializer"], bot_mod.RawPCMSerializer)


@pytest.mark.asyncio
async def test_nova_default_serializer_is_rawpcm(bot_mod, monkeypatch):
    captured = await _drive_kernel(bot_mod, monkeypatch, engine="nova-sonic", serializer=None)
    assert isinstance(captured["serializer"], bot_mod.RawPCMSerializer)


# ---------------------------------------------------------------------------
# AC1 / AC3 — a passed-in serializer is threaded through to the transport params
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pipeline_passthrough_serializer(bot_mod, monkeypatch):
    from pipecat.serializers.base_serializer import FrameSerializer

    class _CustomSerializer(FrameSerializer):
        async def serialize(self, frame):
            return None

        async def deserialize(self, data):
            return None

    custom = _CustomSerializer()
    captured = await _drive_kernel(bot_mod, monkeypatch, engine="pipeline", serializer=custom)
    assert captured["serializer"] is custom
    assert not isinstance(captured["serializer"], bot_mod.RawPCMSerializer)


@pytest.mark.asyncio
async def test_nova_passthrough_serializer(bot_mod, monkeypatch):
    from pipecat.serializers.base_serializer import FrameSerializer

    class _CustomSerializer(FrameSerializer):
        async def serialize(self, frame):
            return None

        async def deserialize(self, data):
            return None

    custom = _CustomSerializer()
    captured = await _drive_kernel(bot_mod, monkeypatch, engine="nova-sonic", serializer=custom)
    assert captured["serializer"] is custom


# ---------------------------------------------------------------------------
# AC2 — builder signatures expose the new optional parameter with default None
# ---------------------------------------------------------------------------
def test_builders_accept_optional_serializer(bot_mod):
    import inspect

    for fn in (bot_mod._build_pipeline, bot_mod._build_nova_sonic_pipeline):
        sig = inspect.signature(fn)
        assert "serializer" in sig.parameters, fn.__name__
        assert sig.parameters["serializer"].default is None, fn.__name__


def test_run_phone_session_signature(bot_mod):
    import inspect

    sig = inspect.signature(bot_mod._run_phone_session)
    params = sig.parameters
    assert "call_id" in params and params["call_id"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "caller" in params and params["caller"].kind is inspect.Parameter.KEYWORD_ONLY
    assert "serializer" in params and params["serializer"].default is None


# ---------------------------------------------------------------------------
# AC2 — /phone/ws endpoint still registered (kernel extraction didn't drop it)
# ---------------------------------------------------------------------------
def test_phone_ws_route_registered(bot_mod):
    paths = {getattr(r, "path", None) for r in bot_mod.app.routes}
    assert "/phone/ws" in paths


# ---------------------------------------------------------------------------
# AC2 — /phone/ws endpoint defaults serializer to None (Chime zero-regression):
# accept() + parse query + session_register + delegate to kernel with no serializer.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_phone_ws_endpoint_delegates_with_none_serializer(bot_mod, monkeypatch):
    seen: dict = {}

    async def _fake_kernel(websocket, *, call_id, caller, serializer=None):
        seen["call_id"] = call_id
        seen["caller"] = caller
        seen["serializer"] = serializer
        # Confirm the endpoint registered the session before delegating.
        seen["registered"] = call_id in bot_mod.ACTIVE_SESSIONS

    monkeypatch.setattr(bot_mod, "_run_phone_session", _fake_kernel)

    class _AcceptWS:
        def __init__(self):
            self.query_params = {"call_id": "phone-xyz", "caller": "+15550001111"}
            self.accepted = False

        async def accept(self):
            self.accepted = True

    ws = _AcceptWS()
    try:
        await bot_mod.phone_ws_endpoint(ws)
    finally:
        await bot_mod.session_unregister("phone-xyz")

    assert ws.accepted is True
    assert seen["call_id"] == "phone-xyz"
    assert seen["caller"] == "+15550001111"
    assert seen["serializer"] is None  # Chime path: default None → RawPCMSerializer
    assert seen["registered"] is True
