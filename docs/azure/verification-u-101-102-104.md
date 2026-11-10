# 独立验收报告 · 任务 #9 批 — U-101/U-102/104 + MiniMax 条件件

- 验收人：验得明（qa-verifier，独立验证）· 日期：2026-11-09
- 验收对象（任务 #9 交付物）：
  - `providers/speech/azure_speech.py`（FC-2 `AzureSpeechRecognizer` + FC-4 `AzureSpeechSynthesizer` + 桥接 + registry）、`providers/speech/_tts_stream.py`（FC-4 共享驱动）、`providers/speech/minimax_tts.py`（MiniMax FC-4 条件件）、`providers/llm/azure_openai.py`（FC-3）
  - `tests/test_azure_speech.py`（17）、`tests/test_azure_openai.py`（11）、`tests/test_minimax_tts.py`（23）、`tests/test_mcp_sigv4.py`（重写，验收窗口内 4→5，见 §四快照动态）
  - 自测记录 `docs/azure/u-101-report.md`、`u-102-report.md`、`u-104-report.md`
- 依据：implementation-plan.md §5 冻结卡 U-101/U-102/U-104 + team-lead MiniMax 补充派令条件件 · spec §5/§11 · `docs/azure/reviews/u-101-102-104-review.md`（**仅作问题面对齐，结论独立下**）
- 环境：`.venv`（Python 3.13.12，pytest 9.2.0 + pytest-asyncio 1.4.1.post1，pipecat-ai==1.4.0，mcp SDK 已按 U-105 重钉）
- 约定：全部 pytest 带 `--capture=sys`；跑前清理 `$TMPDIR` 与 `.venv/tmp` 两处残留 `pytest-of-*`（basetemp 在 `.venv/tmp/pytest-of-*`，不清理则沙箱 shim 报 FileExistsError 假红）；不 commit/push、不触云、不改 AWS 路径

**立场声明**：不采信实现者/评审者的任何报数与结论。下文每条结论均附本人亲自执行的命令与输出摘录（executed），或本人逐行读码核对（read）；自设计对抗用例独立编写，不复用其实现测试代码。

---

## 一、验收结论

### 结论：**PASS-WITH-CONDITIONS**（通过，附 3 项骑带条件）

**「证明它能用」在其声明的 fixture 边界内成立**：4 个交付测试文件 + 5 个 Phase A 文件 + test_mcp_runtime 共 10 文件 **235 passed**（含验收窗口内落地的 F-08 新钉）；全量 **6 failed / 967 passed**，6 红逐条核对全部预存、与本批零因果；我自设计的 **38 条对抗/边界探针全部通过**（MiniMax 线协议真实迁入代码经伪传输实跑、四表与 bot.py 真 import 等价、denylist 三形态双层拒、api_key 构造注入、sigv4 降级信封、FC-2/3/4 生命周期与错误面）；围栏 = 仅 `.env.example` + `tests/test_mcp_sigv4.py` 两个跟踪文件改动（均在声明内）。

骑带条件（均已在跟踪中，不要求本批返工）：
1. **CR-C2**（部署钉缺口）：`azure-cognitiveservices-speech` 不在部署钉，`_AzureSttBridge`/`_AzureTtsBridge` 在本环境**无法 live 执行**——缝位只经轮源行号走查 + 伪服务形测试。live/import-smoke gate 落地（建议并入 U-103/Phase B 收口）前，**不得宣称 Azure 桥 live 可用**。
2. **CR-C1**（§11 凭据占位符缺口）：SPEECH_KEY/LLM_API_KEY/MINIIMAX_API_KEY 三类凭据走工厂构造注入（`make_*_factory(api_key=...)`），错误文案指名注入通道；补 §11 变量（建议 Key Vault secret-name 先例）属 CR 裁定。
3. **C2 fixture 修复**（Phase A 验收条件 2 延续）：`test_tool_filler`/`test_mcp_integration` 的 `FunctionCallParams.pipeline_worker` 漂移修复不晚于 Phase B exit（届时 RETAIN 集成 CI 闸门）。

### 卡面验收条目逐条映射

| 卡 | 验收条目（冻结卡/派令要点） | 证据（我方执行/读取） | 判定 |
|---|---|---|---|
| U-101 | FC-2/FC-4 按 api-notes 包装 1.4.0 Azure services | **read** `azure_speech.py` 全文 vs `providers/base.py:125-164` 方法集逐签名核对（FC-2 `start/push_audio/events/stop`+`supports_language_detection`；FC-4 `synthesize/cancel/output_format`），1:1 实现、仅加法无删改；桥接缝位 docstring 指认 wheel 行号（stt.py:334-387、tts_service.py:532-552/:1406）**read** 对照 api-notes §1.1–§1.3；**executed** 伪服务形探针 a1–a4 实跑适配器行为 | ✅（live 边界见条件 1） |
| U-101 | registry 条目 `azure_speech`（STT+TTS） | **executed** 探针 a8/r3：`register()` 后 `reg.create` 出契约实例（isinstance 过 registry 检查），§11 字段映射逐一断言（region/speech_endpoint/locale/voice） | ✅ |
| U-101 | 能力标志按适配器现实声明 | **executed** 探针 a3：`supports_language_detection is False`（AzureSTTService 单语言构造的诚实声明，不虚报 Transcribe 能力） | ✅ |
| U-101 | fixture 驱动（无真实 Azure） | **executed** 全部探针与套件零网络（桥懒加载，本环境无 azure SDK 亦全绿） | ✅ |
| U-101 条件件 | 新注册名 `minimax_tts`，遗留名永拒 | **executed** 探针 R 组：`minimax/MiniMax/MINIMAX/MiNiMaX/mInImAx` 五形态 **register 即拒** + **resolve 即拒且工厂零调用**（无回退实证）+ 走私直塞 `_factories` 仍被 resolve 层拦；`minimax_tts` 与 `azure_speech` 共存、按 `TTS_PROVIDER` 显式二选一 | ✅ |
| U-101 条件件 | 行为保真（线协议/错误分支） | **executed** M 组 12 探针驱动**真实迁入代码**（`_default_service_factory` 真桥 + 真 wheel 基类）：SSE 跨任意字节边界重装、base_resp!=0 响亮报错（含 status_msg）、hex 重切 48000→[24000,24000] 且 is_final 只落末块、`返/(faan1)` 修正随 boost 开关、GroupId 空/非空 URL 语义（wheel tts.py:310 拼接 + run_tts 剥离）、畸形 JSON/hex 跳过不崩、HTTP≠200 截断 200 字符、post 异常、错误帧经适配器路径抛带标签 RuntimeError、空文本不触服务 | ✅ |
| U-101 条件件 | 四表 + 解析器防漂移 | **executed** T 组：真 import bot.py 对照——`MINIMAX_VOICES`/`DEFAULT_MINIMAX_VOICE`/`DEFAULT_MINIMAX_VOICE_BY_LANG`/`MINIMAX_MODELS`/`DEFAULT_MINIMAX_MODEL` 逐字相等；`resolve_minimax_voice` 12 输入组合（含 None/空串/野值/克隆音色）与 `bot._resolve_minimax_voice` 逐输入等价；空活表恒读常量兜底（bot.py:949 语义）永不抛错；`resolve_model` 三态等价；`voice_lookup` 活表注入缝生效 | ✅ |
| U-101 条件件 | api_key 只构造注入（env 忽略） | **executed** 探针 m10：`MINIMAX_API_KEY=env-key` 在环境中、构造传 `ctor-key` → `Authorization: Bearer ctor-key`（env 值零参与） | ✅ |
| U-101 条件件 | registry 并存 + 无默认无回退 | 见 R 组（显式选择、工厂零调用证明） | ✅ |
| U-102 | FC-3 由 LLM_PROVIDER/LLM_ENDPOINT/LLM_DEPLOYMENT/LLM_API_VERSION 驱动 | **executed** 探针 a8：`reg.create(LLM)` 出契约实例，endpoint/deployment/api_version 逐字段映射断言；**read** `azure_openai.py` vs `base.py:142-152` 签名核对 | ✅ |
| U-102 | 声明 LLM_API_VERSION 是否必填（U-003 钩子） | **executed** 探针 a7：`requires_api_version is True`；空 api_version 构造报错**指名 `LLM_API_VERSION`**（D-4 零默认，不采用轮 `2024-09-01-preview` 隐式默认） | ✅ |
| U-102 | fixture 驱动，不打真实端点 | **executed** 11 条套件 + a5–a7 探针全走 `AsyncAzureOpenAI` 形面 fake client，零网络 | ✅ |
| U-102 | D-4：无编译期模型/部署默认 | **executed** 探针 a7：四构造参数空白逐一响亮报错指名 §11 变量 | ✅ |
| U-102 | 工具参数必须校验 + 调用方确认后执行 | **executed** 探针 a6/a7：分片聚合（index 键、id/name/args 增量拼接）、事件序 `delta*→tool_request*→usage→end`、`validate_tool_request` 用 jsonschema 校验（未知工具/缺必填/类型不符 → `ToolRequestError`）且**模块内无执行通道**（只校验不执行）；缺 id/缺名/坏 JSON/非对象四形态响亮 | ✅ |
| U-104 | `auth.type=sigv4` config/UI 保持合法 | **executed** 探针 s1：`mcp_config.validate_auth` 接受 sigv4 形状（默认 service/region）、显式空串拒绝、`upsert/get` 落库往返保留 auth | ✅ |
| U-104 | 连接期 `{ok:false, error}` 降级（MissingCredentialsError 路径） | **executed** 探针 s2/s3：缺凭据时 `resolve_credentials` 与 `make_sigv4_streamable_params` 均抛**类型化** `MissingCredentialsError`（bot.py 两条降级路径只认它）；`admin_mcp_servers_test` 实跑信封 `{"ok": False, "tools": [], "error": "sigv4 setup failed: ... no AWS credentials ..."}`（bot.py:5185-5199 冻结语义）；呼叫路径 `_connect_mcp_clients` skip-with-warning 不炸呼叫（bot.py:1925-1947 **read** + 套件钉） | ✅ |
| U-104 | test_mcp_sigv4.py → 降级测试，旧面退役留台账 | **read** 重写文件：4 条降级测试 + 模块 docstring 退役台账（6 旧断言出处与处置）；验收窗口内 F-08 回补钉落地（5 条，见 §四） | ✅ |

### 「它能用」抽样总判

- **正向**：三条适配器的真实驱动面全部实跑出声/出事件/出 usage（伪传输驱动真代码，非 mock 自证）。
- **对抗**：SSE 随机字节切割、错误块、畸形块、并发 synthesize、cancel 中断、double-start、push-before-start、usage-before-stream 等 38 条全部按契约行为（响亮或保真）。
- **保真**：与 bot.py 遗留语义的等价性经**真 import 对照**证明（四表逐字 + 解析器逐输入），不依赖实现方测试的自证。

---

## 二、executed vs read 声明

**By execution（命令均在 `/Users/colton/Developer/voice-agent-platform` 执行，全部 `--capture=sys`）：**
1. 批次 10 文件 `pytest <test_azure_speech test_azure_openai test_minimax_tts test_mcp_sigv4 test_pinned_pipecat test_provider_contracts test_storage_contracts test_config_contract test_provider_registry test_mcp_runtime> -q` → **234 passed**（T1 快照，F-08 前）→ **235 passed**（T2 快照，F-08 后，见 §四）。
2. 全量 `pytest tests/ -q --capture=sys --tb=no -rf -rE` ×2 → 轮 1（F-08 落地前窗口）：**6 failed, 959 passed, 7 errors**（7 errors = shim 假红，见 §四）；轮 2（干净目录，F-08 后）：**6 failed, 967 passed, 0 errors**（178.79s）。
3. 自设计对抗/边界探针 38 条（`/tmp/qa_u101_104_probe_test.py`，临时文件，跑毕已删）→ **38 passed**（终态复跑仍 38 passed）。
4. RETAIN 闸门 `pytest <Phase A 5 文件 + test_mcp_runtime>` → **179 passed**（171 + 8；171 = 170 + U-105 import-smoke 新钉）。
5. `git diff --stat` / `git status --porcelain` → §四围栏。
6. 轮源核对辅助命令：`sed -n` 读 bot.py:607-736/:928-949/:1757-1797、wheel `pipecat/services/minimax/tts.py:310`、`mcp_sigv4.py`、`bot.py:1915-1955/:5180-5202`。

**By reading（逐行核对）：**
- FC-2/FC-3/FC-4 骨架（`providers/base.py:125-164`）↔ `azure_speech.py`/`azure_openai.py`/`minimax_tts.py` 的方法集、签名、keyword-only 标记、加法面（`requires_api_version`/`validate_tool_request`/`supports_*`）；
- `run_tts` 迁入与 `bot.py:622-736` 逐行对照（SSE 解析/GroupId/base_resp/hex 重切/返音/三错误文案），差异仅 `_mm_json`→`json` 与注释改正（chunk_size 注释按实况改写，行为逐字保真）；
- `_build_tts`（bot.py:1757-1797）↔ `_default_service_factory` 构造形态（settings 注入/stream=True/env 语义保留）；
- `_resolve_minimax_voice`（bot.py:933-949）↔ `resolve_minimax_voice`（含永不失败常量兜底）；
- U-104 两条降级路径（bot.py:1925-1947 呼叫 skip、:5185-5199 /test 信封）与 `MissingCredentialsError`（mcp_sigv4.py:41-46）；
- 三份自测报告与评审报告（仅对齐问题面）；冻结卡 §5 U-101/102/104 与 MiniMax 派令条件件逐条对照。

**未采信**：fullstack-dev 自测报数、code-reviewer 结论、team-lead 对标数字——所有数字均为我方独立重跑；对标见 §四。

---

## 三、自设计对抗/边界用例清单（38 条，逐组结果）

实现方 23+17+11+4(+1) 条全绿只证明「实现者想到的场景」。以下为**我独立设计**的探针（临时文件 `/tmp/qa_u101_104_probe_test.py`，跑毕已删，无留置），驱动真实实现代码（真桥、真 wheel 基类、真 bot.py 对照），fake 只塑形在网络/客户端边界。

| 组 | 用例 | 类型 | 结果 |
|---|---|---|---|
| R 注册表 | `minimax/MiniMax/MINIMAX/MiNiMaX/mInImAx` 五形态 register 即拒 | 异常×5 | ✅ |
| R 注册表 | 三形态 resolve 即拒 + **工厂零调用**（无回退）；走私直塞 `_factories` 仍双层拦 | 异常×2 | ✅ |
| R 注册表 | `minimax_tts` 与 `azure_speech` 共存、`TTS_PROVIDER` 显式二选一、产物过契约检查 | 功能 | ✅ |
| M 线协议 | SSE 块跨任意字节边界（切在 `data:` 与 JSON 中间）完整重装 | 边界 | ✅ |
| M 线协议 | base_resp≠0 中途响亮报错（status_msg 透出，非静默零音频） | 异常 | ✅ |
| M 线协议 | hex 重切整边界 48000→[24000,24000]、is_final 只落末块（wheel 0.5s/块语义） | 边界 | ✅ |
| M 线协议 | `返/(faan1)` 修正随 `Chinese,Yue` 开关（Yue 有 / Japanese 无 + boost 透传） | 功能 | ✅ |
| M 线协议 | GroupId 空剥查询串 / 非空走 wheel 拼接 URL（tts.py:310） | 功能 | ✅ |
| M 线协议 | 流末未闭合块被忽略（SSE 尾块 quirk 保真——观察项见 §六） | 边界 | ✅（如述） |
| M 线协议 | 畸形 JSON/hex 跳过不崩、好块正常出声 | 异常 | ✅ |
| M 线协议 | HTTP≠200 → 错误含状态码 + body 截 200；post 异常 → `MiniMax request failed` | 异常×2 | ✅ |
| M 线协议 | env `MINIMAX_API_KEY` 被忽略、构造 api_key 进 Authorization 头 | 安全 | ✅ |
| M 线协议 | 错误帧经适配器路径抛 `RuntimeError("MiniMax TTS synthesis failed: …")` | 异常 | ✅ |
| M 线协议 | 空/空白文本流零调用零产出 | 边界 | ✅ |
| T 表等价 | 四表 + `DEFAULT_MINIMAX_MODEL` 与真 bot.py 逐字相等 | 防漂移 | ✅ |
| T 表等价 | `resolve_minimax_voice` 12 输入组合与 `bot._resolve_minimax_voice` 逐输入等价 | 防漂移 | ✅ |
| T 表等价 | 空活表 → 常量兜底恒返回、永不抛错（bot.py:949） | 边界 | ✅ |
| T 表等价 | `resolve_model` 三态（命中/未命中→默认/None→默认） | 功能 | ✅ |
| T 表等价 | `voice_lookup` 活表注入缝生效（custom 表优先） | 功能 | ✅ |
| T 构造 | 空 api_key/越契约 sample_rate 响亮报错 | 异常×2 | ✅ |
| A FC-2 | interim/final/error 路由到 `events()`（error 以异常抛出）、per-session language 覆盖 STT_LOCALE | 功能 | ✅ |
| A FC-2 | push-before-start、double-start、stop 幂等、非 pcm16/非契约采样率拒 | 边界×4 | ✅ |
| A FC-2 | 构造校验指名注入通道与 §11 变量（api_key/SPEECH_REGION/STT_LOCALE）；能力标志诚实 | 异常 | ✅ |
| A FC-4 | cancel 代际守卫（打断含终块，下一回合恢复 3 块 + 末块 is_final）；output_format 契约值 | 功能+边界 | ✅ |
| A FC-3 | 事件序 `delta×2→tool_request×2→usage→end`；双工具分片聚合（index 键）；usage 计数/raw 保留 | 功能 | ✅ |
| A FC-3 | 缺 id/缺名/坏 JSON/非对象四形态 `ToolRequestError` | 异常×4 | ✅ |
| A FC-3 | usage-before-stream 响亮；cancel 截断尾部事件；四构造参数指名报错；类声明两标志 | 边界 | ✅ |
| A FC-3 | `validate_tool_request`：通过/未知工具/schema 违例三态（只校验不执行） | 权限/安全 | ✅ |
| A 集成 | registry create 三 kind 契约实例 + §11 字段映射断言 | 功能 | ✅ |
| S sigv4 | config 面合法 + 落库往返 + 空 service 拒 | 功能+异常 | ✅ |
| S sigv4 | 缺凭据双路径抛类型化 `MissingCredentialsError`（继承 RuntimeError） | 异常 | ✅ |
| S sigv4 | /test 信封 `{ok:False, tools:[], error}` 实跑（error 含两段关键文案） | 功能 | ✅ |
| S sigv4 | `sign_request_headers` 稳定头 allowlist 行为（易变头逐个排除——F-08 回归类实证） | 安全 | ✅ |

**诚实记录（我的探针脚手架缺陷 6 处，均修正后 38/38）**：首轮 32 passed / 6 failed 全部为**我方期望值错误**，零实现缺陷——
1. m1/m2/m3/m11 四例未计入 **SSE 尾块 quirk**（流末未闭合块不解析，u-101-report §六.2 已文档化的行为保真项）→ 补尾标记后过；
2. m5 期望值未计 wheel 构造器 `tts.py:310` 自拼 `?GroupId=`（我造了双查询的畸形输入；该输入下遗留 bot.py 行为逐字相同，保真成立）→ 修正输入后过；
3. s4 期望 SignedHeaders 不含 `x-amz-date`——过严：botocore 规范签名本就签 `host`+`x-amz-date`；F-08 的回归类是**易变 httpx 头**混入，修正断言面后过（顺带实证 F-08 回归类在现码不存在）。

---

## 四、回归归因与快照动态

### 批次与闸门数字（全部我方实测）

| 面 | 命令 | 结果 | 对标 |
|---|---|---|---|
| 批次 10 文件（T1，F-08 前） | `pytest <10 files> -q --capture=sys` | **234 passed, 1 warning** | 三方 234 ✓ |
| 批次 10 文件（T2，F-08 后） | 同上 | **235 passed, 1 warning** | 派令预告 234→235 ✓ |
| 全量（轮 1，shim 假红窗口） | `pytest tests/ -q -rf -rE --capture=sys` | **6 failed, 959 passed, 7 errors**（206s） | 959+7=966 = 三方 966 ✓ |
| 全量（轮 2，干净目录） | 同上 | **6 failed, 967 passed, 0 errors**（178.79s） | 967 = 966 + F-08 新钉 |
| RETAIN 闸门 | `pytest <Phase A 5 文件 + test_mcp_runtime>` | **179 passed** | 170+8 → 171+8（+1 为 U-105 import-smoke）✓ |

### 6 红逐条归因（全部预存，与本批零因果）

| # | 测试 | 归因 | 证据 | 与本批因果 |
|---|---|---|---|---|
| 1 | `test_history.py::test_finalize_writes_pending_then_ok` | **C4 预存**（旧测试↔旧代码漂移） | `AttributeError: 'HistoryRecorder' object has no attribute 'finalize'`（test_history.py:139，phase-a-report §1.4 C4 原文逐字相同） | 无——tracked 未改动文件，不 import 本批任何新模块 |
| 2 | `test_history.py::test_finalize_failed_summary_marks_status` | 同 C4 | 同上（:195） | 无 |
| 3 | `test_demo_store.py::test_put_get_list_roundtrip_with_tool_ids_and_decimal` | 同 C4 | 工具 ID 集断言漂移（`{'asr_filter','kb_ack',…}` vs `{'filler','gb_intro',…}`，test_demo_store.py:167，phase-a-report §1.4 C4 原文） | 无 |
| 4 | `test_migrate_demos.py::test_migrate_writes_all_fields_matching_loader` | 同 C4 | 同类工具 ID 集漂移（test_migrate_demos.py:199） | 无 |
| 5 | `test_tool_filler.py::test_integration_real_push_frame_reaches_downstream_before_inner` | **C2 预存**（pipecat 1.4.0 API 漂移） | `FunctionCallParams.__init__() missing 1 required positional argument: 'pipeline_worker'`（test_tool_filler.py:243/286，Phase A 验收 §四已附一手证据；api-notes §4 已录） | 无——Phase A 已裁 ADAPT，修复归条件 3 |
| 6 | `test_mcp_integration.py::test_merge_skips_conflicting_end_call_and_roundtrips` | 同 C2 | 同一 `FunctionCallParams.pipeline_worker` 构造漂移 | 无 |

**机制性佐证**：6 个红文件全部是 tracked 未改动的遗留文件；本批新增/修改文件（providers/speech/*、providers/llm/*、4 个测试文件）在两轮全量中**零红**。Phase A 所致红 = 0 的结论在本批延续成立：**本批所致红 = 0**。

### 7 errors 归因（轮 1，环境假红）

轮 1 的 7 个 error 全为 fixture setup 阶段 `FileExistsError`（沙箱 broker 化 `Path.mkdir` shim，`sitecustomize.py:774 _brokered_path_mkdir` 对 `exist_ok=True` 的已存在目录仍抛 EEXIST——Phase A 验收 §四已一手定性）。轮 2 前清理 `$TMPDIR` 与 `.venv/tmp/pytest-of-*` 残留目录后 **0 errors**，且 959→967 的差额恰为 error 用例回归通过 + F-08 新钉——**假红排除，无隐藏真红**。

### 快照动态（F-08/F-09/U-105 骑带落地，按实测时点记录）

- **T1**（轮 1 前后）：`test_mcp_sigv4.py` 4 条、批次 234；全量含 7 个 shim 假红。
- **T2**（轮 2 前后）：fullstack-dev 修复环落地——**F-08** 回补 `test_signed_headers_excludes_volatile_httpx_headers`（原名保留回归血缘；断言面 = SignedHeaders ⊆ {content-type, accept, host, x-amz-date} 且含稳定头、逐项排除 user-agent/connection/accept-encoding/content-length，静态凭据零 AWS 依赖——与评审 F-08 要求逐点相符，**销项**）；**F-09** 6 个未使用导入已删（`azure_speech.py` grep 零命中）；另 `test_pinned_pipecat.py` 含 U-105 的 `test_pipecat_mcp_service_import_smoke`（任务 #8/#12 产物，RETAIN 闸门项）。批次 235、全量 967/6/0。
- 我的 38 条探针在 T2 终态复跑仍全绿（F-09 的导入清理零行为影响）。

### 围栏核对（executed）

`git diff --stat`：**仅 `.env.example`（Phase A CR-2 遗留 delta）+ `tests/test_mcp_sigv4.py`（U-104 重写，声明内）** 两个跟踪文件改动。`bot.py`、`mcp_sigv4.py`、`mcp_config.py`、`providers/base.py`、`providers/registry.py`、`config_contract.py`、requirements/lock、tools/ 零改动（u-10x 报告声明与实测一致）。新增未跟踪文件全部在交付清单内；`.slidep/`、`output/`、`.workbuddy/`、9 个 canary 为无关会话产物，不计围栏。

---

## 五、NOT verified（照 Phase A 口径，不得宣称生产就绪）

- **无 live Azure 端点**：Azure Speech/Azure OpenAI 零真实调用——`_AzureSttBridge`/`_AzureTtsBridge` 因 `azure-cognitiveservices-speech` 不在部署钉（CR-C2）**连 live 执行都未发生**，缝位正确性以轮源行号走查代查；`AzureOpenAILLM` 只对 fake client 实跑。
- **无 live MiniMax 呼叫**：线协议/错误分支/重切全部经伪 aiohttp 传输实跑真迁入代码（行为面覆盖），但**无真实 t2a_v2 网络往返**；en-SG IVC 克隆音色（`mm_singlish_*`）按 bot.py 注释只有打真 API 才能验证。
- **无真电话/媒体流**：无 Twilio 通话、无 μ-law 8kHz 线上编解码、无 barge-in 实测。
- **无部署/延迟/容量/SLO/鉴权轮换实测**（沿用 Phase A §五口径）。
- 有凭据 sigv4 正路径（真实签名→200）未模拟（需 AWS 凭据）；U-104 只验降级半边（决策 (c) 的另一半由既有代码承载，走查确认未动）。

---

## 六、遗留事项

| 项 | 内容 | 期限/归属 |
|---|---|---|
| **CR-C2**（条件 1） | `pipecat-ai[azure]`（或等价传递钉）入部署钉 + import-smoke/live gate，收口 Azure 桥接子类验证边界 | 建议并入 U-103/Phase B 收口；阻 live smoke 宣称，不阻 U-103 |
| **CR-C1**（条件 2） | §11 补凭据占位符（按 secret-name 先例走 Key Vault 引用）；`make_*_factory(api_key=...)` 注入缝即接管点 | CR 跟踪（#17/P1 消费） |
| **C2 fixture 修复**（条件 3） | `test_tool_filler`/`test_mcp_integration` 的 `FunctionCallParams.pipeline_worker` fixture-only 修复 | 不晚于 Phase B exit（Phase A 条件 2 延续） |
| F-08 | 403 SignedHeaders 回归钉回补——**已落地并经我方独立核验销项**（T2 快照） | ✅ 闭环 |
| F-09/F-10 | F-09 已落地（导入清理核实）；F-10（`_require` 两处重复）建议 U-103 组装期提为单一私有助手（第三份出现必须合并） | U-103 顺手 |
| U-105 | mcp↔pipecat 配对 + import-smoke 进 RETAIN 闸门——**已闭环**（`test_pipecat_mcp_service_import_smoke` 在闸门内实绿） | ✅ 闭环 |
| MiniMax 观察项 | ① SSE 尾块 quirk（流末未闭合块被忽略）按行为保真保留——MiniMax 终止块恰为 extra_info 无音频损失，但若提供方未来把音频放最后一块会被静默丢弃，建议 U-203 音频契约案一并评估；② `chunk_size` 0.5s/块（24kHz=24000B）vs 历史注释「20ms」——行为保真、注释已改正，延迟取舍归 G-2/U-203；③ `voices_for("minimax")` 活表接线待 U-103 组合根传 `voice_lookup`（不接线退化为常量表 = bot.py 非 live 行为） | U-203 / U-103 |
| 环境指引（沿用 Phase A） | pytest basetemp 在 `.venv/tmp/pytest-of-*`（非 `/tmp/`）；跑前清理 `$TMPDIR`+`.venv/tmp` 两处残留，否则 shim FileExistsError 假红 | 即时生效 |

---

## 附：验收环境与执行摘要

- 主机：macOS（WorkBuddy 沙箱，broker 化 file-op shim——假红机理与绕法见 §四）
- `.venv`：Python 3.13.12 · pytest 9.2.0 · pytest-asyncio 1.4.1.post1 · pipecat-ai 1.4.0 · openai 2.54.0 · jsonschema 4.26.0（传递钉）· mcp SDK（U-105 重钉后）
- 执行统计：批次 10 文件 235/235 绿（T2）；全量 6 红（全预存逐证）/ 967 绿 / 0 error（干净轮）；自设计探针 38/38 绿（终态复跑一致）；围栏仅 2 个声明内跟踪文件
- **验收结论：PASS-WITH-CONDITIONS** —— 任务 #9（U-101/102/104 + MiniMax 条件件）验收通过；F-08/F-09/U-105 骑带项均已落地核销。条件 1（CR-C2 live gate）落地方可宣称 Azure 桥 live 可用；条件 2（CR-C1）/条件 3（C2 fixture，Phase B exit）按期闭环。本结论交付后本批可宣告关闭（评审 PASS-WITH-CONDITIONS + 本独立验收通过）。
