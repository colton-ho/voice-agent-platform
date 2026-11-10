// modules/cosmos.bicep — pipeline-infra-spec §1：database voicebot + 8 容器
// + CI-1 複合索引 + defaultTtl:-1 + Session + 連續備份（§1.4）。
// 只落碼不部署（#17 邊界：寫 Bicep ≠ 授權雲上 provisioning）。
// 命名/參數只引用 config_contract.py 冻结变量名（§11），不新造契约面；
// 吞吐/備份是 §1.4 部署參數（workload-driven，起始值實測後調）。

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

@description('spec §11 COSMOS_DATABASE — Cosmos database name（§1.1 钉 voicebot）')
param COSMOS_DATABASE string = 'voicebot'

@description('Cosmos 账户名（infra 命名，非 §11 契约面）')
param accountName string

@description('§1.4 dev：database 级 shared RU/s（8 容器共用）')
param sharedDatabaseThroughput int = 400

@description('§1.4 staging/prod：calls 容器族 autoscale 上限（1000–4000）；0 = 并入 shared（dev 形态）')
@minValue(0)
param callsFamilyAutoscaleMax int = 0

@description('§1.4 连续备份：dev=Continuous7Days，staging/prod=Continuous30Days（U-401 参数）')
@allowed(['Continuous7Days', 'Continuous30Days'])
param continuousBackupTier string = 'Continuous7Days'

resource account 'Microsoft.DocumentDB/databaseAccounts@2024-05-15' = {
  name: accountName
  location: AZURE_LOCATION
  kind: 'GlobalDocumentDB'
  properties: {
    databaseAccountOfferType: 'Standard'
    // §1.4：Session（单写区域性价比最优；Strong 不採）
    consistencyPolicy: {
      defaultConsistencyLevel: 'Session'
    }
    locations: [
      {
        locationName: AZURE_LOCATION
        failoverPriority: 0
        isZoneRedundant: false
      }
    ]
    backupPolicy: {
      type: 'Continuous'
      continuousModeProperties: {
        tier: continuousBackupTier
      }
    }
  }
}

resource database 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases@2024-05-15' = {
  parent: account
  name: COSMOS_DATABASE
  properties: {
    resource: {
      id: COSMOS_DATABASE
    }
    options: {
      throughput: sharedDatabaseThroughput
    }
  }
}

// §1.1 分区键落定（id 形制是应用侧约定，见各 store；此处只有 PK）。
// demos：常量分区 /type（值恒 "demo"，X3 裁量——List=单分区查询）；
// calls：/caller_id（匿名呼叫者落哨兵 "_none_"，潜在热分区只挂监控不预建分片）。
var containers = [
  { name: 'users', pk: '/username' }
  { name: 'demos', pk: '/type' }
  { name: 'voices', pk: '/provider' }
  { name: 'activity', pk: '/day' }
  { name: 'calls', pk: '/caller_id' }
  { name: 'call_events', pk: '/call_id' }
  { name: 'transcripts', pk: '/call_id' }
  { name: 'usage', pk: '/call_id' }
]

// §1.4 calls 容器族（calls/call_events/transcripts/usage）可独立 autoscale
var callsFamily = ['calls', 'call_events', 'transcripts', 'usage']

resource sqlContainers 'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers@2024-05-15' = [for c in containers: {
  parent: database
  name: c.name
  properties: {
    resource: union({
      id: c.name
      partitionKey: {
        paths: [c.pk]
        kind: 'Hash'
        version: 2
      }
      // §1.3-1：defaultTtl -1（不靠容器默认），逐 item 显式 ttl=expires_at-now
      defaultTtl: -1
    }, c.name == 'calls' ? {
      indexingPolicy: {
        automatic: true
        indexingMode: 'consistent'
        includedPaths: [
          {
            path: '/*'
          }
        ]
        excludedPaths: [
          {
            path: '/"_etag"/?'
          }
        ]
        // §1.2 CI-1：三条 Scan 路径收敛单一复合索引（started_at DESC, id ASC）
        // [UNVERIFIED] 系统 /id 进复合索引限制 → V5 前置检查项；不支持时以业务去重键替代
        compositeIndexes: [
          [
            {
              path: '/started_at'
              order: 'descending'
            }
            {
              path: '/id'
              order: 'ascending'
            }
          ]
        ]
      }
    } : {})
    // §1.4 calls 容器族独立 autoscale（0 = 并入 database shared）
    options: (callsFamilyAutoscaleMax > 0 && contains(callsFamily, c.name)) ? {
      autoscaleSettings: {
        maxThroughput: callsFamilyAutoscaleMax
      }
    } : {}
  }
}]

output COSMOS_ENDPOINT string = account.properties.documentEndpoint
