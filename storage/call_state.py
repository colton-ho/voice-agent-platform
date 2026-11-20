"""Call state machine — spec §8.1（U-306，R-A-013）。纯逻辑，无 I/O，时钟可注入。

§8.1 状态图（状态字面量全部来自 spec 文本；枚举单一定义点在 storage/base.py
的冻结 `CallState`，本模块只引用不另立）：

    RECEIVED → CONNECTING → ACTIVE → HANDOFF_PENDING → HANDED_OFF
                             └────────────────────→ ENDED
    Any nonterminal state → FAILED, with a reason and cleanup action

终态：HANDED_OFF / ENDED / FAILED；非终态：RECEIVED / CONNECTING / ACTIVE /
HANDOFF_PENDING（§8.1「Any nonterminal state → FAILED」反推——HANDED_OFF 位于
链条末端，不在 nonterminal 集内）。

允许迁移表（`ALLOWED`，(event_type, 当前态) → 目标态）逐条来源：

  §8.1 原文边（不加不减）：
    connecting        RECEIVED → CONNECTING
    active            CONNECTING → ACTIVE
    handoff_requested ACTIVE → HANDOFF_PENDING
    handoff_completed HANDOFF_PENDING → HANDED_OFF
    ended             ACTIVE → ENDED
    failed            任一非终态 → FAILED（附 reason + cleanup action）

  缺口补全（§8.1 未写、逐条声明的 fill）：
    F1  ended 也允许 RECEIVED / CONNECTING / HANDOFF_PENDING → ENDED。
        理由：§8.1 只画出 ACTIVE→ENDED，但 §6.1.7 早挂断与 handoff 期间挂机是
        正常拆线，若只准落 FAILED 会把正常挂断误记为失败。ENDED=正常终结。
    F2  handoff_failed：HANDOFF_PENDING → ACTIVE（唯一回退边）。理由：§6.4
        「no caller drop on busy/no-answer」——转接忙/无应答/取消后通话继续，
        只能退回 ACTIVE；除此边外状态机单调向前。
    F3  连锁追赶（catch-up）：connecting/active/handoff_requested/
        handoff_completed 若当前态在主链上早于目标态，接受并跳过中间态
        （例：RECEIVED 直接收到 active → ACTIVE，skipped=(CONNECTING,)）。
        理由：§8.1「Treat provider event ordering as untrusted」——到达序不可
        信，拒绝会丢真实信号；接受追赶使状态收敛到「已知的最远事实」，被跳过
        的前置事件迟到时按滞后前置处理（见下）。

事件去重与乱序语义（与 U-301 对齐，不另立语义）：

  - event_id 是冪等去重键——与 CallEvent.event_id / cosmos_calls.append_events
    的 dedup-append（`create_item` 遇 ContainerConflict 吞掉）同一形態：调用方
    提供不透明字符串键，精确匹配去重，不做序号/哈希派生。
  - 同一 event_id 重复投递 → 类型化 `Outcome.IDEMPOTENT_REPEAT` 结果，状态不变、
    不报错、永不复活终态调用。**一处有意偏离 U-301 的检查顺序**：U-301 在
    finalized 行上先抛 CallFinalizedError 再吞重复；本机先查 dedup 再查终态，
    因为 U-306 验收明确要求「applying the same event twice yields the same state
    without error」。「终态后不复活」两边一致。落库仍按 U-301 顺序（append 先
    报 CallFinalizedError）——本模块只管迁移判定。
  - 超前乱序事件（事件先于其前置到达，目标态在主链上晚于当前态）→ 接受追赶
    （F3），TransitionResult.skipped 记录被跳过的中间态。
  - 滞后前置事件（目标态已是走过的状态，如 ACTIVE 时收到 connecting）→
    拒绝并抛 `StaleVersionError`（复用 storage/base.py 冻结异常，与 U-301
    update/finalize 的 stale-version 拒绝同型；不另立平行异常层级）。
  - 终态后的新事件（不同 event_id）→ 抛 `CallFinalizedError`，状态不变——
    「A late event must not revive a finalized call」（与 U-301 finalize 硬不变式
    同型）。即使事件种类恰与终态相同（如 ENDED 后又来一个 ended 新 id）也按
    迟到事件拒绝——幂等重放只保留给同一 event_id。
  - 结构性不可能（未知 event_type、handoff_failed 出现在 ACTIVE 之前、failed
    缺 reason/cleanup、外来 call_id）→ 抛 `InvalidTransitionError`（本模块唯一
    新增异常，派生自 ValueError；late/stale 复用冻结的 CallFinalizedError /
    StaleVersionError，无平行层级）。

可选 `expected_version`（对齐 CallRepository.update 的 OCC 形態）：与迁移自增的
`version` 不符 → StaleVersionError；仅 APPLIED 递增版本。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping

from storage.base import (  # 冻结契约单一定义点（storage/base.py）
    CallEvent,
    CallFinalizedError,
    CallState,
    StaleVersionError,
)

__all__ = [
    "ALLOWED",
    "CallEvent",
    "CallFinalizedError",
    "CallState",
    "CallStateMachine",
    "InvalidTransitionError",
    "Outcome",
    "StaleVersionError",
    "TARGET",
    "TERMINAL",
    "TransitionResult",
]


class InvalidTransitionError(ValueError):
    """U-306 唯一新增异常：结构性不可能的输入/迁移（非 late/stale 场景）。"""


# 主链（§8.1 状态图一行）——用于追赶/陈旧的方向判定。
_CHAIN = (
    CallState.RECEIVED,
    CallState.CONNECTING,
    CallState.ACTIVE,
    CallState.HANDOFF_PENDING,
    CallState.HANDED_OFF,
)
_RANK = {s: i for i, s in enumerate(_CHAIN)}

TERMINAL = frozenset({CallState.HANDED_OFF, CallState.ENDED, CallState.FAILED})
NONTERMINAL = frozenset(CallState) - TERMINAL

# event_type → 目标态（§8.1 事件词汇本模块持有；未知键即 InvalidTransitionError）
TARGET: dict[str, CallState] = {
    "connecting": CallState.CONNECTING,
    "active": CallState.ACTIVE,
    "handoff_requested": CallState.HANDOFF_PENDING,
    "handoff_completed": CallState.HANDED_OFF,
    "handoff_failed": CallState.ACTIVE,
    "ended": CallState.ENDED,
    "failed": CallState.FAILED,
}

# (event_type, 当前态) → 目标态。逐条来源见模块 docstring（§8.1 原文边 + F1/F2/F3）。
ALLOWED: dict[tuple[str, CallState], CallState] = {
    ("connecting", CallState.RECEIVED): CallState.CONNECTING,                     # §8.1
    ("active", CallState.CONNECTING): CallState.ACTIVE,                           # §8.1
    ("active", CallState.RECEIVED): CallState.ACTIVE,                             # F3 追赶
    ("handoff_requested", CallState.ACTIVE): CallState.HANDOFF_PENDING,           # §8.1
    ("handoff_requested", CallState.CONNECTING): CallState.HANDOFF_PENDING,       # F3 追赶
    ("handoff_requested", CallState.RECEIVED): CallState.HANDOFF_PENDING,         # F3 追赶
    ("handoff_completed", CallState.HANDOFF_PENDING): CallState.HANDED_OFF,       # §8.1
    ("handoff_completed", CallState.ACTIVE): CallState.HANDED_OFF,                # F3 追赶
    ("handoff_completed", CallState.CONNECTING): CallState.HANDED_OFF,            # F3 追赶
    ("handoff_completed", CallState.RECEIVED): CallState.HANDED_OFF,              # F3 追赶
    ("handoff_failed", CallState.HANDOFF_PENDING): CallState.ACTIVE,              # F2 回退边
    ("ended", CallState.ACTIVE): CallState.ENDED,                                 # §8.1
    ("ended", CallState.HANDOFF_PENDING): CallState.ENDED,                        # F1
    ("ended", CallState.CONNECTING): CallState.ENDED,                             # F1
    ("ended", CallState.RECEIVED): CallState.ENDED,                               # F1
    ("failed", CallState.RECEIVED): CallState.FAILED,                             # §8.1
    ("failed", CallState.CONNECTING): CallState.FAILED,                           # §8.1
    ("failed", CallState.ACTIVE): CallState.FAILED,                               # §8.1
    ("failed", CallState.HANDOFF_PENDING): CallState.FAILED,                      # §8.1
}


class Outcome(str, Enum):
    APPLIED = "applied"
    IDEMPOTENT_REPEAT = "idempotent_repeat"   # 同 event_id 重投 / 事件蕴含当前态


@dataclass(frozen=True)
class TransitionResult:
    outcome: Outcome
    event_id: str
    event_type: str
    previous_state: CallState
    state: CallState                 # 应用后状态（非 APPLIED 时等于 previous_state）
    version: int                     # 应用后版本（仅 APPLIED 递增）
    skipped: tuple[CallState, ...] = ()   # F3 追赶跳过的中间态
    at: float = 0.0                  # 注入时钟时间戳


@dataclass
class CallStateMachine:
    """单通话状态机：纯逻辑、无 I/O、时钟可注入。

    `initial` 默认 RECEIVED（§8.1 入口）；允许以任意状态启动（测试镶条 /
    崩溃后按持久化状态重建）。
    """

    call_id: str
    initial: CallState = CallState.RECEIVED
    clock: Callable[[], float] = time.time
    _state: CallState = field(init=False)
    _version: int = field(default=0, init=False)
    _seen: set = field(default_factory=set, init=False)
    _history: list = field(default_factory=list, init=False)
    _failure: Mapping[str, object] | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        self._state = self.initial

    # -- 观察面 -------------------------------------------------------------
    @property
    def state(self) -> CallState:
        return self._state

    @property
    def version(self) -> int:
        return self._version

    @property
    def finalized(self) -> bool:
        return self._state in TERMINAL

    @property
    def failure(self) -> Mapping[str, object] | None:
        """FAILED 时的 reason + cleanup action（§8.1 要求随终态保留）。"""
        return self._failure

    @property
    def history(self) -> tuple:
        return tuple(self._history)

    # -- 迁移 ---------------------------------------------------------------
    def apply(self, event: CallEvent, *, expected_version: int | None = None) -> TransitionResult:
        kind = event.event_type
        if event.call_id != self.call_id:
            raise InvalidTransitionError(
                f"event {event.event_id!r} belongs to call {event.call_id!r}, not {self.call_id!r}"
            )
        if kind not in TARGET:
            raise InvalidTransitionError(f"unknown event_type {kind!r}")
        # dedup 先于终态检查——同一 event_id 重投永不出错、永不复活（见 docstring）。
        if event.event_id in self._seen:
            return self._record(Outcome.IDEMPOTENT_REPEAT, event, self._state, self._state, ())
        if self._state in TERMINAL:
            # 迟到新事件：拒绝且不复活（U-301 finalize 硬不变式同型）。
            raise CallFinalizedError(self.call_id)
        if expected_version is not None and expected_version != self._version:
            raise StaleVersionError(
                f"{self.call_id}: expected version {expected_version}, got {self._version}"
            )
        if kind == "failed":
            self._require_reason_cleanup(event)
        key = (kind, self._state)
        if key in ALLOWED:
            previous = self._state
            to = ALLOWED[key]
            skipped = self._skipped(previous, to)
            self._state = to
            self._version += 1
            if to is CallState.FAILED:
                self._failure = dict(event.payload)
            return self._record(Outcome.APPLIED, event, previous, to, skipped)
        if TARGET[kind] is self._state:
            # 事件蕴含当前态（同类不同 id 重发）：幂等吸收，状态不变。
            return self._record(Outcome.IDEMPOTENT_REPEAT, event, self._state, self._state, ())
        t_rank, c_rank = _RANK.get(TARGET[kind]), _RANK.get(self._state)
        if t_rank is not None and c_rank is not None and t_rank < c_rank:
            # 滞后前置事件（目标态已走过）：拒绝，StaleVersionError 同 U-301 语义。
            raise StaleVersionError(
                f"{self.call_id}: stale {kind!r} implies {TARGET[kind].value}, "
                f"already at {self._state.value}"
            )
        raise InvalidTransitionError(
            f"transition {kind!r} from {self._state.value} is not in the §8.1 table"
        )

    # -- 内部 --------------------------------------------------------------
    @staticmethod
    def _skipped(from_state: CallState, to: CallState) -> tuple:
        f, t = _RANK.get(from_state), _RANK.get(to)
        if f is None or t is None or t - f <= 1:
            return ()
        return tuple(_CHAIN[i] for i in range(f + 1, t))

    @staticmethod
    def _require_reason_cleanup(event: CallEvent) -> None:
        # §8.1：FAILED 必须带 reason 与 cleanup action，缺一不收。
        reason = event.payload.get("reason")
        cleanup = event.payload.get("cleanup")
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidTransitionError("failed event requires a non-empty payload['reason'] (§8.1)")
        if not isinstance(cleanup, str) or not cleanup.strip():
            raise InvalidTransitionError("failed event requires a non-empty payload['cleanup'] (§8.1)")

    def _record(self, outcome: Outcome, event: CallEvent, previous: CallState,
                state: CallState, skipped: tuple) -> TransitionResult:
        self._seen.add(event.event_id)
        result = TransitionResult(
            outcome=outcome,
            event_id=event.event_id,
            event_type=event.event_type,
            previous_state=previous,
            state=state,
            version=self._version,
            skipped=skipped,
            at=self.clock(),
        )
        self._history.append(result)
        return result
