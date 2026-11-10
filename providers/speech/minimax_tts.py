"""U-101（G-2 裁决后条件件）— MiniMax TTS FC-4 适配器，注册名 ``minimax_tts``。

G-2 用户审批通过 MiniMax 保留（team-lead 补充派令 2026-11-09）：以**新注册名**
建 FC-4 SpeechSynthesizer，行为保真迁入现有 MiniMax TTS 全部语义。语义源逐项：
- SimpleMiniMaxTTSService.run_tts 线协议（bot.py:606-736）：SSE ``data:`` 块解析、
  空 GroupId 剥 URL 查询串（bot.py:626-629，空 GroupId 被 MiniMax 视作未充值
  账户）、base_resp.status_code != 0 响亮报错（而非静默零音频）、hex PCM 重切、
  粤语「返/(faan1)」多音字修正（bot.py:658-662）、三段错误路径。
- MINIMAX_VOICES（bot.py:501-596，逐字迁移）/ DEFAULT_MINIMAX_VOICE（:597）/
  DEFAULT_MINIMAX_VOICE_BY_LANG（:602）/ _resolve_minimax_voice（:933-949，
  含 boost 语义与永不失败回退）→ :func:`resolve_minimax_voice`。
- MINIMAX_MODELS / DEFAULT_MINIMAX_MODEL（bot.py:868-872，MINIMAX_MODEL_DEFAULT
  env 覆盖）→ :func:`resolve_model`。
- _build_tts 构造形态（bot.py:1757-1797）：MiniMaxTTSSettings(model, voice) +
  language_boost 属性注入 + stream=True + audio_setting 128k/pcm/单声道。

注册名钉死：``minimax_tts`` —— 遗留名 ``minimax`` 在 registry._LEGACY_AWS_NAMES
（registry.py:71-73）casefold 永拒（register :90 + _resolve :114 双保险），绝不复活。

凭据注入（Key Vault 接管点；#17/P1 的迁移不在本卡）：api_key **只**经构造/工厂
参数注入（make_tts_factory(api_key=...)），适配器内不读 MINIMAX_API_KEY 环境
变量——Key Vault 引用接管的就是这个参数。MINIMAX_GROUP_ID / MINIMAX_BASE_URL
保留 _build_tts 的 env 读取语义（可用显式参数覆盖）。

活表语义缝：voices_for("minimax") 活 VOICE_STORE 表优先于常量表（bot.py:988）
——providers 层不导入 bot.py，经 voice_lookup 注入缝由组合根（U-103）传
``lambda: voices_for("minimax")``；缺省用本模块常量表。永不失败回退恒读常量表
（与 bot.py:949 一致），任何输入都不抛错。

MINIMAX_VOICES 是 bot.py:501-596 的逐字迁移（本模块为迁移后的 TTS 语义源；
bot.py 副本属遗留路径，cutover 前不动，tools/registry 输出形状不变）。
tests/test_minimax_tts.py 钉两张表逐字相等 + 解析函数逐输入等价，防漂移。
"""
from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

from config_contract import AppConfig
from providers.base import AudioFormat, SpeechSynthesizer, SynthesisChunk
from providers.registry import ProviderKind, ProviderRegistry
from providers.speech._tts_stream import stream_synthesis

# FC-0 冻结：AudioFormat.sample_rate 只允许这三个值（base.py:21 注释）
_SYSTEM_SAMPLE_RATES = (8000, 16000, 24000)

# registry 名（team-lead 补充派令 2026-11-09：新名注册，遗留名 minimax 永拒）
TTS_REGISTRY_NAME = "minimax_tts"

# _build_tts 同源缺省（bot.py:1791-1792）
DEFAULT_MINIMAX_BASE_URL = "https://api.minimax.chat/v1/t2a_v2"

# ---------------------------------------------------------------------------
# 音色表与解析语义（bot.py:495-604 / :933-949 逐字迁移）
# ---------------------------------------------------------------------------

# MiniMax language_boost 的取值就是这几个 API 字符串。
_BOOST_YUE = "Chinese,Yue"
_BOOST_ZH = "Chinese"
_BOOST_EN = "English"
_BOOST_JA = "Japanese"

MINIMAX_VOICES: dict[str, dict] = {
    # Cantonese (zh-HK) — note full-width "（" in ProfessionalHost IDs.
    "Cantonese_GentleLady":          {"label": "Gentle Lady",          "gender": "F",   "language": "zh-HK", "boost": _BOOST_YUE},
    "Cantonese_KindWoman":           {"label": "Kind Woman",           "gender": "F",   "language": "zh-HK", "boost": _BOOST_YUE},
    "Cantonese_CuteGirl":            {"label": "Cute Girl",            "gender": "F",   "language": "zh-HK", "boost": _BOOST_YUE},
    "Cantonese_PlayfulMan":          {"label": "Playful Man",          "gender": "M",   "language": "zh-HK", "boost": _BOOST_YUE},
    "Cantonese_ProfessionalHost（F)":{"label": "Professional Host","gender": "F",   "language": "zh-HK", "boost": _BOOST_YUE},
    "Cantonese_ProfessionalHost（M)":{"label": "Professional Host","gender": "M",   "language": "zh-HK", "boost": _BOOST_YUE},

    # Mandarin (zh-CN) — verified against `get_voice` API
    "Chinese (Mandarin)_Gentleman":            {"label": "Gentleman",             "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_News_Anchor":          {"label": "News Anchor",           "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Reliable_Executive":   {"label": "Reliable Executive",    "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Male_Announcer":       {"label": "Male Announcer",        "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Radio_Host":           {"label": "Radio Host (M)",        "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Lyrical_Voice":        {"label": "Lyrical Voice (M)",     "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Humorous_Elder":       {"label": "Humorous Elder",        "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Kind-hearted_Elder":   {"label": "Kind-hearted Elder",    "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Gentle_Senior":        {"label": "Gentle Senior",         "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Sincere_Adult":        {"label": "Sincere Adult",         "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Mature_Woman":         {"label": "Mature Woman",          "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Wise_Women":           {"label": "Wise Woman",            "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Warm_Bestie":          {"label": "Warm Bestie",           "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Sweet_Lady":           {"label": "Sweet Lady",            "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Warm_Girl":            {"label": "Warm Girl",             "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Crisp_Girl":           {"label": "Crisp Girl",            "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Soft_Girl":            {"label": "Soft Girl",             "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Cute_Spirit":          {"label": "Cute Spirit",           "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Kind-hearted_Antie":   {"label": "Kind-hearted Auntie",   "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_HK_Flight_Attendant":  {"label": "HK Flight Attendant",   "gender": "F",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Stubborn_Friend":      {"label": "Stubborn Friend",       "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Gentle_Youth":         {"label": "Gentle Youth",          "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Southern_Young_Man":   {"label": "Southern Young Man",    "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Unrestrained_Young_Man":{"label": "Unrestrained Young Man","gender": "M",  "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Straightforward_Boy":  {"label": "Straightforward Boy",   "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},
    "Chinese (Mandarin)_Pure-hearted_Boy":     {"label": "Pure-hearted Boy",      "gender": "M",   "language": "zh-CN", "boost": _BOOST_ZH},

    # English (en-US) — verified against `get_voice` API (only 6 enabled)
    "English_Trustworthy_Man":   {"label": "Trustworthy Man",  "gender": "M", "language": "en-US", "boost": _BOOST_EN},
    "English_Graceful_Lady":     {"label": "Graceful Lady",    "gender": "F", "language": "en-US", "boost": _BOOST_EN},
    "English_Aussie_Bloke":      {"label": "Aussie Bloke",     "gender": "M", "language": "en-US", "boost": _BOOST_EN},
    "English_Whispering_girl":   {"label": "Whispering Girl",  "gender": "F", "language": "en-US", "boost": _BOOST_EN},
    "English_Diligent_Man":      {"label": "Diligent Man",     "gender": "M", "language": "en-US", "boost": _BOOST_EN},
    "English_Gentle-voiced_man": {"label": "Gentle-voiced Man","gender": "M", "language": "en-US", "boost": _BOOST_EN},

    # Japanese (ja-JP)
    "Japanese_IntellectualSenior":  {"label": "Intellectual Senior",   "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_GentleButler":        {"label": "Gentle Butler",         "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_LoyalKnight":         {"label": "Loyal Knight",          "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_DominantMan":         {"label": "Dominant Man",          "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_SeriousCommander":    {"label": "Serious Commander",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_GenerousIzakayaOwner":{"label": "Izakaya Owner",         "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_InnocentBoy":         {"label": "Innocent Boy",          "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_KindLady":            {"label": "Kind Lady",             "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_CalmLady":            {"label": "Calm Lady",             "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_DependableWoman":     {"label": "Dependable Woman",      "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_DecisivePrincess":    {"label": "Decisive Princess",     "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_ColdQueen":           {"label": "Cold Queen",            "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_GracefulMaiden":      {"label": "Graceful Maiden",       "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_OptimisticYouth":     {"label": "Optimistic Youth",      "gender": "M/F", "language": "ja-JP", "boost": _BOOST_JA},
    "Japanese_SportyStudent":       {"label": "Sporty Student",        "gender": "M/F", "language": "ja-JP", "boost": _BOOST_JA},

    # Japanese — custom cloned voices (1222 batch)
    "jap_female_1222_1":            {"label": "JP Female 1222-1",      "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_1222_3":            {"label": "JP Female 1222-3",      "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_1222_4":            {"label": "JP Female 1222-4",      "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_1222_6":            {"label": "JP Female 1222-6",      "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_1222_1":              {"label": "JP Male 1222-1",        "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_1222_2":              {"label": "JP Male 1222-2",        "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},

    # Japanese — custom cloned voices (voice_agent batch)
    "jap_female_voice_agent_001":   {"label": "JP Female Agent 001",   "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_voice_agent_002":   {"label": "JP Female Agent 002",   "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_voice_agent_003":   {"label": "JP Female Agent 003",   "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_voice_agent_004":   {"label": "JP Female Agent 004",   "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_voice_agent_005":   {"label": "JP Female Agent 005",   "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_voice_agent_006":   {"label": "JP Female Agent 006",   "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_voice_agent_007":   {"label": "JP Female Agent 007",   "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_female_voice_agent_008":   {"label": "JP Female Agent 008",   "gender": "F",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_001":     {"label": "JP Male Agent 001",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_002":     {"label": "JP Male Agent 002",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_004":     {"label": "JP Male Agent 004",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_005":     {"label": "JP Male Agent 005",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_006":     {"label": "JP Male Agent 006",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_007":     {"label": "JP Male Agent 007",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_008":     {"label": "JP Male Agent 008",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_009":     {"label": "JP Male Agent 009",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},
    "jap_male_voice_agent_010":     {"label": "JP Male Agent 010",     "gender": "M",   "language": "ja-JP", "boost": _BOOST_JA},

    # Singapore English (en-SG) — user-supplied MiniMax IVC custom voices.
    # These are account-scoped IVC IDs, not public preset voices, so they're
    # only validated by hitting t2a_v2 (see T3 pre-deploy IVC API smoke).
    "mm_singlish_female_5_ivc_v3":  {"label": "Singlish Female 5",     "gender": "F",   "language": "en-SG", "boost": _BOOST_EN},
    "mm_singlish_male_2_ivc":       {"label": "Singlish Male 2",       "gender": "M",   "language": "en-SG", "boost": _BOOST_EN},
    "mm_singlish_male_1_ivc":       {"label": "Singlish Male 1",       "gender": "M",   "language": "en-SG", "boost": _BOOST_EN},
}
DEFAULT_MINIMAX_VOICE = "Cantonese_GentleLady"

# Per-language default voice override. Used when a session has lang=X but no
# explicit voice — without this, en-SG would fall back to Cantonese_GentleLady,
# which speaks Cantonese (wrong language).
DEFAULT_MINIMAX_VOICE_BY_LANG: dict[str, str] = {
    "en-SG": "mm_singlish_female_5_ivc_v3",
}


def resolve_minimax_voice(
    voice_key: str | None,
    lang: str | None = None,
    *,
    voices: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[str, str]:
    """bot.py:933-949 语义迁移：返回 (voice_id, language_boost)，永不抛错。

    顺序：已知 key 直接赢 → 按 lang 走 DEFAULT_MINIMAX_VOICE_BY_LANG →
    DEFAULT_MINIMAX_VOICE → 常量表兜底 boost（即便活表里默认音色被删也不崩）。
    """
    if voices is None:
        voices = MINIMAX_VOICES
    if voice_key in voices:
        return voice_key, voices[voice_key]["boost"]
    if lang and lang in DEFAULT_MINIMAX_VOICE_BY_LANG:
        fb = DEFAULT_MINIMAX_VOICE_BY_LANG[lang]
        if fb in voices:
            return fb, voices[fb]["boost"]
    if DEFAULT_MINIMAX_VOICE in voices:
        return DEFAULT_MINIMAX_VOICE, voices[DEFAULT_MINIMAX_VOICE]["boost"]
    return DEFAULT_MINIMAX_VOICE, MINIMAX_VOICES[DEFAULT_MINIMAX_VOICE]["boost"]


# ---------------------------------------------------------------------------
# 模型语义（bot.py:865-872 逐字迁移；MINIMAX_MODEL_DEFAULT env 覆盖）
# ---------------------------------------------------------------------------

MINIMAX_MODELS: dict[str, str] = {
    "speech-2.8-turbo": "speech-2.8-turbo (faster, recommended)",
    "speech-2.8-hd":    "speech-2.8-hd (higher quality, slower)",
}
DEFAULT_MINIMAX_MODEL = os.environ.get("MINIMAX_MODEL_DEFAULT", "speech-2.8-turbo")


def resolve_model(model: str | None) -> str:
    """bot.py:1782 语义：不在 MINIMAX_MODELS 的取值一律落到默认模型。"""
    return model if model in MINIMAX_MODELS else DEFAULT_MINIMAX_MODEL


# ---------------------------------------------------------------------------
# FC-4 SpeechSynthesizer
# ---------------------------------------------------------------------------


class MiniMaxSpeechSynthesizer(SpeechSynthesizer):
    """MiniMax T2A v2 TTS。线协议 = SimpleMiniMaxTTSService（bot.py:606-736）迁入。

    FC-4 无显式 start/stop：服务按 synthesize() 调用惰性建/关，cancel() 提升
    generation 并关掉活动服务；跨调用零状态。音色/boost 在构造期解析一次
    （与 _build_tts 构建期解析一致）。
    """

    def __init__(
        self,
        *,
        api_key: str,
        voice: str | None,
        sample_rate: int,
        lang: str | None = None,
        model: str | None = None,
        group_id: str = "",
        base_url: str = DEFAULT_MINIMAX_BASE_URL,
        voice_lookup: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
        service_factory: Callable[..., Any] | None = None,
    ) -> None:
        api_key = (api_key or "").strip()
        if not api_key:
            # api_key 无 §11 变量名可指（CR 缺口）；此形参即 Key Vault 接管点（#17/P1）
            raise ValueError(
                "api_key: empty — MiniMax API key required (no §11 credential placeholder "
                "yet — U-101 CR gap; inject via make_tts_factory(api_key=...), which is the "
                "Key Vault takeover point, #17/P1)"
            )
        self._api_key = api_key
        if sample_rate not in _SYSTEM_SAMPLE_RATES:
            # 采样率是 FC-0 音频契约参数，由组合根（U-103）显式传入——不做默认值（D-4 姿态）
            raise ValueError(
                f"sample_rate: must be one of {_SYSTEM_SAMPLE_RATES} (FC-0 AudioFormat), "
                f"got {sample_rate!r}"
            )
        self._sample_rate = sample_rate
        lookup = voice_lookup or (lambda: MINIMAX_VOICES)
        self._voice_id, self._language_boost = resolve_minimax_voice(
            voice, lang, voices=lookup()
        )
        self._model = resolve_model(model)
        self._group_id = group_id or ""
        base_url = (base_url or "").strip()
        self._base_url = base_url or DEFAULT_MINIMAX_BASE_URL
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
        factory = self._service_factory or _default_service_factory
        service = factory(
            api_key=self._api_key,
            group_id=self._group_id,
            base_url=self._base_url,
            voice=self._voice_id,
            model=self._model,
            language_boost=self._language_boost,
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
                error_label="MiniMax TTS synthesis failed",
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
            # 在途 HTTP 流无法中断，但产物已被代际守卫丢弃
            await service.close()


# ---------------------------------------------------------------------------
# 默认服务工厂：pipecat 1.4.0 桥接子类（懒加载；run_tts 逐行迁入 bot.py:622-736）
# ---------------------------------------------------------------------------


def _default_service_factory(
    *,
    api_key: str,
    group_id: str,
    base_url: str,
    voice: str,
    model: str,
    language_boost: str,
    sample_rate: int,
    session=None,
):
    """构建 MiniMaxHttpTTSService 桥。session 缺省自建（close 时随桥关闭）；
    测试/组合根可注入 aiohttp 风格传输（.post(url, headers=, json=)）换掉网络层。
    """
    import json

    from loguru import logger
    from pipecat.frames.frames import EndFrame, ErrorFrame, StartFrame, TTSAudioRawFrame
    from pipecat.services.minimax.tts import MiniMaxHttpTTSService, MiniMaxTTSSettings
    from pipecat.utils.tracing.service_decorators import traced_tts

    class _MiniMaxTtsBridge(MiniMaxHttpTTSService):
        """独立驱动桥：run_tts = SimpleMiniMaxTTSService 线协议（bot.py:622-736）
        逐行迁入；只额外覆写管道缝。

        缝位注：TTSService.start/stop 经 create_task 建音频上下文任务
        （tts_service.py:532-552/:1406-1410）——task_manager 未 setup，覆写落到
        事件循环裸任务；push_frame 无下游可推（no-op）。start_tts_usage_metrics /
        stop_ttfb_metrics 保留原调用（与 bot.py 行为一致），其指标帧被 no-op
        push_frame 吞掉，独立驱动下无副作用。
        """

        def __init__(self, *, owns_session: bool = False, **kwargs):
            self._owns_session = owns_session
            super().__init__(**kwargs)

        async def open(self) -> None:
            await self.start(StartFrame(audio_out_sample_rate=self.sample_rate))

        async def close(self) -> None:
            await self.stop(EndFrame())
            if self._owns_session:
                await self._session.close()

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

        # ---- SimpleMiniMaxTTSService.run_tts 逐行迁入（bot.py:622-736）----

        @traced_tts
        async def run_tts(self, text: str, context_id: str):
            logger.debug(f"{self}: MiniMax synth [{text}]")

            # Empty GroupId in URL == different (unfunded) account on MiniMax's side.
            url = self._base_url
            if not (self._group_id and self._group_id.strip()):
                url = url.split("?", 1)[0]

            headers = {
                "accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            }
            voice_setting = {
                "voice_id": self._settings.voice,
                "speed": self._settings.speed,
                "vol": self._settings.volume,
                "pitch": self._settings.pitch,
            }
            if self._settings.emotion is not None:
                voice_setting["emotion"] = self._settings.emotion
            payload: dict = {
                "stream": True,
                "model": self._settings.model,
                "text": text,
                "voice_setting": voice_setting,
                "audio_setting": {
                    "bitrate": self._audio_bitrate,
                    "format": self._audio_format,
                    "channel": self._audio_channel,
                    "sample_rate": self._audio_sample_rate,
                },
            }
            if self._settings.language_boost is not None:
                payload["language_boost"] = self._settings.language_boost
                # Cantonese 「返」homograph fix: force faan1 (虚词「再做一次」) over the
                # default faan2 (回去) reading. MiniMax T2A v2 pronunciation_dict.tone
                # accepts Jyutping 1-6, format "原字/(jyutping)".
                if self._settings.language_boost == "Chinese,Yue":
                    payload["pronunciation_dict"] = {"tone": ["返/(faan1)"]}

            try:
                resp_cm = self._session.post(url, headers=headers, json=payload)
            except Exception as e:
                yield ErrorFrame(error=f"MiniMax request failed: {e}")
                return

            usage_started = False
            try:
                async with resp_cm as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        yield ErrorFrame(error=f"MiniMax HTTP {resp.status}: {body[:200]}")
                        return

                    buffer = bytearray()
                    CHUNK = 8192
                    async for raw in resp.content.iter_chunked(CHUNK):
                        if not raw:
                            continue
                        buffer.extend(raw)
                        # Walk all complete SSE blocks ("data: ..." separated).
                        while b"data:" in buffer:
                            start = buffer.find(b"data:")
                            nxt = buffer.find(b"data:", start + 5)
                            if nxt == -1:
                                # Keep current block in buffer; need more bytes.
                                if start > 0:
                                    buffer = buffer[start:]
                                break
                            block = bytes(buffer[start:nxt])
                            buffer = buffer[nxt:]
                            try:
                                data = json.loads(block[5:].decode("utf-8"))
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue

                            # First chunk of a stream may be base_resp / extra_info only.
                            base = (data or {}).get("base_resp") or {}
                            if base.get("status_code", 0) != 0:
                                yield ErrorFrame(error=f"MiniMax error: {base.get('status_msg', data)}")
                                return
                            if "extra_info" in data:
                                # Final chunk; loop will end naturally.
                                continue

                            audio_hex = ((data or {}).get("data") or {}).get("audio") or ""
                            if not audio_hex:
                                continue
                            try:
                                pcm = bytes.fromhex(audio_hex)
                            except ValueError:
                                continue
                            if not pcm:
                                continue

                            if not usage_started:
                                await self.start_tts_usage_metrics(text)
                                usage_started = True
                            await self.stop_ttfb_metrics()

                            # 重切为 wheel chunk_size（tts_service.py:417：0.5s 音频/块，
                            # 24kHz 即 24000 字节）；or 960 为历史兜底。bot.py 原注释
                            # 写「20ms/960」与 1.4.0 属性不符——行为以现状为准（u-101-report）。
                            chunk_bytes = self.chunk_size or 960
                            for i in range(0, len(pcm), chunk_bytes):
                                yield TTSAudioRawFrame(
                                    audio=pcm[i : i + chunk_bytes],
                                    sample_rate=self._audio_sample_rate,
                                    num_channels=1,
                                    context_id=context_id,
                                )
            except Exception as e:
                yield ErrorFrame(error=f"MiniMax stream failed: {e}")
                return

    owns = session is None
    if owns:
        import aiohttp

        session = aiohttp.ClientSession()
    settings = MiniMaxTTSSettings(model=model, voice=voice)
    settings.language_boost = language_boost
    return _MiniMaxTtsBridge(
        owns_session=owns,
        api_key=api_key,
        group_id=group_id,
        base_url=base_url,
        aiohttp_session=session,
        sample_rate=sample_rate,
        stream=True,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# registry 工厂构建器（TTS_PROVIDER=minimax_tts；与 azure_speech 并存，二选一显式解析）
# ---------------------------------------------------------------------------


def make_tts_factory(
    *,
    api_key: str,
    tts_sample_rate: int,
    group_id: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    voice_lookup: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
    service_factory: Callable[..., Any] | None = None,
) -> Callable[[AppConfig], MiniMaxSpeechSynthesizer]:
    # _build_tts 同源 env 语义（bot.py:1791-1792）：未显式给值才读环境变量
    if group_id is None:
        group_id = os.environ.get("MINIMAX_GROUP_ID", "")
    if base_url is None:
        base_url = os.environ.get("MINIMAX_BASE_URL", DEFAULT_MINIMAX_BASE_URL)

    def factory(config: AppConfig) -> MiniMaxSpeechSynthesizer:
        return MiniMaxSpeechSynthesizer(
            api_key=api_key,
            voice=config.tts_voice,
            sample_rate=tts_sample_rate,
            model=model,
            group_id=group_id,
            base_url=base_url,
            voice_lookup=voice_lookup,
            service_factory=service_factory,
        )

    return factory


def register(
    registry: ProviderRegistry,
    *,
    api_key: str,
    tts_sample_rate: int,
    group_id: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    voice_lookup: Callable[[], Mapping[str, Mapping[str, Any]]] | None = None,
    service_factory: Callable[..., Any] | None = None,
) -> None:
    """注册 TTS_PROVIDER=minimax_tts（与 azure_speech 并存，无默认无回退 R-C-002）。"""
    registry.register(
        ProviderKind.TTS,
        TTS_REGISTRY_NAME,
        make_tts_factory(
            api_key=api_key,
            tts_sample_rate=tts_sample_rate,
            group_id=group_id,
            base_url=base_url,
            model=model,
            voice_lookup=voice_lookup,
            service_factory=service_factory,
        ),
    )
