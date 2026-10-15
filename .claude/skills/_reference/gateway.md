# Gateway — AI Gateway and API Gateway

`wso2apip-ai-gateway-<ver>` and `wso2apip-api-gateway-<ver>` are the same shape:
a **gateway-controller** (talks to the control plane, owns the database) and a
**gateway-runtime** (Envoy plus the policy engine, carries every request). The
procedure below applies to both.

A gateway registers to a control plane — either a self-hosted AI Workspace or a
SaaS one. It needs no control plane to *serve* traffic; it keeps routing when the
control plane is unreachable, it just stops receiving configuration updates.

## Setup

```bash
cd /opt/wso2/wso2apip-ai-gateway-<ver>
ADMIN_USERNAME=admin ADMIN_PASSWORD='<generated>' ./scripts/setup.sh
```

Writes the AES-256 at-rest key, keeps the shipped self-signed listener
certificate, generates the controller admin credential, and pins
`COMPOSE_PROJECT_NAME`. Generate the password locally — it prints once.

**Ordering note:** if certificates were already placed into
`resources/listener-certs/` as root-owned files, `setup.sh` fails with
`chmod: Operation not permitted`. Either run `setup.sh` first, or ensure the
cert-deploy script chowns to `ubuntu` (see `tls-and-certs.md`).

**Never `setup.sh --force` afterwards.** It rotates the AES key — making
everything encrypted in the controller database unreadable — *and* rewrites
`api-platform.env`, discarding the control-plane host and registration token.

## Four config changes the shipped file needs

```toml
# 1. Ships enabled = true with a PLACEHOLDER Moesif ID, which logs a publish
#    error on every single request. Disable, or supply a real ID.
[analytics]
enabled = false
# with a real key:
#   enabled = true
#   enabled_publishers = ["moesif"]
#   [analytics.publishers.moesif]
#   application_id = '{{ env "APIP_GW_ANALYTICS_PUBLISHERS_MOESIF_APPLICATION_ID" "" }}'

# 2. Ships as "true". This channel carries the registration token and every
#    configuration update. A publicly-trusted control-plane certificate needs no
#    trust plumbing, so turn verification on.
[controller.controlplane]
insecure_skip_verify = '{{ env "APIP_GW_CONTROLLER_CONTROLPLANE_INSECURE_SKIP_VERIFY" "false" }}'

# 3. gateway_id must be UNIQUE across the estate. Default is
#    "platform-gateway-id" — change it for any second gateway, and give
#    gateway_name a distinct value too.
[controller.server]
gateway_id = '{{ env "APIP_GW_CONTROLLER_SERVER_GATEWAY_ID" "<unique-id>" }}'
```

4. **Raise the resource limits.** The shipped Compose values are quickstart
   only — 60 MB / 0.025 CPU for the controller, 180 MB / 0.175 for the runtime.
   Put these in `docker-compose.override.yaml`, not the shipped file:

```yaml
services:
  gateway-controller:
    mem_limit: 2g
    mem_reservation: 1g
    cpus: 1.0
  gateway-runtime:
    mem_limit: 2g
    mem_reservation: 2g
    cpus: 2.0
```

The runtime carries every request, and body-inspecting policies (PII masking,
JSON schema and URL guardrails) buffer full request and response bodies — far
larger for LLM traffic than typical REST payloads.

## Reaching the control plane

`[controller.controlplane] host` is a bare `host:port`, **no scheme, no path**.
Port 443 is assumed if omitted. It is used directly as the HTTPS and WebSocket
authority.

Both values come from the control plane's **Get Started** panel when the gateway
is registered there:

```bash
cd /opt/wso2/wso2apip-ai-gateway-<ver>
umask 077
read -rsp "Registration token: " GW_TOKEN && echo
printf 'APIP_GW_CONTROLLER_CONTROLPLANE_HOST=%s\n' '<host:port>' >> api-platform.env
printf 'APIP_GW_CONTROLLER_CONTROLPLANE_TOKEN=%s\n' "$GW_TOKEN" >> api-platform.env
unset GW_TOKEN
chmod 600 api-platform.env
```

Read the token with `read -rsp` — it keeps it out of shell history and the
process list.

**The token is shown once and is single-use.** Losing it means clicking
**Reconfigure** in the control plane, which revokes the old token and disconnects
the running gateway. Treat a reconfigure as downtime for that gateway.

**Pre-flight before spending the token.** A failed registration burns it. Test
the exact path the container will take, with verification on:

```bash
docker run --rm curlimages/curl:latest -sS --max-time 10 https://<cp-host>/health
# add --add-host <cp-host>:host-gateway if the control plane is on this same host
```

### Same host as the control plane

Use the public hostname with an `extra_hosts` mapping so it resolves over the
Docker bridge — AWS will not hairpin to the instance's own Elastic IP:

```yaml
services:
  gateway-controller:
    extra_hosts:
      - "host.docker.internal:host-gateway"
      - "<cp-host>:host-gateway"
```

This keeps `insecure_skip_verify = false` working and means port 9243 need not be
open publicly.

### Different host, or a SaaS control plane

Ordinary DNS applies; no `extra_hosts` needed. A SaaS control plane presents a
publicly-trusted certificate, so verification works with no extra trust setup.
The connection is **outbound only** — the control plane never calls the gateway,
so no inbound rule is required for it.

## Registration is the user's step

They must create the gateway in the control-plane UI and give you the host and
token. Ask for both; do not guess. The gateway URL they enter should be
`https://<host>:8443`.

## Storage

`sqlite` (default) needs nothing — the controller creates and migrates it. Data
lives in the `controller-data` volume.

### PostgreSQL — the schema is NOT auto-applied

The controller connects to an external database but runs **no DDL** against it;
auto-applying DDL to an operator-owned database is treated as a security risk.
Four steps, and **the order matters**:

```bash
# 1. database + login, as an admin account
psql "host=<db> port=5432 dbname=postgres user=<admin> sslmode=require" \
  -c "CREATE DATABASE gateway_controller;" \
  -c "CREATE USER gateway WITH PASSWORD '<pw>';"

# 2. schema, as the ADMIN (needs DDL), aborting on first error
psql "host=<db> port=5432 dbname=gateway_controller user=<admin> sslmode=require" \
  -v ON_ERROR_STOP=1 -f resources/gateway-controller/db-scripts/gateway-controller-db.postgres.sql

# 3. grants — AFTER the schema, because ALL TABLES only covers existing tables
psql "host=<db> port=5432 dbname=gateway_controller user=<admin> sslmode=require" \
  -c "GRANT CONNECT ON DATABASE gateway_controller TO gateway;" \
  -c "GRANT USAGE ON SCHEMA public TO gateway;" \
  -c "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO gateway;"

# 4. verify — expect exactly 15 tables
psql "..." -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
```

The database is **`gateway_controller`**, not `gateway`. Running grants before
the schema is the documented failure mode — it surfaces later as
`permission denied for table …` at runtime.

The schema uses no sequences, serials or extensions, so those grants are
complete. Confirm the runtime user has DML but **no DDL**:

```bash
psql "...user=gateway..." -c "CREATE TABLE x (i int);"   # must fail
```

Then add `[controller.storage.postgres]` — the shipped `config.toml` carries only
`[controller.storage.sqlite]`. **Key names differ from the Platform API's**: it is
`database` not `name`, and `sslmode` as one lowercase word, not `ssl_mode`.

```bash
APIP_GW_CONTROLLER_STORAGE_TYPE=postgres
APIP_GW_CONTROLLER_STORAGE_POSTGRES_HOST=<endpoint>
APIP_GW_CONTROLLER_STORAGE_POSTGRES_DATABASE=gateway_controller
APIP_GW_CONTROLLER_STORAGE_POSTGRES_USER=gateway
APIP_GW_CONTROLLER_STORAGE_POSTGRES_PASSWORD=<pw>
APIP_GW_CONTROLLER_STORAGE_POSTGRES_SSLMODE=require
```

`require` encrypts without validating the server certificate; `verify-full`
additionally needs the CA bundle mounted into the container.

An external database is also what allows more than one controller replica —
SQLite cannot.

## Verifying

```bash
docker compose up -d
docker compose ps

# registration
docker compose logs gateway-controller | grep -iE "Connection state|established|register"
#   from=connecting to=connected
#   Control plane connection established gateway_id=...

# storage — for Postgres, expect "PostgreSQL storage initialized"
docker compose logs gateway-controller | grep -iE "storage|postgres"

# Envoy readiness — from the HOST, the runtime image has no curl or wget
curl -fsk https://localhost:8443/_gateway-health/ready     # {"status":"ready"}
curl -s  http://localhost:9901/listeners
```

The control plane shows the gateway moving from **Not Active** to **Active**.

A `404` at `https://<host>:8443/` is correct — TLS terminated, nothing deployed
at `/` yet.

One benign `WARN` at startup: `Failed to check if certificate exists …
configuration not found` is the controller's certificate store initialising
against an empty database.

## Version skew against a control plane

If the control plane is older than the gateway, registration and deployment can
work while two sync calls fail:

```
404 on /api/internal/v1/artifacts/import-gateway-artifacts
401 on the platform secrets fetch
```

Both are cosmetic if artifacts still deploy — check for
`Runtime deploy config added` and `Deployment ack sent … status=success`. The
control plane's Get Started panel shows which gateway versions it supports; if it
only offers an older one, expect these warnings.

## Debugging traffic

Two components share the runtime container, told apart by prefix:

- `[rtr]` — Envoy, including the per-request access log
- `[pol]` — the policy engine: guardrails, rate limits, token accounting

```bash
docker compose logs gateway-runtime 2>&1 | grep '\[rtr\]'
```

In the access log, `direct_response` means Envoy answered itself and no upstream
was involved. Once a provider or proxy is deployed, the final field carries the
real upstream host — the quickest confirmation that routing works.

Raise Envoy's log level live, no restart:

```bash
curl -XPOST "localhost:9901/logging?level=debug"    # ?level=info to revert
```

For per-request structured JSON, set `[traffic_logging] enabled = true` and
`up -d`.

## Diagnosing a 404 through the gateway

Check in this order — the routes are in Envoy's config, so read it rather than
guessing:

```bash
curl -s "http://localhost:9901/config_dump?resource=dynamic_route_configs"
```

1. **Is the route there at all?** If not, the artifact did not deploy.
2. **Does the vhost match?** Usually `*` with domains `['*','*:*']`, matching
   everything — so this is rarely the cause.
3. **Is the 404 body Envoy's or the upstream's?** A JSON body like
   `{"code":"404","message":"Not Found","description":"The requested resource is
   not available."}` is a **relayed upstream 404**, not a routing failure.

The common real cause is a **misconfigured upstream URL** in the artifact. For an
MCP proxy the upstream must be the full MCP endpoint including the `/mcp` suffix
— configuring the service base URL alone routes correctly and 404s at the
upstream. Test the upstream directly to confirm:

```bash
curl -sS -X POST "<upstream>/mcp" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"t","version":"1"}}}'
```

## Key managers — where `system.keyManagers` actually lives

The `jwt-auth` and `mcp-auth` policies expose an `issuers` / key-manager
parameter whose default is `${config.policy_configurations.<policy>_v1.keymanagers}`.
That is **gateway config**, not a control-plane resource: it is not in the
Platform API, not in the controller management API, and **not in the shipped
config template**, which is why it looks unresolvable. Add it by hand:

```toml
[[policy_configurations.jwtauth_v1.keymanagers]]
name   = "asgardeo"
issuer = "https://api.asgardeo.io/t/<org>/oauth2/token"
[policy_configurations.jwtauth_v1.keymanagers.jwks.remote]
uri = "https://api.asgardeo.io/t/<org>/oauth2/jwks"

[[policy_configurations.jwtauth_v1.keymanagers]]
name   = "entra"
issuer = "https://login.microsoftonline.com/<tenant-id>/v2.0"
[policy_configurations.jwtauth_v1.keymanagers.jwks.remote]
uri = "https://login.microsoftonline.com/<tenant-id>/discovery/v2.0/keys"
```

**One list for both.** `mcp-auth` (v1.4.0, and the `mcp-auth-passthrough` /
`gateway-mcp-auth` forks) reads `policy_configurations.jwtauth_v1.keymanagers`
— checked in the policy definitions. Blocks under `mcpauth_v1.keymanagers`
are ignored.

- Take `issuer` and the JWKS URI **verbatim from the provider's OIDC discovery
  document** (`/.well-known/openid-configuration`). Entra's v2.0 issuer ends
  `/v2.0`, and its token *endpoint* is not its issuer — using the endpoint is the
  easy mistake.
- `mcp-auth`'s `gatewayhost` is the **host only, no port**. It appends the
  listener port itself, so `host:8443` produces `host:8443:8443`.
- `jwt-auth` reads the header named by `headername` (default `Authorization`);
  `mcp-auth` uses `Authorization`. If they differ, a token that works against one
  policy 401s against the other and the log does not say why.

**Unknown keys are ignored silently.** The policy definitions declare
`additionalProperties: false`, but the engine neither applies nor rejects a key
it does not know. A setting that appears to do nothing may not exist — verify
against the policy definition rather than assuming it took effect.

Verify without a token by fetching the protected-resource metadata the policy
serves; a 401 carrying a `WWW-Authenticate` header means the policy is active.
Not every upstream advertises metadata — some return a plain-text 401 instead.

## Observability profiles

`--profile metrics` (Prometheus 9092, Grafana 3000), `--profile logging`
(OpenSearch 9200, Dashboards 5601, Fluent Bit), `--profile tracing` (Jaeger
16686). All those ports should stay closed; reach them over an SSH tunnel.

### Fix the shipped observability configs BEFORE starting these profiles

Two shipped debug settings make the logging profile consume the disk. Left alone,
this measured **~700,000 documents/minute** — 1.2 GB in twenty minutes — of which
99.97% was the observability stack logging about its own logging.

**1. Fluent Bit tails its own log.** `observability/fluent-bit/fluent-bit.conf`
ships the Elasticsearch OUTPUT with `trace_output On` and `trace_error On`, which
print a line per record shipped. Those lines go to Fluent Bit's stdout, which its
own `tail` input is reading, which produces more records. It is a genuine
exponential feedback loop, not merely chatty logging. **Delete both lines.**

**2. The OTel collector dumps every span.** `observability/otel-collector/config.yaml`
ships a `debug` exporter at `verbosity: detailed` in the traces pipeline, printing
each span in full to stdout — where Fluent Bit picks it up. Remove `debug` from
`service.pipelines.traces.exporters`, leaving `[otlp]`.

Then exclude the observability containers from ingestion. Filter on `component`,
which the shipped `add_component.lua` sets to the container **name** — container
IDs change on every recreate, so an `Exclude_Path` glob is not durable. It must
come after the lua filter and before the OUTPUT:

```
[FILTER]
    Name grep
    Match docker.*
    Exclude component (fluent-bit|otel-collector)
```

After that: ~2 documents/minute at idle, and exactly one document per gateway
request. Verify by request count rather than by eye:

```bash
curl -s "http://localhost:9200/gateway-logs-*/_search?size=0" -H 'Content-Type: application/json' \
  -d '{"query":{"range":{"@timestamp":{"gte":"now-5m"}}},
       "aggs":{"c":{"terms":{"field":"component.keyword","size":10}}}}'
```

If `fluent-bit-1` tops that aggregation, the loop is still running.

**`docker compose --profile <name> down` tears down the entire project**, gateway
included, not just that profile. Use `docker compose stop <service>`.

### Tracing needs a `[tracing]` block and a forced recreate

```toml
[tracing]
enabled               = true
endpoint              = "otel-collector:4317"   # gRPC, no scheme
service_version       = "0.2.0"
batch_timeout         = "1s"
max_export_batch_size = 512
sampling_rate         = 1.0                     # lower for production
```

`docker compose --profile tracing up -d` starts the collector but **does not
recreate the gateway** — Compose cannot see that a bind-mounted `config.toml`
changed, so the running policy engine keeps its old config and no traces appear.
Use `docker compose up -d --force-recreate gateway-controller gateway-runtime`,
naming the services so unrelated containers are left alone.

Confirm in the log, not by assumption:

```bash
docker compose logs gateway-runtime --since 3m 2>&1 | grep -i 'OTLP\|tracer'
#   Initializing OTLP exporter endpoint=otel-collector:4317
#   OpenTelemetry tracer initialized successfully

curl -s http://localhost:16686/api/services      # expect: router, policy-engine
```

The gateway emits two services: `router` (Envoy) and `policy-engine`.
