// modules/monitor.bicep — U-401 可观测面（plan §1）：Log Analytics + App Insights
// （workspace-based）+ action group（邮件接收者参数化）+ 3 条指标告警。
// D5：订阅级 budget 是「部署层级差异」的可选参数，归 main.bicep（避免模块越权）。
// 告警阈值参数化（plan §6：dev 低成本门槛；staging/prod 对应 cost-analysis §8 陷阱项）。
// 只落码不部署（pipeline-infra-spec §4.2 边界）。

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

@description('部署环境名（log-voicebot-<env> / appi-voicebot-<env>，plan §2）')
param environmentName string

@description('§5 #4 scope 名——Cosmos 429 告警目标帐户')
param cosmosAccountName string

@description('§2.1 namespace 名——DLQ 告警目标 postcall-jobs 队列')
param servicebusNamespaceName string

@description('告警 action group 邮件接收者（D5 参数化）')
param actionGroupEmail string

@description('action group 短名（邮件通知中标识来源）')
param actionGroupShortName string = 'vbalert'

@description('DLQ 深度告警阈值（> 此值触发；0 = 有死信即报）')
param dlqThreshold int = 0

@description('App 失败请求（5xx/异常，App Insights requests/failed）告警阈值（5 分钟计数）')
param failedRequestsThreshold int = 10

@description('Cosmos 429 限流告警阈值（5 分钟计数；官方建议 100 起步，实测后调）')
param cosmos429Threshold int = 100

@description('Log Analytics 保留天数')
param logRetentionDays int = 30

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-voicebot-${environmentName}'
  location: AZURE_LOCATION
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: logRetentionDays
    // cost-analysis §8-3：日志摄入按 GB 计费——不预开 Verbose 诊断
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-voicebot-${environmentName}'
  location: AZURE_LOCATION
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: workspace.id
  }
}

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-voicebot-${environmentName}'
  location: 'global'
  properties: {
    groupShortName: actionGroupShortName
    enabled: true
    emailReceivers: [
      {
        name: 'ops-email'
        emailAddress: actionGroupEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// 告警 1：postcall-jobs DLQ 深度（§2.1 死信可测性；U-406 清扫的告警前置）
resource alertDlq 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-dlq-${environmentName}'
  location: 'global'
  properties: {
    description: 'postcall-jobs DLQ 出现死信（重试耗尽/poison）——按 §2.2 I-4 排查 summary_status'
    severity: 2
    enabled: true
    scopes: [
      resourceId('Microsoft.ServiceBus/namespaces/queues', servicebusNamespaceName, 'postcall-jobs')
    ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'DeadLetterCount'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'DeadLetterMessageCount'
          dimensions: [
            {
              name: 'EntityName'
              operator: 'Include'
              values: [
                'postcall-jobs'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: dlqThreshold
          timeAggregation: 'Maximum'
        }
      ]
    }
  }
}

// 告警 2：app 失败请求（5xx/异常面，App Insights requests/failed）
resource alertAppFailed 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-app-failed-${environmentName}'
  location: 'global'
  properties: {
    description: 'control/runtime 失败请求超阈值（5xx/未捕获异常）——看 App Insights 失败请求'
    severity: 2
    enabled: true
    scopes: [
      appInsights.id
    ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'FailedRequests'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'requests/failed'
          operator: 'GreaterThan'
          threshold: failedRequestsThreshold
          timeAggregation: 'Total'
        }
      ]
    }
  }
}

// 告警 3：Cosmos 429 限流（cost-analysis §8-1 陷阱项；§1.4 吞吐起始值实测后调）
resource alertCosmos429 'Microsoft.Insights/metricAlerts@2018-03-01' = {
  name: 'alert-cosmos-429-${environmentName}'
  location: 'global'
  properties: {
    description: 'Cosmos TotalRequests StatusCode=429 超阈值（吞吐不足/热分区哨兵 _none_）'
    severity: 3
    enabled: true
    scopes: [
      resourceId('Microsoft.DocumentDB/databaseAccounts', cosmosAccountName)
    ]
    evaluationFrequency: 'PT1M'
    windowSize: 'PT5M'
    actions: [
      {
        actionGroupId: actionGroup.id
      }
    ]
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [
        {
          name: 'ThrottledRequests'
          criterionType: 'StaticThresholdCriterion'
          metricName: 'TotalRequests'
          dimensions: [
            {
              name: 'StatusCode'
              operator: 'Include'
              values: [
                '429'
              ]
            }
          ]
          operator: 'GreaterThan'
          threshold: cosmos429Threshold
          timeAggregation: 'Total'
        }
      ]
    }
  }
}

output APPLICATIONINSIGHTS_CONNECTION_STRING string = appInsights.properties.ConnectionString
output LOG_ANALYTICS_WORKSPACE_ID string = workspace.id
output LOG_ANALYTICS_CUSTOMER_ID string = workspace.properties.customerId
output ACTION_GROUP_ID string = actionGroup.id
