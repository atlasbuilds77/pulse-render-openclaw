# Pulse Render OpenClaw

Render-hosted OpenClaw Gateway for Pulse Discord agent.

- OpenClaw gateway on `$PORT`
- Local restricted Pulse API on `127.0.0.1:8787`
- Secrets via Render env: `DISCORD_BOT_TOKEN`, `OPENAI_API_KEY`, `POLYGON_API_KEY`, `TITAN_URL`, optional `TITAN_API_KEY`, `OPENCLAW_GATEWAY_TOKEN`
- No shell/filesystem tools exposed to Discord agent.
