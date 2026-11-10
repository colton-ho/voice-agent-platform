# 评审报告 · U-103 批复核（cascade 引擎 + 组合根接线 + 决策 (a) 引擎集 ①-④ + Phase B 总结陈词）

- 评审人：code-reviewer（严过关）｜日期：2026-11-09
- **结论：🟢 PASS**（U-103 批；无阻塞项，2 条非阻塞建议——F-11/F-12 已于 revision 2 修复销项）
- **Phase B 收口裁定：🔴 现在不能收口**——blocking 清单 **2 项**（详见「五、Phase B 批复核总结」+「复评 revision 2」）：**CR-C1、CR-C2**（CR 流程跟踪，锚 #16/#17，不阻施工）。原第 3 项 **C2 fixture 修复** 已于 2026-11-09 修复环落地销项（复评 revision 2），「preserve regression tests」翻 ✅。
- 评审范围（#9 批之后的新增面）：
  - `cascade_engine.py`（317 行，FC-2/3/4 级联引擎）
  - `bot.py` 组合根 `build_cascade_engine`（:2661-2721 hunk）+ `:730-735` chunk_size 注释诚实化 + 决策 (a) 选型面 4 hunk（LANGUAGES 注释/④、NOVA 表 ③、ENGINES/DEFAULT ①，12 个 diff hunk 全数核阅）
  - 测试：`tests/test_cascade_engine.py`（10）、`tests/test_composition_root.py`（4）、5 文件断言重指（`test_nova_voices`/`test_engine_voice_patch`/`test_engine_languages`/`test_admin_api`/`test_guest_link`，diff 逐块核阅）
  - 自测记录：`docs/azure/u-103-report.md`
- 围栏核对：tracked diff = `bot.py` + 上述 5 测试文件 + #9/Phase A 遗留（`.env.example`、`test_mcp_sigv4.py`）；`providers/`、`storage/`、`config_contract.py`、`requirements.*`、store/session 模块、`voice_store`、`test_mcp_runtime` 等声明未动文件确为未改动（git status + diff hunk 表核对）。无夹带。

---

## 一、阻塞项（必须修复才能通过）

无。U-103 批本体（引擎/组合根/决策 (a) 执行/测试重指）零阻塞缺陷。

## 二、非阻塞建议（可选优化）

| # | 位置 | 问题 | 依据 | 建议修法 |
|---|------|------|------|----------|
| F-11 | `cascade_engine.py:133-137`（`start()` 的 `wait_for`）+ `:171-172`（`close()` 只在 `_started` 时 `stt.stop()`） | `stt.start` 超时/取消后的清理缺口：`wait_for` 从 await 点取消 `stt.start` 时，适配器（`AzureSpeechRecognizer.start` 先赋 `self._service` 再 `await open()`，azure_speech.py:135-143）会留下半开服务；此时 `engine._started` 仍为 False → `close()` 不会调 `stt.stop()`，重试 `start()` 又被适配器的 "already started" 拒绝——会话在 start 超时这一角上不可回收 | R-A-005「超时/取消确定性」的收尾半拍；触发条件窄（stt.start 超 10s），故不阻塞 | `start()` 的 `wait_for` 外包 `try/except (TimeoutError, asyncio.CancelledError)` → `with contextlib.suppress(Exception): await self._stt.stop()` 后原样上抛；补 1 条测试（start 超时后 `close()` 能回收服务 / 重试可用） |
| F-12 | 测试面（建议落 `tests/test_composition_root.py` 或 `test_runtime_config.py`） | ruling B ②（PHONE_ENGINE 默认随 DEFAULT_ENGINE 落 pipeline）的机制正确（bot.py:2741 零代码派生），但**无测试钉住默认值**：`test_admin_api.py:251` 只钉 PUT 后状态；`PHONE_ENGINE` 未设时 `_RUNTIME_FALLBACK["phone"]["engine"]=="pipeline"` 无断言 | ①-④ 是裁决 B 的交付物，四项中唯 ② 无行为钉；DEFAULT_ENGINE 将来再变会静默带走 phone 默认 | 补 1 断言：`monkeypatch.delenv("PHONE_ENGINE", raising=False)` 后 `assert bot._RUNTIME_FALLBACK["phone"]["engine"] == "pipeline"` |

## 三、核验重点逐项（(a)–(f)）

| 项 | 结果 | 证据 |
|----|------|------|
| (a) FC-0..FC-8 零破面 | ✅ | `providers/base.py`/`registry.py` 未动（git 状态）；`CascadeEngine` 构造注入 FC-2/3/4，**不冒充 FC-5**（模块注 :15-17 明示 events() 形似 FC-5 联合类型但契约冻结不动）；`tests/test_provider_contracts.py`/`test_storage_contracts.py`/`test_pinned_pipecat.py` 在 226 批全绿 |
| (b) R-P-001/004/009 保持 | ✅ | bot.py 12 个 diff hunk 全部落在：LANGUAGES 注释+④（:299-452）、chunk_size 注释（:721-730）、NOVA 表/ENGINES ③①（:871-907）、组合根新增（:2661-2721）——`_build_nova_sonic_pipeline`/`AWSNovaSonicLLMService`/upsample（:7004-7067）**零 hunk**；MODELS（:3129/:5501）、`voices_for`、voice_store、store/session 零 hunk、零文件改动。6 条稳定红全为预存（见「四」） |
| (c) R-C-002 零默认零回退 + 遗留名永不解析 | ✅ | registry 未动（casefold 双层封杀保持）；`build_cascade_engine` api_key 缺省 "" → 适配器响亮报错（无静默回退）；`test_legacy_aws_names_never_resolve` 钉 transcribe/bedrock/nova-sonic 三方向（nova-sonic 经 REALTIME 选型变量 VOICE_ENGINE 被拒——组合根测试 :113-115） |
| (d) R-A-005 前半（超时/取消确定性） | ✅（附 F-11 尾巴） | 三段超时各有确定性测试（stt_start/llm_event/tts_event，配 cancel 断言 + `on_turn_error(stage)` 观测）；interrupt()/close() 取消路径、作废回合不发 end、会话复用均有钉。start 超时后的服务回收角见 F-11（非阻塞） |
| (e) 决策 (a) 边界合规 | ✅ | 选型面（ENGINES/DEFAULT_ENGINE/NOVA 三表/8 locale engines=[]）改 4 hunk，nova 代码路径、models[]、voice_store 确认 untouched——「选型面改动 vs 代码路径保留」分界干净，git history 回滚路径完整（③ hunk 注释明示 restore 来源）。④「条目本体不删」符合裁决口径；en-US 双表按 A-3 不动（hunk 注释 + `test_admin_options_payload` 双引擎断言保持） |
| (f) 测试重指忠实性 | ✅ | **test_engine_voice_patch 7 处翻转**：逐处核对——4 处「接受→400」（nova engine/table 退出的确定性后果，各带 ruling B 意图注），`test_validate_null_voice_clear...` 重构成 demo_engine 承载 nova（**"clear 永不按生效 engine 校验"的原规则保留**），`test_validate_engine_unset_lenient...` 保留「双表任一命中即过」规则只翻转 nova 表命中项，body-wins 合并规则注释明示不变；**test_admin_api 4 处平移**：PUT nova→400（**新增显式能力收缩钉**）+ pipeline 正常路径、demo PATCH 数据平移、工具回归方向（no tools→engine coupling）保留、别名/表清空断言改为 `{}` 钉——**全部为数据/极性随 C-3 状态的确定性翻转，被测规则无一漂移，函数名全保留**。brief 称「6 文件重指」实为 **5 文件**（git diff 权威；205 = 5 文件 191 + 新引擎测试 14，数字自洽） |

## 四、测试质量与独立执行

**质量面**：fake 全部按 FC ABC 真实现（FakeSTT/FakeLLM/FakeTTS 为契约子类），TTS fake 复刻 `stream_synthesis` 单块前瞻语义并有注释说明；演示 1 用事件序证明流式（首块音频 idx 先于末句 delta）、短语级喂入（tts.texts == 5 句末短语）、is_final 只落终块、单 end 收尾——是行为钉不是 mock 自证。工具演示钉幂等（executed==["call_1"]）+ U-102 消息形状逐轮回灌。组合根测试用 `__name__` 断言规避 reload 双类对象坑（有注释）。

**独立执行数字**（与 dev 自测、team-lead 抽验一致）：

| 执行面 | 命令 | 结果 |
|--------|------|------|
| U-103 小批 | `pytest test_cascade_engine test_composition_root` | **14 passed** ✓ |
| U-103 批（report §五 10 文件） | 同 report 命令 + `--capture=sys` | **226 passed** ✓ |
| ①-④ 受影响批 | 5 重指文件 + 2 新引擎文件 | **205 passed** ✓（5 重指文件单独 191 passed） |
| C2 两文件 | `pytest test_tool_filler test_mcp_integration` | **2 failed, 25 passed**（红名与错误原文 `FunctionCallParams ... pipeline_worker` 与挂账记录逐字一致） |
| 全量 | `pytest tests/ -q --capture=sys` | 3 次独立执行：稳定红恒为 C4×4+C2×2（6 条，**零新增稳定红**）；但每次另有 28–116 条 `test_admin_api` 系 ERROR/1 条 FAILED 飘忽出现，**已定性为环境假红**：traceback 直指沙箱文件代理 `sitecustomize.py:507`（`_brokered_path_mkdir` 宿主代执行 mkdir 失败，`FileNotFoundError ... .venv/tmp/pytest-of-root/pytest-0/...`），全部发生在 **tmp_path fixture 建目录/测试内 mkdir**（setup ERROR 或 `_setup_full_field_demo` 中招），错误集合逐次漂移（116→41→28），同批测试 scoped 全绿（191/98/226 三次复现）——与已记录的 basetemp/shim 假红同族（team-lead 环境怪癖注第 2 条），非代码缺陷。**归因口径**：981 passed = 966 基线 + 1（F-08 钉）+ 14（U-103 新增）算术自洽，逐批 scoped 全数复现达标 |

## 五、Phase B 批复核总结（并案）

**聚合面**：U-101/102/104+MiniMax（#9 批终版 **PASS**，u-101-102-104-review.md revision 2）｜U-105（must-fix **已收**：mcp 重钉 + import-smoke 进 RETAIN 闸门，#9 批验证记录 179 passed 含新钉）｜U-103（本评审 **PASS**）｜决策 (a) ①-④（裁决 B 执行 **PASS**，205 绿、重指忠实）。

**对 spec §14 Phase B completion conditions（Azure_Voicebot_Implementation_Instructions_Corrected.md:408）逐项核对**：

| 条件 | 状态 | 依据 |
|------|------|------|
| Implement interfaces | ✅ | FC-0..FC-8 冻结（Phase A PASS） |
| deterministic test fixtures | ✅ | 全链 fixture 驱动零凭据零网络；R-A-005 超时/取消确定性测试齐 |
| preserve business tools | ✅ | R-P-004 工具面（合并/注册/护栏）零改动，工具幂等+校验钩子有钉 |
| preserve regression tests | ⚠ **部分** | RETAIN 闸门绿（179+）；但 **C2 两文件 fixture 漂移红未修**（下述 blocking ③） |
| Add Azure adapters behind flags | ✅ | U-101/102 适配器经显式 registry 选型接入，未选不触达任何现有呼叫面 |
| Demonstrate local streaming / interruption / lifecycle cleanup / idempotent tools | ✅×4 | 四演示各有确定性测试（test_cascade_engine 10 条） |
| without committing credentials | ✅ | api_key 全构造注入，仓库零凭据，.env.example 占位符 |

**Phase B 能否收口：不能。Blocking 清单（3 项）**：

| # | 项 | 出处 | 状态 |
|---|----|------|------|
| 1 | **CR-C1** §11 凭据占位符（→构造注入/Key Vault 接管点） | 裁决上下文（阻 live smoke 与 Phase B 收口） | 未落地，续跟踪 |
| 2 | **CR-C2** pipecat-ai[azure] 部署钉时机（含桥接子类 live/import-smoke 收口） | 同上 | 未落地，续跟踪 |
| 3 | **C2 fixture 修复**：`test_tool_filler`/`test_mcp_integration` 的 `FunctionCallParams.pipeline_worker` fixture-only 修复 | Phase A 验收条件 2/3 延续，verification-u-101-102-104.md:25,165,183 明载「**不晚于 Phase B exit**」 | **未落地**（本次实测仍 2 红，错误逐字与挂账一致）——超出 team-lead 预期清单（CR-C1/CR-C2）之外的第 3 项，但白纸黑字期限如此；请显式处置：落地（修 2 处构造调用，代价极小）或书面展期至 U-303（ADAPT 港口），二选一，不得静默带过 |

**附注**（不计入 blocking）：F-11/F-12 随 U-103 后续或 U-201 前顺手落；`_validate_segment` 预存边角按裁决录 #18+U-501 不重开；ws/phone 传输集成按卡属 U-201..206（u-103-report §四.2 的边界声明成立）。

## 六、复评要求

1. 若 F-11/F-12 落地：各补 1 条测试，复评仅看新增 2 条 + start 超时回收路径。
2. C2 fixture 修复落地（若 team-lead 选落地）：复评 `test_tool_filler.py:243/286` 两处构造改法 + 两文件全绿即可销项。
3. CR-C1/CR-C2 属 CR 流程，不复评本批代码；CR-C2 落地时附 live/import-smoke 证据收口桥接验证边界。

---

## 复评（revision 2 · 2026-11-09）— C2 / F-11 / F-12 三项销项（修复环落地复评）

- 复评缘起：team-lead C2 裁决（落地不展期，「QA 報告到達後立即進修復環」）——修复环（#33）C2 兩處 + F-11 + F-12 同環落地，一次复评销三项。
- 修复环改动面（git 核对）：`tests/test_tool_filler.py`、`tests/test_mcp_integration.py`（C2，fixture-only）、`cascade_engine.py`（F-11，唯一生产代码改动）、`tests/test_cascade_engine.py`（+1 测试函数）、`tests/test_composition_root.py`（+1 测试函数）、`docs/azure/u-103-report.md`（自测记录快照一致性，见偏差 3）。**改动集恰为六文件白名单**（与 #34 归零复核边界口径一致），**无超范围改动**（bot.py / providers / store 面零变化，与 revision 1 围栏一致）。

### 销项表

| 项 | 落地形态（复评逐项核对） | 判定 |
|---|---|---|
| **C2** | `test_tool_filler.py:243-253` 構造補 `pipeline_worker=None`（why 注释：pipecat 1.4.0 必填字段，此路径只用 `params.llm`/`result_callback`）；`test_mcp_integration.py:228` 補 `pipeline_worker=None,  # required since pipecat 1.4.0; unused here`。git diff 逐 hunk 核对：**各恰 1 hunk、断言意图零动（fixture-only 属实）**。两文件全绿 **27 passed**（独立执行） | ✅ 销 |
| **F-11** | `cascade_engine.py:133-145` 修法与评审建议逐字一致：`try: await asyncio.wait_for(stt.start…)` → `except (TimeoutError, asyncio.CancelledError):` → `with contextlib.suppress(Exception): await self._stt.stop()` → `raise` 原样上抛；注释说明「为什么」（半开服务、close() `_started` 闸、重试被 "already started" 拒、二级异常不得遮蔽原异常）。`suppress(Exception)` 不吞 `CancelledError`（3.8+ BaseException）——取消语义保持。`close()`（:175-187）`_started` 闸验证：超时路径已 stop 后 close 不双调。回收测试 `test_cascade_engine.py:400-414` **真回归捕捉**（无修复时 `stopped==0` → :408 必挂）：钉 stopped==1 + close 干净收尾不双 stop + `_started is False` | ✅ 销 |
| **F-12** | `test_composition_root.py:122-126` 与建议逐字一致：`monkeypatch.delenv("PHONE_ENGINE", raising=False)` → `assert bot._RUNTIME_FALLBACK["phone"]["engine"] == "pipeline"`；注释注明 F-12 来由（DEFAULT_ENGINE 再变会静默带走 phone 默认）。裁决 B ② 四项交付物中最后一项行为钉补齐 | ✅ 销 |

### 补充裁断

1. **C2 原文更正采纳（记录）**：评审原文「test_tool_filler.py:243/286 两处构造」不确——grep 实证每文件恰 1 处 `FunctionCallParams(` 构造（test_tool_filler.py:243 / test_mcp_integration.py:223）；:286（现 :289 `assert inner_order.get("ran") is True, "inner (MCP) wrapper never ran"`）是**同因失败面**（构造 TypeError 被 pipecat 吞成 ErrorFrame → assert never ran），非第二处构造。一处修复两面归零。更正由 fullstack-dev 提供、复评实证采纳，原文口径以本条为准。
2. **新测计数=2**（team-lead 口径澄清：「新增 3 測試」系三修复项笔误）：F-11 回收测试 ×1 + F-12 行为钉 ×1，均独立函数。F-11 钉法取「stop 被调」而非「可重试」二选一——FakeSTT 无 "already started" 守卫，重试腿将为空断言（有无修复都过），回归捕捉集中在 stopped==1。**设计判断正确，接受**。
3. **e24 intentional flip（专段声明，防误读）**：F-11 修后 `stt.start` 超时路径 best-effort 调 `stt.stop()`（半开服务清理）——与 verification-u-103.md §三 e24 / 卡面验收条目 9 所钉现行为 `stt.stopped==0` 相反。**此为缺口本体的行为翻转，修复非回归**；QA 探针口径已同步更新（stop 被调/可重试，标注 intentional flip，team-lead 已同步验得明），两方文档口径一致。

### dev 偏差 4 项（team-lead 转来，按实物归因，不硬凑）

| # | 偏差 | 处置 |
|---|---|---|
| 1 | `tests/test_cascade_engine.py` +1 测试函数不在原五文件枚举内 | ✅ 接受——属派令「或等价钉落点」：FakeSTT fixture 唯一家园即该文件（F-11 回收测试的天然落点），仅追加 1 函数、既有测试零动；计入 u-103-report.md 后改动集恰合六文件白名单，非超范围 |
| 2 | 「:243/:286 两处构造」实测为一处 | ✅ 已更正记录（补充裁断 1：grep 实证每文件恰 1 处 FunctionCallParams 构造，:286 = 同因失败面）——原文计数以更正为准 |
| 3 | u-103-report.md 除 §四.1/§五 外另动 §三 两处一致性（测试计数 10→11 / 4→5；「未动」收束到 U-103 主体时点） | ✅ 接受——同文件自测记录快照一致性（亲验 :45 计数、:48 时点收束语），非范围外改动；#18 全局一致性扫尾职责不变 |
| 4 | F-11 测试只钉「stop 被调」（stopped==1）一腿；「可重试」腿未在 dev 测试落 | ✅ **分工明示**（按派令写明）：dev 测试钉 stopped==1（真回归捕捉，回归捕捉面集中于此）；「可重试」腿由 QA 探针 e24 重建的**脚本化 fake**（带 already-started 守卫）独立钉——两腿各有归属，生产测试零空断言混入 |

### 独立执行数字（QA basetemp 治法：跑前挪开 `$TMPDIR` 与 `.venv/tmp` 两处 `pytest-of-*` + 每次唯一 `--basetemp=.venv/tmp/cr-rev-$RANDOM`；全部 `--capture=sys`，全程 0 error）

| 执行面 | 范围 | 结果 |
|--------|------|------|
| 修复环 4 文件 | test_tool_filler + test_mcp_integration + test_cascade_engine + test_composition_root | **43 passed** ✓（27+16） |
| 契约批 10 文件 | #9/U-103 契约面（含 F-08 钉） | **228 passed** ✓（226+2，FC-0..FC-8 零破面维持） |
| ruling-B 面 7 文件 | 5 重指文件 + 2 新引擎文件 | **207 passed** ✓（205+2，重指规则无回退） |
| 全量 | `pytest tests/` | **4 failed, 985 passed** ✓——4 红恒为 C4×4（test_demo_store×1、test_history×2、test_migrate_demos×1，全部预存），**C2×2 归零**、**零新增红**；归因 985 = 981 + 2（C2 转绿）+ 2（新测） |

### 跟踪项状态（更新）

| # | 项 | 状态 |
|---|----|------|
| 1 | CR-C1 §11 凭据占位符（→构造注入/Key Vault 接管点，锚 #17） | 续跟踪（CR 流程，阻 Phase B 收口不阻施工） |
| 2 | CR-C2 pipecat-ai[azure] 部署钉 + live/import-smoke gate（锚 #16/#17） | 续跟踪（CR 流程，同上） |
| 3 | ~~C2 fixture 修复~~ | **本 revision 销**（全量 C2×2 转绿实证） |
| 4 | F-13（QA verification-u-103.md §七.6 转来）：400 detail 风格不一（`invalid engine: {v}` vs `unknown engine: {v!r} (allowed:…])`） | 非阻塞建议，随 U-201/后续清理落，不扩修复环面 |
| 5 | u-103-report §四.1 过时表述（QA §七.5） | **已提前兑现**——修复环内 dev 更新为裁决 B 快照（u-103-report.md:54 亲验）；#18 文档任务保留全局一致性扫尾 |

### Phase B 状态更新

「preserve regression tests」**翻 ✅**（C2 两文件转绿、契约批 228 全绿、全量零新增红）。**Phase B blocking 缩减为 2 项：CR-C1、CR-C2**（均 CR 流程跟踪、锚 #16/#17，不阻施工）。三项销项完成，本次复评闭环。

*评审执行：read-only 评审 + 独立跑测（3 轮全量 + 5 轮 scoped）；未改动任何实现/测试代码。本文件为唯一产出。*
