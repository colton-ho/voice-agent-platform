// modules/keyvault.bicep — pipeline-infra-spec §3：vault + 4 secrets（§3.1）
// + RBAC（§5 #3 Key Vault Secrets User，role assignment 归 identities.bicep/U-401）。
// 只落碼不部署；secret 值只经 @secure() 部署参数注入——绝不落盘/入配置/入模板字面量。
// 命名/参数只引用 config_contract.py 冻结变量名（§11），不新造契约面。

@description('Key Vault 名（infra 命名；输出 KEY_VAULT_URL 对应 spec §11）')
param keyVaultName string

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

@description('§3.1 MINIMAX-API-KEY 的 secret 值（用户裁决：MiniMax 保留，G-2 已批）')
@secure()
param minimaxApiKey string

@description('§3.1 TWILIO-AUTH-TOKEN 的 secret 值（spec §11 TWILIO_AUTH_TOKEN_SECRET_NAME 指向本名）')
@secure()
param twilioAuthToken string

@description('§3.1 TWILIO-ACCOUNT-SID 的 secret 值（X2 解法：也入 Vault，env 注入值）')
@secure()
param twilioAccountSid string

@description('§3.1 AUTHSECRET 的 secret 值（B12：ephemeral fallback 退役，缺值 fail-fast）')
@secure()
param authSecretValue string

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: AZURE_LOCATION
  properties: {
    tenantId: tenant().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    // §5 #3 Key Vault Secrets User（RBAC）；§3.2 MI 经 Container Apps Key Vault 引用读取
    enableRbacAuthorization: true
  }
}

// §3.1 secret 清单 1:1（名钉死；轮换=手动流程记 U-406 runbook，不建轮换 Function）
var secrets = [
  { name: 'MINIMAX-API-KEY', value: minimaxApiKey }
  { name: 'TWILIO-AUTH-TOKEN', value: twilioAuthToken }
  { name: 'TWILIO-ACCOUNT-SID', value: twilioAccountSid }
  { name: 'AUTHSECRET', value: authSecretValue }
]

resource kvSecrets 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = [for s in secrets: {
  parent: vault
  name: s.name
  properties: {
    value: s.value
  }
}]

output KEY_VAULT_URL string = vault.properties.vaultUri
