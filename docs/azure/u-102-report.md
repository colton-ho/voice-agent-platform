# 自测记录 · U-102 LLM 适配器（FC-3，Azure OpenAI）

批次：任务 #9（U-101/U-102/U-104）· 实现：fullstack-dev · 日期：2026-11-09
冻结卡：`docs/azure/implementation-plan.md` §5 Phase B U-102。

## 一、验收标准逐条自测

| # | 验收标准 | 自测方法 | 结果 | 证据 |
|---|---|---|---|---|
| 1 | `providers/llm/azure_openai.py` 实现 FC-3 `LanguageModel`，由 `LLM_PROVIDER/LLM_ENDPOINT/LLM_DEPLOYMENT/LLM_API_VERSION` 驱动 | `make_llm_factory` 从 AppConfig 映射四变量 + `reg.create(LLM, cfg)` 出契约实例 | ✅ | `test_make_llm_factory_maps_client_injection`、`test_register_registers_azure_openai_and_config_mapping_is_loud` |
| 2 | 声明 `LLM_API_VERSION` 是否必填（U-003 矩阵 registry 钩子） | 类属性 `requires_api_version = True`（构造缺 `LLM_API_VERSION` 响亮报错指名变量） | ✅ | `test_class_declarations_match_u003_matrix` |
| 3 | fixture 驱动测试，G-2 选定部署前不打真实端点 | 全部 11 条测试注入 `AsyncAzureOpenAI` 形面 fake client（`chat.completions.create`→stream），零网络 | ✅ | `tests/test_azure_openai.py` 11 passed |
| 4 | 无编译期模型/部署默认值（D-4） | 构造四参数全响亮校验（含空白）；**不**采用轮 `AzureLLMService` 的 `api_version="2024-09-01-preview"` 隐式默认——api_version 缺失即报错 | ✅ | `test_constructor_loud_errors_for_all_four_variables` |
| 5 | tool calls：`supports_tool_calls` + `ToolSpec`/`ToolRequest` 校验钩子，**参数必须校验且调用方确认后才执行**（用户硬性要求） | `supports_tool_calls = True`；`validate_tool_request(*, tool_request, tools)` 用 jsonschema 校验参数、未知工具名/不合 schema 均抛 `ToolRequestError`；适配器只校验、永不执行（模块无执行通道） | ✅ | `test_validate_tool_request_gates`、`test_tool_call_stream_fragment_assembly_and_order` |

## 二、异常场景自测

| 场景 | 期望 | 结果 |
|---|---|---|
| 工具调用参数分片流式到达（id/name/arguments 按 index 断片） | 按 `tc.index` 聚合拼接后出 `tool_request` 事件，JSON 完整 | ✅ `test_tool_call_stream_fragment_assembly_and_order`（断片 `'{"city": "SZ' + '"}'`） |
| 参数非法 JSON / 非对象 / 缺 id | `ToolRequestError` 响亮（不静默丢弃、不带病执行） | ✅ 同测试内 3 个反例 |
| `validate_tool_request`：未知工具名 / 缺必填字段 / 类型不符 | `ToolRequestError` 逐条 | ✅ `test_validate_tool_request_gates` |
| 事件顺序 | `delta* → tool_request* → usage → end` 严格序（usage 缓冲到工具请求后发） | ✅ 顺序断言 `["delta","tool_request","tool_request","usage","end"]` |
| usage() 早于任何完整流 | RuntimeError 响亮（不给假零值） | ✅ `test_usage_cached_from_last_stream_and_loud_before_stream` |
| cancel() 流中 | 关闭底层 stream，代际不匹配即停止补发收尾事件 | ✅ `test_cancel_closes_stream_and_stops_events`（rest == []） |
| tools=None / 空 tools | payload 不带 `tools` 键 | ✅ `test_message_and_tool_payload_mapping` |
| api_version 空/空白/None | 响亮报错指名 `LLM_API_VERSION`（D-4） | ✅ 构造测试 |

## 三、涉及文件清单

**新增**
- `providers/llm/azure_openai.py` — `AzureOpenAILLM(LanguageModel)` + `ToolRequestError` + `validate_tool_request` + `make_llm_factory` + `register`
- `providers/llm/__init__.py` — 包 docstring
- `tests/test_azure_openai.py`（11）· 本报告

**复用（确认未新增重复实现）**
- `providers/base.py` FC-0/FC-3 契约、`providers/registry.py` 注册语义（只读）；`config_contract.py` AppConfig（只读）
- jsonschema 校验用已锁定的传递钉 jsonschema 4.26.0（懒加载），未新增依赖
- 驱动面直接用 `openai.AsyncAzureOpenAI`（`AzureLLMService` 是其薄封装，api-notes §1.4）——不引 Azure SDK、不引 pipecat 管道机器

**未动（卡外）**：`bot.py`、`providers/base.py`、`providers/registry.py`、`config_contract.py`、requirements/lock、tools/。

## 四、实现要点（契约对照）

1. **流语义**：`stream(*, messages, tools=None)` → `ModelEvent`；payload `{model: LLM_DEPLOYMENT, messages, stream: True, stream_options: {include_usage: True}}`；消息映射保 `role/content/tool_call_id/name`（FC-0 `ChatMessage` 全字段）；工具 schema 按 OpenAI `{"type":"function","function":{name,description,parameters}}` 形状组装（与 `tools/registry.assemble_tools_schema` 同形状）。
2. **工具调用**：`tc.index` 分片聚合（id/name/arguments 增量拼接）；聚合完成后 `_to_tool_request` 校验 id/name 非空、arguments 为合法 JSON **且为对象**，违者 `ToolRequestError`。**执行权在调用方**：适配器只产 `ToolRequest` + 提供 `validate_tool_request` 钩子，调用方校验并确认后自行执行（用户硬性要求，模块内无任何执行通道）。
3. **usage**：`stream_options.include_usage` 取末 chunk 计数，`UsageReport(provider="azure_openai", model=LLM_DEPLOYMENT, prompt/completion tokens, raw=model_dump 全量)`，缓存为 `usage()` 返回值。
4. **cancel**：generation 代际 + `stream.close()`（openai `AsyncStream.close()` 同步）；流末段 cancel 不补发 usage/end（`finally` 里 close 用 `contextlib.suppress` 防二次打扰）。
5. **D-4 姿态**：`api_version` 无任何编译期默认（轮 `AzureLLMService` 的 `2024-09-01-preview` 默认被刻意拒绝采用）；`LLM_DEPLOYMENT` 即 payload 的 `model`，无回退。

## 五、CR 缺口（如实记录，未发明方案）

- §11 无 `LLM_API_KEY` 类凭据占位符（与 U-101 同缺口）→ api_key 经 `make_llm_factory(api_key=...)` 构造注入，错误信息指名注入通道；补占位符属 CR 裁定。
- 真实端点联调（部署/模型名由 G-2 选定）按卡推迟——fixture 驱动已覆盖契约行为。

## 六、自测命令与结果

```
.venv/bin/python -m pytest tests/test_azure_openai.py -q --capture=sys   → 11 passed
批次自测（4 新文件 + 5 Phase A + test_mcp_runtime）                       → 234 passed, 1 warning
全量 tests/ -q --capture=sys                                              → 6 failed（全预存，见 u-101-report §七）, 966 passed
```

## 七、未覆盖项 / 遗留问题

1. 真实 Azure OpenAI 端点联调未做（卡明示 fixture-only 直到 G-2 选定部署）。
2. `requires_api_version = True` 是 U-003 矩阵的声明钩子；矩阵消费方（U-103 启动校验）待接线。
3. 凭据占位符 CR 缺口同 U-101 §五.1。
