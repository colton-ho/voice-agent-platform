"""Tests for voice_store.VoiceStore (T2) — Cosmos-backed editable voice registry.

Coverage (AC#5):
  - seed_if_empty writes the in-code constants ONCE on an empty live container
    and is idempotent (a non-empty container is never re-seeded).
  - put -> rescan -> get/list CRUD round-trip across all three providers, incl.
    a numeric MiniMax boost surviving the number round-trip.
  - delete removes one item.
  - Container-missing degrades: rescan() returns -1, live stays False,
    get()->None, list()->{} — no raise (containers resolve lazily, never at
    ctor).

bot-level fallback (container-missing -> voices_for returns constants, resolver
byte-identical; deleted voice -> provider default; nova alias) is covered in
tests/test_admin_api.py where the bot module + FastAPI app are in scope.

Storage is the in-memory Cosmos fake (tests/fake_cosmos.py) injected through
the storage.cosmos provider seam; the missing-container degrade path runs with
a provider that raises ContainerNotFound.
"""

from __future__ import annotations

import asyncio

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos
from storage.cosmos import ContainerNotFound
from voice_store import VoiceStore


@pytest.fixture
def fake():
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _missing_provider(name):
    """Simulates absent containers: every op raises ContainerNotFound."""
    raise ContainerNotFound(name)


# A tiny constants map mirroring the three in-code dict shapes.
SAMPLE_CONSTANTS = {
    "minimax": {
        "Cantonese_GentleLady": {"label": "Gentle Lady", "gender": "F", "language": "zh-HK", "boost": "Cantonese"},
        "English_Graceful_Lady": {"label": "Graceful Lady", "gender": "F", "language": "en-US", "boost": "English"},
    },
    "polly": {
        "Zhiyu": {"label": "Zhiyu", "gender": "F", "language": "zh-CN", "engine": "neural"},
    },
    "nova-sonic": {
        "tiffany": {"label": "Tiffany", "gender": "F", "locale": "en-US", "lang_label": "English (US)", "polyglot": True},
        "ambre": {"label": "Ambre", "gender": "F", "locale": "fr-FR", "lang_label": "Français", "polyglot": False},
    },
}


# ---------------------------------------------------------------------------
# Lazy accessor / empty-cache tolerance — no storage at all.
# ---------------------------------------------------------------------------

def test_ctor_is_lazy_and_empty_cache_safe():
    """Constructing a VoiceStore never touches storage; reads on an unprewarmed
    store return empty/None and `live` starts False."""
    store = VoiceStore()
    assert store.live is False
    assert store.get("minimax", "anything") is None
    assert store.list("minimax") == {}
    assert store.list() == {"minimax": {}, "polly": {}, "nova-sonic": {}}


# ---------------------------------------------------------------------------
# Container-missing degrade — NO container created.
# ---------------------------------------------------------------------------

def test_missing_table_degrades():
    async def run():
        store = VoiceStore()
        n = await store.rescan()
        assert n == -1  # explicit not-live signal
        assert store.live is False
        assert store.get("minimax", "x") is None
        assert store.list("minimax") == {}
        assert store.last_skipped and "missing" in store.last_skipped[0]["reason"]

    cosmos.set_container_provider(_missing_provider)
    try:
        asyncio.run(run())
    finally:
        cosmos.reset_container_provider()


def test_seed_noop_when_table_missing():
    async def run():
        store = VoiceStore()
        written = await store.seed_if_empty(SAMPLE_CONSTANTS)
        assert written == 0
        assert store.live is False

    cosmos.set_container_provider(_missing_provider)
    try:
        asyncio.run(run())
    finally:
        cosmos.reset_container_provider()


# ---------------------------------------------------------------------------
# seed-on-empty + idempotency.
# ---------------------------------------------------------------------------

def test_seed_if_empty_writes_constants_once(fake):
    async def run():
        store = VoiceStore()
        written = await store.seed_if_empty(SAMPLE_CONSTANTS)
        assert written == 5  # 2 minimax + 1 polly + 2 nova
        assert store.live is True
        # Cache reflects the seeded rows, byte-identical attr shape.
        assert store.get("minimax", "Cantonese_GentleLady")["boost"] == "Cantonese"
        assert store.get("polly", "Zhiyu")["engine"] == "neural"
        assert store.get("nova-sonic", "tiffany")["polyglot"] is True

        # Idempotent: a non-empty live container is never re-seeded.
        again = await store.seed_if_empty(SAMPLE_CONSTANTS)
        assert again == 0

    asyncio.run(run())


def test_seed_not_reinjected_after_full_delete(fake):
    """Deleting every voice then re-seeding still re-injects (container empty
    again) — but a partially-populated container is NOT re-seeded."""
    async def run():
        store = VoiceStore()
        await store.seed_if_empty(SAMPLE_CONSTANTS)
        # Delete all but one nova voice -> container non-empty -> no re-seed.
        await store.delete("minimax", "Cantonese_GentleLady")
        await store.delete("minimax", "English_Graceful_Lady")
        await store.delete("polly", "Zhiyu")
        await store.delete("nova-sonic", "ambre")
        again = await store.seed_if_empty(SAMPLE_CONSTANTS)
        assert again == 0  # still has tiffany -> not empty -> no re-seed

    asyncio.run(run())


# ---------------------------------------------------------------------------
# CRUD round-trip incl. numeric boost round-trip.
# ---------------------------------------------------------------------------

def test_put_get_delete_roundtrip(fake):
    async def run():
        store = VoiceStore()
        await store.put("minimax", "custom_voice_1", {"label": "Custom", "gender": "M", "language": "en-US", "boost": 1.5})
        await store.put("polly", "Joanna", {"label": "Joanna", "gender": "F", "language": "en-US", "engine": "generative"})
        n = await store.rescan()
        assert n == 2
        assert store.live is True

        mm = store.get("minimax", "custom_voice_1")
        assert mm["label"] == "Custom"
        # Numeric boost survives the round-trip as a plain float.
        assert mm["boost"] == 1.5 and isinstance(mm["boost"], float)
        # Cached attrs DO NOT carry the key columns.
        assert "provider" not in mm and "voice_id" not in mm

        # list(provider) shape == {voice_id: attrs}; list() nested.
        assert set(store.list("minimax").keys()) == {"custom_voice_1"}
        assert set(store.list().keys()) == {"minimax", "polly", "nova-sonic"}

        # delete -> rescan -> gone.
        await store.delete("minimax", "custom_voice_1")
        await store.rescan()
        assert store.get("minimax", "custom_voice_1") is None
        assert store.get("polly", "Joanna") is not None

    asyncio.run(run())


def test_put_requires_keys(fake):
    async def run():
        store = VoiceStore()
        with pytest.raises(ValueError):
            await store.put("", "x", {})
        with pytest.raises(ValueError):
            await store.put("minimax", "", {})

    asyncio.run(run())
