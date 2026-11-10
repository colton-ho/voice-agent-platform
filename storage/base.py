"""FC-6 CallRepository, FC-7 SessionDirectory + shared value types and errors.

Frozen contract — implementation-plan §3.3 (single definition site).
方法集与命名与 §3 完全一致（FC-7 动词严格对齐 spec §5）。
导入纯度约束（U-002 验收）：只允许标准库，禁止 pipecat/boto3/botocore/
aioboto3/azure SDK。
"""
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class CallState(str, Enum):                # spec §8.1 state machine
    RECEIVED = "RECEIVED"
    CONNECTING = "CONNECTING"
    ACTIVE = "ACTIVE"
    HANDOFF_PENDING = "HANDOFF_PENDING"
    HANDED_OFF = "HANDED_OFF"
    ENDED = "ENDED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Frozen exception homes (§3.3): StaleVersionError / CallFinalizedError /
# CapacityExceededError 定义在此；其余异常各有归属（config_contract.py /
# providers/registry.py），不在本模块重复。
# ---------------------------------------------------------------------------


class StaleVersionError(Exception):
    """Optimistic-concurrency version mismatch on update/finalize."""


class CallFinalizedError(Exception):
    """Mutation attempted on a finalized call."""


class CapacityExceededError(Exception):
    """Admission refused — MAX_CONCURRENT_CALLS_PER_REPLICA reached."""


# ---------------------------------------------------------------------------
# Value types (§3.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CallRecord:
    call_id: str
    state: CallState
    started_at: datetime
    tenant_id: str | None = None
    provider_ids: Mapping[str, str] | None = None  # CR-2 豁免：放宽为 Optional（§3.3 冻结的唯一偏差）— e.g. {"twilio_call_sid": ..., "stream_sid": ...}
    ended_at: datetime | None = None
    expires_at: datetime | None = None           # business retention boundary (spec §9.2)
    owner: str | None = None
    handoff_outcome: str | None = None
    version: str | int = 0                       # optimistic-concurrency token


@dataclass(frozen=True)
class CallEvent:
    event_id: str                                  # dedup key
    call_id: str
    event_type: str
    occurred_at: datetime
    payload: Mapping[str, object]                  # sanitized — no audio, no credentials (spec §9.1)


@dataclass(frozen=True)
class HistoryPage:
    items: Sequence[CallRecord]
    next_cursor: str | None


@dataclass(frozen=True)
class Reservation:
    call_id: str
    expires_at: datetime


@dataclass(frozen=True)
class Lease:
    call_id: str
    owner_id: str
    expires_at: datetime


@dataclass(frozen=True)
class Command:
    command_id: str
    name: str
    payload: Mapping[str, object]


@dataclass(frozen=True)
class CommandAck:
    command_id: str
    accepted: bool
    detail: str | None = None


# ---------------------------------------------------------------------------
# FC-6 / FC-7 interfaces (§3.3)
# ---------------------------------------------------------------------------


class CallRepository(ABC):                 # FC-6

    @abstractmethod
    async def create(self, record: CallRecord) -> CallRecord: ...

    @abstractmethod
    async def get(self, call_id: str) -> CallRecord | None: ...

    @abstractmethod
    async def update(self, call_id: str, changes: Mapping[str, object],
                     *, expected_version: str | int) -> CallRecord: ...   # StaleVersionError

    @abstractmethod
    async def append_events(self, call_id: str, events: Sequence[CallEvent]) -> int: ...
        # dedup by event_id; returns appended count; CallFinalizedError on finalized call

    @abstractmethod
    async def finalize(self, call_id: str, *, outcome: Mapping[str, object],
                       expected_version: str | int) -> CallRecord: ...

    @abstractmethod
    async def query_history(self, *, caller: str | None = None,
                            tenant_id: str | None = None,
                            cursor: str | None = None,
                            limit: int = 50) -> HistoryPage: ...


class SessionDirectory(ABC):              # FC-7 — spec §5 verbs exactly

    @abstractmethod
    async def reserve_capacity(self, *, call_id: str, ttl_seconds: int) -> Reservation: ...
        # raises CapacityExceededError (admission per MAX_CONCURRENT_CALLS_PER_REPLICA)

    @abstractmethod
    async def bind_owner(self, *, call_id: str, owner_id: str) -> Lease: ...
        # atomic, at authenticated stream acceptance (spec §8.2)

    @abstractmethod
    async def renew_lease(self, *, call_id: str, owner_id: str) -> Lease: ...

    @abstractmethod
    async def route_command(self, *, call_id: str, command: Command) -> CommandAck: ...
        # idempotent on command_id

    @abstractmethod
    async def release(self, *, call_id: str, owner_id: str) -> None: ...
