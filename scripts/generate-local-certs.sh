#!/usr/bin/env bash
# Generates everything docker-compose.yaml mounts from resources/ that
# scripts/setup.sh would normally produce (that script ships with the WSO2
# distribution zip, not with this repo). Self-signed certs for localhost —
# fine for local dev, never for the VM.
#
# Idempotent: skips anything that already exists so re-running is safe.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$ROOT/scripts/tools.sh"
AIW="$ROOT/ai-workspace"
GW="$ROOT/ai-gateway"

echo "### ai-workspace: control-plane TLS (shared by platform-api, ai-workspace, api-portal)"
gen_selfsigned "$AIW/resources/certificates" cert.pem key.pem

echo "### ai-workspace: at-rest encryption key (platform-api)"
mkdir -p "$AIW/resources/keys"
if [[ -f "$AIW/resources/keys/encryption.key" ]]; then
  echo "  skip encryption.key (exists)"
else
  openssl rand -hex 32 > "$AIW/resources/keys/encryption.key"
  echo "  generated encryption.key"
fi

echo "### ai-workspace: JWT RS256 keypair (platform-api signs, api-portal verifies)"
if [[ -f "$AIW/resources/keys/jwt_private.pem" && -f "$AIW/resources/keys/jwt_public.pem" ]]; then
  echo "  skip jwt keypair (exists)"
else
  openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 \
    -out "$AIW/resources/keys/jwt_private.pem" 2>/dev/null
  openssl rsa -in "$AIW/resources/keys/jwt_private.pem" \
    -pubout -out "$AIW/resources/keys/jwt_public.pem" 2>/dev/null
  chmod 600 "$AIW/resources/keys/jwt_private.pem"
  echo "  generated jwt_private.pem + jwt_public.pem"
fi

echo "### ai-workspace: api-portal encryption key + session secret"
if [[ -f "$AIW/resources/keys/api-portal-encryption.key" ]]; then
  echo "  skip api-portal-encryption.key (exists)"
else
  openssl rand -hex 32 > "$AIW/resources/keys/api-portal-encryption.key"
  echo "  generated api-portal-encryption.key"
fi
if [[ -f "$AIW/resources/keys/api-portal-session-secret" ]]; then
  echo "  skip api-portal-session-secret (exists)"
else
  openssl rand -hex 32 > "$AIW/resources/keys/api-portal-session-secret"
  echo "  generated api-portal-session-secret"
fi

echo "### ai-gateway: Envoy downstream listener TLS"
gen_selfsigned "$GW/resources/listener-certs" default-listener.crt default-listener.key

echo "### ai-gateway: upstream custom-CA trust store (empty dir is valid — optional)"
mkdir -p "$GW/resources/certificates"

echo "### ai-gateway: AES-256-GCM at-rest key"
mkdir -p "$GW/resources/aesgcm-keys"
if [[ -f "$GW/resources/aesgcm-keys/default-aesgcm256-v1.bin" ]]; then
  echo "  skip default-aesgcm256-v1.bin (exists)"
else
  openssl rand -out "$GW/resources/aesgcm-keys/default-aesgcm256-v1.bin" 32
  echo "  generated default-aesgcm256-v1.bin"
fi

echo "### admin credentials (used by both api-platform.env files)"
if [[ -f "$AIW/api-platform.env" ]] && grep -q '^APIP_CP_ADMIN_PASSWORD_HASH=\$2' "$AIW/api-platform.env"; then
  echo "  skip (hash already present in ai-workspace/api-platform.env)"
else
  # Non-interactive shells (no TTY) get an empty ADMIN_PASSWORD from `read`,
  # which would silently hash an empty string — refuse rather than lock
  # everyone out of a working "admin"/"admin" login.
  if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
    if [[ ! -t 0 ]]; then
      echo "  no TTY and ADMIN_PASSWORD unset — refusing to hash an empty password." >&2
      echo "  run interactively, or: ADMIN_PASSWORD='...' $0" >&2
      exit 1
    fi
    read -rsp "Choose a local admin password: " ADMIN_PASSWORD && echo
  fi
  if [[ -z "${ADMIN_PASSWORD:-}" ]]; then
    echo "  empty admin password — refusing to hash it, run again and type one." >&2
    exit 1
  fi
  ADMIN_HASH="$(htpasswd -bnBC 12 "" "$ADMIN_PASSWORD" | tr -d ':\n')"
  unset ADMIN_PASSWORD

  sed -i.bak "s|^APIP_CP_ADMIN_PASSWORD_HASH=.*|APIP_CP_ADMIN_PASSWORD_HASH=$ADMIN_HASH|" \
    "$AIW/api-platform.env" && rm -f "$AIW/api-platform.env.bak"
  sed -i.bak "s|^APIP_GW_CONTROLLER_AUTH_BASIC_ADMIN_PASSWORD_HASH=.*|APIP_GW_CONTROLLER_AUTH_BASIC_ADMIN_PASSWORD_HASH=$ADMIN_HASH|" \
    "$GW/api-platform.env" && rm -f "$GW/api-platform.env.bak"
  unset ADMIN_HASH
  echo "  admin username: admin — password hash written to both api-platform.env files"
fi

chmod 600 "$AIW/api-platform.env" "$GW/api-platform.env"

echo
echo "done."
