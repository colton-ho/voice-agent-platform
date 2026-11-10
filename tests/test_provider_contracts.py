"""U-002 contract tests — FC-0..FC-5, FC-8 structural shape (light).

只验冻结契约的结构（U-002 验收 2/3）：ABC 拒绝直接实例化、最小 stub 子类
可实例化并暴露且仅暴露 §3 冻结的抽象成员（防"多余动词"漂移）、能力标志已声明、
契约模块导入纯净（无 SDK）。深度适配器行为测试属于 Phase B。
"""
import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from providers.base import (
    AudioFormat,
    ChatMessage,
    LanguageModel,
    ModelEvent,
    RealtimeVoiceEngine,
    SignatureValidator,
    SpeechRecognizer,
    SpeechSynthesizer,
    SynthesisChunk,
    TelephonyAdapter,
    ToolRequest,
    ToolSpec,
    TranscriptEvent,
    UsageReport,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- 最小 stub 子类：实现每个抽象成员即可，行为留给 Phase B -------------------


class StubTelephony(TelephonyAdapter):
    supports_transfer = False

    def validate_webhook(self, *, url, params, signature, validator):
        return True

    def start_stream(self, *, stream_url, caller):
        return "<Response/>"

    def decode_incoming_media(self, *, payload):
        return b""

    def encode_outgoing_media(self, *, pcm):
        return ""

    def clear_playback(self):
        return {}

    def transfer(self, *, destination):
        return ""

    def hang_up(self):
        return {}


class StubRecognizer(SpeechRecognizer):
    supports_language_detection = False

    async def start(self, *, language, input_format):
        return None

    async def push_audio(self, pcm):
        return None

    def events(self):
        return iter(())

    async def stop(self):
        return None


class StubLLM(LanguageModel):
    supports_tool_calls = False

    def stream(self, *, messages, tools=None):
        return iter(())

    async def cancel(self):
        return None

    def usage(self):
        return UsageReport(provider="stub", model="stub", input_units=0, output_units=0, raw={})


class StubSynth(SpeechSynthesizer):
    def synthesize(self, text_stream):
        return iter(())

    async def cancel(self):
        return None

    def output_format(self):
        return AudioFormat(sample_rate=8000, encoding="pcm16")


class StubRealtime(RealtimeVoiceEngine):
    async def start(self, *, context):
        return None

    async def push_audio(self, pcm):
        return None

    def events(self):
        return iter(())


class StubSignatureValidator(SignatureValidator):
    def is_valid(self, *, url, params, signature):
        return True


# §3.2 冻结方法集——断言 __abstractmethods__ 与此完全一致，多一个少一个都算漂移
FROZEN_METHODS = {
    TelephonyAdapter: {"validate_webhook", "start_stream", "decode_incoming_media",
                       "encode_outgoing_media", "clear_playback", "transfer", "hang_up"},
    SpeechRecognizer: {"start", "push_audio", "events", "stop"},
    LanguageModel: {"stream", "cancel", "usage"},
    SpeechSynthesizer: {"synthesize", "cancel", "output_format"},
    RealtimeVoiceEngine: {"start", "push_audio", "events"},
}

STUBS = {
    TelephonyAdapter: StubTelephony,
    SpeechRecognizer: StubRecognizer,
    LanguageModel: StubLLM,
    SpeechSynthesizer: StubSynth,
    RealtimeVoiceEngine: StubRealtime,
}


@pytest.mark.parametrize("contract", sorted(FROZEN_METHODS, key=lambda c: c.__name__))
def test_abc_rejects_direct_instantiation(contract):
    with pytest.raises(TypeError):
        contract()


@pytest.mark.parametrize("contract", sorted(FROZEN_METHODS, key=lambda c: c.__name__))
def test_stub_subclass_instantiates_and_exposes_frozen_members(contract):
    assert set(contract.__abstractmethods__) == FROZEN_METHODS[contract]
    stub = STUBS[contract]()
    for name in FROZEN_METHODS[contract]:
        assert callable(getattr(stub, name)), name


@pytest.mark.parametrize(
    "contract,flag",
    [
        (TelephonyAdapter, "supports_transfer"),
        (SpeechRecognizer, "supports_language_detection"),
        (LanguageModel, "supports_tool_calls"),
    ],
)
def test_capability_flags_declared(contract, flag):
    # 能力标志（spec §7.1/决策 D-5）：契约层声明，具体值由适配器给出
    assert flag in contract.__annotations__
    assert isinstance(getattr(STUBS[contract](), flag), bool)


def test_signature_validator_is_pluggable_protocol():
    assert getattr(SignatureValidator, "_is_protocol", False)
    with pytest.raises(TypeError):
        SignatureValidator()
    assert callable(StubSignatureValidator().is_valid)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: AudioFormat(sample_rate=8000, encoding="pcm16"),
        lambda: TranscriptEvent(text="hi", is_final=True),
        lambda: ChatMessage(role="user", content="hi"),
        lambda: ToolSpec(name="t", description="d", parameters_schema={}),
        lambda: ToolRequest(id="1", name="t", arguments={}),
        lambda: UsageReport(provider="p", model="m", input_units=1, output_units=1, raw={}),
        lambda: ModelEvent(kind="delta", text="x"),
        lambda: SynthesisChunk(audio=b"", format=AudioFormat(sample_rate=8000, encoding="mulaw")),
    ],
    ids=["AudioFormat", "TranscriptEvent", "ChatMessage", "ToolSpec",
         "ToolRequest", "UsageReport", "ModelEvent", "SynthesisChunk"],
)
def test_fc0_value_types_are_frozen_dataclasses(factory):
    obj = factory()
    with pytest.raises(FrozenInstanceError):
        obj.contract_frozen_probe = 1


def test_contract_modules_import_no_provider_sdks():
    # U-002 验收 3 的自动化形态（自测 grep 的等价断言）：AST 扫描被禁导入根
    banned = ("pipecat", "boto3", "botocore", "aioboto3", "azure")
    for rel in ("providers/base.py", "storage/base.py"):
        tree = ast.parse((REPO_ROOT / rel).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            assert not any(r in banned for r in roots), f"{rel} imports SDK: {roots}"
