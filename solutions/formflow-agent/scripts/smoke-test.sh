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

if [[ -z "${SMALLBIZ_SMOKE_TABLE_URL:-}" ]]; then
  echo "Missing env SMALLBIZ_SMOKE_TABLE_URL (a Feishu Bitable /base/ or /wiki/ URL)." >&2
  exit 2
fi

SMOKE_TEXT="${SMALLBIZ_SMOKE_TEXT:-老王 一个西瓜}"

echo "[1/3] Register table"
python3 solutions/formflow-agent/skills/formflow-feishu/scripts/table_registry.py   --bindings "$SMALLBIZ_BINDINGS_FILE"   --env-file "$ENV_FILE"   add --url "$SMALLBIZ_SMOKE_TABLE_URL" --timeout 30 >/tmp/formflow-smoke-add.json

echo "[2/3] Ingest one message (pending)"
python3 solutions/formflow-agent/skills/formflow-router/scripts/dispatch.py   --text "$SMOKE_TEXT"   --bindings "$SMALLBIZ_BINDINGS_FILE"   --draft "$SMALLBIZ_DRAFT_FILE"   --env-file "$ENV_FILE"   --timeout 60 >/tmp/formflow-smoke-ingest.json

echo "[3/3] Export ledger"
python3 solutions/formflow-agent/skills/formflow-ops/scripts/ops.py   --ledger "$SMALLBIZ_LEDGER_PATH"   ledger_export --days 0 --out-dir "${SMALLBIZ_EXPORT_DIR:-solutions/formflow-agent/exports}"   >/tmp/formflow-smoke-ledger.json

echo "OK"
echo "Outputs:"
echo "- /tmp/formflow-smoke-add.json"
echo "- /tmp/formflow-smoke-ingest.json"
echo "- /tmp/formflow-smoke-ledger.json"
