#!/usr/bin/env bash
# Place the Let's Encrypt pair into both stacks and recreate the containers.
#
# Run once during bring-up (runbook step 12), and again by hand after any
# `certbot renew`. Renewal automation is deliberately not installed for this
# PoC — see runbook step 9.
#
# Neither the control-plane services nor Envoy reload a certificate while
# running, and neither watches the file for changes, so this MUST end in
# --force-recreate rather than `restart`.

set -euo pipefail

# Set these before running — either export them, or `source demo.env`
# (copied from the repo root onto this VM) first. The version numbers must
# match whatever distros were actually unpacked into /opt/wso2 -- demo.env
# is the source of truth, but this script can't assume it's even present
# (it's copied standalone to the VM), hence the inline defaults below.
HOSTNAME_FQDN="${DEMO_HOSTNAME:-changeme.example.com}"
if [[ "$HOSTNAME_FQDN" == "changeme.example.com" ]]; then
    echo "DEMO_HOSTNAME is still the placeholder — set it to this client's real public hostname first" >&2
    exit 1
fi
LIVE=/etc/letsencrypt/live/${HOSTNAME_FQDN}
CP="/opt/wso2/wso2apip-ai-workspace-${AI_WORKSPACE_VERSION:-1.0.0}"
GW="/opt/wso2/wso2apip-ai-gateway-${AI_GATEWAY_VERSION:-1.2.0}"

# Both distributions run their containers as UID 10001, which has no matching
# account on the host. The shipped setup.sh scripts grant read access with an
# ACL and fall back to world-readable when setfacl is unavailable; this
# mirrors that exactly. Without it the listener cannot read its own key.
CONTAINER_UID=10001

# This script runs as root (it reads /etc/letsencrypt/live/*/privkey.pem), but the
# distributions are owned by `ubuntu` and their own scripts/setup.sh chmods these
# same files. Root-owned files make setup.sh fail with "Operation not permitted",
# so hand ownership back to the directory owner after placing each file.
OWNER=ubuntu:ubuntu

# Set by place_key() below if setfacl isn't available; checked at the end to
# warn once instead of per-file.
DEGRADED_KEY_PERMS=0

[[ -r "$LIVE/fullchain.pem" ]] || { echo "no certificate at $LIVE — run certbot first" >&2; exit 1; }

place_cert() {
    install -m 644 -o "${OWNER%:*}" -g "${OWNER#*:}" "$LIVE/fullchain.pem" "$1"
    echo "  cert -> $1"
}

place_key() {
    install -m 600 -o "${OWNER%:*}" -g "${OWNER#*:}" "$LIVE/privkey.pem" "$1"
    if ! setfacl -m "u:${CONTAINER_UID}:r" "$1" 2>/dev/null; then
        echo "  WARNING: setfacl unavailable; falling back to world-readable so UID ${CONTAINER_UID} can read $1" >&2
        chmod 644 "$1"
        DEGRADED_KEY_PERMS=1
    fi
    echo "  key  -> $1"
}

echo "Placing certificates:"

# Control plane: one shared pair. All three services (ai-workspace,
# platform-api, api-portal) answer on the same hostname, so the shipped
# single-directory mount is correct here and needs no changes.
place_cert "$CP/resources/certificates/cert.pem"
place_key  "$CP/resources/certificates/key.pem"

# Gateway: Envoy's downstream listener. Same bytes, different filenames.
# Do NOT touch $GW/resources/certificates/ — that is the upstream trust
# store, not the listener.
place_cert "$GW/resources/listener-certs/default-listener.crt"
place_key  "$GW/resources/listener-certs/default-listener.key"

echo
echo "Recreating containers:"

# cd into each directory rather than using `docker compose -f`, so .env
# (COMPOSE_PROJECT_NAME, COMPOSE_PROFILES) and docker-compose.override.yaml
# are picked up automatically.
for d in "$CP" "$GW"; do
    if [[ ! -f "$d/api-platform.env" ]]; then
        echo "  skipping $d — not set up yet"
        continue
    fi
    echo "  $d"
    ( cd "$d" && docker compose up -d --force-recreate )
done

echo
echo "Certificate expires:"
openssl x509 -enddate -noout -in "$CP/resources/certificates/cert.pem"

if [[ "$DEGRADED_KEY_PERMS" == "1" ]]; then
    echo
    echo "##############################################################"
    echo "# WARNING: setfacl was unavailable, so one or both private   #"
    echo "# keys above are mode 644 -- readable by every local account #"
    echo "# on this host, not just the container's UID. Install acl    #"
    echo "# ('apt-get install -y acl', or see bootstrap-host.sh) and   #"
    echo "# re-run this script to restore the ACL-scoped permission.   #"
    echo "##############################################################"
fi
