# pipeline infra + 部署腳本施工規格（#17 前置，任務 #24）

Author: 高见远 (solution-architect) · Date: 2026-11-09 · Status: **施工規格（doc-only）**
Purpose: 把 `bedrock-to-azure-mapping.md` §4/§5 下鑽為可施工規格，供 #17（pipeline AWS 部件改寫）消費。
**本檔只寫規格，不碰任何代碼/測試/requirements/providers（fullstack-dev U-101/102/104 在建，同文件衝突）。**

輸入優先級：inventory file:line 實證 + mapping 裁決 > 本規格 > implementation-plan 卡片描述；衝突點見 §0。
規格基準：`docs/Azure_Voicebot_Implementation_Instructions_Corrected.md` §9/§10/§11/§12（以下簡稱 spec §n）。
契約凍結：FC-0..FC-8 不動；變更走 CR。**CFN/deploy/ 全凍結不動（AWS 回滾路徑，CR-1 口徑）。**

---

## 0. 與既有文檔/卡片的衝突點（已裁決 2026-11-09，解法在案）

| # | 衝突 | 解法（本檔採納） | 裁決狀態 |
|---|---|---|---|
| X1 | mapping §4.1 按「5 DynamoDB 表→5 容器」敘述；spec §9.1/U-301 把通話數據拆成 4 實體（calls/call_events/transcripts/usage） | **history 表 → `calls` 容器為主體**（legacy item 的 summary/outcome/ttl 語義折疊於此），`call_events`/`transcripts`/`usage` 是 §9.1 **增量容器**（U-301 建造）。即 5 表對應面落定 + 3 增量 = 8 容器 | **已裁決：X1 批准**（team-lead 2026-11-09）。附帶條件：U-301 遷移必須保 summary/outcome/ttl 語義 + 外部 API 形狀不動（§1.1 注維持）；**遷移形狀變化列入 #18 改動清單** |
| X2 | team-lead 清單把 Twilio AccountSid/AuthToken 都入 Vault；spec §11 把 `TWILIO_ACCOUNT_SID` 列普通 env | 兩個都入 Vault（縱深防禦），env 注入值；§11 契約變量名不動 | **已裁決：X2 批准** |
| X3 | mapping §4.1 demos 行「常量分區 vs /id」留二選一 | 本檔 §1.1 落定**常量分區**（理由見該節） | **已裁決：X3 知悉，維持**（寫冷讀熱、單分區 List，裁定合理） |

另：label 改名 **B9 用戶已簽字「同意改名」（2026-11-09）**——本檔涉及 `models[].label` 處標「已簽」。

---

## 1. Cosmos DB 具體設計

### 1.1 database / container 命名 + 分區鍵落定

Database：**`voicebot`**（=spec §11 `COSMOS_DATABASE`）。Container 命名全小寫 snake。API：NoSQL（spec §12）。

| Container | 來源（file:line 實證） | 分區鍵 | id | 落定理由 |
|---|---|---|---|---|
| `users` | user_store.py:42–47（PK `username`） | `/username` | =username | 逐 key 點操作 1:1（get/put/delete），無列表面 |
| `demos` | demo_store.py:51–59（PK `id`；List=Scan+緩存） | **常量分區 `/type`（值 `"demo"`）** | =id | **二選一裁量（X3）**：8 個場景預設、寫冷讀熱（管理台 List 是熱路徑）。常量分區使 List=單分區查詢、點讀=pk 已知常量亦單分區；`/id` 會把每次 List 變跨分區查詢。小表（KB body 字節保真 R-P-003）遠低於邏輯分區 20GB 上限，無熱分區風險 |
| `voices` | voice_store.py:47–55（PK `provider`/SK `voice_id`） | `/provider` | =voice_id | 按 provider 分組查詢=單分區（provider 少數：polly/minimax/azure_speech）；`seed_if_empty` 全表讀=跨分區查詢（啟動一次性，可接受） |
| `activity` | activity_store.py:57–68（PK `day`/SK `ts_id`） | `/day` | =ts_id | 逐日 Query 1:1（bot.py 層 `Key("day").eq`）；allow-list 脫敏是應用層語義（:100），不入索引 |
| `calls` | HistoryRecorder bot.py:1254–1421（PK `call_id`；item :1317–1333）→ §9.1 `calls` | **`/caller_id`**（匿名呼叫者落哨兵 `"_none_"`） | =call_id | mapping §4.1 已落定：**今日唯一 GSI `caller-started-index`（bot.py:3640）的訪問模式本身就是分區鍵**——spec §9.1「勿假設 /call_id+複合索引能重現 caller GSI」的警告由此直接消解（caller 列表=單分區查詢，零跨分區代價）。代價：按 call_id 點操作降級為跨分區索引查詢（TTL 30 天量級可接受）；若成熱點再加 call_id→caller_id 索引容器（未來項，不預建） |
| `call_events` | §9.1 增量（U-301）：turns/outcome 行（bot.py:1317–1333/:1363–1421 的事件部分） | `/call_id` | =event_id（冪等去重鍵，R-A-007） | 不可變事件流；finalize 後遲到/重複事件拒絕（`CallFinalizedError`） |
| `transcripts` | §9.1 增量（U-301）：轉寫段（consent/retention 受控） | `/call_id` | =segment_id | 音頻**禁止**入 Cosmos（spec §9.1：私有 Blob，`RECORDING_ENABLED=false`） |
| `usage` | §9.1 增量（F9 綠野：今日 token 用量零持久化） | `/call_id` | =usage row id | U-502 成本對賬消費；provider usage + meter id + 對賬狀態 |

> 注意：`calls` 的 summary/summary_status/handoff_outcome/transfer_* 欄位保留（error-as-data 語義不動）；
> turns 內容遷 `call_events`/`transcripts`（內部形狀變化，**外部 API 形狀不動**——RETAIN 契約）。
> （X1 裁決附帶條件 2026-11-09：U-301 遷移保 summary/outcome/ttl 語義；**遷移形狀變化列入 #18 改動清單**。）

### 1.2 history 三條 Scan 路徑 → 複合索引定義（含排序方向）

`calls` 容器 indexingPolicy：`automatic: true`、默認 range 索引保留（過濾殘餘謂詞需要），**顯式複合索引一個**：

```json
"compositeIndexes": [
  [ { "path": "/started_at", "order": "descending" },
    { "path": "/id",         "order": "ascending"  } ]
]
```

| 今日路徑（file:line） | 現狀 | Cosmos 改寫 | 服務索引 |
|---|---|---|---|
| caller 過濾列表 bot.py:3613–3652（GSI Query，`ScanIndexForward=False`） | GSI | 單分區（`WHERE c.caller_id=@x ORDER BY c.started_at DESC, c.id ASC`） | CI-1 |
| 無 caller 列表 bot.py:3654–3676（Scan+FilterExpression） | 全表 Scan | 跨分區查詢 + 同 ORDER BY（filter 謂詞作殘餘過濾） | CI-1 |
| CSV 導出 bot.py:3828（Scan，無序契約） | 全表 Scan | 跨分區查詢 ORDER BY 同上（導出順序本無契約，新在前即可） | CI-1 |
| （附）指標聚合 bot.py:6679（Scan） | 全表 Scan | Cosmos 聚合（`SELECT VALUE COUNT(1)` / SUM），**替代 Python 端全掃** | 默認索引 |

- `id` 參與複合索引是為了同秒並列時的穩定排序（U-301 游標分頁需要確定性順序）。
  **[UNVERIFIED]** Cosmos 對系統 `/id` 進複合索引的限制 → 若施工驗證不支持，用業務去重鍵（如 `started_at` 後綴序號字段）替代，列入 V5 前置檢查。
- 排序方向固定 `started_at DESC, id ASC`（對應今日「新在前」）；CSV 不需要 ASC 索引（無序契約，不加第二個複合索引——防過度設計）。

### 1.3 TTL 實現要點（expires_at + 剩餘 TTL；spec §9.2）

Cosmos TTL = **距最後修改**起算（非業務到期），每次 update 都重置——直接搬 `ttl` 字段會把 30 天窗口無限續期（總結更新場景必踩）。規則：

1. 每容器 `defaultTtl: -1`（不靠容器默認），逐 item 顯式 `ttl`；
2. 每次寫入（**包括 summary/status 更新**）重算 `ttl = expires_at_epoch - now_epoch`（秒）——刪除時點恆等於 `expires_at`，與修改次數無關（mapping B6 的補償落點）；
3. `expires_at` 是業務邊界：`calls`/`call_events`/`transcripts`/`usage` = `started_at + HISTORY_TTL_DAYS`（默認 30，bot.py:1228）；`activity` = `ts + ACTIVITY_LOG_TTL_DAYS`（默認 90，activity_store.py:293）；四容器共享 call 的同一邊界；
4. **讀側也強制過期**（spec §9.2）：查詢帶 `WHERE (c.expires_at = null OR c.expires_at > @now)` 或等價——避免 TTL 刪除前讀到過期數據；
5. 遷移導入時**丟棄已過期行**（spec §9.2/§9.3）；`expires_at` 從 legacy `ttl` 字段反推（`expires_at = ttl` 的 epoch 語義 1:1，legacy `ttl` 就是 epoch 秒，bot.py:1316）。

### 1.4 RU / 容量 / 一致性建議（workload-driven，起始值實測後調）

| 項 | dev | staging/prod | 說明 |
|---|---|---|---|
| 吞吐 | database 級 shared **400 RU/s**（8 容器共用） | `calls` 家族（calls/call_events/transcripts/usage）**autoscale 1000–4000**；配置 4 容器 shared 400 | 量級：通話 TTL 30 天窗 + 8 demo + 少量 users/voices；先實測（V5）再調 |
| 一致性 | **Session**（Cosmos 默認） | Session | spec §12「approved consistency」留 U-401 參數；單寫區域下 Session=性價比最優，Strong 不採 |
| 備份 | 連續備份 7 天 | 連續備份 30 天 | U-401 參數 |
| 熱分區（spec §9.1 要求定義） | — | 監控 pk 分布；**哨兵 `"_none_"` 是潛在熱分區**（匿名呼叫者集中） | 現量級接受；超限再議分片哨兵（`"_none_#0..N"`）——**不預建**，只掛監控 |

> 勘誤指針（U-401 補注，2026-11-10）：上表「autoscale 1000–4000」地板不可達——Cosmos autoscale 實際吞吐帶=max 的 10%–100%，max=4000 ⇒ **400–4000 RU/s**；勘正與 V5 調參口徑見 `u-401-deployment-plan.md` §6「Cosmos 吞吐」行（原文數字不改動，僅此備案）。

---

## 2. Service Bus 最小集（F8：僅此一作業，不建通用消息層）

### 2.1 隊列命名與顯式配置（TTL 不靠默認——官方文檔自相矛盾）

Namespace：`sb-voicebot-<env>`（=spec §11 `SERVICEBUS_NAMESPACE`）。**單隊列** `postcall-jobs`（job_type 判別：summary 現在；approved artifacts 未來同隊列，F8 最小集）：

| 配置項 | 值 | 显式設置理由 |
|---|---|---|
| `defaultMessageTimeToLive` | **P7D（7 天）** | 显式釘死（V6）；過期消息不應殘留到月底 |
| `enableDeadLetteringOnMessageExpiration` | **true** | 過期消息進 DLQ 可觀測，而非靜默丟棄 |
| `lockDuration` | **PT1M（60s）** | 需 > worker 單次處理節奏（30s 總結超時 + 寫回）；显式不走默認 |
| `maxDeliveryCount` | **5** | 重試上限；超限自動死信 |
| `enableDuplicateDetection` | false | 冪等在消費側按 `job_id` 做（見 2.2）——broker 去重窗口語義不如消費側可靠，不疊加 |
| DLQ | `postcall-jobs/$DeadLetterQueue`（內建） | 消費端手動排空入口（U-406 runbook 屬 Phase E） |

### 2.2 post-call 冪等 job（R-A-010）：投遞 / 消費 / 重試 / 30s 超時形態

- **投遞**：通話結束（BYE/write-before-teardown，call_control_tools.py:123–137 語義保留）→ 投 `{"job_type":"summary","job_id":"summary:<call_id>","call_id":...}`，`MessageId=job_id`。生產=at-most-once（與現狀 fire-and-forget 等同，進程崩潰仍可能丟——**outbox 不預建**，Phase D 議）。投遞方：runtime（mi-runtime，Data Sender）；管理端 re-summarize（bot.py:4090）**已裁決（team-lead 2026-11-09）：投同隊列異步**——`POST /api/admin/history/{call_id}/summarize` 回 `200 {call_id, summary_status:"pending"}` 受理 → 行翻 pending → worker 終態；入隊/持久化失敗仍 `500 {error}`。**C-9 註冊生效**（re-summarize 回應語義：`summary` 省略 + `summary_status:"pending"`，additive/behavioral）。理由：B7 的「超時=暫時性可重試」只在 worker 模型成立（同步 30s 硬超時只能即刻返 failed，行為分裂）；單一摘要路徑=單一重試/冪等語義（R-1…R-4）；SPA 已驗證容忍（零代碼改動）。
- **消費**：worker（mi-worker，Data Receiver）peek-lock 處理：
  1. 以 `calls.summary_status`/etag 查重：已 **`ok`** → 直接 complete（重複投遞無副作用，R-A-010；wire enum `{pending, ok, failed}`——product-impact §7.1 釘死，**不得引入第四個值**）；
  2. 空轉寫短路語義保留（bot.py:5641–5649，不發 LLM 請求寫空結構）；
  3. LLM 總結調用包 **30s 硬超時**（`asyncio.wait_for(timeout=30.0)`——bot.py:1400–1401 的預算**移 worker 側原值保留**）；
  4. 成功 → 寫 summary（optimistic concurrency）→ complete；解析失敗 → `_coerce_summary_struct` 診斷結構（**永不拋**，bot.py:5675 語義）→ complete。
- **重試**：暫時性失敗（LLM/網絡/429/5xx）→ abandon（delivery_count++，借 lock 過期自然退避）至 maxDeliveryCount=5 → DLQ；**永久性失敗**（如調用配置非法）→ 寫 `summary_status='failed'` + `summary_error`（error-as-data 語義 :1408–1415）→ complete（failed 是終態，不重試）。
  **I-4 硬性不變式（已裁決 team-lead 2026-11-09）：重試耗盡必落 `failed` 終態，任何行不得永卡 `pending`**——worker 消費時讀投遞計數，當 `delivery_count == maxDeliveryCount`（=5，即最後一次投遞）仍遇暫時性失敗 → **走永久性失敗分支**：寫 `summary_status='failed'` + `summary_error`（error-as-data）→ complete（不再 abandon）。DLQ 只留進程崩潰/未及處理的殘餘 poison；U-406 runbook 加 DLQ 清掃條目（殘留 pending 超 A-3 上限 15 分鐘翻 failed）——runbook 屬 Phase E（此為指針）。
- **30s 超時的具體形態**：不是隊列鎖超時，是 handler 內 `asyncio.wait_for` 對 LLM 調用的預算——超時按暫時性失敗走重試（行為變化：今日超時=failed 終態，明日=可重試，屬 B7 已記錄範疇）。

### 2.3 驗證點挂鉤

- **V6**：逐隊列顯式 TTL/lock/maxDelivery 值與上表一致（Bicep 部署後 `az servicebus queue show` 核對）+ 過期消息進 DLQ 實測。
- **V9**：摘要可用時機=最終一致（掛斷後由 worker 異步落）；S 面口徑由 product-designer 出稿落 `docs/azure/product-impact.md`（2026-11-09 派發），驗收時指向該節（mapping B7 已記錄）。

---

## 3. Key Vault

### 3.1 secret 清單（1:1 對應 + 用戶裁決在案）

| Secret 名（建議） | 來源（現狀） | 消費方 | 說明 |
|---|---|---|---|
| `MINIMAX-API-KEY` | SecretsManager `MinimaxSecret`（cloudformation.yaml:103–108；UserData 解析→.env :434–436） | runtime（bot.py:1790 讀取點語義保留） | **用戶裁決 2026-11-09：MiniMax 保留（G-2 已批）**——照遷 Key Vault；U-101 以新註冊名（如 `minimax_tts`）建 MiniMax FC-4 適配器，**遺留名 denylist 不變** |
| `TWILIO-AUTH-TOKEN` | 新增（G-9/§6.1 工作） | control（webhook 驗簽，U-201）+ runtime（media 升級驗簽） | spec §11 `TWILIO_AUTH_TOKEN_SECRET_NAME` 指向本名 |
| `TWILIO-ACCOUNT-SID` | 新增 | control + runtime | spec §11 是普通 env（X2 解法：也入 Vault，env 注入值） |
| `AUTHSECRET` | 現狀 AUTH_SECRET ephemeral fallback（bot.py:155–161） | control（JWT 簽發） | **已裁決（team-lead 2026-11-09）：採納入 Vault**——Container Apps 引用注入 env（參數名不動）；ephemeral fallback 於 Azure 路徑退役→**缺值 fail-fast**（多副本會話一致性優先）；已記 B12（併 B8 口徑） |

### 3.2 MI 讀取 + 輪換處理

- **讀取**：Container Apps 環境 Key Vault 引用（MI，secret 不落 env 文件）+ 應用啟動解析（保留「啟動期取密鑰」語義，對應 UserData 舊路徑）。主體：mi-runtime / mi-control / mi-worker（§5）。
- **輪換**：Key Vault **無內置自動輪換**——現狀僅 3–4 個 secret，**不建輪換 Function/Logic App**（防過度設計）；手動輪換流程記 U-406 runbook（Phase E）。Twilio token 輪換本質是 Twilio 控制台操作；MiniMax key 同理。MI token 刷新語義見 mapping §3.1（F4 行為變化點，不重複）。

---

## 4. 部署腳本 / 配置文件改動清單（逐文件劃界）

### 4.1 Bicep 模組清單（spec §12 口徑 = Bicep + GH Actions OIDC）

| 模組（新建 `infra/bicep/`） | 內容 | 歸屬 |
|---|---|---|
| `main.bicep` | 編排 + env 參數化（dev/staging/prod） | U-401 |
| `modules/cosmos.bicep` | §1 的 8 容器 + 索引/TTL/一致性/備份 | U-401（**最小集建議提前**，見下） |
| `modules/servicebus.bicep` | §2 隊列 + 显式 TTL/DLQ/lock | U-401（同上） |
| `modules/keyvault.bicep` | §3 vault + secret + access policy/RBAC | U-401（同上） |
| `modules/containerApps.bicep` | env + control/runtime/worker 三 app | U-401 |
| `modules/acr.bicep` | 映像倉庫 | U-401 |
| `modules/identities.bicep` | MI + role assignments（§5） | U-401 |
| `modules/blob.bicep` | 私有容器、公訪禁用（R-A-008） | U-401 |
| `modules/monitor.bicep` | Log Analytics + App Insights + alerts/budgets/action groups | U-401（telemetry 儀表 U-403） |
| `modules/network.bicep` | DNS/TLS/私有端點 | U-401 |
| `modules/redis.bicep`（佔位） | D-9 未批，**不建** | — |
| Speech/Foundry 資源 | 商業審批/部署手動前置，文檔化（spec §12） | 用戶（G-2）+ U-401 文檔項 |

### 4.2 逐文件劃界（本工作包 #17 必須 vs 推遲 Phase E）

| 路徑 | 改動內容 | 歸屬任務卡 / 劃界 |
|---|---|---|
| `deploy/cloudformation.yaml` | **不動**（AWS 回滾路徑凍結，CR-1；:426–429 aioboto3 警示已在 cutover runbook） | — |
| `deploy/deploy.sh`、`deploy_use_previous_secrets.sh` | **不動**（凍結） | — |
| `infra/bicep/**`（新建） | §4.1 模組 | U-401；**已裁決（2026-11-09）：cosmos/servicebus/keyvault 三模組提前**供 #17 開發/驗證用，其餘留 U-401。**邊界：寫 Bicep 代碼 ≠ 授權部署——雲上 provisioning 仍待用戶明示授權，#17 只落代碼不執行部署** |
| `.github/workflows/**`（新建） | GH Actions OIDC 流水線（spec §12 8 步） | **U-402，推遲 Phase E** |
| `stores/*.py`（user/demo/voice/activity_store.py） | DynamoDB→Cosmos 適配器（**方法簽名不變**，discovery §10 原則） | U-302 |
| `bot.py`（HistoryRecorder :1254–1421、總結任務 :1393） | → `storage/cosmos_calls.py`（FC-6）+ Service Bus 投遞 | U-301 + U-304 |
| `workers/`（新建） | post-call 冪等 worker（§2.2） | U-304 |
| `scripts/migrate_demos_to_ddb.py` | → Cosmos 一次性冪等 seeder（skip/--overwrite 語義保留） | **已裁決（2026-11-09）：歸 U-404（D-8 門控），不併 #17**——腳本須活 Cosmos 目標 + 真實數據才能驗（冪等/逐字節校驗 R-P-003）；#17 只保證 store 層 API 形狀讓腳本保持薄 |
| `scripts/migrate_demo_tools.py` | **不動**（零 AWS，盤點 B2） | — |
| `config/runtime.json`、`mcp_servers.json` | 單機文件態 → 持久存儲/每副本重歸屬（R-C-003） | U-302 |
| `.env.example` | **不重改**（U-003/CR-2 已改為 spec §11 模板）——#17 施工時核對表名 env 變量退役標注 | 核對項 |
| `asr_multilang.py`、`smoke_bedrock.py` 等 | 退役/重寫為 Azure 連通冒煙 | **U-101 範圍（非 #17）**，標註防誤認 |
| `voice-server/` | **不動**（F1 零 SDK，cutover 整目錄退役） | U-207/cutover |
| `tests/`（12 ADAPT 檔） | moto/boto3 fixture → Cosmos 契約測試 | U-303 |
| `docs/onboarding/*`、runbooks | 更新 | **U-406，推遲 Phase E** |
| `README` env 表 | 更新（USERS_TABLE 等 5 表名 env 變量退役） | #18 改動清單 |
| telemetry/OTel 接線 | §13 指標 | **U-403，推遲 Phase E** |

---

## 5. RBAC 具體 role assignments（mapping §3.2 九行展開）

主體：`mi-runtime`（runtime app）/ `mi-control`（control app）/ `mi-worker`（worker）/ `mi-deploy`（GH OIDC 聯邦，U-402）。環境資源各自獨立（spec §10）。

| # | Scope | Role | Principal | 理由（AWS 對應） |
|---|---|---|---|---|
| 1 | Foundry/AOAI 資源 | **Cognitive Services OpenAI User** | mi-runtime, mi-worker, mi-control | bedrock:InvokeModel×3（F11）；worker=總結/翻譯作業，control=翻譯預覽端點 |
| 2 | Speech 資源 | **Cognitive Services Speech User** | mi-runtime | transcribe×2 + polly（F11）；STT/TTS 只在通話面 |
| 3 | Key Vault | **Key Vault Secrets User** | mi-runtime, mi-control, mi-worker | secretsmanager:GetSecretValue（F11）；§3.2 |
| 4 | Cosmos 帳戶 | **Cosmos DB Built-in Data Contributor** | mi-runtime, mi-control, mi-worker | dynamodb 分表 CRUD（F11）；built-in 角色為帳戶級，按容器細分需自定義角色（不預建） |
| 5 | Service Bus 隊列 `postcall-jobs` | **Azure Service Bus Data Sender** | mi-runtime, mi-control | 投遞 post-call job（runtime）/re-summarize（control） |
| 6 | 同上 | **Azure Service Bus Data Receiver** | mi-worker | worker 消費 |
| 7 | ACR | **AcrPull** | mi-runtime, mi-control, mi-worker（或環境 MI） | s3:GetObject 代碼桶（F11）→ 映像拉取 |
| 8 | Blob（approved artifacts） | **Storage Blob Data Contributor** | mi-runtime | 零運行時 S3 對應；`RECORDING_ENABLED=false`，最小面 |
| 9 | Twilio / MCP bedrock-agentcore | **無 Azure RBAC** | — | Twilio=外部帳戶（憑證入 Vault）；MCP sigv4 按決策 (c) 降級，不遷 |
| 10 | 資源組（部署面） | Contributor（或最小 deployment 角色） | mi-deploy（GH OIDC，分環境聯邦） | spec §10；審批門 U-402 |

---

## 6. P7 零引用項「不實現」清單（防施工側過度設計）

全倉 grep 實證零引用（盤點 §2.3/F8），以下**一律不建**，僅記錄對應關係：

- SQS →（Service Bus 隊列已建，但**只有 postcall-jobs 一個**——不是通用消息層）；SNS/EventBridge → Event Grid / Event Hubs Capture / Schema Registry：**不建**（事件面僅 WS 流 + EventBroadcaster + SPA 輪詢，盤點 E1）
- Step Functions → Durable Functions/Logic Apps：**不建**（無工作流）；AWS Batch → Azure Batch/Container Apps Jobs：**不建**（遷移腳本一次性）
- Lambda → Functions：**不建**（Connect Flow Lambda 已被 A-2 倉內 Twilio 流程替代）
- KMS/CMK：**不建**（F7=0）；S3 運行時/Blob 錄音：**不建**（RECORDING_ENABLED=false）
- DynamoDB Streams → Cosmos Change Feed：**不建**（零 Streams 引用）
- embedding/Titan/Rerank/KB：**不建**（零引用）；Azure Managed Redis：**不建**（D-9 未批）
- 通用 retry/DLQ 框架、outbox、CQRS、自定義 Cosmos 角色、哨兵分片：**不建**（各自「未來項」記錄在 §1/§2，夠用即止）

---

## 7. F1–F11 行為保真義務對照表（本規格的補償落點）

| F 項 | 義務 | 本規格落點 |
|---|---|---|
| F1 voice-server 零 SDK | 整目錄退役，無 SDK 遷移 | §4.2 劃界行（不動） |
| F2 GSI+3 條 Scan | 消滅全表 Scan | §1.1 `/caller_id` 分區 + §1.2 CI-1 + 指標聚合化 |
| F3 重試/超時預算 | 復刻 5s/2 次/30s | §2.2（30s 移 worker 側原值）；LLM 側 U-102（mapping §1.4） |
| F4 凍結憑證生命週期 | 行為變化點記錄 | §3.2（指向 mapping §3.1，不重複） |
| F5 caching 門控 | per-deployment | U-102（本規格不涉及，標註） |
| F6 max_tokens 地圖 | 原值遷移 | U-102（標註） |
| F7 KMS=0 | Key Vault 1:1 | §3.1 |
| F8 無消息中間件 | Service Bus 最小集 | §2（單隊列） |
| F9 用量零持久化 | `usage` 容器綠野 | §1.1 `usage` 行（U-502 消費） |
| F10 配置契約 | bedrock_id 標註、死變量刪除 | §4.2 .env.example 核對行 |
| F11 IAM 全量 | RBAC 對照 | §5 十行 |

---

## 8. 驗證點挂鉤 + mapping §7 breaking change（不改 mapping；BC-c1/c2/c3 已由 team-lead 記入 mapping §7 為 B10/B11/B12）

### 8.1 驗證點

- **V5**（Cosmos 分區鍵切換）：8 容器建齊後逐路徑 RU/耗時對比（caller 列表/全局列表/CSV/指標四路徑）+ §1.2 `/id` 複合索引支持度前置檢查 + 數據遷移完整性（U-404）。
- **V6**（Service Bus）：§2.1 六個配置值部署後逐項核對 + 過期→DLQ 實測 + 重複投遞冪等實測（R-A-010）。
- **V9**（摘要時機）：掛斷→摘要可用的端到端延遲分佈；S 面口徑見 `docs/azure/product-impact.md`（product-designer 出稿，2026-11-09 派發；B7 在案）。

### 8.2 新增 breaking change（**不改 mapping 文檔**——BC-c1/c2/c3 已由 team-lead 統一記入 mapping §7 為 B10/B11/B12，2026-11-09）

| 候選 | 內容 | 性質 | 裁決狀態 |
|---|---|---|---|
| BC-c1 | **5 個 DynamoDB 表名 env 變量退役**（USERS_TABLE/DEMOS_TABLE/VOICES_TABLE/ACTIVITY_TABLE/HISTORY_TABLE）→ COSMOS_ENDPOINT/COSMOS_DATABASE 契約變量（R-C-003） | 配置（運維可見） | **已記 B10**（team-lead 統一記入）；#18/README 同步 |
| BC-c2 | **MiniMax 註冊名變化**（U-101 新名如 `minimax_tts`）vs voice registry `provider` 數據鍵（voice_store PK，盤點 §2.4） | 原標用戶可見→**裁決後零用戶可見變化**（C-1 邊界內） | **已記 B11（裁決 2026-11-09：零用戶可見變化）**：voice-store `provider` 數據鍵保留 `minimax` **不遷移不重灌**（數據鍵≠註冊名，不觸 denylist；voices 容器 `/provider` 分區設計不變），SPA 顯示字串保留 "MiniMax"（顯示映射承載=真實供應商名，同 B9 口徑）。**裁決後無需簽字——若 SPA/fixture 釘死註冊名被迫遷數據鍵，才升級 UI 變化走 V11** |
| BC-c3 | AUTH_SECRET 入 Vault（§3.1 **已裁決採納**）——ephemeral fallback 行為退役，缺值 fail-fast | 行為（部署） | **已記 B12**（併 B8 口徑） |

（label 改名 B9 用戶已簽「同意改名」2026-11-09，非候選。B7 已由 team-lead 補記超時語義變化：failed 終態→暫時性可重試（§2.2 發現）。）

---

*Contract freeze: FC-0..FC-8 不動，變更走 CR。與卡片衝突處以 inventory file:line + mapping 裁決為準（衝突點 §0 在案）。*
