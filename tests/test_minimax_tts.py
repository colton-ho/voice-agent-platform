"""U-101（G-2 裁决后条件件）— providers/speech/minimax_tts.py 测试（fixture 驱动，零网络）。

覆盖三层：
1. 语义迁移面：resolve_minimax_voice / resolve_model / MINIMAX_VOICES 逐字表 +
   与 bot.py 遗留副本的逐输入等价（防漂移钉）。
2. 线协议面：经 _default_service_factory + 伪 aiohttp 传输驱动**真实**迁入的
   run_tts（SimpleMiniMaxTTSService 语义）——payload 形状、GroupId 修复、
   base_resp 报错、hex 重切、返/(faan1) 修正、SSE 尾块不解析quirk、错误三段。
3. FC-4 行为面 + registry：is_final 前瞻、cancel 代际、minimax_tts 与
   azure_speech 并存注册、遗留名 minimax（含大小写变体）永拒。
"""
import asyncio
import importlib
import json
import sys

import pytest

from config_contract import load_config
from providers.base import AudioFormat, SpeechRecognizer, SpeechSynthesizer
from providers.registry import ProviderKind, ProviderRegistry, UnsupportedProviderError
from providers.speech import minimax_tts
from providers.speech.azure_speech import register as register_azure
from providers.speech.minimax_tts import (
    DEFAULT_MINIMAX_MODEL,
    DEFAULT_MINIMAX_VOICE,
    DEFAULT_MINIMAX_VOICE_BY_LANG,
    MINIMAX_MODELS,
    MINIMAX_VOICES,
    MiniMaxSpeechSynthesizer,
    make_tts_factory,
    register,
    resolve_minimax_voice,
    resolve_model,
)

BASE_ENV = {
    "APP_ENV": "test",
    "AZURE_LOCATION": "test-region",
    "PUBLIC_CONTROL_BASE_URL": "https://example.test",
    "PUBLIC_MEDIA_BASE_URL": "wss://example.test",
    "TELEPHONY_PROVIDER": "test_telephony_stub",
    "VOICE_ENGINE": "cascade",
    "STT_PROVIDER": "azure_speech",
    "TTS_PROVIDER": "minimax_tts",
    "LLM_PROVIDER": "test_llm_stub",
    "LLM_ENDPOINT": "https://example.test/llm",
    "LLM_DEPLOYMENT": "test-deployment",
    "SPEECH_REGION": "eastus",
    "STT_LOCALE": "zh-CN",
    "TTS_VOICE": "Cantonese_GentleLady",
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
    import dataclasses

    cfg = load_config(dict(BASE_ENV))
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


def _text_stream(*items):
    async def gen():
        for item in items:
            yield item

    return gen()


# --- 1. 语义迁移面 ----------------------------------------------------------


def test_resolve_voice_known_key_wins_over_lang_fallback():
    # 已知 key 直接赢（bot.py:938-939），per-lang 回退只在 key 未命中时触发
    assert resolve_minimax_voice("Cantonese_GentleLady", "en-SG") == (
        "Cantonese_GentleLady",
        "Chinese,Yue",
    )
    assert resolve_minimax_voice("Japanese_GentleButler", None) == (
        "Japanese_GentleButler",
        "Japanese",
    )


def test_resolve_voice_per_lang_fallback_then_default():
    assert resolve_minimax_voice("no-such-voice", "en-SG") == (
        "mm_singlish_female_5_ivc_v3",
        "English",
    )
    assert resolve_minimax_voice("no-such-voice", "zh-HK") == (
        DEFAULT_MINIMAX_VOICE,
        "Chinese,Yue",
    )
    assert resolve_minimax_voice(None, None) == (DEFAULT_MINIMAX_VOICE, "Chinese,Yue")


def test_resolve_voice_never_crash_even_with_empty_table():
    # bot.py:949 兜底：即便默认音色从活表被删（dangling），仍返回常量表 boost
    assert resolve_minimax_voice("x", "en-SG", voices={}) == (DEFAULT_MINIMAX_VOICE, "Chinese,Yue")


def test_resolve_voice_reads_injected_live_table():
    live = {"custom_ivc": {"boost": "English"}}
    assert resolve_minimax_voice("custom_ivc", "zh-HK", voices=live) == ("custom_ivc", "English")
    # 活表里没有 → 走默认链（默认音色也不在活表 → 常量兜底）
    assert resolve_minimax_voice("Cantonese_GentleLady", None, voices=live) == (
        DEFAULT_MINIMAX_VOICE,
        "Chinese,Yue",
    )


def test_resolve_model_falls_back_to_default():
    assert resolve_model("speech-2.8-hd") == "speech-2.8-hd"
    assert resolve_model("bogus") == DEFAULT_MINIMAX_MODEL == "speech-2.8-turbo"
    assert resolve_model(None) == DEFAULT_MINIMAX_MODEL
    assert set(MINIMAX_MODELS) == {"speech-2.8-turbo", "speech-2.8-hd"}


def test_default_model_env_override(monkeypatch):
    monkeypatch.setenv("MINIMAX_MODEL_DEFAULT", "speech-2.8-hd")
    try:
        importlib.reload(minimax_tts)
        assert minimax_tts.DEFAULT_MINIMAX_MODEL == "speech-2.8-hd"
    finally:
        monkeypatch.delenv("MINIMAX_MODEL_DEFAULT", raising=False)
        importlib.reload(minimax_tts)  # 还原 import 期语义，勿污染后续用例


def test_voice_tables_and_resolution_match_legacy_bot(monkeypatch, tmp_path):
    # 防漂移钉：本模块是迁移后的 TTS 语义源，bot.py:501-604/:933-949 是遗留副本
    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("MCP_CFG_PATH_OVERRIDE", str(tmp_path / "mcp.json"))
    for name in ("bot", "runtime_config", "demo_loader", "mcp_config", "user_store"):
        sys.modules.pop(name, None)
    bot = importlib.import_module("bot")

    assert MINIMAX_VOICES == bot.MINIMAX_VOICES
    assert DEFAULT_MINIMAX_VOICE == bot.DEFAULT_MINIMAX_VOICE
    assert DEFAULT_MINIMAX_VOICE_BY_LANG == bot.DEFAULT_MINIMAX_VOICE_BY_LANG
    assert MINIMAX_MODELS == bot.MINIMAX_MODELS
    for key in ("Cantonese_GentleLady", "mm_singlish_female_5_ivc_v3", "", "junk", None):
        for lang in (None, "en-SG", "zh-HK", "ja-JP", "xx"):
            assert resolve_minimax_voice(key, lang) == bot._resolve_minimax_voice(key, lang)


# --- 2. 线协议面（真实迁入 run_tts × 伪 aiohttp 传输） -----------------------


class _FakeContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, n):
        for c in self._chunks:
            if isinstance(c, Exception):
                raise c
            yield c


class _FakeResponse:
    def __init__(self, *, status=200, body="", chunks=()):
        self.status = status
        self._body = body
        self.content = _FakeContent(chunks)

    async def text(self):
        return self._body


class _FakeResponseCM:
    def __init__(self, response):
        self._r = response

    async def __aenter__(self):
        return self._r

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, *, response=None, error=None):
        self.calls: list[dict] = []
        self._response = response
        self._error = error

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self._error is not None:
            raise self._error
        return _FakeResponseCM(self._response)


def _sse(*objs):
    return b"".join(b"data: " + json.dumps(o).encode() + b"\n\n" for o in objs)


def _audio_block(pcm: bytes):
    return {"data": {"audio": pcm.hex()}, "base_resp": {"status_code": 0, "status_msg": "ok"}}


_EXTRA_BLOCK = {"extra_info": {"characters": 0}, "base_resp": {"status_code": 0, "status_msg": "ok"}}


def _wire_factory(store, session):
    def factory(**kwargs):
        svc = minimax_tts._default_service_factory(**{**kwargs, "session": session})
        store.append(svc)
        return svc

    return factory


def _wire_synth(store, session, **kw):
    kw.setdefault("api_key", "mm-key")
    kw.setdefault("voice", "Cantonese_GentleLady")
    kw.setdefault("sample_rate", 24000)
    return MiniMaxSpeechSynthesizer(service_factory=_wire_factory(store, session), **kw)


def test_wire_payload_shape_and_yue_pronunciation_fix():
    session = _FakeSession(response=_FakeResponse(chunks=[_sse(_audio_block(b"\x01\x02"), _EXTRA_BLOCK)]))
    syn = _wire_synth([], session)

    async def run():
        return [c async for c in syn.synthesize(_text_stream("你好"))]

    chunks = asyncio.run(run())
    assert [c.audio for c in chunks] == [b"\x01\x02"] and chunks[0].is_final is True

    call = session.calls[0]
    # 空 GroupId → 剥掉 ?GroupId= 查询串（bot.py:626-629 修复）
    assert call["url"] == "https://api.minimax.chat/v1/t2a_v2"
    assert call["headers"]["Authorization"] == "Bearer mm-key"
    assert call["json"] == {
        "stream": True,
        "model": "speech-2.8-turbo",
        "text": "你好",
        "voice_setting": {"voice_id": "Cantonese_GentleLady", "speed": 1.0, "vol": 1.0, "pitch": 0},
        "audio_setting": {"bitrate": 128000, "format": "pcm", "channel": 1, "sample_rate": 24000},
        "language_boost": "Chinese,Yue",
        "pronunciation_dict": {"tone": ["返/(faan1)"]},
    }


def test_wire_non_yue_boost_has_no_pronunciation_dict_and_group_id_kept():
    session = _FakeSession(response=_FakeResponse(chunks=[_sse(_audio_block(b"\x03"), _EXTRA_BLOCK)]))
    syn = _wire_synth([], session, voice="Japanese_GentleButler", group_id="grp123")

    async def run():
        return [c async for c in syn.synthesize(_text_stream("hi"))]

    asyncio.run(run())
    call = session.calls[0]
    assert call["url"] == "https://api.minimax.chat/v1/t2a_v2?GroupId=grp123"
    payload = call["json"]
    assert payload["language_boost"] == "Japanese"
    assert "pronunciation_dict" not in payload


def test_wire_key_comes_from_parameter_not_env(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "env-key-should-not-be-read")
    session = _FakeSession(response=_FakeResponse(chunks=[_sse(_audio_block(b"\x04"), _EXTRA_BLOCK)]))
    syn = _wire_synth([], session, api_key="explicit-key")

    async def run():
        return [c async for c in syn.synthesize(_text_stream("x"))]

    asyncio.run(run())
    assert session.calls[0]["headers"]["Authorization"] == "Bearer explicit-key"


def test_wire_hex_pcm_resliced_to_wheel_chunk_size():
    # chunk_size 是 wheel 属性（tts_service.py:417 = sample_rate×0.5s×2 字节）——
    # 24kHz 即 24000 字节/块；bot.py 注释「20ms/960」与 1.4.0 不符，行为以现状为准
    pcm = bytes(range(256)) * 118  # 30208 字节 → 24000 + 6208
    session = _FakeSession(response=_FakeResponse(chunks=[_sse(_audio_block(pcm), _EXTRA_BLOCK)]))
    syn = _wire_synth([], session)

    async def run():
        return [c async for c in syn.synthesize(_text_stream("x"))]

    chunks = asyncio.run(run())
    assert [len(c.audio) for c in chunks] == [24000, 6208]
    assert [c.is_final for c in chunks] == [False, True]
    assert all(c.format == AudioFormat(24000, "pcm16", 1) for c in chunks)


def test_wire_last_incomplete_sse_block_is_not_parsed():
    # 既有 quirk（保真保留）：块解析只在遇到下一个 data: 标记时闭合——
    # 流末未闭合的块不解析。单块流 = 零音频；补一个尾块标记 = 正常出声。
    session = _FakeSession(response=_FakeResponse(chunks=[_sse(_audio_block(b"\x07"))]))
    syn = _wire_synth([], session)

    async def run():
        return [c async for c in syn.synthesize(_text_stream("x"))]

    assert asyncio.run(run()) == []

    session2 = _FakeSession(response=_FakeResponse(chunks=[_sse(_audio_block(b"\x07"), _EXTRA_BLOCK)]))

    async def run2():
        syn2 = _wire_synth([], session2)
        return [c async for c in syn2.synthesize(_text_stream("x"))]

    assert [c.audio for c in asyncio.run(run2())] == [b"\x07"]


def test_wire_malformed_json_and_hex_blocks_are_skipped():
    raw = (
        b"data: {oops}\n\n"
        + _sse({"data": {"audio": "zz"}, "base_resp": {"status_code": 0}})
        + _sse(_audio_block(b"\x03\x04"), _EXTRA_BLOCK)
    )
    session = _FakeSession(response=_FakeResponse(chunks=[raw]))
    syn = _wire_synth([], session)

    async def run():
        return [c async for c in syn.synthesize(_text_stream("x"))]

    assert [c.audio for c in asyncio.run(run())] == [b"\x03\x04"]


def test_wire_base_resp_error_is_surfaced():
    err = {"base_resp": {"status_code": 2045, "status_msg": "invalid request"}}
    session = _FakeSession(response=_FakeResponse(chunks=[_sse(err, _EXTRA_BLOCK)]))
    syn = _wire_synth([], session)

    async def run():
        return [c async for c in syn.synthesize(_text_stream("x"))]

    with pytest.raises(RuntimeError, match="MiniMax error: invalid request"):
        asyncio.run(run())


def test_wire_error_paths_request_http_and_stream():
    # 请求失败（bot.py:666-668）
    s1 = _FakeSession(error=RuntimeError("conn refused"))
    # HTTP != 200 带 body[:200]（bot.py:673-676）
    s2 = _FakeSession(response=_FakeResponse(status=401, body="unauthorized"))
    # 流中断（bot.py:734-736）
    s3 = _FakeSession(response=_FakeResponse(chunks=[RuntimeError("net down")]))

    for session, match in (
        (s1, "MiniMax request failed: conn refused"),
        (s2, "MiniMax HTTP 401: unauthorized"),
        (s3, "MiniMax stream failed: net down"),
    ):
        syn = _wire_synth([], session)

        async def run(syn=syn):
            return [c async for c in syn.synthesize(_text_stream("x"))]

        with pytest.raises(RuntimeError, match=match.replace("(", r"\(").replace(")", r"\)")):
            asyncio.run(run())


# --- 3. FC-4 行为面（伪服务工厂）+ registry ---------------------------------


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


def test_fc4_is_final_only_on_last_and_full_lifecycle():
    store: list = []
    responses = {"hello": [_FakeFrame(b"h1"), _FakeFrame(b"h2")], "world": [_FakeFrame(b"w1")]}
    syn = MiniMaxSpeechSynthesizer(
        api_key="k", voice="Cantonese_GentleLady", sample_rate=24000,
        service_factory=_fake_tts_factory(store, responses),
    )

    async def run():
        return [c async for c in syn.synthesize(_text_stream("hello", "world"))]

    chunks = asyncio.run(run())
    assert [c.audio for c in chunks] == [b"h1", b"h2", b"w1"]
    assert [c.is_final for c in chunks] == [False, False, True]
    assert all(c.format == AudioFormat(24000, "pcm16", 1) for c in chunks)
    assert store[0].opened == 1 and store[0].closed == 1
    assert all(ctx.startswith("gen") for _, ctx in store[0].calls)


def test_fc4_skips_blank_text_and_empty_audio():
    store: list = []
    responses = {" ": [_FakeFrame(b"nope")], "ok": [_FakeFrame(b""), _FakeFrame(b"tail")]}
    syn = MiniMaxSpeechSynthesizer(
        api_key="k", voice="v", sample_rate=8000,
        service_factory=_fake_tts_factory(store, responses),
    )

    async def run():
        return [c async for c in syn.synthesize(_text_stream(" ", "  ", "ok"))]

    chunks = asyncio.run(run())
    assert [c.audio for c in chunks] == [b"tail"] and chunks[0].is_final is True
    assert [t for t, _ in store[0].calls] == ["ok"]


def test_fc4_error_frame_raises_with_minimax_label():
    syn = MiniMaxSpeechSynthesizer(
        api_key="k", voice="v", sample_rate=8000,
        service_factory=_fake_tts_factory([], {"boom": [_FakeErrorFrame("quota exceeded")]}),
    )

    async def run():
        return [c async for c in syn.synthesize(_text_stream("boom"))]

    with pytest.raises(RuntimeError, match="MiniMax TTS synthesis failed: quota exceeded"):
        asyncio.run(run())


def test_fc4_cancel_aborts_inflight_and_next_turn_works():
    store: list = []
    syn = MiniMaxSpeechSynthesizer(
        api_key="k", voice="v", sample_rate=24000,
        service_factory=_fake_tts_factory(store),
    )

    async def run():
        stream = syn.synthesize(_text_stream("a", "b", "c"))
        first = await stream.__anext__()
        await syn.cancel()
        rest = [c async for c in stream]
        new = [c async for c in syn.synthesize(_text_stream("d"))]
        return first, rest, new

    first, rest, new = asyncio.run(run())
    assert first.audio == b"a" and rest == []
    assert [c.audio for c in new] == [b"d"]
    assert store[0].closed == 1 and store[1].closed == 1


def test_constructor_loud_errors():
    with pytest.raises(ValueError, match="api_key: empty"):
        MiniMaxSpeechSynthesizer(api_key="", voice="v", sample_rate=24000)
    with pytest.raises(ValueError, match="sample_rate"):
        MiniMaxSpeechSynthesizer(api_key="k", voice="v", sample_rate=44100)


def test_constructor_resolves_voice_boost_model_and_voice_lookup():
    store: list = []
    syn = MiniMaxSpeechSynthesizer(
        api_key="k", voice="no-such-voice", sample_rate=24000, lang="en-SG",
        model="speech-2.8-hd",
        voice_lookup=lambda: {"custom": {"boost": "English"}},
        service_factory=_fake_tts_factory(store),
    )

    async def run():
        return [c async for c in syn.synthesize(_text_stream("x"))]

    asyncio.run(run())
    # voice_lookup 活表优先：解析不到才走默认链（此处表里只有 custom → 默认兜底）
    assert store[0].kwargs["voice"] == DEFAULT_MINIMAX_VOICE
    assert store[0].kwargs["language_boost"] == "Chinese,Yue"
    assert store[0].kwargs["model"] == "speech-2.8-hd"

    store2: list = []
    syn2 = MiniMaxSpeechSynthesizer(
        api_key="k", voice="custom", sample_rate=24000,
        voice_lookup=lambda: {"custom": {"boost": "English"}},
        service_factory=_fake_tts_factory(store2),
    )

    async def run2():
        return [c async for c in syn2.synthesize(_text_stream("x"))]

    asyncio.run(run2())
    assert store2[0].kwargs["voice"] == "custom" and store2[0].kwargs["language_boost"] == "English"


def test_register_coexists_with_azure_speech_and_legacy_name_stays_refused(monkeypatch):
    monkeypatch.delenv("MINIMAX_GROUP_ID", raising=False)
    monkeypatch.delenv("MINIMAX_BASE_URL", raising=False)
    reg = ProviderRegistry()
    register_azure(reg, api_key="az-key", tts_sample_rate=24000)
    register(reg, api_key="mm-key", tts_sample_rate=24000)

    cfg = _config()
    stt = reg.create(ProviderKind.STT, cfg)
    assert isinstance(stt, SpeechRecognizer)

    store: list = []
    reg2 = ProviderRegistry()
    register_azure(reg2, api_key="az-key", tts_sample_rate=24000)
    register(reg2, api_key="mm-key", tts_sample_rate=24000,
             group_id="grp-env", service_factory=_fake_tts_factory(store))
    mm = reg2.create(ProviderKind.TTS, cfg)
    # 类名断言而非 isinstance(导入类)：前面的 env 用例 reload 过模块，类对象会换新
    assert type(mm).__name__ == "MiniMaxSpeechSynthesizer" and isinstance(mm, SpeechSynthesizer)
    az = reg2.create(ProviderKind.TTS, _config(tts_provider="azure_speech"))
    assert type(az).__name__ == "AzureSpeechSynthesizer"

    for legacy in ("minimax", "MiniMax", "MINIMAX"):
        with pytest.raises(UnsupportedProviderError):
            reg2.create(ProviderKind.TTS, _config(tts_provider=legacy))


def test_make_tts_factory_maps_config_env_and_overrides(monkeypatch):
    monkeypatch.setenv("MINIMAX_GROUP_ID", "grp-env")
    monkeypatch.setenv("MINIMAX_BASE_URL", "https://mm.example/v1/t2a_v2")
    monkeypatch.setenv("MINIMAX_API_KEY", "env-key-ignored")

    store: list = []
    factory = make_tts_factory(api_key="explicit", tts_sample_rate=16000,
                               service_factory=_fake_tts_factory(store))
    syn = factory(_config(tts_voice="Japanese_GentleButler"))

    async def run():
        return [c async for c in syn.synthesize(_text_stream("x"))]

    asyncio.run(run())
    kw = store[0].kwargs
    assert kw["api_key"] == "explicit"
    assert kw["group_id"] == "grp-env" and kw["base_url"] == "https://mm.example/v1/t2a_v2"
    assert kw["voice"] == "Japanese_GentleButler" and kw["language_boost"] == "Japanese"
    assert kw["model"] == DEFAULT_MINIMAX_MODEL and kw["sample_rate"] == 16000

    store2: list = []
    factory2 = make_tts_factory(api_key="explicit", tts_sample_rate=24000,
                                group_id="explicit-grp", base_url="https://x.example/t2a",
                                service_factory=_fake_tts_factory(store2))
    syn2 = factory2(_config())

    async def run2():
        return [c async for c in syn2.synthesize(_text_stream("x"))]

    asyncio.run(run2())
    kw2 = store2[0].kwargs
    assert kw2["group_id"] == "explicit-grp" and kw2["base_url"] == "https://x.example/t2a"
