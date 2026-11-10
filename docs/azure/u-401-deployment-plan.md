# U-401 · Cloud Platform Deployment Plan（部署前準備）

Author: team-lead · Date: 2026-11-10 · Status: **施工前設計（doc-first，鐵律：本階段只備齊部署包，不執行雲上 provisioning）**
Scope: implementation-plan:422 U-401「Bicep modules + environments」；closes R-A-017, R-S-001/002/009, R-O-003。
Contract basis: `docs/Azure_Voicebot_Implementation_Instructions_Corrected.md` §8.3/§10/§11/§12 + `docs/azure/pipeline-infra-spec.md` §1–§5（衝突解法以 spec + mapping 裁決為準）。

**邊界（pipeline-infra-spec §4.2 在案）：寫 Bicep/腳本 ≠ 授權部署。`az deployment`/`az ... create` 執行前必須取得用戶明示授權；本階段驗證止於 `az bicep build`（本地編譯）+ 測試基線。**

---

## 1. 目標拓撲（per environment：dev / staging / prod）

Resource group `rg-voicebot-<env>`，單區域（`AZURE_LOCATION`，cost-analysis §3 以 East US 為計價錨）：

| 資源 | 模組 | 說明 |
|---|---|---|
| Cosmos DB 帳戶 + `voicebot` 庫 + 8 容器 | `modules/cosmos.bicep`（**已建**） | §1.1/§1.2/§1.3/§1.4 全落定；CI-1 複合索引；Session；Continuous 備份 |
| Service Bus `sb-voicebot-<env>` + `postcall-jobs` 隊列 | `modules/servicebus.bicep`（**已建**） | §2.1 六值顯式；DLQ 內建 |
| Key Vault + 4 secrets | `modules/keyvault.bicep`（**已建**） | §3.1 清單；RBAC 啟用 |
| User-assigned MI ×3（mi-runtime/mi-control/mi-worker）+ role assignments | `modules/identities.bicep`（新建） | §5 十行 RBAC 矩陣 |
| ACR | `modules/acr.bicep`（新建） | 映像倉庫（AcrPull→MI） |
| Container Apps env + control/runtime/worker 三 app | `modules/containerApps.bicep`（新建） | §8.3 部署要求；§11 環境契約注入 |
| Blob 帳戶（私有，公訪禁用） | `modules/blob.bicep`（新建） | R-A-008；approved artifacts 最小面 |
| Log Analytics + App Insights + alerts/budgets/action groups | `modules/monitor.bicep`（新建） | `APPLICATIONINSIGHTS_CONNECTION_STRING` 來源 |
| DNS/TLS/私有端點（按拓撲） | `modules/network.bicep`（新建，收窄見 §7） | §12 適配條目 |
| Redis | **不建** | D-9 未批（§6 P7 清單） |
| Azure AI Foundry（AOAI 帳戶 + 模型部署）+ Speech 資源 | **文檔化手動前置**（本檔 §5） | pipeline-infra-spec §4.1 末行：商業審批/部署手動前置（G-2），不入 Bicep |

編排入口：`infra/bicep/main.bicep` + `infra/bicep/params/{dev,staging,prod}.bicepparam`。
`deploy/cloudformation.yaml`、`deploy/deploy.sh` 全凍結不動（AWS 回滾路徑，CR-1）；Azure 部署腳本落新文件 `scripts/deploy/`。

## 2. 命名方案（infra 面；§11 契約面不動）

| 資源 | 命名 | 備註 |
|---|---|---|
| 資源組 | `rg-voicebot-<env>` | env ∈ {dev,staging,prod} |
| Cosmos 帳戶 | `cosmos-voicebot-<env>` | 輸出 `COSMOS_ENDPOINT` |
| Service Bus | `sb-voicebot-<env>` | §2.1 釘名 |
| Key Vault | `kv-voicebot-<env>` | 輸出 `KEY_VAULT_URL` |
| ACR | `acrvinebot<env>`（去連字符，ACR 限制） | |
| Container Apps env | `cae-voicebot-<env>` | 關聯 Log Analytics |
| 三 app | `ca-voicebot-<env>-control` / `-runtime` / `-worker` | |
| MI | `mi-voicebot-<env>-runtime` / `-control` / `-worker` | |
| Blob | `stvoicebot<env>`（去連字符） | 私有容器 `artifacts` |
| App Insights | `appi-voicebot-<env>` | |
| Log Analytics | `log-voicebot-<env>` | |

> 注（CR note，契约文本不动）：`SERVICEBUS_NAMESPACE` 工作约定=**短名** `sb-voicebot-<env>`（Bicep 输出 `namespace.name`；`storage/servicebus.py` 拼接 `.servicebus.windows.net` 组成 FQDN）。冻结契约 `_MEANINGS` 文本写 "fully qualified"，实际有效约定=短名——差异在此备案。

## 3. 身份與 RBAC（pipeline-infra-spec §5 十行，落 identities.bicep）

主體三 MI（mi-deploy 屬 U-402 GH OIDC，本階段不建）：

| # | Scope | Role | Principal | 對應 |
|---|---|---|---|---|
| 1 | Foundry/AOAI | Cognitive Services OpenAI User | runtime, worker, control | bedrock:InvokeModel（F11） |
| 2 | Speech | Cognitive Services Speech User | runtime | STT/TTS 通話面 |
| 3 | Key Vault | Key Vault Secrets User | runtime, control, worker | §3.2 secretRef |
| 4 | Cosmos 帳戶 | Cosmos DB Built-in Data Contributor | runtime, control, worker | 分表 CRUD |
| 5 | SB `postcall-jobs` | Azure Service Bus Data Sender | runtime, control | 投遞/re-summarize |
| 6 | SB `postcall-jobs` | Azure Service Bus Data Receiver | worker | 消費 |
| 7 | ACR | AcrPull | 三 MI | 映像拉取 |
| 8 | Blob | Storage Blob Data Contributor | runtime | approved artifacts |
| 9 | Twilio/MCP | 無 Azure RBAC | — | 憑證入 Vault；sigv4 決策 (c) 降級 |
| 10 | 資源組 | Contributor | mi-deploy | **U-402，本階段不建** |

Foundry/Speech 資源由用戶手動建立（§5），role assignment 模組以參數接收其資源 ID（手動資源建成後填參數執行補派）。

## 4. Secret 流（§3.1/§3.2；deviation ④ 本階段裁決）

**主通道（§3.2）**：Key Vault → Container Apps `configuration.secrets[].keyVaultRef`（MI 引用）→ `env[].secretRef` 注入。secret 不落 env 文件、不落模板字面量；keyvault.bicep 的值只經 `@secure()` 部署參數。

**裁決（deviation ④「KV 兜底接受帶條件（azure-keyvault-secrets 鑄版歸 U-401，不引則裁撤兜底）」——本階段執行裁撤支）**：
`requirements.txt` 未釘 `azure-keyvault-secrets`（CR-C2 原則：只釘有真實運行時消費者的依賴）。U-401 目標拓撲=ACA 平台級 Key Vault 引用，完全覆蓋 §3.2 主通道，Python 側 KV 直讀兜底無運行時消費者。**裁撤 `bot.py resolve_secret/_kv_secret` 的 lazy-import KV 直讀兜底**，保留 env-only 解析；`test_startup_secrets.py` 隨行；mapping §6 補一行。反向選項（釘 `azure-keyvault-secrets==4.x` 保留兜底）留檔備查，如需回轉是小 revert。

Env 注入清單（app 運行時面）：
- §11 三十占位符（`config_contract.py` 凍結；`COSMOS_DATABASE=voicebot`）
- secret 面：`MINIMAX_API_KEY`（←KV `MINIMAX-API-KEY`）、`AUTH_SECRET`（←KV `AUTHSECRET`，B12 fail-fast）、`TWILIO_AUTH_TOKEN`（←KV `TWILIO-AUTH-TOKEN`；`TWILIO_AUTH_TOKEN_SECRET_NAME=TWILIO-AUTH-TOKEN`）
- 普通面：`TWILIO_ACCOUNT_SID`（X2：KV 存值、env 注入）、`MINIMAX_GROUP_ID`、`MINIMAX_BASE_URL`（默認值已在代碼）、`HISTORY_DISABLED`（B10 後唯一開關，默認 0=啟用）、`ACTIVITY_LOG_TTL_DAYS`（§9.2）

## 5. 手動前置（G-2 商業審批；本階段只文檔化）

1. **Azure AI Foundry / AOAI 帳戶 + 模型部署**：按 `bot.py:516` MODELS 註冊表建部署——`gpt-4.1-mini`、`gpt-5-mini`、`gpt-4.1-nano`、`claude-haiku-4-5`、`claude-sonnet-4-5`、`claude-sonnet-4-6`（A-1 裁定：MODELS 值=Foundry 模型名；模型名→部署解析歸 FC-3 適配器 `LLM_*` 契約變量）。**Claude 系列經 Foundry Marketplace/Anthropic 協議，須 G-2 商業審批**。
2. **Azure Speech 資源**（`SPEECH_REGION`；STT/TTS）。
3. **Twilio 側**：AccountSid/AuthToken 入 Vault（X2）；webhook/媒体 URL 指向 `PUBLIC_CONTROL_BASE_URL`/`PUBLIC_MEDIA_BASE_URL`。
4. MiniMax key（外部服務）入 Vault。

## 6. 環境參數（dev / staging / prod）

| 項 | dev | staging/prod | 來源 |
|---|---|---|---|
| Cosmos 吞吐 | database shared 400 RU/s | calls 族 autoscale 400–4000（max=4000，10% 地板=400；勘误：原写「1000–4000」——地板不可达，Cosmos autoscale 实际带宽=max 的 10%–100%）+ 配置 4 容器 shared 400 | §1.4 |
| Cosmos 備份 | Continuous7Days | Continuous30Days | §1.4 |
| 一致性 | Session | Session | §1.4（Strong 不採） |
| runtime 副本 | min 1（warm，§8.3） | min 1–2 + max 參數化 | §8.3 |
| worker 副本 | min 1 | min 1（單隊列競爭消費） | §2.2 |
| control 副本 | min 0–1 | 按流量 | — |
| 預算/告警 | 低成本門檻 | cost-analysis §8 陷阱項對應告警 | monitor.bicep |

## 7. 決策記錄（本階段裁定）

| # | 決策 | 理由 |
|---|---|---|
| D1 | **KV 直讀兜底裁撤**（§4） | deviation ④ 預註冊支；CR-C2 最小依賴面；ACA 平台引用已覆蓋 |
| D2 | **network.bicep 收窄**：ACA 內建 ingress + 托管 TLS + 自定義域名參數化；私有端點/VNet 不預建，掛 V11 前檢查項 | §12「optional private endpoints appropriate to the selected topology」；當前拓撲=Twilio 公網 WS 必須保留公網端點（§10），私網化非本階段必需；防過度設計（§6 原則） |
| D3 | **control/runtime 進程面**：當前 `bot.py` 單 FastAPI 進程同時承 control+media（bot.py:2850）；三 app 按 §12 建齊，control/runtime 共用映像、命令參數化，`includeControlApp` 參數控制啟用；進程拆分屬代碼側後續項（§8.2 owner-lease 機制在案），**不屬 U-401** | spec §12 要求三 app + 現狀誠實映射 |
| D4 | **R-C-003 持久卷**：Container Apps 掛 Azure Files 卷到 `config/`（`MCP_CFG_PATH_OVERRIDE`/`RUNTIME_CFG_PATH_OVERRIDE` 縫對應），多副本共享；**多副本配置寫入分叉**=運維約束（配置寫入走單副本/低頻人工，管理台寫入後重啟生效），不建分布式鎖 | R-C-003「持久卷留 U-401 部署面」承接；分叉備案=本條運維約束，runbook 記錄 |
| D5 | **budgets/action groups**：monitor.bicep 建 alert 規則 + action group（郵件接收者參數化）；subscription 級 budget 可選參數（部署層級差異） | §12 項齊；預算歸屬訂閱層，避免模組越權 |

## 8. 驗證挂鉤（部署執行後的驗收口徑，本階段不執行）

- **V5**：8 容器 RU/耗時四路徑對比 + `/id` 複合索引支持度前置檢查（§1.2 [UNVERIFIED] 項）
- **V6**：`az servicebus queue show` 六值逐項核對 + 過期→DLQ 實測 + 重複投遞冪等
- **V1–V11**：product-impact 驗證點表；部署後逐項執行
- 測試基線不變：**1037 passed / 2 failed（C4×2 預存字段漂移，字節穩定）/ 1039 collected**——任何越界紅=阻塞

## 9. 部署序列（授權後執行；scripts/deploy/ 封裝）

1. `az login` + 訂閱選擇（preflight 檢查）
2. `az deployment sub what-if`（參數文件逐環境）→ 用戶目視確認
3. Foundry/Speech 手動前置（§5）完成
4. `az deployment sub create`（RG + 全模組）
5. Secrets 灌入（KV 寫入腳本，值不落盤）
6. RBAC 補派（Foundry/Speech 資源 ID 填參後）
7. 映像推送 ACR + app 部署
8. V5/V6 + V1–V11 驗收
9. 回滾路徑：RG 級刪除重建（無狀態數據面）；數據回滾走 U-404 導出（§9.3-7）

*Contract freeze: FC-0..FC-8 不動；§11 契約變量名不新造。與既有文檔衝突處以 spec + mapping 裁決為準。*
