#!/usr/bin/env bash
# Full from-scratch rebuild of the local demo, meant to work right after a
# fresh `git clone` with no other manual steps except pasting two API keys.
#
# What it does, in order:
#   1. Checks required host tools are installed (docker, openssl, htpasswd,
#      curl, unzip, and a Python 3 interpreter -- python3, or python if
#      that's confirmed to actually be Python 3).
#   2. Checks secrets/api-keys.env exists — exits with copy-paste
#      instructions if not, before touching anything. (Which keys it needs to
#      actually hold, and whether each looks real, is scripts/python/provision_showcases.py's
#      job — it fails loudly there instead, after everything else below.)
#   3. Installs the `ap` CLI into ./.bin/ap if it isn't already there
#      (downloads the pinned release for this machine's OS/arch).
#   4. Builds the custom gateway image (semantic/time/cost/intelligent-model-routing
#      + model-round-robin need it — the stock image doesn't carry them) if
#      it is missing, or if build.yaml or a local policy changed since.
#   5. Wipes ai-workspace + ai-gateway state (containers + volumes).
#   6. Creates the two api-platform.env files from their examples, then
#      generates local certs/keys (both idempotent).
#   7. Brings the stack up and provisions every lane through
#      scripts/python/provision_showcases.py.
#
# DESTRUCTIVE at step 5: `docker compose down -v` deletes the control-plane
# SQLite volumes (platform-api, gateway-controller, redis cache data) — every
# provider, gateway registration, application and cached response is gone.
# Local certs/keys under resources/ and secrets/ are host files, not volumes,
# and are left untouched.
#
# Usage: ./scripts/reset-and-setup.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/tools.sh"
load_demo_env "$ROOT"

LOGS="$ROOT/.logs"

AP_VERSION="0.9.1"                       # bump here if a newer ap CLI release is needed
AP_BIN="$ROOT/.bin/ap"
CUSTOM_IMAGE="ghcr.io/wso2/api-platform/${DEMO_NAME}-gateway-runtime:${AI_GATEWAY_VERSION}"

echo "### checking required tools"
missing=()
for tool in docker openssl htpasswd curl unzip; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "missing required tools: ${missing[*]}" >&2
  echo "install them first (all are standard on macOS; on Linux, htpasswd is in apache2-utils/httpd-tools)" >&2
  exit 1
fi
resolve_python   # sets $PYTHON -- python3, or a confirmed-Python-3 `python` (see tools.sh)
docker info >/dev/null 2>&1 || { echo "docker is not running — start Docker/Colima first" >&2; exit 1; }

echo "### checking secrets"
SECRETS_FILE="secrets/api-keys.env"
if [[ ! -f "$SECRETS_FILE" ]]; then
  echo "missing $SECRETS_FILE" >&2
  echo "  cp secrets/api-keys.env.example $SECRETS_FILE   # then edit it with real keys" >&2
  exit 1
fi
# Which keys are actually required (only the enabled providers, per
# providers.json) and whether each one looks real is scripts/python/provision_showcases.py's
# job alone (read_secret()) -- it already fails loudly and specifically, so
# this doesn't re-implement that check in bash too.

echo "### ap CLI"
if [[ -x "$AP_BIN" ]]; then
  echo "  found at $AP_BIN ($("$AP_BIN" version 2>&1))"
else
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "$arch" in
    x86_64) arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) echo "unsupported architecture: $arch — install ap manually, see .claude/skills/wso2-custom-policy" >&2; exit 1 ;;
  esac
  asset="ap-${os}-${arch}-v${AP_VERSION}.zip"
  url="https://github.com/wso2/api-platform/releases/download/ap%2Fv${AP_VERSION}/${asset}"
  echo "  downloading $asset"
  tmp="$(mktemp -d)"
  curl -sfL "$url" -o "$tmp/$asset"
  unzip -oq "$tmp/$asset" -d "$tmp"
  mkdir -p "$ROOT/.bin"
  install -m 755 "$tmp"/ap-*/ap "$AP_BIN"
  rm -rf "$tmp"
  echo "  installed to $AP_BIN ($("$AP_BIN" version 2>&1))"
fi

echo "### custom gateway image (routing + failover policies)"
# The policies are compiled into the image, so it is rebuilt when build.yaml or
# a local policy changes: their fingerprint at the last build is kept in .bin/.
policies_fingerprint() {
  { cat ai-gateway/build.yaml
    find ai-gateway/policies -type f -print0 | sort -z | xargs -0 cat; } | shasum | cut -d' ' -f1
}
stamp="$ROOT/.bin/gateway-image-${DEMO_NAME}-${AI_GATEWAY_VERSION}"
if docker image inspect "$CUSTOM_IMAGE" >/dev/null 2>&1 \
   && [[ ! -f "$stamp" || "$(cat "$stamp")" == "$(policies_fingerprint)" ]]; then
  echo "  $CUSTOM_IMAGE already present"
else
  echo "  building — this compiles semantic/time/cost/intelligent-model-routing and"
  echo "  model-round-robin into the image, takes a few minutes"
  (cd ai-gateway && "$AP_BIN" gateway image build --name "$DEMO_NAME")
fi
mkdir -p "$ROOT/.bin" && policies_fingerprint > "$stamp"

echo "### wiping ai-workspace + ai-gateway state (containers + volumes)"
# Each command's own noise goes to a log file instead of the terminal; set -e
# still stops this script on failure, and the log has the detail.
(cd ai-workspace && docker compose down -v) >"$LOGS/ai-workspace-down.log" 2>&1
# Forces every profile regardless of demo.env's current ANALYTICS_MODE --
# see stop-local.sh's own identical comment on why.
(cd ai-gateway && COMPOSE_PROFILES=moesif,tracing,logging,metrics docker compose down -v) >"$LOGS/ai-gateway-down.log" 2>&1
(cd mcp-servers && docker compose down) >"$LOGS/mcp-servers-down.log" 2>&1

echo "### api-platform.env files (local values, nothing to edit for a local run)"
copy_example_if_missing "$ROOT" ai-workspace/api-platform.env
copy_example_if_missing "$ROOT" ai-gateway/api-platform.env

# The admin password is asked once, here, so the hash written next and the
# provisioning's login further down use the same one.
if [[ -z "${ADMIN_PASSWORD:-}" && -t 0 ]] \
   && ! grep -q '^APIP_CP_ADMIN_PASSWORD_HASH=\$2' ai-workspace/api-platform.env; then
  read -rsp "Choose a local admin password for AI Workspace (user admin): " ADMIN_PASSWORD && echo
fi
if [[ -n "${ADMIN_PASSWORD:-}" ]]; then export ADMIN_PASSWORD; fi

echo "### certs and keys (idempotent — fills in only what's missing)"
./scripts/generate-local-certs.sh

echo "### bringing up ai-workspace + ai-gateway + ai-chatbot"
./scripts/run-local.sh

echo "### waiting for ai-workspace to answer"
for i in $(seq 1 30); do
  if curl -sk -o /dev/null -w '' "https://localhost:${PLATFORM_API_PORT}/health" 2>/dev/null \
     && curl -sk -o /dev/null -w '' "https://localhost:${AI_WORKSPACE_PORT}/healthz" 2>/dev/null; then
    break
  fi
  sleep 2
done

echo "### provisioning every lane, the gateway registration, and the app grouping"
"$PYTHON" ./scripts/python/provision_showcases.py

echo
echo "done: http://localhost:${AI_CHATBOT_PORT}"
