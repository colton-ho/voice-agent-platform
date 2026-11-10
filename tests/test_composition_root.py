"""U-103 — bot.py 组合根接线测试（build_cascade_engine）。

钉三件事：load_config 在组合根成为真启动校验（违规一次性全报）；registry 显式
选型组出 FC-2/3/4 三元组（含 §11→FC-0 采样率映射、minimax voice_lookup 注入缝）；
遗留名/缺失选型零默认零回退（R-C-002）。构造期无网络（api_key 走工厂注入缝，
CR-C1 的 Key Vault 接管点），凭据一律 dummy。
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_contract import ConfigError  # noqa: E402
from providers.base import (  # noqa: E402
    AudioFormat,
    LanguageModel,
    SpeechRecognizer,
    SpeechSynthesizer,
)
from providers.registry import UnsupportedProviderError  # noqa: E402

import bot  # noqa: E402

BASE_ENV = {
    "APP_ENV": "test",
    "AZURE_LOCATION": "eastus",
    "PUBLIC_CONTROL_BASE_URL": "https://example.test",
    "PUBLIC_MEDIA_BASE_URL": "wss://example.test",
    "TELEPHONY_PROVIDER": "test_telephony_stub",
    "VOICE_ENGINE": "cascade",
    "STT_PROVIDER": "azure_speech",
    "TTS_PROVIDER": "azure_speech",
    "LLM_PROVIDER": "azure_openai",
    "LLM_ENDPOINT": "https://example.test/llm",
    "LLM_DEPLOYMENT": "gpt-test",
    "LLM_API_VERSION": "2024-09-01-preview",
    "SPEECH_REGION": "eastus",
    "STT_LOCALE": "zh-HK",
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

KEYS = {"speech_api_key": "k-speech", "llm_api_key": "k-llm", "minimax_api_key": "k-mm"}


def _build(**env_overrides):
    env = dict(BASE_ENV)
    env.update(env_overrides)
    return bot.build_cascade_engine(env, **KEYS)


# =====================================================================
# load_config 成为真启动校验（R-C-001：违规一次性全报，指名变量+含义）
# =====================================================================

def test_load_config_is_startup_validation():
    with pytest.raises(ConfigError) as exc_info:
        bot.build_cascade_engine({}, **KEYS)
    message = str(exc_info.value)
    # 一次性列出全部违规：多个 §11 变量同时在报，而不是逐个试错
    assert "VOICE_ENGINE" in message and "STT_PROVIDER" in message
    assert "LLM_ENDPOINT" in message and "COSMOS_ENDPOINT" in message
    assert "deployment environment name" in message  # 违规带占位符含义


# =====================================================================
# registry 显式选型 → FC-2/3/4 三元组（§11→FC-0 映射、注入缝）
# =====================================================================

def test_registry_selected_adapters_wired():
    engine = _build()
    # 具体类用 __name__ 断言（test_minimax_tts 会 reload 适配器模块，双类对象下
    # isinstance 导入期类必假阴性——既有教训）；契约合规走 providers.base ABC
    assert isinstance(engine._stt, SpeechRecognizer)
    assert type(engine._stt).__name__ == "AzureSpeechRecognizer"
    assert isinstance(engine._llm, LanguageModel)
    assert type(engine._llm).__name__ == "AzureOpenAILLM"
    assert isinstance(engine._tts, SpeechSynthesizer)
    assert type(engine._tts).__name__ == "AzureSpeechSynthesizer"
    # FC-0 音频契约映射：识别 16 kHz（INPUT_SAMPLE_RATE），合成 24 kHz（OUTPUT_SAMPLE_RATE）
    assert engine._input_format == AudioFormat(sample_rate=16000, encoding="pcm16", channels=1)
    assert engine._tts.output_format() == AudioFormat(sample_rate=24000, encoding="pcm16", channels=1)


def test_minimax_tts_selectable_with_voice_lookup_seam():
    engine = _build(TTS_PROVIDER="minimax_tts")
    assert isinstance(engine._tts, SpeechSynthesizer)
    assert type(engine._tts).__name__ == "MiniMaxSpeechSynthesizer"
    # U-101 注入缝：voice_lookup 由组合根接 bot.py voices_for（活表/常量单读路径）
    assert engine._tts._voice_id  # 解析出了音色（未知 key 兜底常量默认，永不抛错）


# =====================================================================
# 零默认零回退（R-C-002）：遗留名永不解析
# =====================================================================

def test_legacy_aws_names_never_resolve():
    with pytest.raises(UnsupportedProviderError):
        _build(STT_PROVIDER="transcribe")
    with pytest.raises(UnsupportedProviderError):
        _build(LLM_PROVIDER="bedrock")
    # nova-sonic 是 REALTIME 遗留名：VOICE_ENGINE 选它同样被拒
    with pytest.raises(UnsupportedProviderError):
        _build(VOICE_ENGINE="nova-sonic")


# =====================================================================
# F-12 — 裁决 B ② 行为钉：PHONE_ENGINE 默认随 DEFAULT_ENGINE
# =====================================================================

def test_phone_engine_default_rides_default_engine(monkeypatch):
    # 默认值是裁决 B ② 的交付物：不钉住的话 DEFAULT_ENGINE 将来再变
    # 会静默带走 phone 默认（reviewer F-12）
    monkeypatch.delenv("PHONE_ENGINE", raising=False)
    assert bot._RUNTIME_FALLBACK["phone"]["engine"] == "pipeline"


# =====================================================================
# #16 A-1/A-2 — MODELS 值重指錨（Foundry 模型名）+ LABELS 顯示名表
# =====================================================================

def test_models_six_key_anchor():
    # 裁决 A-1 失實值原则：struct["model"] 落史数据/UI label/能力位
    # claude-*/gpt-* 匹配都要真实模型名而非部署别名——值表一变就红
    assert bot.MODELS == {
        "nova-2-lite": "gpt-4.1-mini",
        "nova-lite": "gpt-5-mini",
        "nova-micro": "gpt-4.1-nano",
        "claude-haiku-4.5": "claude-haiku-4-5",
        "claude-sonnet-4.5": "claude-sonnet-4-5",
        "claude-sonnet-4.6": "claude-sonnet-4-6",
    }


def test_labels_b9_display_names():
    # B9 簽署显示名表；与 MODELS 逐 key 齐全（label 来源=LABELS 的前提）
    assert set(bot.LABELS) == set(bot.MODELS)
    assert bot.LABELS == {
        "nova-2-lite": "GPT-4.1 mini",
        "nova-lite": "GPT-5 mini",
        "nova-micro": "GPT-4.1 nano",
        "claude-haiku-4.5": "Claude Haiku 4.5",
        "claude-sonnet-4.5": "Claude Sonnet 4.5",
        "claude-sonnet-4.6": "Claude Sonnet 4.6",
    }


# =====================================================================
# #16 A-3 — L1 流式行为钉：FC-3→pipecat 桥接帧序/工具/用量保真
# =====================================================================

def test_fc3_pipecat_bridge_streaming_behavior_pin(monkeypatch):
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext

    from providers.base import ModelEvent, ToolRequest, UsageReport

    tool_schema = ToolsSchema(standard_tools=[FunctionSchema(
        name="end_call", description="End the call",
        properties={"reason": {"type": "string"}}, required=["reason"],
    )])
    events = [
        ModelEvent(kind="delta", text="he"),
        ModelEvent(kind="delta", text="llo"),
        ModelEvent(kind="tool_request", tool_request=ToolRequest(
            id="t1", name="end_call", arguments={"reason": "bye"})),
        ModelEvent(kind="usage", usage=UsageReport(
            provider="azure_openai", model="gpt-4.1-mini",
            input_units=7, output_units=3, raw={})),
        ModelEvent(kind="end"),
    ]

    class _FakeFC3:
        def __init__(self):
            self.stream_kwargs = None

        def stream(self, *, messages, tools=None, max_tokens=None):
            self.stream_kwargs = {"messages": messages, "tools": tools, "max_tokens": max_tokens}

            async def _gen():
                for ev in events:
                    yield ev

            return _gen()

    fc3 = _FakeFC3()
    bridge = bot._FC3PipecatLLM(llm=fc3, system_instruction="SYS", max_tokens=256)

    frames, calls, usage = [], [], []

    async def _capture_frame(frame, direction=None):
        frames.append(frame)

    async def _capture_calls(fc):
        calls.extend(fc)

    async def _capture_usage(t):
        usage.append(t)

    async def _noop(*a, **k):
        pass

    monkeypatch.setattr(bridge, "push_frame", _capture_frame)
    monkeypatch.setattr(bridge, "run_function_calls", _capture_calls)
    monkeypatch.setattr(bridge, "start_llm_usage_metrics", _capture_usage)
    for name in ("start_processing_metrics", "stop_processing_metrics",
                 "start_ttfb_metrics", "stop_ttfb_metrics"):
        monkeypatch.setattr(bridge, name, _noop)

    ctx = LLMContext(tools=tool_schema)
    ctx.add_message({"role": "user", "content": "hi"})
    asyncio.run(bridge.process_frame(LLMContextFrame(ctx), bot.FrameDirection.DOWNSTREAM))

    # 帧序保真：封套 + delta 逐段流式（LLMTextFrame 不合并）
    assert [type(f).__name__ for f in frames] == [
        "LLMFullResponseStartFrame", "LLMTextFrame", "LLMTextFrame", "LLMFullResponseEndFrame",
    ]
    assert [f.text for f in frames[1:3]] == ["he", "llo"]
    # 工具请求翻成 FunctionCallFromLLM 交注册执行器
    assert len(calls) == 1
    assert calls[0].function_name == "end_call" and calls[0].tool_call_id == "t1"
    assert calls[0].arguments == {"reason": "bye"}
    # 用量计量保真（行为保真卡：参数/流式/工具调用/用量计量）
    assert len(usage) == 1
    assert usage[0].prompt_tokens == 7 and usage[0].completion_tokens == 3
    # FC-3 调用面：系统指令前置、F6 对话原值 256、ToolSpec 边界翻译
    kw = fc3.stream_kwargs
    assert kw["max_tokens"] == 256
    assert kw["messages"][0] == {"role": "system", "content": "SYS"}
    assert kw["messages"][-1]["content"] == "hi"
    assert kw["tools"][0].name == "end_call"
    assert kw["tools"][0].parameters_schema["properties"] == {"reason": {"type": "string"}}
