"""U-004 — §3.4 provider registry：显式注册、显式选择，零默认、零回退。

冻结语义（§3.4）：
- kind：TELEPHONY | STT | TTS | LLM | REALTIME，各绑定一个 §11 选择变量；
- VOICE_ENGINE=cascade 不是引擎名，而是组合模式：create(REALTIME) 返回
  (FC-2, FC-3, FC-4) 三元组交组合根接线；其余 VOICE_ENGINE 值按 REALTIME 名解析（FC-5）；
- 选择变量缺失/为空 -> MissingSelectionError（指名变量）；未注册名 ->
  UnsupportedProviderError（列出已注册名单）；AWS 时代遗留名永不注册、永不解析。
工厂签名（§3.4）：Callable[[AppConfig], 契约实例]，产物必须过契约 ABC 检查。
"""
from collections.abc import Callable
from enum import Enum

from config_contract import AppConfig
from providers.base import (LanguageModel, RealtimeVoiceEngine, SpeechRecognizer,
                            SpeechSynthesizer, TelephonyAdapter)


class ProviderKind(str, Enum):
    TELEPHONY = "TELEPHONY"
    STT = "STT"
    TTS = "TTS"
    LLM = "LLM"
    REALTIME = "REALTIME"


class MissingSelectionError(Exception):
    """选择变量缺失/为空 —— 无默认值、无回退（R-C-002）。"""

    def __init__(self, variable: str) -> None:
        self.variable = variable
        super().__init__(
            f"{variable}: missing or empty — provider selection has no default and no fallback (R-C-002)"
        )


class UnsupportedProviderError(Exception):
    """未注册（或永不解析）的供应商名 —— 报错必须列出可选名单（§3.4）。"""

    def __init__(self, *, kind: ProviderKind, name: str, variable: str,
                 registered: tuple[str, ...]) -> None:
        self.kind, self.name, self.variable = kind, name, variable
        self.registered = registered
        listed = ", ".join(registered) if registered else "(none registered)"
        super().__init__(
            f"{variable}: provider {name!r} is not registered for kind {kind.value}; "
            f"registered names: {listed}"
        )


# kind -> §11 选择变量（AppConfig 字段即变量名的小写蛇形）
_SELECTION_VAR = {
    ProviderKind.TELEPHONY: "TELEPHONY_PROVIDER",
    ProviderKind.STT: "STT_PROVIDER",
    ProviderKind.TTS: "TTS_PROVIDER",
    ProviderKind.LLM: "LLM_PROVIDER",
    ProviderKind.REALTIME: "VOICE_ENGINE",
}

# kind -> 契约 ABC：工厂产物必须是其实例，鸭子类型/伪适配器一律拒绝
_CONTRACT = {
    ProviderKind.TELEPHONY: TelephonyAdapter,
    ProviderKind.STT: SpeechRecognizer,
    ProviderKind.TTS: SpeechSynthesizer,
    ProviderKind.LLM: LanguageModel,
    ProviderKind.REALTIME: RealtimeVoiceEngine,
}

# AWS 时代/遗留供应商名 —— 永不注册、永不解析（§3.4「must never resolve」，R-C-002）。
# Phase B 若批准新适配器（如 MiniMax，U-101 且 G-1/G-2 放行），须用新注册名，不得复活遗留名。
_LEGACY_AWS_NAMES = frozenset({
    "aws_transcribe", "transcribe", "bedrock", "polly", "nova-sonic", "minimax",
})

Factory = Callable[[AppConfig], object]


class ProviderRegistry:
    """name->factory 显式注册表。选择只认注册名，没有任何默认或回退。"""

    def __init__(self) -> None:
        self._factories: dict[tuple[ProviderKind, str], Factory] = {}

    def register(self, kind: ProviderKind, name: str, factory: Factory) -> None:
        kind = ProviderKind(kind)
        name = name.strip()
        if not name:
            raise ValueError("provider name must be non-empty")
        # casefold 匹配：大小写变体同样封杀（F-05）
        if name.casefold() in _LEGACY_AWS_NAMES:
            raise UnsupportedProviderError(kind=kind, name=name,
                                           variable=_SELECTION_VAR[kind],
                                           registered=self._names(kind))
        key = (kind, name)
        if key in self._factories:
            raise ValueError(f"duplicate registration: kind={kind.value} name={name!r}")
        self._factories[key] = factory

    def create(self, kind: ProviderKind, config: AppConfig) -> object:
        """按配置解析并实例化。REALTIME + VOICE_ENGINE=cascade 返回 (stt, llm, tts) 三元组。"""
        kind = ProviderKind(kind)
        if kind is ProviderKind.REALTIME and (config.voice_engine or "").strip() == "cascade":
            # cascade 是组合模式而非引擎名：FC-2+FC-3+FC-4 各走显式选择，交组合根接线（§3.4）
            return (self.create(ProviderKind.STT, config),
                    self.create(ProviderKind.LLM, config),
                    self.create(ProviderKind.TTS, config))
        return self._resolve(kind, config)

    def _resolve(self, kind: ProviderKind, config: AppConfig):
        variable = _SELECTION_VAR[kind]
        name = (getattr(config, variable.lower()) or "").strip()
        if not name:
            raise MissingSelectionError(variable)
        if name.casefold() in _LEGACY_AWS_NAMES:
            # 双保险：即使有人绕过 register 直接塞表，遗留名也永不解析
            raise UnsupportedProviderError(kind=kind, name=name, variable=variable,
                                           registered=self._names(kind))
        factory = self._factories.get((kind, name))
        if factory is None:
            raise UnsupportedProviderError(kind=kind, name=name, variable=variable,
                                           registered=self._names(kind))
        instance = factory(config)
        contract = _CONTRACT[kind]
        if not isinstance(instance, contract):
            # 工厂产物必须过契约检查 —— 注册表绝不伪造/放行伪适配器
            raise TypeError(
                f"factory for {kind.value}/{name!r} returned {type(instance).__name__}, "
                f"expected {contract.__name__}"
            )
        return instance

    def _names(self, kind: ProviderKind) -> tuple[str, ...]:
        return tuple(sorted(n for (k, n) in self._factories if k is kind))
