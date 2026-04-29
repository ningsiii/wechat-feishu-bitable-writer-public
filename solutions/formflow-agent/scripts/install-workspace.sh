#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../../.."

WORKSPACE_DIR="${OPENCLAW_WORKSPACE_DIR:-$HOME/.openclaw/workspace}"
REPO_ROOT="$(pwd)"

mkdir -p "$WORKSPACE_DIR"

tmp="$(mktemp)"
cp -f solutions/formflow-agent/openclaw-workspace/AGENTS.md "$tmp"
sed -i "s|__FORMFLOW_REPO_ROOT__|$REPO_ROOT|g" "$tmp"
cp -f "$tmp" "$WORKSPACE_DIR/AGENTS.md"
rm -f "$tmp"

cp -f solutions/formflow-agent/openclaw-workspace/IDENTITY.md "$WORKSPACE_DIR/IDENTITY.md"

echo "Installed formflow-agent workspace to: $WORKSPACE_DIR"
echo "Next: run solutions/formflow-agent/scripts/run-gateway-wecom.sh"

