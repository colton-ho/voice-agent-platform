# 自测记录 · #17 B-3→B-6 + 修红令 · checkpoint 2/2（fullstack-dev）

## 一、修红令（test_history 2 红转绿）

| # | 验收项 | 自测方法 | 结果 | 证据 |
|---|---|---|---|---|
| 1 | 两测转绿 | mv-dodge 定向跑 | ✅ | test_history 6/6 + test_postcall_worker 8/8 = 14 passed |
| 2 | P0 断言原样 | 逐字节比对 | ✅ | `"model": bot.DEFAULT_MODEL` + A-4 注释 2 处内容不动（:100/:101、:160/:161，行号因删 moto helper 前移） |
| 3 | 修法① | HistoryRecorder.finalize 薄封装 → flush_turns_and_summarize | ✅ | bot.py（API 面保留，不复制流程） |
| 4 | 读回侧重指 | moto/DynamoDB → FakeCosmos（U-303 同型） | ✅ | 断言形状逐字保留：`item["ttl"]`（_read_row 曝光=删除点 epoch）、turns、summary_status 迁移 |

实现注：`_read_row` 将 ttl 曝光为 expires_at（§1.3-5 语义 1:1），故 `expected_ttl = int(started)+7*86400` 断言零改动通过。

## 二、B-3 Service Bus postcall-jobs worker

| # | 验收项 | 自测方法 | 结果 |
|---|---|---|---|
| 1 | V6 I-4 正常完成腿 | test_postcall_worker leg 1 | ✅ ok 写回+complete |
| 2 | V6 I-4 delivery_count==5 终态腿 | leg 2（inject count=5） | ✅ transient→failed 终态+complete，不重投、不卡 pending |
| 3 | 30s 预算=暂时性 | timeout_s=0.05 测试 | ✅ abandon 留下一轮，不误标 failed |
| 4 | R-A-010 幂等 | ok 行重复投递 | ✅ 直接 complete，零 LLM 调用 |
| 5 | wire 枚举 {pending,ok,failed} | WIRE_STATUSES 常量+断言 | ✅ 无第 4 值 |
| 6 | C-9 异步 200 契约 | test_admin_dashboard 三腿 | ✅ 200 {call_id,summary_status:"pending"}（summary 不回）、入队失败 500+failed 落行、无 SERVICEBUS_NAMESPACE → in-process 降级 200 |
| 7 | at-most-once 信封 | _summary_job §2.2 形状 | ✅ job_type/job_id/call_id，MessageId=job_id |

## 三、B-4 bicep 三模块（只落码不部署）

cosmos.bicep（8 容器+CI-1 复合索引+defaultTtl:-1+Session+backup）/ servicebus.bicep（§2.1 显式六值）/ keyvault.bicep（§3.1 四 secret+RBAC）。`az bicep build` 三模块 ✅（仅 compile，零部署）；命名只引用 config_contract.py 冻结变量名。

## 四、B-5 CR-C1 三件套

| 件 | 落点 | 结果 |
|---|---|---|
| ① 启动期取密钥→api_key 注入缝 | bot.py `resolve_secret`/`_kv_secret`/`resolve_api_keys`（env→§3.1 KV 名 MI 直读；secret 只驻内存）；:1828 散读收编为 CR-C1 缝 | ✅ test_startup_secrets 6/6 |
| ② AUTH_SECRET 兜底退役（B12） | token_urlsafe(48)+告警 删除→缺值 fail-fast（错误文案指名注入通道） | ✅ fail-fast/KV 解析 2 测绿；tests/conftest.py setdefault 测试缝 |
| ③ 5 表名 env 退役（B10/BC-c1） | user/demo/voice/activity_store + bot.py HISTORY_TABLE 删；容器名落 §1.1 冻结常量；13 测试文件消费点随行；HISTORY_DISABLED=唯一开关（默认启用） | ✅ 残留扫描=零 env 消费（仅防朽钉/文档/CLI 参数值） |

## 五、B-6 排除面零改动

- `git status/diff -- cloudformation.yaml deploy.sh`：**零** ✅
- bot.py 锚点内容原样：`from botocore.config import Config`（:3549，原 :3485，全局增删行漂移 +64）、`AWSNovaSonicLLMService`（:75/:2235/:2243，原 :76/:2173/:2181）、`SimplePollyTTSService`（:808/:1840）✅
- 本 run 文件清单零排除面文件。

## 六、全量数字（mv-dodge 4 块）

| 块 | passed | failed |
|---|---|---|
| 1（activity…config_contract） | 384 | 0 |
| 2（cosmos…latency_log） | 211 | 1（C4 demo_store key-set） |
| 3（latency_report…provider_contracts） | 193 | 1（C4 migrate_demos key-set） |
| 4（provider_registry…voice_store） | 247 | 0 |
| **合计** | **1035** | **2（1037 collected）** |

红签名逐字节同基线：`assert set(demo.keys()) == {...}` / `Extra items: asr_filter engine voice model provider`。
账目：1022（checkpoint 1/2 旧集）+ 2（修红转绿）+ 15（B-3 worker 8 + dashboard C-9 1 + startup_secrets 6） = 1037 ✅。

## 七、偏差清单（规范原文 → 实际 → 理由）

1. **红数口径**：续跑令「红数回到恰好 C4×4」→ 实测 **C4×2**。C4×4 账面含 test_history finalize×2，已按「两测转绿」销项；余 2 红签名不变。两要求算术互斥，按显式验收项执行，请裁定 #19 C4 口径。
2. **HISTORY 默认启用**：discovery:156 旧语义「HISTORY_TABLE 空⇒禁用」→ 实际「默认启用，HISTORY_DISABLED=1 禁用」。理由：B10/BC-c1 表名 env 退役+discovery:256 keep-only-HISTORY_DISABLED。**行为翻转属运维可见面，请复核**（要 opt-in 请出变更）。
3. `test_history_disabled_when_table_empty` → `test_history_enabled_by_default` 重指（钉新真值；断言意图从"空表禁用"转"默认启用"，随偏差 2）。
4. B-5① KV 直读=兜底通道（主通道 §3.2 secretRef env 注入）；azure-identity/azure-keyvault-secrets 未装（lazy import 降级 warning）→ U-401 requirements 挂账。
5. B-5③ 顺行文档面：scripts/migrate_demos_to_ddb.py docstring+help 文案（DEMOS_TABLE 语义指向 §1.1 容器）。
6. test_config_contract.py:231 `"USERS_TABLE"` 保留=「旧变量不被读取」防朽钉，非消费残留。
7. 修红读回侧重指（moto→FakeCosmos）为修法①实施细节，断言形状逐字保留。
8. test_engine_languages ②双保险简化（既裁 opportunistic）**未做**：错过 B-3 窗口，避免额外扰动，保持原状。

## 八、#18 挂账清单（16 项，见收口报）

> 已逐项落 `bedrock-to-azure-mapping.md` §6 回填行（X1 + 掛賬#2–#16 標籤）；N1/N2/N3 新注、KV 兜底條件注、U-103 dev 偏差①–④ 錨同行區——三處（本表 §七 / u-103-review dev 偏差表 / mapping §6）無孤項。

X1 迁移形态 / bedrock_id 名不动值重指 / A-4 裁决落纸 / attach-meta model=key 語義 / R-C-003 多副本分歧 / 错误分类行为注 / VoiceStore.delete 幂等 / 聚合 10k 帽 §1.2 偏差 / "_none_" 哨兵 / ADAPT 12=11+1 / V2 回滚规则文 / mapping:191 措辞更正 / talkConfig fixture 风格注 / F-13 / _validate_segment 既有边角 / u-103-review §四.1 清扫。

## 九、未覆盖项 / 遗留

- V5/V6 部署面实测（RU/耗时对比、DLQ 实测）留 U-401/U-406，本地仅契约级自测。
- 真实 Service Bus/Key Vault live 链路未测（无云端 provisioning 纪律）。
- KV 直读兜底通道 live 未测（依赖未装，见偏差 4）。
