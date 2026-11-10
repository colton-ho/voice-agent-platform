// modules/blob.bicep — U-401 Blob 帐户（plan §2：stvoicebot<env>vap，去连字符；
// 命名偏差 2026-11-11：stvoicebot<env> 全局唯一命名空间被第三方占用，加 vap 后缀）。
// R-A-008：公共访问禁用（帐户级 + 容器级双锁）；私有容器 artifacts
// = approved artifacts 最小面（§5 #8）。RECORDING_ENABLED=false（A10），不建录音容器。
// 只落码不部署（pipeline-infra-spec §4.2 边界）。

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

@description('Blob 帐户名（infra 命名 stvoicebot<env>vap，非 §11 契约面）')
param storageAccountName string

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: storageAccountName
  location: AZURE_LOCATION
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    // R-A-008：帐户级公共访问禁用
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource artifacts 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: blobService
  name: 'artifacts'
  properties: {
    // R-A-008 双保险：容器级也锁死
    publicAccess: 'None'
  }
}

output BLOB_ACCOUNT_URL string = storage.properties.primaryEndpoints.blob
output BLOB_ACCOUNT_NAME string = storage.name
output BLOB_ACCOUNT_ID string = storage.id
