#!/usr/bin/env bash
# scripts/migration/migrate.sh — thin dispatcher for the U-404 §9.3 stage CLIs
# (spec §9.3 migration procedure; runbook: docs/azure/migration-runbook.md).
#
# Safety defaults (house style, deploy-azure.sh):
#   - every stage defaults to dry-run / in-memory targets; external writes
#     need an explicit --apply (stages 3/5/6) or --yes (stage 7 rollback);
#   - `--target real` is the cutover path and is D-8 gated: nothing runs
#     against AWS or Azure without explicit user authorization;
#   - no secrets are echoed; AWS/Azure credentials flow through the standard
#     SDK chains (env / managed identity), never through this script.
#
# Usage:
#   scripts/migration/migrate.sh export      --source users --workdir runs/cutover
#   scripts/migration/migrate.sh transform   --source users --input ... --out ...
#   scripts/migration/migrate.sh test-import --input ... [--apply] [--target real]
#   scripts/migration/migrate.sh reconcile   --snapshots runs/cutover/snapshots
#   scripts/migration/migrate.sh delta       --source users --workdir runs/cutover
#   scripts/migration/migrate.sh full-run    --workdir runs/cutover [--target real --apply]
#   scripts/migration/migrate.sh rollback    --manifest runs/cutover/manifest.json [--yes]
#
# (Stage arguments are passed through verbatim — see `migrate.sh <stage> --help`.)
set -euo pipefail

usage() {
  sed -n '2,23p' "$0" | sed 's/^# \{0,1\}//'
  exit 1
}

[ $# -ge 1 ] || usage
STAGE="$1"
shift
case "${STAGE}" in
  export) MODULE="export" ;;
  transform) MODULE="transform" ;;
  test-import) MODULE="test_import" ;;
  reconcile) MODULE="reconcile" ;;
  delta) MODULE="delta" ;;
  full-run) MODULE="full_run" ;;
  rollback) MODULE="rollback" ;;
  *)
    echo "error: unknown stage '${STAGE}' (expected export|transform|test-import|reconcile|delta|full-run|rollback)"
    usage
    ;;
esac

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
export PYTHONPATH="${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
cd "${REPO_ROOT}"

# Interpreter: explicit ${PYTHON} wins; otherwise the repo venv when present
# (runtime deps: yaml/boto3/... — see requirements.txt); else bare python3.
if [ -n "${PYTHON:-}" ]; then
  PY="${PYTHON}"
elif [ -x "${REPO_ROOT}/.venv/bin/python" ]; then
  PY="${REPO_ROOT}/.venv/bin/python"
else
  PY="python3"
fi

exec "${PY}" -m "scripts.migration.${MODULE}" "$@"
