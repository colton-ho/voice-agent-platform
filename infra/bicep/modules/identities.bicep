// modules/identities.bicep — U-401 三个用户分配 MI + pipeline-infra-spec §5 RBAC 行 1–8。
// 行 10（mi-deploy / Contributor / GH OIDC）归 U-402，本模块不建（plan §3）。
// 行 1/2 目标（Foundry/AOAI、Speech）是用户手动建立的资源（plan §5 G-2）：
// 资源 ID 参数留空即整行跳过（条件派发），建成后填参重跑补派（plan §9 步骤 6）。
// 行 4 是 Cosmos 数据面 sqlRoleAssignments（built-in Data Contributor 是
// Cosmos DB SQL 角色，不是 ARM 角色——走 /sqlRoleDefinitions/00000000-…-0002）。
// 只落码不部署（pipeline-infra-spec §4.2 边界）。

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

@description('部署环境名（MI 命名 mi-voicebot-<env>-runtime/-control/-worker，plan §2）')
param environmentName string

@description('Key Vault 名（§5 #3 scope）')
param keyVaultName string

@description('Cosmos 帐户名（§5 #4 scope）')
param cosmosAccountName string

@description('Service Bus namespace 名（§5 #5/6 scope = postcall-jobs 队列）')
param servicebusNamespaceName string

@description('ACR 名（§5 #7 scope）')
param acrName string

@description('Blob 帐户名（§5 #8 scope = artifacts 容器）')
param storageAccountName string

@description('§5 #1 手动 Foundry/AOAI 帐户完整资源 ID（plan §5）；留空 = 跳过该行派发')
param foundryResourceId string = ''

@description('§5 #2 手动 Speech 资源完整资源 ID（plan §5）；留空 = 跳过该行派发')
param speechResourceId string = ''

// ---- 三 MI（plan §2/§3；固定顺序 runtime=0 / control=1 / worker=2）----
var miSuffixes = ['runtime', 'control', 'worker']

resource mis 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = [for s in miSuffixes: {
  name: 'mi-voicebot-${environmentName}-${s}'
  location: AZURE_LOCATION
}]

// ---- 派发目标的现有资源引用（existing 不产生部署依赖；main.bicep 以 dependsOn 保序）----
resource kv 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource cosmosAccount 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' existing = {
  name: cosmosAccountName
}

resource sbNamespace 'Microsoft.ServiceBus/namespaces@2021-11-01' existing = {
  name: servicebusNamespaceName
}

resource postcallQueue 'Microsoft.ServiceBus/namespaces/queues@2021-11-01' existing = {
  parent: sbNamespace
  // 冻结名（§2.1）
  name: 'postcall-jobs'
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: acrName
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storage
  name: 'default'
}

resource artifacts 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  parent: blobService
  name: 'artifacts'
}

// 手动资源可空：按完整资源 ID 解析订阅/RG/名称，经 csRoleAssignments 模块跨 RG 派发
// （BCP139：跨作用域必须走 module）。
// ID 形制：/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<name>
// 空 ID 时 sub/rg 回退到当前订阅/RG：condition=false 不会部署，但 ARM 对假条件资源仍会
// 部分求值 scope（bicep#3265/#3990），resourceGroup('','') 会让整次 what-if/create 失败（B2）
var foundrySub = empty(foundryResourceId) ? subscription().subscriptionId : split(foundryResourceId, '/')[2]
var foundryRg = empty(foundryResourceId) ? resourceGroup().name : split(foundryResourceId, '/')[4]
var foundryName = empty(foundryResourceId) ? '' : last(split(foundryResourceId, '/'))
var speechSub = empty(speechResourceId) ? subscription().subscriptionId : split(speechResourceId, '/')[2]
var speechRg = empty(speechResourceId) ? resourceGroup().name : split(speechResourceId, '/')[4]
var speechName = empty(speechResourceId) ? '' : last(split(speechResourceId, '/'))

// ---- 内置角色定义 ID（§5 十行矩阵；GUID 为 Azure built-in roles 官方值）----
var roleOpenAiUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
var roleSpeechUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'f2dc8367-1007-4938-bd23-fe263f013447')
var roleKvSecretsUser = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
var roleSbDataSender = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39')
var roleSbDataReceiver = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0')
var roleAcrPull = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var roleBlobDataContributor = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')
// Cosmos DB Built-in Data Contributor（数据面 SQL 角色，固定 GUID）
var cosmosDataContributorDef = '00000000-0000-0000-0000-000000000002'

// ---- §5 #3 Key Vault Secrets User ×3（§3.2 secretRef 读取面）----
resource kvSecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (s, i) in miSuffixes: {
  name: guid(kv.id, mis[i].id, roleKvSecretsUser)
  scope: kv
  properties: {
    roleDefinitionId: roleKvSecretsUser
    principalId: mis[i].properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

// ---- §5 #4 Cosmos DB Built-in Data Contributor ×3（分表 CRUD）----
resource cosmosDataContributor 'Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments@2024-05-15' = [for (s, i) in miSuffixes: {
  parent: cosmosAccount
  name: guid(cosmosAccount.id, mis[i].id, cosmosDataContributorDef)
  properties: {
    roleDefinitionId: '${cosmosAccount.id}/sqlRoleDefinitions/${cosmosDataContributorDef}'
    principalId: mis[i].properties.principalId
    scope: cosmosAccount.id
  }
}]

// ---- §5 #5 Azure Service Bus Data Sender ×2（runtime 投递 / control re-summarize）----
resource sbDataSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (s, i) in miSuffixes: if (i < 2) {
  name: guid(postcallQueue.id, mis[i].id, roleSbDataSender)
  scope: postcallQueue
  properties: {
    roleDefinitionId: roleSbDataSender
    principalId: mis[i].properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

// ---- §5 #6 Azure Service Bus Data Receiver ×1（worker 消费）----
resource sbDataReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for s in [2]: {
  name: guid(postcallQueue.id, mis[s].id, roleSbDataReceiver)
  scope: postcallQueue
  properties: {
    roleDefinitionId: roleSbDataReceiver
    principalId: mis[s].properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

// ---- §5 #7 AcrPull ×3（映像拉取）----
resource acrPull 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (s, i) in miSuffixes: {
  name: guid(acr.id, mis[i].id, roleAcrPull)
  scope: acr
  properties: {
    roleDefinitionId: roleAcrPull
    principalId: mis[i].properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

// ---- §5 #8 Storage Blob Data Contributor ×1（approved artifacts；scope=artifacts 容器最小面）----
resource blobContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for s in [0]: {
  name: guid(artifacts.id, mis[s].id, roleBlobDataContributor)
  scope: artifacts
  properties: {
    roleDefinitionId: roleBlobDataContributor
    principalId: mis[s].properties.principalId
    principalType: 'ServicePrincipal'
  }
}]

// ---- §5 #1 Cognitive Services OpenAI User ×3（手动生成资源；空 ID = 整行跳过）----
module foundryRbac 'csRoleAssignments.bicep' = if (!empty(foundryResourceId)) {
  scope: resourceGroup(foundrySub, foundryRg)
  name: 'foundry-openai-user'
  params: {
    accountName: foundryName
    roleDefinitionId: roleOpenAiUser
    principalIds: [
      mis[0].properties.principalId
      mis[1].properties.principalId
      mis[2].properties.principalId
    ]
    assignmentSeed: foundryResourceId
  }
}

// ---- §5 #2 Cognitive Services Speech User ×1（runtime；手动生成资源；空 ID = 跳过）----
module speechRbac 'csRoleAssignments.bicep' = if (!empty(speechResourceId)) {
  scope: resourceGroup(speechSub, speechRg)
  name: 'speech-user'
  params: {
    accountName: speechName
    roleDefinitionId: roleSpeechUser
    principalIds: [
      mis[0].properties.principalId
    ]
    assignmentSeed: speechResourceId
  }
}

output MI_RUNTIME_ID string = mis[0].id
output MI_CONTROL_ID string = mis[1].id
output MI_WORKER_ID string = mis[2].id
