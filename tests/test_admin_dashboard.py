"""Tests for the admin call-history endpoints (T3).

Covers the 5 ``/api/admin/history*`` routes added in T3:

  GET    /api/admin/history                — cursor-paged list with filters
  GET    /api/admin/history/{call_id}      — full row incl. turns + summary
  GET    /api/admin/history/{call_id}/export.md  — server-rendered Markdown
  GET    /api/admin/history.csv            — streamed CSV (cap + truncation)
  POST   /api/admin/history/{call_id}/summarize — re-run Bedrock summary

Tests run in-process via :class:`fastapi.testclient.TestClient` and stub the
storage layer at the container-provider seam (``tests/fake_cosmos.py``) with
the real ``CosmosCallRepository`` on top, so the production SQL builder is
exercised and tests assert on the recorded query text. The Basic-Auth
middleware is exercised end-to-end so we also verify the routes don't
double-up with a per-route Depends.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos as _cosmos


def _import_bot_with_env(env: dict):
    for mod in list(sys.modules):
        if mod in ("bot", "runtime_config", "demo_loader"):
            del sys.modules[mod]
    for k, v in env.items():
        os.environ[k] = v
    return importlib.import_module("bot")


def _basic_auth_header(user: str = "admin", pwd: str = "test-pw") -> dict:
    # Auth is now JWT-cookie based; the admin_app fixture overrides the
    # require_user/require_admin deps so these tests don't need real login.
    # Kept as a no-op so existing call sites (headers=_basic_auth_header())
    # remain valid.
    return {}


def _override_auth(bot) -> None:
    """Bypass require_user/require_admin so dashboard tests focus on behaviour,
    not auth (auth is covered by test_auth.py / test_admin_api.py)."""
    admin_user = {"username": "admin", "role": "admin", "disabled": False, "created_at": 0}
    bot.app.dependency_overrides[bot.require_user] = lambda: admin_user
    bot.app.dependency_overrides[bot.require_admin] = lambda: admin_user


def _seed_call(fake: FakeCosmos, call_id: str, **fields) -> dict:
    """Seed one row into the calls container (PK /caller_id, §1.1).

    Only ``id``/``call_id``/``caller_id`` are mandatory; rows seeded without
    ``expires_at`` pass the read-side forced-expiry predicate (§1.3-4).
    """
    doc = {"id": call_id, "call_id": call_id, "caller_id": "_none_"}
    doc.update(fields)
    fake.seed("calls", doc)
    return doc


def _seed_turns(fake: FakeCosmos, call_id: str, turns: list) -> None:
    """Seed transcript rows (X1 split: text turns live in the transcripts
    container with deterministic ids ``call_id#t{seq}``)."""
    for i, t in enumerate(turns):
        row = dict(t)
        row.update({"id": f"{call_id}#t{i}", "call_id": call_id})
        fake.seed("transcripts", row)


@pytest.fixture
def admin_app(monkeypatch):
    """Boot bot.py with admin auth on + history enabled over FakeCosmos.

    The real CosmosCallRepository runs against the in-memory containers so
    tests exercise the production SQL builder; per-container query text is
    recorded for shape assertions.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)
    bot = _import_bot_with_env({
        "ADMIN_PASSWORD": "test-pw",
    })

    _override_auth(bot)
    fake = FakeCosmos()
    _cosmos.set_container_provider(fake)
    # Belt-and-braces: pin the gate open even if a leaked HISTORY_DISABLED
    # sneaks in from another test's env residue.
    monkeypatch.setattr(bot, "HISTORY_DISABLED", False)

    bot._fake_cosmos = fake  # smuggle the handle to the test
    yield bot
    _cosmos.reset_container_provider()


# --- AC #1: cursor pagination ------------------------------------------


def test_admin_history_pagination(admin_app):
    """3 rows, limit=2 → page-1 returns the 2 newest + cursor; page-2 returns
    the last one with next_cursor=null.

    行為重指（#17 CI-1）：排序改為全局 `started_at DESC, id ASC` keyset 分頁，
    舊 DDB Scan 的「頁內排序」語義退役——斷言值從 ["b","a"]/["c"] 改為
    ["c","b"]/["a"]。
    """
    from fastapi.testclient import TestClient

    fake = admin_app._fake_cosmos
    _seed_call(fake, "a", caller="+1", started_at=100, ended_at=200,
               duration_s=100, outcome="user_requested", engine="nova-sonic",
               scenario="it-helpdesk", lang="en-US", summary_status="ok",
               transfer_requested=False, turn_count=4)
    _seed_call(fake, "b", caller="+1", started_at=300, ended_at=400,
               duration_s=100, outcome="task_completed", engine="nova-sonic",
               scenario="it-helpdesk", lang="en-US", summary_status="ok",
               transfer_requested=False, turn_count=5)
    _seed_call(fake, "c", caller="+1", started_at=500, ended_at=600,
               duration_s=100, outcome="user_requested", engine="pipeline",
               scenario="sales", lang="en-US", summary_status="ok",
               transfer_requested=True, turn_count=6)

    client = TestClient(admin_app.app)

    r1 = client.get("/api/admin/history?limit=2", headers=_basic_auth_header())
    assert r1.status_code == 200, r1.text
    body1 = r1.json()
    assert [it["call_id"] for it in body1["items"]] == ["c", "b"]
    assert body1["next_cursor"] is not None

    r2 = client.get(
        f"/api/admin/history?limit=2&cursor={body1['next_cursor']}",
        headers=_basic_auth_header(),
    )
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert [it["call_id"] for it in body2["items"]] == ["a"]
    assert body2["next_cursor"] is None


# --- AC #2: filters (outcome + engine on cross-partition path) ----------


def test_admin_history_filters_outcome_and_engine_on_scan(admin_app):
    """outcome+engine combo must land in the WHERE clause as residual
    filters (cross-partition path, §1.2)."""
    from fastapi.testclient import TestClient

    calls = admin_app._fake_cosmos.container("calls")

    client = TestClient(admin_app.app)
    r = client.get(
        "/api/admin/history?outcome=user_requested&engine=nova-sonic",
        headers=_basic_auth_header(),
    )
    assert r.status_code == 200, r.text
    # exactly one query, cross-partition (no caller ⇒ partition_key None)
    assert len(calls.queries) == 1
    q = calls.queries[0]
    assert q["partition_key"] is None
    sql = q["query"]
    assert "c.outcome = @outcome" in sql
    assert "c.engine = @engine" in sql
    assert " AND " in sql
    params = {p["name"]: p["value"] for p in q["parameters"]}
    assert params["@outcome"] == "user_requested"
    assert params["@engine"] == "nova-sonic"


def test_admin_history_caller_uses_gsi_query(admin_app):
    """When ``caller`` is provided the query is single-partition (PK
    /caller_id, §1.2 caller path) — partition_key set, caller predicate in
    WHERE, CI-1 ORDER BY fixed."""
    from fastapi.testclient import TestClient

    calls = admin_app._fake_cosmos.container("calls")

    client = TestClient(admin_app.app)
    r = client.get(
        "/api/admin/history?caller=%2B14155551234",
        headers=_basic_auth_header(),
    )
    assert r.status_code == 200, r.text
    assert len(calls.queries) == 1
    q = calls.queries[0]
    assert q["partition_key"] == "+14155551234"
    sql = q["query"]
    assert "c.caller_id = @caller" in sql
    assert "ORDER BY c.started_at DESC, c.id ASC" in sql
    params = {p["name"]: p["value"] for p in q["parameters"]}
    assert params["@caller"] == "+14155551234"


def test_admin_history_demo_alias_maps_to_scenario(admin_app):
    """demo=foo must filter by the on-disk column ``scenario``."""
    from fastapi.testclient import TestClient

    calls = admin_app._fake_cosmos.container("calls")

    client = TestClient(admin_app.app)
    r = client.get(
        "/api/admin/history?demo=it-helpdesk",
        headers=_basic_auth_header(),
    )
    assert r.status_code == 200, r.text
    q = calls.queries[0]
    assert "c.scenario = @scenario" in q["query"]
    params = {p["name"]: p["value"] for p in q["parameters"]}
    assert params["@scenario"] == "it-helpdesk"


# --- AC #3: detail GET returns turns[] + summary -----------------------


def _call_doc(fake: FakeCosmos, call_id: str) -> dict:
    for d in fake.container("calls").docs.values():
        if d.get("id") == call_id:
            return d
    raise AssertionError(f"no calls doc for {call_id}")


def test_admin_history_detail_returns_turns_and_summary(admin_app):
    """GET /api/admin/history/{id} surfaces the full row including turns +
    summary. X1：turns 由 transcripts 行重拼，外部形狀不變。"""
    from fastapi.testclient import TestClient

    fake = admin_app._fake_cosmos
    _seed_call(fake, "abc", caller="+1", started_at=100, ended_at=200,
               duration_s=100, outcome="user_requested", engine="nova-sonic",
               scenario="it-helpdesk", lang="en-US", summary_status="ok",
               summary={"intent": "billing", "key_questions": ["q1"],
                        "sentiment": "neutral"},
               turn_count=2)
    _seed_turns(fake, "abc", [
        {"who": "bot", "text": "Hi", "t": 1},
        {"who": "user", "text": "I have a question", "t": 2},
    ])

    client = TestClient(admin_app.app)
    r = client.get("/api/admin/history/abc", headers=_basic_auth_header())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["call_id"] == "abc"
    assert isinstance(body["turns"], list) and len(body["turns"]) == 2
    assert body["turns"][0]["who"] == "bot"
    assert body["summary"]["intent"] == "billing"


def test_admin_history_detail_404_when_missing(admin_app):
    from fastapi.testclient import TestClient

    # no seeded rows → not found
    client = TestClient(admin_app.app)
    r = client.get("/api/admin/history/nope", headers=_basic_auth_header())
    assert r.status_code == 404


# --- AC #4: export.md headers + body -----------------------------------


def test_admin_history_export_md_shape(admin_app):
    from fastapi.testclient import TestClient

    fake = admin_app._fake_cosmos
    _seed_call(fake, "xyz-1", caller="+1234", started_at=1_716_000_000,
               ended_at=1_716_000_120, duration_s=120,
               outcome="user_requested", engine="nova-sonic",
               scenario="it-helpdesk", lang="zh-HK", summary_status="ok",
               summary={"intent": "reset password", "key_questions": ["q?"],
                        "sentiment": "neutral"},
               transfer_requested=False, transfer_topic="", turn_count=2)
    _seed_turns(fake, "xyz-1", [
        {"who": "bot", "text": "你好", "t": 1},
        {"who": "user", "text": "我想重設密碼", "t": 2},
    ])

    client = TestClient(admin_app.app)
    r = client.get("/api/admin/history/xyz-1/export.md", headers=_basic_auth_header())
    assert r.status_code == 200, r.text

    # Headers — Content-Disposition + filename present, content-type Markdown.
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd.lower()
    assert "xyz-1.md" in cd
    assert r.headers.get("content-type", "").startswith("text/markdown")

    body = r.text
    assert "## Summary" in body
    assert "## Transcript" in body
    # The structured summary must render gracefully (intent + key_questions
    # surface in the rendered Markdown).
    assert "reset password" in body
    # Both turns must appear, prefixed by their role.
    assert "**bot**" in body
    assert "**user**" in body


def test_admin_history_export_md_summary_not_available(admin_app):
    """If summary_status != 'ok' the export must say so explicitly rather
    than rendering an empty Summary section."""
    from fastapi.testclient import TestClient

    fake = admin_app._fake_cosmos
    _seed_call(fake, "pending-row", caller="+1", started_at=1_716_000_000,
               ended_at=1_716_000_010, duration_s=10,
               summary_status="pending")
    client = TestClient(admin_app.app)
    r = client.get(
        "/api/admin/history/pending-row/export.md",
        headers=_basic_auth_header(),
    )
    assert r.status_code == 200
    assert "[summary not available: pending]" in r.text


# --- AC #5: CSV truncation header --------------------------------------


def test_admin_history_csv_truncation_header(admin_app, monkeypatch):
    """5001 mocked rows → response body has 5000 data rows + the
    ``X-Truncated: 1`` header."""
    from fastapi.testclient import TestClient

    # Bypass _scan_history_for_csv with a generator that yields 5001 minimal
    # rows. This is far cheaper than actually pumping 5001 rows through the
    # FakeCosmos keyset loop.
    def fake_scan(**kwargs):
        for i in range(5001):
            yield {
                "call_id": f"c-{i}",
                "caller": "+1",
                "started_at": 1_716_000_000 + i,
                "ended_at": 1_716_000_000 + i + 30,
                "duration_s": 30,
                "outcome": "user_requested",
                "engine": "nova-sonic",
                "scenario": "it-helpdesk",
                "lang": "en-US",
                "summary_status": "ok",
                "transfer_requested": False,
                "transfer_topic": "",
                "turn_count": 1,
            }

    monkeypatch.setattr(admin_app, "_scan_history_for_csv", fake_scan)

    client = TestClient(admin_app.app)
    r = client.get("/api/admin/history.csv", headers=_basic_auth_header())
    assert r.status_code == 200, r.text
    assert r.headers.get("X-Truncated") == "1", dict(r.headers)
    # CSV body: 1 header line + 5000 data lines = 5001 lines.
    lines = [ln for ln in r.text.splitlines() if ln]
    assert len(lines) == 5001, f"expected 5001 non-empty lines, got {len(lines)}"
    # Header line first.
    assert lines[0].split(",")[0] == "call_id"


def test_admin_history_csv_no_truncation_under_cap(admin_app, monkeypatch):
    """3 rows → no X-Truncated header, header + 3 lines."""
    from fastapi.testclient import TestClient

    def fake_scan(**kwargs):
        for i in range(3):
            yield {
                "call_id": f"c-{i}",
                "caller": "+1",
                "started_at": 1_716_000_000,
                "duration_s": 30,
                "summary_status": "ok",
                "turn_count": 1,
            }

    monkeypatch.setattr(admin_app, "_scan_history_for_csv", fake_scan)

    client = TestClient(admin_app.app)
    r = client.get("/api/admin/history.csv", headers=_basic_auth_header())
    assert r.status_code == 200
    assert "X-Truncated" not in r.headers
    lines = [ln for ln in r.text.splitlines() if ln]
    # 1 header + 3 rows
    assert len(lines) == 4


# --- AC #6 (C-9 重指): summarize POST 受理异步化 -----------------------
#
# C-9（已裁决）：受理即回 200 {call_id, summary_status:"pending"}（summary
# 省略），行翻 pending、§2.2 job 信封投 postcall-jobs 队列；终态 ok/failed
# 归 worker（tests/test_postcall_worker.py），不在请求内同步 invoke。


def test_admin_history_summarize_c9_accepts_and_enqueues_job(admin_app, monkeypatch):
    """POST /api/admin/history/{id}/summarize（C-9 受理腿）:
      - 回 200 {call_id, summary_status:"pending"}，summary 省略
      - 行翻 summary_status='pending'（终态归 worker，不在请求内 invoke）
      - §2.2 job 信封投 postcall-jobs 队列（MessageId=job_id）
    """
    from fastapi.testclient import TestClient
    from fake_servicebus import FakeQueue
    from storage import servicebus as _sb

    fake = admin_app._fake_cosmos
    _seed_call(fake, "s1", lang="en-US", summary_status="failed",
               summary_error="stale-error")
    _seed_turns(fake, "s1", [
        {"who": "bot", "text": "Hello", "t": 1},
        {"who": "user", "text": "Reset password please", "t": 2},
    ])

    async def must_not_run(turns, lang, **_kw):
        raise AssertionError("C-9: summarize must not run inline in the request")

    monkeypatch.setattr(admin_app, "_invoke_summary_bedrock", must_not_run)

    q = FakeQueue("postcall-jobs")
    _sb.set_queue_provider(lambda _name: q)
    monkeypatch.setenv("SERVICEBUS_NAMESPACE", "test-ns")
    try:
        client = TestClient(admin_app.app)
        r = client.post("/api/admin/history/s1/summarize", headers=_basic_auth_header())
        assert r.status_code == 200, r.text
        assert r.json() == {"call_id": "s1", "summary_status": "pending"}

        # 受理即翻 pending；终态由 worker 落，此处不得已是 ok/failed。
        doc = _call_doc(fake, "s1")
        assert doc["summary_status"] == "pending"

        assert q.sent == [("summary:s1", {
            "job_type": "summary", "job_id": "summary:s1", "call_id": "s1",
        })]
    finally:
        _sb.reset_queue_provider()


def test_admin_history_summarize_c9_enqueue_failure_writes_failed_and_500(admin_app, monkeypatch):
    """C-9 入队失败腿：队列 send 抛错 → error-as-data 写 summary_status='failed'
    + summary_error（行不永卡 pending）后回 500 {"error"}。"""
    from fastapi.testclient import TestClient
    from fake_servicebus import FakeQueue
    from storage import servicebus as _sb

    class BrokenQueue(FakeQueue):
        def send(self, *, message_id, body):
            raise RuntimeError("queue unreachable")

    fake = admin_app._fake_cosmos
    _seed_call(fake, "s2", lang="en-US")

    _sb.set_queue_provider(lambda _name: BrokenQueue("postcall-jobs"))
    monkeypatch.setenv("SERVICEBUS_NAMESPACE", "test-ns")
    try:
        client = TestClient(admin_app.app)
        r = client.post("/api/admin/history/s2/summarize", headers=_basic_auth_header())
        assert r.status_code == 500
        body = r.json()
        assert "error" in body
        assert "queue unreachable" in body["error"]
        # 入队失败也必须落 failed 面包屑，行不得永卡 pending。
        doc = _call_doc(fake, "s2")
        assert doc["summary_status"] == "failed"
        assert "queue unreachable" in doc["summary_error"]
    finally:
        _sb.reset_queue_provider()


def test_admin_history_summarize_c9_inprocess_fallback_accepts_pending(admin_app, monkeypatch):
    """C-9 降级腿：未配置 SERVICEBUS_NAMESPACE → in-process 异步落终态，
    受理契约同形（200 {call_id, summary_status:"pending"} + 行翻 pending）。
    背景任务时序不在此钉（终态行为由 test_postcall_worker.py 覆盖）。"""
    from fastapi.testclient import TestClient

    fake = admin_app._fake_cosmos
    _seed_call(fake, "s3", lang="en-US")

    async def fake_update(call_id, turns, meta):
        return None

    monkeypatch.delenv("SERVICEBUS_NAMESPACE", raising=False)
    monkeypatch.setattr(admin_app._history, "_summarize_and_update", fake_update)

    client = TestClient(admin_app.app)
    r = client.post("/api/admin/history/s3/summarize", headers=_basic_auth_header())
    assert r.status_code == 200, r.text
    assert r.json() == {"call_id": "s3", "summary_status": "pending"}
    assert _call_doc(fake, "s3")["summary_status"] == "pending"


# --- AC #7: middleware-only auth (no per-route Depends) ----------------


def _route_dep_names(app, path: str) -> list[str]:
    """Return the callable names of a route's per-route dependencies."""
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return [
                getattr(d.call, "__name__", repr(d.call))
                for d in getattr(getattr(route, "dependant", None), "dependencies", [])
            ]
    return []


def test_admin_history_routes_have_require_admin_dependency(admin_app):
    """Every admin/history route must now carry a per-route
    ``Depends(require_admin)`` (the admin_path_guard middleware was removed when
    auth moved to JWT sessions)."""
    paths = {
        "/api/admin/history",
        "/api/admin/history/{call_id}",
        "/api/admin/history/{call_id}/export.md",
        "/api/admin/history.csv",
        "/api/admin/history/{call_id}/summarize",
    }
    registered = {getattr(r, "path", None) for r in admin_app.app.routes}
    for path in paths:
        assert path in registered, f"{path} route not registered"
        names = _route_dep_names(admin_app.app, path)
        assert "require_admin" in names, (
            f"{path} missing Depends(require_admin); got deps {names!r}"
        )


def test_admin_history_endpoints_require_auth(admin_app):
    """Without a session cookie, each admin/history route returns 401."""
    from fastapi.testclient import TestClient

    # Drop the auth-bypass overrides so the real require_admin runs.
    admin_app.app.dependency_overrides.clear()
    client = TestClient(admin_app.app, base_url="https://testserver")
    for path in (
        "/api/admin/history",
        "/api/admin/history/abc",
        "/api/admin/history/abc/export.md",
        "/api/admin/history.csv",
    ):
        r = client.get(path)
        assert r.status_code == 401, f"{path} expected 401, got {r.status_code}"
    r = client.post("/api/admin/history/abc/summarize")
    assert r.status_code == 401


# =======================================================================
# T1 — pure aggregation helpers + cache + scan
# T2 — /api/admin/metrics endpoint
# =======================================================================
#
# These tests target the metrics aggregation layer (T1) and the thin
# endpoint wrapper (T2). They were lost from disk between T2 submission
# and T3 merge; reconstructed here from the recovered test names plus
# the implementation in bot.py.

import asyncio as _asyncio


def _import_bot_for_metrics(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MINIMAX_API_KEY", "x")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-pw")
    monkeypatch.delenv("HISTORY_DISABLED", raising=False)
    return _import_bot_with_env(
        {"ADMIN_PASSWORD": "test-pw"}
    )


# --- _peak_concurrent --------------------------------------------------


def test_peak_concurrent_overlap(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    rows = [
        {"started_at": 100, "ended_at": 200},
        {"started_at": 150, "ended_at": 250},
    ]
    assert bot._peak_concurrent(rows) == 2


def test_peak_concurrent_nested(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    rows = [
        {"started_at": 100, "ended_at": 500},
        {"started_at": 200, "ended_at": 300},
    ]
    assert bot._peak_concurrent(rows) == 2


def test_peak_concurrent_three_way_nested(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    rows = [
        {"started_at": 100, "ended_at": 500},
        {"started_at": 150, "ended_at": 450},
        {"started_at": 200, "ended_at": 400},
    ]
    assert bot._peak_concurrent(rows) == 3


def test_peak_concurrent_same_second_boundary(monkeypatch):
    """Call A ends at t=200 while call B starts at t=200 — peak stays 1."""
    bot = _import_bot_for_metrics(monkeypatch)
    rows = [
        {"started_at": 100, "ended_at": 200},
        {"started_at": 200, "ended_at": 300},
    ]
    assert bot._peak_concurrent(rows) == 1


def test_peak_concurrent_skips_unfinished_rows(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    rows = [
        {"started_at": 100, "ended_at": 0},
        {"started_at": 100, "ended_at": None},
        {"started_at": 100, "ended_at": 200},
    ]
    assert bot._peak_concurrent(rows) == 1


def test_peak_concurrent_empty(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    assert bot._peak_concurrent([]) == 0


# --- _percentile -------------------------------------------------------


def test_percentile_empty_returns_zero(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    assert bot._percentile([], 50) == 0
    assert bot._percentile([], 95) == 0


def test_percentile_p50_p95(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    vals = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert bot._percentile(vals, 50) == 50
    assert bot._percentile(vals, 95) == 100
    assert bot._percentile(vals, 0) == 10
    assert bot._percentile(vals, 100) == 100


# --- _aggregate_metrics ------------------------------------------------


def test_aggregate_empty_table_returns_zeros(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    out = bot._aggregate_metrics(now_ts=1_700_000_000.0, rows=[], active_calls=0)
    assert out["active_calls"] == 0
    assert out["today"]["total"] == 0
    assert out["today"]["avg_duration_s"] == 0
    assert out["today"]["p50_duration_s"] == 0
    assert out["today"]["p95_duration_s"] == 0
    assert out["transfer_rate_24h"] == 0
    assert out["peak_concurrent_24h"] == 0
    assert out["demo_distribution_24h"] == {}
    assert out["engine_distribution_24h"] == {}
    # canonical outcome buckets always emit, even on empty input
    for k in ("user_requested", "task_completed", "transferred",
              "timeout", "error", "unknown"):
        assert out["outcome_24h"][k] == 0


def test_aggregate_full_payload_shape(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    # Pin "now" to a UTC midnight + 1h so all of today's rows are >= midnight.
    midnight = 1_700_006_400  # 2023-11-14 00:00:00 UTC
    now_ts = midnight + 3600
    rows = [
        {"started_at": midnight + 100, "ended_at": midnight + 160,
         "duration_s": 60, "outcome": "user_requested",
         "transfer_requested": False, "scenario": "it-helpdesk", "engine": "nova-sonic"},
        {"started_at": midnight + 200, "ended_at": midnight + 320,
         "duration_s": 120, "outcome": "task_completed",
         "transfer_requested": False, "scenario": "it-helpdesk", "engine": "nova-sonic"},
        {"started_at": midnight + 300, "ended_at": midnight + 360,
         "duration_s": 60, "outcome": "transferred",
         "transfer_requested": True, "scenario": "sales", "engine": "pipeline"},
        {"started_at": midnight + 400, "ended_at": midnight + 1000,
         "duration_s": 600, "outcome": "user_requested",
         "transfer_requested": False, "scenario": "it-helpdesk", "engine": "nova-sonic"},
    ]
    out = bot._aggregate_metrics(now_ts, rows, active_calls=2)

    # required top-level keys
    assert set(out.keys()) >= {
        "as_of", "active_calls", "today", "outcome_24h",
        "transfer_rate_24h", "demo_distribution_24h",
        "engine_distribution_24h", "peak_concurrent_24h",
    }
    assert set(out["today"].keys()) == {
        "total", "avg_duration_s", "p50_duration_s", "p95_duration_s",
    }
    assert out["as_of"] == int(now_ts)
    assert out["active_calls"] == 2
    assert out["today"]["total"] == 4
    assert out["today"]["avg_duration_s"] == 210.0  # (60+120+60+600)/4
    assert out["outcome_24h"]["user_requested"] == 2
    assert out["outcome_24h"]["task_completed"] == 1
    assert out["outcome_24h"]["transferred"] == 1
    assert out["transfer_rate_24h"] == 0.25
    assert out["demo_distribution_24h"] == {"it-helpdesk": 3, "sales": 1}
    assert out["engine_distribution_24h"] == {"nova-sonic": 3, "pipeline": 1}


def test_aggregate_unknown_outcome_bucket(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    rows = [
        {"started_at": 100, "ended_at": 200, "duration_s": 100,
         "outcome": "weird-new-bucket", "scenario": "it-helpdesk", "engine": "nova-sonic"},
    ]
    out = bot._aggregate_metrics(1_700_000_000.0, rows, active_calls=0)
    assert out["outcome_24h"]["weird-new-bucket"] == 1
    # canonical buckets still present at zero
    assert out["outcome_24h"]["user_requested"] == 0


# --- _collect_metrics + cache ------------------------------------------


def _reset_metrics_cache(bot):
    bot._METRICS_CACHE["ts"] = 0.0
    bot._METRICS_CACHE["value"] = None


def test_collect_metrics_active_calls_from_registry(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    _reset_metrics_cache(bot)

    async def fake_scan(now_ts):
        return []

    monkeypatch.setattr(bot, "_scan_recent_history", fake_scan)
    monkeypatch.setitem(bot.ACTIVE_SESSIONS, "k1", {})
    monkeypatch.setitem(bot.ACTIVE_SESSIONS, "k2", {})
    out = _asyncio.run(bot._collect_metrics())
    assert out["active_calls"] == 2


def test_collect_metrics_empty_table_no_raise(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    _reset_metrics_cache(bot)

    async def fake_scan(now_ts):
        return []

    monkeypatch.setattr(bot, "_scan_recent_history", fake_scan)
    out = _asyncio.run(bot._collect_metrics())
    assert out["today"]["total"] == 0
    assert out["peak_concurrent_24h"] == 0


def test_collect_metrics_cache_hit_skips_scan(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    _reset_metrics_cache(bot)
    call_count = {"n": 0}

    async def counting_scan(now_ts):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(bot, "_scan_recent_history", counting_scan)
    _asyncio.run(bot._collect_metrics())
    _asyncio.run(bot._collect_metrics())
    assert call_count["n"] == 1


def test_collect_metrics_cache_expires(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    _reset_metrics_cache(bot)
    call_count = {"n": 0}

    async def counting_scan(now_ts):
        call_count["n"] += 1
        return []

    monkeypatch.setattr(bot, "_scan_recent_history", counting_scan)
    _asyncio.run(bot._collect_metrics())
    # Force the cache to look stale.
    bot._METRICS_CACHE["ts"] = 0.0
    _asyncio.run(bot._collect_metrics())
    assert call_count["n"] == 2


def test_collect_metrics_history_disabled_returns_zeros(monkeypatch):
    bot = _import_bot_for_metrics(monkeypatch)
    _reset_metrics_cache(bot)
    monkeypatch.setattr(bot, "HISTORY_DISABLED", True)
    monkeypatch.setattr(bot, "_history", None)
    out = _asyncio.run(bot._collect_metrics())
    assert out["today"]["total"] == 0
    assert out["peak_concurrent_24h"] == 0


# --- _scan_recent_history (metrics SQL shape, §1.2) ---------------------


def _metrics_bot_with_fake(monkeypatch):
    """Import bot + wire FakeCosmos; returns (bot, fake).

    The repo resolves containers lazily through the provider seam, so the
    real ``bot._history._repo`` runs against FakeCosmos unchanged.
    """
    bot = _import_bot_for_metrics(monkeypatch)
    fake = FakeCosmos()
    _cosmos.set_container_provider(fake)
    monkeypatch.setattr(bot, "HISTORY_DISABLED", False)
    return bot, fake


def test_scan_uses_projection_expression(monkeypatch):
    """Metrics scan selects only the 8 narrow columns — never turns/summary
    (AC #2), with the CI-1 ORDER BY fixed."""
    bot, fake = _metrics_bot_with_fake(monkeypatch)
    try:
        rows = _asyncio.run(bot._scan_recent_history(1_700_000_000.0))
        assert rows == []
        queries = fake.container("calls").queries
        assert len(queries) == 1
        sql = queries[0]["query"]
        for col in ("call_id", "started_at", "ended_at", "duration_s",
                    "outcome", "transfer_requested", "scenario", "engine"):
            assert col in sql, f"missing {col} in metrics SELECT"
        assert "turns" not in sql
        assert "summary" not in sql
        assert "ORDER BY c.started_at DESC, c.id ASC" in sql
    finally:
        _cosmos.reset_container_provider()


def test_scan_pages_through_last_evaluated_key(monkeypatch):
    """Keyset paging: >page_size rows ⇒ multiple queries, each subsequent one
    anchored by the keyset predicate (started_at < @ks_started OR ...)."""
    bot, fake = _metrics_bot_with_fake(monkeypatch)
    try:
        # 501 rows crosses the repo's page_size=500 boundary once.
        for i in range(501):
            _seed_call(fake, f"m-{i:04d}", started_at=1_699_990_000 + i,
                       ended_at=1_699_990_030 + i, duration_s=30,
                       outcome="user_requested", transfer_requested=False,
                       scenario="it-helpdesk", engine="nova-sonic")
        rows = _asyncio.run(bot._scan_recent_history(1_700_000_000.0))
        assert [r["call_id"] for r in rows] == [f"m-{i:04d}" for i in range(500, -1, -1)]
        queries = fake.container("calls").queries
        assert len(queries) == 2
        assert "@ks_started" not in queries[0]["query"]
        assert "@ks_started" in queries[1]["query"]
    finally:
        _cosmos.reset_container_provider()


# --- /api/admin/metrics endpoint (T2) ----------------------------------


def test_metrics_endpoint_returns_200_with_full_schema(admin_app):
    from fastapi.testclient import TestClient

    _reset_metrics_cache(admin_app)

    client = TestClient(admin_app.app)
    r = client.get("/api/admin/metrics", headers=_basic_auth_header())
    assert r.status_code == 200, r.text
    body = r.json()
    for k in ("as_of", "active_calls", "today", "outcome_24h",
              "transfer_rate_24h", "demo_distribution_24h",
              "engine_distribution_24h", "peak_concurrent_24h"):
        assert k in body
    assert set(body["today"].keys()) == {
        "total", "avg_duration_s", "p50_duration_s", "p95_duration_s",
    }


def test_metrics_endpoint_401_without_auth(admin_app):
    from fastapi.testclient import TestClient

    # Drop the auth-bypass overrides so the real require_admin runs.
    admin_app.app.dependency_overrides.clear()
    client = TestClient(admin_app.app, base_url="https://testserver")
    r = client.get("/api/admin/metrics")
    assert r.status_code == 401


def test_metrics_endpoint_401_with_invalid_cookie(admin_app):
    from fastapi.testclient import TestClient

    admin_app.app.dependency_overrides.clear()
    client = TestClient(admin_app.app, base_url="https://testserver")
    client.cookies.set("vb_session", "not-a-valid-jwt")
    r = client.get("/api/admin/metrics")
    assert r.status_code == 401


def test_metrics_endpoint_active_calls_reflects_registry(admin_app, monkeypatch):
    from fastapi.testclient import TestClient

    _reset_metrics_cache(admin_app)
    monkeypatch.setitem(admin_app.ACTIVE_SESSIONS, "live-1", {})

    client = TestClient(admin_app.app)
    r = client.get("/api/admin/metrics", headers=_basic_auth_header())
    assert r.status_code == 200
    assert r.json()["active_calls"] >= 1


def test_metrics_endpoint_500_on_aggregator_exception(admin_app, monkeypatch):
    from fastapi.testclient import TestClient

    _reset_metrics_cache(admin_app)

    async def boom():
        raise RuntimeError("metrics-broke")

    monkeypatch.setattr(admin_app, "_collect_metrics", boom)
    client = TestClient(admin_app.app)
    r = client.get("/api/admin/metrics", headers=_basic_auth_header())
    assert r.status_code == 500
    assert "error" in r.json()


def test_metrics_endpoint_route_has_require_admin_dependency(admin_app):
    """Regression lock: the metrics route is admin-only via a per-route
    Depends(require_admin) (the admin_path_guard middleware was removed)."""
    names = _route_dep_names(admin_app.app, "/api/admin/metrics")
    assert "require_admin" in names, f"missing require_admin; got {names!r}"
