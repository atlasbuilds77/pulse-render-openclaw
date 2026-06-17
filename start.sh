#!/usr/bin/env bash
set -euo pipefail
mkdir -p /data/pulse /data/agents/pulse/sessions
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/data}"
export OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/app/openclaw.json}"
export OPENCLAW_GATEWAY_PORT="${PORT:-3000}"

# Render secrets only exist at runtime, and the live state dir is /data.
# Rebuild the plugin registry here so the Discord channel plugin is active for the gateway.
echo "[start] repairing OpenClaw runtime/plugin state in ${OPENCLAW_STATE_DIR}"
openclaw doctor --fix || echo "[start] warning: openclaw doctor --fix reported issues; continuing gateway boot"

python3 /app/pulse_api.py &
API_PID=$!
cleanup() { kill "$API_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
node /app/dist/index.js gateway --allow-unconfigured --port "${PORT:-3000}" --bind lan
