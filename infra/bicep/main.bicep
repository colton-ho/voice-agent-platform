// main.bicep — U-401 编排入口（plan §1 目标拓扑 / §9 部署序列）。
// 订阅级部署：建 rg-voicebot-<env> + 全模块（plan §9 步骤 4：az deployment sub create）。
// 契约面：§11 变量名冻结（config_contract.py），不新造；输出五值喂 §11 env 注入。
// 边界：只落码不部署（pipeline-infra-spec §4.2——写 Bicep ≠ 授权云上 provisioning）。
// 手动前置（plan §5）：Foundry/AOAI + Speech 资源不入 Bicep（G-2 商业审批），
// 建成后以资源 ID 参数补派 §5 #1/#2 角色。

targetScope = 'subscription'

// ---- 部署面 ----
@description('部署环境名（plan §2：dev / staging / prod）')
@allowed([
  'dev'
  'staging'
  'prod'
])
param environmentName string

@description('spec §11 AZURE_LOCATION — validated Azure region（plan §1 单区域）')
param AZURE_LOCATION string = 'eastus'

// ---- §11 契约面（名字冻结；值按环境落 params/*.bicepparam）----
@description('spec §11 PUBLIC_CONTROL_BASE_URL（首布后回填 app FQDN 或自定义域名，见 runbook）')
param PUBLIC_CONTROL_BASE_URL string

@description('spec §11 PUBLIC_MEDIA_BASE_URL（同上）')
param PUBLIC_MEDIA_BASE_URL string

@description('spec §11 TELEPHONY_PROVIDER')
param TELEPHONY_PROVIDER string = 'twilio'

@description('spec §11 VOICE_ENGINE（cascade=FC-2+FC-3+FC-4）')
param VOICE_ENGINE string = 'cascade'

@description('spec §11 STT_PROVIDER')
param STT_PROVIDER string = 'azure_speech'

@description('spec §11 TTS_PROVIDER')
param TTS_PROVIDER string = 'azure_speech'

@description('spec §11 LLM_PROVIDER（approved 适配器注册名）')
param LLM_PROVIDER string

@description('spec §11 LLM_ENDPOINT（Foundry/AOAI 端点，plan §5 手动前置产出）')
param LLM_ENDPOINT string

@description('spec §11 LLM_DEPLOYMENT（Foundry 部署名，plan §5 A-1 MODELS）')
param LLM_DEPLOYMENT string

@description('spec §11 LLM_API_VERSION（可选）')
param LLM_API_VERSION string = ''

@description('spec §11 SPEECH_REGION（plan §5 Speech 手动前置）')
param SPEECH_REGION string

@description('spec §11 SPEECH_ENDPOINT（可选）')
param SPEECH_ENDPOINT string = ''

@description('spec §11 STT_LOCALE')
param STT_LOCALE string

@description('spec §11 TTS_VOICE')
param TTS_VOICE string

@description('spec §11 REDIS_ENDPOINT（可选；D-9 未批——留空）')
param REDIS_ENDPOINT string = ''

@description('spec §11 MAX_CONCURRENT_CALLS_PER_REPLICA')
param MAX_CONCURRENT_CALLS_PER_REPLICA int

@description('spec §11 MAX_CALL_DURATION_SECONDS')
param MAX_CALL_DURATION_SECONDS int

@description('spec §11 RECORDING_ENABLED（严格 true/false，A10 默认 false）')
param RECORDING_ENABLED string = 'false'

@description('spec §11 TRANSCRIPT_RETENTION_DAYS')
param TRANSCRIPT_RETENTION_DAYS int

@description('spec §11 VOICE_LIVE_ENABLED（严格 true/false，默认 false）')
param VOICE_LIVE_ENABLED string = 'false'

// ---- secrets（§3.1 清单；值只经 @secure() 部署参数，不落盘/不入字面量——plan §4）----
@description('§3.1 MINIMAX-API-KEY（对应 env MINIMAX_API_KEY）')
@secure()
param minimaxApiKey string

@description('§3.1 TWILIO-AUTH-TOKEN（对应 env TWILIO_AUTH_TOKEN + TWILIO_AUTH_TOKEN_SECRET_NAME）')
@secure()
param twilioAuthToken string

@description('§3.1 TWILIO-ACCOUNT-SID（X2 裁决：入 Vault，env 注入值）')
@secure()
param twilioAccountSid string

@description('§3.1 AUTHSECRET（B12：ephemeral fallback 退役，缺值 fail-fast）')
@secure()
param authSecretValue string

// ---- 手动前置资源（plan §5；留空 = 跳过 §5 #1/#2 派发，建成后重跑补派）----
@description('§5 #1 手动 Foundry/AOAI 帐户完整资源 ID（plan §9 步骤 6 补派）')
param foundryResourceId string = ''

@description('§5 #2 手动 Speech 资源完整资源 ID（plan §9 步骤 6 补派）')
param speechResourceId string = ''

// ---- 容器面（§8.3；命令默认值对齐现网 systemd ExecStart）----
@description('容器映像（ACR 全限定名，plan §9 步骤 7 推送后填 tag）')
param containerImage string

@description('D3：是否建独立 control app（当前 control 与 runtime 同进程，默认不建）')
param includeControlApp bool = false

@description('runtime 容器命令')
param runtimeCommand array = [
  'python'
  'bot.py'
  '--host'
  '0.0.0.0'
  '--port'
  '7860'
]

@description('control 容器命令（D3：进程拆分前与 runtime 相同）')
param controlCommand array = [
  'python'
  'bot.py'
  '--host'
  '0.0.0.0'
  '--port'
  '7860'
]

@description('worker 容器命令（§2.2 postcall-jobs 消费入口）')
param workerCommand array = [
  'python'
  'workers/postcall_worker.py'
]

@description('runtime 最小副本（§8.3 warm，plan §6）')
param runtimeMinReplicas int = 1

@description('runtime 最大副本')
param runtimeMaxReplicas int = 3

@description('control 最小副本（plan §6：0–1）')
param controlMinReplicas int = 0

@description('control 最大副本')
param controlMaxReplicas int = 2

@description('worker 最小副本（§2.2 单队列竞争消费）')
param workerMinReplicas int = 1

@description('worker 最大副本')
param workerMaxReplicas int = 2

@description('每容器 vCPU（§8.3）')
param cpuCores string = '0.5'

@description('每容器内存（§8.3）')
param memorySize string = '1Gi'

@description('HTTP/WS 监听端口（ingress + 探针）')
param appPort int = 7860

@description('liveness/readiness 探针路径（无鉴权 GET）')
param probePath string = '/api/auth/setup-status'

@description('D4 config 卷挂载路径（R-C-003 override 缝指向此处）')
param configMountPath string = '/app/config'

// ---- 非 §11 运行时面（plan §4）----
@description('plan §4：MiniMax group id')
param MINIMAX_GROUP_ID string = ''

@description('plan §4：MiniMax TTS base URL（默认值对齐 bot.py:1837）')
param MINIMAX_BASE_URL string = 'https://api.minimax.chat/v1/t2a_v2'

@description('plan §4：MiniMax TTS 模型（默认值对齐 bot.py:916）')
param MINIMAX_MODEL_DEFAULT string = 'speech-2.8-turbo'

@description('plan §4：历史持久化开关（0=启用，bot.py:1263）')
param HISTORY_DISABLED string = '0'

@description('plan §4 §9.2：activity 日志 TTL 天数')
param ACTIVITY_LOG_TTL_DAYS string = '90'

// ---- Cosmos 容量（§1.4；workload-driven，V5 实测后调）----
@description('§1.4 dev：database 级 shared RU/s')
param sharedDatabaseThroughput int = 400

@description('§1.4 staging/prod：calls 容器族 autoscale 上限（0 = 并入 shared）')
param callsFamilyAutoscaleMax int = 0

@description('§1.4 连续备份（U-401 参数）')
@allowed([
  'Continuous7Days'
  'Continuous30Days'
])
param continuousBackupTier string = 'Continuous7Days'

// ---- 监控（monitor.bicep）----
@description('告警 action group 邮件接收者（D5 参数化）')
param actionGroupEmail string

@description('action group 短名')
param actionGroupShortName string = 'vbalert'

@description('DLQ 深度告警阈值')
param dlqThreshold int = 0

@description('app 失败请求告警阈值（5 分钟计数）')
param failedRequestsThreshold int = 10

@description('Cosmos 429 告警阈值（5 分钟计数）')
param cosmos429Threshold int = 100

// ---- 自定义域名（D2；两步/三步部署见 runbook：DNS 就绪前留空）----
@description('control 面自定义主机名（留空 = 默认 FQDN）')
param controlHostName string = ''

@description('media 面自定义主机名（留空 = 默认 FQDN）')
param mediaHostName string = ''

@description('自定义域名绑定形态：稳态默认 SniEnabled（后续部署保持在线绑定不回退）；首绑那一次须显式置 Disabled（证书签发验证期），就绪后在 params 文件持久化 SniEnabled（两跳，见 runbook §2 步骤 8）')
@allowed([
  'Disabled'
  'SniEnabled'
])
param customDomainBindingType string = 'SniEnabled'

// ---- 预算（D5 可选；订阅层，budgetAmount=0 不建）----
@description('D5：订阅级预算名（空 = budget-voicebot-<env>）')
param budgetName string = ''

@description('D5：月度预算金额（USD；0 = 不建预算）')
param budgetAmount int = 0

@description('D5：预算起始日（Consumption 要求月初，格式 YYYY-MM-01T00:00:00Z）')
param budgetStartDate string = ''

@description('D5：预算通知邮箱列表（启用预算时必填）')
param budgetContactEmails array = []

// ---- 命名（plan §2 逐字；§11 契约面不动）----
var rgName = 'rg-voicebot-${environmentName}'
var cosmosAccountName = 'cosmos-voicebot-${environmentName}'
var serviceBusName = 'sb-voicebot-${environmentName}'
var keyVaultName = 'kv-voicebot-${environmentName}'
var acrName = 'acrvinebot${environmentName}'
var caeName = 'cae-voicebot-${environmentName}'
// plan §2 命名偏差（2026-11-11 what-if 实测）：storage 帐户名为全局唯一命名空间，
// stvoicebot<env> 的 dev/prod 已被第三方占用 → 统一加项目缩写后缀 vap（三环境一致）
var storageAccountName = 'stvoicebot${environmentName}vap'
var logAnalyticsName = 'log-voicebot-${environmentName}'

resource rg 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: rgName
  location: AZURE_LOCATION
}

module monitor 'modules/monitor.bicep' = {
  scope: rg
  name: 'monitor'
  params: {
    AZURE_LOCATION: AZURE_LOCATION
    environmentName: environmentName
    cosmosAccountName: cosmosAccountName
    servicebusNamespaceName: serviceBusName
    actionGroupEmail: actionGroupEmail
    actionGroupShortName: actionGroupShortName
    dlqThreshold: dlqThreshold
    failedRequestsThreshold: failedRequestsThreshold
    cosmos429Threshold: cosmos429Threshold
  }
}

module cosmos 'modules/cosmos.bicep' = {
  scope: rg
  name: 'cosmos'
  params: {
    AZURE_LOCATION: AZURE_LOCATION
    COSMOS_DATABASE: 'voicebot'
    accountName: cosmosAccountName
    sharedDatabaseThroughput: sharedDatabaseThroughput
    callsFamilyAutoscaleMax: callsFamilyAutoscaleMax
    continuousBackupTier: continuousBackupTier
  }
}

module servicebus 'modules/servicebus.bicep' = {
  scope: rg
  name: 'servicebus'
  params: {
    SERVICEBUS_NAMESPACE: serviceBusName
    AZURE_LOCATION: AZURE_LOCATION
  }
}

module keyvault 'modules/keyvault.bicep' = {
  scope: rg
  name: 'keyvault'
  params: {
    keyVaultName: keyVaultName
    AZURE_LOCATION: AZURE_LOCATION
    minimaxApiKey: minimaxApiKey
    twilioAuthToken: twilioAuthToken
    twilioAccountSid: twilioAccountSid
    authSecretValue: authSecretValue
  }
}

module acr 'modules/acr.bicep' = {
  scope: rg
  name: 'acr'
  params: {
    AZURE_LOCATION: AZURE_LOCATION
    acrName: acrName
  }
}

module blob 'modules/blob.bicep' = {
  scope: rg
  name: 'blob'
  params: {
    AZURE_LOCATION: AZURE_LOCATION
    storageAccountName: storageAccountName
  }
}

// §5 行 3–8 的派发目标（KV/Cosmos/SB 队列/ACR/artifacts 容器）须先存在
module identities 'modules/identities.bicep' = {
  scope: rg
  name: 'identities'
  dependsOn: [
    cosmos
    servicebus
    keyvault
    acr
    blob
  ]
  params: {
    AZURE_LOCATION: AZURE_LOCATION
    environmentName: environmentName
    keyVaultName: keyVaultName
    cosmosAccountName: cosmosAccountName
    servicebusNamespaceName: serviceBusName
    acrName: acrName
    storageAccountName: storageAccountName
    foundryResourceId: foundryResourceId
    speechResourceId: speechResourceId
  }
}

// D2：托管 TLS 自定义域名（空 host = 零资源）；证书先于 app 绑定
module network 'modules/network.bicep' = {
  scope: rg
  name: 'network'
  params: {
    AZURE_LOCATION: AZURE_LOCATION
    managedEnvironmentName: caeName
    environmentName: environmentName
    controlHostName: controlHostName
    mediaHostName: mediaHostName
  }
}

// 自定义域名绑定：media → runtime app；control → control app（未建时并入 runtime，D3）
var controlDomains = empty(controlHostName) ? [] : [
  {
    name: controlHostName
    certificateId: network.outputs.CONTROL_CERTIFICATE_ID
    bindingType: customDomainBindingType
  }
]
var runtimeDomains = concat(
  empty(mediaHostName) ? [] : [
    {
      name: mediaHostName
      certificateId: network.outputs.MEDIA_CERTIFICATE_ID
      bindingType: customDomainBindingType
    }
  ],
  includeControlApp ? [] : controlDomains
)

module containerApps 'modules/containerApps.bicep' = {
  scope: rg
  name: 'containerApps'
  // 依赖 network 的隐式序：证书创建/更新先于 ingress 绑定（D2 三步部署；
  // runtimeDomains/controlDomains 引用 network.outputs 即形成依赖边）
  params: {
    AZURE_LOCATION: AZURE_LOCATION
    environmentName: environmentName
    containerAppsEnvName: caeName
    logAnalyticsCustomerId: monitor.outputs.LOG_ANALYTICS_CUSTOMER_ID
    // shared key 是密钥面：main 以确定性 workspace ID 经 listKeys 即取即传（@secure
    // 参数），不入模块输出（模块输出值非部署起点值，listKeys 不收——BCP181）。
    // 订阅级模板里 resourceId 必须用 4 参形制（订阅+RG+类型+名）；3 参 (rg,type,name)
    // 形制只在 RG 级模板有效，在订阅级会被解析为 (subscriptionId,...) 并报
    // "not valid subscription identifier"（what-if 实测，2026-11-11）
    logAnalyticsSharedKey: listKeys(resourceId(subscription().subscriptionId, rg.name, 'Microsoft.OperationalInsights/workspaces', logAnalyticsName), '2023-09-01').primarySharedKey
    containerImage: containerImage
    runtimeIdentityId: identities.outputs.MI_RUNTIME_ID
    controlIdentityId: identities.outputs.MI_CONTROL_ID
    workerIdentityId: identities.outputs.MI_WORKER_ID
    KEY_VAULT_URL: keyvault.outputs.KEY_VAULT_URL
    COSMOS_ENDPOINT: cosmos.outputs.COSMOS_ENDPOINT
    BLOB_ACCOUNT_URL: blob.outputs.BLOB_ACCOUNT_URL
    SERVICEBUS_NAMESPACE: servicebus.outputs.SERVICEBUS_NAMESPACE
    APPLICATIONINSIGHTS_CONNECTION_STRING: monitor.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
    storageAccountName: storageAccountName
    configMountPath: configMountPath
    runtimeCommand: runtimeCommand
    controlCommand: controlCommand
    workerCommand: workerCommand
    includeControlApp: includeControlApp
    runtimeMinReplicas: runtimeMinReplicas
    runtimeMaxReplicas: runtimeMaxReplicas
    controlMinReplicas: controlMinReplicas
    controlMaxReplicas: controlMaxReplicas
    workerMinReplicas: workerMinReplicas
    workerMaxReplicas: workerMaxReplicas
    cpuCores: cpuCores
    memorySize: memorySize
    appPort: appPort
    probePath: probePath
    runtimeCustomDomains: runtimeDomains
    controlCustomDomains: controlDomains
    PUBLIC_CONTROL_BASE_URL: PUBLIC_CONTROL_BASE_URL
    PUBLIC_MEDIA_BASE_URL: PUBLIC_MEDIA_BASE_URL
    TELEPHONY_PROVIDER: TELEPHONY_PROVIDER
    VOICE_ENGINE: VOICE_ENGINE
    STT_PROVIDER: STT_PROVIDER
    TTS_PROVIDER: TTS_PROVIDER
    LLM_PROVIDER: LLM_PROVIDER
    LLM_ENDPOINT: LLM_ENDPOINT
    LLM_DEPLOYMENT: LLM_DEPLOYMENT
    LLM_API_VERSION: LLM_API_VERSION
    SPEECH_REGION: SPEECH_REGION
    SPEECH_ENDPOINT: SPEECH_ENDPOINT
    STT_LOCALE: STT_LOCALE
    TTS_VOICE: TTS_VOICE
    REDIS_ENDPOINT: REDIS_ENDPOINT
    MAX_CONCURRENT_CALLS_PER_REPLICA: MAX_CONCURRENT_CALLS_PER_REPLICA
    MAX_CALL_DURATION_SECONDS: MAX_CALL_DURATION_SECONDS
    RECORDING_ENABLED: RECORDING_ENABLED
    TRANSCRIPT_RETENTION_DAYS: TRANSCRIPT_RETENTION_DAYS
    VOICE_LIVE_ENABLED: VOICE_LIVE_ENABLED
    MINIMAX_GROUP_ID: MINIMAX_GROUP_ID
    MINIMAX_BASE_URL: MINIMAX_BASE_URL
    MINIMAX_MODEL_DEFAULT: MINIMAX_MODEL_DEFAULT
    HISTORY_DISABLED: HISTORY_DISABLED
    ACTIVITY_LOG_TTL_DAYS: ACTIVITY_LOG_TTL_DAYS
  }
}

// D5：订阅级预算（可选；「預算歸屬訂閱層，避免模組越權」——不落 monitor.bicep）
resource budget 'Microsoft.Consumption/budgets@2023-11-01' = if (budgetAmount > 0) {
  name: empty(budgetName) ? 'budget-voicebot-${environmentName}' : budgetName
  properties: {
    category: 'Cost'
    amount: budgetAmount
    timeGrain: 'Monthly'
    timePeriod: {
      // Consumption 约束：起始日必须是月初
      startDate: budgetStartDate
    }
    notifications: {
      Actual_GTE_80: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 80
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
      Actual_GTE_100: {
        enabled: true
        operator: 'GreaterThanOrEqualTo'
        threshold: 100
        thresholdType: 'Actual'
        contactEmails: budgetContactEmails
      }
    }
  }
}

// ---- §11 契约面输出（喂 config_contract env；部署脚本/管理台取值）----
output COSMOS_ENDPOINT string = cosmos.outputs.COSMOS_ENDPOINT
output KEY_VAULT_URL string = keyvault.outputs.KEY_VAULT_URL
output SERVICEBUS_NAMESPACE string = servicebus.outputs.SERVICEBUS_NAMESPACE
output BLOB_ACCOUNT_URL string = blob.outputs.BLOB_ACCOUNT_URL
output APPLICATIONINSIGHTS_CONNECTION_STRING string = monitor.outputs.APPLICATIONINSIGHTS_CONNECTION_STRING
// 辅助输出（非契约面）：两步域名/映像推送用
output RESOURCE_GROUP_NAME string = rg.name
output ACR_LOGIN_SERVER string = acr.outputs.ACR_LOGIN_SERVER
output RUNTIME_FQDN string = containerApps.outputs.RUNTIME_FQDN
