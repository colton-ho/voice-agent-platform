#!/usr/bin/env bash
# scripts/deploy/deploy-azure.sh — U-401 Azure 部署脚本（plan §9 部署序列封装）。
#
# 安全默认（pipeline-infra-spec §4.2 边界）：
#   - 默认 what-if 预演，不落任何资源；必须显式 --apply 才执行 create；
#   - --apply 前置拦截：参数文件 REPLACE_ME 占位符未回填 / secret 环境变量缺失 → 拒绝；
#     例外：PUBLIC_*_BASE_URL 为两遍回填设计（首布后取 RUNTIME_FQDN 再回填，runbook §2），
#     首布时 FQDN 无法预知 → 只警告不拦截；
#   - secret 值只经环境变量流入（params 文件 readEnvironmentVariable），不回显、
#     不落盘、不进命令行参数；请勿以 bash -x 运行本脚本；
#   - 本脚本不代替授权——执行 az deployment 前须取得用户明示授权（plan 头铁律）。
#
# 用法:
#   scripts/deploy/deploy-azure.sh <dev|staging|prod>            # what-if 预演
#   scripts/deploy/deploy-azure.sh <dev|staging|prod> --apply    # 真正部署
#
# --apply 需要的环境变量（§3.1 四值）:
#   MINIMAX_API_KEY  TWILIO_AUTH_TOKEN  TWILIO_ACCOUNT_SID  AUTH_SECRET
set -euo pipefail

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

[ $# -ge 1 ] || usage
ENV="$1"
shift
case "$ENV" in
  dev | staging | prod) ;;
  *)
    echo "错误：未知环境 '$ENV'（只接受 dev|staging|prod）"
    usage
    ;;
esac

APPLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    *)
      echo "错误：未知参数 '$arg'"
      usage
      ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PARAMS_FILE="$REPO_ROOT/infra/bicep/params/$ENV.bicepparam"
TEMPLATE="$REPO_ROOT/infra/bicep/main.bicep"

# ---- preflight（plan §9 步骤 1）----
command -v az >/dev/null 2>&1 || {
  echo "错误：未安装 az CLI"
  exit 1
}
az account show >/dev/null 2>&1 || {
  echo "错误：未登录 Azure —— 先 az login 并选定订阅（az account set --subscription <id>）"
  exit 1
}
az bicep version >/dev/null 2>&1 || {
  echo "错误：bicep 不可用 —— 先 az bicep install"
  exit 1
}
[ -f "$PARAMS_FILE" ] || {
  echo "错误：缺参数文件 $PARAMS_FILE"
  exit 1
}
[ -f "$TEMPLATE" ] || {
  echo "错误：缺模板 $TEMPLATE"
  exit 1
}

# 部署区域取参数文件 AZURE_LOCATION（单一事实来源）
LOCATION="$(sed -n "s/^param AZURE_LOCATION = '\(.*\)'.*/\1/p" "$PARAMS_FILE" | head -1)"
[ -n "$LOCATION" ] || {
  echo "错误：无法从 $PARAMS_FILE 解析 AZURE_LOCATION"
  exit 1
}

echo "==== U-401 部署计划 ===="
echo "环境:       $ENV"
echo "订阅:       $(az account show --query name -o tsv)"
echo "区域:       $LOCATION"
echo "模板:       $TEMPLATE"
echo "参数文件:   $PARAMS_FILE"
echo "将创建/更新（plan §1 拓扑）:"
echo "  - 资源组 rg-voicebot-$ENV"
echo "  - Cosmos cosmos-voicebot-$ENV（voicebot 库 + 8 容器，§1.1–1.4）"
echo "  - Service Bus sb-voicebot-$ENV + postcall-jobs 队列（§2.1 六值）"
echo "  - Key Vault kv-voicebot-$ENV + 4 secrets（§3.1）"
echo "  - MI ×3 mi-voicebot-$ENV-{runtime,control,worker} + RBAC 行 1–8（§5）"
echo "  - ACR acrvinebot$ENV"
echo "  - Container Apps 环境 cae-voicebot-$ENV + app（runtime/worker；control 视 includeControlApp）"
echo "  - Blob stvoicebot$ENV（私有 artifacts 容器，R-A-008）"
echo "  - Log Analytics + App Insights + action group + 3 告警（DLQ/app 5xx/Cosmos 429）"
echo "  - （可选）自定义域名托管证书 / 订阅预算（参数非空时）"
echo "========================="

DEPLOY_NAME="u401-voicebot-$ENV"
# sub 在命令形制里（az deployment sub create/what-if），不进参数数组——进数组会拼出
# az deployment create sub ... 被 CLI 解析期拒（B1 复审修复）
DEPLOY_ARGS=(--name "$DEPLOY_NAME" --location "$LOCATION" --parameters "$PARAMS_FILE")

if [ "$APPLY" = 1 ]; then
  # --apply 安全闸：占位符与 secret 缺一不可（PUBLIC_* 两遍回填除外，见文件头）
  # 只匹配 param 行（排除提及 REPLACE_ME 的注释）；grep -e 多模式可移植（BSD grep 无 BRE 交替）
  leftover="$(grep '^param .*REPLACE_ME' "$PARAMS_FILE" | grep -v -e '^param PUBLIC_CONTROL_BASE_URL' -e '^param PUBLIC_MEDIA_BASE_URL' || true)"
  if [ -n "$leftover" ]; then
    echo "错误：$PARAMS_FILE 仍有 REPLACE_ME 占位符未回填（见 runbook §1.2 参数回填表；PUBLIC_* 两遍回填除外）:"
    echo "$leftover"
    exit 1
  fi
  if grep -Eq '^param PUBLIC_(CONTROL|MEDIA)_BASE_URL = .*REPLACE_ME' "$PARAMS_FILE"; then
    echo "警告：PUBLIC_*_BASE_URL 仍为占位符——首布后按 runbook §2 步骤 4/6 取 RUNTIME_FQDN 回填并重跑 --apply"
  fi
  for v in MINIMAX_API_KEY TWILIO_AUTH_TOKEN TWILIO_ACCOUNT_SID AUTH_SECRET; do
    # 只验非空，值不经 shell 展开打印（防 set -x 泄露）
    if ! printenv "$v" 2>/dev/null | grep -q '.'; then
      echo "错误：secret 环境变量 $v 缺失或为空（§3.1；值不回显）"
      exit 1
    fi
  done
  echo "==> 执行部署（az deployment sub create）"
  az deployment sub create "${DEPLOY_ARGS[@]}"
  echo "==> 完成。部署输出:"
  az deployment sub show --name "$DEPLOY_NAME" --query properties.outputs -o json
else
  echo "==> what-if 预演（az deployment sub what-if，不落资源）"
  az deployment sub what-if "${DEPLOY_ARGS[@]}"
  echo ""
  echo "预演结束。确认变更无误且已取得用户明示授权后执行："
  echo "  MINIMAX_API_KEY=... TWILIO_AUTH_TOKEN=... TWILIO_ACCOUNT_SID=... AUTH_SECRET=... \\"
  echo "    $0 $ENV --apply"
fi
