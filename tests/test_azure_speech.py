"""U-101 — providers/speech/azure_speech.py 测试（fixture 驱动，零真实 Azure）。

冻结卡：fixture-driven tests only（azure SDK 不在部署钉里，api-notes §1.1/§1.2
LIVE import FAILED）——服务工厂缝注入 fake 产物，测 FC-2/FC-4 行为、§11 映射、
响亮报错、registry 注册名。pipecat 桥接子类本身不在此环境执行（无 SDK）。
"""
import asyncio
import dataclasses

import pytest

from config_contract import load_config
from providers.base import AudioFormat, SpeechRecognizer, SpeechSynthesizer, TranscriptEvent
from providers.registry import ProviderKind, ProviderRegistry
from providers.speech.azure_speech import (
    AzureSpeechRecognizer,
    AzureSpeechSynthesizer,
    make_stt_factory,
    make_tts_factory,
    register,
)

BASE_ENV = {
    "APP_ENV": "test",
    "AZURE_LOCATION": "test-region",
    "PUBLIC_CONTROL_BASE_URL": "https://example.test",
    "PUBLIC_MEDIA_BASE_URL": "wss://example.test",
    "TELEPHONY_PROVIDER": "test_telephony_stub",
    "VOICE_ENGINE": "cascade",
    "STT_PROVIDER": "azure_speech",
    "TTS_PROVIDER": "azure_speech",
    "LLM_PROVIDER": "test_llm_stub",
    "LLM_ENDPOINT": "https://example.test/llm",
    "LLM_DEPLOYMENT": "test-deployment",
    "SPEECH_REGION": "eastus",
    "STT_LOCALE": "zh-CN",
    "TTS_VOICE": "zh-CN-XiaoxiaoNeural",
    "COSMOS_ENDPOINT": "https://example.test/cosmos",
    "COSMOS_DATABASE": "testdb",
    "BLOB_ACCOUNT_URL": "https://example.test/blob",
    "SERVICEBUS_NAMESPACE": "test-ns",
    "KEY_VAULT_URL": "https://example.test/kv",
    "MAX_CONCURRENT_CALLS_PER_REPLICA": "5",
    "MAX_CALL_DURATION_SECONDS": "3600",
    "TRANSCRIPT_RETENTION_DAYS": "30",
    "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=test",
}


def _config(**overrides):
    cfg = load_config(dict(BASE_ENV))
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


# --- fake 服务产物（工厂缝协议：STT open/feed/close + event_sink；TTS open/run_tts/close）


class _FakeSttService:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.fed: list[bytes] = []
        self.opened = 0
        self.closed = 0

    async def open(self):
        self.opened += 1

    async def feed(self, pcm: bytes):
        self.fed.append(pcm)

    async def close(self):
        self.closed += 1


def _fake_stt_factory(store):
    def factory(**kwargs):
        svc = _FakeSttService(**kwargs)
        store.append(svc)
        return svc

    return factory


class _FakeFrame:
    def __init__(self, audio: bytes):
        self.audio = audio


class _FakeErrorFrame:
    def __init__(self, error: str):
        self.error = error


class _FakeTtsService:
    def __init__(self, *, responses=None, **kwargs):
        self.kwargs = kwargs
        self.responses = responses or {}
        self.calls: list[tuple[str, str]] = []
        self.opened = 0
        self.closed = 0

    async def open(self):
        self.opened += 1

    async def close(self):
        self.closed += 1

    async def run_tts(self, text: str, context_id: str):
        self.calls.append((text, context_id))
        for frame in self.responses.get(text, [_FakeFrame(text.encode())]):
            yield frame


def _fake_tts_factory(store, responses=None):
    def factory(**kwargs):
        svc = _FakeTtsService(responses=responses, **kwargs)
        store.append(svc)
        return svc

    return factory


# --- FC-2 能力标志与构造校验 ------------------------------------------------


def test_recognizer_declares_no_language_detection():
    # 钉死：声明 = 适配器现实（AzureSTTService 每会话单一识别语言），不虚报
    assert AzureSpeechRecognizer.supports_language_detection is False


def test_recognizer_constructor_loud_errors():
    with pytest.raises(ValueError, match="api_key"):
        AzureSpeechRecognizer(api_key="", region="eastus", locale="en-US")
    with pytest.raises(ValueError, match="SPEECH_REGION"):
        AzureSpeechRecognizer(api_key="k", region="", locale="en-US")
    with pytest.raises(ValueError, match="STT_LOCALE"):
        AzureSpeechRecognizer(api_key="k", region="eastus", locale="")


# --- FC-2 start/语言解析/格式校验 -------------------------------------------


def test_start_prefers_explicit_language_then_configured_locale():
    store1: list = []
    store2: list = []
    rec1 = AzureSpeechRecognizer(
        api_key="k", region="eastus", locale="zh-CN", service_factory=_fake_stt_factory(store1)
    )
    rec2 = AzureSpeechRecognizer(
        api_key="k", region="eastus", locale="zh-CN", service_factory=_fake_stt_factory(store2)
    )

    async def run():
        await rec1.start(language="ja-JP", input_format=AudioFormat(16000, "pcm16"))
        await rec1.stop()
        await rec2.start(language="", input_format=AudioFormat(16000, "pcm16"))
        await rec2.stop()

    asyncio.run(run())
    assert store1[0].kwargs["language"] == "ja-JP"   # 显式 per-session 语言优先
    assert store2[0].kwargs["language"] == "zh-CN"   # 空则回落 §11 STT_LOCALE
    assert store1[0].kwargs["region"] == "eastus"
    assert store1[0].kwargs["sample_rate"] == 16000


def test_start_rejects_non_pcm16_and_off_contract_rate():
    rec = AzureSpeechRecognizer(
        api_key="k", region="eastus", locale="en-US", service_factory=_fake_stt_factory([])
    )

    async def run():
        with pytest.raises(ValueError, match="pcm16"):
            await rec.start(language="en-US", input_format=AudioFormat(8000, "mulaw"))
        with pytest.raises(ValueError, match="FC-0"):
            await rec.start(language="en-US", input_format=AudioFormat(44100, "pcm16"))

    asyncio.run(run())


# --- FC-2 push_audio / events / stop ----------------------------------------


def test_push_audio_feeds_service_and_events_flow_to_stop():
    store: list = []
    rec = AzureSpeechRecognizer(
        api_key="k", region="eastus", locale="en-US", service_factory=_fake_stt_factory(store)
    )

    # events() 以 stop() 收尾，故收集与 stop 并发推进
    async def run():
        await rec.start(language="en-US", input_format=AudioFormat(8000, "pcm16"))
        svc = store[0]
        await rec.push_audio(b"\x01\x02")
        svc.kwargs["event_sink"](TranscriptEvent(text="hel", is_final=False))
        svc.kwargs["event_sink"](TranscriptEvent(text="hello", is_final=True))
        task = asyncio.ensure_future(_collect(rec.events()))
        await rec.stop()
        return svc, await task

    svc, events = asyncio.run(run())
    assert svc.fed == [b"\x01\x02"]
    assert svc.opened == 1 and svc.closed == 1
    assert [(e.text, e.is_final) for e in events] == [("hel", False), ("hello", True)]


async def _collect(iterator):
    return [e async for e in iterator]


def test_events_raise_service_errors_and_stop_is_idempotent():
    store: list = []
    rec = AzureSpeechRecognizer(
        api_key="k", region="eastus", locale="en-US", service_factory=_fake_stt_factory(store)
    )

    async def run():
        await rec.start(language="en-US", input_format=AudioFormat(8000, "pcm16"))
        svc = store[0]
        svc.kwargs["event_sink"](RuntimeError("Azure STT: recognition canceled"))
        with pytest.raises(RuntimeError, match="recognition canceled"):
            await _collect(rec.events())
        await rec.stop()
        await rec.stop()  # 幂等：第二次不再 close 服务
        return svc

    svc = asyncio.run(run())
    assert svc.closed == 1


def test_push_audio_before_start_and_double_start_are_loud():
    rec = AzureSpeechRecognizer(
        api_key="k", region="eastus", locale="en-US", service_factory=_fake_stt_factory([])
    )

    async def run():
        with pytest.raises(RuntimeError, match="before start"):
            await rec.push_audio(b"")
        await rec.start(language="en-US", input_format=AudioFormat(8000, "pcm16"))
        with pytest.raises(RuntimeError, match="already started"):
            await rec.start(language="en-US", input_format=AudioFormat(8000, "pcm16"))
        await rec.stop()

    asyncio.run(run())


# --- FC-4 构造/输出格式 -----------------------------------------------------


def test_synthesizer_constructor_loud_errors():
    with pytest.raises(ValueError, match="api_key"):
        AzureSpeechSynthesizer(api_key="", region="eastus", voice="v", sample_rate=24000)
    with pytest.raises(ValueError, match="SPEECH_REGION"):
        AzureSpeechSynthesizer(api_key="k", region="", voice="v", sample_rate=24000)
    with pytest.raises(ValueError, match="TTS_VOICE"):
        AzureSpeechSynthesizer(api_key="k", region="eastus", voice="", sample_rate=24000)
    with pytest.raises(ValueError, match="FC-0"):
        AzureSpeechSynthesizer(api_key="k", region="eastus", voice="v", sample_rate=44100)


def test_output_format_pcm16_at_declared_rate():
    syn = AzureSpeechSynthesizer(
        api_key="k", region="eastus", voice="v", sample_rate=16000, service_factory=_fake_tts_factory([])
    )
    assert syn.output_format() == AudioFormat(sample_rate=16000, encoding="pcm16", channels=1)


# --- FC-4 synthesize / cancel ------------------------------------------------


def _text_stream(*items):
    async def gen():
        for item in items:
            yield item

    return gen()


def test_synthesize_phrases_is_final_only_on_last_chunk():
    store: list = []
    responses = {"hello": [_FakeFrame(b"h1"), _FakeFrame(b"h2")], "world": [_FakeFrame(b"w1")]}
    syn = AzureSpeechSynthesizer(
        api_key="k", region="eastus", voice="v", sample_rate=24000,
        service_factory=_fake_tts_factory(store, responses),
    )

    async def run():
        return [c async for c in syn.synthesize(_text_stream("hello", "world"))]

    chunks = asyncio.run(run())
    assert [c.audio for c in chunks] == [b"h1", b"h2", b"w1"]
    assert [c.is_final for c in chunks] == [False, False, True]  # is_final 只打在末块
    assert all(c.format == AudioFormat(24000, "pcm16", 1) for c in chunks)
    assert store[0].closed == 1  # 每次 synthesize 调用走完整建/关生命周期


def test_synthesize_skips_blank_text_and_empty_audio():
    store: list = []
    responses = {" ": [_FakeFrame(b"nope")], "ok": [_FakeFrame(b""), _FakeFrame(b"tail")]}
    syn = AzureSpeechSynthesizer(
        api_key="k", region="eastus", voice="v", sample_rate=8000,
        service_factory=_fake_tts_factory(store, responses),
    )

    async def run():
        return [c async for c in syn.synthesize(_text_stream(" ", "  ", "ok"))]

    chunks = asyncio.run(run())
    assert [c.audio for c in chunks] == [b"tail"]
    assert chunks[0].is_final is True
    assert [t for t, _ in store[0].calls] == ["ok"]  # 空白文本未触达服务


def test_synthesize_raises_on_error_frame():
    syn = AzureSpeechSynthesizer(
        api_key="k", region="eastus", voice="v", sample_rate=8000,
        service_factory=_fake_tts_factory([], {"boom": [_FakeErrorFrame("quota exceeded")]}),
    )

    async def run():
        return [c async for c in syn.synthesize(_text_stream("boom"))]

    with pytest.raises(RuntimeError, match="quota exceeded"):
        asyncio.run(run())


def test_cancel_aborts_inflight_stream_and_next_turn_works():
    store: list = []
    syn = AzureSpeechSynthesizer(
        api_key="k", region="eastus", voice="v", sample_rate=24000,
        service_factory=_fake_tts_factory(store),
    )

    async def run():
        stream = syn.synthesize(_text_stream("a", "b", "c"))
        first = await stream.__anext__()
        await syn.cancel()
        rest = [c async for c in stream]  # 代际不匹配：不再产出
        # cancel 后下一回合照常（新一代合成）
        new = [c async for c in syn.synthesize(_text_stream("d"))]
        return first, rest, new

    first, rest, new = asyncio.run(run())
    assert first.audio == b"a"
    assert rest == []
    assert [c.audio for c in new] == [b"d"]
    assert store[0].closed == 1 and store[1].closed == 1


def test_empty_text_stream_yields_nothing():
    syn = AzureSpeechSynthesizer(
        api_key="k", region="eastus", voice="v", sample_rate=24000,
        service_factory=_fake_tts_factory([]),
    )

    async def run():
        return [c async for c in syn.synthesize(_text_stream())]

    assert asyncio.run(run()) == []


# --- registry 注册名 + §11 映射（make_*_factory 缝） -------------------------


def test_register_registers_azure_speech_for_stt_and_tts():
    reg = ProviderRegistry()
    register(reg, api_key="k", tts_sample_rate=16000)
    cfg = _config()
    stt = reg.create(ProviderKind.STT, cfg)
    tts = reg.create(ProviderKind.TTS, cfg)
    assert isinstance(stt, SpeechRecognizer) and isinstance(tts, SpeechSynthesizer)
    # 构造即完成 §11 映射、且不触碰 azure SDK（服务在 start/synthesize 才惰性建）——
    # 本测试在无 azure SDK 的环境通过即证明懒加载成立


def test_registered_factory_reads_speech_config_loudly():
    reg = ProviderRegistry()
    register(reg, api_key="k", tts_sample_rate=16000)
    with pytest.raises(ValueError, match="STT_LOCALE"):
        reg.create(ProviderKind.STT, _config(stt_locale=""))
    with pytest.raises(ValueError, match="SPEECH_REGION"):
        reg.create(ProviderKind.STT, _config(speech_region="", speech_endpoint=None))
    with pytest.raises(ValueError, match="TTS_VOICE"):
        reg.create(ProviderKind.TTS, _config(tts_voice=""))


def test_make_factories_map_config_into_service_kwargs():
    stt_store: list = []
    tts_store: list = []
    stt_factory = make_stt_factory(api_key="sekrit", service_factory=_fake_stt_factory(stt_store))
    tts_factory = make_tts_factory(
        api_key="sekrit", sample_rate=16000, service_factory=_fake_tts_factory(tts_store)
    )
    cfg = _config(speech_region="westus2", speech_endpoint="https://pe.test",
                  stt_locale="ja-JP", tts_voice="ja-JP-NanamiNeural")

    async def run():
        rec = stt_factory(cfg)
        await rec.start(language="", input_format=AudioFormat(16000, "pcm16"))
        await rec.stop()
        syn = tts_factory(cfg)

        async def one():
            return [c async for c in syn.synthesize(_text_stream("hi"))]

        return await one()

    asyncio.run(run())
    stt_kwargs = dict(stt_store[0].kwargs)
    stt_kwargs.pop("event_sink")  # 回调对象逐次绑定，单独断言存在即可
    assert stt_kwargs == {
        "api_key": "sekrit", "region": "westus2", "private_endpoint": "https://pe.test",
        "language": "ja-JP", "sample_rate": 16000,
    }
    assert callable(stt_store[0].kwargs["event_sink"])
    assert tts_store[0].kwargs["api_key"] == "sekrit"
    assert tts_store[0].kwargs["region"] == "westus2"
    assert tts_store[0].kwargs["private_endpoint"] == "https://pe.test"
    assert tts_store[0].kwargs["voice"] == "ja-JP-NanamiNeural"
    assert tts_store[0].kwargs["sample_rate"] == 16000
