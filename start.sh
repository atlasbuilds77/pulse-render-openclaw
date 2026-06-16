#!/usr/bin/env bash
set -euo pipefail
mkdir -p /data/pulse
export OPENCLAW_STATE_DIR="${OPENCLAW_STATE_DIR:-/data}"
export OPENCLAW_CONFIG_PATH="${OPENCLAW_CONFIG_PATH:-/app/openclaw.json}"
export OPENCLAW_GATEWAY_PORT="${PORT:-3000}"

if [[ -n "${PULSE_CODEX_AUTH_B64:-}" ]]; then
  mkdir -p /data/agents/pulse/agent /data/agents/main/agent
  printf '%s' "$PULSE_CODEX_AUTH_B64" | base64 -d > /data/agents/pulse/agent/auth-profiles.json
  cp /data/agents/pulse/agent/auth-profiles.json /data/agents/main/agent/auth-profiles.json
  chmod 600 /data/agents/pulse/agent/auth-profiles.json /data/agents/main/agent/auth-profiles.json
  echo "[start] Codex auth profile synced to /data"
fi
python3 /app/pulse_api.py &
API_PID=$!
cleanup() { kill "$API_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
node /app/dist/index.js gateway --allow-unconfigured --port "${PORT:-3000}" --bind lan
