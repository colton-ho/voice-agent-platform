# 评审报告 · #19 批次复核（#16 Bedrock→Foundry + #17 pipeline 重写）

- 评审人：code-reviewer（严过关）｜日期：2026-11-10
- **结论：🟢 PASS**（零阻塞项；3 条非阻塞建议 F-14/F-15/F-16）
- **销项判定（上轮 Phase B blocking 双项）**：**CR-C1 ✅ 销**（B-5 三件套闭合）；**CR-C2 ✅ 销（收窄口径）**（import-smoke 入 RETAIN + 双 SDK 钉版落地；live smoke 按裁决挂账 provisioning 后，不阻本次）。
- 评审对象：工作树 #16+#17 两工作包全部改动（基线 commit 6375e98，树未 commit，只读不修）：
  - **#16（Bedrock→Foundry）**：bot.py MODELS/LABELS 六值表（:516-534，裁决 A-1/A-2）+ C-2 形状双锚 payload（:3335 `_admin_options_payload` / :5506 `/api/config`）+ `_fc3_tool_specs`（:2378）/`_FC3PipecatLLM` 桥（:2392，裁决 A-3 案 (ii)）+ L1-L3 调用面接线（:5629 `_invoke_summary_bedrock` / :5736 `_invoke_translate_bedrock` → `make_managed_llm`）+ providers/llm/azure_openai.py `resolve_deployment`（:71）/`make_managed_llm`（:357）+ requirements 双钉（azure-identity==1.27.0 / openai==2.54.0）
  - **#17（pipeline 重写）**：storage/cosmos.py（§1.1 八容器冻结常量 + 7 动词窄接口）+ storage/cosmos_calls.py（FC-6 实现 + X1 折叠 legacy 历史面，403 行）+ 四 store DynamoDB→Cosmos 适配（user/demo/activity/voice_store，公共方法签名不变）+ workers/postcall_worker.py（I-4 + 30s 预算 + R-A-010 + wire enum，185 行）+ C-9（`_dispatch_summary` 投递缝 bot.py:1462-1474 + admin 受理三腿）+ B-5 CR-C1 三件套（bot.py:157-198）+ infra/bicep 三模块（只落码不部署）+ smoke_bedrock.py 诚实重写 + 修红令（HistoryRecorder.finalize 薄封装）
  - 测试面：新增 tests/fake_cosmos.py、fake_servicebus.py、test_postcall_worker.py、test_startup_secrets.py、test_cosmos_calls.py、conftest.py；30 个 tracked 文件改动（四 store + 20 测试文件重指）
- 围栏核对：`deploy/cloudformation.yaml`、`deploy/deploy.sh` **零 diff**；排除锚 `from botocore.config import Config`（bot.py:3549）、`AWSNovaSonicLLMService`（:75/:2235/:2243）、`SimplePollyTTSService`（:808/:1840）内容原样（行漂移与派令 +64 口径一致）；改动文件全数归因 #16/#17/修红令面，无越界文件、无夹带。

---

## 一、阻塞项（必须修复才能通过）

**无。**

## 二、非阻塞建议（可选优化）

| # | 位置 | 问题 | 依据 | 建议修法 |
|---|------|------|------|----------|
| F-14 | `workers/postcall_worker.py:25` `WIRE_STATUSES`（全 tests 无断言） | wire enum「不出第四值」缺防朽钉：常量在，但 dev 自测「常量+斷言」仅前半成立——`grep WIRE tests/` 零命中，三值现靠各测试的 `summary_status` 行为断言间接钉，将来加第 4 值测试不会拦截 | 评审重点 1「wire enum 不出第四值」；product-impact §7.1 钉死口径 | test_postcall_worker.py 补 1 行 `assert WIRE_STATUSES == ("pending", "ok", "failed")` |
| F-15 | bot.py:5629 `_invoke_summary_bedrock` / :5736 `_invoke_translate_bedrock`（同类：scripts/migrate_demos_to_ddb.py 文件名及 "AWS region resolution" 残留措辞） | 名实漂移：函数名含 bedrock、实现已 A-4/A-5 走 make_managed_llm（Azure MI）；函数内注释诚实（引裁决/F6 原值），但名字无 legacy 注记。smoke_bedrock.py 同类情况已补「Rewrote the Bedrock probe」注记，这两处没有 | 简约性原则·注释诚实（派令重点 4「注释与实现一致性」） | 各补 1 行「名字保留旧 API 面，行为已 A-4/A-5 迁 Azure」注记，或 #18 挂账加统一 rename 项（与 X1 迁移形态/X2 名不动值重指同批处理） |
| F-16 | bot.py:1285-1299 `_to_ddb` | 死码：DynamoDB 遗物（float→Decimal），四 store 迁 Cosmos 后全仓无生产调用点（仅自递归 :1296/:1298），docstring 仍称 "DynamoDB resource layer" | 简约性原则（冗余代码） | 删除；若 Cosmos 写入面仍需 Decimal 语义则移入存储层并改写 docstring |

（F-13 承上轮：400 detail 风格不一 → 已列 #18 挂账 X14，不重复计。）

## 三、评审重点逐项（(1)–(6)）

| 项 | 结果 | 证据 |
|----|------|------|
| (1) 冻结契约零破 | ✅ | FC-6/FC-7 单一定义站 `storage/base.py` 未动（"Frozen contract — implementation-plan §3.3 single definition site"，U-002 导入纯度达标）；`CosmosCallRepository` 六方法集齐且签名匹配（cosmos_calls.py:134/159/163/169/194/224：create/get/update/append_events/finalize/query_history，expected_version kw 位一致）；**C-2 models[] 形状 {id,label,bedrock_id}** 双锚逐字保持（bot.py:3335/:5506 同构推导式 + talkConfig.spec.js:61-62 三键形状钉 + test_admin_api.py:308-312 断言含「id/bedrock_id 字段名冻结不动（C-2），bedrock_id 值=MODELS 值（名不动值重指）」注记）；**U-003 §11 变量零新增**（config_contract.py `_MEANINGS` 冻结面原样，LLM_ENDPOINT/LLM_DEPLOYMENT/LLM_API_VERSION/COSMOS_ENDPOINT/COSMOS_DATABASE/KEY_VAULT_URL 均在冻结名单；`make_managed_llm` 只读这三个 LLM_* 变量）；**wire enum** 常量恰三值（缺防朽钉=F-14，行为面三值各有断言） |
| (2) CR-C1 复核销项 | ✅ **销** | 三件套闭合上轮发现：① `resolve_secret`/`_kv_secret`/`resolve_api_keys`（bot.py:158-189）——secret 只驻内存（不落盘/不进 config，docstring 明示）、env（secretRef 注入/测试缝）优先、§3.1 冻结 secret 名 MI 直读兜底、散读收编单缝（speech/llm 无 §3.1 KV 名=名单冻结补名属 CR 裁定，注释明示）；② AUTH_SECRET 缺值 fail-fast（:194-199）——ephemeral fallback（token_urlsafe+告警）已删，RuntimeError 文案指名注入通道（env AUTH_SECRET / KV 'AUTHSECRET'）；③ 五表名 env 退役（user/demo/voice/activity_store + bot.py HISTORY_TABLE 删），容器名落 §1.1 冻结常量（storage/cosmos.py:15-22）。消费面错误文案指名通道（test_startup_secrets 6/6：env 优先且 KV 不触达守卫、冻结名点读防自造名、fail-fast 拒启、KV 模块假件直读 AUTHSECRET、MiniMax 缺 key 报 MINIMAX-API-KEY）。**残留（按偏差 4 条件裁决）**：azure-keyvault-secrets 仍未钉装——KV 兜底通道 live 未验（lazy import 降级 warning 有假件测试钉），挂账 U-401 |
| (2b) CR-C2 收窄口径 | ✅ **销（收窄）** | requirements.txt:14-18 明文「CR-C2（裁决 A-3 收窄面，#16）：只钉 azure-identity / openai SDK 两个版本——LLM 侧 MI（B5 行为变化）+ FC-3 适配器驱动面。pipecat-ai[azure] 不引（案 (i) 否决）」→ azure-identity==1.27.0 / openai==2.54.0 落钉；桥接 import-smoke 在 RETAIN 门（本次 179 passed 含 test_pinned_pipecat）；live smoke 留 provisioning 后（挂账），按裁决不阻本次 |
| (3) 排除面零改动（硬验收） | ✅ | deploy/ 两文件零 diff（`git diff --stat deploy/` 空）；`from botocore.config import Config` :3549、`AWSNovaSonicLLMService` :75/:2235/:2243（nova 回滚路径）、`SimplePollyTTSService` :808/:1840 全位、内容原样；131 个 bot.py hunk 全数归因（CR-C1 块/MODELS-LABELS/HistoryRecorder-Cosmos 化/_FC3PipecatLLM 桥/L1-L3 接线/历史 API 面重指/#33 时代改动混于基线 diff 但逐一识别）——AWSNovaSonicLLMService 区域无实质改动 |
| (4) 简约性原则 | ✅（3 非阻塞） | 无重复实现：`_FC3PipecatLLM` 桥 docstring 明示「F3/F5/F6（重试预算/caching 能力位/max_tokens 参数名）单一事实源留在 FC-3 适配器，桥接零重复」——属实（桥只做帧序翻译/metrics，消息摊平复用 pipecat `OpenAILLMAdapter.get_llm_invocation_params`，无第二套实现）；`_fc3_tool_specs` 明示「桥接边界翻译，providers/base 冻结不动」。无过度抽象：cosmos.py 7 动词窄接口 + 3 语义错误（NotFound/Conflict/PreconditionFailed）恰当地隔离 SDK，业务层不感知 azure-cosmos；fake_cosmos/fake_servicebus 是 SDK 边界替身（SQL 评估器只覆盖实际发出的形状、查询原文记录供形状断言；broker 语义最小化建模）——非 mock 自证。注释诚实：I-4 双约定注释（postcall_worker.py:33-41）与实现/边界钉三方一致；`_dispatch_summary` at-most-once 权衡注释（bot.py:1463-1466「進程崩潰仍可能丟——outbox 不預建，行留 pending 由 U-406 清掃」）如实；缺口=F-15/F-16 |
| (5) 偏差裁定落地一致性 | ✅ | 8 偏差逐项核（详见「六」）。重点两处：**偏差 2 HISTORY 默认启用翻转**——实现注释（bot.py:1271-1277「HISTORY_DISABLED=1 ⇒ _history is None 全调用点 no-op；B10/BC-c1 HISTORY_TABLE 退役；开关=HISTORY_DISABLED」）+ `test_history_enabled_by_default`（delenv → `bot.HISTORY_DISABLED is False`）+ 禁用腿（setenv "1"），**注释/实现/测试钉三方一致**；**I-4 `>=maxDeliveryCount-1` 双约定**——实现注释（0 基恰好末投收口、1 基提前一轮，「宁早收口不永卡 pending」）+ `test_is_final_delivery_boundary`（is_final(4)=True/ is_final(5)=True 双制式收口点）+ V6 腿 2（count==5 注入 → failed 終態 + complete 不再投递）钉面一致 |
| (6) F-11/F-12 守卫规格复用 | ✅ | 本批新测沿用同规格：V6 双腿拆分（正常腿/终态腿各为真回归捕捉，无空断言腿）；负向守卫为真钉（C-9 受理腿 `must_not_run` AssertionError「must not run inline in the request」、`pytest.fail("KV must not be touched")`）；KV 兜底腿用脚本化模块假件（fake azure.identity/azure.keyvault.secrets 经 sys.modules 注入）独立钉而不空转——与 F-11 两腿分工同型。#33 守卫（test_cascade_engine.py:400-414 F-11 回收钉 / test_composition_root.py:122-126 F-12 行为钉）在 1037 基线内全绿存活，规格未被稀释 |

## 四、销项判定（上轮 Phase B blocking 双项）

| 项 | 判定 | 说明 |
|----|------|------|
| **CR-C1** §11 凭据占位符 | ✅ **销** | B-5 三件套逐项闭合上轮发现（secret 不落盘/不进 config、散读收编、fail-fast 语义、错误文案指名注入通道）；唯一残留 = azure-keyvault-secrets 钉装（偏差 4 条件接受，挂账 U-401，KV 兜底通道 live 未验） |
| **CR-C2** pipecat-ai[azure] 部署钉 + live/import-smoke | ✅ **销（收窄口径）** | import-smoke 入 RETAIN 门 + azure-identity/openai 双钉落地 + pipecat-ai[azure] 明确不引（A-3 桥接替代，案 (i) 否决）；live smoke 按裁决留 provisioning 后挂账，不阻本次 |

Phase B 收口状态旁注：spec §14「preserve regression tests」已 ✅（#33 销项）；本批测试基线 1035/2 = 裁决 C4×2 预存债口径。我方 blocking 双项均处置完毕——Phase B 是否收口由 team-lead 汇 QA 半边结论裁定。

## 五、测试质量与独立执行

**质量面**：fake 全部落在 SDK/网络边界（FakeCosmos 按 §1.1 分区键建容器、SQL 评估器只覆盖实际发出形状并记录查询原文供形状断言；FakeQueue 最小 broker 语义含 delivery_count 自增/超限进 DLQ）；被测代码全部走真路径（CosmosCallRepository/PostcallWorker/resolve_secret 真身）。行为钉非 mock 自证：V6 双腿断言行状态+队列结算双面、C-9 受理腿断言精确响应体 `{call_id, summary_status:"pending"}` + 信封形状 + 内联 invoke 禁令、双约定边界钉与实现注释逐句对应。

**独立执行数字**（QA basetemp 治法：跑前挪开 `$TMPDIR` 与 `.venv/tmp` 两处 `pytest-of-*` + 每次唯一 `--basetemp=.venv/tmp/cr19-*`；全部 `--capture=sys`，全程 0 error）：

| 执行面 | 范围 | 结果 |
|--------|------|------|
| 全量 | `pytest tests/` | **2 failed, 1035 passed**（1037 collected）——红恰 C4×2（`test_demo_store::test_put_get_list_roundtrip_with_tool_ids_and_decimal`、`test_migrate_demos::test_migrate_writes_all_fields_matching_loader`，字段漂移预存债，签名逐字节同基线），**零越界红、零 error** |
| 新关键面 | test_postcall_worker + test_startup_secrets + test_cosmos_calls + test_history | **42 passed**（8+6+22+6） |
| 契约闸门 | RETAIN 6 文件（config_contract/provider_registry/provider_contracts/pinned_pipecat/storage_contracts/mcp_runtime） | **179 passed**（FC 零破面维持） |

账目脚注：1037 = 1022（checkpoint 1/2 旧集）+ 15 净新增（worker 8 + startup_secrets 6 + dashboard C-9 净 1——三腿中 2 腿为既有 summarize 测试重指）；test_history×2「修红转绿」是状态变化非计数变化——dev 自测 §六 把「+2」列作加数属记账冗余，总数 1037 无误。2 failed = 1037 − 1035 闭合。

## 六、偏差 8 项落地一致性

| # | 偏差 | 裁决 | 落地核对 |
|---|------|------|----------|
| 1 | 红数口径 C4×2（续跑令「C4×4」算术互斥） | 条件接受 | ✅ 独立全量 2 红 = 裁决口径，测试 ID/签名同基线 |
| 2 | HISTORY 默认启用翻转（旧：表空⇒禁用 → 新：默认启用、HISTORY_DISABLED=1 禁用） | 条件接受（运维可见面，请复核） | ✅ **复核通过**：注释/实现/测试钉三方一致（bot.py:1271-1277 + test_history_enabled_by_default + 禁用腿），翻转语义与 discovery 依赖矩阵 keep-only-HISTORY_DISABLED 口径吻合 |
| 3 | test_history_disabled_when_table_empty → test_history_enabled_by_default 重指 | 接受 | ✅ 断言意图正确翻转（默认启用真值） |
| 4 | B-5① KV 直读=兜底通道；azure-identity/azure-keyvault-secrets 钉装挂账 | 条件接受 | ✅ 一致：azure-identity 已钉（#16 双钉）；azure-keyvault-secrets 仍缺（U-401 挂账属实未清），lazy import 降级行为有模块假件测试钉 |
| 5 | migrate_demos_to_ddb.py 文档面（DEMOS_TABLE 语义→§1.1 容器） | 接受 | ✅ 5 行改动=docstring/help（亲验：「The target is the Cosmos `demos` container (§1.1; B10 retired the `DEMOS_TABLE` table-name env)」）；文件名/AWS region 残留措辞归 F-15 同类 |
| 6 | test_config_contract.py:231 `"USERS_TABLE"` 保留=防朽钉 | 接受 | ✅ 在位（:231） |
| 7 | 修红读回侧重指 moto→FakeCosmos（断言形状逐字保留） | 接受 | ✅ test_history 6/6 绿，expected_ttl=started+7d 断言零改动通过（`_read_row` ttl→expires_at 曝光 = §1.3-5 语义 1:1） |
| 8 | test_engine_languages ② 双保险简化未做（opportunistic） | 接受 | ✅ 保持原状属实（diff 零 def 增删，改动全在假件换轨面） |

## 七、#18 挂账确认

verification-b3-b6.md §八 16 项挂账（X1 迁移形态 / bedrock_id 名不动值重指 / A-4 裁决落纸 / attach-meta model=key 语义 / R-C-003 多副本分歧 / 错误分类行为注 / VoiceStore.delete 幂等 / 聚合 10k 帽 / "_none_" 哨兵 / ADAPT 12=11+1 / V2 回滚规则文 / mapping:191 措辞 / talkConfig fixture 风格注 / F-13 / _validate_segment 边角 / u-103-review §四.1 清扫）已收录核对——与本报告 F-15/F-16 无重叠冲突，F-15 建议并入 X1/X2 同批处理。u-103-report §四.1 过时表述已于 #33 修复环提前兑现（上轮 revision 2 在案），X16 属清扫确认项。

## 八、结论

**🟢 PASS。** 六项评审重点全过：冻结契约零破、CR-C1 销项、CR-C2 收窄销项、排除面零改动（硬验收达标）、简约性达标（3 非阻塞）、偏差落地一致。测试基线 1035/2 与裁决口径吻合、零越界红。#19 评审半边完成，待 qa-verifier 半边（#19 含独立 QA 验收）后由 team-lead 汇总裁决。
