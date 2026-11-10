"""FC-6 CosmosCallRepository 行為契約測（U-301）。

覆蓋：create/get/update（StaleVersionError）/append_events（event_id 去重、
CallFinalizedError）/finalize（終態、遲到事件不復活）/query_history（游標）；
X1 折疊面：TTL 語義（§1.3-2/3/5）、turns 拆分重拼（transcripts/call_events）、
§1.2 查詢形狀（caller 單分區 + CI-1 ORDER BY、keyset 游標、讀側強制過期）。
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from fake_cosmos import FakeCosmos
from storage import cosmos
from storage.base import (
    CallEvent,
    CallFinalizedError,
    CallRecord,
    CallState,
    StaleVersionError,
)
from storage.cosmos_calls import CosmosCallRepository


@pytest.fixture
def fake():
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    yield f
    cosmos.reset_container_provider()


def _repo(ttl_days: int = 30) -> CosmosCallRepository:
    repo = CosmosCallRepository(ttl_days=ttl_days)
    # 釘死時鐘：fixture 時間戳圍繞 NOW，讀側強制過期（§1.3-4）才可控
    repo._now = lambda: NOW
    return repo


NOW = 1_700_000_500


def _record(call_id: str = "c1", **kw) -> CallRecord:
    base = dict(
        call_id=call_id,
        state=CallState.ACTIVE,
        started_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        tenant_id="t1",
        provider_ids={"twilio_call_sid": "CA1"},
        expires_at=None,
        owner="replica-1",
        handoff_outcome=None,
        version=0,
    )
    base.update(kw)
    return CallRecord(**base)


# --- FC-6 create/get ----------------------------------------------------


def test_create_get_roundtrip(fake):
    repo = _repo()
    created = asyncio.run(repo.create(_record()))
    assert created.call_id == "c1"
    assert created.state == CallState.ACTIVE
    assert created.provider_ids == {"twilio_call_sid": "CA1"}
    got = asyncio.run(repo.get("c1"))
    assert got == created
    # PK /caller_id：FC-6 無 caller 欄 → 匿名哨兵（§1.1）
    doc = fake.container("calls").docs[("_none_", "c1")]
    assert doc["call_id"] == "c1"
    assert doc["tenant_id"] == "t1"


def test_create_is_idempotent_on_duplicate(fake):
    repo = _repo()
    first = asyncio.run(repo.create(_record()))
    second = asyncio.run(repo.create(_record()))
    assert second.call_id == first.call_id
    assert second.version == first.version  # 既有記錄原樣返回


def test_get_missing_returns_none(fake):
    assert asyncio.run(_repo().get("nope")) is None


# --- FC-6 update（樂觀並發） ---------------------------------------------


def test_update_happy_path_and_stale_version(fake):
    repo = _repo()
    rec = asyncio.run(repo.create(_record()))
    updated = asyncio.run(
        repo.update("c1", {"handoff_outcome": "queued"}, expected_version=rec.version)
    )
    assert updated.handoff_outcome == "queued"
    assert updated.version != rec.version
    with pytest.raises(StaleVersionError):
        asyncio.run(
            repo.update("c1", {"handoff_outcome": "x"}, expected_version=rec.version)
        )


def test_update_after_finalize_raises(fake):
    repo = _repo()
    rec = asyncio.run(repo.create(_record()))
    asyncio.run(repo.finalize("c1", outcome={"handoff_outcome": "done"}, expected_version=rec.version))
    with pytest.raises(CallFinalizedError):
        asyncio.run(repo.update("c1", {"owner": "x"}, expected_version=rec.version))


# --- FC-6 append_events / finalize ---------------------------------------


def test_append_events_dedup_by_event_id(fake):
    repo = _repo()
    asyncio.run(repo.create(_record()))
    ev = CallEvent(
        event_id="e1", call_id="c1", event_type="tool_call",
        occurred_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
        payload={"kind": "end_call"},
    )
    assert asyncio.run(repo.append_events("c1", [ev, ev])) == 1  # 重複 event_id 去重
    assert asyncio.run(repo.append_events("c1", [ev])) == 0


def test_finalize_terminal_and_late_event_rejected(fake):
    repo = _repo()
    rec = asyncio.run(repo.create(_record()))
    fin = asyncio.run(
        repo.finalize("c1", outcome={"handoff_outcome": "transferred"}, expected_version=rec.version)
    )
    assert fin.state == CallState.ENDED
    assert fin.handoff_outcome == "transferred"
    assert fake.container("calls").docs[("_none_", "c1")]["finalized"] is True
    # 遲到事件不復活終態呼叫
    late = CallEvent(
        event_id="e-late", call_id="c1", event_type="tool_call",
        occurred_at=datetime(2026, 1, 1, 12, 5, tzinfo=timezone.utc),
        payload={},
    )
    with pytest.raises(CallFinalizedError):
        asyncio.run(repo.append_events("c1", [late]))
    with pytest.raises(CallFinalizedError):
        asyncio.run(repo.finalize("c1", outcome={}, expected_version=fin.version))


def test_finalize_stale_version_raises(fake):
    repo = _repo()
    rec = asyncio.run(repo.create(_record()))
    with pytest.raises(StaleVersionError):
        asyncio.run(repo.finalize("c1", outcome={}, expected_version="bogus"))


# --- FC-6 query_history + §1.2 查詢形狀 -----------------------------------


def test_query_history_caller_path_is_single_partition_ci1(fake):
    repo = _repo()
    asyncio.run(repo.create(_record()))
    page = asyncio.run(repo.query_history(caller="_none_", limit=10))
    assert [r.call_id for r in page.items] == ["c1"]
    q = fake.container("calls").queries[-1]
    assert "c.caller_id = @caller" in q["query"]
    assert "ORDER BY c.started_at DESC, c.id ASC" in q["query"]  # CI-1
    assert q["partition_key"] == "_none_"  # 單分區
    # 讀側強制過期（§1.3-4）
    assert "expires_at" in q["query"]


def test_query_history_global_path_is_cross_partition(fake):
    repo = _repo()
    asyncio.run(repo.create(_record()))
    page = asyncio.run(repo.query_history(limit=10))
    assert len(page.items) == 1
    q = fake.container("calls").queries[-1]
    assert q["partition_key"] is None


def test_query_history_cursor_ties_break_on_id_asc(fake):
    repo = _repo()
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    for cid in ("c-b", "c-a", "c-c"):  # 同秒並列 → id 決定順序
        asyncio.run(repo.create(_record(cid, started_at=base)))
    page1 = asyncio.run(repo.query_history(limit=2))
    assert [r.call_id for r in page1.items] == ["c-a", "c-b"]
    assert page1.next_cursor
    page2 = asyncio.run(repo.query_history(cursor=page1.next_cursor, limit=2))
    assert [r.call_id for r in page2.items] == ["c-c"]
    assert page2.next_cursor is None


def test_query_history_hides_expired_rows(fake):
    repo = _repo()
    rec = asyncio.run(repo.create(_record()))
    # 把 expires_at 撥到過去：讀側強制過期 ⇒ 查不到
    fake.container("calls").docs[("_none_", "c1")]["expires_at"] = 1
    assert asyncio.run(repo.query_history(limit=10)).items == []
    assert asyncio.run(repo.get("c1")) is None


# --- X1 折疊：TTL 語義（§1.3） --------------------------------------------


def test_outcome_row_ttl_epoch_semantics(fake):
    repo = _repo(ttl_days=7)
    started = 1_700_000_000
    repo.upsert_outcome_doc({
        "call_id": "c1", "caller": "+1555", "caller_id": "+1555",
        "started_at": started, "ended_at": started + 10, "duration_s": 10,
        "summary_status": "pending", "ttl": started + 7 * 86400,
        "transfer_requested": False, "transfer_topic": "", "web_user": "",
    })
    doc = fake.container("calls").docs[("+1555", "c1")]
    # §1.3-5：legacy ttl=刪除點 epoch → expires_at 1:1
    assert doc["expires_at"] == started + 7 * 86400
    # 系統 ttl = 剩餘秒
    assert doc["ttl"] == started + 7 * 86400 - NOW
    # 讀出曝光 ttl = 刪除點 epoch（外部 API 形狀不動）
    assert repo.get_history_doc("c1")["ttl"] == started + 7 * 86400


def test_patch_recomputes_remaining_ttl_window_not_renewed(fake):
    repo = _repo(ttl_days=30)
    started = 1_700_000_000
    expires = started + 30 * 86400
    repo.upsert_outcome_doc({
        "call_id": "c1", "caller_id": "a", "caller": "a",
        "started_at": started, "ttl": expires, "summary_status": "pending",
    })
    repo._now = lambda: expires - 100  # 模擬很久之後的 summary 更新
    repo.patch_fields("c1", {"summary": {"intent": "x"}, "summary_status": "ok"})
    doc = fake.container("calls").docs[("a", "c1")]
    # 刪除時點恆等 expires_at：更新不把 30 天窗口無限續期（§1.3-2）
    assert doc["ttl"] == 100
    assert doc["expires_at"] == expires


def test_patch_missing_row_raises(fake):
    with pytest.raises(KeyError):
        _repo().patch_fields("nope", {"summary_status": "ok"})


# --- X1 折疊：turns 拆分/重拼（transcripts + call_events） ------------------


def test_write_turns_splits_and_reassembles(fake):
    repo = _repo()
    started = 1_700_000_000
    repo.upsert_outcome_doc({
        "call_id": "c1", "caller_id": "a", "caller": "a",
        "started_at": started, "ttl": started + 30 * 86400, "summary_status": "pending",
    })
    turns = [
        {"who": "user", "text": "hello", "t": 0.5},
        {"kind": "tool_call", "data": {"name": "end_call"}, "t": 1.0},
        {"who": "bot", "text": "hi there", "t": 1.2},
    ]
    repo.write_turns("c1", turns)
    assert len(fake.container("transcripts").docs) == 2
    assert len(fake.container("call_events").docs) == 1
    assert repo.get_history_doc("c1")["turns"] == turns  # 形狀 1:1 重拼（按 t 排序）


def test_write_turns_reflush_is_idempotent(fake):
    repo = _repo()
    repo.upsert_outcome_doc({
        "call_id": "c1", "caller_id": "a", "caller": "a",
        "started_at": 1_700_000_000, "ttl": 1_800_000_000, "summary_status": "pending",
    })
    turns = [{"who": "user", "text": "hi", "t": 0.1}]
    repo.write_turns("c1", turns)
    repo.write_turns("c1", turns)  # 同 seq 撞 id ⇒ 跳過
    assert len(fake.container("transcripts").docs) == 1
    assert repo.get_history_doc("c1")["turns"] == turns


def test_get_history_doc_merges_fc6_event_shape(fake):
    repo = _repo()
    asyncio.run(repo.create(_record()))
    ev = CallEvent(
        event_id="e1", call_id="c1", event_type="hangup",
        occurred_at=datetime(2026, 1, 1, 12, 1, tzinfo=timezone.utc),
        payload={"reason": "remote"},
    )
    asyncio.run(repo.append_events("c1", [ev]))
    turns = repo.get_history_doc("c1")["turns"]
    assert turns == [{"kind": "hangup", "data": {"reason": "remote"}, "t": ev.occurred_at.timestamp()}]


# --- 列表/遍歷（§1.2） ----------------------------------------------------


def test_query_history_docs_limit_peek_and_filters(fake):
    repo = _repo()
    for i, oc in enumerate(("user_requested", "task_completed", "user_requested")):
        repo.upsert_outcome_doc({
            "call_id": f"c-{i}", "caller_id": f"+1555{i}", "caller": f"+1555{i}",
            "started_at": 1_700_000_000 + i, "ttl": 1_800_000_000,
            "summary_status": "ok", "outcome": oc, "engine": "pipeline", "scenario": "it-helpdesk",
        })
    rows, cursor = repo.query_history_docs(outcome="user_requested", limit=1)
    assert [r["call_id"] for r in rows] == ["c-2"]  # 最新在前
    assert cursor  # 偷看 limit+1 ⇒ 仍有後續
    rows2, cursor2 = repo.query_history_docs(outcome="user_requested", limit=1, cursor=cursor)
    assert [r["call_id"] for r in rows2] == ["c-0"]
    assert cursor2 is None


def test_query_history_docs_caller_and_range_filters(fake):
    repo = _repo()
    for i in range(3):
        repo.upsert_outcome_doc({
            "call_id": f"c-{i}", "caller_id": "+1", "caller": "+1",
            "started_at": 1_700_000_000 + i * 100, "ttl": 1_800_000_000,
            "summary_status": "ok",
        })
    rows, _ = repo.query_history_docs(caller="+1", start_after=1_700_000_100, start_before=1_700_000_150)
    assert [r["call_id"] for r in rows] == ["c-1"]
    q = fake.container("calls").queries[-1]
    assert q["partition_key"] == "+1"


def test_iter_history_docs_pages_with_keyset(fake):
    repo = _repo()
    for i in range(5):
        repo.upsert_outcome_doc({
            "call_id": f"c-{i}", "caller_id": f"x{i}", "caller": f"x{i}",
            "started_at": 1_700_000_000 + i, "ttl": 1_800_000_000, "summary_status": "ok",
        })
    got = list(repo.iter_history_docs(page_size=2))
    assert [r["call_id"] for r in got] == ["c-4", "c-3", "c-2", "c-1", "c-0"]
    qs = fake.container("calls").queries
    assert len(qs) == 3  # 2+2+1 三頁
    assert "c.id > @ks_id" in qs[1]["query"]  # keyset 續頁


def test_metrics_columns_projection_excludes_heavy_fields(fake):
    repo = _repo()
    cols = ["call_id", "started_at", "ended_at", "duration_s", "outcome", "transfer_requested", "scenario", "engine"]
    list(repo.iter_history_docs(start_after=1, columns=cols))
    sql = fake.container("calls").queries[-1]["query"]
    for col in cols:
        assert f"c.{col}" in sql
    assert "c.turns" not in sql
    assert "c.summary" not in sql
