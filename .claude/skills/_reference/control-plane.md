# Control plane — AI Workspace, Platform API, API Portal

## These are one stack, not three

The `wso2apip-ai-workspace-<ver>` distribution ships **all three** as Compose
profiles over a **single** `configs/config.toml`:

| Profile | Service | Port |
|---|---|---|
| `platform-api` | Platform API — the control plane REST API | 9243 |
| `ai-workspace` | React SPA + Go BFF | 9643 |
| `api-portal` | API Portal & MCP Hub | 9543 |

Each service reads only its own root table (`[platform_api]`, `[ai_workspace]`,
`[api_portal]`) and ignores the others. Selection is `COMPOSE_PROFILES` in `.env`.

**Do not deploy the standalone `wso2apip-api-portal-<ver>` distribution
alongside this one.** It bundles a second Platform API and binds 9243 and 9543,
colliding with this stack. It is only useful for its samples, seeding script and
Portal database schemas.

Ask which components are wanted, then set:

```
COMPOSE_PROFILES=ai-workspace,api-portal,platform-api
```

`platform-api` is mandatory — the other two are clients of it.

## Setup

```bash
cd /opt/wso2/wso2apip-ai-workspace-<ver>
ADMIN_USERNAME=admin ADMIN_PASSWORD='<generated>' ./scripts/setup.sh
```

Generate the password locally and pass it in, rather than letting the script
invent one — it prints it **once** and stores only a bcrypt hash. Save it
somewhere the user keeps.

`setup.sh` writes: a self-signed TLS pair, `resources/keys/` (at-rest encryption
key, JWT keypair, Portal encryption key and session secret), the admin hash in
`api-platform.env`, and pins `COMPOSE_PROJECT_NAME` in `.env`.

**Back up `resources/keys/encryption.key` immediately.** It protects every stored
secret and has no recovery path — a database restored without it yields rows
nobody can decrypt.

**Never run `setup.sh --force` on a live deployment.** It rotates keys, making
existing encrypted data unreadable, and rewrites `api-platform.env`, discarding
anything appended to it.

## Three config edits the shipped file needs

### 1. Remove `ca_file` (if using a public CA)

`[ai_workspace.control_plane] ca_file` points at the old self-signed cert. Let's
Encrypt already chains to a root in the container's trust store, so delete the
line and leave `tls_skip_verify = false`.

### 2. Add `[platform_api.server.https]` — the shipped file omits it entirely

```toml
[platform_api.server.https]
enabled   = true
port      = 9243
cert_file = "/app/data/certs/cert.pem"
key_file  = "/app/data/certs/key.pem"
```

There is no self-signed fallback; a missing certificate stops the listener.

### 3. Add `[api_portal.*]`, but fix the HTTPS block

The shipped `config.toml` has no `[api_portal]` section. Copy it from
`configs/config-template.toml` — **except the HTTPS table**, which the template
ships as `enabled = false` with paths under `./resources/security/` that do not
exist in this stack. Copying it verbatim stops the Portal from starting. Use:

```toml
[api_portal.server.https]
enabled   = true
cert_file = "/etc/api-portal/tls/cert.pem"
key_file  = "/etc/api-portal/tls/key.pem"
```

No `{{ env }}` token exists for those, so it must be a TOML edit.

Also set `role_to_scope_mapping = "./resources/role-to-scope-mapping.yaml"` — the
in-image path. This stack mounts that file into `ai-workspace` and
`platform-api` but **not** into `api-portal`, so an `/etc/api-portal/...` path
will not resolve.

## The organization handle — get it right first time

```toml
[api_portal.organization]
handle       = "default"
display_name = "Default"
```

**Immutable after first start.** It is written into the organization's IDP
reference ID when the row is seeded, and the Management API refuses to change it.
Changing it means discarding the `api-portal-data` volume so the org re-seeds.

It is also **matched against the authenticated user's organization**. The
Platform API's file-auth org is `[platform_api.auth.file.organization] id`,
default `default`. If the two disagree, login fails and the Portal reports it to
the browser as **"Invalid username or password"** — which sends you hunting a
credential problem that does not exist. The real signal is in the Portal log:

```
Rejected login for a user outside this portal's organization {"expected":"<handle>"}
reason: "organization_mismatch"
```

**Default to matching the Platform API's org id** (`default`) unless the user has
a specific reason. If they do want a custom handle, either set it *and* the
Platform API's `file.organization.id` to the same value before first start, or
expect this failure.

With an OIDC provider the claim carrying the org must also equal the handle —
and note the product docs disagree with each other about whether that is
`org_name` (display name) or `org_handle` (slug). If both values are identical
in the IdP, either mapping works; otherwise test and adjust.

## Environment values

Everything below is already `{{ env }}`-tokenised, so it goes in
`api-platform.env` rather than the TOML:

```bash
APIP_AIW_DOMAIN=<host>:9643
APIP_AIW_CONTROL_PLANE_URL=https://<host>:9243
APIP_AIW_GATEWAY_CONTROLPLANE_HOST=<host>:9243
APIP_AP_SERVER_BASE_URL=https://<host>:9543
APIP_AP_AUTH_LOCAL_PLATFORM_API_URL=https://<host>:9243
APIP_AP_AUTH_LOCAL_TLS_SKIP_VERIFY=false
```

`chmod 600`. Compose loads it with `format: raw` because the bcrypt hash contains
`$`; comment lines are handled fine.

**Two of these are display-only or easily confused:**

- `APIP_AIW_DOMAIN` affects only the startup banner.
- `APIP_AIW_GATEWAY_CONTROLPLANE_HOST` is what the Workspace *prints* in gateway
  setup instructions. Nothing connects to it. A wrong value leaves the Workspace
  working and silently breaks every gateway.

## Internal hops must use the public hostname

`[ai_workspace.control_plane] url` and `[api_portal.auth.local] platform_api_url`
default to `https://platform-api:9243` — the Compose service name. A public CA
certificate cannot cover a private name, so with verification on those hops fail.

Point them at the public hostname, then make it resolve locally:

```yaml
services:
  ai-workspace:
    extra_hosts: ["<host>:host-gateway"]
  api-portal:
    extra_hosts: ["<host>:host-gateway"]
```

Public DNS resolves the hostname to the Elastic IP, and **AWS does not hairpin
traffic from an instance back to its own EIP**. `host-gateway` maps it to the
Docker bridge, so the connection stays on the box, the SNI name still matches
the certificate, and verification passes.

Bonus: bridge traffic never traverses the security group, so **9243 can stay
closed** even though three containers use it. Only open 9243 when a gateway runs
on a different host.

Verify:

```bash
docker compose exec ai-workspace getent hosts <host>   # expect 172.17.0.1
```

## Fix the Portal healthcheck

The shipped Compose file healthchecks `api-portal` with
`["CMD","curl","-fk","https://localhost:9543/health"]`, but **`curl` is not in
that image** (it has `wget`, `node`, `nc`). Every probe fails with `executable
file not found`, and the container sits permanently `(unhealthy)` while serving
fine. Override it:

```yaml
    healthcheck:
      test: ["CMD-SHELL", "wget -q --no-check-certificate -O /dev/null https://localhost:9543/health"]
      interval: 30s
      timeout: 5s
      start_period: 15s
      retries: 3
```

## Starting and verifying

```bash
docker compose pull && docker compose up -d
docker compose ps

curl -fsk https://localhost:9243/health      # platform-api
curl -fsk https://localhost:9643/healthz     # ai-workspace
curl -fsk https://localhost:9543/health      # api-portal
```

Health checks and probes use **`localhost`** — they run inside containers or on
the host against published ports. Only the service-to-service hops use the
public hostname. Rewriting a healthcheck to the hostname stalls
`depends_on: service_healthy` and `ai-workspace` never starts.

Confirm the env reached the container and the org seeded as intended:

```bash
docker compose exec ai-workspace env | grep '^APIP_AIW_'
docker compose logs api-portal | grep -i 'organization seeded'
```

## Sessions are in memory

`[ai_workspace.session] store` supports only `memory` in this version. **Any
recreation of `ai-workspace` signs out every logged-in user**, regardless of
database. Warn the user before recreating during a demo.

## Optional: PostgreSQL

`[platform_api.database] driver = "postgres"` with host/port/name/user/password
and `ssl_mode`. Unlike the gateway, **the Platform API applies its own schema on
startup** — no manual DDL. The Portal's own database (`[api_portal.database]`)
does need its schema applied by hand, and those scripts ship only in the
standalone Portal distribution.

Note the key-name inconsistency across components: Platform API and Portal use
`name` and `ssl_mode`; the gateway uses `database` and `sslmode`.
