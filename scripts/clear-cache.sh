#!/usr/bin/env bash
# Empties the gateway's semantic cache: every cached answer is deleted, so the
# next question goes to the model again. The Semantic caching card has a
# button that runs it. Only for this machine's gateway.
#
# Usage: ./scripts/clear-cache.sh   (prints how many answers were deleted)

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Each cached answer is a doc:<id> entry in Redis. Only those are deleted: the
# search index stays, so the gateway goes on caching straight away.
"$ROOT/scripts/gateway-compose.sh" exec -T redis \
  redis-cli --user default --pass default --no-auth-warning EVAL \
  "local keys = redis.call('KEYS', 'doc:*') for _, key in ipairs(keys) do redis.call('DEL', key) end return #keys" 0
