// modules/containerApps.bicep — U-401 Container Apps 环境 + 三 app（spec §12）：
// control / runtime / worker。§11 三十契约占位符 + plan §4 非 §11 运行时面全量注入。
//
// 决策 D3（plan §7）：当前 bot.py 是单 FastAPI 进程同时承 control+media（bot.py:2836），
// control/runtime 共用映像、命令参数化；includeControlApp 默认 false（control 现与
// runtime 同进程）。进程拆分属代码侧后续项，不属 U-401。
//
// 决策 D4（plan §7）：Azure Files 卷挂载到 config 目录（MCP_CFG_PATH_OVERRIDE /
// RUNTIME_CFG_PATH_OVERRIDE 缝，bot.py:2871/2881 R-C-003），多副本共享。
// ⚠ 单写者运维约束：配置写入走单副本/低频人工（管理台写入后重启生效），
// 不建分布式锁——多副本并发写配置会产生分叉（runbook 有专节）。
//
// Secret 流（plan §4 主通道）：configuration.secrets[].keyVaultUrl + identity
// （MI 引用，不落字面量）→ env[].secretRef。MI 必须具备 Key Vault Secrets User（§5 #3）。
// 只落码不部署（pipeline-infra-spec §4.2 边界）。

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

@description('部署环境名（app 命名 ca-voicebot-<env>-*，plan §2）')
param environmentName string

@description('Container Apps 环境名（cae-voicebot-<env>，plan §2）')
param containerAppsEnvName string

@description('Log Analytics customerId（appLogsConfiguration；monitor.bicep 输出）')
param logAnalyticsCustomerId string

@description('Log Analytics shared key（monitor.bicep 工作区 listKeys，经 main 传入）')
@secure()
param logAnalyticsSharedKey string

@description('容器映像（ACR 全限定名，如 acrvinebot<env>.azurecr.io/voicebot:<tag>）')
param containerImage string

@description('runtime MI 资源 ID（identities.bicep 输出）')
param runtimeIdentityId string

@description('control MI 资源 ID（identities.bicep 输出）')
param controlIdentityId string

@description('worker MI 资源 ID（identities.bicep 输出）')
param workerIdentityId string

@description('spec §11 KEY_VAULT_URL（keyvault.bicep 输出；keyVaultRef uri 拼接基址）')
param KEY_VAULT_URL string

@description('spec §11 COSMOS_ENDPOINT（cosmos.bicep 输出）')
param COSMOS_ENDPOINT string

@description('spec §11 BLOB_ACCOUNT_URL（blob.bicep 输出）')
param BLOB_ACCOUNT_URL string

@description('spec §11 SERVICEBUS_NAMESPACE（servicebus.bicep 输出；契约值=短名）')
param SERVICEBUS_NAMESPACE string

@description('spec §11 APPLICATIONINSIGHTS_CONNECTION_STRING（monitor.bicep 输出）')
param APPLICATIONINSIGHTS_CONNECTION_STRING string

// ---- D4 Azure Files（config 持久卷）----
@description('Blob/Files 存储帐户名（blob.bicep 同帐户；D4 file share 挂载体）')
param storageAccountName string

@description('Azure Files 共享名（config 文件）')
param fileShareName string = 'config'

@description('D4 config 卷挂载路径（app 经 override 缝读取；运行时参数化）')
param configMountPath string = '/app/config'

// ---- 命令 / 副本 / 资源（§8.3 参数化；命令默认值对齐现网 systemd ExecStart）----
@description('runtime 容器命令（D3 共用映像；默认=现网 bot.py 进程入口）')
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

@description('D3：是否建独立 control app（当前 control 与 runtime 同进程，默认不建）')
param includeControlApp bool = false

@description('runtime 最小副本（§8.3 warm，plan §6）')
param runtimeMinReplicas int = 1

@description('runtime 最大副本（plan §6 参数化）')
param runtimeMaxReplicas int = 3

@description('control 最小副本（plan §6：0–1）')
param controlMinReplicas int = 0

@description('control 最大副本')
param controlMaxReplicas int = 2

@description('worker 最小副本（§2.2 单队列竞争消费，plan §6）')
param workerMinReplicas int = 1

@description('worker 最大副本')
param workerMaxReplicas int = 2

@description('每容器 vCPU（§8.3 资源 requests/limits 参数化）')
param cpuCores string = '0.5'

@description('每容器内存（§8.3；cost-analysis §8-2：内存翻倍=月费翻倍，实测后调）')
param memorySize string = '1Gi'

@description('HTTP/WS 监听端口（ingress targetPort + probe port）')
param appPort int = 7860

@description('liveness/readiness 探针路径（须无鉴权 GET——bot.py:2995 setup-status）')
param probePath string = '/api/auth/setup-status'

@description('探针初始延迟（秒）')
param probeInitialDelaySeconds int = 15

@description('探针周期（秒）')
param probePeriodSeconds int = 15

@description('探针超时（秒）')
param probeTimeoutSeconds int = 5

@description('探针失败阈值（连续失败次数）')
param probeFailureThreshold int = 3

// ---- 自定义域名（D2；main.bicep 按 host 参数拼装 [{name, certificateId, bindingType}]）----
@description('runtime app 自定义域名绑定（media WS 面；空数组 = 用默认 FQDN）')
param runtimeCustomDomains array = []

@description('control app 自定义域名绑定（空数组 = 用默认 FQDN）')
param controlCustomDomains array = []

// ---- §11 契约面取值（名字冻结，config_contract.py；不含模块输出与内部推导项）----
@description('spec §11 PUBLIC_CONTROL_BASE_URL')
param PUBLIC_CONTROL_BASE_URL string

@description('spec §11 PUBLIC_MEDIA_BASE_URL')
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

@description('spec §11 LLM_ENDPOINT')
param LLM_ENDPOINT string

@description('spec §11 LLM_DEPLOYMENT（Foundry 部署名，plan §5 A-1）')
param LLM_DEPLOYMENT string

@description('spec §11 LLM_API_VERSION（可选；空=适配器不需）')
param LLM_API_VERSION string = ''

@description('spec §11 SPEECH_REGION')
param SPEECH_REGION string

@description('spec §11 SPEECH_ENDPOINT（可选）')
param SPEECH_ENDPOINT string = ''

@description('spec §11 STT_LOCALE')
param STT_LOCALE string

@description('spec §11 TTS_VOICE')
param TTS_VOICE string

@description('spec §11 REDIS_ENDPOINT（可选；D-9 未批——留空不注入协调）')
param REDIS_ENDPOINT string = ''

@description('spec §11 MAX_CONCURRENT_CALLS_PER_REPLICA')
param MAX_CONCURRENT_CALLS_PER_REPLICA int

@description('spec §11 MAX_CALL_DURATION_SECONDS')
param MAX_CALL_DURATION_SECONDS int

@description('spec §11 RECORDING_ENABLED（严格 true/false，默认 false——A10）')
param RECORDING_ENABLED string = 'false'

@description('spec §11 TRANSCRIPT_RETENTION_DAYS')
param TRANSCRIPT_RETENTION_DAYS int

@description('spec §11 VOICE_LIVE_ENABLED（严格 true/false，默认 false）')
param VOICE_LIVE_ENABLED string = 'false'

// ---- 非 §11 运行时面（plan §4 env 注入清单）----
@description('plan §4：MiniMax group id（bot.py:1836 读取）')
param MINIMAX_GROUP_ID string = ''

@description('plan §4：MiniMax TTS base URL（默认值对齐 bot.py:1837）')
param MINIMAX_BASE_URL string = 'https://api.minimax.chat/v1/t2a_v2'

@description('plan §4：MiniMax TTS 模型（默认值对齐 bot.py:916）')
param MINIMAX_MODEL_DEFAULT string = 'speech-2.8-turbo'

@description('plan §4：历史持久化开关（B10 后唯一开关；0=启用，bot.py:1263）')
param HISTORY_DISABLED string = '0'

@description('plan §4 §9.2：activity 日志 TTL 天数（activity_store.py 默认 90）')
param ACTIVITY_LOG_TTL_DAYS string = '90'

// ---- 资源引用：Blob 帐户（D4 file share 依附其上）----
resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvName
  location: AZURE_LOCATION
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalyticsCustomerId
        sharedKey: logAnalyticsSharedKey
      }
    }
    // D2：不配置 vnetConfiguration —— 公网 ingress 承载 Twilio WS（plan §10）
  }
}

// D4：config 共享文件面（KB 级配置文件，1 GiB 配额足够）
resource configShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-05-01' = {
  parent: fileServices
  name: fileShareName
  properties: {
    shareQuota: 1
  }
}

// D4：环境级 Azure Files 存储绑定（accountKey 经 listKeys，不落字面量）
resource caeStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: managedEnvironment
  name: 'configstorage'
  properties: {
    azureFile: {
      accountName: storageAccountName
      shareName: fileShareName
      accountKey: storage.listKeys().keys[0].value
      accessMode: 'ReadWrite'
    }
  }
}

// ---- env 注入清单：§11 三十占位符 + plan §4 非 §11 面（三 app 统一注入）----
// COSMOS_DATABASE 钉 voicebot（plan §4）；TWILIO_AUTH_TOKEN_SECRET_NAME 钉
// TWILIO-AUTH-TOKEN（plan §4，指向 KV secret 名）；TWILIO_ACCOUNT_SID 按 X2
// 裁决入 Vault、env 注入值（secretRef）。
var commonEnv = [
  // —— §11 契约三十占位符（config_contract.py 冻结）——
  {
    name: 'APP_ENV'
    value: environmentName
  }
  {
    name: 'AZURE_LOCATION'
    value: AZURE_LOCATION
  }
  {
    name: 'PUBLIC_CONTROL_BASE_URL'
    value: PUBLIC_CONTROL_BASE_URL
  }
  {
    name: 'PUBLIC_MEDIA_BASE_URL'
    value: PUBLIC_MEDIA_BASE_URL
  }
  {
    name: 'TELEPHONY_PROVIDER'
    value: TELEPHONY_PROVIDER
  }
  {
    name: 'VOICE_ENGINE'
    value: VOICE_ENGINE
  }
  {
    name: 'STT_PROVIDER'
    value: STT_PROVIDER
  }
  {
    name: 'TTS_PROVIDER'
    value: TTS_PROVIDER
  }
  {
    name: 'LLM_PROVIDER'
    value: LLM_PROVIDER
  }
  {
    name: 'LLM_ENDPOINT'
    value: LLM_ENDPOINT
  }
  {
    name: 'LLM_DEPLOYMENT'
    value: LLM_DEPLOYMENT
  }
  {
    name: 'LLM_API_VERSION'
    value: LLM_API_VERSION
  }
  {
    name: 'SPEECH_REGION'
    value: SPEECH_REGION
  }
  {
    name: 'SPEECH_ENDPOINT'
    value: SPEECH_ENDPOINT
  }
  {
    name: 'STT_LOCALE'
    value: STT_LOCALE
  }
  {
    name: 'TTS_VOICE'
    value: TTS_VOICE
  }
  {
    name: 'COSMOS_ENDPOINT'
    value: COSMOS_ENDPOINT
  }
  {
    name: 'COSMOS_DATABASE'
    value: 'voicebot'
  }
  {
    name: 'BLOB_ACCOUNT_URL'
    value: BLOB_ACCOUNT_URL
  }
  {
    name: 'SERVICEBUS_NAMESPACE'
    value: SERVICEBUS_NAMESPACE
  }
  {
    name: 'REDIS_ENDPOINT'
    value: REDIS_ENDPOINT
  }
  {
    name: 'KEY_VAULT_URL'
    value: KEY_VAULT_URL
  }
  {
    name: 'TWILIO_ACCOUNT_SID'
    secretRef: 'twilio-account-sid'
  }
  {
    name: 'TWILIO_AUTH_TOKEN_SECRET_NAME'
    value: 'TWILIO-AUTH-TOKEN'
  }
  {
    name: 'MAX_CONCURRENT_CALLS_PER_REPLICA'
    value: string(MAX_CONCURRENT_CALLS_PER_REPLICA)
  }
  {
    name: 'MAX_CALL_DURATION_SECONDS'
    value: string(MAX_CALL_DURATION_SECONDS)
  }
  {
    name: 'RECORDING_ENABLED'
    value: RECORDING_ENABLED
  }
  {
    name: 'TRANSCRIPT_RETENTION_DAYS'
    value: string(TRANSCRIPT_RETENTION_DAYS)
  }
  {
    name: 'VOICE_LIVE_ENABLED'
    value: VOICE_LIVE_ENABLED
  }
  {
    name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
    value: APPLICATIONINSIGHTS_CONNECTION_STRING
  }
  // —— 非 §11 运行时面（plan §4）——
  {
    name: 'MINIMAX_API_KEY'
    secretRef: 'minimax-api-key'
  }
  {
    name: 'AUTH_SECRET'
    secretRef: 'auth-secret'
  }
  {
    name: 'TWILIO_AUTH_TOKEN'
    secretRef: 'twilio-auth-token'
  }
  {
    name: 'MINIMAX_GROUP_ID'
    value: MINIMAX_GROUP_ID
  }
  {
    name: 'MINIMAX_BASE_URL'
    value: MINIMAX_BASE_URL
  }
  {
    name: 'MINIMAX_MODEL_DEFAULT'
    value: MINIMAX_MODEL_DEFAULT
  }
  {
    name: 'HISTORY_DISABLED'
    value: HISTORY_DISABLED
  }
  {
    name: 'ACTIVITY_LOG_TTL_DAYS'
    value: ACTIVITY_LOG_TTL_DAYS
  }
  // —— D4：配置持久卷 override 缝（bot.py:2871/2881，R-C-003）——
  {
    name: 'MCP_CFG_PATH_OVERRIDE'
    value: '${configMountPath}/mcp_servers.json'
  }
  {
    name: 'RUNTIME_CFG_PATH_OVERRIDE'
    value: '${configMountPath}/runtime.json'
  }
]

// 三 app 规格（D3：includeControlApp=false 时 runtime 兼承 control 面）。
// 固定顺序 [runtime, (control), worker]——runtime 恒为 apps[0]。
var runtimeSpec = {
  suffix: 'runtime'
  containerName: 'runtime'
  identityId: runtimeIdentityId
  command: runtimeCommand
  minReplicas: runtimeMinReplicas
  maxReplicas: runtimeMaxReplicas
  customDomains: runtimeCustomDomains
  httpIngress: true
}
var controlSpec = {
  suffix: 'control'
  containerName: 'control'
  identityId: controlIdentityId
  command: controlCommand
  minReplicas: controlMinReplicas
  maxReplicas: controlMaxReplicas
  customDomains: controlCustomDomains
  httpIngress: true
}
var workerSpec = {
  suffix: 'worker'
  containerName: 'worker'
  identityId: workerIdentityId
  command: workerCommand
  minReplicas: workerMinReplicas
  maxReplicas: workerMaxReplicas
  customDomains: []
  // §2.2 队列消费者无 HTTP 监听——不配 ingress/探针
  httpIngress: false
}
var appSpecs = includeControlApp ? [
  runtimeSpec
  controlSpec
  workerSpec
] : [
  runtimeSpec
  workerSpec
]

resource apps 'Microsoft.App/containerApps@2024-03-01' = [for s in appSpecs: {
  name: 'ca-voicebot-${environmentName}-${s.suffix}'
  location: AZURE_LOCATION
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${s.identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: managedEnvironment.id
    configuration: {
      activeRevisionsMode: 'Single'
      // plan §4 主通道：Key Vault 引用（keyVaultUrl+identity，MI 解析）→ env secretRef；
      // secret 不落 env 文件/模板字面量；不钉版本=随 KV 轮换 30 分钟内自动刷新
      secrets: [
        {
          name: 'minimax-api-key'
          keyVaultUrl: '${KEY_VAULT_URL}secrets/MINIMAX-API-KEY'
          identity: s.identityId
        }
        {
          name: 'auth-secret'
          keyVaultUrl: '${KEY_VAULT_URL}secrets/AUTHSECRET'
          identity: s.identityId
        }
        {
          name: 'twilio-auth-token'
          keyVaultUrl: '${KEY_VAULT_URL}secrets/TWILIO-AUTH-TOKEN'
          identity: s.identityId
        }
        {
          name: 'twilio-account-sid'
          keyVaultUrl: '${KEY_VAULT_URL}secrets/TWILIO-ACCOUNT-SID'
          identity: s.identityId
        }
      ]
      ingress: s.httpIngress ? {
        external: true
        targetPort: appPort
        // 'auto'：HTTP 与 WS（/twilio/media-stream、/ws）同端口
        transport: 'auto'
        allowInsecure: false
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
        customDomains: empty(s.customDomains) ? null : s.customDomains
      } : null
    }
    template: {
      containers: [
        {
          name: s.containerName
          image: containerImage
          command: s.command
          env: commonEnv
          resources: {
            cpu: json(cpuCores)
            memory: memorySize
          }
          volumeMounts: [
            {
              volumeName: 'config-vol'
              mountPath: configMountPath
            }
          ]
          probes: s.httpIngress ? [
            {
              type: 'Liveness'
              httpGet: {
                path: probePath
                port: appPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: probeInitialDelaySeconds
              periodSeconds: probePeriodSeconds
              timeoutSeconds: probeTimeoutSeconds
              failureThreshold: probeFailureThreshold
            }
            {
              type: 'Readiness'
              httpGet: {
                path: probePath
                port: appPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: probeInitialDelaySeconds
              periodSeconds: probePeriodSeconds
              timeoutSeconds: probeTimeoutSeconds
              failureThreshold: probeFailureThreshold
            }
          ] : null
        }
      ]
      scale: {
        minReplicas: s.minReplicas
        maxReplicas: s.maxReplicas
      }
      volumes: [
        // D4：Azure Files config 卷，多副本共享；单写者约束见文件头注释
        {
          name: 'config-vol'
          storageType: 'AzureFile'
          storageName: 'configstorage'
        }
      ]
    }
  }
}]

output CONTAINER_APPS_ENV_ID string = managedEnvironment.id
output RUNTIME_FQDN string = apps[0].properties.configuration.ingress.fqdn
output RUNTIME_APP_NAME string = apps[0].name
