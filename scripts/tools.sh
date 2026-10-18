#!/usr/bin/env bash
# Shared helpers for the local-dev scripts in this directory. Source, don't
# execute:
#   source "$ROOT/scripts/tools.sh"
#
# deploy-certs-remote.sh does NOT source this file on purpose: it gets
# installed standalone on the VM (as a certbot renewal hook, see
# .claude/skills/_reference/tls-and-certs.md), with no sibling scripts/
# directory to find this file in.

# Resolves the Python 3 interpreter into $PYTHON. A `python3` shell alias is
# invisible to a non-interactive script, so this surfaces a clear error
# instead of a confusing "command not found". Falls back to bare `python`
# only after confirming it's actually Python 3.
resolve_python() {
  if command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
  elif command -v python >/dev/null 2>&1 \
       && python -c 'import sys; sys.exit(0 if sys.version_info[0] >= 3 else 1)' 2>/dev/null; then
    PYTHON=python
  else
    echo "no Python 3 interpreter found (looked for python3, then python) -- install Python 3 first" >&2
    exit 1
  fi
}

# Loads this machine's settings from demo.env. On the first run, demo.env is
# created from demo.env.example; later, any setting the example has gained is
# pointed out, so it can be added.
load_demo_env() {
  local root="$1" key
  if [[ ! -f "$root/demo.env" ]]; then
    cp "$root/demo.env.example" "$root/demo.env"
    echo "  created demo.env from demo.env.example -- adapt it to this machine if needed"
  fi
  for key in $(grep -oE '^[A-Z_]+=' "$root/demo.env.example" | tr -d '='); do
    grep -q "^$key=" "$root/demo.env" \
      || echo "  demo.env has no $key: add it from demo.env.example ($(grep "^$key=" "$root/demo.env.example"))" >&2
  done
  set -a
  source "$root/demo.env"
  set +a
}

# Moves every gateway port up by the port offset from demo.env (1 turns 8443
# into 8444) and hands the result to Docker Compose.
export_gateway_ports() {
  local root="$1" offset="${GATEWAY_PORT_OFFSET:-0}" name base
  while IFS='=' read -r name base; do
    export "$name=$((base + offset))"
  done < <(grep -E '^GATEWAY_[A-Z_]+_PORT=[0-9]+$' "$root/ai-gateway/.env")
}

# Turns demo.env's ANALYTICS_MODE plus secrets/api-keys.env's Moesif id into
# $ANALYTICS_ENABLED / $ANALYTICS_MOESIF_ID / $ANALYTICS_COMPOSE_PROFILES.
# Pass these as a command-scoped prefix on the one docker compose call that
# needs them (see run-local.sh) -- not exported globally, since
# COMPOSE_PROFILES means something different to ai-workspace's own compose
# call in the same script and would otherwise leak into it.
resolve_analytics() {
  local root="$1"
  ANALYTICS_MOESIF_ID="$(sed -n 's/^APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID=//p' \
    "$root/secrets/api-keys.env" 2>/dev/null | tail -1)"
  case "${ANALYTICS_MODE:-moesif}" in
    moesif)
      # enabled=true with a blank id isn't a harmless no-op -- policy-engine's
      # config validation rejects it and the container refuses to start.
      if [[ -n "$ANALYTICS_MOESIF_ID" ]]; then
        ANALYTICS_ENABLED=true
        ANALYTICS_COMPOSE_PROFILES="moesif"   # the collector sending traces to Moesif
      else
        ANALYTICS_ENABLED=false
        ANALYTICS_COMPOSE_PROFILES=""
        echo "  analytics: ANALYTICS_MODE=moesif but secrets/api-keys.env has no" \
             "APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID -- leaving analytics off" \
             "(set ANALYTICS_MODE=local instead for the local observability stack)" >&2
      fi
      ;;
    local)
      ANALYTICS_ENABLED=false
      ANALYTICS_COMPOSE_PROFILES="tracing,logging,metrics"
      ;;
    *)
      echo "demo.env: ANALYTICS_MODE must be 'moesif' or 'local', got '$ANALYTICS_MODE'" >&2
      exit 1
      ;;
  esac
}

# The semantic-cache and semantic-model-routing policies both call an
# embedding API -- its own credential, kept separate from any single
# showcase's chat provider key (today it's always Mistral, config.toml
# hardcodes embedding_provider = "MISTRAL"; see secrets/api-keys.env.example's
# own TODO for when that might change). Sets $EMBEDDING_PROVIDER_API_KEY,
# passed the same scoped way as resolve_analytics()'s output (see
# run-local.sh).
resolve_embedding_key() {
  local root="$1"
  EMBEDDING_PROVIDER_API_KEY="$(sed -n 's/^EMBEDDINGS_PROVIDER_API_KEY=//p' \
    "$root/secrets/api-keys.env" 2>/dev/null | tail -1)"
}

require_env() {
  local file="$1"
  if [[ ! -f "$file" ]]; then
    echo "Missing $file — copy it from ${file}.example and fill in real values first." >&2
    exit 1
  fi
}

gen_selfsigned() {
  local dir="$1" cert="$2" key="$3"
  if [[ -f "$dir/$cert" && -f "$dir/$key" ]]; then
    echo "  skip $dir/$cert (exists)"
    return
  fi
  mkdir -p "$dir"
  openssl req -x509 -newkey rsa:2048 -sha256 -days 825 -nodes \
    -keyout "$dir/$key" -out "$dir/$cert" \
    -subj "/CN=localhost" \
    -addext "subjectAltName=DNS:localhost,IP:127.0.0.1"
  chmod 600 "$dir/$key"
  echo "  generated $dir/$cert + $key"
}

# Expects a global `pids` array of background job PIDs in the caller, e.g.:
#   pids=(); trap cleanup_pids EXIT INT TERM
cleanup_pids() {
  if ((${#pids[@]})); then
    kill "${pids[@]}" 2>/dev/null
  fi
}
