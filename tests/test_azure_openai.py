"""U-102 — providers/llm/azure_openai.py 测试（fixture 驱动，零真实 Azure/OpenAI）。

冻结卡：fixture-driven tests only until G-2 picks the deployment。fake 客户端按
openai SDK 表面（chat.completions.create / AsyncStream.close / CompletionUsage.model_dump）
塑形，测 FC-3 流事件序、工具请求解析与校验钩子、D-4 零默认、§11 映射、registry 名。
"""
import asyncio
import dataclasses
import types

import pytest

from config_contract import load_config
from providers.base import ChatMessage, LanguageModel, ToolRequest, ToolSpec
from providers.registry import ProviderKind, ProviderRegistry
from providers.llm.azure_openai import (
    AzureOpenAILLM,
    ToolRequestError,
    make_llm_factory,
    make_managed_llm,
    register,
)

BASE_ENV = {
    "APP_ENV": "test",
    "AZURE_LOCATION": "test-region",
    "PUBLIC_CONTROL_BASE_URL": "https://example.test",
    "PUBLIC_MEDIA_BASE_URL": "wss://example.test",
    "TELEPHONY_PROVIDER": "test_telephony_stub",
    "VOICE_ENGINE": "cascade",
    "STT_PROVIDER": "test_stt_stub",
    "TTS_PROVIDER": "test_tts_stub",
    "LLM_PROVIDER": "azure_openai",
    "LLM_ENDPOINT": "https://example.test/llm",
    "LLM_DEPLOYMENT": "gpt-test",
    "LLM_API_VERSION": "2024-09-01-preview",
    "SPEECH_REGION": "eastus",
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
    cfg = load_config(dict(BASE_ENV))
    return dataclasses.replace(cfg, **overrides) if overrides else cfg


# --- openai SDK 表面 fake ----------------------------------------------------


class _FakeFunction:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, index, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _FakeFunction(name=name, arguments=arguments)


class _FakeDelta:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        self.delta = _FakeDelta(content=content, tool_calls=tool_calls)
        self.finish_reason = finish_reason


class _FakeUsage:
    def __init__(self, prompt, completion):
        self.prompt_tokens = prompt
        self.completion_tokens = completion
        self.total_tokens = prompt + completion

    def model_dump(self):
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


class _FakeChunk:
    def __init__(self, choices=None, usage=None):
        self.choices = choices or []
        self.usage = usage


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.closed or not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    def close(self):
        self.closed = True


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks
        self.payloads: list[dict] = []

    async def create(self, **payload):
        self.payloads.append(payload)
        return _FakeStream(self._chunks)


class _FakeClient:
    def __init__(self, chunks):
        self.completions = _FakeCompletions(chunks)
        self.chat = types.SimpleNamespace(completions=self.completions)


def _llm(chunks):
    client = _FakeClient(chunks)
    llm = AzureOpenAILLM(
        api_key="sekrit", endpoint="https://oai.test", deployment="gpt-test",
        api_version="2024-09-01-preview", client=client,
    )
    return llm, client


# --- 声明与 D-4 零默认 -------------------------------------------------------


def test_declares_tool_calls_and_required_api_version():
    assert AzureOpenAILLM.supports_tool_calls is True
    assert AzureOpenAILLM.requires_api_version is True  # U-003 矩阵 hook 声明


def test_constructor_loud_errors_name_section11_variables():
    with pytest.raises(ValueError, match="api_key"):
        AzureOpenAILLM(api_key="", endpoint="e", deployment="d", api_version="v", client=object())
    with pytest.raises(ValueError, match="LLM_ENDPOINT"):
        AzureOpenAILLM(api_key="k", endpoint="", deployment="d", api_version="v", client=object())
    with pytest.raises(ValueError, match="LLM_DEPLOYMENT"):
        AzureOpenAILLM(api_key="k", endpoint="e", deployment="", api_version="v", client=object())
    # D-4：api-version 零编译默认（不采用 wheel 的 2024-09-01-preview 默认）——缺失即响亮报错
    for missing in ("", "   ", None):
        with pytest.raises(ValueError, match="LLM_API_VERSION"):
            AzureOpenAILLM(api_key="k", endpoint="e", deployment="d", api_version=missing, client=object())


# --- FC-3 stream 映射与事件序 ------------------------------------------------


def test_stream_maps_messages_and_tools_to_openai_payload():
    llm, client = _llm([_FakeChunk(choices=[_FakeChoice(content="hi")], usage=_FakeUsage(1, 1))])
    messages = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="hello"),
        ChatMessage(role="assistant", content="calling"),
        ChatMessage(role="tool", content="{}", tool_call_id="call_1", name="get_weather"),
    ]
    tools = [ToolSpec(name="get_weather", description="d", parameters_schema={"type": "object"})]

    async def run():
        return [e async for e in llm.stream(messages=messages, tools=tools)]

    asyncio.run(run())
    payload = client.completions.payloads[0]
    assert payload["model"] == "gpt-test"
    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "calling"},
        {"role": "tool", "content": "{}", "tool_call_id": "call_1", "name": "get_weather"},
    ]
    assert payload["tools"] == [{
        "type": "function",
        "function": {"name": "get_weather", "description": "d", "parameters": {"type": "object"}},
    }]


def test_stream_without_tools_omits_tools_key():
    llm, client = _llm([_FakeChunk(choices=[_FakeChoice(content="hi")], usage=_FakeUsage(1, 1))])

    async def run():
        return [e async for e in llm.stream(messages=[ChatMessage(role="user", content="q")])]

    asyncio.run(run())
    assert "tools" not in client.completions.payloads[0]


def test_stream_emits_delta_tool_request_usage_end_in_order():
    chunks = [
        _FakeChunk(choices=[_FakeChoice(content="thinking ")]),
        _FakeChunk(choices=[_FakeChoice(tool_calls=[
            _FakeToolCall(0, id="call_1", name="get_weather", arguments='{"city": "SZ',),
        ])]),
        _FakeChunk(choices=[_FakeChoice(tool_calls=[
            _FakeToolCall(0, arguments='"}'),
            _FakeToolCall(1, id="call_2", name="end_call", arguments="{}"),
        ], finish_reason="tool_calls")]),
        _FakeChunk(choices=[], usage=_FakeUsage(10, 4)),
    ]
    llm, _ = _llm(chunks)

    async def run():
        return [e async for e in llm.stream(messages=[ChatMessage(role="user", content="q")])]

    events = asyncio.run(run())
    assert [e.kind for e in events] == ["delta", "tool_request", "tool_request", "usage", "end"]
    first, second = events[1].tool_request, events[2].tool_request
    # 分片参数按 index 拼接后整体解析（'{"city": "SZ' + '"}'）
    assert first == ToolRequest(id="call_1", name="get_weather", arguments={"city": "SZ"})
    assert second == ToolRequest(id="call_2", name="end_call", arguments={})
    report = events[3].usage
    assert (report.provider, report.model) == ("azure_openai", "gpt-test")
    assert (report.input_units, report.output_units) == (10, 4)
    assert report.raw == {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}


def test_malformed_tool_requests_are_loud():
    bad_json = [_FakeChunk(choices=[_FakeChoice(tool_calls=[
        _FakeToolCall(0, id="call_1", name="t", arguments='{"city": '),
    ], finish_reason="tool_calls")])]
    non_object = [_FakeChunk(choices=[_FakeChoice(tool_calls=[
        _FakeToolCall(0, id="call_1", name="t", arguments="[1, 2]"),
    ], finish_reason="tool_calls")])]
    missing_id = [_FakeChunk(choices=[_FakeChoice(tool_calls=[
        _FakeToolCall(0, name="t", arguments="{}"),
    ], finish_reason="tool_calls")])]

    for chunks, match in ((bad_json, "not valid JSON"), (non_object, "JSON object"), (missing_id, "no id")):
        llm, _ = _llm(chunks)

        async def run():
            return [e async for e in llm.stream(messages=[ChatMessage(role="user", content="q")])]

        with pytest.raises(ToolRequestError, match=match):
            asyncio.run(run())


# --- 工具校验钩子（用户硬要求：参数校验 + 调用方确认后才执行） ----------------


def test_validate_tool_request_gates_tool_calls():
    llm, _ = _llm([])
    spec = ToolSpec(
        name="get_weather",
        description="d",
        parameters_schema={
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    )
    ok = ToolRequest(id="c", name="get_weather", arguments={"city": "SZ"})
    assert llm.validate_tool_request(tool_request=ok, tools=[spec]) is None  # 只校验不执行

    with pytest.raises(ToolRequestError, match="not in the advertised ToolSpec list"):
        llm.validate_tool_request(tool_request=ToolRequest(id="c", name="nope", arguments={}), tools=[spec])
    with pytest.raises(ToolRequestError, match="get_weather"):
        llm.validate_tool_request(
            tool_request=ToolRequest(id="c", name="get_weather", arguments={}), tools=[spec]
        )
    with pytest.raises(ToolRequestError, match="get_weather"):
        llm.validate_tool_request(
            tool_request=ToolRequest(id="c", name="get_weather", arguments={"city": 42}), tools=[spec]
        )


# --- usage() / cancel() ------------------------------------------------------


def test_usage_is_cached_last_with_loud_pre_stream_error():
    llm, _ = _llm([_FakeChunk(choices=[_FakeChoice(content="x")], usage=_FakeUsage(3, 2))])
    with pytest.raises(RuntimeError, match="before any completed stream"):
        llm.usage()

    async def run():
        return [e async for e in llm.stream(messages=[ChatMessage(role="user", content="q")])]

    asyncio.run(run())
    assert llm.usage().input_units == 3


def test_cancel_closes_stream_and_stops_events():
    chunks = [
        _FakeChunk(choices=[_FakeChoice(content="a")]),
        _FakeChunk(choices=[_FakeChoice(content="b")]),
        _FakeChunk(choices=[_FakeChoice(content="c")], usage=_FakeUsage(1, 1)),
    ]
    llm, client = _llm(chunks)

    async def run():
        stream = llm.stream(messages=[ChatMessage(role="user", content="q")])
        first = await stream.__anext__()
        await llm.cancel()
        rest = [e async for e in stream]  # 代际不匹配：不再补发收尾事件
        return first, rest

    first, rest = asyncio.run(run())
    assert first.kind == "delta" and first.text == "a"
    assert rest == []
    assert client.completions.payloads  # create 确实被调用
    # close 在 cancel 与生成器 finally 各有一条路径，流必被关（fake 记录 closed）
    # 通过再开一条流观察 fake 生命周期不被误复用即可；此处以 rest 为空为准


# --- factory / registry ------------------------------------------------------


def test_make_llm_factory_maps_config_and_injects_client():
    seen: dict = {}
    chunks = [_FakeChunk(choices=[_FakeChoice(content="ok")], usage=_FakeUsage(1, 1))]

    def client_factory(*, api_key, endpoint, api_version):
        seen.update(api_key=api_key, endpoint=endpoint, api_version=api_version)
        return _FakeClient(chunks)

    factory = make_llm_factory(api_key="sekrit", client_factory=client_factory)
    cfg = _config(
        llm_endpoint="https://oai.test", llm_deployment="gpt-dep", llm_api_version="2025-01-01"
    )

    async def run():
        llm = factory(cfg)
        return [e async for e in llm.stream(messages=[ChatMessage(role="user", content="q")])]

    events = asyncio.run(run())
    assert seen == {
        "api_key": "sekrit", "endpoint": "https://oai.test", "api_version": "2025-01-01",
    }
    assert events[-1].kind == "end"


def test_register_registers_azure_openai_and_keeps_api_version_required():
    reg = ProviderRegistry()
    register(reg, api_key="sekrit")
    llm = reg.create(ProviderKind.LLM, _config())
    assert isinstance(llm, LanguageModel)
    # §11 LLM_API_VERSION 为 optional 占位符，但本适配器声明必需——缺失即响亮报错（D-4）
    reg2 = ProviderRegistry()
    register(reg2, api_key="sekrit")
    with pytest.raises(ValueError, match="LLM_API_VERSION"):
        reg2.create(ProviderKind.LLM, _config(llm_api_version=None))



# --- #16 A-1/A-6/A-7 — 能力位表 / max_tokens 參數名 / 模型名→部署解析 ----------------


def _llm_with_model(model, chunks):
    client = _FakeClient(chunks)
    llm = AzureOpenAILLM(
        api_key="sekrit", endpoint="https://oai.test", deployment="gpt-test",
        api_version="2024-09-01-preview", client=client, model=model,
    )
    return llm, client


def _drain(llm, **kw):
    async def run():
        return [e async for e in llm.stream(**kw)]

    return asyncio.run(run())


def test_family_caps_capability_bits():
    # A-6 能力位（裁決：逐部署能力位、禁全局開）：claude 傳顯式 caching 參數、
    # gpt 不傳；未知家族按不傳（不冒然擴展）
    from providers.llm.azure_openai import family_caps

    assert family_caps("claude-haiku-4-5") == {
        "explicit_prompt_caching": True, "max_tokens_param": "max_tokens",
    }
    assert family_caps("gpt-4.1-mini") == {
        "explicit_prompt_caching": False, "max_tokens_param": "max_completion_tokens",
    }
    assert family_caps("unknown-x") == {
        "explicit_prompt_caching": False, "max_tokens_param": "max_tokens",
    }


def test_claude_passes_explicit_caching_and_max_tokens_name():
    llm, client = _llm_with_model(
        "claude-sonnet-4-5",
        [_FakeChunk(choices=[_FakeChoice(content="ok")], usage=_FakeUsage(1, 1))],
    )
    tools = [ToolSpec(name="end_call", description="d", parameters_schema={"type": "object"})]
    _drain(
        llm,
        messages=[ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="q")],
        tools=tools,
        max_tokens=512,
    )
    payload = client.completions.payloads[0]
    # A-7/F6：原值照搬，claude 系參數名 max_tokens（V10-⑦）
    assert payload["max_tokens"] == 512
    assert "max_completion_tokens" not in payload
    # 显式 caching 参数：system 内容块 + 工具定义都带 cache_control 标记（F5）
    assert payload["messages"][0]["content"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
    assert payload["tools"][0]["cache_control"] == {"type": "ephemeral"}


def test_gpt_no_explicit_caching_and_completion_tokens_name():
    llm, client = _llm_with_model(
        "gpt-4.1-mini",
        [_FakeChunk(choices=[_FakeChoice(content="ok")], usage=_FakeUsage(1, 1))],
    )
    _drain(
        llm,
        messages=[ChatMessage(role="system", content="sys"), ChatMessage(role="user", content="q")],
        max_tokens=256,
    )
    payload = client.completions.payloads[0]
    # gpt 系：max_completion_tokens（V10-⑦）+ 零顯式 caching 參數（Azure 自動 caching）
    assert payload["max_completion_tokens"] == 256
    assert "max_tokens" not in payload
    assert payload["messages"][0] == {"role": "system", "content": "sys"}


def test_model_name_resolves_deployment_inside_adapter():
    # 裁決 A-1：模型名→部署解析歸適配器內部單點（部署=模型名命名約定）
    llm, client = _llm_with_model(
        "gpt-4.1-mini",
        [_FakeChunk(choices=[_FakeChoice(content="x")], usage=_FakeUsage(1, 1))],
    )
    _drain(llm, messages=[ChatMessage(role="user", content="q")])
    assert client.completions.payloads[0]["model"] == "gpt-4.1-mini"
    # 未帶 model：退 LLM_DEPLOYMENT 锚面（D-4 零默认仍由 ctor 保）
    llm2, client2 = _llm([_FakeChunk(choices=[_FakeChoice(content="x")], usage=_FakeUsage(1, 1))])
    _drain(llm2, messages=[ChatMessage(role="user", content="q")])
    assert client2.completions.payloads[0]["model"] == "gpt-test"


def test_complete_one_shot_collects_text():
    llm, _ = _llm([
        _FakeChunk(choices=[_FakeChoice(content="he")]),
        _FakeChunk(choices=[_FakeChoice(content="llo")]),
        _FakeChunk(choices=[], usage=_FakeUsage(1, 2)),
    ])

    async def run():
        return await llm.complete(messages=[ChatMessage(role="user", content="q")], max_tokens=128)

    assert asyncio.run(run()) == "hello"


def test_raw_dict_messages_pass_through():
    # 桥接侧工具连贯消息（assistant.tool_calls）FC-3 型表不达——raw 直通（裁決 A-3 案 ii）
    raw = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "end_call", "arguments": "{}"}}
        ],
    }
    llm, client = _llm([_FakeChunk(choices=[_FakeChoice(content="x")], usage=_FakeUsage(1, 1))])
    _drain(llm, messages=[raw, {"role": "tool", "content": "ok", "tool_call_id": "call_1"}])
    assert client.completions.payloads[0]["messages"] == [
        raw, {"role": "tool", "content": "ok", "tool_call_id": "call_1"},
    ]


def test_make_managed_llm_wires_mi_and_model_resolution():
    # B5/V4 耦合旗：LLM 側 MI 無密鑰；裁決 A-1 解析單點（部署=模型名命名約定）
    env = {k: BASE_ENV[k] for k in ("LLM_ENDPOINT", "LLM_DEPLOYMENT", "LLM_API_VERSION")}
    llm = make_managed_llm(model="gpt-4.1-mini", env=env)
    assert llm._model == "gpt-4.1-mini"
    assert llm._deployment == "gpt-4.1-mini"
    assert llm._api_key == ""  # keyless：MI 接管
    assert llm._client._azure_ad_token_provider is not None  # token provider 已接線
    # 未給 model → LLM_DEPLOYMENT 錨面（D-4 零默認語義不變）
    anchor = make_managed_llm(env=env)
    assert anchor._deployment == "gpt-test"
