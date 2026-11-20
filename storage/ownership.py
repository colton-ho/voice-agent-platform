"""FC-7 SessionDirectory 实现：call→runtime-owner 租约 + 容量准入 + 命令路由。

spec §8.2（控制面/媒体面所有权分离）+ §8.3（容量/排空）；契约=storage/base.py
FC-7 五动词（reserve_capacity / bind_owner / renew_lease / route_command /
release），本模块不改契约、不改 cosmos_calls 公共 API。

存储：Cosmos `ownership` 容器（PK /call_id）。§1.1 的八容器是数据面冻结清单，
本容器是 §8.2 协调面（U-305 增补，2026-11-11）。两类行同居一容器：
  lease 行   id=call_id     —— 预订/租约（无 owner_id = 已预订未绑定）
  command 行 id=command_id —— 命令投递簿记（幂等去重键 = command_id）
TTL 走 §1.3 口径：每次写入重算 ttl = expires_at - now（lease 行删除点=租约到期；
command 行删除点=命令幂等窗口 command_dedup_ttl_seconds）。

原子绑定（§8.2「bind the actual owner atomically」）：create/replace 全程带
etag OCC（storage.cosmos 的 ContainerConflict / ContainerPreconditionFailed），
竞态输家重读后见胜者活租约 → StaleVersionError。两个副本抢同一 call 恰一胜。

【owner-loss 主叫体验（§8.2 明文，不做透明恢复承诺）】
owner 进程死亡时：
- 已建立的电话/媒体 WebSocket 随进程消失——共享状态不会迁移活连接。主叫体验
  =这通电话断掉，走运营商/前端的常规断线处理；主叫可重拨，或由控制面重新发起。
  **不存在「另一副本无缝接管继续这通活通话」的路径**。
- 租约停跳后在 lease_ttl_seconds 内到期。此后 route_command 一律
  accepted=False（detail="owner lost ..."），不盲投任何副本。
- 其他副本可在租约过期后 bind_owner 接手——但那只恢复【后续命令路由】（新
  租约），不恢复已断的媒体面。

【排空/停机（§8.3）】
停机序：停止准入（不再调 reserve_capacity）→ drain() 释放本副本全部活租约
（容量即时回收）→ 取消后台工作。崩溃/被 SIGKILL 的进程不做任何清理：租约靠
TTL 自行过期，容量在 lease_ttl_seconds 内自动回收。§8.3 明文：shutdown grace
不保证每通电话都跑完。

【调用侧注意】route_command 的幂等是「投递侧恰一次」；R-A-014 还要求 owner
侧按 command_id 幂等执行（防御投递簿记写入失败等残余窗口的重复投递）。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from storage import cosmos
from storage.base import (
    CapacityExceededError,
    Command,
    CommandAck,
    Lease,
    Reservation,
    SessionDirectory,
    StaleVersionError,
)

# U-305 协调面容器（PK /call_id；Bicep containers 数组同步登记）
OWNERSHIP = "ownership"

_LEASE_KIND = "lease"
_COMMAND_KIND = "command"

# 抢占重读预算：create/replace 竞态各重读一次后仍冲突 → 认定输掉（恰一胜）
_CLAIM_ATTEMPTS = 2

_provider_override = None


def set_store_provider(provider) -> None:
    """测试缝（FakeQueue 模式）：provider() -> store（下方窄接口实现）。"""
    global _provider_override
    _provider_override = provider


def reset_store_provider() -> None:
    global _provider_override
    _provider_override = None


def get_store():
    if _provider_override is not None:
        return _provider_override()
    return _CosmosStore()


class _CosmosStore:
    """ownership 容器窄接口（cosmos_calls 同款：get_container + 语义异常翻译）。

    put_* 的 etag=None 走 create（存在即 ContainerConflict）；带 etag 走
    replace 的 OCC（etag 不符即 ContainerPreconditionFailed）——上层据此实现
    「恰一胜」竞态语义。
    """

    def _c(self):
        return cosmos.get_container(OWNERSHIP)

    def get_lease(self, call_id: str):
        try:
            return self._c().read_item(call_id, call_id)
        except cosmos.ContainerNotFound:
            return None

    def put_lease(self, doc: dict, etag: str | None = None) -> dict:
        if etag is None:
            return self._c().create_item(doc)
        return self._c().replace_item(doc["id"], doc["call_id"], doc, etag=etag)

    def list_leases(self, owner_id: str, now: float) -> list:
        # 准入/排空按 owner 计数（per-replica 口径）；PK 是 /call_id，故降级为
        # 跨分区查询（§1.1 同款取舍：行数量级小，可接受）
        sql = (
            "SELECT * FROM c WHERE c.kind = @kind AND c.lease_expires_at > @now "
            "AND (c.owner_id = @owner "
            "OR (NOT IS_DEFINED(c.owner_id) AND c.reserved_by = @owner))"
        )
        return self._c().query_items(sql, [
            {"name": "@kind", "value": _LEASE_KIND},
            {"name": "@now", "value": now},
            {"name": "@owner", "value": owner_id},
        ])

    def get_command(self, call_id: str, command_id: str):
        try:
            return self._c().read_item(command_id, call_id)
        except cosmos.ContainerNotFound:
            return None

    def put_command(self, doc: dict, etag: str | None = None) -> dict:
        if etag is None:
            return self._c().create_item(doc)
        return self._c().replace_item(doc["id"], doc["call_id"], doc, etag=etag)


class CosmosSessionDirectory(SessionDirectory):
    """FC-7 实现（owner-loss/排空语义见模块 docstring）。

    owner_id = 本副本标识；max_concurrent_calls = MAX_CONCURRENT_CALLS_PER_REPLICA
    （§11 必填配置，无编译内默认——构造方从 config 注入）。clock 可注入以便
    租约过期确定性测试。send = owner-addressable 传输
    ``(owner_id, Command) -> bool``（awaitable）；未接线时 route_command 如实回
    accepted=False（不假投递）。
    """

    def __init__(
        self,
        *,
        owner_id: str,
        max_concurrent_calls: int,
        lease_ttl_seconds: float = 30.0,
        max_delivery_attempts: int = 3,
        command_claim_grace_seconds: float = 30.0,
        command_dedup_ttl_seconds: int = 86_400,
        send=None,
        clock=time.time,
        store=None,
    ):
        self._owner_id = owner_id
        self._max_concurrent = int(max_concurrent_calls)
        self._lease_ttl = float(lease_ttl_seconds)
        self._max_attempts = int(max_delivery_attempts)
        self._claim_grace = float(command_claim_grace_seconds)
        self._dedup_ttl = int(command_dedup_ttl_seconds)
        self._send = send
        self._clock = clock
        self._store = store

    # ---- helpers --------------------------------------------------------

    def _repo(self):
        return self._store if self._store is not None else get_store()

    def _now(self) -> float:
        return float(self._clock())

    @staticmethod
    def _dt(epoch: float) -> datetime:
        return datetime.fromtimestamp(float(epoch), tz=timezone.utc)

    @staticmethod
    def _live(row: dict, now: float) -> bool:
        return float(row.get("lease_expires_at") or 0) > now

    @staticmethod
    def _ttl_fields(expires_at: float, now: float) -> dict:
        # §1.3-2：ttl=剩余秒、每次写入重算——Cosmos TTL 从最后修改起算，
        # 直接搬字段会让租约随续期无限存活
        return {"expires_at": float(expires_at), "ttl": max(1, int(expires_at - now))}

    def _lease_doc(self, call_id: str, *, owner_id: str | None,
                   reserved_by: str, expires_at: float, now: float) -> dict:
        doc = {
            "id": call_id,
            "call_id": call_id,
            "kind": _LEASE_KIND,
            "reserved_by": reserved_by,
            "lease_expires_at": float(expires_at),
            **self._ttl_fields(expires_at, now),
        }
        if owner_id is not None:
            doc["owner_id"] = owner_id
        return doc

    # ---- FC-7 verbs -----------------------------------------------------

    async def reserve_capacity(self, *, call_id: str, ttl_seconds: int) -> Reservation:
        now = self._now()
        # 同 call 重复预订=幂等：先于准入检查——重试/重放不占新容量，满容量下
        # 也不被误拒（webhook 重试同 call 属同一坑位）。
        existing = await asyncio.to_thread(self._repo().get_lease, call_id)
        if existing is not None and self._live(existing, now):
            return Reservation(call_id=call_id,
                               expires_at=self._dt(existing["lease_expires_at"]))
        # 准入：只数本副本的活行（per-replica 口径 = MAX_CONCURRENT_CALLS_PER_REPLICA）
        live = await asyncio.to_thread(self._repo().list_leases, self._owner_id, now)
        if len(live) >= self._max_concurrent:
            raise CapacityExceededError(
                f"replica {self._owner_id} at capacity ({self._max_concurrent})"
            )
        expires_at = now + float(ttl_seconds)
        for _ in range(_CLAIM_ATTEMPTS):
            existing = await asyncio.to_thread(self._repo().get_lease, call_id)
            if existing is not None and self._live(existing, now):
                # 同 call 重复预订=幂等：返回既有活预订，不重复占坑
                return Reservation(call_id=call_id,
                                   expires_at=self._dt(existing["lease_expires_at"]))
            doc = self._lease_doc(
                call_id, owner_id=None,
                reserved_by=(existing or {}).get("reserved_by") or self._owner_id,
                expires_at=expires_at, now=now,
            )
            try:
                await asyncio.to_thread(self._repo().put_lease, doc,
                                        (existing or {}).get("_etag"))
                return Reservation(call_id=call_id, expires_at=self._dt(expires_at))
            except (cosmos.ContainerConflict, cosmos.ContainerPreconditionFailed):
                continue  # 并发抢占：重读按现状再判
        raise StaleVersionError(f"reserve race for {call_id} lost by {self._owner_id}")

    async def bind_owner(self, *, call_id: str, owner_id: str) -> Lease:
        now = self._now()
        expires_at = now + self._lease_ttl
        for _ in range(_CLAIM_ATTEMPTS):
            existing = await asyncio.to_thread(self._repo().get_lease, call_id)
            if existing is not None:
                cur = existing.get("owner_id") or None
                if cur and cur != owner_id and self._live(existing, now):
                    # 另一副本的活租约：原子绑定的输家（恰一胜）
                    raise StaleVersionError(
                        f"{call_id} owned by {cur} until {existing.get('lease_expires_at')}"
                    )
            doc = self._lease_doc(
                call_id, owner_id=owner_id,
                reserved_by=(existing or {}).get("reserved_by") or owner_id,
                expires_at=expires_at, now=now,
            )
            try:
                await asyncio.to_thread(self._repo().put_lease, doc,
                                        (existing or {}).get("_etag"))
                return Lease(call_id=call_id, owner_id=owner_id,
                             expires_at=self._dt(expires_at))
            except (cosmos.ContainerConflict, cosmos.ContainerPreconditionFailed):
                continue  # 抢占竞态：重读见胜者则认输，否则再判一次
        raise StaleVersionError(f"bind race for {call_id} lost by {owner_id}")

    async def renew_lease(self, *, call_id: str, owner_id: str) -> Lease:
        now = self._now()
        expires_at = now + self._lease_ttl
        for _ in range(_CLAIM_ATTEMPTS):
            existing = await asyncio.to_thread(self._repo().get_lease, call_id)
            if existing is None or (existing.get("owner_id") or None) != owner_id:
                # 租约已释放/已过期后被接管：续期者不再是 owner
                raise StaleVersionError(f"renew denied for {call_id}: not the owner")
            doc = self._lease_doc(
                call_id, owner_id=owner_id,
                reserved_by=existing.get("reserved_by") or owner_id,
                expires_at=expires_at, now=now,
            )
            try:
                await asyncio.to_thread(self._repo().put_lease, doc, existing.get("_etag"))
                return Lease(call_id=call_id, owner_id=owner_id,
                             expires_at=self._dt(expires_at))
            except cosmos.ContainerPreconditionFailed:
                continue  # 并发续期/接管：重读再判
        raise StaleVersionError(f"renew race for {call_id} lost by {owner_id}")

    async def route_command(self, *, call_id: str, command: Command) -> CommandAck:
        now = self._now()
        store = self._repo()
        cid = command.command_id

        if self._send is None:
            # 未接 owner-addressable 传输：如实拒绝，不假投递、不落簿记
            return CommandAck(command_id=cid, accepted=False,
                              detail="no owner-addressable transport wired")

        existing = await asyncio.to_thread(store.get_command, call_id, cid)
        if existing is not None:
            status = existing.get("status")
            if status == "delivered":
                # 幂等：同一 command_id 恰投递一次，重复路由零副作用
                return CommandAck(command_id=cid, accepted=True,
                                  detail=existing.get("ack_detail"))
            if status == "failed":
                return CommandAck(command_id=cid, accepted=False,
                                  detail=existing.get("ack_detail"))
            if float(existing.get("claimed_at") or 0) + self._claim_grace > now:
                # 并发重复路由：另一路在飞，防双投
                return CommandAck(command_id=cid, accepted=False,
                                  detail="duplicate command_id: routing in flight")

        # 活 owner 才投。owner-loss 拒绝不落终态、不耗预算：新租约接手后同
        # command_id 仍可投（主叫体验见模块 docstring）
        lease = await asyncio.to_thread(store.get_lease, call_id)
        if lease is None or not lease.get("owner_id") or not self._live(lease, now):
            return CommandAck(command_id=cid, accepted=False,
                              detail="owner lost: no live lease")

        owner = lease["owner_id"]
        record = {
            "id": cid,
            "call_id": call_id,
            "kind": _COMMAND_KIND,
            "name": command.name,
            "payload": dict(command.payload),
            "target_owner": owner,
            "status": "pending",
            "attempts": int((existing or {}).get("attempts") or 0),
            "claimed_at": now,
            **self._ttl_fields(now + self._dedup_ttl, now),
        }
        try:
            if existing is None:
                stored = await asyncio.to_thread(store.put_command, record)
            else:
                # 崩溃残留的 pending（超宽限期）：接管续投，预算累计
                stored = await asyncio.to_thread(store.put_command, record,
                                                 existing.get("_etag"))
        except (cosmos.ContainerConflict, cosmos.ContainerPreconditionFailed):
            return CommandAck(command_id=cid, accepted=False,
                              detail="duplicate command_id: routing in flight")
        etag = stored.get("_etag")

        # 有界重投：max_delivery_attempts 为累计预算（跨崩溃/跨调用封顶），
        # 每次尝试先落账再投——进程崩在投递中也不丢计数
        while record["attempts"] < self._max_attempts:
            record["attempts"] += 1
            record["claimed_at"] = self._now()
            try:
                ok = bool(await self._send(owner, command))
            except Exception:  # noqa: BLE001 — 传输异常=投递失败（暂时性计次）
                ok = False
            if ok:
                record["status"] = "delivered"
                record["ack_accepted"] = True
                record["ack_detail"] = f"delivered to {owner}"
                stored = await asyncio.to_thread(store.put_command, record, etag)
                etag = stored.get("_etag") or etag  # etag 每写即变，跨尝试必须跟新
                return CommandAck(command_id=cid, accepted=True,
                                  detail=record["ack_detail"])
            stored = await asyncio.to_thread(store.put_command, record, etag)
            etag = stored.get("_etag") or etag  # 同上：沿用旧 etag 重投≥2 次必 412
        record["status"] = "failed"
        record["ack_accepted"] = False
        record["ack_detail"] = f"delivery attempts exhausted ({record['attempts']})"
        await asyncio.to_thread(store.put_command, record, etag)
        return CommandAck(command_id=cid, accepted=False, detail=record["ack_detail"])

    async def release(self, *, call_id: str, owner_id: str) -> None:
        now = self._now()
        for _ in range(_CLAIM_ATTEMPTS):
            existing = await asyncio.to_thread(self._repo().get_lease, call_id)
            if existing is None:
                return  # 幂等：已释放/不存在
            cur = existing.get("owner_id") or None
            if cur and cur != owner_id and self._live(existing, now):
                raise StaleVersionError(f"release denied for {call_id}: not the owner")
            # 释放=清 owner + 租约立时到期（行随 TTL 秒级删除）。不直接 delete：
            # 窄接口 delete 无 etag 守卫，会与并发 bind 竞态吃掉新 owner 的活租约
            doc = self._lease_doc(
                call_id, owner_id=None,
                reserved_by=existing.get("reserved_by") or owner_id,
                expires_at=now, now=now,
            )
            try:
                await asyncio.to_thread(self._repo().put_lease, doc, existing.get("_etag"))
                return
            except cosmos.ContainerPreconditionFailed:
                continue  # 并发接管：重读再判（接管成功则按非 owner 拒绝）
        raise StaleVersionError(f"release race for {call_id} lost by {owner_id}")

    # ---- §8.3 运维辅助（FC-7 契约外，drain 序列的收口面） ----------------

    async def drain(self, *, owner_id: str | None = None) -> int:
        """释放本副本全部活租约（停机排空）；返回释放条数。

        竞态被接管的行跳过（已不属本副本），不中断整个排空循环。
        """
        me = owner_id or self._owner_id
        now = self._now()
        rows = await asyncio.to_thread(self._repo().list_leases, me, now)
        released = 0
        for row in rows:
            try:
                await self.release(call_id=row["call_id"], owner_id=me)
                released += 1
            except StaleVersionError:
                continue
        return released
