"""U-103 — VOICE_ENGINE=cascade 的 STT→LLM→TTS 级联引擎（spec §7.1，R-A-005 前半）。

独立成模块（不进 bot.py）：引擎循环是可确定性测试的应用逻辑，bot.py 只做组合根
接线；FC-2/3/4 适配器全部构造注入，测试零凭据零网络（spec Phase B completion）。

行为基准（spec §7.1）：
- 连续流式识别（非逐句单次识别）：start 后持续 push_audio，final 转写触发回合；
- 显式识别语言：不假设 code-switching；检测能力只经 supports_language_detection
  如实声明——本系统适配器都不驱动语言检测，空 language 响亮拒绝而非静默兜底；
- 有界短语级 LLM→TTS：LLM 增量按句末标点/长度上限切短语逐段喂 FC-4，不等整段
  回答（首块音频先于回合结束产出）；
- 各阶段超时 + 取消：stt.start / LLM 事件步 / TTS 音频块步各有超时；interrupt()
  （barge-in，R-P-009）与 close() 走 cancel()/stop() 取消路径，均有确定性测试。

事件流 events() 形如 FC-5 的联合类型，但本引擎不是 FC-5（那是 spec §7.3
RealtimeVoiceEngine 的契约，冻结不动）。回合正常收尾补发单个 ModelEvent(kind="end")；
LLM 流内每轮的 end 不外发（工具多轮时逐轮 end 会被误读为回合结束）。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence

from providers.base import (
    AudioFormat,
    ChatMessage,
    LanguageModel,
    ModelEvent,
    SpeechRecognizer,
    SpeechSynthesizer,
    SynthesisChunk,
    ToolRequest,
    ToolSpec,
    TranscriptEvent,
)

EngineEvent = TranscriptEvent | ModelEvent | SynthesisChunk

# 句末标点（CJK 全角 + ASCII）；'.' 单独不算——3.14 / v1.2 不能被切开（见 _next_phrase）
_PHRASE_ENDERS = "。！？!?；;…\n"


def _next_phrase(buffer: str, max_chars: int) -> tuple[str, str] | None:
    """从 buffer 开头切出首个完整短语，返回 (短语, 余量)；切不出返回 None（继续累积）。

    有界性：句末标点立即切；无标点则超过 max_chars 硬切（R-A-005「bounded
    phrase-level streaming」）。
    """
    for i, ch in enumerate(buffer):
        if ch in _PHRASE_ENDERS:
            return buffer[: i + 1], buffer[i + 1 :]
        if ch == "." and i + 1 < len(buffer) and buffer[i + 1].isspace():
            return buffer[: i + 1], buffer[i + 1 :]
    if len(buffer) >= max_chars:
        return buffer[:max_chars], buffer[max_chars:]
    return None


async def _phrase_stream(phrases: asyncio.Queue) -> AsyncIterator[str]:
    while True:
        item = await phrases.get()
        if item is None:
            return
        yield item


class CascadeEngine:
    """FC-2 + FC-3 + FC-4 组合成一个级联语音会话（组合根产物）。

    生命周期：start() → push_audio() 会话内持续识别，events() 出 TranscriptEvent /
    ModelEvent / SynthesisChunk；interrupt() 打断在途回合后会话可继续用；close()
    幂等清理（stt.stop + 取消在途回合 + 关闭事件出口）。
    """

    def __init__(
        self,
        *,
        stt: SpeechRecognizer,
        llm: LanguageModel,
        tts: SpeechSynthesizer,
        input_format: AudioFormat,
        system_prompt: str = "",
        tools: Sequence[ToolSpec] = (),
        execute_tool: Callable[[ToolRequest], Awaitable[str]] | None = None,
        phrase_max_chars: int = 80,
        max_tool_rounds: int = 8,
        stt_start_timeout: float = 10.0,
        llm_event_timeout: float = 30.0,
        tts_event_timeout: float = 30.0,
        on_turn_error: Callable[[str, BaseException], None] | None = None,
    ) -> None:
        self._stt = stt
        self._llm = llm
        self._tts = tts
        self._input_format = input_format
        self._tools = tuple(tools)
        self._execute_tool = execute_tool
        self._phrase_max_chars = phrase_max_chars
        self._max_tool_rounds = max_tool_rounds
        self._stt_start_timeout = stt_start_timeout
        self._llm_event_timeout = llm_event_timeout
        self._tts_event_timeout = tts_event_timeout
        self._on_turn_error = on_turn_error
        self._history: list[ChatMessage] = (
            [ChatMessage(role="system", content=system_prompt)] if system_prompt else []
        )
        # 工具幂等缓存：ToolRequest.id -> 执行结果（重试/重发的同 id 请求只执行一次）
        self._tool_results: dict[str, str] = {}
        self._out: asyncio.Queue[EngineEvent | None] = asyncio.Queue()
        self._started = False
        self._closed = False
        self._turn_gen = 0
        self._turn_task: asyncio.Task | None = None
        self._stt_task: asyncio.Task | None = None

    # -- 会话生命周期 -------------------------------------------------------

    async def start(self, *, language: str) -> None:
        if self._closed:
            raise RuntimeError("engine closed")
        if self._started:
            raise RuntimeError("already started — close() before restarting")
        lang = (language or "").strip()
        if not lang:
            # 显式识别语言是硬要求：系统内适配器都不驱动语言检测，靠检测的配置
            # 错误必须响亮失败而不是静默接受（spec §7.1 / R-A-005）
            raise ValueError(
                "language: explicit recognition locale required — auto-detection is "
                "unsupported (SpeechRecognizer.supports_language_detection=False) and "
                "code-switching is never assumed (spec §7.1)"
            )
        try:
            await asyncio.wait_for(
                self._stt.start(language=lang, input_format=self._input_format),
                self._stt_start_timeout,
            )
        except (TimeoutError, asyncio.CancelledError):
            # F-11：超时/取消发生在 start 途中时适配器可能已半开（_service 已建、
            # open() 未完），而 close() 只在 _started 时 stop()——此处不回收就永久
            # 漏，重试 start 还会被适配器 "already started" 拒绝。best-effort 回收
            # （二级异常吞掉，不得遮蔽原始异常）后原样上抛。
            with contextlib.suppress(Exception):
                await self._stt.stop()
            raise
        self._started = True
        self._stt_task = asyncio.create_task(self._pump_stt())

    async def push_audio(self, pcm: bytes) -> None:
        if not self._started or self._closed:
            raise RuntimeError("push_audio outside an open session")
        await self._stt.push_audio(pcm)

    def events(self) -> AsyncIterator[EngineEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[EngineEvent]:
        while True:
            item = await self._out.get()
            if item is None:
                return
            yield item

    async def interrupt(self) -> None:
        """R-P-009 barge-in：作废在途回合（LLM+TTS 取消），会话本身继续可用。"""
        self._turn_gen += 1
        await self._llm.cancel()
        await self._tts.cancel()
        task, self._turn_task = self._turn_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def close(self) -> None:
        if self._closed:
            return  # 生命周期清理幂等
        self._closed = True
        await self.interrupt()
        if self._started:
            await self._stt.stop()
        task, self._stt_task = self._stt_task, None
        if task is not None and not task.done():
            task.cancel()  # stt.stop() 后泵任务只剩收尾，取消即干净退出
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self._out.put(None)

    # -- STT 泵：final 转写触发回合 ----------------------------------------

    async def _pump_stt(self) -> None:
        try:
            async for event in self._stt.events():
                await self._out.put(event)
                if event.is_final and (event.text or "").strip() and not self._closed:
                    await self._begin_turn(event.text.strip())
        except BaseException as e:
            if not self._closed:
                self._report_error("stt", e)
        finally:
            # STT 流死亡 = 会话结束：关闭事件出口（close() 的哨兵与此幂等）
            await self._out.put(None)

    async def _begin_turn(self, text: str) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            await self.interrupt()  # 新语音即 barge-in，旧回合产出作废（R-P-009）
        self._turn_gen += 1
        self._turn_task = asyncio.create_task(self._run_turn(text, self._turn_gen))

    # -- 回合：LLM 侧产短语 与 TTS 侧出音频 并行 ---------------------------

    async def _run_turn(self, text: str, generation: int) -> None:
        phrases: asyncio.Queue[str | None] = asyncio.Queue()
        llm_task = asyncio.create_task(self._drive_llm(phrases, text))
        tts_task = asyncio.create_task(self._drive_tts(phrases, generation))
        error: BaseException | None = None
        stage = ""
        try:
            # FIRST_EXCEPTION：任一侧失败即返回；否则等两侧都收尾
            await asyncio.wait({llm_task, tts_task}, return_when=asyncio.FIRST_EXCEPTION)
            for label, task in (("llm", llm_task), ("tts", tts_task)):
                if task.done() and not task.cancelled() and task.exception() is not None:
                    error, stage = task.exception(), label
                    break
        finally:
            for task in (llm_task, tts_task):
                if not task.done():
                    task.cancel()
            for task in (llm_task, tts_task):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if generation != self._turn_gen:
            return  # 打断/关闭作废的回合静默退场：interrupt 是动作不是错误
        if error is not None:
            # 失败回合的各阶段取消路径（R-A-005：超时/取消均有确定性测试）
            await self._llm.cancel()
            await self._tts.cancel()
            self._report_error(stage, error)
        await self._out.put(ModelEvent(kind="end"))

    async def _drive_llm(self, phrases: asyncio.Queue[str | None], text: str) -> None:
        self._history.append(ChatMessage(role="user", content=text))
        buffer = ""
        try:
            for _ in range(self._max_tool_rounds + 1):
                round_text: list[str] = []
                round_tools: list[ChatMessage] = []
                saw_tool = False
                async for event in self._stream_llm():
                    if event.kind == "delta" and event.text:
                        round_text.append(event.text)
                        await self._out.put(event)
                        buffer += event.text
                        while True:
                            split = _next_phrase(buffer, self._phrase_max_chars)
                            if split is None:
                                break
                            phrase, buffer = split
                            await phrases.put(phrase)
                    elif event.kind == "tool_request" and event.tool_request is not None:
                        await self._out.put(event)
                        result = await self._execute_tool_idempotent(event.tool_request)
                        round_tools.append(ChatMessage(
                            role="tool", content=result,
                            tool_call_id=event.tool_request.id,
                            name=event.tool_request.name,
                        ))
                        saw_tool = True
                    elif event.kind == "usage":
                        await self._out.put(event)
                    # 每轮的 end 不外发：回合级 end 由 _run_turn 在合成收尾后补发
                # 工具消息形状沿 U-102 钉死用法：assistant 正文 + role="tool" 回执
                self._history.append(ChatMessage(role="assistant", content="".join(round_text)))
                self._history.extend(round_tools)
                if not saw_tool:
                    break
            tail = buffer.strip()
            if tail:
                await phrases.put(tail)
        finally:
            phrases.put_nowait(None)  # 文本流关闭 → TTS 侧收尾（is_final 终块）

    async def _stream_llm(self) -> AsyncIterator[ModelEvent]:
        stream = self._llm.stream(messages=tuple(self._history), tools=self._tools or None)
        while True:
            try:
                event = await asyncio.wait_for(anext(stream), self._llm_event_timeout)
            except StopAsyncIteration:
                return
            yield event

    async def _drive_tts(self, phrases: asyncio.Queue[str | None], generation: int) -> None:
        stream = self._tts.synthesize(_phrase_stream(phrases))
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(anext(stream), self._tts_event_timeout)
                except StopAsyncIteration:
                    return
                if generation != self._turn_gen:
                    return
                await self._out.put(chunk)
        finally:
            with contextlib.suppress(Exception):
                await stream.aclose()

    async def _execute_tool_idempotent(self, request: ToolRequest) -> str:
        if request.id in self._tool_results:
            # 幂等：同 id 重复请求（重试/重发）直接回放缓存结果，绝不二次执行
            return self._tool_results[request.id]
        validate = getattr(self._llm, "validate_tool_request", None)
        if validate is not None:
            # FC-3 钩子只校验不执行；执行确认 = 组合根注入的 execute_tool
            validate(tool_request=request, tools=self._tools)
        if self._execute_tool is None:
            raise RuntimeError(
                f"tool {request.name!r} requested but no executor wired — "
                f"execute_tool is the composition-root execution confirmation"
            )
        result = await self._execute_tool(request)
        self._tool_results[request.id] = result
        return result

    def _report_error(self, stage: str, error: BaseException) -> None:
        if self._on_turn_error is not None:
            self._on_turn_error(stage, error)
