// modules/network.bicep — U-401 网络面，决策 D2 刻意收窄（plan §7 D2）：
// 只做 ACA 托管 TLS 自定义域名（managed certificate，免费自动续期）；
// VNet / 私有端点 / DNS 区不预建——挂 V11 前检查项（当前拓扑=Twilio 公网
// WS 必须保留公网端点，plan §7 D2/§10；防过度设计）。
//
// 两步部署（ACA 托管证书的域名控制验证先于签发）：
//   ① 首布：controlHostName/mediaHostName 留空 → 本模块零资源，app 用默认 FQDN；
//   ② DNS：按 runbook 把 CNAME 指到 app 默认 FQDN；
//   ③ 回填 host 参数重跑 → 证书签发 + containerApps 侧 SNI 绑定（main 保序）。
// 只落码不部署（pipeline-infra-spec §4.2 边界）。

@description('spec §11 AZURE_LOCATION — validated Azure region')
param AZURE_LOCATION string

@description('Container Apps 环境名（cae-voicebot-<env>；证书挂在环境下）')
param managedEnvironmentName string

@description('部署环境名（证书命名 cert-control-<env> / cert-media-<env>，与 main.bicep resourceId 计算同步）')
param environmentName string

@description('control 面自定义主机名（如 voice.example.com）；留空 = 不建证书（首布形态）')
param controlHostName string = ''

@description('media 面自定义主机名（WS 入口，如 media.example.com）；留空 = 不建证书（首布形态）')
param mediaHostName string = ''

@description('托管证书域名控制验证方式（ACA managed certificate 支持项）')
@allowed(['CNAME', 'HTTP', 'TXT'])
param domainControlValidation string = 'CNAME'

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: managedEnvironmentName
}

resource controlCert 'Microsoft.App/managedEnvironments/managedCertificates@2023-05-01' = if (!empty(controlHostName)) {
  parent: managedEnvironment
  name: 'cert-control-${environmentName}'
  location: AZURE_LOCATION
  properties: {
    subjectName: controlHostName
    domainControlValidation: domainControlValidation
  }
}

resource mediaCert 'Microsoft.App/managedEnvironments/managedCertificates@2023-05-01' = if (!empty(mediaHostName)) {
  parent: managedEnvironment
  name: 'cert-media-${environmentName}'
  location: AZURE_LOCATION
  properties: {
    subjectName: mediaHostName
    domainControlValidation: domainControlValidation
  }
}

output CONTROL_CERTIFICATE_ID string = empty(controlHostName) ? '' : controlCert.id
output MEDIA_CERTIFICATE_ID string = empty(mediaHostName) ? '' : mediaCert.id
