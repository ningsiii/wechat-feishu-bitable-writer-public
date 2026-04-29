#!/usr/bin/env bash
set -euo pipefail

ROOT="${OPENCLAW_ROOT:-$HOME/.openclaw}"
NODE_BIN="${OPENCLAW_NODE_BIN:-$ROOT/tools/node/bin/node}"
ENTRY_JS="${OPENCLAW_ENTRY_JS:-$ROOT/lib/node_modules/openclaw/dist/entry.js}"
PATCH="${OPENCLAW_PATCH_OS_NETWORK:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/patch-os-network.mjs}"

# In some WSL setups, the default temp dir maps to a Windows path that can
# cause permission errors for OpenClaw plugins (jiti transpilation cache).
export TMPDIR="${TMPDIR:-/tmp}"
export TMP="${TMP:-$TMPDIR}"
export TEMP="${TEMP:-$TMPDIR}"

exec "$NODE_BIN" --import "$PATCH" "$ENTRY_JS" "$@"
