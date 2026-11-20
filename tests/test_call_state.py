"""U-306 · call state machine（spec §8.1）测试。纯逻辑：无 I/O、无 sleep、无网络。

覆盖要求：ALLOWED 表每条迁移至少执行一次（参数化遍历表本身）、重复事件、乱序
事件（超前追赶 + 滞后前置）、迟到事件、finalize-then-late-event（不复活）、
幂等重放、非法迁移抛类型化异常。
"""
from datetime import datetime, timezone

import pytest

from storage.base import CallEvent, CallFinalizedError, CallState, StaleVersionError
from storage.call_state import (
    ALLOWED,
    CallStateMachine,
    InvalidTransitionError,
    Outcome,
)

OCCURRED = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)


def ev(kind: str, *, eid: str = "e1", call_id: str = "c1", payload: dict | None = None) -> CallEvent:
    return CallEvent(
        event_id=eid,
        call_id=call_id,
        event_type=kind,
        occurred_at=OCCURRED,
        payload=payload or {},
    )


def machine(state: CallState = CallState.RECEIVED, *, call_id: str = "c1") -> CallStateMachine:
    return CallStateMachine(call_id=call_id, initial=state, clock=lambda: 1234.5)


FAILED_PAYLOAD = {"reason": "upstream 502", "cleanup": "release lease + close ws"}


# ---------------------------------------------------------------------------
# 表完整性：每条迁移至少执行一次（参数化即遍历，防表被悄悄增删）
# ---------------------------------------------------------------------------

PINNED_ALLOWED_KEYS = {
    ("connecting", CallState.RECEIVED),
    ("active", CallState.CONNECTING),
    ("active", CallState.RECEIVED),
    ("handoff_requested", CallState.ACTIVE),
    ("handoff_requested", CallState.CONNECTING),
    ("handoff_requested", CallState.RECEIVED),
    ("handoff_completed", CallState.HANDOFF_PENDING),
    ("handoff_completed", CallState.ACTIVE),
    ("handoff_completed", CallState.CONNECTING),
    ("handoff_completed", CallState.RECEIVED),
    ("handoff_failed", CallState.HANDOFF_PENDING),
    ("ended", CallState.ACTIVE),
    ("ended", CallState.HANDOFF_PENDING),
    ("ended", CallState.CONNECTING),
    ("ended", CallState.RECEIVED),
    ("failed", CallState.RECEIVED),
    ("failed", CallState.CONNECTING),
    ("failed", CallState.ACTIVE),
    ("failed", CallState.HANDOFF_PENDING),
}


def test_allowed_table_is_exactly_the_pinned_transition_set():
    assert set(ALLOWED) == PINNED_ALLOWED_KEYS


@pytest.mark.parametrize(
    "kind,from_state,to_state",
    sorted((k[0], k[1], v) for k, v in ALLOWED.items()),
    ids=[f"{k[0].value if hasattr(k[0], 'value') else k[0]}-from-{k[1].value}" for k in sorted(ALLOWED)],
)
def test_every_allowed_transition_applies(kind, from_state, to_state):
    m = machine(from_state)
    result = m.apply(ev(kind, payload=FAILED_PAYLOAD if kind == "failed" else None))
    assert result.outcome is Outcome.APPLIED
    assert result.previous_state is from_state
    assert result.state is to_state
    assert m.state is to_state
    assert m.version == 1


# ---------------------------------------------------------------------------
# 基本形状
# ---------------------------------------------------------------------------

def test_initial_state_is_received_with_version_zero():
    m = machine()
    assert m.state is CallState.RECEIVED
    assert m.version == 0
    assert m.finalized is False
    assert m.history == ()


def test_injected_clock_stamps_results():
    m = machine(CallState.CONNECTING)
    result = m.apply(ev("active"))
    assert result.at == 1234.5


def test_failed_transition_retains_reason_and_cleanup():
    m = machine(CallState.ACTIVE)
    m.apply(ev("failed", payload=FAILED_PAYLOAD))
    assert m.state is CallState.FAILED
    assert m.failure == FAILED_PAYLOAD


def test_nonterminal_states_are_not_finalized():
    for state in (CallState.RECEIVED, CallState.CONNECTING, CallState.ACTIVE, CallState.HANDOFF_PENDING):
        assert machine(state).finalized is False


@pytest.mark.parametrize(
    "state", [CallState.HANDED_OFF, CallState.ENDED, CallState.FAILED]
)
def test_terminal_states_are_finalized(state):
    assert machine(state).finalized is True


# ---------------------------------------------------------------------------
# 幂等：同 event_id 重投 / 同类不同 id 重发
# ---------------------------------------------------------------------------

def test_duplicate_event_id_is_idempotent_repeat_without_error():
    m = machine(CallState.CONNECTING)
    first = m.apply(ev("active", eid="a1"))
    assert first.outcome is Outcome.APPLIED
    again = m.apply(ev("active", eid="a1"))
    assert again.outcome is Outcome.IDEMPOTENT_REPEAT
    assert again.state is CallState.ACTIVE
    assert m.state is CallState.ACTIVE
    assert m.version == 1  # 幂等重放不递增


def test_idempotent_reapply_same_kind_new_event_id_absorbed_at_current_state():
    m = machine(CallState.CONNECTING)
    m.apply(ev("active", eid="a1"))
    result = m.apply(ev("active", eid="a2"))  # 重发但换了 id，已处于 ACTIVE
    assert result.outcome is Outcome.IDEMPOTENT_REPEAT
    assert result.previous_state is CallState.ACTIVE
    assert m.state is CallState.ACTIVE
    assert m.version == 1


def test_handoff_failed_at_active_is_idempotent_repeat():
    m = machine(CallState.ACTIVE)
    result = m.apply(ev("handoff_failed", eid="hf1"))
    assert result.outcome is Outcome.IDEMPOTENT_REPEAT
    assert m.state is CallState.ACTIVE
    assert m.version == 0


# ---------------------------------------------------------------------------
# 乱序：超前追赶（接受）与滞后前置（拒绝 StaleVersionError）
# ---------------------------------------------------------------------------

def test_out_of_order_successor_before_predecessor_accepted_with_skip():
    # connecting 事件还没到，active 先到：接受追赶（F3），跳过 CONNECTING。
    m = machine(CallState.RECEIVED)
    result = m.apply(ev("active", eid="a1"))
    assert result.outcome is Outcome.APPLIED
    assert result.state is CallState.ACTIVE
    assert result.skipped == (CallState.CONNECTING,)
    assert m.state is CallState.ACTIVE


def test_out_of_order_handoff_requested_from_received_skips_two_states():
    m = machine(CallState.RECEIVED)
    result = m.apply(ev("handoff_requested", eid="hr1"))
    assert result.state is CallState.HANDOFF_PENDING
    assert result.skipped == (CallState.CONNECTING, CallState.ACTIVE)


def test_out_of_order_handoff_completed_from_connecting():
    m = machine(CallState.CONNECTING)
    result = m.apply(ev("handoff_completed", eid="hc1"))
    assert result.state is CallState.HANDED_OFF
    assert result.skipped == (CallState.ACTIVE, CallState.HANDOFF_PENDING)


def test_late_predecessor_rejected_as_stale_version_error():
    # active 已应用后 connecting 前置才到：拒绝（StaleVersionError），不回退。
    m = machine(CallState.ACTIVE)
    with pytest.raises(StaleVersionError):
        m.apply(ev("connecting", eid="c-1"))
    assert m.state is CallState.ACTIVE
    assert m.version == 0
    assert m.history == ()


def test_late_predecessor_after_catch_up_rejected():
    m = machine(CallState.RECEIVED)
    m.apply(ev("active", eid="a1"))  # 追赶跳过 connecting
    with pytest.raises(StaleVersionError):
        m.apply(ev("connecting", eid="c-late"))
    assert m.state is CallState.ACTIVE


# ---------------------------------------------------------------------------
# 迟到事件 / finalize-then-late-event：不复活（U-301 硬不变式同型）
# ---------------------------------------------------------------------------

def test_finalize_ended_then_late_event_must_not_revive():
    m = machine(CallState.ACTIVE)
    m.apply(ev("ended", eid="end1"))
    assert m.state is CallState.ENDED and m.finalized
    with pytest.raises(CallFinalizedError):
        m.apply(ev("failed", eid="f-late", payload=FAILED_PAYLOAD))
    assert m.state is CallState.ENDED  # 不复活


def test_finalize_handed_off_then_late_event_must_not_revive():
    m = machine(CallState.HANDOFF_PENDING)
    m.apply(ev("handoff_completed", eid="hc1"))
    assert m.state is CallState.HANDED_OFF
    with pytest.raises(CallFinalizedError):
        m.apply(ev("ended", eid="end-late"))
    assert m.state is CallState.HANDED_OFF


def test_finalize_failed_then_late_event_must_not_revive():
    m = machine(CallState.CONNECTING)
    m.apply(ev("failed", eid="f1", payload=FAILED_PAYLOAD))
    with pytest.raises(CallFinalizedError):
        m.apply(ev("ended", eid="end-late"))
    assert m.state is CallState.FAILED


def test_new_event_matching_final_kind_after_finalize_is_still_late():
    # ENDED 后又来一个 ended（不同 id）：迟到事件，拒绝——幂等只留给同 event_id。
    m = machine(CallState.ACTIVE)
    m.apply(ev("ended", eid="end1"))
    with pytest.raises(CallFinalizedError):
        m.apply(ev("ended", eid="end2"))
    assert m.state is CallState.ENDED


def test_same_event_redelivery_on_finalized_call_is_idempotent_not_error():
    # 有意偏离 U-301 检查顺序（docstring 已记录）：同 event_id 重投跨终态仍幂等吸收。
    m = machine(CallState.ACTIVE)
    m.apply(ev("ended", eid="end1"))
    result = m.apply(ev("ended", eid="end1"))
    assert result.outcome is Outcome.IDEMPOTENT_REPEAT
    assert m.state is CallState.ENDED


# ---------------------------------------------------------------------------
# 非法输入：类型化 InvalidTransitionError
# ---------------------------------------------------------------------------

def test_unknown_event_type_rejected():
    m = machine(CallState.ACTIVE)
    with pytest.raises(InvalidTransitionError):
        m.apply(ev("exploded", eid="x1"))
    assert m.state is CallState.ACTIVE


def test_foreign_call_id_rejected():
    m = machine(CallState.ACTIVE, call_id="c1")
    with pytest.raises(InvalidTransitionError):
        m.apply(ev("ended", eid="x1", call_id="other-call"))
    assert m.state is CallState.ACTIVE


@pytest.mark.parametrize("from_state", [CallState.RECEIVED, CallState.CONNECTING])
def test_handoff_failed_before_active_is_structurally_impossible(from_state):
    # F2 边界：handoff_failed 只在 HANDOFF_PENDING → ACTIVE 合法；ACTIVE 之前无追赶。
    m = machine(from_state)
    with pytest.raises(InvalidTransitionError):
        m.apply(ev("handoff_failed", eid="hf1"))
    assert m.state is from_state


def test_failed_without_reason_rejected():
    m = machine(CallState.ACTIVE)
    with pytest.raises(InvalidTransitionError):
        m.apply(ev("failed", eid="f1", payload={"cleanup": "hang up"}))
    assert m.state is CallState.ACTIVE
    assert m.version == 0


def test_failed_without_cleanup_rejected():
    m = machine(CallState.ACTIVE)
    with pytest.raises(InvalidTransitionError):
        m.apply(ev("failed", eid="f1", payload={"reason": "boom"}))
    assert m.state is CallState.ACTIVE


def test_failed_with_blank_reason_rejected():
    m = machine(CallState.ACTIVE)
    with pytest.raises(InvalidTransitionError):
        m.apply(ev("failed", eid="f1", payload={"reason": "  ", "cleanup": "hang up"}))


def test_handoff_failed_backward_edge_applies():
    # F2：转接忙/无应答/取消后通话继续（§6.4 no caller drop）。
    m = machine(CallState.HANDOFF_PENDING)
    result = m.apply(ev("handoff_failed", eid="hf1", payload={"reason": "busy"}))
    assert result.outcome is Outcome.APPLIED
    assert result.state is CallState.ACTIVE
    assert result.skipped == ()


# ---------------------------------------------------------------------------
# 版本（OCC 形態，对齐 CallRepository.update）
# ---------------------------------------------------------------------------

def test_expected_version_mismatch_raises_stale_version_error():
    m = machine(CallState.RECEIVED)
    with pytest.raises(StaleVersionError):
        m.apply(ev("connecting", eid="c1"), expected_version=5)
    assert m.state is CallState.RECEIVED


def test_expected_version_match_applies_and_increments():
    m = machine(CallState.RECEIVED)
    result = m.apply(ev("connecting", eid="c1"), expected_version=0)
    assert result.state is CallState.CONNECTING
    assert result.version == 1
    assert m.version == 1


def test_version_increments_only_on_applied():
    m = machine(CallState.CONNECTING)
    m.apply(ev("active", eid="a1"))          # APPLIED → 1
    m.apply(ev("active", eid="a1"))          # 重复 id → 不递增
    m.apply(ev("active", eid="a2"))          # 同类新 id → 不递增
    m.apply(ev("ended", eid="e1"))           # APPLIED → 2
    assert m.version == 2


def test_history_records_accepted_results_in_order():
    m = machine(CallState.RECEIVED)
    m.apply(ev("active", eid="a1"))
    m.apply(ev("active", eid="a1"))
    m.apply(ev("ended", eid="e1"))
    outcomes = [(r.event_id, r.outcome, r.previous_state, r.state) for r in m.history]
    assert outcomes == [
        ("a1", Outcome.APPLIED, CallState.RECEIVED, CallState.ACTIVE),
        ("a1", Outcome.IDEMPOTENT_REPEAT, CallState.ACTIVE, CallState.ACTIVE),
        ("e1", Outcome.APPLIED, CallState.ACTIVE, CallState.ENDED),
    ]


def test_rejected_events_leave_machine_untouched():
    m = machine(CallState.ACTIVE)
    for bad in (
        lambda: m.apply(ev("connecting", eid="b1")),                       # stale → StaleVersionError
        lambda: m.apply(ev("exploded", eid="b2")),                         # unknown kind
        lambda: m.apply(ev("failed", eid="b3", payload={"reason": "x"})),  # 缺 cleanup
    ):
        with pytest.raises((StaleVersionError, InvalidTransitionError)):
            bad()
    assert m.state is CallState.ACTIVE
    assert m.version == 0
    assert m.history == ()
