#!/usr/bin/env bash
# scripts/deploy/set-secrets.sh — U-401 KV secret 写入/轮换（plan §9 步骤 5；§3.1 四值）。
#
# 值来自环境变量：不回显、不落盘、不进命令行回显（az 返回体只取 name）。
# 用途：① 首次部署后灌入；② 手动轮换（pipeline-infra-spec §3.2——无自动轮换）。
# 注意：az CLI 的 --value 会短暂出现在本机进程表（ps）；在可信操作机上执行。
# 请勿以 bash -x 运行本脚本。
#
# 用法:
#   MINIMAX_API_KEY=... TWILIO_AUTH_TOKEN=... TWILIO_ACCOUNT_SID=... AUTH_SECRET=... \
#     scripts/deploy/set-secrets.sh <dev|staging|prod>
set -euo pipefail

usage() {
  sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

[ $# -eq 1 ] || usage
ENV="$1"
case "$ENV" in
  dev | staging | prod) ;;
  *)
    echo "错误：未知环境 '$ENV'（只接受 dev|staging|prod）"
    usage
    ;;
esac

command -v az >/dev/null 2>&1 || {
  echo "错误：未安装 az CLI"
  exit 1
}
az account show >/dev/null 2>&1 || {
  echo "错误：未登录 Azure —— 先 az login"
  exit 1
}

VAULT="kv-voicebot-$ENV"

# §3.1 清单：环境变量名 → KV secret 名（名钉死，spec §11 TWILIO_AUTH_TOKEN_SECRET_NAME 指向 TWILIO-AUTH-TOKEN）
SECRET_MAP=(
  "MINIMAX_API_KEY MINIMAX-API-KEY"
  "TWILIO_AUTH_TOKEN TWILIO-AUTH-TOKEN"
  "TWILIO_ACCOUNT_SID TWILIO-ACCOUNT-SID"
  "AUTH_SECRET AUTHSECRET"
)

for entry in "${SECRET_MAP[@]}"; do
  read -r env_name kv_name <<<"$entry"
  if ! printenv "$env_name" 2>/dev/null | grep -q '.'; then
    echo "错误：secret 环境变量 $env_name 缺失或为空"
    exit 1
  fi
done

echo "==> 写入 ${VAULT}（4 个 secret；值不回显）"
for entry in "${SECRET_MAP[@]}"; do
  read -r env_name kv_name <<<"$entry"
  # --query name 只回显 secret 名；值不进输出/日志/磁盘
  result="$(az keyvault secret set \
    --vault-name "$VAULT" \
    --name "$kv_name" \
    --value "$(printenv "$env_name")" \
    --query name -o tsv)"
  echo "  已写入: $result"
done
echo "==> 完成。ACA 侧经 keyVaultUrl 引用自动取新值（无版本钉死时 ≤30 分钟刷新并重启副本）"
