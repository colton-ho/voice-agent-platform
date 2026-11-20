#!/usr/bin/env bash
# scripts/deploy/drain-runtime.sh — U-405 blue/green release drain tool
# (scheme: docs/azure/blue-green-release.md; leases: storage/ownership.py FC-7).
#
# Watches the FC-7 call-to-owner lease census for a revision ("blue") while it
# drains after new-call routing moved to green, and reports when it is safe to
# retire. Companion helper: scripts/deploy/drain_status.py.
#
# Safety defaults (mirroring scripts/deploy/deploy-azure.sh):
#   - default mode = what-if: read-only census + plan, nothing is mutated;
#   - --apply runs the drain wait loop; its ONLY mutation is
#     `az containerapp revision deactivate` behind --retire-when-drained and
#     only after drain-complete (zero live leases attributed to the revision);
#   - --force (forced-drain at the approved deadline) requires --apply AND
#     --confirm-forced-drain=<revision>. It attempts the announced forced-BYE
#     (FC-7 `hangup` command per live call -> owner ends the call through its
#     normal end-of-call path -> Twilio BYE; forced-drain table in the design
#     doc). It NEVER hard-kills replicas while leases are live: that drops
#     calls mid-audio (silent drop) and stays a manual, explicitly signed-off
#     last resort;
#   - this script does not grant authorization: run it against a live
#     environment only with user authorization (repo iron rule).
#
# Usage:
#   scripts/deploy/drain-runtime.sh <dev|staging|prod> <revision> [options]
#
#   <env>       deployment environment (dev|staging|prod)
#   <revision>  the BLUE revision name (the draining one; new calls go to green)
#
#   Options:
#     --apply                        run the drain wait loop (default: what-if)
#     --deadline <minutes>           deadline (default 60 >= MAX_CALL_DURATION_SECONDS
#                                    default 3600s -> natural drain always possible)
#     --poll-seconds <n>             poll interval (default 30 = FC-7 lease_ttl_seconds)
#     --retire-when-drained          deactivate the revision once drained
#     --force                        forced-drain at the deadline (needs --apply and
#                                    --confirm-forced-drain=<revision>)
#     --confirm-forced-drain=<rev>   explicit confirmation; must equal <revision>
#
# Exit codes:
#   0  drained (or what-if report printed)
#   1  usage / preflight error
#   2  deadline reached with live leases and no --force
#   3  forced-drain attempted but incomplete (transport not wired / residual leases)
set -euo pipefail

usage() {
  sed -n '2,44p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

# ---- arguments (parsed before any az/preflight so usage works offline) ----
[ $# -ge 2 ] || usage
ENV="$1"
shift
case "${ENV}" in
  dev | staging | prod) ;;
  *)
    echo "error: unknown environment '${ENV}' (expected dev|staging|prod)"
    usage
    ;;
esac
REVISION="$1"
shift

APPLY=0
FORCE=0
RETIRE=0
CONFIRM=""
DEADLINE_MIN=60
POLL_SECONDS=30
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    --retire-when-drained) RETIRE=1 ;;
    --deadline)
      [ $# -ge 2 ] || {
        echo "error: --deadline needs a value"
        usage
      }
      DEADLINE_MIN="$2"
      shift
      ;;
    --poll-seconds)
      [ $# -ge 2 ] || {
        echo "error: --poll-seconds needs a value"
        usage
      }
      POLL_SECONDS="$2"
      shift
      ;;
    --confirm-forced-drain=*)
      CONFIRM="${1#*=}"
      ;;
    *)
      echo "error: unknown argument '$1'"
      usage
      ;;
  esac
  shift
done

case "${DEADLINE_MIN}" in
  '' | *[!0-9]*)
    echo "error: --deadline must be a whole number of minutes (got '${DEADLINE_MIN}')"
    usage
    ;;
esac
case "${POLL_SECONDS}" in
  '' | *[!0-9]*)
    echo "error: --poll-seconds must be a whole number of seconds (got '${POLL_SECONDS}')"
    usage
    ;;
esac
[ "${POLL_SECONDS}" -ge 1 ] || {
  echo "error: --poll-seconds must be >= 1 (got '${POLL_SECONDS}')"
  usage
}

if [ "${FORCE}" = 1 ]; then
  [ "${APPLY}" = 1 ] || {
    echo "error: --force requires --apply (what-if mode never forces)"
    exit 1
  }
  [ "${CONFIRM}" = "${REVISION}" ] || {
    echo "error: --force requires --confirm-forced-drain=${REVISION} (explicit confirmation of the revision to force-drain)"
    exit 1
  }
elif [ -n "${CONFIRM}" ]; then
  echo "error: --confirm-forced-drain is only valid together with --force"
  usage
fi

# ---- preflight ----
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HELPER="${REPO_ROOT}/scripts/deploy/drain_status.py"
APP_NAME="ca-voicebot-${ENV}-runtime"
RG="rg-voicebot-${ENV}"

command -v az >/dev/null 2>&1 || {
  echo "error: az CLI not installed"
  exit 1
}
az account show >/dev/null 2>&1 || {
  echo "error: not logged in to Azure — run az login and select a subscription first (az account set --subscription <id>)"
  exit 1
}
command -v python3 >/dev/null 2>&1 || {
  echo "error: python3 not installed"
  exit 1
}
[ -f "${HELPER}" ] || {
  echo "error: missing helper ${HELPER}"
  exit 1
}

# owner_id = Container Apps replica name (owner convention, blue-green-release.md)
list_owners() {
  local out
  out="$(az containerapp replica list --name "${APP_NAME}" --resource-group "${RG}" \
    --revision "${REVISION}" --query "[].name" -o tsv)" || {
    echo "error: failed to list replicas for '${REVISION}' (az containerapp replica list)"
    exit 1
  }
  printf '%s\n' "${out}" | tr '\t' '\n' | grep -v '^$' || true
}

owners_csv() {
  paste -sd, -
}

run_helper() {
  PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" python3 "${HELPER}" "$@"
}

retire_revision() {
  echo "==> retiring blue revision (az containerapp revision deactivate)"
  az containerapp revision deactivate --name "${APP_NAME}" --resource-group "${RG}" \
    --revision "${REVISION}"
}

echo "==== U-405 blue/green drain ===="
echo "environment:   ${ENV}"
echo "app:           ${APP_NAME}"
echo "resource grp:  ${RG}"
echo "blue revision: ${REVISION} (draining; new calls land on green)"
if [ "${APPLY}" = 1 ]; then
  echo "mode:          apply (drain wait loop)"
else
  echo "mode:          what-if (read-only)"
fi
echo "deadline:      ${DEADLINE_MIN} min    poll: ${POLL_SECONDS}s"
if [ "${RETIRE}" = 1 ]; then
  echo "retire:        yes (--retire-when-drained, after drain-complete only)"
else
  echo "retire:        no"
fi
if [ "${FORCE}" = 1 ]; then
  echo "force:         yes (announced forced-BYE at the deadline)"
else
  echo "force:         no"
fi
echo "================================"

if [ "${APPLY}" != 1 ]; then
  # ---- what-if: read-only census + plan ----
  echo "==> what-if: read-only census (revision state + FC-7 leases; nothing mutated)"
  az containerapp revision show --name "${APP_NAME}" --resource-group "${RG}" \
    --revision "${REVISION}" \
    --query "{name:name,active:properties.active,traffic:properties.trafficWeight}" -o json || {
    echo "error: revision '${REVISION}' not found on ${APP_NAME}"
    exit 1
  }
  OWNERS="$(list_owners)"
  OWNERS_CSV="$(printf '%s\n' "${OWNERS}" | owners_csv)"
  if [ -z "${OWNERS_CSV}" ]; then
    echo "warning: no replicas for '${REVISION}' (already inactive or scaled in?) — any calls it owned are gone (owner-loss) and its leases self-reap within 30s; verify before trusting a zero census"
  fi
  run_helper census --owners "${OWNERS_CSV}" --format text
  echo ""
  echo "What --apply would do:"
  echo "  1. poll the FC-7 lease census every ${POLL_SECONDS}s until zero live leases are"
  echo "     attributed to replicas of '${REVISION}' (drain-complete criterion,"
  echo "     docs/azure/blue-green-release.md), deadline ${DEADLINE_MIN} min;"
  if [ "${RETIRE}" = 1 ]; then
    echo "  2. on drain-complete, deactivate '${REVISION}' (az containerapp revision deactivate);"
  else
    echo "  2. on drain-complete, report DRAINED (no --retire-when-drained, no deactivation);"
  fi
  echo "  3. at the deadline with live leases: report the census and the forced-drain"
  echo "     options, then exit 2."
  echo "What --force would add at the deadline (requires --apply and --confirm-forced-drain=${REVISION}):"
  echo "  - announced forced-BYE: one FC-7 'hangup' command per live call"
  echo "    (drain_status.py force-end) — the caller hears an early but normal ending;"
  echo "  - settle window of 2 polls, then recount;"
  echo "  - never deactivate with live leases (no silent drops) — residual leases"
  echo "    exit 3 with the manual last-resort steps spelled out."
  echo ""
  echo "what-if finished. Nothing was changed. To execute the drain:"
  echo "  ${0} ${ENV} ${REVISION} --apply"
  exit 0
fi

# ---- apply: the drain wait loop ----
DEADLINE_AT=$(($(date +%s) + DEADLINE_MIN * 60))
echo "==> drain wait (poll every ${POLL_SECONDS}s, deadline ${DEADLINE_MIN} min)"
while :; do
  OWNERS="$(list_owners)"
  OWNERS_CSV="$(printf '%s\n' "${OWNERS}" | owners_csv)"
  if [ -z "${OWNERS_CSV}" ]; then
    echo "warning: no replicas for '${REVISION}' — its calls are gone (owner-loss); leases self-reap within 30s"
  fi
  COUNT="$(run_helper census --owners "${OWNERS_CSV}" --format count)"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] live leases on '${REVISION}': ${COUNT} (owners: ${OWNERS_CSV:-none})"
  if [ "${COUNT}" -eq 0 ]; then
    echo "==> drain-complete: zero live leases attributed to '${REVISION}'"
    if [ "${RETIRE}" = 1 ]; then
      retire_revision
    else
      echo "blue revision is safe to retire (rerun with --retire-when-drained to do it now)"
    fi
    exit 0
  fi
  if [ "$(date +%s)" -ge "${DEADLINE_AT}" ]; then
    break
  fi
  sleep "${POLL_SECONDS}"
done

# ---- deadline reached with live leases ----
echo ""
echo "==> deadline reached with ${COUNT} live lease(s) still attributed to '${REVISION}'"
run_helper census --owners "${OWNERS_CSV}" --format text
if [ "${FORCE}" != 1 ]; then
  echo ""
  echo "Forced-drain options at the approved deadline (forced-drain table,"
  echo "docs/azure/blue-green-release.md — default posture B, no silent drops):"
  echo "  A. let the remaining calls finish — rerun with a longer --deadline (caller unaffected)"
  echo "  B. announced forced-BYE (DEFAULT) — rerun with --force --confirm-forced-drain=${REVISION}"
  echo "  C. carrier-side completion        — Twilio console/REST per CallSid (normal carrier hangup)"
  echo "  D. hard teardown (last resort, MANUAL, explicit sign-off required — drops the"
  echo "     calls listed above mid-audio):"
  echo "       az containerapp revision deactivate --name ${APP_NAME} --resource-group ${RG} --revision ${REVISION}"
  exit 2
fi

echo ""
echo "==> forced-drain (announced forced-BYE): one FC-7 'hangup' command per live call"
echo "    (owner ends the call through its normal end-of-call path -> Twilio BYE;"
echo "     the caller hears an early but normal ending — no silent drops)"
FORCE_RC=0
run_helper force-end --owners "${OWNERS_CSV}" --reason "forced-drain U-405 deadline" || FORCE_RC=$?
echo "==> settling (${POLL_SECONDS}s x 2) for calls to end and leases to release"
sleep "${POLL_SECONDS}"
sleep "${POLL_SECONDS}"
OWNERS="$(list_owners)"
OWNERS_CSV="$(printf '%s\n' "${OWNERS}" | owners_csv)"
COUNT="$(run_helper census --owners "${OWNERS_CSV}" --format count)"
echo "live leases on '${REVISION}': ${COUNT}"
if [ "${COUNT}" -eq 0 ]; then
  echo "==> drain-complete after forced-drain"
  if [ "${RETIRE}" = 1 ]; then
    retire_revision
  fi
  exit 0
fi
run_helper census --owners "${OWNERS_CSV}" --format text
echo ""
echo "==> forced-drain incomplete: ${COUNT} live lease(s) remain (force-end rc=${FORCE_RC})."
echo "    This tool will NOT kill replicas while calls are live — that would drop the"
echo "    calls above mid-audio (silent drop). Next steps:"
echo "      - if force-end reported 'no owner-addressable transport wired': the U-305"
echo "        transport seam (DRAIN_SEND_FACTORY) is not wired; wire it and rerun;"
echo "      - let them finish: rerun with a longer --deadline;"
echo "      - carrier-side completion via Twilio console/REST per CallSid;"
echo "      - last resort (MANUAL, explicit sign-off required — drops the calls above):"
echo "          az containerapp revision deactivate --name ${APP_NAME} --resource-group ${RG} --revision ${REVISION}"
exit 3
