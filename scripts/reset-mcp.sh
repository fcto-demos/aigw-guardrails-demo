#!/usr/bin/env bash
# Puts the MCP servers' data back to how a demo starts: they keep it in
# memory, so recreating them resets it (Acme Mobile's account goes back on
# the Everyday plan).
#
# Usage: ./scripts/reset-mcp.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
set -a; source "$ROOT/demo.env"; set +a

(cd "$ROOT/mcp-servers" && docker compose up -d --force-recreate)
