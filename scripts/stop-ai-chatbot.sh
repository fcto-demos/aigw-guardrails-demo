#!/usr/bin/env bash
# Stops ai-chatbot by asking the process itself to exit over loopback HTTP --
# no lsof, no pidfile, no kill signal, nothing OS-specific. Shared by
# stop-local.sh (stopping everything) and run-local.sh (forcing a clean stop
# right before it starts a new one, so a repeat run never fights the old
# process for the port).

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/tools.sh"
load_demo_env "$ROOT"

BASE="http://127.0.0.1:${AI_CHATBOT_PORT}"

if ! curl -fsS -o /dev/null --max-time 2 "$BASE/health" 2>/dev/null; then
  echo "  not running"
  exit 0
fi

curl -fsS -o /dev/null --max-time 2 -X POST "$BASE/__shutdown__"

for _ in $(seq 1 10); do
  sleep 1
  curl -fsS -o /dev/null --max-time 1 "$BASE/health" 2>/dev/null || { echo "  stopped"; exit 0; }
done

echo "  still responding after stop request -- check manually: curl $BASE/health" >&2
exit 1
