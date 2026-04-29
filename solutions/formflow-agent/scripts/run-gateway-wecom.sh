#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../../.."

ENV_FILE="${FORMFLOW_ENV_FILE:-solutions/formflow-agent/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export SMALLBIZ_ENV_FILE="${SMALLBIZ_ENV_FILE:-$ENV_FILE}"
export SMALLBIZ_BINDINGS_FILE="${SMALLBIZ_BINDINGS_FILE:-solutions/formflow-agent/config/bindings.json}"
export SMALLBIZ_DRAFT_FILE="${SMALLBIZ_DRAFT_FILE:-solutions/formflow-agent/data/draft.json}"
export SMALLBIZ_LEDGER_PATH="${SMALLBIZ_LEDGER_PATH:-solutions/formflow-agent/data/ledger.jsonl}"
export SMALLBIZ_PROFILE="${SMALLBIZ_PROFILE:-single}"
export SMALLBIZ_ENABLE_ORGANIZE_REMINDER="${SMALLBIZ_ENABLE_ORGANIZE_REMINDER:-0}"

# Ensure runtime workspace uses formflow-agent prompt set, not legacy smallbiz.
solutions/formflow-agent/scripts/install-workspace.sh >/dev/null 2>&1 || true

exec tools/run-gateway-wecom.sh
