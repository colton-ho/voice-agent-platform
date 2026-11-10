"""U-102 — Azure OpenAI LLM adapter (FC-3) over the approved endpoint.

冻结卡（implementation-plan §5 U-102）：providers/llm/azure_openai.py 由
LLM_PROVIDER/LLM_ENDPOINT/LLM_DEPLOYMENT/LLM_API_VERSION 驱动 FC-3，声明
LLM_API_VERSION 是否必需（registry hook per U-003 矩阵），测试只走 fixture。

包装对象选型：pipecat 1.4.0 AzureLLMService 本身就是 openai.AsyncAzureOpenAI
的薄封装（api-notes §1.4，ctor api_key/endpoint/model/api_version 直映）；FC-3 的
stream()/cancel()/usage() 语义又与 pipecat 管道帧机器（LLMContext/run_llm）不同，
故直接驱动同一个 AsyncAzureOpenAI 客户端（1:1 映射 LLM_ENDPOINT→azure_endpoint、
LLM_DEPLOYMENT→model、LLM_API_VERSION→api_version），不引 pipecat 管道、不引
Azure SDK。客户端可注入（client / client_factory）——fixture 驱动测试的缝。

D-4 姿态（冻结）：模型/部署零编译默认值。api-version 同样零默认——不采用 wheel
ctor 的 api_version='2024-09-01-preview' 默认值，缺失即响亮报错并指名变量；
requires_api_version=True 是给 U-003 registry hook 的声明。

CR 缺口（同 U-101，报告走 CR）：§11 无 LLM_API_KEY 类凭据占位符 → api_key 走
工厂构造注入（make_llm_factory(api_key=...)）。

工具调用硬要求（用户裁定）：工具参数必须校验、且调用方确认后才执行——本适配器
只留 validate_tool_request 校验钩子（jsonschema 对 ToolSpec.parameters_schema），
绝不执行工具；执行前确认归组合根（U-103）。
"""
from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from config_contract import AppConfig
from providers.base import (
    ChatMessage,
    LanguageModel,
    ModelEvent,
    ToolRequest,
    ToolSpec,
    UsageReport,
)
from providers.registry import ProviderKind, ProviderRegistry

LLM_REGISTRY_NAME = "azure_openai"

# F3 重試預算釘死（mapping §1.4）：瞬時錯誤 ≤2 次、5s 超時、流中不重試。不釘會隨
# openai SDK/azure-core 默認漂移（vendored pipecat :126–127 參考是唯一事實源）。
RETRY_MAX_RETRIES = 2
RETRY_TIMEOUT_SECS = 5.0

# A-6 能力位表（F5/F6，裁決：逐部署能力位、禁全局開）。語義二元——「是否傳顯式
# caching 參數」+「max_tokens 參數名」。gpt 系 Azure 自動 caching 無顯式開關；
# claude 系顯式標記（cachePoint 等價物 hosted-on-Azure 支持度 [UNVERIFIED]
# V1/V10-①，缺則按 §1.1 退路規則逐部署降級，禁全局關）。V10-⑦：gpt 系
# max_completion_tokens 參數名 [UNVERIFIED]，CR-C2 live smoke 核。
_CAPS_BY_FAMILY = {
    "claude-*": {"explicit_prompt_caching": True, "max_tokens_param": "max_tokens"},
    "gpt-*": {"explicit_prompt_caching": False, "max_tokens_param": "max_completion_tokens"},
}
_CAPS_DEFAULT = {"explicit_prompt_caching": False, "max_tokens_param": "max_tokens"}


def family_caps(model: str) -> dict:
    import fnmatch

    for pattern, caps in _CAPS_BY_FAMILY.items():
        if fnmatch.fnmatch(model, pattern):
            return caps
    return _CAPS_DEFAULT


def resolve_deployment(model: str) -> str:
    """裁決 A-1：模型名→部署的解析封裝在此單點（部署=模型名命名約定）。

    CR-C2 live smoke 對實資源核驗解析實效（V10-⑤）；若實資源部署命名不同，
    只改這一個函數——這正是「解析歸適配器內部」的落點。"""
    return model


class ToolRequestError(Exception):
    """工具请求未过校验（未知工具名 / 参数不合 parameters_schema / 参数非 JSON 对象）。"""


def _require(value: str | None, name: str, hint: str) -> str:
    v = (value or "").strip()
    if not v:
        raise ValueError(f"{name}: empty — {hint}")
    return v


class AzureOpenAILLM(LanguageModel):
    """FC-3 over openai.AsyncAzureOpenAI（chat.completions streaming）。"""

    supports_tool_calls = True
    # U-003 矩阵 hook：本适配器把 LLM_API_VERSION 声明为必需（见模块注 D-4）
    requires_api_version = True

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        deployment: str,
        api_version: str,
        client: Any = None,
        model: str | None = None,
        token_provider: Callable[[], Any] | None = None,
    ) -> None:
        self._api_key = (api_key or "").strip()
        # key 常態必需（U-102 CR gap 響亮姿態，client 注入不豁免）；唯一豁免=
        # token_provider（MI 無密鑰，裁決 A-3 耦合旗 B5/V4）
        if token_provider is None and not self._api_key:
            raise ValueError(
                "api_key: empty — Azure OpenAI key required (no §11 credential "
                "placeholder yet — U-102 CR gap; inject via "
                "make_llm_factory(api_key=...) or pass token_provider)"
            )
        self._endpoint = _require(endpoint, "LLM_ENDPOINT", "Azure OpenAI endpoint required (§11)")
        self._api_version = _require(
            api_version, "LLM_API_VERSION",
            "declared required by requires_api_version (U-003 matrix hook, D-4 zero default)",
        )
        # 裁決 A-1：model（Foundry 模型名）給定時，部署經內部解析單點得出；未給
        # 則退 LLM_DEPLOYMENT 錨面（D-4：部署零默認，缺失響亮報錯）
        self._model = model
        if model is not None:
            self._deployment = resolve_deployment(model)
        else:
            self._deployment = _require(deployment, "LLM_DEPLOYMENT", "deployment name required (§11, D-4 zero default)")
        self._client = client if client is not None else _build_client(
            self._api_key, self._endpoint, self._api_version, token_provider=token_provider
        )
        self._usage: UsageReport | None = None
        self._generation = 0
        self._active_stream: Any = None

    # -- FC-3 ---------------------------------------------------------------

    def stream(
        self,
        *,
        messages: Sequence[ChatMessage | dict],
        tools: Sequence[ToolSpec] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelEvent]:
        return self._stream_events(messages=messages, tools=tools, max_tokens=max_tokens)

    async def complete(
        self, *, messages: Sequence[ChatMessage | dict], max_tokens: int | None = None
    ) -> str | None:
        """One-shot：FC-3 stream 收集（L2/L3 調用面，mapping §1.4）。"""
        chunks: list[str] = []
        async for ev in self.stream(messages=messages, max_tokens=max_tokens):
            if ev.kind == "delta" and ev.text:
                chunks.append(ev.text)
        return "".join(chunks) or None

    async def cancel(self) -> None:
        self._generation += 1
        stream, self._active_stream = self._active_stream, None
        if stream is not None:
            with contextlib.suppress(Exception):
                stream.close()

    def usage(self) -> UsageReport:
        if self._usage is None:
            # loud pre-stream：usage() 只报已完成流的缓存计数，不猜、不补零
            raise RuntimeError(
                "usage() before any completed stream — no provider counters cached yet"
            )
        return self._usage

    # -- 校验钩子（用户硬要求：参数校验 + 调用方确认后才执行） ----------------

    def validate_tool_request(
        self, *, tool_request: ToolRequest, tools: Sequence[ToolSpec] | None
    ) -> None:
        """校验工具请求参数；只校验不执行，执行确认归调用方（U-103 组合根）。"""
        import jsonschema  # 懒加载：锁内传递依赖（requirements.lock jsonschema==4.26.0）

        spec = next((t for t in (tools or []) if t.name == tool_request.name), None)
        if spec is None:
            raise ToolRequestError(
                f"tool {tool_request.name!r} is not in the advertised ToolSpec list"
            )
        try:
            jsonschema.validate(instance=tool_request.arguments, schema=spec.parameters_schema)
        except jsonschema.ValidationError as e:
            raise ToolRequestError(
                f"tool {tool_request.name!r} arguments violate parameters_schema: {e.message}"
            ) from e

    # -- 内部 ---------------------------------------------------------------

    async def _stream_events(
        self, *, messages: Sequence[ChatMessage | dict], tools: Sequence[ToolSpec] | None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelEvent]:
        generation = self._generation
        payload: dict[str, Any] = {
            "model": self._deployment,
            "messages": [_to_openai_message(m) for m in messages],
            "stream": True,
            # FC-3 usage() 依赖 usage 块；api-version 不支持 stream_options 会在请求期响亮失败
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = [_to_openai_tool(t) for t in tools]

        caps = family_caps(self._model or self._deployment)
        if max_tokens is not None:
            # A-7/F6：256/512/4096/128 原值照搬；參數名差異（V10-⑦）在適配層吸收
            payload[caps["max_tokens_param"]] = max_tokens
        if caps["explicit_prompt_caching"]:
            # F5 能力位語義=傳顯式 caching 參數：Anthropic cache_control 標記經
            # OpenAI 兼容面透傳（擴展鍵）。等價物支持度 [UNVERIFIED]（V1/V10-①），
            # CR-C2 live smoke 核；缺則逐部署降級（§1.1），禁全局關。
            payload["messages"] = [_with_cache_marker(m) for m in payload["messages"]]
            if payload.get("tools"):
                payload["tools"] = [
                    {**t, "cache_control": {"type": "ephemeral"}} for t in payload["tools"]
                ]

        stream = await self._client.chat.completions.create(**payload)
        self._active_stream = stream
        tool_acc: dict[int, dict[str, Any]] = {}
        usage_event: ModelEvent | None = None
        try:
            try:
                async for chunk in stream:
                    if self._generation != generation:
                        return  # cancel() 已中止本代流
                    if chunk.usage is not None:
                        usage_event = ModelEvent(kind="usage", usage=_to_usage(chunk.usage, self._deployment))
                        continue
                    for choice in chunk.choices or []:
                        delta = choice.delta
                        if delta.content:
                            yield ModelEvent(kind="delta", text=delta.content)
                        for tc in delta.tool_calls or []:
                            acc = tool_acc.setdefault(
                                tc.index, {"id": None, "name": None, "args": []}
                            )
                            if tc.id:
                                acc["id"] = tc.id
                            if tc.function.name:
                                acc["name"] = tc.function.name
                            if tc.function.arguments:
                                acc["args"].append(tc.function.arguments)
            except Exception:
                # cancel() 关流会让在途 __anext__ 报错——按取消收场，不当作流错误
                if self._generation != generation:
                    return
                raise
            if self._generation != generation:
                return  # cancel() 在流末段（自然耗尽后）中止：不再补发收尾事件
            # 事件序：delta* → tool_request* → usage → end（usage 块在流末，缓冲后置以定序）
            for index in sorted(tool_acc):
                yield ModelEvent(
                    kind="tool_request", tool_request=_to_tool_request(index, tool_acc[index])
                )
            if usage_event is not None:
                self._usage = usage_event.usage
                yield usage_event
            yield ModelEvent(kind="end")
        finally:
            if self._active_stream is stream:
                self._active_stream = None
            with contextlib.suppress(Exception):
                stream.close()


def _to_tool_request(index: int, acc: dict[str, Any]) -> ToolRequest:
    if not acc["id"]:
        raise ToolRequestError(f"tool call stream at index {index} carries no id")
    if not acc["name"]:
        raise ToolRequestError(f"tool call {acc['id']!r} carries no function name")
    joined = "".join(acc["args"]) or "{}"
    try:
        arguments = json.loads(joined)
    except json.JSONDecodeError as e:
        raise ToolRequestError(f"tool {acc['name']!r} arguments are not valid JSON: {e}") from e
    if not isinstance(arguments, dict):
        raise ToolRequestError(f"tool {acc['name']!r} arguments must be a JSON object, got {type(arguments).__name__}")
    return ToolRequest(id=acc["id"], name=acc["name"], arguments=arguments)


def _with_cache_marker(m: dict) -> dict:
    if m.get("role") != "system" or not isinstance(m.get("content"), str):
        return m
    return {
        **m,
        "content": [
            {"type": "text", "text": m["content"], "cache_control": {"type": "ephemeral"}}
        ],
    }


def _to_openai_message(m: ChatMessage | dict) -> dict[str, Any]:
    if isinstance(m, dict):
        # 橋接側工具連貫消息（assistant.tool_calls 等）FC-3 型表不達——raw 直通
        return dict(m)
    d: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.tool_call_id is not None:
        d["tool_call_id"] = m.tool_call_id
    if m.name is not None:
        d["name"] = m.name
    return d


def _to_openai_tool(t: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters_schema,
        },
    }


def _to_usage(raw: Any, deployment: str) -> UsageReport:
    counters = raw.model_dump()  # openai CompletionUsage——原样保留提供方计数
    return UsageReport(
        provider="azure_openai",
        model=deployment,
        input_units=counters["prompt_tokens"],
        output_units=counters["completion_tokens"],
        raw=counters,
    )


def _build_client(
    api_key: str, endpoint: str, api_version: str,
    token_provider: Callable[[], Any] | None = None,
):
    from openai import AsyncAzureOpenAI  # 懒加载：openai 是 pipecat 基础依赖（锁内 2.54.0）

    # api-notes §1.4：AzureLLMService 内部同款客户端构造（azure_endpoint/api_version 直映）
    # F3 重試預算在此釘死（RETRY_*），不隨 SDK 默認漂移
    common: dict[str, Any] = {
        "azure_endpoint": endpoint,
        "api_version": api_version,
        "max_retries": RETRY_MAX_RETRIES,
        "timeout": RETRY_TIMEOUT_SECS,
    }
    if token_provider is not None:
        # B5/V4（裁決 A-3 耦合旗）：LLM 側 MI，azure-identity token provider，無密鑰
        return AsyncAzureOpenAI(azure_ad_token_provider=token_provider, **common)
    return AsyncAzureOpenAI(api_key=api_key, **common)


# ---------------------------------------------------------------------------
# MI 構造面（#16 裁決 A-3 耦合旗 B5/V4）+ registry 工厂构建器（U-102 交付物）
# ---------------------------------------------------------------------------


def make_managed_llm(
    *, model: str | None = None, env: Mapping[str, str] | None = None
) -> AzureOpenAILLM:
    """Managed-Identity 構造面（B5/V4）：LLM 側無密鑰，STT 側凍結憑證另案。

    只讀凍結契約變量 LLM_ENDPOINT/LLM_DEPLOYMENT/LLM_API_VERSION（零新增），
    缺失由 FC-3 ctor D-4 響亮驗證；model 給定時部署解析走 resolve_deployment
    單點（裁決 A-1），未給則退 LLM_DEPLOYMENT 錨。"""
    import os

    from azure.identity import DefaultAzureCredential, get_bearer_token_provider

    e = os.environ if env is None else env
    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default"
    )
    return AzureOpenAILLM(
        api_key="",
        endpoint=e.get("LLM_ENDPOINT") or "",
        deployment=e.get("LLM_DEPLOYMENT") or "",
        api_version=e.get("LLM_API_VERSION") or "",
        model=model,
        token_provider=token_provider,
    )


def make_llm_factory(
    *, api_key: str, client_factory: Callable[..., Any] | None = None
) -> Callable[[AppConfig], AzureOpenAILLM]:
    def factory(config: AppConfig) -> AzureOpenAILLM:
        client = None
        if client_factory is not None:
            client = client_factory(
                api_key=api_key,
                endpoint=config.llm_endpoint,
                api_version=config.llm_api_version,
            )
        return AzureOpenAILLM(
            api_key=api_key,
            endpoint=config.llm_endpoint,
            deployment=config.llm_deployment,
            api_version=config.llm_api_version,
            client=client,
        )

    return factory


def register(registry: ProviderRegistry, *, api_key: str) -> None:
    """注册 azure_openai registry 名（冻结卡 U-102 交付物）。"""
    registry.register(
        ProviderKind.LLM, LLM_REGISTRY_NAME, make_llm_factory(api_key=api_key)
    )
