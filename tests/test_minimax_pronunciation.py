"""SimpleMiniMaxTTSService payload-injection tests for the Cantonese
「返」 → faan1 pronunciation override.

Strategy: instantiate ``SimpleMiniMaxTTSService`` with a fake aiohttp
session whose ``post`` method captures the JSON payload, then drive
``run_tts`` to completion and assert on the captured payload.

The mock returns an empty SSE stream (zero chunks, HTTP 200) so the
streaming loop terminates cleanly without yielding any audio frames.

Mirrors fixture style from ``tests/test_history.py`` and
``tests/test_runtime_config.py``.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# aiohttp.ClientSession.post() mock
# ---------------------------------------------------------------------------


class _FakeContent:
    """Minimal ``response.content`` stub that exposes ``iter_chunked``.

    Yields no bytes — the SSE-walking loop in run_tts simply exits.
    """

    def iter_chunked(self, _size: int):
        async def _agen():
            if False:
                yield b""  # pragma: no cover — make this an async generator
        return _agen()


class _FakeResponse:
    """Stand-in for ``aiohttp.ClientResponse``."""

    def __init__(self) -> None:
        self.status = 200
        self.content = _FakeContent()

    async def text(self) -> str:  # pragma: no cover — only hit on non-200
        return ""


class _FakePostCM:
    """Async context-manager returned by ``ClientSession.post(...)``."""

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def __aenter__(self) -> _FakeResponse:
        return _FakeResponse()

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class _FakeSession:
    """Drop-in for ``aiohttp.ClientSession`` — only ``post`` is exercised."""

    def __init__(self) -> None:
        self.last_payload: dict[str, Any] | None = None
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None

    def post(self, url: str, *, headers: dict[str, str], json: dict[str, Any]):
        # NB: NOT async — aiohttp's real .post() is sync and returns an
        # async-context-manager.
        self.last_url = url
        self.last_headers = headers
        # Defensive copy so subsequent in-place mutations (none expected)
        # don't change what we asserted on.
        self.last_payload = dict(json)
        return _FakePostCM(self.last_payload)


# ---------------------------------------------------------------------------
# bot module / service fixture
# ---------------------------------------------------------------------------


def _import_bot_clean() -> object:
    """(Re)import bot.py with the env vars MiniMax construction needs."""
    os.environ.setdefault("MINIMAX_API_KEY", "test-api-key")
    os.environ.setdefault("MINIMAX_GROUP_ID", "")
    os.environ.setdefault("ADMIN_PASSWORD", "")
    os.environ.setdefault("HISTORY_DISABLED", "1")
    for mod in list(sys.modules):
        if mod in ("bot",):
            del sys.modules[mod]
    return importlib.import_module("bot")


@pytest.fixture(scope="module")
def bot_module():
    return _import_bot_clean()


def _make_service(bot, fake_session: _FakeSession, *, language_boost: str | None):
    """Build a SimpleMiniMaxTTSService wired to ``fake_session``.

    Skips Pipecat's pipeline ``start()`` lifecycle and instead sets the
    runtime-bound attributes (``_audio_sample_rate``) directly — the
    payload-build path doesn't need any pipeline framework state.
    """
    settings = bot.MiniMaxTTSSettings(
        model="speech-2.8-turbo",
        voice="Cantonese_GentleLady",
    )
    settings.language_boost = language_boost

    svc = bot.SimpleMiniMaxTTSService(
        api_key="test-api-key",
        group_id="",
        base_url="https://api.minimax.io/v1/t2a_v2",
        aiohttp_session=fake_session,
        sample_rate=24000,
        stream=True,
        settings=settings,
    )
    # ``start()`` would normally set this from ``self.sample_rate``; do it
    # by hand so we don't drag the full pipeline lifecycle into a unit test.
    svc._audio_sample_rate = 24000
    return svc


async def _drain_run_tts(svc, text: str) -> None:
    """Iterate ``run_tts`` to exhaustion (mock yields zero audio chunks)."""
    async for _frame in svc.run_tts(text, context_id="ctx-test"):
        pass


# ---------------------------------------------------------------------------
# Tests — 4 boost variants
# ---------------------------------------------------------------------------


def test_cantonese_injects_pronunciation_dict(bot_module):
    fake = _FakeSession()
    svc = _make_service(bot_module, fake, language_boost="Chinese,Yue")

    asyncio.run(_drain_run_tts(svc, "讀返一次"))

    assert fake.last_payload is not None, "post() was never called"
    assert fake.last_payload.get("language_boost") == "Chinese,Yue"
    assert fake.last_payload.get("pronunciation_dict") == {
        "tone": ["返/(faan1)"],
    }


def test_mandarin_no_injection(bot_module):
    fake = _FakeSession()
    svc = _make_service(bot_module, fake, language_boost="Chinese")

    asyncio.run(_drain_run_tts(svc, "你好"))

    assert fake.last_payload is not None
    assert fake.last_payload.get("language_boost") == "Chinese"
    assert "pronunciation_dict" not in fake.last_payload, (
        "pronunciation_dict must NOT be injected for non-Cantonese boosts"
    )


def test_english_no_injection(bot_module):
    fake = _FakeSession()
    svc = _make_service(bot_module, fake, language_boost="English")

    asyncio.run(_drain_run_tts(svc, "hello world"))

    assert fake.last_payload is not None
    assert fake.last_payload.get("language_boost") == "English"
    assert "pronunciation_dict" not in fake.last_payload


def test_no_language_boost_no_injection(bot_module):
    fake = _FakeSession()
    svc = _make_service(bot_module, fake, language_boost=None)

    asyncio.run(_drain_run_tts(svc, "neutral text"))

    assert fake.last_payload is not None
    assert "language_boost" not in fake.last_payload
    assert "pronunciation_dict" not in fake.last_payload
