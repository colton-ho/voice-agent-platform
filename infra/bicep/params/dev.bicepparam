// U-401 dev 环境参数（plan §6 表：dev 形态）。
// REPLACE_ME 标记的值 apply 前必须替换（deploy-azure.sh --apply 会拦截未替换项）。
// 「暂定」数值 spec §11 本身亦为占位符（<load-tested-value> 等）——待压测/审批回填。
// secrets 从环境变量读取（值不落盘/不入模板字面量，plan §4）。

using '../main.bicep'

param environmentName = 'dev'
param AZURE_LOCATION = 'eastus'

// —— §11 契约面 ——
// 首布后回填 app 默认 FQDN（部署输出 RUNTIME_FQDN）或自定义域名；runbook 有两步说明
param PUBLIC_CONTROL_BASE_URL = 'https://REPLACE_ME'
param PUBLIC_MEDIA_BASE_URL = 'wss://REPLACE_ME'
param TELEPHONY_PROVIDER = 'twilio'
param VOICE_ENGINE = 'cascade'
param STT_PROVIDER = 'azure_speech'
param TTS_PROVIDER = 'azure_speech'
param LLM_PROVIDER = 'azure_openai'
// plan §5 手动前置产出（Foundry/AOAI 帐户端点 + 部署名，A-1 MODELS）
param LLM_ENDPOINT = 'https://REPLACE_ME.openai.azure.com/'
param LLM_DEPLOYMENT = 'REPLACE_ME'
// plan §5 手动前置产出（Speech 资源区域）
param SPEECH_REGION = 'REPLACE_ME'
param STT_LOCALE = 'REPLACE_ME'
param TTS_VOICE = 'REPLACE_ME'
param MAX_CONCURRENT_CALLS_PER_REPLICA = 3 // 暂定：待压测
param MAX_CALL_DURATION_SECONDS = 3600 // 暂定：待批
param TRANSCRIPT_RETENTION_DAYS = 30 // 对齐 §1.3 通话 TTL 默认 30 天
param RECORDING_ENABLED = 'false' // A10
param VOICE_LIVE_ENABLED = 'false'

// —— secrets（§3.1；env → @secure() 参数）——
param minimaxApiKey = readEnvironmentVariable('MINIMAX_API_KEY', 'UNSET-SECRET-DO-NOT-USE')
param twilioAuthToken = readEnvironmentVariable('TWILIO_AUTH_TOKEN', 'UNSET-SECRET-DO-NOT-USE')
param twilioAccountSid = readEnvironmentVariable('TWILIO_ACCOUNT_SID', 'UNSET-SECRET-DO-NOT-USE')
param authSecretValue = readEnvironmentVariable('AUTH_SECRET', 'UNSET-SECRET-DO-NOT-USE')

// —— 手动前置资源 ID（plan §5 建成后回填，补派 §5 #1/#2；空=跳过）——
param foundryResourceId = ''
param speechResourceId = ''

// —— 容器面 ——
param containerImage = 'acrvinebotdev.azurecr.io/voicebot:REPLACE_ME_TAG'
param includeControlApp = false // D3：control 现与 runtime 同进程
param runtimeMinReplicas = 1 // §8.3 warm
param runtimeMaxReplicas = 3
param controlMinReplicas = 0 // plan §6：0–1
param controlMaxReplicas = 2
param workerMinReplicas = 1 // §2.2 单队列竞争消费
param workerMaxReplicas = 2

// —— Cosmos 容量（§1.4 dev 形态：database shared + 连续备份 7 天）——
param sharedDatabaseThroughput = 400
param callsFamilyAutoscaleMax = 0 // 0 = calls 族并入 shared
param continuousBackupTier = 'Continuous7Days'

// —— 监控（plan §6：dev 低成本门槛）——
param actionGroupEmail = 'REPLACE_ME@example.com'
param dlqThreshold = 0
param failedRequestsThreshold = 10
param cosmos429Threshold = 100

// —— 自定义域名（D2；DNS 就绪前留空=默认 FQDN）——
param controlHostName = ''
param mediaHostName = ''

// —— 预算（D5 可选；0 = 不建。启用时补 budgetStartDate（月初）与 budgetContactEmails）——
param budgetAmount = 0
