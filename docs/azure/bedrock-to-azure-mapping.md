# Bedrock→Azure 對應方案 + 改動清單骨架（#15）

Author: 高见远 (solution-architect) · Date: 2026-11-06 · Status: **方案側定稿（施工側待回填）**
Purpose: 以 `aws-usage-inventory.md`（file:line 實證）+ `dependency-matrix.md` 補遺 F1–F11 為輸入，
給出模型/服務映射、行為保真要點與改動清單骨架。**本檔定義「模型選擇與行為映射」，不造適配器**——
FC-3 實現體是 `providers/llm/azure_openai.py`（U-102，在建），FC-5 槽位在 `providers/base.py`（冷路徑）。

**基準**：模型/服務映射採用 2026-11-09 網絡考證結論（team-lead 合併下發，來源 URL 見任務 #15 描述）；
標 **[UNVERIFIED]** 處沿用該標記，列入 §7 現場核實清單。**方案與盤點矛盾處以盤點 file:line 實證為準。**
**契約凍結**：FC-0..FC-8 不動（implementation-plan §3），任何介面變更走 CR。

---

## 1. 模型對應表（app 實際用到的 6+1 model ID）

現狀（盤點 §1.3）：`MODELS` dict bot.py:469–476 = 3 Nova + 3 Claude（全 `us.*` inference profile）+ Nova Sonic（bot.py:2160）；
titan/embedding/KB = 0 引用。`DEFAULT_MODEL="nova-2-lite"` :482、`TRANSLATE_MODEL="claude-sonnet-4.5"` :488、`PHONE_MODEL`（bot.py:2692）。

### 1.1 Claude 檔（Foundry 原生 Claude，首選——行為保真度最高）

Claude 在 Microsoft Foundry 有原生版本（GA 2026-06-29）。兩種托管：**hosted on Azure**（Entra 認證/計費/治理，US data zone）
與 hosted on Anthropic；部署版本 1=Anthropic 托管、**版本 2=Azure 托管（首選）**。區域 eastus2/swedencentral 全家族、westus2 Sonnet+Opus。
計費 CCU（100 CCU=$1，Azure Marketplace，可計 MACC）。1M 上下文覆蓋 4.6+ 型號。

| app key (bot.py) | Bedrock ID（現狀） | Foundry 部署建議 | 行為保真要點 | 無對應時替代 |
|---|---|---|---|---|
| `claude-haiku-4.5` :473 | `us.anthropic.claude-haiku-4-5-20251001-v1:0` | `claude-haiku-4-5`，hosted-on-Azure（部署版本 2），eastus2 | Messages API ↔ Converse 語義；工具流/流式支持；**prompt caching（F5 門控）逐特性核對 hosted-on-Azure 支持度 [UNVERIFIED]** | gpt-4.1-mini（檔位下移）/ gpt-5-mini |
| `claude-sonnet-4.5` :474（**TRANSLATE_MODEL**） | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | `claude-sonnet-4-5`，hosted-on-Azure，eastus2/swedencentral | 翻譯專用（max_tokens=4096，F6）；長輸出指令跟隨是保留它的理由（bot.py:483–487 註釋） | gpt-5（深度推理）/ gpt-4.1 |
| `claude-sonnet-4.6` :475 | `us.anthropic.claude-sonnet-4-6` | `claude-sonnet-4-6`，hosted-on-Azure，eastus2/swedencentral | 1M 上下文覆蓋 4.6+；`us.*` profile 約束（bot.py:465–468）隨 Bedrock 消亡 | 同上 |

- **托管默認（team-lead 已裁決 2026-11-09）**：hosted-on-Azure **部署版本 2** 為默認（數據駐留 Azure 側，隱私默認最穩），
  版本 1（hosted-on-Anthropic）僅作退路。
- **退路規則**：個別 API 特性可能僅 hosted-on-Anthropic（部署版本 1）有 → 若 F5 cachePoint 等價物在 hosted-on-Azure 缺失，
  該部署改版本 1（Anthropic 托管）或退 gpt 檔；**不得全局關閉 caching 遷就**（F5 是 per-model 門控，映射後仍是 per-deployment 門控）。
- `_is_claude_model_id`（bot.py:479–480，`.anthropic.` 子串）隨調用面退役，門控改由配置側 per-deployment 能力位承載（見 §2）。

### 1.2 Nova 檔（無同族等價，按檔位映射）

| app key (bot.py) | Bedrock ID（現狀） | Foundry 部署建議 | 檔位/理由 | 替代 |
|---|---|---|---|---|
| `nova-2-lite` :470（**DEFAULT_MODEL + PHONE_MODEL**，最高流量路徑） | `us.amazon.nova-2-lite-v1:0` | **gpt-4.1-mini**（eastus2） | 對話默認：低延遲高吞吐最接近 nova-2-lite 定位；max_tokens=256 逐 turn（F6） | gpt-5-mini（推理強、延遲高） |
| `nova-lite` :471（對話備選） | `us.amazon.nova-lite-v1:0` | **gpt-5-mini** | 中檔備選，與 nova-2-lite 部署拉開差異（6 key 保持 6 個可選部署） | gpt-4.1-mini |
| `nova-micro` :472（低延遲備選） | `us.amazon.nova-micro-v1:0` | **gpt-4.1-nano** | 最快/最省；或 Phi-4-mini 作低成本替代 | gpt-5-nano / Phi-4-mini |
| Nova Sonic S2S（L4 bot.py:2154–2167） | `amazon.nova-2-sonic-v1:0` | **Azure AI Voice Live API（GA）**：GPT-Realtime/GPT-4o/GPT-4.1（GA）、GPT-5 系/Phi-4-MM-Realtime（預覽）；SDK `azure-ai-voicelive` Python GA | 決策 (a)：FC-5 槽位，`VOICE_LIVE_ENABLED=false` 冷路徑；nova-sonic 路徑 cutover 後退役（AWS 回滾保留） | 無（S2S 唯一對應） |

- GPT-5 系 272K 入/128K 出（共 400K）；gpt-4.1 最高 ~1M。dall-e-3 已退役勿選。
- Voice Live：單一 WebSocket 整合 ASR+LLM+TTS；140+ 語言、語義 VAD、Custom Voice；按模型 Pro/Basic/Lite 計費檔。
  啟用前須基準測試 + 錄製 endpoint/model/voice/billing（G-6，dependency-matrix Voice Live 行）。
- **team-lead 裁決 2026-11-09 採納**：上表 6 key→6 部署的檔位分配（nova-2-lite→gpt-4.1-mini / nova-lite→gpt-5-mini /
  nova-micro→gpt-4.1-nano）**經裁決確認**，不收窄到考證基準的二選一——理由：保持現有語義分層（默認/備選/低延遲三檔）不塌縮，
  默認路徑取低延遲款。**V2 若不過，nova-2-lite 回退 gpt-5-mini 再議。**

### 1.3 零引用項（記錄在案，不設計不存在的架構）

| 項 | 現狀 | 對應 | 說明 |
|---|---|---|---|
| Titan Text Embeddings | **0 引用**（盤點 §1.3 grep 實證） | text-embedding-3-large/small（維度/向量空間不同 → 全量重建索引） | breaking change **本倉不觸發**，僅記錄 |
| Titan Multimodal | 0 引用 | Cohere embed-v-4-0 **[UNVERIFIED]** | 同上 |
| Bedrock KnowledgeBase/Rerank | 0 引用 | Azure AI Search / Foundry IQ | 不適用 |
| Nova Canvas/Reel | 0 引用 | gpt-image-2/FLUX、Sora-2 預覽且退役傳聞 **[UNVERIFIED]** | 不適用 |
| 視頻輸入理解 | 0 引用 | 抽幀方案 | 不適用 |

### 1.4 行為保真要點（跨全部映射，對應補遺 F3/F5/F6/F9）

| 維度 | 現狀（實證） | Azure 端要求 |
|---|---|---|
| max_tokens 任務地圖（F6） | 對話 256（bot.py:2374）/ 總結 512（:5662）/ 翻譯 4096（:5771）/ smoke 128 | 原值照搬；**翻譯部署 max output 必須 ≥4096**；gpt 系 max_tokens↔max_completion_tokens 參數名差異在 U-102 適配 |
| prompt caching 門控（F5） | `enable_prompt_caching=True` 僅 `.anthropic.` 模型（bot.py:2376–2380/5664/5773；Nova 以 400 拒 `cachePoint`） | **per-deployment 能力位，禁全局開**：Claude-on-Foundry 顯式 caching（等價物支持度 [UNVERIFIED]）；gpt 系在 Azure OpenAI 為自動 caching（無顯式開關）→ 能力位語義=「是否傳顯式 caching 參數」 |
| 工具調用 | ToolsSchema 注入（bot.py:2402–2405/:2141–2144）+ MCP 合併（:2151–2152，注冊表同名勝出） | Chat Completions/Responses 工具流均支持；**工具定義翻譯**（pipecat toolConfig→OpenAI tools）在 U-102 內做，不改 `tools/registry.py` 輸出形狀 |
| 流式語義 | `converse_stream` 逐 token（L1）；`run_inference` 一次性（L2/L3） | 流式 SSE 對齊；一次性調用= FC-3 `stream()` 收集或非流式調用（pipecat 1.4.0 Azure service 是否有 run_inference 等價 **[UNVERIFIED]** → U-102 補 one-shot helper） |
| 重試/超時（F3） | 流式 retry_timeout_secs=5.0 / retry_on_timeout=False（vendored 1.1.x 參考，1.4.0 實現 unverified）；總結硬超時 30s（bot.py:1400–1401） | **顯式釘死重試策略**（azure-core 默認 3 次重試≠現狀）：建議連接瞬時錯誤最多 2 次、5s 超時、流中不重試；30s wait_for 保留在調用方 |
| 用量計量（F9） | `enable_usage_metrics`（bot.py:2248/2506）只進事件流/日誌，**零入庫** | Foundry response usage（prompt/completion tokens）→ FC-3 `usage() -> UsageReport`；`usage` 容器是綠野（spec §9.1），本方案只保證取數不落庫 |

---

## 2. 調用面改寫圖（4 構造點 + run_inference ×2 + MODELS dict）

**定位聲明**：FC-3 實現體 = U-102 `providers/llm/azure_openai.py`（在建）；本表定義**每個調用點的模型選擇與行為參數遷移**，
供 U-102/U-103 接線消費，不重複造輪子。FC-5 = `providers/base.py` `RealtimeVoiceEngine`（冷路徑）。

| # | 調用點 (file:line) | 現狀 | FC 映射 | Azure 對應 | 遷移行為參數 |
|---|---|---|---|---|---|
| L1 | `_build_pipeline` bot.py:2371–2387 | `AWSBedrockLLMService` 流式逐 turn；`Settings(model, system_instruction, max_tokens=256)`；凍結憑證 :2381–2387 | **FC-3 `stream()`** | `providers/llm/azure_openai.py`（LLM_PROVIDER 選型） | max_tokens=256；caching 按 §1.4 能力位；重試預算釘死 |
| L2 | `_invoke_summary_bedrock` bot.py:5624–5679 | `llm.run_inference(ctx)` 一次性；max_tokens=512；空轉寫短路 :5641–5649 | **FC-3 one-shot**（stream 收集） | 同上 | max_tokens=512；**解析失敗永不拋**（`_coerce_summary_struct`）語義保留；Bedrock 錯誤向上拋→`summary_status='failed'` 語義保留 |
| L3 | `_invoke_translate_bedrock` bot.py:5741–5787 | 一次性；max_tokens=4096；model 鏈 `caller key → TRANSLATE_MODEL → DEFAULT_MODEL` :5763–5764 | **FC-3 one-shot** | 同上 | max_tokens=4096；**model 解析鏈原樣保留**（key 不變，部署重指）；`{"_error"}`→502 不寫盤語義保留 |
| L4 | `_build_nova_sonic_pipeline` bot.py:2154–2167 | `AWSNovaSonicLLMService` S2S 雙向流 :2160；凍結憑證 :2122 | **FC-5 `RealtimeVoiceEngine`** | Azure Voice Live（`VOICE_LIVE_ENABLED=false`） | 決策 (a)：cutover 後 nova 路徑退役；AWS 回滾保留 |
| R1 | `run_inference`（=L2 helper）+ 4 觸發點（bot.py:1393/:5932/:4090） | 見盤點 §1.2 觸發表 | FC-3 one-shot | 同上 | 30s `asyncio.wait_for` 保留在調用方（後續 Phase D 遷 Service Bus worker，見 §4） |
| R2 | `run_inference`（=L3 helper）+ `POST /api/admin/demos/{id}/translate` bot.py:5798–5815 | 翻譯預覽從不寫盤 | FC-3 one-shot | 同上 | 同 L3 |
| M1 | `MODELS` dict bot.py:469–476 + `DEFAULT_MODEL`/`TRANSLATE_MODEL` :482/:488 | Bedrock inference-profile ID | **配置驅動部署**（`LLM_DEPLOYMENT` 家族） | `models[]` 形狀 `{id,label,bedrock_id}` **保留**（C-1/C-2）；`bedrock_id` **字段名不動**，內容改為 Foundry 部署名/模型名 | 見 §5；6 key 1:1 重指（§1.1/§1.2），不增刪 key |
| M2 | `_is_claude_model_id` bot.py:479–480 | `.anthropic.` 子串 → caching 門控 | 配置側 per-deployment 能力位 | 見 §1.4 | 函數隨 AWS 調用面退役 |

> **label 文案（team-lead 裁決 2026-11-09，已裁決）**：口徑 = **鍵不動、值重指、顯示文案跟隨改名**——`models[].label`
> 隨新部署改名（如 "Nova Lite"→"GPT-4.1 mini"），理由：顯示真實模型名是正確性問題，掛舊名構成誤標缺陷。
> 但屬**用戶可見 UI 文案變化**：§6 該行標 Breaking change（UI 文案）+ 手動驗證點，§7-B9/V11 記錄，**發布前需用戶點頭**——**用戶已點頭 2026-11-09：同意改名**；
> 用戶若要求凍結文案，一行回退（label 恢復原值）即可。

---

## 3. 認證 / 初始化改寫（F4 行為變化點 + F11 RBAC 對照）

### 3.1 憑證生命週期（**行為變化點，必須記錄 + 測試**）

| 維度 | 現狀（F4 實證） | Azure 目標 | 行為差異 / 補償 |
|---|---|---|---|
| 模式 | **凍結憑證**：5 構造點各一次 `get_frozen_credentials()`（bot.py:2122/2347/5658/5767/6392）→ access_key/secret_key/session_token 三元組傳入 service；實例每會話新建、無複用 | **Managed Identity 長駐客戶端**（DefaultAzureCredential/azure-identity） | **差異**：現狀=輪換後新構造實例即拿新憑證、長會話持舊憑證；MI=長駐客戶端 token 緩存自動刷新（過期前 proactive refresh，刷新窗口由 azure-identity 內部策略決定）。**補償**：①文檔記錄刷新語義；②長流式會話（L1 逐 turn 重建 service 實例的語義本來就是 per-turn）→ FC-3 適配器**每次 stream 調用可重建 client 或顯式刷新 token**，對齊「構造即取新憑證」語義；③驗證點見 §7-V4 |
| MCP SigV4（mcp_sigv4.py:54–56/:85–86） | boto3 默認憑證鏈 + botocore SigV4Auth | **決策 (c)**：連接時降級 `{ok:false,error}`，無 Azure 對應認證 | `MissingCredentialsError` 降級路徑保留（:42–46）；botocore 離場 |
| chime 客戶端（bot.py:3348–3356） | 每請求構造，connect 3s/read 5s/retries 2 | Twilio（決策 (d)）：AccountSid/AuthToken → Key Vault | 重試/超時預算遷 Twilio SDK（U-201/207） |

### 3.2 IAM action → Azure RBAC 對照（F11 全量輸入：cloudformation.yaml:131–193）

| AWS action（運行角色） | Azure 對應 | 說明 |
|---|---|---|
| `bedrock:InvokeModel / InvokeModelWithResponseStream / InvokeModelWithBidirectionalStream` | **Cognitive Services OpenAI User**（或 `Cognitive Services User`）綁定 AOAI/Foundry 資源 | 覆蓋 L1–L3；Bidirectional 一條隨 nova 退役 |
| `bedrock-agentcore:InvokeAgentRuntime` | —（MCP sigv4 降級，決策 (c)） | 不遷 |
| `transcribe:StartStreamTranscription(WebSocket)` | **Cognitive Services Speech User**（Speech 資源） | U-101 Azure STT/TTS 共用 |
| `polly:SynthesizeSpeech` | 同上（Speech 資源 TTS） | Polly 路徑本就是死路（G-2），僅 RBAC 覆蓋 |
| `chime:List*/GetPhoneNumber`（只讀 ×3） | —（Twilio 非 Azure RBAC） | Twilio 憑證入 Key Vault；決策 (d) |
| `secretsmanager:GetSecretValue` | **Key Vault Secrets User** | 1:1（F7） |
| `s3:GetObject`（代碼桶） | **AcrPull**（映像拉取） | 運行時無 S3（盤點 S3 行） |
| `dynamodb:*`（分表 CRUD，:175–193） | **Cosmos DB Built-in Data Contributor**（或按容器細分 RBAC） | 5 表→容器見 §4.1 |
| SSM + `CloudWatchAgentServerPolicy` 托管策略（:122–123） | Azure Monitor Agent / App Insights（Container Apps 內建） | 可觀測性綠建（F9/S4） |

---

## 4. pipeline 部件對應（收斂後真實範圍；零引用項只記錄不設計）

### 4.1 DynamoDB 5 表 → Cosmos DB for NoSQL（核心：F2 GSI/Scan 消滅方案）

| 表（盤點 §2.4） | 現狀 PK/GSI/查詢 | Cosmos 分區鍵建議 | 3 條 Scan 路徑的索引化改寫 |
|---|---|---|---|
| users（user_store.py:42–47） | PK `username`；逐 key 點操作 | `/username` | 無 Scan；點讀寫 1:1 |
| demos（demo_store.py:51–59） | PK `id`；**List=Scan+緩存** | 常量分區（如 `/type":"demo"`，小表單分區）或 `/id`+跨分區查詢 | List 從 DynamoDB 全表 Scan → Cosmos 單分區查詢（小表，RU 極低） |
| voices（voice_store.py:47–55） | PK `provider`/SK `voice_id`；seed_if_empty | `/provider` | 無 Scan；按 provider 分組=單分區查詢 |
| activity（activity_store.py:57–68） | PK `day`/SK `ts_id`；TTL attr | `/day` | 逐日 Query 本就是分區查詢 1:1；**TTL 語義差異**：Cosmos TTL=最後修改起算 → 用 `expires_at` + 剩餘 TTL（§9.2，dependency-matrix 行已記） |
| history（HistoryRecorder bot.py:1254–1421） | PK `call_id`；**唯一 GSI `caller-started-index`**（:3640）；item 形狀 :1317–1333 | **`/caller_id`**（匿名呼叫者落哨兵值如 `"_none_"`）；`id=call_id` | 見下三行 |

**history 三條 Scan 路徑（F2）→ 索引化改寫**：

| 路徑（file:line） | 現狀 | Cosmos 改寫 |
|---|---|---|
| caller 過濾列表 bot.py:3613–3652 | GSI Query，`ScanIndexForward=False` 新在前 | **單分區查詢** `WHERE c.caller_id=@caller ORDER BY c.started_at DESC`（對應今日 GSI 語義） |
| 無 caller 列表 bot.py:3654–3676 | **Scan+FilterExpression** | 跨分區查詢 + **複合索引 `(started_at DESC)`** ——索引伺服，非全表掃 |
| CSV 導出 :3828 + 指標 :6679 | 全表 Scan ×2 | 同上複合索引支撐；指標聚合可加 `SUM/COUNT` 查詢替代 Python 端掃描 |
| （附）按 call_id 讀/更新 :1363–1421 | PK 點操作 | `id` 等值跨分區索引查詢（本量級 TTL 30 天可接受）；**如成熱點**再加 call_id→caller_id 索引容器（記錄為未來項，不預建） |

- TTL：history `ttl`（`HISTORY_TTL_DAYS` 30）與 activity `ttl`（90）→ Cosmos `expires_at` + 剩餘 TTL（語義差異見上）。
- Cosmos 其餘等價（考證基準）：GSI→複合索引、400KB→2MB 文檔、5 檔一致性、DynamoDB Streams→Change Feed（本倉零 Streams 引用，不啟用）。

### 4.2 其餘 pipeline 部件

| # | 部件（盤點實證） | Azure 對應 | 方案要點 |
|---|---|---|---|
| P1 | **SecretsManager MinimaxSecret**（cloudformation.yaml:103–108；UserData 解析→.env :434–436） | **Key Vault**（F7 KMS=0，1:1） | `MINIMAX_API_KEY` 改 Key Vault 引用 + MI；啟動期解析語義保留；**Key Vault 無內置自動輪換**——若需輪換用 Function/Logic App 自建（當前僅一密鑰，不預建輪換）；**G-2 已批 2026-11-09**：MiniMax 保留為 TTS 供應商，U-101 以新註冊名建 FC-4 適配器（遺留名 denylist 不變） |
| P2 | **post-call 總結作業** bot.py:1393 `asyncio.create_task` → 30s wait_for :1400（唯一異步作業，F8） | **Service Bus 佇列 + worker**（最小集） | 冪等 post-call job（R-A-010）；30s 硬超時→worker 側；重試+DLQ；**TTL 逐隊列顯式設置**（官方文檔自相矛盾，不靠默認）；語義變化=總結從「掛斷後 30s 內」→「最終一致」，S 面摘要出現時機需在驗證點記錄 |
| P3 | **Chime/voice-server**（F1：12 .ts **零 AWS SDK**，純 SIP/RTP 協議橋） | **整目錄退役**（決策 (d)） | 無 SDK 遷移工作量；`/api/admin/phone-numbers` 改 Twilio 同信封（U-207）；chime_provisioner_handler.py 外部 Lambda 隨 A-2 退役 |
| P4 | **Transcribe**（AWSTranscribeSTTService bot.py:2362–2369 + seam patch :6093–6183 + asr_multilang.py 自研 presigner） | **Azure Speech STT**（U-101 `providers/speech/azure_speech.py`） | 整套 presigner/seam patch 退役；多語言映射 `language["stt"]`→`STT_LOCALE`；連續流+首音早於語句結束（R-A-005）需 Azure 側復驗 |
| P5 | **Polly**（SimplePollyTTSService bot.py:750–824，G-2 死引用 :783） | **Azure Speech TTS** | 不移植（本就不可用）；`POLLY_VOICES` :831–859 → voice registry 加 `azure_speech` 行（C-1） |
| P6 | **批次**：migrate_demos_to_ddb.py（→DDB）、migrate_demo_tools.py（零 AWS）、demo 逐個翻譯（L3） | 遷移腳本 → **Cosmos 數據遷移腳本**（一次性、冪等語義保留）；翻譯=FC-3（§2 R2） | `bot._to_ddb` float→Decimal 對應 Cosmos 數值語義核對；migrate_demo_tools 原樣保留 |
| P7 | **零引用項**（F8 + 盤點實證）：SQS / SNS / EventBridge / Step Functions / AWS Batch / Lambda（倉內）/ KMS / S3 運行時 / CloudWatch SDK / embedding | **不適用/零引用** | 對應考證基準（Event Grid/Service Bus/Durable Functions/Batch/Functions/Key Vault/Blob）僅記錄於此行，**不設計不存在的架構**；Connect Flow Lambda 是倉外件，A-2 已裁（in-repo Twilio 轉接流程） |

---

## 5. 配置 / 環境變數映射表

| 現變量 / 字段 | 讀取點 | Azure 目標（spec §11 契約變量） | 標註 |
|---|---|---|---|
| `AWS_REGION` | bot.py:2121/2346/5657/5766/6391；smoke_*.py | **退役**（區域內嵌於 `LLM_ENDPOINT`/`SPEECH_ENDPOINT`） | us.* profile 約束隨 Bedrock 消亡 |
| `DDB_REGION` | 4 存儲 + bot.py:1432/6679 | 併入 Cosmos 端點配置 | 優先鏈語義廢除 |
| `BEDROCK_MODEL_ID` | **僅** smoke_bedrock.py:15 | **刪除**（smoke 專用死變量，F10） | smoke 重寫為 Foundry 連通腳本 |
| `MODELS[].bedrock_id` | `/api/config`（talkConfig.spec.js:61–62 釘死） | **字段名保留（C-2）**，內容改 Foundry 部署/模型名 | ⚠️ 映射時顯式標註：名不動、值重指 |
| `PHONE_MODEL` | bot.py:2692（CFN :470 寫死） | 保留（值仍為 MODELS key） | key 契約不動 |
| `DEFAULT_MODEL`/`TRANSLATE_MODEL` | bot.py:482/:488（常量） | 保留常量，key 不變 | 值隨部署重指生效 |
| `AWS_ACCESS_KEY_ID`/`SECRET`/`SESSION_TOKEN` | 凍結憑證鏈 | **退役**（MI 無密鑰） | 見 §3.1 行為變化點 |
| `MINIMAX_API_KEY`（SecretsManager→.env） | bot.py:1790 | Key Vault 引用 | P1（G-2 已批 2026-11-09，MiniMax 保留） |
| `AUTH_SECRET` | bot.py:155–161 | 保留（spec §11） | — |
| `LLM_PROVIDER/LLM_ENDPOINT/LLM_DEPLOYMENT/LLM_API_VERSION` | U-003 `config_contract.py`（硬必需） | 新契約變量（已落 Phase A） | D-4 無默認；`LLM_API_VERSION` 是否必需由 U-102 聲明 |
| `STT_PROVIDER/STT_LOCALE/TTS_PROVIDER/SPEECH_ENDPOINT` | U-003 矩陣 | 新契約變量 | U-101 消費 |
| `TELEPHONY_PROVIDER=twilio` + `TWILIO_*` | U-003 條件必需 | 新契約變量 | A-1/U-201 消費 |
| `VOICE_LIVE_ENABLED` | U-003 嚴格 bool（CR-2） | 新契約變量，默認 `false` | FC-5 冷路徑 |
| `config/runtime.json`/`mcp_servers.json` | runtime_config.py（gitignored 單機文件態） | 遷持久存儲/每副本重歸屬 | dependency-matrix env 行 |
| `.env` 加載鏈 | bot.py:105 `load_dotenv`；CFN systemd :535 | Container Apps 環境變量 + Key Vault 引用 | AWS 回滾路徑不動 |

---

## 6. 改動清單（方案側骨架擴全 + #18 回填；「手動驗證點」執行結果留 V1–V11 逐條簽字）

表頭固定：**元素 / 原本用法(file:line) / Azure 對應 / 替代方案與理由 / 手動驗證點 / Breaking change**。
> #18 回填行（2026-11-10）：「元素」欄前綴標籤（X1 / N1–N3 / B10–B12 / 掛賤#n）；行尾旗標【手動驗證】/【Breaking】；偏差錨標註指向 verification-b3-b6.md 偏差表 / u-103-review dev 偏差表。

| 元素 | 原本用法 (file:line) | Azure 對應 | 替代方案與理由 | 手動驗證點 | Breaking change |
|---|---|---|---|---|---|
| MODELS dict | bot.py:469–476（6 個 `us.*` ID） | Foundry **模型名**（§1.1/§1.2 逐 key 重指；構造時經 LLM_ENDPOINT/LLM_DEPLOYMENT 解析到部署） | Claude=原生 on Foundry（保真）；Nova=gpt 檔位映射（無同族） | 各 key 點名回包 | 值變名不變（C-2） |
| L1 流式主路徑 | bot.py:2371–2387 | FC-3 `providers/llm/azure_openai.py` | 調用面收斂單一適配器 | 逐 turn 流式+工具調用實測 | 否 |
| L2 總結 | bot.py:5624–5679（run_inference） | FC-3 one-shot | 同上 | 同 prompt 新舊總結對比 | 否（failed 語義保留） |
| L3 翻譯 | bot.py:5741–5787 | FC-3 one-shot | 同上；TRANSLATE_MODEL 保真理由見 bot.py:483–487 | 4 語言翻譯對比 | 否 |
| L4 S2S | bot.py:2154–2167 | FC-5 Voice Live（`VOICE_LIVE_ENABLED=false`） | 決策 (a)：冷路徑，cutover 退役 nova | Voice Live 啟用前基準測試（G-6） | **是**（引擎集 `engines[]→[pipeline]`，(a) 已裁） |
| caching 門控 | `_is_claude_model_id` bot.py:479–480 | per-deployment 能力位（§1.4） | F5：Nova 拒 cachePoint 的教訓——禁全局開 | Claude 部署 caching 生效驗證 | 否 |
| max_tokens 地圖 | bot.py:2374/5662/5771、smoke_bedrock.py:22 | 原值遷移（F6） | 行為保真 | 翻譯部署輸出上限 ≥4096 | 否 |
| 重試/超時預算 | F3（pipecat vendored :126–127；bot.py:1400） | 顯式釘死重試策略（§1.4） | azure-core 默認≠現狀，不釘會漂移 | 故障注入驗證重試次數 | 否 |
| 凍結憑證 ×5 | bot.py:2122/2347/5658/5767/6392 | MI 長駐客戶端（§3.1） | 無密鑰是安全升級；刷新語義差異記錄 | V4：token 刷新/長會話測試 | **是（行為）**：憑證輪換生效時機 |
| MCP sigv4 | mcp_sigv4.py:54–56/:85–86 | 連接降級 `{ok:false,error}`（決策 (c)） | botocore 離場（R-T-001） | 降級信封形狀測試 | **是（C-5）**：sigv4 MCP 連不上 |
| DynamoDB 5 表 | §2.4 各 store | Cosmos（§4.1 分區鍵設計） | F2：消滅 GSI+3 條 Scan | 分區鍵切換+數據遷移校驗 | **是（數據）**：分區鍵變更需遷移 |
| history GSI/Scan | bot.py:3613–3676/:3828/:6679 | 複合索引+分區查詢（§4.1） | RU 成本防爆 | 三路徑查詢計劃/耗時對比 | 否（API 形狀不動） |
| TTL ×2 | bot.py:1316、activity_store.py:247 | `expires_at`+剩餘 TTL | Cosmos TTL 語義差異（§9.2） | 過期項清理驗證 | 否 |
| post-call 任務 | bot.py:1393 `asyncio.create_task` | Service Bus 隊列+worker（P2） | F8 最小集；進程崩潰即丟是 G-3 | 冪等重投+DLQ 測試 | **是（行為）**：摘要出現時機最終一致 |
| SecretsManager | CFN :103–108 | Key Vault（P1） | F7 KMS=0，1:1 | 啟動解析+無密鑰殘留掃描 | 否 |
| Chime/voice-server | 12 .ts + bot.py:3322–3421 | 整目錄退役 + Twilio（決策 (d)） | F1 零 SDK，無遷移量 | 同信封 `/api/admin/phone-numbers` | **是（C-4）**：號碼視圖數據源 |
| Transcribe 全套 | bot.py:2362–2369/:6093–6183、asr_multilang.py | Azure Speech STT（U-101） | presigner/seam patch 死亡 | 首音早於語句結束（R-A-005） | 否 |
| Polly TTS | bot.py:750–824（G-2 死引用 :783） | Azure Speech TTS | 不移植死代碼 | voices registry 不崩 | 否 |
| IAM 清單 | CFN :131–193 | RBAC 對照（§3.2） | F11 全量 | 最小權限驗證 | 否 |
| 遷移腳本 | scripts/migrate_demos_to_ddb.py | Cosmos 遷移腳本（P6） | 一次性冪等保留 | 8 場景預設逐字節校驗（R-P-003） | 否 |
| smoke 腳本 | smoke_bedrock.py 等 4 | Foundry 連通腳本 | `BEDROCK_MODEL_ID` 刪除 | 連通冒煙 | 否（工具） |
| 測試 mock ×12 檔 | §1.7 清單 | 重指向 FC-3/存儲契約測試 | monkeypatch 面=調用面，隨 §2 改 | RETAIN CI gate 綠 | 否 |
| `models[].bedrock_id`（掛賤#2） | talkConfig.spec.js:61–62 | 字段名保留（C-2）；值=Foundry 模型名重指（§1.1/§1.2） | SPA 契約釘死（釘字段名不釘值）；名不動值重指 | SPA 測試不紅 | 否（名不動值重指） |
| `models[].label` 文案 | `/api/config` label 顯示字段（如 "Nova Lite"） | 隨新部署改名（"GPT-4.1 mini" 等） | **裁決 1（2026-11-09）**：顯示真實模型名是正確性問題，掛舊名構成誤標缺陷 | **UI 文案驗證 + 用戶簽字（發布前）** | **是（UI 文案）**：用戶已簽 2026-11-09 同意改名（V11 發布前 UI 核對仍做）；凍結回退=一行恢復原 label |

**#18 回填行（2026-11-10，fullstack-dev）：**

| 元素 | 原本用法 (file:line) | Azure 對應 | 替代方案與理由 | 手動驗證點 | Breaking change |
|---|---|---|---|---|---|
| 修紅：HistoryRecorder API 面 + 讀回側重指 | tests/test_history.py finalize×2（原 :140/:191，C4×4 預存成員）；moto/DynamoDB 讀回 | `finalize()` → `flush_turns_and_summarize` 薄封裝（修法①，API 面名保留）；讀回 FakeCosmos（U-303 同型）+ CosmosCallRepository | 銷預存債 #2/#3；P0 斷言（`"model": bot.DEFAULT_MODEL`，A-4 釘）逐字節不動；斷言形狀逐字保留（ttl 曝光=刪除點 epoch，§1.3-5 語義 1:1） | —（測試面，CI 綠即驗證）【verification 偏差 7 錨】 | 否（API 面名保留） |
| B-5① 啟動期密钥解析→api_key 注入缝 | `_build_tts` 散讀 `os.environ["MINIMAX_API_KEY"]` 等（原 bot.py:1828）；級聯引擎構造直讀 env | `resolve_secret`/`resolve_api_keys`（env secretRef 主通道→§3.1 KV 名 MI 直讀兜底）→ `build_cascade_engine` speech/llm/minimax api_key 參數縫 | CR-C1 接管點=構造注入；密钥只駐內存、不落盤/不入 config；不造 §11 新變量 | 【手動驗證】啟動解析（部署期）；test_startup_secrets 6/6 契約級 | 否（內部縫，對外面不動） |
| KV 直讀兜底條件注 | azure-identity / azure-keyvault-secrets 未裝（lazy import 降級 warning） | 兜底通道=附加通道（主通道 §3.2:136 secretRef env 注入+啟動解析） | **條件（偏差 4 裁定）**：釘版與否歸 U-401 requirements 決議——若 U-401 不引該依賴，兜底通道同步裁撤、不留死代碼 | —（注記）【verification 偏差 4 錨→U-401】 | 否（注記） |
| B10 5 表名 env 變量退役 | USERS_TABLE/DEMOS_TABLE/VOICES_TABLE/ACTIVITY_TABLE/HISTORY_TABLE（user/demo/voice/activity_store.py + bot.py 消費點；13 測試檔 monkeypatch 隨行） | COSMOS_ENDPOINT/COSMOS_DATABASE + §1.1 容器常量（USERS/DEMOS/VOICES/ACTIVITY/CALLS…，storage/cosmos.py） | BC-c1 純退役、不造 COSMOS_*_TABLE 新名；HISTORY_DISABLED=唯一存留開關；test_config_contract:231 "USERS_TABLE"=防朽釘（舊變量不被讀取）保留 | 【手動驗證】V5（連線/遷移校驗）【verification 偏差 6 錨：防朽釘】 | **【Breaking·配置】**（運維可見，§7.2-B10 錄案） |
| B12 AUTH_SECRET 兜底退役 | bot.py:161–166（原）未設即 `token_urlsafe(48)` 隨機生成+告警 | `resolve_secret("AUTH_SECRET","AUTHSECRET")`：env secretRef 主通道 + Key Vault `AUTHSECRET`（§3.1 凍結名）；缺值 **fail-fast** | 多副本會話一致性優先（隨機 fallback 靜默裂會話）；tests/conftest.py setdefault 測試縫 | 【手動驗證】V4 系（啟動解析/輪換） | **【Breaking·行為/部署】**（§7.2-B12 錄案） |
| B11 MiniMax 註冊名改名 | TTS provider 註冊名 `minimax`（遺留 denylist 永拒） | 新註冊名 `minimax_tts`（TTS_PROVIDER 選型值隨之）；voice-store `provider` 數據鍵保留 `minimax` 不遷移不重灌；SPA 顯示留 "MiniMax" | 數據鍵≠註冊名（不觸 denylist）；零可見變化口徑（裁決 2026-11-09）；若 SPA/fixture 釘死註冊名被迫遷數據鍵→才升級 UI 變化走 V11 | 【手動驗證】V11 類（僅當升級 UI 變化） | 否（零可見變化口徑） |
| X1 history→`calls` 折疊遷移形狀 | history 獨立表（HISTORY_TABLE）+ DynamoDB item 形狀 | 折入 `calls` 容器（§1.1 8 容器，`/caller_id` 分區）；item shape merge（summary/outcome/ttl 保留、外部 API 形狀不變） | X1 折疊語義 per §1.1；遷移腳本 doc 面隨行更新（migrate_demos_to_ddb.py→§1.1 容器語義）【verification 偏差 5 錨】；遷移執行歸 U-404（D-8 門控） | 【手動驗證】V5（分區鍵切換+遷移完整性） | **【Breaking·數據】**（分區鍵/形狀合併） |
| 掛賤#9 「_none_」匿名哨兵數據形狀 | calls `/caller_id` 分區匿名哨兵 `_none_`（X1 家族，pipeline-infra-spec §4.1） | 哨兵落存儲行數據形狀（READ/WRITE 哨兵語義）註記：匿名 caller 落行可查、查詢側按 caller_id 過濾缺席時查 `_none_` | 數據形狀決定（X1 家族→#18 註冊，接受） | —（注記，數據形狀隨 V5 遷移校驗覆蓋） | 否（注記） |
| 掛賤#3 A-4 裁定(a) 落紙 | history/attach-meta 行 `model` 欄（測試期望 P0） | **`model` 欄=選擇 key**（MODELS dict key / DEFAULT_MODEL），非部署值 | 裁定落紙為正式規範行：fixture key 語義釘 `bot.DEFAULT_MODEL`（test_history.py:100–101/:160–161，P0 逐字節） | —（P0 契約釘） | 否（語義落紙） |
| 掛賤#4 attach-meta model=key 語義 | bot.py attach_meta 寫點 `"model": model`（web :6000 / phone :6914；U-103 時點 :6079/:7111） | 預存行為：runtime 寫入即**選擇 key**（非 MODELS 值）；A-4(a) 釘定 | 語義=`model` 欄記錄選型 key（DEFAULT_MODEL/PHONE_MODEL 鍵），部署值由 MODELS[key] 解析；fixture 隨 key 語義 | —（行為註記，P0 覆蓋） | 否（預存行為） |
| 掛賤#5 R-C-003 多副本 config 寫入分叉 | 每副本重歸屬縫（RUNTIME_CFG 路徑覆寫，bot.py:2900 附近） | 多副本下 config 寫入會分叉 | G-3 單副本驗證配置內可接受；U-401 部署面承接（持久卷或集中配置，部署可將路徑指向持久卷）；**部署面責任非本批代碼責任** | 【手動驗證】U-401 部署面清單 | 否（部署面注記） |
| 掛賤#6 錯誤分類學行為註記（Option A） | DDB `ConditionalCheckFailedException`（create 衝突拋錯） | Cosmos `ContainerConflict`→store.create 內歸一 `ValueError` | **409 外部語義保留**、改名誠實（錯誤類名跟後端）；外部 API 面不變 | —（行為註記；契約測試綠） | 否（外部語義保留） |
| 掛賤#7 VoiceStore.delete 幂等 | DDB 刪缺表/缺行拋錯 | Cosmos 刪缺容器/缺行**吞成冪等成功** | 幂等 delete=合理語義；缺容器/缺行拋→成功為 corner 行為變化（實務不可見） | —（行為註記） | 否（行為變化註記） |
| 掛賤#8 指標聚合 10k 帽 | §1.2 原案：Cosmos server-side 聚合 | 實現=Python 聚合器 + **10k 帽**（數據保真上限） | spec-debt 註記（§1.2 原案與實現有偏）；cap-hit 必須可觀測（log/warn）；未來可遷 Cosmos 聚合 | 【手動驗證】cap-hit 可觀測（驗證期數據量不觸帽） | 否（實現偏差註記） |
| 掛賤#10 ADAPT 對賬 12=11+1 | 測試文件處置清單 ADAPT 名義 12 名 | 12 名=11 原案 + 1（CR-3 `test_tool_filler` RETAIN→ADAPT 重分類） | 對賬誠實：moto→Cosmos 契約重指 + test_tool_filler fixture 修（Phase B exit 前）；test_mcp_integration 零改動=裁定接受（§8 G/H 盤點 moto/store 觸點；其 AWS 觸點=sigv4 憑據鏈非存儲面） | —（對賬註記） | 否（對賬） |
| 掛賤#11 V2 回退規則文面 | V2 跨模型行為對比（§7.1）：nova-2-lite→gpt-4.1-mini=最大行為跳變 | 記錄性回退規則成文：**不過→退 gpt-5-mini 再議**（裁決 2） | 規則文面=本行（原散記 #16 收口報告）；面覆蓋 DEFAULT_MODEL+PHONE_MODEL 全流量 | 【手動驗證】V2（三任務 × 4 驗收語言） | 否（規則文面） |
| 掛賤#12 mapping §6「部署名」措辭更正 | 本表 MODELS dict 行原作「Foundry 部署名」 | 更正=「Foundry **模型名**」：value=模型名（§1.1/§1.2 字串），構造時經 LLM_ENDPOINT/LLM_DEPLOYMENT 解析到部署（§11 契約變量） | 裁決 A-2 口徑（「部署名/模型名」二選一→採模型名）；原文已就地更正，本行即更正記錄 | —（措辭更正） | 否（措辭） |
| 掛賤#13 talkConfig fixture 樣式註記 | talkConfig.spec.js:59–62 models fixture（label==id 舊樣式自足測試數據） | 與新 label 值表（§7.2-B9）表層不一致但**零紅**、非用戶可見 | fixture=純 helper 測試自足數據；SPA 按計劃零改動；fixture 跟真=前端面活（範圍外），如需另派 | —（注記） | 否（非用戶可見） |
| 掛賤#14 F-13 錄案 | 400 detail 風格不一：`invalid engine: {v}` vs `unknown engine: {v!r} (allowed: […])` | 錄案不修（非阻塞建議） | 隨 U-201/後續清理落，不擴修復環面（u-103-review 跟踪項 4） | —（注記，延後清理） | 否（注記） |
| 掛賤#15 `_validate_segment` 預存邊角 | bot.py `_validate_segment` engine/lang 交叉校驗（存量 engine=nova-sonic 遺留數據跳過 cross-check） | 預存休眠邊角、非引擎集 ①-④ 引入；不立新卡 | Azure 路徑下 stored engine=nova-sonic 僅存 AWS 回滾語義/舊數據；cross-check skip=遺留 config 校驗缺口 | 【手動驗證】U-501 cutover 清單（legacy data validation gap） | 否（預存邊角） |
| 掛賤#16 u-103-report §四.1 全局一致性掃尾 | u-103-report.md §四.1（裁決 B 前快照） | 已提前兌現為裁決 B 快照（u-103-report.md:54 親驗）；#18 全局一致性掃尾=本行完成記錄 | u-103-review dev 偏差 3 同責（§三 計數一致性 10→11/4→5 已隨行） | —（文檔掃尾） | 否（文檔） |
| N1 HISTORY 開關運維可見翻轉 | 舊語義「HISTORY_TABLE 空⇒禁用」（discovery:156） | 默認**啟用**；唯一關閉通道 `HISTORY_DISABLED=1`（discovery:256 keep-only-HISTORY_DISABLED） | 隨 B10 表名 env 退役消亡之語義翻轉（opt-in→opt-out）；C-9/postcall 管線以 calls 行為前提，默認禁用會使目標態管線靜默失效；**U-501 runbook 文檔化「HISTORY_DISABLED=1 為唯一關閉通道」**；test_history_enabled_by_default 重指隨行（verification 偏差 3） | 【手動驗證】U-501 runbook 確認 | **【Breaking·行為】**（運維可見）【verification 偏差 2/3 錨】 |
| N2 C4 債務簿記 4→2 + 口徑重定 | C4 預存紅簿記（verification-u-103 R2.1:153–154 在案：test_history finalize×2 亦為 C4×4 成員，baseline 即同簽名紅） | 簿記更正 **4→2**（修法①銷預存債 #2/#3）；「B-3 打破 2 測/2 新紅」為 team-lead 誤判——**勘誤以本行為證** | #19 口徑：紅恰= test_demo_store::test_put_get_list_roundtrip_with_tool_ids_and_decimal + test_migrate_demos::test_migrate_writes_all_fields_matching_loader（字段漂移簽名逐字節），**越界紅=阻塞** | —（#19 驗收口徑）【verification 偏差 1 錨】 | 否（簿記） |
| N3 I-4 delivery_count 雙約定註 | postcall worker 失敗腿終態判定（workers/postcall_worker.py） | `delivery_count >= maxDeliveryCount-1` 穩健實現 | 計數 0-based/1-based 雙約定下純 `==5` 會**永卡 pending**、違硬不變式（重試耗盡必落 failed 終態——I-4 裁決 2026-11-09）；V6 leg 2 釘 spec 字面值 maxDeliveryCount=5 | —（行為註記；test_postcall_worker 覆蓋） | 否（實現註） |
| U-103 修復環 dev 偏差 ①–④ 处置 | tests/test_cascade_engine.py +1 函數 / test_tool_filler:243 構造計數 / u-103-report §三 / F-11 測試腿 | ①+1 函數=F-11 回收測試落點（FakeSTT fixture 唯一家園，等價釘落點，既有測試零動）②「:243/:286 兩處構造」更正=一處（:286=同因失敗面）③§三 計數一致性快照 10→11/4→5 ④F-11 測試釘 stopped==1、可重試腿歸 QA 探針 e24（分工明示，零空斷言） | 四項均裁接受（u-103-review dev 偏差表）；本行為 §6 錨 | —（測試面註記）【u-103-review dev 偏差 ①–④ 錨】 | 否（測試面） |
| 測試面機會性簡化（未做） | tests/test_engine_languages.py:243–244 monkeypatch.setenv + _isolate_runtime 雙保險 | 既裁 opportunistic 簡化**未做**（錯過 B-3 窗口，避免額外擾動） | 保持原狀（功能等價）；如需簡化=一個 hunk，另批 | —（注記）【verification 偏差 8 錨】 | 否（未做） |

---

## 7. 手動驗證點 & Breaking change 總表

### 7.1 手動驗證點（施工後逐條簽字）

| # | 驗證點 | 對應 |
|---|---|---|
| V1 | **Claude hosted-on-Azure 特性核對**：prompt caching（cachePoint 等價物）/ 工具流 / 流式 / extended thinking 逐特性實測；缺者切部署版本 1（hosted-on-Anthropic）或退 gpt 檔 | §1.1 [UNVERIFIED]、F5 |
| V2 | **跨模型行為對比**：同 prompt 新舊輸出（對話 256/總結 512/翻譯 4096 三任務 × 4 驗收語言）；**重點用例：nova-2-lite→gpt-4.1-mini = 最大行為跳變（DEFAULT_MODEL+PHONE_MODEL 全流量）**——不過則回退 gpt-5-mini 再議（裁決 2） | §1.2、F6 |
| V3 | **Claude 計費核對**：CCU 計量（100 CCU=$1，Marketplace/MACC） | §1.1 |
| V4 | **F4 憑證行為變化**：MI token 刷新時機 vs 凍結憑證輪換語義；長流式會話跨過期點 | §3.1 |
| V5 | **Cosmos 分區鍵切換**：history `/caller_id` 設計 + 3 條 Scan 路徑改寫的 RU/耗時對比；數據遷移完整性 | §4.1 |
| V6 | **Service Bus TTL 顯式設置**（逐隊列，不依賴默認）+ 冪等重投 + DLQ | P2 |
| V7 | **Voice Live 語音質量**：基準測試 + 錄製 endpoint/model/voice/billing 後才可啟用（G-6） | §1.2 |
| V8 | **Azure STT 流式語義**：首音早於語句結束、多語言 locale 映射、code-switching 不自動（§7.1） | P4 |
| V9 | **摘要時機變化**：掛斷後摘要從「30s 內」→ 最終一致，S 面驗收口徑確認 | P2 |
| V10 | **[UNVERIFIED] 逐條現場核實**：①cachePoint 等價物 hosted-on-Azure 支持度 ②hosted-on-Azure vs on-Anthropic 逐特性差 ③Cohere embed-v-4-0 規格（零引用僅記錄）④Sora-2 狀態（零引用僅記錄）⑤pipecat 1.4.0 Azure service 簽名（G-1，U-101）⑥1.4.0 流式重試實現（vendored 1.1.x 僅參考）⑦gpt 系 max_completion_tokens 參數名 | §1.4 |
| V11 | **label 文案 UI 驗證 + 用戶簽字（發布前）**：改名後 SPA 顯示逐一核對；用戶若要求凍結文案，一行回退（裁決 1） | §2/§6、B9 |

### 7.2 Breaking change 總表

| # | 變化 | 性質 | 已有裁決/補償 |
|---|---|---|---|
| B1 | 引擎集 `engines[]→[pipeline]`、8 個 Nova-only locale 退出電話默認 | 產品可見（C-3） | **決策 (a) FINAL**，用戶已接受 |
| B2 | `models[]` 值重指（3 Nova→gpt 檔、3 Claude→Foundry 原生） | 契約內（C-2 字段名保留） | §2 非阻斷確認項（label 文案） |
| B3 | sigv4 MCP 連接降級為 `{ok:false,error}` | 產品可見（C-5） | **決策 (c) FINAL** |
| B4 | `/api/admin/phone-numbers` 數據源 Chime→Twilio（信封不變） | 產品可見（C-4） | **決策 (d) FINAL** |
| B5 | 憑證輪換生效時機（凍結憑證→MI 刷新） | 行為 | §3.1 補償 + V4 |
| B6 | 歷史/活動數據分區鍵切換 | 數據遷移 | §4.1 設計 + V5 |
| B7 | 摘要可用時機最終一致化；30s 總結超時從「failed 終態」→暫時性失敗可重試（**重試耗盡/最後一次投遞必落 failed 終態——I-4 裁決 2026-11-09**，DLQ 只留殘餘 poison + runbook 清掃）；re-summarize 同步→異步受理（**C-9 裁決 2026-11-09**） | 行為 | P2 + V9 + pipeline-infra-spec §2.2 + product-impact §7（2026-11-09 補） |
| B8 | `BEDROCK_MODEL_ID` 刪除、AWS_* 全退役 | 配置 | §5；smoke/CI 隨改 |
| B9 | `models[].label` 顯示文案跟隨新部署改名（"Nova Lite"→"GPT-4.1 mini"） | UI 文案（用戶可見） | **裁決 1（2026-11-09）**：顯示真實模型名是正確性問題；發布前用戶簽字（V11）→ **已簽 2026-11-09 同意改名**；凍結回退=一行恢復原 label |
| B10 | 5 個 DynamoDB 表名 env 變量退役（USERS_TABLE/DEMOS_TABLE/VOICES_TABLE/ACTIVITY_TABLE/HISTORY_TABLE）→ §11 `COSMOS_*` 契約變量 | 配置（運維可見） | pipeline-infra-spec §8.2-BC-c1（2026-11-09 錄案）；#18/README 同步 |
| B11 | MiniMax 註冊名 `minimax`（遺留 denylist 永拒）→ 新註冊名 `minimax_tts`（TTS_PROVIDER 選型值隨之） | 配置/內部（裁決後用戶/API 可見面不變） | **裁決 2026-11-09（零可見變化口徑）**：voice-store `provider` 數據鍵保留 `minimax` 不遷移不重灌（數據鍵≠註冊名，不觸 denylist）；SPA 顯示字串保留 "MiniMax"（顯示映射承載=真實供應商名，同 B9 口徑）。若 SPA/fixture 釘死註冊名被迫遷數據鍵，才升級 UI 變化走 V11 |
| B12 | AUTH_SECRET 值入 Key Vault（Container Apps 引用注入 env，參數名不動）；ephemeral fallback（bot.py:155–161 未設即隨機生成）於 Azure 路徑退役→缺值 fail-fast | 行為（部署） | **裁決 2026-11-09**：多副本會話一致性優先（隨機 fallback 靜默裂會話）；併 B8 口徑，V4 系驗證 |

---

*Contract freeze: FC-0..FC-8 不動，變更走 CR（dependency-matrix「Contract rulings」）。本檔與盤點矛盾處以盤點 file:line 為準。*
