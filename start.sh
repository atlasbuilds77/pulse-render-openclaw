#!/usr/bin/env bash
set -euo pipefail
mkdir -p /data/pulse /data/agents/pulse/sessions
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/data}"
export OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/app/openclaw.json}"
export OPENCLAW_GATEWAY_PORT="${OPENCLAW_GATEWAY_PORT:-18789}"

python3 /app/pulse_api.py &
API_PID=$!
node /app/dist/index.js gateway --allow-unconfigured --port "${OPENCLAW_GATEWAY_PORT}" --bind loopback &
GATEWAY_PID=$!
cleanup() { kill "$API_PID" "$GATEWAY_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
python3 /app/pulse_proxy.py
