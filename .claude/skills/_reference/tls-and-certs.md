# TLS and certificates

## One hostname, many ports

A TLS certificate is issued for a **hostname**, not a hostname-and-port. So one
name and one certificate cover every component, with the port doing the routing:

```
https://<host>:9643/ai-workspace          AI Workspace
https://<host>:9543/api-portal/<org>/...  API Portal
https://<host>:8443                       Gateway data plane
https://<host>:9243                       Platform API
```

One DNS A record, one certificate, one SAN. Distinct hostnames per component
only become necessary behind a load balancer on port 443, where the hostname is
the sole routing key — and that is not this deployment shape.

Ask the user for **one** hostname unless they specifically want a reverse proxy
on 443. Do not invent extra hostnames.

## Issuing

**DNS-01 via Route 53** is the better default: no inbound port 80, and it works
for a host with no public HTTP listener.

```bash
sudo apt-get install -y certbot python3-certbot-dns-route53
sudo certbot certonly --dns-route53 -d <hostname> \
  --email <email> --agree-tos --non-interactive
```

Requires an instance role. Scope it tightly — the instance needs to write
exactly one TXT record and nothing else:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Effect": "Allow",
      "Action": ["route53:ListHostedZones", "route53:GetChange"],
      "Resource": "*" },
    { "Effect": "Allow",
      "Action": "route53:ListResourceRecordSets",
      "Resource": "arn:aws:route53:::hostedzone/<ZONE_ID>" },
    { "Effect": "Allow",
      "Action": "route53:ChangeResourceRecordSets",
      "Resource": "arn:aws:route53:::hostedzone/<ZONE_ID>",
      "Condition": { "ForAllValues:StringEquals": {
        "route53:ChangeResourceRecordSetsRecordTypes": ["TXT"],
        "route53:ChangeResourceRecordSetsNormalizedRecordNames": ["_acme-challenge.<hostname>"]
      }}}
  ]
}
```

`ListResourceRecordSets` is read-only but **required** — certbot's plugin needs
it. The two conditions mean the instance cannot touch the A records or anything
else in the zone.

**HTTP-01 alternative** when IAM writes are unavailable: `certbot certonly
--standalone`, needs inbound port 80. Envoy uses 8080 so there is no conflict.

## The DNS record trap

In the Route 53 console the record-name field already appends the zone. Entering
the full FQDN produces:

```
<host>.<zone>.<zone>     ← wrong, resolves to nothing
```

Always verify after the user says DNS is done — do not take it on trust:

```bash
aws route53 list-resource-record-sets --hosted-zone-id <ZONE> \
  --query "ResourceRecordSets[?contains(Name,'<label>')].{Name:Name,Type:Type,Value:ResourceRecords[0].Value}" --output table
for r in 8.8.8.8 1.1.1.1; do dig +short @$r <hostname> A; done
```

## Deploying certificates into the stacks

Two things make this fiddly, and both are silent failures.

**Containers run as UID 10001**, which has no matching host account. `chmod 600`
alone leaves the private key unreadable and the listener fails at startup. The
shipped `setup.sh` scripts use an ACL with a world-readable fallback; match that.

**The script runs as root** (it reads `/etc/letsencrypt/live/*/privkey.pem`) but
the distributions are owned by `ubuntu`, and their own `setup.sh` chmods these
same files. Root-owned files make `setup.sh` fail with `Operation not permitted`.
Hand ownership back.

```bash
#!/usr/bin/env bash
set -euo pipefail
LIVE=/etc/letsencrypt/live/<hostname>
CP=/opt/wso2/wso2apip-ai-workspace-<ver>
GW=/opt/wso2/wso2apip-ai-gateway-<ver>
CONTAINER_UID=10001
OWNER=ubuntu:ubuntu

place_cert() { install -m 644 -o "${OWNER%:*}" -g "${OWNER#*:}" "$LIVE/fullchain.pem" "$1"; }
place_key()  { install -m 600 -o "${OWNER%:*}" -g "${OWNER#*:}" "$LIVE/privkey.pem" "$1"
               setfacl -m "u:${CONTAINER_UID}:r" "$1" 2>/dev/null || chmod 644 "$1"; }

# Control plane: ONE shared pair. All three services answer on the same
# hostname, so the shipped single-directory mount is correct as-is.
place_cert "$CP/resources/certificates/cert.pem"
place_key  "$CP/resources/certificates/key.pem"

# Gateway: Envoy's listener. Same bytes, different filenames.
# Do NOT touch $GW/resources/certificates/ — that is the upstream trust store.
place_cert "$GW/resources/listener-certs/default-listener.crt"
place_key  "$GW/resources/listener-certs/default-listener.key"

# Certificates are read at startup only — recreate, never restart.
for d in "$CP" "$GW"; do
    [ -f "$d/api-platform.env" ] || continue
    ( cd "$d" && docker compose up -d --force-recreate )
done
```

`cd` into each directory rather than using `docker compose -f` — that is what
makes `.env` and `docker-compose.override.yaml` load.

## The shared-mount question

The shipped control-plane Compose file mounts one `resources/certificates/`
directory into all three services. The product docs suggest splitting it
per-service for production — but that exists to stop two *different* hostnames
clashing in one certificate. **When every service answers on the same hostname,
the shared mount is correct** and needs no change. The only cost is one private
key in three containers.

Splitting the mounts is required only if the user insists on separate hostnames
per component.

## Renewal

Neither the control-plane services nor Envoy reload a certificate while running,
and neither watches the file. **An expired certificate is an outage, not a
warning**, because internal service-to-service hops verify certificates too.

The `certbot` package installs a `certbot.timer` that renews automatically — but
without a deploy hook the renewed certificate never reaches the containers. So
installing the hook is nearly free:

```bash
sudo install -m 755 deploy-certs-remote.sh /etc/letsencrypt/renewal-hooks/deploy/
```

If the user declines automation, record the expiry and tell them recovery is
`certbot renew --force-renewal` followed by the deploy script.

## Verifying

```bash
# from outside; no -k, so success means the chain validates
curl -sS -o /dev/null -w '%{http_code}\n' https://<host>:9643/ai-workspace/

# explicit, on the HOST (macOS LibreSSL mishandles these flags)
openssl s_client -connect <host>:9643 -servername <host> \
  -verify_hostname <host> -verify_return_error </dev/null 2>/dev/null \
  | grep 'Verify return code'
```

Expect `Verify return code: 0 (ok)`. A `404` at a gateway root is normal — TLS
terminated, nothing deployed at `/` yet.
