"""FC-0 shared value types, FC-1..FC-5 provider interfaces, FC-8 SignatureValidator.

Frozen contract — implementation-plan §3.1/§3.2 (single definition site).
方法集与命名与 §3 完全一致：不加多余动词，不引入任何 provider 类型。
导入纯度约束（U-002 验收）：只允许标准库，禁止 pipecat/boto3/botocore/
aioboto3/azure SDK —— 适配器在边界翻译，契约层不受 SDK 签名漂移影响（G-1）。
"""
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

# ---------------------------------------------------------------------------
# FC-0 shared value types (§3.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AudioFormat:
    sample_rate: int                       # 8000 | 16000 | 24000 in this system
    encoding: Literal["pcm16", "mulaw"]
    channels: int = 1


@dataclass(frozen=True)
class TranscriptEvent:
    text: str
    is_final: bool
    language: str | None = None


@dataclass(frozen=True)
class ChatMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str
    tool_call_id: str | None = None
    name: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters_schema: dict                # JSON Schema dict — same shape tools/registry.assemble_tools_schema emits


@dataclass(frozen=True)
class ToolRequest:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class UsageReport:
    provider: str
    model: str
    input_units: int
    output_units: int
    raw: dict                              # provider-native counters preserved verbatim


@dataclass(frozen=True)
class ModelEvent:
    kind: Literal["delta", "tool_request", "usage", "end"]
    text: str | None = None                # kind in {"delta","end"}
    tool_request: ToolRequest | None = None
    usage: UsageReport | None = None


@dataclass(frozen=True)
class SynthesisChunk:
    audio: bytes
    format: AudioFormat
    is_final: bool = False


# ---------------------------------------------------------------------------
# FC-8 SignatureValidator (§3.2) — pluggable so decision A-1 (SDK vs stdlib)
# blocks nothing: 可以换成 SDK 实现或纯 stdlib 实现，调用方无感知。
# ---------------------------------------------------------------------------


class SignatureValidator(Protocol):        # FC-8 — pluggable (decision A-1)
    def is_valid(self, *, url: str, params: Mapping[str, str], signature: str) -> bool: ...


# ---------------------------------------------------------------------------
# FC-1..FC-5 provider-facing interfaces (§3.2)
# ---------------------------------------------------------------------------


class TelephonyAdapter(ABC):               # FC-1
    # 能力标志（decision D-5/G-5）：转接不是所有 telephony 都有，禁止用单一方法硬凑
    supports_transfer: bool                # capability flag (decision D-5/G-5)

    @abstractmethod
    def validate_webhook(self, *, url: str, params: Mapping[str, str], signature: str,
                         validator: SignatureValidator) -> bool: ...

    @abstractmethod
    def start_stream(self, *, stream_url: str, caller: str) -> str:
        """Provider handshake payload (Twilio: TwiML <Connect><Stream> document)."""

    @abstractmethod
    def decode_incoming_media(self, *, payload: Mapping[str, object]) -> bytes:
        """Provider media frame (Twilio: base64 μ-law/8 kHz) -> PCM16 bytes."""

    @abstractmethod
    def encode_outgoing_media(self, *, pcm: bytes) -> str:
        """PCM16 bytes -> provider outbound media payload (Twilio: base64 μ-law/8 kHz)."""

    @abstractmethod
    def clear_playback(self) -> Mapping[str, object]:
        """Provider message flushing queued playback (Twilio: `clear`)."""

    @abstractmethod
    def transfer(self, *, destination: str) -> str:
        """Provider message/TwiML performing the transfer (gated by D-5/G-5)."""

    @abstractmethod
    def hang_up(self) -> Mapping[str, object]: ...


class SpeechRecognizer(ABC):               # FC-2
    supports_language_detection: bool      # capability flag (spec §7.1)

    @abstractmethod
    async def start(self, *, language: str, input_format: AudioFormat) -> None: ...

    @abstractmethod
    async def push_audio(self, pcm: bytes) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[TranscriptEvent]: ...

    @abstractmethod
    async def stop(self) -> None: ...


class LanguageModel(ABC):                  # FC-3
    supports_tool_calls: bool

    @abstractmethod
    def stream(self, *, messages: Sequence[ChatMessage],
               tools: Sequence[ToolSpec] | None = None) -> AsyncIterator[ModelEvent]: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    @abstractmethod
    def usage(self) -> UsageReport: ...


class SpeechSynthesizer(ABC):              # FC-4

    @abstractmethod
    def synthesize(self, text_stream: AsyncIterator[str]) -> AsyncIterator[SynthesisChunk]: ...

    @abstractmethod
    async def cancel(self) -> None: ...

    @abstractmethod
    def output_format(self) -> AudioFormat: ...


class RealtimeVoiceEngine(ABC):            # FC-5 — optional; not needed for baseline (spec §7.3)

    @abstractmethod
    async def start(self, *, context: Mapping[str, object]) -> None: ...

    @abstractmethod
    async def push_audio(self, pcm: bytes) -> None: ...

    @abstractmethod
    def events(self) -> AsyncIterator[TranscriptEvent | ModelEvent | SynthesisChunk]: ...
