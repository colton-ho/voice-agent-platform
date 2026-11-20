"""FC-1 Twilio Media Streams —— 流预订/关联 + 帧处理（U-202，§6.1.5–6.1.7）。

本单元覆盖 spec §6.1.5–6.1.7 三件事：
  * §6.1.5 流预订 + 关联：webhook 时预订（reserve），WS ``start`` 时校验
    AccountSid/CallSid/StreamSid 一致性并绑定（accept），未知/过期/不匹配/
    streamSid 被占用一律拒绝。
  * §6.1.6 敏感数据不进 URL/stream 参数（本模块不碰 URL，由 U-201 签名层负责）。
  * §6.1.7 帧处理：``media`` 帧解 base64 → μ-law 原始字节；``mark``/``clear``
    事件构造（播放位置跟踪 / barge-in 清空）。

关于「FC-7 contract」的边界（避免误解）：storage/base.py 的 FC-7 ``SessionDirectory``
是**呼叫容量 + owner 租约 + 命令路由**（reserve_capacity/bind_owner/renew_lease/
route_command/release），属 U-305 分布式归属层。本模块的 ``StreamSessionDirectory``
是**流会话预订/关联**（reserve/accept/close/get），是 §6.1.5 的更薄一层，二者是
不同契约。此实现是 in-memory 单副本（dict），U-305 用分布式实现替换，接口保持
reserve/accept/close/get 不变即可。

帧格式对齐 Twilio Media Streams 文档（与 pipecat ``TwilioFrameSerializer`` 一致）：
  * media（Twilio→服务端）：``{"event":"media","media":{"payload":"<base64 μ-law>"}}``
  * mark（服务端→Twilio）：``{"event":"mark","name":"<mark>"}``
  * clear（服务端→Twilio）：``{"event":"clear"}``
  mark/clear 的 ``streamSid`` 由接线层在已知时附加（pipecat 亦如此），故此处返回
  文档最简形态。
"""
from __future__ import annotations

import base64
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum


class StreamState(str, Enum):
    RESERVED = "reserved"
    ACTIVE = "active"
    CLOSED = "closed"


# 预订 TTL：webhook→WS start 之间留足握手时间（§6.1.5 过期即拒）。默认 5 分钟。
DEFAULT_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class StreamReservation:
    """一次流会话预订（value object，不可变；生命周期由 Directory 以 replace 推进）。

    ``state`` 语义（``StreamState``）：reserved → active（accept 绑定 stream_sid）
    → closed。``stream_sid`` 仅在 accept 后非空。
    """

    call_sid: str
    account_sid: str
    stream_sid: str | None
    created_at: float
    expires_at: float
    state: str


class StreamSessionDirectory:
    """最小单副本流会话目录（in-memory dict）。

    接口即接缝：U-305 换成分布式实现时，保持 reserve/accept/close/get 四个方法
    签名不变即可替换（Python 结构化类型）。为让「过期」可确定性测试，``clock``
    可注入（默认 ``time.time``）；``ttl_seconds`` 控制预订存活期。
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._by_call: dict[str, StreamReservation] = {}
        self._stream_owner: dict[str, str] = {}  # stream_sid -> call_sid，用于「streamSid 被占用」检测

    def reserve(self, call_sid: str, account_sid: str) -> StreamReservation:
        """webhook 时创建预订（state=reserved）。同 call_sid 重复预订以最新为准。"""
        now = self._clock()
        res = StreamReservation(
            call_sid=call_sid,
            account_sid=account_sid,
            stream_sid=None,
            created_at=now,
            expires_at=now + self._ttl,
            state=StreamState.RESERVED,
        )
        self._by_call[call_sid] = res
        return res

    def accept(
        self, stream_sid: str, call_sid: str, account_sid: str
    ) -> StreamReservation | None:
        """WS ``start`` 时校验一致性并绑定 stream_sid（reserved→active）。

        返回 None 的拒绝面（§6.1.5）：未知 call_sid、已过期、account_sid 不匹配、
        stream_sid 已被占用（含重复 accept）。绑定是把 stream_sid 记到该预订并登记
        stream_owner，用于后续关联与二次 accept 拒绝。
        """
        res = self._by_call.get(call_sid)
        if res is None:
            return None  # 未知 call_sid
        if res.state is StreamState.CLOSED:
            return None  # 已关闭的会话不再接受绑定
        if res.stream_sid is not None:
            return None  # 重复 accept：预订已绑定过 stream
        if self._clock() > res.expires_at:
            return None  # 过期
        if res.account_sid != account_sid:
            return None  # account_sid 不一致
        if stream_sid in self._stream_owner:
            return None  # streamSid 已被占用
        accepted = replace(res, stream_sid=stream_sid, state=StreamState.ACTIVE)
        self._by_call[call_sid] = accepted
        self._stream_owner[stream_sid] = call_sid
        return accepted

    def close(self, call_sid: str) -> None:
        """标记会话关闭（state=closed）。记录保留，get 仍可读到关闭态。"""
        res = self._by_call.get(call_sid)
        if res is None:
            return
        self._by_call[call_sid] = replace(res, state=StreamState.CLOSED)

    def get(self, call_sid: str) -> StreamReservation | None:
        return self._by_call.get(call_sid)


# ---------------------------------------------------------------------------
# 帧处理（§6.1.7）
# ---------------------------------------------------------------------------


def parse_media_frame(payload: dict) -> bytes | None:
    """从 Twilio ``media`` 事件抽取 base64 解码后的 μ-law 原始字节。

    ``payload`` 可传整条 media 事件消息或其 ``media`` 子对象。返回 μ-law 原始字节
    （μ-law→PCM 归 U-203 音频契约）。缺 ``payload`` 字段或非法 base64 → None，
    不抛异常。用 ``validate=True`` 才能把「非法 base64」可靠判为 None（否则会
    静默丢弃非法字符、解出垃圾字节）。
    """
    if not isinstance(payload, dict):
        return None
    media = payload.get("media")
    source = media if isinstance(media, dict) else payload
    b64 = source.get("payload")
    if not isinstance(b64, str) or not b64:
        return None
    try:
        return base64.b64decode(b64, validate=True)
    except Exception:
        return None


def build_mark_event(mark_name: str) -> dict:
    """构造服务端→Twilio 的 ``mark`` 事件（跟踪播放位置）。

    文档最简形态 ``{"event":"mark","name":...}``；接线层在已知 streamSid 时附加。
    """
    return {"event": "mark", "name": mark_name}


def build_clear_event() -> dict:
    """构造服务端→Twilio 的 ``clear`` 事件（barge-in 清空播放缓冲）。"""
    return {"event": "clear"}
