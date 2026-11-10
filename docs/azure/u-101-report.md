# 自测记录 · U-101 Azure Speech STT/TTS 适配器（FC-2 + FC-4）+ MiniMax FC-4（G-2 裁决条件件）

批次：任务 #9（U-101/U-102/U-104）· 实现：fullstack-dev · 日期：2026-11-09
冻结卡：`docs/azure/implementation-plan.md` §5 Phase B U-101；补充派令（team-lead 2026-11-09）：G-2 用户审批 MiniMax 保留，条件件要建。

## 一、验收标准逐条自测

| # | 验收标准 | 自测方法 | 结果 | 证据 |
|---|---|---|---|---|
| 1 | `providers/speech/azure_speech.py` 按 `docs/azure/pipecat-1.4.0-api-notes.md` 包装 1.4.0 Azure services 实现 FC-2/FC-4 | 代码走查 + fixture 测试 17 条；桥接子类缝位逐一对 api-notes §1.1/§1.3 轮源行号核注（stt.py:334-387 回调路径、tts_service.py:532-552/:1406 任务缝） | ✅ | `tests/test_azure_speech.py` 17 passed；缝位注记见 `_default_stt_service_factory`/`_default_tts_service_factory` 桥 docstring |
| 2 | registry 条目 `STT_PROVIDER=azure_speech` / `TTS_PROVIDER=azure_speech` | `register()` 双 kind 注册 + `reg.create` 出契约实例 | ✅ | `test_register_registers_azure_speech_for_stt_and_tts` |
| 3 | `supports_language_detection` 按适配器现实声明并钉死 | AzureSTTService 单 language 构造 → 声明 False，测试钉死 | ✅ | `test_recognizer_declares_no_language_detection` |
| 4 | §11 配置值驱动（SPEECH_REGION/SPEECH_ENDPOINT/STT_LOCALE/TTS_VOICE） | `make_*_factory` 映射测试 + 缺失时响亮报错指名变量 | ✅ | `test_make_factories_map_config_into_service_kwargs`、`test_registered_factory_reads_speech_config_loudly` |
| 5 | fixture 驱动测试（无真实 Azure/MiniMax） | 全部测试走 service_factory 注入 fake；零网络 | ✅ | 4 个测试文件全绿且无网络调用（桥懒加载，本环境无 azure SDK 亦全通过） |
| 6 | 配置项缺失 → 停下报告、走 CR（不发明方案） | 两个 CR 缺口如实记录于 §五，未在代码里发明凭据方案 | ✅ | 本报告 §五 |
| 7 | （条件件，G-2 批准后）MiniMax FC-4 适配器，新注册名，行为保真 | `providers/speech/minimax_tts.py` 23 条测试，含与 bot.py 遗留实现的逐输入等价钉 | ✅ | `tests/test_minimax_tts.py` 23 passed；§四逐项对照 |

## 二、异常场景自测

| 场景 | 期望 | 结果 |
|---|---|---|
| cancel() 打断在途合成 | 代际不匹配即停发（含终块），下一回合照常 | ✅ `test_fc4_cancel_aborts_inflight_and_next_turn_works` / `test_cancel_aborts_inflight_stream_and_next_turn_works` |
| 服务产出错误帧 | RuntimeError 带适配器标签与错误文本 | ✅ 两适配器各 1 条 |
| 空文本流 / 空白文本 / 空音频帧 | 不产出 / 跳过、不触达服务 | ✅ 各 1 条 |
| stop() 幂等、push_audio 早于 start、双重 start | 哨兵幂等 / 响亮报错 | ✅ `test_events_raise_service_errors_and_stop_is_idempotent` 等 3 条 |
| MiniMax：HTTP 请求抛错 / HTTP != 200 / 流中断 | 三段错误逐字保真（request failed / HTTP nnn: body[:200] / stream failed） | ✅ `test_wire_error_paths_request_http_and_stream` |
| MiniMax：base_resp.status_code != 0 | 响亮报错带 status_msg（而非静默零音频） | ✅ `test_wire_base_resp_error_is_surfaced` |
| MiniMax：畸形 JSON / 畸形 hex SSE 块 | 跳过不崩，后续好块正常出声 | ✅ `test_wire_malformed_json_and_hex_blocks_are_skipped` |
| MiniMax：空 GroupId | 剥 `?GroupId=` 查询串（未充值账户 bug 规避） | ✅ `test_wire_payload_shape_and_yue_pronunciation_fix`（URL 断言） |
| registry：遗留名 minimax（含大小写变体） | 永不解析（casefold 双保险），报 UnsupportedProviderError | ✅ `test_register_coexists_with_azure_speech_and_legacy_name_stays_refused` |
| MiniMax：api_key 为空 / sample_rate 越契约 | 响亮报错（指名注入通道 / FC-0 契约） | ✅ `test_constructor_loud_errors` |

## 三、涉及文件清单

**新增**
- `providers/speech/azure_speech.py` — FC-2 `AzureSpeechRecognizer` + FC-4 `AzureSpeechSynthesizer` + 桥接子类 + `make_stt_factory`/`make_tts_factory`/`register`
- `providers/speech/minimax_tts.py` — MiniMax FC-4（条件件，注册名 `minimax_tts`）
- `providers/speech/_tts_stream.py` — FC-4 合成驱动循环单点（is_final 前瞻 + 代际守卫）
- `providers/speech/__init__.py` — 包 docstring（懒加载说明）
- `tests/test_azure_speech.py`（17）· `tests/test_minimax_tts.py`（23）
- 本报告

**修改**
- `providers/speech/azure_speech.py` `_synthesize` 内循环 → 委托 `_tts_stream.stream_synthesis`（契约面零变化；理由：U-101 卡内两适配器共享同一驱动循环语义，is_final/cancel 竞态只应有一份实现；17 条既有测试为回归钉）

**复用（确认未新增重复实现）**
- `providers/base.py`（FC-0/FC-4 契约，只读）· `providers/registry.py`（注册语义，只读）
- `config_contract.py` AppConfig（只读）· 测试 BASE_ENV/`_config` 模式取自 `tests/test_provider_registry.py` 先例
- MiniMax 线协议 = bot.py `SimpleMiniMaxTTSService.run_tts`（:622-736）**逐行迁入**（迁移语义，非重复实现；bot.py 遗留路径未动）；`MINIMAX_VOICES` 等常量为 bot.py:501-604 逐字迁移

**未动（卡外）**：`bot.py`、`mcp_sigv4.py`、`mcp_config.py`、`tools/`、`providers/base.py`、`providers/registry.py`、`config_contract.py`、requirements/lock。

## 四、MiniMax 补充派令覆盖（逐条）

1. **新注册名 `minimax_tts`，绝不用遗留名** ✅ — `TTS_REGISTRY_NAME = "minimax_tts"`；`registry.py:71-73` `_LEGACY_AWS_NAMES` 对 `minimax`（casefold 变体含 `MiniMax`/`MINIMAX`）在 `_resolve` 永拒，测试钉 3 个变体全部 `UnsupportedProviderError`。
2. **行为保真：全部现有 MiniMax TTS 语义迁入 FC-4** ✅ —
   - 线协议：`SimpleMiniMaxTTSService.run_tts`（bot.py:622-736）逐行迁入桥 `run_tts`（SSE 解析 / GroupId 修复 / base_resp 报错 / hex 重切 / 返/(faan1) 修正 / 三段错误）；经伪 aiohttp 传输驱动**真实迁入代码**测试（payload 逐字段断言）。
   - 音色：`MINIMAX_VOICES`（逐字）/`DEFAULT_MINIMAX_VOICE`/`DEFAULT_MINIMAX_VOICE_BY_LANG`/`_resolve_minimax_voice`（含 boost、永不失败回退）→ `resolve_minimax_voice()`。
   - 模型：`MINIMAX_MODELS`/`DEFAULT_MINIMAX_MODEL`（`MINIMAX_MODEL_DEFAULT` env）→ `resolve_model()`。
   - 构造形态：`_build_tts`（bot.py:1757-1797）的 `MiniMaxTTSSettings(model, voice)` + `language_boost` 注入 + `stream=True` + audio_setting 128k/pcm/单声道。
   - **等价钉**：`test_voice_tables_and_resolution_match_legacy_bot` 导入 bot.py，四张表逐字相等 + 解析函数在 key×lang 组合（含 None/空/野值）上逐输入等价。
   - `tools/registry` 输出形状未动（bot.py 表为 UI 面，遗留路径保留）。
3. **registry 并存 + TTS_PROVIDER 二选一显式解析（无默认无回退）** ✅ — `test_register_coexists_with_azure_speech_and_legacy_name_stays_refused`：STT=azure_speech、TTS=azure_speech、TTS=minimax_tts 同表共存，按 `TTS_PROVIDER` 显式解析（R-C-002 语义在 registry 层已冻结，本卡只做注册）。
4. **MINIMAX_API_KEY 注入点可被 Key Vault 接管** ✅ — api_key 只经构造/工厂参数（`make_tts_factory(api_key=...)`），适配器内**不读**环境变量；`test_wire_key_comes_from_parameter_not_env` 钉死 env key 被忽略、显式参数进 Authorization 头。Key Vault 迁移本身属 #17/P1，未做（卡外）。
5. **models[].label 改名（#16/#17）** ✅ 未动 — 本卡零 tools/registry 改动。

## 五、CR 缺口与派单偏差（如实记录，未发明方案）

**CR 缺口（补 §11 变量属 CR）**
1. §11 30 个冻结变量里**无 SPEECH_KEY / LLM_API_KEY / MINIMAX_API_KEY 类凭据占位符** → 三类凭据均走工厂构造注入（`make_*_factory(api_key=...)`），错误信息指明注入通道与 Key Vault 接管点。补占位符或 secret-name 引用属 CR 裁定。
2. `pipecat-ai[azure]`（azure-cognitiveservices-speech）不在部署钉（api-notes §1.1/§1.2 LIVE import FAILED）→ 桥接类懒加载；本环境无法 live wrap Azure 服务，行为测试全走 service_factory 注入的 fixture（卡要求的 fixture-driven 正好覆盖）。live wrap 的可验证性在部署钉补 SDK 后由 U-103/U-501 承接。

**与派单措辞的偏差（冻结卡为准）**
1. 派单 `providers/stt/`（仅 FC-2）vs 冻结卡 `providers/speech/azure_speech.py`（FC-2+FC-4）→ 按卡落盘。
2. 派单 `AZURE_LOCATION` vs §11 `SPEECH_REGION`/`SPEECH_ENDPOINT` → 按 §11。
3. 派单「optional MiniMax」条件 → G-2 已批准，按补充派令建 `minimax_tts`。

## 六、实现发现（评审注意点）

1. **chunk_size 注释与实际不符（保真保留行为）**：bot.py:724 注释写「20ms @ 24 kHz = 960 bytes」，但 1.4.0 的 `chunk_size` 是属性（tts_service.py:417 = sample_rate×0.5s×2 字节，24kHz = 24000 字节/块），`or 960` 实际不触发。迁入行 `chunk_bytes = self.chunk_size or 960` **逐字保留**（行为现状为准），适配器内注释改为如实描述，测试钉 30208 字节 → 24000+6208 切分。是否改回 20ms 切分属产品/评审决定，不在本卡。
2. **SSE 尾块 quirk（保真保留）**：块解析只在遇到下一个 `data:` 标记时闭合——流末未闭合块不解析（bot.py 既有行为：MiniMax 终止块 extra_info 恰好因此被忽略，无音频损失）。测试显式钉住（单块流零音频；补尾标记即出声）。
3. **wheel settings 合并**：`MiniMaxTTSSettings(model, voice)` 经 wheel `__init__` 的 `apply_update` 合并后，speed/volume/pitch 取 wheel 缺省 1.0/1.0/0（bot.py 生产同路径）——payload 测试逐字段钉死。
4. **独立驱动安全性**：`@traced_tts` 无 tracing 时是 no-op（service_decorators.py:176-190）；`start_tts_usage_metrics`/`stop_ttfb_metrics` 的指标帧被桥的 no-op `push_frame` 吞掉——原调用保留（行为保真），独立驱动无副作用。
5. **活表语义缝**：`voices_for("minimax")` 活 VOICE_STORE 优先语义经 `voice_lookup` 注入缝保留（providers 层不导入 bot.py）；U-103 接线时传 `lambda: voices_for("minimax")` 即恢复活表，缺省常量表行为与 bot.py 非 live 时一致。

## 七、自测命令与结果

```
.venv/bin/python -m pytest tests/test_azure_speech.py -q --capture=sys          → 17 passed
.venv/bin/python -m pytest tests/test_minimax_tts.py -q --capture=sys           → 23 passed（1 warning：bot.py 导入的 AudioContextTTSService 预存弃用告警）
.venv/bin/python -m pytest <U-101/102/104 四文件 + 5 Phase A + test_mcp_runtime> -q --capture=sys → 234 passed, 1 warning
.venv/bin/python -m pytest tests/ -q --capture=sys                              → 6 failed, 966 passed, 266 warnings（206s）
```

全量 6 红全部为**预存**（与 phase-a-report.md §1.4 分类逐条对上，零本批文件参与）：
- `test_history.py:139/:195` AttributeError 'HistoryRecorder' has no 'finalize'（C4 预存，报告原文逐字相同）
- `test_demo_store.py:167` / `test_migrate_demos.py:199` 工具 ID 集断言漂移（C4 预存：`{'asr_filter','kb_ack',…}` vs `{'filler','gb_intro',…}`）
- `test_tool_filler.py:286` / `test_mcp_integration.py:223` `FunctionCallParams.__init__() missing 'pipeline_worker'`（C2 预存 API 漂移，api-notes §4 已录；C2 fixture 修复按 verification-phase-a.md:21 条件 2「不晚于 Phase B exit」另有归属，不在本卡）

## 八、未覆盖项 / 遗留问题

1. Azure 桥接子类（`_AzureSttBridge`/`_AzureTtsBridge`）在本环境无法 live 执行（azure SDK 不在钉）——缝位已对轮源逐行核注，live wrap 验证推迟到部署钉补 SDK 后（U-103/U-501）。
2. MiniMax 桥已 live 执行（wheel 无额外 SDK 依赖），含 `TTSService.start/stop` 任务缝实跑通过。
3. 凭据占位符 CR 缺口（§五.1）待 CR 裁定；`MINIMAX_MODEL_DEFAULT`/`MINIMAX_GROUP_ID`/`MINIMAX_BASE_URL` env 语义按 `_build_tts` 原样保留。
4. `voices_for` 活表接线（`voice_lookup`）待 U-103 组合根传入；不接线则退化为常量表（与 bot.py 非 live 行为一致）。
