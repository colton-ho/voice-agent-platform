# 独立验收报告 · Phase A — Provider interfaces + config validation (U-001..U-005)

- 验收人：验得明（qa-verifier，独立验证）· 日期：2026-11-06
- 验收对象：Phase A 冻结产物（`providers/base.py`、`providers/registry.py`、`storage/base.py`、`config_contract.py`、`requirements.txt`、`requirements.lock`、`.env.example`、5 个新测试文件）
- 依据：`docs/azure/implementation-plan.md` U-001..U-005 卡面验收条目 · spec §5/§11/§15 · `docs/azure/phase-a-report.md`（实现自述）· `docs/azure/reviews/phase-a-review.md`（评审 revision 3 = PASS，仅作对照，不作依据）
- 环境：`.venv`（Python 3.13.12，pytest 9.2.0，pipecat-ai==1.4.0，mcp==2.3.0）；deploy 目标 3.12 差异见 `requirements.lock:5-9` 头注
- 约定：所有 pytest 带 `--capture=sys`（本机 delete-guard 击穿 fd-capture 的 tempfile 清理，裸跑必崩）；不 commit/push、不触云资源、不改 AWS 路径

**立场声明**：本报告不采信实现者/评审者的任何报数。下文每条结论均附本人亲自执行的命令与输出摘录（executed），或本人逐行读码核对（read）；两类来源逐条标注。

---

## 一、验收结论

### 结论：**PASS-WITH-CONDITIONS**（通过，附 2 项骑带条件）

Phase A 产物**可用**：170 条新测试全绿、27 文件 RETAIN 回归红名单与预先裁决的预期逐名相符（4 红全部归因为预存/互作缺陷，**Phase A 所致 = 0**）、隔离围栏成立、配置契约/注册表的负面与边界行为经我自设计的 43 条抽样逐一验证成立。

骑带条件（均为已被裁决跟踪的遗留项，**不要求 Phase A 返工**，但必须按期落地）：
1. **U-105**（mcp↔pipecat 互作）：当前钉定对 pipecat-ai==1.4.0 + mcp==2.3.0 使**线上 MCP 工具路径**（`bot.py:1908-1909` `from pipecat.services.mcp_service import MCPClient`）import 即炸——Phase B 必修，含 import-smoke 进 RETAIN CI 闸门；
2. **C2 fixture 修复**（`tests/test_tool_filler.py` 的 `FunctionCallParams` 构造漂移）：不晚于 Phase B exit（该文件已重分类 ADAPT，届时 RETAIN 集成为 CI 闸门）。

### U-001..U-005 逐条映射（卡面验收条目 → 证据 → 判定）

| 卡 | 验收条目（卡面原文要点） | 证据（我方执行/读取） | 判定 |
|---|---|---|---|
| U-001 | ① venv 记录解释器差异 | **read** `requirements.lock:5-9` 头注明载「deploy target is Python 3.12 … No 3.12 interpreter … must be regenerated in CI」 | ✅ |
| U-001 | ② `pip install -r` 可装 + collect 可收全 | **executed** `pytest tests/ --collect-only -q` → **923 tests collected**（51 文件 = 46 旧 + 5 新；报告 916 = pre-CR-3.1，+7 revision-3 新钉 = 923，账目相符） | ✅ |
| U-001 | ③ `test_pinned_pipecat.py` 绿（版本 + import 源） | **executed** 170 套件含 2 条通过；另独立执行 `python -c "import pipecat"` → `version: 1.4.0`、`file: .venv/lib/python3.13/site-packages/pipecat/__init__.py`、`in-site-packages: True`、`not-repo-vendored: True` | ✅ |
| U-001 | ④ api-notes 六类诚实标注在/不在 | **executed** grep：`AzureSTTService/AzureTTSService/AzureHttpTTSService/AzureLLMService/AzureRealtimeLLMService/TwilioFrameSerializer` 六节齐全，均标 PRESENT 并附 introspection 转录（`pipecat-1.4.0-api-notes.md:34,61,87,109,134,154`） | ✅ |
| U-001 | ⑤ 无跟踪文件修改 | **executed** `git diff --stat` → 仅 `.env.example`（见 U-005 ② 围栏注记） | ✅（注 1） |
| U-002 | ① 八接口名冻结拼写 | **executed** 170 套件（名字钉 + `__abstractmethods__` 双向钉全绿）+ **read** `providers/base.py`/`storage/base.py` 类名逐个核对 | ✅ |
| U-002 | ② ABC 拒绝直接实例化 + stub 暴露全部抽象成员 + 能力标志 | **executed** 套件绿；我方 `test_q6_*` 亲证 `CallRepository()/SessionDirectory()/TelephonyAdapter()` 抛 `TypeError` | ✅ |
| U-002 | ③ import 纯度 | **executed** `grep -nE "^(import\|from) (pipecat\|boto3\|botocore\|aioboto3\|azure)" providers/base.py storage/base.py` → 零命中；全量 import 清单仅 stdlib（abc/collections.abc/dataclasses/typing/datetime/enum） | ✅ |
| U-002 | ④ 仅新文件 | **executed** `git status --porcelain tests` → 仅 5 个新文件 untracked，46 旧测试文件零改动 | ✅ |
| U-002 | CR-2 偏差（`provider_ids` 放宽） | **read** `storage/base.py:55` 带 CR-2 引注，其余字段与冻结骨架一致（v14 表 3.3 逐行比对） | ✅ |
| U-003 | ① 违规一次列全 + 占位符含义 | **executed** 自设计 Q1×2：5 类违规同抛一检（`len(violations)==5`）、跨类别聚合（缺 required + 缺 TWILIO 条件对 + 坏 bool = 4 条全列），`ConfigError.violations` 逐条含变量名+含义 | ✅ |
| U-003 | ② 选择变量缺失→启动失败、零回退 | **executed** 自设计 q5：legacy-only 环境（AWS_REGION/USERS_TABLE/…）报错**只列 §11 名**、legacy 名不出现（loader 根本不读）；170 套件含逐变量 unset 钉 | ✅ |
| U-003 | ③ 两 bool 默认 false 且仅解析暴露 | **executed** 自设计 Q2：`cfg().recording_enabled is False`、`cfg().voice_live_enabled is False`；`"true"/"false"` 字面量精确命中 | ✅ |
| U-003 | ④ legacy-only 环境失败并指名 §11 名 | 同②（同一条自设计用例） | ✅ |
| U-003 | ⑤ `.env.example` 仅占位符、零密钥 | **executed** 密钥样式扫描（AKIA/sk-/PEM/passw/secret/api_key/token 等，含大小写不敏感广扫）→ **零命中**；`diff` 占位符名集 vs spec §11 dotenv 块名集 → **30 名逐一致** | ✅ |
| U-003 | 矩阵 23/2/2/3 冻结 | **read** `config_contract.py:46-58`：`_REQUIRED` 23 项、`_CONDITIONAL_TWILIO` 2 项、`_DEFAULTED_FALSE` 2 项、`_OPTIONAL` 3 项，`AppConfig` 30 字段一一对应 | ✅ |
| U-004 | ① create 严格按 §11 变量派生选择；缺→指名变量 | **executed** 自设计 q4：`MissingSelectionError.variable == "LLM_PROVIDER"` 且报文含变量名 | ✅ |
| U-004 | ② 未知名→列已注册名；bedrock/aws_transcribe/polly/nova-sonic 显式拒绝 | **executed** 自设计 q4×6 变体注册拒绝（`Bedrock/Polly/NOVA-SONIC/Aws_Transcribe/MiNiMaX` 经 casefold 双层拒）+ 解析拒绝 + **工厂零调用**（`calls == []`）+ 走私直塞 `_factories` 仍被 resolve 层拦截；未知名报文列已注册名 | ✅ |
| U-004 | ③ cascade 组合 FC-2/3/4；其他值按 REALTIME 解析、未注册即败 | **executed** 自设计 q4：cascade 返回三元组、三工厂各调一次；`LLM_PROVIDER=nope` 时全组合失败（tts 零调用，all-or-nothing）；`VOICE_ENGINE=voice_live` 走 REALTIME 名解析，未注册名响亮失败 | ✅ |
| U-004 | ④ 显式注册、重复注册抛错、产物过契约检查 | **executed** 自设计 q4：重复注册 `ValueError`；工厂返回 `object()` → `TypeError`（鸭子类型伪适配器拒绝） | ✅ |
| U-005 | ① RETAIN 基线诚实（红=预存+证据） | **executed** 27 文件回归 `4 failed, 457 passed, 1 skipped`（详见 §四，4 红逐名逐证归因，Phase A 所致 = 0） | ✅ |
| U-005 | ② 隔离围栏 | **executed** `git diff --stat` → 仅 `.env.example`（48+/7-，CR-2.1 批准重写）；`git status --porcelain bot.py voice-server deploy tests` → bot.py、voice-server/、deploy/、46 旧测试零改动（仅 5 新测试 untracked）；9 个 canary 各 4 字节如述 | ✅ |
| U-005 | ③ 测试清单更新（R-T-015） | **read** `phase-a-report.md §3`：5 新文件 170 用例入册、27 具名 RETAIN 口径如实（30 vs 27 差额已声明） | ✅ |
| U-005 | ④ close-out 五节齐全 | **read** `phase-a-report.md` §1–§5（baseline honesty / isolation / inventory / close-out / decision gate） | ✅ |
| U-005 | ⑤ 决策闸门注明 | **read** `phase-a-report.md §5` + `implementation-plan.md §7`：(a)–(e)、A-1/A-2/A-3 全部 FINAL、登记表零开放项 | ✅ |

注 1：U-001 ⑤/U-005 ② 卡面写「no modified tracked files」，而 `.env.example` 是被修改的跟踪文件——此为 CR-2.1 裁决批准的重写（卡面产出清单本就含 `.env.example`）。围栏实义 = **除 `.env.example` 外零跟踪文件改动**，我方按此口径核验成立。措辞差异记录在案，不构成缺陷。

### spec §15 相关行映射（诚实口径）

spec §15 是验收矩阵（证据多需真实通话/部署），全部 14 行的实证归属 Phase F（U-501）。Phase A 的贡献是**冻结这些行赖以成立的契约与配置**，映射如下（Phase A 判定 ≠ §15 行验收通过）：

| spec §15 行 | Phase A 贡献（本阶段验证到的） | §15 行实证状态 |
|---|---|---|
| Azure hosting（无 AWS 运行时依赖） | 配置契约拒绝 legacy AWS 变量、注册表永拒 AWS 遗留名（双层，含大小写变体） | 待 Phase E/F |
| Callbacks（签名拒绝/去重/晚到） | FC-8 `SignatureValidator` 可插拔 seam 冻结 | 待 Phase C/F |
| Tools（schema/授权/幂等） | `ToolSpec`/`ToolRequest` 值类型冻结 | 待 Phase C/D/F |
| Interruption / Audio / Languages / Handoff / Scaling / Failure / Deployment / Data / Privacy / Cost / Rollback | FC-1..FC-5 动词集 + `CallState` 7 态 + FC-6/FC-7 语义冻结（含乐观并发/去重/终态语义） | 待 Phase B–F |

**不得宣称生产就绪**（spec §17.9）——本报告只证明契约与配置骨架可用。

---

## 二、executed vs read 声明

**By execution（命令均在 `/Users/colton/Developer/voice-agent-platform` 执行）：**
1. `pytest tests/test_pinned_pipecat.py tests/test_provider_contracts.py tests/test_storage_contracts.py tests/test_config_contract.py tests/test_provider_registry.py -q --capture=sys` → **170 passed**，exit 0。
2. `pytest <27 RETAIN 文件> -q -rs -rf --capture=sys --tb=line`（两轮）→ 首轮 `4 failed, 330 passed, 128 errors`（shim 缺陷，见 §四注）；清理陈旧 pytest-of 目录后 → **`4 failed, 457 passed, 1 skipped in 49.05s`**，红名与预期逐名相符。
3. `pytest tests/test_mcp_runtime.py tests/test_tool_filler.py` 单独跑 → 同 4 红复现；`--tb=short` 抓取两处报错原文（§四）。
4. `python -c "import pipecat.services.mcp_service"` → `ImportError: cannot import name 'streamablehttp_client' from 'mcp.client.streamable_http'`（C5 一手复现）。
5. `git diff --stat` / `git status --porcelain`（含定向 `bot.py voice-server deploy tests`）/ canary 文件 `wc -c` → §一 U-005 ②。
6. `diff <(pip freeze \| sort) <(grep '^[A-Za-z0-9]' requirements.lock \| sort)` → **identical（LOCK-FAITHFUL）**，108 pins。
7. `.env.example` 密钥扫描（严格 + 大小写不敏感两轮）→ 零命中；占位符名集 vs spec §11 `diff` → 30 名一致。
8. import 纯度 grep（U-002 自测命令原样）→ 零命中。
9. `python -c "import pipecat"` → 1.4.0 / site-packages / 非 vendored。
10. `pytest tests/ --collect-only -q` → **923 collected**。
11. 自设计负面/边界套件 43 条（`/tmp/qa_phase_a_negative_test.py`，临时文件，跑毕已 `rm` 删除成功）→ **43 passed**。

**By reading（逐行核对）：**
- `config_contract.py` 验证矩阵四类计数（23/2/2/3）、`AppConfig` 30 字段、`_fmt` 错误文案四要素（变量名/合法字面量/大小写敏感/收到值）；
- `providers/registry.py` §3.4 语义（kind→变量映射、cascade 组合分支、casefold 双层遗留名拒绝、注册表契约检查）；
- `providers/base.py`/`storage/base.py` 类/方法集 vs implementation-plan §3 骨架（FC-0..FC-8 全量）、`storage/base.py:55` CR-2 偏差引注、`CallState` 7 态与三异常（`storage/base.py:15-40`）；
- `.env.example` 内容（占位符+注释，无真实值）、`requirements.txt` CR-1/CR-3 引注、`requirements.lock` 头注；
- `phase-a-report.md` §1.3/§4.5、`phase-a-review.md` revision 3、`implementation-plan.md` 卡面与 §7 ADR、spec §5/§11/§15；
- `pipecat-1.4.0-api-notes.md` 六节在/不在标注；`bot.py:1908-1909` MCPClient 线上路径（sed 亲读）。

**未采信**：fullstack-dev 的自测报数与 code-reviewer 的 PASS 结论仅作对照，所有数字均我方重跑得出。

---

## 三、自设计负面/边界用例清单（43 条，逐项结果）

实现方 170 条测试全绿只证明「实现者想到的场景」；以下为**我独立设计**的抽样，重点打实现者未覆盖或可能想当然的路径。临时文件 `/tmp/qa_phase_a_negative_test.py` 跑毕即删（删除成功，无留置）。

| 组 | 用例（节选） | 类型 | 结果 |
|---|---|---|---|
| Q1 聚合 | 缺 1 required + 空白值 + 坏 int + 负 int + 坏 bool → 一次抛 5 条违规，条条在列 | 边界 | ✅ |
| Q1 聚合 | 跨类别（缺 required + 缺 TWILIO 条件对×2 + 坏 bool）→ 一次 4 条全列 | 异常 | ✅ |
| Q2 严格 bool | `True/TRUE/yes/1/0/off/tRuE/no` 逐一拒绝（8 变体）；报文含变量名+`'true'/'false'`+`case-sensitive`+收到值（四要素） | 异常×8 | ✅ |
| Q2 严格 bool | `true/false` 精确命中；两 bool 默认 false | 功能 | ✅ |
| Q2 信息项 | `"  false  "` 去空白后**被接受**——全 loader 统一 `.strip()` 归一化语义（与「空白=未设」一致），记录为语义说明而非缺陷（注 2） | 边界 | ✅（如述） |
| Q3 MAX_* | `0/-1/abc/1.5/1e3/空白` 六变体拒绝；`"1"` 临界接受且为 int；`86400` 大值接受 | 边界×8 | ✅ |
| Q4 注册拒绝 | `Bedrock/Polly/NOVA-SONIC/Aws_Transcribe/MiNiMaX` 注册即拒（casefold）；`"  "` 空白名 `ValueError` | 异常×6 | ✅ |
| Q4 解析拒绝 | `STT_PROVIDER=Bedrock` → `UnsupportedProviderError`（指名 STT_PROVIDER）且**工厂零调用**（无回退实证） | 异常 | ✅ |
| Q4 走私 | 直塞 `_factories` 绕过 register 的 `bedrock` → resolve 层照样拒（双保险名副其实） | 异常 | ✅ |
| Q4 缺选择 | 空选择（绕过 config 层）→ `MissingSelectionError`，`.variable` 精确指名 | 异常 | ✅ |
| Q4 未知名 | `espeak` → 报文含已注册名 `azure_speech`（名单真的列出） | 异常 | ✅ |
| Q4 注册纪律 | 重复注册 `ValueError`；工厂返回 `object()` 伪适配器 → `TypeError` | 异常×2 | ✅ |
| Q4 cascade | 三工厂各调一次返回三元组；`LLM_PROVIDER=nope` 全组合失败且 tts 零调用（all-or-nothing） | 功能+异常 | ✅ |
| Q4 REALTIME | `VOICE_ENGINE=voice_live` 走 FC-5 解析；`something_new` 响亮失败 | 功能+异常 | ✅ |
| Q5 config | 非 twilio 时 TWILIO 对→None（条件必填真的只对 twilio 生效）；twilio 缺对→两变量齐报 + `required when TELEPHONY_PROVIDER=twilio` | 边界×2 | ✅ |
| Q5 config | legacy-only 环境：报错只含 §11 名，`AWS_REGION/USERS_TABLE` 零出现（loader 不读 legacy） | 异常 | ✅ |
| Q5 config | 三个 optional 空白→None 归一；正例返回 30 字段齐、int 已解析 | 边界 | ✅ |
| Q6 storage | `CallState` 恰 7 态（与 spec §8.1 同集）；`CallRepository/SessionDirectory/TelephonyAdapter` 直接实例化抛 `TypeError` | 功能×3 | ✅ |

**合计 43 条全过（43 passed, 0 failed）。** 首轮 5 红均为**我方测试脚手架自身缺陷**（`env()` 键为大写 §11 名，我误传小写 kwarg 致覆盖静默失效）——已修正后复跑全绿；如实记录，不讳。

注 2：唯一语义观察——严格 bool 对**空白包裹的合法字面量**（`" true "`）接受（先 `.strip()` 后匹配）。与其余变量的「空白=缺失」归一化一致，属可辩护的宽容解析；若产品要求逐字节严格，可在 Phase B 收紧并补钉。不构成 U-003 ③/⑤ 违规。

---

## 四、RETAIN 回归红名单逐个归因（4 红 + shim 注）

命令：`.venv/bin/python -m pytest <27 文件> -q -rs -rf --capture=sys --tb=line` → **4 failed, 457 passed, 1 skipped**（462 = 27 文件用例全集）。**Phase A 所致红 = 0**（跟踪代码零改动、新旧测试无 import 交集，机制上不可能由 Phase A 引入；下述 4 红均有一手报错证据）。

| # | 测试 | 归类 | 报错证据（file:line） | 裁决状态 |
|---|---|---|---|---|
| 1 | `test_mcp_runtime::test_connect_failure_is_swallowed` | **C5 依赖版本互作** | `ImportError: cannot import name 'streamablehttp_client' from 'mcp.client.streamable_http'` @ `.venv/…/pipecat/services/mcp_service.py:28`（`from mcp.client.streamable_http import streamablehttp_client`），环境 mcp==2.3.0（`pip show mcp`）；我方 `python -c "import pipecat.services.mcp_service"` 一手复现同错。**非仅测试问题**：`bot.py:1908-1909` 线上 MCP 工具路径同 import 即炸 | 已裁决 **U-105**（Phase B 必修，CR-3），条件 1 |
| 2 | `test_mcp_runtime::test_connect_collects_tools` | 同 C5 | 同上（同一 import 炸点） | 同 U-105 |
| 3 | `test_mcp_runtime::test_connect_skips_disabled_server` | 同 C5 | 同上 | 同 U-105 |
| 4 | `test_tool_filler::test_integration_real_push_frame_reaches_downstream_before_inner` | **C2 预存 API 漂移**（pipecat 1.4.0 vs 测试钉的 vendored 1.1.x 语义） | 失败断言 `tests/test_tool_filler.py:286` `assert inner_order.get("ran") is True, "inner (MCP) wrapper never ran"`；根因（捕获 stderr 一手）：`ERROR pipecat.processors.frame_processor:push_error_frame:697 - _DriverLLMService#0 exception (tests/test_tool_filler.py:243): Error processing frame: FunctionCallParams.__init__() missing 1 required positional argument: 'pipeline_worker'`——1.4.0 的 `FunctionCallParams` 新增 `pipeline_worker` 形参，测试 fixture 未跟；任何符合 deploy 钉（cloudformation.yaml:427 = pipecat-ai==1.4.0）的环境在 pre 树同样红 | 已重分类 **ADAPT**，fixture-only 修复不晚于 **Phase B exit**，条件 2 |

skip 1 = `tests/test_admin_mcp_servers.py:402: FastMCP smoke server failed to start`（子进程 mock server 未起，环境性 flaky，与 phase-a-report §1.5 所录逐字一致）。

**首轮 128 errors 归因（环境缺陷，非代码）**：首轮 27 文件跑出 `4 failed, 330 passed, 128 errors`——128 个 error 全部是 **fixture setup 阶段** `FileExistsError: [Errno 17] EEXIST`（10 个 tmp_path 相关文件：test_mcp_config×45、test_admin_mcp_servers×19、test_demo_translate×17、test_demo_loader_tools×15、test_admin_rest_tools×12、test_runtime_config×6、test_latency_log×5、test_latency_report×4、test_demo_loader×3、test_latency×2）。一手 traceback 指认：沙箱 broker 化 `Path.mkdir` shim（`/Applications/WorkBuddy.app/…/shim/sitecustomize.py:774 _brokered_path_mkdir`）在 `exist_ok=True` 时仍对已存在目录抛 EEXIST（shim 分支 `if not exist_ok and os.path.exists(...)` 漏掉 exist_ok=True 的正常路径，直接把 mkdir 发给 host broker 被拒）。**关键发现**：本环境 pytest basetemp 落在 `.venv/tmp/pytest-of-*`（非 `/tmp/`，`TMPDIR` 虽指 `/var/folders/…` 但 shim broker 归一到 venv 内）——把 `.venv/tmp/` 下 10 个陈旧 `pytest-of-*` 目录 mv 挪开后复跑，128 errors 全部消失，得干净的 `4 failed, 457 passed, 1 skipped`。此发现补正了环境约定中「挪 `/tmp/pytest-of-*`」的指引（实际要挪 `.venv/tmp/pytest-of-*`），建议同步给全队。

---

## 五、NOT verified（不得宣称生产就绪）

沿用 `phase-a-report.md §4.5` 口径，我方独立确认以下**均未验证**：

- 任何 **Azure 服务真实集成**（Speech/LLM/Cosmos/Blob/Service Bus/Key Vault/App Insights 零云交互）；
- 任何**真实通话/媒体流**（Twilio webhook/WS/μ-law 编解码未上真线）；
- 任何**部署**（`deploy/` 保持 AWS 冻结回滚路径，未触碰；3.12 部署环境 lock 复核未做）；
- **延迟/容量/SLO**（零负载数据，spec §15「Scaling」行实证缺位——spec 明令不得发明数值 SLO）；
- **鉴权与密钥轮换实测**、灾难恢复；
- Pipecat 1.4.0 六适配器的**运行时行为**（api-notes 只证「在/不在 + 签名内省」，不证可跑通）。

本阶段产物是**契约与配置骨架**。「无证据不得宣称生产就绪」（spec §17.9）在此逐字适用。

---

## 六、遗留事项

| 项 | 内容 | 期限/归属 |
|---|---|---|
| **U-105**（条件 1） | pipecat↔mcp 配对：装 pipecat 1.4.0 实际支持的 mcp 版本 + 重生成 lock + `import pipecat.services.mcp_service` import-smoke 进 RETAIN CI 闸门；保护活路径 `MCPClient`（bot.py:1909）。**当前钉定对使线上 MCP 工具路径 import 即炸** | Phase B 必修（implementation-plan U-105） |
| **C2 ADAPT**（条件 2） | `tests/test_tool_filler.py` fixture-only 修复（`FunctionCallParams.pipeline_worker`）；该文件 RETAIN→ADAPT 重分类（CR-3），修复随 RETAIN CI 闸门落地 | 不晚于 Phase B exit |
| **G-1/G-2** | G-1 仅完成第 1 步（wheel 实证）；MiniMax 续用待 G-2 第三方批准——`SimpleMiniMaxTTSService` 路径在此之前不得宣称可用 | Phase B（U-101 闸门） |
| 人工清理 | ① 9 个 4 字节沙箱 canary 文件（`2jyh9mb7` 等，内容 `blat`，沙箱工具层探针，我方已核大小）——留待用户手动清理（沙箱 delete-guard）；② `config/runtime.json`（685B，旧测试 seed 的运行时配置遗留物，deploy.sh:79 本就排除）——同留置；③ `.venv/tmp/` 下 10 个我方挪出的 `pytest-of-*-aside-*` 目录——确认无需后可删 | 用户手动 |
| 环境指引补正 | pytest basetemp 在 `.venv/tmp/pytest-of-*`（非 `/tmp/`）——FileExistsError 时挪前者；建议随 U-005 环境注记同步全队 | 即时生效 |
| F-04 复核注 | 评审 F-04 的中英混排修复已落（bool 报文英文化）；我方 Q2 四要素复核含此点，成立 | 已闭环 |

---

## 附：验收环境与执行摘要

- 主机：macOS（WorkBuddy 沙箱，broker 化 file-op shim——本报告 §四已记录其对 pytest 的影响与绕法）
- `.venv`：Python 3.13.12 · pytest 9.2.0 · pytest-asyncio 1.4.1.post1 · pipecat-ai 1.4.0 · mcp 2.3.0 · moto 5.x
- 执行统计：Phase A 套件 170/170 绿；RETAIN 27 文件 462 用例 = 4 红（全归因）/ 457 绿 / 1 skip（环境性）；自设计 43/43 绿；静态检查（隔离/lock/密钥/import 纯度/占位符集）全过
- **验收结论：PASS-WITH-CONDITIONS** —— Phase A（U-001..U-005）验收通过，条件 1（U-105）/条件 2（C2 fixture）按期限随 Phase B 闭环。本结论交付后 Phase A 可宣告关闭（评审 PASS + 本独立验收通过，决策登记表零开放项）。
