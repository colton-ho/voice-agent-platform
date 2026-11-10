"""V6/C-9 契约测试：postcall-jobs worker（pipeline-infra-spec §2.2）。

- V6 I-4 两条腿：正常完成腿 + delivery_count==5 终态腿（重试耗尽必落
  failed 終態、complete 不可再投递——任何行不得永卡 pending）。
- 30s 预算语义：超时/暂时性失败 = 留下一轮（abandon），不得误标 failed。
- R-A-010 幂等：summary_status=='ok' 的重复投递直接 complete，零 LLM 调用。
- 永久性失败（调用配置非法）→ failed 終態，不重试。
"""
from __future__ import annotations

import asyncio

import pytest

from fake_cosmos import FakeCosmos
from fake_servicebus import FakeQueue
from storage import cosmos
from storage.cosmos_calls import CosmosCallRepository
from workers.postcall_worker import (
    MAX_DELIVERY_COUNT,
    WIRE_STATUSES,
    PermanentJobError,
    PostcallWorker,
    is_final_delivery,
)

# Pin the repo clock: seeded rows carry deletion points at NOW+30d — without
# pinning, read-side forced expiry (§1.3-4) would hide them.
NOW = 1_700_000_500

JOB = {"job_type": "summary", "job_id": "summary:c1", "call_id": "c1"}


@pytest.fixture
def world():
    f = FakeCosmos()
    cosmos.set_container_provider(f)
    repo = CosmosCallRepository(ttl_days=30)
    repo._now = lambda: NOW
    queue = FakeQueue()
    yield {"fake": f, "repo": repo, "queue": queue}
    cosmos.reset_container_provider()


def _seed_pending(repo, call_id: str = "c1") -> None:
    repo.upsert_outcome_doc({
        "call_id": call_id,
        "caller": "+1555",
        "started_at": 1_700_000_000,
        "summary_status": "pending",
        "ttl": NOW + 30 * 86400,  # legacy ttl=删除点 epoch（§1.3-5）
    })
    repo.write_turns(call_id, [{"who": "user", "text": "hi", "t": 1}])


def _msg(queue: FakeQueue, delivery_count: int):
    queue.inject(JOB, delivery_count=delivery_count)
    return queue.receive()[0]


def _ok_struct():
    return {
        "intent": "i",
        "key_questions": [],
        "action_items": [],
        "sentiment": "neutral",
    }


# ---------------------------------------------------------------------------
# V6 leg 1 — 正常完成腿
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v6_normal_completion_writes_ok_and_completes(world):
    repo, queue = world["repo"], world["queue"]
    _seed_pending(repo)
    calls = []

    async def invoke(turns, lang):
        calls.append((turns, lang))
        return _ok_struct()

    worker = PostcallWorker(repo, invoke, queue)
    msg = _msg(queue, delivery_count=1)
    assert await worker.handle(msg) == "completed"

    # LLM 恰调用一次，入参来自行内 turns/lang。
    assert len(calls) == 1
    assert any(t.get("text") == "hi" for t in calls[0][0])
    doc = repo.get_history_doc("c1")
    assert doc["summary_status"] == "ok"
    assert doc["summary"]["intent"] == "i"
    assert "summary_error" not in doc
    # 消息结算：complete，零 abandon。
    assert queue.completed == [msg]
    assert queue.abandoned == []


# ---------------------------------------------------------------------------
# V6 leg 2 — I-4 终态腿（delivery_count==5 最后一投）
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_v6_retry_exhaustion_settles_failed_terminal(world):
    repo, queue = world["repo"], world["queue"]
    _seed_pending(repo)

    async def invoke(turns, lang):
        raise RuntimeError("llm down")

    worker = PostcallWorker(repo, invoke, queue)
    msg = _msg(queue, delivery_count=MAX_DELIVERY_COUNT)  # ==5 即最后一投
    assert await worker.handle(msg) == "completed"

    doc = repo.get_history_doc("c1")
    # I-4 硬不变式：failed 終態落行，error-as-data。
    assert doc["summary_status"] == "failed"
    assert "llm down" in doc["summary_error"]
    # complete（不可再投递）——不 abandon、不进 DLQ 侧。
    assert queue.completed == [msg]
    assert queue.abandoned == []


# ---------------------------------------------------------------------------
# 30s 预算 / 暂时性失败 — 留下一轮，不得误标 failed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_transient_failure_abandons_without_marking_failed(world):
    repo, queue = world["repo"], world["queue"]
    _seed_pending(repo)

    async def invoke(turns, lang):
        raise RuntimeError("429 blip")

    worker = PostcallWorker(repo, invoke, queue)
    msg = _msg(queue, delivery_count=2)
    assert await worker.handle(msg) == "abandoned"

    doc = repo.get_history_doc("c1")
    assert doc["summary_status"] == "pending"  # 不得误标 failed
    assert queue.abandoned == [msg]
    assert queue.completed == []


@pytest.mark.asyncio
async def test_timeout_budget_is_transient_left_for_next_round(world):
    repo, queue = world["repo"], world["queue"]
    _seed_pending(repo)

    async def slow(turns, lang):
        await asyncio.sleep(5)

    worker = PostcallWorker(repo, slow, queue, timeout_s=0.05)
    msg = _msg(queue, delivery_count=1)
    assert await worker.handle(msg) == "abandoned"

    doc = repo.get_history_doc("c1")
    assert doc["summary_status"] == "pending"  # 超时≠failed，留下一轮
    assert queue.abandoned == [msg]


# ---------------------------------------------------------------------------
# R-A-010 幂等 / 永久性失败 / 非 summary 信封
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_idempotent_ok_row_completes_without_invoke(world):
    repo, queue = world["repo"], world["queue"]
    _seed_pending(repo)
    repo.patch_fields("c1", {"summary": _ok_struct(), "summary_status": "ok"})
    calls = []

    async def invoke(turns, lang):
        calls.append(1)
        return _ok_struct()

    worker = PostcallWorker(repo, invoke, queue)
    msg = _msg(queue, delivery_count=3)
    assert await worker.handle(msg) == "completed"
    assert calls == []  # 重复投递零副作用
    assert queue.completed == [msg]


@pytest.mark.asyncio
async def test_permanent_failure_settles_failed_on_first_delivery(world):
    repo, queue = world["repo"], world["queue"]
    _seed_pending(repo)

    async def invoke(turns, lang):
        raise ValueError("bad call config")

    worker = PostcallWorker(repo, invoke, queue)
    msg = _msg(queue, delivery_count=1)
    assert await worker.handle(msg) == "completed"  # 不重试

    doc = repo.get_history_doc("c1")
    assert doc["summary_status"] == "failed"
    assert "bad call config" in doc["summary_error"]
    assert queue.completed == [msg]
    assert queue.abandoned == []


@pytest.mark.asyncio
async def test_explicit_permanent_job_error_settles_failed_without_retry(world):
    """BUG-01 钉：显式 PermanentJobError → failed 終態 + complete 零重投。

    修复前该异常落进 except Exception 被误判暂时性（首投 abandon，至多
    5 投白付 LLM 才收口）——与 transient 腿（首投 abandon）对照钉死。
    """
    repo, queue = world["repo"], world["queue"]
    _seed_pending(repo)

    async def invoke(turns, lang):
        raise PermanentJobError("summary schema drift")

    worker = PostcallWorker(repo, invoke, queue)
    msg = _msg(queue, delivery_count=1)  # 首投即收口，对照 transient 腿
    assert await worker.handle(msg) == "completed"

    doc = repo.get_history_doc("c1")
    assert doc["summary_status"] == "failed"
    assert "summary schema drift" in doc["summary_error"]
    assert queue.completed == [msg]
    assert queue.abandoned == []  # 零重投


@pytest.mark.asyncio
async def test_non_summary_job_is_dropped_without_row_touch(world):
    repo, queue = world["repo"], world["queue"]
    _seed_pending(repo)
    queue.inject(
        {"job_type": "future-artifact", "job_id": "x", "call_id": "c1"},
        delivery_count=1,
    )
    msg = queue.receive()[0]
    worker = PostcallWorker(repo, None, queue)
    assert await worker.handle(msg) == "completed"
    doc = repo.get_history_doc("c1")
    assert doc["summary_status"] == "pending"
    assert queue.completed == [msg]


# ---------------------------------------------------------------------------
# is_final_delivery 边界（I-4 收口判定，双约定安全侧）
# ---------------------------------------------------------------------------
def test_is_final_delivery_boundary():
    assert is_final_delivery(1) is False
    assert is_final_delivery(3) is False
    # AMQP 0 基末投（max-1）与 1 基末投（max）都收口——I-4 优先于精确边界。
    assert is_final_delivery(MAX_DELIVERY_COUNT - 1) is True
    assert is_final_delivery(MAX_DELIVERY_COUNT) is True


# ---------------------------------------------------------------------------
# F-14 wire enum 防朽钉（product-impact §7.1）——恰三值、无第四值
# ---------------------------------------------------------------------------
def test_wire_enum_is_exactly_three_values():
    assert WIRE_STATUSES == ("pending", "ok", "failed")
