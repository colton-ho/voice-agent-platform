"""U-405 · drain-status helper tests (scripts/deploy/drain_status.py).

Covers the pure drain logic (live filter, owner attribution mirroring
storage/ownership.list_leases, drain-complete criterion, hangup command shape,
deadline clamp) and the forced-BYE delivery (force_end) against the real
CosmosSessionDirectory with the FakeOwnershipStore pattern from
tests/test_ownership.py. Deterministic: injected clocks, no sleep, no network.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from storage import cosmos  # noqa: E402
from storage.base import Command  # noqa: E402
from storage.ownership import (  # noqa: E402
    CosmosSessionDirectory,
    reset_store_provider,
    set_store_provider,
)

_spec = importlib.util.spec_from_file_location(
    "drain_status", ROOT / "scripts" / "deploy" / "drain_status.py"
)
drain_status = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(drain_status)

T0 = 1_700_000_000.0

ContainerConflict = cosmos.ContainerConflict
ContainerNotFound = cosmos.ContainerNotFound
ContainerPreconditionFailed = cosmos.ContainerPreconditionFailed


class Clock:
    def __init__(self, t: float = T0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


class FakeOwnershipStore:
    """ownership 窄接口内存替身（tests/test_ownership.py 同款语义）。"""

    def __init__(self):
        self.rows: dict = {}
        self._etag = iter(range(1, 10_000))

    def _stamp(self, doc: dict) -> dict:
        out = dict(doc)
        out["_etag"] = str(next(self._etag))
        return out

    def seed(self, kind: str, doc: dict) -> dict:
        stored = self._stamp(doc)
        self.rows[(kind, doc["id"])] = stored
        return dict(stored)

    def _put(self, key, doc: dict, etag):
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


class FakeTransport:
    """owner-addressable 传输替身：记录投递，按序回结果（空则 True）。"""

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


def lease(call_id: str, *, owner: str | None, reserved_by: str,
          expires_at: float) -> dict:
    row = {
        "id": call_id,
        "call_id": call_id,
        "kind": "lease",
        "reserved_by": reserved_by,
        "lease_expires_at": float(expires_at),
    }
    if owner is not None:
        row["owner_id"] = owner
    return row


# ---------------------------------------------------------------------------
# live_rows / attribute_rows / remaining_seconds
# ---------------------------------------------------------------------------

def test_live_rows_keeps_live_leases_only():
    rows = [
        lease("c-live", owner="r1", reserved_by="r1", expires_at=T0 + 5),
        lease("c-dead", owner="r1", reserved_by="r1", expires_at=T0),
        {"id": "cmd-1", "call_id": "c-live", "kind": "command"},
    ]
    out = drain_status.live_rows(rows, T0)
    assert [r["call_id"] for r in out] == ["c-live"]


def test_live_rows_treats_missing_expiry_as_dead():
    rows = [{"id": "c1", "call_id": "c1", "kind": "lease"}]
    assert drain_status.live_rows(rows, T0) == []


def test_attribute_rows_bound_owner_and_unbound_reserver():
    rows = [
        lease("c1", owner="r1", reserved_by="r1", expires_at=T0 + 5),
        lease("c2", owner=None, reserved_by="r1", expires_at=T0 + 5),
        lease("c3", owner="r2", reserved_by="r2", expires_at=T0 + 5),
        lease("c4", owner=None, reserved_by="r2", expires_at=T0 + 5),
    ]
    out = drain_status.attribute_rows(rows, ["r1"])
    assert sorted(r["call_id"] for r in out) == ["c1", "c2"]


def test_attribute_rows_excludes_foreign_and_empty_owner_sets():
    rows = [lease("c1", owner="r2", reserved_by="r2", expires_at=T0 + 5)]
    assert drain_status.attribute_rows(rows, ["r1"]) == []
    assert drain_status.attribute_rows(rows, []) == []


def test_remaining_seconds_clamps_at_zero():
    assert drain_status.remaining_seconds(T0, T0 + 12) == 12
    assert drain_status.remaining_seconds(T0, T0 - 3) == 0


# ---------------------------------------------------------------------------
# census / is_drain_complete
# ---------------------------------------------------------------------------

def test_census_totals_per_owner_and_holder_labels():
    rows = [
        lease("c1", owner="r1", reserved_by="r1", expires_at=T0 + 30),
        lease("c2", owner="r1", reserved_by="r1", expires_at=T0 + 10),
        lease("c3", owner=None, reserved_by="r1", expires_at=T0 + 20),
        lease("c4", owner="r2", reserved_by="r2", expires_at=T0 + 30),
        lease("c5", owner="r2", reserved_by="r2", expires_at=T0),  # expired
    ]
    result = drain_status.census(rows, ["r1", "r2"], T0)
    assert result["total"] == 4
    assert result["per_owner"] == {"r1": 3, "r2": 1}
    holders = {c["call_id"]: c["holder"] for c in result["calls"]}
    assert holders["c1"] == "r1"
    assert holders["c3"] == "reserved:r1"
    remaining = {c["call_id"]: c["remaining_seconds"] for c in result["calls"]}
    assert remaining["c1"] == 30
    assert remaining["c3"] == 20


def test_is_drain_complete_scenarios():
    live = [lease("c1", owner="r1", reserved_by="r1", expires_at=T0 + 30)]
    expired = [lease("c2", owner="r1", reserved_by="r1", expires_at=T0)]
    foreign = [lease("c3", owner="r9", reserved_by="r9", expires_at=T0 + 30)]
    assert drain_status.is_drain_complete([], ["r1"], T0) is True
    assert drain_status.is_drain_complete(live, ["r1"], T0) is False
    assert drain_status.is_drain_complete(live, ["r2"], T0) is True
    assert drain_status.is_drain_complete(expired, ["r1"], T0) is True
    assert drain_status.is_drain_complete(foreign, ["r1"], T0) is True


# ---------------------------------------------------------------------------
# build_hangup_command
# ---------------------------------------------------------------------------

def test_build_hangup_command_shape():
    command = drain_status.build_hangup_command("c1", "forced-drain U-405", "cid-1")
    assert isinstance(command, Command)
    assert command.command_id == "cid-1"
    assert command.name == "hangup"
    assert command.payload == {"reason": "forced-drain U-405", "call_id": "c1"}


# ---------------------------------------------------------------------------
# force_end (announced forced-BYE) against the real CosmosSessionDirectory
# ---------------------------------------------------------------------------

def _seed_live_call(store: FakeOwnershipStore, call_id: str, owner: str) -> None:
    store.seed("lease", lease(call_id, owner=owner, reserved_by=owner,
                              expires_at=T0 + 30))


def make_dir(store, clock, *, transport=None) -> CosmosSessionDirectory:
    send = transport.send if transport is not None else None
    return CosmosSessionDirectory(
        owner_id="drain-tool",
        max_concurrent_calls=10 ** 9,
        send=send,
        clock=clock,
        store=store,
    )


def ids(call_id: str) -> str:
    return "forced-drain-%s-test" % call_id


@pytest.mark.asyncio
async def test_force_end_unwired_transport_reports_not_accepted():
    clock, store = Clock(), FakeOwnershipStore()
    _seed_live_call(store, "c1", "r1")
    directory = make_dir(store, clock, transport=None)
    results = await drain_status.force_end(
        directory, store.list_leases("r1", T0), ["r1"], T0,
        "forced-drain U-405", command_id_factory=ids,
    )
    assert len(results) == 1
    assert results[0]["accepted"] is False
    assert "no owner-addressable transport" in results[0]["detail"]
    # 不落命令簿记：没投递就没账
    assert store.get_command("c1", "forced-drain-c1-test") is None


@pytest.mark.asyncio
async def test_force_end_delivers_hangup_and_records_bookkeeping():
    clock, store = Clock(), FakeOwnershipStore()
    _seed_live_call(store, "c1", "r1")
    transport = FakeTransport()
    directory = make_dir(store, clock, transport=transport)
    results = await drain_status.force_end(
        directory, store.list_leases("r1", T0), ["r1"], T0,
        "forced-drain U-405", command_id_factory=ids,
    )
    assert [r["accepted"] for r in results] == [True]
    assert [r["command_id"] for r in results] == ["forced-drain-c1-test"]
    # 投递目标 = 租约 owner（replica 名）
    assert [(owner, command.name) for owner, command in transport.calls] == [
        ("r1", "hangup")
    ]
    assert transport.calls[0][1].payload == {
        "reason": "forced-drain U-405", "call_id": "c1",
    }
    row = store.get_command("c1", "forced-drain-c1-test")
    assert row is not None
    assert row["status"] == "delivered"
    assert row["target_owner"] == "r1"


@pytest.mark.asyncio
async def test_force_end_targets_only_attributed_live_calls():
    clock, store = Clock(), FakeOwnershipStore()
    _seed_live_call(store, "c1", "r1")
    store.seed("lease", lease("c-expired", owner="r1", reserved_by="r1",
                              expires_at=T0))
    store.seed("lease", lease("c-foreign", owner="r2", reserved_by="r2",
                              expires_at=T0 + 30))
    store.seed("lease", lease("c-unbound", owner=None, reserved_by="r1",
                              expires_at=T0 + 30))
    transport = FakeTransport()
    directory = make_dir(store, clock, transport=transport)
    results = await drain_status.force_end(
        directory, store.list_leases("r1", T0), ["r1"], T0,
        "forced-drain U-405", command_id_factory=ids,
    )
    assert sorted(r["call_id"] for r in results) == ["c-unbound", "c1"]
    by_call = {r["call_id"]: r for r in results}
    assert by_call["c1"]["accepted"] is True
    # 未绑定预订没有 owner 可投——route_command 如实拒（"owner lost: no live
    # lease"），force_end 如实上报，不装作已结束该通电话
    assert by_call["c-unbound"]["accepted"] is False
    assert "owner lost" in by_call["c-unbound"]["detail"]


@pytest.mark.asyncio
async def test_force_end_delivery_failure_reported_not_accepted():
    clock, store = Clock(), FakeOwnershipStore()
    _seed_live_call(store, "c1", "r1")
    transport = FakeTransport(False, False, False)  # 3 = max_delivery_attempts 默认预算
    directory = make_dir(store, clock, transport=transport)
    results = await drain_status.force_end(
        directory, store.list_leases("r1", T0), ["r1"], T0,
        "forced-drain U-405", command_id_factory=ids,
    )
    assert results[0]["accepted"] is False
    assert "attempts exhausted" in results[0]["detail"]
