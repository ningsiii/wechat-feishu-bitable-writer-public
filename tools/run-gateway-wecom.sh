#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

ENV_FILE="${SMALLBIZ_ENV_FILE:-solutions/smallbiz/.env}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

export PATH="$HOME/.openclaw/bin:$HOME/.openclaw/tools/node/bin:$PATH"

# Avoid plugin load failures in WSL due to Windows temp dir permission issues.
export TMPDIR="${TMPDIR:-/tmp}"
export TMP="${TMP:-$TMPDIR}"
export TEMP="${TEMP:-$TMPDIR}"

# If the gateway previously crashed, the PID-based lock can linger and block restart.
# Allow multi-gateway to bypass the lock; port binding still guarantees only one listener.
export OPENCLAW_ALLOW_MULTI_GATEWAY="${OPENCLAW_ALLOW_MULTI_GATEWAY:-1}"

# Use the "safe" launcher to avoid WSL/networkInterfaces crashes and to make
# startup behavior consistent across terminals (PATH/env).
#
# Also: do not force the dev port (19001). Unless you are running with --dev,
# OpenClaw's default gateway port is 18789; forcing 19001 makes CLI commands
# (health/agent/tui) miss the running gateway and looks like "it's not working".
exec tools/openclaw-safe.sh gateway run --bind loopback --auth none
