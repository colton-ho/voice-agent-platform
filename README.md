# Voice Bot Platform

一个基于 Pipecat 的实时语音对话系统，同时支持**网页**和**电话呼入（PSTN）**两种入口，可在浏览器
里以"监听者"身份实时观察任意通话的 ASR / LLM / TTS 事件流。

> **架构方向**：LLM 走 Azure AI Foundry，STT/TTS 走 Azure Speech + MiniMax，
> 部署走 Azure Container Apps（Bicep）。AWS 路径（Bedrock / Transcribe / Polly / CFN）
> 已冻结，仅供回滚。

## 功能一览

- **多语言**：粤语 / 普通话 / 英文 / 日文
- **多 LLM**（Azure AI Foundry）：GPT-4.1 mini / GPT-5 mini / GPT-4.1 nano / Claude Haiku 4.5 / Claude Sonnet 4.5 / Claude Sonnet 4.6
- **多 TTS**：MiniMax（多种音色，含粤语）+ Azure Speech
- **级联引擎**：`VOICE_ENGINE=cascade` → Azure Speech STT + Azure AI Foundry LLM + TTS，phrase-level streaming
- **场景预设**：默认 / 销售客服 / IT 支持 / 模拟面试 / 语言陪练 / 设备技术支持（KB grounded）
- **总结**：按需对当前会话生成多语言总结（Markdown 渲染）
- **电话呼入**：Twilio Media Streams（主） + Chime SDK Voice Connector → SIP/RTP（冻结）
- **Web Monitor**：浏览器以只读模式订阅任意活跃通话的事件流

## 架构

```
┌─────────────┐     WSS /ws (16 kHz PCM)        ┌──────────────────┐
│  浏览器 Web │ ─────────────────────────────► │                  │
└─────────────┘                                  │                  │
                                                 │   bot.py         │
┌─────────────┐  Twilio Media Streams            │   (Pipecat)      │
│   Twilio    │ ─────────► POST /twilio/incoming  │                  │
│   (PSTN)    │           WSS /twilio/media-stream│  /ws  /phone/ws  │
└─────────────┘                                  │  /monitor/ws     │
                                                 │  /api/...        │
┌─────────────┐  SIP+RTP ┌────────────────┐      │                  │
│ Chime Voice │ ───────► │  voice-server  │ ───► │ Pipeline:        │
│  Connector  │ (冻结)   │   Node SIP UAS │      │ STT + LLM + TTS  │
└─────────────┘          └────────────────┘      │                  │
                                                 │                  │
┌─────────────┐  WSS /monitor/ws (events only)   │                  │
│ Web Monitor │ ◄─────────────────────────────── │                  │
└─────────────┘                                  └──────────────────┘
                                                          │
                                            ┌─────────────┼──────────────┐
                                            ▼             ▼              ▼
                                      Azure AI       Azure Speech     MiniMax
                                      Foundry (LLM)  (STT + TTS)      (TTS)
                                            │
                                      ┌─────┴──────┐
                                      │ Cosmos DB   │ (活动/通话/转录/用量)
                                      │ Service Bus │ (postcall 异步队列)
                                      │ Blob Storage│ (approved artifacts)
                                      │ Key Vault   │ (secret 管理)
                                      └────────────┘
```

## 目录结构

```
.
├── bot.py                      主服务（FastAPI + Pipecat）
├── cascade_engine.py           级联引擎（STT→LLM→TTS phrase-level streaming）
├── config_contract.py          §11 配置契约（30 项冻结占位符，启动时验证）
├── providers/                  Provider 注册表（显式注册、零默认、零回退）
│   ├── registry.py             ProviderKind: TELEPHONY|STT|TTS|LLM|REALTIME
│   ├── base.py                 契约 ABC（SpeechRecognizer / LanguageModel / SpeechSynthesizer）
│   ├── llm/azure_openai.py     Azure AI Foundry LLM 适配器
│   └── speech/
│       ├── azure_speech.py     Azure Speech STT + TTS
│       └── minimax_tts.py      MiniMax TTS（HTTP streaming）
├── storage/                    持久化层
│   ├── cosmos.py               Cosmos DB 客户端
│   ├── cosmos_calls.py         通话/转录/活动 CRUD
│   └── servicebus.py           Service Bus 投递（postcall 异步任务）
├── workers/
│   └── postcall_worker.py      通话后异步处理（从 Service Bus 消费）
├── tools/
│   └── registry.py             MCP 工具注册表
├── infra/bicep/                Azure 基础设施（Bicep）
│   ├── main.bicep              订阅级编排（RG + 10 模块）
│   ├── modules/                acr / blob / containerApps / identities / keyvault /
│   │                           monitor / network / servicebus / cosmos / csRoleAssignments
│   └── params/                 dev.bicepparam / staging.bicepparam / prod.bicepparam
├── scripts/deploy/
│   ├── deploy-azure.sh         一键部署（默认 what-if，--apply 才落资源）
│   └── set-secrets.sh          Key Vault secret 写入
├── deploy/                     AWS 部署（已冻结）
│   ├── deploy.sh               CFN 一键部署脚本
│   ├── cloudformation.yaml     CFN 模板（EC2 + CloudFront + IAM）
│   └── README.md               部署细节
├── voice-server/               Node SIP/RTP 桥（Chime 路径，冻结）
├── static/index.html           Web UI（Talk + Monitor 双模式）
├── data/                       KB 场景示例文档
├── config/                     运行时配置（runtime.json）
├── tests/                      测试套件
└── pipecat/                    Pipecat 上游 vendor 副本（仅参考）
```

## 配置契约（§11）

配置由 `config_contract.py` 在启动时验证，共 30 项冻结占位符。**零默认值**
（`RECORDING_ENABLED` / `VOICE_LIVE_ENABLED` 除外，默认 `false`）。

### Provider 选择

| 环境变量 | 示例值 | 说明 |
| --- | --- | --- |
| `TELEPHONY_PROVIDER` | `twilio` | 电话接入提供商 |
| `VOICE_ENGINE` | `cascade` | `cascade` = STT→LLM→TTS 三段式；其他值走 FC-5 实时引擎 |
| `STT_PROVIDER` | `azure_speech` | 语音识别提供商 |
| `TTS_PROVIDER` | `azure_speech` | 语音合成提供商（也可选 `minimax`） |
| `LLM_PROVIDER` | `azure_openai` | LLM 适配器名称 |

### LLM / Speech

| 环境变量 | 说明 |
| --- | --- |
| `LLM_ENDPOINT` | Azure AI Foundry 端点 URL |
| `LLM_DEPLOYMENT` | 部署名（`gpt-4.1-mini` / `gpt-5-mini` / `gpt-4.1-nano` / `claude-*`） |
| `SPEECH_REGION` | Azure Speech 区域 |
| `STT_LOCALE` | 识别语言（如 `zh-HK` / `en-US`） |
| `TTS_VOICE` | 合成音色 ID |

### 数据与基础设施

| 环境变量 | 说明 |
| --- | --- |
| `AZURE_LOCATION` | Azure 区域（如 `eastus`） |
| `COSMOS_ENDPOINT` | Cosmos DB 端点 |
| `COSMOS_DATABASE` | Cosmos 数据库名 |
| `BLOB_ACCOUNT_URL` | Blob Storage 帐户 URL |
| `SERVICEBUS_NAMESPACE` | Service Bus 命名空间（短名） |
| `KEY_VAULT_URL` | Key Vault URL（secret 经平台级 `secretRef` 注入） |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights 连接串 |

### 运行限制

| 环境变量 | 说明 |
| --- | --- |
| `MAX_CONCURRENT_CALLS_PER_REPLICA` | 每副本并发上限（待压测确定） |
| `MAX_CALL_DURATION_SECONDS` | 最大通话时长（秒） |
| `TRANSCRIPT_RETENTION_DAYS` | 转录保留天数 |
| `RECORDING_ENABLED` | 录音开关（默认 `false`） |
| `VOICE_LIVE_ENABLED` | Voice Live 实时引擎开关（默认 `false`，冷路径） |

### Secret（4 项，经环境变量注入，不落盘）

| 环境变量 | Key Vault Secret 名 |
| --- | --- |
| `MINIMAX_API_KEY` | `MINIMAX-API-KEY` |
| `TWILIO_AUTH_TOKEN` | `TWILIO-AUTH-TOKEN` |
| `TWILIO_ACCOUNT_SID` | `TWILIO-ACCOUNT-SID` |
| `AUTH_SECRET` | `AUTHSECRET` |

## LLM 模型（6，pipeline 模式）

| UI key | Azure AI Foundry 部署名 | 定位 |
| --- | --- | --- |
| `nova-2-lite` | `gpt-4.1-mini` | 默认对话模型（低延迟高吞吐） |
| `nova-lite` | `gpt-5-mini` | 中档备选 |
| `nova-micro` | `gpt-4.1-nano` | 最快/最省 |
| `claude-haiku-4.5` | `claude-haiku-4-5` | 轻量 Claude |
| `claude-sonnet-4.5` | `claude-sonnet-4-5` | 翻译专用（`TRANSLATE_MODEL`） |
| `claude-sonnet-4.6` | `claude-sonnet-4-6` | 1M 上下文 |

> **注意**：UI 中的 key 名（`nova-*` / `claude-*`）为向后兼容保留，实际调用
> Foundry 部署名。`config_contract.py` 的 `LLM_DEPLOYMENT` 变量指定部署。

## TTS Provider（pipeline 模式）

- **MiniMax**：海外版账户的全部系统音色（按语言筛选展示），默认 TTS
- **Azure Speech**：`azure_speech` 注册名，经 Provider Registry 选择

> AWS Polly / Nova Sonic 已从可选列表中移除，仅作为 AWS 冻结路径的代码保留。

## 电话呼入接入

### Twilio Media Streams（主路径）

```
PSTN → Twilio → POST /twilio/incoming-call (TwiML) → WSS /twilio/media-stream → bot
```

Twilio 号码 Voice 配置：**A CALL COMES IN** → Webhook (HTTP POST) → `https://<域名>/twilio/incoming-call`

所需环境变量：`TWILIO_AUTH_TOKEN`、`TWILIO_PUBLIC_BASE_URL`（必需），`TWILIO_ACCOUNT_SID`（可选）。
未配齐时 Twilio 端点优雅禁用（503 / 拒接），对现有链路零影响。

### Chime SDK Voice Connector（冻结）

```
PSTN → Chime VC → SIP/RTP → voice-server(Node) → WS /phone/ws → bot
```

Chime VC 配置需在 AWS 控制台手工设置一次（Origination 路由 + 号码绑定）。

## 本地运行（开发模式）

```bash
# 1. 准备 Python 3.12 + Node 20
python3.12 -m venv .venv
.venv/bin/pip install --upgrade pip wheel
.venv/bin/pip install -r requirements.txt

# 2. 写 .env（参见上方配置契约）
cat > .env <<EOF
TELEPHONY_PROVIDER=twilio
VOICE_ENGINE=cascade
STT_PROVIDER=azure_speech
TTS_PROVIDER=minimax
LLM_PROVIDER=azure_openai
LLM_ENDPOINT=https://<your-foundry>.openai.azure.com/
LLM_DEPLOYMENT=gpt-4.1-mini
SPEECH_REGION=eastus
STT_LOCALE=zh-HK
TTS_VOICE=<voice-id>
MINIMAX_API_KEY=sk-...
AUTH_SECRET=<random-32+>
# ... 其余 §11 变量见配置契约
EOF

# 3. 启动 bot.py
.venv/bin/python bot.py --host 0.0.0.0 --port 7860

# 4. （可选）启动电话桥（Chime 路径，冻结）
cd voice-server
npm install
npx tsc
PUBLIC_IP=<EC2_PUBLIC_IP> RTP_PORT_BASE=10000 RTP_PORT_COUNT=1000 \
  PIPECAT_WS_URL=ws://127.0.0.1:7860/phone/ws \
  node dist/server.js
```

打开浏览器访问 `http://<host>:7860/`。如果设置了 `SITE_PASSWORD`，会弹出 Basic Auth 提示。

## 云端部署

### Azure（主路径）

使用 Bicep + Azure Container Apps。详见 [`docs/azure/u-401-deployment-runbook.md`](docs/azure/u-401-deployment-runbook.md)。

```bash
# 预演（默认，不落资源）
scripts/deploy/deploy-azure.sh dev

# 实际部署（需先回填 REPLACE_ME 参数 + 设置 secret 环境变量）
MINIMAX_API_KEY=sk-... TWILIO_AUTH_TOKEN=... TWILIO_ACCOUNT_SID=... AUTH_SECRET=... \
  scripts/deploy/deploy-azure.sh dev --apply
```

部署资源拓扑：RG + Cosmos DB + Service Bus + Key Vault + ACR + Container Apps
（runtime / worker）+ Blob Storage + Log Analytics + App Insights + 3 告警。

Secret 经 Container Apps 平台级 Key Vault `secretRef` 注入，不落模板字面量。
详见 [`docs/azure/u-401-deployment-plan.md`](docs/azure/u-401-deployment-plan.md)。

### AWS（冻结）

```bash
cd deploy
MINIMAX_API_KEY=sk-... SITE_PASSWORD=your-pwd ./deploy.sh
```

部署完成后输出 `https://*.cloudfront.net/` 地址。栈名默认 `voicebot`，Region `us-east-1`。

首次部署后打开 CloudFront 地址会跳转到 `/setup` 设置管理员账号（详见
[`docs/first-run-setup.md`](docs/first-run-setup.md)）。

## API 参考

| 路由 | 类型 | 用途 |
| --- | --- | --- |
| `GET /` | HTTP | 返回 `static/index.html`（Web UI） |
| `GET /api/config` | HTTP | UI 启动时拉取的所有可选项 |
| `POST /api/summary` | HTTP | 对一次会话历史做 LLM 总结 |
| `GET /api/calls` | HTTP | 当前活跃通话列表（Monitor UI 用） |
| `WS  /ws` | WebSocket | Web 客户端音频通道（16 kHz PCM） |
| `WS  /phone/ws` | WebSocket | voice-server → bot 电话音频通道（Chime） |
| `POST /twilio/incoming-call` | HTTP | Twilio 语音 webhook（返回 TwiML） |
| `WS  /twilio/media-stream` | WebSocket | Twilio Media Streams 音频通道 |
| `WS  /monitor/ws?call_id=...` | WebSocket | 只读事件订阅（受 SitePassword 保护） |

`WS /ws` query params：`engine`、`lang`、`model`、`provider`、`voice`、`minimax_model`、`scenario`、`system`、`greeting`

## 选项清单

### 语言（4）

`zh-HK`（粤语）、`zh-CN`（普通话）、`en-US`（英语）、`ja-JP`（日语）。

### 场景

| key | 来源 | 说明 |
| --- | --- | --- |
| `default` | SCENARIOS | 友善通用助手 |
| `sales` | SCENARIOS | 销售客服 |
| `it-support` | SCENARIOS | IT 支持工程师 |
| `interviewer` | SCENARIOS | 模拟面试官 |
| `language-tutor` | SCENARIOS | 语言陪练 |
| `acme-security-support` | KB_SCENARIOS | 安防设备技术支持 Tina |

KB 场景把文档以"用户先发一条 message"的形式注入会话上下文，而不是塞进 system prompt。

## 监控（Web Monitor）

1. 打开 Web UI，顶部切到 **Monitor** 模式
2. 下拉里出现所有活跃 `call_id` + 主叫号码
3. 选一通通话即可看到 ASR partial / final、LLM token 流、TTS 起止、说话状态
4. 监听者只接收事件，不接收音频

## Provider Registry

`providers/registry.py` 实现显式注册、显式选择、零默认、零回退：

- `ProviderKind`：`TELEPHONY` | `STT` | `TTS` | `LLM` | `REALTIME`
- `VOICE_ENGINE=cascade` 返回 `(stt, llm, tts)` 三元组
- 选择变量缺失 → `MissingSelectionError`（指名变量）
- 未注册名 → `UnsupportedProviderError`（列出已注册名单）
- AWS 时代遗留名永不注册、永不解析

## 常见问题

- **MiniMax TTS 没声音 / 401**：检查 `MINIMAX_GROUP_ID` 是否被设为空字符串（应不写）。
- **启动报 `MissingSelectionError`**：§11 契约变量缺失。对照「配置契约」逐项补齐。
- **启动报 `UnsupportedProviderError`**：provider 名拼写错误或未注册。已注册名见报错信息。
- **Twilio 端点返回 503**：`TWILIO_AUTH_TOKEN` / `TWILIO_PUBLIC_BASE_URL` 未配齐。
- **电话打不断**：voice-server 的 RTP 出栈队列在 `user_speaking=true` 时 mute，等 LLM/TTS 事件解 mute。

## 已知限制

- 仅支持 Inbound（呼入），不做 Outbound
- 通话内容持久化依赖 Cosmos DB（活动/通话/转录/用量）
- 没有 IVR 菜单 / 主叫鉴权
- Nova Sonic / Voice Live 为冷路径（`VOICE_LIVE_ENABLED=false`）
- AWS 路径（Bedrock / Transcribe / Polly / CFN / Chime）已冻结，仅供回滚
