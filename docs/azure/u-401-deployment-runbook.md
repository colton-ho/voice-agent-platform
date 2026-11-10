# U-401 · Azure 部署 Runbook（运维操作手册）

Author: fullstack-dev-2 · Date: 2026-11-10 · Status: **部署包交付物（随 U-401 代码评审）**
Scope: `docs/azure/u-401-deployment-plan.md` §5/§8/§9 的操作面展开；供授权后执行部署的运维人员使用。
配套交付：`infra/bicep/**`（模板与参数）、`scripts/deploy/deploy-azure.sh`、`scripts/deploy/set-secrets.sh`。

> **边界（plan 头铁律 / pipeline-infra-spec §4.2）**：本 runbook 描述"如何部署"，不构成部署授权。
> 执行 `az deployment ... create` / `az ... create` 前必须取得**用户明示授权**；授权前的验证止于
> `az bicep build`（本地编译）+ 测试基线。CI/CD 自动部署属 U-402（GH OIDC），本手册是人工路径。

---

## 1. 前置条件（plan §5 手动前置，G-2 商业审批）

### 1.1 手动创建的外部资源（不在 Bicep 内，部署前完成）

| # | 事项 | 产出物（回填到 params） | 备注 |
|---|---|---|---|
| 1 | **Azure AI Foundry / AOAI 帳戶 + 模型部署**：按 `bot.py` MODELS 注册表建部署——`gpt-4.1-mini`、`gpt-5-mini`、`gpt-4.1-nano`、`claude-haiku-4-5`、`claude-sonnet-4-5`、`claude-sonnet-4-6` | `LLM_ENDPOINT`（帐户端点）、`LLM_DEPLOYMENT`（部署名） | A-1 裁定：MODELS 值=Foundry 模型名，模型名→部署解析归 FC-3 适配器。**Claude 系列经 Foundry Marketplace/Anthropic 协议，须 G-2 商业审批** |
| 2 | **Azure Speech 资源**（STT/TTS） | `SPEECH_REGION`、`STT_LOCALE`、`TTS_VOICE` | |
| 3 | **Twilio**：AccountSid/AuthToken 准备好（X2 裁决：两者都入 Vault）；webhook/媒体 URL 指向 `PUBLIC_CONTROL_BASE_URL`/`PUBLIC_MEDIA_BASE_URL` | secret 环境变量（§1.3） | URL 回填见 §2 步骤 4/6 两遍流程 |
| 4 | **MiniMax key**（外部服务） | secret 环境变量 | |

### 1.2 参数回填（`infra/bicep/params/<env>.bicepparam`）

所有 `REPLACE_ME` 占位符在 `--apply` 前必须回填（脚本拦截；`PUBLIC_*` 除外，见 §2 两遍流程）：

| 参数 | 来源 | 说明 |
|---|---|---|
| `LLM_ENDPOINT` / `LLM_DEPLOYMENT` | §1.1 第 1 项 | Foundry 端点 + 部署名 |
| `SPEECH_REGION` / `STT_LOCALE` / `TTS_VOICE` | §1.1 第 2 项 | |
| `actionGroupEmail` | 运维告警邮箱 | monitor 告警接收者 |
| `containerImage` | ACR 推送后的映像 | `acrvinebot<env>.azurecr.io/voicebot:<tag>` |
| `PUBLIC_CONTROL_BASE_URL` / `PUBLIC_MEDIA_BASE_URL` | **两遍回填**（§2 步骤 4/6） | 首布取 `RUNTIME_FQDN` 输出后回填 |
| `MAX_CONCURRENT_CALLS_PER_REPLICA=3` / `MAX_CALL_DURATION_SECONDS=3600` / `TRANSCRIPT_RETENTION_DAYS=30` | 暂定值 | spec §11 占位符（`<load-tested-value>` 等）——压测/审批后回填，暂定值不阻塞首布 |
| `foundryResourceId` / `speechResourceId` | §1.1 建成后 | RBAC 补派（§2 步骤 7），空=跳过 |
| `budgetAmount`（0=不建）/ `budgetStartDate` / `budgetContactEmails` | 预算启用时 | D5 可选项 |
| `controlHostName` / `mediaHostName` | 自定义域名启用时 | D2 三步舞（§2 步骤 8） |

### 1.3 Secret 环境变量（§3.1 四值；值不落盘、不进 argv、不回显）

```bash
export MINIMAX_API_KEY=...      # → KV MINIMAX-API-KEY
export TWILIO_AUTH_TOKEN=...    # → KV TWILIO-AUTH-TOKEN（TWILIO_AUTH_TOKEN_SECRET_NAME 指向此名）
export TWILIO_ACCOUNT_SID=...   # → KV TWILIO-ACCOUNT-SID（X2：也入 Vault，env 注入值）
export AUTH_SECRET=...          # → KV AUTHSECRET（B12：缺值 fail-fast）
```

⚠️ **请勿以 `bash -x` 运行部署/灌密脚本**；`az keyvault secret set --value` 会短暂出现在本机进程表（ps），在可信操作机上执行。

### 1.4 工具与权限

- `az` CLI + Bicep（`az bicep install`）；`az login` 后 `az account set --subscription <id>` 选定目标订阅。
- 部署主体权限：目标订阅下建 RG + 资源（Contributor 级，或等价自定义）；角色赋值（RBAC 行 1–8）需 `Role Based Access Control Administrator` 或用户协助。
- Foundry/Speech 补派（§2 步骤 7）若跨资源组，部署主体需对目标 RG 有 `Microsoft.Authorization/roleAssignments/write`。

---

## 2. 部署序列（plan §9 九步；`scripts/deploy/` 封装）

> 每一步都是幂等的：参数回填后可重复 `--apply`（增量更新），不会重复建资源。

### 步骤 1 · 登录与订阅选择（preflight）

```bash
az login && az account set --subscription <id>
az account show        # 确认订阅
az bicep version       # 确认 bicep 可用
```
`deploy-azure.sh` 内置同样 preflight，缺失即拒绝执行。

### 步骤 2 · what-if 预演（默认行为，不落资源）

```bash
scripts/deploy/deploy-azure.sh <dev|staging|prod>
```
脚本回显部署计划（RG/资源清单）并执行 `az deployment sub what-if`。**逐项目视确认变更无误**。

### 步骤 3 · 手动前置完成确认

§1.1 四项完成；§1.2 中 `LLM_*`/`SPEECH_*`/`STT_*`/`TTS_*`/`actionGroupEmail`/`containerImage` 已回填（`--apply` 会拦截剩余 `REPLACE_ME`）。

### 步骤 4 · 首次部署（RG + 全模块）

```bash
# §1.3 四个 secret 环境变量已 export
scripts/deploy/deploy-azure.sh <env> --apply
```
- 创建：RG、Cosmos（8 容器）、Service Bus、Key Vault（4 secret 名占位）、MI×3 + RBAC、ACR、
  Container Apps 环境 + app、Blob（私有）、Log Analytics + App Insights + 告警、（可选）证书/预算。
- `PUBLIC_*_BASE_URL` 此时仍可为占位符（脚本只警告）——首布后**必须**做两遍回填（步骤 6）。
- 完成后脚本打印部署输出：**记下 `RUNTIME_FQDN`**（步骤 6 用）。

### 步骤 5 · Secrets 灌入（plan §9 步骤 5）

```bash
scripts/deploy/set-secrets.sh <env>
```
把 §1.3 四值写入 `kv-voicebot-<env>`；值不回显、不落盘。ACA 经 `keyVaultUrl` 引用自动取新值
（无版本钉死，≤30 分钟刷新并重启副本）。灌入后重启三个 app 使 env 注入立即生效（可选，不重启则等自动刷新）。

### 步骤 6 · PUBLIC_* 两遍回填（**必做**，否则 Twilio webhook/媒体无有效指向）

1. 用步骤 4 输出的 `RUNTIME_FQDN` 回填参数文件：
   ```
   param PUBLIC_CONTROL_BASE_URL = 'https://<RUNTIME_FQDN>'
   param PUBLIC_MEDIA_BASE_URL   = 'wss://<RUNTIME_FQDN>'
   ```
2. 重跑 `scripts/deploy/deploy-azure.sh <env> --apply`（幂等更新 env）。
3. Twilio 侧 webhook/媒体 URL 同步指向这两个地址（§1.1 第 3 项）。
4. 若启用了自定义域名（步骤 8），换成自定义域名后再重跑一次。

### 步骤 7 · RBAC 补派（plan §9 步骤 6；§5 行 1/2）

Foundry/Speech 资源 ID 填入 `foundryResourceId`/`speechResourceId` → 重跑 `--apply`。
`identities.bicep` 条件模块补派 Cognitive Services OpenAI User / Speech User（空=跳过，非阻塞）。

### 步骤 8 · 自定义域名（可选，D2 三步舞）

network.bicep 只建托管证书 + 域名绑定（无 VNet/私有端点——D2 收窄，私网化挂 V11 前检查项）。证书签发与绑定存在先有鸡还是先有蛋，按序：

1. **首次部署必须将 `controlHostName`/`mediaHostName` 留空**（默认 FQDN 先行，步骤 6 已可跑通）——模板不强制 network→containerApps 的部署顺序，首布带域名会因证书未签发而卡死。
2. **DNS**：为 control/media 域名建 CNAME 指向 `RUNTIME_FQDN`（域名控制验证 `domainControlValidation=CNAME`）。
3. 回填 `controlHostName`/`mediaHostName`，**首绑那一次**在 params 文件显式置 `customDomainBindingType='Disabled'`（证书签发验证期 SNI 不能先开）→ 重跑 `--apply`：network.bicep 签发 `managedCertificates` 并绑定 → 托管证书就绪后把 **params 文件里的** `customDomainBindingType` 改成 `'SniEnabled'`（**持久化写入 params**——防止后续 `--apply` 沿用 `'Disabled'` 把在线域名绑定翻回、打断 TLS）→ 再重跑一次 `--apply`（**两跳**）。
4. `PUBLIC_*` 换自定义域名 → 再重跑一次（步骤 6 第 4 点）。

### 步骤 9 · 映像推送 + app 上线（plan §9 步骤 7）

```bash
az acr build --registry acrvinebot<env> --image voicebot:<tag> .   # 或 az acr login + docker push
```
`containerImage` 回填 `acrvinebot<env>.azurecr.io/voicebot:<tag>` → 重跑 `--apply`。
- control 与 runtime **共用同一映像**（D3：`bot.py` 单进程承 control+media；`includeControlApp=false`，
  进程拆分属代码侧后续项，不属 U-401）。
- worker 入口：`python workers/postcall_worker.py`（`workerCommand`）。
- runtime/worker `minReplicas=1`（warm，§8.3）；AcrPull 已授三 MI（§5 行 7），推送后 app 自动拉取。

### 步骤 10 · 验收（§3）→ 通过后上线；异常走 §4 回滚。

---

## 3. 验证挂勾（plan §8；部署后执行）

### 3.1 契约输出核对

部署输出五值（§11 契约面供给）逐项非空且格式正确：
`COSMOS_ENDPOINT` / `KEY_VAULT_URL` / `SERVICEBUS_NAMESPACE` / `BLOB_ACCOUNT_URL` / `APPLICATIONINSIGHTS_CONNECTION_STRING`。

> `SERVICEBUS_NAMESPACE` 工作约定=**短名** `sb-voicebot-<env>`（Bicep 输出 `namespace.name`；`storage/servicebus.py` 拼接 `.servicebus.windows.net` 组成 FQDN）。冻结契约 `_MEANINGS` 文本写 "fully qualified"，实际约定是短名（CR note，契约文本不动）。

### 3.2 V6 · Service Bus 六值核对 + DLQ 实测

```bash
az servicebus queue show --namespace-name sb-voicebot-<env> --resource-group rg-voicebot-<env> \
  --name postcall-jobs \
  --query "{ttl:defaultMessageTimeToLive, deadLetterOnExpire:deadLetteringOnMessageExpiration, lock:lockDuration, maxDelivery:maxDeliveryCount, dupDetect:requiresDuplicateDetection}" -o json
```
逐项对 §2.1：`P7D` / `true` / `PT1M` / `5` / `false`；DLQ（`postcall-jobs/$DeadLetterQueue`）存在。
再实测：过期消息进 DLQ + 重复投递幂等（R-A-010，`job_id` 去重）。
> 注：ARM 属性名 `deadLetteringOnMessageExpiration` / `requiresDuplicateDetection` 对应 spec §2.1 表述
> `enableDeadLetteringOnMessageExpiration` / `enableDuplicateDetection`（语义同值；命名修复见 U-401 交付说明）。

### 3.3 V5 · Cosmos 分区键切换

8 容器 RU/耗时四路径对比（caller 列表 / 全局列表 / CSV 导出 / 指标聚合）+ `/id` 复合索引支持度前置检查（§1.2 [UNVERIFIED] 项）。

> 注：calls 族 autoscale 口径（400–4000 RU/s，max=4000、10% 地板=400；原「1000–4000」系笔误勘正）见 `u-401-deployment-plan.md` §6 表「Cosmos 吞吐」行；V5 实测后调参同此。

### 3.4 V1–V11 + 基线

- V1–V11 按 `docs/azure/product-impact.md` 验证点表逐项执行。
- 测试基线口径：**`pytest -q tests/`** → **1037 passed / 2 failed（C4×2 预存字段漂移，字节稳定）/ 1039 collected**——任何越界红=阻塞。
  ⚠️ 勿用裸 `pytest` 从仓库根收集：vendored `pipecat/scripts/krisp/test_krisp_viva_filter_audiofile.py` import 期直接退出（缺 soundfile），会 INTERNALERROR——与被测代码无关。

---

## 4. 回滚（plan §9 步骤 9）

| 层 | 路径 |
|---|---|
| **基础设施** | RG 级删除重建（无状态数据面）：`az group delete --name rg-voicebot-<env>` → 按 §2 重部。注意 §5 的配置卷数据随 RG 删除，先导出备份 |
| **数据** | 数据回滚走 **U-404** 导出/还原工具（§9.3-7）；Cosmos 连续备份（dev 7 天 / staging·prod 30 天）支持时间点还原（point-in-time restore） |
| **映像/发布** | `containerImage` 回填上一 tag → 重跑 `--apply`（蓝绿媒体端点发布属 U-405） |
| **AWS 回退** | `deploy/cloudformation.yaml`、`deploy/deploy.sh` 全程冻结未动（CR-1），回退路径可用 |

---

## 5. D4 配置卷单写约束（R-C-003，运维约束）

Container Apps 挂 **Azure Files 卷**到配置目录（`configMountPath=/app/config`；`MCP_CFG_PATH_OVERRIDE`/`RUNTIME_CFG_PATH_OVERRIDE` 缝对应），多副本共享同一份 `config/`（runtime.json、mcp_servers.json）。

**约束（不建分布式锁——D4 裁决）**：多副本并发写配置会产生分叉。运维规则：

1. **配置写入走单副本/低频人工**：写配置时先把写入目标收敛到单副本（或暂停其余副本），写完后重启 app 使全副本重读。
2. **管理台写入后重启生效**：写入方=管理台（control 面）时同样遵守单写；写完重启 runtime/worker。
3. 文件 share（`configstorage` / `config-vol`，`accessMode=ReadWrite`）配额 1 GiB——配置类小文件，勿存数据。

---

## 6. Secret 手动轮换（pipeline-infra-spec §3.2；**无自动轮换**）

KV 无内置自动轮换；仅 3–4 个 secret，**不建轮换 Function/Logic App**（防过度设计）。轮换即手动：

```bash
# 1) 外部侧先换新值（Twilio 控制台换 AuthToken / MiniMax 控制台换 key / 生成新 AUTH_SECRET）
# 2) 灌入 KV（新值经环境变量，不落盘）：
export TWILIO_AUTH_TOKEN=<新值>   # 换哪个 export 哪个
scripts/deploy/set-secrets.sh <env>
# 3) ACA 自动取新值（≤30 分钟刷新并重启副本）；急用时手动重启三个 app
# 4) 验证：webhook 验签 / TTS 调用 / JWT 签发各打一发冒烟
```

- `AUTHSECRET` 轮换会使已签发 JWT 失效（B12 fail-fast 语义）——低峰执行，换后用户需重新登录。
- Twilio token 轮换本质是 Twilio 控制台操作；MiniMax key 同理。
- 细化流程与演练记录归 **U-406** operator runbook（Phase E）。

---

## 7. 后续指针

| 事项 | 归属 |
|---|---|
| CI/CD 流水线（GH OIDC、mi-deploy、§5 行 10 RBAC、§12 八步） | U-402 |
| 蓝绿媒体端点发布 / forced-drain | U-405 |
| 运维 runbook（DLQ 清扫、secret 轮换演练、broker/storage 故障、Twilio 故障回退、drain/rollback 演练记录） | U-406 |
| 验收矩阵执行 + `acceptance.md`（真实通话套件、四语言 A-3、负载 D-6、cutover 测试处置） | U-501 |
| 数据迁移/导出（回滚数据路径） | U-404 |
| Telemetry 仪表 / OTel 接线 | U-403 |
| 私有端点/VNet（D2 递延项） | V11 前检查项 |

---

*Contract freeze: FC-0..FC-8 不动；§11 契约变量名不新造。与既有文档冲突处以 pipeline-infra-spec + mapping 裁决为准。*
