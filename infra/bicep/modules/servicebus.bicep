// modules/servicebus.bicep — pipeline-infra-spec §2：Service Bus 最小集
// 单队列 postcall-jobs（F8：仅此一作业，不建通用消息层）。
// 只落碼不部署；六项配置显式钉死（V6 逐项核对）——TTL 不靠默认。
// 命名/参数只引用 config_contract.py 冻结变量名（§11），不新造契约面。

@description('spec §11 SERVICEBUS_NAMESPACE — namespace 名（§2.1 sb-voicebot-<env>）。契约值=短名（消费侧 storage/servicebus.py 拼 .servicebus.windows.net）')
param SERVICEBUS_NAMESPACE string

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

resource namespace 'Microsoft.ServiceBus/namespaces@2021-11-01' = {
  name: SERVICEBUS_NAMESPACE
  location: AZURE_LOCATION
  // §2.1 属性组（含 enableDuplicateDetection 显式项）仅 Standard+ 可声明
  sku: {
    name: 'Standard'
    tier: 'Standard'
  }
  properties: {}
}

resource postcallQueue 'Microsoft.ServiceBus/namespaces/queues@2021-11-01' = {
  parent: namespace
  // 冻结名：storage/servicebus.py POSTCALL_QUEUE（§2.1；不设 env 变量，不新造契约面）
  name: 'postcall-jobs'
  properties: {
    // §2.1 六值显式：过期消息不残留到月底；过期进 DLQ 可观测而非静默丢弃
    defaultMessageTimeToLive: 'P7D'
    enableDeadLetteringOnMessageExpiration: true
    // lock > worker 单次处理节奏（30s 总结超时 + 写回）
    lockDuration: 'PT1M'
    // 重试上限，超限自动死信（消费侧 I-4：最后一次投递必落 failed 终态）
    maxDeliveryCount: 5
    // 冪等在消费侧按 job_id（§2.2）——broker 去重窗口语义不可靠，不叠加
    enableDuplicateDetection: false
    // DLQ 是内建 $DeadLetterQueue，无需单独资源（U-406 清扫属 Phase E）
  }
}

output SERVICEBUS_NAMESPACE string = namespace.name
