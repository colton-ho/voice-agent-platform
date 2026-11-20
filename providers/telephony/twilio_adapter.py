"""FC-1 Twilio adapter —— webhook + WS-upgrade 签名层（U-201）。

本单元只负责「签名校验 + TwiML 握手」两件事：把 ``X-Twilio-Signature`` 的
HMAC-SHA1 校验和 ``<Connect><Stream>`` TwiML 生成封装成可插拔构件，供后续
U-202..U-207 把 bot.py 的 `/twilio/*` 端点切到 FC-1 适配器。端点在切换前
保持原样，故此模块不碰 bot.py、也不碰 `twilio_sig.py`（保留为独立测试 oracle）。

A-1 裁决（2026-11-06，FINAL）：Azure 路径用官方 twilio SDK 的
``RequestValidator`` 做签名校验，stdlib ``twilio_sig`` 退为独立测试 oracle。
但 `twilio` SDK 尚未进 requirements（Phase C 才引入），故本模块的签名后端
做成「SDK 可导入则优先用之、否则回退 stdlib」：今天实际走 stdlib，装上 SDK
后无需改调用方即自动切到 SDK（升级路径见 report）。两条后端实现同一套
Twilio 算法，语义等价。

外部 URL 签名要点（canonical pitfall）：Twilio 签的是它自己看到的完整 URL
（``TWILIO_PUBLIC_BASE_URL`` + path + query），不是 CloudFront 改写后的内部
Host。故校验所用的 ``url`` 必须由调用方以 `twilio_sig.build_signed_url`
(public_base, path, query) 拼出，本模块只校验、不猜 URL。

U-206 追加（§6.4 human handoff，只增不改）：``build_twiml_dial_transfer`` /
``build_twiml_say_hangup`` 两个转接 TwiML 构件，与 ``build_twiml_connect_stream``
同居本模块；签名层与既有构件零改动。
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from xml.sax import saxutils

import twilio_sig  # 独立测试 oracle（stdlib HMAC-SHA1，A-1）

# Twilio Media Stream 的默认 WS 路径（bot.py `/twilio/media-stream` 同名）。
DEFAULT_MEDIA_STREAM_PATH = "/twilio/media-stream"

# 流起不来时的兜底播报（C-6 fallback 起步版）：<Say> 挂在 </Connect> 之后，
# 流没建立/立即失败时 Twilio 落到这里播报，避免死寂。真正的「仅失败才播」
# 条件化/转接路由留给 U-205 细化，此处先落一个基本兜底文案。
_FALLBACK_SAY = "Sorry, we are unable to connect your call right now. Please try again later."

# 三态哨兵：区分「自动探测 SDK」(默认) / 「强制 stdlib」(显式 None) / 「注入后端」。
_AUTO = object()


def _load_sdk_validator(auth_token: str):
    """返回 twilio SDK 的 RequestValidator；SDK 不可导入则返回 None（A-1）。

    惰性导入 + 宽捕获：`twilio` 非声明依赖时不能让模块 import 失败，回退
    stdlib 即可，故此处吞掉一切导入期异常。
    """
    try:
        from twilio.request_validator import RequestValidator  # noqa: PLC0415
    except Exception:
        return None
    return RequestValidator(auth_token)


@dataclass(frozen=True)
class TwilioConfig:
    """Twilio 连接参数（签名 + TwiML 生成的最小配置面）。

    ``public_base_url`` 是 Twilio 可见的外部 https 源（如 CloudFront 域名或
    自定义域名），用于拼签名 URL 与 TwiML 的 ``wss://`` host。``account_sid``
    可选，仅 serializer 的 auto-hangup 需要。
    """

    auth_token: str
    public_base_url: str
    account_sid: str | None = None


class TwilioSignatureValidator:
    """FC-8 ``SignatureValidator``：校验 ``X-Twilio-Signature``（decision A-1）。

    可插拔后端：SDK 可导入则用官方 ``RequestValidator``，否则回退 stdlib
    ``twilio_sig``。两种后端实现同一 Twilio 算法，行为等价——差异只在依赖面。
    构造后端可注入（``sdk_validator``）以驱动确定性测试：
      * 不传（默认）→ 自动探测；
      * 显式传 None → 强制 stdlib oracle；
      * 传入对象 → 直接用作后端（测试注入 fake）。
    """

    def __init__(self, auth_token: str, *, sdk_validator=_AUTO):
        self._auth_token = auth_token
        self._sdk = _load_sdk_validator(auth_token) if sdk_validator is _AUTO else sdk_validator

    def is_valid(self, *, url: str, params: Mapping[str, str], signature: str) -> bool:
        """常量时间比对签名。缺失签名/令牌直接 False，不抛异常。

        ``url`` 必须是 Twilio 签下的外部完整 URL（见模块 docstring）。
        """
        if not signature or not self._auth_token:
            return False
        if self._sdk is not None:
            return self._sdk_is_valid(url, params, signature)
        return twilio_sig.verify_twilio_signature(
            self._auth_token, url, dict(params), signature
        )

    def _sdk_is_valid(self, url: str, params: Mapping[str, str], signature: str) -> bool:
        try:
            return bool(self._sdk.validate(url, dict(params), signature))
        except Exception:
            return False


def validate_incoming_call(
    config: TwilioConfig, signature: str, url: str, params: Mapping[str, str]
) -> bool:
    """校验 `/twilio/incoming-call` webhook 的 ``X-Twilio-Signature``。

    ``url`` 由调用方以 ``twilio_sig.build_signed_url(config.public_base_url,
    path, query)`` 拼出的外部完整 URL。返回 bool，False 由调用方映射为 403。
    """
    return TwilioSignatureValidator(config.auth_token).is_valid(
        url=url, params=params, signature=signature
    )


def _public_host(public_base_url: str) -> str:
    """剥掉 scheme 与尾斜杠，得到 TwiML ``wss://`` 用的 host[:port][/path]。"""
    host = public_base_url.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    return host.rstrip("/")


def _stream_ws_url(config: TwilioConfig, stream_url: str) -> str:
    """归一化出 Twilio 应拨的 Media Stream ``wss://`` URL。

    传完整 ws(s):// URL 原样用；传路径（或空）则用 config 的 Twilio 可见 host
    拼，默认路径 ``/twilio/media-stream``。这样「给定 WS URL」与「由
    public_base_url host 推导」两种调用方式都成立，config 始终参与 host 生成。
    """
    if stream_url and stream_url.startswith(("ws://", "wss://")):
        return stream_url
    path = stream_url or DEFAULT_MEDIA_STREAM_PATH
    if not path.startswith("/"):
        path = "/" + path
    return f"wss://{_public_host(config.public_base_url)}{path}"


def build_twiml_connect_stream(
    config: TwilioConfig, stream_url: str, *, caller: str = ""
) -> str:
    """生成 ``<Connect><Stream>`` TwiML，把 Twilio 的 Media Stream 接到 ``stream_url``。

    ``stream_url`` 语义见 ``_stream_ws_url``（完整 URL 或路径皆可）。``caller``
    非空时附带 ``<Parameter name="caller">``（与 bot.py 现网 TwiML 同形，便于
    无缝切换）。末尾 ``<Say>`` 是流起不来时的基本兜底播报（见 ``_FALLBACK_SAY``）。
    属性用 ``saxutils.quoteattr``、文本用 ``escape``，保证产出合法 XML。
    """
    url_attr = saxutils.quoteattr(_stream_ws_url(config, stream_url))
    caller_param = (
        f'<Parameter name="caller" value={saxutils.quoteattr(caller)}/>' if caller else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f"<Stream url={url_attr}>{caller_param}</Stream>"
        "</Connect>"
        f"<Say>{saxutils.escape(_FALLBACK_SAY)}</Say>"
        "</Response>"
    )


# --- U-206 · §6.4 human handoff builders（增量，只增不改） -----------------

# 转接兜底播报（R-P-005 / spec §6.4）：坐席忙、no-answer、失败、超时或未知
# 结局时播给主叫——绝不因转接不成而把主叫静默掉线。
TRANSFER_FALLBACK_SAY = (
    "Sorry, no agents are available to take your call right now."
)

# 盲转接通、坐席挂机后的收尾播报。
TRANSFER_GOODBYE_SAY = "Thank you. Goodbye."

# 严格 E.164：`+` 后 7~15 位数字、首位非 0。目标来自 allowlist 解析缝，
# 这里是构造边界的形状校验（XML 注入由 escape/quoteattr 防御，语义防御
# 由此正则兜底——非号码绝不进 <Dial>）。
_E164_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")

DEFAULT_TRANSFER_TIMEOUT_S = 30


def build_twiml_say_hangup(message: str) -> str:
    """生成 ``<Say>`` + ``<Hangup>`` 的收尾 TwiML（U-206 增量构件）。

    转接结果端点（``/twilio/transfer-result``）按 ``DialCallStatus`` 回播的
    兜底/收尾文案用它拼——任何转接结局下主叫都先听到播报再挂断（不掉线）。
    文本走 ``saxutils.escape``，保证产出合法 XML。
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{saxutils.escape(message)}</Say>"
        "<Hangup/>"
        "</Response>"
    )


def build_twiml_dial_transfer(
    target_e164: str,
    *,
    timeout_s: int = DEFAULT_TRANSFER_TIMEOUT_S,
    record: bool = False,
    action_url: str | None = None,
    fallback_say: str = TRANSFER_FALLBACK_SAY,
) -> str:
    """生成 §6.4 盲转接的 ``<Dial>`` TwiML（U-206 增量构件；A-2 裁决的
    in-repo 转接流本体，取代外挂 Connect Flow Lambda）。

    ``target_e164`` 必须是严格 E.164（``+`` + 7~15 位、首位非 0），否则
    ``ValueError``——非号码绝不进 ``<Dial>``。``timeout_s`` 是被叫振铃秒数
    （须 > 0）；``record=True`` 映射 Twilio ``record="record-from-answer"``
    （是否录音由调用方按 RECORDING_ENABLED 决定）。

    Dial 之后的处置两种形态（调用方二选一，不叠加）：

      * ``action_url=None``（默认）—— ``</Dial>`` 后紧跟
        ``<Say>fallback_say</Say><Hangup/>``。Twilio 语义：Dial 未接通
        （busy/no-answer/failed/canceled）或坐席挂机后继续执行同文档后续
        verb，主叫听到播报才挂断。**这是无端点形态下的不掉线保证**。
        注意该形态无法区分「坐席忙」与「坐席聊完挂机」，文案取其一；
        需要按状态区分播报/记结果请用 action_url 形态。
      * ``action_url`` 给定—— ``<Dial action=... method="POST">``。Dial 以
        任何原因结束后 Twilio 把 ``DialCallStatus`` POST 回该端点，由端点
        记录转接结果并按状态回播 ``build_twiml_say_hangup``（同一不掉线
        保证 + 结果落账 + 文案可分状态）。此形态下同文档不放尾随 verb：
        Dial 之后的一切由 action 端点接管。

    属性用 ``saxutils.quoteattr``、文本用 ``escape``，产出合法 XML。
    """
    target = (target_e164 or "").strip()
    if not _E164_RE.match(target):
        raise ValueError(
            f"target_e164 must be E.164 ('+' then 7-15 digits, first non-zero), got {target_e164!r}"
        )
    try:
        timeout = int(timeout_s)
    except (TypeError, ValueError):
        raise ValueError(f"timeout_s must be an integer > 0, got {timeout_s!r}") from None
    if timeout <= 0:
        raise ValueError(f"timeout_s must be an integer > 0, got {timeout_s!r}")

    dial_attrs = f' timeout="{timeout}"'
    if record:
        dial_attrs += ' record="record-from-answer"'
    if action_url:
        dial_attrs += f" action={saxutils.quoteattr(action_url)} method=\"POST\""
    dial = (
        f"<Dial{dial_attrs}>"
        f"<Number>{saxutils.escape(target)}</Number>"
        "</Dial>"
    )
    # action 形态由端点接管 Dial 之后的 TwiML；无 action 形态才自带兜底。
    tail = (
        ""
        if action_url
        else f"<Say>{saxutils.escape(fallback_say)}</Say><Hangup/>"
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response>{dial}{tail}</Response>"
    )
