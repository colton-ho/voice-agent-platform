"""Unit tests for activity_store.ActivityStore (T3 / Part B) — Cosmos-backed
operations audit log + DEFAULT-DENY redaction.

Coverage (AC#1, #2, #5):
  - log() writes a row with correct day (UTC YYYY-MM-DD) / ts_id
    (<epoch_ms>#<uuid>) / ttl (now + ttl_days*86400).
  - DEFAULT-DENY redaction (round-1 BLOCKER fix): _sanitize_detail masks an
    ODDLY-NAMED credential field (one NOT matching any secret substring) to its
    NAME ONLY ("***changed***"), never its value — the allowlist is the
    primary defence, not the substring scrub. Unknown type masks all values.
  - query() filters by actor/type + date range, newest-first, paginates.
  - Container-missing: log() is a silent no-op AND a simulated underlying op
    runs unaffected; query() -> empty. No raise.
  - Lazy container: constructing an ActivityStore against absent storage never
    raises (containers resolve on first USE, not in __init__).

Storage is the in-memory Cosmos fake (tests/fake_cosmos.py) injected through
the storage.cosmos provider seam. Best-effort/lazy paths run with a provider
that raises ContainerNotFound (missing-container semantics).
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

import activity_store
from activity_store import (
    ActivityStore,
    _sanitize_detail,
    ACTIVITY_DETAIL_VALUE_ALLOWLIST,
)
from fake_cosmos import FakeCosmos
from storage import cosmos
from storage.cosmos import ContainerNotFound

_MASK = "***changed***"


@pytest.fixture
def fake():
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _missing_provider(name):
    """Simulates absent containers: every op raises ContainerNotFound."""
    raise ContainerNotFound(name)


# --------------------------------------------------------------------------
# REDACTION — the BLOCKER-fix proof (no storage needed — pure function)
# --------------------------------------------------------------------------

def test_sanitize_masks_oddly_named_credential_to_name_only():
    """An mcp 'url'/'headers'/'auth' value — keys that do NOT match the secret
    substring scrub — must be stored as the FIELD NAME ONLY, never the raw
    value. This is the default-deny allowlist proof (round-1 BLOCKER)."""
    detail = {
        "id": "srv1",
        "label": "My Server",
        "transport": "streamable_http",
        "enabled": True,
        # Credential-bearing fields whose KEYS do not match password|token|...
        "url": "https://secret-host.internal/mcp?session=abcdef",
        "headers": {"X-Custom": "raw-bearer-value-xyz"},
        "auth": {"type": "sigv4", "service": "bedrock"},
    }
    out = _sanitize_detail("mcp-upsert", detail)
    # Allowlisted fields survive by value.
    assert out["id"] == "srv1"
    assert out["label"] == "My Server"
    assert out["transport"] == "streamable_http"
    assert out["enabled"] is True
    # Non-allowlisted credential-bearing fields → NAME ONLY, value masked.
    assert out["url"] == _MASK
    assert out["headers"] == _MASK
    assert out["auth"] == _MASK
    # The raw secret never appears anywhere in the serialized detail.
    assert "secret-host.internal" not in repr(out)
    assert "raw-bearer-value-xyz" not in repr(out)


def test_sanitize_user_never_stores_password_value():
    """user-* allowlist = {role, disabled}; a password (even under the literal
    'password' key) is masked. The allowlist is what protects us — confirm the
    value is gone."""
    out = _sanitize_detail("user-update", {"role": "admin", "password": "hunter2", "disabled": False})
    assert out["role"] == "admin"
    assert out["disabled"] is False
    assert out["password"] == _MASK
    assert "hunter2" not in repr(out)


def test_sanitize_unknown_type_masks_all_values():
    """An unknown activity type has no allowlist → ALL values masked
    (field-names preserved)."""
    out = _sanitize_detail("totally-unknown-type", {"a": "1", "b": "2"})
    assert out == {"a": _MASK, "b": _MASK}


def test_sanitize_config_allowlist_value_through_residual_scrub():
    """A config field whose KEY accidentally matches the residual scrub list
    but is allowlisted by value still survives the allowlist, then the residual
    scrub runs last as belt-and-suspenders. Confirm an allowlisted non-secret
    survives and a non-allowlisted secret-keyed field is name-only."""
    out = _sanitize_detail("config-web", {"engine": "pipeline", "api_key": "sk-xxxx"})
    assert out["engine"] == "pipeline"
    # api_key is NOT in config-* allowlist → masked by default-deny (and would
    # also be caught by the residual scrub).
    assert out["api_key"] == _MASK
    assert "sk-xxxx" not in repr(out)


def test_allowlist_excludes_secrets_by_construction():
    """Defence-in-depth contract: no secret-bearing field name is in any
    type's allowlist."""
    forbidden = {"password", "password_hash", "url", "headers", "auth", "token", "api_key", "secret"}
    for atype, allow in ACTIVITY_DETAIL_VALUE_ALLOWLIST.items():
        assert not (allow & forbidden), f"{atype} allowlist leaks {allow & forbidden}"


# --------------------------------------------------------------------------
# LAZY CONTAINER — constructing against absent storage must never raise
# --------------------------------------------------------------------------

def test_lazy_table_ctor_never_raises_on_absent_table():
    """Constructing an ActivityStore when the container does NOT exist must not
    touch storage / raise — containers resolve on first use (N3)."""
    touched: list[str] = []
    f = FakeCosmos()

    def provider(name):
        touched.append(name)
        return f.container(name)

    cosmos.set_container_provider(provider)
    try:
        store = ActivityStore()
        assert touched == []  # container not resolved eagerly
    finally:
        cosmos.reset_container_provider()


# --------------------------------------------------------------------------
# log() — row shape + ttl
# --------------------------------------------------------------------------

def test_log_writes_row_with_day_ts_id_ttl(fake):
    store = ActivityStore(ttl_days=90)
    t0 = time.time()
    asyncio.run(store.log(
        actor="admin", actor_role="admin", type="login",
        target="admin", detail={"username": "admin"},
    ))
    out = asyncio.run(store.query())
    assert len(out["items"]) == 1
    row = out["items"][0]
    # day = UTC YYYY-MM-DD
    assert len(row["day"]) == 10 and row["day"][4] == "-" and row["day"][7] == "-"
    # ts_id = <epoch_ms>#<uuid-hex>
    assert "#" in row["ts_id"]
    epoch_ms, uid = row["ts_id"].split("#", 1)
    assert epoch_ms.isdigit()
    uuid.UUID(hex=uid)  # parses → valid uuid hex
    # ttl ~= now + 90 days (allow a few seconds of slack)
    expected = int(t0) + 90 * 86400
    assert abs(int(row["ttl"]) - expected) < 10
    assert row["actor"] == "admin"
    assert row["type"] == "login"
    assert row["status"] == "success"
    assert row["detail"] == {"username": "admin"}
    # §1.3-2 raw write shape: expires_at=业务删除点；ttl=剩余秒（Cosmos TTL
    # 按最后修改起算，读侧再曝光 ttl=expires_at 保持外部形狀）
    raw = next(iter(fake.container("activity").docs.values()))
    assert abs(raw["expires_at"] - expected) < 10
    assert raw["ttl"] == 90 * 86400
    assert raw["id"] == raw["ts_id"]


def test_log_sanitizes_detail_at_store_layer(fake):
    """Even a careless caller passing a raw credential value is sanitized by the
    store-side backstop before the row is written."""
    store = ActivityStore()
    asyncio.run(store.log(
        actor="admin", type="mcp-upsert", target="srv1",
        detail={"id": "srv1", "url": "https://secret/x", "headers": {"A": "tok"}},
    ))
    row = asyncio.run(store.query())["items"][0]
    assert row["detail"]["id"] == "srv1"
    assert row["detail"]["url"] == _MASK
    assert row["detail"]["headers"] == _MASK
    assert "secret" not in repr(row["detail"])


# --------------------------------------------------------------------------
# CONTAINER-MISSING — best-effort no-op, underlying op unaffected
# --------------------------------------------------------------------------

def test_log_table_missing_is_silent_noop_and_op_unaffected():
    """log() against absent containers swallows the error (no raise), and a
    simulated underlying operation runs to completion regardless."""
    cosmos.set_container_provider(_missing_provider)
    try:
        store = ActivityStore()
        op_ran = {"done": False}

        async def underlying_op():
            # The audit log is best-effort: even if it explodes internally, the
            # op must proceed. log() returns None and never raises.
            await store.log(actor="admin", type="login", detail={"username": "admin"})
            op_ran["done"] = True
            return "ok"

        result = asyncio.run(underlying_op())
        assert result == "ok"
        assert op_ran["done"] is True
    finally:
        cosmos.reset_container_provider()


def test_query_table_missing_returns_empty():
    cosmos.set_container_provider(_missing_provider)
    try:
        store = ActivityStore()  # no container
        out = asyncio.run(store.query())
        assert out == {"items": [], "cursor": None}
    finally:
        cosmos.reset_container_provider()


# --------------------------------------------------------------------------
# query() — filter + newest-first + paginate
# --------------------------------------------------------------------------

def test_query_filters_and_orders_newest_first(fake):
    store = ActivityStore()
    # Write several rows on today's partition; spacing the epoch_ms so
    # ts_id ordering is deterministic.
    for i in range(5):
        asyncio.run(store.log(
            actor=("alice" if i % 2 == 0 else "bob"),
            type=("login" if i < 3 else "logout"),
            target=f"t{i}",
            detail={"username": "alice" if i % 2 == 0 else "bob"},
        ))
        time.sleep(0.005)  # ensure distinct epoch_ms

    # No filter: all 5, newest-first (descending ts).
    out = asyncio.run(store.query())
    items = out["items"]
    assert len(items) == 5
    ts_list = [it["ts"] for it in items]
    assert ts_list == sorted(ts_list, reverse=True), "not newest-first"

    # Filter by actor.
    alice = asyncio.run(store.query(actor="alice"))
    assert {it["actor"] for it in alice["items"]} == {"alice"}
    assert len(alice["items"]) == 3  # i=0,2,4

    # Filter by type.
    logout = asyncio.run(store.query(type="logout"))
    assert {it["type"] for it in logout["items"]} == {"logout"}
    assert len(logout["items"]) == 2  # i=3,4


def test_query_paginates_via_cursor(fake):
    store = ActivityStore()
    for i in range(5):
        asyncio.run(store.log(actor="admin", type="login", target=f"t{i}",
                              detail={"username": "admin"}))
        time.sleep(0.005)

    page1 = asyncio.run(store.query(limit=2))
    assert len(page1["items"]) == 2
    assert page1["cursor"] is not None

    page2 = asyncio.run(store.query(limit=2, cursor=page1["cursor"]))
    assert len(page2["items"]) == 2

    page3 = asyncio.run(store.query(limit=2, cursor=page2["cursor"]))
    assert len(page3["items"]) == 1
    assert page3["cursor"] is None

    # No row repeated across pages; full set covered; strictly descending.
    all_ts = [it["ts"] for it in page1["items"] + page2["items"] + page3["items"]]
    assert len(set(all_ts)) == 5
    assert all_ts == sorted(all_ts, reverse=True)


def test_query_date_range_excludes_out_of_window(fake):
    """A row whose day partition is outside [from,to] is not returned."""
    store = ActivityStore()
    asyncio.run(store.log(actor="admin", type="login", detail={"username": "admin"}))
    # Window entirely in the past (yesterday and before) → no today rows.
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone.utc)
    y = (today - timedelta(days=1)).strftime("%Y-%m-%d")
    past = (today - timedelta(days=5)).strftime("%Y-%m-%d")
    out = asyncio.run(store.query(day_from=past, day_to=y))
    assert out["items"] == []
    # Today included → row appears.
    td = today.strftime("%Y-%m-%d")
    out2 = asyncio.run(store.query(day_from=past, day_to=td))
    assert len(out2["items"]) == 1
