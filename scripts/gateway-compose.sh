#!/usr/bin/env bash
# Runs docker compose for ai-gateway with this machine's settings: its ports
# (port offset included), analytics and the embeddings key. run-local.sh and
# the provisioning both start the gateway through it, so it always comes up
# with the same settings.
#
# Usage: ./scripts/gateway-compose.sh <docker compose arguments>

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/tools.sh"
load_demo_env "$ROOT"
resolve_analytics "$ROOT"
resolve_embedding_key "$ROOT"
export_gateway_ports "$ROOT"

cd "$ROOT/ai-gateway"
APIP_GW_ANALYTICS_ENABLED="$ANALYTICS_ENABLED" \
APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID="$ANALYTICS_MOESIF_ID" \
COMPOSE_PROFILES="$ANALYTICS_COMPOSE_PROFILES" \
APIP_GW_EMBEDDING_PROVIDER_API_KEY="$EMBEDDING_PROVIDER_API_KEY" \
  exec docker compose "$@"
