"""U-101 — Azure Speech STT (FC-2) / TTS (FC-4) adapters over pipecat-ai 1.4.0.

冻结卡（implementation-plan §5 U-101）：providers/speech/azure_speech.py 以
docs/azure/pipecat-1.4.0-api-notes.md 为准包装 1.4.0 Azure services，注册
STT_PROVIDER=azure_speech / TTS_PROVIDER=azure_speech 两个 registry 名。

与派单措辞的偏差（冻结卡为准，逐条记录于 docs/azure/u-101-report.md）：
- 派单写 providers/stt/azure_speech.py（仅 FC-2）；冻结卡是 providers/speech/
  azure_speech.py（FC-2 + FC-4）——按卡落盘。
- 派单写 AZURE_LOCATION；§11 冻结面用 SPEECH_REGION / SPEECH_ENDPOINT——按 §11。
- MiniMax 可选 FC-4 适配器需 G-1/G-2 批准，未批准 → 不实现，
  SimpleMiniMaxTTSService 路径不动（卡内明示）。

CR 缺口（不在此发明方案，报告走 CR）：
- §11 无 SPEECH_KEY 类凭据占位符（30 个冻结变量里只有 KEY_VAULT_URL +
  TWILIO_AUTH_TOKEN_SECRET_NAME 这类 secret-name 先例）→ api_key 走工厂构造
  注入（make_stt_factory / make_tts_factory 的 api_key 形参），补 §11 变量属 CR。
- pipecat-ai[azure]（azure-cognitiveservices-speech<2,>=1.47.0）不在部署钉里
  （api-notes §1.1/§1.2 LIVE import FAILED）→ 桥接类懒加载，本环境无法 live
  wrap，全部行为测试走 service_factory 注入的 fixture。

服务工厂缝（fixture 驱动测试的注入面）——工厂被调用时收到的关键字参数：
- STT: api_key, region, private_endpoint, language, sample_rate, event_sink
  产物协议：async open() / async feed(pcm: bytes) / async close()
  event_sink(item: TranscriptEvent | BaseException) —— 流结束哨兵由适配器自理
- TTS: api_key, region, private_endpoint, voice, sample_rate
  产物协议：async open() / run_tts(text, context_id)（产出带 .audio 的帧）/ async close()

TTS 包装类选型（api-notes §1.2/§1.3「either class is constructible」）：取
AzureHttpTTSService —— 其 run_tts 是单次自包含调用并已剥 44 字节 WAV 头
（wheel tts.py:964-966），无 SDK 线程回调/词边界任务，独立驱动面最小；流式
AzureTTSService 的词时间戳机器只为管道服务（本适配器全部丢弃）。G-2 若要求
流式分块 TTS，换类是一行工厂改动。
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

from config_contract import AppConfig
from providers.base import (
    AudioFormat,
    SpeechRecognizer,
    SpeechSynthesizer,
    SynthesisChunk,
    TranscriptEvent,
)
from providers.registry import ProviderKind, ProviderRegistry
from providers.speech._tts_stream import stream_synthesis

# FC-0 冻结：AudioFormat.sample_rate 只允许这三个值（base.py:21 注释）
_SYSTEM_SAMPLE_RATES = (8000, 16000, 24000)

# registry 名（冻结卡 §5 U-101 明示）
STT_REGISTRY_NAME = "azure_speech"
TTS_REGISTRY_NAME = "azure_speech"


def _require(value: str | None, name: str, hint: str) -> str:
    v = (value or "").strip()
    if not v:
        raise ValueError(f"{name}: empty — {hint}")
    return v


# ---------------------------------------------------------------------------
# FC-2 SpeechRecognizer
# ---------------------------------------------------------------------------


class AzureSpeechRecognizer(SpeechRecognizer):
    """Azure Speech STT。包装 pipecat 1.4.0 AzureSTTService（经服务工厂缝）。

    能力标志：AzureSTTService 按会话固定单一识别语言，未驱动识别语言自动检测
    （wheel ctor 只收单个 language）→ supports_language_detection=False。
    """

    # U-101 钉死：声明 = 适配器现实，不为对齐旧 AWS Transcribe 能力而虚报
    supports_language_detection = False

    def __init__(
        self,
        *,
        api_key: str,
        region: str,
        locale: str,
        endpoint: str | None = None,
        service_factory: Callable[..., Any] | None = None,
    ) -> None:
        # api_key 没有 §11 变量名可指（CR 缺口），错误里指明注入通道
        self._api_key = _require(
            api_key, "api_key",
            "Azure Speech subscription key required (no §11 credential placeholder yet "
            "— U-101 CR gap; inject via make_stt_factory(api_key=...))",
        )
        region = (region or "").strip()
        endpoint = (endpoint or "").strip()
        if not region and not endpoint:
            # 与 wheel ctor 同语义（要么 region 要么 private_endpoint），提前响亮报错并指名 §11 变量
            raise ValueError(
                "region/endpoint: at least one of SPEECH_REGION or SPEECH_ENDPOINT is required"
            )
        self._region = region or None
        self._endpoint = endpoint or None
        self._locale = _require(locale, "STT_LOCALE", "recognition locale required (§11)")
        self._service_factory = service_factory
        self._queue: asyncio.Queue[TranscriptEvent | BaseException | None] = asyncio.Queue()
        self._service: Any = None

    async def start(self, *, language: str, input_format: AudioFormat) -> None:
        if input_format.encoding != "pcm16":
            # FC-1 decode_incoming_media 的归一化输出是 PCM16；μ-law 转换属 U-203 音频契约
            raise ValueError(
                f"input_format.encoding: must be 'pcm16' (FC-1 decode produces PCM16), "
                f"got {input_format.encoding!r}"
            )
        if input_format.sample_rate not in _SYSTEM_SAMPLE_RATES:
            raise ValueError(
                f"input_format.sample_rate: must be one of {_SYSTEM_SAMPLE_RATES} "
                f"(FC-0 AudioFormat), got {input_format.sample_rate!r}"
            )
        if self._service is not None:
            raise RuntimeError("already started — stop() before starting again")
        # 显式 per-session language 优先；空则用配置的 STT_LOCALE（§11）。两者皆空在构造期已封死
        lang = (language or "").strip() or self._locale
        factory = self._service_factory or _default_stt_service_factory
        self._service = factory(
            api_key=self._api_key,
            region=self._region,
            private_endpoint=self._endpoint,
            language=lang,
            sample_rate=input_format.sample_rate,
            event_sink=self._queue.put_nowait,
        )
        await self._service.open()

    async def push_audio(self, pcm: bytes) -> None:
        if self._service is None:
            raise RuntimeError("push_audio before start()")
        await self._service.feed(pcm)

    def events(self) -> AsyncIterator[TranscriptEvent]:
        return self._events()

    async def _events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def stop(self) -> None:
        if self._service is not None:
            await self._service.close()
            self._service = None
        # 流结束哨兵：events() 迭代在此终止（stop() 幂等，多放一个哨兵无害）
        self._queue.put_nowait(None)


# ---------------------------------------------------------------------------
# FC-4 SpeechSynthesizer
# ---------------------------------------------------------------------------


class AzureSpeechSynthesizer(SpeechSynthesizer):
    """Azure Speech TTS。包装 pipecat 1.4.0 AzureHttpTTSService（经服务工厂缝）。

    FC-4 无显式 start/stop：服务按 synthesize() 调用惰性建/关（HTTP 类的 open
    只建本地 SpeechConfig/SpeechSynthesizer 对象，连接按 speak_ssml 即时建立），
    cancel() 提升 generation 并关掉活动服务；跨调用零状态。
    """

    def __init__(
        self,
        *,
        api_key: str,
        region: str,
        voice: str,
        sample_rate: int,
        endpoint: str | None = None,
        service_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._api_key = _require(
            api_key, "api_key",
            "Azure Speech subscription key required (no §11 credential placeholder yet "
            "— U-101 CR gap; inject via make_tts_factory(api_key=...))",
        )
        region = (region or "").strip()
        endpoint = (endpoint or "").strip()
        if not region and not endpoint:
            raise ValueError(
                "region/endpoint: at least one of SPEECH_REGION or SPEECH_ENDPOINT is required"
            )
        self._region = region or None
        self._endpoint = endpoint or None
        self._voice = _require(voice, "TTS_VOICE", "synthesis voice required (§11)")
        if sample_rate not in _SYSTEM_SAMPLE_RATES:
            # 采样率不是 §11 配置项，是 FC-0 音频契约参数，由组合根（U-103）显式传入——不做默认值（D-4 姿态）
            raise ValueError(
                f"sample_rate: must be one of {_SYSTEM_SAMPLE_RATES} (FC-0 AudioFormat), "
                f"got {sample_rate!r}"
            )
        self._sample_rate = sample_rate
        self._service_factory = service_factory
        self._generation = 0
        self._service: Any = None

    def output_format(self) -> AudioFormat:
        return AudioFormat(sample_rate=self._sample_rate, encoding="pcm16", channels=1)

    def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[SynthesisChunk]:
        return self._synthesize(text_stream)

    async def _synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[SynthesisChunk]:
        if self._service is not None:
            raise RuntimeError("concurrent synthesize() not supported — cancel() first")
        generation = self._generation
        factory = self._service_factory or _default_tts_service_factory
        service = factory(
            api_key=self._api_key,
            region=self._region,
            private_endpoint=self._endpoint,
            voice=self._voice,
            sample_rate=self._sample_rate,
        )
        self._service = service
        await service.open()
        try:
            async for chunk in stream_synthesis(
                service=service,
                text_stream=text_stream,
                context_id=f"gen{generation}",
                output_format=self.output_format(),
                is_current=lambda: self._generation == generation,
                error_label="Azure TTS synthesis failed",
            ):
                yield chunk
        finally:
            if self._service is service:
                self._service = None
                await service.close()

    async def cancel(self) -> None:
        self._generation += 1
        service, self._service = self._service, None
        if service is not None:
            # 在途 Azure 调用（线程内 speak_ssml）无法中断，但产物已被丢弃
            await service.close()


# ---------------------------------------------------------------------------
# 默认服务工厂：pipecat 1.4.0 桥接子类（懒加载——azure SDK 不在部署钉里）
# ---------------------------------------------------------------------------


def _default_stt_service_factory(
    *, api_key, region, private_endpoint, language, sample_rate, event_sink
):
    from pipecat.frames.frames import EndFrame, StartFrame
    from pipecat.services.azure.stt import AzureSTTService, AzureSTTSettings

    class _AzureSttBridge(AzureSTTService):
        """独立驱动桥：只覆写管道缝，wheel 的 SDK 回调路径原样工作。

        缝位注（api-notes §1.1 + wheel stt.py:334-387）：SDK 线程回调经
        run_coroutine_threadsafe(..., self.get_event_loop()) 投递转写帧/错误；
        get_event_loop 在无管道时会炸（task_manager 未 setup），故覆写为存下的
        事件循环；push_frame 翻译成 TranscriptEvent 入事件队列（FC-2 events()），
        push_error 变成队列异常（events() 抛出）。_handle_transcription 覆写为空：
        final 帧已由 push_frame 翻译，避免同一结果发两次事件。
        """

        def __init__(self, *, event_sink, **kwargs):
            self._event_sink = event_sink
            self._loop: asyncio.AbstractEventLoop | None = None
            super().__init__(**kwargs)

        async def open(self) -> None:
            self._loop = asyncio.get_running_loop()
            await self.start(StartFrame(audio_in_sample_rate=self.sample_rate))

        async def feed(self, pcm: bytes) -> None:
            async for frame in self.run_stt(pcm):
                if frame is not None:
                    raise RuntimeError(
                        f"Azure STT feed failed: {getattr(frame, 'error', frame)!r}"
                    )

        async def close(self) -> None:
            await self.stop(EndFrame())

        def get_event_loop(self) -> asyncio.AbstractEventLoop:
            return self._loop

        async def push_frame(self, frame, direction=None) -> None:
            is_final = type(frame).__name__
            if is_final == "InterimTranscriptionFrame":
                self._event_sink(
                    TranscriptEvent(
                        text=frame.text, is_final=False, language=_frame_language(frame)
                    )
                )
            elif is_final == "TranscriptionFrame":
                self._event_sink(
                    TranscriptEvent(
                        text=frame.text, is_final=True, language=_frame_language(frame)
                    )
                )

        async def _handle_transcription(self, transcript, is_final, language=None) -> None:
            pass  # final 走 push_frame 单通道，防重发

        async def push_error(self, error_msg: str, exception=None, fatal: bool = False) -> None:
            self._event_sink(RuntimeError(f"Azure STT: {error_msg}"))

    return _AzureSttBridge(
        event_sink=event_sink,
        api_key=api_key,
        region=region,
        private_endpoint=private_endpoint,
        language=language,
        sample_rate=sample_rate,
    )


def _frame_language(frame) -> str | None:
    lang = getattr(frame, "language", None)
    return str(lang) if lang is not None else None


def _default_tts_service_factory(*, api_key, region, private_endpoint, voice, sample_rate):
    from pipecat.frames.frames import EndFrame, StartFrame
    from pipecat.services.azure.tts import AzureHttpTTSService

    class _AzureTtsBridge(AzureHttpTTSService):
        """独立驱动桥：覆写管道缝，run_tts 原样复用（yield 帧自带剥头 PCM）。

        缝位注（wheel tts_service.py:540/:1406）：TTSService.start/stop 会经
        create_task 建音频上下文任务——task_manager 未 setup，故覆写
        create_task/cancel_task 落到事件循环裸任务；push_frame 无下游可推（no-op），
        该任务空转到 stop() 的哨兵即退出。
        """

        async def open(self) -> None:
            await self.start(StartFrame(audio_out_sample_rate=self.sample_rate))

        async def close(self) -> None:
            await self.stop(EndFrame())

        def get_event_loop(self) -> asyncio.AbstractEventLoop:
            return asyncio.get_running_loop()

        def create_task(self, coroutine, name=None):
            return asyncio.get_running_loop().create_task(coroutine)

        async def cancel_task(self, task, timeout: float | None = 1.0):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        async def push_frame(self, frame, direction=None) -> None:
            pass

    return _AzureTtsBridge(
        api_key=api_key,
        region=region,
        private_endpoint=private_endpoint,
        voice=voice,
        sample_rate=sample_rate,
    )


# ---------------------------------------------------------------------------
# registry 工厂构建器（冻结卡：STT_PROVIDER=azure_speech / TTS_PROVIDER=azure_speech）
# ---------------------------------------------------------------------------


def make_stt_factory(
    *, api_key: str, service_factory: Callable[..., Any] | None = None
) -> Callable[[AppConfig], AzureSpeechRecognizer]:
    def factory(config: AppConfig) -> AzureSpeechRecognizer:
        return AzureSpeechRecognizer(
            api_key=api_key,
            region=config.speech_region,
            endpoint=config.speech_endpoint,
            locale=config.stt_locale,
            service_factory=service_factory,
        )

    return factory


def make_tts_factory(
    *,
    api_key: str,
    sample_rate: int,
    service_factory: Callable[..., Any] | None = None,
) -> Callable[[AppConfig], AzureSpeechSynthesizer]:
    def factory(config: AppConfig) -> AzureSpeechSynthesizer:
        return AzureSpeechSynthesizer(
            api_key=api_key,
            region=config.speech_region,
            endpoint=config.speech_endpoint,
            voice=config.tts_voice,
            sample_rate=sample_rate,
            service_factory=service_factory,
        )

    return factory


def register(
    registry: ProviderRegistry, *, api_key: str, tts_sample_rate: int
) -> None:
    """注册 azure_speech 两个 registry 名（冻结卡 U-101 交付物）。"""
    registry.register(
        ProviderKind.STT, STT_REGISTRY_NAME, make_stt_factory(api_key=api_key)
    )
    registry.register(
        ProviderKind.TTS,
        TTS_REGISTRY_NAME,
        make_tts_factory(api_key=api_key, sample_rate=tts_sample_rate),
    )
