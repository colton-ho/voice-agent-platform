"""U-004 — providers/registry.py 单测（§3.4 冻结语义）。

覆盖：显式选择成功、缺失/空选择点名变量、未注册名列出名单、AWS 遗留名永不解析、
cascade 组合 FC-2/3/4、非 cascade 解析 REALTIME 且未注册即报、重复注册拒绝、
工厂产物契约检查、零默认零回退。
"""
import dataclasses

import pytest

from config_contract import load_config
from providers.base import (LanguageModel, RealtimeVoiceEngine, SpeechRecognizer,
                            SpeechSynthesizer, TelephonyAdapter)
from providers.registry import (MissingSelectionError, ProviderKind, ProviderRegistry,
                                UnsupportedProviderError)

# 合法基线（走 U-003 loader，TELEPHONY_PROVIDER 非 twilio 以免条件必填干扰）
BASE_ENV = {
    "APP_ENV": "test",
    "AZURE_LOCATION": "test-region",
    "PUBLIC_CONTROL_BASE_URL": "https://example.test",
    "PUBLIC_MEDIA_BASE_URL": "wss://example.test",
    "TELEPHONY_PROVIDER": "test_telephony_stub",
    "VOICE_ENGINE": "cascade",
    "STT_PROVIDER": "test_stt_stub",
    "TTS_PROVIDER": "test_tts_stub",
    "LLM_PROVIDER": "test_llm_stub",
    "LLM_ENDPOINT": "https://example.test/llm",
    "LLM_DEPLOYMENT": "test-deployment",
    "SPEECH_REGION": "test-region",
    "STT_LOCALE": "en-US",
    "TTS_VOICE": "test-voice",
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
    # replace 可造出 load_config 永不产出的退化配置（如空选择）——registry 须独立设防
    cfg = load_config(dict(BASE_ENV))
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


# --- 最小 stub：每个契约一个，仅用于注册表解析/类型检查 -------------------------


class _StubTelephony(TelephonyAdapter):
    supports_transfer = False

    def validate_webhook(self, *, url, params, signature, validator): return True
    def start_stream(self, *, stream_url, caller): return "<xml/>"
    def decode_incoming_media(self, *, payload): return b""
    def encode_outgoing_media(self, *, pcm): return ""
    def clear_playback(self): return {}
    def transfer(self, *, destination): return ""
    def hang_up(self): return {}


class _StubSTT(SpeechRecognizer):
    supports_language_detection = False

    async def start(self, *, language, input_format): ...
    async def push_audio(self, pcm): ...
    def events(self): ...
    async def stop(self): ...


class _StubLLM(LanguageModel):
    supports_tool_calls = True

    def stream(self, *, messages, tools=None): ...
    async def cancel(self): ...
    def usage(self): ...


class _StubTTS(SpeechSynthesizer):
    def synthesize(self, text_stream): ...
    async def cancel(self): ...
    def output_format(self): ...


class _StubRealtime(RealtimeVoiceEngine):
    async def start(self, *, context): ...
    async def push_audio(self, pcm): ...
    def events(self): ...


# kind -> (选择变量, 测试专用注册名, stub 类)；REALTIME 单独走 VOICE_ENGINE 用例
KINDS = {
    ProviderKind.TELEPHONY: ("TELEPHONY_PROVIDER", "test_telephony_stub", _StubTelephony),
    ProviderKind.STT: ("STT_PROVIDER", "test_stt_stub", _StubSTT),
    ProviderKind.TTS: ("TTS_PROVIDER", "test_tts_stub", _StubTTS),
    ProviderKind.LLM: ("LLM_PROVIDER", "test_llm_stub", _StubLLM),
}

# §3.4 冻结四名 + team-lead 定向补充（transcribe/minimax）——永不解析
LEGACY_CASES = [
    (ProviderKind.LLM, "llm_provider", "bedrock"),
    (ProviderKind.STT, "stt_provider", "aws_transcribe"),
    (ProviderKind.STT, "stt_provider", "transcribe"),
    (ProviderKind.TTS, "tts_provider", "polly"),
    (ProviderKind.TTS, "tts_provider", "minimax"),
    (ProviderKind.REALTIME, "voice_engine", "nova-sonic"),
]


# --- 验收 1：显式选择成功（测试名注册 stub） ---------------------------------


@pytest.mark.parametrize("kind", list(KINDS))
def test_explicit_selection_success(kind):
    variable, name, stub_cls = KINDS[kind]
    calls = []
    reg = ProviderRegistry()
    reg.register(kind, name, lambda cfg: (calls.append(cfg), stub_cls())[1])
    cfg = _config()
    out = reg.create(kind, cfg)
    assert isinstance(out, stub_cls)
    assert calls == [cfg]                     # 显式选择：工厂恰好一次，拿到同一 AppConfig


# --- 验收 1：缺失/空选择 -> MissingSelectionError 点名配置变量 -----------------


@pytest.mark.parametrize("kind,variable,field", [
    (ProviderKind.TELEPHONY, "TELEPHONY_PROVIDER", "telephony_provider"),
    (ProviderKind.STT, "STT_PROVIDER", "stt_provider"),
    (ProviderKind.TTS, "TTS_PROVIDER", "tts_provider"),
    (ProviderKind.LLM, "LLM_PROVIDER", "llm_provider"),
    (ProviderKind.REALTIME, "VOICE_ENGINE", "voice_engine"),
])
@pytest.mark.parametrize("blank", ["", "   "])
def test_missing_selection_names_variable(kind, variable, field, blank):
    reg = ProviderRegistry()                  # 零注册：证明缺失检查先于查找，不存在默认
    with pytest.raises(MissingSelectionError) as excinfo:
        reg.create(kind, _config(**{field: blank}))
    assert excinfo.value.variable == variable
    assert variable in str(excinfo.value)


# --- 验收 2：未注册名 -> UnsupportedProviderError 列出已注册名单 ----------------


def test_unregistered_configured_name_lists_registered_names():
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "test_stt_stub", lambda cfg: _StubSTT())
    reg.register(ProviderKind.STT, "test_stt_alt", lambda cfg: _StubSTT())
    with pytest.raises(UnsupportedProviderError) as excinfo:
        reg.create(ProviderKind.STT, _config(stt_provider="azure_speech"))
    msg = str(excinfo.value)
    assert "azure_speech" in msg
    assert "test_stt_stub" in msg and "test_stt_alt" in msg
    assert excinfo.value.registered == ("test_stt_alt", "test_stt_stub")


# --- 验收 2：AWS 遗留名永不解析、永不回退（R-C-002） --------------------------


@pytest.mark.parametrize("kind,field,name", LEGACY_CASES)
def test_aws_era_names_never_resolve(kind, field, name):
    reg = ProviderRegistry()
    with pytest.raises(UnsupportedProviderError) as excinfo:
        reg.create(kind, _config(**{field: name}))
    assert name in str(excinfo.value)


@pytest.mark.parametrize("kind,field,name", LEGACY_CASES)
def test_aws_era_names_never_fall_back_to_registered(kind, field, name):
    # 即使该 kind 已有合法注册，遗留名也不得落到任何工厂（无回退）
    stub_cls = KINDS.get(kind, (None, None, _StubRealtime))[2]
    called = []
    reg = ProviderRegistry()
    reg.register(kind, "test_ok_name", lambda cfg: (called.append(1), stub_cls())[1])
    with pytest.raises(UnsupportedProviderError):
        reg.create(kind, _config(**{field: name}))
    assert called == []


@pytest.mark.parametrize("name", ["bedrock", "aws_transcribe", "transcribe",
                                  "polly", "nova-sonic", "minimax"])
def test_register_rejects_aws_era_names(name):
    with pytest.raises(UnsupportedProviderError):
        ProviderRegistry().register(ProviderKind.TTS, name, lambda cfg: _StubTTS())


@pytest.mark.parametrize("name", ["Bedrock", "Polly", "NOVA-SONIC"])
def test_register_rejects_aws_era_names_case_insensitively(name):
    # F-05：denylist 按 casefold 匹配——大小写变体同样不得注册
    with pytest.raises(UnsupportedProviderError):
        ProviderRegistry().register(ProviderKind.TTS, name, lambda cfg: _StubTTS())


def test_denylist_case_insensitive_never_resolves():
    # F-05：STT_PROVIDER=Bedrock 这类大小写变体永不解析、永不回退
    called = []
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "test_stt_stub", lambda cfg: (called.append(1), _StubSTT())[1])
    with pytest.raises(UnsupportedProviderError):
        reg.create(ProviderKind.STT, _config(stt_provider="Bedrock"))
    assert called == []


@pytest.mark.parametrize("name", ["", "   ", "\t"])
def test_register_rejects_blank_name(name):
    # F-06：空/纯空白名字不得进注册表（否则 (kind, "") 形同暗桩默认名）
    with pytest.raises(ValueError):
        ProviderRegistry().register(ProviderKind.STT, name, lambda cfg: _StubSTT())


# --- 验收 3：VOICE_ENGINE=cascade 组合 FC-2/3/4；其他值解析 REALTIME -----------


def test_voice_engine_cascade_composes_stt_llm_tts_via_factories():
    order = []
    reg = ProviderRegistry()

    def make(tag, stub_cls):
        return lambda cfg: (order.append(tag), stub_cls())[1]

    reg.register(ProviderKind.STT, "test_stt_stub", make("stt", _StubSTT))
    reg.register(ProviderKind.LLM, "test_llm_stub", make("llm", _StubLLM))
    reg.register(ProviderKind.TTS, "test_tts_stub", make("tts", _StubTTS))
    stt, llm, tts = reg.create(ProviderKind.REALTIME, _config(voice_engine="cascade"))
    assert isinstance(stt, SpeechRecognizer)
    assert isinstance(llm, LanguageModel)
    assert isinstance(tts, SpeechSynthesizer)
    assert order == ["stt", "llm", "tts"]     # FC-2+FC-3+FC-4（§3.4），各工厂恰好一次


def test_voice_engine_cascade_composes_strictly_no_fallback():
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "test_stt_stub", lambda cfg: _StubSTT())
    reg.register(ProviderKind.LLM, "test_llm_stub", lambda cfg: _StubLLM())
    with pytest.raises(UnsupportedProviderError):   # TTS 未注册：组合缺一即报，不将就
        reg.create(ProviderKind.REALTIME, _config(voice_engine="cascade"))
    with pytest.raises(MissingSelectionError) as excinfo:
        reg.create(ProviderKind.REALTIME,
                   _config(voice_engine="cascade", tts_provider=""))
    assert excinfo.value.variable == "TTS_PROVIDER"


def test_voice_engine_other_value_resolves_realtime_kind():
    reg = ProviderRegistry()
    reg.register(ProviderKind.REALTIME, "test_realtime_stub", lambda cfg: _StubRealtime())
    out = reg.create(ProviderKind.REALTIME, _config(voice_engine="test_realtime_stub"))
    assert isinstance(out, RealtimeVoiceEngine)
    assert not isinstance(out, tuple)         # 非 cascade 不返回组合三元组


def test_voice_engine_unregistered_fails_loudly():
    reg = ProviderRegistry()                  # Phase A 不注册任何 REALTIME 引擎
    with pytest.raises(UnsupportedProviderError) as excinfo:
        reg.create(ProviderKind.REALTIME, _config(voice_engine="voice-live"))
    assert "VOICE_ENGINE" in str(excinfo.value)


# --- 验收 4：显式注册（重复拒绝）+ 产物过契约 ABC -----------------------------


def test_duplicate_registration_raises():
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "test_stt_stub", lambda cfg: _StubSTT())
    with pytest.raises(ValueError):
        reg.register(ProviderKind.STT, "test_stt_stub", lambda cfg: _StubSTT())
    reg.register(ProviderKind.TTS, "test_stt_stub", lambda cfg: _StubTTS())  # 跨 kind 同名可
    reg.register(ProviderKind.STT, "test_stt_alt", lambda cfg: _StubSTT())   # 同 kind 异名可


class _DuckSTT:                                # 方法名对得上但非契约子类：必须被拒
    async def start(self, *, language, input_format): ...
    async def push_audio(self, pcm): ...
    def events(self): ...
    async def stop(self): ...


def test_factory_result_checked_against_contract_abc():
    reg = ProviderRegistry()
    reg.register(ProviderKind.STT, "test_stt_stub", lambda cfg: _DuckSTT())
    with pytest.raises(TypeError) as excinfo:
        reg.create(ProviderKind.STT, _config())
    assert "SpeechRecognizer" in str(excinfo.value)


# --- 无默认、无回退（R-C-002） ------------------------------------------------


def test_no_default_and_no_fallback():
    reg = ProviderRegistry()                  # 只注册 STT
    reg.register(ProviderKind.STT, "test_stt_stub", lambda cfg: _StubSTT())
    # 其他 kind 即便拿到"别家合法名"也不解析——绝不落到默认/其他注册
    for kind, field in ((ProviderKind.TELEPHONY, "telephony_provider"),
                        (ProviderKind.TTS, "tts_provider"),
                        (ProviderKind.LLM, "llm_provider"),
                        (ProviderKind.REALTIME, "voice_engine")):
        with pytest.raises(UnsupportedProviderError):
            reg.create(kind, _config(**{field: "test_stt_stub"}))
    # 选择缺失只报缺失，绝不替换成任何默认名
    with pytest.raises(MissingSelectionError):
        reg.create(ProviderKind.STT, _config(stt_provider=""))
