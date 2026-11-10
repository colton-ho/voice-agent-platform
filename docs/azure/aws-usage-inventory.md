# AWS Usage Inventory — Bedrock/LLM 層 + pipeline AWS 部件（#13/#14 施工盤點）

Author: 高见远 (solution-architect) · Date: 2026-11-06 · Status: **替換施工輸入**（《改動清單》的證據底稿）
Purpose: 面向 Bedrock→Azure AI Foundry 與 pipeline AWS→Azure 替換施工的全量用法盤點。file:line 級精度；每處記錄行為語義（參數/重試/超時/用量/失敗回退）。

**分區標記（zone）**：`【活代碼】` app 運行代碼（bot.py + 獨立模組 + scripts/ + tests/ + voice-server/）· `【vendored】` pipecat/ 樹（1.1.x 參考源，**不得作為 API 依據**，僅標行為語義）· `【凍結】` deploy/ 回滾區（cloudformation.yaml + deploy 腳本 = AWS 回滾路徑，**只標不動**，CR-1 口徑）。
**證據規則**：路徑均為 repo-root 相對；全部經 grep/閱讀實證，未見即標 *unverified*。與 `dependency-matrix.md` 既有行對照處標 [矩陣#行]。

---

## Part 1 — Bedrock / LLM 層（任務 #13）

### 1.1 SDK 引用點與版本

| # | 引用點 (file:line) | 內容 / 行為語義 | 分區 |
|---|---|---|---|
| 1 | requirements.txt:13 | `boto3`（聲明式、未鎖版本；鎖定見 lock） | 【活代碼】 |
| 2 | requirements.txt:8, :14 | 註釋記錄 extras 映射（`aws`→aiobotocore、`aws-nova-sonic`→`aws_sdk_bedrock_runtime`）；**aioboto3 已按 CR-1 移除**（與 pipecat 1.4.0 的 `aiobotocore<4` 不可共存 + G-2 死引用） | 【活代碼】 |
| 3 | requirements.lock:24/:39/:40 | `aiobotocore==3.10.0`、`boto3==1.43.117`、`botocore==1.43.117`（botocore 對鎖以滿足 aiobotocore 3.10.0 的 `<1.43.118` 釘）；:16–23 頭注記錄 CR-1 排除推理 | 【活代碼】 |
| 4 | requirements.lock:36 | `aws_sdk_bedrock_runtime==0.4.0`（Nova Sonic 雙向流 SDK） | 【活代碼】 |
| 5 | deploy/cloudformation.yaml:427 | `pipecat-ai[aws,aws-nova-sonic,silero,websocket]==1.4.0`（版本權威來源；代碼側 PINNED_PIPECAT bot.py:6090 雙保險） | 【凍結】 |
| 6 | deploy/cloudformation.yaml:428 | `boto3 aioboto3 aiohttp ...`（回滾行**含 aioboto3**——如重建 AWS 路徑會 ResolutionImpossible，CR-1 已記 cutover-runbook 警示） | 【凍結】 |
| 7 | pipecat/pyproject.toml:60 | `aws-nova-sonic = ["aws_sdk_bedrock_runtime~=0.4.0; python_version>='3.12'"]` | 【vendored】 |

### 1.2 bedrock-runtime 調用點（含參數/重試/超時/用量/失敗）

App 側共 **4 個 LLM 構造點**（3× `AWSBedrockLLMService` + 1× `AWSNovaSonicLLMService`）+ 1 個 smoke。調用面全部經 pipecat service 間接落到 bedrock-runtime（無 app 直呼 bedrock-runtime client）。

| # | 調用點 (file:line) | 行為語義 | 參數 / 重試 / 超時 / 用量 | 失敗 / 回退 | 分區 |
|---|---|---|---|---|---|
| L1 | `_build_pipeline` bot.py:2371–2387 | **流式對話主路徑**（pipeline 引擎逐 turn `converse_stream`）：`Settings(model=model_id, system_instruction, max_tokens=256)`；`enable_prompt_caching=True` **僅 Claude**（:2376–2380，`cachePoint` 被 Nova 以 400 拒絕 → 每模型能力門控）；ctor 逐次傳凍結憑證 :2381–2387 | max_tokens=256；temperature/stop 未設（走 pipecat 默認）；用量計量：`PipelineParams(enable_metrics=True, enable_usage_metrics=True)` bot.py:2506 → pipecat MetricsFrame（token/char）**只進事件流/日誌，不入庫** | LLM 異常 → pipecat pipeline 錯誤路徑（會話終止/事件）；無應用級重試（重試在 pipecat 內部：vendored 參考 `converse_stream` retry_timeout_secs=5.0、retry_on_timeout=False，**1.4.0 實現 unverified**） | 【活代碼】 |
| L2 | `_invoke_summary_bedrock` bot.py:5624–5679 | **一次性 Converse 總結**（`llm.run_inference(ctx)` :5675）：`Settings(model, system_instruction=SUMMARY_PROMPTS+JSON 指令, max_tokens=512)`；Claude 同樣門控 prompt caching :5664 | max_tokens=512；空轉寫短路 :5641–5649（不發請求，回空結構）；無內部重試 | 解析失敗**永不拋**（`_coerce_summary_struct` 回診斷結構）；Bedrock 錯誤**向上拋** → 調用方寫 `summary_status='failed'`（bot.py:1408–1415） | 【活代碼】 |
| L3 | `_invoke_translate_bedrock` bot.py:5741–5787 | **一次性 Converse 翻譯**（`run_inference` :5786）：`Settings(model, _TRANSLATE_SYSTEM_TEMPLATE, max_tokens=4096)`；model 解析鏈 `caller key → TRANSLATE_MODEL → DEFAULT_MODEL` :5763–5764 | max_tokens=4096；Claude caching 門控 :5773 | 解析失敗回 `{"_error": ...}` → 端點 502、**不寫盤**；Bedrock 錯誤向上拋 | 【活代碼】 |
| L4 | `_build_nova_sonic_pipeline` bot.py:2154–2167 | **S2S 雙向流**：`AWSNovaSonicLLMService(Settings(model="amazon.nova-2-sonic-v1:0", voice, system_instruction))`；凍結憑證 :2122；tools 注入 :2165–2166 | 參數僅 model/voice/system；用量 :2248 同 L1 | 依賴 `aws_sdk_bedrock_runtime`（缺失即 ImportError——api-notes §2.3 記錄）；決策 (a) 已裁定 **cutover 後退役**，AWS 回滾路徑保留 | 【活代碼】 |

**調用方接線（誰觸發 L2/L3）**：

| 觸發點 (file:line) | 語義 | 超時/回退 |
|---|---|---|
| 通話後任務 bot.py:1393 `asyncio.create_task(_summarize_and_update)` → :1400–1401 `asyncio.wait_for(..., timeout=30.0)` | **唯一的異步「作業」**：掛斷後 fire-and-forget 總結 | 30s 硬超時；失敗→`summary_status='failed'`+`summary_error`；**進程崩潰即丟**（G-3，無隊列/重試 → 目標 Service Bus worker） |
| `POST /api/summary` bot.py:5932 | 總結端點（error-as-data：200+`{error}`） | 同 L2 |
| 管理端 re-summarize bot.py:4090 | `POST /api/admin/history/{id}/summarize` 覆寫 summary | 同 L2 |
| `POST /api/admin/demos/{id}/translate` bot.py:5798–5815 | 翻譯預覽（**從不寫盤**，確認後走 PATCH） | 同 L3 |

**工具調用（tool use / function calling）接線**：

| 接線點 (file:line) | 語義 | 分區 |
|---|---|---|
| bot.py:2402–2405（pipeline）/ :2141–2144（nova）`_resolve_demo_tools` + `tools/registry.py: assemble_tools_schema` | 注冊表工具（end_call/transfer_to_human）→ pipecat ToolsSchema 注入 LLM context | 【活代碼】 |
| bot.py:2151–2152 `_connect_mcp_clients` + `_merge_mcp_tools`（pipeline 同構） | MCP 工具發現/合併；**注冊表同名勝出**（安全紅線）；MCPClient 走 pipecat `MCPClient`（bot.py:1909 —— U-105 的 `streamablehttp_client` 配對點） | 【活代碼】 |
| tool_filler.py:95–129 `with_filler` | 工具包裝器前置 `TTSSpeakFrame` ack（覆蓋 LLM `tool_use`→工具→再推理 的雙回合靜默，tool_filler.py:3–7）；`TOOL_FILLER_ENABLED` 門控 :31–41 | 【活代碼】 |
| 【vendored】pipecat/src/pipecat/services/aws/llm.py:315/:318/:336–358 | 行為參考：`service_name="bedrock-runtime"`（aiobotocore 會話）；一次性 `client.converse()`、流式 `client.converse_stream()`；`retry_timeout_secs=5.0`、`retry_on_timeout=False`（:126–127）；Settings 含 max_tokens/temperature/stop_sequences（:107–110）；toolConfig 由 pipecat context→request 組裝 | 【vendored】 |
| 【vendored】pipecat/src/pipecat/services/aws/nova_sonic/llm.py:598/:976/:776 | 行為參考：`invoke_model_with_bidirectional_stream`，endpoint `bedrock-runtime.{region}.amazonaws.com` | 【vendored】 |

### 1.3 模型 ID / inference profile 及用途

| key / 用途 | 模型 ID | 說明 (file:line) |
|---|---|---|
| `nova-2-lite`（**DEFAULT_MODEL**） | `us.amazon.nova-2-lite-v1:0` | bot.py:470/:482；電話默認 `PHONE_MODEL` 同 key（bot.py:2692；CFN 寫死 `PHONE_MODEL=nova-2-lite` cloudformation.yaml:470） |
| `nova-lite` | `us.amazon.nova-lite-v1:0` | bot.py:471（對話備選） |
| `nova-micro` | `us.amazon.nova-micro-v1:0` | bot.py:472（對話備選，低延遲） |
| `claude-haiku-4.5` | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | bot.py:473（對話備選） |
| `claude-sonnet-4.5`（**TRANSLATE_MODEL**） | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | bot.py:474/:488（demo 翻譯專用：nova-2-lite 會回顯長 prompt，claude-haiku 語言漂移，:483–487 註釋） |
| `claude-sonnet-4.6` | `us.anthropic.claude-sonnet-4-6` | bot.py:475（對話備選） |
| Nova Sonic S2S | `amazon.nova-2-sonic-v1:0` | bot.py:2160（僅 L4；決策 (a) 退役） |
| smoke 默認 | `us.amazon.nova-2-lite-v1:0` | smoke_bedrock.py:15（`BEDROCK_MODEL_ID` 覆蓋） |

關鍵約束（bot.py:465–468 註釋）：**Claude 必須走 inference profile**（`us.*`/`global.*`）——裸 `anthropic.*` ID 直接 `ValidationException`（"on-demand throughput isn't supported"）；棧在 us-east-1 故選 `us.*`。`_is_claude_model_id` bot.py:479–480（`.anthropic.` 子串判斷）驅動 prompt-caching 門控。
**形狀契約**：`/api/config` 的 `models[].bedrock_id` 字段名是 SPA 釘死的歷史形狀（talkConfig.spec.js:61–62；C-2 規定**保留字段名**）。
全倉庫 grep 實證：**無 titan-\*、無 embedding 模型、無 Bedrock KnowledgeBase/Rerank 調用**。

### 1.4 認證鏈

| # | 環節 (file:line) | 語義 | 分區 |
|---|---|---|---|
| 1 | bot.py:2347（L1）/:2122（L4）/:5658（L2）/:5767（L3）/:6392（asr-test） | **凍結憑證模式**：每構造一次 `boto3.Session(region).get_credentials().get_frozen_credentials()` → access_key/secret_key/session_token 三元組傳入 service | 【活代碼】 |
| 2 | mcp_sigv4.py:54–56 | MCP SigV4：boto3 **默認憑證鏈**（env/profile/EC2 role）`Session().get_credentials()`；:85–86 `botocore.auth.SigV4Auth`+`AWSRequest`；無憑證→`MissingCredentialsError` :42–46（降級不崩）；全懶加載 :22 | 【活代碼】 |
| 3 | mcp_config.py:63/:79 | `SIGV4_DEFAULT_SERVICE="bedrock-agentcore"`、默認 region `us-east-1`（MCP 側可配） | 【活代碼】 |
| 4 | Region 解析 | `AWS_REGION`（默認 `us-east-1`）：bot.py:2121/2346/5657/5766/6391；存儲層 `DDB_REGION` 優先：user_store.py:118、demo_store.py:94–95、voice_store.py:79–80、activity_store.py:187–188、bot.py:1432/6679 | 【活代碼】 |
| 5 | IAM（運行角色全量 action） | cloudformation.yaml:131–133 `bedrock:InvokeModel/WithResponseStream/WithBidirectionalStream`；:141 `bedrock-agentcore:InvokeAgentRuntime`；:145–146 `transcribe:StartStreamTranscription(WebSocket)`；:150 `polly:SynthesizeSpeech`；:160–162 `chime:List*/GetPhoneNumber`（只讀）；:167 `secretsmanager:GetSecretValue`；:171 `s3:GetObject`（代碼桶）；:175–193 `dynamodb:*`（分表）；:122–123 SSM + CloudWatchAgent 托管策略 | 【凍結】 |

### 1.5 環境變數（Bedrock/LLM 相關）

| 變量 | 讀取點 | 語義 |
|---|---|---|
| `AWS_REGION` | bot.py:2121/2346/5657/5766/6391；smoke_*.py:14/:21/:41 | Bedrock/Transcribe/Polly 調用區域（默認 us-east-1；`us.*` inference profile 綁定 us-east-1） |
| `DDB_REGION` | 4 存儲 + bot.py:1432/6679 | DynamoDB 區域覆蓋（`DDB_REGION`→`AWS_REGION`→`us-east-1`） |
| `BEDROCK_MODEL_ID` | **僅** smoke_bedrock.py:15 | ⚠️ app 內**死變量**（05-config-and-env.md:171 實證 grep 不到）；LLM 選型走 `MODELS` dict + WS query |
| `PHONE_MODEL` | bot.py:2692 | 電話呼入默認模型 key（fallback `DEFAULT_MODEL`；CFN :470 寫死） |
| （常量非 env）`DEFAULT_MODEL`/`TRANSLATE_MODEL` | bot.py:482/:488 | 內建常量 |
| `.env` 加載 | bot.py:105 `load_dotenv`；systemd `EnvironmentFile=/opt/voicebot/.env`（CFN :535） | .env.example = spec §11 模板（CR-2 口徑） |

### 1.6 客戶端初始化 / 生命週期

- **每次構造、無複用**：L1–L4 每個會話/調用新建 service 實例（凍結憑證當場解析）→ 憑證輪換後**長會話持舊憑證至構造下一實例**（生命週期語義，Azure MI 長駐客戶端是行為變化點）。
- chime 客戶端：每請求 `boto3.client("chime-sdk-voice", Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}))` bot.py:3348–3356。
- 存儲 Table：懶構造（user_store.py:123、demo_store.py:103、voice_store.py:98、activity_store.py:197；HistoryRecorder bot.py:1274 —— 同步 `boto3.resource` 經 `asyncio.to_thread`，:1270–1274 註釋記錄 moto 兼容原因）。

### 1.7 smoke 腳本與測試 mock/fixture

| 類型 | file:line | 語義 |
|---|---|---|
| smoke | smoke_bedrock.py:1–34 | 一次性 `run_inference`（LLMSettings max_tokens=128；`BEDROCK_MODEL_ID`） |
| smoke | smoke_transcribe.py:54–55 | 凍結憑證 → SigV4 presigner 連通 |
| smoke | smoke_polly.py:21 / smoke_minimax.py | Polly / MiniMax 連通（非 Bedrock） |
| mock | tests/test_demo_translate.py:240–346 | monkeypatch `bot.AWSBedrockLLMService`（Settings 反射取真類） |
| mock | tests/test_history.py:120/:178 | monkeypatch `bot._invoke_summary_bedrock`（成功/爆炸兩路徑；:11/:177–208 斷言 `summary_status='failed'`） |
| mock | tests/test_admin_dashboard.py:490–557 | fake `_invoke_summary_bedrock`（計數+參數斷言+失敗寫回） |
| mock | tests/test_mcp_sigv4.py:33–39、test_mcp_integration.py:298–305 | monkeypatch `boto3.Session` → 無憑證鏈（降級路徑） |
| stub | tests/test_phone_numbers.py:12–32 | `botocore.stub.Stubber` + monkeypatch `bot.boto3.client`（chime 讀接口；:149–152 客戶端構造爆炸也要降級） |
| fixture | tests/test_migrate_demos.py:27/:127/:244–301 | moto DDB 遷移測試（us-east-1） |

---

## Part 2 — pipeline AWS 部件（任務 #14）

### 2.1 編排（通話控制流 / 異步作業 / 填充器）

| # | 部件 (file:line) | AWS 耦合點 | 行為語義 / 失敗回退 | 分區 |
|---|---|---|---|---|
| O1 | 通話後總結任務 bot.py:1393 `asyncio.create_task` → `_summarize_and_update` :1395–1421 | 寫 DDB（summary/summary_status）+ L2 Bedrock | **唯一異步作業**：fire-and-forget、無隊列/重試/去重；wait_for 30s :1400；失敗寫 failed+error :1408–1415；進程死即丟 → Service Bus worker 目標（[矩陣 Service Bus 行]） | 【活代碼】 |
| O2 | call_control_tools.py:24（`GRACE_SECONDS=2.0`）、:123–137（write-before-teardown） | 經 HistoryRecorder 寫 outcome 行（bot.py:1317–1333 item 形狀） | `mark_transfer`/`write_outcome` **先寫後 EndTaskFrame**；外部「Connect Flow Lambda」消費方 :113–116（**A-2 已裁**：改倉內 Twilio 轉接流程，cutover 退役） | 【活代碼】 |
| O3 | timeout_filler.py（整檔） | **零 AWS** | BaseObserver + asyncio 定時器注入語氣詞；`UserStopped/TTSStarted` 幀競態關閉設計（檔頭文檔） | 【活代碼】 |
| O4 | tool_filler.py:31–41/:95–129 | **零 AWS**（包裝 LLM 工具回調） | 見 1.2 工具接線 | 【活代碼】 |
| O5 | 會話內核 bot.py:1138 `ACTIVE_SESSIONS`、:1528 `EventBroadcaster`、單進程 uvicorn :7398–7403 | 進程內 dict | [矩陣 事件扇出行] G-3：單副本語義 | 【活代碼】 |

### 2.2 批次（demo 遷移 / 批量翻譯）

| # | 部件 (file:line) | AWS 耦合點 | 語義 | 分區 |
|---|---|---|---|---|
| B1 | scripts/migrate_demos_to_ddb.py（整檔） | `DemoStore.put` → DynamoDB DemosTable（env `DEMOS_TABLE`，默認 `genaiic-voicebot-demos`；region 鏈同 demo_store） | 一次性冪等 seeder（skip/--overwrite）；`bot._to_ddb` float→Decimal；**不改運行時** | 【活代碼】 |
| B2 | scripts/migrate_demo_tools.py（整檔） | **零 AWS**（in-code SCENARIOS→manifest.yaml 磁盤遷移） | 一次性；自檢 7 ids | 【活代碼】 |
| B3 | scripts/latency_report.py | *unverified 內容*（未讀全文；僅列入清單） | 報告腳本 | 【活代碼】 |
| B4 | 翻譯「批量」 | L3 逐 demo 調用（bot.py:5798） | **無批量 API**——管理員逐個預覽；遷移側無批量 Bedrock 作業 | 【活代碼】 |
| B5 | tests/test_migrate_demos.py:27/:127/:244–301 | moto DDB | 遷移測試 fixture | 【活代碼】 |

### 2.3 事件驅動（status callback / 流式事件 / 訂閱輪詢）

| # | 面 (file:line) | 語義 | 分區 |
|---|---|---|---|
| E1 | **無 SQS/SNS/EventBridge**（全倉 grep 實證為零） | 事件面僅三種：① WS 流式事件（bot.py:1645–1669 `_WATCHED` 事件集）② `EventBroadcaster` :1528 → `/monitor/ws` :7292 訂閱扇出 ③ SPA 輪詢 `GET /api/admin/metrics` 7s（product-impact S-11） | 【活代碼】 |
| E2 | Twilio status callbacks | **不存在**（G-9：spec §6.1.8 新工作，簽名回調+fallback TwiML） | — |
| E3 | 流式語音事件 | Transcribe 流（asr_multilang presigner）/ Bedrock converse_stream / Nova bidi —— 均為**長連接流**，非消息驅動 | 【活代碼】 |

### 2.4 DynamoDB 存儲（接口面與查詢模式）

| 存儲 (file:line) | 表 env | PK / SK | GSI | TTL | 查詢模式 / 關鍵語義 | 分區 |
|---|---|---|---|---|---|---|
| user_store.py:42–47/:123 | USERS_TABLE | `username` (H) | 無 | 無 | get/put/delete 逐 key；bcrypt cost 12 :73；非 ConditionalCheck 外錯誤**不吞**（:189 註釋） | 【活代碼】 |
| demo_store.py:51–59/:103 | DEMOS_TABLE | `id` (H) | 無 | 無 | List=Scan + 掃描緩存；put 冪等 upsert | 【活代碼】 |
| voice_store.py:47–55/:98 | VOICES_TABLE | `provider` (H) / `voice_id` (R) | 無 | 無 | `seed_if_empty`；按 provider 分組 | 【活代碼】 |
| activity_store.py:57–68/:197 | ACTIVITY_TABLE | `day` (H) / `ts_id` (R) | 無 | `ttl`（now+`ACTIVITY_LOG_TTL_DAYS`×86400 :247；默認 90 天窗 :293） | `Key("day").eq` 逐日 Query :322；allow-list 明細脫敏 :100 | 【活代碼】 |
| HistoryRecorder bot.py:1254–1421/:1274 | HISTORY_TABLE | `call_id` (H) | **`caller-started-index`**（bot.py:3640；建表 test_history.py:44–57） | `ttl`（`HISTORY_TTL_DAYS` 默認 30，bot.py:1228/:1316） | caller 過濾→**GSI Query** :3613–3652（`ScanIndexForward=False` 新在前）；無 caller→**Scan+FilterExpression** :3654–3676；CSV 導出 Scan :3828；指標 Scan :6679；item 形狀 :1317–1333（outcome 行含 transfer_*） | 【活代碼】 |

> 關鍵語義：唯一 GSI 是 history 的 `caller-started-index`；列表/指標路徑大量 Scan → Cosmos 目標必須以分區鍵+索引消滅全表 Scan（[矩陣 Cosmos 行] 未展開此點，見補遺 F2）。

### 2.5 Chime 話務 / voice-server

| # | 面 (file:line) | AWS 耦合 | 語義 | 分區 |
|---|---|---|---|---|
| C1 | **voice-server/src/**/*.ts 全 12 檔** | **AWS SDK = 0**（grep `aws-sdk`/`@aws-sdk` 零命中；package.json/lock 無 AWS 依賴） | 純 SIP/RTP 協議橋：sip-server.ts:2/:80（UDP 5060）、server.ts:2–7/:19–20（RTP 10000–10999）、rtp-session.ts:5/:13（PCMU 8 kHz）、consts.ts:7–8（`CHIME_SAMPLE_RATE=8000`）→ **cutover 是整目錄退役，無 SDK 遷移工作量** | 【活代碼】 |
| C2 | `_list_chime_vc_numbers` bot.py:3322–3421 | `chime-sdk-voice` client :3348–3356（3s/5s/2 次重試） | 只讀 `ListVoiceConnectors` :3365 + `ListPhoneNumbers` :3376+ 後本地 join；best-effort 失敗回 `{voice_connectors:[], error}` :3341–3343；`CHIME_REGION="us-east-1"` :3322 → 決策 (d) 改 Twilio 同信封 | 【活代碼】 |
| C3 | chime_provisioner_handler.py:1–27 | 獨立 Lambda（開通器） | 倉外部署件，隨 Chime 退役 | 【凍結/外部】 |

### 2.6 Transcribe / Polly（含已知缺陷）

| # | 部件 (file:line) | AWS 耦合 | 語義 / 缺陷 | 分區 |
|---|---|---|---|---|
| T1 | `AWSTranscribeSTTService` bot.py:75/:2362–2369 | transcribe 流式 | 構造傳凍結憑證+`Settings(language=lang["stt"])`；per-call 區域 | 【活代碼】 |
| T2 | `_MultiLangTranscribeSTT` seam patch bot.py:6093–6183 | monkeypatch pipecat `get_presigned_url` 內部 | 多語言切換縫合層（版本守衛 PINNED_PIPECAT :6090–6149）；隨 Transcribe 退役 | 【活代碼】 |
| T3 | asr_multilang.py:35–202（endpoint :114） | 自研 SigV4 presigner | `wss://transcribestreaming.{region}.amazonaws.com:8443`；凍結憑證 presign；**整檔退役**（Azure STT 用 SDK 鑑權） | 【活代碼】 |
| T4 | `SimplePollyTTSService` bot.py:750–824（`polly.synthesize_speech` :793；`POLLY_VOICES` :831–859；構造於 `_build_tts` :1768–1779） | polly | **已知缺陷 G-2**：`aioboto3.Session()` bot.py:783 **全倉無 import** → 構造即 NameError（路徑從未可用）；不移植，Azure TTS 替代 | 【活代碼】 |
| T5 | `SimpleMiniMaxTTSService` bot.py:607–736 | **非 AWS**（MiniMax HTTP；`MINIMAX_API_KEY` :1790，密鑰源自 SecretsManager） | 保留為 adapter（spec §2 待隱私審批） | 【活代碼】 |

### 2.7 Secrets / KMS / CloudWatch / deploy / .env 約定

| # | 面 (file:line) | 語義 | 分區 |
|---|---|---|---|
| S1 | SecretsManager `MinimaxSecret` cloudformation.yaml:103–108；UserData `get-secret-value`→寫 `.env` :434–436；IAM :167–168 | 唯一密鑰存儲（MiniMax key）；啟動期解析 | 【凍結】 |
| S2 | **KMS = 0**（全倉 grep 實證無 kms/CMK 引用） | 無客戶密鑰 → Key Vault 對接 1:1 簡單映射 | — |
| S3 | S3 僅代碼桶（deploy.sh:35–85 上傳；CFN :420 下載；IAM :171–172 GetObject） | 零運行時 S3（[矩陣 S3 行] 已記） | 【凍結】 |
| S4 | CloudWatch：**無應用 metrics SDK**；loguru→stdout→systemd append `/var/log/voicebot.log`（CFN :528–536）；`CloudWatchAgentServerPolicy` :123（agent 實裝 unverified）；手動 CLI 查 `AWS/Bedrock` 命名空間（runbook 03:130–134，ModelId 維度） | 可觀測性全綠野 → Azure Monitor/OTel 綠建（[矩陣 CloudWatch 行]） | 【凍結】 |
| S5 | deploy.sh:20 `BEDROCK_REGION`（默認 us-east-1）→ :141 `BedrockRegion=` 參數；CFN :40 BedrockRegion 定義、:454 `AWS_REGION=${BedrockRegion}`；DRIFT_IMPORT_RUNBOOK.md:31–32/:210 | Bedrock 推理區可與 EC2 區分離（us.\* profile 綁 us-east-1） | 【凍結】 |
| S6 | `.env`：bot.py:105 `load_dotenv`；systemd `EnvironmentFile` :535；`.env.example` = spec §11 模板（CR-2）；`config/*.json`（runtime.json/mcp_servers.json）gitignored 單機文件態 | 文件態需遷移（[矩陣 env 行]） | 【活代碼】 |

---

## 統計摘要（盤點計數）

- **bedrock-runtime 調用面**：app 構造點 4（3 Converse 族 + 1 Nova bidi）+ smoke 1；一次性 run_inference 函數 2（summary/translate）被 4 個觸發點調用；流式主路徑 1。**app 零直呼 bedrock-runtime client**（全經 pipecat service）。
- **模型 ID**：app 6（3 Nova + 3 Claude，全 `us.*` inference profile）+ Sonic 1 + smoke 默認 1；titan/embedding = 0。
- **boto3/botocore/aiobotocore 引用**：【活代碼】約 25 處/10 檔（bot.py 13、stores 10、mcp_sigv4 4、smoke 2）；【測試】約 30 處/12 檔；【凍結】CFN/deploy 若干；【vendored】不計。
- **DynamoDB**：5 表（4 存儲 + history）；GSI ×1（caller-started-index）；TTL ×2（history ttl、activity ttl）；Scan 型路徑 ×3（列表/CSV/指標）。
- **Chime**：Python 客戶端 1 處（2 個只讀 API）；voice-server **AWS SDK 0 引用**（12 .ts 純協議）。
- **Transcribe/Polly**：Transcribe service 1 + 自研 presigner 1 + seam patch 1；Polly 自研 service 1（含 G-2 死引用 :783）。
- **周邊**：SecretsManager 1；KMS 0；SQS/SNS/EventBridge 0；CloudWatch SDK 0；S3 運行時 0。
- **smoke 腳本 4**；**AWS mock/fixture 測試 12 檔**。
