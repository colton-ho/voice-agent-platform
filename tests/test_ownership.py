"""U-305 · FC-7 分布式所有权 + 容量（spec §8.2/§8.3）测试。

覆盖 storage/ownership.py 的实际 API：reserve_capacity / bind_owner /
renew_lease / route_command / release + drain。语义点：绑定竞态恰一胜、租约续期、
注入时钟过期、命令幂等（重复投递零二次执行）、有界重投耗尽、
MAX_CONCURRENT_CALLS_PER_REPLICA 准入拒绝、排空释放、owner-loss 文档化行为。

FakeQueue 同款注入模式：FakeOwnershipStore 直接实现 ownership 窄接口（5 动词 +
etag OCC，抛 storage.cosmos 语义异常），经 store= / set_store_provider 挂载。
（fake_cosmos.FakeContainer 只建 §1.1 八容器且 SQL 评估器不支持 ownership 的
OR/NOT IS_DEFINED 复合谓词，故替身落在 store 层——与模块暴露的缝同级。）
确定性：注入时钟、无 sleep、无网络。
"""
from __future__ import annotations

import itertools

import pytest

from storage import cosmos
from storage.base import CapacityExceededError, Command, StaleVersionError
from storage.ownership import (
    CosmosSessionDirectory,
    reset_store_provider,
    set_store_provider,
)

T0 = 1_700_000_000.0
TTL = 30.0


class Clock:
    def __init__(self, t: float = T0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class FakeOwnershipStore:
    """ownership 窄接口内存替身：put(etag=None)=create（冲突即 Conflict），
    put(etag=…)=replace OCC（不符即 PreconditionFailed）。put_hooks 每条在下一次
    _put 前弹出执行（可改行/可抛异常）——竞态注入用。"""

    def __init__(self):
        self.rows: dict = {}
        self.put_hooks: list = []
        self._etag = itertools.count(1)

    # -- 供测试直接播种/断言 -----------------------------------------------
    def _stamp(self, doc: dict) -> dict:
        out = dict(doc)
        out["_etag"] = str(next(self._etag))
        return out

    def seed(self, kind: str, doc: dict) -> dict:
        stored = self._stamp(doc)
        self.rows[(kind, doc["id"])] = stored
        return dict(stored)

    # -- 窄接口 -------------------------------------------------------------
    def _put(self, key, doc: dict, etag):
        if self.put_hooks:
            self.put_hooks.pop(0)(self)
        cur = self.rows.get(key)
        if etag is None:
            if cur is not None:
                raise ContainerConflict(doc["id"])
        else:
            if cur is None:
                raise ContainerNotFound(doc["id"])
            if str(cur.get("_etag")) != str(etag):
                raise ContainerPreconditionFailed(doc["id"])
        stored = self._stamp(doc)
        self.rows[key] = stored
        return dict(stored)

    def get_lease(self, call_id: str):
        row = self.rows.get(("lease", call_id))
        return dict(row) if row else None

    def put_lease(self, doc: dict, etag: str | None = None) -> dict:
        return self._put(("lease", doc["id"]), doc, etag)

    def list_leases(self, owner_id: str, now: float) -> list:
        # 与 _CosmosStore.list_leases 的 SQL 同语义：kind=lease ∧ 活租约 ∧
        # （我是 owner ∨（未绑定 ∧ 我是预订方））
        out = []
        for (kind, _), row in self.rows.items():
            if kind != "lease":
                continue
            if float(row.get("lease_expires_at") or 0) <= now:
                continue
            if row.get("owner_id") == owner_id or (
                row.get("owner_id") is None and row.get("reserved_by") == owner_id
            ):
                out.append(dict(row))
        return out

    def get_command(self, call_id: str, command_id: str):
        row = self.rows.get(("command", command_id))
        return dict(row) if row else None

    def put_command(self, doc: dict, etag: str | None = None) -> dict:
        return self._put(("command", doc["id"]), doc, etag)


ContainerConflict = cosmos.ContainerConflict
ContainerNotFound = cosmos.ContainerNotFound
ContainerPreconditionFailed = cosmos.ContainerPreconditionFailed


class FakeTransport:
    """owner-addressable 传输替身：按序回结果（bool / Exception），空则 True。"""

    def __init__(self, *results):
        self.calls: list = []
        self._results = list(results)

    async def send(self, owner_id: str, command: Command) -> bool:
        self.calls.append((owner_id, command))
        if self._results:
            r = self._results.pop(0)
            if isinstance(r, Exception):
                raise r
            return bool(r)
        return True


@pytest.fixture(autouse=True)
def _clean_provider():
    reset_store_provider()
    yield
    reset_store_provider()


def make_dir(store, clock, *, owner="replica-a", max_calls=10, ttl=TTL,
             max_attempts=3, transport=None) -> CosmosSessionDirectory:
    send = transport.send if transport is not None else None
    return CosmosSessionDirectory(
        owner_id=owner,
        max_concurrent_calls=max_calls,
        lease_ttl_seconds=ttl,
        max_delivery_attempts=max_attempts,
        send=send,
        clock=clock,
        store=store,
    )


def cmd(cid: str = "cmd-1") -> Command:
    return Command(command_id=cid, name="hangup", payload={"reason": "admin"})


# ---------------------------------------------------------------------------
# reserve_capacity / 准入（MAX_CONCURRENT_CALLS_PER_REPLICA）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reserve_creates_unbound_reservation():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock)
    res = await d.reserve_capacity(call_id="c1", ttl_seconds=30)
    assert res.call_id == "c1"
    assert res.expires_at.timestamp() == T0 + 30
    row = store.rows[("lease", "c1")]
    assert row.get("owner_id") is None      # 预订未绑定
    assert row["reserved_by"] == "replica-a"
    assert row["lease_expires_at"] == T0 + 30


@pytest.mark.asyncio
async def test_reserve_same_call_is_idempotent():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock, max_calls=1)
    first = await d.reserve_capacity(call_id="c1", ttl_seconds=30)
    again = await d.reserve_capacity(call_id="c1", ttl_seconds=30)  # 同 call 不重复占坑
    assert again.expires_at == first.expires_at
    assert len(store.rows) == 1


@pytest.mark.asyncio
async def test_admission_rejected_at_capacity():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock, max_calls=2)
    await d.reserve_capacity(call_id="c1", ttl_seconds=30)
    await d.reserve_capacity(call_id="c2", ttl_seconds=30)
    with pytest.raises(CapacityExceededError):
        await d.reserve_capacity(call_id="c3", ttl_seconds=30)
    assert ("lease", "c3") not in store.rows


@pytest.mark.asyncio
async def test_admission_ignores_expired_rows():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock, max_calls=2)
    await d.reserve_capacity(call_id="c1", ttl_seconds=30)
    await d.reserve_capacity(call_id="c2", ttl_seconds=30)
    clock.advance(TTL + 1)  # 过期行不占容量（注入时钟）
    await d.reserve_capacity(call_id="c3", ttl_seconds=30)


@pytest.mark.asyncio
async def test_admission_ignores_other_replica_rows():
    clock, store = Clock(), FakeOwnershipStore()
    other = make_dir(store, clock, owner="replica-b", max_calls=10)
    await other.reserve_capacity(call_id="x1", ttl_seconds=30)
    await other.bind_owner(call_id="x1", owner_id="replica-b")
    d = make_dir(store, clock, owner="replica-a", max_calls=1)
    await d.reserve_capacity(call_id="c1", ttl_seconds=30)  # 别的副本不占我的容量
    assert store.rows[("lease", "x1")]["owner_id"] == "replica-b"


@pytest.mark.asyncio
async def test_reserve_race_lost_raises_stale_version():
    # 并发抢占：两次 create 都撞车（读侧尚未见到胜者行）→ 预算耗尽认输。
    clock, store = Clock(), FakeOwnershipStore()
    store.put_hooks = [
        lambda s: (_ for _ in ()).throw(ContainerConflict("c1")),
        lambda s: (_ for _ in ()).throw(ContainerConflict("c1")),
    ]
    d = make_dir(store, clock)
    with pytest.raises(StaleVersionError):
        await d.reserve_capacity(call_id="c1", ttl_seconds=30)


# ---------------------------------------------------------------------------
# bind_owner（原子绑定，恰一胜）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_bind_creates_owner_lease():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock, ttl=TTL)
    lease = await d.bind_owner(call_id="c1", owner_id="replica-a")
    assert lease.owner_id == "replica-a"
    assert lease.expires_at.timestamp() == T0 + TTL
    assert store.rows[("lease", "c1")]["owner_id"] == "replica-a"


@pytest.mark.asyncio
async def test_bind_after_reserve_keeps_reserver():
    # §8.2 序：webhook 预订（副本 A）→ 流接受时绑定 owner；reserved_by 保留。
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock, owner="replica-a")
    await d.reserve_capacity(call_id="c1", ttl_seconds=30)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    row = store.rows[("lease", "c1")]
    assert row["owner_id"] == "replica-a"
    assert row["reserved_by"] == "replica-a"


@pytest.mark.asyncio
async def test_bind_race_exactly_one_wins():
    # 两副本抢同一 call：恰一胜，输家 StaleVersionError，行归胜者。
    clock, store = Clock(), FakeOwnershipStore()
    a = make_dir(store, clock, owner="replica-a")
    b = make_dir(store, clock, owner="replica-b")
    await a.bind_owner(call_id="c1", owner_id="replica-a")
    with pytest.raises(StaleVersionError):
        await b.bind_owner(call_id="c1", owner_id="replica-b")
    assert store.rows[("lease", "c1")]["owner_id"] == "replica-a"


@pytest.mark.asyncio
async def test_bind_create_race_loser_sees_winner():
    # create 竞态：读空 → 胜者已落行 → 我的 create 撞 Conflict → 重读见胜者认输。
    clock, store = Clock(), FakeOwnershipStore()

    def winner_lands_then_conflict(s: FakeOwnershipStore):
        s.rows[("lease", "c1")] = s._stamp({
            "id": "c1", "call_id": "c1", "kind": "lease", "reserved_by": "replica-b",
            "owner_id": "replica-b", "lease_expires_at": T0 + TTL, "expires_at": T0 + TTL,
        })
        raise ContainerConflict("c1")

    store.put_hooks = [winner_lands_then_conflict]
    d = make_dir(store, clock, owner="replica-a")
    with pytest.raises(StaleVersionError):
        await d.bind_owner(call_id="c1", owner_id="replica-a")
    assert store.rows[("lease", "c1")]["owner_id"] == "replica-b"


@pytest.mark.asyncio
async def test_bind_same_owner_refreshes_lease():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    clock.advance(10)
    lease = await d.bind_owner(call_id="c1", owner_id="replica-a")
    assert lease.expires_at.timestamp() == clock.t + TTL  # 续了新到期点


@pytest.mark.asyncio
async def test_bind_takes_over_expired_lease():
    # owner 停跳 → 租约 TTL 过期 → 其他副本可接手（只恢复后续命令路由）。
    clock, store = Clock(), FakeOwnershipStore()
    a = make_dir(store, clock, owner="replica-a")
    b = make_dir(store, clock, owner="replica-b")
    await a.bind_owner(call_id="c1", owner_id="replica-a")
    clock.advance(TTL + 1)
    lease = await b.bind_owner(call_id="c1", owner_id="replica-b")
    assert lease.owner_id == "replica-b"
    assert store.rows[("lease", "c1")]["owner_id"] == "replica-b"


# ---------------------------------------------------------------------------
# renew_lease
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renew_extends_expiry():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    clock.advance(10)
    lease = await d.renew_lease(call_id="c1", owner_id="replica-a")
    assert lease.expires_at.timestamp() == clock.t + TTL


@pytest.mark.asyncio
async def test_renew_denied_for_non_owner():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock, owner="replica-a")
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    with pytest.raises(StaleVersionError):
        await d.renew_lease(call_id="c1", owner_id="replica-b")


@pytest.mark.asyncio
async def test_renew_denied_after_release():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    await d.release(call_id="c1", owner_id="replica-a")
    with pytest.raises(StaleVersionError):
        await d.renew_lease(call_id="c1", owner_id="replica-a")


@pytest.mark.asyncio
async def test_renew_resurrects_expired_own_lease():
    # 过期但 owner 仍是自己（没被接管）：允许续期复活（文档化语义）。
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    clock.advance(TTL + 1)
    lease = await d.renew_lease(call_id="c1", owner_id="replica-a")
    assert lease.expires_at.timestamp() == clock.t + TTL
    assert store.rows[("lease", "c1")]["owner_id"] == "replica-a"


@pytest.mark.asyncio
async def test_renew_retries_on_transient_occ_conflict():
    # 并发写撞 etag → 重读重试成功（_CLAIM_ATTEMPTS 预算内）。
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock)
    await d.bind_owner(call_id="c1", owner_id="replica-a")

    def concurrent_write(s: FakeOwnershipStore):
        cur = dict(s.rows[("lease", "c1")])
        cur["lease_expires_at"] = clock.t + TTL
        s.rows[("lease", "c1")] = s._stamp(cur)  # 并发续期抬走 etag
        raise ContainerPreconditionFailed("c1")

    store.put_hooks = [concurrent_write]
    lease = await d.renew_lease(call_id="c1", owner_id="replica-a")
    assert lease.owner_id == "replica-a"


# ---------------------------------------------------------------------------
# release / drain
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_release_clears_owner_and_expires_lease():
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    await d.release(call_id="c1", owner_id="replica-a")
    row = store.rows[("lease", "c1")]
    assert row.get("owner_id") is None
    assert row["lease_expires_at"] == clock.t  # 立时到期，不靠 delete
    ack = await d.route_command(call_id="c1", command=cmd())  # 释放后无活租约
    assert ack.accepted is False
    assert "owner lost" in ack.detail
    assert t.calls == []


@pytest.mark.asyncio
async def test_release_missing_call_is_idempotent():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock)
    await d.release(call_id="ghost", owner_id="replica-a")  # 不抛


@pytest.mark.asyncio
async def test_release_twice_is_idempotent():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    await d.release(call_id="c1", owner_id="replica-a")
    await d.release(call_id="c1", owner_id="replica-a")  # 不抛


@pytest.mark.asyncio
async def test_release_denied_for_non_owner_live_lease():
    clock, store = Clock(), FakeOwnershipStore()
    a = make_dir(store, clock, owner="replica-a")
    b = make_dir(store, clock, owner="replica-b")
    await a.bind_owner(call_id="c1", owner_id="replica-a")
    with pytest.raises(StaleVersionError):
        await b.release(call_id="c1", owner_id="replica-b")
    assert store.rows[("lease", "c1")]["owner_id"] == "replica-a"  # 活租约未被误删


@pytest.mark.asyncio
async def test_drain_releases_all_own_leases():
    # §8.3 停机序：排空释放本副本全部活租约（含未绑定预订），容量即时回收。
    clock, store = Clock(), FakeOwnershipStore()
    a = make_dir(store, clock, owner="replica-a", max_calls=2)
    b = make_dir(store, clock, owner="replica-b", max_calls=10)
    await a.reserve_capacity(call_id="c1", ttl_seconds=30)
    await a.bind_owner(call_id="c1", owner_id="replica-a")
    await a.reserve_capacity(call_id="c2", ttl_seconds=30)   # 未绑定预订也算容量
    await b.bind_owner(call_id="x1", owner_id="replica-b")
    released = await a.drain()
    assert released == 2
    assert store.rows[("lease", "x1")]["owner_id"] == "replica-b"  # 别人的不动
    await a.reserve_capacity(call_id="c3", ttl_seconds=30)  # 容量已回收


@pytest.mark.asyncio
async def test_drain_skips_rows_taken_over_by_other():
    # 排空中被并发接管的行跳过（StaleVersionError 不中断整个循环）。
    clock, store = Clock(), FakeOwnershipStore()
    a = make_dir(store, clock, owner="replica-a", max_calls=10)
    await a.bind_owner(call_id="c1", owner_id="replica-a")
    await a.bind_owner(call_id="c2", owner_id="replica-a")

    def takeover(s: FakeOwnershipStore):
        cur = dict(s.rows[("lease", "c1")])
        cur["owner_id"] = "replica-b"
        s.rows[("lease", "c1")] = s._stamp(cur)  # c1 被 B 抢走
        raise ContainerPreconditionFailed("c1")

    store.put_hooks = [takeover]  # drain 按序先处理 c1
    released = await a.drain()
    assert released == 1  # c1 跳过，c2 释放
    assert store.rows[("lease", "c1")]["owner_id"] == "replica-b"


# ---------------------------------------------------------------------------
# route_command：投递 / 幂等 / 有界重试 / owner-loss
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_route_delivers_to_live_owner():
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is True
    assert t.calls == [("replica-a", cmd())]
    assert store.rows[("command", "cmd-1")]["status"] == "delivered"


@pytest.mark.asyncio
async def test_route_without_transport_rejected_honestly():
    clock, store = Clock(), FakeOwnershipStore()
    d = make_dir(store, clock, transport=None)  # send=None
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is False
    assert "no owner-addressable transport" in ack.detail
    assert ("command", "cmd-1") not in store.rows  # 不假投递、不落簿记


@pytest.mark.asyncio
async def test_route_owner_lost_no_live_lease():
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t)
    ack = await d.route_command(call_id="c1", command=cmd())  # 从未绑定
    assert ack.accepted is False
    assert "owner lost" in ack.detail
    assert t.calls == []
    assert ("command", "cmd-1") not in store.rows  # 非终态：不落失败簿记


@pytest.mark.asyncio
async def test_route_rejected_after_lease_expiry():
    # 注入时钟：租约到期后命令一律 accepted=False（owner-loss 主叫体验，不盲投）。
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    clock.advance(TTL + 1)
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is False
    assert "owner lost" in ack.detail
    assert t.calls == []


@pytest.mark.asyncio
async def test_route_owner_loss_is_nonterminal_retry_after_rebind():
    # owner 丢失拒绝不耗预算：新租约接手后同一 command_id 仍可投。
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    clock.advance(TTL + 1)
    assert (await d.route_command(call_id="c1", command=cmd())).accepted is False
    clock.advance(1)
    await d.bind_owner(call_id="c1", owner_id="replica-a")  # 重新接手
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is True
    assert len(t.calls) == 1


@pytest.mark.asyncio
async def test_command_idempotent_duplicate_delivery_executes_once():
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    first = await d.route_command(call_id="c1", command=cmd())
    again = await d.route_command(call_id="c1", command=cmd())  # 同 command_id 重投
    assert first.accepted is True and again.accepted is True
    assert len(t.calls) == 1  # 恰投一次，重复路由零副作用


@pytest.mark.asyncio
async def test_command_bounded_retries_exhausted():
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport(False, False, False)
    d = make_dir(store, clock, transport=t, max_attempts=3)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is False
    assert "delivery attempts exhausted (3)" in ack.detail
    assert len(t.calls) == 3  # 有界：封顶 max_delivery_attempts
    assert store.rows[("command", "cmd-1")]["status"] == "failed"


@pytest.mark.asyncio
async def test_command_failed_is_terminal_on_reroute():
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport(False, False, False)
    d = make_dir(store, clock, transport=t, max_attempts=3)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    await d.route_command(call_id="c1", command=cmd())
    ack = await d.route_command(call_id="c1", command=cmd())  # 耗尽后重路
    assert ack.accepted is False
    assert len(t.calls) == 3  # 不再投


@pytest.mark.asyncio
async def test_command_send_exception_counts_as_attempt():
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport(OSError("transport down"), OSError("transport down"))
    d = make_dir(store, clock, transport=t, max_attempts=2)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is False
    assert len(t.calls) == 2  # 传输异常=失败计次，不是崩溃


@pytest.mark.asyncio
async def test_command_in_flight_duplicate_rejected():
    # 另一路在飞（pending 且在宽限期内）：防双投，拒绝且不发。
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    store.seed("command", {
        "id": "cmd-1", "call_id": "c1", "kind": "command", "name": "hangup",
        "payload": {}, "target_owner": "replica-a", "status": "pending",
        "attempts": 1, "claimed_at": clock.t,  # 宽限期内
    })
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is False
    assert "in flight" in ack.detail
    assert t.calls == []


@pytest.mark.asyncio
async def test_command_stale_pending_reclaimed_with_cumulative_attempts():
    # 崩溃残留 pending（超宽限期）：接管续投，预算累计不重置。
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t, max_attempts=3)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    store.seed("command", {
        "id": "cmd-1", "call_id": "c1", "kind": "command", "name": "hangup",
        "payload": {}, "target_owner": "replica-a", "status": "pending",
        "attempts": 1, "claimed_at": clock.t - 31,  # 宽限期 30s，已过
    })
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is True
    assert store.rows[("command", "cmd-1")]["attempts"] == 2  # 1 + 本次


@pytest.mark.asyncio
async def test_command_attempts_budget_caps_across_crashes():
    # 预算耗尽的 pending 残留（进程崩在写终态前）：零再投、直接落 failed。
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t, max_attempts=3)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    store.seed("command", {
        "id": "cmd-1", "call_id": "c1", "kind": "command", "name": "hangup",
        "payload": {}, "target_owner": "replica-a", "status": "pending",
        "attempts": 3, "claimed_at": clock.t - 31,
    })
    ack = await d.route_command(call_id="c1", command=cmd())
    assert ack.accepted is False
    assert "delivery attempts exhausted (3)" in ack.detail
    assert t.calls == []  # 累计预算封顶，不二次投递


@pytest.mark.asyncio
async def test_command_record_ttl_recomputed_on_write():
    # §1.3 口径：ttl=剩余秒、每次写入重算（幂等窗口 command_dedup_ttl_seconds）。
    clock, store = Clock(), FakeOwnershipStore()
    t = FakeTransport()
    d = make_dir(store, clock, transport=t)
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    await d.route_command(call_id="c1", command=cmd())
    row = store.rows[("command", "cmd-1")]
    assert row["expires_at"] == clock.t + 86_400
    assert row["ttl"] == 86_400


# ---------------------------------------------------------------------------
# 测试缝（FakeQueue 模式）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_store_provider_seam_used_when_no_explicit_store():
    clock, store = Clock(), FakeOwnershipStore()
    set_store_provider(lambda: store)
    d = CosmosSessionDirectory(
        owner_id="replica-a", max_concurrent_calls=5,
        send=FakeTransport().send, clock=clock,
    )  # 不传 store=，走 get_store()
    await d.bind_owner(call_id="c1", owner_id="replica-a")
    assert store.rows[("lease", "c1")]["owner_id"] == "replica-a"
