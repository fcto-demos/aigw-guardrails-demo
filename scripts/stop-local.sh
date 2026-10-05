#!/usr/bin/env bash
# Stops everything started by run-local.sh.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; source "$ROOT/demo.env"; set +a

echo "### ai-gateway"
# Forces every profile: unlike up, down only tears down what the resolved
# COMPOSE_PROFILES selects, so switching ANALYTICS_MODE between runs would
# otherwise orphan containers.
(cd "$ROOT/ai-gateway" && COMPOSE_PROFILES=tracing,logging,metrics docker compose down)

echo "### ai-workspace"
(cd "$ROOT/ai-workspace" && docker compose down)

echo "### ai-chatbot"
"$ROOT/scripts/stop-ai-chatbot.sh"

echo
echo "done."
