// modules/csRoleAssignments.bicep — U-401 §5 行 1/2 的跨 RG 派发载体（内部小模块）。
// 为什么存在：BCP139 要求资源作用域=文件作用域，跨 RG 的 role assignment 必须经
// module 落地。Foundry/AOAI 与 Speech 是用户手动建立资源（plan §5 G-2），可能
// 在本 RG 也可能在别的 RG——统一以 resourceGroup 作用域模块派发，两种情形都覆盖。
// 两个目标同为 Microsoft.CognitiveServices/accounts 类型（Foundry 与 Speech 皆是）。
// 只落码不部署（pipeline-infra-spec §4.2 边界）。

targetScope = 'resourceGroup'

@description('Cognitive Services 帐户名（Foundry/AOAI 或 Speech）')
param accountName string

@description('内置角色定义 ID（§5 #1 OpenAI User / #2 Speech User）')
param roleDefinitionId string

@description('主体 MI principalId 列表')
param principalIds array

@description('role assignment name 的 guid 种子（用目标资源 ID，防跨目标命名冲突）')
param assignmentSeed string

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: accountName
}

resource assignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for p in principalIds: {
  name: guid(assignmentSeed, p, roleDefinitionId)
  scope: account
  properties: {
    roleDefinitionId: roleDefinitionId
    principalId: p
    principalType: 'ServicePrincipal'
  }
}]
