#!/usr/bin/env bash
# Tails logs from all three local components together, each line prefixed
# with its source. Ctrl-C stops all of them.
#
# Usage:
#   ./scripts/logs-local.sh                # everything
#   ./scripts/logs-local.sh gateway-runtime # one docker compose service by name

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/tools.sh"
load_demo_env "$ROOT"
SERVICE="${1:-}"

pids=()
trap cleanup_pids EXIT INT TERM

if [[ -z "$SERVICE" ]]; then
  (cd "$ROOT/ai-workspace" && docker compose logs -f --tail=50) \
    | sed -u 's/^/[ai-workspace] /' &
  pids+=($!)

  (cd "$ROOT/ai-gateway" && docker compose logs -f --tail=50) \
    | sed -u 's/^/[ai-gateway] /' &
  pids+=($!)

  (cd "$ROOT/mcp-servers" && docker compose logs -f --tail=50) \
    | sed -u 's/^/[mcp-servers] /' &
  pids+=($!)

  tail -F "$ROOT/.logs/ai-chatbot.log" 2>/dev/null | sed -u 's/^/[ai-chatbot] /' &
  pids+=($!)
else
  if (cd "$ROOT/ai-workspace" && docker compose config --services 2>/dev/null | grep -qx "$SERVICE"); then
    (cd "$ROOT/ai-workspace" && exec docker compose logs -f --tail=50 "$SERVICE")
  elif (cd "$ROOT/ai-gateway" && docker compose config --services 2>/dev/null | grep -qx "$SERVICE"); then
    (cd "$ROOT/ai-gateway" && exec docker compose logs -f --tail=50 "$SERVICE")
  elif (cd "$ROOT/mcp-servers" && docker compose config --services 2>/dev/null | grep -qx "$SERVICE"); then
    (cd "$ROOT/mcp-servers" && exec docker compose logs -f --tail=50 "$SERVICE")
  elif [[ "$SERVICE" == "ai-chatbot" ]]; then
    exec tail -F "$ROOT/.logs/ai-chatbot.log"
  else
    echo "unknown service: $SERVICE" >&2
    exit 1
  fi
  exit 0
fi

wait
