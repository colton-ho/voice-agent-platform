# 独立验收报告 · U-103 语音纵向切片（本地）— 级联引擎 + 组合根 + 决策 (a) 引擎集

- 验收人：验得明（qa-verifier，独立验证）· 日期：2026-11-09 · 对应 team 任务卡 #11 QA 半边「Phase B independent QA gate」
- 验收对象（任务 #10/U-103 交付物）：
  - `cascade_engine.py`（FC-2/3/4 级联引擎：短语级流式、工具幂等、三段超时/取消、生命周期）
  - `bot.py` 组合根 `build_cascade_engine`（load_config 真启动校验 + registry 显式选型接线 + voices_for→minimax voice_lookup 注入缝 + execute_tool 执行确认缝）+ bot.py:724-728 注释诚实化
  - 决策 (a) 引擎集 ①-④（ruling B 2026-11-09）：`ENGINES`→`[pipeline]`、`DEFAULT_ENGINE`/PHONE_ENGINE 默认→pipeline、`NOVA_SONIC_VOICES/_ALIASES/DEFAULT_NOVA_SONIC_VOICE` 清空、8 个 Nova-only locale `engines:[]` 退出 phone 默认
  - 14 新测（`tests/test_cascade_engine.py` 10 + `tests/test_composition_root.py` 4）+ 5 文件测试重指（test_admin_api / test_engine_languages / test_engine_voice_patch / test_guest_link / test_nova_voices）【team-lead 2026-11-09 口径更正确认：重指面为 5 文件，原派令「6 文件」系笔误；test_mcp_sigv4 属 #9 批/Step 1 F-08 面，不入本批重指计数。205 = 重指 191 + 新测 14，与我方独立解码逐数吻合】
- 依据：implementation-plan.md §5 冻结卡 U-103 + spec §7.1 + spec Phase B completion（四演示）+ team-lead 派令验收重点 + §7 决策 (a)/(A-3) + team-lead 已定裁决（④ engines:[] 口径维持；_validate_segment 预存边角不修、录 #18+U-501；CR-C1/CR-C2 骑带）
- 环境：`.venv`（Python 3.13.12，pytest 9.2.0 + pytest-asyncio 1.4.1.post1，pipecat-ai==1.4.0，mcp SDK 已按 U-105 重钉）；全部 pytest 带 `--capture=sys`
- 约定：不 commit/push、不触云、不改 AWS 路径；探针文件独立编写（`/tmp/qa_u103_probe_test.py`，51 条，验收后删除），不复用实现测试代码

**立场声明**：不采信实现者（u-103-report.md）任何自报数字与结论；下文每条结论均附本人亲自执行的命令与输出（executed）或逐行读码/AST 对照（read）。结论独立下。

---

## 一、验收结论

### 结论：**PASS-WITH-CONDITIONS**（通过，附 3 项骑带条件——均为继承跟踪项，不要求本批返工）

**「证明它能用」在其声明的 fixture 边界内成立**：U-103 直接面 **205/205**（2 新 + 5 重指文件）、契约批次 **226/226**、RETAIN 闸门 **179 全绿**、全量 **6 failed / 981 passed / 0 errors**（6 红逐条核对全部预存、与本批零因果）；我自设计的 **51 条对抗/边界探针全部通过**——短语切分边界（前瞻/硬切/小数点保护/多结束符）、**跨回合工具夹击幂等**（同 id 跨轮+跨回合+乱序交错只执行一次、回执形状逐字段钉死）、生命周期边角（close-before-start 不调 stt.stop、双 start、start-after-close、push 越界）、空/空白 final 不触发回合、STT 流死亡关出口、三段超时双取消、`max_tool_rounds` 有界不悬死、U-003 钩子指名 `LLM_API_VERSION` 响亮拒绝、registry 双层拒遗留名 8 变体 + 走私 `_factories` 工厂零调用、决策 (a) ①-④ 全表面（含路由级 400 端到端、DB 音色 merge 非空且 store 不删、PHONE_ENGINE 显式覆写、/api/config 键形状守恒、en-US 双列与 5 条 pipeline locale 不变）。

骑带条件（跟踪中）：
1. **CR-C2**（部署钉缺口，继承 #29）：`azure-cognitiveservices-speech` 不在部署钉，Azure STT/TTS 桥与 cascade 真实三元组**无法 live 执行**——本批「它能用」= fixture/伪服务驱动真代码。live/import-smoke gate 落地（阻 Phase B 收口，不阻施工）前，**不得宣称 cascade/Azure 桥 live 可用**。
2. **CR-C1**（§11 凭据占位符，继承 #29）：SPEECH_KEY/LLM_API_KEY/MINIMAX_API_KEY 走工厂/组合根构造注入（`build_cascade_engine(speech_api_key=…, llm_api_key=…, minimax_api_key=…)`），补 §11 变量属 CR 裁定。
3. **C2 fixture 修复**（Phase A 验收条件 2 延续）：`test_tool_filler`/`test_mcp_integration` 的 `FunctionCallParams.pipeline_worker` 漂移修复**不晚于 Phase B exit**（届时 RETAIN 集成 CI 闸门）。

（按派令已定裁决不重开：④ engines:[] 口径维持；`_validate_segment` 预存边角不修、已录 #18+U-501。）

### U-103 卡面验收条目逐条映射

| # | 验收条目（冻结卡/派令/四演示） | 证据（我方执行/读取） | 判定 |
|---|---|---|---|
| 1 | bot.py 组合根接线：显式 registry 选型，`load_config` 成为真启动校验（违规一次性全报） | **executed** c1：空环境 → ConfigError 一条消息 ≥20 个 §11 变量同报（VOICE_ENGINE/STT_PROVIDER/LLM_ENDPOINT/COSMOS_ENDPOINT 等逐一在文）；c2：`build_cascade_engine` 真接线出 CascadeEngine 三元组 | ✅ |
| 2 | registry 显式选型组出 FC-2/3/4 三元组（零默认零回退） | **executed** c2（类名 + 输入 16 kHz/合成 24 kHz 映射）、c3（层 1 load_config 指名 STT_PROVIDER；层 2 registry `MissingSelectionError.variable`）、c6（未注册名报错列已注册名单、工厂零调用） | ✅ |
| 3 | 遗留名永不解析（transcribe/bedrock/nova-sonic 及大小写变体、走私 _factories） | **executed** c4：8 组合体（transcribe/Transcribe/bedrock/Bedrock/minimax/MiNiMaX/nova-sonic/NOVA-SONIC）全部 UnsupportedProviderError；c5：直塞 `_factories[(STT,"transcribe")]` 后 resolve 仍拒 + 工厂零调用（无回退实证）；c10：`VOICE_ENGINE=Cascade`（非全小写）按名解析响亮失败、不静默兜底 | ✅ |
| 4 | `VOICE_ENGINE=cascade` 路径 STT→LLM→TTS 短语级流式（spec §7.1：不等整段回答） | **executed** e25：`phrase_max_chars=4` 无标点长文本以定长 [4,4,2] 短语逐段喂 TTS（硬切有界路径）；e1：句末标点即切、`3.14`/`Ver.2` 小数点保护、尾随 `.` 前瞻不误切、位置 0 单字短语、`;`/`\n` 结束符、多结束符取先 | ✅ |
| 5 | 演示 ①本地流式 + FC-4 契约（首块音频先于回合 end） | **executed** e18/e19/e22（事件形状：轮内 end 不外发、回合恰一个 end、usage 转发、消息史 system+user 起手）；实现方 10 测全绿独立复跑（14 passed） | ✅ |
| 6 | 演示 ②打断（R-P-009）：LLM+TTS 取消、会话可复用 | **executed** e20（空闲 interrupt 安全 + 之后回合正常收尾）、e21（**终块+end 全产出后 interrupt，下一回合完整恢复**）；自动 barge-in 由实现方测试钉 + 我方跨回合工具夹击（e2）间接覆盖旧回合作废路径 | ✅ |
| 7 | 演示 ③生命周期清理：close 幂等、stt.stop、在途回合取消、close 后 push_audio 拒绝 | **executed** e9（**close-before-start 不调 stt.stop**——started 门控）、e10（双 start → RuntimeError）、e11（close 后 start → RuntimeError）、e12（push 前 start → RuntimeError）；close 幂等/停泵由实现方测试钉 + 我方 e16（流死亡关出口、不误调 stt.stop） | ✅ |
| 8 | 演示 ④幂等工具：同 ToolRequest.id 只执行一次且回执形状钉死 | **executed** e2（**跨回合夹击**：回合 1 执行、回合 2 重放 → 全程恰执行一次，重放回执内容 R1 来自缓存）、e3（乱序交错 b,a,a,b → 各恰一次）、e4（一轮多工具：assistant+tool(a)+tool(b) 顺序与 tool_call_id/name/content 逐字段钉死） | ✅ |
| 9 | R-A-005：三段超时各配 cancel 断言 | **executed** e23（LLM 事件步超时 → **llm.cancel + tts.cancel 双取消** + on_turn_error("llm",TimeoutError) + 回合 end 收尾）、e19（TTS 音频块步超时 → stage="tts"、on_turn_error 先于 end）、e24（stt.start 超时 → TimeoutError 上抛、状态干净：push 拒绝、stt.stopped==0） | ✅ |
| 10 | spec §7.1：显式识别语言，配置错误不静默接受 | **executed** 空/空白 language → ValueError（实现方测试钉）；e13（空/空白 final 转写**不触发回合**、事件仍转发）；c10（响亮失败不兜底） | ✅ |
| 11 | bot.py:724 注释诚实性（0.5s×sample_rate×2 口径 vs 实际行为逐位核） | **read+executed** 轮源 `tts_service.py:430-431`：`CHUNK_SECONDS=0.5; return int(self.sample_rate * CHUNK_SECONDS * 2)` —— 与新注释「sample_rate × 0.5 × 2 bytes；24 kHz = 24000」**逐位一致**；`or 960` 历史兜底描述与 bot.py `chunk_bytes = self.chunk_size or 960` 一致；实际重切行为（48000→[24000,24000]、is_final 只落末块）已在 #29 M 组探针经真代码实跑钉住 | ✅ |
| 12 | R-P-001/004/009 保持：现有路径行为不变 | **executed** 全量 981 passed / 6 failed（6 红全预存、零因果，见 §四）；**read** git diff bot.py 12 个 hunk 全部落在：LANGUAGES 注释+8 个 engines 字段、:724 注释、NOVA/ENGINES 表、组合根新增段——`_build_pipeline`/L1–L3 Bedrock 调用面/ws/phone 传输面零触碰 | ✅ |
| 13 | nova-sonic 代码路径零改动（_build_nova_sonic_pipeline/AWSNovaSonicLLMService/upsample，bot.py:7004-7067） | **executed** hunk 归属扫描：12 个 hunk 无一落在 7004-7067（唯一命中是 871 hunk 注释里*提及*该函数名的说明文字）；电话会话内核 `p_engine=="nova-sonic"` 分支（bot.py:7114）原样保留（AWS 回滚路径） | ✅ |
| 14 | models[] 键/label/bedrock_id 零动 | **executed** AST 对照 HEAD：`MODELS/DEFAULT_MODEL/MINIMAX_MODELS/DEFAULT_MINIMAX_MODEL/TTS_PROVIDERS/DEFAULT_PROVIDER/POLLY_VOICES` 七常量 **IDENTICAL**；x8：/api/config `models` 全行恰为 {id,label,bedrock_id} 三键 | ✅ |
| 15 | store/session 零动 | **executed** `git diff --name-only` = `.env.example, bot.py` + 6 测试文件；user_store/runtime_config/demo_loader/demo_store/voice_store/activity_store/mcp_config 均不在改动面 | ✅ |
| 16 | 决策 (a) ①：`engines[]`→[pipeline] | **executed** x1/x8：`set(ENGINES)=={"pipeline"}`、payload `engines`==`[{id:pipeline}]`、PATCH/PUT/launch 三面 `engine=nova-sonic` → 400（x4/x4b/x5/x10，x4b 为 TestClient 路由级端到端且验证校验先于落盘） | ✅ |
| 17 | 决策 (a) ②：PHONE_ENGINE 默认→pipeline + 显式覆写行为 | **executed** x3（subprocess 隔离）：env 未设 → phone fallback engine `"pipeline"`；`PHONE_ENGINE=nova-sonic` 显式设 → `"nova-sonic"` **照旧生效**（只改默认不动覆写语义 = AWS 回滚缝保持） | ✅ |
| 18 | 决策 (a) ③（C-3）：nova_sonic_voices 清空 | **executed** x2/x8：`NOVA_SONIC_VOICES=={}`、`NOVA_SONIC_VOICE_ALIASES=={}`、`DEFAULT_NOVA_SONIC_VOICE==""`（且不在表内，无幽灵默认）；**键形状守恒**：`nova_sonic_voices` 键在（值 `[]`）、`default_nova_sonic_voice` 键在（值 `""`）——键删改名即 breaking 未发生 | ✅ |
| 19 | 决策 (a) ④：8 个 Nova-only locale 退出 phone 默认（engines:[] 口径，裁决维持） | **executed** x7：fr-FR 等 8 条 PUT（merged engine=pipeline）→ 400；x8/x11：5 条 pipeline locale（zh-HK/zh-CN/en-US/ja-JP/en-SG）`engines` 不变（en-US 双列 `["pipeline","nova-sonic"]` 原样，A-3 零产品变化）；**AST 对照 HEAD**：13 条 LANGUAGES 仅 8 条 Nova-only 的 engines 字段改动、非 engine 字段（label/stt/prompt/greeting）逐字不动、条目不删 | ✅ |
| 20 | 自定义 DB 音色 merge 后 payload 非空（store 层不动、用户数据不删） | **executed** x9：fake live VOICE_STORE（含 custom nova 音色）→ payload `nova_sonic_voices==[custom_nova]` 非空；GET 后 store 行原样在（零删除）；x6：遗留 demo（demo_engine=nova-sonic）+ nova 音色 PATCH → 空表解析 400（引擎未设 + nova 音色 → 双表皆空 400） | ✅ |
| 21 | 14 新测 + 5 文件重指独立复跑 | **executed** 新测 14 passed；重指面 205/205（2 新 + 5 重指文件 = 191+14）；契约批 226/226（10 文件）；test_mcp_sigv4（Step 1/F-08 面）随批绿 | ✅ |

---

## 二、executed vs read 声明

**By execution**（命令均在 `/Users/colton/Developer/voice-agent-platform` 执行，全部 `--capture=sys`；basetemp 见 §五环境说明）：
1. `pytest tests/test_cascade_engine.py tests/test_composition_root.py -q` → **14 passed**
2. `pytest <10 文件: test_mcp_sigv4 test_azure_speech test_minimax_tts test_azure_openai test_provider_registry test_config_contract test_provider_contracts test_pinned_pipecat test_cascade_engine test_composition_root> -q` → **226 passed**（实现方口径，逐文件计数 5+17+23+11+47+83+23+3+10+4=226 复核）
3. `pytest <7 文件: test_admin_api test_engine_languages test_engine_voice_patch test_guest_link test_nova_voices test_cascade_engine test_composition_root> -q` → **205 passed**（派令「205 口径」解码：U-103 直接面 = 2 新 + 5 重指；含 test_mcp_sigv4 则 210，扣除其 5 条 #9 归属即 205）
4. `pytest tests/ -q`（全量）→ **6 failed, 981 passed, 0 errors**（981 = 966 评审基线 + 1 F-08 + 14 新增，与三方口径吻合）
5. `pytest <RETAIN 门 6 文件: test_config_contract test_provider_registry test_provider_contracts test_pinned_pipecat test_storage_contracts test_mcp_runtime> -q` → **179 passed**（Phase A 171 + test_mcp_runtime 8，含 U-105 import-smoke）
6. `pytest /tmp/qa_u103_probe_test.py -q` → **51 passed**（我方对抗探针，组 E 20 / 组 C 11 / 组 X 12 / 切分边界 3 / 补 5）
7. `pytest <6 红文件> --tb=line` → **6 failed**（签名逐条录 §四）

**By read/AST**：`cascade_engine.py` 全文逐行（短语切分/幂等缓存/三段超时/取消路径/世代作废语义）；`bot.py` 组合根与 4 个校验面（`_validate_segment` 3264-3300、`_validate_engine_voice_patch` 4547-4670、`_validate_launch_config` 4700-4754、`_admin_options_payload` 3176-3218、`/api/config` 5547、会话内核 7054-7115）；`providers/registry.py` 全文（cascade 三元组 + 双层拒）；bot.py 对 HEAD 的 12 个 diff hunk 归属；`tts_service.py:415-431` 轮源 chunk_size 公式；`u-103-report.md`（仅作问题面对齐）。

**探针自纠台账（透明度）**：首轮 51 探针中 9 失败，逐条 triage 后**全部为我方探针自身缺陷**（env 漏设 LLM_API_VERSION 触发 U-003 钩子先行 ×4；史消息索引取错 ×1；STT 死亡与在途回合竞态断言 ×1；TTS 无短语可 stall ×1；detail 文案预期错 ×1；异常层级预期错 ×1）——修正探针后 51/51 全绿。**实现缺陷 0**。其中两处「失败」实为正向证据：U-003 钩子（c2b 补钉）与 `_validate_segment` 的 detail 文案（400 语义正确、文案与 `_validate_engine_voice_patch` 风格不一致——文案统一属 code-reviewer 面，不构成本批缺陷）。

---

## 三、我方对抗探针清单（51 条，独立编写）

- **组 E · 级联引擎边缘（20）**：e1 短语切分 8 组边界（恰 max 硬切/max-1 不切/位置 0 单字短语/尾随 `.` 前瞻/`3.14`+`Ver.2` 小数点保护/`;`+`\n`/多结束符取先）；e25 硬切定长短语喂 TTS；e2 跨回合工具夹击幂等；e3 乱序交错 id 各恰一次；e4 一轮多工具回执逐字段钉死；e5 无执行器 → RuntimeError("execution confirmation") 响亮；e6 校验钩子仅首次走、失败走回合错误路径且不执行；e7 无钩子适配器照常执行；e8 `max_tool_rounds` 有界（恰 max+1 轮、不悬死）；e9 close-before-start 不调 stt.stop；e10 双 start 拒；e11 close 后 start 拒；e12 push 前 start 拒；e13 空/空白 final 不触发回合；e16 STT 流死亡关出口且不误调 stop；e17 泵异常 → on_turn_error("stt")；e18 多轮工具回合恰一个 end；e19 usage 转发 + 错误先于 end；e20 空闲打断安全；e21 终块后打断下回合恢复；e23 LLM 超时**双取消**；e24 stt 超时后状态干净；e22 消息史形状（system+user / 二回合 assistant 续史）。
- **组 C · 组合根（11）**：c1 空环境 ConfigError 聚合 ≥20 变量同报指名；c2 三元组真接线（类名/采样率 16k→24k/系统提示）；c2b U-003 钩子缺 `LLM_API_VERSION` 响亮指名；c3 缺选型双层（load_config 指名 + registry MissingSelectionError.variable）；c4 遗留名 8 变体拒；c5 走私 `_factories` 双层拒 + 工厂零调用；c6 未注册名报错列名单 + 零调用；c7 `minimax_tts` 可选 + 24k 输出；c10 `VOICE_ENGINE=Cascade` 响亮不兜底。
- **组 X · 决策 (a) 表面（12）**：x1 ENGINES/DEFAULT_ENGINE；x2 三表清空一致性（默认不在表内）；x3 PHONE_ENGINE subprocess 显式覆写（未设→pipeline；=nova-sonic→照旧生效）；x4 PUT engine=nova-sonic → 400 + merged 交叉检查；x4b **路由级端到端**（TestClient PUT web/phone → 400 且校验先于落盘）；x5 PATCH engine=nova-sonic → 400（detail 列 allowed=[pipeline]）；x6 nova 音色两形态 400（遗留 demo 引擎 / 引擎未设双表）；x7 8 条 nova-only lang PUT 全 400 + 5 条 pipeline lang 可用；x8 /api/config 键形状守恒（nova_sonic_voices=[]、default_nova_sonic_voice=""、engines=[pipeline]、en-US 双列、models 三键）；x9 DB 音色 merge 非空 + store 零删；x10 launch 配置 4 形态（engine/lang×engine/单 lang/默认引擎音色）；x11 5 条 pipeline locale + en-US 双列行为钉。

---

## 四、全量 6 红归属（零因果实证）

| # | 失败用例 | 签名 | 归属 | 与本批因果 |
|---|---|---|---|---|
| 1 | test_demo_store::test_put_get_list_roundtrip_with_tool_ids_and_decimal | `AssertionError: {'asr_filter','kb_ack',…} == {'filler','kb_intro',…}`（字段漂移） | C4 预存（存储层字段漂移） | 零：文件未在改动面，与 bot.py 新表/组合根无 import 关系 |
| 2 | test_history::test_finalize_writes_pending_then_ok | `AttributeError: 'HistoryRecorder' object has no attribute 'finalize'` | C4 预存 | 零：同上 |
| 3 | test_history::test_finalize_failed_summary_marks_status | 同上 | C4 预存 | 零：同上 |
| 4 | test_mcp_integration::test_merge_skips_conflicting_end_call_and_roundtrips | `AssertionError: inner (MCP) wrapper never ran` | C2 预存（FunctionCallParams 漂移面） | 零：文件未在改动面 |
| 5 | test_migrate_demos::test_migrate_writes_all_fields_matching_loader | `AssertionError: {'asr_filter','kb_ack',…} == {'filler','kb_intro',…}` | C4 预存 | 零：同上 |
| 6 | test_tool_filler::test_integration_real_push_frame_reaches_downstream_before_inner | `TypeError: FunctionCallParams.__init__() missing 1 required positional argument: 'pipeline_worker'` | C2 预存（pipecat 1.4.0 漂移） | 零：同上 |

6/6 与派令给定名单及 #29 基线**逐条同名同签名**；`git diff --name-only` 不含以上 5 文件（test_history 出现 2 红）。**本批新增红 = 0。**

---

## 五、环境与防假红记录

- 沙箱 shim（`sitecustomize.py:774 _brokered_path_mkdir`，宿主代执行链 `sitecustomize.py:507 _broker_request_host_operation`）对已存在 basetemp 目录的 `mkdir` 抛 `FileExistsError`（无视 exist_ok）→ fixture-setup 假红。本轮实测三种污染形态：88 errors（与他跑并发）、228 errors（并发）、67 errors（独跑仍偶发 broker 抖动，2 条预存红被掩成 error）。**team-lead 同日转告同族现象**（code-reviewer 三轮全量 28-116 条 test_admin_api 系 ERROR/FAILED 飘忽，traceback 同定性 `.venv/tmp/pytest-of-root/pytest-0/...` mkdir 假红；错误集合逐次漂移、scoped 全绿）——**判定口径：稳定红恒为 6 条（C4×4+C2×2）即达标；飘忽 ERROR 按假红记录附说明，不当代码缺陷追**。本轮干净全量（唯一 basetemp 治法后）为 0 errors，高于该口径。
- **治法（本轮验证有效）**：每次跑前 mv 挪开 `$TMPDIR` 与 `.venv/tmp` 两处 `pytest-of-*`，并用**唯一 `--basetemp=.venv/tmp/qa-*-$RANDOM`** 隔离——全量干净跑由此得到 `6 failed / 981 passed / 0 errors`。凡 0-error 数字均出自该治法后的运行。
- `test_minimax_tts` reload 双类对象坑：组合根断言按类名 + 契约 ABC（探针同理绕开）。
- **C2 修复环快照声明**：team-lead 裁定 C2 fixture 修复环在本报告到达后才动工作树（防与验收归因数字互扰）——本报告全部数字取自**修复前快照**，验收时 C2 两文件 2 红（test_tool_filler::test_integration_real_push_frame_reaches_downstream_before_inner、test_mcp_integration::test_merge_skips_conflicting_end_call_and_roundtrips）按预存挂账记录，修复后须按 §四 名单重跑归零复核。

## 六、NOT-verified 清单（与 Phase A/#29 同风格声明）

1. 无真实 Azure Speech/Azure OpenAI/MiniMax 线上调用——全部为 fixture/伪服务驱动真代码（CR-C2 live gate 未落）。
2. cascade 路径未接 ws/phone 传输层（卡明示 local vertical slice；传输集成属 U-2xx）——无真实电话链路、无 Twilio/Chime 通话验证。
3. `_build_nova_sonic_pipeline` AWS 回滚路径**未执行**（本批零改动为其验收条件；回滚演练属 U-503）。
4. `PHONE_ENGINE=nova-sonic` 覆写 → 会话内核 nova 分支的**运行行为**未执行（只验 env 读取语义；代码未动）。
5. 并发/多会话/容量面未测（G-3 单副本验证配置声明内）。
6. U-501 验收矩阵、acceptance.md 数值目标、生产就绪性均不作断言。

## 七、遗留/跟踪项

1. **CR-C2** live/import-smoke gate（阻 Phase B 收口）——继承 #29。
2. **CR-C1** §11 凭据占位符——继承 #29。
3. **C2 fixture 修复**（test_tool_filler/test_mcp_integration）不晚于 Phase B exit。
4. `_validate_segment` 预存边角：按裁决不修，录 #18+U-501。
5. 文档一致性提示（非缺陷）：`u-103-report.md` §四.1 仍述「决策 (a) 未获裁决前按 A 执行」——快照过时；本批实际已按 **ruling B 2026-11-09** 落地 ①-④（bot.py 注释/重指测试可证）。建议 #18 改动清单文档时同步修正该表述。
6. 文案风格观察（code-reviewer 面，不入缺陷）：`_validate_segment` 的 `invalid engine: {v}` 与 `_validate_engine_voice_patch` 的 `unknown engine: {v!r} (allowed: […])` 两处 400 detail 风格不一。

---

# 修订 R2 · 2026-11-09 · #33 修复环后归零复核（验得明，#34）【终版 — 全项完成】

> **快照声明**：§一–§七 为**修复前快照**（全量 6 failed / 981 passed）的历史记录，不改写。本 R2 节全部数字取自 **#33 修复环后快照**（基线：全量 **4 failed / 985 passed**，0 errors）。C2/F-11/F-12 三项销项（code-reviewer revision 2，reviews/u-103-review.md:85-136）由本次归零复核独立认证。
> **执行纪律**：#34 第三次执行（前两次死于基建故障），分块执行逐项落盘——本节全部小节已完成并落盘，R2.2–R2.6 为终版结论。

## R2.0 · ④ 六文件白名单边界核对（✅ 完成 — executed）

**口径**：修复环变更面恰合六文件 = `tests/test_tool_filler.py`、`tests/test_mcp_integration.py`、`cascade_engine.py`、`tests/test_cascade_engine.py`、`tests/test_composition_root.py`、`docs/azure/u-103-report.md`；多一少一都要报。

- **executed** `find .（排除 .venv/.git/.workbuddy/output/.slidep）-newermt "2026-11-09 17:10"`（17:10 = 我方修复前快照完成时点，修复环起点）→ 命中集恰为：
  | 文件 | mtime | 白名单 |
  |---|---|---|
  | tests/test_tool_filler.py | 17:24:55 | ✅ |
  | tests/test_mcp_integration.py | 17:24:55 | ✅ |
  | cascade_engine.py | 17:25:03 | ✅ |
  | tests/test_cascade_engine.py | 17:25:19 | ✅ |
  | tests/test_composition_root.py | 17:25:19 | ✅ |
  | docs/azure/u-103-report.md | 17:42:44 | ✅ |
  | .pytest_cache/v/cache/* | 跑测副产物 | 非代码，忽略 |
  | config/runtime.json | 17:54:55 | **gitignored 运行态**（`git check-ignore` 实证），测试运行副产物，非变更面 |
- **白名单外零触碰**：`bot.py` 16:01:56、`tests/test_admin_api.py` 16:09:47 = #32 裁决 B 落地时点（**早于修复环窗**，属我方修复前快照的一部分，非修复环动作）；`providers/*`、`config_contract.py`、`.env.example` 13:37 均为旧时点。**git status 与修复前快照（17:49 记录）逐行一致**：tracked 改动集不变（.env.example/bot.py/8 测试文件），唯一新增 tracked delta 仍是 C2 两文件（+1/+3 行，`pipeline_worker=None` fixture-only）。
- **忽略项核对**：根目录 8 个 "blat" canaries（2jyh9mb7/2v3rj7t8/6oywt12e/b7_lpmic/czsml6gs/e5sofotv/isknw3ys/ovgiolk6）不在 17:10 之后命中集内（11-06 预存，非漂移）；.slidep/、.workbuddy/ 已排除。
- **判定：恰合六文件白名单，零超范围改动** ✅（含 u-103-report.md §三/§四.1 一致性小修 = revision 2 偏差 3 已采纳项；test_cascade_engine.py 仅 +1 函数 = 偏差 1 已采纳项，测试函数 10→11、test_composition_root.py 4→5 与 u-103-report.md:45 亲验计数一致）

## R2.1 · ① §四归零：全量 985/4（✅ 完成 — executed）

- **executed** 全量 `pytest tests/ --capture=sys --basetemp=唯一`（升级治法后）→ **4 failed, 985 passed, 0 errors**（311.74s）。红名单逐字恰为预存 C4×4：
  1. `tests/test_demo_store.py::test_put_get_list_roundtrip_with_tool_ids_and_decimal`（字段漂移，同修复前签名）
  2. `tests/test_history.py::test_finalize_writes_pending_then_ok`（`HistoryRecorder.finalize` AttributeError）
  3. `tests/test_history.py::test_finalize_failed_summary_marks_status`（同上）
  4. `tests/test_migrate_demos.py::test_migrate_writes_all_fields_matching_loader`（字段漂移）
- **C2×2 归绿确认（因果链完整）**：`test_tool_filler::test_integration_real_push_frame_reaches_downstream_before_inner` 与 `test_mcp_integration::test_merge_skips_conflicting_end_call_and_roundtrips` 本次全量与 scoped（27 passed）均绿。因果 = fix-loop 的 fixture 补齐（read git diff：test_tool_filler.py:248-250 + test_mcp_integration.py:228 各恰 1 hunk `pipeline_worker=None`，pipecat 1.4.0 必填字段补齐即消 TypeError→ErrorFrame 吞错链）——**非跳过、非 skip 标记、非 xfail**（scoped 27/27 真执行通过，:286 同因失败面一处修复两面归零）。
- **零新增红**：985 = 981 + 2（C2 转绿）+ 2（F-11/F-12 新测），与 revision 2 归因逐数吻合。985/4 与三方基线**完全收敛，归因差异 0**。

## R2.2 · ② 探针 51+1 重建重跑（✅ 完成 — executed）

- 探针文件从会话记录逐 Write/Edit 重放重建（`/tmp/qa_u103_probe_test.py`，45 函数 = 52 案例：51 原探针 + 探针 #52）。
- e24 intentional flip（按 revision 2 偏差表第 4 项两腿分工）：dev 生产测试只钉「stop 被调」腿（stopped==1）；**「可重试」腿归我方**——脚本化 fake 带 **already-started 守卫**（start 一进入即半开拒 restart，stop 回收后方可 restart）：无 F-11 修复时半开服务拒 restart（探针转红=真捕捉），有修复时 stop 已回收、restart 成功并完成整轮对话。旧 e24 钉 `stopped==0`、新钉 `stopped==1` = 缺口本体的行为翻转，**intentional flip（F-11 修复）非回归**。
- 探针 #52（F-11 既有规格钉）：stop 自拋二级异常不得顶替 TimeoutError 原样上抛、且不得挂死（`contextlib.suppress(Exception)` 不吞 TimeoutError——revision 2 预期绿依据）。
- **实测数字：52 passed, 1 warning, 11.58s**（45 函数 / 52 案例全绿；warning 为 pytest 弃用提示，非测试失败）。逐项：
  - 51 原探针全绿（含 e24 升级后两腿版）；
  - **e24 两腿全过**：可重试腿（already-started 守卫下 restart 成功并完成整轮 = F-11 回收语义被探针独立捕捉）+ dev 腿（stopped==1，close 不双停）；
  - **e52 两腿全过**：stop 二级异常（RuntimeError）不顶替 TimeoutError 原样上抛且 <1.0s 无挂死；CancelledError（BaseException）不被 `suppress(Exception)` 吞掉、原样穿透（task.cancel 路径实证）。
  - 守卫真实性预检：already-started 守卫在无 F-11 修复时使 e24 可重试腿转红（真捕捉而非空转）——正式反证见 R2.3 變異檢驗实测。

## R2.3 · ③ F-11/F-12 變異檢驗（守護真實性，/tmp 一次性树副本，共享树零接触）（✅ 完成 — executed）

**方法**：`rsync` 一次性树副本 `/tmp/qa_u103_mut`（排除 .venv/.git/.workbuddy/.slidep/__pycache__/.pytest_cache），探针副本 REPO 改指副本根；**provenance 断言通过**（`cascade_engine.__file__` / `bot.__file__` 均 = /tmp/qa_u103_mut）。**共享树零接触**：全部变异只落 /tmp 副本，共享树文件零改动。副本基线先行：**68 passed**（52 探针 + 11 dev cascade + 5 dev composition）。

- **M1 · F-11 回退**（cascade_engine.py:133-145 还原裸 `wait_for`，变异 pattern 精确匹配断言 count==1）→ **3 failed / 65 passed**，红名单恰为对应守卫、零附带红：
  1. `test_e24_stt_start_timeout_state_clean` — 失败点 = `RuntimeError("already started (half-open service not reclaimed)")`：already-started 守卫在无回收时拒 restart，**「可重试」腿真捕捉**（非空转）；
  2. `test_e52_stop_exception_never_masks_timeout` — 腿 A/B 的 `stopped == 1` 断言失守（裸 wait_for 无回收路径，stop 从未被调）；
  3. `tests/test_cascade_engine.py::test_stt_start_timeout_reclaims_half_open_service`（dev F-11 测试）— `stopped == 1` 失守。
  其余 65 全绿（含 49 个无关探针）→ 变异只翻转对应守卫，覆盖面精准。**重复一次结果一致**（稳定红，非 fake-red）。恢复原文件 → 三守卫 **3/3 转绿**。
- **M2a · F-12 字面回退**（test_composition_root.py:122-126 删除，pattern 精确匹配）→ **0 red / 5 passed**（dev 文件 5→4 测试 + 我方 x3 仍绿）。**诚实记录**：删除守卫在结构上不可能产出红——「删除 → 对应探针转红」的字面口径不可满足；删除后行为面覆盖并未丢失，因我方 x3 独立守卫同一行为。守護真實性由 M2b 完成。
- **M2b · F-12 行为级回退**（bot.py:2741 phone 默认还原交付前语义 `os.environ.get("PHONE_ENGINE", "nova-sonic")`——git HEAD :907 `DEFAULT_ENGINE="nova-sonic"` / :2687 env-get 实证，被钉交付物 = 默认随 DEFAULT_ENGINE=="pipeline"）→ **2 failed / 4 passed**，红名单恰为：
  1. `tests/test_composition_root.py::test_phone_engine_default_rides_default_engine`（dev F-12 钉）— 默认 ≠ "pipeline"；
  2. `test_x3_phone_engine_env_override`（我方探针）— 默认腿 `"nova-sonic" != "pipeline"` 失败（override 腿因 env-get 保留而巧合绿，探针整体转红）。
  零附带红（composition 其余 4 绿）。恢复 → **6/6 转绿**。
- **判定：守護真實性成立** ✅——F-11/F-12 的守卫均为真守卫：回退被守内容（修复块 / 被钉行为）恰使对应守卫转红（M1: 3 红、M2b: 2 红），零附带红；恢复即转绿（3/3、6/6）；M2a 字面口径的结构性限制已如实记录并以 M2b 补足反证。红/绿对照数：M1 68→3红/65绿→3绿；M2a 6→0红/5绿；M2b 6→2红/4绿→6绿。

## R2.4 · 数字收敛表（对 revision 2 独立执行数字表 43/27/228/207/985-4）（✅ 完成 — executed）

| 执行面 | 范围 | 三方收敛值 | 我方实测 | 状态 |
|---|---|---|---|---|
| 修复环 4 文件 | test_tool_filler + test_mcp_integration + test_cascade_engine + test_composition_root | 43 passed | **43 passed** | ✅ 收敛 |
| C2 銀牌 2 文件 | test_tool_filler + test_mcp_integration | 27 passed | **27 passed** | ✅ 收敛 |
| 契约批 10 文件 | #9/U-103 契约面 | 228 passed（226+2） | **228 passed** | ✅ 收敛 |
| ruling-B 面 7 文件 | 5 重指 + 2 新引擎文件 | 207 passed（205+2） | **207 passed** | ✅ 收敛 |
| 全量 | pytest tests/ | 4 failed / 985 passed | **4 failed / 985 passed, 0 errors** | ✅ 收敛 |

## R2.5 · NOT-verified 增补（✅ 完成 — executed）

本次归零复核**未覆盖**（如实划界，非遗漏）：

1. **C4×4 预存红**（test_demo_store/test_history×2/test_migrate_demos）：修复前快照即红、签名逐字未变，属预存债务（字段漂移 ×2 + `HistoryRecorder.finalize` AttributeError ×2），不在 #33 修复环范围；本复核只确认「零新增红」，不销 C4。
2. **CR-C1/CR-C2**（revision 2 Phase B 阻塞项）与 **F-13**（非阻塞跟踪项）：归 code-reviewer 跟踪闭环，不在 #34 口径内。
3. **环境边界**：无真实 Azure 电话链路 / 线上 STT/LLM/TTS 端到端；无负载/性能测试；沙箱出网受限（nltk 代理噪声为环境现象）。全部验证基于脚本化 fake 与单测面契约，端到端行为正确性由契约批（228）与 ruling-B 面（207）测试面承担。
4. **探针套件本体**：`/tmp/qa_u103_probe_test.py` 为一次性验证件，按惯例记录后删除；可随时从会话记录逐 Write/Edit 重放重建（本次重建即为实证）。

## R2.6 · 终版结论（✅ 完成 — executed）

**结论：🟢 通过**——#33 修复环经独立归零复核认证成立，五点口令逐项达成，归因差异 0。

| 口令 | 项 | 实测 | 判定 |
|---|---|---|---|
| ① | §四归零 | 全量 4 failed / 985 passed / 0 errors；红名单逐字 = 预存 C4×4；C2×2 因果归绿（fixture 补齐，非 skip/xfail） | ✅ |
| ② | 探针 51+1 | 52/52 绿（e24/e52 两腿版全过；e24 intentional flip 已标记） | ✅ |
| ③ | 變異檢驗 | M1 精确 3 红/零附带 + M2b 精确 2 红/零附带，恢复即绿；M2a 字面口径结构性限制已如实记录 | ✅ 守護真實性成立 |
| ④ | 六文件白名单 | 命中集恰为 6 文件，白名单外零触碰（git status 与修复前快照一致） | ✅ |
| ⑤ | 升级治法 | 双目录 pytest-of-* 移场 + 唯一 --basetemp + --capture=sys，全程 0 errors | ✅ |

- **数字收敛**：43/27/228/207/985-4 与 revision 2 独立执行数字表逐数吻合（R2.4），归因差异 0。
- **诚实归因**：本次探针自身缺陷 **0**（会话记录逐 Write/Edit 重放重建，精确匹配断言全过）；探针弱点 **1 已修**——旧 e24 的 fake 无 already-started 守卫，「可重试」腿本会空转（有无 F-11 修复都绿），按 revision 2 偏差表第 4 项升级后变真捕捉（变异 M1 中以 `RuntimeError("already started...")` 转红实证）；`stopped` 0→1 为缺口本体的行为翻转，**intentional flip（F-11 修复）非回归**。实现缺陷新增 **0**（C4×4 为预存，非修复环引入）。
- **遗留风险**：见 R2.5（C4×4 预存债务、CR-C1/CR-C2/F-13 跟踪项、无真实链路端到端）。
- **回归纪律**：M1/M2 均为「变异→红→恢复→绿」全闭环；共享树全程零接触，验证后树状态与验证前一致。
