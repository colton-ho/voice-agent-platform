# 独立 QA 验收 · #19（#16 LLM 层 + #17 pipeline 重写）【终版 — 全项完成】

> **验收员**：qa-verifier（验得明）· 2026-11-09/10 · 开工令口径：对抗性自设探针 / 變異檢驗 / 三方数字收敛 / 诚实归因。
> **世界状态基准**：dev 自测 docs/azure/verification-b3-b6.md；dev 数字 = team-lead 独立复现 = 1035 passed / 2 failed / 1037 collected（mv-dodge 治法）。
> **红口径（team-lead 重裁）**：C4×2 = test_demo_store::test_put_get_list_roundtrip_with_tool_ids_and_decimal + test_migrate_demos::test_migrate_writes_all_fields_matching_loader，签名须逐字节 = 字段漂移基线（Extra items: asr_filter engine voice model provider 型）。**越界红 = 阻塞；此 2 红消失或签名变 = 阻塞（口径锚）**。
> **纪律**：工作树只读（探针/变异一律 /tmp，共享树零接触）；产出分段落盘防 502；ripgrep 交替（禁 BSD grep `\|`）；basetemp 治法（双目录移场 + 唯一 --basetemp + --capture=sys）；NOT-verified 如实划界。

## A0 · 全量基线（✅ 完成 — executed）

- **executed** 全量 `pytest tests/ --capture=sys --basetemp=.venv/tmp/qa-b19-<唯一>`（mv-dodge 双目录移场先行）→ **2 failed / 1035 passed / 1037 collected**（159.40s，exit=1 属预期）。与 dev 自测（112.23s）及 team-lead 独立复现**三方数字收敛 ✅**（耗时差=机器负载，不入账）。
- **红名单恰为 C4×2 口径锚，零越界红**：
  1. `tests/test_demo_store.py::test_put_get_list_roundtrip_with_tool_ids_and_decimal`（断言点 :152）
  2. `tests/test_migrate_demos.py::test_migrate_writes_all_fields_matching_loader`（断言点 :181）
- **签名逐字节核验 ✅（双侧）**：
  - **输出侧**：两红失败签名 = `AssertionError: assert {'asr_filter'...'kb_ack', ...} == {'filler', 'g...b_intro', ...}` + `Extra items in the left set:` {engine, provider, voice, model, asr_filter} —— 集合元素与 phase-a-report:59 / u-103:87,91 / u-101-report:97 / verification-b3-b6:54 四处历史记录**逐项吻合**（asr_filter engine voice model provider 五项，无第六项、无 Missing items）。
  - **源码侧**：`git diff HEAD` 对两测 401 行改动**零触及断言**（`set(demo.keys())` / `set(stored.keys()) == CANONICAL_KEYS` / `asr_filter` 在 diff 中 0 命中）；python 逐行提取断言块 HEAD↔worktree **BYTE-EQUAL: True**（两测均是）。
  - 诚实划界：docs 无历史「完整输出逐字节」捕获件（最早为 phase-a 摘要形），故逐字节核验以上述双侧（输出元素集 + 源码断言块字节）组合达成；输出渲染形式（`Extra items in the left set:` 竖排）为 pytest 版本渲染差异，元素集本身不变。
- **口径锚判定：成立** ✅——2 红在位、签名未漂移、无越界红（1035 绿中无新增红，账目 1022+2+15=1037 与 dev §六 一致，独立复算 ✅：384+211+193+247=1035）。

## A1 · 修法①真实性（finalize 薄封装 + 读回侧重指 + P0 字节保全）（✅ 完成 — executed）

自设对抗性探针 9 条（`/tmp/qa_b19_a1_probe.py`，脚本化 ScriptRepo/ScriptSink **非 dev 的 FakeCosmos** 用于等价性面；FakeCosmos 仅用于读回语义面）→ **9/9 绿**。

- **① 薄封装行为等价 ✅**：`finalize` vs `flush_turns_and_summarize` 两入口各跑一遍同构状态，脚本化 repo 调用序列**逐字节相等**（upsert_outcome_doc → write_turns → patch_fields → sink 投递 ×1）；若复制流程必现翻倍/多步。实现即 `bot.py:1457-1460` 单行委托 + docstring 声明「不複製流程」，行为面证实。
- **② 读回侧断言形状逐字保留 ✅（独立验）**：python 逐行提取 test_history.py 断言块 git HEAD（moto 版）↔ worktree（FakeCosmos 版）→ **10/10 断言行逐字节相等**（item["ttl"]/turns/summary_status 迁移/summary_error 全部原样）。行为面：独立 repo 实例（**读侧 ttl_days=30 ≠ 写侧 7**）读回 `item["ttl"] == int(started)+7*86400`（±2）——删除点 epoch 在写侧固化，§1.3-5 语义 1:1 成立；turns 形状（turn_count/who）与 summary_status 迁移（pending→ok；failed+summary_error 带因）全过；summary_error 999 字符错误 → 落库 ≤500（`str(err)[:500]` 截断钉住）。
- **③ P0 字节保全 ✅**：test_history.py :100-101/:160-161 两处 A-4 注释 + `"model": bot.DEFAULT_MODEL,` 与规范文本逐字节相等（含缩进），两处互为一致；`bot.DEFAULT_MODEL` 值未漂移。
- **边界/异常补充**：未知 call_id finalize → 零调用空转 ✅；双 finalize 幂等（恰好 1×upsert/1×turns/1×patch/1×job）✅；repo 写失败不外抛且流程续行（turns/patch/job 照走，收口不中断）✅；**过期行读侧强制不可见**（started_at 远古 → get_history_doc 回 None，§1.3-4 `expires_at > now` 过滤实证；收口路径对过期行不异常外抛）✅。
- **诚实归因**：探针自身缺陷 2 已修（P0 规范串漏缩进；读回探针误用远古 started_at 撞上读侧强制过期过滤——后者顺手转化为独立边界探针⑤）；实现缺陷 0。观察项（非缺陷）：过期行 write_turns 内部 KeyError 被 flush 内吞记日志（行已过期，行为=优雅降级）。

## A2 · B-3 I-4 硬不变式双腿 + 预算/幂等/枚举（✅ 完成 — executed）

自设对抗性探针 17 条（`/tmp/qa_b19_a2_probe.py`，ScriptRepo/ScriptQueue/SpyInvoke 全自设）→ **16 绿 / 1 红（=BUG-01，实现缺陷，探针无缺陷）**。

- **I-4 腿 1 正常完成 ✅**：patch(summary+ok, unset summary_error, **etag 乐观并发传入**) + complete，零 abandon。
- **I-4 腿 2 transient 耗尽 ✅**：delivery_count=4（0 基末投）+ transient → **failed 终态 + complete 不重投**，summary_error 带因。
- **`>=maxDeliveryCount-1` 双约定 ✅（重点核验）**：`is_final_delivery` 单元面（4,5）/（5,5）真、（3,5）/（0,5)假、（3,4）真；**双约定扫描探针**：0 基 count=0..5 与 1 基 count=1..6 在 always-transient 下逐投验证——非末投必 abandon、末投必 failed+complete，**两约定下均不永卡 pending**（failed 终态必达，1 基按设计提前一轮收口=「宁早收口」裁决实证）。
- **30s 预算超时 ✅**：timeout_s=0.05 + 0.5s invoke → **abandon 留下一轮、零 failed 写**（不误标）；末投时同样收口 failed（I-4 优先于预算语义）。
- **R-A-010 幂等 ✅**：ok 行重复投递（delivery_count=3）→ complete，**invoke 零调用、零 patch、零 abandon**。
- **wire enum ✅**：WIRE_STATUSES == ("pending","ok","failed") 无第四值；三场景行为面收集 summary_status 写入值 ⊆ enum 且 == {ok,failed}。
- **异常/边界全绿 ✅**：畸形信封 5 形态（{} / 错 job_type / 缺 call_id / 缺 job_type / body=None）→ complete 零副作用；行缺失 → complete 零 invoke；读失败 → transient（abandon）末投收口；成功路写冲突（etag）→ abandon 留重判；**终态写入自身失败 → abandon**（I-4 优先，保下轮写入机会）；永久错误消息 999 字符 → 落库 ≤500。
- **失败分类（探针在此转红）**：ValueError/TypeError/KeyError/AttributeError → failed ✅（spec §2.2「調用配置非法」面）；RuntimeError/TimeoutError/ConnectionError → transient ✅；**PermanentJobError → 实测走 transient/abandon（BUG-01）**。

### BUG-01 · PermanentJobError 显式永久性标记不生效（被当暂时性重试）

- **关联单元**：U-新 B-3 `workers/postcall_worker.py`（#17）/ **严重度：🟡**（latent：全仓零调用方；终态收敛不破 I-4；但与自文档契约相悖，未来调用方踩雷）
- **复现步骤**：`PostcallWorker.handle` 中 invoke_summary 抛 `PermanentJobError("p")`（`/tmp/qa_b19_a2_probe.py::test_a2_permanent_error_classes_settle_failed`）→ 实测 `out=="abandoned"`、日志 `transient failure ... abandoning`。
- **期望结果**：按模块 docstring :8-9「永久性：……或**显式 PermanentJobError** → 写 failed 終態 → complete（不重试）」→ 应立即 `summary_status='failed'` + complete。
- **实际结果**：`PermanentJobError` 继承 `Exception`，不在 :102 永久性元组 `(ValueError, TypeError, KeyError, AttributeError)` 内 → 落入 :104 `except Exception` → abandon 重投（至多 5 投后才经 I-4 收口 failed；每投白付一次 LLM 调用）。
- **证据**：探针输出 `assert ('abandoned' == 'completed')` + worker 日志 `transient failure for c1 (delivery_count=0): p; abandoning`；全仓 Grep `PermanentJobError` 仅 :9 docstring + :29 类定义，**无任何 raise 点/捕获点**（类是死契约）。
- **口径判定**：spec §2.2 只要求「調用配置非法→failed 不重试」（元组已覆盖，探针绿）；`PermanentJobError` 是实现自加的扩展点、其自文档行为失灵——**不触碰 team-lead 阻塞口径（越界红/口径锚漂移），判非阻塞缺陷**，建议随 #18 挂账或小修法环销项。
- **建议修复方向**：:102 前加 `except PermanentJobError: return await self._settle_failed(...)`（或并入永久性元组）+ 补 1 钉测（显式永久性→零重投）。

## A3 · C-9 异步 summarize 三腿（✅ 完成 — executed）

自设探针 9 条（`/tmp/qa_b19_a3_probe.py`，ScriptRepo/SpySink 自设 + HTTP 层权限探针）→ **9/9 绿**。

- **腿 1 受理契约 ✅**：返回恰为 `{"call_id", "summary_status":"pending"}`——**summary 不回**（键不存在）；行先翻 pending 再投递；job 信封 §2.2 形状（job_type/job_id=`summary:<call_id>`/call_id）。
- **腿 2 入队失败 ✅**：sink 抛异常 → **500 + `{"error":"enqueue failed: ..."}`**（实现为 return JSONResponse 而非 raise HTTPException——HTTP 语义等价，细节入账）；行 error-as-data 落 **failed + summary_error**（≤500 截断钉住）——不永卡 pending。**双败边界**：pending 落行即失败 → 500 `{"error":"persist failed..."}`、零投递、不崩。
- **腿 3 in-process 降级 ✅**：sink=None（无 SERVICEBUS_NAMESPACE）→ **同一受理契约 200 pending**；`_summarize_and_update` 以 (call_id, turns, {"lang"}) 被异步拉起（spy 实证），终态由本进程落。
- **边界/异常 ✅**：未知 call_id → 404；HISTORY_DISABLED → 404；repo 读失败 → 500 "backend unavailable"。
- **权限面 ✅**：`require_admin` 非 admin 角色 → 403 "admin role required"，admin 放行；HTTP 未登录直打 POST /api/admin/history/c1/summarize → 401/403 拒绝。
- **诚实归因**：探针自身缺陷 2 已修（腿 2 两探针误按 raise HTTPException 断言，实为 return JSONResponse——先直脚本复现确认实现行为符合契约后修正期望形；loguru `logger.exception` 的 stderr traceback 一度被误读为异常逃逸，已澄清为日志输出）；实现缺陷 0。

## A4 · B-5 CR-C1 三件套（✅ 完成 — executed）

自设探针 11 条（`/tmp/qa_b19_a4_probe.py`，subprocess 隔离导入 + 缝单测 + 全仓扫描）→ **11/11 绿**。

- **① AUTH_SECRET fail-fast ✅**：subprocess 缺值导入（同时断 KEY_VAULT_URL）→ 非零退出 + `AUTH_SECRET missing` + 文案**指名双注入通道**（env `AUTH_SECRET` / KV `AUTHSECRET`）；有值导入干净且值正确。**token_urlsafe 兜底退役 ✅**：bot.py 代码级调用点 = 0（仅注释提及退役史）。
- **② 缝语义 ✅**：`resolve_secret` env 优先（含空白视同缺值落 KV）、缺省空串=MI 模式；`_kv_secret` 无 SDK/不可达 → "" 不抛（lazy 降级）；`resolve_api_keys` 形状恰为三键 strip 后注入缝形状（speech/llm env 直给、minimax 走 resolve_secret）。**散读收编 ✅**：全仓 grep 四密钥 env 读取点仅剩缝内 :186-187 两处（SPEECH_KEY/LLM_API_KEY，在 `resolve_api_keys` 本体），AUTH_SECRET/MINIMAX_API_KEY 均走 `resolve_secret`——CR-C1 缝收编主张成立。
- **③ 五表名 env 零消费 ✅**：哨兵值（五 env 全置 SENTINEL_*）subprocess 导入 → 容器名恒为 §1.1 冻结值 `users/demos/voices/activity/calls`（storage/cosmos.py:13-20），**哨兵零影响**；全仓正则扫描 `os.environ` 读取五表名 → **0 命中**（残留仅防朽钉/注释/文档/CLI help——分类逐项核对：config_contract:111 注释、activity_store:182/bot:1273 退役注记、test_config_contract:231 防朽钉、migrate_demos CLI help/docstring、test_migrate_demos:43 测试局部变量名=CLI 参数值）。
- **HISTORY 默认启用翻转 ✅（已裁接受，验一致性）**：默认导入 `_history` 非 None + `HISTORY_DISABLED is False`；`test_history_enabled_by_default` 钉测存在且注释（「B10：HISTORY_TABLE 退役后默认启用」）与钉一致——偏差 2/3 的测试面与注释面自洽。

## A5 · B-6 排除面零改动（✅ 完成 — executed，git 级）

- `git diff HEAD --stat -- cloudformation.yaml deploy.sh` → **空输出 = 零改动** ✅；git status 变更面亦无任何排除面文件。
- **锚点内容 git 级逐字节比对（worktree ↔ HEAD）全等 ✅**：
  | 锚点 | worktree 行位 | HEAD 行位 | 内容逐字节 |
  |---|---|---|---|
  | `AWSNovaSonicLLMService`（import/:Settings(/llm=） | :75/:2235/:2243 | :74/:2159/:2167 | ✅ True |
  | `from botocore.config import Config` | :3549 | :3345 | ✅ True |
  | `SimplePollyTTSService`（class/实例化） | :808/:1840 | :750/:1771 | ✅ True |
  行位漂移=全局增删行所致（内容零触碰）；dev 记录「原 :3485」与我方 HEAD 实测 :3345 的行号引用差属记录口径差，**内容字节面结论一致**。infra/bicep 为新增文件，不触及 cloudformation.yaml。

## A6 · 数字收敛表 + 變異檢驗抽查（✅ 完成 — executed）

### 三方数字收敛（全量）

| 面 | dev 自测（verification-b3-b6） | team-lead 独立复现 | 我方独立执行 | 收敛 |
|---|---|---|---|---|
| collected | 1037 | 1037 | **1037** | ✅ |
| passed | 1035 | 1035 | **1035** | ✅ |
| failed | 2（C4×2） | 2（C4×2） | **2（C4×2，签名逐字节）** | ✅ |
| errors | 0 | 0 | **0** | ✅ |

账目独立复算 ✅：1022（checkpoint 1/2 旧集）+ 2（修红转绿）+ 15（worker 8 + dashboard C-9 1 + startup_secrets 6）= 1037；分块 384+211+193+247 = 1035。**归因差异 0**。

### 自设探针汇总（45 条）

| 组 | 探针 | 结果 |
|---|---|---|
| A1 修法① | 9 | 9/9 ✅ |
| A2 B-3 I-4 | 17 | 16 绿 / 1 红（=BUG-01，探针无缺陷） |
| A3 C-9 | 9 | 9/9 ✅ |
| A4 B-5 | 11 | 11/11 ✅ |

### 變異檢驗抽查（/tmp 副本，共享树零接触；provenance REPO 改指副本根）

| # | 变异 | 红集（=对应守卫） | 附带红 | 恢复后 | 判定 |
|---|---|---|---|---|---|
| M-A | `is_final_delivery` `>=`→`>`（0 基末投漏收口 off-by-one） | **5 红**：dev `test_is_final_delivery_boundary` + 我方 a2×4（final_delivery_0based / dual_convention / budget_timeout_final / read_failure_final） | 0（BUG-01 预存红除外） | 5/5 转绿 | ✅ 真守卫 |
| M-B | AUTH_SECRET 还原 token_urlsafe 兜底（B12 回归） | **3 红**：dev `test_auth_secret_fail_fast_when_missing` + 我方 a4×2（fail_fast / fallback_retired） | 0 | 17/17 转绿 | ✅ 真守卫 |

红/绿对照：M-A 42 案→5 红/36 绿(含 BUG-01)→5 绿；M-B 17 案→3 红/14 绿→17 绿。**守護真實性成立**：两处关键守卫（I-4 收口判定、B12 fail-fast）变异后恰使对应守卫集转红、零附带，恢复即绿。

### 诚实归因（全程）

- **探针自身缺陷 6 处，全部已修**：A1×2（P0 规范串漏缩进；读回探针误用远古 started_at 撞读侧强制过期——顺手转化为边界探针⑤）；A3×2（腿 2 按 raise HTTPException 断言，实为 return JSONResponse——直脚本复现实证实现符合契约后修正期望形）；A4×2（正则漏引号/括号形态，运行前自审修正）。
- **实现缺陷 1 处：BUG-01**（🟡 非阻塞，PermanentJobError 契约失灵，详见 A2）。
- 口径锚（C4×2）零漂移；全量零新增红；三方数字归因差异 0。

## A7 · 验收判词（✅ 完成 — executed）

**结论：🟢 通过**（附 1 个非阻塞缺陷 BUG-01，建议 #18 挂账或小修法环销项）

- **阻塞口径（team-lead 重裁）逐项核验**：越界红 = **0** ✅；口径锚 C4×2 在位、红签名逐字节 = 字段漂移基线（输出元素集 + 源码断言块双侧）✅；2 红未消失、未变签名 ✅。**阻塞项全过**。
- **五探针面判词**：修法①真实性 ✅（薄封装等价/断言形状逐字保留/P0 字节保全）；B-3 I-4 硬不变式双腿 ✅（双约定不永卡 pending、预算不误标、R-A-010、wire enum）；C-9 三腿 ✅；B-5 三件套 ✅；B-6 排除面 ✅。變異檢驗抽查两处守卫均真。
- **缺陷清单**：BUG-01（🟡 PermanentJobError 被当暂时性重试——spec 纳入项全部达标，属实现自文档扩展点失灵，全仓零调用方）。**升级条款**：若 team-lead 裁定模块 docstring 契約（「显式 PermanentJobError → failed 終態不重试」）属交付面必保语义，则 BUG-01 升级为阻塞缺陷退回 fullstack-dev 修复（复现件与修复方向已备齐，修复后我方全量回归 A2 组）。→ **【R1 已销项】**修复环落地（postcall_worker.py:104-107），回归达标，详见 R1.4。
- **NOT-verified 如实划界**：V5/V6 部署面实测（RU/耗时、DLQ）留 U-401/U-406；真实 Service Bus/Key Vault/KV 直读 live 链路（azure SDK 未装，lazy 降级路径已单测）；负载/性能；C4×2 预存债务（=口径锚，非本批责任）；#18 挂账 16 项（验收清单外）。
- **回归纪律**：M-A/M-B 均「变异→红→恢复→绿」全闭环；共享树只读零接触（全部探针/变异在 /tmp，变异树已清理）；basetemp 治法全程 0 error。

---

## R1 · 修复环後回归（BUG-01 划销 + 新钉鉴别力）（✅ 完成 — executed）

> 修复环落点（team-lead 亲验，本段独立回归）：BUG-01 = workers/postcall_worker.py:104-107 显式 `except PermanentJobError` → `_settle_failed`（先于 :108 泛型分支，注释注明子类误判坑）+ 钉测 tests/test_postcall_worker.py:202-222；F-14 = test_postcall_worker.py:255-256 WIRE_STATUSES 三值防朽钉；F-15 = bot.py:5612/:5720 legacy 注记；F-16 = `_to_ddb` 删除 + test_migrate_demos.py 消费者转化（名保留裁定）。

### R1.0 修复落点亲验（✅ 完成 — executed）

- **BUG-01 修复块 ✅**：workers/postcall_worker.py:104-107 `except PermanentJobError as e: return await self._settle_failed(...)`，顺序正确（:102 永久元组 → :104 显式分支 → :108 泛型），注释注明「PermanentJobError 是 Exception 子类，落进泛型分支会被误判暂时性（BUG-01：白付重投）」。
- **BUG-01 钉测 ✅**：tests/test_postcall_worker.py:202-222 `test_explicit_permanent_job_error_settles_failed_without_retry`——首投（delivery_count=1）raise PermanentJobError → completed + failed 終態 + summary_error 带因 + complete + **abandoned==[]（零重投）**；docstring 带修复前对照（transient 腿首投 abandon）。鉴别力设计合格（对照腿明确）。
- **F-14 钉 ✅**：:255-256 `assert WIRE_STATUSES == ("pending", "ok", "failed")` 恰三值。
- **F-15 注记 ✅**：bot.py:5612/:5720 双处「legacy 命名注记：行为已 Azure FC-3，函数名保留冻结（2026-11-10 裁定「名不动值重指」）」在位。
- **F-16 死码清零 ✅**：`_to_ddb` 函数 bot.py 零代码残留（唯一字符串命中 = :124 注释中的脚本名 `migrate_demos_to_ddb` 子串）；残余提及全为退役注记/文档/测试名（voice_store:36、demo_store:32、test_demo_db_integration:260 = F-16 退役注记；docs 历史记录）——无悬挂调用点。消费者转化 ✅：test_migrate_demos.py:132-146 `test_pure_demo_dict_to_ddb_item_preserves_tool_ids`（名保留=裁定）body 转 `normalize_demo_dict` 共享 normalizer，钉 tools→tool_ids 键形保全，docstring 注明「原 DDB 序列化步已隨 F-16 死碼退役」。

### R1.1 A2 组全量回归 + BUG-01 复现件（✅ 完成 — executed）

- **我方 A2 探针 17/17 全绿**（`/tmp/qa_b19_a2_probe.py`，REPO 指共享树）——**BUG-01 复现件 `test_a2_permanent_error_classes_settle_failed` 转绿**：PermanentJobError 现达契约（failed 終態 + complete + 零 abandon），ValueError 族/暂时性族/全部异常/边界腿原样全绿。
- **dev 钉测 10/10 全绿**（test_postcall_worker.py：8 原测 + BUG-01 钉 + F-14 钉）。
- **A2 组判定：修复达标，复现件转绿** ✅（BUG-01 由缺陷→销项）。

### R1.2 全量三方收敛（✅ 完成 — executed）

- **executed** 全量 `pytest tests/ --capture=sys --basetemp=.venv/tmp/qa-b19r-<唯一>`（mv-dodge 治法）→ **2 failed / 1037 passed / 1039 collected**（139.86s，0 errors）。与 dev 自测 = team-lead 独立复现 **三方收敛 ✅**（1037/2/1039）。
- **红 = C4×2 锚不变性 ✅**：红名单恰为 test_demo_store::test_put_get_list_roundtrip_with_tool_ids_and_decimal + test_migrate_demos::test_migrate_writes_all_fields_matching_loader；两处失败签名与修复前逐字节一致（`assert {'asr_filter'...'kb_ack', ...} == {'filler', 'g...b_intro', ...}` + Extra items 恰 = {asr_filter, engine, model, provider, voice} 五元素，断言形 `set(demo.keys())` / `set(stored.keys()) == CANONICAL_KEYS` 在位）——锚未消失、签名未变、零越界红。
- **账目复算 ✅**：1037（修复前 passed）+ 2（BUG-01 钉 + F-14 钉）= 1039 collected；1035+2=1037 passed。

### R1.3 新钉鉴别力變異檢驗（✅ 完成 — executed，/tmp 副本，共享树零接触）

副本基线先行：dev 10 + 我方 A2×17 = **27/27 绿**。

| # | 变异 | 红集（=对应守卫） | 附带红 | 恢复后 | 判定 |
|---|---|---|---|---|---|
| M-C | 摘除 postcall_worker.py:104-107 显式 `except PermanentJobError` 分支（还原 BUG-01 回归态） | **2 红**：dev `test_explicit_permanent_job_error_settles_failed_without_retry` + 我方 `test_a2_permanent_error_classes_settle_failed` | 0 | 2/2 转绿 | ✅ BUG-01 钉具鉴别力 |
| M-D | `WIRE_STATUSES` 加第四值 `"unknown"` | **2 红**：dev `test_wire_enum_is_exactly_three_values`（F-14 钉）+ 我方 `test_a2_wire_enum_closed` | 0 | 27/27 转绿 | ✅ F-14 钉对第四值敏感 |

红/绿对照：M-C 27→2 红/25 绿→2 绿；M-D 27→2 红/25 绿→27 绿。**两枚新钉均真守卫**（变异→红→恢复→绿全闭环）。

### R1.4 终版判词（✅ 完成 — executed）

**结论：🟢 通过（终版，修复环後）**——#19 验收判词经修复环回归后落定。

- **BUG-01 划销 ✅**：修复落点 workers/postcall_worker.py:104-107（显式分支先于泛型 + 误判坑注释）亲验在位；钉测 :202-222 含对照腿鉴别力合格；我方复现件 `test_a2_permanent_error_classes_settle_failed` **转绿**（PermanentJobError → failed 終態 + complete + 零重投 = 契约达标）；M-C 变异实证钉真。**BUG-01 从缺陷清单销项**（原 A7 缺陷清单项以本段为准更新）。
- **新钉鉴别力 ✅**：M-C/M-D 双变异红集精确、零附带、恢复即绿（F-14 对第四值敏感实证）。
- **F-15/F-16 抽验 ✅**：legacy 注记双处在位（名冻结）；`_to_ddb` 死码 bot.py 清零（残余=退役注记/文档/名保留测试消费者，无悬挂调用）；test_migrate_demos.py:132-146 消费者转 `normalize_demo_dict` 键形钉收敛。
- **全量三方收敛 ✅**：1037 passed / 2 failed / 1039 collected（= dev = team-lead 复现）；红 = C4×2 锚逐字节不变（签名元素集 + 断言形双侧）；零越界红、0 errors。
- **遗留不变**：NOT-verified 划界照 A7（V5/V6 部署实测、真实云 live 链路、负载性能、C4×2 锚=预存债、#18 挂账 16 项）。
- **回归纪律**：M-C/M-D 全闭环；共享树只读（探针/变异 /tmp，变异树已清理）；basetemp 治法 0 error；ripgrep 交替纪律沿用。
