#!/usr/bin/env bash
# Host bootstrap — runbook step 5.
# Idempotent: safe to re-run.

set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
# Ubuntu 24.04 ships needrestart, which prompts about restarting services during
# apt runs. Force the non-interactive answer so an unattended install can't hang.
echo '$nrconf{restart} = "a";' | sudo tee /etc/needrestart/conf.d/99-noprompt.conf >/dev/null 2>&1 || true

echo "### apt update"
sudo apt-get update -qq

echo "### base packages"
# acl provides setfacl, which deploy-certs-remote.sh needs to grant the container user
# (UID 10001) read access to the TLS private key. Not always on minimal images.
sudo apt-get install -y -qq \
    ca-certificates curl gnupg unzip jq openssl acl postgresql-client

echo "### docker apt repo"
# Docker CE engine — Apache 2.0. Docker Desktop (the licensed product) is a
# separate thing and is macOS/Windows only; nothing here installs it.
sudo install -m 0755 -d /etc/apt/keyrings
# Store the key ARMORED, exactly as served. Do NOT pipe through `gpg --dearmor`
# while keeping the .asc name: on Ubuntu 24.04 that combination makes apt fail
# with "NO_PUBKEY 7EA0A9C3F273FCD8" — the CE deb signing subkey — even though
# the key is present in the file. Armored is unambiguous and apt reads it fine.
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
grep -q 'BEGIN PGP PUBLIC KEY BLOCK' /etc/apt/keyrings/docker.asc \
    || { echo "docker gpg key looks wrong" >&2; exit 1; }
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt-get update -qq

echo "### docker engine + compose plugin"
sudo apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "### certbot + route53 dns plugin"
sudo apt-get install -y -qq certbot python3-certbot-dns-route53

echo "### docker group"
sudo usermod -aG docker ubuntu

echo "### install dir"
sudo mkdir -p /opt/wso2
sudo chown ubuntu:ubuntu /opt/wso2

echo
echo "### versions"
docker --version
sudo docker compose version
certbot --version 2>&1
openssl version
setfacl --version | head -1
jq --version
unzip -v | head -1

echo
echo "### docker daemon"
sudo systemctl is-enabled docker && sudo systemctl is-active docker

echo
echo "### certbot route53 plugin visible?"
certbot plugins 2>/dev/null | grep -A1 dns-route53 || echo "WARNING: dns-route53 plugin not listed"

echo
echo "bootstrap complete"
