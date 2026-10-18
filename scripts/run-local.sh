#!/usr/bin/env bash
# Launch ai-workspace, ai-gateway and ai-chatbot locally.
#
# ai-gateway uses the same ports, image and config locally and on the VM --
# see ai-gateway/.env. Compose also loads each docker-compose.override.yaml.
#
# Requires api-platform.env in ai-gateway/ and ai-workspace/ (copy from
# api-platform.env.example) and config.env in ai-chatbot/ (copy from
# config.env.example).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/tools.sh"
load_demo_env "$ROOT"   # this machine's settings, created on the first run (see tools.sh)
resolve_python   # sets $PYTHON -- python3, or a confirmed-Python-3 `python` (see tools.sh)
resolve_analytics "$ROOT"   # sets $ANALYTICS_* from ANALYTICS_MODE (see tools.sh)
resolve_embedding_key "$ROOT"   # sets $EMBEDDING_PROVIDER_API_KEY from EMBEDDINGS_PROVIDER_API_KEY (see tools.sh)

LOGS="$ROOT/.logs"

# The gateway's ports, moved up by the port offset from demo.env (see tools.sh).
export_gateway_ports "$ROOT"

require_env "$ROOT/ai-workspace/api-platform.env"
require_env "$ROOT/ai-gateway/api-platform.env"
require_env "$ROOT/ai-chatbot/config.env"

# Container noise goes to a log file, not the terminal; set -e still stops
# the script on failure.
# The MCP servers' network, shared by both stacks (see the two
# docker-compose.override.yaml files).
docker network inspect "$DEMO_NAME-mcp" >/dev/null 2>&1 || docker network create "$DEMO_NAME-mcp" >/dev/null

echo "### mcp-servers"
# Recreated on every run, so the current version of their code is the one running.
(cd "$ROOT/mcp-servers" && docker compose up -d --force-recreate) >"$LOGS/mcp-servers.log" 2>&1

echo "### ai-workspace"
(cd "$ROOT/ai-workspace" && docker compose up -d --wait) >"$LOGS/ai-workspace.log" 2>&1

echo "### ai-gateway"
# Analytics vars are scoped to just this one call (command-prefix, not
# exported) -- see resolve_analytics() in tools.sh for why.
(cd "$ROOT/ai-gateway" && \
  APIP_GW_ANALYTICS_ENABLED="$ANALYTICS_ENABLED" \
  APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID="$ANALYTICS_MOESIF_ID" \
  COMPOSE_PROFILES="$ANALYTICS_COMPOSE_PROFILES" \
  APIP_GW_EMBEDDING_PROVIDER_API_KEY="$EMBEDDING_PROVIDER_API_KEY" \
  docker compose up -d) >"$LOGS/ai-gateway.log" 2>&1

echo "### ai-chatbot"
# Force a clean stop first — otherwise a repeat run leaves the old process
# holding the port, and the new one fails to bind.
"$ROOT/scripts/stop-ai-chatbot.sh"
(cd "$ROOT/ai-chatbot" && nohup "$PYTHON" server.py >"$LOGS/ai-chatbot.log" 2>&1 &)

echo
echo "done. Everything is up:"
echo "  ai-workspace (control plane) : https://localhost:${AI_WORKSPACE_PORT}"
echo "  api-portal (not used)        : https://localhost:${API_PORTAL_PORT}"
echo "  platform-api                 : https://localhost:${PLATFORM_API_PORT}"
echo "  ai-gateway (data plane)      : https://localhost:${GATEWAY_HTTPS_PORT}"
echo "  ai-chatbot                   : http://localhost:${AI_CHATBOT_PORT}"
echo
echo "logs: $LOGS/{ai-workspace,ai-gateway,ai-chatbot}.log"
echo "stop everything: ./scripts/stop-local.sh"
