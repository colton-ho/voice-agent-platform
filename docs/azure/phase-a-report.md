# Phase A Close-out Report (R-O-005) — U-005

Voicebot AWS→Azure migration · Phase A（provider interfaces + config validation）· 2026-11-06
作者：fullstack-dev（寇豆码）· 环境：`.venv`（Python 3.13.12，pipecat-ai==1.4.0 钉定；deploy 用 3.12，见 requirements.lock 头注）
所有 pytest 运行均带 `--capture=sys`（本机 delete-guard 会击穿 fd-capture 的 tempfile，裸跑必崩——环境强制偏差）。

---

## 1. Baseline honesty（验收 1）

### 1.1 为什么没有"改动前树"的实测跑

卡面要求 pre/post 各跑一次全量。**pre 树实测不可行**，论证如下（git stash-free）：

1. Phase A 只新增未跟踪文件；唯一被改动的跟踪文件是 `.env.example`（CR-2.1 裁决批准的重写），见 §2 `git diff --stat`。因此**跟踪代码与 Phase A 前逐字节相同**——旧测试在本环境今天跑出的结果，就是 pre 树在同一环境下的结果（旧测试不读取 `.env.example`；读它的只有新测试 `tests/test_config_contract.py`）。
2. Phase A 之前**不存在任何可运行的 Python 环境**：仓库无根 lockfile（发现 A-4 gap），`.venv` 是 U-001 首建。"改动前跑一次"在物理上需要一个 Phase A 之前不存在的环境，任何构造出来的"pre 环境"本身就携带 Phase A 的 U-001 产物。

故以「跟踪树逐字节等价 + 首个环境如实记录」代替 pre/post 双跑，下面是**诚实的 post-change 实测数字**。

### 1.2 全量跑证据（pinned venv）

命令：`.venv/bin/python -m pytest tests/ -q -rs -rf --capture=sys --tb=no`

| 轮次 | 结果 | 用时 | 说明 |
|---|---|---|---|
| **R1（基准）** | **34 failed, 879 passed, 3 skipped, 0 errors** | 183.10s | fixture 全部成功建立的最完整执行（916 = 收集数）|
| R2（同命令复跑） | 31 failed, 661 passed, 224 **errors** | 71.94s | 224 errors 全部是 fixture **setup** 阶段失败，非测试体结果 |

R2 差异根因（已取 traceback 证据）：本机沙箱 broker 化的 `Path.mkdir` shim（`…/WorkBuddy.app/…/shim/sitecustomize.py:774`，`_brokered_path_mkdir`）**忽略 `exist_ok=True`**，路径已存在时抛 `FileExistsError: [Errno 17]`，且按 broker 窗口呈**非确定性**（同一命令不同窗口 0→224 errors 漂移）。这是环境缺陷，与代码无关；R1 为引用基准。辅助佐证：19 个非 RETAIN 文件单跑三次均出现 5 failed / 190 passed / **96 errors**（同一 shim，同一形态）。

**[环境指引补正 2026-11-06（QA 发现，见 verification-phase-a.md §四）]** 本机 pytest basetemp 落在 **`.venv/tmp/pytest-of-*`（非 `/tmp/`）**——fixture setup 出现 `FileExistsError` 群发时，挪开 `.venv/tmp/` 下陈旧 `pytest-of-*` 目录即可清障（QA 实测：挪出 10 个后 128 errors 全清）。完整跑测约定 = `--capture=sys` + 必要时挪 `.venv/tmp/pytest-of-*`。

### 1.3 RETAIN 子集（发现 §8 组 A–F、I 的 27 个具名文件）

命令：`.venv/bin/python -m pytest <27 个 RETAIN 文件> -q -rs --capture=sys --tb=line`
结果：**28 failed, 433 passed, 1 skipped in 52.29s**（R1 归因后为 26 failed / 435 passed / 1 skipped，差额 2 个即 §1.2 的 shim 漂移对）。

按组归因（R1 口径；组内其余全部通过）：

| 组 | 文件 | 红/绿 | 红的归因 |
|---|---|---|---|
| A. Twilio telephony | 3 | 10 红 | 全部 async 插件缺失（下表 C1）|
| B. Phone session & audio | 2 | 5 红 | 同 C1 |
| C. Call control & tools | 4 | 7 红 | 6×C1 + 1×C2（test_tool_filler）|
| D. Pure logic / latency / config | 7 | **全绿** | — |
| E. MiniMax TTS | 1 | **全绿** | — |
| F. MCP | 3 | 4 红 + 1 skip | 4×C1（test_mcp_runtime）；skip 见 §1.5 |
| I. Admin API / demo surfaces | 7 | **全绿** | （R1；子集跑曾 1×C3 漂移红）|

### 1.4 每个红测试的分类（预存 / 环境缺失 / Phase A 所致）

**Phase A 所致：0 个。** 跟踪代码零改动（§2），旧测试与 Phase A 产物无 import 关系。

| # | 分类 | 数量 | 证据 | 涉及 |
|---|---|---|---|---|
| C1 | **env-missing（测试工具缺口）** | 25（RETAIN）+3（非 RETAIN） | `_pytest/python.py:177: Failed: async def functions are not natively supported.` + `PytestUnknownMarkWarning: Unknown pytest.mark.asyncio`；环境装了 pytest==9.2.0 但**没有 pytest-asyncio**（requirements.txt:26 只列 pytest；仓库无 dev-requirements/CI 声明——grep README/.github/setup.cfg/pyproject/pytest.ini 零命中）**[更新 2026-11-06：C1 已由 CR-3.1 关闭——pytest-asyncio==1.4.1.post1 已补入 requirements.txt、requirements.lock 已按 pip freeze 重生成（108 pins，与 freeze 逐一致，评审已核）。本行数字为 CR-3.1 前历史记录，照录不重推（评审裁定）]**| test_twilio_endpoints(8)、test_twilio_integration(2)、test_phone_session(5)、test_call_control_tools(6)、test_mcp_runtime(4)、test_mcp_integration(≥1) |
| C2 | **预存（pipecat 1.4.0 API 漂移）** | 1 | `FunctionCallParams.__init__() missing 1 required positional argument: 'pipeline_worker'` → `AssertionError: inner (MCP) wrapper never ran`（tests/test_tool_filler.py:286）——测试钉的是 vendored 1.1.x 时代语义，与 deploy 钉定的 pipecat-ai==1.4.0（cloudformation.yaml:427）不兼容；正是 `docs/azure/pipecat-1.4.0-api-notes.md` §4 记录的漂移。任何符合 deploy 契约的环境在 pre 树上同样红 | test_tool_filler::test_integration_real_push_frame_reaches_downstream_before_inner |
| C3 | **env-flaky（沙箱 shim）** | 2（仅子集跑红，R1 绿） | fixture setup 抛 `FileExistsError`（sitecustomize.py:774 `_brokered_path_mkdir` 忽略 exist_ok）；见 §1.2 | test_mcp_config::test_failed_write_keeps_previous_file_intact、test_demo_loader_tools::test_last_skipped_reset_each_rescan |
| C4 | **预存（旧测试↔旧代码漂移）** | 3 | `AttributeError: 'HistoryRecorder' object has no attribute 'finalize'`（test_history.py:139，×2）；`AssertionError: assert {'asr_filter','kb_ack',…} == {'filler','gb_intro',…}`（test_demo_store.py:167，工具 ID 集不匹配）| test_history(2)、test_demo_store(1) |
| C5 | **env/依赖版本互作** | 1 | `ImportError: cannot import name 'streamablehttp_client' from 'mcp.client.streamable_http'`（pipecat/services/mcp_service.py:28）——pipecat 1.4.0 与解析到的 mcp SDK 2.3.0 命名互作问题 | test_mcp_sigv4::test_params_accepted_by_pipecat_mcpclient（§8 本就 REPLACE，决策 (c)）|
| C6 | **env-flaky（子进程/端口）** | 1 | test_mcp_integration 的 mock server 端口窗口（源码 skip 语句见 §1.5） | test_mcp_integration(其余) |

非 RETAIN 红（R1 共 8 个）全部落在 **12 ADAPT / 4 REPLACE-RETIRE** 名单内（§3），本就是 Phase D / cutover 重定向对象，不属 RETAIN 闸门项。

### 1.5 Skips（原因逐字）

- **捕获到的**：`SKIPPED [1] tests/test_admin_mcp_servers.py:402: FastMCP smoke server failed to start`（RETAIN 子集跑原文）。
- R1 共 3 个 skip；另 2 个的 summary 行在留存输出窗口之外（证据局限，如实声明）。全库 skip 点全集（源码枚举）：`test_admin_mcp_servers.py:402`（上句）、`test_chime_provisioner.py:388`（`"inline ZipFile not in cloudformation.yaml yet (added in T2)"`，确定性）、`test_mcp_integration.py:110/113`（`"mock MCP server process exited before opening its port"` / `"did not open its port within 10s"`，flaky）、`test_voice_store.py`/`test_migrate_demos.py` 的 `skipif(not _HAVE_MOTO)`（moto 5.2.5 在环境内，不触发）。R1 的另 2 个 skip 即 chime ZipFile 与 mcp_integration mock-port 二者之中（复跑尝试被 §1.2 的 shim 漂移吞成 setup error，未能再现）。

---

## 2. Isolation（验收 2）

`git diff --stat`（跟踪文件，全文）：

```
 .env.example | 55 ++++++++++++++++++++++++++++++++++++++++++++++++-------
 1 file changed, 48 insertions(+), 7 deletions(-)
```

唯一改动的跟踪文件是 `.env.example`（U-003 产物，**CR-2.1 裁决批准原样保留**）。**零 app-code 修改**（bot.py、根模块、voice-server/、deploy/、46 个旧 tests 文件逐字节不变）。

Phase A 新增文件（全部未跟踪，与卡面清单一致）：

- `requirements.txt`、`requirements.lock`
- `providers/`（`__init__.py`、`base.py`、`registry.py`）、`storage/`（`__init__.py`、`base.py`）
- `config_contract.py`
- `tests/test_pinned_pipecat.py`、`tests/test_provider_contracts.py`、`tests/test_storage_contracts.py`、`tests/test_config_contract.py`、`tests/test_provider_registry.py`
- `docs/azure/pipecat-1.4.0-api-notes.md`、`docs/azure/*.md`（规划文档）+ 本报告

**非 Phase A 产物（如实申报，均未跟踪）**：
- 9 个沙箱 canary 文件（`2jyh9mb7` 等，各 4 字节 `blat`）——沙箱工具层探针，非团队产物，未触碰；
- `config/runtime.json`——旧 `runtime_config.py`（其 docstring:9 声明存储于此）在旧测试**首次访问时 seed** 的运行时配置（`docs/onboarding/05-config-and-env.md:175` 记载该行为；时间戳落在首轮 RETAIN 跑窗口），属测试运行遗留物，deploy 脚本本就排除（deploy.sh:79）；未删除（delete-guard）亦无需删除。

---

## 3. Test inventory update（R-T-015）

发现 §8 清单追加 5 个新测试文件（Phase A 冻结产物，纯 stdlib/本地依赖，不触 AWS 路径）：

| 新文件 | 测试数 | 所属卡 | 作用 |
|---|---|---|---|
| tests/test_pinned_pipecat.py | 2 | U-001 | pipecat 1.4.0 版本钉 + vendored 命名空间陷阱守卫 |
| tests/test_provider_contracts.py | 23 | U-002 | FC-0..FC-5、FC-8 结构契约 + 导入纯度 |
| tests/test_storage_contracts.py | 15 | U-002 | FC-6/FC-7、CallState、三个异常 |
| tests/test_config_contract.py | 83 | U-003 | §11 验证矩阵逐行 + 无回退/无遗留变量 |
| tests/test_provider_registry.py | 40 | U-004 | §3.4 注册表显式选择语义 |

**分类重申**：30 RETAIN / 12 ADAPT → 用户 Phase D / 4 REPLACE-RETIRE 于 cutover + `test_phone_numbers` 重定向（Chime→Twilio，决策 (d)）。

**口径注记（诚实优先）**：§8 表格逐行点名的 RETAIN 是 **27** 个文件（A–F、I），总数行写 **30**——差额 3 个是 F 组混排的 `test_mcp_integration`(ADAPT)、`test_mcp_sigv4`(REPLACE) 与 H 组 `test_migrate_demos`(REPLACE) 被总数行计入 RETAIN 所致；本报告以逐行具名为准跑闸门，全量跑覆盖任何口径。且"30 RETAIN green"在本环境**不是字面成立**：C1/C2 红项见 §1.4，其根因是环境工具缺口与预存漂移，非 Phase A 引入——按 U-005 诚实规则，如实记录优先于绿色宣称。

---

## 4. Close-out（R-O-005）

### 4.1 Changed files

见 §2（跟踪：仅 `.env.example`，CR-2.1 批准；新增 15 个文件/目录）。另有 4 处授权内的既有文档更新：`docs/azure/implementation-plan.md` U-002 卡 CR-2 偏差行、`docs/azure/dependency-matrix.md` Contract rulings（solution-architect 维护）、CR-1 注释行（requirements.txt，新文件）。

### 4.2 Commands actually executed + results（全部在 pinned `.venv`）

| 命令（节选参数） | 结果 |
|---|---|
| `pytest tests/test_pinned_pipecat.py -q`（U-001 自测） | 2 passed |
| `pytest tests/test_provider_contracts.py tests/test_storage_contracts.py -q`（U-002 自测） | 38 passed |
| `pytest tests/test_config_contract.py -q`（U-003 自测） | 83 passed |
| `pytest tests/test_provider_registry.py -q`（U-004 自测） | 40 passed |
| `pytest <4 个新套件> -q`（CR-2 回归） | 161 passed |
| `pytest tests/ --collect-only -q` | 916 tests collected（755 旧 + 161 新）|
| `pytest tests/ -q -rs -rf`（U-005 自测 R1） | **34 failed, 879 passed, 3 skipped**（§1.2）|
| `pytest tests/ -q -rs -rf`（R2 复核） | 31 failed, 661 passed, 224 errors（shim 漂移，§1.2）|
| `pytest <27 RETAIN> -q -rs` | 28 failed, 433 passed, 1 skipped（§1.3）|
| `git diff --stat` / `git status --porcelain` | §2 |
| 失败归因抽样 `pytest <node-id> --tb=line/short` | §1.4 各分类证据 |

### 4.3 Unresolved blockers（未决）

- **决策 (a)–(e)**（Nova 引擎处置、/setup 归宿、MCP sigv4 降级、Chime→Twilio 话号来源、asr-test 退役）：**用户已确认（2026-11-06），全部 FINAL**——见 implementation-plan.md §7 ADR register。要点：(a) nova-sonic 移出基线（`engines[]`→`[pipeline]`、`PHONE_ENGINE` 默认→`pipeline`、8 个 Nova-only 语言退出电话默认，AWS 回滚路径不动）；(b)(c)(d)(e) 按 discovery §12 原建议确认。
- **A-1**（SDK vs stdlib 签名验证）：**已定（2026-11-06）**——Azure 路径采用官方 twilio SDK `RequestValidator`；`twilio_sig.py` 保留为独立测试基准（test oracle）；`twilio` SDK 依赖于 Phase C / U-201 进入（Phase A requirements 不含）。
- **A-2**：**已定（2026-11-06）**——外部 Connect Flow Lambda 由仓内 Twilio 转接流程（spec §6.4）替代，cutover 时退役；R-P-005 验收 = Phase C 测试（含 no-answer/busy/failure）。**A-3**（语言范围）：**已定（2026-11-06）**——「全部保留、验收测 4」：所有现有 `LANGUAGES` 条目在两条通路继续可用（含 en-SG，零产品变化）；验收测试覆盖 4 个商务语言 zh-HK/zh-CN/en-US/ja-JP（spec §15，R-T-004）；8 个 Nova-only 语言随决策 (a) 随 nova-sonic 退出电话默认。**决策登记表零开放项。**
- **G-1/G-2**（1.4.0 适配器可用性 / 第三方批准）：U-001 只完成 G-1 第 1 步（wheel 实证记录）；MiniMax 续用待 G-2。
- **C1 测试工具缺口**：pytest-asyncio 未装（28 个 async 测试红）。补装需改 requirements.txt + 重生成 lock——CR-1 冻结 lock 口径待裁决，未擅动。**[更新 2026-11-06：C1 已由 CR-3.1 关闭]**：pytest-asyncio==1.4.1.post1 已补入 requirements.txt、requirements.lock 已按 pip freeze 重生成（108 pins，与 freeze 逐一致，评审已核）；CR-3.1 后 RETAIN 红 28→4、全量红 34→11，28 个 async 红全清。上文历史跑数为 CR-3.1 前记录，照录不重推（评审裁定）。
- **C5 mcp SDK 互作**：pipecat 1.4.0 ↔ mcp 2.3.0 命名冲突——**已裁决（CR-3）并入计划**：列为 Phase B 必修项 implementation-plan.md **U-105**（安装 pipecat 1.4.0 支持的 mcp 版本 + 重生成 lock + `import pipecat.services.mcp_service` import-smoke 进入 RETAIN CI 闸门）；保护活路径 `MCPClient`（bot.py:1909）。

### 4.4 Manual prerequisites（人工前置）

- 用户确认 §5 决策闸门（(a)–(e) + A-1）——**已完成（2026-11-06，含 A-2）**。
- Azure 订阅/资源（Speech、OpenAI、Cosmos、Blob、Service Bus、Key Vault、App Insights）与 Twilio 账户的实配——本阶段零云交互。
- 真实密钥/端点填入 §11 占位符（`.env.example` 仅占位，无真实值）。
- 3.12 部署环境的 lock 复核（本地 lock 基于 3.13.12，头注已声明差异）。

### 4.5 NOT verified（不得宣称生产就绪）

**未验证**：任何 Azure 服务集成、任何真实通话/媒体流、任何部署（deploy/ 保持 AWS 冻结回滚路径）、延迟/容量/SLO、鉴权与密钥轮换实测、灾难恢复。本阶段产物是**契约与配置骨架**，spec「无证据不得宣称生产就绪」在此逐字适用。

---

## 5. Decision gate note（验收 5）

**决策 (a)–(e) 与 A-1 当前运行于 spec §6 的 LOCAL 开发默认值之上，必须经用户确认后 Phase A 才能宣告关闭。** 本报告交付 ≠ Phase A 关闭；随后进入 code review 与独立 QA（任务 #6/#7），其结论亦为关闭前提。

**[更新 2026-11-06：用户已确认 (a)–(e)、A-1、A-2、A-3（已并入 implementation-plan.md §7 ADR register，状态 FINAL）；本条"用户确认"前置条件已满足，决策登记表零开放项（A-3 =「全部保留、验收测 4」）。Phase A 关闭仍待 code review 与独立 QA（任务 #6/#7）结论。]**
