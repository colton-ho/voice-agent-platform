"""U-103 — cascade_engine.py 测试（fixture 驱动，零凭据零网络）。

覆盖冻结卡四演示（spec Phase B completion）：本地流式、打断、生命周期清理、
幂等工具；外加 R-A-005 验收要求的各阶段超时/取消路径确定性测试（stt.start /
LLM 事件步 / TTS 音频块步）与 §7.1 显式语言守卫。fake 适配器按 FC-2/3/4
ABC 实现，TTS fake 复刻 stream_synthesis 的单块前瞻（is_final 只落终块）。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.base import (  # noqa: E402
    AudioFormat,
    LanguageModel,
    ModelEvent,
    SpeechRecognizer,
    SpeechSynthesizer,
    SynthesisChunk,
    ToolRequest,
    ToolSpec,
    TranscriptEvent,
    UsageReport,
)
from cascade_engine import CascadeEngine, _next_phrase  # noqa: E402

FMT = AudioFormat(sample_rate=16000, encoding="pcm16", channels=1)


class FakeSTT(SpeechRecognizer):
    supports_language_detection = False

    def __init__(self, *, start_hangs: bool = False) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self.start_hangs = start_hangs
        self.started: tuple | None = None
        self.stopped = 0
        self.pushed: list[bytes] = []

    async def start(self, *, language, input_format):
        if self.start_hangs:
            await asyncio.Event().wait()
        self.started = (language, input_format)

    async def push_audio(self, pcm: bytes) -> None:
        self.pushed.append(pcm)

    def events(self):
        return self._iter()

    async def _iter(self):
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    async def stop(self) -> None:
        self.stopped += 1
        self._queue.put_nowait(None)

    def emit(self, event: TranscriptEvent) -> None:
        self._queue.put_nowait(event)


class FakeLLM(LanguageModel):
    supports_tool_calls = True

    def __init__(self, rounds=None, *, delay: float = 0.0, stall: bool = False) -> None:
        self._rounds = [list(r) for r in (rounds or [])]
        self._delay = delay
        self.stall = stall
        self.calls: list[tuple] = []
        self.cancel_calls = 0

    def stream(self, *, messages, tools=None):
        self.calls.append((list(messages), list(tools or [])))
        events = self._rounds.pop(0) if self._rounds else [ModelEvent(kind="end")]
        return self._gen(events)

    async def _gen(self, events):
        if self.stall:
            await asyncio.Event().wait()
        for event in events:
            # 即便 delay=0 也逐步让出控制：真实流式源每个事件间都有 I/O 挂起点，
            # 否则 fake 会一口气跑完整轮，短语级交错被假性抹平
            await asyncio.sleep(self._delay)
            yield event

    async def cancel(self) -> None:
        self.cancel_calls += 1

    def usage(self) -> UsageReport:
        return UsageReport(provider="fake", model="fake", input_units=0, output_units=0, raw={})


class FakeTTS(SpeechSynthesizer):
    def __init__(self, *, stall: bool = False) -> None:
        self.texts: list[str] = []
        self.cancel_calls = 0
        self.stall = stall

    def output_format(self) -> AudioFormat:
        return AudioFormat(sample_rate=24000, encoding="pcm16", channels=1)

    def synthesize(self, text_stream):
        return self._gen(text_stream)

    async def _gen(self, text_stream):
        # 单块前瞻（与 _tts_stream.stream_synthesis 同语义）：is_final 只落终块
        pending = None
        async for text in text_stream:
            if self.stall:
                await asyncio.Event().wait()
            self.texts.append(text)
            chunk = SynthesisChunk(
                audio=b"x:" + text.encode("utf-8"), format=self.output_format(), is_final=False
            )
            if pending is not None:
                yield pending
            pending = chunk
        if pending is not None:
            yield SynthesisChunk(audio=pending.audio, format=pending.format, is_final=True)

    async def cancel(self) -> None:
        self.cancel_calls += 1


def _engine(stt, llm, tts, **kw):
    kw.setdefault("llm_event_timeout", 2.0)
    kw.setdefault("tts_event_timeout", 2.0)
    kw.setdefault("stt_start_timeout", 2.0)
    return CascadeEngine(stt=stt, llm=llm, tts=tts, input_format=FMT, **kw)


async def _collect_until_end(engine, *, timeout: float = 3.0):
    """收集 events() 直到回合级 end 事件（含）。"""
    got: list = []

    async def run():
        async for event in engine.events():
            got.append(event)
            if isinstance(event, ModelEvent) and event.kind == "end":
                return

    await asyncio.wait_for(run(), timeout)
    return got


def _is_delta(event) -> bool:
    return isinstance(event, ModelEvent) and event.kind == "delta"


# =====================================================================
# 演示 1 — 本地流式：短语级 LLM→TTS，首块音频先于整段回答结束
# =====================================================================

def test_local_streaming_phrase_level_tts():
    async def main():
        stt = FakeSTT()
        sentences = [f"第{i}句。" for i in range(1, 6)]
        llm = FakeLLM(rounds=[[
            *(ModelEvent(kind="delta", text=s) for s in sentences),
            ModelEvent(kind="usage", usage=UsageReport(
                provider="fake", model="fake", input_units=1, output_units=2, raw={})),
            ModelEvent(kind="end"),
        ]])
        tts = FakeTTS()
        engine = _engine(stt, llm, tts, system_prompt="sys")
        await engine.start(language="zh-HK")
        await engine.push_audio(b"\x00\x01")
        stt.emit(TranscriptEvent(text="你", is_final=False))
        stt.emit(TranscriptEvent(text="你好世界", is_final=True))
        events = await _collect_until_end(engine)

        # 转写先行：interim → final
        assert isinstance(events[0], TranscriptEvent) and events[0].is_final is False
        assert isinstance(events[1], TranscriptEvent) and events[1].is_final is True

        # LLM 增量原序外发
        deltas = [e.text for e in events if _is_delta(e)]
        assert deltas == sentences

        # 短语级：5 个句末短语逐段喂 TTS（有界：按句末标点切）
        assert tts.texts == sentences

        # 流式证明：首块音频先于整段回答结束（最后一句 delta 之前已出音频）
        kinds = [type(e).__name__ + (":" + e.kind if isinstance(e, ModelEvent) else "") for e in events]
        first_chunk = next(i for i, e in enumerate(events) if isinstance(e, SynthesisChunk))
        last_delta = max(i for i, e in enumerate(events) if _is_delta(e))
        assert first_chunk < last_delta, kinds

        # FC-4 契约：终块 is_final=True，此前全是 False；回合以单个 end 收尾
        chunks = [e for e in events if isinstance(e, SynthesisChunk)]
        assert [c.is_final for c in chunks] == [False, False, False, False, True]
        assert all(c.audio for c in chunks)
        assert isinstance(events[-1], ModelEvent) and events[-1].kind == "end"
        assert any(isinstance(e, ModelEvent) and e.kind == "usage" for e in events)

        # 回合消息形状（U-102 钉死用法）：首轮调用收到 system + user；
        # assistant 正文是该轮产出，回合后才入史（多轮形状由工具演示钉）
        msgs = llm.calls[0][0]
        assert [m.role for m in msgs] == ["system", "user"]
        assert msgs[1].content == "你好世界"

    asyncio.run(main())


# =====================================================================
# 演示 2 — 打断（R-P-009）：LLM+TTS 取消，产出即刻停止，会话可复用
# =====================================================================

def test_interruption_cancels_llm_and_tts():
    async def main():
        stt = FakeSTT()
        slow = [ModelEvent(kind="delta", text=f"第{i}句。") for i in range(1, 30)]
        llm = FakeLLM(rounds=[slow, [ModelEvent(kind="delta", text="第二回合。"),
                                    ModelEvent(kind="end")]], delay=0.05)
        tts = FakeTTS()
        engine = _engine(stt, llm, tts)
        await engine.start(language="zh-HK")
        stt.emit(TranscriptEvent(text="第一題", is_final=True))

        got: list = []
        end_seen = asyncio.Event()

        async def drain():
            async for event in engine.events():
                got.append(event)
                if isinstance(event, ModelEvent) and event.kind == "end":
                    end_seen.set()

        drain_task = asyncio.create_task(drain())
        await asyncio.sleep(0.12)  # 回合 1 进行中（delay=0.05 → 已出若干 delta）
        assert any(_is_delta(e) for e in got)

        await engine.interrupt()
        assert llm.cancel_calls >= 1 and tts.cancel_calls >= 1

        n = len(got)
        await asyncio.sleep(0.25)  # 未取消的话约 5 个新 delta 会继续出
        assert len(got) == n, "打断后产出必须即刻停止"
        assert not any(isinstance(e, ModelEvent) and e.kind == "end" for e in got), \
            "被作废的回合不发 end（interrupt 是动作不是错误）"

        # 会话复用：新回合正常收尾
        stt.emit(TranscriptEvent(text="第二題", is_final=True))
        await asyncio.wait_for(end_seen.wait(), 2.0)
        assert isinstance(got[-1], ModelEvent) and got[-1].kind == "end"
        drain_task.cancel()

    asyncio.run(main())


# =====================================================================
# 演示 3 — 生命周期清理：close() 幂等、stt.stop、在途回合被取消
# =====================================================================

def test_lifecycle_cleanup_idempotent():
    async def main():
        stt = FakeSTT()
        llm = FakeLLM(stall=True)  # 回合挂在 LLM 上 → close 时必须取消
        tts = FakeTTS()
        engine = _engine(stt, llm, tts)
        await engine.start(language="en-US")
        await engine.push_audio(b"\x00\x00")
        stt.emit(TranscriptEvent(text="hi", is_final=True))
        await asyncio.sleep(0.05)  # 回合已开始（挂在 stall LLM 上）

        await engine.close()
        assert stt.stopped == 1
        assert llm.cancel_calls >= 1 and tts.cancel_calls >= 1

        await engine.close()  # 幂等：不抛错、不重复 stop
        assert stt.stopped == 1

        # 事件出口关闭：drain 会终止，且被作废回合没有 end
        events = await asyncio.wait_for(_drain_all(engine), 2.0)
        assert not any(isinstance(e, ModelEvent) and e.kind == "end" for e in events)

        # 关闭后拒绝继续喂音频
        with pytest.raises(RuntimeError):
            await engine.push_audio(b"\x00")

    asyncio.run(main())


async def _drain_all(engine):
    return [event async for event in engine.events()]


# =====================================================================
# 演示 4 — 冪等工具：同 ToolRequest.id 只执行一次，重复请求回放缓存
# =====================================================================

def test_tool_requests_execute_once_per_id():
    async def main():
        tool = ToolSpec(name="get_weather", description="查天气",
                        parameters_schema={"type": "object"})
        tr = ToolRequest(id="call_1", name="get_weather", arguments={})
        llm = FakeLLM(rounds=[
            [ModelEvent(kind="delta", text="我查一下。"),
             ModelEvent(kind="tool_request", tool_request=tr),
             ModelEvent(kind="end")],
            # 重复 id（重试/重发）→ 必须走缓存，不得二次执行
            [ModelEvent(kind="tool_request", tool_request=tr),
             ModelEvent(kind="end")],
            [ModelEvent(kind="delta", text="天氣是晴。"),
             ModelEvent(kind="end")],
        ])
        executed: list[str] = []

        async def execute_tool(request: ToolRequest) -> str:
            executed.append(request.id)
            return '{"ok": true}'

        stt, tts = FakeSTT(), FakeTTS()
        engine = _engine(stt, llm, tts, tools=[tool], execute_tool=execute_tool)
        await engine.start(language="zh-HK")
        stt.emit(TranscriptEvent(text="今天天氣如何", is_final=True))
        events = await _collect_until_end(engine)

        assert executed == ["call_1"], "同 id 重复请求只执行一次（幂等）"
        assert len(llm.calls) == 3

        # 工具回执按 U-102 钉死形状回灌：assistant 正文 + role="tool" 回执
        for round_no in (1, 2):
            msgs = llm.calls[round_no][0]
            roles = [m.role for m in msgs]
            assert roles[-2:] == ["assistant", "tool"], roles
            assert msgs[-1].tool_call_id == "call_1"
            assert msgs[-1].name == "get_weather"
            assert msgs[-1].content == '{"ok": true}'
        assert llm.calls[1][0][-2].content == "我查一下。"

        # 工具请求事件外发；最终回答照常收尾
        assert any(isinstance(e, ModelEvent) and e.kind == "tool_request" for e in events)
        assert isinstance(events[-1], ModelEvent) and events[-1].kind == "end"

    asyncio.run(main())


# =====================================================================
# R-A-005 验收 — 各阶段超时/取消路径（确定性）
# =====================================================================

def test_llm_event_timeout_cancels_llm():
    async def main():
        stt, tts = FakeSTT(), FakeTTS()
        llm = FakeLLM(stall=True)
        errors: list = []
        engine = _engine(stt, llm, tts, llm_event_timeout=0.05,
                         on_turn_error=lambda stage, exc: errors.append((stage, exc)))
        await engine.start(language="en-US")
        stt.emit(TranscriptEvent(text="hi", is_final=True))
        events = await _collect_until_end(engine)

        assert errors and errors[0][0] == "llm"
        assert isinstance(errors[0][1], TimeoutError)
        assert llm.cancel_calls >= 1, "LLM 超时必须走 cancel 取消路径"
        assert isinstance(events[-1], ModelEvent) and events[-1].kind == "end"

    asyncio.run(main())


def test_tts_event_timeout_cancels_tts():
    async def main():
        stt = FakeSTT()
        llm = FakeLLM(rounds=[[ModelEvent(kind="delta", text="一句話。"),
                               ModelEvent(kind="end")]])
        tts = FakeTTS(stall=True)
        errors: list = []
        engine = _engine(stt, llm, tts, tts_event_timeout=0.05,
                         on_turn_error=lambda stage, exc: errors.append((stage, exc)))
        await engine.start(language="en-US")
        stt.emit(TranscriptEvent(text="hi", is_final=True))
        events = await _collect_until_end(engine)

        assert errors and errors[0][0] == "tts"
        assert isinstance(errors[0][1], TimeoutError)
        assert tts.cancel_calls >= 1, "TTS 超时必须走 cancel 取消路径"
        assert isinstance(events[-1], ModelEvent) and events[-1].kind == "end"

    asyncio.run(main())


def test_stt_start_timeout():
    async def main():
        stt = FakeSTT(start_hangs=True)
        engine = _engine(stt, FakeLLM(), FakeTTS(), stt_start_timeout=0.05)
        with pytest.raises(TimeoutError):
            await engine.start(language="en-US")

    asyncio.run(main())


def test_stt_start_timeout_reclaims_half_open_service():
    """F-11：start 超时后半开服务必须当场回收，close() 干净收尾。"""
    async def main():
        stt = FakeSTT(start_hangs=True)
        engine = _engine(stt, FakeLLM(), FakeTTS(), stt_start_timeout=0.05)
        with pytest.raises(TimeoutError):
            await engine.start(language="en-US")
        # 超时回收 best-effort stop 已跑：半开服务不留到 close()
        assert stt.stopped == 1
        await engine.close()
        # close() 干净收尾（_started=False 不二次 stop），无半开态、无残留
        assert stt.stopped == 1
        assert engine._started is False

    asyncio.run(main())


# =====================================================================
# spec §7.1 — 显式识别语言（code-switching 不假设，配置错误响亮拒绝）
# =====================================================================

def test_explicit_language_required():
    async def main():
        stt = FakeSTT()
        engine = _engine(stt, FakeLLM(), FakeTTS())
        with pytest.raises(ValueError):
            await engine.start(language="   ")
        assert stt.started is None

    asyncio.run(main())


def test_new_final_transcript_barges_in_active_turn():
    """新 final 转写即 barge-in（R-P-009）：旧回合作废，新回合正常收尾。"""
    async def main():
        stt = FakeSTT()
        slow = [ModelEvent(kind="delta", text=f"长回答第{i}句。") for i in range(1, 30)]
        llm = FakeLLM(rounds=[slow, [ModelEvent(kind="delta", text="第二回合回答。"),
                                    ModelEvent(kind="end")]], delay=0.05)
        tts = FakeTTS()
        engine = _engine(stt, llm, tts)
        await engine.start(language="zh-HK")
        stt.emit(TranscriptEvent(text="第一題", is_final=True))
        await asyncio.sleep(0.12)
        stt.emit(TranscriptEvent(text="第二題", is_final=True))  # barge-in

        events = await _collect_until_end(engine)
        assert llm.cancel_calls >= 1
        assert isinstance(events[-1], ModelEvent) and events[-1].kind == "end"
        # 旧回合被作废：end 只会来自第二回合，且回答文本来自第二回合
        assert any(_is_delta(e) and e.text == "第二回合回答。" for e in events)

    asyncio.run(main())


# =====================================================================
# 短语切分纯逻辑（有界性）
# =====================================================================

def test_phrase_split_bounded():
    # 句末标点立即切（含余量）
    assert _next_phrase("今天天氣不錯。後面還有", 80) == ("今天天氣不錯。", "後面還有")
    # ASCII 句号只在后随空白时断句：3.14 / v1.2 不被切开
    assert _next_phrase("3.14 is pi", 80) is None
    assert _next_phrase("Hello. World", 80) == ("Hello.", " World")
    # 无标点则超长硬切（有界）
    assert _next_phrase("a" * 10, 4) == ("aaaa", "aaaaaa")
    # 未到边界不切
    assert _next_phrase("還沒結束", 80) is None
