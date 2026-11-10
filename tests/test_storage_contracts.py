"""U-002 contract tests — FC-6, FC-7 structural shape (light).

同 test_provider_contracts：只验结构（ABC 拒绝直接实例化、stub 暴露且仅暴露
§3 冻结方法集、状态机与异常齐备）。存储行为测试（一致性、幂等、容量）属于后续阶段。
"""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from storage.base import (
    CallEvent,
    CallFinalizedError,
    CallRecord,
    CallRepository,
    CallState,
    CapacityExceededError,
    Command,
    CommandAck,
    HistoryPage,
    Lease,
    Reservation,
    SessionDirectory,
    StaleVersionError,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class StubCallRepository(CallRepository):
    async def create(self, record):
        return record

    async def get(self, call_id):
        return None

    async def update(self, call_id, changes, *, expected_version):
        raise NotImplementedError

    async def append_events(self, call_id, events):
        return 0

    async def finalize(self, call_id, *, outcome, expected_version):
        raise NotImplementedError

    async def query_history(self, *, caller=None, tenant_id=None, cursor=None, limit=50):
        return HistoryPage(items=(), next_cursor=None)


class StubSessionDirectory(SessionDirectory):
    async def reserve_capacity(self, *, call_id, ttl_seconds):
        return Reservation(call_id=call_id, expires_at=NOW)

    async def bind_owner(self, *, call_id, owner_id):
        return Lease(call_id=call_id, owner_id=owner_id, expires_at=NOW)

    async def renew_lease(self, *, call_id, owner_id):
        return Lease(call_id=call_id, owner_id=owner_id, expires_at=NOW)

    async def route_command(self, *, call_id, command):
        return CommandAck(command_id=command.command_id, accepted=True)

    async def release(self, *, call_id, owner_id):
        return None


FROZEN_METHODS = {
    CallRepository: {"create", "get", "update", "append_events", "finalize", "query_history"},
    SessionDirectory: {"reserve_capacity", "bind_owner", "renew_lease", "route_command", "release"},
}

STUBS = {CallRepository: StubCallRepository, SessionDirectory: StubSessionDirectory}


@pytest.mark.parametrize("contract", [CallRepository, SessionDirectory])
def test_abc_rejects_direct_instantiation(contract):
    with pytest.raises(TypeError):
        contract()


@pytest.mark.parametrize("contract", [CallRepository, SessionDirectory])
def test_stub_subclass_instantiates_and_exposes_frozen_members(contract):
    assert set(contract.__abstractmethods__) == FROZEN_METHODS[contract]
    stub = STUBS[contract]()
    for name in FROZEN_METHODS[contract]:
        assert callable(getattr(stub, name)), name


def test_call_state_machine_is_frozen():
    # spec §8.1 状态机七个状态，名称即值（str Enum），不得增删
    assert [s.name for s in CallState] == [
        "RECEIVED", "CONNECTING", "ACTIVE", "HANDOFF_PENDING", "HANDED_OFF", "ENDED", "FAILED",
    ]
    assert all(s.value == s.name for s in CallState)


@pytest.mark.parametrize(
    "exc", [StaleVersionError, CallFinalizedError, CapacityExceededError]
)
def test_frozen_storage_errors_are_distinct_exceptions(exc):
    assert issubclass(exc, Exception)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CallRecord(call_id="c1", state=CallState.RECEIVED, started_at=NOW),
        lambda: CallEvent(event_id="e1", call_id="c1", event_type="t", occurred_at=NOW, payload={}),
        lambda: HistoryPage(items=(), next_cursor=None),
        lambda: Reservation(call_id="c1", expires_at=NOW),
        lambda: Lease(call_id="c1", owner_id="o1", expires_at=NOW),
        lambda: Command(command_id="x", name="n", payload={}),
        lambda: CommandAck(command_id="x", accepted=True),
    ],
    ids=["CallRecord", "CallEvent", "HistoryPage", "Reservation", "Lease", "Command", "CommandAck"],
)
def test_storage_value_types_are_frozen_dataclasses(factory):
    obj = factory()
    with pytest.raises(FrozenInstanceError):
        obj.contract_frozen_probe = 1
