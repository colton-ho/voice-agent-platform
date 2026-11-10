// modules/acr.bicep — U-401 映像仓库（plan §2：acrvinebot<env>，去连字符，ACR 命名限制）。
// AcrPull 授权归 identities.bicep（§5 #7）；admin 凭据不启用——拉取只走 MI。
// 只落码不部署（pipeline-infra-spec §4.2 边界：写 Bicep ≠ 授权云上 provisioning）。

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

@description('ACR 名（infra 命名 acrvinebot<env>，非 §11 契约面）')
param acrName string

@description('ACR SKU（dev Basic 起步；异地复制/更高吞吐需求上来再调，cost-analysis §8 不预付）')
@allowed(['Basic', 'Standard', 'Premium'])
param sku string = 'Basic'

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: AZURE_LOCATION
  sku: {
    name: sku
  }
  properties: {
    // 映像拉取走 AcrPull（MI）；admin 凭据不启用
    adminUserEnabled: false
    // 公网拉取（D2：私有端点不预建，挂 V11 前检查项）
    publicNetworkAccess: 'Enabled'
    // 注：未打 tag 的映像清理策略（retentionPolicy）仅 Premium SKU 可配，
    // Basic 起步不预付（cost-analysis §8）；升级 Premium 时随运维补配。
  }
}

output ACR_NAME string = registry.name
output ACR_LOGIN_SERVER string = registry.properties.loginServer
output ACR_ID string = registry.id
