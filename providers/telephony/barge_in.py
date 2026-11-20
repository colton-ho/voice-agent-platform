"""FC-1 Twilio Media Streams —— barge-in 控制器 + mark/clear 台账（U-204，§6.3）。

覆盖 R-A-004 的验收面：
  * 确认用户说话（confirmed speech）→ 生成代际（epoch）递增；下游一切产物带代际戳。
  * 打断时取消 LLM + TTS（可注入回调）、清空本地出向队列（flush）、向 Twilio 发
    恰好一条 ``clear``；``clear`` 之后不再发出旧代际的任何音频。
  * 迟到回调（旧代际的 LLM 产出 / TTS 分片 / mark ack）一律丢弃（guard）。
  * mark 台账区分 cleared（clear 先到、从未播出）vs played（mark ack 确认播出）。
  * generated / cleared / played 时长核算（generated = 产出；played = mark 确认）。
  * 中断的助手内容（interrupted assistant content）按裁定写入历史。

关键策略（均为「定义并文档化」的裁量点，测试钉死行为）：

1. **历史策略（裁决 A-1 / V9 摘要真实性）**：被打断的助手内容**照常记入历史**
   （按「已生成」记录），但带 ``interrupted: true`` 标志；其未播出部分只进
   cleared，**绝不进 played_duration**（played 只认 mark 确认的播出）。
   理由：摘要要如实反映「助手说过/说到一半」——丢弃中断内容会让事后摘要与
   用户实际听到的对话脱节（V9：摘要必须与用户感知一致）；而 played 若计入
   未播出内容，会让「已播出时长」虚高、清算/计费/验收口径失真。二者分开记账，
   标志位让上层摘要自行措辞（「助手被打断」）。
2. **单 clear 语义 + 快速双打断去抖**：每次「打断事件」（epoch 转移）恰好发一条
   ``build_clear_event()``。``on_user_speech_confirmed()`` 在去抖窗口
   （``debounce_seconds``，默认 0.25s，时钟可注入）内的重复确认视为同一次打断
   吸收掉：不递增 epoch、不发第二条 clear、不再触发取消。理由：STT 端点检测/
   VAD 可能对同一次用户开口重复给出 confirmed 信号，双 clear 会让 Twilio 侧
   清两次缓冲并使台账出现同一代际转移的重复清算。窗口过后的新确认是新打断
   （新 epoch + 新 clear）。
3. **队列丢弃策略 = 丢最新（drop-newest）**：出向队列（deque）有帧数
   （``max_queue_frames``）与字节（``max_queue_bytes``）双重上限，超限时**拒绝
   新到帧**（新帧计入 generated + cleared）。理由：队列保序可让整句语音保持
   连续（截尾像弱网断句，可懂）；丢最旧会在句子中间挖洞（开头丢失，不可懂）。
   溢出本身意味着线上通道已积压/停滞，此时新音频最可能即将被 barge-in 清掉。
   核算恒等式 generated = played + cleared + pending 在两种边界下都精确成立。
4. **flush() 语义**：清空本地队列并把所有未结算（queued/sent）的 mark 段落判为
   cleared——即「这些音频永远不会被确认播出了」。因此 flush 必须与线上的
   ``clear``（或通话拆除）配对使用；``on_user_speech_confirmed`` 内部按
   clear → flush 顺序执行。
5. **mark 状态机**：queued（在本地队列里、尚未写到线上）→ sent（已写线上，等待
   Twilio 回 ack）→ played（ack 到达，段内音频确认播出）| cleared（clear/flush
   先到，从未播出）。ack 迟到（旧代际）被 guard 丢弃，mark 保持 cleared——
   满足「marks arriving after clear are classified as cleared」（R-A-004）。
6. **输出节流（pacing）**：20ms 媒体帧（8kHz × 20ms = 160 样本）按注入时钟滴送，
   无真实 sleep；mark 事件紧跟其覆盖音频之后写线上（Twilio 按流内顺序回 ack）。

纯内存、单副本；只依赖 stdlib + 本包 twilio_audio（重采样/编码）与 twilio_stream
（mark/clear 事件构造），均为 import-only。
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum

from providers.telephony.twilio_audio import (
    MEDIA_STREAM_RATE,
    build_media_event,
    resample_pcm16,
)
from providers.telephony.twilio_stream import build_clear_event, build_mark_event

# Twilio 媒体流约定：20ms 一帧 @ 8kHz 单声道 = 160 样本 = 320 字节 PCM16。
FRAME_SAMPLES = 160
FRAME_BYTES = FRAME_SAMPLES * 2
FRAME_INTERVAL = FRAME_SAMPLES / MEDIA_STREAM_RATE  # 0.02s

# 队列默认上限（每通话内存护栏）：500 帧 = 10s 音频 ≈ 160KB PCM16。
DEFAULT_MAX_QUEUE_FRAMES = 500
DEFAULT_MAX_QUEUE_BYTES = 256 * 1024

# 快速双打断去抖窗口（见模块 docstring 策略 2）。
DEFAULT_DEBOUNCE_SECONDS = 0.25


class MarkState(str, Enum):
    """mark 生命周期：queued → sent → played | cleared。"""

    QUEUED = "queued"  # 已入本地队列，尚未写到线上
    SENT = "sent"  # 已写线上，等待 Twilio ack
    PLAYED = "played"  # ack 确认：段内音频已播出
    CLEARED = "cleared"  # clear/flush 先到：从未播出


@dataclass
class _Segment:
    """一段「两个 mark 之间」的音频账目（样本数，int 精确计数）。

    每帧入队时记入 pending；mark ack → pending 并入 played；clear/flush →
    pending 并入 cleared。generated = pending + played + cleared（派生量）。
    段与 epoch 绑定，用于被打断内容的历史核算（按代际拆分）。
    """

    epoch: int
    pending: int = 0
    played: int = 0
    cleared: int = 0


@dataclass
class _MarkRecord:
    name: str
    epoch: int
    state: MarkState
    segment: _Segment  # 该 mark 覆盖的段（mark 之前、上一个 mark 之后的音频）


@dataclass
class _Frame:
    segment: _Segment
    pcm: bytes
    samples: int


@dataclass
class _MarkItem:
    record: _MarkRecord


class BargeInController:
    """每通话会话的 barge-in 控制器（epoch/取消/队列/台账/节流一体）。

    接缝全部可注入（send_event / cancel_llm / cancel_tts / history_sink / clock），
    纯内存确定性实现：无网络、无真实 sleep，测试用假时钟驱动。
    """

    def __init__(
        self,
        *,
        stream_sid: str,
        send_event: Callable[[dict], Awaitable[None]],
        cancel_llm: Callable[[], Awaitable[None]] | None = None,
        cancel_tts: Callable[[], Awaitable[None]] | None = None,
        history_sink: Callable[[dict], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
        max_queue_frames: int = DEFAULT_MAX_QUEUE_FRAMES,
        max_queue_bytes: int = DEFAULT_MAX_QUEUE_BYTES,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        self._stream_sid = stream_sid
        self._send_event = send_event
        self._cancel_llm = cancel_llm
        self._cancel_tts = cancel_tts
        self._history_sink = history_sink
        self._clock = clock
        self._max_frames = max_queue_frames
        self._max_bytes = max_queue_bytes
        self._debounce = debounce_seconds

        self._epoch = 0
        self._last_barge_in: float | None = None
        self._next_frame_at = clock()

        self._queue: deque[_Frame | _MarkItem] = deque()
        self._frames_queued = 0
        self._bytes_queued = 0
        self._buffer = b""  # 不足一帧的 PCM16 残样
        self._segments: list[_Segment] = [_Segment(epoch=0)]  # 末尾为当前开启段
        self._marks: dict[tuple[int, str], _MarkRecord] = {}
        self._content_by_epoch: dict[int, list[str]] = {}

    # ------------------------------------------------------------------
    # 代际（epoch）
    # ------------------------------------------------------------------

    @property
    def epoch(self) -> int:
        return self._epoch

    def guard(self, epoch: int) -> bool:
        """迟到回调丢弃接缝：仅「当前代际」放行。

        TTS 分片 / LLM 产出 / mark ack 在发起时盖上当时的 epoch 戳，交付时先过
        guard；旧代际（< 当前）与未来代际（> 当前，异常来源）一律 False。
        """
        return epoch == self._epoch

    # ------------------------------------------------------------------
    # 内容入史（历史策略的输入面）
    # ------------------------------------------------------------------

    def note_assistant_content(self, content: str, *, epoch: int | None = None) -> bool:
        """登记助手内容（LLM 产出），供打断时按历史策略写入 history_sink。

        ``epoch`` 缺省为当前代际；显式传旧代际（迟到 LLM 产出）→ 丢弃返回 False。
        正常（未打断）路径的历史写入由接线层负责，本方法只累积、只在打断时出账。
        """
        if epoch is not None and not self.guard(epoch):
            return False
        self._content_by_epoch.setdefault(self._epoch, []).append(content)
        return True

    # ------------------------------------------------------------------
    # 音频入队（TTS 产出）
    # ------------------------------------------------------------------

    def queue_audio(self, data: bytes, *, src_rate: int = MEDIA_STREAM_RATE, epoch: int | None = None) -> bool:
        """PCM16 LE 音频入出向队列（非 8kHz 先重采样到 8kHz，再切成 20ms 帧）。

        返回 True 表示全部样本已入队；False 表示有样本被丢弃——旧代际丢弃
        （``epoch`` 传旧值）或队列超限（drop-newest，见策略 3）。被拒样本照常
        计入 generated + cleared（它们由 TTS 产出、但永远不会播出，恒等式
        generated = played + cleared + pending 才能精确成立）。奇数长度输入
        沿用 twilio_audio 的 ValueError 契约（数据损坏不应静默吞掉）。
        """
        if epoch is not None and not self.guard(epoch):
            return False
        if len(data) % 2 != 0:
            raise ValueError(f"pcm16 data length must be even, got {len(data)}")
        pcm = data if src_rate == MEDIA_STREAM_RATE else resample_pcm16(data, src_rate, MEDIA_STREAM_RATE)
        self._buffer += pcm
        accepted_all = True
        seg = self._segments[-1]
        while len(self._buffer) >= FRAME_BYTES:
            frame, self._buffer = self._buffer[:FRAME_BYTES], self._buffer[FRAME_BYTES:]
            accepted_all = self._enqueue_frame(seg, frame, FRAME_SAMPLES) and accepted_all
        return accepted_all

    def _enqueue_frame(self, seg: _Segment, pcm: bytes, samples: int) -> bool:
        """入队一帧；队列超限按 drop-newest 拒绝（计入 generated + cleared）。"""
        if self._frames_queued >= self._max_frames or self._bytes_queued + len(pcm) > self._max_bytes:
            seg.cleared += samples  # 产出过但永不播出
            return False
        seg.pending += samples
        self._queue.append(_Frame(segment=seg, pcm=pcm, samples=samples))
        self._frames_queued += 1
        self._bytes_queued += len(pcm)
        return True

    # ------------------------------------------------------------------
    # mark 台账
    # ------------------------------------------------------------------

    def mark(self, name: str) -> None:
        """在当前位置打 mark（覆盖「上个 mark 之后到此刻」的段）。

        mark 事件与音频同队列排队，pump 保证其紧跟所覆盖音频之后写线上——
        Twilio 只有在流内前面的音频播完后才回 ack，因此 mark 不能先于其音频
        发出。残样（不足一帧）在此刻成帧并入段，保证段边界与 mark 位置一致。
        同代际重名抛 ValueError（台账按 (epoch, name) 索引，重名会串账）。
        """
        key = (self._epoch, name)
        if key in self._marks:
            raise ValueError(f"duplicate mark name {name!r} in epoch {self._epoch}")
        seg = self._segments[-1]
        if self._buffer:
            samples = len(self._buffer) // 2
            self._enqueue_frame(seg, self._buffer, samples)  # 残样成短帧入账
            self._buffer = b""
        record = _MarkRecord(name=name, epoch=self._epoch, state=MarkState.QUEUED, segment=seg)
        self._marks[key] = record
        self._queue.append(_MarkItem(record=record))
        self._segments.append(_Segment(epoch=self._epoch))  # 开启下一段

    def ack_mark(self, name: str, *, epoch: int) -> bool:
        """Twilio 回送 mark ack：段内音频确认播出（queued/sent → played）。

        旧代际 ack（clear 之后才到）→ guard 丢弃返回 False，mark 保持 cleared
        （R-A-004：marks arriving after clear are classified as cleared）。
        未知 mark、已结算 mark 同样返回 False（防重复入账）。
        """
        if not self.guard(epoch):
            return False
        record = self._marks.get((epoch, name))
        if record is None or record.state not in (MarkState.QUEUED, MarkState.SENT):
            return False
        seg = record.segment
        seg.played += seg.pending
        seg.pending = 0
        record.state = MarkState.PLAYED
        return True

    def mark_state(self, name: str, *, epoch: int) -> MarkState | None:
        record = self._marks.get((epoch, name))
        return record.state if record else None

    # ------------------------------------------------------------------
    # 核算（样本数 → 秒）
    # ------------------------------------------------------------------

    @property
    def generated_duration(self) -> float:
        return sum(s.pending + s.played + s.cleared for s in self._segments) / MEDIA_STREAM_RATE

    @property
    def played_duration(self) -> float:
        """仅 mark 确认的播出时长（策略 1：中断内容未播出部分绝不计入）。"""
        return sum(s.played for s in self._segments) / MEDIA_STREAM_RATE

    @property
    def cleared_duration(self) -> float:
        return sum(s.cleared for s in self._segments) / MEDIA_STREAM_RATE

    @property
    def pending_duration(self) -> float:
        """已生成但尚未结算（未 ack 也未 clear）的时长；静默态应为 0。"""
        return sum(s.pending for s in self._segments) / MEDIA_STREAM_RATE

    def _epoch_split(self, epoch: int) -> tuple[float, float, float]:
        """某代际的 (generated, played, cleared)——历史记录的核算拆分。"""
        segs = [s for s in self._segments if s.epoch == epoch]
        gen = sum(s.pending + s.played + s.cleared for s in segs)
        played = sum(s.played for s in segs)
        cleared = sum(s.cleared for s in segs)
        return gen / MEDIA_STREAM_RATE, played / MEDIA_STREAM_RATE, cleared / MEDIA_STREAM_RATE

    # ------------------------------------------------------------------
    # flush / barge-in
    # ------------------------------------------------------------------

    def flush(self) -> None:
        """清空本地出向队列并把所有未结算段落判为 cleared（策略 4）。

        残样、队列中未发帧、已发未 ack 的 mark 覆盖段——全部「不会被确认播出」。
        必须与线上 ``clear`` 或通话拆除配对；``on_user_speech_confirmed`` 内部
        以 clear → flush 顺序调用。
        """
        if self._buffer:
            # 残样产出过但从未入流：generated + cleared。
            self._segments[-1].cleared += len(self._buffer) // 2
            self._buffer = b""
        # 台账里所有未结算 mark（含已写线上、从队列弹出的 in-flight）一律 cleared：
        # clear 之后 Twilio 不会再播它们，ack 也不会再有效到达。
        for record in self._marks.values():
            if record.state in (MarkState.QUEUED, MarkState.SENT):
                record.state = MarkState.CLEARED
        self._queue.clear()
        self._frames_queued = 0
        self._bytes_queued = 0
        for seg in self._segments:
            seg.cleared += seg.pending
            seg.pending = 0

    async def on_user_speech_confirmed(self) -> bool:
        """用户确认说话 → 一次打断事件。返回 True 表示发生了 epoch 转移。

        顺序：递增 epoch（guard 立即封锁旧产物）→ 发恰一条 clear → flush →
        写中断历史（若该代际登记过助手内容）→ 取消 LLM/TTS。历史先于取消写入，
        保证取消回调异常时中断记录不丢。去抖窗口内的重复确认被吸收返回 False
        （策略 2：同一次打断只有一条 clear）。
        """
        now = self._clock()
        if self._last_barge_in is not None and now - self._last_barge_in < self._debounce:
            return False
        self._last_barge_in = now

        interrupted_epoch = self._epoch
        self._epoch += 1

        await self._send_event({**build_clear_event(), "streamSid": self._stream_sid})
        self.flush()
        # 新代际的音频必须落在新段上（段带 epoch 标签，历史按代际拆账）。
        self._segments.append(_Segment(epoch=self._epoch))

        parts = self._content_by_epoch.pop(interrupted_epoch, [])
        content = "".join(parts)
        if content and self._history_sink is not None:
            gen, played, cleared = self._epoch_split(interrupted_epoch)
            await self._history_sink(
                {
                    "role": "assistant",
                    "content": content,
                    "interrupted": True,
                    "epoch": interrupted_epoch,
                    "generated_duration": gen,
                    "played_duration": played,
                    "cleared_duration": cleared,
                }
            )

        if self._cancel_llm is not None:
            await self._cancel_llm()
        if self._cancel_tts is not None:
            await self._cancel_tts()
        return True

    # ------------------------------------------------------------------
    # 输出节流（pacing）
    # ------------------------------------------------------------------

    async def pump(self) -> int:
        """按注入时钟把队列音频以 20ms 帧滴送到线上。返回本次发出的音频帧数。

        节流规则：每个 20ms 时钟区间发一帧（落后可追赶、无区间上限）；mark 事件
        不占区间、紧跟其覆盖音频之后写线上；队列排空后不欠债（下次新音频立即
        可发首帧）。无真实 sleep——由驱动循环按自己的节奏调用。
        """
        now = self._clock()
        sent = 0
        await self._flush_leading_marks()
        while self._next_frame_at <= now:
            item = self._pop_frame()
            if item is None:
                self._next_frame_at = now  # 空队列不欠债
                break
            await self._send_event(build_media_event(self._stream_sid, item.pcm))
            sent += 1
            self._next_frame_at += FRAME_INTERVAL
            await self._flush_leading_marks()  # mark 紧跟其音频（流内顺序 = ack 语义）
        return sent

    def _pop_frame(self) -> _Frame | None:
        """弹出队首音频帧。队首是 mark 或队列已空 → None（mark 由 _flush_leading_marks 先发）。"""
        if not self._queue:
            return None
        item = self._queue[0]
        if isinstance(item, _MarkItem):
            return None
        self._queue.popleft()
        self._frames_queued -= 1
        self._bytes_queued -= len(item.pcm)
        return item

    async def _flush_leading_marks(self) -> None:
        while self._queue and isinstance(self._queue[0], _MarkItem):
            item = self._queue.popleft()
            record = item.record
            if record.state is MarkState.QUEUED:
                record.state = MarkState.SENT
            await self._send_event({**build_mark_event(record.name), "streamSid": self._stream_sid})
