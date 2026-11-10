"""Tests for demo_store.DemoStore (T3) — Cosmos-backed demo store that preserves
DemoLoader's interface.

Coverage:
  - put -> rescan -> get/list reads back the SAME dict, including the
    ``tool_ids`` key (NOT ``tools``), kb_chars summary, and a number
    round-trip for filler.probability.
  - rescan() reads the whole container into the in-memory cache.
  - Container-missing degrades: get()->None, list()->[], last_skipped recorded,
    no raise.
  - The pure normalization helper (demo_loader.normalize_demo_dict) yields a
    dict whose tool list key is ``tool_ids``.

Storage is the in-memory Cosmos fake (tests/fake_cosmos.py) injected through
the storage.cosmos provider seam; the missing-container degrade path runs with
a provider that raises ContainerNotFound.
"""

from __future__ import annotations

import asyncio

import pytest

from demo_loader import normalize_demo_dict
from demo_store import DemoStore, _from_ddb
from fake_cosmos import FakeCosmos
from storage import cosmos
from storage.cosmos import ContainerNotFound


# A demo dict that exercises every key shape DemoLoader.get produces:
# per-lang system/greeting/kb_intro/kb_ack maps, kb_body as a per-lang map,
# tool_ids (NOT tools), mcp_servers, a filler block with a FLOAT probability,
# and tags.
SAMPLE_DEMO = {
    "id": "acme-demo",
    "label": "Acme Demo",
    "lang": "zh-HK",
    "system": {"zh-HK": "你係客服", "en-US": "you are support"},
    "greeting": {"zh-HK": "你好", "en-US": "hello"},
    "kb_intro": {"zh-HK": "以下係資料", "en-US": "here is info"},
    "kb_ack": {"zh-HK": "明白", "en-US": "got it"},
    "kb_body": {"zh-HK": "知識庫內容" * 10, "en-US": "kb body here" * 10},
    "tool_ids": ["end_call"],
    "mcp_servers": ["weather-mcp"],
    "filler": {"enabled": True, "timeout_ms": 1200, "probability": 0.35},
    "tags": ["support", "demo"],
}


@pytest.fixture
def fake():
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _missing_provider(name):
    """Simulates absent containers: every op raises ContainerNotFound."""
    raise ContainerNotFound(name)


# ---------------------------------------------------------------------------
# Pure helper — runs WITHOUT any storage.
# ---------------------------------------------------------------------------

def test_normalize_helper_produces_tool_ids_key():
    """The shared normalizer's tool-list key is `tool_ids`, never `tools`,
    regardless of whether the raw dict used `tool_ids` or `tools`."""
    out = normalize_demo_dict(SAMPLE_DEMO, validate_tools=False)
    assert "tool_ids" in out
    assert "tools" not in out
    assert out["tool_ids"] == ["end_call"]

    # Raw manifest style (key `tools`) is also accepted and normalized to
    # `tool_ids`.
    raw = dict(SAMPLE_DEMO)
    raw.pop("tool_ids")
    raw["tools"] = ["end_call", "transfer_to_human"]
    out2 = normalize_demo_dict(raw, validate_tools=False)
    assert "tools" not in out2
    assert out2["tool_ids"] == ["end_call", "transfer_to_human"]


def test_from_ddb_decimal_conversion():
    """_from_ddb turns Decimals into int (integral) / float (fractional)."""
    from decimal import Decimal

    src = {"a": Decimal("0.35"), "b": Decimal("1200"), "c": [Decimal("0.5")]}
    out = _from_ddb(src)
    assert out["a"] == 0.35 and isinstance(out["a"], float)
    assert out["b"] == 1200 and isinstance(out["b"], int)
    assert isinstance(out["c"][0], float)


# ---------------------------------------------------------------------------
# Container-missing degrade — the point is that NO container exists.
# ---------------------------------------------------------------------------

def test_missing_table_degrades():
    """No container -> rescan degrades (no raise), get()->None, list()->[],
    last_skipped records the reason; cache stays empty."""
    async def run():
        store = DemoStore()
        n = await store.rescan()
        assert n == 0
        assert store.get("anything") is None
        assert store.list() == []
        assert store.last_skipped
        assert "missing" in store.last_skipped[0]["reason"]

    cosmos.set_container_provider(_missing_provider)
    try:
        asyncio.run(run())
    finally:
        cosmos.reset_container_provider()


# ---------------------------------------------------------------------------
# Empty-cache tolerance BEFORE any rescan (no prewarm) — pure, no storage.
# ---------------------------------------------------------------------------

def test_empty_cache_before_prewarm_is_safe():
    """get()/list() must not raise on a freshly constructed store (cache empty,
    rescan not yet awaited) — get()->None, list()->[]."""
    store = DemoStore()
    assert store.get("acme-demo") is None
    assert store.list() == []


# ---------------------------------------------------------------------------
# Full round-trip — put -> rescan -> get/list (requires the fake container).
# ---------------------------------------------------------------------------

def test_put_get_list_roundtrip_with_tool_ids_and_decimal(fake):
    async def run():
        store = DemoStore()
        await store.put(SAMPLE_DEMO)
        n = await store.rescan()
        assert n == 1

        # --- get(): full dict, byte-identical key shape to DemoLoader ---
        demo = store.get("acme-demo")
        assert demo is not None
        # CRITICAL: tool list key is `tool_ids`, not `tools`.
        assert "tool_ids" in demo
        assert "tools" not in demo
        assert demo["tool_ids"] == ["end_call"]
        # Canonical key set (same as demo_loader.normalize_demo_dict output).
        assert set(demo.keys()) == {
            "id", "label", "lang", "system", "greeting",
            "kb_intro", "kb_ack", "kb_body", "tool_ids",
            "mcp_servers", "filler", "tags",
        }
        # Per-lang maps survive.
        assert demo["system"]["zh-HK"] == "你係客服"
        assert demo["greeting"]["en-US"] == "hello"
        assert demo["kb_intro"]["zh-HK"] == "以下係資料"
        assert demo["kb_ack"]["en-US"] == "got it"
        assert isinstance(demo["kb_body"], dict)
        assert demo["mcp_servers"] == ["weather-mcp"]
        assert demo["tags"] == ["support", "demo"]

        # --- number round-trip: filler.probability is a float ---
        assert demo["filler"]["probability"] == 0.35
        assert isinstance(demo["filler"]["probability"], float)
        # timeout_ms (integral) comes back as int, not Decimal.
        assert demo["filler"]["timeout_ms"] == 1200
        assert isinstance(demo["filler"]["timeout_ms"], int)
        assert demo["filler"]["enabled"] is True

        # --- list(): summary shape {id,label,lang,kb_chars} ---
        summaries = store.list()
        assert len(summaries) == 1
        s = summaries[0]
        assert set(s.keys()) == {"id", "label", "lang", "kb_chars"}
        assert s["id"] == "acme-demo"
        assert s["label"] == "Acme Demo"
        assert s["lang"] == "zh-HK"
        # kb_chars = sum over the per-lang kb_body variants.
        expected_kb = len(SAMPLE_DEMO["kb_body"]["zh-HK"]) + len(
            SAMPLE_DEMO["kb_body"]["en-US"]
        )
        assert s["kb_chars"] == expected_kb

    asyncio.run(run())


def test_rescan_scans_all_items(fake):
    """rescan loads every demo in the container into the cache."""
    async def run():
        store = DemoStore()
        for i in range(5):
            d = dict(SAMPLE_DEMO)
            d["id"] = f"demo-{i}"
            d["label"] = f"Demo {i}"
            await store.put(d)
        n = await store.rescan()
        assert n == 5
        ids = sorted(x["id"] for x in store.list())
        assert ids == [f"demo-{i}" for i in range(5)]
        for i in range(5):
            assert store.get(f"demo-{i}") is not None

    asyncio.run(run())


def test_rescan_degrade_keeps_prior_cache(fake):
    """If a later rescan hits missing storage, the prior cache is KEPT (not
    wiped) and the previous count is returned."""
    async def run():
        store = DemoStore()
        await store.put(SAMPLE_DEMO)
        assert await store.rescan() == 1
        # Point the store at missing storage and rescan again.
        cosmos.set_container_provider(_missing_provider)
        n = await store.rescan()
        assert n == 1  # prior count preserved
        assert store.get("acme-demo") is not None  # cache intact
        assert store.last_skipped

    asyncio.run(run())
