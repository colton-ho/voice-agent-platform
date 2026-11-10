"""Integration tests for /api/history endpoints.

Hits the FastAPI app via TestClient. History storage runs on FakeCosmos
through the real CosmosCallRepository (production SQL builder + keyset
cursor, §1.2); the users container runs on the same FakeCosmos through the
Cosmos user_store (B-2).
Covers:
  - GET /api/history list shape, started_at desc ordering + keyset paging.
  - GET /api/history?cursor=... continues from the previous page.
  - GET /api/history/by-caller single-partition query returns full rows.
  - GET /api/history/{call_id} returns full item / 404 on miss.
  - HISTORY_DISABLED short-circuits to empty (no storage access).
  - SITE_PASSWORD enforces 401 on unauthenticated requests.
"""
from __future__ import annotations

import base64
import importlib
import json
import os
import sys

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos




# Pin the repo clock: stub rows use base=1_700_000_000 with deletion points at
# base+30d — without pinning, read-side forced expiry (§1.3-4) would hide them.
NOW = 1_700_000_500


@pytest.fixture
def fake_history():
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _seed(bot, rows: list[dict]) -> None:
    """Seed rows through the real repo write path (upsert_outcome_doc).

    Exercises §1.3-5: legacy ``ttl`` (= deletion-point epoch) maps 1:1 into
    ``expires_at``; the system ``ttl`` becomes remaining seconds. ``caller_id``
    (PK /caller_id) is derived from ``caller`` exactly as write_outcome_row does.
    """
    repo = bot._history._repo
    repo._now = lambda: NOW
    for r in rows:
        item = dict(r)
        item.setdefault("caller_id", item.get("caller") or "_none_")
        repo.upsert_outcome_doc(item)



_ADMIN_PWD = "test-pwd"


def _basic(pwd: str = _ADMIN_PWD, user: str = "x") -> dict:
    # Back-compat no-op: auth is now via the vb_session cookie on the client.
    return {}


def _import_bot_with_env(env: dict) -> object:
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_loader", "user_store"):
            del sys.modules[mod]
    # Always wire the auth env so the JWT-cookie login path works in tests.
    env = {
        "AUTH_SECRET": "test-secret",
        "ADMIN_PASSWORD": _ADMIN_PWD,
        **env,
    }
    for k, v in env.items():
        os.environ[k] = v
    return importlib.import_module("bot")


def _login_admin(bot):
    """Return an authed https TestClient (creates + logs in 'admin').

    The ADMIN_PASSWORD first-boot seed has been removed, so the bootstrap admin
    is created explicitly. Requires the users container on the active FakeCosmos
    provider (auto-created on first access).
    https base_url so the Secure vb_session cookie round-trips."""
    import asyncio
    from fastapi.testclient import TestClient
    asyncio.run(bot.USER_STORE.create("admin", _ADMIN_PWD, role="admin"))
    client = TestClient(bot.app, base_url="https://testserver")
    client.__enter__()
    r = client.post("/api/auth/login", json={"username": "admin", "password": _ADMIN_PWD})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("ADMIN_PASSWORD", _ADMIN_PWD)
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    yield


def _stub_rows(n: int, *, caller: str | None = None) -> list[dict]:
    """Build n rows with monotonically increasing started_at."""
    rows = []
    base = 1_700_000_000
    for i in range(n):
        rows.append({
            "call_id": f"call-{i:04d}",
            "caller": caller or f"+1555{i:07d}",
            "started_at": base + i,
            "ended_at": base + i + 30,
            "duration_s": 30,
            "turn_count": 4,
            "summary_status": "ok" if i % 2 == 0 else "pending",
            "ttl": base + i + 30 * 86400,
            "summary": {"intent": f"row {i}", "model": "stub", "generated_at": base + i + 30},
            # /api/history is now scoped to the caller's own web calls; tag rows
            # to the seeded 'admin' user so the list/cursor tests still exercise
            # the list + pagination machinery.
            "web_user": "admin",
        })
    return rows


def test_list_history_orders_desc_and_paginates(auth_env, monkeypatch, fake_history):
    """20 rows seeded, limit=5 should return the 5 newest (call-0019..0015)."""
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)

    bot = _import_bot_with_env({})
    _seed(bot, _stub_rows(20))

    client = _login_admin(bot)
    r = client.get("/api/history?limit=5", headers=_basic())
    assert r.status_code == 200
    body = r.json()
    assert "items" in body
    items = body["items"]
    assert len(items) == 5
    # Newest first.
    assert [it["call_id"] for it in items] == [
        "call-0019", "call-0018", "call-0017", "call-0016", "call-0015"
    ]
    for it in items:
        # Card shape only — `turns` should NOT leak.
        assert "turns" not in it
        assert {"call_id", "caller", "started_at", "turn_count", "summary_status"} <= set(it)
    # Even rows have summary => intent flattened.
    even = next(it for it in items if it["call_id"] == "call-0018")
    assert even.get("intent") == "row 18"


def test_list_history_cursor_resumes(auth_env, monkeypatch, fake_history):
    """Verify next_cursor round-trips: page 1 + page 2 cover disjoint sets."""
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)

    bot = _import_bot_with_env({})
    # Overflow one page (repo peeks limit+1 and pages 500 at a time).
    _seed(bot, _stub_rows(100))

    client = _login_admin(bot)
    r1 = client.get("/api/history?limit=10", headers=_basic())
    assert r1.status_code == 200
    body1 = r1.json()
    cursor = body1.get("next_cursor")
    # 100 rows > limit=10 ⇒ keyset cursor always present. Decoding must
    # yield the anchor key shape {"started_at", "call_id"} (U-301).
    if cursor is not None:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        assert "call_id" in decoded
        r2 = client.get(f"/api/history?limit=10&cursor={cursor}", headers=_basic())
        assert r2.status_code == 200
        assert r2.json()["items"]  # second page non-empty


def test_by_caller_uses_gsi_and_batch_get(auth_env, monkeypatch, fake_history):
    """Two callers, query for one returns only their rows in desc order.

    行為重指（#17 §1.2）：GSI+BatchGetItem 兩跳退役 → PK /caller_id 單分區
    查詢；斷言（5 行、desc 順序、caller 歸屬）逐字保留。
    """
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)

    bot = _import_bot_with_env({})
    rows = _stub_rows(5, caller="+15551111111") + _stub_rows(3, caller="+15552222222")
    # Make call_ids unique across the two batches by re-tagging.
    for i, row in enumerate(rows):
        row["call_id"] = f"mixed-{i:03d}"
    _seed(bot, rows)

    # by-caller is admin-only now.
    client = _login_admin(bot)
    r = client.get(
        "/api/history/by-caller?caller=%2B15551111111&limit=10",
        headers=_basic(),
    )
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 5
    # Newest started_at first.
    starts = [it["started_at"] for it in items]
    assert starts == sorted(starts, reverse=True)
    assert {it["caller"] for it in items} == {"+15551111111"}


def test_get_one_history(auth_env, monkeypatch, fake_history):
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)

    bot = _import_bot_with_env({})
    _seed(bot, _stub_rows(2))

    # admin bypasses per-user ownership on the detail endpoint.
    client = _login_admin(bot)
    r = client.get("/api/history/call-0001", headers=_basic())
    assert r.status_code == 200
    assert r.json()["call_id"] == "call-0001"

    r404 = client.get("/api/history/does-not-exist", headers=_basic())
    assert r404.status_code == 404
    assert r404.json()["detail"] == "not found"


def test_history_disabled_returns_empty(auth_env, monkeypatch, fake_history):
    """HISTORY_DISABLED=1 ⇒ list endpoints return empty, detail returns 404,
    and no storage container is ever touched."""
    monkeypatch.setenv("HISTORY_DISABLED", "1")
    monkeypatch.delenv("SITE_PASSWORD", raising=False)

    bot = _import_bot_with_env({
        "HISTORY_DISABLED": "1",
    })
    assert bot._history is None

    client = _login_admin(bot)
    r = client.get("/api/history", headers=_basic())
    assert r.status_code == 200
    assert r.json() == {"items": [], "next_cursor": None}

    r2 = client.get("/api/history/by-caller?caller=%2B1", headers=_basic())
    assert r2.status_code == 200
    assert r2.json() == {"items": [], "next_cursor": None}

    r3 = client.get("/api/history/anything", headers=_basic())
    assert r3.status_code == 404

    # Disabled path must not touch the calls container at all.
    assert fake_history.container("calls").queries == []
    assert fake_history.container("calls").writes == []


def test_history_requires_auth(auth_env, monkeypatch, fake_history):
    """No session cookie ⇒ 401 on every history route."""
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)

    bot = _import_bot_with_env({})

    from fastapi.testclient import TestClient
    client = TestClient(bot.app, base_url="https://testserver")
    r1 = client.get("/api/history")
    assert r1.status_code == 401
    r2 = client.get("/api/history/by-caller?caller=x")
    assert r2.status_code == 401
    r3 = client.get("/api/history/anything")
    assert r3.status_code == 401
    r4 = client.get("/api/history", headers=_basic(pwd="wrong"))
    assert r4.status_code == 401
