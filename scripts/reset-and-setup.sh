#!/usr/bin/env bash
# Rebuilds the local demo from scratch. Works right after a fresh git clone:
# the only manual step is putting the API keys in secrets/api-keys.env.
#
# Steps (each one is marked "Step N" below and prints "### N/8" when it runs):
#   1. Check the tools: docker, openssl, htpasswd, curl, unzip and Python 3.
#   2. Check the secrets: secrets/api-keys.env, with at least one provider key.
#   3. Install the ap CLI in .bin/ if it is not there yet.
#   4. Build the custom gateway image, which carries the routing and failover
#      policies, when it is missing or its policies changed.
#   5. Wipe AI Workspace and the gateway: containers and volumes.
#   6. Create the local config files, the admin password and the certificates.
#   7. Start everything with run-local.sh, and wait for AI Workspace.
#   8. Create every lane in AI Workspace, with provision_showcases.py.
#
# DESTRUCTIVE at step 5: every provider, gateway registration, application
# and cached response is deleted. Certificates and keys under resources/ and
# secrets/ are files on this machine, so they stay.
#
# Usage: ./scripts/reset-and-setup.sh

set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
source "$ROOT/scripts/tools.sh"
load_demo_env "$ROOT"

# Each command's output goes to a log file here, not to the terminal.
LOGS="$ROOT/.logs"

AP_VERSION="0.9.1"                       # bump here if a newer ap CLI release is needed
AP_BIN="$ROOT/.bin/ap"
CUSTOM_IMAGE="ghcr.io/wso2/api-platform/${DEMO_NAME}-gateway-runtime:${AI_GATEWAY_VERSION}"

# ==== Step 1/8: check the tools ===============================================
echo "### 1/8 checking required tools"
missing=()
for tool in docker openssl htpasswd curl unzip; do
  command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "missing required tools: ${missing[*]}" >&2
  echo "install them first (all are standard on macOS; on Linux, htpasswd is in apache2-utils/httpd-tools)" >&2
  exit 1
fi
resolve_python   # the Python 3 to use (see tools.sh)
# This script rebuilds this machine's stack: refuse when demo.env points to
# a remote AI Workspace, before anything is wiped.
if ! "$PYTHON" -c "import sys; sys.path.insert(0, 'scripts/python'); import target; sys.exit(target.REMOTE)"; then
  echo "demo.env points to a remote AI Workspace (REMOTE_ settings)." >&2
  echo "  This script only rebuilds this machine's stack. To provision the remote one, see the README." >&2
  exit 1
fi
docker info >/dev/null 2>&1 || { echo "docker is not running: start Docker or Colima first" >&2; exit 1; }

# ==== Step 2/8: check the secrets =============================================
echo "### 2/8 checking secrets"
SECRETS_FILE="secrets/api-keys.env"
if [[ ! -f "$SECRETS_FILE" ]]; then
  echo "missing $SECRETS_FILE" >&2
  echo "  cp secrets/api-keys.env.example $SECRETS_FILE   # then edit it with real keys" >&2
  exit 1
fi
# At least one provider key, or there is nothing to deploy. Whether each key
# looks real is checked at step 8.
if ! "$PYTHON" -c "import sys; sys.path.insert(0, 'scripts/python'); from platform_api import PROVIDERS, provider_key; sys.exit(not any(map(provider_key, PROVIDERS)))"; then
  echo "no provider key in $SECRETS_FILE: add at least one (see the README)" >&2
  exit 1
fi

# ==== Step 3/8: ap CLI ========================================================
echo "### 3/8 ap CLI"
if [[ -x "$AP_BIN" ]]; then
  echo "  found at $AP_BIN ($("$AP_BIN" version 2>&1))"
else
  # Downloads the release for this machine's system and processor.
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"
  case "$arch" in
    x86_64) arch="amd64" ;;
    arm64|aarch64) arch="arm64" ;;
    *) echo "unsupported architecture: $arch, install ap by hand (see .claude/skills/wso2-custom-policy)" >&2; exit 1 ;;
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

# ==== Step 4/8: custom gateway image ==========================================
echo "### 4/8 custom gateway image (routing + failover policies)"
use_external_registry "$ROOT"   # registry images, if secrets/registry.env turns them on
# With the registry on, the gateway is built on the registry's base images,
# from a copy of build.yaml that names them.
build_dir="$ROOT/ai-gateway"
if [[ -n "${GATEWAY_BUILDER_IMAGE:-}" ]]; then
  build_dir="$ROOT/.bin/gateway-build"
  rm -rf "$build_dir" && mkdir -p "$build_dir" && cp -R ai-gateway/policies "$build_dir/"
  awk -v images="  images:\n    builder: $GATEWAY_BUILDER_IMAGE\n    controller: $GATEWAY_CONTROLLER_IMAGE\n    runtime: $GATEWAY_RUNTIME_IMAGE" \
    '{ print } /^gateway:/ { gateway = 1 } gateway && /^  version:/ { print images; gateway = 0 }' \
    ai-gateway/build.yaml > "$build_dir/build.yaml"
fi
# The image is rebuilt when build.yaml, a local policy or the base images
# change: a fingerprint of the last build is kept in .bin/.
policies_fingerprint() {
  { cat "$build_dir/build.yaml"
    find ai-gateway/policies -type f -print0 | sort -z | xargs -0 cat; } | shasum | cut -d' ' -f1
}
stamp="$ROOT/.bin/gateway-image-${DEMO_NAME}-${AI_GATEWAY_VERSION}"
if docker image inspect "$CUSTOM_IMAGE" >/dev/null 2>&1 \
   && [[ ! -f "$stamp" || "$(cat "$stamp")" == "$(policies_fingerprint)" ]]; then
  echo "  $CUSTOM_IMAGE already present"
else
  echo "  building: this compiles semantic/time/cost/intelligent-model-routing and"
  echo "  model-round-robin into the image, takes a few minutes"
  (cd "$build_dir" && "$AP_BIN" gateway image build --name "$DEMO_NAME")
fi
mkdir -p "$ROOT/.bin" && policies_fingerprint > "$stamp"

# ==== Step 5/8: wipe (DESTRUCTIVE) ============================================
echo "### 5/8 wiping ai-workspace + ai-gateway state (containers + volumes)"
(cd ai-workspace && docker compose down -v) >"$LOGS/ai-workspace-down.log" 2>&1
# All analytics profiles, whatever ANALYTICS_MODE says now, so that no
# container started under another mode is left behind.
(cd ai-gateway && COMPOSE_PROFILES=moesif,tracing,logging,metrics docker compose down -v) >"$LOGS/ai-gateway-down.log" 2>&1
(cd mcp-servers && docker compose down) >"$LOGS/mcp-servers-down.log" 2>&1

# ==== Step 6/8: config files, admin password, certificates ====================
echo "### 6/8 api-platform.env files, admin password, certs and keys"
# Local values, nothing to edit for a local run.
copy_example_if_missing "$ROOT" ai-workspace/api-platform.env
copy_example_if_missing "$ROOT" ai-gateway/api-platform.env
# The admin password is asked once, here, so the certificates script below and
# the provisioning at step 8 use the same one.
if [[ -z "${ADMIN_PASSWORD:-}" && -t 0 ]] \
   && ! grep -q '^APIP_CP_ADMIN_PASSWORD_HASH=\$2' ai-workspace/api-platform.env; then
  read -rsp "Choose a local admin password for AI Workspace (user admin): " ADMIN_PASSWORD && echo
fi
if [[ -n "${ADMIN_PASSWORD:-}" ]]; then export ADMIN_PASSWORD; fi
# Only creates what is missing, so a rerun keeps the existing ones.
./scripts/generate-local-certs.sh

# ==== Step 7/8: start everything ==============================================
echo "### 7/8 bringing up ai-workspace + ai-gateway + ai-chatbot (run-local.sh, its own steps follow)"
./scripts/run-local.sh
echo "  waiting for ai-workspace to answer"
for i in $(seq 1 30); do
  if curl -sk -o /dev/null -w '' "https://localhost:${PLATFORM_API_PORT}/health" 2>/dev/null \
     && curl -sk -o /dev/null -w '' "https://localhost:${AI_WORKSPACE_PORT}/healthz" 2>/dev/null; then
    break
  fi
  sleep 2
done

# ==== Step 8/8: provisioning ==================================================
echo "### 8/8 provisioning every lane, the gateway registration, and the app grouping"
"$PYTHON" ./scripts/python/provision_showcases.py

echo
echo "done: http://localhost:${AI_CHATBOT_PORT}"
