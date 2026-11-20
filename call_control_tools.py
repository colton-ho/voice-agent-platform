"""LLM tools that let the pipeline gracefully end the active session.

Used from all four pipeline entry points (phone Nova Sonic, phone three-stage,
web Nova Sonic, web three-stage). The ``scope`` kwarg of
``register_call_control_handlers`` selects between the channels: phone scope
expects DDB / SIP-BYE callbacks (``mark_transfer`` / ``write_outcome`` /
``history_append``), web scope passes ``None`` for those — the handlers
are tolerant of any combination so the same code path covers both.

The tool *description* text the LLM sees still differs by channel (the
registry in ``tools/registry.py`` mentions both phone and web semantics).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from loguru import logger as log

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import EndTaskFrame
from providers.telephony.twilio_adapter import (
    TRANSFER_FALLBACK_SAY,
    TRANSFER_GOODBYE_SAY,
    build_twiml_dial_transfer,
    build_twiml_say_hangup,
)

GRACE_SECONDS: float = 2.0

VALID_END_REASONS: frozenset[str] = frozenset(
    {"user_requested", "task_completed", "transfer_requested", "abusive"}
)

EmitFn = Callable[[dict[str, Any]], Awaitable[None]]
HistoryAppendFn = Callable[[str, dict[str, Any]], None]
MarkTransferFn = Callable[[str], None]
WriteOutcomeFn = Callable[[], Awaitable[bool]]


def call_control_tool_schemas() -> ToolsSchema:
    """Return the cross-provider tool schema for end_call + transfer_to_human.

    Kept for backwards compatibility; T5 pipelines normally pull schemas
    via ``tools.registry.assemble_tools_schema`` instead, which honors the
    demo's per-id allow-list.
    """
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(
                name="end_call",
                description=(
                    "End the current call. Call ONLY when the user clearly "
                    "asks to hang up / says they are done, OR when the "
                    "support task is fully resolved AND the user has "
                    "acknowledged. Always speak a brief polite farewell "
                    "BEFORE calling this tool. "
                    # scope=web 时 end_call 关闭 WS,scope=phone 时触发 SIP BYE.
                    "On phone leg this terminates the PSTN call (SIP BYE); "
                    "on web it closes the WebSocket session and only "
                    "records intent."
                ),
                properties={
                    "reason": {
                        "type": "string",
                        "enum": sorted(VALID_END_REASONS),
                        "description": "Why the call is ending.",
                    }
                },
                required=["reason"],
            ),
            FunctionSchema(
                name="transfer_to_human",
                description=(
                    "Indicate the caller wants a human agent. The system "
                    "logs the request and ends the call. Do NOT promise a "
                    "callback time. On phone leg the call hangs up via "
                    "SIP BYE; on web the WebSocket is closed and only the "
                    "intent is recorded."
                ),
                properties={
                    "topic": {
                        "type": "string",
                        "description": "Short reason the caller wants a human.",
                    }
                },
                required=[],
            ),
        ]
    )


# ---------------------------------------------------------------------------
# §6.4 human handoff（U-206；decision D-5 门控，A-2 FINAL）。
#
# D-5 默认（用户裁决，保持不动）：record+hangup 语义是默认——
# transfer_requested + transfer_topic 必须在 teardown 之前落账
# （write-before-teardown，R-P-005），然后结束通话。§6.4 目标形态
# （in-repo Twilio <Dial> 转接，A-2 裁决取代外挂 Connect Flow Lambda）
# 只在 ``supports_transfer`` 能力标志打开时启用，任何拒绝/失败都回落
# D-5 record+hangup，主叫绝不因转接不成被静默掉线。
#
# 能力标志读取处（flag source）：providers/base.py:95 冻结契约 FC-1
# ``TelephonyAdapter.supports_transfer``（decision D-5/G-5）。本模块只消费
# 布尔值（per-call capability seam）：bot.py 接线时从 FC-1 适配器实例读出
# 传入（``bot._phone_telephony_adapter`` 缝）；未声明/未接线 → False →
# D-5 默认路径逐字不变。
#
# 目标解析链（spec §6.4）：approved allowlist → tenant authorization →
# destination policy，逐级默认拒绝（deny-by-default，与库内 redaction/
# authorization 惯例一致）。config_contract.py 冻结 30 个占位符且 ZERO new
# keys——没有 allowlist 键，故 allowlist 是注入式解析缝（callable/strategy），
# 保守默认 = 空 allowlist = 任何转接都拒绝；真实来源（config 或 store）的
# 接线是后续决策（见 U-206 report）。
#
# 状态词表（spec §6.4「Record requested, connecting, connected, failed, and
# returned-to-bot states where applicable」）：requested（每次转接请求）、
# connecting（批准、发出 <Dial>）、connected（Dial 接通）、failed（任一环
# 拒绝或 busy/no-answer/failed/canceled）、returned-to-bot——「where
# applicable」：U-206 盲转接形态的兜底是 record+hangup / Say+Hangup
#（R-P-005 口径），不产生 returned-to-bot；该态保留给后续「兜底把主叫送回
# bot」的形态（如 <Redirect> 回语音 webhook）。
# ---------------------------------------------------------------------------

DEFAULT_TRANSFER_TIMEOUT_S = 30

TRANSFER_STATES = ("requested", "connecting", "connected", "failed", "returned_to_bot")

# Twilio <Dial action> 回传 DialCallStatus 的归类（见 TransferFlow.dial_result）。
DIAL_CONNECTED_STATUSES = frozenset({"answered", "completed"})
DIAL_FAILED_STATUSES = frozenset({"busy", "no-answer", "failed", "canceled"})

# 解析缝形状（注入式，deny-by-default）：
#   allowlist   (topic) -> 已批准目标 E.164 | None    —— 默认空目录 → None
#   tenant auth (tenant_id, target) -> bool           —— 默认 False
#   destination policy (target) -> bool               —— 默认 False
TargetResolver = Callable[[str], "str | None"]
TenantAuthorizer = Callable[[str, str], bool]
DestinationPolicy = Callable[[str], bool]
# 转接状态记录缝（state, detail）-> None —— 默认 noop。
RecordStateFn = Callable[[str, dict[str, Any]], None]
# call-update 触发缝：把 <Dial> TwiML 挂到通话上；False/异常 = 触发失败
#（回落 D-5 record+hangup，绝不掉线）。
StartTransferFn = Callable[["TransferPlan"], Awaitable[bool]]


def _empty_allowlist(topic: str) -> str | None:
    """保守默认 allowlist：空目录 = 任何 topic 都解析不到目标 = 转接拒绝。"""
    return None


def _deny_tenant(tenant_id: str, target: str) -> bool:
    """默认拒绝：未接线的租户授权缝不通任何转接（deny-by-default）。"""
    return False


def _deny_destination(target: str) -> bool:
    """默认拒绝：未接线的目的地策略不通任何转接（deny-by-default）。"""
    return False


def _noop_record_state(state: str, detail: dict[str, Any]) -> None:
    return None


@dataclass(frozen=True)
class TransferPlan:
    """§6.4 转接决策。approved → ``twiml`` 是 <Dial> TwiML；denied → 空
    ``twiml`` + ``reason`` 指名拒绝环节（调用方回落 D-5 record+hangup）。"""

    approved: bool
    target: str | None
    state: str        # "connecting"（approved）| "failed"（denied）
    reason: str
    twiml: str


@dataclass(frozen=True)
class DialOutcome:
    """``<Dial action>`` 回传 ``DialCallStatus`` 后的处置。

    ``twiml`` 永远是 ``<Say>+<Hangup>``（connected 用收尾播报，其余用兜底
    播报）——任何转接结局下主叫都先听到播报再挂断，绝不静默掉线（R-P-005）。
    """

    state: str        # "connected" | "failed"
    detail: str       # 原始 DialCallStatus（未知/缺失 → "unknown"）
    twiml: str


class TransferFlow:
    """§6.4 受控转接流（D-5 门后；A-2 裁决的 in-repo 转接流）。

    构造缝全部注入、默认全拒绝：
      * ``supports_transfer`` —— FC-1 能力标志（见上方 flag source 注释）；
      * ``resolve_target`` / ``tenant_authorize`` / ``destination_policy`` ——
        解析链三缝（默认空 allowlist + 双 deny）；
      * ``record_state`` —— 状态记录缝（spec §6.4 状态词表）；
      * ``timeout_s`` / ``record`` / ``action_url`` —— <Dial> 参数（action_url
        指向 /twilio/transfer-result 时 Dial 结果回传给我们落账 + 兜底）。

    ``plan()`` 受能力标志门控；``dial_result()`` 不受门控——能收到 Dial
    结果就说明转接真实发生过，结果记录与不掉线兜底必须无条件执行。
    """

    def __init__(
        self,
        *,
        supports_transfer: bool = False,
        resolve_target: TargetResolver | None = None,
        tenant_authorize: TenantAuthorizer | None = None,
        destination_policy: DestinationPolicy | None = None,
        timeout_s: int = DEFAULT_TRANSFER_TIMEOUT_S,
        record: bool = False,
        action_url: str | None = None,
        record_state: RecordStateFn | None = None,
    ) -> None:
        self._enabled = bool(supports_transfer)
        self._resolve = resolve_target or _empty_allowlist
        self._tenant_auth = tenant_authorize or _deny_tenant
        self._dest_policy = destination_policy or _deny_destination
        self._timeout_s = timeout_s
        self._record_flag = record
        self._action_url = action_url
        self._record_state = record_state or _noop_record_state

    @property
    def enabled(self) -> bool:
        """FC-1 ``supports_transfer`` 能力标志。False = §6.4 路径不激活。"""
        return self._enabled

    def record(self, state: str, detail: dict[str, Any] | None = None) -> None:
        """经 ``record_state`` 缝记一条转接状态（spec §6.4 状态词表）。

        缝抛异常只记日志、绝不打断通话路径（与 mark_transfer / history_append
        同一容错惯例）。
        """
        try:
            self._record_state(state, dict(detail or {}))
        except Exception:
            log.exception(f"transfer record_state failed state={state}")

    def plan(self, *, tenant_id: str = "", topic: str = "") -> TransferPlan:
        """执行解析链并产出转接决策。

        链（spec §6.4）：approved allowlist → tenant authorization →
        destination policy——任一环拒绝即拒绝（deny-by-default），``reason``
        指名拒绝环节。无论批准与否都先记 "requested"，再记 "connecting"
        或 "failed"。解析缝给出的目标不是严格 E.164（构造器 ValueError）
        时按拒绝处理（fail closed）。
        """
        self.record("requested", {"tenant_id": tenant_id, "topic": topic})
        if not self._enabled:
            return self._deny(None, "supports_transfer disabled (D-5 default)")
        target = self._resolve(topic)
        if not target:
            return self._deny(
                None,
                "no approved target (allowlist empty or topic not in approved directory)",
            )
        if not self._tenant_auth(tenant_id, target):
            return self._deny(target, "tenant not authorized for target")
        if not self._dest_policy(target):
            return self._deny(target, "destination policy denied")
        try:
            twiml = build_twiml_dial_transfer(
                target,
                timeout_s=self._timeout_s,
                record=self._record_flag,
                action_url=self._action_url,
            )
        except ValueError as e:
            return self._deny(target, f"approved target rejected: {e}")
        self.record("connecting", {"target": target, "topic": topic})
        return TransferPlan(
            approved=True, target=target, state="connecting",
            reason="approved", twiml=twiml,
        )

    def _deny(self, target: str | None, reason: str) -> TransferPlan:
        self.record("failed", {"reason": reason, "target": target})
        return TransferPlan(
            approved=False, target=target, state="failed", reason=reason, twiml=""
        )

    def dial_result(self, *, dial_status: str) -> DialOutcome:
        """处置 ``<Dial action>`` 回传的 ``DialCallStatus``。

        answered/completed → "connected" + 收尾 ``<Say>+<Hangup>``；busy/
        no-answer/failed/canceled 及未知状态 → "failed" + 兜底
        ``<Say>+<Hangup>``（保守：未知按失败处理，主叫照样听到播报，绝不
        掉线）。每次调用都经 ``record_state`` 缝落状态；duplicate callback
        的去重在持久层（bot._persist_transfer_outcome 的确定性 event_id），
        本方法是纯映射、可重复调用。
        """
        status = (dial_status or "").strip().lower()
        if status in DIAL_CONNECTED_STATUSES:
            self.record("connected", {"dial_status": status})
            return DialOutcome(
                state="connected", detail=status,
                twiml=build_twiml_say_hangup(TRANSFER_GOODBYE_SAY),
            )
        detail = status or "unknown"
        self.record("failed", {"dial_status": detail})
        return DialOutcome(
            state="failed", detail=detail,
            twiml=build_twiml_say_hangup(TRANSFER_FALLBACK_SAY),
        )


def register_call_control_handlers(
    llm,
    task,
    *,
    call_id: str,
    emit: EmitFn,
    scope: str = "phone",
    history_append: HistoryAppendFn | None = None,
    mark_transfer: MarkTransferFn | None = None,
    write_outcome: WriteOutcomeFn | None = None,
    transfer_flow: TransferFlow | None = None,
    start_transfer: StartTransferFn | None = None,
    tenant_id: str = "",
) -> None:
    """Register async end_call / transfer_to_human handlers on ``llm``.

    ``scope`` selects the channel ("phone" or "web"). It is used purely
    for log breadcrumbs — the actual side-effects are driven by which
    callbacks the caller passed:

      * ``write_outcome``  — phone-only DDB outcome row writer; awaited
        inside the grace-period task BEFORE the EndTaskFrame is queued.
      * ``mark_transfer``  — phone-only DDB top-level flag setter.
      * ``history_append`` — phone-only chronological event recorder.

    For ``scope="web"`` the caller is expected to pass ``None`` for all
    three; every handler treats ``None`` as a noop so the same code path
    covers both channels.

    Pipecat's EndFrame propagation drives the WS close (which on phone
    becomes the SIP BYE), so completing the DDB write first guarantees
    the row exists by the time a Connect Flow Lambda fires after the BYE.

    Closure-scoped state ensures only one EndTaskFrame is queued per call
    even if the LLM invokes the tool multiple times.

    U-206 §6.4 human handoff (D-5 门控)——三个可选参数，缺省时本函数行为
    与 U-206 之前逐字一致：

      * ``transfer_flow`` —— §6.4 受控转接流（TransferFlow）。``None`` 或
        ``transfer_flow.enabled is False``（FC-1 supports_transfer 关）→
        完全走 D-5 record+hangup 原路径。能力标志打开 → 每次
        transfer_to_human 先跑解析链拿 TransferPlan：批准 → 由
        ``start_transfer`` 缝发 call-update（<Dial> TwiML），拒绝 → 回落
        D-5 record+hangup。任何路径都保持 write-before-teardown：
        write_outcome 先落，start_transfer 与 EndTaskFrame 都在其后。
      * ``start_transfer`` —— call-update 触发缝（async, plan -> bool）。
        ``None``/False/异常 = 触发失败 → 回落 D-5 record+hangup 并记
        "failed"（主叫绝不掉线）。
      * ``tenant_id`` —— 解析链租户授权环节的 per-call 租户身份缝（默认
        空串；真实租户身份的接线与 allowlist 真实来源同为后续决策）。
    """
    state = {"ending": False}

    async def _grace_then_end(
        reason: str, *, transfer_plan: TransferPlan | None = None
    ) -> None:
        try:
            await asyncio.sleep(GRACE_SECONDS)
            if write_outcome is not None:
                try:
                    await write_outcome()
                except Exception:
                    log.exception(f"write_outcome failed call_id={call_id}")
            # write_outcome is None on web scope → just skip; the WS close
            # driven by EndTaskFrame is the only thing the web leg needs.
            if transfer_plan is not None and transfer_plan.approved:
                # §6.4 call-update（A-2 in-repo 转接流）：outcome row 先落
                #（write-before-teardown，R-P-005），然后才发 call-update，
                # 最后 EndTaskFrame 拆流。`<Connect><Stream>` 会挡住后续
                # TwiML 直到流结束（spec §6.4），故顺序必须是：落账 →
                # call-update（通话 TwiML 换成 <Dial>）→ EndTaskFrame
                #（流关闭）。触发缝缺失/失败 → 回落 D-5 record+hangup。
                ok = False
                if start_transfer is not None:
                    try:
                        ok = bool(await start_transfer(transfer_plan))
                    except Exception:
                        log.exception(f"start_transfer failed call_id={call_id}")
                if not ok and transfer_flow is not None:
                    transfer_flow.record(
                        "failed",
                        {
                            "detail": "transfer trigger failed",
                            "target": transfer_plan.target,
                        },
                    )
            await task.queue_frames(
                [EndTaskFrame(reason=f"tool:end_call:{reason}")]
            )
        except Exception:
            log.exception(f"grace-then-end failed call_id={call_id}")

    async def _emit_event(payload: dict[str, Any]) -> None:
        try:
            await emit(payload)
        except Exception:
            log.exception(f"tool event emit failed call_id={call_id}")

    async def _handle_end_call(params):
        if state["ending"]:
            await params.result_callback({"status": "already_ending"})
            return
        raw_reason = (getattr(params, "arguments", None) or {}).get("reason") or "user_requested"
        reason = raw_reason if raw_reason in VALID_END_REASONS else "user_requested"
        state["ending"] = True
        log.info(f"end_call call_id={call_id} reason={reason}")
        await _emit_event(
            {"type": "tool_call", "name": "end_call", "args": {"reason": reason}}
        )
        if history_append is not None:
            try:
                history_append("tool_call", {"name": "end_call", "reason": reason})
            except Exception:
                log.exception(f"history_append failed call_id={call_id}")
        asyncio.create_task(_grace_then_end(reason))
        await params.result_callback({"status": "ending", "reason": reason})

    async def _handle_transfer(params):
        if state["ending"]:
            await params.result_callback({"status": "already_ending"})
            return
        topic = (getattr(params, "arguments", None) or {}).get("topic") or ""
        state["ending"] = True
        log.info(f"transfer_to_human call_id={call_id} topic={topic!r}")
        if mark_transfer is not None:
            try:
                mark_transfer(topic)
            except Exception:
                log.exception(f"mark_transfer failed call_id={call_id}")
        await _emit_event(
            {
                "type": "tool_call",
                "name": "transfer_to_human",
                "args": {"topic": topic},
            }
        )
        if history_append is not None:
            try:
                history_append(
                    "tool_call", {"name": "transfer_to_human", "topic": topic}
                )
            except Exception:
                log.exception(f"history_append failed call_id={call_id}")
        # §6.4 (U-206)：能力标志（FC-1 supports_transfer）打开时跑解析链
        # 拿转接决策；标志关 / 未接线 / 决策抛异常 → plan 为 None → 完全
        # 走 D-5 record+hangup 原路径（fail closed，默认不变）。
        plan = None
        if transfer_flow is not None and transfer_flow.enabled:
            try:
                plan = transfer_flow.plan(tenant_id=tenant_id, topic=topic)
            except Exception:
                log.exception(f"transfer plan failed call_id={call_id}")
                plan = None
        asyncio.create_task(
            _grace_then_end("transfer_requested", transfer_plan=plan)
        )
        await params.result_callback(
            {"status": "ending", "reason": "transfer_requested"}
        )

    log.info(
        f"registering call-control tools call_id={call_id} scope={scope}"
    )
    llm.register_function("end_call", _handle_end_call)
    llm.register_function("transfer_to_human", _handle_transfer)
