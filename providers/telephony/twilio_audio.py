"""FC-1 Twilio Media Streams —— μ-law↔PCM16 编解码层（U-203）。

Twilio Media Streams 的线上音频是 G.711 μ-law / 8kHz / 单声道 8-bit；本模块提供
它与内部 16-bit little-endian PCM 之间的双向转换，以及线性插值重采样、媒体帧
构造/解析辅助。全部为纯函数：不做 I/O、无全局可变状态、仅用标准库
（base64 / struct），因此可在任何上下文安全调用。

为什么不用 ``audioop``：``audioop`` 在 Python 3.13 已被移除（PEP 594），目标
运行时是 3.13，故编解码用纯 Python 查表实现（256 项解码表 + 65536 项编码表）。

μ-law 码字布局（G.711）：8-bit 码 = ``~(S|EEE|MMMM)``（ones' complement）。
本模块采用「±8 zero」约定（也称 bias 约定）：
  * 0xFF → +8（正零）、0x7F → −8（负零）
  * 0x80 → +32132（正满幅）、0x00 → −32132（负满幅）
即 magnitude = (((man << 3) + 0x84) << exp) − 0x7C。之所以选这一约定而非
「标准」的 −0x84（0xFF→0）：产品契约明确要求 0xFF 解出文档的 +8 bias 值，且
±8 zero 让零点附近对称、round-trip 误差上界一致。满幅 ±32132 与该公式自洽
（−0x84 公式下满幅是 32124，无法与 0xFF→+8 共存于同一公式）。

编码表由解码表单调最近邻反演生成（详见 ``_build_ulaw_encode_table``），
保证 round-trip 一致性：任一码字 u，``encode(decode(u)) == u``。
"""
from __future__ import annotations

import base64
import struct

# Twilio Media Streams 线上采样率固定 8kHz 单声道 μ-law。
MEDIA_STREAM_RATE = 8000


# ---------------------------------------------------------------------------
# G.711 μ-law 解码（±8 zero 约定）
# ---------------------------------------------------------------------------


def _ulaw_decode_one(u: int) -> int:
    """单码字 μ-law → PCM16（±8 zero 约定）。见模块 docstring 的公式推导。"""
    u = (~u) & 0xFF  # ones' complement：还原符号/指数/尾数位
    sign = u & 0x80
    exp = (u >> 4) & 0x07
    man = u & 0x0F
    magnitude = (((man << 3) + 0x84) << exp) - 0x7C
    return -magnitude if sign else magnitude


# 256 项解码表：码字 → PCM16。构建期一次性算好，运行期 O(1) 查表。
_ULAW_TO_PCM16: tuple[int, ...] = tuple(_ulaw_decode_one(u) for u in range(256))


# ---------------------------------------------------------------------------
# G.711 μ-law 编码表（由解码表单调最近邻反演）
# ---------------------------------------------------------------------------


def _build_ulaw_encode_table() -> bytes:
    """由解码表反演生成 65536 项编码表：PCM16 (int16) → μ-law 码字。

    解码表按码字顺序不是单调的（符号位在最高位），先按解码值排序成
    ``(value, code)`` 序列；由于解码值互不相同，排序后严格单调。对每个
    PCM 样本 x，取解码值最接近 x 的码字（最近邻；等距取较小值），线性扫一遍
    即可（指针只前进），O(65536 + 255)。这样保证 ``encode(decode(u)) == u``
    （码字自身解出的值必然离自己最近），round-trip 误差即量化误差。
    """
    pairs = sorted((value, code) for code, value in enumerate(_ULAW_TO_PCM16))
    table = bytearray(65536)
    pos = 0
    for x in range(-32768, 32768):
        # 指针推进到使 vals[pos] 是 x 的最近邻（等距取较小值 → 不越过中点）
        while pos + 1 < len(pairs) and (x - pairs[pos][0]) > (pairs[pos + 1][0] - x):
            pos += 1
        table[x + 32768] = pairs[pos][1]
    return bytes(table)


# 65536 项编码表：以 (sample + 32768) 为下标取 μ-law 码字。
_PCM16_TO_ULAW: bytes = _build_ulaw_encode_table()


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def ulaw_to_pcm16(data: bytes) -> bytes:
    """μ-law 原始字节 → little-endian PCM16 字节（每字节 → 2 字节）。

    输入为空返回 b""。输出长度恒为 ``len(data) * 2``。
    """
    if not data:
        return b""
    values = [_ULAW_TO_PCM16[b] for b in data]
    return struct.pack(f"<{len(values)}h", *values)


def pcm16_to_ulaw(data: bytes) -> bytes:
    """little-endian PCM16 字节 → μ-law 原始字节（每 2 字节 → 1 字节）。

    输入为空返回 b""。长度为奇数抛 ValueError（PCM16 样本必须 2 字节对齐，
    奇数说明上游截断/拼接出错，静默丢弃尾字节会掩盖数据损坏）。
    """
    if not data:
        return b""
    if len(data) % 2 != 0:
        raise ValueError(f"pcm16 data length must be even, got {len(data)}")
    n = len(data) // 2
    samples = struct.unpack(f"<{n}h", data)
    return bytes(_PCM16_TO_ULAW[s + 32768] for s in samples)


def resample_pcm16(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """little-endian PCM16 线性插值重采样（如 24kHz → 8kHz）。

    边界约定：
      * ``src_rate``/``dst_rate`` ≤ 0 → ValueError（无意义的采样率）。
      * 长度奇数 → ValueError（同 ``pcm16_to_ulaw``）。
      * 空输入 → b""。
      * ``src_rate == dst_rate`` → 原样返回（恒等，不做无谓重排）。

    输出样本数 = ``round(n_in * dst_rate / src_rate)``。第 i 个输出样本取
    输入位置 ``i * src_rate / dst_rate`` 处的线性插值；末尾越界则钳到最后一
    个输入样本（不外推，避免边界振铃）。
    """
    if src_rate <= 0 or dst_rate <= 0:
        raise ValueError(f"sample rates must be positive, got {src_rate}->{dst_rate}")
    if len(data) % 2 != 0:
        raise ValueError(f"pcm16 data length must be even, got {len(data)}")
    if not data:
        return b""
    if src_rate == dst_rate:
        return data
    n_in = len(data) // 2
    samples = struct.unpack(f"<{n_in}h", data)
    n_out = round(n_in * dst_rate / src_rate)
    out: list[int] = []
    for i in range(n_out):
        pos = i * src_rate / dst_rate
        idx = int(pos)
        if idx >= n_in - 1:
            out.append(samples[n_in - 1])  # 末尾钳位，不外推
            continue
        frac = pos - idx
        s = samples[idx] + (samples[idx + 1] - samples[idx]) * frac
        out.append(int(round(s)))
    return struct.pack(f"<{len(out)}h", *out)


def build_media_event(stream_sid: str, pcm16: bytes, *, src_rate: int = MEDIA_STREAM_RATE) -> dict:
    """构造服务端→Twilio 的 ``media`` 事件（PCM16 → μ-law → base64）。

    ``src_rate`` 是输入 PCM16 的采样率；非 8000 时先线性插值到 8000（Twilio
    线上固定 8kHz μ-law）。返回 ``{"event":"media","streamSid":...,
    "media":{"payload":"<base64 μ-law>"}}``，可被 ``twilio_stream.parse_media_frame``
    解回（round-trip 契约）。
    """
    if src_rate != MEDIA_STREAM_RATE:
        pcm16 = resample_pcm16(pcm16, src_rate, MEDIA_STREAM_RATE)
    ulaw = pcm16_to_ulaw(pcm16)
    payload_b64 = base64.b64encode(ulaw).decode("ascii")
    return {
        "event": "media",
        "streamSid": stream_sid,
        "media": {"payload": payload_b64},
    }


def decode_inbound_frame(payload_b64: str) -> bytes | None:
    """Twilio 入向 media 帧的 base64 payload → PCM16 字节。

    非 str / 空串 / 非法 base64 → None（不抛异常；WS 入向是不可信边界，
    坏帧不应打断会话）。合法则返回 little-endian PCM16。
    """
    if not isinstance(payload_b64, str) or not payload_b64:
        return None
    try:
        ulaw = base64.b64decode(payload_b64, validate=True)
    except Exception:
        return None
    return ulaw_to_pcm16(ulaw)
