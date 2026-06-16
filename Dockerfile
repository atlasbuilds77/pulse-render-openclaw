FROM ghcr.io/openclaw/openclaw:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pulse_api.py /app/pulse_api.py
COPY pulse /app/pulse
COPY workspace /app/workspace
COPY openclaw.json /app/openclaw.json
COPY start.sh /app/start.sh
RUN OPENCLAW_CONFIG_PATH=/app/openclaw.json OPENCLAW_STATE_DIR=/tmp/openclaw-build OPENCLAW_GATEWAY_TOKEN=build-token openclaw doctor --fix || true
RUN chmod +x /app/start.sh && mkdir -p /data && chown -R node:node /data /app/pulse /app/workspace /app/openclaw.json /app/start.sh /app/pulse_api.py
USER node
ENV NODE_ENV=production OPENCLAW_STATE_DIR=/data OPENCLAW_CONFIG_PATH=/app/openclaw.json OPENCLAW_DISABLE_BONJOUR=1
CMD ["/app/start.sh"]
